#!/usr/bin/env python3
"""Run isolated Junction validators and host-side bench-tps clients from TOML."""

import argparse
import csv
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TPS = re.compile(r"\bAverage TPS:\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)")
PEAK = re.compile(r"\bHighest TPS:\s*([0-9]+(?:\.[0-9]+)?)")
DROP = re.compile(r"\bdrop rate:\s*([0-9]+(?:\.[0-9]+)?)")
STAKE = re.compile(r"Stake for specified client_node_id:\s*([0-9]+)")
STAMP = re.compile(r"^\[([^ ]+) ")


def positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def keys(section, expected, label):
    if not isinstance(section, dict):
        raise ValueError(f"{label} must be a table")
    unknown = set(section) - set(expected)
    if unknown:
        raise ValueError(f"Unknown {label} setting(s): {', '.join(sorted(unknown))}")


def cpu_topology(cpu):
    base = Path(f"/sys/devices/system/cpu/cpu{cpu}")
    nodes = list(base.glob("node[0-9]*"))
    if len(nodes) != 1:
        raise ValueError(f"Cannot determine NUMA node for CPU {cpu}")
    topology = base / "topology"
    return (int(nodes[0].name[4:]),
            (int((topology / "physical_package_id").read_text()),
             int((topology / "core_id").read_text())))


def load_config(path):
    with path.open("rb") as source:
        data = tomllib.load(source)
    keys(data, {"version", "name", "validator_count", "hosts", "validator", "benchmark", "run"}, "experiment")
    if data.get("version") != 1 or type(data.get("version")) is not int:
        raise ValueError("version must be 1")
    name = data.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) or name in (".", ".."):
        raise ValueError("name must be a safe directory name")
    count = positive(data.get("validator_count"), "validator_count")
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or len(hosts) != count or any(not isinstance(h, str) for h in hosts):
        raise ValueError("hosts must contain exactly validator_count IP addresses")
    for host in hosts:
        ipaddress.IPv4Address(host)
    if len(set(hosts)) != count:
        raise ValueError("validator hosts must be distinct")

    val = data.get("validator")
    keys(val, {"template", "runtime_kthreads", "banking_threads", "sigverify_threads"}, "validator")
    for key in ("runtime_kthreads", "banking_threads", "sigverify_threads"):
        positive(val.get(key), f"validator.{key}")
    template = val.get("template")
    if not isinstance(template, str):
        raise ValueError("validator.template must be a path")
    template_path = Path(template)
    template_path = template_path if template_path.is_absolute() else ROOT / template_path
    if not template_path.is_file():
        raise ValueError(f"Missing validator template: {template_path}")
    val["template"] = str(template_path.resolve())
    # Also verify that the template can be rendered before starting processes.
    render_config(template_path.read_text(), hosts[0], val["runtime_kthreads"])

    bench = data.get("benchmark")
    keys(bench, {"cores_per_client", "cpu_pool", "memory_node", "sender_threads",
                 "duration_seconds", "tx_count", "thread_batch_sleep_ms"}, "benchmark")
    for key in ("cores_per_client", "sender_threads", "duration_seconds", "tx_count"):
        positive(bench.get(key), f"benchmark.{key}")
    if type(bench.get("thread_batch_sleep_ms")) is not int or bench["thread_batch_sleep_ms"] < 0:
        raise ValueError("benchmark.thread_batch_sleep_ms must be nonnegative")
    if type(bench.get("memory_node")) is not int or bench["memory_node"] < 0:
        raise ValueError("benchmark.memory_node must be nonnegative")
    pool = bench.get("cpu_pool")
    if not isinstance(pool, list) or len(pool) != count * bench["cores_per_client"] or any(type(c) is not int for c in pool):
        raise ValueError("cpu_pool must contain exactly validator_count * cores_per_client CPUs")
    if len(set(pool)) != len(pool) or any(c < 0 for c in pool):
        raise ValueError("cpu_pool must contain distinct, nonnegative CPUs")
    allowed = os.sched_getaffinity(0)
    physical = set()
    for cpu in pool:
        if cpu not in allowed:
            raise ValueError(f"CPU {cpu} is not available in the current affinity mask")
        node, core = cpu_topology(cpu)
        if node != bench["memory_node"]:
            raise ValueError(f"CPU {cpu} is on NUMA node {node}, not {bench['memory_node']}")
        if core in physical:
            raise ValueError(f"CPU {cpu} shares a physical core with another bench CPU")
        physical.add(core)
    bench["cpu_masks"] = [pool[i:i + bench["cores_per_client"]]
                          for i in range(0, len(pool), bench["cores_per_client"])]

    run = data.get("run")
    keys(run, {"repetitions", "startup_delay_seconds",
               "ready_timeout_seconds", "rpc_timeout_seconds"}, "run")
    for key in ("repetitions", "ready_timeout_seconds", "rpc_timeout_seconds"):
        positive(run.get(key), f"run.{key}")
    if type(run.get("startup_delay_seconds")) is not int or run["startup_delay_seconds"] < 0:
        raise ValueError("run.startup_delay_seconds must be nonnegative")
    return data


def render_config(template, host, kthreads):
    lines = template.splitlines(keepends=True)
    for key, value in (("host_addr", host), ("runtime_kthreads", str(kthreads))):
        matches = [i for i, line in enumerate(lines) if line.split()[:1] == [key]]
        if len(matches) != 1:
            raise ValueError(f"Template must have exactly one {key} setting")
        lines[matches[0]] = f"{key} {value}\n"
    return "".join(lines)


def machine_env():
    env = os.environ.copy()
    local = ROOT / "agave/packaging/junction-qat/local.env"
    if local.exists():
        raise ValueError(f"Remove {local}; it overrides workspace settings")
    config = ROOT / "workspace.env"
    if config.is_file():
        output = subprocess.check_output(
            ["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(config)], env=env)
        env.update(dict(part.decode().split("=", 1) for part in output.split(b"\0") if part))
    env["QATLIB_BUILD"] = env.get("QATLIB_BUILD") or str(ROOT / "qat_driver/build")
    env["LD_LIBRARY_PATH"] = env["QATLIB_BUILD"] + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def preflight(env):
    programs = [env.get("JUNCTION_RUN", ""), "/usr/bin/fish", "sudo", "numactl", "taskset",
                *(str(ROOT / "agave/target/release-with-debug" / name) for name in
                  ("solana-keygen", "solana-genesis", "solana-faucet", "agave-validator", "solana-bench-tps"))]
    for program in programs:
        found = shutil.which(program) if program and "/" not in program else program
        if not found or not os.access(found, os.X_OK) or Path(found).is_dir():
            raise ValueError(f"Missing executable: {program or 'JUNCTION_RUN (set in workspace.env)'}")
    ld_path = Path(env.get("JUNCTION_LD_PATH") or env["QATLIB_BUILD"])
    if not (ld_path / "libqat_s.so").is_file() or not (Path(env["QATLIB_BUILD"]) / "libqat_s.so").is_file():
        raise ValueError("Missing QAT library in QATLIB_BUILD or JUNCTION_LD_PATH")


def run_command(args, env, log, cwd=ROOT):
    with log.open("wb") as output:
        subprocess.run(args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=True)


def create_ledger(state, env):
    ledger = state / "ledger"
    ledger.mkdir(parents=True, mode=0o700)
    faucet = state / "faucet.json"
    bin_dir = ROOT / "agave/target/release-with-debug"
    keygen = bin_dir / "solana-keygen"
    for dest in (faucet, *(ledger / f"{name}.json" for name in ("identity", "stake-account", "vote-account"))):
        run_command([str(keygen), "new", "--no-passphrase", "--silent", "--outfile", str(dest)],
                    env, state / f"keygen-{dest.stem}.log")
    run_command([str(bin_dir / "solana-genesis"), "--ledger", str(ledger),
                 "--faucet-pubkey", str(faucet), "--faucet-lamports", "500000000000000000",
                 "--hashes-per-tick", "auto", "--cluster-type", "development",
                 "--enable-warmup-epochs", "--bootstrap-validator",
                 str(ledger / "identity.json"), str(ledger / "vote-account.json"),
                 str(ledger / "stake-account.json")], env, state / "genesis.log", cwd=ROOT / "agave")
    lock = ledger / "rocksdb/LOCK"
    if lock.exists() or lock.is_symlink():
        if not lock.is_file() or lock.is_symlink():
            raise RuntimeError(f"Refusing to remove non-regular genesis lock: {lock}")
        if shutil.which("fuser") and subprocess.run(["fuser", "-s", str(lock)], check=False).returncode == 0:
            raise RuntimeError(f"Refusing to remove open genesis lock: {lock}")
        lock.unlink()
    return ledger, faucet


def git_id(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=False).stdout.strip()


def git_status(path):
    return subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True,
                          text=True, check=False).stdout.splitlines()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_bench(log, instance, host, cpus):
    result = {"validator": instance, "host": host, "cpus": cpus,
              "average_tps": None, "peak_tps": None, "drop_rate": None, "client_stake": None,
              "send_start": None, "send_end": None, "log": str(log)}
    with log.open(errors="replace") as source:
        for line in source:
            stamp = STAMP.match(line)
            if "Sampling TPS every" in line and stamp:
                result["send_start"] = stamp[1]
            match = TPS.search(line)
            if match and "solana_bench_tps::bench" in line:
                result["average_tps"] = float(match[1])
                result["send_end"] = stamp[1] if stamp else None
            match = PEAK.search(line)
            if match:
                result["peak_tps"] = float(match[1])
            match = DROP.search(line)
            if match:
                result["drop_rate"] = float(match[1])
            match = STAKE.search(line)
            if match:
                result["client_stake"] = int(match[1])
    if result["average_tps"] is None:
        raise RuntimeError(f"No Average TPS in {log}")
    if result["client_stake"] == 0:
        raise RuntimeError(f"Benchmark started without active client stake: {log}")
    return result


class Trial:
    def __init__(self, config, path, env):
        self.config, self.path, self.env = config, path, env
        self.active = {}
        self.identities = []
        self.commands = []
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def launch(self, name, args):
        self.commands.append({"name": name, "argv": args,
                              "started_at": datetime.now(timezone.utc).isoformat()})
        (self.path / "commands.json").write_text(json.dumps(self.commands, indent=2) + "\n")
        output = (self.path / f"{name}.log").open("wb")
        try:
            proc = subprocess.Popen(args, cwd=ROOT, env=self.env, stdout=output,
                                    stderr=subprocess.STDOUT, process_group=0)
        except BaseException:
            output.close()
            raise
        self.active[proc.pid] = (proc, output)
        print(f"Started {name}: PID {proc.pid}", flush=True)
        return proc

    def stop(self, proc):
        if proc.pid not in self.active:
            return
        for sig, timeout in ((signal.SIGINT, 12), (signal.SIGTERM, 5), (signal.SIGKILL, 3)):
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            # A wrapper can exit before its subprocesses; check the group
            # again, not just the wrapper PID.
            if sig == signal.SIGKILL:
                break
        proc.wait()
        self.active.pop(proc.pid)[1].close()

    def wait(self, proc, name):
        code = proc.wait()
        if code:
            self.stop(proc)
            raise RuntimeError(f"{name} exited {code}; see {self.path / (name + '.log')}")
        self.active.pop(proc.pid)[1].close()

    def validator(self, i, ledger, faucet, junction_config):
        lock = ledger / "rocksdb/LOCK"
        if lock.exists() and shutil.which("fuser") and subprocess.run(
                ["fuser", "-s", str(lock)], check=False).returncode == 0:
            raise RuntimeError(f"Ledger is still open; refusing to start validator {i + 1}: {lock}")
        args = ["sudo", self.env["JUNCTION_RUN"], str(junction_config)]
        variables = {"AGAVE_ROOT": str(ROOT / "agave"), "QATLIB_BUILD": self.env["QATLIB_BUILD"],
                     "MASTER_HOST": self.config["hosts"][i], "LEDGER_PATH": str(ledger),
                     "FAUCET_KEYPAIR": str(faucet),
                     "SOLANA_BANKING_THREADS": str(self.config["validator"]["banking_threads"]),
                     "SOL_SIGVERIFY_THREADS": str(self.config["validator"]["sigverify_threads"])}
        if self.env.get("RUST_LOG"):
            variables["RUST_LOG"] = self.env["RUST_LOG"]
        for key, value in variables.items():
            args.extend(["--env", f"{key}={value}"])
        args.extend(["--ld_path", self.env.get("JUNCTION_LD_PATH") or self.env["QATLIB_BUILD"],
                     "--", "/usr/bin/fish", str(ROOT / "experiment-validator.fish")])
        return self.launch(f"validator-{i + 1}", args)

    def ready(self, i, proc):
        host = self.config["hosts"][i]
        run = self.config["run"]
        time.sleep(run["startup_delay_seconds"])
        deadline = time.monotonic() + run["ready_timeout_seconds"]
        rpc_deadline = time.monotonic() + run["rpc_timeout_seconds"]
        seen_rpc = False
        last_report = 0
        message = "RPC not yet reachable"
        print(f"Waiting for active stake on validator {i + 1} ({host})", flush=True)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Validator {i + 1} exited; see {self.path / f'validator-{i + 1}.log'}")
            request = urllib.request.Request(f"http://{host}:8899", data=b'{"jsonrpc":"2.0","id":1,"method":"getVoteAccounts"}',
                                             headers={"Content-Type": "application/json"})
            try:
                with self.opener.open(request, timeout=2) as response:
                    votes = json.load(response).get("result", {}).get("current")
                if isinstance(votes, list):
                    seen_rpc = True
                    if any(v.get("nodePubkey") == self.identities[i] and
                           int(v.get("activatedStake", 0)) > 0 for v in votes):
                        print(f"Validator {i + 1} has active stake", flush=True)
                        return
                    message = "RPC reachable, no active vote account for this validator identity"
                else:
                    message = "RPC returned no vote accounts"
            except (OSError, ValueError, TypeError, AttributeError) as error:
                message = f"RPC error: {error}"
            now = time.monotonic()
            if now - last_report >= 10:
                print(f"Validator {i + 1}: {message}", flush=True)
                last_report = now
            if not seen_rpc and now >= rpc_deadline:
                raise RuntimeError(f"Validator {i + 1} RPC failed after {run['rpc_timeout_seconds']}s: {message}")
            time.sleep(1)
        raise RuntimeError(f"Validator {i + 1} did not report active stake: {message}")

    def bench(self, i):
        bench = self.config["benchmark"]
        host = self.config["hosts"][i]
        cpus = ",".join(map(str, bench["cpu_masks"][i]))
        ledger = self.path / "private" / f"validator-{i + 1}" / "ledger"
        args = ["bash", str(ROOT / "bench-summary.sh"), "numactl", f"--membind={bench['memory_node']}",
                "taskset", "-c", cpus, str(ROOT / "agave/target/release-with-debug/solana-bench-tps"),
                "--url", f"http://{host}:8899", "--entrypoint", f"{host}:8001", "--faucet", f"{host}:9900",
                "--duration", str(bench["duration_seconds"]), "--tx-count", str(bench["tx_count"]),
                "--threads", str(bench["sender_threads"]), "--thread-batch-sleep-ms",
                str(bench["thread_batch_sleep_ms"]), "--bind-address", "127.0.0.1",
                "--client-node-id", str(ledger / "identity.json")]
        name = f"bench-{i + 1}"
        return self.launch(name, args), name, cpus

    def run(self):
        count = self.config["validator_count"]
        template = Path(self.config["validator"]["template"]).read_text()
        self.path.mkdir(mode=0o700)
        (self.path / "private").mkdir(mode=0o700)
        ledgers, configs, records = [], [], []
        for i, host in enumerate(self.config["hosts"]):
            state = self.path / "private" / f"validator-{i + 1}"
            state.mkdir(mode=0o700)
            ledger, faucet = create_ledger(state, self.env)
            self.identities.append(subprocess.check_output(
                [str(ROOT / "agave/target/release-with-debug/solana-keygen"), "pubkey",
                 str(ledger / "identity.json")], env=self.env, text=True).strip())
            config_path = self.path / f"validator-{i + 1}.config"
            config_path.write_text(render_config(template, host, self.config["validator"]["runtime_kthreads"]))
            ledgers.append((ledger, faucet))
            configs.append(config_path)

        try:
            validators = [self.validator(i, *ledgers[i], configs[i]) for i in range(count)]
            try:
                for i, proc in enumerate(validators):
                    self.ready(i, proc)
                benches = [self.bench(i) for i in range(count)]
                for i, (bench, name, cpus) in enumerate(benches):
                    self.wait(bench, name)
                    records.append(parse_bench(self.path / f"{name}.log", i + 1,
                                               self.config["hosts"][i], cpus))
            finally:
                for proc in validators:
                    self.stop(proc)
        finally:
            for proc, _ in list(self.active.values()):
                self.stop(proc)
        return records


def summarize(records):
    values = [row["average_tps"] for row in records]
    starts = [datetime.fromisoformat(row["send_start"].replace("Z", "+00:00")) for row in records
              if row["send_start"]]
    ends = [datetime.fromisoformat(row["send_end"].replace("Z", "+00:00")) for row in records
            if row["send_end"]]
    overlap = max(0, (min(ends) - max(starts)).total_seconds()) if len(starts) == len(ends) == len(records) else None
    return {"clients": records,
            "mean_tps": sum(values) / len(values) if values else None,
            "total_tps": sum(values) if values else None,
            "lowest_as_percent_of_highest": (100 * min(values) / max(values)
                                             if values and max(values) else None),
            "sampling_start_skew_seconds": ((max(starts) - min(starts)).total_seconds()
                                            if len(starts) == len(records) else None),
            "approximate_sampling_overlap_seconds": overlap}


def write_summary(path, summary):
    (path / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    fields = ("validator", "host", "cpus", "average_tps", "peak_tps", "drop_rate", "client_stake",
              "send_start", "send_end", "log")
    with (path / "summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fields)
        writer.writeheader()
        for row in summary["clients"]:
            writer.writerow(row)


def write_experiment_summary(directory, completed):
    (directory / "summary.json").write_text(json.dumps(completed, indent=2) + "\n")
    fields = ("trial", "validator", "host", "cpus", "average_tps", "peak_tps",
              "drop_rate", "client_stake", "send_start", "send_end", "log")
    with (directory / "summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fields)
        writer.writeheader()
        for trial in completed:
            for row in trial["clients"]:
                writer.writerow({"trial": trial["trial"], **row})

    fields = ("repetition", "mean_tps", "total_tps", "lowest_as_percent_of_highest",
               "sampling_start_skew_seconds", "approximate_sampling_overlap_seconds")
    with (directory / "repetitions.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fields)
        writer.writeheader()
        for trial in completed:
            writer.writerow({"repetition": trial["trial"],
                             **{field: trial[field] for field in fields[1:]}})


def run_repetitions(config, directory, env):
    completed = []
    write_experiment_summary(directory, completed)
    for index in range(config["run"]["repetitions"]):
        print(f"Repetition {index + 1} / {config['run']['repetitions']}", flush=True)
        trial = Trial(config, directory / f"trial-{index + 1:02d}", env)
        records = trial.run()
        summary = summarize(records)
        write_summary(trial.path, summary)
        completed.append({"trial": index + 1, **summary})
        write_experiment_summary(directory, completed)
        for row in summary["clients"]:
            print(f"Validator {row['validator']}: Average TPS "
                  f"{row['average_tps']:.2f}; drop rate {row['drop_rate']}", flush=True)
        spread = summary["lowest_as_percent_of_highest"]
        print(f"\033[1;31mRepetition {index + 1}: mean TPS = "
              f"{summary['mean_tps']:.2f}; "
              f"lowest as % of highest = "
              f"{f'{spread:.2f}%' if spread is not None else 'undefined'}\033[0m", flush=True)
        if summary["sampling_start_skew_seconds"] is not None:
            print(f"Sampling start skew: {summary['sampling_start_skew_seconds']:.2f}s; "
                  f"approximate overlap: {summary['approximate_sampling_overlap_seconds']:.2f}s",
                  flush=True)
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "run"))
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = load_config(config_path)
    for i, host in enumerate(config["hosts"]):
        print(f"Validator {i + 1}: {host}; kthreads={config['validator']['runtime_kthreads']}; "
              f"bench CPUs={config['benchmark']['cpu_masks'][i]}, NUMA={config['benchmark']['memory_node']}")
    if args.action == "validate":
        return 0

    env = machine_env()
    preflight(env)
    os.umask(0o077)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = ROOT / "results" / config["name"] / f"{timestamp}-{uuid.uuid4().hex[:8]}"
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(mode=0o700)
    shutil.copyfile(config_path, directory / "experiment.toml")
    metadata = {"config": config, "git": {name: {"revision": git_id(ROOT / name),
                                            "status": git_status(ROOT / name)} for name in
                (".", "agave", "qat_driver", "solana-sdk", "qat-shim")},
                "machine": {key: env.get(key) for key in ("JUNCTION_RUN", "QATLIB_BUILD", "JUNCTION_LD_PATH")},
                "binaries": {name: {"path": str(path), "sha256": sha256(path)} for name, path in {
                    "validator": ROOT / "agave/target/release-with-debug/agave-validator",
                    "bench": ROOT / "agave/target/release-with-debug/solana-bench-tps",
                    "qat": Path(env["QATLIB_BUILD"]) / "libqat_s.so"}.items()}}
    (directory / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Results: {directory}", flush=True)
    try:
        run_repetitions(config, directory, env)
    except BaseException as error:
        (directory / "error.txt").write_text(str(error) + "\n")
        raise
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError, KeyboardInterrupt) as error:
        print(f"Experiment failed: {error}", file=sys.stderr)
        sys.exit(1)
