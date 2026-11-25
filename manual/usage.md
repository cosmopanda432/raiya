# 使い方 (ライブラリ / CLI / MCP サーバ)

[README に戻る](../README.md)

## 3つの使い方 (記憶機能設計書§4.1)

### 1. ライブラリとして

```python
from raiya import MemoryEngine
from raiya.sansho import Turn

engine = MemoryEngine(state_dir="./my_state")  # 省略時は RAIYA_STATE_DIR か "./state"

# 想起: text (+直近の文脈窓) -> 発芽した種子 + 記録の索引 + 末那識の注入文 (決定論)
recalled = engine.recall("コーヒーについて教えて", context=["さっきの話の続き"])
print(recalled["seeds"], recalled["index"], recalled["narrative"])

# 熏習: 往復本文 + 提案(evidence 付き) -> 決定リスト (決定論。三性ゲートで判定)
turn = Turn(user_text="私はコーヒーが好きです", assistant_text="いいですね")
proposals = [
    {
        "content": "コーヒーが好き",
        "conditions": ["コーヒー"],
        "polarity": "善",
        "evidence": {"source": "user", "span": "私はコーヒーが好きです"},
    },
]
decisions = engine.imprint(turn, proposals)  # [{"decision": "圓成實", ...}]

# 記帳: 往復を追記専用の台帳へ (決定論・LLM 不使用)。統合の材料になる
recorded = engine.record_turn("私はコーヒーが好きです", "いいですね")  # {"ledger_id": 1, "pending": 1}

# 監査: クラス別件数・quarantine 保持/累計・台帳の進捗・imprint_log 末尾 (決定論)
report = engine.audit()

# LLM で提案を生成したい場合のみ (CLI モード専用・backend への依存はこのメソッドの
# 呼び出し時に遅延 import される)
proposals = engine.propose(turn)

# 統合: 台帳の未統合の窓 + 窓の語に触れる既存の記録 (最大 30 件) + 群の見出し (最大 20 群)
#       -> 5 kind の提案 -> 三性ゲート -> 保存 (target_id は種子の # id だけ。群は target にならない)
# (LLM を使う2つ目の動詞。窓が空なら backend を import すらしない)
result = engine.consolidate()  # {"window": [1, 2, ...], "proposals": [...], "decisions": [...], "skipped": None}
```

facade (`raiya.engine.MemoryEngine`) の中核の動詞は次の9つ (記憶機能設計書§4.2・改修4 §7)。
`propose` と `consolidate` 以外はすべて決定論で、LLM (`backend.py`) に一切依存しない。

| 動詞 | 入力 | 出力 | LLM |
|---|---|---|---|
| `recall(text, context=None, project=None, session_start=False, session_id=None)` | 文字列 + 文脈窓 (文字列リスト) + project (`None` は全域の種子だけ) + 最初の想起か | `{"seeds": [...], "narrative": str, "index": [...], "resume": ..., "circles": {...}, "alaya_warning": str\|None, "manas_warning": str\|None}` (`resume` = 再開点。`circles` = `siblings`/`groups`/`global` は各円の項目のリスト、`cut`/`chars`/`max_chars` は容量で落とした件数・字数・上限の数値、`shortened` は ① で縮めて描いた種子の id = `no_summary` (要約を外した) / `name_only` (名だけ)) | 不使用 |
| `imprint(turns, proposals, project=None)` | `Turn` または `Turn` の列 + 提案リスト | 決定リスト (`decision` = 圓成實/依他起/遍計所執/reject/取り下げ照合/双子照合) | 不使用 |
| `record_turn(user_text, assistant_text, session_id=None, grounding_text=None, project=None, tool_refs=None)` | 往復本文 | `{"ledger_id": int, "pending": int}` | 不使用 |
| `consolidate(notes=None, debug=None)` | なし (台帳の未統合窓を読む) | `{"window": [台帳id], "window_sessions": int, "proposals": [...], "decisions": [...], "skipped": None/"empty"/"already_consolidated"/"partial_overlap"/"unparseable"}` | **使用** (CLI/hooks 専用) |
| `propose(turn, notes=None)` | `Turn` | 提案リスト (evidence 付き) | **使用** (CLI モード専用) |
| `introspect_material()` | なし | `{"manas_log": [...], "germinated_history": [...], "alaya_warning", "manas_warning"}` (読むだけ・#71) | 不使用 |
| `record_introspection(report)` | 自己申告文 | `{"ego_weight": float}` | 不使用 |
| `compact(window_ledger_ids, text, project=None, budget_chars=None)` | 窓の台帳 id のリスト + 今の発話 + project + 容量 (`None` = 上限なし) | `{"keep": [台帳id], "elided": [{"from","to","n","line"}], "unknown_ids": [...], "narrative": str, "turns": [...], "seeds": [...], "cut": {"germinated","resume","recent"}, "chars", "budget_chars", "window_sessions", "ledger_warning", "alaya_warning"}` (読むだけ・要約は作らず逐語を残す。MCP には出ない) | 不使用 |
| `audit()` | なし | `{"life", "seed_count", "class_counts", "kind_counts", "project_counts", "superseded_count", "withdrawn_count", "quarantine_retained", "quarantine_total", "ledger": {"turns", "consolidated_upto", "pending"}, "basis_counts", "log_check", "ledger_warning", "imprint_log_warning", "alaya_warning", "manas_warning", "history_total", "imprint_log_tail"}` | 不使用 |

上の9つに加え、人間だけが使う訂正の動詞 `withdraw_seed` / `replace_seed` / `restore_seed`
(種子の取り下げ・置換・復帰。`raiya-correct` 経由で、MCP には出さない) と、読むだけの観測の動詞
`inspect_seeds` / `injection_log` / `seed_provenance` / `explain_recall` (`raiya-inspect` 用。
何も書かない) がある。

### 2. CLI として

```bash
python3 -m raiya.cli
# または (pip install -e . 後)
raiya-cli
```

対話コマンド: `/種子`(種子倉庫一覧) `/透視`(直前応答の内部状態) `/内観`(自己監査+
ego_weight 減少) `/終`(終了。阿頼耶識は残る)。

CLI の熏習は**一往復の `propose()`** のままである (台帳を統合する `consolidate()` を呼ぶのは
hooks だけ。設計 §9)。改修5 からは `propose()` も統合と同じ 5 kind (決定/回避策/所在/再開点/
性向) の説明 (`engine.KIND_GUIDE`) で記録を出し、CLI は起動時 cwd の git トップを `project` と
して `recall(project=…, session_start=最初の往復)` と `imprint(project=…)` に渡す (hooks の stdin
`cwd`・MCP stdio の `os.getcwd()` と同じ `resolve_project`)。したがって CLI で対話しても注入
される索引は kind 別・project 別に絞られ、性向は `RAIYA_INJECT_DISPOSITIONS=1` の時だけ出る
(hooks と同じ挙動)。hooks 経由の「台帳を統合した記録」と CLI 経由の「一往復の記録」は同じ倉庫に
同居する -- 分けたければ常用する `RAIYA_STATE_DIR` と CLI で試す `RAIYA_STATE_DIR` を分けるとよい。

### 3. MCP サーバとして (記憶機能設計書§4.3)

公式 MCP Python SDK (`mcp` 2.x) 上の標準実装。stdio と Streamable HTTP の両 transport で
同じツール群を公開する。

```bash
pip install -e ".[mcp]"

# stdio (既定): Claude Code 等がサブプロセスとして起動する
raiya-mcp-server                      # または python3 -m raiya.mcp_server

# Streamable HTTP: 常駐させて複数クライアント/別プロセスから接続する
raiya-mcp-server --transport http     # http://127.0.0.1:8765/mcp (--host / --port で変更)
```

ブラウザで読むだけ見たいときは `raiya-inspect serve` (127.0.0.1:8766・GET のみ・状態は書かない (空の `.lock` が無ければ作られるだけ)。
`show`/`injected`/`why`/`explain` と同じ文面を表示。MCP の 8765 とは別ポート。`project` を空にすると /show は全プロジェクト、/explain は serve を起動したプロセスの cwd 扱いで、全域だけは `global=1`)。

initialize/initialized のハンドシェイク・`protocolVersion` の交渉・`ping`・`tools/list`・
`tools/call`・JSON-RPC のエラー形・Streamable HTTP の HTTP 層はすべて SDK が処理する。

`memory_recall` / `memory_imprint` は任意引数 `project` (git の最上位の絶対パス) を取る (改修5 §4.2)。
省略時、stdio はサーバを起動した cwd から `resolve_project` した値を既定にし、HTTP は無し = 全域
(`project=null`) の種子のみを返す (§12.3: 絞り込み無しにはしない)。`memory_imprint` の提案は任意の
`kind` を持てる (欄が無ければ 性向)。
`raiya/mcp_server.py` が持つのはツール定義 (`TOOLS`)・ツール名から `MemoryEngine` の動詞への
ディスパッチ・transport の起動だけで、一般的な MCP クライアント (Claude Code / Claude Desktop /
Cursor / Codex CLI / 公式 `mcp` クライアント等) がそのまま接続できる。ツール実行時のエラー
(未知のツール名等) は JSON-RPC error ではなく `isError: true` の結果として返す。

**サーバ内に LLM を置かない** -- `mcp_server.py` は `backend` モジュールを一切 import しない
(`tests/test_mcp_server.py` の `TestMcpServerDoesNotImportBackend` が AST 静的解析と実行時
`sys.modules` 確認の両方で pin している)。LLM で `evidence` 付きの提案を作るのはホスト側
(Claude Code 等) の仕事で、このサーバは `memory_imprint` で**検収だけ**する。
`memory_imprint` の往復本文 (`turn`) は呼び出し側の自己申告なので、接地の確かさは呼び出し側の
誠実さまでしか保証しない。それでも台帳と同じ規則は当てる: `<private>` と貼り付けブロックの中は
根拠にならず、`<elided N chars>` は引用も跨ぎもできず、U+FFFC を含む提案は遍計所執になる。

**生 (life) はここでは進めない**: stdio/HTTP どちらもエンジンを `MemoryEngine(new_life=False)`
で構築する。生の管理は hooks の `recall` (Claude Code セッションの最初のプロンプト) に
一本化している (下記[「Claude Code hooks」](hooks.md)節参照)。理由: Claude Code セッションごとの
サーバ起動+`claude mcp list` 等の health check (initialize だけで終了する起動) のたびに生を
進めていた旧実装では、hooks の「1生=1セッション」と二重計上され、当時あった枯死 (連続
不発芽での減衰・削除) が実質2倍速で走る事故が本番で起きた。枯死は改修4 で撤去したので
生の二重計上で種子が消えることはもう無いが、`planted_at`/`miss_streak` と末那識の注入ログ
は今も生を単位に数えるため、意味論は保つ。hooks を使わない運用では MCP 経由では生が
進まなくなるが、`memory_audit` で現在の life を観測できれば足りる。

HTTP transport の注意:

- stateless (セッション id を持たない)。state はすべて `RAIYA_STATE_DIR` のディスク上にあり、
  動詞ごとに flock+再読込するので、複数クライアントが同時に叩いても直前の書込を取りこぼさない
  (下記[「多重書き手対策」](state.md)節)。サーバを再起動してもクライアント側の登録はそのまま使える。
- 既定は `127.0.0.1` にバインドし、localhost のときは SDK の DNS リバインディング保護が
  自動で有効になる。認証は持たないので、`--host 0.0.0.0` 等で LAN/外部に公開する場合は
  前段 (リバースプロキシ等) で保護すること。
- 1 リクエストの上限は両 transport 共通で `MAX_REQUEST_BYTES` (4 MiB)。stdio は引数の UTF-8 バイト数が
  超えると `isError` (上限を示す日本語の文) で拒否し、HTTP は本文が超えるとハンドラの前に SDK が
  `413 Request Entity Too Large` で弾く (transport 層の応答なので `isError` にはならない。HTTP は JSON-RPC の
  封筒の分だけ閾値が数百バイト低い)。
- `RAIYA_STATE_DIR` はサーバ起動時の環境変数で与える (stdio ではクライアント側の env 設定)。
  起動ログ (uvicorn) は stderr に出る。

tools: `memory_recall` / `memory_imprint` / `memory_audit` / `memory_introspect_material` /
`memory_record_introspection` (引数の形は `raiya/mcp_server.py` の `TOOLS` を参照)。5つの
ツール名と必須引数は改修4 でも変えていない。`memory_recall` には任意の `context`
(直近のプロンプト等の文脈窓・文字列配列) を足せる。境界で型を正す (配列でなければ空配列
として扱い、文字列でない要素は落とす) ため、壊れた `context` でツール呼び出しが失敗する
ことはない。
`memory_imprint` の `arguments` は次の形:

```json
{
  "turn": {"user_text": "...", "assistant_text": "..."},
  "proposals": [
    {
      "content": "...",
      "conditions": ["..."],
      "polarity": "善 or 不善 or 無記",
      "evidence": {"source": "user or assistant", "span": "本文からの逐語引用"}
    }
  ]
}
```

#### Claude Code への登録例

```bash
# stdio: venv の console script を直接指定する (RAIYA_STATE_DIR は -e で渡す)
claude mcp add raiya -e RAIYA_STATE_DIR=$HOME/.raiya -- <clone>/.venv/bin/raiya-mcp-server

# HTTP: 別途 `RAIYA_STATE_DIR=$HOME/.raiya raiya-mcp-server --transport http` を常駐させておく
claude mcp add --transport http raiya http://127.0.0.1:8765/mcp
```
