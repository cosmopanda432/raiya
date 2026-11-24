"""設定ファイル (config.yaml) と hooks の配線 pin (別プロセス E2E)。
spec: docs/superpowers/specs/2026-10-23-config-file-design.md §5.2 / §5.3 / §6。LLM は実際には呼ばない。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests._state_fixture import ledger_rows, meta_value  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

BROKEN_LLM_KEY = "llm:\n  SECRET_TYPO_KEY: command\n"


class _HookCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = os.path.join(self.root, "state")
        self.cwd = os.path.join(self.root, "proj")
        os.makedirs(self.cwd)
        self.cfg = os.path.join(self.root, "config.yaml")
        self.marker = os.path.join(self.root, "llm_was_called")

    def write_cfg(self, text):
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(text)

    def run_hook(self, sub, payload, env_extra=None, state_in_env=True, drop=()):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAIYA_")}
        env["RAIYA_CONFIG"] = self.cfg
        if state_in_env:
            env["RAIYA_STATE_DIR"] = self.state
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.update(env_extra or {})
        for k in drop:
            env.pop(k, None)
        result = subprocess.run(
            [sys.executable, "-m", "raiya.hooks.claude_code", sub],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, env=env, cwd=self.cwd, timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result

    def turn(self, n=0, env_extra=None, **kw):
        sid = "s1"
        self.run_hook("recall", {"session_id": sid, "prompt": "猫が好きです {}".format(n), "cwd": self.cwd},
                      env_extra, **kw)
        return self.run_hook("stop", {"session_id": sid, "last_assistant_message": "了解です {}".format(n),
                                      "cwd": self.cwd}, env_extra, **kw)

    def hooks_log(self):
        path = os.path.join(self.state, "hooks.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def ledger_rows(self):
        return ledger_rows(self.state)

    def llm_script(self):
        path = os.path.join(self.root, "llm.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ncat > /dev/null\ntouch '{}'\necho '[]'\n".format(self.marker))
        os.chmod(path, 0o755)
        return path


class TestGuardDoesNotReadConfig(_HookCase):
    def test_guard_with_broken_config_writes_nothing(self):
        self.write_cfg("{ not yaml: [")
        for guard in ({"RAIYA_HOOK_ACTIVE": "1"}, {"RAIYA_HOOKS": "0"}):
            for sub in ("recall", "stop", "session_end"):
                with self.subTest(guard=guard, sub=sub):
                    r = self.run_hook(sub, {"session_id": "s1", "prompt": "猫が好き", "cwd": self.cwd}, guard)
                    self.assertEqual((r.stdout, r.stderr), ("", ""))
                    self.assertFalse(os.path.exists(self.state))  # state dir も hooks.log も作られない

    def test_guard_with_state_dir_only_in_file_uses_default_not_file(self):
        # ファイルだけで state_dir を決めていても guard は読まない (cwd/state にも file の state にも何も作らない)
        file_state = os.path.join(self.root, "file_state")
        self.write_cfg("state_dir: {}\n".format(file_state))
        os.makedirs(os.path.join(file_state))
        with open(os.path.join(file_state, "debug"), "w") as f:
            f.write("1")
        self.run_hook("recall", {"prompt": "猫が好き"}, {"RAIYA_HOOK_ACTIVE": "1"}, state_in_env=False)
        self.assertEqual(os.listdir(file_state), ["debug"])  # guard イベントは file の debug.log に出ない


class TestBrokenConfigKeepsDeterministicPath(_HookCase):
    def test_recall_and_stop_continue_and_log_summary_only(self):
        self.write_cfg(BROKEN_LLM_KEY)
        self.turn(0)
        self.assertEqual(len(self.ledger_rows()), 1)
        log = self.hooks_log()
        config_lines = [x for x in log.splitlines() if "config 不正" in x]
        self.assertEqual(len(config_lines), 2)  # recall と stop で 1 hook につき 1 行
        self.assertIn("category=unknown_key key=llm", config_lines[0])
        self.assertIn("有効なキーと env で続行", config_lines[0])
        for secret in ("SECRET_TYPO_KEY", self.cfg, "command"):
            self.assertNotIn(secret, log)

    def test_good_config_logs_no_config_line(self):
        self.write_cfg("inject_dispositions: false\n")
        self.turn(0)
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertNotIn("config", self.hooks_log())

    def test_other_valid_keys_survive_a_bad_key(self):
        file_state = os.path.join(self.root, "file_state")
        self.write_cfg("state_dir: {}\nllm:\n  model: no\n".format(file_state))
        self.turn(0, state_in_env=False)
        self.assertEqual(len(ledger_rows(file_state)), 1)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "state")))


class TestHostileConfigHook(_HookCase):
    def test_hook_with_overflowing_timeout_still_records(self):
        self.write_cfg("state_dir: {}\nllm:\n  timeout_sec: {}\n".format(self.state, "9" * 400))
        self.turn(0, state_in_env=False)
        self.assertEqual(len(self.ledger_rows()), 1)
        lines = [x for x in self.hooks_log().splitlines() if "config 不正" in x]
        self.assertEqual(len(lines), 2)  # recall と stop
        self.assertIn("config 不正 (category=bad_value key=llm.timeout_sec)", lines[0])


class TestConsolidateRefused(_HookCase):
    def test_issue_blocks_consolidation_and_window_stays(self):
        self.write_cfg(BROKEN_LLM_KEY)
        env = {"RAIYA_BACKEND": "command", "RAIYA_LLM_CMD": self.llm_script()}
        for n in range(5):  # CONSOLIDATE_EVERY
            self.turn(n, env)
        self.assertFalse(os.path.exists(self.marker))  # env が揃っていても LLM は呼ばれない
        self.assertEqual(meta_value(self.state, "ledger.consolidated_upto"), 0)
        self.assertEqual(len(self.ledger_rows()), 5)
        log = self.hooks_log()
        self.assertIn("consolidate失敗: BackendError: 設定ファイルが不正です (category=unknown_key key=llm)", log)
        self.assertNotIn("SECRET_TYPO_KEY", log)

    def test_good_config_consolidates(self):
        self.write_cfg("inject_dispositions: false\n")
        env = {"RAIYA_BACKEND": "command", "RAIYA_LLM_CMD": self.llm_script()}
        for n in range(5):
            self.turn(n, env)
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(meta_value(self.state, "ledger.consolidated_upto"), 5)


class TestStateDirLost(_HookCase):
    def test_broken_file_without_env_state_dir_stops_silently(self):
        for text in ("{ not yaml: [", "state_dir: relative/path\n", "- list\n"):
            with self.subTest(text=text):
                self.write_cfg(text)
                for sub in ("recall", "stop", "session_end"):
                    r = self.run_hook(sub, {"session_id": "s1", "prompt": "猫が好き", "cwd": self.cwd},
                                      state_in_env=False)
                    self.assertEqual(r.stdout, "")
                    self.assertEqual(len(r.stderr.splitlines()), 1)
                    self.assertIn("state_dir", r.stderr)
                    self.assertNotIn(self.cfg, r.stderr)
                self.assertEqual(os.listdir(self.cwd), [])  # ./state へ落ちていない

    def test_env_state_dir_rescues(self):
        self.write_cfg("- list\n")
        self.turn(0)  # RAIYA_STATE_DIR が env にある
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertIn("config 不正", self.hooks_log())

    def test_other_bad_key_does_not_lose_state_dir(self):
        self.write_cfg("llm:\n  timeout_sec: 0\n")
        self.turn(0, state_in_env=False, env_extra={"RAIYA_STATE_DIR": self.state})
        self.assertEqual(len(self.ledger_rows()), 1)


class TestHookEndConfigKeys(_HookCase):
    def test_hook_end_carries_config_src_and_issue_categories(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "debug"), "w") as f:
            f.write("1")
        self.write_cfg(BROKEN_LLM_KEY)
        self.run_hook("recall", {"session_id": "s1", "prompt": "猫が好きです", "cwd": self.cwd})
        with open(os.path.join(self.state, "debug.log"), encoding="utf-8") as f:
            events = [json.loads(x) for x in f if x.strip()]
        end = [e for e in events if e["event"] == "hook_end"][-1]
        self.assertEqual(end["config_src"], "env")
        self.assertEqual(end["config_issues"], ["unknown_key"])
        self.assertNotIn("SECRET_TYPO_KEY", json.dumps(events))

    def test_disabled_and_none(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "debug"), "w") as f:
            f.write("1")
        home = os.path.join(self.root, "home")
        os.makedirs(home)
        for extra, drop, expect in (({"RAIYA_CONFIG": ""}, (), "disabled"),
                                    ({"HOME": home}, ("RAIYA_CONFIG", "XDG_CONFIG_HOME"), "none")):
            with self.subTest(expect=expect):
                env_extra = dict(extra)
                if "HOME" in env_extra:
                    env_extra["XDG_CONFIG_HOME"] = ""
                self.run_hook("recall", {"session_id": "s1", "prompt": "猫が好きです", "cwd": self.cwd},
                              env_extra, drop=("RAIYA_CONFIG",) if expect == "none" else ())
                with open(os.path.join(self.state, "debug.log"), encoding="utf-8") as f:
                    events = [json.loads(x) for x in f if x.strip()]
                end = [e for e in events if e["event"] == "hook_end"][-1]
                self.assertEqual((end["config_src"], end["config_issues"]), (expect, []))


if __name__ == "__main__":
    unittest.main()
