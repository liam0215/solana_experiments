import csv
import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import experiment


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.example = experiment.ROOT / "configs/experiments/three-validators.toml"

    def test_example_uses_distinct_node_one_cores(self):
        config = experiment.load_config(self.example)
        self.assertEqual(config["validator_count"], 3)
        self.assertEqual([len(mask) for mask in config["benchmark"]["cpu_masks"]], [8, 8, 8])
        self.assertEqual(config["validator"]["runtime_kthreads"], 8)
        self.assertEqual(config["benchmark"]["memory_node"], 1)

    def test_mismatched_core_pool_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            text = self.example.read_text().replace("cores_per_client = 8", "cores_per_client = 9")
            path = Path(directory) / "bad.toml"
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "cpu_pool"):
                experiment.load_config(path)

    def test_wrong_numa_node_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            text = self.example.read_text().replace("memory_node = 1", "memory_node = 0")
            path = Path(directory) / "bad.toml"
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "NUMA node"):
                experiment.load_config(path)

    def test_duplicate_benchmark_cpu_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            text = self.example.read_text().replace("47, 49, 51]", "47, 49, 5]")
            path = Path(directory) / "bad.toml"
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "distinct"):
                experiment.load_config(path)

    def test_single_validator_config(self):
        path = experiment.ROOT / "configs/experiments/one-validator-8.toml"
        config = experiment.load_config(path)
        self.assertEqual(config["validator_count"], 1)
        self.assertEqual(config["benchmark"]["cpu_masks"], [[5, 7, 9, 11, 13, 15, 17, 19]])

    def test_two_validators_with_twelve_workers_and_cores(self):
        path = experiment.ROOT / "configs/experiments/two-validators-12.toml"
        config = experiment.load_config(path)
        self.assertEqual(config["validator_count"], 2)
        self.assertEqual(config["validator"]["runtime_kthreads"], 12)
        self.assertEqual(config["run"]["repetitions"], 10)
        self.assertEqual([len(mask) for mask in config["benchmark"]["cpu_masks"]], [12, 12])
        self.assertFalse(set(config["benchmark"]["cpu_masks"][0]) &
                         set(config["benchmark"]["cpu_masks"][1]))

    def test_old_solo_setting_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            path = Path(directory) / "single.toml"
            path.write_text(self.example.read_text().replace("repetitions = 1",
                                                        "solo_baseline_per_validator = true\nrepetitions = 1"))
            with self.assertRaisesRegex(ValueError, "solo_baseline_per_validator"):
                experiment.load_config(path)

    def test_repetitions_must_be_positive(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            path = Path(directory) / "config.toml"
            original = (experiment.ROOT / "configs/experiments/two-validators-12.toml").read_text()
            for invalid in ("0", "true", "-1"):
                with self.subTest(value=invalid):
                    path.write_text(original.replace("repetitions = 10", f"repetitions = {invalid}"))
                    with self.assertRaisesRegex(ValueError, "run.repetitions"):
                        experiment.load_config(path)

    def test_iterations_setting_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            path = Path(directory) / "config.toml"
            original = (experiment.ROOT / "configs/experiments/two-validators-12.toml").read_text()
            path.write_text(original.replace("repetitions = 10", "iterations = 10"))
            with self.assertRaisesRegex(ValueError, "iterations"):
                experiment.load_config(path)

    def test_repetitions_config(self):
        config = experiment.load_config(self.example)
        self.assertEqual(config["run"]["repetitions"], 1)

    def test_validator_settings_render_identically_except_host(self):
        template = (experiment.ROOT / "configs/validator.config").read_text()
        first = experiment.render_config(template, "192.168.120.7", 10)
        second = experiment.render_config(template, "192.168.120.9", 10)
        self.assertEqual(first.replace("192.168.120.7", ""),
                         second.replace("192.168.120.9", ""))
        self.assertIn("runtime_kthreads 10", first)

    def test_results_are_ignored(self):
        self.assertIn("/results/", (experiment.ROOT / ".gitignore").read_text())


class SummaryTests(unittest.TestCase):
    def test_repetition_csv_contains_all_clients_and_rounds(self):
        config = experiment.load_config(experiment.ROOT / "configs/experiments/two-validators-12.toml")
        config["run"]["repetitions"] = 2
        def rows(round_number):
            return [{"validator": i, "host": config["hosts"][i - 1], "cpus": "5,7",
                     "average_tps": 100 * round_number + i, "peak_tps": 110,
                     "drop_rate": 0.0, "client_stake": 100, "send_start": None,
                     "send_end": None, "log": f"bench-{i}.log"} for i in (1, 2)]

        paths = []
        def fake_run(trial):
            trial.path.mkdir()
            paths.append(trial.path)
            return rows(len(paths))

        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            path = Path(directory)
            with mock.patch.object(experiment.Trial, "run", autospec=True, side_effect=fake_run) as run:
                experiment.run_repetitions(config, path, {})
            self.assertEqual(run.call_count, 2)
            with (path / "repetitions.csv").open(newline="") as output:
                repetitions = list(csv.DictReader(output))
            with (path / "summary.csv").open(newline="") as output:
                clients = list(csv.DictReader(output))
            self.assertEqual([row["repetition"] for row in repetitions], ["1", "2"])
            self.assertEqual([float(row["mean_tps"]) for row in repetitions], [101.5, 201.5])
            self.assertEqual([row["trial"] for row in clients], ["1", "1", "2", "2"])
            self.assertEqual(len(list(path.glob("trial-*/summary.csv"))), 2)

    def test_completed_round_csv_survives_later_failure(self):
        config = experiment.load_config(experiment.ROOT / "configs/experiments/two-validators-12.toml")
        config["run"]["repetitions"] = 2
        row = {"validator": 1, "host": "192.168.120.7", "cpus": "5,7", "average_tps": 50,
               "peak_tps": 60, "drop_rate": 0, "client_stake": 100, "send_start": None,
               "send_end": None, "log": "bench-1.log"}
        attempts = []
        def fake_run(trial):
            attempts.append(trial.path)
            if len(attempts) > 1:
                raise RuntimeError("failed")
            trial.path.mkdir()
            return [row]

        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            path = Path(directory)
            with mock.patch.object(experiment.Trial, "run", autospec=True, side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    experiment.run_repetitions(config, path, {})
            with (path / "repetitions.csv").open(newline="") as output:
                self.assertEqual([line["repetition"] for line in csv.DictReader(output)], ["1"])

    def test_parse_and_summarize(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            path = Path(directory) / "bench.log"
            path.write_text(
                "[2026-10-09T17:59:59Z INFO solana_bench_tps] Stake for specified client_node_id: 123, total stake: 123\n"
                "[2026-10-09T18:00:00Z INFO solana_bench_tps::bench] Sampling TPS every 1 second...\n"
                "[2026-10-09T18:00:50Z INFO solana_bench_tps::bench] Highest TPS: 90.00 sampling period 1s drop rate: 0.05\n"
                "[2026-10-09T18:00:51Z INFO solana_bench_tps::bench] Average TPS: 60.00\n"
                "\x1b[1;31mAverage TPS: 60.00\x1b[0m\n")
            first = experiment.parse_bench(path, 1, "192.168.120.7", "5,7")
            second = experiment.parse_bench(path, 2, "192.168.120.9", "9,11")
            summary = experiment.summarize([first, second])
            self.assertEqual(summary["mean_tps"], 60)
            self.assertEqual(summary["total_tps"], 120)
            self.assertEqual(second["peak_tps"], 90)
            self.assertEqual(second["drop_rate"], 0.05)
            self.assertEqual(second["client_stake"], 123)
            self.assertEqual(summary["approximate_sampling_overlap_seconds"], 51)
            self.assertEqual(json.loads(json.dumps(summary))["clients"][0]["validator"], 1)
            experiment.write_summary(Path(directory), summary)
            self.assertNotIn("percent_of_solo", (Path(directory) / "summary.csv").read_text())

    def test_single_validator_summary(self):
        row = {"validator": 1, "average_tps": 50, "send_start": None, "send_end": None}
        summary = experiment.summarize([row])
        self.assertEqual(summary["mean_tps"], 50)
        self.assertEqual(summary["total_tps"], 50)
        self.assertIsNone(summary["approximate_sampling_overlap_seconds"])

    def test_stops_only_its_own_process_group(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            trial = experiment.Trial(experiment.load_config(
                experiment.ROOT / "configs/experiments/three-validators.toml"),
                Path(directory), os.environ.copy())
            proc = trial.launch("dummy", ["/bin/sleep", "30"])
            try:
                trial.stop(proc)
                self.assertIsNotNone(proc.poll())
                self.assertFalse(trial.active)
            finally:
                trial.stop(proc)

    def test_readiness_requires_matching_identity_and_stake(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            config = experiment.load_config(
                experiment.ROOT / "configs/experiments/three-validators.toml")
            config["run"]["startup_delay_seconds"] = 0
            trial = experiment.Trial(config, Path(directory), os.environ.copy())
            trial.identities = ["expected"]
            proc = mock.Mock()
            proc.poll.return_value = None
            response = io.BytesIO(b'{"result":{"current":[{"nodePubkey":"expected","activatedStake":100}]}}')
            with mock.patch.object(trial.opener, "open", return_value=response):
                trial.ready(0, proc)

    def test_full_trial_orchestration_with_fake_processes(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            config = experiment.load_config(
                experiment.ROOT / "configs/experiments/three-validators.toml")
            trial = experiment.Trial(config, Path(directory) / "trial", os.environ.copy())
            running_before_start = []

            def fake_ledger(state, _env):
                ledger = state / "ledger"
                ledger.mkdir()
                return ledger, state / "faucet.json"

            def fake_validator(_self, i, _ledger, _faucet, _config):
                running_before_start.append(len(_self.active))
                return _self.launch(f"validator-{i + 1}", ["/bin/sleep", "30"])

            def fake_bench(_self, i):
                name = f"bench-{i + 1}"
                proc = _self.launch(name, ["/bin/true"])
                (_self.path / f"{name}.log").write_text(
                    "[2026-10-09T18:00:00Z INFO solana_bench_tps] Stake for specified client_node_id: 100, total stake: 100\n"
                    "[2026-10-09T18:00:01Z INFO solana_bench_tps::bench] Sampling TPS every 1 second...\n"
                    "[2026-10-09T18:00:51Z INFO solana_bench_tps::bench] Average TPS: 50\n")
                return proc, name, "5,7"

            with mock.patch.object(experiment, "create_ledger", side_effect=fake_ledger), \
                 mock.patch.object(experiment.subprocess, "check_output", return_value="pubkey"), \
                 mock.patch.object(experiment.Trial, "validator", fake_validator), \
                 mock.patch.object(experiment.Trial, "bench", fake_bench), \
                 mock.patch.object(experiment.Trial, "ready"):
                rows = trial.run()
            self.assertEqual(len(rows), 3)
            self.assertEqual(running_before_start, [0, 1, 2])
            self.assertFalse(trial.active)
            self.assertEqual(experiment.summarize(rows)["mean_tps"], 50)

    def test_one_validator_trial_runs_once(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
            config = experiment.load_config(
                experiment.ROOT / "configs/experiments/one-validator-8.toml")
            trial = experiment.Trial(config, Path(directory) / "trial", os.environ.copy())
            ledger = trial.path / "private" / "validator-1" / "ledger"

            def fake_ledger(_state, _env):
                ledger.mkdir()
                return ledger, ledger.parent / "faucet.json"

            with mock.patch.object(experiment, "create_ledger", side_effect=fake_ledger), \
                 mock.patch.object(experiment.subprocess, "check_output", return_value="pubkey"), \
                 mock.patch.object(experiment.Trial, "validator", return_value=mock.Mock(pid=1)) as start, \
                 mock.patch.object(experiment.Trial, "ready"), \
                 mock.patch.object(experiment.Trial, "bench", return_value=(mock.Mock(), "bench-1", "5,7")) as bench, \
                 mock.patch.object(experiment.Trial, "wait"), \
                 mock.patch.object(experiment.Trial, "stop"), \
                 mock.patch.object(experiment, "parse_bench", return_value={"average_tps": 50}) as parse:
                self.assertEqual(trial.run(), [{"average_tps": 50}])
            start.assert_called_once()
            bench.assert_called_once_with(0)
            parse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
