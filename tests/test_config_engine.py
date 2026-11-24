"""設定ファイル (config.yaml) と engine の配線 pin。spec: docs/superpowers/specs/2026-10-23-config-file-design.md §3 / §4.2 / §7。"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from raiya import config, inspect_cli  # noqa: E402
from raiya.engine import DEFAULT_STATE_DIR, INJECT_DISPOSITIONS_ENV, MemoryEngine, resolve_state_dir  # noqa: E402
from tests.test_inspect import P, _seed, _write_state  # noqa: E402


class _CfgCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg_path = os.path.join(self.root, "config.yaml")
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": self.cfg_path})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in (INJECT_DISPOSITIONS_ENV, "RAIYA_STATE_DIR"):
            os.environ.pop(k, None)
        config._reset()
        self.addCleanup(config._reset)

    def write_cfg(self, text):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(text)
        config._reset()


class TestStateDirPriority(_CfgCase):
    def test_table(self):
        file_dir = os.path.join(self.root, "from_file")
        env_dir = os.path.join(self.root, "from_env")
        arg_dir = os.path.join(self.root, "from_arg")
        self.assertEqual(resolve_state_dir(), DEFAULT_STATE_DIR)  # ファイル無し (not_found) -> 既定
        self.write_cfg("state_dir: {}\n".format(file_dir))
        self.assertEqual(resolve_state_dir(), file_dir)
        os.environ["RAIYA_STATE_DIR"] = env_dir
        self.assertEqual(resolve_state_dir(), env_dir)
        self.assertEqual(resolve_state_dir(arg_dir), arg_dir)

    def test_empty_env_falls_to_file(self):
        self.write_cfg("state_dir: /from/file\n")
        os.environ["RAIYA_STATE_DIR"] = ""
        self.assertEqual(resolve_state_dir(), "/from/file")

    def test_engine_uses_file_state_dir(self):
        file_dir = os.path.join(self.root, "from_file")
        self.write_cfg("state_dir: {}\n".format(file_dir))
        self.assertEqual(MemoryEngine(new_life=False).state_dir, file_dir)


class TestInjectDispositionsPriority(_CfgCase):
    def test_table(self):
        from raiya.engine import _inject_dispositions
        self.assertFalse(_inject_dispositions())
        self.write_cfg("inject_dispositions: true\n")
        self.assertTrue(_inject_dispositions())
        os.environ[INJECT_DISPOSITIONS_ENV] = "0"  # "0" はファイルの true を打ち消す
        self.assertFalse(_inject_dispositions())
        os.environ[INJECT_DISPOSITIONS_ENV] = "1"
        self.write_cfg("inject_dispositions: false\n")
        self.assertTrue(_inject_dispositions())
        os.environ[INJECT_DISPOSITIONS_ENV] = ""  # 空はファイルへ落ちる
        self.write_cfg("inject_dispositions: true\n")
        self.assertTrue(_inject_dispositions())

    def test_bad_key_is_dropped_not_fatal(self):
        from raiya.engine import _inject_dispositions
        self.write_cfg("inject_dispositions: maybe\n")
        self.assertFalse(_inject_dispositions())


class TestFileDispositionsRecallExplainAndReadOnly(_CfgCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.root, "state")
        seeds = [_seed(1, ["猫", "犬"], kind="性向"), _seed(2, ["猫"], kind="所在")]
        _write_state(self.state, seeds)
        self.write_cfg("inject_dispositions: true\n")

    def test_recall_equals_explain_with_file_setting(self):
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        explained = eng.explain_recall("猫と犬", project=P)
        recalled = eng.recall("猫と犬", project=P)
        self.assertIn(1, [s["id"] for s in recalled["seeds"]])  # 性向が注入された (ファイルが効いている)
        self.assertEqual([s["id"] for s in explained["seeds"]], [s["id"] for s in recalled["seeds"]])
        self.assertEqual(explained["narrative"], recalled["narrative"])
        # ファイルを false にすると両方から性向が消える
        self.write_cfg("inject_dispositions: false\n")
        explained = eng.explain_recall("猫と犬", project=P)
        recalled = eng.recall("猫と犬", project=P)
        self.assertNotIn(1, [s["id"] for s in recalled["seeds"]])
        self.assertEqual([s["id"] for s in explained["seeds"]], [s["id"] for s in recalled["seeds"]])

    def test_inspect_verbs_leave_state_dir_bytes_and_config_untouched(self):
        MemoryEngine(state_dir=self.state, new_life=False).recall("猫", project=P)

        def digest():
            out = {}
            for root, _, files in os.walk(self.root):
                for name in files:
                    full = os.path.join(root, name)
                    with open(full, "rb") as f:
                        out[os.path.relpath(full, self.root)] = hashlib.sha256(f.read()).hexdigest()
            return out

        before = digest()
        for argv in (["show"], ["injected"], ["why", "1"], ["explain", "猫と犬", "--project", P]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(inspect_cli.main(["--state-dir", self.state] + argv), 0, argv)
        self.assertEqual(digest(), before)


if __name__ == "__main__":
    unittest.main()
