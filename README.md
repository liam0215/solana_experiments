# Agave + QAT source workspace

This parent repository groups the modified Agave and QAT sources as sibling
Git submodules. The directory names are part of the build: Cargo uses
relative paths such as `../solana-sdk` and `../qat-shim`.

| Submodule | Purpose |
| --- | --- |
| `agave` | Validator, benchmark, and supported launch scripts |
| `solana-sdk` | Modified SDK crates |
| `ed25519-dalek`, `ed25519-dalek-bip32`, `curve25519-dalek` | Modified signing/verification dependencies |
| `qat-shim` | Rust/C QAT binding |
| `qat_driver` | Modified out-of-tree QAT driver source (`qatlib_oot` fork) |

Junction and Caladan are **external**; neither is a submodule here. The
experimental CSVs/plots live in a separate `agave-results` repository, not
in this runnable source workspace. The pinned submodule commits are source
checkpoints; compiled driver objects, firmware, runtime configuration,
secrets, ledgers, and Agave binaries are not included.

## Get started

```bash
git clone --recurse-submodules <parent-repo-url> agave-junction-qat
cd agave-junction-qat
git submodule status
```

For an existing parent clone, run `git submodule update --init`. Do **not**
run `git submodule update --remote` unless deliberately updating and retesting
the whole source set.

Build the included QAT driver for the host first, following its own setup
instructions. Its generated `qat_driver/build/include/` and
`qat_driver/build/libqat_s.so` must exist. Provision your Junction/Caladan
host and two distinct Junction runtime configurations separately; the
validator config must enable QAT and QAT asymmetric operations.

Copy `agave/packaging/junction-qat/env.example` to
`agave/packaging/junction-qat/local.env` and set at least:

```bash
QATLIB_SRC=/absolute/path/to/agave-junction-qat/qat_driver
QATLIB_BUILD=/absolute/path/to/agave-junction-qat/qat_driver/build
JUNCTION_RUN=/absolute/path/to/junction_run
JUNCTION_VALIDATOR_CONFIG=/absolute/path/to/validator.config
JUNCTION_BENCH_CONFIG=/absolute/path/to/bench.config
MASTER_HOST=validator-host-address
```

Then follow [the Agave Junction+QAT guide](agave/packaging/junction-qat/README.md):

```bash
bash agave/packaging/junction-qat/build.sh
bash agave/packaging/junction-qat/init-ledger.sh  # fresh clone only; refuses existing agave/config/
bash agave/packaging/junction-qat/run-junction.sh validator
# From a second terminal after the validator is ready:
bash agave/packaging/junction-qat/run-junction.sh bench
```

The binaries run under Junction; genesis creation is a host-side setup step.
Never share the generated `agave/config/` keypairs. The older run/sweep
scripts in Agave and the results repository are historical and contain
machine-specific paths and cleanup commands.

The driver Gitlink points to `95b79b8` on `export_eddsa`, available from
`git@github.com:liam0215/qatlib_oot.git`. The parent repository has no
remote yet; publish it to share one recursive-clone entry point. No Junction
runtime test has been performed from this new parent checkout; the prior
successful run was from the original development working trees.
