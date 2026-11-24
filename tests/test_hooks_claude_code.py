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
import urllib.error
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from raiya import backend, engine, sansho  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import alaya_state, clear_state, ledger_rows, table_bodies  # noqa: E402
from raiya.hooks import claude_code as hooks  # noqa: E402


class _HooksTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_environ = dict(os.environ)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def _log_text(self):
        with open(os.path.join(self.tmpdir, "hooks.log"), "r", encoding="utf-8") as f:
            return f.read()

    def _turn(self, session_id, user, assistant):
        """1 往復 (recall -> stop) を回す。stop は台帳へ追記するだけで LLM は呼ばない。"""
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": user, "session_id": session_id}, self.tmpdir)
        hooks.cmd_stop({"session_id": session_id, "last_assistant_message": assistant}, self.tmpdir)

    def _fill_window(self, session_id, user, assistant):
        """統合が走るところ (CONSOLIDATE_EVERY 往復) まで台帳を埋める。最後の 1 往復の
        stop で `_run_consolidation` = 唯一の LLM 呼び出しが起きる。"""
        for _ in range(hooks.CONSOLIDATE_EVERY):
            self._turn(session_id, user, assistant)


class TestGuards(_HooksTestCase):
    """常に exit 0・RAIYA_HOOK_ACTIVE/RAIYA_HOOKS=0/不正stdin/未知サブコマンドの skip。"""

    def _run_main(self, argv, stdin_text, env_extra=None):
        env = dict(os.environ)
        env["RAIYA_STATE_DIR"] = self.tmpdir
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(
            [sys.executable, "-m", "raiya.hooks.claude_code"] + argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )
        return result

    def test_hook_active_recursion_guard_skips_recall_and_creates_no_files(self):
        result = self._run_main(["recall"], '{"prompt": "猫が好き"}', {"RAIYA_HOOK_ACTIVE": "1"})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_hook_active_recursion_guard_skips_stop(self):
        result = self._run_main(
            ["stop"],
            '{"session_id": "s1", "last_assistant_message": "hi"}',
            {"RAIYA_HOOK_ACTIVE": "1"},
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_raiya_hooks_zero_disables_recall(self):
        result = self._run_main(["recall"], '{"prompt": "猫が好き"}', {"RAIYA_HOOKS": "0"})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_malformed_stdin_exits_zero_and_logs(self):
        result = self._run_main(["recall"], "not valid json at all")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        log_path = os.path.join(self.tmpdir, "hooks.log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as f:
            self.assertIn("stdin 解析失敗", f.read())

    def test_unknown_subcommand_exits_zero_without_reading_stdin(self):
        result = self._run_main(["unknown-subcommand"], '{"prompt": "猫が好き"}')
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_no_subcommand_exits_zero(self):
        result = self._run_main([], "{}")
        self.assertEqual(result.returncode, 0, msg=result.stderr)


class TestSessionCache(_HooksTestCase):
    def test_write_then_read_roundtrip(self):
        hooks._write_session_cache(self.tmpdir, "sess-1", "こんにちは")
        self.assertEqual(hooks._read_session_cache_prompt(self.tmpdir, "sess-1"), "こんにちは")

    def test_missing_session_returns_none_for_fallback(self):
        self.assertIsNone(hooks._read_session_cache_prompt(self.tmpdir, "no-such-session"))

    def test_writes_even_for_short_or_slash_prefixed_prompts(self):
        # skip 判定 (出力/熏習に使うか) とキャッシュ書込は独立。短い/スラッシュでも書く。
        hooks._write_session_cache(self.tmpdir, "sess-2", "/help")
        self.assertEqual(hooks._read_session_cache_prompt(self.tmpdir, "sess-2"), "/help")
        hooks._write_session_cache(self.tmpdir, "sess-3", "a")
        self.assertEqual(hooks._read_session_cache_prompt(self.tmpdir, "sess-3"), "a")

    def test_path_traversal_session_id_is_rejected(self):
        hooks._write_session_cache(self.tmpdir, "../../etc/evil", "x")
        self.assertEqual(os.listdir(hooks._sessions_dir(self.tmpdir)) if os.path.exists(hooks._sessions_dir(self.tmpdir)) else [], [])
        self.assertIsNone(hooks._read_session_cache_prompt(self.tmpdir, "../../etc/evil"))

    def test_prune_keeps_only_the_most_recent_n(self):
        sdir = hooks._sessions_dir(self.tmpdir)
        os.makedirs(sdir, exist_ok=True)
        for i in range(hooks.SESSIONS_MAX_KEEP + 5):
            path = os.path.join(sdir, "s{}.json".format(i))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"prompt": "p{}".format(i), "ts": i}, f)
            os.utime(path, (i, i))
        hooks._prune_sessions(sdir)
        remaining = [n for n in os.listdir(sdir) if n.endswith(".json")]
        self.assertEqual(len(remaining), hooks.SESSIONS_MAX_KEEP)
        # 一番古い (s0) は消え、一番新しい (最後の番号) は残る。
        self.assertNotIn("s0.json", remaining)
        self.assertIn("s{}.json".format(hooks.SESSIONS_MAX_KEEP + 4), remaining)


class TestTranscriptExtraction(unittest.TestCase):
    """合成 transcript fixture (実物の内容は写さず、~/.claude/projects/*/*.jsonl で
    確認した構造だけを模す): user 文字列 content 型・user text ブロック型・
    tool_result 混在・assistant の tool_use+text 混在。"""

    def test_user_string_content_type(self):
        lines = [{"type": "user", "message": {"role": "user", "content": "こんにちは"}}]
        self.assertEqual(hooks._find_last_user_text(lines), "こんにちは")

    def test_user_text_block_list_content_type(self):
        lines = [
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "元気ですか"}]},
            }
        ]
        self.assertEqual(hooks._find_last_user_text(lines), "元気ですか")

    def test_tool_result_only_user_entry_is_not_a_real_user_turn(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "最初の発話"}},
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"tool_use_id": "t1", "type": "tool_result", "content": "結果", "is_error": False}
                    ],
                },
            },
        ]
        # tool_result のみのエントリはスキップされ、その前の実発話が採用される。
        self.assertEqual(hooks._find_last_user_text(lines), "最初の発話")

    def test_assistant_with_mixed_tool_use_and_text_blocks_concatenates_only_text(self):
        lines = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
                        {"type": "text", "text": "実行しました。"},
                    ],
                },
            }
        ]
        self.assertEqual(hooks._find_last_assistant_text(lines), "実行しました。")

    def test_assistant_text_block_with_non_string_text_is_dropped(self):
        # user 側と同じ fail-closed: text が str でないブロックは連結に混ぜない。
        # 素通しすると `"".join` が TypeError になり、fail-soft の catch-all まで飛んで
        # 往復ごと取りこぼす (transcript の中身は外部入力なので型を信じない)。
        lines = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": 999}, {"type": "text", "text": "いいですね"}],
                },
            }
        ]
        self.assertEqual(hooks._find_last_assistant_text(lines), "いいですね")

    def test_docs_style_user_message_and_assistant_message_type_names_are_accepted(self):
        user_lines = [{"type": "user_message", "message": {"role": "user", "content": "テスト"}}]
        self.assertEqual(hooks._find_last_user_text(user_lines), "テスト")
        assistant_lines = [
            {"type": "assistant_message", "message": {"role": "assistant", "content": [{"type": "text", "text": "応答"}]}}
        ]
        self.assertEqual(hooks._find_last_assistant_text(assistant_lines), "応答")

    def test_slash_prefixed_user_turn_is_skipped_in_favor_of_earlier_real_turn(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "実際の質問です"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "回答"}]}},
            {"type": "user", "message": {"role": "user", "content": "/clear"}},
        ]
        self.assertEqual(hooks._find_last_user_text(lines), "実際の質問です")

    def test_empty_transcript_returns_empty_string(self):
        self.assertEqual(hooks._find_last_user_text([]), "")
        self.assertEqual(hooks._find_last_assistant_text([]), "")

    def test_thinking_block_is_excluded_from_assistant_text(self):
        # LOW-1 (characterization pin): 実物の transcript は assistant メッセージに
        # {"type": "thinking", "thinking": "...", "signature": "..."} を含みうる
        # (拡張思考)。thinking 本文が誤って熏習対象の assistant_text に混入しないこと。
        lines = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "内部の思考過程です。ユーザーには見せない。",
                            "signature": "sig-abc123",
                        },
                        {"type": "text", "text": "こちらが実際の応答です。"},
                    ],
                },
            }
        ]
        result = hooks._find_last_assistant_text(lines)
        self.assertEqual(result, "こちらが実際の応答です。")
        self.assertNotIn("内部の思考過程", result)
        self.assertNotIn("thinking", result)

    def test_user_text_block_with_non_string_text_is_dropped(self):
        # 壊れた (text が str でない) ブロックは実発話ではないので落とす。素通しすると
        # harness 判定の .lstrip で AttributeError になり、fail-soft の catch-all まで
        # 飛んで往復ごと取りこぼす (text ブロックの中身は外部入力なので型を信じない)。
        content = [{"type": "text", "text": 12345}, {"type": "text", "text": "本当の質問"}]
        self.assertEqual(hooks._extract_user_text_from_content(content), "本当の質問")

    def test_is_real_user_entry_tool_result_only_is_false(self):
        # LOW-2: _is_real_user_entry を直接呼ぶ単体 pin。
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"tool_use_id": "t1", "type": "tool_result", "content": "結果", "is_error": False}
                ],
            },
        }
        self.assertFalse(hooks._is_real_user_entry(entry))

    def test_is_real_user_entry_with_text_is_true(self):
        entry = {"type": "user", "message": {"role": "user", "content": "こんにちは"}}
        self.assertTrue(hooks._is_real_user_entry(entry))
        entry_block = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "こんにちは"}]},
        }
        self.assertTrue(hooks._is_real_user_entry(entry_block))


class TestSkipLogic(unittest.TestCase):
    def test_empty_and_whitespace_only_are_skippable(self):
        self.assertTrue(hooks._is_skippable_prompt(""))
        self.assertTrue(hooks._is_skippable_prompt("   "))
        self.assertTrue(hooks._is_skippable_prompt(None))

    def test_slash_prefixed_is_skippable(self):
        self.assertTrue(hooks._is_skippable_prompt("/help"))

    def test_too_short_is_skippable(self):
        self.assertTrue(hooks._is_skippable_prompt("a"))

    def test_normal_prompt_is_not_skippable(self):
        self.assertFalse(hooks._is_skippable_prompt("猫が好きです"))


class TestTruncate(unittest.TestCase):
    def test_short_text_is_not_truncated(self):
        text, truncated = hooks._truncate("短い文", 8000)
        self.assertEqual(text, "短い文")
        self.assertFalse(truncated)

    def test_long_text_keeps_head_and_tail_with_elision_marker_and_is_flagged(self):
        # 処方 8: 先頭だけでなく末尾も残し、間に省略の印 (省いた字数) を挟む。
        limit = hooks.TRUNCATE_LIMIT
        long_text = "前" * limit + "中" * 1000 + "後" * limit
        text, truncated = hooks._truncate(long_text, limit)
        self.assertTrue(truncated)
        self.assertEqual(
            text,
            "前" * (limit // 2) + "\n<elided {} chars>\n".format(len(long_text) - limit) + "後" * (limit - limit // 2),
        )
        self.assertNotIn("中", text)

    def test_text_exactly_at_limit_is_not_truncated(self):
        text, truncated = hooks._truncate("あ" * hooks.TRUNCATE_LIMIT, hooks.TRUNCATE_LIMIT)
        self.assertEqual(text, "あ" * hooks.TRUNCATE_LIMIT)
        self.assertFalse(truncated)


class TestCmdRecall(_HooksTestCase):
    def test_no_germinated_seeds_produces_no_output(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.cmd_recall({"prompt": "まだ何も熏習していない話題です"}, self.tmpdir)
        self.assertEqual(buf.getvalue(), "")

    def test_germinated_seed_produces_hook_specific_output_json(self):
        from raiya.engine import MemoryEngine
        from raiya.sansho import Turn

        engine = MemoryEngine(state_dir=self.tmpdir)
        turn = Turn(user_text="猫が好きです/", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "猫が好きです/"},
        }
        engine.imprint(turn, [proposal])
        engine.imprint(turn, [proposal])  # strength 2 で発芽条件クリア

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.cmd_recall({"prompt": "猫の話をしたい", "session_id": "s1"}, self.tmpdir)
        output = json.loads(buf.getvalue())
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )
        self.assertIn("猫が好き", output["hookSpecificOutput"]["additionalContext"])

    def test_skippable_prompt_still_caches_but_produces_no_output(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.cmd_recall({"prompt": "/help", "session_id": "s2"}, self.tmpdir)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(hooks._read_session_cache_prompt(self.tmpdir, "s2"), "/help")

    def test_user_input_key_is_also_accepted(self):
        hooks.cmd_recall({"user_input": "こんにちは", "session_id": "s3"}, self.tmpdir)
        self.assertEqual(hooks._read_session_cache_prompt(self.tmpdir, "s3"), "こんにちは")

    def test_non_string_prompt_is_coerced_and_does_not_break_the_session(self):
        # LOW-3: prompt が非文字列 (例: 数値) でも fail-soft が破れず、キャッシュには
        # 文字列として残る。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.cmd_recall({"session_id": "X", "prompt": 12345}, self.tmpdir)
        hooks.cmd_stop({"session_id": "X", "last_assistant_message": "ok"}, self.tmpdir)

        log_path = os.path.join(self.tmpdir, "hooks.log")
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                log_text = f.read()
            self.assertNotIn("失敗", log_text)

        cached = hooks._read_session_cache_prompt(self.tmpdir, "X")
        self.assertIsInstance(cached, str)


class TestCmdStop(_HooksTestCase):
    """Stop = 台帳追記 (決定論)。LLM が動くのは CONSOLIDATE_EVERY 往復ごとの統合だけなので、
    end-to-end 系はいずれも `_fill_window` で窓を満たしてから検分する (改修4 §3/§7)。"""

    def _write_dummy_llm(self, proposals_json):
        path = os.path.join(self.tmpdir, "dummy_llm.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ncat > /dev/null\necho '{}'\n".format(proposals_json))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def _last_ledger_row(self):
        """台帳の最後の 1 行。stop が「どの本文を採ったか」の検分先 (旧: propose の
        プロンプト spy)。統合はこの行をそのまま読むので、境界はここへ移った。"""
        return MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]

    def test_stop_hook_active_true_skips_entirely(self):
        hooks.cmd_stop({"stop_hook_active": True, "session_id": "s1"}, self.tmpdir)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_no_user_text_available_skips_and_logs(self):
        hooks.cmd_stop({"session_id": "no-such-session", "last_assistant_message": "hi"}, self.tmpdir)
        log_path = os.path.join(self.tmpdir, "hooks.log")
        with open(log_path, encoding="utf-8") as f:
            self.assertIn("skip", f.read())

    def test_end_to_end_grounded_proposal_lands_in_seeds(self):
        script = self._write_dummy_llm(
            '[{"content":"猫が好き","conditions":["猫"],"polarity":"善",'
            '"evidence":{"source":"user","span":"私は猫が好きです/"}}]'
        )
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        self._fill_window("s2", "私は猫が好きです/", "いいですね")
        alaya = alaya_state(self.tmpdir)
        self.assertEqual(len(alaya["seeds"]), 1)
        self.assertEqual(alaya["seeds"][0]["sansho"], "圓成實")

    def test_end_to_end_non_grounded_proposal_lands_in_quarantine(self):
        script = self._write_dummy_llm(
            '[{"content":"捏造された記憶","conditions":["猫"],"polarity":"無記",'
            '"evidence":{"source":"user","span":"完全に捏造されたspanです"}}]'
        )
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        self._fill_window("s3", "猫が好きです/", "そうですか")
        alaya = alaya_state(self.tmpdir)
        self.assertEqual(alaya["seeds"], [])
        self.assertEqual(len(alaya["quarantine"]), 1)
        self.assertEqual(alaya["quarantine"][0]["sansho"], "遍計所執")

    def test_last_assistant_message_is_preferred_over_transcript_in_the_ledger(self):
        transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "transcriptの方の応答"}]},
            }) + "\n")
        hooks._write_session_cache(self.tmpdir, "s4", "テスト発話です")
        hooks.cmd_stop(
            {
                "session_id": "s4",
                "last_assistant_message": "stdinの方の応答",
                "transcript_path": transcript_path,
            },
            self.tmpdir,
        )
        self.assertEqual(self._last_ledger_row()["assistant_text"], "stdinの方の応答")

    def test_transcript_fallback_used_in_the_ledger_when_last_assistant_message_missing(self):
        transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "transcript由来の応答"}]},
            }) + "\n")
        hooks._write_session_cache(self.tmpdir, "s5", "テスト発話です")
        hooks.cmd_stop({"session_id": "s5", "transcript_path": transcript_path}, self.tmpdir)
        self.assertEqual(self._last_ledger_row()["assistant_text"], "transcript由来の応答")

    def test_session_cache_user_text_preferred_over_transcript_in_the_ledger(self):
        transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "transcript由来のユーザー発話"},
            }) + "\n")
        hooks._write_session_cache(self.tmpdir, "s6", "session cache由来のユーザー発話")
        hooks.cmd_stop(
            {
                "session_id": "s6",
                "last_assistant_message": "ok",
                "transcript_path": transcript_path,
            },
            self.tmpdir,
        )
        self.assertEqual(self._last_ledger_row()["user_text"], "session cache由来のユーザー発話")

    def test_transcript_fallback_walks_past_a_harness_injected_user_entry(self):
        """session cache が無く transcript へ fallback する経路でも、harness 注入の
        エントリは採らずに一つ前の実発話まで戻ること (改修4 §8)。注入だけの
        エントリを採ってしまうと、その往復が丸ごと skip されて実発話も落ちる。"""
        transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            for entry in (
                {"type": "user", "message": {"role": "user", "content": "実際の質問です"}},
                {"type": "assistant",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "回答"}]}},
                {"type": "user",
                 "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}},
            ):
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        hooks.cmd_stop({"session_id": "s-fb", "transcript_path": transcript_path}, self.tmpdir)
        self.assertEqual(self._last_ledger_row()["user_text"], "実際の質問です")
        ledger_text = "\n".join(table_bodies(self.tmpdir, "ledger"))
        self.assertNotIn("system-reminder", ledger_text)
        self.assertNotIn("system-reminder", self._log_text())

    def test_raw_skippable_user_text_via_session_cache_is_skipped_and_logged(self):
        """session cache 経由でも生の user_text が skippable (スラッシュ) なら台帳へ載せず、
        早期判定の hooks.log 行を残す (この判定は _cmd_stop に一つだけある)。"""
        hooks._write_session_cache(self.tmpdir, "s-slash", "/clear")
        outcome = hooks.cmd_stop({"session_id": "s-slash", "last_assistant_message": "回答"}, self.tmpdir)
        self.assertEqual(outcome, "skip:skippable_user")
        self.assertIn("session=s-slash skip: user_text が空/短い/スラッシュ/harness 注入", self._log_text())
        self.assertEqual(MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1), [])

    def test_leading_harness_tag_in_transcript_fallback_is_skipped_on_the_raw_text(self):
        """生の先頭が harness タグ + 実本文の user エントリ (transcript fallback)。タグを
        除くと実本文が残るが、生で判定するので往復ごと skip される (生の判定が残る理由)。"""
        transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "<system-reminder>注入</system-reminder>本当の質問です"},
            }, ensure_ascii=False) + "\n")
        outcome = hooks.cmd_stop({"session_id": "s-tag", "transcript_path": transcript_path,
                                  "last_assistant_message": "回答"}, self.tmpdir)
        self.assertEqual(outcome, "skip:skippable_user")
        self.assertIn("session=s-tag skip: user_text が空/短い/スラッシュ/harness 注入", self._log_text())
        self.assertEqual(MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1), [])

    def test_transcript_user_entry_with_appended_harness_block_keeps_only_user_text(self):
        """実発話の後ろに注入ブロックが足された user エントリ: 連結すると先頭は
        ユーザー本文なので `_is_harness_injected` をすり抜ける。text ブロックごとに
        落とさなければ注入本文がそのまま台帳へ載る (統合で圓成實に接地しうる)。"""
        transcript_path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": "本当の質問"},
                    {"type": "text", "text": "<system-reminder>SECRET</system-reminder>"},
                ]},
            }, ensure_ascii=False) + "\n")
        # 応答が空の往復は記帳しない (処方 2) ので、応答は stdin から与える。
        hooks.cmd_stop({"session_id": "s-block", "transcript_path": transcript_path,
                        "last_assistant_message": "回答"}, self.tmpdir)
        self.assertEqual(self._last_ledger_row()["user_text"], "本当の質問")
        ledger_text = "\n".join(table_bodies(self.tmpdir, "ledger"))
        self.assertNotIn("SECRET", ledger_text)
        self.assertNotIn("SECRET", self._log_text())

    def test_stop_survives_a_non_string_cached_prompt(self):
        """壊れた/手書きのキャッシュで prompt が非 str でも fail-soft を破らないこと。
        素通しすると `_is_skippable_prompt` の `int.strip()` で例外になり、catch-all の
        `stop 失敗:` 行だけ残して往復を取りこぼす。"""
        sdir = hooks._sessions_dir(self.tmpdir)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "s-badprompt.json"), "w", encoding="utf-8") as f:
            json.dump({"prompt": 999, "ts": 0, "recent": []}, f)
        hooks.cmd_stop({"session_id": "s-badprompt", "last_assistant_message": "ok"}, self.tmpdir)
        log_text = self._log_text()
        self.assertIn("session=s-badprompt skip:", log_text)
        self.assertNotIn("失敗", log_text)
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["ledger"]["turns"], 0)

    def test_consolidate_backend_error_is_logged_and_swallowed(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ.pop("RAIYA_LLM_CMD", None)  # 未設定 -> BackendError
        self._fill_window("s7", "何か発話です", "ok")
        self.assertIn("consolidate失敗", self._log_text())
        # 統合が失敗しても往復は台帳に残り、次の機会に再統合される (追記は決定論)。
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["ledger"]["pending"], hooks.CONSOLIDATE_EVERY)

    def test_consolidate_backend_error_via_command_backend_logs_bounded_summary_only(self):
        """hooks.log は長命な平文ログなので、command backend の stderr (会話や秘密を
        含みうる) が生のまま載らないこと (記憶機能設計書§10.3d)。書かれるのは
        reason (定型の失敗カテゴリ) と detail_len (長さ) だけ。stderr を2行に分けて
        出し、内部の改行がログ1行の構造 (行末 "\\n" まで) を壊さないことも確かめる。
        """
        script_path = os.path.join(self.tmpdir, "fail_with_sentinel.sh")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "cat > /dev/null\n"
                "echo 'SENTINEL_STDERR_L1_これは秘密の本文の一行目です' >&2\n"
                "echo 'SENTINEL_STDERR_L2_これは秘密の本文の二行目です' >&2\n"
                "exit 1\n"
            )
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)

        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script_path
        self._fill_window("s-cmd-fail", "何か発話です", "ok")

        log_text = self._log_text()

        # stderr の内部改行がログへ紛れ込んでいれば行数が増える。1エントリ=1行のまま
        # (台帳追記の ledger_id 行が CONSOLIDATE_EVERY 本 + 統合失敗の1本だけ)。
        lines = log_text.splitlines()
        self.assertEqual(len(lines), hooks.CONSOLIDATE_EVERY + 1)

        expected_detail = (
            "SENTINEL_STDERR_L1_これは秘密の本文の一行目です\n"
            "SENTINEL_STDERR_L2_これは秘密の本文の二行目です"
        )
        # len(detail) と len(reason) が異なる長さになるよう選んだ sentinel なので、
        # detail_len に reason の長さが紛れ込む mutant もこの pin で検出できる。
        expected_reason = (
            "LLM 呼び出しに失敗しました (backend=command): command backend が失敗しました"
        )
        self.assertNotEqual(len(expected_detail), len(expected_reason))
        expected_line = (
            "session=s-cmd-fail consolidate失敗: BackendError: {} (detail_len={})".format(
                expected_reason, len(expected_detail)
            )
        )
        self.assertTrue(lines[-1].endswith(expected_line), lines[-1])
        self.assertNotIn("SENTINEL_STDERR", log_text)

    def test_consolidate_backend_error_via_openai_http_error_logs_bounded_summary_only(self):
        """openai-api backend の HTTP エラー応答本文 (会話を含みうる) が hooks.log に
        生のまま載らないこと (記憶機能設計書§10.3d)。urllib.request.urlopen を差し替え、
        実 API へは接続しない。"""

        def _raising_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/chat/completions",
                429,
                "Too Many Requests",
                {},
                io.BytesIO('{"error":"SENTINEL_HTTP_本文"}'.encode("utf-8")),
            )

        os.environ["RAIYA_BACKEND"] = "openai-api"
        os.environ["OPENAI_API_KEY"] = "dummy"
        os.environ.pop("RAIYA_OPENAI_API_KEY", None)
        saved_urlopen = backend.urllib.request.urlopen
        backend.urllib.request.urlopen = _raising_urlopen
        try:
            self._fill_window("s-http-fail", "何か発話です", "ok")
        finally:
            backend.urllib.request.urlopen = saved_urlopen

        log_text = self._log_text()
        self.assertIn("consolidate失敗: BackendError:", log_text)
        self.assertIn("HTTP 429", log_text)
        # detail_len は実際に流し込んだ HTTP body と同じ bytes から計算する
        # (reason の長さ (66字) とは異なる値になるよう選んだ body なので、
        # detail_len に reason の長さが紛れ込む mutant もこの pin で検出できる)。
        expected_detail_len = len('{"error":"SENTINEL_HTTP_本文"}')
        self.assertIn("detail_len={}".format(expected_detail_len), log_text)
        self.assertNotIn("SENTINEL_HTTP_本文", log_text)

    def test_consolidate_backend_error_via_claude_cli_nonzero_exit_logs_bounded_summary_only(self):
        """claude-cli backend の stderr (会話や秘密を含みうる) が hooks.log に
        生のまま載らないこと (記憶機能設計書§10.3d)。subprocess.run を差し替え、
        実 claude コマンドは呼ばない。"""

        class _FakeCompletedProcess:
            returncode = 1
            stdout = ""
            stderr = "SENTINEL_CLI_L1_秘密\nSENTINEL_CLI_L2_秘密\n"

        def _fake_run(cmd, **kwargs):
            return _FakeCompletedProcess()

        os.environ["RAIYA_BACKEND"] = "claude-cli"
        saved_run = backend.subprocess.run
        backend.subprocess.run = _fake_run
        try:
            self._fill_window("s-cli-fail", "何か発話です", "ok")
        finally:
            backend.subprocess.run = saved_run

        log_text = self._log_text()
        # stderr の内部改行がログへ紛れ込んでいれば行数が増える。1エントリ=1行のまま
        # (台帳追記の ledger_id 行が CONSOLIDATE_EVERY 本 + 統合失敗の1本だけ)。
        self.assertEqual(len(log_text.splitlines()), hooks.CONSOLIDATE_EVERY + 1)
        self.assertIn("consolidate失敗: BackendError:", log_text)
        self.assertNotIn("SENTINEL_CLI", log_text)

    def test_consolidate_non_backend_error_logs_bounded_summary_only(self):
        """統合中に BackendError 以外の例外 (例: engine 側のバグ) が出ても、
        例外文字列 (会話本文を含みうる) をそのまま書かず、クラス名+長さだけを
        書くこと (記憶機能設計書§10.3d)。MemoryEngine.consolidate をクラス属性ごと
        差し替える (cmd_stop はこのクラスの同じオブジェクトを import している)。"""
        original_consolidate = hooks.MemoryEngine.consolidate

        def _raise_generic(self, notes=None):
            raise ValueError("SENTINEL_GENERIC_会話本文")

        hooks.MemoryEngine.consolidate = _raise_generic
        try:
            self._fill_window("s-generic-fail", "何か発話です", "ok")
        finally:
            hooks.MemoryEngine.consolidate = original_consolidate

        log_text = self._log_text()
        expected_msg_len = len("SENTINEL_GENERIC_会話本文")
        self.assertIn(
            "session=s-generic-fail consolidate失敗: ValueError (msg_len={})".format(
                expected_msg_len
            ),
            log_text,
        )
        self.assertNotIn("SENTINEL_GENERIC", log_text)

    def test_hooks_log_entry_is_single_line_when_reason_contains_newline(self):
        """reason は operator の env 設定値 (例: RAIYA_BACKEND) をそのまま埋め込む
        箇所があるため、その値自体に改行が入っていても hooks.log の1エントリが
        複数行に割れないこと (記憶機能設計書§10.3d: 偽の追加ログ行=ログ偽装の防止)。
        """
        os.environ["RAIYA_BACKEND"] = "unknown\nFORGED_LOG_LINE"
        self._fill_window("s-nl", "何か発話です", "ok")

        log_text = self._log_text()
        lines = log_text.splitlines()
        # 台帳追記の ledger_id 行が CONSOLIDATE_EVERY 本 + 統合失敗の1本。改行が
        # エスケープされていなければここが1本増える。
        self.assertEqual(len(lines), hooks.CONSOLIDATE_EVERY + 1)
        # 生の改行ではなく、エスケープされた "\n" (バックスラッシュ+n の2文字) が
        # 1行の中に入っていること。
        self.assertIn("\\n", lines[-1])
        self.assertIn("FORGED_LOG_LINE", lines[-1])

    def test_hooks_log_records_class_breakdown(self):
        """三性内訳(圓成實/依他起/遍計所執/reject)のログ行を、4スロットそれぞれに
        固有の証人ケース(そのスロットだけが非0、他3つは0)で pin する。

        統合の単位が窓になったので、内訳が載るのは stop の `proposals=` 行ではなく
        統合の `consolidate window=` 行になった (改修4 §7)。1ケースにつき提案は1件だけ
        返すため、4カウントの合計は常に1にしかならない。よって4スロットを互いに
        異なる値にする(team-lessons の「対称値は判別力ゼロ」の素朴な適用)ことは
        できず、スロットごとに「そのスロットのみ非0・残り3つは0」の別ケースを
        用意する。これでどの2スロット間の入れ替えも、どちらか一方のケースで必ず
        不一致になる。supersede スロットは別ケース
        (test_hooks_log_records_supersede_in_the_consolidate_line) で 1 を出して pin する。

        4ケースは raiya/sansho.py の三性ゲート規則で直接検分した:
            圓成實   -- span がユーザー発話に逐語接地
            依他起   -- span がアシスタント発話にのみ逐語接地
            遍計所執 -- span はあるが 1 文字改変で往復本文のどちらにも非接地
            reject   -- span はユーザー発話に接地するが、conditions が往復本文
                        どちらにも実在しない(縁が全滅=却下、遍計所執とは別経路)
        """
        cases = [
            (
                "圓成實",
                "私は猫が好きです/",
                "そうですか",
                "私は猫が好きです/",
                ["猫"],
                (1, 0, 0, 0),
            ),
            (
                "依他起",
                "教えてください",
                "その傾向は本人の性格によるものです",
                "その傾向は本人の性格によるものです",
                ["性格"],
                (0, 1, 0, 0),
            ),
            (
                "遍計所執",
                "私は猫が好きです/",
                "そうですか",
                "私は犬が好きです",  # span を1文字改変=往復本文のどちらにも非接地
                ["猫"],
                (0, 0, 1, 0),
            ),
            (
                "reject",
                "猫の話をしましょう",
                "はい",
                "猫の話をしましょう",  # span 自体はユーザー発話に接地(圓成實側)
                ["犬"],  # だが縁が往復本文のどちらにも実在せず却下
                (0, 0, 0, 1),
            ),
        ]
        for label, user_text, assistant_text, span, conditions, expected in cases:
            with self.subTest(label=label):
                session_id = "class-breakdown-{}".format(label)
                proposal = (
                    '[{{"content":"テスト","conditions":{conditions},'
                    '"polarity":"無記","kind":"所在","evidence":{{"source":"user","span":"{span}"}}}}]'
                ).format(
                    conditions=json.dumps(conditions, ensure_ascii=False),
                    span=span,
                )
                script = self._write_dummy_llm(proposal)
                os.environ["RAIYA_BACKEND"] = "command"
                os.environ["RAIYA_LLM_CMD"] = script
                self._fill_window(session_id, user_text, assistant_text)
                log_text = self._log_text()
                enjoujitsu, etaki, hengeshoshu, reject = expected
                expected_line = (
                    "session={} consolidate window={} sessions=1 proposals=1 "
                    "圓成實={} 依他起={} 遍計所執={} reject={} supersede=0"
                ).format(
                    session_id, hooks.CONSOLIDATE_EVERY, enjoujitsu, etaki, hengeshoshu, reject
                )
                # 行末 "\n" まで含めて照合する。部分一致だと、将来 §7 の上限が
                # 10 件以上に上がったとき末尾欄が supersede=0 と supersede=01 の
                # 両方に当たってしまう (2026-08-26 QA 指摘)。
                self.assertIn(expected_line + "\n", log_text)
                # 健全なセッション: LLM 出力が正しく JSON として読めたので parse_notes 行は無い。
                self.assertNotIn("session={} parse_notes".format(session_id), log_text)

    def test_hooks_log_records_supersede_in_the_consolidate_line(self):
        """`supersede=` スロットの証人 (残り4スロットは上の witness 4 件が持つ)。

        置換が通った提案はクラス欄 (圓成實) と supersede 欄の両方に 1 を出すので、
        supersede を常に 0 と書く mutant も、クラス欄と取り違える mutant も落ちる。
        """
        first = json.dumps(
            [{"content": "コーヒーが好き", "conditions": ["コーヒー"], "polarity": "善","kind": "所在",
              "evidence": [{"source": "user", "span": "コーヒーが好きです/"}]}],
            ensure_ascii=False,
        )
        with mock.patch("raiya.backend.call_llm", return_value=first):
            self._fill_window("s-supersede", "コーヒーが好きです/", "いいですね")
        seed_id = MemoryEngine(state_dir=self.tmpdir, new_life=False).alaya.list_seeds()[0]["id"]
        second = json.dumps(
            [{"action": "supersede", "target_id": seed_id, "content": "緑茶が好き",
              "conditions": ["緑茶"], "polarity": "善","kind": "所在",
              "evidence": [{"source": "user", "span": "最近は緑茶が好きです/"}]}],
            ensure_ascii=False,
        )
        with mock.patch("raiya.backend.call_llm", return_value=second):
            self._fill_window("s-supersede", "最近は緑茶が好きです/", "そうですか")
        self.assertIn(
            "session=s-supersede consolidate window={} sessions=1 proposals=1 "
            "圓成實=1 依他起=0 遍計所執=0 reject=0 supersede=1\n".format(hooks.CONSOLIDATE_EVERY),
            self._log_text(),
        )

    def test_hooks_log_counts_withdrawal_shadow_only_when_nonzero(self):
        """plan pin 14: 影 0 件なら consolidate 行は不変、1 件なら ` 取り下げ照合=1` (skipped= の前)。"""
        raw = json.dumps(
            [{"content": "コーヒーが好き", "conditions": ["コーヒー"], "polarity": "善", "kind": "所在",
              "evidence": [{"source": "user", "span": "コーヒーが好きです/"}]}],
            ensure_ascii=False,
        )
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            self._fill_window("s-shadow", "コーヒーが好きです/", "いいですね")
        base = ("session=s-shadow consolidate window={} sessions=1 proposals=1 "
                "圓成實=1 依他起=0 遍計所執=0 reject=0 supersede=0\n").format(hooks.CONSOLIDATE_EVERY)
        self.assertIn(base, self._log_text())
        self.assertNotIn("取り下げ照合", self._log_text())
        engine = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        seed = engine.alaya.list_seeds()[0]
        engine.withdraw_seed(seed["id"], "太郎", "誤り", ledger_ids=[seed["evidences"][0]["ledger_id"]])
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            self._fill_window("s-shadow", "コーヒーが好きです/", "いいですね")
        self.assertIn(
            "session=s-shadow consolidate window={} sessions=1 proposals=1 "
            "圓成實=0 依他起=0 遍計所執=0 reject=0 supersede=0 取り下げ照合=1\n".format(hooks.CONSOLIDATE_EVERY),
            self._log_text(),
        )
        after = MemoryEngine(state_dir=self.tmpdir, new_life=False).alaya.list_seeds()
        self.assertEqual(len(after), 1)  # 影は植えない (consolidate 経路で効く: plan pin 6)

    def test_hooks_log_counts_twin_match_only_when_nonzero(self):
        """#78: 双子照合 0 件なら consolidate 行は不変、1 件なら ` 双子照合=1` (取り下げ照合の後・skipped= の前)。
"""
        raw = json.dumps(
            [{"content": "コーヒーが好き", "conditions": ["コーヒー", "好き"], "polarity": "善", "kind": "所在",
              "evidence": [{"source": "assistant", "span": "コーヒーが好きです/ね"}]}],
            ensure_ascii=False,
        )

        def seed(seed_id, sansho_class, strength):
            return {"id": seed_id, "content": "コーヒーが好き", "strength": strength,
                    "conditions": ["コーヒー", "好き"], "polarity": "無記", "sansho": sansho_class, "kind": "所在",
                    "project": None, "superseded_by": None, "evidences": [], "history": [],
                    "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}

        def write(seeds):
            clear_state(self.tmpdir)  # fixture の書き直し (write_state は既存 state を拒否する)
            write_state(self.tmpdir, {"seeds": seeds, "alaya_meta": {"life": 1, "next_id": len(seeds) + 1}})

        write([seed(1, "依他起", 2)])  # E 無し: 通常の reinforce
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            self._fill_window("s-twin", "コーヒーの話", "コーヒーが好きです/ね")
        self.assertNotIn("双子照合", self._log_text())
        write([seed(1, "圓成實", 1), seed(2, "依他起", 2)])  # E あり: H は隠れる
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            self._fill_window("s-twin", "コーヒーの話", "コーヒーが好きです/ね")
        self.assertIn(
            "session=s-twin consolidate window={} sessions=1 proposals=1 "
            "圓成實=0 依他起=0 遍計所執=0 reject=0 supersede=0 双子照合=1\n".format(hooks.CONSOLIDATE_EVERY),
            self._log_text(),
        )
        h = [x for x in alaya_state(self.tmpdir)["seeds"] if x["id"] == 2][0]
        self.assertEqual(h["strength"], 2)

    def test_hooks_log_counts_record_only_when_nonzero(self):
        """#101 D / #105 R: 強めずに記録だけ残した提案が 1 件以上なら ` 記録のみ=N`、0 件なら行は不変。"""
        raw = json.dumps(
            [{"content": "コーヒーが好き", "conditions": ["コーヒー", "好き"], "polarity": "善", "kind": "所在",
              "evidence": [{"source": "assistant", "span": "コーヒーが好きです/ね"}]}],
            ensure_ascii=False,
        )

        def seed(evidences):
            return {"id": 1, "content": "コーヒーが好き", "strength": 2,
                    "conditions": ["コーヒー", "好き"], "polarity": "無記", "sansho": "依他起", "kind": "所在",
                    "project": None, "superseded_by": None, "evidences": evidences, "history": [],
                    "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}

        def write(seeds):
            clear_state(self.tmpdir)
            write_state(self.tmpdir, {"seeds": seeds, "alaya_meta": {"life": 1, "next_id": len(seeds) + 1}})

        raw_new = json.dumps(
            [{"content": "紅茶が嫌い", "conditions": ["紅茶"], "polarity": "悪", "kind": "所在",
              "evidence": [{"source": "assistant", "span": "紅茶が嫌いですね"}]}],
            ensure_ascii=False,
        )
        write([seed([])])  # 記録のみになる提案なし (別種子を植えるだけ)
        with mock.patch("raiya.backend.call_llm", return_value=raw_new):
            self._fill_window("s-ro", "紅茶の話", "紅茶が嫌いですね")
        self.assertNotIn("記録のみ", self._log_text())
        write([seed([{"source": "user", "span": "コーヒーが好き"}])])  # D
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            self._fill_window("s-ro", "コーヒーの話", "コーヒーが好きです/ね")
        self.assertIn(
            "session=s-ro consolidate window={} sessions=1 proposals=1 "
            "圓成實=0 依他起=1 遍計所執=0 reject=0 supersede=0 記録のみ=1\n".format(hooks.CONSOLIDATE_EVERY),
            self._log_text(),
        )

    def test_hooks_log_counts_settled_match_as_record_only(self):
        """#152 SC': 圓成實 の種子と同一 content の 依他起 の提案 (settled_match) も ` 記録のみ=N` に数える。"""
        raw = json.dumps(
            [{"content": "コーヒーが好き", "conditions": ["コーヒー", "好き"], "polarity": "善", "kind": "性向",
              "evidence": [{"source": "assistant", "span": "コーヒーが好きです/ね"}]}],
            ensure_ascii=False,
        )
        seed = {"id": 1, "content": "コーヒーが好き", "strength": 2,
                "conditions": ["コーヒー", "好き"], "polarity": "無記", "sansho": "圓成實", "kind": "性向",
                "project": None, "superseded_by": None, "evidences": [], "history": [],
                "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}
        clear_state(self.tmpdir)
        write_state(self.tmpdir, {"seeds": [seed], "alaya_meta": {"life": 1, "next_id": 2}})
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            self._fill_window("s-sc", "コーヒーの話", "コーヒーが好きです/ね")
        self.assertIn(
            "session=s-sc consolidate window={} sessions=1 proposals=1 "
            "圓成實=0 依他起=1 遍計所執=0 reject=0 supersede=0 記録のみ=1\n".format(hooks.CONSOLIDATE_EVERY),
            self._log_text(),
        )
        self.assertNotIn("コーヒーが好き", self._log_text())

    def test_debug_consolidate_counts_include_match_keys(self):
        """#78: debug.log の consolidate イベントの counts に 双子照合 / 取り下げ照合 が常に出る (0 も)。"""
        from raiya import debuglog
        raw = json.dumps(
            [{"content": "コーヒーが好き", "conditions": ["コーヒー", "好き"], "polarity": "善", "kind": "所在",
              "evidence": [{"source": "assistant", "span": "コーヒーが好きです/ね"}]}],
            ensure_ascii=False,
        )

        def seed(seed_id, sansho_class, strength):
            return {"id": seed_id, "content": "コーヒーが好き", "strength": strength,
                    "conditions": ["コーヒー", "好き"], "polarity": "無記", "sansho": sansho_class, "kind": "所在",
                    "project": None, "superseded_by": None, "evidences": [], "history": [],
                    "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}

        def write(seeds):
            clear_state(self.tmpdir)  # fixture の書き直し (write_state は既存 state を拒否する)
            write_state(self.tmpdir, {"seeds": seeds, "alaya_meta": {"life": 1, "next_id": len(seeds) + 1}})

        with open(os.path.join(self.tmpdir, "debug"), "w", encoding="utf-8") as f:
            f.write("1")

        def fill():
            for _ in range(hooks.CONSOLIDATE_EVERY):
                with contextlib.redirect_stdout(io.StringIO()):
                    hooks.cmd_recall({"prompt": "コーヒーの話", "session_id": "s-dbg"}, self.tmpdir)
                hooks.cmd_stop({"session_id": "s-dbg", "last_assistant_message": "コーヒーが好きです/ね"},
                               self.tmpdir, debug_level=1)

        def last_counts():
            events = [e for e in debuglog.read_events(self.tmpdir) if e.get("event") == "consolidate"]
            self.assertTrue(events)
            return events[-1]["counts"]

        write([seed(1, "依他起", 2)])  # E 無し
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            fill()
        counts = last_counts()
        self.assertEqual((counts["双子照合"], counts["取り下げ照合"]), (0, 0))
        write([seed(1, "圓成實", 1), seed(2, "依他起", 2)])  # E あり
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            fill()
        counts = last_counts()
        self.assertEqual((counts["双子照合"], counts["取り下げ照合"]), (1, 0))

    def test_hooks_log_reports_the_already_consolidated_skip(self):
        """行末の任意欄 ` skipped=already_consolidated` の証人。別の書き手が応答待ちの
        間に同じ窓を統合していれば、統合はせず skip を明示して1行だけ残す。"""

        def _race(prompt, **kwargs):
            # LLM 応答待ちの間に別プロセスが同じ窓を統合した状況を再現する
            # (LLM 呼び出しが lock の内側へ移ったらここはハングして落ちる)。
            other = MemoryEngine(state_dir=self.tmpdir, new_life=False)
            other.ledger.reload()
            other.ledger.mark_consolidated(hooks.CONSOLIDATE_EVERY)
            other.ledger.commit()
            return "[]"

        with mock.patch("raiya.backend.call_llm", side_effect=_race):
            self._fill_window("s-race", "何か発話です", "ok")
        self.assertIn(
            "session=s-race consolidate window={} sessions=1 proposals=0 "
            "圓成實=0 依他起=0 遍計所執=0 reject=0 supersede=0 skipped=already_consolidated\n".format(
                hooks.CONSOLIDATE_EVERY
            ),
            self._log_text(),
        )

    def test_hooks_log_no_parse_notes_line_for_healthy_empty_response(self):
        # LLM が正常に空配列 [] を返したケース(健全)は proposals=0 になるが、
        # parse_notes 行は出ない(「静けさ=健全」を保つ)。
        script = self._write_dummy_llm("[]")
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        self._fill_window("s9", "何か発話です", "了解です")
        log_text = self._log_text()
        self.assertIn(
            "consolidate window={} sessions=1 proposals=0".format(hooks.CONSOLIDATE_EVERY), log_text
        )
        self.assertNotIn("parse_notes", log_text)

    def test_hooks_log_records_parse_notes_when_llm_output_is_unreadable(self):
        # LLM が壊れた出力を返すケース(異常): 読み取れた提案だけが残るが、寛容パースが
        # 働いた理由が parse_notes 行として残るため hooks.log 上で健全な [] と区別できる。
        # 7 件返って先頭5件のみ採用(切り捨てnote)・うち1件が content 欠落で却下
        # (検証却下note)、というふうに複数の note が積まれる経路を1本の実行で確かめ、
        # かつ note に現れる件数 (7件中5件 / 却下1件) を互いに異なる値にすることで
        # note の順序を入れ替える mutant も検出できるようにする
        # (team-lessons: 対称な値の pin は判別力ゼロ)。
        script = self._write_dummy_llm(
            '[{"conditions":["猫"],"polarity":"無記"},'
            '{"content":"猫が好き","conditions":["猫"],"polarity":"善",'
            '"evidence":{"source":"user","span":"私は猫が好きです/"}},'
            '{"content":"三件目(採用されない)","conditions":[],"polarity":"無記"},'
            '{"content":"d4"},{"content":"d5"},{"content":"d6"},{"content":"d7"}]'
        )
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        self._fill_window("s10", "私は猫が好きです/", "いいですね")
        log_text = self._log_text()
        # このテストが実際に検分するのは parse_notes 行の有無・内容。三性の内訳
        # (この fixture では 1/0/3/0) は test_hooks_log_records_class_breakdown 側の
        # witness 4 件で pin 済みなので、ここで重ねて assert しない (判別力の弱い pin を
        # 増やしても mutant は捕まらない=team-lessons)。
        self.assertIn(
            "session=s10 consolidate window={} sessions=1 proposals=4".format(hooks.CONSOLIDATE_EVERY),
            log_text,
        )
        self.assertIn(
            "session=s10 parse_notes: 7件中5件のみ採用(上限5); "
            "項目検証で1件を却下(dict でない/content 欠落)",
            log_text,
        )


class TestLogUnencodable(_HooksTestCase):
    def test_lone_surrogate_in_message_is_written_escaped_and_does_not_raise(self):
        hooks._log(self.tmpdir, "session=a\ud800 x")
        lines = self._log_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("session=a\\ud800 x", lines[0])


    def test_control_chars_are_escaped_and_entry_stays_one_line(self):
        hooks._log(self.tmpdir, "a\nb\rc\x1b[31md\x00e\tf")
        text = self._log_text()
        self.assertEqual(len(text.splitlines()), 1)
        for bad in ("\r", "\x1b", "\x00", "\t"):
            self.assertNotIn(bad, text)
        self.assertIn("a\\nb\\rc\\x1b[31md\\x00e\\tf", text)

    def test_printable_non_ascii_is_kept(self):
        hooks._log(self.tmpdir, "セッション 本文")
        self.assertIn("セッション 本文", self._log_text())


class TestUnsafeSessionIdControlChars(_HooksTestCase):
    def test_safe_session_id_rejects_control_chars_keeps_non_ascii(self):
        for sid in ("a\nb", "a\rb", "a\x1bb", "a\x00b", "a\x7fb", "a\ud800"):
            with self.subTest(sid=sid):
                self.assertIsNone(hooks._safe_session_id(sid))
        self.assertEqual(hooks._safe_session_id("セッション-1"), "セッション-1")

    def test_recall_with_newline_session_id_writes_no_session_file(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "こんにちは世界", "session_id": "bad\nid"}, self.tmpdir)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "sessions")))

    def test_stop_logs_session_none_for_control_char_id(self):
        hooks.cmd_stop({"session_id": "x\r\x1b[31mFAKE", "agent_id": "sub"}, self.tmpdir)
        text = self._log_text()
        self.assertIn("session=None", text)
        self.assertNotIn("FAKE", text)


    def test_unicode_separators_and_format_chars_are_escaped_fffc_kept(self):
        hooks._log(self.tmpdir, "a\u2028b\u200bc\U000e0001d\ufffce")
        text = self._log_text()
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("a\\u2028b\\u200bc\\U000e0001d\ufffce", text)


class TestControlCharSessionIdAdapter(_HooksTestCase):
    def test_cmd_stop_record_line_shows_session_none_and_ledger_keeps_raw_id(self):
        sid = "x\r\u001b[31mFAKE"
        transcript_path = os.path.join(self.tmpdir, "t.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            for role, text in (("user", "テスト発話です"), ("assistant", "応答です")):
                f.write(json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}},
                                   ensure_ascii=False) + "\n")
        hooks.cmd_stop({"session_id": sid, "transcript_path": transcript_path}, self.tmpdir)
        rows = ledger_rows(self.tmpdir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], sid)
        with open(os.path.join(self.tmpdir, "hooks.log"), "rb") as f:
            raw = f.read()
        self.assertIn(b"session=None ledger_id=", raw)
        for bad in (b"\r", b"\x1b", b"FAKE"):
            self.assertNotIn(bad, raw)

    def test_cmd_recall_with_lone_surrogate_session_id_begins_no_life(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "こんにちは世界", "session_id": "\ud83d"}, self.tmpdir)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "sessions")))
        from raiya.engine import MemoryEngine
        self.assertEqual(MemoryEngine(state_dir=self.tmpdir, new_life=False).life, 0)


class TestMainImplExceptionSummaries(_HooksTestCase):
    """`_main_impl` の2つの catch-all (stdin 解析失敗・サブコマンド失敗) でも、
    `consolidate失敗` (TestCmdStop) と同じ有界な要約だけが書かれ、例外文字列そのもの
    (秘密や入力本文を含みうる) が hooks.log に漏れないこと (記憶機能設計書§10.3d)。
    `_exc_summary` は共有関数なので、これは同じ不変条件を3箇所目・4箇所目の
    呼び出し元でも独立に確かめる pin (team-lessons: 対称verb1本のguardは
    兄弟verbで穴が開いたままになる、の3箇所版)。
    """

    def _read_log(self):
        path = os.path.join(self.tmpdir, "hooks.log")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_stdin_parse_failure_logs_bounded_summary_without_stdin_body(self):
        os.environ["RAIYA_STATE_DIR"] = self.tmpdir
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        os.environ.pop("RAIYA_HOOKS", None)
        original_stdin = sys.stdin
        sys.stdin = io.StringIO('{"broken": SENTINEL_STDIN_本文')  # 壊れた JSON
        try:
            hooks.main(["stop"])
        finally:
            sys.stdin = original_stdin

        log_text = self._read_log()
        self.assertIn("stdin 解析失敗 (stop): JSONDecodeError (msg_len=", log_text)
        # 注意: str(JSONDecodeError) は位置情報 (line/column/char) のみで、解析対象の
        # 生ドキュメントそのものは含まない。よってこの sentinel 非漏洩 assert は
        # 実漏洩に対する pin ではなく、_exc_summary が str(e) でなく
        # クラス名+msg_len の書式を使っていることの pin にとどまる (実際の漏洩耐性は
        # read() 自体が失敗するケース=test_stdin_read_failure_... 側で確かめる)。
        self.assertNotIn("SENTINEL_STDIN", log_text)

    def test_stdin_read_failure_logs_bounded_summary_without_message_body(self):
        """stdin.read() 自体が例外を送出するケース (壊れた JSON ではなく読取失敗)。
        この経路では例外メッセージに会話本文が直接載りうるので、こちらが実際の
        漏洩耐性を確かめる本体の pin になる (記憶機能設計書§10.3d)。"""
        os.environ["RAIYA_STATE_DIR"] = self.tmpdir
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        os.environ.pop("RAIYA_HOOKS", None)

        class _RaisingStdin:
            def read(self):
                raise RuntimeError("SENTINEL_STDIN_会話本文")

        original_stdin = sys.stdin
        sys.stdin = _RaisingStdin()
        try:
            hooks.main(["stop"])
        finally:
            sys.stdin = original_stdin

        log_text = self._read_log()
        expected_msg_len = len("SENTINEL_STDIN_会話本文")
        self.assertIn(
            "stdin 解析失敗 (stop): RuntimeError (msg_len={})".format(expected_msg_len),
            log_text,
        )
        self.assertNotIn("SENTINEL_STDIN_会話本文", log_text)

    def test_subcommand_failure_logs_bounded_summary_without_message_body(self):
        os.environ["RAIYA_STATE_DIR"] = self.tmpdir
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        os.environ.pop("RAIYA_HOOKS", None)
        original_stop = hooks.SUBCOMMANDS["stop"]

        def _raise_generic(payload, state_dir):
            raise RuntimeError("SENTINEL_SUB_本文")

        hooks.SUBCOMMANDS["stop"] = _raise_generic
        original_stdin = sys.stdin
        sys.stdin = io.StringIO("{}")
        try:
            hooks.main(["stop"])
        finally:
            sys.stdin = original_stdin
            hooks.SUBCOMMANDS["stop"] = original_stop

        log_text = self._read_log()
        self.assertIn("stop 失敗: RuntimeError (msg_len=", log_text)
        self.assertNotIn("SENTINEL_SUB", log_text)


class TestClaudeCliEnvAndArgvViaStopHook(_HooksTestCase):
    """stop hook が engine.consolidate() 経由で claude-cli backend を呼ぶとき、
    軽量モデル+--no-session-persistence+RAIYA_HOOK_ACTIVE=1 が実際に付くことの pin
    (subprocess.run を差し替える。実 claude コマンドは呼ばない)。"""

    def setUp(self):
        super().setUp()
        self._saved_subprocess_run = backend.subprocess.run
        self.calls = []

        class _FakeCompletedProcess:
            returncode = 0
            stdout = "[]"
            stderr = ""

        def _fake_run(cmd, **kwargs):
            self.calls.append({"cmd": cmd, "env": kwargs.get("env")})
            return _FakeCompletedProcess()

        backend.subprocess.run = _fake_run

    def tearDown(self):
        backend.subprocess.run = self._saved_subprocess_run
        super().tearDown()

    def test_stop_hook_consolidate_call_gets_model_no_session_persistence_and_hook_active_env(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ.pop("RAIYA_PROPOSE_MODEL", None)
        self._fill_window("s1", "何かの話です", "ok")
        self.assertEqual(len(self.calls), 1)  # 統合は CONSOLIDATE_EVERY 往復で1回だけ
        cmd = self.calls[0]["cmd"]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(self.calls[0]["env"].get("RAIYA_HOOK_ACTIVE"), "1")


class TestSessionLifeSemantics(_HooksTestCase):
    """「1 生=1 Claude Code セッション」を保つこと。recall がそのセッションの最初の
    プロンプトのときだけ begin_new_life() を1回呼び、stop は生を進めない。
    """

    def _write_noop_llm_script(self):
        script = os.path.join(self.tmpdir, "dummy_noop.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ncat > /dev/null\necho '[]'\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        return script

    def _current_life(self):
        from raiya.engine import MemoryEngine

        return MemoryEngine(state_dir=self.tmpdir, new_life=False).life

    def test_same_session_multiple_recall_and_stop_calls_advance_life_by_one_only(self):
        # (a)
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = self._write_noop_llm_script()

        self.assertEqual(self._current_life(), 0)
        for i in range(3):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                hooks.cmd_recall(
                    {"prompt": "話題{}です".format(i), "session_id": "same-session"}, self.tmpdir
                )
        for i in range(3):
            hooks.cmd_stop(
                {"session_id": "same-session", "last_assistant_message": "応答{}".format(i)},
                self.tmpdir,
            )
        self.assertEqual(self._current_life(), 1)  # recall 3回+stop 3回=6回呼んでも +1 のみ

    def test_different_session_ids_each_advance_life_by_one(self):
        # (b)
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = self._write_noop_llm_script()

        self.assertEqual(self._current_life(), 0)
        for session_id in ("session-a", "session-b", "session-c"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                hooks.cmd_recall({"prompt": "こんにちは、よろしく", "session_id": session_id}, self.tmpdir)
        self.assertEqual(self._current_life(), 3)

    def test_no_session_id_never_advances_life(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = self._write_noop_llm_script()

        self.assertEqual(self._current_life(), 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.cmd_recall({"prompt": "こんにちは、よろしく"}, self.tmpdir)
        self.assertEqual(self._current_life(), 0)

    def test_seed_not_recalled_for_three_turns_is_neither_deleted_nor_weakened(self):
        # (d): 「1 ターン=1 生」に化けていれば生が3つ進むケース。改修4 で種子は削除も
        # 減衰もされない (KUN-3 恒隨轉) が、planted_at / miss_streak (連続不発芽の生の数)
        # は今も生を単位に数えるため、生の二重計上は観測値をそのまま狂わせる。
        # 種の植え付けは台帳の統合が担うようになったので (stop は追記するだけ)、種はここで直接植える。

        # ターン1: セッション最初のプロンプト (生が1つ進む)。種の縁には触れない語にする。
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.cmd_recall({"prompt": "こんにちは、よろしく", "session_id": "s-decay"}, self.tmpdir)
        MemoryEngine(state_dir=self.tmpdir, new_life=False).imprint(
            sansho.Turn(user_text="私は猫が好きです/", assistant_text="いいですね"),
            [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
              "evidence": {"source": "user", "span": "私は猫が好きです/"}}],
        )

        before = alaya_state(self.tmpdir)
        self.assertEqual(len(before["seeds"]), 1)
        self.assertEqual(before["seeds"][0]["strength"], 1)

        # ターン2〜4: 同一セッションのまま、種の縁 (猫) に一切触れないプロンプトで
        # recall/stop を3回繰り返す (「1 ターン=1 生」ならここで生が3つ進む)。
        for i in range(3):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                hooks.cmd_recall(
                    {"prompt": "全く関係の無い話題{}です".format(i), "session_id": "s-decay"}, self.tmpdir
                )
            hooks.cmd_stop(
                {"session_id": "s-decay", "last_assistant_message": "そうですか"}, self.tmpdir
            )

        after = alaya_state(self.tmpdir)
        # 同一セッション内なので生は1のまま。種は一度も発芽していないが、削除も減衰も
        # されず strength は不変で、miss_streak も 0 のまま (生が進まないので連続不発芽の
        # 計上自体が起きない)。
        self.assertEqual(self._current_life(), 1)
        self.assertEqual(len(after["seeds"]), 1)
        self.assertEqual(after["seeds"][0]["strength"], 1)
        self.assertEqual(after["seeds"][0]["miss_streak"], 0)
        self.assertIsNone(after["seeds"][0]["last_fired"])
        self.assertEqual(after["seeds"][0]["fire_count"], 0)


class TestHarnessInjectionFilter(unittest.TestCase):
    """Claude Code が user 枠へ差し込む harness 由来テキストを skip すること (改修4 §8)。

    これは机上の心配ではない: master 側で稼働中の hooks が、この開発中の
    `<system-reminder>` 由来のタスク通知そのものを「ユーザーが言ったこと」として
    圓成實の種にした実例がある。圓成實=「ユーザーが逐語で言った」の意味を守るには、
    ユーザー発話でない注入は想起・台帳・熏習のどこにも入れてはならない。
    """

    def test_known_harness_tags_are_skipped(self):
        for text in ("<task-notification>\n<task-id>x</task-id>", "  <system-reminder>...</system-reminder>",
                     "<local-command-stdout>ok</local-command-stdout>", "<IDE_SELECTION>x"):
            self.assertTrue(hooks._is_harness_injected(text), text)
            self.assertTrue(hooks._is_skippable_prompt(text), text)

    def test_subagent_agent_message_is_skipped(self):
        # 本番台帳 id 9/14/15 の先頭と同形 (サブエージェントの報告が user 枠に届いたもの)。
        text = '<agent-message from="aa3aa987115019f06">\n[Subagent hand-back] The text below is ...\n</agent-message>'
        self.assertTrue(hooks._is_harness_injected(text))
        self.assertTrue(hooks._is_skippable_prompt(text))
        self.assertFalse(hooks._is_harness_injected("agent-message について教えて"))

    def test_ordinary_prompts_are_not_skipped(self):
        for text in ("<div> を使いたい", "コマンドを表示して", "a < b ?"):
            self.assertFalse(hooks._is_harness_injected(text), text)
        self.assertFalse(hooks._is_skippable_prompt("<div> を使いたい"))


class TestRecallContextAndIndex(_HooksTestCase):
    """recall の文脈窓 (直前までのプロンプト) と性向索引の注入 (改修4 §6)。"""

    def _plant(self, content, cond):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="{} p/ {}".format(content, cond), assistant_text=""),  # #149: 所在 の形
                    [{"content": content, "conditions": [cond], "polarity": "無記","kind": "所在",
                      "evidence": {"source": "user", "span": content + " p/"}}])

    def _plant_etaki(self, content, cond, span, extra_span=None):
        """assistant_text 側に接地する 依他起 を植える。extra_span があれば 2 往復の窓で strength 2。"""
        eng = MemoryEngine(state_dir=self.tmpdir)
        if extra_span is None:
            eng.imprint(sansho.Turn(user_text="質問 {}".format(cond),
                                    assistant_text="{} {}".format(span, cond)),
                        [{"content": content, "conditions": [cond], "polarity": "無記","kind": "所在",
                          "evidence": {"source": "assistant", "span": span}}])
            return
        window = [
            sansho.Turn(user_text="質問 {}".format(cond), assistant_text="{} {}".format(span, cond)),
            sansho.Turn(user_text="再確認 {}".format(cond), assistant_text="{} {}".format(extra_span, cond)),
        ]
        eng.imprint(window, [{"content": content, "conditions": [cond], "polarity": "無記","kind": "所在",
                              "evidence": [{"source": "assistant", "span": span},
                                           {"source": "assistant", "span": extra_span}]}])

    def test_session_cache_keeps_recent_prompts_excluding_current(self):
        for i in range(hooks.RECALL_CONTEXT_TURNS + 2):
            with contextlib.redirect_stdout(io.StringIO()):
                hooks.cmd_recall({"prompt": "p{}".format(i), "session_id": "s1"}, self.tmpdir)
        cache = hooks._read_session_cache(self.tmpdir, "s1")
        self.assertEqual(cache["prompt"], "p{}".format(hooks.RECALL_CONTEXT_TURNS + 1))
        self.assertEqual(cache["recent"], ["p{}".format(i) for i in range(1, hooks.RECALL_CONTEXT_TURNS + 1)])

    def test_skippable_prompts_are_not_added_to_recent(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "普通の発言", "session_id": "s2"}, self.tmpdir)
            hooks.cmd_recall({"prompt": "/help", "session_id": "s2"}, self.tmpdir)
            hooks.cmd_recall({"prompt": "<task-notification>x", "session_id": "s2"}, self.tmpdir)
            hooks.cmd_recall({"prompt": "次の発言", "session_id": "s2"}, self.tmpdir)
        self.assertEqual(hooks._read_session_cache(self.tmpdir, "s2")["recent"], ["普通の発言"])

    PASTE = '<pasted_content id="x">{}</pasted_content id="x">'

    def test_current_prompt_matches_with_paste_but_recent_carries_stripped_text(self):
        """#44 P1/P2: 今回の照合は貼り付けを含めた本文、キャッシュの prompt も貼り付け入り、
        次回の recent は貼り付けを除いた本文だけ。"""
        self._plant("猫が好き", "猫")
        prompt = "これを見て " + self.PASTE.format("猫についての長い外部文書") + " お願いします"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": prompt, "session_id": "sp"}, self.tmpdir)
        self.assertIn("猫が好き", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(hooks._read_session_cache(self.tmpdir, "sp")["prompt"], prompt)
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "次の発言です", "session_id": "sp"}, self.tmpdir)
        recent = hooks._read_session_cache(self.tmpdir, "sp")["recent"]
        self.assertEqual(recent, ["これを見て  お願いします"])
        self.assertNotIn("猫", "".join(recent))

    def test_paste_only_prompt_is_not_carried_into_recent(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "普通の発言です", "session_id": "sq"}, self.tmpdir)
            hooks.cmd_recall({"prompt": self.PASTE.format("貼り付けだけの長い本文" * 5), "session_id": "sq"}, self.tmpdir)
            hooks.cmd_recall({"prompt": "最後の発言です", "session_id": "sq"}, self.tmpdir)
        self.assertEqual(hooks._read_session_cache(self.tmpdir, "sq")["recent"], ["普通の発言です"])

    def test_recall_fires_via_context_window(self):
        self._plant("猫が好き", "猫")
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "猫の話をしたい", "session_id": "s3"}, self.tmpdir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "続けて", "session_id": "s3"}, self.tmpdir)
        payload = json.loads(out.getvalue())
        self.assertIn("猫が好き", payload["hookSpecificOutput"]["additionalContext"])
        self.assertIn("【この入力に触れた記録】", payload["hookSpecificOutput"]["additionalContext"])

    def test_recall_emits_index_even_without_germination(self):
        self._plant("猫が好き", "猫")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "全く別の話題", "session_id": "s4"}, self.tmpdir)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("【記録の索引", ctx)
        self.assertNotIn("【この入力に触れた記録】", ctx)

    def test_recall_emits_nothing_when_no_seeds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "まだ何も無い", "session_id": "s5"}, self.tmpdir)
        self.assertEqual(out.getvalue(), "")

    def test_recall_emits_index_with_suitei_label_for_ungerminated_etaki_decision_kind(self):
        # 改修5 §12.5: 決定系 kind (ここでは 所在) の依他起は発芽前でも索引に「(推定)」付きで載る。
        self._plant_etaki("アシスタントの推定", "推定", "アシスタントの推定です")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "全く別の話題", "session_id": "s-etaki"}, self.tmpdir)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- 所在(推定): アシスタントの推定", ctx)
        self.assertNotIn("【この入力に触れた記録】", ctx)

    def test_recall_emits_nothing_for_ungerminated_disposition_etaki(self):
        # 性向の依他起は索引に載らない (従来どおり INJECTION_INDEX_CLASSES = 圓成實 のみ) ので何も出ない。
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定"),
                    [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記", "kind": "性向",
                      "evidence": {"source": "assistant", "span": "アシスタントの推定です"}}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "全く別の話題", "session_id": "s-etaki-d"}, self.tmpdir)
        self.assertEqual(out.getvalue(), "")

    def test_recall_germinated_etaki_emits_narrative_without_index(self):
        self._plant_etaki("アシスタントの推定", "推定", "アシスタントの推定です", extra_span="同じ推定の再確認です")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "推定の話をしたい", "session_id": "s-etaki2"}, self.tmpdir)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- 所在(推定): アシスタントの推定(強度2)", ctx)  # 発芽した依他起は narrative に
        # 発芽した依他起は ① に出るので、外側の ③ の索引では繰り返さない (layered-memory 計画 §2.3 の 発芽 の門)。
        self.assertNotIn("【記録の索引", ctx)

    def test_recall_context_and_cached_recent_are_the_same_window(self):
        # 文脈窓は 1 度だけ組み立てて「想起へ渡す context」と「キャッシュへ書く recent」
        # の両方に使う。2 度組み立てると、片方だけを直す変更で注入に使った窓と次回の窓が
        # 静かに食い違う (どちらのテストも単独では気づけない)。
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "最初の発言", "session_id": "s7"}, self.tmpdir)
        empty = {"seeds": [], "narrative": "", "index": []}
        with mock.patch.object(MemoryEngine, "recall", autospec=True, return_value=empty) as recall:
            with contextlib.redirect_stdout(io.StringIO()):
                hooks.cmd_recall({"prompt": "次の発言", "session_id": "s7"}, self.tmpdir)
        context = recall.call_args.kwargs["context"]
        self.assertEqual(context, ["最初の発言"])
        self.assertEqual(hooks._read_session_cache(self.tmpdir, "s7")["recent"], context)

    def test_stop_and_recall_accept_legacy_cache_without_recent(self):
        # 改修4 以前のセッションキャッシュ ({"prompt", "ts"} だけで recent が無い) が
        # sessions/ に残ったまま更新した場合でも、stop はその prompt を台帳へ記帳でき、
        # 次の recall はそれを文脈窓に積む (欠けている recent は空扱い)。
        self._plant("猫が好き", "猫")
        sdir = hooks._sessions_dir(self.tmpdir)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "s-legacy.json"), "w", encoding="utf-8") as f:
            json.dump({"prompt": "猫が好き", "ts": 0}, f)
        with mock.patch("raiya.backend.call_llm", side_effect=AssertionError("Stop は LLM を呼ばない")):
            hooks.cmd_stop({"session_id": "s-legacy", "last_assistant_message": "いいですね"}, self.tmpdir)
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.ledger.tail(1)[0]["user_text"], "猫が好き")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "続けて", "session_id": "s-legacy"}, self.tmpdir)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        # 現在の prompt に縁 (猫) は無いので、発芽したなら旧キャッシュの prompt が
        # 文脈窓として渡ったということ。
        self.assertIn("【この入力に触れた記録】", ctx)
        self.assertEqual(hooks._read_session_cache(self.tmpdir, "s-legacy")["recent"], ["猫が好き"])

    def test_private_block_never_reaches_cache_or_recall(self):
        # 処方 6: `<private>` の中身はキャッシュ (prompt/recent = stop が台帳へ書く本文と次の
        # 文脈窓) にも想起の照合にも使わない。縁「秘密」の種子は発芽しない。
        self._plant("秘密が好き", "秘密")
        for prompt in ("全く別の話題 <private>秘密</private>", "続けて"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hooks.cmd_recall({"prompt": prompt, "session_id": "s-priv"}, self.tmpdir)
            self.assertNotIn("【この入力に触れた記録】", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        cache = hooks._read_session_cache(self.tmpdir, "s-priv")
        self.assertEqual(cache["prompt"], "続けて")
        self.assertEqual(cache["recent"], ["全く別の話題"])
        with open(os.path.join(hooks._sessions_dir(self.tmpdir), "s-priv.json"), encoding="utf-8") as f:
            self.assertNotIn("秘密", f.read())

    def test_same_prompt_without_private_tags_does_germinate(self):
        # 上の対照: タグが無ければ同じ語で発芽する (= 上のテストは除去を見ている)。
        self._plant("秘密が好き", "秘密")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "全く別の話題 秘密", "session_id": "s-pub"}, self.tmpdir)
        self.assertIn("【この入力に触れた記録】", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])

    def test_corrupt_cache_with_non_string_prompt_does_not_break_recall(self):
        # 失敗経路: 手書き/壊れたキャッシュで prompt が文字列でなくても、文脈窓の
        # 組み立てで例外にしない (engine.recall の context は str の列でなければ
        # TypeError になるため、非 str は窓へ入れずに落とす)。
        sdir = hooks._sessions_dir(self.tmpdir)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "s6.json"), "w", encoding="utf-8") as f:
            json.dump({"prompt": 12345, "ts": 0, "recent": ["普通の発言", 99, None]}, f)
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "次の発言です", "session_id": "s6"}, self.tmpdir)
        self.assertEqual(hooks._read_session_cache(self.tmpdir, "s6")["recent"], ["普通の発言"])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "hooks.log")))


class TestStopRecordsLedgerAndConsolidates(_HooksTestCase):
    """Stop = 台帳追記のみ (LLM 非依存)、CONSOLIDATE_EVERY 往復ごとに統合 (改修4 §3/§7/§8)。

    `_turn` / `_fill_window` / `_log_text` は `_HooksTestCase` の共通ヘルパ。
    """

    def test_consolidate_every_does_not_exceed_engine_window_max(self):
        # 1 回の統合が読む往復は engine.CONSOLIDATE_WINDOW_MAX 件まで (残りは次回が拾う)。
        # CONSOLIDATE_EVERY がこれを超えると、毎回の統合で必ず消費しきれない往復が
        # 溜まり続ける (pending が 0 に戻らない)。
        self.assertLessEqual(hooks.CONSOLIDATE_EVERY, engine.CONSOLIDATE_WINDOW_MAX)

    def test_stop_appends_ledger_without_calling_backend(self):
        with mock.patch("raiya.backend.call_llm", side_effect=AssertionError("Stop は LLM を呼ばない")):
            self._turn("s1", "猫が好きです/", "いいですね")
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["ledger"], {"turns": 1, "consolidated_upto": 0, "pending": 1})
        self.assertIn("session=s1 ledger_id=1 pending=1\n", self._log_text())
        self.assertNotIn("猫が好き", self._log_text())

    def test_stop_strips_private_before_truncating_and_keeps_paste_in_record(self):
        # 処方 6/8: `<private>` は切り詰めの**前**に除く。開きタグが省略側に落ち閉じタグが
        # 末尾側に残る長さにしてある -- 切ってから除くと塊が一致せず、末尾側の中身が残る。
        # 処方 5: 貼り付けは記録 (user_text) に残し、根拠 (grounding_text) からだけ剥がす。
        limit = hooks.TRUNCATE_LIMIT
        secret = "<private>" + "秘" * limit + "</private>"
        paste = '<pasted_content id="p1">\n貼った本文\n</pasted_content id="p1">'
        user = "打った文 " + "前" * (limit // 2) + secret + paste
        self._turn("s-private", user, "応答 <private>秘密の復唱</private>")
        row = MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]
        self.assertNotIn("秘", row["user_text"])
        self.assertNotIn("秘", row["assistant_text"])
        self.assertIn(paste, row["user_text"])
        self.assertNotIn("貼った本文", row["grounding_text"])
        self.assertTrue(row["grounding_text"].startswith("打った文 "))

    def _consolidate_spans(self, spans):
        """記帳済みの台帳を、span ごとに 1 提案の統合にかけて決定を返す。"""
        raw = json.dumps([{"content": "性向", "conditions": [c], "polarity": "無記","kind": "所在",
                           "evidence": [{"source": "user", "span": sp}]} for sp, c in spans], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            result = MemoryEngine(state_dir=self.tmpdir, new_life=False).consolidate()
        return [d["decision"] for d in result["decisions"]]

    def test_two_long_pastes_do_not_leak_into_grounding_through_truncation(self):
        # QA 実測の穴: 切ってから剥がすと A の閉じと B の開きが省略側に落ち、両方の貼り付けが
        # 根拠に残って圓成實になった。根拠は切る前に剥がす。
        paste_a = '<pasted_content id="a">\n' + "甲" * 6000 + '\n</pasted_content id="a">'
        paste_b = '<pasted_content id="b">\n' + "乙" * 6000 + '\n</pasted_content id="b">'
        self._turn("s-2paste", "打った指示です p/\n\n" + paste_a + "\n" + paste_b, "了解しました")
        row = MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]
        self.assertIn("<elided", row["user_text"])  # 記録は貼り付け込みで切り詰められている
        self.assertEqual(row["grounding_text"], "打った指示です p/")
        self.assertEqual(
            self._consolidate_spans([("甲甲甲甲", "甲"), ("乙乙乙乙", "乙"), ("打った指示です p/", "指示")]),
            ["遍計所執", "遍計所執", "圓成實"],
        )

    def test_paste_followed_by_long_typed_text_keeps_only_typed_text_as_grounding(self):
        paste = '<pasted_content id="p">\n' + "貼" * 10000 + '\n</pasted_content id="p">'
        typed_tail = "了解、続けて p/" + "打" * 5000
        self._turn("s-tail", "前置き\n\n" + paste + "\n" + typed_tail, "はい")
        row = MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]
        self.assertNotIn("貼", row["grounding_text"])
        self.assertEqual(
            self._consolidate_spans([("貼貼貼貼", "貼"), ("了解、続けて p/", "続け")]),
            ["遍計所執", "圓成實"],
        )

    def test_stop_grounding_may_exceed_truncated_record(self):
        # 意図した性質 (README/CLAUDE.md に明記): 根拠は切る前の貼り付け抜きの本文から作る
        # ので、記録 (user_text) の省略側に落ちた打った文も根拠には残り、圓成實に接地する。
        # 監査は user_text だけでなく grounding_text も見ること。
        paste = '<pasted_content id="p">\n' + "貼" * 4000 + '\n</pasted_content id="p">'
        typed = "打" * 500 + "真ん中の大事な一文 p/" + "打" * 4500
        self._turn("s-exceed", paste + "\n" + typed, "了解しました")
        row = MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]
        self.assertIn("<elided", row["user_text"])
        self.assertNotIn("真ん中の大事な一文 p/", row["user_text"])
        self.assertIn("真ん中の大事な一文 p/", row["grounding_text"])
        self.assertEqual(self._consolidate_spans([("真ん中の大事な一文 p/", "大事")]), ["圓成實"])

    def test_stop_consolidates_every_n_turns(self):
        raw = json.dumps([{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                           "evidence": [{"source": "user", "span": "猫が好きです/"}]}], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw) as call:
            for i in range(hooks.CONSOLIDATE_EVERY):
                self._turn("s2", "猫が好きです/ {}".format(i), "そうですね")
        self.assertEqual(call.call_count, 1)
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["seed_count"], 1)
        self.assertEqual(eng.audit()["ledger"]["pending"], 0)
        log = self._log_text()
        self.assertIn(
            "session=s2 consolidate window={} sessions=1 proposals=1 圓成實=1 依他起=0 遍計所執=0 reject=0 supersede=0\n".format(
                hooks.CONSOLIDATE_EVERY
            ),
            log,
        )

    def test_consolidate_unparseable_output_logs_the_skip_and_keeps_the_window(self):
        # 行末の任意欄 ` skipped=unparseable` の証人 (already_consolidated と同じ形)。
        # 読み取れなかった窓は消費しないので pending は減らず、次の統合が読み直す。
        cut_off = '[{"content": "x", "evidence": [{"span": "'  # max_tokens で切れた出力
        with mock.patch("raiya.backend.call_llm", return_value=cut_off):
            for i in range(hooks.CONSOLIDATE_EVERY):
                self._turn("s-unparseable", "猫が好きです/ {}".format(i), "そうですね")
        log = self._log_text()
        self.assertIn(
            "session=s-unparseable consolidate window={} sessions=1 proposals=0 "
            "圓成實=0 依他起=0 遍計所執=0 reject=0 supersede=0 skipped=unparseable\n".format(
                hooks.CONSOLIDATE_EVERY
            ),
            log,
        )
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["ledger"]["pending"], hooks.CONSOLIDATE_EVERY)
        self.assertEqual(eng.audit()["seed_count"], 0)

    def test_consolidate_line_reports_how_many_sessions_the_window_mixes(self):
        """`sessions=` の証人 (七文書 §2.3 同依)。先頭の `session=` は統合を起動した
        セッションだけなので、別セッションの往復が窓に混ざっていても読み取れない。
        2 セッションを混ぜて sessions=2 を pin する (sessions=1 の固定値 mutant を落とす)。
        行は ledger_id/件数/session id だけで、会話本文は載らない (§10.3d)。"""
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            for i in range(hooks.CONSOLIDATE_EVERY - 1):
                self._turn("s-other", "猫が好きです/ {}".format(i), "そうですね")
            self._turn("s-last", "犬も好きです", "そうですね")
        log = self._log_text()
        self.assertIn(
            "session=s-last consolidate window={} sessions=2 proposals=0 "
            "圓成實=0 依他起=0 遍計所執=0 reject=0 supersede=0\n".format(hooks.CONSOLIDATE_EVERY),
            log,
        )
        self.assertNotIn("犬も好きです", log)

    def test_audit_log_check_agrees_with_the_stop_hook_line(self):
        """stop の `ledger_id=` 行を audit の log_check が実物 (台帳の最終 id) と照合できること
        (七文書 §3.3)。ログの書式と audit の読み取りがずれると 記録なし になって落ちる。"""
        self._turn("s-check", "猫が好きです/", "そうですね")
        self._turn("s-check", "犬も好きです", "そうですね")
        check = MemoryEngine(state_dir=self.tmpdir, new_life=False).audit()["log_check"]
        self.assertEqual(check, {"status": "一致", "log_ledger_id": 2, "ledger_last_id": 2})

    def test_stuck_window_retries_on_every_stop(self):
        """読み取れない出力が続く窓は「5 往復に 1 回」ではなく **Stop のたびに** 再試行
        される (pending が CONSOLIDATE_EVERY 未満へ戻らないため)。有界なのはプロンプト
        の大きさ (CONSOLIDATE_WINDOW_MAX) だけで、呼び出し回数は改修4 以前の
        「毎往復 propose」と同じ = README のコスト記述はこの事実に合わせてある。
        """
        cut_off = '[{"content": "x", "evidence": [{"span": "'  # 常に読み取れない出力
        turns = hooks.CONSOLIDATE_EVERY + 3
        with mock.patch("raiya.backend.call_llm", return_value=cut_off) as call:
            for i in range(turns):
                self._turn("s-stuck", "猫が好きです/ {}".format(i), "そうですね")
        # 5・6・7・8 往復目の Stop で 1 回ずつ (= 往復数 - CONSOLIDATE_EVERY + 1)。
        self.assertEqual(call.call_count, turns - hooks.CONSOLIDATE_EVERY + 1)
        self.assertEqual(call.call_count, 4)
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["ledger"]["pending"], turns)  # 窓は消費されないまま

    def test_harness_injected_turn_is_never_recorded_in_the_ledger(self):
        # 改修4 §8 の動機そのもの: harness 注入は「ユーザーが言ったこと」ではないので
        # 台帳にも載せない (載せれば統合で圓成實の種になりうる)。
        with mock.patch("raiya.backend.call_llm", side_effect=AssertionError("LLM は呼ばない")):
            self._turn("s-harness", "<system-reminder>これは通知であって発話ではない</system-reminder>", "了解です")
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.audit()["ledger"], {"turns": 0, "consolidated_upto": 0, "pending": 0})
        log = self._log_text()
        self.assertIn("session=s-harness skip:", log)
        self.assertNotIn("これは通知であって発話ではない", log)

    def test_session_end_consolidates_pending_and_is_silent_when_empty(self):
        raw = json.dumps([{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                           "evidence": [{"source": "user", "span": "猫が好きです/"}]}], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw) as call:
            self._turn("s3", "猫が好きです/", "そうですね")
            hooks.cmd_session_end({"session_id": "s3", "reason": "exit"}, self.tmpdir)
            hooks.cmd_session_end({"session_id": "s3", "reason": "exit"}, self.tmpdir)  # 窓が空
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self._log_text().count("consolidate window="), 1)

    def test_session_end_on_a_fresh_state_dir_is_silent_and_calls_no_backend(self):
        # 失敗経路 (というより「何も無い」経路): 台帳が空なら例外も stdout も
        # hooks.log も出さず、backend も読まない (engine.consolidate の skipped="empty")。
        out = io.StringIO()
        with mock.patch("raiya.backend.call_llm", side_effect=AssertionError("窓が空なら LLM は呼ばない")):
            with contextlib.redirect_stdout(out):
                hooks.cmd_session_end({"session_id": "s-empty", "reason": "clear"}, self.tmpdir)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "hooks.log")))

    def test_consolidate_backend_error_is_logged_bounded_and_swallowed(self):
        from raiya.backend import BackendError

        with mock.patch("raiya.backend.call_llm", side_effect=BackendError("HTTP 401", detail="secret body")):
            for i in range(hooks.CONSOLIDATE_EVERY):
                self._turn("s4", "猫が好きです/ {}".format(i), "そうですね")
        log = self._log_text()
        self.assertIn("session=s4 consolidate失敗: BackendError: HTTP 401 (detail_len=11)", log)
        self.assertNotIn("secret body", log)

    def test_consolidate_parse_notes_are_logged_without_llm_output(self):
        with mock.patch("raiya.backend.call_llm", return_value="SECRET not json"):
            for i in range(hooks.CONSOLIDATE_EVERY):
                self._turn("s5", "猫が好きです/ {}".format(i), "そうですね")
        log = self._log_text()
        self.assertIn("session=s5 parse_notes:", log)
        self.assertNotIn("SECRET", log)

    def test_consolidate_failure_via_session_end_is_logged_bounded_and_silent(self):
        from raiya.backend import BackendError

        self._turn("s6", "猫が好きです/", "そうですね")
        out = io.StringIO()
        with mock.patch("raiya.backend.call_llm", side_effect=BackendError("HTTP 401", detail="secret body")):
            with contextlib.redirect_stdout(out):
                hooks.cmd_session_end({"session_id": "s6", "reason": "logout"}, self.tmpdir)
        self.assertEqual(out.getvalue(), "")  # SessionEnd は stdout を出さない
        log = self._log_text()
        self.assertIn("session=s6 consolidate失敗: BackendError: HTTP 401 (detail_len=11)", log)
        self.assertNotIn("secret body", log)

    def test_main_dispatches_session_end(self):
        self.assertIn("session_end", hooks.SUBCOMMANDS)


class TestPartBStopSkipsAndSubagentGuard(_HooksTestCase):
    """設計書 §10.6 処方 2/7/9: 台帳へ入れない往復と、サブエージェントの hook の扱い。"""

    def _ledger_turns(self):
        return MemoryEngine(state_dir=self.tmpdir, new_life=False).audit()["ledger"]["turns"]

    # --- 処方 2: 応答が空の往復 ---
    def test_empty_assistant_text_is_not_recorded_and_is_logged(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "委任の指示文です", "session_id": "s-empty"}, self.tmpdir)
        hooks.cmd_stop({"session_id": "s-empty", "last_assistant_message": "  "}, self.tmpdir)
        self.assertEqual(self._ledger_turns(), 0)
        self.assertIn("session=s-empty skip: assistant_text が空\n", self._log_text())
        self.assertNotIn("委任", self._log_text())

    def test_private_only_assistant_text_counts_as_empty(self):
        self._turn("s-aprv", "猫が好きです/", "<private>復唱</private>")
        self.assertEqual(self._ledger_turns(), 0)

    def test_non_empty_assistant_text_is_recorded(self):
        self._turn("s-ok", "猫が好きです/", "いいですね")
        self.assertEqual(self._ledger_turns(), 1)

    # --- 処方 7: 除去の後で skip 判定 ---
    def test_paste_only_prompt_is_not_recorded(self):
        paste = '\n\n<pasted_content id="p">\n再開ポイントを保存しました\n</pasted_content id="p">'
        self._turn("s-paste", paste, "了解しました")
        self.assertEqual(self._ledger_turns(), 0)
        self.assertIn("session=s-paste skip: 貼り付け/private を除いた user_text", self._log_text())

    def test_private_only_prompt_in_cache_is_not_recorded_by_stop(self):
        # recall を通らずに残ったキャッシュ (旧版が書いた等) でも stop 側で独立に落とす。
        hooks._write_session_cache(self.tmpdir, "s-uprv", "<private>秘密の話</private>")
        hooks.cmd_stop({"session_id": "s-uprv", "last_assistant_message": "はい"}, self.tmpdir)
        self.assertEqual(self._ledger_turns(), 0)
        self.assertNotIn("秘密", self._log_text())

    def test_harness_block_revealed_after_stripping_a_paste_is_not_recorded(self):
        text = '<pasted_content id="p">x</pasted_content id="p">\n<system-reminder>注入</system-reminder>'
        hooks._write_session_cache(self.tmpdir, "s-hx", text)
        hooks.cmd_stop({"session_id": "s-hx", "last_assistant_message": "はい"}, self.tmpdir)
        self.assertEqual(self._ledger_turns(), 0)

    def test_typed_text_with_a_paste_is_recorded(self):
        paste = '<pasted_content id="p">\n手順書の本文\n</pasted_content id="p">'
        self._turn("s-typed", "これを読んで\n\n" + paste, "読みました")
        row = MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]
        self.assertEqual((row["grounding_text"], row["user_text"]), ("これを読んで", "これを読んで\n\n" + paste))

    # --- 処方 9: サブエージェント (agent_id) ---
    def test_subagent_recall_neither_writes_cache_nor_outputs_nor_advances_life(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "サブエージェントへの委任文", "session_id": "s-sub",
                              "agent_id": "agent-1"}, self.tmpdir)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_subagent_recall_does_not_overwrite_parent_cache_and_parent_stop_records_parent_prompt(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "親の質問です", "session_id": "s-par"}, self.tmpdir)
            hooks.cmd_recall({"prompt": "サブエージェントへの委任文", "session_id": "s-par",
                              "agent_id": "agent-1"}, self.tmpdir)
        hooks.cmd_stop({"session_id": "s-par", "last_assistant_message": "回答", "agent_id": "agent-1"}, self.tmpdir)
        self.assertEqual(self._ledger_turns(), 0)
        self.assertIn("session=s-par skip: サブエージェント (agent_id)\n", self._log_text())
        hooks.cmd_stop({"session_id": "s-par", "last_assistant_message": "回答"}, self.tmpdir)
        row = MemoryEngine(state_dir=self.tmpdir, new_life=False).ledger.tail(1)[0]
        self.assertEqual(row["user_text"], "親の質問です")

    def test_empty_or_missing_agent_id_is_the_main_session(self):
        self.assertFalse(hooks._is_subagent_payload({}))
        self.assertFalse(hooks._is_subagent_payload({"agent_id": ""}))
        self.assertFalse(hooks._is_subagent_payload({"agent_id": None}))
        self.assertFalse(hooks._is_subagent_payload({"agent_id": "  "}))
        self.assertTrue(hooks._is_subagent_payload({"agent_id": "a1"}))
        self.assertTrue(hooks._is_subagent_payload({"agent_id": 7}))  # 壊れた値は記録しない側
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "猫が好きです/", "session_id": "s-main", "agent_id": ""}, self.tmpdir)
        hooks.cmd_stop({"session_id": "s-main", "last_assistant_message": "はい", "agent_id": ""}, self.tmpdir)
        self.assertEqual(self._ledger_turns(), 1)


class TestHooksFailSoftBranches(_HooksTestCase):
    """Issue #13 重要な未カバー 4-7(hooks): 型違いの入力・書込失敗・想定外例外でも
    落ちず、hooks.log に例外メッセージを書かない。"""

    def _run_main_with_stdin(self, sub, stdin_text):
        os.environ["RAIYA_STATE_DIR"] = self.tmpdir
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        os.environ.pop("RAIYA_HOOKS", None)
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                hooks.main([sub])
        finally:
            sys.stdin = original_stdin
        return out.getvalue()

    def test_stdin_json_list_is_treated_as_empty_payload(self):
        log_path = os.path.join(self.tmpdir, "hooks.log")
        for sub in ("recall", "stop"):
            with self.subTest(sub=sub):
                out = self._run_main_with_stdin(sub, '["SENTINEL_LIST_本文", "猫が好き"]')
                self.assertEqual(out, "")
                self.assertFalse(os.path.exists(hooks._sessions_dir(self.tmpdir)))
                self.assertEqual(ledger_rows(self.tmpdir), [])  # 台帳は空 (空 payload でも engine は raiya.db を作りうる)
                if sub == "recall":
                    self.assertFalse(os.path.exists(log_path))
                else:
                    self.assertIn("skip:", self._log_text())  # 空 payload は skip: 行で記録
                if os.path.exists(log_path):
                    log_text = self._log_text()
                    self.assertNotIn("失敗", log_text)
                    self.assertNotIn("SENTINEL_LIST", log_text)
                    self.assertNotIn("猫が好き", log_text)

    def test_extract_text_from_content_non_str_non_list_is_empty(self):
        for content in (123, None, {"type": "text", "text": "x"}, 1.5, True):
            with self.subTest(content=content):
                self.assertEqual(hooks._extract_text_from_content(content), "")

    def test_is_real_user_entry_non_dict_is_falsy(self):
        for entry in (None, "user", ["user"], 3):
            with self.subTest(entry=entry):
                self.assertFalse(hooks._is_real_user_entry(entry))

    def test_log_does_not_raise_when_hooks_log_cannot_be_opened(self):
        with mock.patch("builtins.open", side_effect=OSError("SENTINEL_LOG_本文")):
            hooks._log(self.tmpdir, "msg")  # 例外が出ないこと
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "hooks.log")))

    def test_write_session_cache_does_not_raise_on_oserror(self):
        with mock.patch("builtins.open", side_effect=OSError("SENTINEL_CACHE_本文")):
            hooks._write_session_cache(self.tmpdir, "sess-oserr", "こんにちは")
        self.assertIsNone(hooks._read_session_cache_prompt(self.tmpdir, "sess-oserr"))

    def test_write_session_cache_does_not_raise_when_makedirs_fails(self):
        with mock.patch("os.makedirs", side_effect=PermissionError("denied")):
            hooks._write_session_cache(self.tmpdir, "sess-mk", "こんにちは")
        self.assertIsNone(hooks._read_session_cache_prompt(self.tmpdir, "sess-mk"))

    def test_prune_sessions_does_not_raise_when_listdir_fails(self):
        with mock.patch("os.listdir", side_effect=OSError("boom")):
            hooks._prune_sessions(os.path.join(self.tmpdir, "sessions"))

    def test_prune_sessions_continues_when_remove_fails(self):
        sdir = hooks._sessions_dir(self.tmpdir)
        os.makedirs(sdir)
        for i in range(hooks.SESSIONS_MAX_KEEP + 3):
            path = os.path.join(sdir, "s{}.json".format(i))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"prompt": "p", "ts": i}, f)
            os.utime(path, (i, i))
        with mock.patch("os.remove", side_effect=OSError("boom")) as rm:
            hooks._prune_sessions(sdir)  # 例外が出ず、全ての古い行を試す
        self.assertEqual(rm.call_count, 3)

    def test_recall_continues_when_session_cache_write_fails(self):
        os.environ["RAIYA_STATE_DIR"] = self.tmpdir
        with mock.patch("builtins.open", side_effect=_selective_open_failure(
                os.path.join(self.tmpdir, "sessions"))):
            with contextlib.redirect_stdout(io.StringIO()):
                hooks.cmd_recall({"prompt": "猫が好きです/", "session_id": "s-cont"}, self.tmpdir)
        self.assertIsNone(hooks._read_session_cache_prompt(self.tmpdir, "s-cont"))
        # 書込失敗後も recall 本体は進み、末那識の注入ログに 1 行残る
        self.assertEqual(len(MemoryEngine(state_dir=self.tmpdir, new_life=False).injection_log(5)), 1)

    def test_main_exits_zero_when_main_impl_raises_unexpectedly(self):
        with mock.patch.object(hooks, "_main_impl", side_effect=RuntimeError("SENTINEL_MAIN_本文")):
            self.assertIsNone(hooks.main(["recall"]))  # 例外が外へ出ない

    def test_main_as_subprocess_exits_zero_when_main_impl_raises(self):
        code = (
            "import sys; from raiya.hooks import claude_code as h; "
            "h._main_impl = lambda argv: (_ for _ in ()).throw(RuntimeError('x')); "
            "h.main(['recall'])"
        )
        env = dict(os.environ)
        env["RAIYA_STATE_DIR"] = self.tmpdir
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env=env, cwd=REPO_ROOT, timeout=30)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


def _selective_open_failure(blocked_prefix):
    """blocked_prefix 配下のファイルを開くときだけ OSError にする open の代替。"""
    real_open = open

    def _open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file).startswith(blocked_prefix):
            raise OSError("SENTINEL_OPEN_本文")
        return real_open(file, *args, **kwargs)

    return _open


if __name__ == "__main__":
    unittest.main()
