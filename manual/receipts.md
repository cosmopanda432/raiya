# テスト・receipts・パッケージ構成

[README に戻る](../README.md)

## テスト

```bash
cd <clone>
.venv/bin/python -m unittest discover
```

- `tests/test_mcp_server.py` / `tests/test_mcp_abnormal.py` -- MCP サーバ (stdio の生 JSON-RPC 往復、公式 `mcp` クライアントからの stdio/HTTP 往復、異常入力)。公式 `mcp` SDK が無い環境では AST pin 以外を skip。
- `tests/test_entry_points.py` -- 使い捨て venv へ `pip install -e ".[mcp]"` して `raiya-cli`/`raiya-mcp-server` を実走(ネットワークが無く build backend や `mcp` を取得できない環境では skip)。

## receipts (記憶機能設計書§5 -- 効くことの証拠)

`receipts/` に、決定論 fixture ハーネス (§5.1)・実 LLM smoke (§5.2)・P-M1 再測 40往復 (§5.3)
の実測結果を JSON で記帳する。いずれも `scripts/receipt_harness.py` / `scripts/manual_smoke_run.py` /
`scripts/manual_smoke_long_run.py` で再生成できる。
なお、Issue #26 の修正前に、稼働中の `~/.raiya` があるマシンで取った smoke は、対話側の
`claude -p` が raiya hooks を発火させ本番 state に汚染されていた可能性がある (refs #26)。

### §5.1 決定論 fixture ハーネス (`receipts/deterministic.json`)

```bash
python3 scripts/receipt_harness.py
```

LLM 非依存・台本化した往復列+提案列を実体の `raiya.alaya.AlayaStore` へ通し、5指標を
**外側から** (ハーネス自身の独立オラクル・before/after 観測) 測る。本リポで再測定した
結果はコード移植前 (cosmicanalysis 波2) と全指標一致 (捏造遮断率=1.0 (4/4)・圓成實誤付与=0
件・書換件数=0件・越境併合=0件・想起精度=precision 1.0/recall 1.0)。P-M3 (新旧併合規則の
比較。新規則側は`raiya.alaya`本体・旧規則側は`scripts/receipt_harness.py`内のテスト専用参照実装)
は `hit: true` (旧規則誤併合2件 / 新規則誤併合0件)。

### 改修5 想起 fixture (`receipts/recall_before.json` / `receipts/recall.json`)

`python3 scripts/receipt_harness.py` が `receipts/recall.json` も書く (`--recall-before` は改修前のコードで
1 度だけ採った `recall_before.json` 用)。fixture は本番 `~/.raiya` 全削除前の種子 72 件
(`tests/fixtures/recall_seeds_20261017.json`) に kind / project を割り当て
(`tests/fixtures/recall_fixture_20261017.json`)、7 クエリで `MemoryEngine.recall` を呼んで
kind ごとの precision / recall・性向の混入・他 project の混入を数える。決定論・LLM なし・本番 state は
読まない。合格線は置かない (spec §11.5)。done は「改修前後の receipt が出て、性向の混入 0・他 project
の混入 0」。

| | 改修前 | 改修後 |
|---|---|---|
| 性向の混入 (7 クエリ合計) | 176 | **0** |
| 他 project の混入 | 77 | **0** |
| 決定 precision / recall | 0.13 / 0.71 | 0.20 / 1.00 |
| 索引の件数 (クエリごと) | 30 ずつ | 4〜11 (その project の決定系のみ) |

### §5.2 実 LLM smoke (`receipts/smoke.json` / `receipts/judgment_smoke.json`)

(改修 3 時点 = `propose` 経路・`raiya.cli` に対する実測。hooks の `consolidate` 実測は §5.4 で、この受領証とは別ファイル)

```bash
python3 scripts/manual_smoke_run.py         # 既存の成功済み receipts/smoke.json があれば再走しない
python3 scripts/manual_smoke_run.py --force # 強制再走
```

`claude-cli` backend で会話 10 往復 (受領証採取時は自然な日常会話、2026-10-23 からコーディングの会話) を `python3 -m raiya.cli` へ実際に供給し、
生成された `raiya.db` の `seeds` / `imprint_log` 表を外側から集計する。「手動1本の規律」=
テスト (`unittest discover`) には含めない・むやみに再走しない。
runner は `raiya.cli` をリポジトリ直下ではなく一時 state dir から起動する (子の `claude -p` がリポジトリの AGENTS.md を読まないため・Issue #29)。ユーザー単位の `~/.claude/CLAUDE.md` は引き続き読まれる (既知の残り)。

現在の `receipts/smoke.json` は 2026-08-26 に本リポ (raiya 移植後のコード) に対して
新規に採取したもの (`proposal_total=12`, `class_counts={"圓成實": 10, "依他起": 2,
"遍計所執": 0, "reject": 0}`, `quarantine_count=0`, `seed_count=12`)。事前登録した2つの
予測 (`judgment_smoke.json`) の再判定:

- **P-M1** (実 LLM 提案のうち非接地=quarantine 行きが1件以上): `quarantine_count=0` のため
  `hit: false`。
- **P-M2** (接地した提案のうち依他起が圓成實より多い): `圓成實=10 > 依他起=2` のため
  `hit: false`。

いずれも「外れ」だが、これは判定コードを甘くした結果ではなく実測(自然な雑談スクリプトでは
LLM がユーザー発話をほぼ逐語で言い換える提案を出しやすく、非接地な捏造や間接的言明の方が
少数派になる、という LLM 側の傾向)。cosmicanalysis 側の波2 smoke (r1/r2、別リポの旧
`agent.py` に対する計測) でも同じ2予測が同様に外れており、単発のノイズでなく再現性のある
傾向であることの傍証になっている (それぞれのリポの receipts が独立の provenance を持つ
点に注意: 本リポの `smoke.json` は raiya 移植後のコードに対する実測であり、
cosmicanalysis の smoke 受領証を流用したものではない)。

改修5 (`edbb9e4`) で `propose` の範囲がコーディングの記録に変わったため、この受領証 (と §5.3 の
`smoke_long.json`) の `proposal_total` は現行コードと比較できない (2026-10-23・Issue #10)。

### §5.3 P-M1 再測・40往復(`receipts/smoke_long.json` / `receipts/judgment_smoke_long.json`)

(改修 3 時点 = `propose` 経路・`raiya.cli` に対する実測。hooks の `consolidate` 実測は §5.4 で、この受領証とは別ファイル)

記憶機能設計書§5.3 の外れ時処方(P-M1 が外れたら「往復数を増やして再測」)に従い、
`claude-cli` backend で **40往復** を再測した。台本 (`scripts/manual_smoke_long_run.py`) は
§5.2 と同種の自然な言明に加え、LLM が記憶を「作り込みたくなる」誘導を意図的に混ぜている:
曖昧な言及の蒸し返し(「前に話してた件」「さっきの続き」)・ユーザーが言っていないことを
前提にした質問(「さっき言った3つの候補」「前に話した子供の名前」「以前住んでいた街」等)・
長い応答を引き出す話題(年表・量子コンピュータの仕組み)・数値/固有名詞の多い話題
(新幹線の停車駅・距離・所要時間)・会話全体の要約要求(最後の1往復)。

```bash
python3 scripts/manual_smoke_long_run.py         # 既存の成功済み receipts/smoke_long.json があれば再走しない
python3 scripts/manual_smoke_long_run.py --force # 強制再走 (smoke.json とは独立したガード=別レシート名)
```

2026-08-26 に本リポのコードに対して新規に採取:

| 指標 | 値 |
|---|---|
| 提案数 (proposal_total) | 26 |
| クラス分布 | 圓成實 21 / 依他起 4 / 遍計所執 0 / reject 1 |
| 却下理由分布 | 「縁(conditions)が往復本文のいずれにも見つからず、発芽条件を持てないため却下」× 1 |
| quarantine 保持 / 累計 | 0 / 0 |

§5.2 と**同じ凍結述語**(`receipt_harness.judge_p_m1`/`judge_p_m2`)で再判定
(`judgment_smoke_long.json`。元の10往復の `judgment_smoke.json` は上書きしていない):

- **P-M1**(非接地提案 ≥1件): `quarantine_count=0` のため **miss**。
- **P-M2**(依他起 > 圓成實): `圓成實=21 > 依他起=4` のため **miss**。

quarantine が0件のため、遍計所執の実例 span はまだ引用できない(捏造経路が実在することを
示す初の receipt はまだ得られていない)。この40往復では、ユーザーが言っていないことを
前提にした質問群に対しても、応答は毎回「その話題はこの会話に出ていません」「教えて
いただけますか」という趣旨の確認を返しており、存在しない事実を作り話として答える様子は
`stdout_tail` 上でも観測されなかった。

**10(cosmicanalysis r2)+10(raiya §5.2)+40(本節)往復・計60往復で quarantine 0 =
「捏造経路が実在する」という主張は現時点で支持されない。** これはゲート自体の判定力を
否定するものではない(§5.1 の決定論 fixture では非接地 span を100%遮断することを確認
済み=ゲートは構造として機能する)。だが実運用で LLM が実際に非接地の記憶を提案してくる
頻度についての経験的主張は、この3回・計60往復の手動 smoke の範囲では根拠を持たない。

### §5.4 hooks の consolidate 実測 (`receipts/smoke_consolidate.json`)

対話は台本固定で、ライブなのは `consolidate` の LLM 呼び出しだけ。`record_turn` は stop と同じ順で `<private>` を除き、貼り付けを根拠から剥がし、8000字で切る。未統合が 5 件で `consolidate` し、残りは session_end と同じく `consolidate` する。失敗にするのはゲートの破れだけ (貼り付け・`<private>`・切り詰めの切れ目を跨ぐ span が種子になる、他 project が recall に混ざる、性向が注入される)。提案数・kind・三性・却下理由・`skipped`・P-M1/P-M2 は記録する。`smoke.json` / `smoke_long.json` は書かない。

```bash
OPENAI_API_KEY=... python3 scripts/manual_smoke_consolidate.py
# 鍵が無いときは受領証を書かずに終了する。モデルはスクリプトが openai-api / gpt-6-luna に固定する。
# 既存の receipts/smoke_consolidate.json があれば再走しない。再走は --force。
```

ドライラン (LLM なし) は `tests/test_manual_smoke_consolidate.py` が `call_llm` を差し替えて行う。`unittest discover` に含まれるのはこのドライランだけで、ライブ実行は含まれない。

2026-10-18 に `openai-api` / `gpt-6-luna` で1回採取した。ゲートの破れは 0。両窓とも `skipped` は null。決定 4 件のうち 2 件（種子 6・7）は、却下の印が span には無く content にだけある (`decision_marker_outside_span`)。これはゲート破れにしない。全統合が BackendError または unparseable の受領証は `blocked: true` で、成功として再走を止めない。

| 指標 | 値 |
|---|---|
| 提案数 | 7 |
| kind | 決定 4 / 回避策 1 / 所在 1 / 再開点 1 |
| 三性 | 圓成實 7 / 依他起 0 / 遍計所執 0 / reject 0 |
| P-M1 / P-M2 | miss / miss |

貼り付け・`<private>`・切り詰めの中間や切れ目は種子になっていない。project B の決定は project A の recall に混ざっていない。性向の種子は出ていない。

## パッケージ構成

```
raiya/
  raiya/__init__.py    MemoryEngine, Turn を再export
  raiya/alaya.py        阿頼耶識 (種子倉庫)
  raiya/sansho.py        三性ゲート (決定論の判定コア)
  raiya/manas.py         末那識 (自己物語の強制注入)
  raiya/engine.py         facade MemoryEngine (§4.2)
  raiya/config.py         設定ファイル config.yaml (探索・検証・メモ化。PyYAML は遅延 import)
  raiya/backend.py        LLM backend 抽象 (CLI/hooks モード専用)
  raiya/cli.py             対話 CLI (facade の1利用者)
  raiya/mcp_server.py       MCP サーバ (公式 mcp SDK。stdio / Streamable HTTP、§4.3)
  raiya/ledger.py           台帳 (全往復の追記専用ログ。保存は raiya.db の ledger 表・改修4)
  raiya/storage.py          sqlite3 の永続化 (raiya.db。#38)
  raiya/lock.py             多重書き手対策のファイルロック (波4)
  raiya/hooks/__init__.py
  raiya/hooks/claude_code.py  Claude Code hooks (recall/stop/session_end、波4・改修4)
  raiya/hooks/settings.example.json  hooks 登録の設定例
tests/                  全テスト (unittest discover でリポ root から実行)
receipts/                §5 receipt (deterministic.json / smoke.json / judgment_smoke.json /
                          smoke_long.json / judgment_smoke_long.json /
                          smoke_consolidate.json はライブ実行時に追加)
scripts/receipt_harness.py        §5.1 ハーネス本体 (raiya/ パッケージの外=出荷物ではなく検証器具)
scripts/manual_smoke_run.py       §5.2 手動 smoke ランナー (10往復)
scripts/manual_smoke_long_run.py  §5.3 P-M1 再測ランナー (40往復)
scripts/manual_smoke_consolidate.py  §5.4 hooks consolidate 実測 (台本固定・統合だけライブ)
pyproject.toml
LICENSE                   Apache License 2.0 の本文
```
