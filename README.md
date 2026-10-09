# Solana experiments: Agave + QAT source workspace

This parent repository groups the modified Agave and QAT sources as sibling
Git submodules. The directory names matter: Cargo uses relative paths such
as `../solana-sdk` and `../qat-shim`.

| Submodule | Purpose |
| --- | --- |
| `agave` | Validator, benchmark, and Junction launch scripts |
| `solana-sdk` | Modified SDK crates |
| `ed25519-dalek`, `ed25519-dalek-bip32`, `curve25519-dalek` | Modified signing/verification dependencies |
| `qat-shim` | Rust/C QAT binding |
| `qat_driver` | Modified out-of-tree QAT driver source (`qatlib_oot` fork) |

Junction and Caladan are **external**, unpinned host prerequisites; neither
is a submodule. Experimental CSVs and plots live in a separate `agave-results`
repository. Generated driver objects, firmware, secrets, ledgers, and Agave
binaries are not included.

## Clone and prepare the host

```bash
git clone --recurse-submodules <parent-repo-url> solana_experiments
cd solana_experiments
git submodule status
```

For an existing parent clone, run `git submodule update --init`. Do **not**
use `git submodule update --remote` unless deliberately updating and retesting
the whole source set.

Build the included `qat_driver` submodule for the host following its own
setup instructions. Its generated `qat_driver/build/include/` and
`qat_driver/build/libqat_s.so` must exist.

`configs/validator.config` and `configs/bench.config` are copies of the
original validator/bench Junction configs. They are the **default paths** for
the parent launcher, not universal network settings: the sample addresses
`192.168.120.7` and `192.168.120.8`, netmask, gateway, runtime threads, and
QAT flags must be reviewed for your host. The two `host_addr` values must be
distinct; the validator config enables QAT and asymmetric operations.
To keep tracked examples unchanged, copy them to `configs/local/` (ignored by
Git) and point `JUNCTION_VALIDATOR_CONFIG` and `JUNCTION_BENCH_CONFIG` there.
The launcher reads `MASTER_HOST` from the validator config's `host_addr` and
rejects a conflicting explicit value.

For three independent validators, `configs/validator-2.config` and
`configs/validator-3.config` reproduce the working `192.168.120.9` and
`192.168.120.11` Junction settings; instance 1 uses `validator.config`
(`192.168.120.7`). All three validator configs now use `runtime_kthreads 8`.
Review all IPs, CPU availability, NUMA node 1, and
Junction network isolation on your host. Override the numbered config paths
with `JUNCTION_VALIDATOR_CONFIG_2` / `_3` in `workspace.env` if necessary.
Numbered instances read their host from their own config even if `MASTER_HOST`
is set for the original unnumbered launcher.

## Build and run

`workspace.sh` sets `QATLIB_SRC` to this checkout's `qat_driver/` submodule,
`QATLIB_BUILD` to its `build/` directory, and the Junction config paths to
the tracked examples by default. Validators launched through this workspace
default to `SOLANA_BANKING_THREADS=8` and `SOL_SIGVERIFY_THREADS=18`; override
either in `workspace.env` or the caller's environment. These are validator
threads, not the Junction `runtime_kthreads` in `configs/`. If the driver has a
nonstandard build directory, set `QATLIB_BUILD` explicitly. Build and generate fresh keys on
the host:

```bash
bash workspace.sh build
bash workspace.sh init-ledger  # fresh clone only; refuses existing agave/config/
```

After genesis completes successfully, the parent launcher removes the
new ledger's `agave/config/bootstrap-validator/rocksdb/LOCK` file if present.

Before launching, set the external Junction executable (either export
`JUNCTION_RUN` or copy `workspace.env.example` to ignored `workspace.env`
and edit it):

```bash
export JUNCTION_RUN=/absolute/path/to/junction_run
bash workspace.sh validator
# In a second terminal after the validator is ready (set JUNCTION_RUN there too):
bash workspace.sh bench
```

To run three validators and three **host-side** bench clients concurrently,
first create two additional, independent ledgers and keypairs (never share a
RocksDB ledger between validators):

```bash
bash workspace.sh init-ledger 2
bash workspace.sh init-ledger 3
```

Then, in separate terminals, run `bash workspace.sh validator 1`,
`bash workspace.sh validator 2`, and `bash workspace.sh validator 3`.
After all three are ready, in three more terminals run
`bash workspace.sh bench 1`, `bash workspace.sh bench 2`, and
`bash workspace.sh bench 3`. Numbered benchmarks run directly on the host
with `numactl --membind=1 taskset -c` on CPUs `5,7,...,19` (1),
`21,23,...,35` (2), and `37,39,...,51` (3). They use each instance's identity,
bind to `127.0.0.1`, and target its validator IP with the original duration
and transaction count (configurable via `BENCH_DURATION` / `BENCH_TX_COUNT`).
They require `numactl` and `taskset`; numbered validators still run under
Junction and require `JUNCTION_RUN`. The three ledgers have separate genesis
and keys: these are separate experiments, **not** a three-node cluster.
Unnumbered `validator` and `bench` retain the earlier Junction-based behavior.
All `workspace.sh bench` commands stream the original output and repeat the
last `Average TPS:` value in red at the end.

To run the full comparison automatically, first initialize ledgers 2 and 3
as above, then run `bash workspace.sh all` (or `bash run-three.sh`). It starts
validator 1, waits five seconds, and runs benchmark 1 alone for an
uncontended baseline. Keeping validator 1 running with the **same config and
ledger**, it then starts validators 2 and 3, waits five seconds, and runs all
three benchmarks concurrently. After each five-second delay it also waits for
each validator's RPC to report active stake before starting its benchmark;
this requires `curl` and `jq` and times out after 300 seconds by default
(`VALIDATOR_READY_TIMEOUT` overrides the timeout). If RPC is unreachable or
fails to return vote accounts for 30 seconds, it fails earlier instead of
silently retrying (`VALIDATOR_RPC_TIMEOUT` overrides that timeout). It prints the baseline,
each contended Average TPS, the contended per-client mean, the lowest contended
TPS as a percentage of the highest, and both the contended mean and validator
1's contended TPS as percentages of the uncontended baseline. The summary is
red; then it stops
the validators. Separate validator and benchmark logs (including
`bench-1-uncontended.log`) are saved in the printed temporary directory;
if a process fails or the run is interrupted, the validators are stopped and
the logs are kept for troubleshooting. The comparison uses the benchmark's
`Average TPS:` line, not the higher `Average max TPS:` (peak sample) line.
Check `Stake for specified client_node_id:` in each benchmark log if results
look unexpectedly low; a value of zero means that benchmark started without
active stake.

The child Agave scripts also accept `agave/packaging/junction-qat/local.env`,
but do not create it when using this parent launcher: it would override the
parent's defaults. Use `workspace.env` for machine-specific overrides. The
Agave [Junction+QAT guide](agave/packaging/junction-qat/README.md) documents
the binary, ledger, and launcher behavior for direct use of those scripts;
its external-driver layout predates this parent repo.

The binaries run under Junction; genesis creation is a host-side setup step.
Never share generated `agave/config/` keypairs. Older Agave run/sweep scripts
and the separate results repo are historical and contain machine-specific
paths and cleanup commands.

## Configured experiments

`configs/experiments/three-validators.toml` runs a three-validator experiment;
`configs/experiments/one-validator-8.toml` and `one-validator-12.toml` are
separate solo experiments.
`configs/experiments/two-validators-12.toml` runs two validators with
12 Junction runtime kthreads each and two benchmark clients with 12 CPU cores
each (NUMA node 1); its `[run] repetitions = 10` runs ten rounds. The runner
needs Python 3.11+, Junction, the built Agave binaries and QAT driver,
`numactl`, `taskset`, `sudo`, and `/usr/bin/fish`. Set `JUNCTION_RUN` and any
machine-specific driver paths in `workspace.env` as for `workspace.sh`.

```bash
bash workspace.sh experiment validate configs/experiments/three-validators.toml
bash workspace.sh experiment run configs/experiments/three-validators.toml
# For ten rounds of two contending validators:
bash workspace.sh experiment run configs/experiments/two-validators-12.toml
# For an independent solo run:
bash workspace.sh experiment run configs/experiments/one-validator-8.toml
```

`python3 experiment.py validate|run CONFIG.toml` is equivalent.

`validator_count` must match the number of explicit `hosts`. The runner uses
the validator template to generate **identical settings except for host IP**:
`runtime_kthreads`, `SOLANA_BANKING_THREADS`, and `SOL_SIGVERIFY_THREADS` all
come from the experiment config. The benchmark CPU pool is split into equal,
non-overlapping `cores_per_client` sets; it rejects CPU pairs sharing a
physical core, offline/unavailable CPUs, and a mismatched `memory_node`.
`runtime_kthreads` is a Junction worker limit, **not** validator CPU pinning.
The example uses NUMA node 1 for all benchmark clients. The runner does not
use `--duration 0` for funding or claim exactly synchronized send starts.

Set `[run] repetitions = 10` (or another positive integer) to repeat the
experiment. `repetitions` is the only supported key for the round count.
For each repetition, the runner generates fresh genesis ledgers and keys
in `results/<name>/<timestamp>-<id>/trial-N/private/`, starts only the validators
listed in that config, and benchmarks them once. A one-validator config runs
one validator and one benchmark; a three-validator config runs three of each
concurrently. These are independent runs: the runner does not automatically
run solo baselines or compare results across configs. Each run waits for RPC
to report an active vote account. Result directories are ignored by Git,
created with private permissions, and **never automatically deleted**; they may be large and
contain private keypairs. Keep the printed path for inspection, and treat
those artifacts as secrets.

Each trial contains rendered Junction configs, separate genesis/validator/
benchmark logs, `summary.json` and `summary.csv`. The parent run records a
config snapshot, resolved CPU allocation, source revisions and status, binary
hashes, and `summary.csv` with one row per benchmark client per repetition.
`repetitions.csv` has one row per repetition with mean and total TPS, lowest-to-
highest TPS ratio, and approximate sampling overlap (not an exact send barrier).
The CSVs are updated after every completed repetition, so completed results
remain available if a later repetition fails. `summary.json` contains the same
per-client and per-repetition data.
On failure the runner stops processes it launched and keeps logs and
`error.txt` for diagnosis. The old `workspace.sh` and `run-three.sh` commands
remain available for manual runs.

The driver Gitlink points to `95b79b8` on `export_eddsa`, available from
`git@github.com:liam0215/qatlib_oot.git`. The parent repository has no remote
yet; publish it to share one recursive-clone entry point. No Junction runtime
test has been performed from this parent checkout; the prior successful run
was from the original development working trees.
