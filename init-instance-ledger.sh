#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
instance=${1:?Specify instance 2 or 3}
[[ $instance == 2 || $instance == 3 ]] || { echo 'Specify instance 2 or 3' >&2; exit 2; }

bin="$root/agave/target/release-with-debug"
for program in solana-keygen solana-genesis; do
  [[ -x $bin/$program ]] || { echo "Build $program first: bash workspace.sh build" >&2; exit 1; }
done

ledger="$root/agave/config/bootstrap-validator-$instance"
faucet="$root/agave/config/faucet-$instance.json"
[[ ! -e $ledger && ! -L $ledger && ! -e $faucet && ! -L $faucet ]] || {
  echo "Refusing to replace existing $ledger or $faucet" >&2
  exit 1
}
umask 077
export LD_LIBRARY_PATH="${QATLIB_BUILD:-$root/qat_driver/build}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$ledger"
"$bin/solana-keygen" new --no-passphrase --silent --outfile "$faucet"
for key in identity stake-account vote-account; do
  "$bin/solana-keygen" new --no-passphrase --silent --outfile "$ledger/$key.json"
done
cd "$root/agave"
"$bin/solana-genesis" \
  --ledger "$ledger" \
  --faucet-pubkey "$faucet" \
  --faucet-lamports 500000000000000000 \
  --hashes-per-tick auto \
  --cluster-type development \
  --enable-warmup-epochs \
  --bootstrap-validator "$ledger/identity.json" "$ledger/vote-account.json" "$ledger/stake-account.json"

# Genesis has exited; refuse to unlink a live/non-regular RocksDB lock.
lock="$ledger/rocksdb/LOCK"
if [[ -e $lock || -L $lock ]]; then
  [[ -f $lock && ! -L $lock ]] || { echo "Refusing to remove non-regular lock: $lock" >&2; exit 1; }
  if command -v fuser >/dev/null 2>&1 && fuser -s "$lock"; then
    echo "Refusing to remove open lock: $lock" >&2
    exit 1
  fi
  rm -- "$lock"
fi
