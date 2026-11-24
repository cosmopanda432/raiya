"""recall_latency_harness.py (休眠と蓄積 設計書 §3.1) を小さい N で回す。LLM は使わない。

自作の tmp に `--out` するだけで、`receipts/recall_latency.json` には触れない (sha256 で確認)。
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import recall_latency_harness as harness  # noqa: E402

REPO_RECEIPT = os.path.join(harness.BASE_DIR, "receipts", "recall_latency.json")


def _quiet_main(argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return harness.main(argv)


def _sha(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestRecallLatencyHarness(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="raiya_latency_test_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def test_small_run_writes_expected_shape_and_leaves_repo_receipt(self):
        before = _sha(REPO_RECEIPT)
        _quiet_main(["--out", self.out, "--sizes", "20", "--repeats", "1", "--no-hook"])
        self.assertEqual(_sha(REPO_RECEIPT), before)
        with open(os.path.join(self.out, "recall_latency.json"), encoding="utf-8") as f:
            data = json.load(f)
        for key in ("generated_at", "environment", "params", "budget", "rows",
                    "first_n_exceeding_soft", "first_n_exceeding_hard"):
            self.assertIn(key, data)
        self.assertEqual({r["variant"] for r in data["rows"]}, {"path", "nopath"})
        by = {r["variant"]: r for r in data["rows"]}
        self.assertEqual(by["nopath"]["stat_calls"], 0)
        self.assertGreater(by["path"]["stat_calls"], 0)
        for row in data["rows"]:
            self.assertEqual(row["n"], 20)
            self.assertIsNone(row["hook_ms"])
            self.assertGreater(row["recall_ms"]["median"], 0)
            self.assertGreater(row["db_bytes"], 0)
        self.assertIsNone(data["first_n_exceeding_soft"])
        self.assertEqual(data["budget"]["soft_ms"], 1000)
        self.assertEqual(data["budget"]["hard_ms"], 10000)

    def test_rows_carry_group_stats_and_narrative_len(self):
        _quiet_main(["--out", self.out, "--sizes", "30", "--repeats", "1", "--no-hook"])
        with open(os.path.join(self.out, "recall_latency.json"), encoding="utf-8") as f:
            data = json.load(f)
        for row in data["rows"]:
            gs = row["group_stats"]
            self.assertEqual(sorted(gs), ["group_ms", "groups", "max_size"])
            self.assertIsInstance(gs["groups"], int)
            self.assertIsInstance(gs["max_size"], int)
            self.assertIsInstance(gs["group_ms"], float)
            self.assertGreaterEqual(gs["group_ms"], 0)
            self.assertTrue(gs["groups"] == 0 or gs["max_size"] >= 2)
            self.assertIsInstance(row["narrative_len"], int)
            self.assertGreaterEqual(row["narrative_len"], 0)
            self.assertIsInstance(row["exceeds_group_ms"], bool)
        self.assertIn("group_first_n_exceeding_100ms", data)
        self.assertIsNone(data["group_first_n_exceeding_100ms"])
        self.assertEqual(data["budget"]["group_ms"], 100)
        self.assertIn("group_ms", data["first_n_exceeding_by_variant"])

    def test_environment_has_no_host_or_tmp_path(self):
        _quiet_main(["--out", self.out, "--sizes", "20", "--repeats", "1", "--no-hook"])
        with open(os.path.join(self.out, "recall_latency.json"), encoding="utf-8") as f:
            text = f.read()
        import platform
        if len(platform.node()) > 4:
            self.assertNotIn(platform.node(), text)
        self.assertNotIn(tempfile.gettempdir() + os.sep + "recall_latency_", text)
        self.assertEqual(sorted(json.loads(text)["environment"]),
                         ["cpu_count", "machine", "python", "release", "system"])

    def test_import_and_run_leave_environment_alone(self):
        before = dict(os.environ)
        _quiet_main(["--out", self.out, "--sizes", "20", "--repeats", "1", "--no-hook"])
        self.assertEqual(dict(os.environ), before)

    def test_bad_sizes_exit(self):
        with self.assertRaises(SystemExit):
            _quiet_main(["--out", self.out, "--sizes", "0"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "recall_latency.json")))


class TestRecallLatencyLogs(unittest.TestCase):
    """--logs (Issue #36/#37 の第一段)。小さい行数で回し、receipts/ には触れない。"""

    ARGS = ["--logs", "--n", "20", "--imprint-sizes", "0,30", "--manas-sizes", "0,10",
            "--combos", "30:10", "--quiet-combos", "30:0", "--repeats", "1", "--no-hook"]

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="raiya_latency_logs_test_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def _run(self):
        before = {n: _sha(os.path.join(harness.BASE_DIR, "receipts", n))
                  for n in ("recall_latency.json", "recall_latency_logs.json")}
        _quiet_main(self.ARGS + ["--out", self.out])
        after = {n: _sha(os.path.join(harness.BASE_DIR, "receipts", n)) for n in before}
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(self.out, "recall_latency.json")))
        with open(os.path.join(self.out, "recall_latency_logs.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_shape_breakdown_and_sizes(self):
        data = self._run()
        for key in ("generated_at", "environment", "params", "budget", "rows"):
            self.assertIn(key, data)
        self.assertEqual([(r["imprint_log_rows"], r["manas_log_rows"]) for r in data["rows"]],
                         [(0, 0), (30, 0), (0, 10), (30, 10), (30, 0)])
        self.assertEqual([r["germinating"] for r in data["rows"]], [True] * 4 + [False])
        keys = {"load_ms", "flush_ms", "commit_ms", "rest_ms", "instrumented_recall_ms", "sections"}
        for row in data["rows"]:
            self.assertEqual(row["n"], 20)
            self.assertIsNone(row["hook_ms"])
            self.assertGreater(row["recall_ms"]["median"], 0)
            self.assertTrue(keys <= set(row["breakdown"]))
            # recall は書く区間を 1 つだけ持つ (読み・差分の書き込み・COMMIT が 1 回ずつ)。
            self.assertEqual(row["breakdown"]["sections"], 1)
            for key in ("load_ms", "flush_ms", "commit_ms"):
                self.assertGreaterEqual(row["breakdown"][key], 0)
            self.assertGreater(row["breakdown"]["commit_ms"], 0)  # synchronous=FULL の COMMIT は必ず時間がかかる
        by = {(r["imprint_log_rows"], r["manas_log_rows"]): r for r in data["rows"][:4]}
        self.assertGreater(by[(30, 0)]["imprint_log_body_bytes"], by[(0, 0)]["imprint_log_body_bytes"])
        self.assertEqual(by[(0, 0)]["imprint_log_body_bytes"], 0)
        self.assertGreater(by[(0, 10)]["manas_log_body_bytes"], 0)
        self.assertEqual(by[(30, 10)]["imprint_log_body_bytes"], by[(30, 0)]["imprint_log_body_bytes"])
        self.assertGreater(by[(30, 10)]["db_bytes"], by[(0, 0)]["db_bytes"])

    def test_environment_clean_and_no_real_state(self):
        before = dict(os.environ)
        data = self._run()
        self.assertEqual(dict(os.environ), before)
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(tempfile.gettempdir() + os.sep + "recall_latency_", text)
        self.assertNotIn(".raiya", text)
        self.assertEqual(sorted(data["environment"]), ["cpu_count", "machine", "python", "release", "system"])

    def test_synthetic_rows_are_deterministic_and_sized(self):
        a = harness.build_imprint_log(40)
        self.assertEqual(a, harness.build_imprint_log(40))
        avg = len(harness._file_json_bytes(a)) / 40
        self.assertTrue(0.8 * harness.IMPRINT_ROW_BYTES < avg < 1.2 * harness.IMPRINT_ROW_BYTES, avg)
        m = harness.build_manas_state(40)
        avg = len(harness._file_json_bytes(m)) / 40
        self.assertTrue(0.8 * harness.MANAS_ROW_BYTES < avg < 1.2 * harness.MANAS_ROW_BYTES, avg)
        self.assertEqual(set(m["log"][0]), {"life", "ego_weight", "injected_text", "seed_ids", "index_ids",
                                            "ts", "session_id", "project"})

    def test_bad_args_exit(self):
        for bad in (["--logs", "--imprint-sizes", "-1"], ["--logs", "--combos", "5"], ["--logs", "--n", "0"],
                    ["--logs", "--sizes", "20"], ["--n", "20"], ["--imprint-sizes", "0"], ["--manas-sizes", "0"],
                    ["--combos", "1:1"], ["--quiet-combos", "1:1"]):
            with self.assertRaises(SystemExit, msg=bad):
                _quiet_main(["--out", self.out, "--no-hook"] + bad)
        for name in ("recall_latency.json", "recall_latency_logs.json"):
            self.assertFalse(os.path.exists(os.path.join(self.out, name)))

    def test_probe_reads_backend_counters_and_patches_nothing(self):
        """計器は保存層の `timing_totals` を引くだけ (sqlite3 化 #38 スライス 3)。raiya の属性は何も差し替えない。"""
        import shutil as _shutil
        from raiya import storage
        from raiya.engine import MemoryEngine
        before = {n: storage.SqliteBackend.__dict__[n] for n in ("section", "lock", "open_stores")}
        copyfile = _shutil.copyfile
        state = tempfile.mkdtemp(prefix="raiya_probe_test_")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        engine = MemoryEngine(state_dir=os.path.join(state, "s"), new_life=False)
        with harness._Probe(engine) as probe:
            engine.recall("x", project=None)
        self.assertEqual(probe.sections, 1)
        self.assertGreater(probe.ms["commit"], 0)
        self.assertIs(_shutil.copyfile, copyfile)
        for n, orig in before.items():
            self.assertIs(storage.SqliteBackend.__dict__[n], orig, n)


if __name__ == "__main__":
    unittest.main()
