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
  build|init-ledger)
    exec bash "$root/agave/packaging/junction-qat/$action.sh"
    ;;
  validator|bench)
    exec bash "$root/agave/packaging/junction-qat/run-junction.sh" "$action"
    ;;
esac
