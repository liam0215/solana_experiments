#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $# -eq 3 && $1 == experiment ]]; then
  case "$2" in
    validate|run) exec python3 "$root/experiment.py" "$2" "$3" ;;
    *) echo 'Usage: workspace.sh experiment validate|run CONFIG.toml' >&2; exit 2 ;;
  esac
fi
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 build|init-ledger [2|3]|validator [1|2|3]|bench [1|2|3]|all|experiment validate|run CONFIG.toml" >&2
  exit 2
fi

case "$1" in
  build|init-ledger|validator|bench|all) action=$1 ;;
  *) echo "Choose build, init-ledger, validator, bench, or all" >&2; exit 2 ;;
esac
instance=${2:-}
if [[ -n $instance ]]; then
  case "$action:$instance" in
    init-ledger:2|init-ledger:3|validator:1|validator:2|validator:3|bench:1|bench:2|bench:3) ;;
    *) echo "Invalid instance for $action (use 1, 2, or 3; init-ledger accepts 2 or 3)" >&2; exit 2 ;;
  esac
fi

if [[ -f $root/workspace.env ]]; then
  # shellcheck source=/dev/null
  source "$root/workspace.env"
fi
if [[ -f $root/agave/packaging/junction-qat/local.env ]]; then
  echo "Remove agave/packaging/junction-qat/local.env (or use the Agave scripts directly); it overrides parent settings" >&2
  exit 1
fi

export SOLANA_BANKING_THREADS="${SOLANA_BANKING_THREADS:-8}"
export SOL_SIGVERIFY_THREADS="${SOL_SIGVERIFY_THREADS:-18}"

for variable in JUNCTION_RUN JUNCTION_LD_PATH BENCH_DURATION BENCH_TX_COUNT RUST_LOG; do
  if [[ -v $variable ]]; then
    export "$variable"
  fi
done

export QATLIB_SRC="$root/qat_driver"
export QATLIB_BUILD="${QATLIB_BUILD:-$QATLIB_SRC/build}"
export JUNCTION_VALIDATOR_CONFIG="${JUNCTION_VALIDATOR_CONFIG:-$root/configs/validator.config}"
export JUNCTION_BENCH_CONFIG="${JUNCTION_BENCH_CONFIG:-$root/configs/bench.config}"

if [[ $action == validator || ( $action == bench && -z $instance ) ]]; then
  [[ -x ${JUNCTION_RUN:-} && ! -d ${JUNCTION_RUN:-} ]] || {
    echo "Missing executable junction_run: ${JUNCTION_RUN:-<set JUNCTION_RUN in workspace.env>}" >&2
    exit 1
  }
  [[ -f $QATLIB_BUILD/libqat_s.so ]] || {
    echo "Missing QAT driver library: $QATLIB_BUILD/libqat_s.so" >&2
    exit 1
  }
  [[ -x /usr/bin/fish ]] || {
    echo "Missing /usr/bin/fish" >&2
    exit 1
  }
fi

if [[ $action == validator || ( $action == bench && -z $instance ) ]]; then
  if [[ $instance == 2 || $instance == 3 ]]; then
    config_var="JUNCTION_VALIDATOR_CONFIG_$instance"
    export JUNCTION_VALIDATOR_CONFIG="${!config_var:-$root/configs/validator-$instance.config}"
  fi
  [[ -f $JUNCTION_VALIDATOR_CONFIG ]] || {
    echo "Missing validator Junction config: $JUNCTION_VALIDATOR_CONFIG" >&2
    exit 1
  }
  validator_host=$(awk '$1 == "host_addr" { print $2; exit }' "$JUNCTION_VALIDATOR_CONFIG")
  [[ -n $validator_host ]] || {
    echo "Missing host_addr in $JUNCTION_VALIDATOR_CONFIG" >&2
    exit 1
  }
  if [[ $instance != 2 && $instance != 3 && -n ${MASTER_HOST:-} && $MASTER_HOST != "$validator_host" ]]; then
    echo "MASTER_HOST must match host_addr ($validator_host) in $JUNCTION_VALIDATOR_CONFIG" >&2
    exit 1
  fi
  export MASTER_HOST="$validator_host"
fi

if [[ $action == bench && -n $instance ]]; then
  if [[ $instance == 1 ]]; then
    config=$JUNCTION_VALIDATOR_CONFIG
    cpus=5,7,9,11,13,15,17,19
    identity="$root/agave/config/bootstrap-validator/identity.json"
  else
    config_var="JUNCTION_VALIDATOR_CONFIG_$instance"
    config="${!config_var:-$root/configs/validator-$instance.config}"
    identity="$root/agave/config/bootstrap-validator-$instance/identity.json"
    if [[ $instance == 2 ]]; then
      cpus=21,23,25,27,29,31,33,35
    else
      cpus=37,39,41,43,45,47,49,51
    fi
  fi
  [[ -f $config ]] || { echo "Missing validator Junction config: $config" >&2; exit 1; }
  validator_host=$(awk '$1 == "host_addr" { print $2; exit }' "$config")
  [[ -n $validator_host ]] || { echo "Missing host_addr in $config" >&2; exit 1; }
  bin="$root/agave/target/release-with-debug/solana-bench-tps"
  [[ -x $bin && -r $identity ]] || { echo "Missing $bin or $identity; build and init-ledger first" >&2; exit 1; }
  command -v numactl >/dev/null && command -v taskset >/dev/null || {
    echo 'Numbered benchmarks require numactl and taskset' >&2
    exit 1
  }
  [[ -f $QATLIB_BUILD/libqat_s.so ]] || { echo "Missing QAT driver library: $QATLIB_BUILD/libqat_s.so" >&2; exit 1; }
  export LD_LIBRARY_PATH="$QATLIB_BUILD${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

case "$action" in
  all)
    exec bash "$root/run-three.sh"
    ;;
  build)
    exec bash "$root/agave/packaging/junction-qat/build.sh"
    ;;
  init-ledger)
    if [[ -n $instance ]]; then
      exec bash "$root/init-instance-ledger.sh" "$instance"
    fi
    bash "$root/agave/packaging/junction-qat/init-ledger.sh"
    # Genesis has exited successfully; do not unlink a lock on validator start.
    lock="$root/agave/config/bootstrap-validator/rocksdb/LOCK"
    if [[ -e $lock || -L $lock ]]; then
      [[ -f $lock && ! -L $lock ]] || {
        echo "Refusing to remove non-regular RocksDB lock: $lock" >&2
        exit 1
      }
      if command -v fuser >/dev/null 2>&1 && fuser -s "$lock"; then
        echo "Refusing to remove an open RocksDB lock: $lock" >&2
        exit 1
      fi
      rm -- "$lock"
    fi
    ;;
  validator)
    if [[ $instance == 2 || $instance == 3 ]]; then
      ledger="$root/agave/config/bootstrap-validator-$instance"
      [[ -r $ledger/genesis.bin && -r $ledger/identity.json && -r $ledger/vote-account.json && -r $root/agave/config/faucet-$instance.json ]] || {
        echo "Missing instance $instance ledger/keys; run bash workspace.sh init-ledger $instance" >&2
        exit 1
      }
      ld_path="${JUNCTION_LD_PATH:-$QATLIB_BUILD}"
      [[ -f $ld_path/libqat_s.so ]] || { echo "Missing QAT driver library: $ld_path/libqat_s.so" >&2; exit 1; }
      args=(--env "AGAVE_ROOT=$root/agave" --env "QATLIB_BUILD=$QATLIB_BUILD"
            --env "MASTER_HOST=$MASTER_HOST" --env "INSTANCE=$instance")
      for variable in SOLANA_BANKING_THREADS SOL_SIGVERIFY_THREADS RUST_LOG; do
        if [[ -v $variable ]]; then
          args+=(--env "$variable=${!variable}")
        fi
      done
      exec sudo "$JUNCTION_RUN" "$JUNCTION_VALIDATOR_CONFIG" "${args[@]}" --ld_path "$ld_path" -- \
        /usr/bin/fish "$root/validator-instance.fish"
    fi
    exec bash "$root/agave/packaging/junction-qat/run-junction.sh" validator
    ;;
  bench)
    if [[ -n $instance ]]; then
      exec bash "$root/bench-summary.sh" numactl --membind=1 taskset -c "$cpus" "$bin" \
        --url "http://$validator_host:8899" --entrypoint "$validator_host:8001" \
        --faucet "$validator_host:9900" --duration "${BENCH_DURATION:-50}" \
        --tx-count "${BENCH_TX_COUNT:-50000}" --thread-batch-sleep-ms 0 \
        --bind-address 127.0.0.1 --client-node-id "$identity"
    fi
    exec bash "$root/bench-summary.sh" bash "$root/agave/packaging/junction-qat/run-junction.sh" bench
    ;;
esac
