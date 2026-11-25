# 頼耶 (raiya) -- 捏造を構造的に通さない記憶エンジン(fixture で実証)

唯識の三能変 (阿頼耶識 → 末那識 → 六識) をそのまま処理パイプラインにした記憶ライブラリ。
LLM は記憶の「提案」しかできない。提案の身分 (圓成實=ユーザー発話に逐語接地/依他起=アシ
スタント発話にのみ接地/遍計所執=非接地=捏造) は、決定論の三性ゲート (`sansho.py`) だけが
判定する。LLM は自分の提案の身分を自称できない (EXE-15 異熟果=無記の原理)。

**この「構造的に通さない」は §5.1 の決定論 fixture (非接地 span を人工的に与えて 100%
遮断することを確認) で実証した、ゲートの構造的性質についての主張である。「LLM は実際に
捏造する」という経験的主張ではない -- 実 LLM が非接地の記憶を実際に提案してくる頻度に
ついては §5.2/§5.3 の手動 smoke(計60往復)を参照。現時点では非接地の提案は1件も
観測されていない(改修 3 時点 = `propose` 経路・`raiya.cli` に対する実測。hooks の `consolidate` 実測は §5.4)。

記憶の単位は「一往復の要約」ではなく**台帳横断で統合した記録 (性向・決定などの種子)**である (改修4)。全往復は
追記専用の台帳 (`raiya.db` の `ledger` 表) に積み、未統合が一定数溜まったところで台帳の窓をまとめて
読み、そこから抽出した記録 (改修5 から決定/回避策/所在/再開点/性向の 5 kind) を種子として熏習する。
台帳も種子も削除しない (減衰も枯死も無い = 業力保存・KUN-3 恒隨轉)。注入される索引は既定では
決定/回避策/所在で、性向は `RAIYA_INJECT_DISPOSITIONS=1` のときだけ載る (索引の上限は kind 別)。

出自: `cosmicanalysis/src/Eight_Consciousness` (波1〜2、2026-08-25) からの独立リポ切り出し
(波3・記憶機能設計書§4「ライブラリ化+MCP」・裁定枠a)。「機構は持ち出す、語彙と統治は
置いていく」= KojikiOS の語彙 (柱/神名帳/御魂/誓約 等) は持ち込まない。唯識語彙
(阿頼耶識/末那識/三性) は頼耶自身の語彙なのでそのまま使う。

本文や docstring に出てくる設計書 (`2026-…-design.md` など) と記憶機能設計書は、このリポジトリには含めていない。
名前は判断の出どころの記録として残している。

## インストール

コアの依存は PyYAML だけ (設定ファイル `config.yaml` の読み込み、`yaml.safe_load` のみ。
`dependencies = ["PyYAML>=6.0.1,<7"]`)。「標準ライブラリだけ」の縛りは 2026-10-23 に廃止した
(設計書 `2026-10-23-config-file-design.md`)。
MCP サーバ (`raiya-mcp-server`) だけは公式 MCP Python SDK (`mcp` 2.x、Python 3.10+) を
必要とし、extra `[mcp]` で入れる。

```bash
cd <clone>                          # <clone> = clone してできたディレクトリ (本リポの根。例: $HOME/raiya)
python3 -m venv .venv
.venv/bin/pip install -e .          # ライブラリ + CLI + hooks (依存は PyYAML のみ)
.venv/bin/pip install -e ".[mcp]"   # + MCP サーバ (公式 mcp SDK を導入)
```

これで `raiya` パッケージの import に加え、`raiya-cli` / `raiya-mcp-server` / `raiya-inspect` / `raiya-correct` /
`raiya-hook` の5コマンドが使えるようになる (`pyproject.toml` の `[project.scripts]`)。

`PYTHONPATH` に raiya のディレクトリを入れて使うことはしない。特に `~/.bashrc` などで
`export` すると、どの venv でも `import raiya` が通り、`pip freeze` がリポジトリ直下の
`raiya.egg-info` を拾って、無関係なプロジェクトの requirements.txt に raiya が混ざる。
hooks などからは、上で作った venv の Python (`<clone>/.venv/bin/python`) を絶対パスで呼ぶ。

## 最小の使用例

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

## 詳しい説明 (manual/)

- [使い方](https://github.com/cosmopanda432/raiya/blob/master/manual/usage.md): ライブラリ・CLI・MCP サーバとしての使い方と、Claude Code への登録例
- [Claude Code hooks](https://github.com/cosmopanda432/raiya/blob/master/manual/hooks.md): 自動想起・自動熏習の仕組み、設定手順、コストへの注意
- [state](https://github.com/cosmopanda432/raiya/blob/master/manual/state.md): `raiya.db` の中身、台帳・種子スキーマ、復旧、多重書き手対策
- [公開 API・環境変数・設定ファイル](https://github.com/cosmopanda432/raiya/blob/master/manual/api.md): 凍結した契約、環境変数、`config.yaml`
- [テスト・receipts・パッケージ構成](https://github.com/cosmopanda432/raiya/blob/master/manual/receipts.md): テストの回し方、効くことの証拠 (receipts §5.1〜§5.4)、ファイル構成

## 止めきれないもの

raiya は、誤った記憶や改ざんを 100% は防げない。

- ゲートは「ユーザーがそう言ったか」を字で確かめるだけで、言ったことが正しいかは分からない。間違った発言も 圓成實 として残る。
- 同じことを別の言葉で言い直すと、別の記録として残ることがある。
- 印の無い短い貼り付けは、ユーザーの発言と区別できない。`<private>` はタグを書いた所にしか効かない。
- hooks が失敗しても会話は止まらない。その往復は記録されず、`hooks.log` に 1 行残るだけ。
- 注入した記録をモデルが無視することも、鵜呑みにすることも止められない。
- `raiya-correct` はシェルを使える者なら誰でも実行できる。`--by` は自己申告。
- `raiya.db` を直に書ける者は、記録を書き換えも消しもできる。

これを防ぐには、ファイルの権限やサンドボックスなど、raiya の外の仕組みが要る。

## ライセンス

Apache License 2.0 で公開します。本文は [LICENSE](https://github.com/cosmopanda432/raiya/blob/master/LICENSE) にあります。

Copyright 2026 cosmopanda432
