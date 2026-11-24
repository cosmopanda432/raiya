"""`raiya-hook` 契約 v1 のテスト (Issue #62・pi 拡張設計書 §2)。

`tests/fixtures/raiya_hook_contract_v1/` の `<verb>__<case>.stdin.json` / `.expect.json` の対を、
一時 state_dir で `python -m raiya.hooks.raiya_hook <verb>` を別プロセスで起動して照合する。
stdin 5 件 (recall__normal / recall__unknown_field / recall__version2 / record__demoted / record__normal) は
raiya-pi 側のコピーと**バイト一致**でなければならない (拡張が文字列で比べる)。変えない。

expect = {exit, outcome, writes, stdout_keys, env?, prelude?}。`writes` は raiya.db を sqlite3 で直接
数えた差分: ledger = ledger 行数が増えた / life = meta `alaya.life` が増えた / manas = manas_log 行数が増えた /
sessions = `sessions/<session_id>.json` が新しくできた。観測した集合が expect と**一致**することを見る
(何も書かない case の「書かない」も確かめる)。exit 2 は `raiya.db` を作らず、hooks.log は state dir が既にあるときだけ 1 行 (無ければ state dir も作らない)。
実 LLM・実ネットワークは使わない (RAIYA_LLM_CMD は exit 1 / 固定 JSON を返す sh)。
"""
import ast
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "raiya_hook_contract_v1")
HOOK_PATH = os.path.join(REPO_ROOT, "raiya", "hooks", "raiya_hook.py")


def _cases():
    names = sorted(f[: -len(".stdin.json")] for f in os.listdir(FIXTURE_DIR) if f.endswith(".stdin.json"))
    return names


def _read_bytes(name, kind):
    with open(os.path.join(FIXTURE_DIR, "{}.{}.json".format(name, kind)), "rb") as f:
        return f.read()


class _HookTestCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.fail_llm = self._write_llm("fail_llm.sh", "#!/bin/sh\ncat > /dev/null\nexit 1\n")

    def _write_llm(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def _env(self, llm=None, extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAIYA_")}
        env["RAIYA_CONFIG"] = ""
        env["RAIYA_STATE_DIR"] = self.state
        env["RAIYA_BACKEND"] = "command"
        env["RAIYA_LLM_CMD"] = llm or self.fail_llm
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.update(extra or {})
        return env

    def _run(self, verb, stdin, llm=None, env_extra=None, env=None):
        if isinstance(stdin, (dict, list)):
            stdin = json.dumps(stdin, ensure_ascii=False, separators=(",", ":"))
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8")
        return subprocess.run(
            [sys.executable, "-m", "raiya.hooks.raiya_hook", verb], input=stdin, capture_output=True,
            env=env if env is not None else self._env(llm, env_extra), cwd=self.root, timeout=60,
        )

    def _out(self, result):
        self.assertEqual(result.stderr, b"", msg=result.stderr)
        text = result.stdout.decode("utf-8")
        self.assertEqual(len(text.splitlines()), 1, msg=text)
        return json.loads(text)

    # --- 観測 (sqlite3 で raiya.db を直接読む。raiya の API を通さない) ---
    def _db_path(self):
        return os.path.join(self.state, "raiya.db")

    def _snapshot(self, session_id=None):
        snap = {"ledger": 0, "life": 0, "manas": 0, "sessions": False}
        if session_id is not None:
            snap["sessions"] = os.path.exists(os.path.join(self.state, "sessions", session_id + ".json"))
        if os.path.exists(self._db_path()):
            con = sqlite3.connect("file:{}?mode=ro".format(self._db_path()), uri=True)
            try:
                snap["ledger"] = con.execute("SELECT count(*) FROM ledger").fetchone()[0]
                snap["manas"] = con.execute("SELECT count(*) FROM manas_log").fetchone()[0]
                row = con.execute("SELECT value FROM meta WHERE key = 'alaya.life'").fetchone()
                snap["life"] = int(row[0]) if row else 0
            finally:
                con.close()
        return snap

    def _ledger_bodies(self):
        con = sqlite3.connect("file:{}?mode=ro".format(self._db_path()), uri=True)
        try:
            return [json.loads(r[0]) for r in con.execute("SELECT body FROM ledger ORDER BY seq")]
        finally:
            con.close()

    def _hooks_log_lines(self):
        path = os.path.join(self.state, "hooks.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    def _record(self, **fields):
        payload = {"contract_version": 1, "session_id": "s1", "cwd": "/work", "user_text": "hello",
                   "assistant_text": "answer"}
        payload.update(fields)
        return payload


class TestFixtureContract(_HookTestCase):
    def test_every_stdin_has_an_expect_and_vice_versa(self):
        stdins = {f[: -len(".stdin.json")] for f in os.listdir(FIXTURE_DIR) if f.endswith(".stdin.json")}
        expects = {f[: -len(".expect.json")] for f in os.listdir(FIXTURE_DIR) if f.endswith(".expect.json")}
        others = [f for f in os.listdir(FIXTURE_DIR) if not f.endswith((".stdin.json", ".expect.json"))]
        self.assertEqual(stdins, expects)
        self.assertEqual(others, [])
        self.assertGreaterEqual(len(stdins), 10)

    def test_stdin_files_are_compact_json_without_trailing_newline(self):
        for name in _cases():
            with self.subTest(name=name):
                raw = _read_bytes(name, "stdin").decode("utf-8")
                obj = json.loads(raw)
                self.assertEqual(raw, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def _run_case(self, name):
        verb = name.split("__")[0]
        expect = json.loads(_read_bytes(name, "expect"))
        for pre in expect.get("prelude", []):
            pre_result = self._run(pre.split("__")[0], _read_bytes(pre, "stdin"))
            self.assertEqual(pre_result.returncode, 0, msg=pre_result.stderr)
        stdin = _read_bytes(name, "stdin")
        session_id = json.loads(stdin).get("session_id")
        before = self._snapshot(session_id)
        lines_before = len(self._hooks_log_lines())
        result = self._run(verb, stdin, env_extra=expect.get("env"))
        after = self._snapshot(session_id)

        self.assertEqual(result.returncode, expect["exit"], msg=result.stderr)
        out = self._out(result)
        self.assertEqual(out["contract_version"], 1)
        self.assertEqual(out["outcome"], expect["outcome"])
        self.assertEqual(sorted(out), expect["stdout_keys"])
        observed = set()
        if after["ledger"] > before["ledger"]:
            observed.add("ledger")
        if after["life"] > before["life"]:
            observed.add("life")
        if after["manas"] > before["manas"]:
            observed.add("manas")
        if after["sessions"] and not before["sessions"]:
            observed.add("sessions")
        self.assertEqual(sorted(observed), sorted(expect["writes"]))
        if expect["exit"] == 2:
            self.assertFalse(os.path.exists(self._db_path()), "exit 2 は raiya.db を作らない")
            self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))
            self.assertEqual(len(self._hooks_log_lines()) - lines_before, 1)
            self.assertEqual(len(self._hooks_log_lines()), 1)
            self.assertIn(expect["outcome"], self._hooks_log_lines()[0])
        return out

    def test_every_fixture_case(self):
        for name in _cases():
            with self.subTest(name=name):
                # 1 case ごとに空の state_dir (subTest の間で使い回さない)
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                self._run_case(name)

    def test_compact_fixture_semantics(self):
        expected = {
            "compact__ok": lambda o: (self.assertEqual(o["keep"], [1, 2, 3]), self.assertNotEqual(o["narrative"], "")),
            "compact__empty_window": lambda o: (self.assertEqual(o["narrative"], ""), self.assertEqual(o["keep"], [])),
            "compact__unknown_ids": lambda o: (self.assertEqual(o["unknown_ids"], [101, 102]),
                                               self.assertEqual(o["narrative"], "")),
            "compact__private_prompt": lambda o: self.assertEqual(o["keep"], [1, 2, 3]),
        }
        for name, check in expected.items():
            with self.subTest(name=name):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                check(self._run_case(name))

    def test_recall_normal_reports_context_turns_and_life_begun(self):
        out = self._run_case("recall__normal")
        self.assertEqual(out["context_turns"], 5)
        self.assertIs(out["life_begun"], True)
        self.assertEqual(out["life"], 1)
        self.assertEqual(out["narrative"], "")
        self.assertEqual((out["seeds"], out["index"], out["resume"], out["warnings"]), ([], [], None, []))

    def test_second_session_start_does_not_advance_life(self):
        first = self._run("recall", _read_bytes("recall__normal", "stdin"))
        second = self._run("recall", _read_bytes("recall__second_session_start", "stdin"))
        a, b = self._out(first), self._out(second)
        self.assertIs(a["life_begun"], True)
        self.assertIs(b["life_begun"], False)
        self.assertEqual(a["life"], b["life"])
        with open(os.path.join(self.state, "sessions", "sess.json"), encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), ["ts"])  # 印は {"ts"} だけ (prompt / recent は持たない)

    def test_record_tool_refs_land_in_the_ledger_row_body(self):
        out = self._run_case("record__tool_refs")
        self.assertEqual(out["outcome"], "ok")
        rows = self._ledger_bodies()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_refs"], [
            {"kind": "file", "path": "/work/a.py", "line_start": 1, "line_end": 10}, {"kind": "command", "name": "pytest"},
        ])

    def test_record_without_tool_refs_writes_no_key(self):
        self._run_case("record__normal")
        self.assertNotIn("tool_refs", self._ledger_bodies()[0])

    def test_record_demoted_row_has_empty_grounding(self):
        out = self._run_case("record__demoted")
        self.assertIs(out["demoted"], True)
        row = self._ledger_bodies()[0]
        self.assertEqual(row["grounding_text"], "")
        self.assertEqual(row["user_text"], "nope")

    def test_record_paste_keeps_paste_in_record_but_not_in_grounding(self):
        self._run_case("record__paste")
        row = self._ledger_bodies()[0]
        self.assertIn("some pasted log text", row["user_text"])
        self.assertNotIn("some pasted log text", row["grounding_text"])

    def test_cwd_that_does_not_exist_is_accepted(self):
        # resolve_project は存在しない絶対 path でも realpath を返す (git 外 = cwd 自身)。null になるのは
        # cwd が無い・非 str・相対のときだけ。
        out = self._out(self._run("recall", _read_bytes("recall__normal", "stdin")))
        self.assertEqual(out["project"], "/work")
        no_cwd = self._out(self._run("recall", {"contract_version": 1, "prompt": "hello"}))
        self.assertIsNone(no_cwd["project"])
        relative = self._out(self._run("record", self._record(cwd="rel/dir")))
        self.assertIsNone(relative["project"])


class TestRefusals(_HookTestCase):
    def _refused(self, verb, stdin, code, field):
        result = self._run(verb, stdin)
        self.assertEqual(result.returncode, 2, msg=result.stdout)
        out = self._out(result)
        self.assertEqual(out, {"contract_version": 1, "outcome": "refused:" + code, "field": field})
        self.assertFalse(os.path.exists(self._db_path()))
        self.assertEqual(len(self._hooks_log_lines()), 1)
        self.assertIn("refused:" + code, self._hooks_log_lines()[0])

    def test_refusal_creates_nothing_when_state_dir_is_absent(self):
        missing = os.path.join(self.root, "not", "yet", "state")
        cases = (("bogus", b"{}", "verb", None),
                 ("recall", b'{"contract_version":1,"session_id":"s","cwd":"/w","prompt":"p","zzz":1}', "field", "zzz"),
                 ("recall", b'{"contract_version":2}', "version", "contract_version"),
                 ("recall", b"not json", "stdin", None),
                 ("recall", b'{"contract_version":1,"prompt":3}', "type", "prompt"))
        for verb, stdin, code, _field in cases:
            with self.subTest(verb=verb, code=code):
                result = self._run(verb, stdin, env_extra={"RAIYA_STATE_DIR": missing})
                self.assertEqual(result.returncode, 2, msg=result.stdout)
                self.assertEqual(self._out(result)["outcome"], "refused:" + code)
                self.assertFalse(os.path.exists(os.path.join(self.root, "not")))

    def test_refusal_when_state_dir_is_a_file(self):
        path = os.path.join(self.root, "state_is_file")
        with open(path, "wb") as f:
            f.write(b"keep me")
        result = self._run("bogus", b"{}", env_extra={"RAIYA_STATE_DIR": path})
        self.assertEqual(result.returncode, 2, msg=result.stdout)
        self.assertEqual(self._out(result)["outcome"], "refused:verb")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        self.assertEqual(os.path.getsize(path), 7)

    def test_version_flag_in_empty_cwd_creates_no_default_state_dir(self):
        cwd = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAIYA_")}
        env["RAIYA_CONFIG"] = ""
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-m", "raiya.hooks.raiya_hook", "--version"], input=b"",
                                capture_output=True, env=env, cwd=cwd, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._out(result)["outcome"], "refused:verb")
        self.assertEqual(os.listdir(cwd), [])

    def test_stdin_that_is_not_a_json_object(self):
        for stdin in (b"", b"not json", b"[1]", b"3", b'"x"', b"null", b"\xff\xfe"):
            with self.subTest(stdin=stdin):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                self._refused("recall", stdin, "stdin", None)

    def test_version_missing_wrong_or_not_an_int(self):
        for version in ({}, {"contract_version": 0}, {"contract_version": 2}, {"contract_version": True},
                        {"contract_version": 1.0}, {"contract_version": "1"}, {"contract_version": None}):
            with self.subTest(version=version):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                payload = dict(version, prompt="hello")
                self._refused("recall", payload, "version", "contract_version")

    def test_unknown_field_per_verb(self):
        cases = [("recall", {"contract_version": 1, "prompt": "hello", "user_text": "x"}, "user_text"),
                 ("record", self._record(prompt="x"), "prompt"),
                 ("consolidate", {"contract_version": 1, "cwd": "/work"}, "cwd"),
                 ("session_end", {"contract_version": 1, "reason": "exit"}, "reason")]
        for verb, payload, field in cases:
            with self.subTest(verb=verb):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                self._refused(verb, payload, "field", field)

    def test_version_is_checked_before_unknown_fields_and_types(self):
        self._refused("recall", {"contract_version": 2, "extra": 1, "prompt": 3}, "version", "contract_version")

    def test_unknown_field_is_checked_before_types(self):
        self._refused("recall", {"contract_version": 1, "extra": 1, "prompt": 3}, "field", "extra")

    def test_type_violations(self):
        recall = {"contract_version": 1, "prompt": "hello"}
        cases = [
            ("recall", {"contract_version": 1}, "prompt"),
            ("recall", dict(recall, prompt=3), "prompt"),
            ("recall", dict(recall, session_id=3), "session_id"),
            ("recall", dict(recall, cwd=["/work"]), "cwd"),
            ("recall", dict(recall, context="x"), "context"),
            ("recall", dict(recall, context=["a", 1]), "context"),
            ("recall", dict(recall, session_start="true"), "session_start"),
            ("recall", dict(recall, session_start=None), "session_start"),
            ("record", self._record(user_text=None), "user_text"),
            ("record", {k: v for k, v in self._record().items() if k != "assistant_text"}, "assistant_text"),
            ("record", self._record(grounding_text=None), "grounding_text"),
            ("record", self._record(grounding_text="evidence"), "grounding_text"),
            ("record", self._record(tool_refs="x"), "tool_refs"),
            ("consolidate", {"contract_version": 1, "session_id": 3}, "session_id"),
        ]
        for verb, payload, field in cases:
            with self.subTest(verb=verb, field=field, payload=payload):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                self._refused(verb, payload, "type", field)

    def test_tool_refs_limits(self):
        ok_file = {"kind": "file", "path": "/a"}
        bad = {
            "too_many": [ok_file] * 65,
            "relative": [{"kind": "file", "path": "a/b"}],
            "empty_path": [{"kind": "file", "path": ""}],
            "long_path": [{"kind": "file", "path": "/" + "a" * 4096}],
            "multibyte_long_path": [{"kind": "file", "path": "/" + "あ" * 1366}],  # 4099 bytes
            "start_zero": [{"kind": "file", "path": "/a", "line_start": 0}],
            "end_before_start": [{"kind": "file", "path": "/a", "line_start": 5, "line_end": 2}],
            "end_zero": [{"kind": "file", "path": "/a", "line_end": 0}],
            "start_bool": [{"kind": "file", "path": "/a", "line_start": True}],
            "start_str": [{"kind": "file", "path": "/a", "line_start": "1"}],
            "lines_key": [{"kind": "file", "path": "/a", "lines": [1, 2]}],
            "unknown_key": [{"kind": "file", "path": "/a", "mode": "r"}],
            "unknown_kind": [{"kind": "url", "name": "x"}],
            "not_a_dict": ["/a"],
            "long_name": [{"kind": "command", "name": "n" * 257}],
            "empty_name": [{"kind": "command", "name": ""}],
            "name_not_str": [{"kind": "command", "name": 3}],
            "command_with_path": [{"kind": "command", "name": "ls", "path": "/a"}],
        }
        for label, refs in bad.items():
            with self.subTest(label=label):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                self._refused("record", self._record(tool_refs=refs), "type", "tool_refs")

    def test_tool_refs_accepted_edges(self):
        good = [{"kind": "file", "path": "/" + "a" * 4095}, {"kind": "file", "path": "/a", "line_start": 3, "line_end": 3},
                {"kind": "file", "path": "/a", "line_start": 7}, {"kind": "file", "path": "/a", "line_end": 9},
                {"kind": "command", "name": "n" * 256}] + [{"kind": "file", "path": "/a"}] * 59
        self.assertEqual(len(good), 64)
        out = self._out(self._run("record", self._record(tool_refs=good)))
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(self._ledger_bodies()[0]["tool_refs"], good)
        empty = self._out(self._run("record", self._record(tool_refs=[], user_text="hello again")))
        self.assertEqual(empty["outcome"], "ok")
        self.assertEqual(self._ledger_bodies()[1]["tool_refs"], [])

    def test_unknown_verb_and_missing_verb(self):
        for argv in (["bogus"], ["stop"], []):
            with self.subTest(argv=argv):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                result = subprocess.run([sys.executable, "-m", "raiya.hooks.raiya_hook"] + argv,
                                        input=b'{"contract_version":1}', capture_output=True, env=self._env(),
                                        cwd=self.root, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self._out(result), {"contract_version": 1, "outcome": "refused:verb", "field": None})
                self.assertFalse(os.path.exists(self._db_path()))
                self.assertEqual(len(self._hooks_log_lines()), 1)

    def test_hooks_log_does_not_carry_conversation_text(self):
        secret = "SECRET_PROMPT_TEXT"
        self._refused("recall", {"contract_version": 1, "prompt": 3, "session_id": "s-" + secret}, "type", "prompt")
        self._run("record", self._record(user_text=secret + " question", assistant_text="SECRET_ANSWER"))
        text = "\n".join(self._hooks_log_lines())
        self.assertNotIn("SECRET_ANSWER", text)
        self.assertNotIn(secret + " question", text)


class TestExit0Paths(_HookTestCase):
    def test_guard_env_skips_before_reading_stdin_and_writes_nothing(self):
        for key, value in (("RAIYA_HOOK_ACTIVE", "1"), ("RAIYA_HOOKS", "0")):
            for verb in ("recall", "record", "consolidate", "session_end"):
                with self.subTest(key=key, verb=verb):
                    result = self._run(verb, b"this is not json", env_extra={key: value})
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(self._out(result), {"contract_version": 1, "outcome": "skip:guard"})
                    self.assertEqual(os.listdir(self.state), [])

    def test_legacy_state_is_skipped_and_nothing_is_written(self):
        with open(os.path.join(self.state, "alaya.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        result = self._run("record", self._record())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"contract_version": 1, "outcome": "skip:legacy_state"})
        self.assertEqual(result.stderr.decode("utf-8").count("\n"), 1)  # stderr は人が読む 1 行だけ
        self.assertEqual(sorted(os.listdir(self.state)), ["alaya.json"])

    def test_state_dir_lost_is_skipped_and_nothing_is_written(self):
        bad = os.path.join(self.root, "config.yaml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("state_dir: [unclosed\n  : :\n")
        env = self._env()
        del env["RAIYA_STATE_DIR"]
        env["RAIYA_CONFIG"] = bad
        result = self._run("record", self._record(), env=env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout), {"contract_version": 1, "outcome": "skip:state_dir_lost"})
        self.assertEqual(os.listdir(self.root).count("state"), 1)
        self.assertEqual(os.listdir(self.state), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "raiya.db")))

    def test_unreadable_db_is_fail_with_class_name_and_exit_0(self):
        with open(self._db_path(), "wb") as f:
            f.write(b"this is not a sqlite database" * 100)
        for verb, stdin in (("recall", {"contract_version": 1, "prompt": "hello"}), ("record", self._record()),
                            ("consolidate", {"contract_version": 1})):
            with self.subTest(verb=verb):
                result = self._run(verb, stdin)
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(self._out(result), {"contract_version": 1, "outcome": "fail:StateUnreadable"})
        self.assertIn("状態DB 読めず category=", self._hooks_log_lines()[0])

    def test_unsafe_session_id_does_not_begin_a_life(self):
        for sid in ("a/b", "..", "", None):
            with self.subTest(sid=sid):
                out = self._out(self._run("recall", {"contract_version": 1, "session_id": sid, "prompt": "hello",
                                                     "session_start": True}))
                self.assertIs(out["life_begun"], False)
                self.assertEqual(out["life"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))

    def test_control_char_session_id_does_not_begin_a_life(self):
        for sid in ("bad\nid", "bad\rid", "x\u001b[31mid", "a\u0000b", "a\u007fb"):
            with self.subTest(sid=sid):
                out = self._out(self._run("recall", {"contract_version": 1, "session_id": sid, "prompt": "hello",
                                                     "session_start": True}))
                self.assertIs(out["life_begun"], False)
                self.assertEqual(out["life"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))

    def test_non_ascii_printable_session_id_is_still_accepted(self):
        out = self._out(self._run("recall", {"contract_version": 1, "session_id": "セッション-1",
                                             "prompt": "hello", "session_start": True}))
        self.assertIs(out["life_begun"], True)
        self.assertTrue(self._snapshot("セッション-1")["sessions"])

    def test_skippable_recall_still_begins_the_life_and_writes_the_mark(self):
        out = self._out(self._run("recall", {"contract_version": 1, "session_id": "s9", "prompt": "/help",
                                             "session_start": True}))
        self.assertEqual(out, {"contract_version": 1, "outcome": "skip:skippable_prompt"})
        self.assertEqual(self._snapshot("s9")["life"], 1)
        self.assertTrue(self._snapshot("s9")["sessions"])

    def test_record_skips(self):
        cases = {
            "skip:skippable_user": self._record(user_text="/clear"),
            "skip:empty_assistant": self._record(assistant_text="  <private>p</private>  "),
            "skip:skippable_after_strip": self._record(
                user_text='<pasted_content id="1">only a paste</pasted_content id="1">'),
        }
        for outcome, payload in cases.items():
            with self.subTest(outcome=outcome):
                out = self._out(self._run("record", payload))
                self.assertEqual(out, {"contract_version": 1, "outcome": outcome})
        self.assertEqual(self._snapshot()["ledger"], 0)

    def test_record_reports_consolidate_due_at_threshold(self):
        outs = [self._out(self._run("record", self._record(user_text="question {}".format(i)))) for i in range(5)]
        self.assertEqual([o["pending"] for o in outs], [1, 2, 3, 4, 5])
        self.assertEqual([o["consolidate_due"] for o in outs], [False] * 4 + [True])
        self.assertEqual([o["ledger_id"] for o in outs], [1, 2, 3, 4, 5])
        self.assertIn("session=s1 ledger_id=1 pending=1", self._hooks_log_lines()[0])

    def test_truncation_is_logged_and_applied(self):
        out = self._out(self._run("record", self._record(user_text="a" * 9000)))
        self.assertEqual(out["outcome"], "ok")
        self.assertIn("切り詰めました", "\n".join(self._hooks_log_lines()))
        self.assertIn("<elided", self._ledger_bodies()[0]["user_text"])


class TestRecallContextReachesEngine(_HookTestCase):
    def test_context_passed_to_engine_recall_is_stripped_filtered_and_last_five(self):
        from unittest import mock
        from raiya.hooks import raiya_hook
        context = ["<private>hidden</private>", "/skip", "<system-reminder>x</system-reminder>",
                   'keep0 <pasted_content id="1">PASTEBODY</pasted_content id="1">',
                   '<pasted_content id="2">only paste</pasted_content id="2">']
        context += ["turn {}".format(i) for i in range(7)]
        engine = mock.Mock()
        engine.life = 1
        engine.recall.return_value = {"narrative": "", "seeds": [], "index": [], "resume": None,
                                      "alaya_warning": None, "manas_warning": None}
        args = {"session_id": "s1", "cwd": None, "prompt": "hello <private>PRIV</private>there",
                "context": context, "session_start": False}
        with mock.patch.object(raiya_hook, "MemoryEngine", return_value=engine):
            out = raiya_hook._do_recall(args, self.state)
        self.assertEqual(out["outcome"], "ok")
        (prompt,), kwargs = engine.recall.call_args
        self.assertNotIn("PRIV", prompt)
        self.assertEqual(kwargs["context"], ["turn {}".format(i) for i in range(2, 7)])
        # 5 件より少ないときは、private / 貼り付け / skippable が落ちた残りが全部入る
        args["context"] = context[:5] + ["turn A"]
        with mock.patch.object(raiya_hook, "MemoryEngine", return_value=engine):
            raiya_hook._do_recall(args, self.state)
        ctx = engine.recall.call_args[1]["context"]
        self.assertEqual(ctx, ["keep0", "turn A"])
        self.assertTrue(all("hidden" not in c and "PASTEBODY" not in c and "only paste" not in c for c in ctx))


    def test_lone_surrogate_only_context_element_is_dropped_on_the_raw_value(self):
        from unittest import mock
        from raiya.hooks import raiya_hook
        engine = mock.Mock()
        engine.life = 1
        engine.recall.return_value = {"narrative": "", "seeds": [], "index": [], "resume": None,
                                      "alaya_warning": None, "manas_warning": None}
        payload = (b'{"contract_version":1,"session_id":"s1","prompt":"hello there",'
                   b'"context":["\\ud83d","turn A"]}')
        args = raiya_hook._validate("recall", json.loads(payload))
        with mock.patch.object(raiya_hook, "MemoryEngine", return_value=engine):
            raiya_hook._do_recall(args, self.state)
        self.assertEqual(engine.recall.call_args[1]["context"], ["turn A"])


class TestSurrogatesAndLogging(_HookTestCase):
    def test_surrogate_session_id_with_type_error_is_exit_2_with_one_log_line(self):
        result = self._run("recall", b'{"contract_version":1,"session_id":"a\\ud800","prompt":3}')
        self.assertEqual(result.returncode, 2, msg=result.stdout)
        self.assertEqual(self._out(result)["outcome"], "refused:type")
        self.assertEqual(len(self._hooks_log_lines()), 1)
        self.assertFalse(os.path.exists(self._db_path()))

    def test_surrogate_unknown_key_is_exit_2_with_one_log_line(self):
        result = self._run("recall", b'{"contract_version":1,"prompt":"hi","x\\ud800":1}')
        self.assertEqual(result.returncode, 2, msg=result.stdout)
        self.assertEqual(self._out(result)["outcome"], "refused:field")
        self.assertEqual(len(self._hooks_log_lines()), 1)
        self.assertFalse(os.path.exists(self._db_path()))

    def test_record_with_surrogate_session_id_is_ok_and_logged(self):
        result = self._run("record", b'{"contract_version":1,"session_id":"a\\ud800","user_text":"hello",'
                                     b'"assistant_text":"answer"}')
        self.assertEqual(result.returncode, 0, msg=result.stdout)
        self.assertEqual(self._out(result)["outcome"], "ok")
        self.assertEqual(self._snapshot()["ledger"], 1)
        self.assertEqual(len(self._hooks_log_lines()), 1)
        self.assertIn("ledger_id=1", self._hooks_log_lines()[0])

    def test_hooks_log_has_no_raw_control_chars_for_refusal_and_record(self):
        sid = "x\r\u001b[31mFAKE\nforged line"
        self._run("record", {"contract_version": 1, "session_id": sid, "cwd": "/w", "user_text": 3,
                             "assistant_text": "a"})  # refusal path
        self._run("record", self._record(session_id=sid))  # ok path
        self._run("record", self._record(session_id=sid, user_text="/clear"))  # skip path
        with open(os.path.join(self.state, "hooks.log"), "rb") as f:
            raw = f.read()
        for bad in (b"\r", b"\x1b", b"\x00"):
            self.assertNotIn(bad, raw)
        lines = self._hooks_log_lines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIn("session=None", line)
            self.assertNotIn("FAKE", line)
        self.assertEqual(self._snapshot()["ledger"], 1)  # ledger still gets the id as-is

    def test_long_session_id_is_cut_in_every_log_line(self):
        sid = "S" * 300
        self._run("record", self._record(session_id=sid))
        self._run("record", self._record(session_id=sid, user_text="/clear"))
        self._run("recall", {"contract_version": 1, "session_id": sid, "prompt": 3})
        lines = self._hooks_log_lines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertNotIn("S" * 65, line)
        self.assertIn("skip:skippable_user", lines[1])
        self.assertNotIn("skip: skip:", lines[1])


class TestUnencodableStrings(_HookTestCase):
    def test_surrogates_in_any_string_field_never_fail(self):
        raw = (b'{"contract_version":1,"session_id":"s1","cwd":"/w\\ud800","user_text":"hello \\ud83d",'
               b'"assistant_text":"answer \\ud83d"}')
        out = self._out(self._run("record", raw))
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(self._snapshot()["ledger"], 1)
        row = self._ledger_bodies()[0]
        self.assertEqual(row["assistant_text"], "answer \\ud83d")
        recall = (b'{"contract_version":1,"session_id":"s2","cwd":"/w\\ud800","prompt":"hello \\ud83d",'
                  b'"context":["ctx \\ud83d"],"session_start":true}')
        self.assertEqual(self._out(self._run("recall", recall))["outcome"], "ok")

    def test_lone_surrogate_only_user_text_and_prompt_are_skippable_on_the_raw_value(self):
        # 裁定 (2026-10-27): skippable は _clean 前の生の値で判定する (生は 1 字 = 短い)。
        # _clean 後の `\\ud83d` (6 字) で判定すると記録されてしまう。
        raw = b'{"contract_version":1,"session_id":"s1","user_text":"\\ud83d","assistant_text":"answer"}'
        result = self._run("record", raw)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result)["outcome"], "skip:skippable_user")
        self.assertEqual(self._snapshot()["ledger"], 0)
        # 生の判定が最初: assistant が空でも skip:skippable_user (skip:empty_assistant ではない)
        raw = b'{"contract_version":1,"session_id":"s1","user_text":"\\ud83d","assistant_text":""}'
        self.assertEqual(self._out(self._run("record", raw))["outcome"], "skip:skippable_user")
        self.assertEqual(self._snapshot()["ledger"], 0)
        recall = b'{"contract_version":1,"session_id":"s2","prompt":"\\ud83d"}'
        result = self._run("recall", recall)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result)["outcome"], "skip:skippable_prompt")

    def test_private_only_plus_lone_surrogate_is_skipped_like_stop_path(self):
        # 生の値で段ごとに判定する: strip_private 後は "\ud83d" 1 字 (短い)。_clean 後 (6 字) で判定すると記録される。
        from raiya.hooks.core import prepare_record
        self.assertEqual(prepare_record("<private>secret</private>\ud83d", "answer").skip,
                         "skip:skippable_after_strip")
        raw = (b'{"contract_version":1,"session_id":"s1","user_text":"<private>secret</private>\\ud83d",'
               b'"assistant_text":"answer"}')
        result = self._run("record", raw)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result)["outcome"], "skip:skippable_after_strip")
        self.assertEqual(self._snapshot()["ledger"], 0)

    def test_recall_private_only_plus_lone_surrogate_prompt_is_skippable(self):
        recall = b'{"contract_version":1,"session_id":"s2","prompt":"<private>secret</private>\\ud83d"}'
        result = self._run("recall", recall)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result)["outcome"], "skip:skippable_prompt")

    def test_context_private_only_plus_lone_surrogate_is_dropped(self):
        from unittest import mock
        from raiya.hooks import raiya_hook
        engine = mock.Mock()
        engine.life = 1
        engine.recall.return_value = {"narrative": "", "seeds": [], "index": [], "resume": None,
                                      "alaya_warning": None, "manas_warning": None}
        payload = (b'{"contract_version":1,"session_id":"s1","prompt":"hello there",'
                   b'"context":["<private>x</private>\\ud83d","turn A"]}')
        args = raiya_hook._validate("recall", json.loads(payload))
        with mock.patch.object(raiya_hook, "MemoryEngine", return_value=engine):
            raiya_hook._do_recall(args, self.state)
        self.assertEqual(engine.recall.call_args[1]["context"], ["turn A"])

    def test_record_lone_surrogates_respect_truncate_limit(self):
        from raiya.hooks.core import TRUNCATE_LIMIT
        body = "x" * 20 + "\\ud83d" * 9000
        raw = ('{"contract_version":1,"session_id":"s1","user_text":"' + body + '","assistant_text":"' + body
               + '"}').encode("ascii")
        result = self._run("record", raw)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result)["outcome"], "ok")
        row = self._ledger_bodies()[0]
        for key in ("user_text", "assistant_text", "grounding_text"):
            text = row[key]
            self.assertLessEqual(len(text), TRUNCATE_LIMIT + 60, key)
            text.encode("utf-8")  # 孤立サロゲートが残っていない
            self.assertIn("elided", text, key)

    def test_record_lone_surrogate_in_assistant_only_is_recorded_clean(self):
        raw = b'{"contract_version":1,"session_id":"s1","user_text":"hello there","assistant_text":"answer \\udc00"}'
        self.assertEqual(self._out(self._run("record", raw))["outcome"], "ok")
        row = self._ledger_bodies()[0]
        self.assertTrue(row["assistant_text"].endswith("\\udc00"), row["assistant_text"])

    def test_debug_log_session_id_is_the_safe_value_or_none(self):
        from raiya import debuglog
        with open(os.path.join(self.state, "debug"), "w") as f:
            f.write("1")
        raw = b'{"contract_version":1,"session_id":"s\\ud83d1","user_text":"hello there","assistant_text":"answer"}'
        self.assertEqual(self._out(self._run("record", raw))["outcome"], "ok")
        record = [e for e in debuglog.read_events(self.state) if e.get("event") == "record"][0]
        # debug-log spec: session_id は _safe_session_id 後の値。`s\\ud83d1` は不可 -> None / ok False
        self.assertIsNone(record["session_id"])
        self.assertIs(record["session_id_ok"], False)
        raw = b'{"contract_version":1,"session_id":"s1","user_text":"hello there","assistant_text":"answer"}'
        self.assertEqual(self._out(self._run("record", raw))["outcome"], "ok")
        record = [e for e in debuglog.read_events(self.state) if e.get("event") == "record"][-1]
        self.assertEqual(record["session_id"], "s1")
        self.assertIs(record["session_id_ok"], True)

    def test_surrogate_in_command_name_is_refused(self):
        raw = (b'{"contract_version":1,"user_text":"hello","assistant_text":"a",'
               b'"tool_refs":[{"kind":"command","name":"ls\\ud800"}]}')
        result = self._run("record", raw)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._out(result)["field"], "tool_refs")
        self.assertEqual(len(self._hooks_log_lines()), 1)
        self.assertFalse(os.path.exists(self._db_path()))

    def test_refusal_with_stdout_closed_is_still_exit_2_with_one_log_line(self):
        cmd = "exec '{}' -m raiya.hooks.raiya_hook recall >&-".format(sys.executable)
        result = subprocess.run(["sh", "-c", cmd], input=b'{"contract_version":1,"prompt":3}',
                                stderr=subprocess.PIPE, env=self._env(), cwd=self.root, timeout=60)
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertEqual(len(self._hooks_log_lines()), 1)
        self.assertIn("refused:type", self._hooks_log_lines()[0])
        self.assertFalse(os.path.exists(self._db_path()))


class TestPrepareRecordOrder(_HookTestCase):
    def test_private_only_user_is_skippable_after_strip_for_raiya_hook(self):
        out = self._out(self._run("record", self._record(user_text="<private>x</private>")))
        self.assertEqual(out, {"contract_version": 1, "outcome": "skip:skippable_after_strip"})

    def test_empty_assistant_wins_over_after_strip_when_user_is_private_only(self):
        out = self._out(self._run("record", self._record(user_text="<private>x</private>", assistant_text="")))
        self.assertEqual(out["outcome"], "skip:empty_assistant")

    def test_claude_code_stop_uses_the_same_order(self):
        from raiya.hooks import claude_code
        for answer, expected in (("answer", "skip:skippable_after_strip"), ("", "skip:empty_assistant")):
            with self.subTest(answer=answer):
                transcript = os.path.join(self.root, "t.jsonl")
                with open(transcript, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "user", "message": {"content": "<private>x</private>"}}) + "\n")
                payload = {"session_id": "cc-" + expected[5:], "transcript_path": transcript,
                           "last_assistant_message": answer}
                self.assertEqual(claude_code.cmd_stop(payload, self.state), expected)
        self.assertFalse(os.path.exists(self._db_path()))


class TestDebugLogEvents(_HookTestCase):
    def test_level1_recall_and_record_events_carry_no_conversation_text(self):
        from raiya import debuglog
        with open(os.path.join(self.state, "debug"), "w") as f:
            f.write("1")
        secret = "SECRETWORDXYZ"
        self._out(self._run("recall", {"contract_version": 1, "session_id": "d1", "cwd": "/work",
                                       "prompt": "hello " + secret, "context": ["ctx " + secret],
                                       "session_start": True}))
        self._out(self._run("record", self._record(session_id="d1", user_text="question " + secret,
                                                   assistant_text="answer " + secret)))
        events = debuglog.read_events(self.state)
        names = [e.get("event") for e in events]
        self.assertEqual(names, ["recall", "hook_end", "record", "hook_end"], msg=events)
        recall, record = events[0], events[2]
        self.assertEqual(recall["context_n"], 1)
        self.assertIs(recall["life_begun"], True)
        self.assertEqual(recall["session_id"], "d1")
        self.assertEqual(record["ledger_id"], 1)
        self.assertEqual(record["user_len"], len("question " + secret))
        with open(os.path.join(self.state, "debug.log"), encoding="utf-8") as f:
            self.assertNotIn(secret, f.read())
        with open(os.path.join(self.state, "hooks.log"), encoding="utf-8") as f:
            self.assertNotIn(secret, f.read())

    def test_level2_adds_text(self):
        from raiya import debuglog
        with open(os.path.join(self.state, "debug"), "w") as f:
            f.write("2")
        self._run("record", self._record(user_text="question LEVEL2TEXT"))
        record = [e for e in debuglog.read_events(self.state) if e.get("event") == "record"][0]
        self.assertIn("LEVEL2TEXT", record["user_text"])

    def test_level0_writes_no_debug_log(self):
        self._run("recall", {"contract_version": 1, "prompt": "hello"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "debug.log")))


class TestConsolidateVerbs(_HookTestCase):
    def _fixed_llm(self, proposals):
        body = "#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{}\nEOF\n".format(json.dumps(proposals, ensure_ascii=False))
        return self._write_llm("fixed_llm.sh", body)

    def test_consolidate_sets_recursion_guard_env_for_the_llm_child(self):
        marker = os.path.join(self.root, "child_env.txt")
        llm = self._write_llm(
            "env_llm.sh", "#!/bin/sh\ncat > /dev/null\necho \"$RAIYA_HOOK_ACTIVE\" > '{}'\necho '[]'\n".format(marker))
        self._run("record", self._record())
        for verb in ("consolidate", "session_end"):
            if os.path.exists(marker):
                os.remove(marker)
            self._run("record", self._record(user_text="another question for " + verb))
            out = self._out(self._run(verb, {"contract_version": 1, "session_id": "s1"}, llm=llm))
            self.assertEqual(out["outcome"], "ok", msg=out)
            self.assertGreaterEqual(out["window"], 1)
            with open(marker, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "1")

    def test_consolidate_result_shape_and_hooks_log_line(self):
        llm = self._fixed_llm([])
        self._run("record", self._record())
        out = self._out(self._run("consolidate", {"contract_version": 1, "session_id": "s1"}, llm=llm))
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(out["window"], 1)
        self.assertEqual(out["window_sessions"], 1)
        self.assertEqual(out["counts"], {key: 0 for key in (
            "圓成實", "依他起", "遍計所執", "reject", "supersede", "取り下げ照合", "双子照合")})
        self.assertIn("session=s1 consolidate window=1 sessions=1 proposals=0", self._hooks_log_lines()[-1])
        # 窓は消費済み: 次は空窓
        again = self._out(self._run("session_end", {"contract_version": 1}, llm=llm))
        self.assertEqual((again["outcome"], again["window"], again["window_sessions"]), ("ok", 0, 0))

    def test_consolidate_counts_a_planted_seed(self):
        self._run("record", self._record(user_text="我々は deploy に blue-green を使う", assistant_text="了解"))
        proposal = [{"content": "deploy は blue-green", "conditions": ["deploy"], "polarity": "無記", "kind": "決定",
                     "evidence": {"source": "user", "span": "deploy に blue-green を使う"}}]
        out = self._out(self._run("consolidate", {"contract_version": 1, "session_id": "s1"},
                                  llm=self._fixed_llm(proposal)))
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(out["counts"]["圓成實"], 1)
        self.assertEqual(set(out["counts"]), {
            "圓成實", "依他起", "遍計所執", "reject", "supersede", "取り下げ照合", "双子照合"})

    def test_llm_failure_is_fail_class_with_exit_0_and_hooks_log_line(self):
        self._run("record", self._record())
        result = self._run("consolidate", {"contract_version": 1, "session_id": "s1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result), {"contract_version": 1, "outcome": "fail:BackendError"})
        self.assertIn("consolidate失敗: BackendError", self._hooks_log_lines()[-1])
        self.assertEqual(self._snapshot()["ledger"], 1)  # 窓は消費されない (従来どおり)

    BAD_SID = "x\r\u001b[31mFAKE"

    def _assert_log_clean(self):
        with open(os.path.join(self.state, "hooks.log"), "rb") as f:
            raw = f.read()
        for bad in (b"\r", b"\x1b", b"FAKE"):
            self.assertNotIn(bad, raw)

    def test_control_char_session_id_failure_line_shows_session_none(self):
        for verb in ("consolidate", "session_end"):
            with self.subTest(verb=verb):
                self._run("record", self._record(session_id=self.BAD_SID, user_text="question for " + verb))
                result = self._run(verb, {"contract_version": 1, "session_id": self.BAD_SID})
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self._out(result)["outcome"], "fail:BackendError")
                self.assertIn("session=None consolidate失敗", self._hooks_log_lines()[-1])
        self._assert_log_clean()

    def test_control_char_session_id_window_line_shows_session_none(self):
        llm = self._fixed_llm([])
        for verb in ("consolidate", "session_end"):
            with self.subTest(verb=verb):
                self._run("record", self._record(session_id=self.BAD_SID, user_text="question for " + verb))
                result = self._run(verb, {"contract_version": 1, "session_id": self.BAD_SID}, llm=llm)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self._out(result)["outcome"], "ok")
                self.assertIn("session=None consolidate window=1", self._hooks_log_lines()[-1])
        self._assert_log_clean()


class TestCompactVerb(_HookTestCase):
    """`compact` (契約 v1.1・設計書 §2.5.1 / §2.10)。読むだけの動詞。"""

    PRELUDE = ("record__normal", "record__paste", "record__tool_refs")

    def _prelude(self):
        for name in self.PRELUDE:
            r = self._run("record", _read_bytes(name, "stdin"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def _compact(self, **fields):
        payload = {"contract_version": 1, "session_id": "s1", "cwd": self.root, "window_ledger_ids": [1, 2, 3],
                   "prompt": "hello"}
        payload.update(fields)
        return self._run("compact", payload)

    def _tree_digest(self):
        import hashlib
        digest = {}
        for dirpath, _, files in os.walk(self.state):
            for f in files:
                if f in ("hooks.log", ".lock"):  # 件数だけの 1 行ログと flock の印。記憶 (raiya.db ほか) は不変であること
                    continue
                path = os.path.join(dirpath, f)
                with open(path, "rb") as fh:
                    digest[os.path.relpath(path, self.state)] = hashlib.sha256(fh.read()).hexdigest()
        return digest

    def test_control_char_session_id_log_line_shows_session_none(self):
        self._prelude()
        result = self._compact(session_id="x\r\u001b[31mFAKE")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._out(result)["outcome"], "ok")
        self.assertIn("session=None compact", self._hooks_log_lines()[-1])
        with open(os.path.join(self.state, "hooks.log"), "rb") as f:
            raw = f.read()
        for bad in (b"\r", b"\x1b", b"FAKE"):
            self.assertNotIn(bad, raw)

    def test_verb_set_equals_fixture_prefixes(self):
        from raiya.hooks import raiya_hook
        prefixes = {n.split("__")[0] for n in _cases() if n.split("__")[0] != "bogus"}
        self.assertEqual(set(raiya_hook.VERBS), prefixes)
        self.assertEqual(set(raiya_hook.ALLOWED_FIELDS), set(raiya_hook.VERBS))

    def test_ok_shape_and_no_turns_or_seeds(self):
        self._prelude()
        result = self._compact()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        out = self._out(result)
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(sorted(out), sorted([
            "contract_version", "outcome", "keep", "elided", "unknown_ids", "narrative", "cut", "chars",
            "budget_chars", "window_sessions", "project", "warnings"]))
        self.assertEqual(out["keep"], [1, 2, 3])
        self.assertTrue(out["narrative"])
        self.assertEqual(out["chars"], len(out["narrative"]))
        self.assertIsNone(out["budget_chars"])
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["unknown_ids"], [])
        self.assertEqual(out["window_sessions"], 1)

    def test_compact_is_read_only_state_dir_sha256_unchanged(self):
        self._prelude()
        before = self._tree_digest()
        for fields in ({}, {"window_ledger_ids": [1, 2, 3, 99], "budget_chars": 50},
                       {"prompt": "<private>x</private>"}, {"window_ledger_ids": []}):
            out = self._out(self._compact(**fields))
            self.assertEqual(out["outcome"], "ok")
        self.assertEqual(self._tree_digest(), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))
        self.assertIn("raiya.db", before)

    def test_empty_window_unknown_ids_and_dedup(self):
        self._prelude()
        out = self._out(self._compact(window_ledger_ids=[]))
        self.assertEqual((out["narrative"], out["keep"], out["chars"]), ("", [], 0))
        out = self._out(self._compact(window_ledger_ids=[7, 8]))
        self.assertEqual((out["narrative"], out["unknown_ids"]), ("", [7, 8]))
        a = self._out(self._compact(window_ledger_ids=[3, 1, 2, 2, 1]))
        b = self._out(self._compact(window_ledger_ids=[1, 2, 3]))
        self.assertEqual(a, b)

    def test_window_cap_counts_before_dedup(self):
        self._prelude()
        at_cap = self._compact(window_ledger_ids=[1] * 1000)
        self.assertEqual(at_cap.returncode, 0, msg=at_cap.stderr)
        self.assertEqual(self._out(at_cap)["keep"], [1])
        over = self._compact(window_ledger_ids=[1] * 1001)
        self.assertEqual(over.returncode, 2)
        out = self._out(over)
        self.assertEqual((out["outcome"], out["field"]), ("refused:type", "window_ledger_ids"))

    def test_refusals(self):
        base = {"contract_version": 1, "session_id": "s1", "cwd": "/w", "window_ledger_ids": [1], "prompt": "p"}
        cases = [
            ("window_ledger_ids", "field", lambda d: d.pop("window_ledger_ids")),
            ("prompt", "field", lambda d: d.pop("prompt")),
            ("window_ledger_ids", "type", lambda d: d.update(window_ledger_ids=None)),
            ("window_ledger_ids", "type", lambda d: d.update(window_ledger_ids="1")),
            ("window_ledger_ids", "type", lambda d: d.update(window_ledger_ids=[1, True])),
            ("window_ledger_ids", "type", lambda d: d.update(window_ledger_ids=[1, "2"])),
            ("window_ledger_ids", "type", lambda d: d.update(window_ledger_ids=[1.0])),
            ("window_ledger_ids", "type", lambda d: d.update(window_ledger_ids=[None])),
            ("prompt", "type", lambda d: d.update(prompt=None)),
            ("prompt", "type", lambda d: d.update(prompt=5)),
            ("budget_chars", "type", lambda d: d.update(budget_chars=None)),
            ("budget_chars", "type", lambda d: d.update(budget_chars=True)),
            ("budget_chars", "type", lambda d: d.update(budget_chars=1.5)),
            ("budget_chars", "type", lambda d: d.update(budget_chars="9")),
            ("budget_chars", "type", lambda d: d.update(budget_chars=-1)),
            ("session_id", "type", lambda d: d.update(session_id=3)),
            ("cwd", "type", lambda d: d.update(cwd=3)),
            ("extra", "field", lambda d: d.update(extra=1)),
            ("contract_version", "version", lambda d: d.update(contract_version=2)),
        ]
        for field, code, mutate in cases:
            with self.subTest(field=field, code=code):
                shutil.rmtree(self.state)
                os.makedirs(self.state)
                payload = dict(base)
                mutate(payload)
                result = self._compact_raw(payload)
                self.assertEqual(result.returncode, 2, msg=payload)
                out = self._out(result)
                self.assertEqual((out["outcome"], out["field"]), ("refused:" + code, field))
                self.assertFalse(os.path.exists(self._db_path()))
                self.assertEqual(len(self._hooks_log_lines()), 1)

    def _compact_raw(self, payload):
        return self._run("compact", payload)

    def test_budget_chars_zero_and_positive_are_accepted(self):
        self._prelude()
        for budget in (0, 10, 10 ** 6):
            with self.subTest(budget=budget):
                out = self._out(self._compact(budget_chars=budget))
                self.assertEqual((out["outcome"], out["budget_chars"]), ("ok", budget))

    def test_skippable_and_private_only_and_empty_prompt_are_not_skipped(self):
        self._prelude()
        for prompt in ("/help", "", "<private>secret</private>", "\ud83d"):
            with self.subTest(prompt=prompt):
                stdin = json.dumps({"contract_version": 1, "window_ledger_ids": [1, 2, 3], "prompt": prompt})
                out = self._out(self._run("compact", stdin))
                self.assertEqual(out["outcome"], "ok")
                self.assertEqual(out["keep"], [1, 2, 3])

    def test_private_text_never_reaches_logs(self):
        self._prelude()
        self._out(self._compact(prompt="<private>TOPSECRET</private> VISIBLEWORDXYZ"))
        for path in (os.path.join(self.state, "hooks.log"), os.path.join(self.state, "debug.log")):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    body = f.read()
                self.assertNotIn("TOPSECRET", body)
                self.assertNotIn("VISIBLEWORDXYZ", body)
        line = [l for l in self._hooks_log_lines() if " compact " in l][-1]
        self.assertRegex(line, r"session=s1 compact window=3 keep=3 elided=0 unknown=0 chars=\d+$")

    def test_debug_log_compact_event_has_counts_only_even_at_level_2(self):
        self._prelude()
        with open(os.path.join(self.state, "debug"), "w", encoding="utf-8") as f:
            f.write("2")
        self._out(self._compact(prompt="PROMPTMARKER"))
        with open(os.path.join(self.state, "debug.log"), encoding="utf-8") as f:
            body = f.read()
        events = [json.loads(l) for l in body.splitlines()]
        compact = [e for e in events if e.get("event") == "compact"]
        self.assertEqual(len(compact), 1, msg=body)
        for key in ("window_n", "keep", "elided", "unknown", "chars"):
            self.assertIn(key, compact[0])
        self.assertNotIn("PROMPTMARKER", body)
        self.assertNotIn("narrative", compact[0])

    def test_skip_responses_carry_only_version_and_outcome(self):
        out = self._out(self._run("compact", {"contract_version": 1, "window_ledger_ids": [], "prompt": "p"},
                                  env_extra={"RAIYA_HOOKS": "0"}))
        self.assertEqual(out, {"contract_version": 1, "outcome": "skip:guard"})
        self.assertFalse(os.path.exists(self._db_path()))

    def test_legacy_state_is_skipped(self):
        with open(os.path.join(self.state, "alaya.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        result = self._compact()
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(out, {"contract_version": 1, "outcome": "skip:legacy_state"})
        self.assertFalse(os.path.exists(self._db_path()))

    def test_unreadable_db_is_fail_not_skip(self):
        with open(self._db_path(), "wb") as f:
            f.write(b"not a sqlite database at all" * 10)
        out = self._out(self._compact())
        self.assertEqual(out, {"contract_version": 1, "outcome": "fail:StateUnreadable"})

    def test_lone_surrogate_in_old_ledger_row_is_normalized_and_chars_reset(self):
        from raiya.snapshot import write_state
        from tests._state_fixture import add_ledger_body
        shutil.rmtree(self.state)
        write_state(self.state, {})
        # 古い行: 本文は JSON の `\\ud83d` escape として DB に入っている (読むと孤立サロゲートの str)
        body = json.dumps({"id": 1, "life": 0, "session_id": "old", "user_text": "u\ud83dx",
                           "assistant_text": "a\ude00y"})
        add_ledger_body(self.state, body, row_id=1)
        result = self._compact(window_ledger_ids=[1])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        raw = result.stdout.decode("ascii")  # ensure_ascii の出力 (孤立サロゲートの生の escape は残らない)
        out = json.loads(raw)
        self.assertEqual(out["outcome"], "ok")
        self.assertIn("\\ud83d", out["narrative"])
        self.assertIn("\\ude00", out["narrative"])
        self.assertNotIn("\ud83d", raw.replace("\\ud83d", ""))
        out["narrative"].encode("utf-8")  # 符号化できる
        self.assertEqual(out["chars"], len(out["narrative"]))

    def test_elided_lines_are_normalized(self):
        from raiya.snapshot import write_state
        from tests._state_fixture import add_ledger_body
        shutil.rmtree(self.state)
        rows = [{"id": i, "life": 0, "session_id": "old", "user_text": "u\ud83d" + str(i),
                 "assistant_text": "a" + str(i)} for i in range(1, 12)]
        write_state(self.state, {})
        for r in rows:
            add_ledger_body(self.state, json.dumps(r), row_id=r["id"])
        out = self._out(self._compact(window_ledger_ids=list(range(1, 12))))
        self.assertTrue(out["elided"], msg=out)
        for e in out["elided"]:
            self.assertEqual(sorted(e), ["from", "line", "n", "to"])
            e["line"].encode("utf-8")
        out["narrative"].encode("utf-8")
        self.assertEqual(out["chars"], len(out["narrative"]))


class TestNoBackendImport(unittest.TestCase):
    def test_raiya_hook_source_does_not_import_backend_anywhere(self):
        with open(HOOK_PATH, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(a.name == "backend" or a.name.endswith(".backend") for a in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse(node.module == "backend" or (node.module or "").endswith(".backend"))
                self.assertFalse(any(a.name == "backend" for a in node.names))

    def test_backend_is_not_loaded_after_recall_record_and_compact(self):
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAIYA_")}
        env.update(RAIYA_CONFIG="", RAIYA_STATE_DIR=os.path.join(root, "state"),
                   PYTHONPATH=REPO_ROOT + os.pathsep + env.get("PYTHONPATH", ""))
        code = (
            "import io, runpy, sys\n"
            "sys.argv = ['raiya-hook', sys.argv[1]]\n"
            "class _In:\n"
            "    buffer = io.BytesIO(open(%r, 'rb').read())\n"
            "sys.stdin = _In()\n"
            "try:\n"
            "    runpy.run_module('raiya.hooks.raiya_hook', run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    print('exit', e.code, file=sys.stderr)\n"
            "print('BACKEND_LOADED' if 'raiya.backend' in sys.modules else 'BACKEND_ABSENT')\n"
        )
        # compact は同じ state_dir に 3 往復を記録した後の ok case (窓が空だと rows_by_ids / 発芽の前に返るため)
        for verb, name in (("recall", "recall__normal"), ("record", "record__normal"), ("record", "record__paste"),
                           ("record", "record__tool_refs"), ("compact", "compact__ok")):
            with self.subTest(verb=verb, name=name):
                path = os.path.join(FIXTURE_DIR, name + ".stdin.json")
                result = subprocess.run([sys.executable, "-c", code % path, verb], capture_output=True, text=True,
                                        env=env, cwd=root, timeout=60)
                self.assertEqual(result.stderr.strip(), "exit 0", msg=result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(json.loads(lines[0])["outcome"], "ok")
                self.assertEqual(lines[1], "BACKEND_ABSENT")


class TestEngineToolRefs(unittest.TestCase):
    def test_record_turn_writes_tool_refs_only_when_given(self):
        from raiya.engine import MemoryEngine
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        engine = MemoryEngine(state_dir=root, new_life=False)
        engine.record_turn("hello", "answer")
        engine.record_turn("hello", "answer", tool_refs=[])
        engine.record_turn("hello", "answer", tool_refs=[{"kind": "command", "name": "ls"}])
        engine.record_turn("hello", "answer", tool_refs="not a list")
        rows = engine.ledger.rows_by_ids([1, 2, 3, 4])
        got = [("tool_refs" in rows[i], rows[i].get("tool_refs")) for i in (1, 2, 3, 4)]
        self.assertEqual(got, [(False, None), (True, []), (True, [{"kind": "command", "name": "ls"}]),
                               (False, None)])


if __name__ == "__main__":
    unittest.main()
