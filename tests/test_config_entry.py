"""設定ファイル (config.yaml) と raiya-cli / raiya-mcp-server / raiya-inspect の起動時の配線 pin。
spec: docs/superpowers/specs/2026-10-23-config-file-design.md §5.2 / §5.3 / §9 項目 7。LLM は使わない。"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from raiya import config, inspect_cli  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from tests.test_inspect import _seed, _write_state  # noqa: E402
from tests.test_mcp_server import HAS_MCP, requires_mcp  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = os.path.join(self.root, "config.yaml")
        self.cwd = os.path.join(self.root, "cwd")
        os.makedirs(self.cwd)
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": self.cfg})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RAIYA_STATE_DIR", None)
        config._reset()
        self.addCleanup(config._reset)

    def write_cfg(self, text):
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(text)
        config._reset()


class TestInspectPrecedence(_Case):
    def _state(self, name, content):
        path = os.path.join(self.root, name)
        _write_state(path, [_seed(1, ["x"], content=content, project=None)])
        return path

    def _show(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inspect_cli.main(["--json"] + list(argv) + ["show"])
        return code, out.getvalue(), err.getvalue()

    def test_arg_over_env_over_file_over_default(self):
        arg_state = self._state("arg", "FROM_ARG")
        env_state = self._state("env", "FROM_ENV")
        file_state = self._state("file", "FROM_FILE")
        self.write_cfg("state_dir: {}\n".format(file_state))
        code, out, _ = self._show()
        self.assertEqual(code, 0)
        self.assertIn("FROM_FILE", out)
        os.environ["RAIYA_STATE_DIR"] = env_state
        self.assertIn("FROM_ENV", self._show()[1])
        code, out, _ = self._show("--state-dir", arg_state)
        self.assertIn("FROM_ARG", out)
        self.assertNotIn("FROM_ENV", out)

    def test_default_when_no_file_is_cwd_state_and_does_not_create_it(self):
        self.write_cfg("")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.cwd)
        code, _, err = self._show()
        self.assertEqual(code, 2)  # ./state/alaya.json が無い
        self.assertEqual(os.listdir(self.cwd), [])

    def test_bad_key_reports_one_stderr_line_and_proceeds(self):
        state = self._state("file", "FROM_FILE")
        self.write_cfg("state_dir: {}\nllm:\n  model: no\n".format(state))
        code, out, err = self._show()
        self.assertEqual(code, 0)
        self.assertIn("FROM_FILE", out)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("raiya-inspect", err)
        self.assertIn("llm.model", err)

    def test_lost_state_dir_exits_2_unless_arg_or_env(self):
        arg_state = self._state("arg", "FROM_ARG")
        self.write_cfg("{ not yaml: [")
        code, out, err = self._show()
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("state_dir", err)
        code, out, err = self._show("--state-dir", arg_state)  # 明示の引数は救う
        self.assertEqual(code, 0)
        self.assertIn("FROM_ARG", out)
        self.assertEqual(len(err.splitlines()), 1)  # 報告はする
        os.environ["RAIYA_STATE_DIR"] = arg_state  # env も救う
        self.assertEqual(self._show()[0], 0)

    def test_broken_config_still_leaves_state_dir_bytes_identical(self):
        state = self._state("arg", "FROM_ARG")
        self.write_cfg("llm:\n  nope: 1\n")
        MemoryEngine(state_dir=state, new_life=False)  # .lock を先に作る

        def digest():
            out = {}
            for root, _, files in os.walk(self.root):
                for name in files:
                    full = os.path.join(root, name)
                    with open(full, "rb") as f:
                        out[os.path.relpath(full, self.root)] = hashlib.sha256(f.read()).hexdigest()
            return out

        before = digest()
        self.assertEqual(self._show("--state-dir", state)[0], 0)
        self.assertEqual(digest(), before)


class TestLostStateDirSubprocess(_Case):
    def _run(self, args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAIYA_")}
        env["RAIYA_CONFIG"] = self.cfg
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run([sys.executable] + args, input="", capture_output=True, text=True,
                              env=env, cwd=self.cwd, timeout=60)

    def test_cli_and_mcp_server_exit_2_and_create_nothing(self):
        self.write_cfg("{ not yaml: [")
        targets = [("cli", ["-m", "raiya.cli"])]
        if HAS_MCP:
            targets.append(("mcp", ["-m", "raiya.mcp_server"]))
        for name, args in targets:
            with self.subTest(name):
                r = self._run(args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
                self.assertIn("state_dir", r.stderr)
                self.assertEqual(os.listdir(self.cwd), [])  # ./state へ落ちていない

    @requires_mcp
    def test_mcp_server_does_not_import_backend_even_with_config(self):
        code = (
            "import sys\nfrom raiya import mcp_server\n"
            "print('raiya.backend' in sys.modules)\n"
        )
        self.write_cfg("inject_dispositions: true\n")
        r = self._run(["-c", code])
        self.assertEqual(r.stdout.strip(), "False", r.stderr)


if __name__ == "__main__":
    unittest.main()
