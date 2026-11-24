import contextlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import manual_smoke_long_run  # noqa: E402
import receipt_harness  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402


class TestRunSmokeLong(unittest.TestCase):
    """§5.3 P-M1 再測 runner: `_receipts_dir` と `subprocess.run` を差し替え、
    実 LLM を一切呼ばずに `run_smoke_long()` の再走ガードを確認する
    (`tests/test_manual_smoke_run.py::TestRunSmoke` と同型)。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_receipts_dir = manual_smoke_long_run._receipts_dir
        self._saved_subprocess_run = manual_smoke_long_run.subprocess.run
        manual_smoke_long_run._receipts_dir = lambda: self.tmpdir

    def tearDown(self):
        manual_smoke_long_run._receipts_dir = self._saved_receipts_dir
        manual_smoke_long_run.subprocess.run = self._saved_subprocess_run
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_existing_smoke_long(self, blocked):
        out_path = os.path.join(self.tmpdir, "smoke_long.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                {"blocked": blocked, "generated_at": "existing-marker", "quarantine_count": 0, "class_counts": {}},
                f,
            )
        return out_path

    def test_non_blocked_existing_receipt_short_circuits_without_calling_subprocess(self):
        self._write_existing_smoke_long(blocked=False)

        def _explode(*args, **kwargs):
            raise AssertionError("subprocess.run が呼ばれてはいけない (非blocked receipt が既存)")

        manual_smoke_long_run.subprocess.run = _explode

        result = manual_smoke_long_run.run_smoke_long(force=False)
        self.assertEqual(result["generated_at"], "existing-marker")

    def test_force_true_attempts_rerun_via_subprocess_even_with_existing_receipt(self):
        self._write_existing_smoke_long(blocked=False)
        calls = []

        class _FakeCompletedProcess:
            returncode = 1  # 実 LLM を呼ばない最小 fake。state を生成しないので blocked になる
            stdout = ""
            stderr = "fake failure (subprocess replaced for test, no real claude CLI call)"

        def _fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeCompletedProcess()

        manual_smoke_long_run.subprocess.run = _fake_run
        # LOW-4 と同型: run_smoke_long() の blocked 分岐は receipt JSON を print() で
        # stdout へ流すため、discover の集約出力に紛れ込まないよう抑止する。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = manual_smoke_long_run.run_smoke_long(force=True)
        self.assertEqual(len(calls), 1)  # subprocess.run が実際に (1回) 呼ばれた
        self.assertTrue(result["blocked"])  # fake の returncode!=0 なので blocked 記録になる
        # 40往復の台本がそのまま subprocess へ渡っていること (10往復runnerとの取り違え防止)。
        self.assertEqual(result["turns_scripted"], len(manual_smoke_long_run.SCRIPTED_TURNS))
        self.assertEqual(len(manual_smoke_long_run.SCRIPTED_TURNS), 40)

    def test_child_exit_zero_without_state_is_blocked(self):
        # 子が returncode 0 で終わっても state を何も書かなければ blocked (engine を空 dir に作って
        # 零件で成功扱いにしない)。消すのは自分が作った tmpdir 配下だけ。
        calls = []

        class _FakeCompletedProcess:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(*args, **kwargs):
            calls.append(kwargs)
            return _FakeCompletedProcess()

        real_mkdtemp = manual_smoke_long_run.tempfile.mkdtemp
        manual_smoke_long_run.tempfile.mkdtemp = lambda *a, **kw: real_mkdtemp(*a, **dict(kw, dir=self.tmpdir))
        self.addCleanup(setattr, manual_smoke_long_run.tempfile, "mkdtemp", real_mkdtemp)
        manual_smoke_long_run.subprocess.run = _fake_run
        with contextlib.redirect_stdout(io.StringIO()):
            result = manual_smoke_long_run.run_smoke_long(force=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["blocked"])
        self.assertNotIn("state_dir", result)
        with open(os.path.join(self.tmpdir, "smoke_long.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["blocked"])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "judgment_smoke_long.json")))

    def test_cli_subprocess_runs_from_neutral_cwd_not_repo_root(self):
        # Issue #29 (refs #26): 会話応答の `claude -p` が cwd 経由でリポジトリの
        # AGENTS.md / CLAUDE.md を読まないよう、cwd は一時 state dir でなければならない。
        calls = []

        class _FakeCompletedProcess:
            returncode = 1
            stdout = ""
            stderr = "fake failure (no real claude CLI call)"

        def _fake_run(*args, **kwargs):
            assert os.path.isdir(kwargs["cwd"]), kwargs["cwd"]
            calls.append((args, kwargs))
            return _FakeCompletedProcess()

        # runner の mkdtemp をこのテストの tmpdir 配下に向け、消すのは自分が作った物だけにする
        # (runtime 由来の cwd を rmtree しない — 2026-10-24 のリポジトリ消失事故の再発防止)。
        real_mkdtemp = manual_smoke_long_run.tempfile.mkdtemp
        saved_mkdtemp = manual_smoke_long_run.tempfile.mkdtemp
        manual_smoke_long_run.tempfile.mkdtemp = lambda *a, **kw: real_mkdtemp(*a, **dict(kw, dir=self.tmpdir))
        self.addCleanup(setattr, manual_smoke_long_run.tempfile, "mkdtemp", saved_mkdtemp)
        manual_smoke_long_run.subprocess.run = _fake_run
        with contextlib.redirect_stdout(io.StringIO()):
            manual_smoke_long_run.run_smoke_long(force=True)
        self.assertEqual(len(calls), 1)
        kwargs = calls[0][1]
        cwd = kwargs["cwd"]
        self.assertTrue(os.path.realpath(cwd).startswith(os.path.realpath(self.tmpdir) + os.sep), cwd)
        self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(manual_smoke_long_run.BASE_DIR))
        self.assertEqual(cwd, kwargs["env"]["RAIYA_STATE_DIR"])
        self.assertIn(manual_smoke_long_run.BASE_DIR, kwargs["env"]["PYTHONPATH"].split(os.pathsep))
        receipt_path = os.path.join(self.tmpdir, "smoke_long.json")
        with open(receipt_path, encoding="utf-8") as f:
            receipt = json.load(f)
        self.assertTrue(receipt["blocked"])  # fake は returncode=1 → blocked 受領証
        self.assertFalse(os.path.exists(os.path.join(cwd, "AGENTS.md")))
        self.assertFalse(os.path.exists(os.path.join(cwd, "CLAUDE.md")))
        d = os.path.realpath(cwd)
        while True:
            self.assertFalse(os.path.exists(os.path.join(d, ".git")), d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent

    def test_blocked_existing_receipt_is_not_short_circuited_even_without_force(self):
        self._write_existing_smoke_long(blocked=True)
        calls = []

        class _FakeCompletedProcess:
            returncode = 1
            stdout = ""
            stderr = "fake failure"

        def _fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeCompletedProcess()

        manual_smoke_long_run.subprocess.run = _fake_run
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            manual_smoke_long_run.run_smoke_long(force=False)
        self.assertEqual(len(calls), 1)

    def test_rerun_guard_is_independent_from_the_short_runners_receipt(self):
        # smoke_long.json 専用のガードであること (smoke.json 側の既存状態に影響されない)。
        # smoke.json 相当のファイルを「blocked=False」で置いても smoke_long.json 側の
        # ガードには関与しない (別名の receipt を見ている) ことを確認する。
        short_runner_path = os.path.join(self.tmpdir, "smoke.json")
        with open(short_runner_path, "w", encoding="utf-8") as f:
            json.dump({"blocked": False, "generated_at": "unrelated-short-runner-receipt"}, f)

        calls = []

        class _FakeCompletedProcess:
            returncode = 1
            stdout = ""
            stderr = "fake failure"

        def _fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeCompletedProcess()

        manual_smoke_long_run.subprocess.run = _fake_run
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # smoke_long.json は存在しないため force=False でも再走を試みる。
            manual_smoke_long_run.run_smoke_long(force=False)
        self.assertEqual(len(calls), 1)


# MED-1: hit を判定するのは `receipt_harness.judge_p_m1`/`judge_p_m2` (凍結述語) であって
# `_write_smoke_long_judgment` 自身がハードコードした値ではないことの pin。
_MEASURED_HIT = {
    "proposal_total": 4,
    "class_counts": {"圓成實": 1, "依他起": 3, "遍計所執": 1, "reject": 0},
    "reject_reason_distribution": {},
    "quarantine_count": 1,
    "seed_count": 3,
}
_MEASURED_MISS = {
    "proposal_total": 4,
    "class_counts": {"圓成實": 3, "依他起": 1, "遍計所執": 0, "reject": 0},
    "reject_reason_distribution": {},
    "quarantine_count": 0,
    "seed_count": 4,
}


class TestLongJudgmentUsesFrozenPredicates(unittest.TestCase):
    """MED-1: `_write_smoke_long_judgment` を `{"hit": True}` 直書きに差し替えても
    既存テストが全緑になってしまう穴を塞ぐ。hit が反転する2通りの measured を通し、
    出力の hit が `receipt_harness.judge_p_m1/judge_p_m2` の戻り値と一致することを
    assert する。加えてソースにハードコードされた `"hit": True` リテラルが無いことも
    確認する (テキストレベルの構造的 pin)。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_long_judgment_uses_the_same_frozen_predicates(self):
        # 2通りの measured で P-M1/P-M2 の hit がそれぞれ反転すること自体を前提として
        # 固定する (fixture 自体が無情報にならないよう)。
        self.assertNotEqual(
            receipt_harness.judge_p_m1(_MEASURED_HIT)["hit"],
            receipt_harness.judge_p_m1(_MEASURED_MISS)["hit"],
        )
        self.assertNotEqual(
            receipt_harness.judge_p_m2(_MEASURED_HIT)["hit"],
            receipt_harness.judge_p_m2(_MEASURED_MISS)["hit"],
        )

        for measured in (_MEASURED_HIT, _MEASURED_MISS):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                manual_smoke_long_run._write_smoke_long_judgment(self.tmpdir, measured)
            judgment_path = os.path.join(self.tmpdir, "judgment_smoke_long.json")
            with open(judgment_path, "r", encoding="utf-8") as f:
                judgment = json.load(f)
            self.assertEqual(judgment["P-M1"]["hit"], receipt_harness.judge_p_m1(measured)["hit"])
            self.assertEqual(judgment["P-M2"]["hit"], receipt_harness.judge_p_m2(measured)["hit"])

        # 構造的 pin: 実装ソースに hit のハードコードリテラルが無いこと。
        source = inspect.getsource(manual_smoke_long_run)
        self.assertNotIn('"hit": True', source)
        self.assertNotIn("'hit': True", source)
        self.assertNotIn('"hit": False', source)
        self.assertNotIn("'hit': False", source)


class TestLongJudgmentFileIndependence(unittest.TestCase):
    """MED-2: judgment_smoke_long.json への書き出しが judgment_smoke.json (10往復
    runner 側) を巻き込まない (別ファイル・上書きしない) ことの pin。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_receipts_dir = manual_smoke_long_run._receipts_dir
        self._saved_subprocess_run = manual_smoke_long_run.subprocess.run
        manual_smoke_long_run._receipts_dir = lambda: self.tmpdir

    def tearDown(self):
        manual_smoke_long_run._receipts_dir = self._saved_receipts_dir
        manual_smoke_long_run.subprocess.run = self._saved_subprocess_run
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_long_judgment_is_written_to_its_own_file_and_leaves_the_short_one_intact(self):
        # 番兵内容の judgment_smoke.json (10往復 runner 側の出力) を先に置いておく。
        sentinel_path = os.path.join(self.tmpdir, "judgment_smoke.json")
        with open(sentinel_path, "w", encoding="utf-8") as f:
            json.dump({"marker": "SHORT-RUNNER-JUDGMENT-DO-NOT-TOUCH", "P-M1": {"hit": "sentinel"}}, f)
        with open(sentinel_path, "rb") as f:
            sentinel_bytes_before = f.read()

        def _fake_run_success(*args, **kwargs):
            env = kwargs.get("env") or {}
            state_dir = env["RAIYA_STATE_DIR"]
            os.makedirs(state_dir, exist_ok=True)
            write_state(state_dir, {"alaya_meta": {"life": 1}, "quarantine": [], "seeds": [], "imprint_log": [{"decision": "圓成實", "reason": "ok"}]})

            class _FakeCompletedProcess:
                returncode = 0
                stdout = "fake ok"
                stderr = ""

            return _FakeCompletedProcess()

        manual_smoke_long_run.subprocess.run = _fake_run_success
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            manual_smoke_long_run.run_smoke_long(force=True)

        long_path = os.path.join(self.tmpdir, "judgment_smoke_long.json")
        self.assertTrue(os.path.isfile(long_path), msg="judgment_smoke_long.json が生成されていない")

        with open(sentinel_path, "rb") as f:
            sentinel_bytes_after = f.read()
        self.assertEqual(sentinel_bytes_before, sentinel_bytes_after)  # judgment_smoke.json はバイト列不変


if __name__ == "__main__":
    unittest.main()
