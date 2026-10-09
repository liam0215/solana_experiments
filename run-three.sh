#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $# -eq 0 ]] || { echo "Usage: $0" >&2; exit 2; }
for tool in curl jq; do
  command -v "$tool" >/dev/null || { echo "Missing $tool (needed to check validator stake)" >&2; exit 1; }
done
if [[ -f $root/workspace.env ]]; then
  # shellcheck source=/dev/null
  source "$root/workspace.env"
fi
ready_timeout=${VALIDATOR_READY_TIMEOUT:-300}
rpc_timeout=${VALIDATOR_RPC_TIMEOUT:-30}
[[ $ready_timeout =~ ^[1-9][0-9]*$ && $rpc_timeout =~ ^[1-9][0-9]*$ ]] || {
  echo 'VALIDATOR_READY_TIMEOUT and VALIDATOR_RPC_TIMEOUT must be positive numbers of seconds' >&2
  exit 2
}

umask 077
tmp_base=${TMPDIR:-/tmp/opencode}
[[ -d $tmp_base ]] || tmp_base=/tmp
logs=$(mktemp -d "$tmp_base/solana-three.XXXXXXXX")
echo "Logs: $logs"

declare -a validators benches averages
# Give each background job its own process group. Cleanup then signals its
# entire tree, including the faucet and benchmark subprocesses.
set -m
cleanup() {
  trap - EXIT INT TERM
  local pid
  for pid in "${benches[@]}" "${validators[@]}"; do
    if [[ -n $pid ]]; then
      kill -TERM -- "-$pid" 2>/dev/null || true
    fi
  done
  for pid in "${benches[@]}" "${validators[@]}"; do
    if [[ -n $pid ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

read_average() {
  awk '
    match($0, /Average TPS: *[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?/) {
      value = substr($0, RSTART, RLENGTH)
      sub(/^Average TPS: */, "", value)
    }
    END { if (value != "") print value }
  ' "$1"
}

wait_for_stake() {
  local i=$1 config_var config host deadline rpc_deadline last_report response status seen_rpc=0
  config_var="JUNCTION_VALIDATOR_CONFIG_$i"
  if [[ $i == 1 ]]; then
    config=${JUNCTION_VALIDATOR_CONFIG:-$root/configs/validator.config}
  else
    config=${!config_var:-$root/configs/validator-$i.config}
  fi
  [[ -f $config ]] || { echo "Missing validator Junction config: $config" >&2; return 1; }
  host=$(awk '$1 == "host_addr" { print $2; exit }' "$config")
  [[ -n $host ]] || { echo "Missing host_addr in $config" >&2; return 1; }

  echo "Waiting for validator $i at $host to report active stake..."
  deadline=$((SECONDS + ready_timeout))
  rpc_deadline=$((SECONDS + rpc_timeout))
  last_report=$SECONDS
  while (( SECONDS < deadline )); do
    if ! kill -0 "${validators[$i]}" 2>/dev/null; then
      echo "Validator $i exited; see $logs/validator-$i.log" >&2
      return 1
    fi
    if response=$(curl --noproxy '*' --silent --show-error --fail --connect-timeout 1 --max-time 2 \
        --header 'Content-Type: application/json' \
        --data '{"jsonrpc":"2.0","id":1,"method":"getVoteAccounts"}' \
        "http://$host:8899" 2>&1); then
      if jq -e '.result.current | type == "array"' >/dev/null 2>&1 <<<"$response"; then
        seen_rpc=1
        if jq -e 'any(.result.current[]; (.activatedStake | tonumber) > 0)' >/dev/null 2>&1 <<<"$response"; then
          echo "Validator $i has active stake"
          return 0
        fi
        status='RPC reachable; no current vote account with active stake yet'
      else
        status='RPC returned an error or unexpected getVoteAccounts response'
      fi
    else
      status="RPC unreachable: $response"
    fi
    if (( SECONDS - last_report >= 10 )); then
      echo "Validator $i: $status" >&2
      last_report=$SECONDS
    fi
    if (( ! seen_rpc && SECONDS >= rpc_deadline )); then
      echo "Validator $i RPC at $host:8899 did not return vote accounts within ${rpc_timeout}s: $status" >&2
      if command -v ip >/dev/null; then
        ip route get "$host" >&2 || true
      fi
      return 1
    fi
    sleep 1
  done
  echo "Validator $i did not report active stake within ${ready_timeout}s; see $logs/validator-$i.log" >&2
  return 1
}

# Run the solo baseline with the same validator 1 that will be used in the
# three-validator phase; keep it running so its ledger is never restarted.
bash "$root/workspace.sh" validator 1 >"$logs/validator-1.log" 2>&1 &
validators[1]=$!
echo "Validator 1 started (PID ${validators[1]}; uncontended baseline)"

sleep 5
wait_for_stake 1

bash "$root/workspace.sh" bench 1 >"$logs/bench-1-uncontended.log" 2>&1 &
benches[1]=$!
echo "Uncontended benchmark 1 started (PID ${benches[1]})"
if wait "${benches[1]}"; then
  unset 'benches[1]'
else
  echo "Uncontended benchmark 1 failed; see $logs/bench-1-uncontended.log" >&2
  exit 1
fi
if ! kill -0 "${validators[1]}" 2>/dev/null; then
  echo "Validator 1 exited during the baseline; see $logs/validator-1.log" >&2
  exit 1
fi
uncontended=$(read_average "$logs/bench-1-uncontended.log")
if [[ -z $uncontended ]]; then
  echo "No Average TPS found for the baseline; see $logs/bench-1-uncontended.log" >&2
  exit 1
fi
echo "Uncontended benchmark 1 Average TPS: $uncontended"

for i in 2 3; do
  bash "$root/workspace.sh" validator "$i" >"$logs/validator-$i.log" 2>&1 &
  validators[$i]=$!
  echo "Validator $i started (PID ${validators[$i]})"
done

sleep 5
for i in 1 2 3; do
  wait_for_stake "$i"
done

for i in 1 2 3; do
  bash "$root/workspace.sh" bench "$i" >"$logs/bench-$i.log" 2>&1 &
  benches[$i]=$!
  echo "Contended benchmark $i started (PID ${benches[$i]})"
done

failed=0
for i in 1 2 3; do
  if wait "${benches[$i]}"; then
    unset 'benches[i]'
  else
    echo "Benchmark $i failed; see $logs/bench-$i.log" >&2
    failed=1
  fi
done
(( failed == 0 )) || exit 1

for i in 1 2 3; do
  if ! kill -0 "${validators[$i]}" 2>/dev/null; then
    echo "Validator $i exited during benchmarking; see $logs/validator-$i.log" >&2
    exit 1
  fi
  averages[$i]=$(read_average "$logs/bench-$i.log")
  if [[ -z ${averages[$i]} ]]; then
    echo "No Average TPS found for benchmark $i; see $logs/bench-$i.log" >&2
    exit 1
  fi
  echo "Contended benchmark $i Average TPS: ${averages[$i]}"
done

awk -v u="$uncontended" -v a="${averages[1]}" -v b="${averages[2]}" -v c="${averages[3]}" '
  BEGIN {
    lowest = highest = a
    if (b < lowest) lowest = b
    if (c < lowest) lowest = c
    if (b > highest) highest = b
    if (c > highest) highest = c

    mean = (a + b + c) / 3
    printf "\033[1;31mContended mean TPS (3 benchmarks): %.2f\033[0m\n", mean
    if (highest == 0) {
      percentage = "undefined (highest TPS is zero)"
    } else {
      percentage = sprintf("%.2f%%", lowest / highest * 100)
    }
    printf "\033[1;31mLowest TPS as %% of highest: %s\033[0m\n", percentage
    if (u == 0) {
      baseline_ratio = "undefined (uncontended TPS is zero)"
      validator_ratio = baseline_ratio
    } else {
      baseline_ratio = sprintf("%.2f%%", mean / u * 100)
      validator_ratio = sprintf("%.2f%%", a / u * 100)
    }
    printf "\033[1;31mContended mean as %% of uncontended TPS: %s\033[0m\n", baseline_ratio
    printf "\033[1;31mContended validator 1 as %% of its uncontended TPS: %s\033[0m\n", validator_ratio
  }
'
