# Claude Code hooks

[README に戻る](../README.md)

## Claude Code hooks(自動想起・自動熏習、記憶機能設計書 波4・改修4 §8)

`raiya/hooks/claude_code.py` (`python3 -m raiya.hooks.claude_code <recall|stop|session_end>`)
を Claude Code の hooks に登録すると、会話のたびに手動で `/種子`・record_turn・consolidate
を呼ばなくても、想起の注入と台帳への記帳・統合が自動で走る。統合の単位は 1 往復ではなく
台帳の窓である (改修4。時間差のある統合は「熏習」とは呼ばない: 起動設計書 §7.1-7)。

### 仕組み

- **`UserPromptSubmit` (`recall`、同期・<1秒目安)**: ユーザーの発言 (prompt) と直前まで
  の文脈窓 (`RECALL_CONTEXT_TURNS` = 5 件) に対して `engine.recall(prompt, context=...)`
  を呼び (`project=`・`session_start=`・`session_id=` も渡す)、注入文 (発芽した種子 + 記録の索引。stdin の `cwd` から `project` を決め、セッション最初の
  prompt では再開点も出す。改修5) が空でなければ
  `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}`
  として stdout に返す (Claude Code がこれを会話コンテキストへ追加する)。発芽が無くても
  索引 (圓成實、決定系の kind は 依他起 も載り、user の span を持たないものだけ「(推定)」付き) や ④ の全域の群があれば出す。あわせて、prompt
  を `$RAIYA_STATE_DIR/sessions/<session_id>.json`
  (`{"prompt", "ts", "recent"}`) へキャッシュする (空/短い/`/`始まりの prompt でも書く。
  stop 側が transcript の非同期書込遅延を避けてこのキャッシュを主に読むため)。`recent` は
  次の想起の文脈窓になる。台帳は読まない (同期 hook の予算を守るため)。
- **`Stop` (`stop`、`async: true` 想定)**: **LLM は呼ばない**。直前の往復を
  `engine.record_turn()` で追記専用の台帳 (`raiya.db` の `ledger` 表) へ記帳するだけで、未統合の
  往復が `CONSOLIDATE_EVERY` (5) 件溜まったときにだけ `engine.consolidate()` (台帳の窓を
  まとめて読む統合。ここで LLM を 1 回呼ぶ) が走る。ユーザー本文は上記のセッション
  キャッシュを優先し、無ければ transcript の最後の実発話 (tool_result・`/`始まりを除く)
  へ fallback する。アシスタント本文は stdin の `last_assistant_message` を優先し
  (transcript は非同期書込で遅延しうるため)、無ければ transcript の最後の assistant 応答へ
  fallback する。空/短い/`/`始まりの skip 判定は stop 側でも独立に再度行う (キャッシュ
  への記録自体は skip しない)。`<private>…</private>` を除いてから、各本文を 8000 字に
  切り詰める: 先頭と末尾を残し、間に `<elided N chars>` の印を挟む (印は台帳に残る。切った
  ことは `hooks.log` にも 1 行残る)。貼り付け (`<pasted_content id="X">…`) を剥がした根拠欄
  `grounding_text` は、切る前の本文から作ってから切る (下記「台帳に何が入るか」)。応答が空の
  往復・貼り付け/private を除くと空になる往復・サブエージェント (`agent_id`) の往復は記帳
  しない (同節)。
- **`SessionEnd` (`session_end`、同期・`"async": true` を付けない)**: 台帳に残った未統合の
  往復を `engine.consolidate()` で統合する (窓が空なら LLM は呼ばれず、ログも出さない)。
  async にすると、セッション終了の後片付けで統合結果を書き出す前にプロセスごと kill され
  うるため、同期 (timeout 120) で登録する。
- **harness 由来の注入は台帳にも文脈窓にも入れない**: `<task-notification>` /
  `<system-reminder>` / `<local-command-stdout>` / `<agent-message>` (サブエージェントの報告) 等 (`HARNESS_TAGS`) は Claude Code が
  user 枠に差し込むテキストであってユーザー発話ではないので、想起の文脈窓からも台帳からも
  外す (これを入れると「ユーザーが逐語そう言った」=圓成實の意味が壊れる)。判定規則は
  どちらの経路でも同じ「先頭 (前後の空白を除く) のタグ名」だが、当てる単位が違う:
  セッションキャッシュ経由では prompt 文字列**全体**の先頭を見て skip 判定し
  (`_is_skippable_prompt`)、transcript fallback 経由では message.content の **text ブロック
  ごと**に落とす (`_extract_user_text_from_content`)。後者をブロック単位にしてあるのは、
  実発話の後ろに注入ブロックが足された user エントリを連結すると先頭がユーザー本文になり、
  全体判定では注入本文がすり抜けて台帳へ載るため。
- **再帰ガード**: `RAIYA_HOOK_ACTIVE=1` が env に立っていれば3つのサブコマンドとも即
  exit 0。既定の `openai-api` backend は子プロセスを起動しない (HTTP 呼び出しのみ) ため
  そもそも再帰しないが、`RAIYA_BACKEND=claude-cli` に切り替えた場合は `stop`/`session_end`
  が `engine.consolidate()` 経由で `claude -p` を subprocess として呼ぶので、その子プロセス
  の env に必ずこれを付ける。ガードは `backend._call_claude_cli` が起動する **全ての**
  `claude -p` (対話本文・内観を含む全 purpose) に付く: 子プロセスは常に新しい Claude Code
  起動で、ユーザー設定の raiya hooks が本番 `~/.raiya` に対して走ると、二重注入・幽霊
  session・smoke からの本番 consolidate が起きるため (Issue #26)。ここで即座に止まり
  無限ループもしない。
- **統合の子 claude の封じ込め** (`backend.CLAUDE_CLI_PROPOSE_ISOLATION`): 同じ経路の
  `claude -p` には `--tools "" --setting-sources "" --strict-mcp-config --disable-slash-commands
  --permission-mode dontAsk` を付ける。統合プロンプトには台帳の本文 (会話・貼り付け) が
  そのまま載るので、子は道具・ユーザー設定 (hooks / plugins / CLAUDE.md / 許可規則)・MCP
  (raiya 自身を含む)・skills を持たない = 本文に紛れた指示で手が出せず、会話以外の文章が
  提案の材料に混ざらない。契約 (OAuth) のまま動く (`--bare` は OAuth を読まないので使わない)。
  この完全な封じ込めは propose 専用。対話本文・内観 (purpose 無指定) の呼び出しには
  `--strict-mcp-config` だけを付け (ユーザー設定の MCP サーバを全て子から外す。raiya 自身を含むので、
  raiya-cli では対話中に他の MCP ツールも使えなくなる。env の再帰ガードは子が起動するものにも継承される)、
  `--tools` / `--setting-sources` / `--disable-slash-commands` / `--permission-mode` は付けない。
- **常に exit 0 (fail-soft)**: 例外は `$RAIYA_STATE_DIR/hooks.log` に1行追記して黙る
  (Claude Code のセッションを止めない。例外文字列そのものは書かない=クラス名・
  失敗カテゴリ・長さのみを記録する)。
- **単一書き手前提の緩和 (上記[「多重書き手対策」](state.md)節)**: hooks (短命プロセス。呼ばれる
  たびに新規 `MemoryEngine`) と MCP サーバ (長命・セッションごとに1本) が同じ
  `RAIYA_STATE_DIR` を同時に書いても、`state_dir/.lock` の flock で排他しつつ
  再読込してから操作・保存するため、直前の書込を取りこぼさない。
- **1 生=1 Claude Code セッション、生の起点は hooks の recall に一本化**:
  `MemoryEngine` は既定 (`new_life=True`) だと構築のたびに `begin_new_life()` を呼び
  新しい生を開始する (`cli.py` は対話1回の起動=1生のままこれは不変)。hooks は
  Claude Code のプロンプトのたびに新規 `MemoryEngine` を構築する短命プロセスのため、
  既定のままだと「1 ターン=1 生」になってしまい、生を単位に数えるもの
  (`planted_at` / 連続不発芽の観測カウンタ `miss_streak` / 末那識の注入ログ) の意味論が
  そのまま狂う。そのため hooks は `MemoryEngine(..., new_life=False)` で構築し、
  `recall` (UserPromptSubmit) がその Claude Code セッションの**最初のプロンプトのとき
  (`sessions/<session_id>.json` がまだ無いとき) だけ** `engine.begin_new_life()` を
  1回呼ぶ。`stop` は生を進めない。`session_id` が無い/不正な入力では生を進めない。
  これにより「1 生=1 セッション」が保たれる。**MCP サーバ (`mcp_server.py`) もこれに
  合わせて `MemoryEngine(new_life=False)` で構築する** (下記[「MCP サーバとして」](usage.md)節参照):
  以前はサーバ起動のたびに (`claude mcp list` の health check=initialize だけで終了する
  起動を含め) 生を進めていたため、hooks の「1生=1セッション」と二重計上され、当時あった
  枯死 (3 生連続不発芽で strength を減衰し 0 で削除する規則) が実質2倍速で走る事故が本番
  で実際に起きた。**枯死は改修4 で撤去した** (種子は削除も減衰もされない=KUN-3 恒隨轉)
  ので生の二重計上で種子が消えることはもう無いが、観測値が狂う点は変わらないので規律は
  維持する。hooks を使わない環境で MCP サーバのみを使う場合、MCP 経由では生が進まなく
  なる点に注意 (`memory_audit` で現在の life は観測できる)。

### 設定手順

1. `raiya/hooks/settings.example.json` を参考に、Claude Code の settings.json
   (`~/.claude/settings.json` 等) の `hooks.UserPromptSubmit` / `hooks.Stop` /
   `hooks.SessionEnd` へ同形のエントリを追加する (このリポは settings.json 本体を直接
   書き換えない)。example には3つとも入っている (`session_end` は改修4 で追加)。
   `SessionEnd` のエントリを省くと、セッション末尾の未統合の往復は次のセッションの
   `stop` が拾うまで台帳に残る。
2. `command` 内の `<clone>/.venv/bin/python` は「インストール」で作った venv の
   Python に、`RAIYA_STATE_DIR=$HOME/.raiya` は使いたい永続化先に合わせて調整する。
   `PYTHONPATH` は使わない (理由は[「インストール」](../README.md)の節)。
3. `Stop` 側は `"async": true` を付ける (5 往復に 1 回、統合の LLM 呼び出しを含むため
   同期実行だとユーザーの応答が待たされる)。`UserPromptSubmit` 側は決定論のみで高速
   (<1秒目安) なため同期のままでよい。`SessionEnd` 側は **`"async": true` を付けない**
   (timeout 120 の同期実行。async だと統合結果を書き出す前に kill されうる)。

### 設定・無効化

| 変数 | 既定 | 用途 |
|---|---|---|
| `RAIYA_HOOKS` | (未設定=有効) | `0` を設定すると3つのサブコマンドとも即 exit 0 (無効化) |
| `RAIYA_PROPOSE_MODEL` | backend 依存 (`openai-api`: `gpt-6-luna` / `claude-cli`: `haiku`) | 統合 (`consolidate`) の LLM 呼び出しが使うモデル。`RAIYA_MODEL` (対話本文用) とは独立 |
| `OPENAI_API_KEY` / `RAIYA_OPENAI_API_KEY` | (なし) | 既定の `openai-api` backend の API キー (後者を優先)。未設定だと統合が失敗し、失敗カテゴリ (reason) と本文長だけが `hooks.log` に記録される (API キーや応答本文そのものは書かない)。台帳への記帳は LLM を使わないので影響を受けず、未統合として残って次回の統合で拾われる |
| `RAIYA_HOOK_ACTIVE` | (hooks が内部で使用) | 再帰ガード。手動で立てると hooks 自体が丸ごと無効化される |

`hooks.log` は `$RAIYA_STATE_DIR/hooks.log` に生成される (各行: `<ISO8601> <セッション/件数/クラス内訳、または失敗理由>`。失敗理由は例外クラス名・`BackendError` の失敗カテゴリ (reason。定型文と operator の env 設定値=backend/model 名のみを含む)・本文長だけで、backend の生出力 (stderr / HTTP body 等) や会話本文そのものは書かない。改行は `\\n` にエスケープして 1 エントリ=1 行を保つ)。

### 設定ファイルと hooks

debug フラグ (`<state_dir>/debug`) は設定ファイルとは別の仕組みで、YAML には入れない。
設定ファイル (`config.yaml`) を置くなら、hooks を動かす python にも PyYAML が要る (ファイルが無ければ不要)。
`RAIYA_HOOK_ACTIVE=1` / `RAIYA_HOOKS=0` の guard は設定ファイルを読まずに終了する。設定が不正なときは
`hooks.log` に `config 不正 (<category/key/line だけの summary>) — 有効なキーと env で続行` を 1 行書く
(値や未知のキー名は書かない)。

### コストへの注意

LLM を呼ぶのは**統合 (`engine.consolidate()`) だけ**で、頻度は **`CONSOLIDATE_EVERY`
(5) 往復ごとに1回 + `SessionEnd` で1回** (既定 backend では OpenAI の `gpt-6-luna` への
API 呼び出し1回、`RAIYA_BACKEND=claude-cli` なら `claude -p` の起動1回)。改修4 以前は
毎往復 `engine.propose()` を呼んでいたので、同じ往復数あたりの呼び出しはおよそ 1/5 に
なる。これは通常の応答生成とは別課金/別呼び出しであり、全プロジェクトのセッション数・
往復数に比例して積み上がる点は変わらない。統合の prompt は台帳の窓を載せるぶん propose
より長い (各往復の片側を `CONSOLIDATE_TURN_CHARS` = 2000 字、窓は
`CONSOLIDATE_WINDOW_MAX` = 10 往復で切り詰め、応答は `CONSOLIDATE_MAX_TOKENS` =
`max_tokens` 4096 -- reasoning 系モデルでは思考トークンもこの上限に含まれるため
propose の 512 より大きく取ってある)。ただし `openai-api` backend は、統合・propose に限らず
CLI の対話・内観も含めて、呼び出し側の値によらず `max_completion_tokens` = 64000 を送る
(`backend.OPENAI_MAX_COMPLETION_TOKENS`、Issue #9)。より安いモデルにしたければ
`RAIYA_PROPOSE_MODEL` (例: `gpt-5.4-mini`) で差し替えられる。
不要なときは `RAIYA_HOOKS=0` で即座に無効化できる。

出力が上限で切れた (`finish_reason=length`) 場合や、そもそも JSON 配列として読み取れ
なかった場合、その統合は失敗/skip として扱われ**窓は未統合のまま残る** (`hooks.log` に
`consolidate失敗:` か ` skipped=unparseable` が 1 行)。同じ窓は次の統合が読み直すので
取りこぼしは無いが、**詰まっている間の再試行は「5 往復ごと」ではなく Stop のたびに 1 回**
になる: 窓が消費されないので `pending` が `CONSOLIDATE_EVERY` 未満へ戻らず、以後の Stop は
毎回この条件を満たす (`finish_reason=length` のときは `call_llm` の 1 回だけの再試行が
加わって 1 Stop あたり 2 回)。**有界なのはプロンプトの大きさ (`CONSOLIDATE_WINDOW_MAX`
= 10 往復) だけで、呼び出し頻度は改修4 以前の「毎往復 propose」と同じところまで戻る**
(例: 12 往復のうち健全なら 2 回のところ、詰まっていれば 8 回)。読める出力が 1 回返れば
窓は消費されて 5 往復ごとの周期に戻る。止めたいときは `RAIYA_HOOKS=0`。

なお `finish_reason=length` を失敗として扱うのは**熏習提案 (`propose`) と統合
(`consolidate`) の呼び出しだけ**である (出力が JSON 契約のため)。対話本文の生成と内観の
呼び出しは切れた応答をそのまま使う (人が読む文章は途中まででも使えるうえ、失敗にすると
再試行で呼び出しが倍になる)。

**台帳には会話本文が平文で残る**: 下記[「state」](state.md)節の台帳の扱いを読むこと。機密を扱う
セッションでは `RAIYA_HOOKS=0` で hooks ごと止めるのが確実。
