"""MCP サーバ (公式 `mcp` SDK 上の stdio / Streamable HTTP) のテスト。

- 生の JSON-RPC 行を stdio に流す往復 (`mcp_wire.StdioServer`): ツール契約・
  初期化ハンドシェイク・エラー形を wire レベルで pin する。
- 公式 `mcp` クライアント (`mcp.client.Client`) からの往復: 「一般的な MCP
  クライアントがそのまま話せる」ことの証拠。stdio と HTTP の両 transport。
- サーバ内に LLM を置かない (backend を import しない) 契約の AST/実行時 pin。

`mcp` SDK が無い環境では wire/クライアントのテストは skip し、AST pin だけ走る。
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_wire import (  # noqa: E402
    INITIALIZED_NOTIFICATION,
    REPO_ROOT,
    HttpServer,
    StdioServer,
    initialize_request,
    make_env,
)
from raiya import sansho  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from tests._state_fixture import add_ledger_body, meta_value, mutate, table_digest  # noqa: E402

MCP_SERVER_PATH = os.path.join(REPO_ROOT, "raiya", "mcp_server.py")

try:
    import anyio  # noqa: F401
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:  # pragma: no cover - 環境依存
    HAS_MCP = False

requires_mcp = unittest.skipUnless(HAS_MCP, "公式 mcp SDK が未インストール (pip install -e '.[mcp]')")

GROUNDED_IMPRINT_ARGS = {
    "turn": {"user_text": "私は猫が好きです/ 猫", "assistant_text": ""},
    "proposals": [
        {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "私は猫が好きです/"},
        }
    ],
}


def imports_backend_at_top_level(source):
    """モジュールソース文字列の直下 (関数内は除く) が "backend" という名前を
    import しているか。

    `import backend` / `import raiya.backend` / `from . import backend` /
    `from raiya import backend` / `from .backend import X` / `from raiya.backend
    import X` のいずれの書き方も検出する。`from . import backend` は
    ImportFrom.module が None になり実際に読み込む名前は names[].name 側に載る
    ため、module 属性だけを見ると見逃す (names[] も必ず見る)。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name == "backend" or alias.name.endswith(".backend") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "backend" or (node.module or "").endswith(".backend"):
                return True
            if any(alias.name == "backend" for alias in node.names):
                return True
    return False


def imports_backend_anywhere(source):
    """MED-2: `imports_backend_at_top_level` は `tree.body` (モジュール直下の文) しか
    見ないため、関数内に隠された遅延 import (例: `_handle_tool_call` の中に
    `import raiya.backend` を書く) を見逃す。mcp_server.py はサーバ内に LLM を一切
    置かない契約 (トップレベルはもちろん、関数内のどこであっても) なので、`ast.walk`
    でモジュール全体 (ネストした関数・クラス本体を含む) の import ノードを走査する
    厳格版をここに用意する。

    注意: engine.py の `propose()` は正当な遅延 import (backend への依存は propose()
    を呼んだときだけ発生させたい) を持つため、engine 側にはこの厳格版を適用しない
    (`imports_backend_at_top_level` のままでよい)。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "backend" or alias.name.endswith(".backend") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "backend" or (node.module or "").endswith(".backend"):
                return True
            if any(alias.name == "backend" for alias in node.names):
                return True
    return False


def _tool_text(response):
    return json.loads(response["result"]["content"][0]["text"])


def _prepare_state_with_life_one(state_dir):
    """hooks の recall がセッション開始時に行うのと同じ状況 (life=1) を用意する。"""
    os.makedirs(state_dir, exist_ok=True)
    setup = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from raiya.engine import MemoryEngine; "
            "MemoryEngine(state_dir=sys.argv[2])",
            REPO_ROOT,
            state_dir,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if setup.returncode != 0:
        raise AssertionError(setup.stderr)
    return _read_life(state_dir)


def _read_life(state_dir):
    return meta_value(state_dir, "alaya.life")


class _StdioTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmpdir, "state")
        self.env = make_env(self.state_dir)
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.kill()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def start_server(self, extra_args=None):
        server = StdioServer(self.env, extra_args=extra_args)
        self._servers.append(server)
        return server

    def close_ok(self, server):
        returncode, stderr = server.close()
        self.assertEqual(returncode, 0, msg=stderr)
        return stderr


@requires_mcp
class TestMcpServerStdioWire(_StdioTestCase):
    """T9: initialize -> initialized -> tools/list -> tools/call の stdio 往復を
    生の JSON-RPC 行で確認する。state は tmp (RAIYA_STATE_DIR を env で注入)。"""

    def test_initialize_tools_list_tools_call_roundtrip(self):
        server = self.start_server()
        init = server.initialize()
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")  # 対応版はそのまま採用
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertEqual(init["result"]["serverInfo"]["name"], "raiya")

        listed = server.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = listed["result"]["tools"]
        self.assertEqual(
            {t["name"] for t in tools},
            {
                "memory_recall",
                "memory_imprint",
                "memory_audit",
                "memory_introspect_material",
                "memory_record_introspection",
            },
        )
        for t in tools:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)

        called = server.call_tool(2, "memory_recall", {"text": "こんにちは"})
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(called["result"]["content"][0]["type"], "text")
        self.assertIn("seeds", _tool_text(called))
        self.close_ok(server)

    def test_imprint_withdrawal_shadow_is_not_an_error_and_shaped_like_reject(self):
        """plan §5: `取り下げ照合` は新 decision だが isError ではなく、結果の形は reject と同じ (seed=None, is_new=None)。"""
        server = self.start_server()
        server.initialize()
        first = _tool_text(server.call_tool(1, "memory_imprint", GROUNDED_IMPRINT_ARGS))
        seed_id = first[0]["seed"]["id"]
        MemoryEngine(state_dir=self.state_dir, new_life=False).withdraw_seed(
            seed_id, "太郎", "誤り", spans=["私は猫が好きです/"])
        shadow_resp = server.call_tool(2, "memory_imprint", GROUNDED_IMPRINT_ARGS)
        self.assertFalse(shadow_resp["result"]["isError"])
        shadow = _tool_text(shadow_resp)[0]
        self.assertEqual(shadow["decision"], "取り下げ照合")
        self.assertEqual(shadow["withdrawn_match"], seed_id)
        reject_args = json.loads(json.dumps(GROUNDED_IMPRINT_ARGS))
        reject_args["proposals"][0]["kind"] = "存在しないkind"
        reject_resp = server.call_tool(3, "memory_imprint", reject_args)
        self.assertFalse(reject_resp["result"]["isError"])
        reject = _tool_text(reject_resp)[0]
        self.assertEqual(reject["decision"], "reject")
        for key in ("seed", "is_new"):
            self.assertIn(key, shadow)
            self.assertIn(key, reject)
            self.assertIsNone(shadow[key])
            self.assertIsNone(reject[key])
        self.close_ok(server)

    def test_initialize_falls_back_to_a_supported_protocol_version(self):
        # 未知の protocolVersion を名乗るクライアントには、サーバが対応する版を返す
        # (MCP 仕様の版交渉。旧実装は何を言われても 2024-11-05 固定だった)。
        server = self.start_server()
        init = server.initialize(protocol_version="1999-01-01")
        negotiated = init["result"]["protocolVersion"]
        self.assertNotEqual(negotiated, "1999-01-01")
        self.assertRegex(negotiated, r"^\d{4}-\d{2}-\d{2}$")
        self.close_ok(server)

        server = self.start_server()
        init = server.initialize(protocol_version="2024-11-05")
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        self.close_ok(server)

    def test_ping_returns_empty_result(self):
        # MCP 仕様: ping には空の result で即応答しなければならない (旧実装は -32601)。
        server = self.start_server()
        server.initialize()
        pong = server.request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(pong["result"], {})
        self.close_ok(server)

    def test_fabricated_proposal_via_tools_call_ends_up_in_quarantine(self):
        # end-to-end: 捏造提案 (非接地 span) を tools/call(memory_imprint) で送り、
        # quarantine に落ちることを memory_audit で確認する。
        server = self.start_server()
        server.initialize()
        imprinted = server.call_tool(
            1,
            "memory_imprint",
            {
                "turn": {"user_text": "今日は晴れです", "assistant_text": ""},
                "proposals": [
                    {
                        "content": "捏造された記憶",
                        "conditions": ["晴れ"],
                        "polarity": "無記","kind": "所在",
                        "evidence": {"source": "user", "span": "完全に捏造されたspanです"},
                    }
                ],
            },
        )
        self.assertEqual(_tool_text(imprinted)[0]["decision"], "遍計所執")

        audit = _tool_text(server.call_tool(2, "memory_audit"))
        self.assertEqual(audit["quarantine_retained"], 1)
        self.assertEqual(audit["quarantine_total"], 1)
        self.assertEqual(audit["seed_count"], 0)  # seeds には構造的に入らない
        self.close_ok(server)

    def test_unknown_method_returns_method_not_found(self):
        server = self.start_server()
        server.initialize()
        response = server.request({"jsonrpc": "2.0", "id": 1, "method": "unknown/method"})
        self.assertEqual(response["error"]["code"], -32601)
        self.close_ok(server)

    def test_initialize_only_launches_twice_do_not_advance_life(self):
        # 本番事故の pin: サーバは MemoryEngine(new_life=False) で構築するため、
        # `claude mcp list` の health check 相当 (initialize だけで終了する起動) を
        # 何度繰り返しても life は進まない (生の管理は hooks の recall に一本化)。
        life_before = _prepare_state_with_life_one(self.state_dir)
        self.assertEqual(life_before, 1)
        for _ in range(2):
            server = self.start_server()
            init = server.initialize()
            self.assertIn("protocolVersion", init["result"])
            self.close_ok(server)
        self.assertEqual(_read_life(self.state_dir), life_before)

    def test_notification_receives_no_response(self):
        server = self.start_server()
        server.initialize()
        server.notify({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 999}})
        listed = server.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self.assertEqual(server.stray, [])  # 通知 (id 無し) には一切応答しない
        self.close_ok(server)

    def test_tools_call_without_params_or_name_returns_invalid_params(self):
        # params キー欠落・name 欠落は「未知のツール」(isError=True の業務エラー) では
        # なく、呼び出し自体が不正形式というプロトコルレベルの -32602 Invalid params。
        server = self.start_server()
        server.initialize()
        no_params = server.request({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        self.assertEqual(no_params["error"]["code"], -32602)
        no_name = server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"arguments": {}}})
        self.assertEqual(no_name["error"]["code"], -32602)
        # 後続メッセージにも応答が続く = サーバが継続稼働している証拠。
        listed = server.request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self.close_ok(server)

    def test_tools_call_with_unknown_tool_returns_isError_true(self):
        # ツール実行時のエラーは JSON-RPC error ではなく isError=True の結果で返す
        # (MCP 仕様: ツールの失敗は LLM が見て回復できるよう result 側に載せる)。
        server = self.start_server()
        server.initialize()
        response = server.call_tool(1, "no_such_tool")
        self.assertEqual(response["result"]["isError"], True)
        self.assertEqual(response["result"]["content"][0]["type"], "text")
        self.assertIn("no_such_tool", response["result"]["content"][0]["text"])
        self.close_ok(server)

    def test_malformed_lines_do_not_kill_the_server(self):
        # 不正 JSON 行・dict でない JSON・params が配列のリクエストのどれもサーバを
        # 落とさず、後続の正常なリクエストに応答が続く。
        server = self.start_server()
        server.initialize()
        server.send_raw("not valid json\n")
        server.send_raw(json.dumps([1, 2, 3]) + "\n")
        server.send_raw(json.dumps("文字列") + "\n")
        server.notify({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2, 3]})
        listed = server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("tools", listed["result"])
        self.close_ok(server)

    def test_introspect_and_record_introspection_via_tools_call(self):
        server = self.start_server()
        server.initialize()
        args = json.loads(json.dumps(GROUNDED_IMPRINT_ARGS))
        args["proposals"] = args["proposals"] * 2
        server.call_tool(1, "memory_imprint", args)
        server.call_tool(2, "memory_recall", {"text": "猫について"})
        material = _tool_text(server.call_tool(3, "memory_introspect_material"))
        self.assertEqual(len(material["manas_log"]), 1)
        self.assertTrue(material["germinated_history"])
        recorded = _tool_text(
            server.call_tool(4, "memory_record_introspection", {"report": "猫の話に色をつけていたかもしれません"})
        )
        self.assertLess(recorded["ego_weight"], 1.0)
        self.close_ok(server)

    def test_memory_imprint_schema_requires_evidence_on_each_proposal(self):
        # LOW-3: evidence 必須化の契約 (§3.2) をスキーマ側でも宣言する。
        server = self.start_server()
        server.initialize()
        listed = server.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in listed["result"]["tools"]}
        proposal_schema = tools["memory_imprint"]["inputSchema"]["properties"]["proposals"]["items"]
        self.assertIn("evidence", proposal_schema["required"])
        self.close_ok(server)

    def test_memory_imprint_description_requires_verbatim_conditions(self):
        # ホスト LLM に見せる description は、縁の逐語接地と非接地→reject を述べる。
        server = self.start_server()
        server.initialize()
        listed = server.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in listed["result"]["tools"]}
        desc = tools["memory_imprint"]["description"]
        self.assertIn("縁", desc)
        self.assertIn("reject", desc)
        # 否定形 (「逐語である必要はなく…reject にはならない」) を通さない (QA m10)
        self.assertIn("逐語で現れる語でなければならず", desc)
        self.assertIn("熏習しない", desc)
        self.close_ok(server)


@requires_mcp
class TestMcpCorruptionWarnings(_StdioTestCase):
    """#65: 破損の警告は結果 JSON のキーと stderr の 1 行 (`raiya-mcp-server: ...`) に出る。健全なら出ない。"""

    def _stderr_warning_lines(self, stderr):
        return [ln for ln in stderr.splitlines() if ln.startswith("raiya-mcp-server: ")]

    def _corrupt_alaya(self):
        _prepare_state_with_life_one(self.state_dir)
        mutate(self.state_dir, lambda c: c.execute(
            "INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, '{')"))

    def test_audit_returns_alaya_warning_and_prints_one_stderr_line(self):
        self._corrupt_alaya()
        server = self.start_server()
        server.initialize()
        body = _tool_text(server.call_tool(1, "memory_audit", {}))
        self.assertIn("読めない行", body["alaya_warning"])
        self.assertIsNone(body["manas_warning"])
        stderr = self.close_ok(server)
        lines = self._stderr_warning_lines(stderr)
        self.assertEqual(len(lines), 1, msg=stderr)
        self.assertIn("読めない行", lines[0])

    def test_recall_returns_alaya_warning_every_time_because_rows_are_never_rewritten(self):
        self._corrupt_alaya()
        server = self.start_server()
        server.initialize()
        first = _tool_text(server.call_tool(1, "memory_recall", {"text": "x"}))
        second = _tool_text(server.call_tool(2, "memory_recall", {"text": "x"}))
        self.assertIn("読めない行", first["alaya_warning"])
        self.assertIn("読めない行", second["alaya_warning"])  # 読めない行は読み飛ばすだけで書き直さない = 毎回出る
        stderr = self.close_ok(server)
        self.assertEqual(len(self._stderr_warning_lines(stderr)), 2, msg=stderr)

    def test_corrupt_ledger_row_audit_twice_prints_two_lines(self):
        _prepare_state_with_life_one(self.state_dir)
        add_ledger_body(self.state_dir, "{bad")
        server = self.start_server()
        server.initialize()
        for i in (1, 2):
            body = _tool_text(server.call_tool(i, "memory_audit", {}))
            self.assertIn("台帳の読めない行", body["ledger_warning"])
        stderr = self.close_ok(server)
        lines = self._stderr_warning_lines(stderr)
        self.assertEqual(len(lines), 2, msg=stderr)  # dedupe しない
        for ln in lines:
            self.assertIn("台帳の読めない行", ln)

    def test_corrupt_manas_audit_returns_manas_warning_and_one_line(self):
        _prepare_state_with_life_one(self.state_dir)
        mutate(self.state_dir, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('{bad')"))
        server = self.start_server()
        server.initialize()
        body = _tool_text(server.call_tool(1, "memory_audit", {}))
        self.assertTrue(body["manas_warning"])
        self.assertIsNone(body["alaya_warning"])
        stderr = self.close_ok(server)
        lines = self._stderr_warning_lines(stderr)
        self.assertEqual(len(lines), 1, msg=stderr)
        self.assertIn("manas_log", lines[0])

    def test_introspect_material_returns_alaya_warning_and_prints_one_stderr_line(self):
        self._corrupt_alaya()
        before = sorted(os.listdir(self.state_dir))
        server = self.start_server()
        server.initialize()
        body = _tool_text(server.call_tool(1, "memory_introspect_material", {}))
        self.assertIn("読めない行", body["alaya_warning"])
        self.assertIsNone(body["manas_warning"])
        stderr = self.close_ok(server)
        lines = self._stderr_warning_lines(stderr)
        self.assertEqual(len(lines), 1, msg=stderr)
        self.assertIn("読めない行", lines[0])
        self.assertEqual([n for n in os.listdir(self.state_dir) if "corrupt" in n], [])
        self.assertTrue(set(os.listdir(self.state_dir)) - {".lock"} <= set(before))

    def test_unreadable_db_mid_session_is_an_is_error_result_not_a_crash(self):
        _prepare_state_with_life_one(self.state_dir)
        server = self.start_server()
        server.initialize()
        self.assertFalse(server.call_tool(1, "memory_audit", {})["result"]["isError"])
        with open(os.path.join(self.state_dir, "raiya.db"), "wb") as f:
            f.write(b"this is not a database " * 400)
        resp = server.call_tool(2, "memory_audit", {})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("状態DB 読めず category=not_a_database", json.dumps(resp, ensure_ascii=False))
        self.assertTrue(server.call_tool(3, "memory_recall", {"text": "x"})["result"]["isError"])  # 続けて呼べる (落ちない)
        self.close_ok(server)

    def test_unreadable_db_at_startup_exits_2_with_one_stderr_line(self):
        _prepare_state_with_life_one(self.state_dir)
        with open(os.path.join(self.state_dir, "raiya.db"), "wb") as f:
            f.write(b"this is not a database " * 400)
        server = self.start_server()
        returncode, stderr = server.close()
        self.assertEqual(returncode, 2, msg=stderr)
        self.assertIn("状態DB 読めず category=not_a_database", stderr)

    def test_legacy_json_state_at_startup_exits_2_without_creating_a_db(self):
        os.makedirs(self.state_dir)
        with open(os.path.join(self.state_dir, "alaya.json"), "w") as f:
            f.write("{}")
        server = self.start_server()
        returncode, stderr = server.close()
        self.assertEqual(returncode, 2, msg=stderr)
        self.assertIn("旧 JSON 版", stderr)
        self.assertEqual(sorted(os.listdir(self.state_dir)), ["alaya.json"])  # .lock も作らない (#86)

    def test_healthy_state_prints_no_warning_line(self):
        _prepare_state_with_life_one(self.state_dir)
        server = self.start_server()
        server.initialize()
        server.call_tool(1, "memory_recall", {"text": "x"})
        body = _tool_text(server.call_tool(2, "memory_audit", {}))
        self.assertIsNone(body["alaya_warning"])
        stderr = self.close_ok(server)
        self.assertEqual(self._stderr_warning_lines(stderr), [], msg=stderr)


@requires_mcp
class TestMcpServerWithOfficialClientStdio(unittest.TestCase):
    """公式 `mcp` クライアントが stdio でそのまま話せる (一般的な MCP サーバである) こと。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = make_env(os.path.join(self.tmpdir, "state"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _params(self):
        from mcp import StdioServerParameters

        return StdioServerParameters(
            command=sys.executable, args=["-m", "raiya.mcp_server"], env=self.env, cwd=REPO_ROOT
        )

    def test_official_client_lists_and_calls_tools(self):
        import anyio
        from mcp.client import Client

        async def scenario():
            async with Client(self._params()) as client:
                self.assertEqual(client.server_info.name, "raiya")
                tools = await client.list_tools()
                names = {t.name for t in tools.tools}
                self.assertIn("memory_recall", names)
                self.assertIn("memory_imprint", names)
                result = await client.call_tool("memory_recall", {"text": "こんにちは"})
                self.assertFalse(result.is_error)
                self.assertIn("seeds", json.loads(result.content[0].text))

        anyio.run(scenario)

    def test_official_client_gets_is_error_result_for_unknown_tool(self):
        import anyio
        from mcp.client import Client

        async def scenario():
            async with Client(self._params()) as client:
                result = await client.call_tool("no_such_tool", {})
                self.assertTrue(result.is_error)

        anyio.run(scenario)


@requires_mcp
class TestMcpServerHttp(unittest.TestCase):
    """`--transport http` (Streamable HTTP): 公式クライアントが URL だけで接続できる。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmpdir, "state")
        self.env = make_env(self.state_dir)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start(self):
        self.server = HttpServer(self.env)
        return self.server.url

    def test_http_official_client_lists_and_calls_tools(self):
        import anyio
        from mcp.client import Client

        url = self._start()

        async def scenario():
            async with Client(url) as client:
                self.assertEqual(client.server_info.name, "raiya")
                tools = await client.list_tools()
                self.assertIn("memory_audit", {t.name for t in tools.tools})
                result = await client.call_tool("memory_recall", {"text": "こんにちは"})
                self.assertFalse(result.is_error)
                self.assertIn("narrative", json.loads(result.content[0].text))

        anyio.run(scenario)

    def test_http_sessions_share_one_memory_state(self):
        # 別々の HTTP セッション (=別クライアント) が同じ RAIYA_STATE_DIR を見る:
        # セッション A の熏習がセッション B の memory_audit に現れる。
        import anyio
        from mcp.client import Client

        url = self._start()

        async def imprint_from_a():
            async with Client(url) as client:
                result = await client.call_tool("memory_imprint", GROUNDED_IMPRINT_ARGS)
                self.assertFalse(result.is_error)
                return json.loads(result.content[0].text)

        async def audit_from_b():
            async with Client(url) as client:
                result = await client.call_tool("memory_audit", {})
                return json.loads(result.content[0].text)

        decisions = anyio.run(imprint_from_a)
        self.assertNotEqual(decisions[0]["decision"], "遍計所執")
        audit = anyio.run(audit_from_b)
        self.assertEqual(audit["seed_count"], 1)

    def test_http_launch_and_connect_do_not_advance_life(self):
        # stdio と同じく HTTP 側も MemoryEngine(new_life=False): 起動・接続で生は進まない。
        import anyio
        from mcp.client import Client

        life_before = _prepare_state_with_life_one(self.state_dir)
        url = self._start()

        async def connect_only():
            async with Client(url) as client:
                await client.list_tools()

        anyio.run(connect_only)
        self.assertEqual(_read_life(self.state_dir), life_before)

    def test_http_over_limit_post_is_413_and_server_keeps_serving(self):
        # #20: 上限超過はハンドラの前に transport 層で 413 (isError にはできない)。次の呼び出しは通る。
        import urllib.error
        import urllib.request

        import anyio
        from mcp.client import Client
        from raiya import mcp_server

        url = self._start()
        body = b"x" * (mcp_server.MAX_REQUEST_BYTES + 1024)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        # #28: サーバーは本文を読み切る前に 413 を返して接続を閉じるので、クライアントが
        # まだ送信中だと HTTPError(413) ではなく URLError(EPIPE / ECONNRESET) になることがある。
        # どちらも「上限超過で拒否された」であり、本題は次の呼び出しが通ること。
        try:
            urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)
            e.close()
        except urllib.error.URLError as e:
            self.assertIsInstance(
                e.reason, (BrokenPipeError, ConnectionResetError), msg=repr(e.reason)
            )
        except (BrokenPipeError, ConnectionResetError):
            # 応答の読み取り中 (getresponse) の切断は urllib が URLError に包まないので素で来る。
            pass
        else:
            self.fail("上限超過の POST が拒否されなかった")

        async def after():
            async with Client(url) as client:
                result = await client.call_tool("memory_recall", {"text": "こんにちは"})
                self.assertFalse(result.is_error)

        anyio.run(after)


@requires_mcp
class TestMcpServerCommandLine(unittest.TestCase):
    def test_help_documents_http_transport_options(self):
        result = subprocess.run(
            [sys.executable, "-m", "raiya.mcp_server", "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=make_env(tempfile.mkdtemp()),
            cwd=REPO_ROOT,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for token in ("--transport", "stdio", "http", "--host", "--port"):
            self.assertIn(token, result.stdout)


@requires_mcp
class TestRecallContextDispatch(unittest.TestCase):
    """T8: memory_recall の context (文脈窓)。text 単体では届かない縁も、context に
    含めれば germinate に届く (engine.recall の第2引数への配線)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_recall_tool_passes_context(self):
        from raiya import mcp_server

        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="猫が好きです/ 猫", assistant_text=""),
                    [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                      "evidence": {"source": "user", "span": "猫が好きです/"}}])
        plain = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "別の話"})
        self.assertEqual(plain["seeds"], [])
        with_ctx = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "別の話", "context": ["猫の話"]})
        self.assertEqual([s["content"] for s in with_ctx["seeds"]], ["猫が好き"])
        schema = next(t for t in mcp_server.TOOLS if t["name"] == "memory_recall")["inputSchema"]
        self.assertEqual(schema["properties"]["context"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(schema["required"], ["text"])

    def test_recall_tool_coerces_bare_string_context_to_empty_list(self):
        # note 1 (task-8 補記): context が str (list ではない) のときは空列として扱う。
        # 素通りさせると alaya.germinate の型ガードが TypeError を出す (str を1文字ずつ
        # の列として渡すのを防ぐガード。engine.recall の docstring 参照)。
        from raiya import mcp_server

        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="猫が好きです/ 猫", assistant_text=""),
                    [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                      "evidence": {"source": "user", "span": "猫が好きです/"}}])
        result = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "x", "context": "猫"})
        self.assertEqual(result["seeds"], [])

    def test_recall_tool_drops_non_str_items_from_context_list(self):
        # note 1 (task-8 補記): list の要素のうち str でないもの (5, None) は捨てて
        # str だけ残す (coerce ではなく filter)。
        from raiya import mcp_server

        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="猫が好きです/ 猫", assistant_text=""),
                    [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                      "evidence": {"source": "user", "span": "猫が好きです/"}}])
        result = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "x", "context": ["猫", 5, None]})
        self.assertEqual([s["content"] for s in result["seeds"]], ["猫が好き"])


@requires_mcp
class TestMcpImprintGateRules(unittest.TestCase):
    """#17 #19: memory_imprint も台帳と同じ規則で往復を読む (貼り付け・U+FFFC)。"""

    def setUp(self):
        from raiya import mcp_server

        self.m = mcp_server
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _p(span, cond):
        return {"content": "記録", "conditions": [cond], "polarity": "無記", "kind": "所在",
                "evidence": {"source": "user", "span": span}}

    def test_paste_only_span_and_fffc_condition_are_quarantined(self):
        paste = '<pasted_content id="z">貼った手順書</pasted_content id="z">'
        args = {
            "turn": {"user_text": "再開して p/\n" + paste + "\ufffc", "assistant_text": ""},
            "proposals": [self._p("貼った手順書", "再開"), self._p("再開して p/", "再開\ufffc"), self._p("再開して p/", "再開")],
        }
        result = self.m._handle_tool_call(self.eng, "memory_imprint", args)
        self.assertEqual([d["decision"] for d in result], ["遍計所執", "遍計所執", "圓成實"])


@requires_mcp
class TestMcpRequestSizeLimit(unittest.TestCase):
    """#20 両 transport 共通の上限 `MAX_REQUEST_BYTES`: stdio 側は arguments のバイト数で isError にする。"""

    def setUp(self):
        from raiya import mcp_server

        self.m = mcp_server
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_limit_matches_sdk_default_and_is_passed_to_http_app(self):
        from unittest import mock

        from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE

        self.assertEqual(self.m.MAX_REQUEST_BYTES, DEFAULT_MAX_REQUEST_BODY_SIZE)
        with mock.patch.object(self.m, "MAX_REQUEST_BYTES", 1234):
            app = self.m.build_http_app(self.eng)
        self.assertEqual(app.routes[0].endpoint.session_manager.max_request_body_size, 1234)

    def test_recall_over_limit_is_argument_error(self):
        text = "あ" * (self.m.MAX_REQUEST_BYTES // 3 + 1)  # UTF-8 で 3 バイト/字 -> 超過
        with self.assertRaises(self.m.ToolArgumentError) as cm:
            self.m._handle_tool_call(self.eng, "memory_recall", {"text": text})
        self.assertIn("上限 {} バイト".format(self.m.MAX_REQUEST_BYTES), str(cm.exception))

    def _grounded_imprint_args(self, padding=""):
        return {
            "turn": {"user_text": "Xを使う。" + padding, "assistant_text": ""},
            "proposals": [{"content": "Xを使う", "conditions": ["X"],
                           "evidence": {"source": "user", "span": "Xを使う"}}],
        }

    def test_imprint_over_limit_writes_nothing(self):
        # 同じ引数は水増し無しなら 1 件植わる (空振りでないことの確認)
        ok_eng = MemoryEngine(state_dir=os.path.join(self.tmpdir, "ok"))
        self.m._handle_tool_call(ok_eng, "memory_imprint", self._grounded_imprint_args())
        self.assertEqual(ok_eng.audit()["seed_count"], 1)

        args = self._grounded_imprint_args("x" * self.m.MAX_REQUEST_BYTES)
        with self.assertRaises(self.m.ToolArgumentError):
            self.m._handle_tool_call(self.eng, "memory_imprint", args)
        audit = self.eng.audit()
        self.assertEqual(audit["seed_count"], 0)
        self.assertEqual(audit["quarantine_retained"], 0)
        self.assertEqual(audit["quarantine_total"], 0)
        self.assertEqual(audit["imprint_log_tail"], [])

    def test_record_introspection_over_limit_keeps_ego_weight(self):
        before = self.eng.manas.ego_weight
        with self.assertRaises(self.m.ToolArgumentError):
            self.m._handle_tool_call(
                self.eng, "memory_record_introspection", {"report": "x" * self.m.MAX_REQUEST_BYTES}
            )
        self.assertEqual(self.eng.manas.ego_weight, before)

    def test_lone_surrogate_is_measured_not_rejected(self):
        result = self.m._handle_tool_call(self.eng, "memory_recall", {"text": "a\ud800b"})
        self.assertIn("narrative", result)

    def test_just_under_limit_succeeds(self):
        overhead = len(json.dumps({"text": ""}).encode("utf-8"))
        text = "x" * (self.m.MAX_REQUEST_BYTES - overhead)
        result = self.m._handle_tool_call(self.eng, "memory_recall", {"text": text})
        self.assertIn("narrative", result)

    def test_unserializable_arguments_are_argument_error_not_crash(self):
        circular = {}
        circular["self"] = circular
        with self.assertRaises(self.m.ToolArgumentError):
            self.m._handle_tool_call(self.eng, "memory_recall", circular)


@requires_mcp
class TestMcpArgumentBoundary(unittest.TestCase):
    """#18 session_start の配線 / #21 型違い引数の isError 化 (engine を呼ぶ前に弾く)。"""

    def setUp(self):
        from raiya import mcp_server

        self.m = mcp_server
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _call(self, name, args):
        return self.m._handle_tool_call(self.eng, name, args)

    def _plant_resume(self, project="/proj/a"):
        self._call("memory_imprint", {
            "turn": {"user_text": "次は裁定待ちです 裁定", "assistant_text": ""},
            "proposals": [{"content": "次 = 裁定待ち", "conditions": ["裁定"], "kind": "再開点",
                           "evidence": {"source": "user", "span": "次は裁定待ちです"}}],
            "project": project,
        })

    def test_session_start_true_returns_resume(self):
        self._plant_resume()
        r = self._call("memory_recall", {"text": "x", "project": "/proj/a", "session_start": True})
        self.assertEqual(r["resume"]["content"], "次 = 裁定待ち")

    def test_session_start_absent_or_non_bool_has_no_resume(self):
        self._plant_resume()
        for extra in ({}, {"session_start": False}, {"session_start": "true"}, {"session_start": 1}):
            with self.subTest(extra=extra):
                r = self._call("memory_recall", dict({"text": "x", "project": "/proj/a"}, **extra))
                self.assertIsNone(r["resume"])
        r = self._call("memory_recall", {"text": "x", "project": "/proj/b", "session_start": True})
        self.assertIsNone(r["resume"])

    def test_session_start_is_declared_in_schema(self):
        schema = next(t for t in self.m.TOOLS if t["name"] == "memory_recall")["inputSchema"]
        self.assertEqual(schema["properties"]["session_start"], {"type": "boolean"})
        self.assertEqual(schema["required"], ["text"])

    def test_record_introspection_rejects_bad_report_and_keeps_ego(self):
        for args in ({}, {"report": ""}, {"report": " \n"}, {"report": 1}, {"report": None}, None):
            with self.subTest(args=args):
                with self.assertRaises(self.m.ToolArgumentError):
                    self._call("memory_record_introspection", args)
        self.assertEqual(self.eng.manas.ego_weight, 1.0)
        self.assertEqual(self.eng.manas.state["introspections"], [])
        ok = self._call("memory_record_introspection", {"report": "色をつけていたかも"})
        self.assertLess(ok["ego_weight"], 1.0)

    def test_recall_text_missing_or_null_is_error_and_non_str_is_error(self):
        for bad in (None, 1, ["a"], {"a": 1}, True):
            with self.assertRaises(self.m.ToolArgumentError):
                self._call("memory_recall", {"text": bad})
        with self.assertRaises(self.m.ToolArgumentError) as cm:
            self._call("memory_recall", {})
        self.assertIn("memory_recall の text は必須です", str(cm.exception))
        self.assertEqual(self._call("memory_recall", {"text": ""})["seeds"], [])

    def test_recall_arguments_none_requires_text(self):
        with self.assertRaises(self.m.ToolArgumentError) as cm:
            self._call("memory_recall", None)
        self.assertIn("memory_recall の text は必須です", str(cm.exception))

    def test_non_dict_arguments_are_tool_argument_errors_before_engine(self):
        before = table_digest(self.tmpdir)
        for name in ("memory_recall", "memory_imprint", "memory_audit"):
            for bad in ("text", ["text"], [], 1):
                with self.subTest(name=name, bad=bad):
                    with self.assertRaises(self.m.ToolArgumentError) as cm:
                        self._call(name, bad)
                    self.assertIn("引数はオブジェクトで指定してください", str(cm.exception))
        self.assertEqual(table_digest(self.tmpdir), before)

    def test_unknown_names_of_mixed_key_types_are_listed_without_crash(self):
        with self.assertRaises(self.m.ToolArgumentError) as cm:
            self._call("memory_audit", {1: "x", "b": "y"})
        self.assertIn("未知の引数があります: 1, b", str(cm.exception))

    def test_imprint_type_violations_are_tool_argument_errors(self):
        good_turn = {"user_text": "u", "assistant_text": "a"}
        cases = [
            {"turn": "nope", "proposals": []},
            {"turn": good_turn, "proposals": "nope"},
            {"turn": good_turn, "proposals": {"a": 1}},
            {"turn": {"user_text": 1, "assistant_text": "a"}, "proposals": []},
            {"turn": {"user_text": "u", "assistant_text": ["a"]}, "proposals": []},
            {"turn": good_turn, "proposals": ["x"]},
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(self.m.ToolArgumentError):
                    self._call("memory_imprint", args)

    def test_imprint_bad_item_rejects_whole_call_before_any_write(self):
        good = {"content": "猫が好き", "conditions": ["猫"], "kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです/"}}
        with self.assertRaises(self.m.ToolArgumentError):
            self._call("memory_imprint", {
                "turn": {"user_text": "猫が好きです/ 猫", "assistant_text": ""},
                "proposals": [good, "x"],
            })
        self.assertEqual(self.eng.audit()["seed_count"], 0)
        self.assertEqual(self.eng.audit()["imprint_log_tail"], [])

    def test_imprint_nested_malformed_field_rejects_whole_call_without_any_write(self):
        good = {"content": "猫が好き", "conditions": ["猫"], "kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです/"}}
        ev = {"source": "user", "span": "猫"}
        def _snap():
            return table_digest(self.tmpdir)

        before = _snap()
        bads = [
            ("conditions", {"content": "x", "conditions": [["猫"]], "evidence": ev}),
            ("conditions", {"content": "x", "conditions": 5, "evidence": ev}),
            ("content", {"content": 5, "conditions": ["猫"], "evidence": ev}),
            ("kind", {"content": "x", "kind": 1, "evidence": ev}),
            ("polarity", {"content": "x", "polarity": [], "evidence": ev}),
            ("evidence.span", {"content": "x", "evidence": {"source": "user", "span": 5}}),
            ("evidence[1].source", {"content": "x", "evidence": [ev, {"source": 1, "span": "猫"}]}),
        ]
        for field, bad in bads:
            with self.subTest(field=field):
                with self.assertRaises(self.m.ToolArgumentError) as cm:
                    self._call("memory_imprint", {
                        "turn": {"user_text": "猫が好きです/ 猫", "assistant_text": ""},
                        "proposals": [good, bad],
                    })
                self.assertIn("proposals[1].{}".format(field), str(cm.exception))
        self.assertEqual(_snap(), before)  # どの表も 1 行も変わらない
        audit = self.eng.audit()
        self.assertEqual(audit["seed_count"], 0)
        self.assertEqual(audit["imprint_log_tail"], [])
        self.assertEqual(audit["quarantine_total"], 0)

    def test_imprint_missing_span_or_evidence_still_reaches_gate(self):
        for ev in ({"source": "user"}, None, "x", {}):
            with self.subTest(ev=ev):
                p = {"content": "x", "conditions": ["猫"]}
                if ev is not None:
                    p["evidence"] = ev
                r = self._call("memory_imprint", {
                    "turn": {"user_text": "猫が好きです/ 猫", "assistant_text": ""}, "proposals": [p]})
                self.assertEqual(r[0]["decision"], "遍計所執")

    def test_imprint_missing_evidence_is_still_recorded_then_quarantined(self):
        r = self._call("memory_imprint", {
            "turn": {"user_text": "猫が好きです/ 猫", "assistant_text": ""},
            "proposals": [{"content": "猫が好き", "conditions": ["猫"]}],
        })
        self.assertEqual(r[0]["decision"], "遍計所執")
        self.assertEqual(self.eng.audit()["quarantine_retained"], 1)

    def test_imprint_null_content_is_not_a_type_error(self):
        # 欠落・null は通す (docstring・AGENTS.md の約束)。content=null は alaya が "" として扱う。
        r = self._call("memory_imprint", {
            "turn": {"user_text": "猫が好きです/ 猫", "assistant_text": ""},
            "proposals": [{"content": None, "conditions": ["猫"],
                           "evidence": {"source": "user", "span": "猫が好き"}}],
        })
        self.assertEqual(len(r), 1)

    def test_imprint_missing_or_null_required_arguments_are_errors_without_write(self):
        before = table_digest(self.tmpdir)
        ut = {"user_text": "u", "assistant_text": "a"}
        cases = [
            {},
            {"proposals": []},
            {"turn": ut},
            {"turn": None, "proposals": []},
            {"turn": ut, "proposals": None},
            {"turn": {"assistant_text": "a"}, "proposals": []},
            {"turn": {"user_text": "u"}, "proposals": []},
            {"turn": {"user_text": None, "assistant_text": "a"}, "proposals": []},
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(self.m.ToolArgumentError):
                    self._call("memory_imprint", args)
        self.assertEqual(table_digest(self.tmpdir), before)
        self.assertEqual(self._call("memory_imprint", {"turn": {"user_text": "", "assistant_text": ""}, "proposals": []}), [])

    def test_unknown_argument_names_are_errors_for_all_tools(self):
        before = table_digest(self.tmpdir)
        ut = {"user_text": "u", "assistant_text": "a"}
        cases = [
            ("memory_recall", {"txt": "x"}),
            ("memory_recall", {"text": "x", "zzz": 1}),
            ("memory_imprint", {"turn": ut, "proposals": [], "proposal": []}),
            ("memory_audit", {"x": 1}),
            ("memory_introspect_material", {"x": 1}),
            ("memory_record_introspection", {"report": "r", "x": 1}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                with self.assertRaises(self.m.ToolArgumentError) as cm:
                    self._call(name, args)
                self.assertIn(name + " に未知の引数があります", str(cm.exception))
        with self.assertRaises(self.m.ToolArgumentError) as cm:
            self._call("memory_recall", {"text": "x", "b": 1, "a": 2})
        self.assertIn("a, b (受け付ける引数: text, context, project, session_start)", str(cm.exception))
        self.assertEqual(table_digest(self.tmpdir), before)
        self._call("memory_audit", None)
        self._call("memory_audit", {})
        self._call("memory_introspect_material", {})

    def test_proposal_and_turn_extra_keys_are_tolerated_and_missing_evidence_reaches_gate(self):
        r = self._call("memory_imprint", {
            "turn": {"user_text": "猫が好きです/ 猫", "assistant_text": "", "extra": 1},
            "proposals": [
                {"content": "猫が好き", "conditions": ["猫"], "sansho": "圓成實",
                 "evidence": {"source": "user", "span": "猫が好きです/"}},
                {"content": "x", "conditions": ["猫"], "sansho": "圓成實"},
            ],
        })
        self.assertEqual(r[1]["decision"], "遍計所執")
        self.assertEqual(len(r), 2)

    def test_boundary_errors_surface_as_is_error_via_client(self):
        import anyio
        from mcp.client import Client

        async def run():
            async with Client(self.m.build_server(self.eng)) as client:
                return [
                    await client.call_tool("memory_recall", {"txt": "x"}),
                    await client.call_tool("memory_recall", {}),
                    await client.call_tool("memory_imprint", {"turn": {"user_text": "u", "assistant_text": "a"}, "proposal": []}),
                ]

        res = anyio.run(run)
        for r in res:
            self.assertTrue(r.is_error)
        self.assertIn("未知の引数があります: txt", res[0].content[0].text)
        self.assertIn("text は必須です", res[1].content[0].text)
        self.assertIn("未知の引数があります: proposal", res[2].content[0].text)

    def test_type_error_surfaces_as_is_error_result_with_clear_message(self):
        if not HAS_MCP:
            self.skipTest("mcp SDK 無し")
        import anyio
        from mcp.client import Client

        async def run():
            async with Client(self.m.build_server(self.eng)) as client:
                return await client.call_tool("memory_recall", {"text": 1})

        res = anyio.run(run)
        self.assertTrue(res.is_error)
        self.assertIn("text は文字列", res.content[0].text)
        self.assertNotIn("normalize", res.content[0].text)


def _tools_from_source(source):
    """mcp_server.TOOLS を AST から読む (mcp SDK 無しで到達する)。

    name / description は隣接文字列がパース時に Constant へ畳まれる。
    inputSchema は入れ子 dict なのでここには載せない (スキーマ pin は wire 側)。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets):
            continue
        tools = []
        for elt in node.value.elts:
            item = {}
            for key, val in zip(elt.keys, elt.values):
                if isinstance(key, ast.Constant) and isinstance(val, ast.Constant):
                    item[key.value] = val.value
            tools.append(item)
        return tools
    raise AssertionError("TOOLS が見つからない")


class TestMcpServerToolDescriptions(unittest.TestCase):
    """TOOLS の description はモジュール定数。mcp SDK が無い環境でも読める。"""

    def test_memory_imprint_description_requires_verbatim_conditions(self):
        # ホスト LLM に見せる memory_imprint の説明は、縁の逐語接地と非接地→reject を述べる。
        # 追加文を消す mutant がここを RED にする (tools/list pin は mcp 無しだと skip)。
        with open(MCP_SERVER_PATH, "r", encoding="utf-8") as f:
            source = f.read()
        tools = {t["name"]: t for t in _tools_from_source(source)}
        desc = tools["memory_imprint"]["description"]
        self.assertIn("縁", desc)
        self.assertIn("reject", desc)
        # 否定形 (「逐語である必要はなく…reject にはならない」) を通さない (QA m10)
        self.assertIn("逐語で現れる語でなければならず", desc)
        self.assertIn("熏習しない", desc)


class TestMcpServerDoesNotImportBackend(unittest.TestCase):
    """サーバ内に LLM を置かない: mcp_server.py は backend を import しない (AST pin)。
    ソースはファイルから直接読む (mcp SDK が無い環境でもこの pin だけは走る)。"""

    def _source(self):
        with open(MCP_SERVER_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def test_mcp_server_module_does_not_import_backend(self):
        self.assertFalse(imports_backend_at_top_level(self._source()))

    def test_mcp_server_does_not_import_backend_anywhere_including_nested_functions(self):
        # MED-2: トップレベルだけでなく、関数内に隠された遅延 import も見逃さない
        # 厳格版 (ast.walk)。mcp_server.py 専用 (engine.py の propose() は正当な
        # 遅延 import を持つため対象外)。
        self.assertFalse(imports_backend_anywhere(self._source()))

    def test_debuglog_source_does_not_import_backend_anywhere(self):
        # debuglog は mcp_server から (間接にも) 読まれうる stdlib のみのモジュール。backend を
        # import しない (トップレベルにも関数内にも)。
        path = os.path.join(os.path.dirname(MCP_SERVER_PATH), "debuglog.py")
        with open(path, "r", encoding="utf-8") as f:
            self.assertFalse(imports_backend_anywhere(f.read()))

    def test_mcp_server_source_declares_new_life_false(self):
        # 生を進めない契約はエンジン構築時の引数1つに懸かっている。実行時テスト
        # (life が進まない) に加え、ソース上でも new_life=False 以外の構築が無いことを pin。
        source = self._source()
        constructions = re.findall(r"MemoryEngine\(([^)]*)\)", source)
        self.assertTrue(constructions)
        for args in constructions:
            self.assertIn("new_life=False", args)

    @requires_mcp
    def test_backend_module_not_loaded_after_a_tools_call_roundtrip(self):
        # MED-2: モジュールを import しただけでなく、実際に tools/call を1回処理
        # した「後」でも raiya.backend が sys.modules に無いことを確認する
        # (関数内の遅延 import は import 文自体を実行するまで sys.modules に
        # 現れないため、import するだけの確認では捉え損ねる余地がある)。
        # 確認はサブプロセス内で完結させる (テストプロセス側の sys.modules を見ても
        # 別プロセスなので無意味なため)。公式クライアントをプロセス内で直結する。
        tmpdir = tempfile.mkdtemp()
        try:
            env = make_env(tmpdir)
            code = (
                "import sys, anyio\n"
                "from raiya import mcp_server\n"
                "from raiya.engine import MemoryEngine\n"
                "from mcp.client import Client\n"
                "async def main():\n"
                "    async with Client(mcp_server.build_server(MemoryEngine(new_life=False))) as client:\n"
                "        result = await client.call_tool('memory_recall', {'text': 'x'})\n"
                "        assert not result.is_error, result\n"
                "anyio.run(main)\n"
                "print('raiya.backend' in sys.modules)\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env=env,
                cwd=REPO_ROOT,
                timeout=30,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    @requires_mcp
    def test_backend_module_never_loaded_by_importing_mcp_server(self):
        # AST だけでなく実行時の sys.modules でも確認する (「読み込まれていない」の
        # より強い保証)。サブプロセスで独立に検証する (同一プロセス内の import 順序に
        # 依存しない)。
        env = make_env(tempfile.mkdtemp())
        code = "import sys; from raiya import mcp_server; print('raiya.backend' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
