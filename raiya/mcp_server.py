"""mcp_server.py -- MCP サーバ (記憶機能設計書§4.3)。公式 MCP Python SDK (`mcp`) 上の標準実装。

transport (どちらも同じツール群を公開する):
    stdio (既定)      `raiya-mcp-server` / `python3 -m raiya.mcp_server`
    Streamable HTTP   `raiya-mcp-server --transport http [--host 127.0.0.1] [--port 8765]`
                      エンドポイントは http://<host>:<port>/mcp (stateless: セッション id 無し。
                      サーバを再起動してもクライアント側の接続情報が無効にならない)

**サーバ内に LLM を置かない**: このモジュールは `backend` を import しない (T9 の AST pin
で確認する)。ホスト側 (Claude Code 等) が LLM で `evidence` 付きの提案を作り、このサーバは
`memory_imprint` で**検収だけ**する (提案者と検収者の分離=CON-5 自証分)。

プロトコル処理 (initialize/initialized のハンドシェイク、protocolVersion の交渉、ping、
tools/list、tools/call、JSON-RPC のエラー形、Streamable HTTP の HTTP 層) はすべて SDK に
委ねる。このモジュールが持つのは (1) ツール定義 `TOOLS`、(2) ツール名 -> `MemoryEngine`
の動詞へのディスパッチ `_handle_tool_call`、(3) transport の起動 (`main`)、の3つだけ。

**生 (life) はここでは進めない**: エンジンは必ず `MemoryEngine(new_life=False)` で構築する。
生の管理は hooks の recall (Claude Code セッションの最初のプロンプト) に一本化してある。
種子は削除も減衰もされない (改修4); それでも生 (life) の二重計上は `planted_at`/`miss_streak`/
`last_fired` の意味を壊すので、サーバ起動や health check で生を進めない規律は維持する。
hooks を使わない運用では MCP 経由では生が進まなくなるが、`memory_audit` で life を観測
できれば足りる (README の「Claude Code hooks」節・「MCP サーバとして」節に明記)。

tools: memory_recall / memory_imprint / memory_audit / memory_introspect_material /
       memory_record_introspection (§4.3)。

`project` (改修5 §4.2・§12.3): MCP には要求ごとの cwd が無いので `memory_recall` / `memory_imprint` に
任意引数 `project: string` を足した (境界で強制変換: 非 str → None)。引数が無ければ stdio は
`resolve_project(os.getcwd())` を既定にし (`build_server(engine, default_project=…)`)、HTTP は None。
None のとき recall は全域 (`project=null`) の種子のみを返す (絞り込み無しにはしない)。
"""
import argparse
import json
import os
import sys

from . import config, sansho
from .engine import MemoryEngine, StateNeedsMigration, StateUnreadable
from .project import resolve_project

try:
    import anyio
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
except ImportError as e:  # pragma: no cover - 環境依存
    raise ImportError(
        "raiya-mcp-server には公式 MCP Python SDK (`mcp` 2.x, Python 3.10+) が必要です: "
        "pip install -e '.[mcp]'"
    ) from e

SERVER_NAME = "raiya"
SERVER_VERSION = "0.1.0b1"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
HTTP_PATH = "/mcp"

TOOLS = [
    {
        "name": "memory_recall",
        "description": (
            "text から発芽した種子(ラベル付き)と末那識の注入文を返す。決定論。"
            "context: 直近のプロンプト等の文脈窓 (任意)。縁は text と各 context をそれぞれ照合する。"
            "project: 現在の project (git の最上位の絶対パス・任意)。省略時は stdio ならサーバの cwd から、"
            "それ以外は全域の種子のみ。"
            "session_start: true のとき、その project の再開点を resume に載せる (任意・セッションの最初の"
            "呼び出しで 1 度だけ true にする。省略/true 以外は false 扱い)。"
            "alaya_warning / manas_warning: 種子倉庫・末那識の保存ファイルが破損していた呼び出しにだけ文字列が入る"
            " (それ以外は null。同じ内容は stderr にも出る)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "context": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string"},
                "session_start": {"type": "boolean"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_imprint",
        "description": (
            "往復本文+提案(evidence 付き)を三性ゲートで判定し熏習する。決定論。"
            "サーバは検収だけを行う(提案の身分は自称できない=EXE-15)。"
            "evidence は各 proposal の必須項目(§3.2 evidence 必須化)。ただしサーバは"
            "proposal の中身はスキーマ違反で拒否するのではなく fail-safe に倒す: evidence 自体が欠落・"
            "span が非接地であれば三性ゲートが遍計所執と判定し quarantine へ送る"
            "(捏造を「エラーで弾く」のではなく「記録した上で隔離する」)。"
            "conditions(縁)は往復本文(user または assistant)に逐語で現れる語でなければならず、"
            "縁が全て非接地の提案は decision=\"reject\" となり熏習しない。"
            "kind (決定/回避策/所在/再開点/性向・任意) は形の検査で読み替え/却下されうる。欄が無ければ 性向。"
            "回避策 はユーザーの訂正と 行為 → 結果 の対(別の span 2 本)が要り、1 往復の窓ではほぼ 性向 に落ちる(利用者が原因と回避を 1 本の発話 (span) で述べた場合は例外で、その発話を span に含めれば 回避策 になる)。"
            "project (任意) は種子の project になる。"
            "turn は呼び出し側が渡す自己申告の本文なので、根拠の確かさは呼び出し側の誠実さまで。"
            "<private>…</private> と <pasted_content id=…>…</pasted_content id=…> の中は根拠にならない"
            "(<private> は両側から除く)。<elided N chars> は切り詰めの印で、引用も跨ぎもできない。"
            "span か縁に U+FFFC を含む提案は丸ごと遍計所執として quarantine する。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "turn": {
                    "type": "object",
                    "properties": {
                        "user_text": {"type": "string"},
                        "assistant_text": {"type": "string"},
                    },
                    "required": ["user_text", "assistant_text"],
                },
                "proposals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "kind": {"type": "string"},
                            "conditions": {"type": "array", "items": {"type": "string"}},
                            "polarity": {"type": "string"},
                            "evidence": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string"},
                                    "span": {"type": "string"},
                                },
                            },
                        },
                        "required": ["content", "evidence"],
                    },
                },
                "project": {"type": "string"},
            },
            "required": ["turn", "proposals"],
        },
    },
    {
        "name": "memory_audit",
        "description": "現在の生 (life)・クラス別件数・quarantine 保持/累計・台帳の進捗 (turns/consolidated_upto/pending) と警告 (ledger_warning)・history 件数合計・imprint_log 末尾と警告 (imprint_log_warning)・alaya_warning / manas_warning (状態は 1 ファイル raiya.db。警告は本文を解釈できなかった行の件数で、その行だけ読み飛ばす。退避も修復もしないので、人が直すまで呼ぶたびに同じ件数が出る。audit は quick_check も走らせ、ok でなければ alaya_warning に足す)・未統合の行の project × session_id 別件数 (basis_counts)・hooks.log の ledger_id= と台帳の最終 id の照合 (log_check) を返す。決定論。audit は何も書かない (読むだけ)。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_introspect_material",
        "description": "末那識ログ+発芽履歴 (manas_log / germinated_history) と alaya_warning / manas_warning (raiya.db の本文を解釈できなかった行の件数。その行だけ読み飛ばし、退避も修復もしないので、人が直すまで呼ぶたびに同じ件数が出る) を返す。決定論。何も書かない (読むだけ)。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_record_introspection",
        "description": "内観の自己申告文を記帳し ego_weight を減少させる。決定論。report は空でない文字列 (空・空白のみ・非文字列は記帳せず ego_weight も変えずにエラー)。",
        "inputSchema": {
            "type": "object",
            "properties": {"report": {"type": "string"}},
            "required": ["report"],
        },
    },
]


def _project_argument(arguments, default_project):
    """`project` 引数の境界での強制変換 (改修5 §4.2): str ならそれ、無ければ既定、それ以外は None。"""
    if "project" in arguments:
        value = arguments.get("project")
        return value if isinstance(value, str) and value else None
    return default_project if isinstance(default_project, str) else None


# 1 リクエストの上限 (バイト)。SDK の Streamable HTTP 既定 (`max_request_body_size` = 4 MiB) と同じ値だが、
# raiya として明示し、両 transport で共有する。
# - HTTP: 本文 (JSON-RPC の封筒を含む) がこれを超えると SDK がハンドラの前に 413 で弾く。
#   transport 層の応答なので isError には変えられない (クライアントには不透明なエラーに見える)。
# - stdio: SDK に上限が無いので `_handle_tool_call` が arguments の UTF-8 バイト数で検査し、
#   超過は ToolArgumentError (isError) にする。封筒の分 (数百バイト) だけ HTTP と閾値がずれるが許容する。
MAX_REQUEST_BYTES = 4 * 1024 * 1024


class ToolArgumentError(ValueError):
    """スキーマに合わない引数 (型違い・空の report 等)。`on_call_tool` が isError の結果に載せる。"""


def _tool_schema(name):
    for tool in TOOLS:
        if tool["name"] == name:
            return tool["inputSchema"]
    return None


def _check_argument_names(name, arguments):
    """未知の引数名・必須引数の欠落/null を ToolArgumentError にする (#108、利用者裁定 A)。

    許す名前と必須は `TOOLS` の inputSchema から導く (一覧を二重に持たない)。未知名は昇順で列挙する。
    提案の中身 (proposals[i] の欠落/余分なキー) と turn の余分なキーは対象外。
    """
    schema = _tool_schema(name)
    if schema is None:
        return
    allowed = list(schema["properties"])
    unknown = sorted((k for k in arguments if k not in schema["properties"]), key=str)
    if unknown:
        raise ToolArgumentError(
            "{} に未知の引数があります: {} (受け付ける引数: {})".format(
                name, ", ".join(str(k) for k in unknown), ", ".join(allowed) if allowed else "なし"
            )
        )
    for key in schema.get("required", []):
        if arguments.get(key) is None:
            raise ToolArgumentError("{} の {} は必須です".format(name, key))


def _str_argument(container, key, label, default=""):
    """str 引数の型検査: str はそのまま、それ以外は ToolArgumentError。

    欠落/null の `default` は保険で、MCP の呼び出し元 (text・turn.user_text・turn.assistant_text) は
    どれも先に必須の検査 (#108) を通るので、この枝には来ない。
    """
    value = container.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolArgumentError("{} は文字列で指定してください (受け取った型: {})".format(label, type(value).__name__))
    return value


def _check_proposal_types(i, p):
    """提案 1 件の各欄の**型**だけを検査する (ToolArgumentError に `proposals[i].field` を載せる)。

    欠落・null は通す (提案の中身の話。トップレベル引数は別; evidence 欠落/span 欠落は三性ゲートが遍計所執として quarantine する)。
    evidence は dict または dict の列を受け、その `source`/`span` が在れば str であること。
    evidence 自体の形の崩れ (非 dict/非 list・非 dict 要素) は検査せずゲートへ渡す
    (`sansho.evidence_list` が空 span に落として遍計所執にする)。
    """

    def _bad(field, expected):
        return ToolArgumentError("proposals[{}].{} は{}で指定してください".format(i, field, expected))

    if p.get("content") is not None and not isinstance(p["content"], str):
        raise _bad("content", "文字列")
    conditions = p.get("conditions")
    if conditions is not None and (
        not isinstance(conditions, list) or not all(isinstance(c, str) for c in conditions)
    ):
        raise _bad("conditions", "文字列の配列")
    for field in ("kind", "polarity"):
        if p.get(field) is not None and not isinstance(p[field], str):
            raise _bad(field, "文字列")
    evidence = p.get("evidence")
    items = evidence if isinstance(evidence, list) else [evidence]
    for j, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        label = "evidence[{}]".format(j) if isinstance(evidence, list) else "evidence"
        for field in ("source", "span"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise _bad("{}.{}".format(label, field), "文字列")


def _handle_tool_call(engine, name, arguments, default_project=None):
    """ツール名 -> MemoryEngine の決定論の動詞。未知のツール名は KeyError。

    トップレベルの引数は厳密に検査する (#108、利用者裁定 A): `arguments` が None なら `{}`、dict 以外
    (str・list (`[]` を含む)・数値) は `ToolArgumentError` (サイズ検査・engine の前)。未知の引数名、必須引数 (`TOOLS` の `required`、
    imprint の turn.user_text / turn.assistant_text を含む) の欠落・null は `ToolArgumentError` (isError)。
    空文字列 "" と空配列 [] は指定済みなので通る。一方 **proposal の中身**は機械的に弾かず、欠落・null は
    そのまま三性ゲートまで通す (fail-safe: 非接地・欠落は遍計所執として quarantine に記録される。
    proposal / turn の余分なキーは読まれないだけで拒否しない)。ただし**型が違う値**
    (text=1・turn="x"・proposals="x"・proposal 要素が dict でない等) は、握りつぶして空として
    通すと呼び出し側の誤りが見えなくなり、素通しすると Python の生の例外文が返るので、
    `ToolArgumentError` (日本語の明確な文) で isError にする。検査はすべて engine を呼ぶ前に行う
    (一部の提案だけ熏習される中途半端な状態を作らない)。
    - memory_recall: text 欠落/null -> エラー。text が非 str -> エラー。context / project / session_start は
      強制変換 (非 list -> []・非 str 要素は捨てる / 非 str -> None / 真の bool `True` だけが session_start)。
    - memory_imprint: turn / proposals 欠落/null -> エラー。turn が非 dict -> エラー。user_text/assistant_text
      が欠落/null/非 str -> エラー。proposals が非 list・要素が非 dict・要素内の content/conditions/kind/polarity/
      evidence.source/span の型違い (欠落/null は可) -> エラー。
    - memory_record_introspection: report が欠落・非 str・空/空白のみ -> エラー (記帳せず ego_weight も不変)。
    `default_project`: `project` 引数が無いときの既定 (stdio = サーバの cwd から。HTTP = None)。
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError("引数はオブジェクトで指定してください (受け取った型: {})".format(type(arguments).__name__))
    try:
        size = len(json.dumps(arguments, ensure_ascii=False, default=repr).encode("utf-8", "surrogatepass"))
    except (TypeError, ValueError, RecursionError):
        # arguments は JSON 由来なので通常は起きない。循環参照・非 str キー・再帰過多などで直列化できない値は
        # 大きさを測れないため拒否する (孤立サロゲートは surrogatepass で測れるので拒否しない)。
        raise ToolArgumentError("引数を JSON として扱えないため大きさを検査できません")
    if size > MAX_REQUEST_BYTES:
        raise ToolArgumentError(
            "引数が大きすぎます ({} バイト > 上限 {} バイト)。分割して送ってください".format(size, MAX_REQUEST_BYTES)
        )
    if name in {t["name"] for t in TOOLS}:
        _check_argument_names(name, arguments)
    if name == "memory_recall":
        text = _str_argument(arguments, "text", "text")
        context = arguments.get("context")
        if not isinstance(context, list):
            context = []
        context = [c for c in context if isinstance(c, str)]
        return engine.recall(
            text,
            context,
            project=_project_argument(arguments, default_project),
            session_start=arguments.get("session_start") is True,
        )
    if name == "memory_imprint":
        turn_arg = arguments["turn"]
        if not isinstance(turn_arg, dict):
            raise ToolArgumentError("turn はオブジェクト ({user_text, assistant_text}) で指定してください")
        for key in _tool_schema(name)["properties"]["turn"]["required"]:
            if turn_arg.get(key) is None:
                raise ToolArgumentError("memory_imprint の turn.{} は必須です".format(key))
        turn = sansho.Turn(
            user_text=_str_argument(turn_arg, "user_text", "turn.user_text"),
            assistant_text=_str_argument(turn_arg, "assistant_text", "turn.assistant_text"),
        )
        proposals = arguments["proposals"]
        if not isinstance(proposals, list):
            raise ToolArgumentError("proposals は提案オブジェクトの配列で指定してください")
        for i, p in enumerate(proposals):
            if not isinstance(p, dict):
                raise ToolArgumentError("proposals[{}] はオブジェクトで指定してください".format(i))
            _check_proposal_types(i, p)
        return engine.imprint(turn, proposals, project=_project_argument(arguments, default_project))
    if name == "memory_audit":
        return engine.audit()
    if name == "memory_introspect_material":
        return engine.introspect_material()
    if name == "memory_record_introspection":
        report = arguments.get("report")
        if not isinstance(report, str) or not report.strip():
            raise ToolArgumentError("report は空でない文字列で指定してください (記帳せず ego_weight も変えません)")
        return engine.record_introspection(report)
    raise KeyError("未知のツールです: {!r}".format(name))


def _text_result(text, is_error=False):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        is_error=is_error,
    )


def build_server(engine, default_project=None):
    """`engine` を公開する SDK の低レベル `Server` を組み立てる (transport 非依存)。

    テストでは `mcp.client.Client(build_server(engine))` でプロセス内直結できる。
    `default_project`: `project` 引数の無い呼び出しに使う既定 (stdio は `resolve_project(os.getcwd())`)。
    """

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in TOOLS])

    async def on_call_tool(ctx, params):
        try:
            result = _handle_tool_call(engine, params.name, params.arguments, default_project=default_project)
        except Exception as e:
            # ツール実行時エラーは JSON-RPC error ではなく isError=True の結果で返す
            # (MCP 仕様: ツールの失敗は LLM が見て回復できるよう result 側に載せる)。
            return _text_result(str(e), is_error=True)
        if isinstance(result, dict):
            # 破損の警告は stderr にも出す (stdout は protocol)。状態も重複除去も持たない。
            for key in ("alaya_warning", "manas_warning", "ledger_warning", "imprint_log_warning"):
                w = result.get(key)
                if isinstance(w, str) and w:
                    print("raiya-mcp-server: {}".format(w), file=sys.stderr)
        return _text_result(json.dumps(result, ensure_ascii=False))

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _engine_or_default(engine):
    return engine if engine is not None else MemoryEngine(new_life=False)


async def serve_stdio(engine=None):
    """stdio transport (改行区切り JSON-RPC 2.0)。stdin が閉じるまで1接続を捌く。"""
    # stdio の既定 project = サーバを起動した cwd の git 最上位 (Claude Code はプロジェクトの cwd で
    # サブプロセスとして起動する・§12.3)。
    server = build_server(_engine_or_default(engine), default_project=resolve_project(os.getcwd()))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve(engine=None):
    """stdio サーバ本体 (同期エントリ)。"""
    anyio.run(serve_stdio, engine)


def build_http_app(engine=None, host=DEFAULT_HTTP_HOST):
    """Streamable HTTP の ASGI アプリ (エンドポイント `HTTP_PATH`)。uvicorn 等で配信する。

    stateless: 1 リクエスト=1 接続として捌き、セッション id を持たない (state は
    すべて `RAIYA_STATE_DIR` のディスク上にあり、動詞ごとに flock+再読込するため、
    複数クライアント・複数セッションが同時に叩いても直前の書込を取りこぼさない)。
    `host` が localhost のとき SDK が DNS リバインディング保護を自動で有効にする。
    認証は持たないので、localhost 以外にバインドする場合は前段で保護すること。
    本文が `MAX_REQUEST_BYTES` を超える POST は SDK がハンドラの前に 413 で弾く (isError にはできない)。
    """
    server = build_server(_engine_or_default(engine))
    return server.streamable_http_app(
        streamable_http_path=HTTP_PATH,
        host=host,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
    )


def serve_http(engine=None, host=DEFAULT_HTTP_HOST, port=DEFAULT_HTTP_PORT):
    """Streamable HTTP サーバ本体 (同期エントリ)。uvicorn は `mcp` の依存として入る。"""
    import uvicorn

    uvicorn.run(build_http_app(engine, host=host), host=host, port=port)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="raiya-mcp-server",
        description="頼耶 (raiya) の MCP サーバ。stdio (既定) または Streamable HTTP で公開する。",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio: 標準入出力 (既定。Claude Code 等がサブプロセスとして起動する) / "
        "http: Streamable HTTP (http://HOST:PORT{} で待ち受ける)".format(HTTP_PATH),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HTTP_HOST,
        help="--transport http のバインド先 (既定 {})".format(DEFAULT_HTTP_HOST),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="--transport http の待受ポート (既定 {})".format(DEFAULT_HTTP_PORT),
    )
    args = parser.parse_args(argv)
    # 設定ファイルは起動時に 1 回。不正なら stderr に 1 行 (stdout は MCP のプロトコル用)。
    # state_dir が失われていれば ./state へ落とさず止める (設計書 §5.3)。
    if not config.startup_check("raiya-mcp-server"):
        sys.exit(2)
    try:
        if args.transport == "http":
            serve_http(host=args.host, port=args.port)
        else:
            serve()
    except StateUnreadable as e:
        # 起動時 (engine の構築) に状態 DB を読めない。stdout は protocol なので stderr へ 1 行。実行中の各ツール呼び出しで
        # 出たものは isError の結果になる (on_call_tool)。
        print("raiya-mcp-server: 状態DB 読めず category={}".format(e.category), file=sys.stderr)
        sys.exit(2)
    except StateNeedsMigration:
        print("raiya-mcp-server: 旧 JSON 版の state です (移行しない。脇に退けてから起動。何も作らずに終了)", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
