import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# venv 作成 + `pip install -e .` の合計に許す上限。これを超えたら報告対象
# (テスト自体は失敗させない。単に目安として計測結果を出力する)。
_SETUP_BUDGET_SEC = 60


class TestPipInstallEntryPoints(unittest.TestCase):
    """(d) `pip install -e .` console scripts declared in pyproject.toml.

    Existing smoke uses `python3 -m raiya.cli` / `python3 -m raiya.mcp_server`.
    The advertised `raiya-cli` / `raiya-mcp-server` commands were untested.

    `raiya-cli`/`raiya-mcp-server` は `pip install -e .` を実行したその環境の
    venv/bin にしか生成されない。以前の実装は「このテストを動かしている
    `sys.executable` の隣に console script が既に在る」ことを前提にしており、
    `pip install -e .` 未実施の fresh な system python3 (clone 直後・CI) では
    `sys.executable` の隣 (例: /usr/bin/) に script が無いため必ず落ちていた。

    ここでは setUpClass で使い捨ての venv を作り、その venv へ本リポを
    `pip install -e ".[mcp]"` で editable install し (MCP サーバは公式 `mcp` SDK を
    必要とするため extra 込み。build backend の取得も含めネットワークが要る)、
    venv の bin/ 配下に生成された console script を使う。venv 作成・pip install の
    どちらかが失敗したら (ensurepip 不在・オフライン等) `unittest.SkipTest` で
    明示的に skip する (fail にしない)。
    """

    venv_dir = None
    bin_dir = None

    @classmethod
    def setUpClass(cls):
        started = time.monotonic()
        cls.venv_dir = tempfile.mkdtemp(prefix="raiya_entry_points_venv_")
        venv_python = os.path.join(cls.venv_dir, "bin", "python3")

        # LOW-2: venv 作成・pip install のどちらかが `subprocess.run(timeout=...)` の
        # `TimeoutExpired` を送出すると、以前はこの下の returncode チェック自体に
        # 到達できず (例外がそのまま setUpClass の外へ抜ける)、rmtree が一度も
        # 実行されないまま cls.venv_dir がリークしていた。setUpClass が例外で終わると
        # tearDownClass は呼ばれない (unittest の仕様) ため、ここで確実に後始末してから
        # SkipTest に変換する。
        try:
            create = subprocess.run(
                [sys.executable, "-m", "venv", cls.venv_dir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if create.returncode != 0 or not os.path.exists(venv_python):
                raise RuntimeError(
                    "venv 作成に失敗しました (returncode={}, stderr_tail={!r})".format(
                        create.returncode, (create.stderr or create.stdout)[-1000:]
                    )
                )

            install = subprocess.run(
                [venv_python, "-m", "pip", "install", "--quiet", "-e", REPO_ROOT + "[mcp]"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if install.returncode != 0:
                raise RuntimeError(
                    "pip install -e '.[mcp]' に失敗しました "
                    "(returncode={}, stderr_tail={!r})".format(
                        install.returncode, (install.stderr or install.stdout)[-1000:]
                    )
                )
        except Exception as e:
            shutil.rmtree(cls.venv_dir, ignore_errors=True)
            cls.venv_dir = None
            raise unittest.SkipTest(
                "venv 作成/pip install が失敗または例外で終了したため console script"
                " テストを skip します (setuptools 未取得・オフライン・timeout 等の"
                " 可能性): {}".format(e)
            )
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SETUP_BUDGET_SEC:
                print(
                    "[test_entry_points] setUpClass (venv作成+pip install) が {:.1f}秒"
                    " かかりました (目安 {}秒超)".format(elapsed, _SETUP_BUDGET_SEC),
                    file=sys.stderr,
                )

        cls.bin_dir = os.path.join(cls.venv_dir, "bin")

    @classmethod
    def tearDownClass(cls):
        if cls.venv_dir:
            shutil.rmtree(cls.venv_dir, ignore_errors=True)

    def _script(self, name):
        """setUpClass が editable install した venv 内の console script の path。"""
        return os.path.join(self.bin_dir, name)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmpdir, "state")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_raiya_cli_script_exists_beside_interpreter(self):
        path = self._script("raiya-cli")
        self.assertTrue(os.path.isfile(path), msg="missing {}".format(path))

    def test_raiya_mcp_server_script_exists_beside_interpreter(self):
        path = self._script("raiya-mcp-server")
        self.assertTrue(os.path.isfile(path), msg="missing {}".format(path))

    def test_raiya_cli_one_round_trip_via_console_script(self):
        script_path = os.path.join(self.tmpdir, "dummy_llm.sh")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "input=$(cat)\n"
                "case \"$input\" in\n"
                "  *JSON*) echo '[]' ;;\n"
                "  *) echo 'entry-point-ok' ;;\n"
                "esac\n"
            )
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)

        env = dict(os.environ)
        env["RAIYA_BACKEND"] = "command"
        env["RAIYA_LLM_CMD"] = script_path
        env["RAIYA_STATE_DIR"] = self.state_dir
        cli = self._script("raiya-cli")
        result = subprocess.run(
            [cli],
            input="hello\n",  # stdin EOF also ends the CLI loop (EOFError)
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("entry-point-ok", result.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, "raiya.db")))

    def test_raiya_mcp_server_initialize_via_console_script(self):
        import json

        env = dict(os.environ)
        env["RAIYA_STATE_DIR"] = self.state_dir
        server = self._script("raiya-mcp-server")
        proc = subprocess.Popen(
            [server],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )
        # initialize は SDK が読み取りループ内で同期処理するため、これ1件だけなら
        # 「流し込んで stdin を閉じる」でも応答は失われない。
        stdin_text = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "entry-points-test", "version": "0.0.1"},
                },
            }
        ) + "\n"
        stdout, stderr = proc.communicate(input=stdin_text, timeout=30)
        self.assertEqual(proc.returncode, 0, msg=stderr)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertEqual(response["id"], 1)
        self.assertIn("protocolVersion", response["result"])
        self.assertEqual(response["result"]["serverInfo"]["name"], "raiya")

    def test_raiya_mcp_server_help_mentions_http_transport(self):
        server = self._script("raiya-mcp-server")
        result = subprocess.run(
            [server, "--help"], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--transport", result.stdout)
        self.assertIn("http", result.stdout)


if __name__ == "__main__":
    unittest.main()
