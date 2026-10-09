#!/usr/bin/env bash
set -euo pipefail

# Solana logs to stderr. Keep streaming the combined output, then repeat the
# last reported average in red without hiding the benchmark's exit status.
"$@" 2>&1 | awk '
  {
    print
    fflush()
    if (match($0, /Average TPS: *[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?/)) {
      average = substr($0, RSTART, RLENGTH)
    }
  }
  END {
    if (average != "") {
      printf "\033[1;31m%s\033[0m\n", average
    }
  }
'
