# Agave + QAT source workspace

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
git clone --recurse-submodules <parent-repo-url> agave-junction-qat
cd agave-junction-qat
git submodule status
```

For an existing parent clone, run `git submodule update --init`. Do **not**
use `git submodule update --remote` unless deliberately updating and retesting
the whole source set.

Build the included `qat_driver` submodule for the host following its own
setup instructions. Its generated `qat_driver/build/include/` and
`qat_driver/build/libqat_s.so` must exist. Provision the QAT hardware,
Junction/Caladan, networking, permissions, and `/usr/bin/fish` separately.

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

## Build and run

`workspace.sh` sets `QATLIB_SRC` to this checkout's `qat_driver/` submodule,
`QATLIB_BUILD` to its `build/` directory, and the Junction config paths to
the tracked examples by default. If the driver has a nonstandard build
directory, set `QATLIB_BUILD` explicitly. Build and generate fresh keys on
the host:

```bash
bash workspace.sh build
bash workspace.sh init-ledger  # fresh clone only; refuses existing agave/config/
```

Before launching, set the external Junction executable (either export
`JUNCTION_RUN` or copy `workspace.env.example` to ignored `workspace.env`
and edit it):

```bash
export JUNCTION_RUN=/absolute/path/to/junction_run
bash workspace.sh validator
# In a second terminal after the validator is ready (set JUNCTION_RUN there too):
bash workspace.sh bench
```

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

The driver Gitlink points to `95b79b8` on `export_eddsa`, available from
`git@github.com:liam0215/qatlib_oot.git`. The parent repository has no remote
yet; publish it to share one recursive-clone entry point. No Junction runtime
test has been performed from this parent checkout; the prior successful run
was from the original development working trees.
