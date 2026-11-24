"""設定ファイル (config.yaml) と backend の配線 pin。
spec: docs/superpowers/specs/2026-10-23-config-file-design.md §3 / §4.2 / §5.2。
LLM は実際には呼ばない (urlopen / subprocess.run を差し替える)。"""
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

from raiya import backend, config  # noqa: E402

LLM_ENV = ("RAIYA_BACKEND", "RAIYA_MODEL", "RAIYA_PROPOSE_MODEL", "RAIYA_LLM_CMD",
           "RAIYA_OPENAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

OK_BODY = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "content": []}


class _Resp:
    def __init__(self, body):
        self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))

    def __enter__(self):
        return self._buf

    def __exit__(self, *exc):
        return False


class _Case(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg_path = os.path.join(self.root, "config.yaml")
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": self.cfg_path})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in LLM_ENV:
            os.environ.pop(k, None)
        config._reset()
        self.addCleanup(config._reset)

    def write_cfg(self, text):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(text)
        config._reset()


class TestPrecedence(_Case):
    def test_env_over_file_over_default(self):
        # ファイル無し (not_found) でも describe は既定へ落ちる
        self.assertEqual(backend.describe("propose"), ("openai-api", backend.DEFAULT_OPENAI_API_MODEL))
        self.write_cfg("llm:\n  backend: claude-cli\n  model: file-m\n  propose_model: file-pm\n")
        self.assertEqual(backend.describe("propose"), ("claude-cli", "file-pm"))
        self.assertEqual(backend.describe(), ("claude-cli", "file-m"))
        os.environ["RAIYA_BACKEND"] = "openai-api"
        os.environ["RAIYA_PROPOSE_MODEL"] = "env-pm"
        self.assertEqual(backend.describe("propose"), ("openai-api", "env-pm"))
        self.assertEqual(backend.describe(), ("openai-api", "file-m"))

    def test_empty_env_backend_falls_to_file(self):
        self.write_cfg("llm:\n  backend: anthropic-api\n")
        os.environ["RAIYA_BACKEND"] = ""
        self.assertEqual(backend.describe()[0], "anthropic-api")

    def test_describe_does_not_raise_on_issues(self):
        self.write_cfg("llm:\n  backnd: command\n  model: m\n")
        self.assertEqual(backend.describe(), ("openai-api", "m"))

    def test_file_backend_command_runs_and_env_wins(self):
        self.write_cfg("llm:\n  backend: command\n  command: 'cat'\n")
        self.assertEqual(backend.call_llm("hello"), "hello")
        os.environ["RAIYA_LLM_CMD"] = "echo from-env"
        self.assertEqual(backend.call_llm("hello"), "from-env")


class TestFileOnlyKnobs(_Case):
    def test_timeout_reaches_urlopen_and_both_subprocesses(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["http"] = timeout
            return _Resp(OK_BODY)

        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        for name, timeout in (("openai-api", 7), ("anthropic-api", 8.5)):
            self.write_cfg("llm:\n  backend: {}\n  timeout_sec: {}\n".format(name, timeout))
            with mock.patch.object(backend.urllib.request, "urlopen", fake_urlopen):
                backend.call_llm("p")
            self.assertEqual(seen["http"], timeout, name)

        for name, extra in (("claude-cli", ""), ("command", "  command: x\n")):
            self.write_cfg("llm:\n  backend: {}\n  timeout_sec: 9\n{}".format(name, extra))
            with mock.patch.object(backend.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="out", stderr="")
                backend.call_llm("p")
            self.assertEqual(run.call_args.kwargs["timeout"], 9, name)

    def test_default_timeout_is_the_constant_read_at_call_time(self):
        self.write_cfg("")
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "x"
        with mock.patch.object(backend.subprocess, "run") as run, \
                mock.patch.object(backend, "REQUEST_TIMEOUT_SEC", 33):
            run.return_value = mock.Mock(returncode=0, stdout="out", stderr="")
            backend.call_llm("p")
        self.assertEqual(run.call_args.kwargs["timeout"], 33)

    def test_max_completion_tokens_in_openai_payload_not_anthropic(self):
        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        payloads = {}

        def fake_urlopen(req, timeout=None):
            payloads[req.full_url] = json.loads(req.data.decode("utf-8"))
            return _Resp(OK_BODY)

        self.write_cfg("llm:\n  openai_max_completion_tokens: 1234\n")
        with mock.patch.object(backend.urllib.request, "urlopen", fake_urlopen):
            backend.call_llm("p", max_tokens=11)
            os.environ["RAIYA_BACKEND"] = "anthropic-api"
            backend.call_llm("p", max_tokens=11)
        self.assertEqual(payloads[backend.OPENAI_API_URL]["max_completion_tokens"], 1234)
        self.assertEqual(payloads[backend.ANTHROPIC_API_URL]["max_tokens"], backend.ANTHROPIC_MAX_TOKENS)
        self.assertNotIn("max_completion_tokens", payloads[backend.ANTHROPIC_API_URL])

    def test_default_max_completion_tokens_is_the_constant(self):
        self.write_cfg("")
        os.environ["OPENAI_API_KEY"] = "k"
        got = {}

        def fake_urlopen(req, timeout=None):
            got.update(json.loads(req.data.decode("utf-8")))
            return _Resp(OK_BODY)

        with mock.patch.object(backend.urllib.request, "urlopen", fake_urlopen):
            backend.call_llm("p")
        self.assertEqual(got["max_completion_tokens"], backend.OPENAI_MAX_COMPLETION_TOKENS)


class TestFailClosed(_Case):
    def _assert_blocked(self, text, env=None):
        self.write_cfg(text)
        os.environ.update(env or {})
        with mock.patch.object(backend.urllib.request, "urlopen") as urlopen, \
                mock.patch.object(backend.subprocess, "run") as run:
            with self.assertRaises(backend.BackendError) as ctx:
                backend.call_llm("SECRET_PROMPT")
        urlopen.assert_not_called()
        run.assert_not_called()
        return ctx.exception

    def test_issue_blocks_even_when_env_is_complete(self):
        e = self._assert_blocked(
            "llm:\n  backnd: command\n",
            {"RAIYA_BACKEND": "command", "RAIYA_LLM_CMD": "cat"},
        )
        self.assertIn("設定ファイルが不正です", e.reason)
        self.assertIn("category=unknown_key key=llm", e.reason)
        self.assertNotIn("backnd", e.reason)  # 未知のキー名は reason に出さない
        self.assertIn("backnd", e.detail)

    def test_whole_file_failure_also_blocks(self):
        self._assert_blocked("- not a mapping\n", {"RAIYA_BACKEND": "command", "RAIYA_LLM_CMD": "cat"})

    def test_settings_resolved_once_no_retry(self):
        self.write_cfg("llm:\n  timeout_sec: 0\n")
        os.environ.update({"RAIYA_BACKEND": "command", "RAIYA_LLM_CMD": "cat"})
        with mock.patch.object(backend, "_settings", wraps=backend._settings) as settings:
            with self.assertRaises(backend.BackendError):
                backend.call_llm("p")
        self.assertEqual(settings.call_count, 1)

    def test_direct_call_also_resolves_and_blocks(self):
        self.write_cfg("llm:\n  model: no\n")
        os.environ["OPENAI_API_KEY"] = "k"
        with mock.patch.object(backend.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(backend.BackendError):
                backend._call_openai_api("p", 10)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
