import io
import json
import os
import shutil
import stat
import sys
import tempfile
import itertools
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import backend  # noqa: E402


class TestBackendCommandMode(unittest.TestCase):
    """command backend の統合テスト (cat / 固定応答スクリプト、LLM 実呼び出し無し)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_environ = dict(os.environ)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    def _write_script(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC)
        return path

    def test_command_backend_echoes_fixed_response(self):
        script = self._write_script(
            "dummy_llm.sh",
            "#!/bin/sh\ncat > /dev/null\necho '固定応答です'\n",
        )
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        result = backend.call_llm("こんにちは")
        self.assertEqual(result, "固定応答です")

    def test_command_backend_passes_stdin_through_cat(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "cat"
        result = backend.call_llm("そのまま返って来い")
        self.assertEqual(result, "そのまま返って来い")

    def test_command_backend_missing_env_raises(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ.pop("RAIYA_LLM_CMD", None)
        with self.assertRaises(backend.BackendError):
            backend.call_llm("テスト")

    def test_command_backend_nonzero_exit_raises(self):
        script = self._write_script("fail.sh", "#!/bin/sh\ncat > /dev/null\nexit 1\n")
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        with self.assertRaises(backend.BackendError):
            backend.call_llm("テスト")

    def test_command_backend_nonzero_exit_separates_reason_from_detail(self):
        # stderr の本文 (会話やシークレットを含みうる) が BackendError.reason に
        # 混入しないこと (記憶機能設計書§10.3d)。2行に分けて出すことで、stderr 内部の
        # 改行が reason 側 (1行の定型文) を汚さず detail 側にだけ現れることも確かめる。
        script = self._write_script(
            "fail_with_sentinel.sh",
            "#!/bin/sh\n"
            "cat > /dev/null\n"
            "echo 'SENTINEL_STDERR_L1_これは秘密の本文の一行目です' >&2\n"
            "echo 'SENTINEL_STDERR_L2_これは秘密の本文の二行目です' >&2\n"
            "exit 1\n",
        )
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = script
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        expected_detail = (
            "SENTINEL_STDERR_L1_これは秘密の本文の一行目です\n"
            "SENTINEL_STDERR_L2_これは秘密の本文の二行目です"
        )
        self.assertEqual(
            e.reason,
            "LLM 呼び出しに失敗しました (backend=command): command backend が失敗しました",
        )
        self.assertEqual(e.detail, expected_detail)
        self.assertNotIn("SENTINEL_STDERR", e.reason)
        self.assertEqual(str(e), e.reason + ": " + e.detail)

    def test_default_backend_is_openai_api(self):
        # RAIYA_BACKEND 未設定なら openai-api (= gpt-6-luna) に落ちること。
        os.environ.pop("RAIYA_BACKEND", None)
        os.environ.pop("RAIYA_OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        self.assertEqual(backend.DEFAULT_BACKEND, "openai-api")
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_unknown_backend_raises(self):
        os.environ["RAIYA_BACKEND"] = "存在しないbackend"
        with self.assertRaises(backend.BackendError):
            backend.call_llm("テスト")

    def test_anthropic_api_backend_missing_key_raises(self):
        os.environ["RAIYA_BACKEND"] = "anthropic-api"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(backend.BackendError):
            backend.call_llm("テスト")


class _FakeResponse:
    """urllib.request.urlopen の戻り値 (context manager) の最小代用。"""

    def __init__(self, body):
        self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))

    def __enter__(self):
        return self._buf

    def __exit__(self, *exc):
        return False


class TestOpenAiApiBackend(unittest.TestCase):
    """openai-api backend (既定 backend) の pin。実 API は呼ばない
    (urllib.request.urlopen を差し替える)。"""

    def setUp(self):
        self._saved_environ = dict(os.environ)
        self._saved_urlopen = backend.urllib.request.urlopen
        self.requests = []
        self.response_body = {
            "choices": [{"message": {"content": "  応答です  "}, "finish_reason": "stop"}]
        }

        def _fake_urlopen(req, timeout=None):
            self.requests.append(
                {
                    "url": req.full_url,
                    "headers": dict(req.header_items()),
                    "payload": json.loads(req.data.decode("utf-8")),
                }
            )
            return _FakeResponse(self.response_body)

        backend.urllib.request.urlopen = _fake_urlopen
        os.environ["RAIYA_BACKEND"] = "openai-api"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ.pop("RAIYA_OPENAI_API_KEY", None)
        os.environ.pop("RAIYA_MODEL", None)
        os.environ.pop("RAIYA_PROPOSE_MODEL", None)

    def tearDown(self):
        backend.urllib.request.urlopen = self._saved_urlopen
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    def test_missing_key_raises(self):
        os.environ.pop("OPENAI_API_KEY", None)
        with self.assertRaises(backend.BackendError):
            backend.call_llm("テスト")

    def test_raiya_openai_api_key_takes_precedence(self):
        os.environ["RAIYA_OPENAI_API_KEY"] = "sk-raiya"
        backend.call_llm("テスト")
        self.assertEqual(self.requests[0]["headers"]["Authorization"], "Bearer sk-raiya")

    def test_returns_stripped_message_content(self):
        self.assertEqual(backend.call_llm("テスト"), "応答です")
        self.assertEqual(self.requests[0]["url"], backend.OPENAI_API_URL)

    def test_propose_purpose_uses_luna_by_default(self):
        backend.call_llm("テスト", purpose="propose")
        self.assertEqual(self.requests[0]["payload"]["model"], "gpt-6-luna")
        self.assertEqual(backend.DEFAULT_OPENAI_API_MODEL, "gpt-6-luna")

    def test_propose_purpose_respects_raiya_propose_model_override(self):
        os.environ["RAIYA_PROPOSE_MODEL"] = "gpt-5.4-mini"
        os.environ["RAIYA_MODEL"] = "gpt-5.5"  # 対話本文用は propose に影響しない
        backend.call_llm("テスト", purpose="propose")
        self.assertEqual(self.requests[0]["payload"]["model"], "gpt-5.4-mini")

    def test_non_propose_call_uses_raiya_model_then_default(self):
        backend.call_llm("テスト")
        self.assertEqual(self.requests[0]["payload"]["model"], "gpt-6-luna")
        os.environ["RAIYA_MODEL"] = "gpt-5.5"
        os.environ["RAIYA_PROPOSE_MODEL"] = "gpt-5.4-mini"  # propose 用は本文に影響しない
        backend.call_llm("テスト")
        self.assertEqual(self.requests[1]["payload"]["model"], "gpt-5.5")

    def test_uses_max_completion_tokens_not_max_tokens(self):
        # 新しめの OpenAI モデルは max_tokens を拒否する。
        backend.call_llm("テスト", max_tokens=512)
        payload = self.requests[0]["payload"]
        # 呼び出し側の 512 は届かず、定数が送られる (reasoning tokens 対策, Issue #9)。
        self.assertEqual(payload["max_completion_tokens"], backend.OPENAI_MAX_COMPLETION_TOKENS)
        self.assertEqual(backend.OPENAI_MAX_COMPLETION_TOKENS, 64000)
        self.assertNotEqual(payload["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", payload)

    def test_empty_content_raises_backend_error(self):
        self.response_body = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        self.assertIn("length", str(ctx.exception))

    def test_empty_content_finish_reason_whitelist(self):
        # finish_reason は backend (server) が返す値なので、許可リスト内はそのまま
        # reason に出るが、リスト外の値は "other" に丸められ生値は reason に出ない
        # (記憶機能設計書§10.3d)。許可リスト外の生値は detail 側へ退避される
        # (cli.py の str(e) 表示では追えるが、hooks.log には detail_len しか出ない)。
        # 許可リスト内 (length) と None は detail が空のまま (str(e) が従来と同じ)。
        # 許可リスト 5 値は全てリテラルで書く (backend.FINISH_REASONS を回すと自己参照
        # フィクスチャになり、リストを縮める変異が生き残る=team-lessons)。
        cases = [
            ("length", "finish_reason=length", None, ""),
            ("stop", "finish_reason=stop", None, ""),
            ("content_filter", "finish_reason=content_filter", None, ""),
            ("tool_calls", "finish_reason=tool_calls", None, ""),
            ("function_call", "finish_reason=function_call", None, ""),
            (
                "SENTINEL_FR_未知の値",
                "finish_reason=other",
                "SENTINEL_FR_未知の値",
                "SENTINEL_FR_未知の値",
            ),
            (None, "finish_reason=None", None, ""),
        ]
        for finish_reason, expected_fragment, forbidden_fragment, expected_detail in cases:
            with self.subTest(finish_reason=finish_reason):
                self.response_body = {
                    "choices": [{"message": {"content": ""}, "finish_reason": finish_reason}]
                }
                with self.assertRaises(backend.BackendError) as ctx:
                    backend.call_llm("テスト")
                e = ctx.exception
                self.assertIn(expected_fragment, e.reason)
                if forbidden_fragment is not None:
                    self.assertNotIn(forbidden_fragment, e.reason)
                if expected_detail:
                    self.assertIn(expected_detail, e.detail)
                else:
                    self.assertEqual(e.detail, "")

    def test_openai_finish_reason_length_with_partial_content_raises(self):
        # propose (熏習提案/統合) 経路では、部分出力があっても finish_reason=length を
        # 失敗にする (改修4 F1): 途中で切れた JSON をそのまま返すと
        # engine._parse_consolidation では「LLM が空配列を返した」(健全) と区別できず、
        # 統合の窓が消費されて内容ごと消える。
        partial = '[{"content": "x", "evidence": [{"span": "'
        self.response_body = {
            "choices": [{"message": {"content": partial}, "finish_reason": "length"}]
        }
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト", purpose="propose")
        e = ctx.exception
        self.assertIn("length", e.reason)
        self.assertNotIn("evidence", e.reason)  # 応答本文は reason に載せない (§10.3d)
        self.assertEqual(e.detail, partial)     # 本文は detail 側 (hooks.log は長さのみ)

    def test_openai_finish_reason_length_on_conversation_path_returns_partial(self):
        # 対話本文の生成・内観 (purpose なし) では切れた応答をそのまま返す: 人が読む
        # 文章は途中まででも使えるし、ここで失敗にすると call_llm の再試行で HTTP 呼び出し
        # が倍になったうえ部分的な回答まで捨ててしまう (JSON 契約なのは propose だけ)。
        self.response_body = {
            "choices": [{"message": {"content": "部分回答"}, "finish_reason": "length"}]
        }
        self.assertEqual(backend.call_llm("テスト"), "部分回答")
        self.assertEqual(len(self.requests), 1)  # 再試行していない

    def test_http_error_raises_backend_error_with_detail(self):
        def _raising_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                backend.OPENAI_API_URL, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
            )

        backend.urllib.request.urlopen = _raising_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        self.assertIn("401", str(ctx.exception))

    def test_url_error_separates_reason_from_detail(self):
        # 接続エラー文 (会話を含みうる) が reason に混入しないこと (記憶機能設計書§10.3d)。
        def _raising_urlopen(req, timeout=None):
            raise urllib.error.URLError("SENTINEL_URL_本文")

        backend.urllib.request.urlopen = _raising_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertEqual(
            e.reason,
            "LLM 呼び出しに失敗しました (backend=openai-api): openai-api への接続に失敗しました",
        )
        self.assertNotIn("SENTINEL_URL_本文", e.reason)
        self.assertIn("SENTINEL_URL_本文", e.detail)

    def test_missing_choices_separates_reason_from_detail(self):
        # choices 欠落時の応答 body 全体 (会話を含みうる) が reason に混入しない
        # こと (記憶機能設計書§10.3d)。
        self.response_body = {
            "choices": [],
            "error": {"message": "SENTINEL_NOCHOICES_本文"},
        }
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertNotIn("SENTINEL_NOCHOICES_本文", e.reason)
        self.assertIn("SENTINEL_NOCHOICES_本文", e.detail)

    def test_parse_failure_names_exception_class_in_reason(self):
        # 解析失敗サイトは str(例外) を reason へ埋め込まず、例外クラス名だけを
        # reason に載せ、生の例外文字列 (JSONDecodeError の位置情報等) は detail 側
        # へ回す (記憶機能設計書§10.3d)。
        class _RawResp:
            def __init__(self, raw):
                self._buf = io.BytesIO(raw)

            def __enter__(self):
                return self._buf

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=None):
            return _RawResp(b"not-json SENTINEL_PARSE")

        backend.urllib.request.urlopen = _fake_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertEqual(
            e.reason,
            "LLM 呼び出しに失敗しました (backend=openai-api): "
            "openai-api の応答の解析に失敗しました (JSONDecodeError)",
        )

    def test_read_timeout_becomes_backend_error_and_is_retried(self):
        # 応答待ちの素の TimeoutError が BackendError になり、call_llm の再試行に乗る。
        calls = []

        def _raising_urlopen(req, timeout=None):
            calls.append(timeout)
            raise TimeoutError("SENTINEL_TO_OAI")

        backend.urllib.request.urlopen = _raising_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("SENTINEL_PROMPT_本文")
        e = ctx.exception
        self.assertEqual(len(calls), backend.MAX_RETRIES + 1)
        self.assertEqual(calls, [backend.REQUEST_TIMEOUT_SEC] * (backend.MAX_RETRIES + 1))
        self.assertIn("タイムアウト", e.reason)
        self.assertIn(str(backend.REQUEST_TIMEOUT_SEC), e.reason)
        self.assertNotIn("SENTINEL_PROMPT_本文", e.reason)
        self.assertNotIn("SENTINEL_TO_OAI", e.reason)


class TestAnthropicApiBackend(unittest.TestCase):
    """anthropic-api backend の失敗パスの reason/detail 分離 (記憶機能設計書§10.3d)。
    実 API は呼ばない (urllib.request.urlopen を差し替える)。"""

    def setUp(self):
        self._saved_environ = dict(os.environ)
        self._saved_urlopen = backend.urllib.request.urlopen
        os.environ["RAIYA_BACKEND"] = "anthropic-api"
        os.environ["ANTHROPIC_API_KEY"] = "dummy"

    def tearDown(self):
        backend.urllib.request.urlopen = self._saved_urlopen
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    def test_sends_fixed_max_tokens_ignoring_callers(self):
        sent = []

        def _fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode("utf-8")))
            return _FakeResponse({"content": [{"type": "text", "text": "ok"}]})

        backend.urllib.request.urlopen = _fake_urlopen
        self.assertEqual(backend.call_llm("テスト", max_tokens=512), "ok")
        self.assertEqual(sent[0]["max_tokens"], backend.ANTHROPIC_MAX_TOKENS)
        self.assertNotIn("max_completion_tokens", sent[0])

    def test_http_error_separates_reason_from_detail(self):
        def _raising_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                backend.ANTHROPIC_API_URL,
                500,
                "Internal Server Error",
                {},
                io.BytesIO('{"error":"SENTINEL_ANT_本文"}'.encode("utf-8")),
            )

        backend.urllib.request.urlopen = _raising_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertIn("HTTP 500", e.reason)
        self.assertNotIn("SENTINEL_ANT_本文", e.reason)
        self.assertIn("SENTINEL_ANT_本文", e.detail)

    def test_url_error_separates_reason_from_detail(self):
        def _raising_urlopen(req, timeout=None):
            raise urllib.error.URLError("SENTINEL_ANT_URL_本文")

        backend.urllib.request.urlopen = _raising_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertEqual(
            e.reason,
            "LLM 呼び出しに失敗しました (backend=anthropic-api): "
            "anthropic-api への接続に失敗しました",
        )
        self.assertNotIn("SENTINEL_ANT_URL_本文", e.reason)
        self.assertIn("SENTINEL_ANT_URL_本文", e.detail)

    def test_parse_failure_names_exception_class_in_reason(self):
        class _RawResp:
            def __init__(self, raw):
                self._buf = io.BytesIO(raw)

            def __enter__(self):
                return self._buf

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=None):
            return _RawResp(b"not-json SENTINEL_ANT_PARSE")

        backend.urllib.request.urlopen = _fake_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertEqual(
            e.reason,
            "LLM 呼び出しに失敗しました (backend=anthropic-api): "
            "anthropic-api の応答の解析に失敗しました (JSONDecodeError)",
        )
        self.assertTrue(e.detail)

    def test_read_timeout_becomes_backend_error_and_is_retried(self):
        # 応答待ちの素の TimeoutError が BackendError になり、call_llm の再試行に乗る。
        calls = []

        def _raising_urlopen(req, timeout=None):
            calls.append(timeout)
            raise TimeoutError("SENTINEL_TO_ANT")

        backend.urllib.request.urlopen = _raising_urlopen
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("SENTINEL_PROMPT_本文")
        e = ctx.exception
        self.assertEqual(len(calls), backend.MAX_RETRIES + 1)
        self.assertEqual(calls, [backend.REQUEST_TIMEOUT_SEC] * (backend.MAX_RETRIES + 1))
        self.assertIn("タイムアウト", e.reason)
        self.assertIn(str(backend.REQUEST_TIMEOUT_SEC), e.reason)
        self.assertNotIn("SENTINEL_PROMPT_本文", e.reason)
        self.assertNotIn("SENTINEL_TO_ANT", e.reason)


class TestClaudeCliBackendProposeExtensions(unittest.TestCase):
    """claude-cli backend は全 purpose で再帰ガード env と --strict-mcp-config を付け
    (Issue #26)、完全な封じ込め (CLAUDE_CLI_PROPOSE_ISOLATION)・軽量モデル切替・
    --no-session-persistence は purpose="propose" のときだけ付けること (purpose 無指定は
    RAIYA_MODEL による既定モデルのまま) の pin。実 claude コマンドは
    呼ばない (subprocess.run を差し替える)。
    """

    def setUp(self):
        self._saved_environ = dict(os.environ)
        self._saved_subprocess_run = backend.subprocess.run
        self.calls = []

        class _FakeCompletedProcess:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(cmd, **kwargs):
            self.calls.append({"cmd": cmd, "env": kwargs.get("env"), "stdin": kwargs.get("stdin")})
            return _FakeCompletedProcess()

        backend.subprocess.run = _fake_run

    def tearDown(self):
        backend.subprocess.run = self._saved_subprocess_run
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    @staticmethod
    def _flag_value(cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_propose_purpose_isolates_child_from_tools_settings_mcp_and_skills(self):
        # 台帳の本文を載せる統合の子 claude は、道具・ユーザー設定 (hooks/plugins/CLAUDE.md)・
        # MCP・skills を持たない (§10.6 後の封じ込め)。値まで pin する: "--tools" だけ残って
        # 値が "default" に化けると全道具が戻る。
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        backend.call_llm("テスト", purpose="propose")
        cmd = self.calls[0]["cmd"]
        self.assertEqual(self._flag_value(cmd, "--tools"), "")
        self.assertEqual(self._flag_value(cmd, "--setting-sources"), "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertEqual(self._flag_value(cmd, "--permission-mode"), "dontAsk")
        self.assertNotIn("--bare", cmd)  # --bare は OAuth を読まない = 契約で動かなくなる
        self.assertIs(self.calls[0]["stdin"], backend.subprocess.DEVNULL)

    def test_conversational_call_has_guard_and_strict_mcp_but_no_full_isolation(self):
        # 対話本文・内観 (purpose 無指定) も claude -p は新しい Claude Code 起動 (Issue #26):
        # raiya の hooks を止める env と、MCP を持ち込まない --strict-mcp-config だけ付ける。
        # 完全な封じ込めは propose 専用のまま。
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        backend.call_llm("テスト")
        cmd = self.calls[0]["cmd"]
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(self.calls[0]["env"].get("RAIYA_HOOK_ACTIVE"), "1")
        for flag in ("--tools", "--setting-sources", "--disable-slash-commands", "--permission-mode"):
            self.assertNotIn(flag, cmd)

    def test_guard_env_is_idempotent_when_parent_already_set(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ["RAIYA_HOOK_ACTIVE"] = "1"
        backend.call_llm("テスト")
        self.assertEqual(self.calls[0]["env"].get("RAIYA_HOOK_ACTIVE"), "1")

    def test_other_non_propose_purpose_gets_guard_and_strict_mcp(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        backend.call_llm("テスト", purpose="introspect")
        cmd = self.calls[0]["cmd"]
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(self.calls[0]["env"].get("RAIYA_HOOK_ACTIVE"), "1")
        self.assertNotIn("--tools", cmd)

    def test_propose_purpose_adds_model_and_no_session_persistence_and_hook_active_env(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ.pop("RAIYA_PROPOSE_MODEL", None)  # 既定 haiku を使わせる
        os.environ.pop("RAIYA_HOOK_ACTIVE", None)
        result = backend.call_llm("テスト", purpose="propose")
        self.assertEqual(result, "ok")
        self.assertEqual(len(self.calls), 1)
        cmd = self.calls[0]["cmd"]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(self.calls[0]["env"].get("RAIYA_HOOK_ACTIVE"), "1")

    def test_propose_purpose_respects_raiya_propose_model_override(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ["RAIYA_PROPOSE_MODEL"] = "sonnet"
        result = backend.call_llm("テスト", purpose="propose")
        self.assertEqual(result, "ok")
        cmd = self.calls[0]["cmd"]
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_conversational_call_adds_no_model_or_session_flags_by_default(self):
        # purpose 無指定は --model / --no-session-persistence を付けない (RAIYA_MODEL 未設定時)。
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ.pop("RAIYA_MODEL", None)
        result = backend.call_llm("テスト")
        self.assertEqual(result, "ok")
        cmd = self.calls[0]["cmd"]
        self.assertNotIn("--no-session-persistence", cmd)
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--bare", cmd)

    def test_non_propose_call_still_honors_raiya_model(self):
        # 既存の RAIYA_MODEL 知恵 (対話本文用) は purpose 無指定のときだけ効く。
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        os.environ["RAIYA_MODEL"] = "opus"
        result = backend.call_llm("テスト")
        self.assertEqual(result, "ok")
        cmd = self.calls[0]["cmd"]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertNotIn("--no-session-persistence", cmd)


class TestClaudeCliBackendFailure(unittest.TestCase):
    """claude-cli backend の失敗パス (nonzero exit) の reason/detail 分離
    (記憶機能設計書§10.3d)。実 claude コマンドは呼ばない (subprocess.run を差し替える)。
    """

    def setUp(self):
        self._saved_environ = dict(os.environ)
        self._saved_subprocess_run = backend.subprocess.run

    def tearDown(self):
        backend.subprocess.run = self._saved_subprocess_run
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    def test_nonzero_exit_separates_reason_from_detail(self):
        class _FakeCompletedProcess:
            returncode = 1
            stdout = ""
            stderr = "SENTINEL_CLI_L1_秘密\nSENTINEL_CLI_L2_秘密\n"

        def _fake_run(cmd, **kwargs):
            return _FakeCompletedProcess()

        backend.subprocess.run = _fake_run
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        with self.assertRaises(backend.BackendError) as ctx:
            backend.call_llm("テスト")
        e = ctx.exception
        self.assertEqual(
            e.reason,
            "LLM 呼び出しに失敗しました (backend=claude-cli): claude -p が失敗しました",
        )
        self.assertEqual(e.detail, "SENTINEL_CLI_L1_秘密\nSENTINEL_CLI_L2_秘密")
        self.assertNotIn("SENTINEL_CLI", e.reason)


class TestSubprocessBackendErrorTranslation(unittest.TestCase):
    """claude-cli / command backend が subprocess 例外を BackendError に翻訳し、
    call_llm が MAX_RETRIES 回だけ再試行して BackendError で終わること。実プロセスは起こさない。"""

    def setUp(self):
        self._saved_environ = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    def test_claude_cli_file_not_found_becomes_backend_error(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        with mock.patch.object(backend.subprocess, "run", side_effect=FileNotFoundError("claude")):
            with self.assertRaises(backend.BackendError) as ctx:
                backend._call_claude_cli("テスト")
        self.assertEqual(ctx.exception.reason, "claude コマンドが見つかりません (Claude Code がインストールされていますか)")
        self.assertIsInstance(ctx.exception.__cause__, FileNotFoundError)

    def test_claude_cli_timeout_becomes_backend_error(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        exc = backend.subprocess.TimeoutExpired(cmd="claude", timeout=1)
        with mock.patch.object(backend.subprocess, "run", side_effect=exc):
            with self.assertRaises(backend.BackendError) as ctx:
                backend._call_claude_cli("テスト")
        self.assertEqual(ctx.exception.reason, "claude -p の呼び出しがタイムアウトしました")
        self.assertIs(ctx.exception.__cause__, exc)

    def test_command_timeout_becomes_backend_error(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "dummy-llm"
        exc = backend.subprocess.TimeoutExpired(cmd="dummy-llm", timeout=1)
        with mock.patch.object(backend.subprocess, "run", side_effect=exc):
            with self.assertRaises(backend.BackendError) as ctx:
                backend._call_command("テスト")
        self.assertEqual(ctx.exception.reason, "command backend の呼び出しがタイムアウトしました")
        self.assertIs(ctx.exception.__cause__, exc)

    def test_call_llm_retries_then_raises_backend_error(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        with mock.patch.object(backend.subprocess, "run", side_effect=FileNotFoundError("claude")) as run:
            with self.assertRaises(backend.BackendError) as ctx:
                backend.call_llm("テスト")
        self.assertEqual(run.call_count, backend.MAX_RETRIES + 1)
        self.assertEqual(
            ctx.exception.reason,
            "LLM 呼び出しに失敗しました (backend=claude-cli): claude コマンドが見つかりません (Claude Code がインストールされていますか)",
        )

    def test_call_llm_recovers_when_retry_succeeds(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "dummy-llm"

        class _Done:
            returncode = 0
            stdout = " 応答 \n"
            stderr = ""

        exc = backend.subprocess.TimeoutExpired(cmd="dummy-llm", timeout=1)
        with mock.patch.object(backend.subprocess, "run", side_effect=[exc, _Done()]) as run:
            self.assertEqual(backend.call_llm("テスト"), "応答")
        self.assertEqual(run.call_count, 2)


class TestBackendErrorClass(unittest.TestCase):
    """BackendError の reason/detail 分離と単一引数コンストラクタの後方互換性
    (記憶機能設計書§10.3d)。"""

    def test_single_arg_construction_is_backward_compatible(self):
        e = backend.BackendError("x")
        self.assertEqual(e.reason, "x")
        self.assertEqual(e.detail, "")
        self.assertEqual(str(e), "x")

    def test_two_arg_construction_combines_reason_and_detail_in_str(self):
        e = backend.BackendError("reason-text", detail="detail-text")
        self.assertEqual(e.reason, "reason-text")
        self.assertEqual(e.detail, "detail-text")
        self.assertEqual(str(e), "reason-text: detail-text")


class TestDescribeMatchesActualCall(unittest.TestCase):
    """`describe(purpose)` は実際に cmd / payload へ入るモデルと一致する (設計書 2026-10-23 §9.4・§9.9)。
    subprocess / urlopen は mock (通信しない)。"""

    ENV_VALUES = (None, "", "MODEL_X")

    def setUp(self):
        self._saved_environ = dict(os.environ)
        for key in [k for k in os.environ if k.startswith("RAIYA_")]:
            del os.environ[key]
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない (開発者の ~/.config へ落とさない)
        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["ANTHROPIC_API_KEY"] = "k"

    def tearDown(self):
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)

    def _set(self, model, propose_model):
        for key, value in (("RAIYA_MODEL", model), ("RAIYA_PROPOSE_MODEL", propose_model)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _actual_model(self, name, purpose):
        os.environ["RAIYA_BACKEND"] = name
        if name == "claude-cli":
            done = mock.Mock(returncode=0, stdout="ok", stderr="")
            with mock.patch.object(backend.subprocess, "run", return_value=done) as run:
                backend.call_llm("p", purpose=purpose)
            cmd = run.call_args[0][0]
            return cmd[cmd.index("--model") + 1] if "--model" in cmd else None
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["payload"] = json.loads(req.data.decode("utf-8"))
            body = ({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
                    if name == "openai-api" else {"content": [{"type": "text", "text": "ok"}]})
            return _Resp(body)

        with mock.patch.object(backend.urllib.request, "urlopen", fake_urlopen):
            backend.call_llm("p", purpose=purpose)
        return sent["payload"]["model"]

    def test_describe_equals_model_actually_used(self):
        for name in ("claude-cli", "openai-api", "anthropic-api"):
            for purpose in (None, "propose"):
                for model, propose_model in itertools.product(self.ENV_VALUES, self.ENV_VALUES):
                    self._set(model, propose_model)
                    os.environ["RAIYA_BACKEND"] = name
                    label = (name, purpose, model, propose_model)
                    self.assertEqual(backend.describe(purpose), (name, self._actual_model(name, purpose)), msg=label)

    def test_command_and_unknown_and_default_backend(self):
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_MODEL"] = "MODEL_X"
        self.assertEqual(backend.describe("propose"), ("command", None))
        os.environ["RAIYA_BACKEND"] = "nonexistent"
        self.assertEqual(backend.describe(), ("nonexistent", None))
        os.environ["RAIYA_BACKEND"] = ""
        # 空文字は「未設定」と同じ (設定ファイル設計書 §3: 唯一の挙動変化)。ファイル無しなら既定へ
        self.assertEqual(backend.describe(), (backend.DEFAULT_BACKEND, "MODEL_X"))
        del os.environ["RAIYA_BACKEND"]
        self.assertEqual(backend.describe(), (backend.DEFAULT_BACKEND, "MODEL_X"))

    def test_documented_defaults(self):
        os.environ["RAIYA_BACKEND"] = "claude-cli"
        self.assertEqual(backend.describe("propose")[1], "haiku")
        self.assertIsNone(backend.describe()[1])
        os.environ["RAIYA_BACKEND"] = "openai-api"
        self.assertEqual(backend.describe("propose")[1], backend.DEFAULT_OPENAI_API_MODEL)
        os.environ["RAIYA_BACKEND"] = "anthropic-api"
        os.environ["RAIYA_PROPOSE_MODEL"] = "ignored"
        self.assertEqual(backend.describe("propose")[1], backend.DEFAULT_ANTHROPIC_API_MODEL)


class _Resp:
    def __init__(self, body):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
