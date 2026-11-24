"""debug.log (raiya/debuglog.py) と、その周辺の配線の決定論 pin。
spec: docs/superpowers/specs/2026-10-23-debug-log-design.md (§9 が §1〜§7 より優先)。

hooks の別プロセス E2E は tests/test_hooks_e2e.py の TestDebugLogE2E、backend.describe の一致は
tests/test_backend.py、backend 不 import の AST pin は tests/test_mcp_server.py にある。LLM は使わない。
"""
import contextlib
import io
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

from raiya import debuglog, inspect_cli  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.hooks import claude_code as hooks  # noqa: E402


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.log = os.path.join(self.state, "debug.log")

    def lines(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]


class TestLevel(_TmpCase):
    def _flag(self, content):
        with open(os.path.join(self.state, "debug"), "wb") as f:
            f.write(content)

    def test_absent_is_off(self):
        self.assertEqual(debuglog.level(self.state), 0)
        self.assertEqual(debuglog.level(os.path.join(self.root, "no-such-dir")), 0)

    def test_flag_contents(self):
        for content, expected in ((b"", 1), (b"1", 1), (b"2", 2), (b"2\n", 2), (b"junk", 1), (b"\xff\xfe", 1), (b"12", 1)):
            self._flag(content)
            self.assertEqual(debuglog.level(self.state), expected, msg=content)

    def test_directory_is_off(self):
        os.makedirs(os.path.join(self.state, "debug"))
        self.assertEqual(debuglog.level(self.state), 0)


class TestEmit(_TmpCase):
    def test_level0_and_bad_level_write_nothing(self):
        debuglog.emit(self.state, 0, "x")
        debuglog.emit(self.state, 3, "x")
        self.assertFalse(os.path.exists(self.log))

    def test_missing_state_dir_is_not_created(self):
        missing = os.path.join(self.root, "missing")
        debuglog.emit(missing, 2, "x", a=1)
        self.assertFalse(os.path.exists(missing))

    def test_never_raises(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("boom")

            __repr__ = __str__

        debuglog.emit(self.state, 2, "x", obj=Boom())
        debuglog.emit(None, 2, "x")
        debuglog.emit(self.state, 2, "x", lone="\ud800 surrogate")
        os.makedirs(self.log + "dir")
        self.assertEqual(debuglog.read_events(os.path.join(self.root, "none")), [])

    def test_new_file_mode_0600_and_existing_mode_kept(self):
        debuglog.emit(self.state, 1, "e")
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600)
        os.chmod(self.log, 0o640)
        debuglog.emit(self.state, 1, "e")
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o640)

    def test_common_keys_and_jsonl(self):
        debuglog.emit(self.state, 2, "hook_end", sub="stop", text="本文")
        (row,) = self.lines()
        self.assertEqual((row["level"], row["event"], row["sub"], row["text"]), (2, "hook_end", "stop", "本文"))
        self.assertEqual(row["pid"], os.getpid())
        self.assertIn("ts", row)

    def test_level1_long_strings_become_len_and_line_stays_under_4k(self):
        debuglog.emit(self.state, 1, "e", short="ok", long="x" * 300, nested=["y" * 999, "z"], **{"k%d" % i: "w" * 255 for i in range(40)})
        (row,) = self.lines()
        self.assertEqual(row["short"], "ok")
        self.assertEqual(row["long"], {"len": 300})
        self.assertEqual(row["nested"], [{"len": 999}, "z"])
        self.assertTrue(row["line_capped"])
        with open(self.log, "rb") as f:
            self.assertLessEqual(len(f.read()), 4096)
        self.assertEqual(row["event"], "e")

    def test_level2_long_body_elided_with_original_length(self):
        body = "あ" * 70000
        debuglog.emit(self.state, 2, "e", prompt=body, small="s")
        (row,) = self.lines()
        self.assertEqual(row["prompt_len"], 70000)
        self.assertIn("<elided 4464 chars>", row["prompt"])
        self.assertLess(len(row["prompt"]), 65600)
        self.assertNotIn("small_len", row)

    def test_line_cap_1mib_replaces_biggest_fields(self):
        # 64 KiB の本文を 20 個: 各々は elide されても合計は 1 MiB を超える。
        fields = {"f%d" % i: "い" * 65536 for i in range(20)}
        debuglog.emit(self.state, 2, "e", keep="small", **fields)
        with open(self.log, "rb") as f:
            raw = f.read()
        self.assertLessEqual(len(raw), 1024 * 1024)
        row = json.loads(raw)
        self.assertTrue(row["line_capped"])
        self.assertEqual(row["keep"], "small")
        self.assertTrue(any(isinstance(v, dict) and "len" in v for v in row.values()))

    def test_rotation_level1_and_level2_thresholds(self):
        for lvl, size in ((1, 1024 * 1024 + 1), (2, 16 * 1024 * 1024 + 1)):
            debuglog.emit(self.state, lvl, "first")
            with open(self.log, "ab") as f:
                f.write(b"\n" * (size - os.path.getsize(self.log)))
            debuglog.emit(self.state, lvl, "after")
            self.assertTrue(os.path.exists(self.log + ".1"), msg=lvl)
            self.assertEqual([r["event"] for r in self.lines()], ["after"])
            events = [e["event"] for e in debuglog.read_events(self.state)]
            self.assertEqual(events, ["first", "after"])
            os.remove(self.log)
            os.remove(self.log + ".1")

    def test_no_rotation_below_threshold(self):
        debuglog.emit(self.state, 1, "a")
        with open(self.log, "ab") as f:
            f.write(b"\n" * (1024 * 1024 - os.path.getsize(self.log)))
        debuglog.emit(self.state, 1, "b")
        self.assertFalse(os.path.exists(self.log + ".1"))

    def test_read_events_skips_broken_lines(self):
        debuglog.emit(self.state, 1, "a")
        with open(self.log, "ab") as f:
            f.write(b'{"event": "torn\n\xff\xfe garbage\n[1, 2]\n')
        debuglog.emit(self.state, 1, "b")
        self.assertEqual([e["event"] for e in debuglog.read_events(self.state)], ["a", "b"])

    def test_concurrent_processes_keep_whole_lines(self):
        code = (
            "import sys\n"
            "from raiya import debuglog\n"
            "debuglog.emit(sys.argv[1], 2, 'big', who=sys.argv[2], body='う' * 60000)\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code, self.state, str(i)], cwd=REPO_ROOT,
                                  env=dict(os.environ, PYTHONPATH=REPO_ROOT)) for i in range(8)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        events = debuglog.read_events(self.state)
        self.assertEqual(sorted(e["who"] for e in events), [str(i) for i in range(8)])
        self.assertTrue(all(len(e["body"]) == 60000 for e in events))


class TestDoesNotLoadBackend(_TmpCase):
    def test_emit_at_level2_does_not_import_backend(self):
        code = (
            "import sys\n"
            "from raiya import debuglog\n"
            "debuglog.emit(sys.argv[1], 2, 'e', a='b')\n"
            "debuglog.level(sys.argv[1]); debuglog.read_events(sys.argv[1])\n"
            "print('raiya.backend' in sys.modules)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, self.state], capture_output=True, text=True,
                                cwd=REPO_ROOT, env=dict(os.environ, PYTHONPATH=REPO_ROOT), timeout=30)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class TestEngineConsolidateDebug(_TmpCase):
    def setUp(self):
        super().setUp()
        self.saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved)))
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "cat > /dev/null; echo '[]'"
        self.engine = MemoryEngine(state_dir=self.state, new_life=False)

    def _record(self, n=2):
        for i in range(n):
            self.engine.record_turn("認証の方針を相談したい{}".format(i), "了解", "s{}".format(i % 2))

    def test_filled_between_locks_on_success(self):
        self._record(2)
        d = {}
        result = self.engine.consolidate(debug=d)
        self.assertEqual(d["window_ids"], result["window"])
        self.assertEqual((d["window_n"], d["window_sessions"]), (2, 2))
        self.assertEqual((d["backend"], d["model"]), ("command", None))
        self.assertEqual(d["prompt_len"], len(d["prompt"]))
        self.assertEqual(d["raw"], "[]")
        self.assertEqual(d["raw_len"], 2)
        self.assertIsInstance(d["llm_ms"], int)

    def test_filled_even_when_call_llm_raises(self):
        self._record(1)
        os.environ["RAIYA_LLM_CMD"] = "cat > /dev/null; exit 1"
        d = {}
        with self.assertRaises(Exception):
            self.engine.consolidate(debug=d)
        self.assertIsNone(d["raw"])
        self.assertIsNone(d["raw_len"])
        self.assertIn("prompt", d)
        self.assertIsInstance(d["llm_ms"], int)

    def test_non_dict_debug_is_ignored(self):
        self._record(1)
        for bad in (lambda **k: 1, "x", [], 5):
            self.engine.record_turn("認証の方針を相談したい", "了解", "s")
            self.engine.consolidate(debug=bad)

    def test_empty_window_sets_only_window_fields_and_skips_backend_import(self):
        code = (
            "import sys, tempfile\n"
            "from raiya.engine import MemoryEngine\n"
            "e = MemoryEngine(state_dir=tempfile.mkdtemp(), new_life=False)\n"
            "d = {}\n"
            "r = e.consolidate(debug=d)\n"
            "print(r['skipped'], sorted(d), 'raiya.backend' in sys.modules)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT,
                                env=dict(os.environ, PYTHONPATH=REPO_ROOT), timeout=30)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "empty ['window_ids', 'window_n', 'window_sessions'] False")


class TestHooksDirectCalls(_TmpCase):
    def test_cmd_functions_work_with_two_args(self):
        from unittest import mock
        from raiya import backend

        payload = {"session_id": "s1", "prompt": "認証の方針を相談したい", "last_assistant_message": "了解"}
        # session_end は consolidate で call_llm を呼ぶ。実 LLM に届かせない (鍵を外し、call_llm を差し替える)。
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RAIYA_OPENAI_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(backend, "call_llm", return_value="[]"):
            self.assertEqual(hooks.cmd_recall(payload, self.state), "ok")
            self.assertEqual(hooks.cmd_stop(payload, self.state), "ok")
            self.assertEqual(hooks.cmd_session_end(payload, self.state), "ok")
        self.assertFalse(os.path.exists(self.log))  # debug_level=0 は何も書かない

    def test_debug_level_writes_stop_event(self):
        payload = {"session_id": "s1", "prompt": "認証の方針を相談したい", "last_assistant_message": "了解"}
        hooks.cmd_recall(payload, self.state)
        hooks.cmd_stop(payload, self.state, debug_level=1)
        self.assertEqual([r["event"] for r in self.lines()], ["stop"])

    def test_consolidate_failure_events_l1_vs_l2(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "cat > /dev/null; echo SENTINEL_BODY >&2; exit 1"
        engine = MemoryEngine(state_dir=self.state, new_life=False)
        engine.record_turn("認証の方針を相談したい", "了解", "s1")
        hooks._run_consolidation(engine, self.state, "s1", 1)
        hooks._run_consolidation(engine, self.state, "s1", 2)
        l1, l2 = self.lines()
        self.assertEqual(l1["exc"], "BackendError")
        self.assertIn("detail_len=", l1["exc_summary"])
        self.assertNotIn("error", l1)
        self.assertIn("SENTINEL_BODY", l2["error"])
        with open(self.log, encoding="utf-8") as f:
            self.assertNotIn("SENTINEL_BODY", f.readline())


class TestInspectDebug(_TmpCase):
    def run_cli(self, *argv, state=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = inspect_cli.main(["--state-dir", state or self.state] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def _before(self):
        return sorted(os.listdir(self.state))

    def test_no_alaya_json_needed_and_creates_nothing(self):
        before = self._before()
        code, out, _ = self.run_cli("debug")
        self.assertEqual(code, 0)
        self.assertIn("デバッグログなし", out)
        self.assertEqual(self._before(), before)  # .lock も作らない
        self.assertEqual(self._before(), [])

    def test_missing_state_dir_exits_2_without_creating(self):
        missing = os.path.join(self.root, "nope")
        code, _, err = self.run_cli("debug", state=missing)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(missing))

    def test_newest_first_filters_and_broken_lines(self):
        debuglog.emit(self.state, 1, "recall", sub="recall", session_id="a", n=1)
        debuglog.emit(self.state, 1, "stop", sub="stop", session_id="b", n=2)
        with open(self.log, "ab") as f:
            f.write(b"{broken\n")
        debuglog.emit(self.state, 1, "hook_end", sub="stop", session_id="a", outcome="ok", dur_ms=5)
        code, out, _ = self.run_cli("debug")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("hook_end", lines[0])
        self.assertIn("outcome=ok", lines[0])
        self.assertIn("recall", lines[2])
        _, out, _ = self.run_cli("debug", "--last", "1")
        self.assertEqual(len(out.strip().splitlines()), 1)
        _, out, _ = self.run_cli("debug", "--session", "b")
        self.assertEqual(len(out.strip().splitlines()), 1)
        _, out, _ = self.run_cli("debug", "--event", "recall")
        self.assertEqual(len(out.strip().splitlines()), 1)
        code, _, _ = self.run_cli("debug", "--last", "0")
        self.assertEqual(code, 2)

    def test_json_array_and_raw_escape(self):
        debuglog.emit(self.state, 2, "stop", text="a\x1b[31mb​c\x7f")
        code, out, _ = self.run_cli("--json", "debug")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["text"], "a\x1b[31mb​c\x7f")
        for argv in (("debug",), ("debug", "--raw")):
            _, out, _ = self.run_cli(*argv)
            self.assertNotIn("\x1b", out)
            self.assertNotIn("​", out)
            self.assertNotIn("\x7f", out)
            self.assertTrue("\\x1b" in out or "\\u001b" in out)  # --raw は JSON が \u001b にする
            self.assertIn("\\u200b", out)

    def test_reads_rotated_generation_too(self):
        debuglog.emit(self.state, 1, "old")
        os.replace(self.log, self.log + ".1")
        debuglog.emit(self.state, 1, "new")
        _, out, _ = self.run_cli("--json", "debug")
        self.assertEqual([e["event"] for e in json.loads(out)], ["new", "old"])


class TestLevel0NeverTouchesDebugPath(_TmpCase):
    """フラグ無し (レベル 0) では emit 系も debug 用の計算も呼ばれない。"""

    def setUp(self):
        super().setUp()
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        os.environ.pop("RAIYA_HOOKS", None)
        os.environ["RAIYA_STATE_DIR"] = self.state
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "cat > /dev/null; echo '[]'"

    def _forbid(self):
        from unittest import mock
        boom = mock.Mock(side_effect=AssertionError("debug path touched at level 0"))
        patches = [mock.patch.object(hooks.debuglog, "emit", boom)]
        for name in ("_emit_stop", "_emit_recall", "_emit_consolidate"):
            patches.append(mock.patch.object(hooks, name, boom))
        from raiya.hooks import core
        patches.append(mock.patch.object(core, "_emit_consolidate", boom))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return boom

    def test_level0_never_touches_debug_path(self):
        boom = self._forbid()
        payload = {"session_id": "s1", "prompt": "認証の方針を相談したい", "last_assistant_message": "了解"}
        self.assertEqual(hooks.cmd_recall(payload, self.state), "ok")
        for _ in range(hooks.CONSOLIDATE_EVERY):
            self.assertEqual(hooks.cmd_stop(payload, self.state), "ok")  # 最後の 1 回で統合も走る
        engine = MemoryEngine(state_dir=self.state, new_life=False)
        self.assertEqual(engine.audit()["ledger"]["consolidated_upto"], hooks.CONSOLIDATE_EVERY)
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "s1.json")))

        payload2 = dict(payload, session_id="s2")
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload2, ensure_ascii=False))
        self.addCleanup(setattr, sys, "stdin", old_stdin)
        hooks._main_impl(["recall"])
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "s2.json")))
        boom.assert_not_called()
        self.assertFalse(os.path.exists(self.log))

    def test_skippable_recall_resolves_project_zero_times(self):
        from unittest import mock
        with mock.patch.object(hooks, "resolve_project", side_effect=AssertionError("resolve_project at level 0")) as rp:
            out = hooks.cmd_recall({"session_id": "s1", "prompt": "/slash", "cwd": self.root}, self.state)
        self.assertEqual(out, "skip:skippable_prompt")
        rp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
