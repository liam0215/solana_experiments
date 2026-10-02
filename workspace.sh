#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 build|init-ledger|validator|bench" >&2
  exit 2
fi

case "$1" in
  build|init-ledger|validator|bench) action=$1 ;;
  *) echo "Choose build, init-ledger, validator, or bench" >&2; exit 2 ;;
esac

if [[ -f $root/workspace.env ]]; then
  # shellcheck source=/dev/null
  source "$root/workspace.env"
fi
if [[ -f $root/agave/packaging/junction-qat/local.env ]]; then
  echo "Remove agave/packaging/junction-qat/local.env (or use the Agave scripts directly); it overrides parent settings" >&2
  exit 1
fi

for variable in JUNCTION_RUN JUNCTION_LD_PATH BENCH_DURATION BENCH_TX_COUNT SOLANA_BANKING_THREADS SOL_SIGVERIFY_THREADS RUST_LOG; do
  if [[ -v $variable ]]; then
    export "$variable"
  fi
done

export QATLIB_SRC="$root/qat_driver"
export QATLIB_BUILD="${QATLIB_BUILD:-$QATLIB_SRC/build}"
export JUNCTION_VALIDATOR_CONFIG="${JUNCTION_VALIDATOR_CONFIG:-$root/configs/validator.config}"
export JUNCTION_BENCH_CONFIG="${JUNCTION_BENCH_CONFIG:-$root/configs/bench.config}"

if [[ $action == validator || $action == bench ]]; then
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
  [[ -f $JUNCTION_VALIDATOR_CONFIG ]] || {
    echo "Missing validator Junction config: $JUNCTION_VALIDATOR_CONFIG" >&2
    exit 1
  }
  validator_host=$(awk '$1 == "host_addr" { print $2; exit }' "$JUNCTION_VALIDATOR_CONFIG")
  [[ -n $validator_host ]] || {
    echo "Missing host_addr in $JUNCTION_VALIDATOR_CONFIG" >&2
    exit 1
  }
  if [[ -n ${MASTER_HOST:-} && $MASTER_HOST != "$validator_host" ]]; then
    echo "MASTER_HOST must match host_addr ($validator_host) in $JUNCTION_VALIDATOR_CONFIG" >&2
    exit 1
  fi
  export MASTER_HOST="$validator_host"
fi

case "$action" in
  build)
    exec bash "$root/agave/packaging/junction-qat/build.sh"
    ;;
  init-ledger)
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
  validator|bench)
    exec bash "$root/agave/packaging/junction-qat/run-junction.sh" "$action"
    ;;
esac
