"""(c) MCP stdio の異常系: ハンドシェイク順序違反、Content-Length プリアンブル、
巨大な1行、1行に複数オブジェクト。正常系 (initialize->list->call) と不正 JSON /
非 dict params は tests/test_mcp_server.py 側。

プロトコル処理は公式 `mcp` SDK に委ねているため、ここで pin するのは「raiya の
サーバプロセスが落ちず、後続の正常なリクエストに応答が続く」こと (=実運用で
セッションが死なない) であり、SDK が不正行に何を返すかの細部ではない。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_wire import StdioServer, make_env  # noqa: E402

try:
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:  # pragma: no cover - 環境依存
    HAS_MCP = False


@unittest.skipUnless(HAS_MCP, "公式 mcp SDK が未インストール (pip install -e '.[mcp]')")
class TestMcpStdioAbnormalOrderAndHugeInput(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = make_env(os.path.join(self.tmpdir, "state"))
        self.server = StdioServer(self.env)

    def tearDown(self):
        self.server.kill()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _close_ok(self):
        returncode, stderr = self.server.close()
        self.assertEqual(returncode, 0, msg=stderr)

    def test_request_before_initialize_is_rejected_then_handshake_works(self):
        # MCP 仕様: initialize 前の (ping 以外の) リクエストは受け付けない。旧実装は
        # 無手順で通していたが、一般的な MCP サーバとしては JSON-RPC error で拒否し、
        # その後の正規のハンドシェイクは通る (拒否でセッションが壊れない)。
        s = self.server
        early = s.call_tool(1, "memory_audit")
        self.assertIn("error", early)
        init = s.initialize()
        self.assertIn("protocolVersion", init["result"])
        listed = s.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        audit = json.loads(s.call_tool(3, "memory_audit")["result"]["content"][0]["text"])
        self.assertIn("seed_count", audit)
        self._close_ok()

    def test_second_initialize_after_handshake_does_not_break_the_session(self):
        s = self.server
        s.initialize()
        again = s.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        self.assertTrue("result" in again or "error" in again)
        listed = s.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self._close_ok()

    def test_content_length_preamble_does_not_kill_the_server(self):
        # LSP 風の Content-Length ヘッダ行は MCP stdio (改行区切り JSON) では不正行。
        # プロセスを落とさず、続く JSON 行は処理される。
        s = self.server
        s.send_raw("Content-Length: 55\n\n")
        s.initialize()
        listed = s.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self._close_ok()

    def test_huge_single_line_tools_call_still_returns(self):
        s = self.server
        s.initialize()
        huge_text = "x" * 100000
        called = s.call_tool(1, "memory_recall", {"text": huge_text})
        self.assertFalse(called["result"]["isError"])
        self.assertIn("seeds", json.loads(called["result"]["content"][0]["text"]))
        listed = s.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self._close_ok()

    def test_two_json_objects_on_one_line_do_not_kill_the_server(self):
        s = self.server
        s.initialize()
        a = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        s.send_raw(a + " " + a + "\n")  # 1行に2オブジェクト = 不正 JSON
        listed = s.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self._close_ok()

    def test_huge_non_json_line_does_not_kill_the_server(self):
        s = self.server
        s.initialize()
        s.send_raw("x" * 100000 + "\n")
        listed = s.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self._close_ok()

    def test_huge_valid_json_line_then_next_request_still_works(self):
        s = self.server
        s.initialize()
        recorded = s.call_tool(1, "memory_record_introspection", {"report": "y" * 100000})
        self.assertFalse(recorded["result"]["isError"])
        self.assertIn("ego_weight", json.loads(recorded["result"]["content"][0]["text"]))
        listed = s.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self._close_ok()


if __name__ == "__main__":
    unittest.main()
