import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestCliSmoke(unittest.TestCase):
    """`python3 -m raiya.cli` 全体を `command` backend + パイプ入力で非対話実行する
    スモークテスト (LLM 実呼び出し無し)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmpdir, "state")
        script_path = os.path.join(self.tmpdir, "dummy_llm.sh")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "input=$(cat)\n"
                "case \"$input\" in\n"
                "  *JSON配列*) echo '[]' ;;\n"
                "  *自己申告文*) echo 'テスト用内観です。' ;;\n"
                "  *) echo 'テスト応答です。' ;;\n"
                "esac\n"
            )
        st = os.stat(script_path)
        os.chmod(script_path, st.st_mode | stat.S_IEXEC)
        self.script_path = script_path

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_cli(self, stdin_text):
        env = dict(os.environ)
        env["RAIYA_BACKEND"] = "command"
        env["RAIYA_LLM_CMD"] = self.script_path
        env["RAIYA_STATE_DIR"] = self.state_dir
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "raiya.cli"],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )

    def test_one_round_trip_smoke(self):
        stdin_text = "こんにちは、これはテストです\n/種子\n/終\n"
        result = self._run_cli(stdin_text)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("第1生", result.stdout)
        self.assertIn("テスト応答です。", result.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, "raiya.db")))
        for legacy in ("alaya.json", "manas.json", "imprint_log.json"):  # 保存層は raiya.db 一つ
            self.assertFalse(os.path.exists(os.path.join(self.state_dir, legacy)))

    def test_state_persists_across_runs_as_new_life(self):
        self._run_cli("こんにちは\n/終\n")
        result2 = self._run_cli("また来たよ\n/終\n")
        self.assertIn("第2生", result2.stdout)

    def test_legacy_json_state_exits_2_and_creates_nothing(self):
        # #86: 案内文「何も作らずに終了」どおり、`.lock` も作らない。
        os.makedirs(self.state_dir)
        with open(os.path.join(self.state_dir, "alaya.json"), "w") as f:
            f.write("{}")
        result = self._run_cli("/終\n")
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("脇に退けてから起動。何も作らずに終了", result.stderr)
        self.assertEqual(sorted(os.listdir(self.state_dir)), ["alaya.json"])

    def test_introspect_command_via_command_backend(self):
        result = self._run_cli("こんにちは\n/内観\n/終\n")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("テスト用内観です。", result.stdout)
        self.assertIn("ego_weight", result.stdout)


if __name__ == "__main__":
    unittest.main()
