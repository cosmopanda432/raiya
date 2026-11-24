import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import manual_smoke_run  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402


class TestRunSmoke(unittest.TestCase):
    """manual_smoke_run.py: `_receipts_dir` と `subprocess.run` を差し替え、
    実 LLM を一切呼ばずに `run_smoke()` の分岐を確認する。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_receipts_dir = manual_smoke_run._receipts_dir
        self._saved_subprocess_run = manual_smoke_run.subprocess.run
        manual_smoke_run._receipts_dir = lambda: self.tmpdir

    def tearDown(self):
        manual_smoke_run._receipts_dir = self._saved_receipts_dir
        manual_smoke_run.subprocess.run = self._saved_subprocess_run
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_existing_smoke(self, blocked):
        out_path = os.path.join(self.tmpdir, "smoke.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                {"blocked": blocked, "generated_at": "existing-marker", "quarantine_count": 0, "class_counts": {}},
                f,
            )
        return out_path

    def test_non_blocked_existing_receipt_short_circuits_without_calling_subprocess(self):
        self._write_existing_smoke(blocked=False)

        def _explode(*args, **kwargs):
            raise AssertionError("subprocess.run が呼ばれてはいけない (非blocked receipt が既存)")

        manual_smoke_run.subprocess.run = _explode

        result = manual_smoke_run.run_smoke(force=False)
        self.assertEqual(result["generated_at"], "existing-marker")

    def test_force_true_attempts_rerun_via_subprocess_even_with_existing_receipt(self):
        self._write_existing_smoke(blocked=False)
        calls = []

        class _FakeCompletedProcess:
            returncode = 1  # 実 LLM を呼ばない最小 fake。state を生成しないので blocked になる
            stdout = ""
            stderr = "fake failure (subprocess replaced for test, no real claude CLI call)"

        def _fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeCompletedProcess()

        manual_smoke_run.subprocess.run = _fake_run
        # LOW-4: run_smoke() の blocked 分岐は receipt JSON を print() で stdout へ流す。
        # unittest discover の集約出力に紛れ込み「Ran N tests / OK」の末尾を埋もれさせるため、
        # /透視 テスト (tests/test_cli.py) と同じ手法で抑止する。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = manual_smoke_run.run_smoke(force=True)
        self.assertEqual(len(calls), 1)  # subprocess.run が実際に (1回) 呼ばれた
        self.assertTrue(result["blocked"])  # fake の returncode!=0 なので blocked 記録になる

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

        real_mkdtemp = manual_smoke_run.tempfile.mkdtemp
        manual_smoke_run.tempfile.mkdtemp = lambda *a, **kw: real_mkdtemp(*a, **dict(kw, dir=self.tmpdir))
        self.addCleanup(setattr, manual_smoke_run.tempfile, "mkdtemp", real_mkdtemp)
        manual_smoke_run.subprocess.run = _fake_run
        with contextlib.redirect_stdout(io.StringIO()):
            result = manual_smoke_run.run_smoke(force=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["blocked"])
        self.assertNotIn("state_dir", result)
        with open(os.path.join(self.tmpdir, "smoke.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["blocked"])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "judgment_smoke.json")))

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
        real_mkdtemp = manual_smoke_run.tempfile.mkdtemp
        saved_mkdtemp = manual_smoke_run.tempfile.mkdtemp
        manual_smoke_run.tempfile.mkdtemp = lambda *a, **kw: real_mkdtemp(*a, **dict(kw, dir=self.tmpdir))
        self.addCleanup(setattr, manual_smoke_run.tempfile, "mkdtemp", saved_mkdtemp)
        manual_smoke_run.subprocess.run = _fake_run
        with contextlib.redirect_stdout(io.StringIO()):
            manual_smoke_run.run_smoke(force=True)
        self.assertEqual(len(calls), 1)
        kwargs = calls[0][1]
        cwd = kwargs["cwd"]
        self.assertTrue(os.path.realpath(cwd).startswith(os.path.realpath(self.tmpdir) + os.sep), cwd)
        self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(manual_smoke_run.BASE_DIR))
        self.assertEqual(cwd, kwargs["env"]["RAIYA_STATE_DIR"])
        self.assertIn(manual_smoke_run.BASE_DIR, kwargs["env"]["PYTHONPATH"].split(os.pathsep))
        receipt_path = os.path.join(self.tmpdir, "smoke.json")
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
        self._write_existing_smoke(blocked=True)
        calls = []

        class _FakeCompletedProcess:
            returncode = 1
            stdout = ""
            stderr = "fake failure"

        def _fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeCompletedProcess()

        manual_smoke_run.subprocess.run = _fake_run
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            manual_smoke_run.run_smoke(force=False)
        self.assertEqual(len(calls), 1)


class TestMeasureFromState(unittest.TestCase):
    """`_measure_from_state` の保持/累計が相異なる値を正しく区別すること。
    state は `snapshot.write_state` で組み立てる (保存形式を知らない)。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_quarantine_count_and_retained_count_can_differ(self):
        # 累計12 (imprint_log 上の 遍計所執 decision 行) だが、保持上限による evict で
        # quarantine リストには10件しか残っていない、という合成データ。
        imprint_log = [{"decision": "遍計所執", "reason": "reason{}".format(i)} for i in range(12)]
        quarantine = [{"id": i} for i in range(3, 13)]  # 10件保持 (先頭2件は evict 済み想定)
        write_state(self.tmpdir, {"quarantine": quarantine, "seeds": [], "imprint_log": imprint_log})

        measured = manual_smoke_run._measure_from_state(self.tmpdir)
        self.assertEqual(measured["quarantine_count"], 12)
        self.assertEqual(measured["quarantine_retained_count"], 10)
        self.assertNotEqual(measured["quarantine_count"], measured["quarantine_retained_count"])


if __name__ == "__main__":
    unittest.main()
