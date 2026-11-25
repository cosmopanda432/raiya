# 公開 API・環境変数・設定ファイル

[README に戻る](../README.md)

## 公開 API (凍結する契約、#61)

正式公開後に変えると利用者の state dir や接続コードを壊す面。列挙と規則の全文は
`2026-10-27-public-api-freeze-design.md`。ここに無いものは内容扱いで、受領書を付ければ変えてよい。

**面は三段**: ライブラリ公開の 9 動詞 (上の表: `recall` / `imprint` / `record_turn` / `consolidate` / `propose` /
`introspect_material` / `record_introspection` / `audit` / `compact`。動詞名・引数の名前と順序と既定値・戻り値の最上位キーと型・
例外のクラスを凍結) ⊃ MCP の 5 tool (名前・`inputSchema`・引数の変換規則・4 MiB 上限・isError の返し方・
`--transport stdio|http` と既定 `127.0.0.1:8765` `/mcp`) 、Claude Code hooks の 3 サブコマンド (登録の形、読む
stdin キー = recall `session_id` `prompt` `cwd` `agent_id` / stop `stop_hook_active` `session_id` `agent_id`
`transcript_path` `last_assistant_message` `cwd` / session_end `session_id`、`additionalContext` の封筒、常に exit 0、
`RAIYA_HOOK_ACTIVE` / `RAIYA_HOOKS` のガード)。提案 (入力) のキー `content` `conditions` `kind` `polarity` `evidence`
`action` `target_id` と「提案の `sansho` は誰も読まない」も凍結 (kind の集合は不変。利用者自身の「原因 → 回避」の言が 回避策 になる自述の入口 (#134 W1) は形の検査の拡張で、凍結の対象は動かさない)。`[project.scripts]` の 5 コマンド名と inspect /
correct の exit code 0/1/2 も凍結。
**`raiya-hook`** (汎用 hook の入口、#62。仕様の正本は pi 拡張設計書 §2): `raiya-hook <recall|record|consolidate|session_end|compact>` (`compact` は契約 v1.1 で追加・#63 スライス 4・読むだけ・仕様は pi 拡張設計書 §2.5.1 / §2.10)、
stdin に JSON object 1 つ (閉じた形・`contract_version: 1` 必須・未知の欄は拒否)、stdout に JSON object 1 つ
(`contract_version` と `outcome` = `ok` / `skip:<code>` / `fail:<例外クラス名>` を全動詞で持ち、`ok` のときだけ動詞ごとの欄)。
exit 0 = 処理した (skip と内部失敗を含む)、exit 2 = 契約拒否 (`outcome: refused:<version|field|type|verb|stdin>` と `field`、
`raiya.db` は作らない。state dir が既にあるときだけ hooks.log に 1 行。無ければ何も作らない)。`record` は任意の `tool_refs` を取る (上の台帳節の形、64 件・path 4096 バイト・name 256 字まで)。
検査の順は 再帰ガード (`RAIYA_HOOK_ACTIVE=1` / `RAIYA_HOOKS=0` は stdin を読む前に `skip:guard`) → 設定ファイル /
`state_dir` (失われていれば `skip:state_dir_lost`) → 旧 JSON 版の state (`skip:legacy_state`) → verb → stdin →
版 → 未知の欄 → 型 で、前の段で止まったときは何も書かない (不明な verb でも旧 state なら exit 2 ではなく skip)。
`session_id` / `cwd` は null を受けるが、`tool_refs` / `grounding_text` の null は型違反 (exit 2)。`compact` の `window_ledger_ids` / `prompt` / `budget_chars` も null は型違反 (exit 2)。契約 fixture: `tests/fixtures/raiya_hook_contract_v1/`。
`compact` は `window_ledger_ids` (int[]・必須・1000 件まで) と `prompt` (必須・skippable でも skip にしない) と任意の `budget_chars` (int ≥ 0。null・bool・負は `refused:type`) を取り、
`keep` / `elided` / `unknown_ids` / `narrative` / `cut` / `chars` / `budget_chars` / `window_sessions` / `project` / `warnings` を返す (何も書かない)。
**内部** (凍結しない): `withdraw_seed` / `replace_seed` / `restore_seed`、`inspect_seeds` / `injection_log` /
`seed_provenance` / `explain_recall`、`begin_new_life`、engine の属性、注入文
(`narrative` / `additionalContext`) の文面、`reason` と `*_warning` の文面、`circles` の要素の形、`hooks.log` /
`debug.log` の行、tool の description、数値の定数 (3200 字・5 件 …)。

**論理キー** (保存の器は #38 で `raiya.db` に替わったが、キーは凍結): 台帳の行 `id` `life` `session_id` `ts` `user_text`
`assistant_text` `grounding_text` (無し = `user_text` を根拠、`""` = 根拠なし) `project` `tool_refs` (任意、上の[台帳節](state.md))。
種子は上の「種子スキーマ」の欄 + `withdrawn`。台帳のカーソル `next_id` / `consolidated_upto` (`meta` 表の `ledger.next_id` / `ledger.consolidated_upto`)。

**互換規則** (コンテキスト設計書 S2 の継承):
- raiya が**出す**もの (台帳の行・種子・戻り値・MCP 結果) は、欠けてよい任意キーを足すことと、意味を変えずに列挙値を
  足すことを破壊的変更としない。読み手は欠けたキーを既定値で読み、知らないキー・知らない値を無視する。キーの削除・改名・
  型や意味や既定値の変更は破壊的変更。古い行は移行しない。古いコードが新しい行を読めること (前方互換) は約束しない。
- raiya が**受ける**もの (`config.yaml`、`raiya-hook` の stdin) は閉じた形で受けて未知の欄を拒むので、欄を足すことは
  その入力の版を上げる変更になる。
- 開いた列挙 (足すのは非破壊): `decision`、`skipped`、hooks の outcome。閉じた列挙 (足すのは破壊的): `sansho` の値、
  `VALID_KINDS`。
- 版番号は既存のものだけ: パッケージ `0.1.0b1` (`pyproject.toml` / `raiya.__version__` / `SERVER_VERSION`)、
  `config.yaml` の `version: 1`、`raiya-hook` の `contract_version: 1`。台帳の行・種子に版キーは置かない。

## 環境変数

| 変数 | 既定 | 用途 |
|---|---|---|
| `RAIYA_STATE_DIR` | `./state` | 上記 state の置き場 |
| `RAIYA_BACKEND` | `openai-api` | `openai-api` \| `claude-cli` \| `anthropic-api` \| `command` |
| `RAIYA_MODEL` | (backend 依存。`openai-api` は `gpt-6-luna`) | 対話本文用に backend へそのまま渡すモデル名 |
| `RAIYA_PROPOSE_MODEL` | (backend 依存。`openai-api` は `gpt-6-luna`) | 提案生成 (`propose` / `consolidate`) 専用のモデル名。`RAIYA_MODEL` とは独立 |
| `OPENAI_API_KEY` / `RAIYA_OPENAI_API_KEY` | (なし) | `openai-api` backend の API キー (後者を優先) |
| `ANTHROPIC_API_KEY` | (なし) | `anthropic-api` backend の API キー |
| `RAIYA_LLM_CMD` | (なし) | `command` backend で実行するシェルコマンド文字列 |
| `RAIYA_INJECT_DISPOSITIONS` | (なし) | `1` で性向を従来どおり注入する (改修5 §7 / §11.2。既定は注入しない)。`engine.recall` が読むので hooks と MCP の両方に効く |

### 設定ファイル

環境変数のほかに、利用者単位の YAML ファイル `config.yaml` でも設定できる
(設計書 `2026-10-23-config-file-design.md`)。
雛形は [`config.example.yaml`](../config.example.yaml) (推奨設定 (`state_dir: ~/.raiya`、`backend: anthropic-api`、`model: claude-haiku-5-5`) が有効な状態で入っており、コピーするとその設定になる):

```bash
mkdir -p ~/.config/raiya && cp config.example.yaml ~/.config/raiya/config.yaml
```

```yaml
version: 1                    # 省略可。書くなら int の 1
state_dir: ~/.raiya           # 絶対パス (~ は展開)               <- RAIYA_STATE_DIR
inject_dispositions: false    # bool                              <- RAIYA_INJECT_DISPOSITIONS
llm:
  backend: openai-api         # openai-api | claude-cli | anthropic-api | command  <- RAIYA_BACKEND
  model: null                 #                                   <- RAIYA_MODEL
  propose_model: null         #                                   <- RAIYA_PROPOSE_MODEL
  command: null               #                                   <- RAIYA_LLM_CMD
  timeout_sec: 60             # 0 < x <= 10**9 (ファイルのみ。socket.settimeout が溢れないための上限)
  openai_max_completion_tokens: 64000   # >= 1 (ファイルのみ・openai-api だけに効く)
```

- **置き場所** (最初に当たった 1 つだけ): `RAIYA_CONFIG` が空文字 → 使わない / `RAIYA_CONFIG` が
  パス → そのファイルだけ (絶対パス必須・無ければエラー) / それ以外 → `$XDG_CONFIG_HOME/raiya/config.yaml`
  (空・相対なら `~/.config/raiya/config.yaml`。無ければ黙って「設定ファイル無し」)。
  プロジェクト内 (cwd・git 最上位) と `<state_dir>/` は探さない: clone したリポジトリが `command` backend の
  シェルコマンドを選べてしまうため。
- **優先順位**: 明示の引数 (`raiya-inspect --state-dir` など) > 環境変数 (空でない値) > 設定ファイル > 既定。
  `RAIYA_INJECT_DISPOSITIONS=0` はファイルの `inject_dispositions: true` も打ち消す。
- **環境変数だけのもの** (ファイルに書くと `unknown_key`): `RAIYA_CONFIG`、`RAIYA_HOOK_ACTIVE`、`RAIYA_HOOKS`、
  API キー (`RAIYA_OPENAI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`)。
- **不正なファイル**: 壊れたキーはそのキーだけ捨てて残りを使う (ファイル全体が読めない/YAML が壊れている/
  `version` が不正のときは全体を捨てる)。記憶の動詞 (recall・記帳・MCP・inspect) は止まらず、LLM を呼ぶ経路
  (`propose` / `consolidate`) は止まる (`command` backend の `backend:` のタイプミスで台帳の本文が既定の
  openai-api へ送られないように)。ただし `state_dir` を決めるはずのファイルが壊れ、環境変数も引数も無いときは
  `./state` へ落とさず止める (hooks は何も書かずに exit 0、`raiya-cli` / `raiya-mcp-server` / `raiya-inspect` は
  stderr に 1 行書いて exit 2)。
- 設定ファイルは通常ファイルで 1 MiB 以下であること (FIFO などは `unreadable` として全体を捨てる)。
- 1 プロセスにつき 1 回、起動時に読む (hooks は 1 起動ごと)。

`backend.py` は CLI/hooks モード専用 (`engine.propose()`・`engine.consolidate()` と
`cli.py` の `/内観` からのみ呼ばれる)。facade の決定論動詞はこの環境変数群に一切依存しない。
