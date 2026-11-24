"""hooks の注入経路の end-to-end テスト。

Claude Code が実際にやる形 (`python -m raiya.hooks.claude_code <sub>` を別プロセスで起動し、
stdin へ JSON を渡す) で recall / stop / session_end を回す。`test_hooks_claude_code.py` は
`cmd_recall` を in-process で呼ぶので、ここは「プロセス境界を越えて」確かめたいことだけを持つ:
stdout が JSON 1 行であること、stderr が空であること、state_dir の実ファイルに残るもの。

実 LLM・実ネットワークは使わない。子プロセスの env は RAIYA_* を全部剥がしたうえで
RAIYA_BACKEND=command に固定する: recall だけの試験は exit 1 する LLM を指す。うっかり LLM が
呼ばれても hook は fail-soft で exit 0 のままなので、落ちるのは hooks.log を検分する試験
(「失敗」の有無を見るもの) だけ。統合まで走らせる試験は固定 JSON を返す sh を指す。
payload 雛形は tests/fixtures/hook_payloads/ (手書き・実採取ではない)。
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "hook_payloads")

from raiya import manas, sansho  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from tests._state_fixture import ledger_rows  # noqa: E402
from raiya.hooks import claude_code as hooks  # noqa: E402


def _load_fixture(name, **overrides):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        payload = json.load(f)
    payload.pop("_note", None)
    payload.update(overrides)
    return payload


class _E2ETestCase(unittest.TestCase):
    def setUp(self):
        # realpath: resolve_project は realpath を返すので、期待値と文字列で揃える。
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.proj = self._make_git_project("proj")
        self.proj_cwd = os.path.join(self.proj, "src")  # git の最上位まで辿れることも見る
        os.makedirs(self.proj_cwd)
        self.fail_llm = self._write_llm("fail_llm.sh", "#!/bin/sh\ncat > /dev/null\nexit 1\n")

    # --- 道具 ---
    def _make_git_project(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.join(path, ".git"))
        return path

    def _write_llm(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def _fixed_llm(self, proposals):
        body = "#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{}\nEOF\n".format(json.dumps(proposals, ensure_ascii=False))
        return self._write_llm("fixed_llm.sh", body)

    def _env(self, llm=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAIYA_")}
        env["RAIYA_CONFIG"] = ""
        env["RAIYA_STATE_DIR"] = self.state
        env["RAIYA_BACKEND"] = "command"
        env["RAIYA_LLM_CMD"] = llm or self.fail_llm
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def _run(self, sub, payload, llm=None):
        result = subprocess.run(
            [sys.executable, "-m", "raiya.hooks.claude_code", sub],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, env=self._env(llm), cwd=self.root, timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        return result

    def _recall(self, session_id, prompt, cwd="default", **extra):
        payload = _load_fixture("userpromptsubmit.json", session_id=session_id, prompt=prompt,
                                cwd=self.proj_cwd if cwd == "default" else cwd, **extra)
        if payload["cwd"] is None:
            del payload["cwd"]
        return self._run("recall", payload)

    def _stop(self, session_id, assistant, llm=None, **extra):
        return self._run("stop", _load_fixture("stop.json", session_id=session_id, cwd=self.proj_cwd,
                                               last_assistant_message=assistant, **extra), llm=llm)

    def _context(self, result):
        """stdout (JSON 1 行) の additionalContext。出力が無ければ None。"""
        if not result.stdout:
            return None
        self.assertEqual(len(result.stdout.splitlines()), 1)
        out = json.loads(result.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return out["hookSpecificOutput"]["additionalContext"]

    def _plant(self, content, cond, kind="所在", project="__proj__"):
        project = self.proj if project == "__proj__" else project
        MemoryEngine(state_dir=self.state).imprint(
            sansho.Turn(user_text="{} {}".format(content, cond), assistant_text=""),
            [{"content": content, "conditions": [cond], "polarity": "無記", "kind": kind,
              "evidence": {"source": "user", "span": "{} {}".format(content, cond)}}],
            project=project,
        )

    def _hooks_log(self):
        path = os.path.join(self.state, "hooks.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _ledger_rows(self):
        return ledger_rows(self.state)


class TestRecallInjection(_E2ETestCase):
    def test_recall_emits_json_line_and_injection_log_row(self):
        self._plant("設定は config/app.toml にある", "設定")
        result = self._recall("sess-A", "設定ファイルの場所を知りたい")
        ctx = self._context(result)
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.startswith(manas.NARRATIVE_HEADER))
        self.assertIn("設定は config/app.toml にある", ctx)
        # 別プロセスの観測 CLI で、注入ログに同じ本文が session_id / project 付きで残っている。
        inspect = subprocess.run(
            [sys.executable, "-m", "raiya.inspect_cli", "--state-dir", self.state, "--json", "injected"],
            capture_output=True, text=True, env=self._env(), cwd=self.root, timeout=60,
        )
        self.assertEqual(inspect.returncode, 0, msg=inspect.stderr)
        rows = json.loads(inspect.stdout)
        self.assertEqual(len(rows), 1)
        newest = rows[0]
        self.assertEqual(newest["session_id"], "sess-A")
        self.assertEqual(newest["project"], self.proj)
        self.assertTrue(newest["ts"])
        self.assertEqual(newest["injected_text"], ctx)

    def test_resume_point_is_emitted_once_per_session(self):
        # 内容が PREFERENCE_SUFFIXES (したい 等) で終わると 性向 に読み替えられるので避ける。
        self._plant("認証の移行を途中で止めた。次はテストを足す", "認証", kind="再開点")
        first = self._context(self._recall("sess-A", "続きをやろう"))
        self.assertIn("- 再開点: 認証の移行を途中で止めた", first)
        self.assertIsNone(self._context(self._recall("sess-A", "もう少し続けよう")))
        again = self._context(self._recall("sess-B", "別の日の続きをやろう"))
        self.assertIn("- 再開点:", again)

    def test_project_scoping_by_cwd(self):
        self._plant("設定は config/app.toml にある", "設定")
        other = self._make_git_project("other")
        # 別 git の cwd -> project 違いで注入なし
        self.assertIsNone(self._context(self._recall("s1", "設定ファイルの場所を知りたい", cwd=other)))
        # 同じ git の下 -> 注入あり
        self.assertIn("config/app.toml", self._context(self._recall("s2", "設定ファイルの場所を知りたい")))

    def test_payload_without_cwd_sees_only_global_seeds(self):
        self._plant("設定は config/app.toml にある", "設定")
        self._plant("ログは var/log に出る", "設定", project=None)
        ctx = self._context(self._recall("s1", "設定の話をしよう", cwd=None))
        self.assertIn("var/log", ctx)
        self.assertNotIn("config/app.toml", ctx)


class TestFullChain(_E2ETestCase):
    PROMPT = "認証はJWTではなくセッションCookieを採用する方針です"
    PROPOSAL = {"content": "認証はJWTではなくセッションCookieを採用", "conditions": ["認証"], "polarity": "無記",
                "kind": "決定", "evidence": {"source": "user", "span": "認証はJWTではなくセッションCookieを採用する方針"}}

    def _turns(self, n, session_id="sess-1", llm=None):
        for _ in range(n):
            self._recall(session_id, self.PROMPT)
            self._stop(session_id, "了解しました", llm=llm)

    def _assert_seed_recalled_in_new_session(self):
        ctx = self._context(self._recall("sess-fresh", "認証について教えて"))
        self.assertIsNotNone(ctx)
        self.assertIn(self.PROPOSAL["content"], ctx)

    def test_stop_x5_consolidates_then_fresh_session_recalls_seed(self):
        llm = self._fixed_llm([self.PROPOSAL])
        self._turns(hooks.CONSOLIDATE_EVERY - 1, llm=llm)
        self.assertNotIn("consolidate window", self._hooks_log())
        self._recall("sess-1", self.PROMPT)
        self._stop("sess-1", "了解しました", llm=llm)
        log = self._hooks_log()
        self.assertIn("consolidate window=5 sessions=1 proposals=1", log)
        self.assertIn("圓成實=1", log)
        self._assert_seed_recalled_in_new_session()

    def test_session_end_consolidates_remaining_turns(self):
        llm = self._fixed_llm([self.PROPOSAL])
        self._turns(2, llm=llm)
        self.assertNotIn("consolidate window", self._hooks_log())
        self._run("session_end", _load_fixture("session_end.json", session_id="sess-1", cwd=self.proj_cwd), llm=llm)
        self.assertIn("consolidate window=2 sessions=1 proposals=1", self._hooks_log())
        self._assert_seed_recalled_in_new_session()


class TestSubagentAndPrivate(_E2ETestCase):
    def test_subagent_payloads_leave_no_trace(self):
        # 親の recall でキャッシュを作っておく (サブエージェントの stop がこれを台帳へ書かないこと)
        self._recall("fixture-session", "親セッションの普通の発言です")
        cache = os.path.join(self.state, "sessions", "fixture-session.json")
        with open(cache, "rb") as f:
            before = f.read()
        recall = self._run("recall", _load_fixture("userpromptsubmit_subagent.json", cwd=self.proj_cwd))
        self.assertEqual(recall.stdout, "")
        with open(cache, "rb") as f:
            self.assertEqual(f.read(), before)  # 委任文でキャッシュが上書きされない
        self._stop("fixture-session", "委任された作業を終えました", agent_id="agent-fixture")
        self.assertEqual(self._ledger_rows(), [])
        self.assertIn("サブエージェント", self._hooks_log())

    def test_private_text_never_reaches_any_state_file(self):
        secret = "秘密"
        self._recall("sess-P", "<private>{}</private>認証の方針を相談したい".format(secret))
        self._stop("sess-P", "了解しました、方針を整理します")
        self.assertEqual(len(self._ledger_rows()), 1)  # 往復自体は記録される (private 以外)
        needle = secret.encode("utf-8")
        found = []
        for dirpath, _dirs, files in os.walk(self.state):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as f:
                    if needle in f.read():
                        found.append(os.path.join(dirpath, name))
        self.assertEqual(found, [])


class TestFailSoft(_E2ETestCase):
    """hook は壊れた入力でも exit 0 で黙る。例外は hooks.log の「失敗」行に落ちるだけで
    exit code も stderr も変わらないので、ここでは hooks.log に「失敗」が無いことまで見る。"""

    def _assert_no_failure_logged(self):
        self.assertNotIn("失敗", self._hooks_log())

    def test_recall_without_cwd(self):
        payload = _load_fixture("userpromptsubmit.json")
        del payload["cwd"]
        self._run("recall", payload)
        self._assert_no_failure_logged()
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "fixture-session.json")))

    def test_recall_without_session_id(self):
        payload = _load_fixture("userpromptsubmit.json")
        del payload["session_id"]
        self._run("recall", payload)
        self._assert_no_failure_logged()
        sdir = os.path.join(self.state, "sessions")
        self.assertEqual(os.listdir(sdir) if os.path.isdir(sdir) else [], [])

    def test_recall_with_non_str_cwd(self):
        self._plant("設定は config/app.toml にある", "設定", project=None)
        result = self._run("recall", _load_fixture("userpromptsubmit.json", cwd=123, prompt="設定の話をしよう"))
        self._assert_no_failure_logged()
        # cwd が壊れていても project=None (全域) として全域の種子は注入される。
        self.assertIn("config/app.toml", self._context(result))

    def test_stop_without_cwd_records_global_row(self):
        recall = _load_fixture("userpromptsubmit.json", prompt="認証の方針を相談したい")
        del recall["cwd"]
        self._run("recall", recall)
        stop = _load_fixture("stop.json")
        del stop["cwd"]
        self._run("stop", stop)
        self._assert_no_failure_logged()
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].get("project"))
        self.assertEqual(rows[0]["session_id"], "fixture-session")

    def test_stop_without_session_id_skips_and_records_nothing(self):
        stop = _load_fixture("stop.json")
        del stop["session_id"]
        self._run("stop", stop)
        self._assert_no_failure_logged()
        self.assertIn("skip", self._hooks_log())
        self.assertEqual(self._ledger_rows(), [])

    def test_stop_with_non_str_cwd(self):
        self._recall("sess-C", "認証の方針を相談したい")
        self._run("stop", _load_fixture("stop.json", session_id="sess-C", cwd=123))
        self._assert_no_failure_logged()
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].get("project"))

    def test_session_end_with_empty_state(self):
        self._run("session_end", _load_fixture("session_end.json"))
        log = self._hooks_log()
        self.assertNotIn("consolidate", log)
        self.assertNotIn("失敗", log)


class TestDebugLogE2E(_E2ETestCase):
    """`<state_dir>/debug` フラグで debug.log を出す (設計書 2026-10-23-debug-log-design.md)。"""

    PROMPT = "認証はJWTではなくセッションCookieを採用する方針です"
    PROPOSAL = {"content": "認証はJWTではなくセッションCookieを採用", "conditions": ["認証"], "polarity": "無記",
                "kind": "決定", "memo": "SENTINEL_LLM_OUT",
                "evidence": {"source": "user", "span": "認証はJWTではなくセッションCookieを採用する方針"}}

    def _flag(self, content=""):
        with open(os.path.join(self.state, "debug"), "w", encoding="utf-8") as f:
            f.write(content)

    def _debug_path(self):
        return os.path.join(self.state, "debug.log")

    def _events(self, event=None):
        from raiya import debuglog
        events = debuglog.read_events(self.state)
        return [e for e in events if event is None or e["event"] == event]

    def _debug_text(self):
        path = self._debug_path()
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _run_env(self, sub, payload, extra_env, llm=None):
        env = self._env(llm)
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "raiya.hooks.claude_code", sub],
            input=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, env=env, cwd=self.root, timeout=60,
        )

    def _state_text_outside_debug_log(self):
        blobs = []
        for dirpath, _dirs, files in os.walk(self.state):
            for name in files:
                if name.startswith("debug.log"):
                    continue
                with open(os.path.join(dirpath, name), "rb") as f:
                    blobs.append((os.path.join(dirpath, name), f.read().decode("utf-8", "replace")))
        return blobs

    def _chain(self, llm, secret=None):
        prompt = self.PROMPT if secret is None else "<private>{}</private>{}".format(secret, self.PROMPT)
        for i in range(hooks.CONSOLIDATE_EVERY):
            self._recall("sess-D", prompt)
            self._stop("sess-D", "了解しました SENTINEL_ASST_TEXT", llm=llm)

    # --- フラグ無し ---
    def test_no_flag_writes_no_debug_log_on_any_path(self):
        self._chain(self._fixed_llm([self.PROPOSAL]))
        self._run("session_end", _load_fixture("session_end.json", session_id="sess-D", cwd=self.proj_cwd))
        self._run("recall", _load_fixture("userpromptsubmit_subagent.json", cwd=self.proj_cwd))
        self.assertFalse(os.path.exists(self._debug_path()))
        self.assertFalse(os.path.exists(self._debug_path() + ".1"))

    def test_no_flag_guard_creates_no_file(self):
        empty = os.path.join(self.root, "never")
        for extra in ({"RAIYA_HOOKS": "0"}, {"RAIYA_HOOK_ACTIVE": "1"}):
            result = self._run_env("recall", {"prompt": "猫が好き"}, dict(extra, RAIYA_STATE_DIR=empty))
            self.assertEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(empty))
        self.assertEqual(os.listdir(self.state), [])

    def test_directory_named_debug_is_off(self):
        os.makedirs(os.path.join(self.state, "debug"))
        self._recall("sess-X", "普通のプロンプトです")
        self.assertFalse(os.path.exists(self._debug_path()))

    # --- レベル 1 ---
    def test_level1_events_present_and_no_text_anywhere(self):
        self._flag("")
        self._chain(self._fixed_llm([self.PROPOSAL]))
        text = self._debug_text()
        for sentinel in ("SENTINEL_ASST_TEXT", "SENTINEL_LLM_OUT", self.PROMPT, "認証"):
            self.assertNotIn(sentinel, text)
        events = self._events()
        names = {e["event"] for e in events}
        self.assertEqual(names, {"recall", "stop", "consolidate", "hook_end"})
        self.assertTrue(all(e["level"] == 1 for e in events))
        consolidate = self._events("consolidate")[0]
        self.assertEqual(consolidate["backend"], "command")
        self.assertEqual(consolidate["window_n"], 5)
        self.assertEqual(consolidate["counts"]["圓成實"], 1)
        self.assertGreater(consolidate["prompt_len"], 0)
        self.assertNotIn("prompt", consolidate)
        stop = self._events("stop")[-1]
        self.assertTrue(stop["consolidate_triggered"])
        self.assertEqual(stop["user_src"], "cache")
        self.assertEqual(stop["asst_src"], "stdin")
        self.assertEqual(stop["truncated"], {"user": False, "grounding": False, "assistant": False})
        self.assertNotIn("user_text", stop)
        for line in self._debug_text().splitlines():
            self.assertLessEqual(len(line.encode("utf-8")) + 1, 4096)
        # hooks.log の書式は変わらない
        self.assertIn("consolidate window=5 sessions=1 proposals=1", self._hooks_log())

    def test_level1_guard_event_and_hook_end(self):
        self._flag("1")
        result = self._run_env("recall", {"prompt": "猫が好き"}, {"RAIYA_HOOKS": "0"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        guard = self._events("guard")
        self.assertEqual(len(guard), 1)
        self.assertEqual((guard[0]["reason"], guard[0]["sub"], guard[0]["argv0"]), ("hooks_off", "recall", "recall"))
        ends = self._events("hook_end")
        self.assertEqual([e["outcome"] for e in ends], ["skip:guard"])
        self._run_env("stop", {}, {"RAIYA_HOOK_ACTIVE": "1"})
        self.assertEqual([e["reason"] for e in self._events("guard")], ["hooks_off", "hook_active"])

    def test_unknown_subcommand_writes_nothing(self):
        self._flag("1")
        self._run_env("bogus", {}, {})
        self.assertEqual(self._events(), [])

    def test_consolidate_failure_l1_has_summary_not_message(self):
        self._flag("1")
        llm = self._write_llm("noisy_fail.sh", "#!/bin/sh\ncat > /dev/null\necho SENTINEL_STDERR_BODY >&2\nexit 1\n")
        self._chain(llm)
        text = self._debug_text()
        self.assertNotIn("SENTINEL_STDERR_BODY", text)
        failed = self._events("consolidate")[0]
        self.assertEqual(failed["exc"], "BackendError")
        self.assertIn("detail_len=", failed["exc_summary"])
        self.assertNotIn("error", failed)
        self.assertEqual([e["outcome"] for e in self._events("hook_end")][-1], "ok")  # 失敗は consolidate イベントに出る

    # --- レベル 2 ---
    def test_level2_has_text_and_private_only_in_raw_fields(self):
        self._flag("2\n")
        secret = "SECRET_PRIVATE_BODY"
        self._chain(self._fixed_llm([self.PROPOSAL]), secret=secret)
        text = self._debug_text()
        for sentinel in ("SENTINEL_ASST_TEXT", "SENTINEL_LLM_OUT", self.PROMPT):
            self.assertIn(sentinel, text)
        carriers = set()
        for e in self._events():
            for key, value in e.items():
                if secret in json.dumps(value, ensure_ascii=False):
                    carriers.add((e["event"], key))
        self.assertEqual(carriers, {("recall", "prompt_raw")})
        # debug.log 以外の state ファイルには 0 件 (台帳・cache・種子・注入ログ・hooks.log)
        for path, blob in self._state_text_outside_debug_log():
            self.assertNotIn(secret, blob, msg=path)
        consolidate = self._events("consolidate")[0]
        self.assertIn("SENTINEL_LLM_OUT", consolidate["raw"])
        self.assertIn(self.PROMPT, consolidate["prompt"])
        self.assertEqual(len(consolidate["decisions"]), 1)

    def test_level2_assistant_private_only_in_assistant_raw(self):
        self._flag("2")
        secret = "SECRET_ASST_PRIVATE"
        self._recall("sess-A", self.PROMPT)
        self._stop("sess-A", "答えます <private>{}</private> 以上".format(secret))
        stop = self._events("stop")[0]
        self.assertIn(secret, stop["assistant_text_raw"])
        self.assertNotIn(secret, stop["assistant_text"])
        self.assertNotIn(secret, json.dumps(self._ledger_rows(), ensure_ascii=False))
        for path, blob in self._state_text_outside_debug_log():
            self.assertNotIn(secret, blob, msg=path)

    def test_level2_transcript_path_user_raw_carries_private(self):
        self._flag("2")
        secret = "SECRET_TRANSCRIPT_USER"
        transcript = os.path.join(self.root, "t.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "<private>{}</private>{}".format(secret, self.PROMPT)}}) + "\n")
        self._stop("sess-T", "応答です", transcript_path=transcript)
        stop = self._events("stop")[0]
        self.assertEqual(stop["user_src"], "transcript")
        self.assertTrue(stop["transcript_read"])
        self.assertIn(secret, stop["user_text_raw"])
        self.assertNotIn(secret, stop["user_text"])
        for path, blob in self._state_text_outside_debug_log():
            self.assertNotIn(secret, blob, msg=path)

    def test_level2_consolidate_failure_error_has_detail(self):
        self._flag("2")
        llm = self._write_llm("noisy_fail.sh", "#!/bin/sh\ncat > /dev/null\necho SENTINEL_STDERR_BODY >&2\nexit 1\n")
        self._chain(llm)
        failed = self._events("consolidate")[0]
        self.assertIn("SENTINEL_STDERR_BODY", failed["error"])
        self.assertNotIn("SENTINEL_STDERR_BODY", self._hooks_log())

    def test_level2_recall_records_duration(self):
        self._flag("2")
        self._plant("設定は config/app.toml にある", "設定")
        self._recall("sess-R", "設定ファイルの場所を知りたい")
        recall = self._events("recall")[0]
        for key in ("dur_ms", "engine_ms", "recall_ms"):
            self.assertIsInstance(recall[key], int)
        self.assertTrue(recall["emitted"])
        self.assertTrue(recall["session_start"])
        self.assertEqual(recall["project"], self.proj)
        self.assertEqual(len(recall["seed_ids"]) + len(recall["index_ids"]) > 0, True)
        self.assertIn("config/app.toml", recall["narrative"])

    def test_level2_long_body_is_elided_with_len(self):
        self._flag("2")
        big = "あ" * 100000
        self._recall("sess-B", big)
        recall = self._events("recall")[0]
        self.assertEqual(recall["prompt_raw_len"], 100000)
        self.assertLess(len(recall["prompt_raw"]), 70000)
        self.assertIn("elided", recall["prompt_raw"])

    # --- hook_end ---
    def test_exactly_one_hook_end_per_hook_including_failure(self):
        self._flag("1")
        self._recall("sess-E", self.PROMPT)
        self._stop("sess-E", "了解")
        self._run("session_end", _load_fixture("session_end.json", session_id="sess-E", cwd=self.proj_cwd))
        bad = self._run_env("recall", "{not json", {})
        self.assertEqual(bad.returncode, 0)
        ends = self._events("hook_end")
        self.assertEqual([e["sub"] for e in ends], ["recall", "stop", "session_end", "recall"])
        self.assertEqual(ends[-1]["outcome"], "fail:stdin:JSONDecodeError")
        self.assertEqual(ends[0]["outcome"], "ok")
        self.assertEqual(ends[0]["payload_keys"], sorted(ends[0]["payload_keys"]))
        self.assertNotIn("stdin_raw", ends[-1])

    def test_level2_failed_stdin_is_kept_raw(self):
        self._flag("2")
        self._run_env("stop", "{SENTINEL_BAD_STDIN", {})
        end = self._events("hook_end")[0]
        self.assertEqual(end["stdin_raw"], "{SENTINEL_BAD_STDIN")
        self.assertIn("error", end)

    def test_skip_outcomes_are_coded(self):
        self._flag("1")
        self._run("recall", _load_fixture("userpromptsubmit_subagent.json", cwd=self.proj_cwd))
        self._recall("sess-S", "/slash command")
        self._stop("sess-S", "応答", stop_hook_active=True)
        self._stop("sess-S", "応答")  # cache は skippable な prompt
        self.assertEqual(
            [e["outcome"] for e in self._events("hook_end")],
            ["skip:subagent", "skip:skippable_prompt", "skip:stop_hook_active", "skip:skippable_user"],
        )

    def test_session_end_empty_window_event_has_no_backend(self):
        self._flag("1")
        self._run("session_end", _load_fixture("session_end.json"))
        c = self._events("consolidate")[0]
        self.assertEqual(c["skipped"], "empty")
        self.assertNotIn("backend", c)
        self.assertNotIn("model", c)
        self.assertEqual(c["window_n"], 0)

    def test_log_file_mode_is_0600(self):
        self._flag("1")
        self._recall("sess-M", self.PROMPT)
        self.assertEqual(stat.S_IMODE(os.stat(self._debug_path()).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
