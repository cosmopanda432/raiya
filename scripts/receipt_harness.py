"""receipt_harness.py -- 記憶機能設計書 §5 receipt ハーネス(効くことの証拠)

決定論・LLM 非依存。台本化した往復列+台本化した提案列 (LLM の代役) を実体の
`raiya.alaya.AlayaStore` / `raiya.sansho` へ通し、§5.1 の5指標を**外側から**測る
(被試験モジュールの内部値をそのままエコーしない)。

置き場の裁定 (波2で確定・波3で raiya リポ root へ移植): tests/ 内のプライベート
ヘルパーではなく repo root のモジュールとして置いた。理由: (1) `tests/test_receipt.py`
(決定論 fixture) と `manual_smoke_run.py` (§5.2 手動 smoke) の**両方から再利用する**
P-M1〜3 の judge_* 関数群を持つため、1 テストファイル専用のプライベートヘルパーでは
収まらない。(2) `raiya/` パッケージ本体の一部ではない (出荷物の外の検証器具) ため、
`raiya/` の中には置かない。標準ライブラリのみ。

計測方針 (指標を外側から数える):
- 捏造遮断率・圓成實誤付与: ハーネス自身の独立オラクル `_oracle_span_class` (生の
  部分文字列一致。sansho.classify_sansho を呼ばない=自己参照 fixture を避ける) で
  fixture 側の「本当は接地しているか」を判定し、alaya の実際の判定 (quarantine への
  格納・seeds の sansho 欄) と突き合わせる。
- 書換件数: 併合を起こす提案の**前後で content スナップショットを比較**する
  (併合呼び出しの戻り値を信じるのではなく、before/after の実測比較)。
- 越境併合: クラス跨ぎ提案の**前後で strength を観測**する (strength が動けば越境)。
- 想起精度: 台本の期待発芽 id 集合 vs `germinate()` の実発芽 id 集合の precision/recall。
- 圓成實誤付与の再検算 (`_detect_false_enjoujitsu`) は evidence の `turn` スナップショット
  だけを見る。台帳由来の evidence (`ledger_id` あり・`turn` スナップショット無し) は
  再検算できず、`user_text` が空として扱われるため**一律に違反として計上される**
  (保守側の誤検出)。決定論 fixture は plain `Turn` のみを使うので受領証の数値には
  影響しないが、実運用 state にこの検出器をそのまま当てると圓成實誤付与 > 0 と出る。
"""
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass

# 設定ファイルを読ませない (開発者の inject_dispositions: true が recall.json を変えないように)。
# raiya を import する前に強制する (設計書 2026-10-23-config-file-design.md §7)。
os.environ["RAIYA_CONFIG"] = ""

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (SCRIPTS_DIR, BASE_DIR):  # 兄弟スクリプトと raiya パッケージ (ルート) を import 可能にする
    if _p not in sys.path:
        sys.path.insert(0, _p)

from raiya import sansho  # noqa: E402
from raiya import storage  # noqa: E402
from raiya.alaya import MERGE_SIMILARITY_THRESHOLD  # noqa: E402


def _new_store(alaya_path, imprint_log_path):
    """standalone の AlayaStore (保存層は sqlite3: `alaya_path` のある dir の `raiya.db`。無ければ新規に作る)。
    ファイル名は無視する (旧 JSON 版の path 2 本の呼び出し形を保つため)。ハーネス内の全呼び出しをここに集約する。
    """
    return storage.open_alaya(os.path.dirname(alaya_path))


@dataclass(frozen=True)
class ScriptedItem:
    """台本化した1提案 (LLM の代役)。"""

    label: str          # 敵対例のラベル (人間可読)
    turn: "sansho.Turn"  # 往復本文
    proposal: dict       # LLM が返したはずの提案 (evidence 込み)


def _oracle_span_class(span, user_text, assistant_text):
    """ハーネス自身の独立オラクル (被試験の `sansho.classify_sansho` を呼ばない)。

    生の部分文字列一致のみ (正規化なし)。fixture 側の span/本文をあらかじめ
    正規化に依存しない形 (完全一致 or 明確な不一致) に構成しているため、
    これで sansho.py の判定を独立に検算できる (KUN 系の自己参照回避)。
    """
    if span and span in (user_text or ""):
        return "圓成實"
    if span and span in (assistant_text or ""):
        return "依他起"
    return "遍計所執"


# --- §5.1 決定論 fixture (敵対例7分類) ---------------------------------------

# 1. 逐語 span (user 本文に一言一句そのまま存在) -> 圓成實 になるはず
_ITEM_VERBATIM = ScriptedItem(
    "verbatim_span",
    sansho.Turn(user_text="私は猫が好きです/", assistant_text=""),
    {
        "content": "猫が好き",
        "conditions": ["猫"],
        "polarity": "善",
        "kind": "所在", "evidence": {"source": "user", "span": "私は猫が好きです/"},
    },
)

# 2. 1文字改変 span (「です」->「でした」) -> 部分文字列に一致せず遍計所執 になるはず
_ITEM_ALTERED = ScriptedItem(
    "one_char_altered_span",
    sansho.Turn(user_text="私は猫が好きです/", assistant_text=""),
    {
        "content": "猫が好き(改変span)",
        "conditions": ["猫"],
        "polarity": "無記",
        "kind": "所在", "evidence": {"source": "user", "span": "私は猫が好きでした"},
    },
)

# 3. assistant にのみある span -> 依他起 になるはず
_ITEM_ASSISTANT_ONLY = ScriptedItem(
    "assistant_only_span",
    sansho.Turn(user_text="今日はいい天気ですね", assistant_text="私は散歩が好きです 天気"),
    {
        "content": "散歩が好き",
        "conditions": ["天気"],
        "polarity": "無記",
        "kind": "所在", "evidence": {"source": "assistant", "span": "私は散歩が好きです"},
    },
)

# 4. span なし -> 遍計所執 になるはず
_ITEM_NO_SPAN = ScriptedItem(
    "no_span",
    sansho.Turn(user_text="今日は晴れです", assistant_text=""),
    {
        "content": "span無し捏造",
        "conditions": ["晴れ"],
        "polarity": "無記",
        "kind": "所在", "evidence": {"source": "user", "span": ""},
    },
)

# 5. span は接地しているが縁が全て未接地 -> reject になるはず (quarantine/seeds どちらでもない)
_ITEM_UNGROUNDED_CONDITIONS = ScriptedItem(
    "ungrounded_conditions",
    sansho.Turn(user_text="猫がとても好きです", assistant_text=""),
    {
        "content": "猫が好き(縁未接地)",
        "conditions": ["宇宙人", "UFO"],
        "polarity": "無記",
        "kind": "所在", "evidence": {"source": "user", "span": "猫がとても好きです"},
    },
)

# 6. 完全に発明された span (元の本文と一切関係が無い捏造。1文字改変とは別種の敵対例)
_ITEM_INVENTED = ScriptedItem(
    "wholly_invented_span",
    sansho.Turn(user_text="今日は仕事に行きました", assistant_text=""),
    {
        "content": "宇宙人と交信した(完全捏造)",
        "conditions": ["仕事"],
        "polarity": "無記",
        "kind": "所在", "evidence": {"source": "user", "span": "宇宙人と交信して未来を予知した"},
    },
)

# 7. 空白のみ span (空文字とは別の非接地ケース)
_ITEM_WHITESPACE_ONLY_SPAN = ScriptedItem(
    "whitespace_only_span",
    sansho.Turn(user_text="今日は晴れです", assistant_text=""),
    {
        "content": "空白spanの捏造",
        "conditions": ["晴れ"],
        "polarity": "無記",
        "kind": "所在", "evidence": {"source": "user", "span": "   "},
    },
)

# 非接地系 (捏造遮断率の分母): 1文字改変・span無し・完全発明・空白のみ span の4件。
# ungrounded_conditions は縁の話で sansho 自体は圓成實 (span 接地) なので数えない
# -- 捏造遮断率は「span が非接地」の4件のみ。分母は下記 `_fabrication_block_stats` が
# この4件を静的に信じるのではなく、オラクルの分類そのものから動的に算出する。
FABRICATED_ITEMS = (_ITEM_ALTERED, _ITEM_NO_SPAN, _ITEM_INVENTED, _ITEM_WHITESPACE_ONLY_SPAN)
GROUNDED_SINGLE_ITEMS = (_ITEM_VERBATIM, _ITEM_ASSISTANT_ONLY, _ITEM_UNGROUNDED_CONDITIONS)


def _fabrication_block_stats(fabrication_results):
    """捏造遮断率の分子/分母を計算する。

    分母 (`非接地提案数`) は「FABRICATED_ITEMS というタプルに何件入っているか」という
    静的な件数ではなく、**各 item をハーネスの独立オラクルが実際に遍計所執(非接地)と
    分類したか**から動的に導出する。fixture の混入ミス (誤って接地している item を
    FABRICATED_ITEMS に入れてしまった等) があっても分母が誤って膨らまない。
    """
    non_grounded_total = sum(1 for r in fabrication_results if r["oracle"] == "遍計所執")
    non_grounded_quarantined = sum(
        1 for r in fabrication_results if r["oracle"] == "遍計所執" and r["decision"] == "遍計所執"
    )
    return non_grounded_total, non_grounded_quarantined


def _detect_false_enjoujitsu(seeds):
    """圓成實 誤付与検出器。sansho=圓成實 の種子のうち、evidences の span が実際には
    その turn の user_text に含まれていないものを独立に検出する (alaya 自身の判定を
    信じずに再チェックする)。

    `seeds` は `alaya.AlayaStore.list_seeds()` の戻り値である必要はない -- 直接組み立てた
    dict のリストでもよい (検出器自体に陽性対照を通すための seam)。
    """
    violations = []
    for seed in seeds:
        if seed.get("sansho") != "圓成實":
            continue
        for ev in seed.get("evidences", []):
            span = ev.get("span") or ""
            user_text = (ev.get("turn") or {}).get("user_text") or ""
            if not (span and span in user_text):
                violations.append({"seed_id": seed.get("id"), "span": span, "user_text": user_text})
    return violations


# 6. 同内容の再提案 (縁完全一致) -> 併合するはず (content 不変・strength+1)
_MERGE_PAIR = (
    ScriptedItem(
        "resubmission_a",
        sansho.Turn(user_text="犬の散歩が好きです/ 犬", assistant_text=""),
        {
            "content": "犬の散歩が好き",
            "conditions": ["犬"],
            "polarity": "善",
            "kind": "所在", "evidence": {"source": "user", "span": "犬の散歩が好きです/"},
        },
    ),
    ScriptedItem(
        "resubmission_b",
        sansho.Turn(user_text="やっぱり犬の散歩が大好きです/ 犬", assistant_text=""),
        {
            "content": "やっぱり犬の散歩が大好き",
            "conditions": ["犬"],
            "polarity": "善",
            "kind": "所在", "evidence": {"source": "user", "span": "やっぱり犬の散歩が大好きです/"},
        },
    ),
)

# 7. 異クラス同内容 (content/conditions/polarity 完全一致・sansho クラスのみ違う) ->
#    併合してはならない (越境併合禁止)。content は「猫が好き」(#1) と 2-gram Jaccard が 0.5 に
#    届かない語にする: 改修5 で polarity が併合条件から外れたため「鳥が好き」(0.5) は #1 に併合された。
_CROSS_CLASS_PAIR = (
    ScriptedItem(
        "cross_class_user",
        sansho.Turn(user_text="鳥の観察が好きです/ 鳥", assistant_text=""),
        {
            "content": "鳥の観察が好き",
            "conditions": ["鳥"],
            "polarity": "無記",
            "kind": "所在", "evidence": {"source": "user", "span": "鳥の観察が好きです/"},
        },
    ),
    ScriptedItem(
        "cross_class_assistant",
        sansho.Turn(user_text="今日は曇りです", assistant_text="鳥の観察が好きです/ 鳥"),
        {
            "content": "鳥の観察が好き",
            "conditions": ["鳥"],
            "polarity": "無記",
            "kind": "所在", "evidence": {"source": "assistant", "span": "鳥の観察が好きです/"},
        },
    ),
)

# 想起精度用: 音楽(強度2まで併合)が真陽性、ジャズ(依他起・強度1 < 閾値2)が
# 「縁は一致するが閾値未満」の近傍陰性 (threshold の実効果を試す)。
_RECALL_ITEMS = (
    ScriptedItem(
        "recall_a",
        sansho.Turn(user_text="音楽を聴くのが好きです/ 音楽", assistant_text=""),
        {
            "content": "音楽を聴くのが好き",
            "conditions": ["音楽"],
            "polarity": "無記",
            "kind": "所在", "evidence": {"source": "user", "span": "音楽を聴くのが好きです/"},
        },
    ),
    ScriptedItem(
        "recall_b_merge",
        sansho.Turn(user_text="やっぱり音楽が好きです/ 音楽", assistant_text=""),
        {
            "content": "やっぱり音楽が好き",
            "conditions": ["音楽"],
            "polarity": "無記",
            "kind": "所在", "evidence": {"source": "user", "span": "やっぱり音楽が好きです/"},
        },
    ),
    ScriptedItem(
        "recall_near_miss",
        sansho.Turn(user_text="今日は雨です 音楽", assistant_text="ジャズが好きです 音楽"),
        {
            "content": "ジャズが好き",
            "conditions": ["音楽"],
            "polarity": "無記",
            # assistant 接地 = 依他起 (strength 1 < 閾値 2)。改修4 で圓成實は 1 件で発芽するため、
            # 「縁は一致するが閾値未満」の近傍陰性は依他起で表現する。
            "evidence": {"source": "assistant", "span": "ジャズが好きです"},
        },
    ),
)
_RECALL_QUERY = "犬の散歩と音楽について今日は話したい"  # 「犬」(merge pair)+「音楽」に触れる


def run_deterministic_harness(alaya_path=None, imprint_log_path=None):
    """§5.1 決定論 fixture を実 alaya へ通し、5指標を外側から測って dict で返す。

    LLM 非依存・純粋 (呼び出しごとに独立した AlayaStore を使う)。
    """
    _owned_tmpdir = None
    if alaya_path is None or imprint_log_path is None:
        _owned_tmpdir = tempfile.mkdtemp(prefix="receipt_harness_")
        alaya_path = os.path.join(_owned_tmpdir, "alaya.json")
        imprint_log_path = os.path.join(_owned_tmpdir, "imprint_log.json")
    store = _new_store(alaya_path, imprint_log_path)
    store.begin_new_life()

    # --- 捏造遮断率・圓成實誤付与 (非接地系4件+単発グラウンド系3件を通す) ---
    fabrication_results = []
    for item in FABRICATED_ITEMS:
        oracle = _oracle_span_class(item.proposal["evidence"]["span"], item.turn.user_text, item.turn.assistant_text)
        result = store.imprint(item.turn, item.proposal)
        fabrication_results.append({"label": item.label, "oracle": oracle, "decision": result["decision"]})

    for item in GROUNDED_SINGLE_ITEMS:
        store.imprint(item.turn, item.proposal)

    non_grounded_total, non_grounded_quarantined = _fabrication_block_stats(fabrication_results)

    # --- 書換件数 (併合ペア): before/after の content スナップショット比較 ---
    r_a = store.imprint(_MERGE_PAIR[0].turn, _MERGE_PAIR[0].proposal)
    content_before = r_a["seed"]["content"]
    r_b = store.imprint(_MERGE_PAIR[1].turn, _MERGE_PAIR[1].proposal)
    content_after = store.get_by_ids([r_a["seed"]["id"]])[0]["content"]
    rewritten = 1 if content_after != content_before else 0

    # --- 越境併合 (異クラス同内容ペア): before/after の strength 観測 ---
    r_c = store.imprint(_CROSS_CLASS_PAIR[0].turn, _CROSS_CLASS_PAIR[0].proposal)
    strength_before = store.get_by_ids([r_c["seed"]["id"]])[0]["strength"]
    store.imprint(_CROSS_CLASS_PAIR[1].turn, _CROSS_CLASS_PAIR[1].proposal)
    strength_after = store.get_by_ids([r_c["seed"]["id"]])[0]["strength"]
    cross_class_merges = 1 if strength_after != strength_before else 0

    # --- 圓成實誤付与: 検出器 (_detect_false_enjoujitsu に外出しし単体で pin 可能に) ---
    false_enjoujitsu = _detect_false_enjoujitsu(store.list_seeds())

    # --- 想起精度 ---
    r_recall_a = store.imprint(_RECALL_ITEMS[0].turn, _RECALL_ITEMS[0].proposal)
    r_recall_b = store.imprint(_RECALL_ITEMS[1].turn, _RECALL_ITEMS[1].proposal)
    store.imprint(_RECALL_ITEMS[2].turn, _RECALL_ITEMS[2].proposal)

    expected_ids = {r_a["seed"]["id"], r_recall_b["seed"]["id"]}
    fired = store.germinate(_RECALL_QUERY)
    store.commit()  # standalone のストア: 動詞途中の暗黙の書込みは無いので、明示的に DB へ書く
    fired_ids = {s["id"] for s in fired}
    tp = len(expected_ids & fired_ids)
    precision = (tp / len(fired_ids)) if fired_ids else None
    recall = (tp / len(expected_ids)) if expected_ids else None

    if _owned_tmpdir is not None:
        # 呼び出し側が path を渡さなかった場合のみ、自分で作った tmpdir を掃除する
        # (path を渡された場合は呼び出し側の tmp_path 管理に任せる=二重掃除を避ける)。
        shutil.rmtree(_owned_tmpdir, ignore_errors=True)

    return {
        "捏造遮断率": {
            "非接地提案数": non_grounded_total,
            "quarantine行き数": non_grounded_quarantined,
            "率": (non_grounded_quarantined / non_grounded_total) if non_grounded_total else None,
            "詳細": fabrication_results,
        },
        "圓成實誤付与": {
            "件数": len(false_enjoujitsu),
            "詳細": false_enjoujitsu,
        },
        "書換件数": {
            "件数": rewritten,
            "content_before": content_before,
            "content_after": content_after,
        },
        "越境併合": {
            "件数": cross_class_merges,
            "strength_before": strength_before,
            "strength_after": strength_after,
        },
        "想起精度": {
            "期待id": sorted(expected_ids),
            "実発芽id": sorted(fired_ids),
            "precision": precision,
            "recall": recall,
        },
    }


# --- P-M3 (旧併合規則との比較) -------------------------------------------------
# 「異内容ペア」: polarity 一致 ∧ 縁1語重複 (旧規則が誤併合する条件) だが、content は
# 明確に無関係 (誤併合すべきでない)。§3.5 の新規則 (sansho一致∧polarity一致∧類似度閾値)
# では類似度が閾値未満になるよう構成してある (下記の docstring 参照)。
P_M3_PAIRS = (
    (
        ScriptedItem(
            "pm3_pair1_a",
            sansho.Turn(user_text="今日は仕事が忙しかった/ 仕事 会議 資料", assistant_text=""),
            {
                "content": "今日は仕事が忙しかった",
                "conditions": ["仕事", "会議", "資料"],
                "polarity": "不善",
                "kind": "所在", "evidence": {"source": "user", "span": "今日は仕事が忙しかった/"},
            },
        ),
        ScriptedItem(
            "pm3_pair1_b",
            sansho.Turn(user_text="猫の爪を切るのを手伝った/ 猫 爪切り 仕事", assistant_text=""),
            {
                "content": "猫の爪を切るのを手伝った",
                "conditions": ["猫", "爪切り", "仕事"],
                "polarity": "不善",
                "kind": "所在", "evidence": {"source": "user", "span": "猫の爪を切るのを手伝った/"},
            },
        ),
    ),
    (
        ScriptedItem(
            "pm3_pair2_a",
            sansho.Turn(user_text="来週の旅行の計画を立てた/ 旅行 計画 来週", assistant_text=""),
            {
                "content": "来週の旅行の計画を立てた",
                "conditions": ["旅行", "計画", "来週"],
                "polarity": "善",
                "kind": "所在", "evidence": {"source": "user", "span": "来週の旅行の計画を立てた/"},
            },
        ),
        ScriptedItem(
            "pm3_pair2_b",
            sansho.Turn(user_text="友達と映画を見に行った/ 映画 友達 来週", assistant_text=""),
            {
                "content": "友達と映画を見に行った",
                "conditions": ["映画", "友達", "来週"],
                "polarity": "善",
                "kind": "所在", "evidence": {"source": "user", "span": "友達と映画を見に行った/"},
            },
        ),
    ),
)


def _legacy_merge_would_fire(existing_conditions, existing_polarity, new_conditions, new_polarity):
    """旧併合規則 (記憶機能設計書§2「polarity 一致 ∧ 縁 1 語重複」) のテスト専用
    参照実装。**本番コードではない** -- P-M3 の比較対象としてのみ使う。

    base `alaya.py` の旧実装 (波1改修前) は新規提案側の conditions だけ
    `conditions = [c for c in (conditions or []) if c]` で falsy を落としてから
    `cond_set` を作り、既存種子側 (`seed.get("conditions", [])`、既に熏習済み=
    生成時点で既に空語を含まない) は再フィルタしていなかった。この非対称を含めて
    同型にする (new_conditions 側だけ `if c` を足す)。
    """
    if existing_polarity != new_polarity:
        return False
    existing_set = set(c.lower() for c in existing_conditions)
    new_set = set(c.lower() for c in new_conditions if c)
    return bool(existing_set & new_set)


def run_p_m3_harness():
    """P-M3: 旧併合規則(参照実装)は異内容ペアを誤併合するか / 新規則(実体)はしないか。

    戻り値: {"旧規則誤併合件数": int, "新規則誤併合件数": int, "詳細": [...]}。
    新規則側は実際に `alaya.imprint()` を2回呼んで越境/誤併合が起きないかを実測する
    (旧規則側は参照実装のシミュレーションのみ = 本番コードを書き換えて試すわけではない)。
    """
    legacy_merge_count = 0
    new_merge_count = 0
    details = []
    for pair_a, pair_b in P_M3_PAIRS:
        legacy_fires = _legacy_merge_would_fire(
            pair_a.proposal["conditions"], pair_a.proposal["polarity"],
            pair_b.proposal["conditions"], pair_b.proposal["polarity"],
        )
        if legacy_fires:
            legacy_merge_count += 1

        tmpdir = tempfile.mkdtemp(prefix="receipt_harness_pm3_")
        try:
            store = _new_store(os.path.join(tmpdir, "alaya.json"), os.path.join(tmpdir, "imprint_log.json"))
            store.begin_new_life()
            r_a = store.imprint(pair_a.turn, pair_a.proposal)
            r_b = store.imprint(pair_b.turn, pair_b.proposal)
            new_fires = r_b["is_new"] is False
            if new_fires:
                new_merge_count += 1

            details.append(
                {
                    "pair": (pair_a.label, pair_b.label),
                    "legacy_would_merge": legacy_fires,
                    "new_rule_merged": new_fires,
                    "seed_a_id_unaffected_by_seed_b": r_a["seed"]["id"] != r_b["seed"]["id"] or not new_fires,
                }
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return {
        "旧規則誤併合件数": legacy_merge_count,
        "新規則誤併合件数": new_merge_count,
        "詳細": details,
    }


# --- 形の検査 (marker-table spec §3.5) ----------------------------------------------
# 言語別の語表 (raiya/markers.py) の下で `_resolve_kind` がどう読み替えるかを 1 件ずつ実測する。
# span は本文の逐語の写し (オラクルは生の部分文字列一致)。期待値は item と別に持つ。
def _kf_item(label, user_text, assistant_text, content, conditions, kind, source, span):
    """span が list なら [(source, span), ...] の複数 span の evidence (回避策 の対の形)。"""
    if isinstance(span, list):
        evidence = [{"source": src, "span": sp} for src, sp in span]
    else:
        evidence = {"source": source, "span": span}
    return ScriptedItem(
        label,
        sansho.Turn(user_text=user_text, assistant_text=assistant_text),
        {
            "content": content,
            "conditions": list(conditions),
            "polarity": "無記",
            "kind": kind,
            "evidence": evidence,
        },
    )


def _oracle_evidence_class(evidence, user_text, assistant_text):
    """evidence 全体 (dict か list) の独立オラクル。全 span が user に接地すれば 圓成實、
    1 つでも非接地なら 遍計所執、そうでなく assistant に落ちる span があれば 依他起。"""
    items = [evidence] if isinstance(evidence, dict) else list(evidence)
    classes = [_oracle_span_class(i["span"], user_text, assistant_text) for i in items]
    if not classes or "遍計所執" in classes:
        return "遍計所執"
    return "圓成實" if all(c == "圓成實" for c in classes) else "依他起"


KIND_FORM_ITEMS = (
    (_kf_item("en_decision_marker_in_user_span", "Use TOML instead of YAML for the config file.",
              "OK, the config file will be TOML.", "Config file: TOML instead of YAML", ["TOML"], "決定",
              "user", "Use TOML instead of YAML"), "決定", "圓成實"),
    (_kf_item("en_decision_marker_only_in_content", "Use TOML for the config file.",
              "Understood, the config file will be TOML.", "Config file: TOML instead of YAML", ["TOML"], "決定",
              "user", "Use TOML for the config file"), "決定", "圓成實"),   # 期待 性向→決定: #114 U (縁 TOML を含み好みの印の無い user span = 断定の形)
    (_kf_item("en_decision_capitalized_marker", "Instead of SQLite, keep alaya.json for now.", "",
              "Storage: keep alaya.json instead of SQLite", ["alaya.json"], "決定",
              "user", "Instead of SQLite, keep alaya.json"), "決定", "圓成實"),
    (_kf_item("en_decision_negation_is_not_rejection", "Do not use YAML for the config file.", "",
              "YAML rejected for the config file", ["YAML"], "決定",
              "user", "Do not use YAML"), "決定", "圓成實"),   # 期待 性向→決定: #114 U (素の not は印でないが、縁 YAML を含む user span = 断定の形)
    (_kf_item("en_decision_marker_in_assistant_span", "Which config format?", "Use TOML rather than YAML here.",
              "Config file: TOML rather than YAML", ["TOML"], "決定",
              "assistant", "Use TOML rather than YAML"), "性向", "依他起"),
    (_kf_item("mixed_ja_span_en_content", "設定ファイルは TOML にする、YAML ではなく。", "",
              "Config file: TOML instead of YAML", ["TOML"], "決定",
              "user", "YAML ではなく"), "決定", "圓成實"),
    (_kf_item("en_workaround_cause_without_correction", "pip install fails because the proxy blocks PyPI.", "",
              "pip install fails because the proxy blocks PyPI → set HTTPS_PROXY", ["pip"], "回避策",
              "user", "pip install fails because the proxy blocks PyPI"), "性向", "圓成實"),
    (_kf_item("en_workaround_pair_with_correction",
              "pip install fails because the proxy blocks PyPI. That's wrong, set HTTPS_PROXY first.", "",
              "pip install fails because the proxy blocks PyPI → set HTTPS_PROXY", ["pip"], "回避策",
              "user", [("user", "pip install fails because the proxy blocks PyPI"),
                       ("user", "That's wrong, set HTTPS_PROXY first")]), "回避策", "圓成實"),
    (_kf_item("ja_workaround_pair_with_correction",
              "デプロイを手で流した。それは違う、元に戻してください。", "",
              "デプロイを手で流す → 順序が原因で本番が壊れた → 順序を守る", ["デプロイ"], "回避策",
              "user", [("user", "デプロイを手で流した"), ("user", "元に戻してください")]), "回避策", "圓成實"),
    (_kf_item("ja_workaround_correction_in_assistant_span",
              "デプロイして", "デプロイを手で流しました。元に戻してください、とのことでした。",
              "デプロイを手で流す → 順序が原因で本番が壊れた → 順序を守る", ["デプロイ"], "回避策",
              "assistant", [("assistant", "デプロイを手で流しました"), ("assistant", "元に戻してください")]),
     "性向", "依他起"),
    (_kf_item("ja_workaround_self_stated_declared_disposition", "テストで時刻まわりが時々落ちます。原因はタイムゾーンが UTC 固定でないことで、TZ=UTC を付けて実行すると回避できます。", "",
              "時刻テストの失敗 → UTC 固定が原因 → TZ=UTC", ["時刻", "TZ=UTC"], "性向",
              "user", "テストで時刻まわりが時々落ちます。原因はタイムゾーンが UTC 固定でないことで、TZ=UTC を付けて実行すると回避できます。"), "回避策", "圓成實"),   # 期待 性向→回避策: #134 W1 (全 span 圓成實 + 同じ span に原因と回避の印)
    (_kf_item("ja_workaround_self_stated_declared_workaround", "ローカルで docker compose up が失敗するのは、ポート 5432 を手元の Postgres が先に使っているのが原因です。停止してから起動すると回避できます。", "",
              "docker compose up の失敗 → 5432 の競合が原因 → 停止してから起動", ["docker", "5432"], "回避策",
              "user", "ローカルで docker compose up が失敗するのは、ポート 5432 を手元の Postgres が先に使っているのが原因です。停止してから起動すると回避できます。"), "回避策", "圓成實"),
    (_kf_item("ja_workaround_self_stated_preference_stays", "原因は UTC 固定なので、TZ=UTC で回避できるようにしたい。", "",
              "TZ=UTC で回す方針", ["TZ=UTC"], "性向",
              "user", "原因は UTC 固定なので、TZ=UTC で回避できるようにしたい。"), "性向", "圓成實"),   # 対照: 好みの形は 性向 のまま
    (_kf_item("en_workaround_without_cause", "Set HTTPS_PROXY before pip install.", "",
              "Set HTTPS_PROXY before pip install", ["pip"], "回避策",
              "user", "Set HTTPS_PROXY before pip install"), "性向", "圓成實"),
    (_kf_item("ja_decision_marker_in_user_span", "B ではなく A を採用する", "",
              "B ではなく A を採用", ["採用"], "決定",
              "user", "B ではなく A を採用する"), "決定", "圓成實"),
    (_kf_item("ja_decision_marker_only_in_content", "A を採用する", "",
              "B ではなく A を採用", ["採用"], "決定",
              "user", "A を採用する"), "決定", "圓成實"),   # 期待 性向→決定: #114 U (縁 採用 を含む user span = 断定の形)
)


def run_kind_form_harness():
    """形の検査: KIND_FORM_ITEMS を item ごとに専用の AlayaStore へ 1 回 imprint し、
    期待の kind / sansho と一致した件数を数える。LLM 非依存。"""
    details = []
    matched = 0
    for item, expected_kind, expected_sansho in KIND_FORM_ITEMS:
        tmpdir = tempfile.mkdtemp(prefix="receipt_harness_kf_")
        try:
            store = _new_store(os.path.join(tmpdir, "alaya.json"), os.path.join(tmpdir, "imprint_log.json"))
            store.begin_new_life()
            oracle = _oracle_evidence_class(item.proposal["evidence"], item.turn.user_text, item.turn.assistant_text)
            r = store.imprint(item.turn, item.proposal)
            seed = r.get("seed")
            kind = seed.get("kind") if seed else None
            sansho_value = seed.get("sansho") if seed else None
            if kind == expected_kind and sansho_value == expected_sansho:
                matched += 1
            details.append(
                {
                    "label": item.label,
                    "oracle": oracle,
                    "decision": r["decision"],
                    "expected_kind": expected_kind,
                    "kind": kind,
                    "expected_sansho": expected_sansho,
                    "sansho": sansho_value,
                }
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return {"件数": len(KIND_FORM_ITEMS), "一致": matched, "詳細": details}


# --- 休眠の抽出 (dormancy spec §4.1・§6) ------------------------------------------------
# `alaya.extract_local_paths` の入力 → 出力。stat なし・`~` 未展開の純粋関数だけを固定する
# (stat の結果は環境に依存するので recall_latency.json 側。ここに入れない)。期待値は入力と別に持つ。
DORMANCY_EXTRACT_ITEMS = (
    ("abs_path_japanese_sentence", "資料は /tmp/raiya-x/doc.md にある。", ("/tmp/raiya-x/doc.md",)),
    ("home_tilde_not_expanded", "設定は ~/.config/raiya/config.yaml にある", ("~/.config/raiya/config.yaml",)),
    ("trailing_period_stripped", "置き場所は /srv/data/file.", ("/srv/data/file",)),
    ("colon_line_number_dropped", "見る場所は /tmp/a/b.py:12", ("/tmp/a/b.py",)),
    ("hash_anchor_dropped", "見る場所は /tmp/a/b.md#L3", ("/tmp/a/b.md",)),
    ("non_ascii_cuts_path", "/tmp/xにある", ("/tmp/x",)),
    ("trailing_dots_run_dropped", "/d/e/...", ("/d/e",)),
    ("u2026_read_as_dots", "/d/e/\u2026", ("/d/e",)),
    ("dotdot_component_excluded", "/a/b/..", ()),
    ("dot_component_in_middle_excluded", "/a/./b", ()),
    ("u2026_in_name_excluded", "/a\u2026/b", ()),
    ("u2026_mid_component_excluded", "/a/x\u2026y/b", ()),
    ("three_dots_mid_component_excluded", "/a/x...y/b", ()),
    ("period_before_slash_kept", "/a/b./", ("/a/b./",)),
    ("name_ellipsis_in_middle_excluded", "/a.../b", ()),
    ("trailing_period_after_name", "/a/b/c.", ("/a/b/c",)),
    ("name_ellipsis_excluded", "raiya.../x/y", ()),
    ("middle_ellipsis_excluded", "/a/.../b", ()),
    ("url_excluded", "https://example.com/a/b と file:///a/b", ()),
    ("host_colon_path_excluded", "host:/a/b と user@host:/a/b", ()),
    ("relative_path_excluded", "docs/x/y と ./a/b", ()),
    ("env_var_excluded", "$HOME/x/y", ()),
    ("elided_marker_adjacent_excluded", "<elided 3 chars>/x/y", ()),
    ("glob_excluded", "/a/b/*.py", ()),
    ("slash_command_excluded", "/mcp と /compact", ()),
    ("top_dir_alone_excluded", "/tmp", ()),
    ("option_equals_starts_path", "--out=/x/y", ("/x/y",)),
    ("tilde_two_components", "~/x/y", ("~/x/y",)),
    ("multiple_paths_dedup_in_order", "(/a/b) と `~/c/d` と /a/b", ("/a/b", "~/c/d")),
    ("no_path_location", "資料は共有の場所にある。", ()),
)


def run_dormancy_extract_harness():
    """休眠の抽出: DORMANCY_EXTRACT_ITEMS を `extract_local_paths` に通し、期待と一致した件数を数える。"""
    from raiya.alaya import extract_local_paths

    details = []
    matched = 0
    for label, text, expected in DORMANCY_EXTRACT_ITEMS:
        got = extract_local_paths(text)
        if got == expected:
            matched += 1
        details.append({"label": label, "input": text, "expected": list(expected), "paths": list(got)})
    return {"件数": len(DORMANCY_EXTRACT_ITEMS), "一致": matched, "詳細": details}


# --- 取り下げ (correction-withdrawal spec・plan §5/§8 pin 15) ---------------------------
# 手組みの state (固定 `at`、now() 不使用) に store メソッドを直接呼び、各読み手の挙動を固定する。
WITHDRAWAL_AT = "2026-10-25T00:00:00+00:00"
WITHDRAWAL_PROJECT = "/home/dev/p"


def _wd_record():
    return {"by": "太郎", "at": WITHDRAWAL_AT, "reason": "誤り", "ledger_ids": [], "spans": [],
            "life": 1, "replaced_by": None}


def _wd_seed(seed_id, conditions, kind="所在", sansho_class="圓成實", strength=1, project=WITHDRAWAL_PROJECT, content=None):
    return {
        "id": seed_id, "content": content or "種子{}".format(seed_id), "strength": strength,
        "conditions": list(conditions), "polarity": "無記", "sansho": sansho_class, "kind": kind,
        "project": project, "superseded_by": None, "evidences": [], "history": [],
        "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0,
    }


def _wd_propose(content, conditions, kind="所在", source="user", **extra):
    # L (question-as-evidence §7): user の span は縁を含める (縁も印も無い 圓成實 所在 は植わらない)。
    # #149: 所在 の形は 區切りの語 (`/` を含む語) なので、所在 の user の span には " p/" を足す。
    mark = " p/" if kind == "所在" and source == "user" else ""
    span = "{} {}{}".format(content, "".join(conditions), mark) if source == "user" else content
    turn = sansho.Turn(
        user_text="{} {}{}".format(content, "".join(conditions), mark) if source == "user" else "",
        assistant_text="" if source == "user" else "{} {}".format(content, "".join(conditions)),
    )
    proposal = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
                "evidence": {"source": source, "span": span}}
    proposal.update(extra)
    return turn, proposal


def _wd_store(tmpdir, seeds):
    from raiya.snapshot import write_state

    write_state(tmpdir, {"seeds": seeds, "alaya_meta": {"life": 3}})
    return _new_store(os.path.join(tmpdir, "alaya.json"), os.path.join(tmpdir, "imprint_log.json"))


def _wd_ids(seeds):
    return [x["id"] for x in seeds]


def run_withdrawal_harness():
    """取り下げ: 読み手の除外・B4 の影・復帰を、固定 `at` の手組み state で測る。LLM 非依存。"""
    P = WITHDRAWAL_PROJECT
    items = []

    def _case(label, expected, build):
        tmpdir = tempfile.mkdtemp(prefix="receipt_harness_wd_")
        try:
            got = build(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        items.append({"label": label, "expected": expected, "got": got})

    def _germ_index(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫"]), _wd_seed(2, ["猫"])])
        store.withdraw(1, _wd_record())
        return {"germinate": _wd_ids(store.germinate("猫", project=P)),
                "index_inject": _wd_ids(store.index(P, "inject"))}

    def _consolidate_index(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫"]), _wd_seed(2, ["猫"])])
        store.withdraw(1, _wd_record())
        return {"index_consolidate": _wd_ids(store.index(P, "consolidate")),
                "seed_count": store.seed_count(P, "consolidate")}

    def _shadow(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _wd_record())
        turn, proposal = _wd_propose("猫が好き", ["猫", "犬"])
        r = store.imprint(turn, proposal)
        row = store.list_imprint_log()[-1]
        return {"decision": r["decision"], "seed_id": row["seed_id"], "seed": r["seed"],
                "seeds": len(store.list_seeds()), "strength": store.get_by_ids([1])[0]["strength"]}

    def _cross_class(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _wd_record())
        turn, proposal = _wd_propose("猫が好き", ["猫", "犬"], source="assistant")
        r = store.imprint(turn, proposal)
        return {"decision": r["decision"], "is_new": r["is_new"], "seeds": len(store.list_seeds())}

    def _resume(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["再"], kind="再開点", content="前の再開点", project=None)])
        store.withdraw(1, _wd_record())
        turn, proposal = _wd_propose("次の再開点", ["次"], kind="再開点")
        r = store.imprint(turn, proposal)
        return {"decision": r["decision"], "is_new": r["is_new"], "superseded": bool(r.get("superseded")),
                "resume_point": store.resume_point(P)["id"]}

    def _restore(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫"])])
        store.withdraw(1, _wd_record())
        before = _wd_ids(store.germinate("猫", project=P))
        store.restore(1, {"by": "花子", "at": WITHDRAWAL_AT, "reason": "戻す", "life": 2})
        return {"withdrawn": before, "restored": _wd_ids(store.germinate("猫", project=P))}

    def _twin(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫", "犬"], sansho_class="圓成實"),
                                   _wd_seed(2, ["猫", "犬"], sansho_class="依他起", strength=2)])
        before = _wd_ids(store.index(P, "inject"))
        store.withdraw(1, _wd_record())
        return {"before": before, "after": _wd_ids(store.index(P, "inject"))}

    def _horizontal(tmpdir):
        store = _wd_store(tmpdir, [_wd_seed(1, ["猫", "犬"], content="旧い話", project=None),
                                   _wd_seed(2, ["猫", "犬"], content="別の旧い話", project=None)])
        store.withdraw(2, _wd_record())
        turn, proposal = _wd_propose("新しい話", ["猫", "犬"], action="supersede", target_id=1)
        r = store.imprint(turn, proposal)
        return {"also_superseded": r.get("also_superseded"), "w_superseded_by": store.get_by_ids([2])[0]["superseded_by"]}

    _case("germination_and_inject_index_exclude_withdrawn", {"germinate": [2], "index_inject": [2]}, _germ_index)
    _case("consolidate_index_excludes_withdrawn", {"index_consolidate": [2], "seed_count": 1}, _consolidate_index)
    _case("reproposal_into_withdrawn_is_shadowed",
          {"decision": "取り下げ照合", "seed_id": 1, "seed": None, "seeds": 1, "strength": 1}, _shadow)
    _case("cross_class_restatement_is_not_shadowed", {"decision": "依他起", "is_new": True, "seeds": 2}, _cross_class)
    _case("resume_point_is_planted_fresh",
          {"decision": "圓成實", "is_new": True, "superseded": False, "resume_point": 2}, _resume)
    _case("restore_regerminates", {"withdrawn": [], "restored": [1]}, _restore)
    _case("twin_inferred_resurfaces", {"before": [1], "after": [2]}, _twin)
    _case("horizontal_supersede_skips_withdrawn", {"also_superseded": [], "w_superseded_by": None}, _horizontal)
    matched = sum(1 for it in items if it["expected"] == it["got"])
    return {"件数": len(items), "一致": matched, "詳細": items}


def run_twin_match_harness():
    """双子照合 (#78): 依他起の再提案が生きた圓成實 E に隠された双子 H へ落ちるとき、積まず記録だけ残す。
    固定の手組み state (now() 不使用)。LLM 非依存。"""
    items = []

    def _case(label, expected, build):
        tmpdir = tempfile.mkdtemp(prefix="receipt_harness_twin_")
        try:
            got = build(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        items.append({"label": label, "expected": expected, "got": got})

    def _pair(with_e=True):
        seeds = []
        if with_e:
            seeds.append(_wd_seed(1, ["猫", "犬"], content="猫が好き", project=None))
        seeds.append(_wd_seed(2, ["猫", "犬"], content="猫が好き", project=None, sansho_class="依他起", strength=2))
        return seeds

    def _run(tmpdir, seeds, proposal_extra=None, withdraw_e=False):
        store = _wd_store(tmpdir, seeds)
        if withdraw_e:
            store.withdraw(1, _wd_record())
        turn, proposal = _wd_propose("猫が好き", ["猫", "犬"], source="assistant", **(proposal_extra or {}))
        r = store.imprint(turn, proposal)
        rows = store.list_imprint_log()
        h = store.get_by_ids([2])[0]
        return {"decision": r["decision"], "is_new": r["is_new"], "seed": r["seed"],
                "twin_match": r.get("twin_match"), "seed_id": rows[-1]["seed_id"], "log_rows": len(rows),
                "seeds": len(store.list_seeds()), "h_strength": h["strength"], "h_evidences": len(h["evidences"]),
                "superseded": bool(r.get("superseded"))}

    shadowed = {"decision": "双子照合", "is_new": None, "seed": None, "twin_match": 2, "seed_id": 2,
                "log_rows": 1, "seeds": 2, "h_strength": 2, "h_evidences": 0, "superseded": False}
    reinforced = {"decision": "依他起", "is_new": False, "seed": "present", "twin_match": None, "seed_id": 2,
                  "log_rows": 1, "seeds": 1, "h_strength": 3, "h_evidences": 1, "superseded": False}

    def _norm(got):
        if got["seed"] is not None:
            got = dict(got, seed="present")
        return got

    _case("merge_into_shadowed_twin_is_matched", shadowed, lambda d: _norm(_run(d, _pair())))
    _case("no_e_means_ordinary_reinforce", reinforced, lambda d: _norm(_run(d, _pair(with_e=False))))
    _case("explicit_reinforce_target_twin_is_matched", shadowed,
          lambda d: _norm(_run(d, _pair(), {"action": "reinforce", "target_id": 2})))
    _case("supersede_target_twin_goes_through",
          dict(reinforced, decision="依他起", seed="present", seeds=2, h_strength=2, superseded=True),
          lambda d: _norm(_run(d, _pair(), {"action": "supersede", "target_id": 2})))
    _case("withdrawn_e_no_longer_shadows", dict(reinforced, seeds=2),
          lambda d: _norm(_run(d, _pair(), withdraw_e=True)))
    matched = sum(1 for it in items if it["expected"] == it["got"])
    return {"件数": len(items), "一致": matched, "詳細": items}


LAYERED_ABSENT_ROOT = "/nonexistent-raiya-xyz"   # 在り得ない root (休眠の「無い」側を決定論にする)
LAYERED_PRESENT_PATH = "/usr/bin"                # どの Linux にも在る (休眠しない側)


def _ly_groups(seeds):
    """seed_groups の結果を (member_ids の列) にして返す。並びは比べやすいよう昇順の組に整える。"""
    from raiya.alaya import seed_groups

    return sorted(list(g["member_ids"]) for g in seed_groups(seeds))


def _assert_audit_sums(engine):
    """#94 (K2): audit() の内訳の和が総数と合うことを検査する (assert だけ。receipt の値は増やさない)。"""
    audit = engine.audit()
    n = audit["seed_count"]
    for key in ("class_counts", "kind_counts", "project_counts"):
        if sum(audit[key].values()) != n:
            raise AssertionError("audit {} sum != seed_count: {}".format(key, audit[key]))
    led = audit["ledger"]
    rows = engine.ledger.tail(led["turns"] + 10)
    consolidated = sum(1 for r in rows if r["id"] <= led["consolidated_upto"])
    if not led["turns"] == len(rows) == consolidated + led["pending"]:
        raise AssertionError("audit ledger sums do not match: {}".format(led))


def _ly_engine(tmpdir, seeds):
    """固定の alaya.json (空の ledger) の上に MemoryEngine を作る。new_life=False・now() は使わない。"""
    from raiya.engine import MemoryEngine
    from raiya.snapshot import write_state

    write_state(tmpdir, {"seeds": seeds, "alaya_meta": {"life": 3}})
    engine = MemoryEngine(state_dir=tmpdir, new_life=False)
    _assert_audit_sums(engine)
    return engine


def _ly_recall(seeds, prompt, project=WITHDRAWAL_PROJECT):
    """seeds を置いた使い捨ての state で recall を 1 回流し、recall の戻り値 (dict) を返す。
    性向の注入 env は外す (受領証は既定=注入しないで採る)。"""
    from unittest import mock

    from raiya.engine import INJECT_DISPOSITIONS_ENV

    tmpdir = tempfile.mkdtemp(prefix="receipt_harness_ly_")
    try:
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            engine = _ly_engine(tmpdir, seeds)
            return engine.recall(prompt, project=project)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_layered_harness():
    """層化: 群 (seed_groups)・同心円・容量・統合の入力を、手組みの固定 state で測る (plan §7 の 15 項目)。LLM 非依存。"""
    from unittest import mock

    from raiya import ledger as ledger_mod
    from raiya import manas
    from raiya.engine import _build_consolidate_prompt

    P = WITHDRAWAL_PROJECT
    Q = "/home/dev/q"
    items = []

    def _item(label, expected, got):
        items.append({"label": label, "expected": expected, "got": got})

    s = _wd_seed

    # 1. 連鎖: A-B 2 語 / B-C 2 語 / A-C 1 語 → 3 件で 1 群
    _item("chain_links_into_one_group", [[1, 2, 3]],
          _ly_groups([s(1, ["甲", "乙", "丙"], kind="決定"), s(2, ["乙", "丙", "丁"], kind="決定"),
                      s(3, ["丙", "丁", "戊"], kind="決定")]))
    # 2. 1 語だけの共有は群にならない
    _item("one_shared_condition_is_not_a_group", [],
          _ly_groups([s(1, ["甲", "乙"], kind="決定"), s(2, ["乙", "丙"], kind="決定")]))
    # 3. 大文字小文字・部分一致は共有に数えない (1-2 は 共通 だけ、1-4 は builder だけ、3-4 は 別 だけ)
    _item("case_and_substring_are_not_shared", [],
          _ly_groups([s(1, ["builder", "共通"], kind="決定"), s(2, ["Builder", "共通"], kind="決定"),
                      s(3, ["build", "別"], kind="決定"), s(4, ["builder", "別"], kind="決定")]))
    # 4. project 違い・kind 違い・全域と P は混ざらない (同じ縁 {甲,乙} の 7 件 → [1,5] と [3,6] だけ)
    _item("buckets_do_not_mix", [[1, 5], [3, 6]],
          _ly_groups([s(1, ["甲", "乙"], kind="決定", project=P), s(2, ["甲", "乙"], kind="決定", project=Q),
                      s(3, ["甲", "乙"], kind="決定", project=None), s(4, ["甲", "乙"], kind="所在", project=P),
                      s(5, ["甲", "乙"], kind="決定", project=P), s(6, ["甲", "乙"], kind="決定", project=None),
                      s(7, ["甲", "乙"], kind="回避策", project=Q)]))
    # 5. superseded (#2)・取り下げ中 (#3) は成員にならない → 生きた #1 #4 だけの群
    withdrawn = dict(s(3, ["甲", "乙"], kind="決定"), withdrawn=_wd_record())
    _item("superseded_and_withdrawn_are_not_members", [[1, 4]],
          _ly_groups([s(1, ["甲", "乙"], kind="決定"), dict(s(2, ["甲", "乙"], kind="決定"), superseded_by=9),
                      withdrawn, s(4, ["甲", "乙"], kind="決定")]))
    # 6. 1 件の群は返らない (#1 は #2 と 1 語だけ共有)
    _item("a_group_of_one_is_not_returned", [],
          _ly_groups([s(1, ["甲", "乙"], kind="決定"), s(2, ["甲", "丙"], kind="決定")]))

    # 7. ラベル: 不共を選ぶ / 不共が無ければ最多 → 同数なら sansho._n の順
    def _labels(seeds):
        from raiya.alaya import seed_groups

        return {tuple(g["member_ids"]): g["label"] for g in seed_groups(seeds)}

    unique = _labels([s(1, ["あ", "乙", "独自1"], kind="決定"), s(2, ["あ", "乙", "独自1"], kind="決定"),
                      s(3, ["あ", "丙", "独自2"], kind="決定"), s(4, ["あ", "丙", "独自2"], kind="決定")])
    # 群 {1,2}: 候補 {あ,乙,独自1}、あ は群 {3,4} も持つので不共でない。不共は {乙,独自1} (各 2 件) → _n の最小 = 乙 (群 {3,4} は 丙)。
    # あ は全候補の中で _n が最小なので、不共の絞りを外すと ラベルが あ に変わる (空振りでない)。
    tie = _labels([s(1, ["a", "c", "d"], kind="決定"), s(2, ["b", "c", "d"], kind="決定"),
                   s(3, ["a", "b", "x"], kind="決定"), s(4, ["a", "b", "y"], kind="決定")])
    most = _labels([s(1, ["z", "m"], kind="決定"), s(2, ["z", "m"], kind="決定"),
                    s(3, ["z", "m", "n"], kind="決定"), s(4, ["z", "n", "y"], kind="決定")])
    _item("label_rule", {"unique_preferred": {"1,2": "乙", "3,4": "丙"}, "most_common": {"1,2,3,4": "z"},
                         "tie_by_n_order": {"1,2": "c", "3,4": "a"}},
          {"unique_preferred": {",".join(str(i) for i in k): v for k, v in unique.items()},
           "most_common": {",".join(str(i) for i in k): v for k, v in most.items()},
           "tie_by_n_order": {",".join(str(i) for i in k): v for k, v in tie.items()}})

    # 8. 代表 = 並びの先頭 (強度降順)、同点は配列順: 配列 [1(強度1), 3(強度3), 2(強度3)] → 並び [3, 2, 1]・代表 3
    from raiya.alaya import seed_groups

    g8 = seed_groups([s(1, ["甲", "乙"], kind="決定", strength=1), s(3, ["甲", "乙"], kind="決定", strength=3),
                      s(2, ["甲", "乙"], kind="決定", strength=3)])
    _item("representative_is_first_in_order_ties_by_array",
          {"member_ids": [3, 2, 1], "representative": 3, "size": 3},
          {"member_ids": g8[0]["member_ids"], "representative": g8[0]["representative"], "size": g8[0]["size"]})

    # 9. 依他起だけの群は ②③ に群として出ない (B8): 群の印も ② も無い
    # #1 は強度 2・縁 甲 乙 で発芽する (依他起は 2 語・強度 2)、#2 は強度 1 で発芽しない。② が出れば #2 が亦如是に載る。
    seeds9 = [s(1, ["甲", "乙", "丙"], kind="決定", sansho_class="依他起", strength=2),
              s(2, ["甲", "乙", "丁"], kind="決定", sansho_class="依他起", strength=1)]
    r9 = _ly_recall(seeds9, "甲 乙")
    r9n = _ly_recall(seeds9, "無関係")
    _item("inferred_only_group_is_not_a_group_in_2_3",
          {"germinated": [1], "groups": [], "siblings": [], "group_mark": False, "③_ids": [1, 2]},
          {"germinated": [x["id"] for x in r9["seeds"]], "groups": r9["circles"]["groups"],
           "siblings": r9["circles"]["siblings"],
           "group_mark": ("[群「" in r9["narrative"]) or ("亦如是" in r9["narrative"]),
           "③_ids": sorted(x["id"] for x in r9n["index"])})

    # 10. 再開点・性向の群は ③ に出ない (B9)。全域なら ④ に数える (P の群は数えない)
    r10 = _ly_recall([s(1, ["甲", "乙"], kind="性向"), s(2, ["甲", "乙"], kind="性向"),
                      s(3, ["甲", "乙"], kind="再開点"), s(4, ["甲", "乙"], kind="再開点"),
                      s(5, ["丙", "丁"], kind="性向", project=None), s(6, ["丙", "丁"], kind="性向", project=None),
                      s(7, ["丙", "丁"], kind="再開点", project=None), s(8, ["丙", "丁"], kind="再開点", project=None)],
                     "無関係")
    _item("disposition_and_resume_groups_leave_3_and_count_in_4_when_global",
          {"index_ids": [], "global": [{"kind": "再開点", "groups": 1, "seeds": 2},
                                       {"kind": "性向", "groups": 1, "seeds": 2}]},
          {"index_ids": [x["id"] for x in r10["index"]], "global": r10["circles"]["global"]})

    # 11. 代表が休眠なら次の成員 (群の並びで)。代表より後ろの成員は ③ の代表選びで stat されない (B10)。
    # 倉庫の順 #1 #2 #3、強度 1 / 3 / 2 → 群の並びは #2 #3 #1。#2 は休眠 → 次は群の並びの #3 (在る)。#1 は代表より後ろ。
    absent_a = LAYERED_ABSENT_ROOT + "/a/two.md"
    absent_b = LAYERED_ABSENT_ROOT + "/b/one.md"
    paths = {"two": absent_a, "three_present": LAYERED_PRESENT_PATH, "one": absent_b}
    seeds11 = [s(1, ["資料", "場所"], kind="所在", strength=1, content="資料は {} にある。".format(absent_b)),
               s(2, ["資料", "場所"], kind="所在", strength=3, content="資料は {} にある。".format(absent_a)),
               s(3, ["資料", "場所"], kind="所在", strength=2, content="資料は {} にある。".format(LAYERED_PRESENT_PATH))]
    stat_counts = {k: 0 for k in paths}
    real_stat = os.stat

    def _counting_stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            text = os.fsdecode(path)
            for k, v in paths.items():
                if text == v:
                    stat_counts[k] += 1
        return real_stat(path, *args, **kwargs)

    with mock.patch("os.stat", side_effect=_counting_stat):
        r11 = _ly_recall(seeds11, "無関係")
    _item("dormant_representative_yields_to_next_member_in_group_order_and_later_members_are_not_stat",
          {"member_ids": [2, 3, 1], "index_ids": [3], "group_size": 3,
           "stat_calls": {"two": 2, "three_present": 2, "one": 1}},
          {"member_ids": (r11["circles"]["groups"][0]["member_ids"] if r11["circles"]["groups"] else None),
           "index_ids": [x["id"] for x in r11["index"]],
           "group_size": (r11["circles"]["groups"][0]["size"] if r11["circles"]["groups"] else None),
           "stat_calls": stat_counts})

    # 12. 双子の H は E と同じ群で、件数に入り、② に id として出ず、③ の代表にならない。
    # E は休眠 (在り得ない root)・H は在る path で、H が配列の先 (強度も高い)。H が代表になれば index_ids に H が出る。
    e_path = LAYERED_ABSENT_ROOT + "/twin/e.md"
    twins = [s(2, ["甲", "乙"], kind="所在", sansho_class="依他起", strength=2,
               content="資料は {} にある。".format(LAYERED_PRESENT_PATH)),
             s(1, ["甲", "乙"], kind="所在", sansho_class="圓成實", strength=1,
               content="資料は {} にある。".format(e_path))]
    r12 = _ly_recall(twins, "無関係")
    twins_live = [dict(twins[0]), dict(twins[1], content="資料は {} にある。".format(LAYERED_PRESENT_PATH))]
    r12live = _ly_recall(twins_live, "無関係")
    r12a = _ly_recall([twins_live[1], dict(twins_live[0])], "甲 乙")
    _item("twin_hidden_never_becomes_representative_even_when_e_is_dormant",
          {"group": [[1, 2]], "index_ids": [], "groups": [],
           "live_index_ids": [1], "live_group_size": 2,
           "sibling_lines": [{"size": 2, "listed": [], "more": 1}], "germinated": [1]},
          {"group": _ly_groups(twins), "index_ids": [x["id"] for x in r12["index"]],
           "groups": r12["circles"]["groups"],
           "live_index_ids": [x["id"] for x in r12live["index"]],
           "live_group_size": (r12live["circles"]["groups"][0]["size"] if r12live["circles"]["groups"] else None),
           "sibling_lines": [{"size": x["size"], "listed": x["listed"], "more": x["more"]}
                             for x in r12a["circles"]["siblings"]],
           "germinated": [x["id"] for x in r12a["seeds"]]})

    # 13. 容量: 長い入力でも注入文 ≤ INJECTION_MAX_CHARS、落ちる順は外側から (③ が切れたら ④ は描画しない)
    long_body = "長い本文" * 40
    seeds13 = []
    for i in range(1, 13):
        seeds13.append(s(i, ["発芽{}a".format(i), "発芽{}b".format(i)], kind="決定", content=long_body + str(i)))
    for i in range(13, 25):
        seeds13.append(s(i, ["索引{}a".format(i), "索引{}b".format(i)], kind="回避策", content=long_body + str(i)))
    for i in range(25, 37):
        seeds13.append(s(i, ["索引{}a".format(i), "索引{}b".format(i)], kind="所在", content=long_body + str(i)))
    seeds13.append(s(37, ["全域甲", "全域乙"], kind="決定", project=None, content="全域の話A"))
    seeds13.append(s(38, ["全域甲", "全域乙"], kind="決定", project=None, content="全域の話B"))
    r13 = _ly_recall(seeds13, "発芽1a 発芽2a 発芽3a")
    cut13 = r13["circles"]["cut"]
    _item("capacity_caps_narrative_and_cuts_from_the_outside",
          {"within_cap": True, "germinated_cut": 0, "index_cut_positive": True, "global_cut": 1,
           "global_rendered": []},
          {"within_cap": len(r13["narrative"]) <= manas.INJECTION_MAX_CHARS,
           "germinated_cut": cut13["germinated"], "index_cut_positive": cut13["index"] > 0,
           "global_cut": cut13["global"], "global_rendered": r13["circles"]["global"]})

    # 14. ④ に他の project が出ない: 全域の群だけを数え、Q の群 (回避策 3 件) は名指さない・project=None なら ④ なし
    seeds14 = [s(1, ["甲", "乙"], kind="決定", project=None, content="全域の決定A"),
               s(2, ["甲", "乙"], kind="決定", project=None, content="全域の決定B"),
               s(3, ["丙", "丁"], kind="回避策", project=Q, content="他所の回避策秘密A"),
               s(4, ["丙", "丁"], kind="回避策", project=Q, content="他所の回避策秘密B"),
               s(5, ["丙", "丁"], kind="回避策", project=Q, content="他所の回避策秘密C")]
    r14 = _ly_recall(seeds14, "無関係")
    r14n = _ly_recall(seeds14, "無関係", project=None)
    _item("global_line_never_names_another_project",
          {"global": [{"kind": "決定", "groups": 1, "seeds": 2}], "other_project_in_text": False,
           "none_project_global": []},
          {"global": r14["circles"]["global"],
           "other_project_in_text": ("他所" in r14["narrative"]) or (Q in r14["narrative"]),
           "none_project_global": r14n["circles"]["global"]})

    # 15. 統合: 窓に触れた 31 件目の種子 (上位 30 の外) が related_total に入り、プロンプトに「(他に 1 件)」が出る
    seeds15 = [s(i, ["語{:02d}".format(i)], kind="決定", sansho_class="圓成實") for i in range(1, 31)]
    seeds15.append(s(31, ["語31"], kind="決定", sansho_class="依他起", strength=1))   # 発芽しない強さでも関連には入る
    tmp15 = tempfile.mkdtemp(prefix="receipt_harness_ly_")
    try:
        store15 = _wd_store(tmp15, seeds15)
        text15 = "".join("語{:02d} ".format(i) for i in range(1, 32))
        view = store15.consolidation_view({P}, [text15], related_max=30, groups_max=20)
        window = [ledger_mod.LedgerTurn(user_text=text15, assistant_text="了解", id=1, life=3, project=P)]
        prompt = _build_consolidate_prompt(window, view)
    finally:
        shutil.rmtree(tmp15, ignore_errors=True)
    _item("consolidation_sees_the_31st_matched_seed_and_says_how_many_more",
          {"related_total": 31, "shown": 30, "31st_shown": False, "prompt_has_more_line": True},
          {"related_total": view["related_total"], "shown": len(view["related"]),
           "31st_shown": any(x["id"] == 31 for x in view["related"]),
           "prompt_has_more_line": "(他に 1 件)" in prompt})

    matched = sum(1 for it in items if it["expected"] == it["got"])
    return {"件数": len(items), "一致": matched, "詳細": items}


# --- §5.3 事前登録予測の判定 (measured 値の上の機械述語。hit にリテラル禁止) --------


COMPACT_FIXTURE = os.path.join(BASE_DIR, "tests", "fixtures", "compact_fixture_20261027.json")


def _compact_state_digest(state_dir):
    """state dir の全ファイル (`.lock` を除く) の相対名 + 内容の sha256 (読むだけの検査用。パスは混ぜない)。"""
    import hashlib

    digest = hashlib.sha256()
    for root, dirs, files in os.walk(state_dir):
        dirs.sort()
        for name in sorted(files):
            if name == ".lock":
                continue
            full = os.path.join(root, name)
            digest.update(os.path.relpath(full, state_dir).encode())
            with open(full, "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()


def run_compact_harness():
    """圧縮 (#63 compact・spec 2026-10-27-compact-design.md §8): 固定 fixture で `MemoryEngine.compact` を 5 case 流す。
    LLM 非依存・本番 state は読まない・now() 不使用 (時刻・tmp パスは記録に入れない)。
    `read_only` = 全 case の前後で state dir の sha256 が同じ。`fired_equals_explain` = 窓が空でない各 case の
    ① の種子 id (順序も) が `explain_recall` の発芽と同じ (空の窓は発芽の判定をしないので対象外)。"""
    import json
    from unittest import mock

    from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine
    from raiya.snapshot import write_state

    with open(COMPACT_FIXTURE, "r", encoding="utf-8") as f:
        fx = json.load(f)
    text, project = fx["text"], fx["project"]
    tmpdir = tempfile.mkdtemp(prefix="receipt_harness_compact_")
    try:
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        env["RAIYA_CONFIG"] = ""
        with mock.patch.dict(os.environ, env, clear=True):
            state_dir = os.path.join(tmpdir, "state")
            write_state(state_dir, {"seeds": fx["seeds"], "alaya_meta": {"life": fx["life"]},
                                    "ledger_rows": fx["ledger_rows"]})
            engine = MemoryEngine(state_dir=state_dir, new_life=False)
            before = _compact_state_digest(state_dir)
            explained = [g["id"] for g in engine.explain_recall(text, project=project)["germinated"]]
            cases = []
            fired_equal = True
            for case in fx["cases"]:
                window = [] if case.get("empty_window") else fx["window"]
                r = engine.compact(window, text, project=project, budget_chars=case["budget_chars"])
                if window:
                    fired = [s["id"] for s in r["seeds"] if s["circle"] == "germinated"]
                    fired_equal = fired_equal and fired == explained
                cases.append({
                    "label": case["label"],
                    "budget_chars": r["budget_chars"],
                    "keep": r["keep"],
                    "elided": r["elided"],
                    "unknown_ids": r["unknown_ids"],
                    "cut": r["cut"],
                    "turns": [[t["id"], t["decision"], t["circle"], t["reason"]] for t in r["turns"]],
                    "seeds": [[s["id"], s["circle"], s["in_window"], s["outside_window"], s["off_ledger"]]
                              for s in r["seeds"]],
                    "chars": r["chars"],
                    "narrative": r["narrative"],
                })
            after = _compact_state_digest(state_dir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"件数": len(cases), "read_only": before == after, "fired_equals_explain": fired_equal, "詳細": cases}


def judge_p_m3(measured_p_m3):
    """P-M3: 旧規則の誤併合件数>=1 かつ 新規則の誤併合件数==0 で hit。

    `measured_p_m3` は `run_p_m3_harness()` の戻り値そのもの (定数を書き込まない)。
    """
    import datetime

    hit = measured_p_m3["旧規則誤併合件数"] >= 1 and measured_p_m3["新規則誤併合件数"] == 0
    return {
        "id": "P-M3",
        "measured": measured_p_m3,
        "hit": bool(hit),
        "judged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def judge_p_m1(smoke_measured):
    """P-M1: 実LLM提案のうち span なし or 非接地 (=quarantine行き) が >=1 件で hit。

    `smoke_measured` は §5.2 smoke ランナーが実測した dict (proposal_total /
    class_counts / quarantine_count 等) をそのまま渡す。定数を書き込まない。
    """
    import datetime

    quarantine_count = smoke_measured.get("quarantine_count")
    hit = quarantine_count is not None and quarantine_count >= 1
    return {
        "id": "P-M1",
        "measured": smoke_measured,
        "hit": bool(hit),
        "judged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def judge_p_m2(smoke_measured):
    """P-M2: 接地した提案のうち 依他起 が 圓成實 より多ければ hit。

    `smoke_measured["class_counts"]` = {"圓成實": n, "依他起": m, "遍計所執": k, ...}。
    """
    import datetime

    class_counts = smoke_measured.get("class_counts") or {}
    enjoujitsu = class_counts.get("圓成實", 0)
    etaki = class_counts.get("依他起", 0)
    hit = etaki > enjoujitsu
    return {
        "id": "P-M2",
        "measured": smoke_measured,
        "hit": bool(hit),
        "judged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# --- 想起 fixture (改修 5 spec §8: 要る記憶が要る時に出たか) ------------------------
# 本番種子 (tests/fixtures/recall_seeds_20261017.json) に kind/project を割り当て
# (tests/fixtures/recall_fixture_20261017.json) て 1 つの alaya.json に実体化し、
# `MemoryEngine.recall` をクエリごとに呼んで kind ごとの precision / recall を数える。
# 改修前 (recall に project 引数が無い) と改修後 (project / session_start あり) を**同じ入力**で
# 走らせるため、kind/project の割り当ては常に適用する (改修前のコードはこれらの欄を読まない)。
# 決定論・LLM なし。本番 state は読まない。
RECALL_SEEDS_FIXTURE = os.path.join(BASE_DIR, "tests", "fixtures", "recall_seeds_20261017.json")
RECALL_FIXTURE = os.path.join(BASE_DIR, "tests", "fixtures", "recall_fixture_20261017.json")
RECALL_FIXTURE_LIFE = 52


def _load_recall_fixture():
    """(seeds, queries)。種子は annotation の kind/project と overrides を適用した dict の list。"""
    import json

    with open(RECALL_SEEDS_FIXTURE, "r", encoding="utf-8") as f:
        seeds = json.load(f)["seeds"]
    with open(RECALL_FIXTURE, "r", encoding="utf-8") as f:
        fx = json.load(f)
    annotations = fx.get("annotations") or {}
    overrides = fx.get("overrides") or {}
    for seed in seeds:
        a = annotations.get(str(seed["id"])) or {}
        seed["kind"] = a.get("kind", "性向")
        seed["project"] = a.get("project")
        seed["superseded_by"] = None
        for key, value in (overrides.get(str(seed["id"])) or {}).items():
            seed[key] = value
    return seeds, list(fx.get("queries") or [])


def _call_recall(engine, query):
    """改修前後で同じ入力を渡す: `recall` が受け取れる引数だけを渡す。"""
    import inspect

    params = inspect.signature(engine.recall).parameters
    kwargs = {"context": list(query.get("context") or [])}
    if "project" in params:
        kwargs["project"] = query.get("project")
    if "session_start" in params:
        kwargs["session_start"] = bool(query.get("session_start"))
    return engine.recall(query["prompt"], **kwargs)


def _returned_seeds(recalled):
    """想起が返した種子 (発芽 + 注入索引 + 再開点) を id -> seed で返す。"""
    out = {}
    for seed in list(recalled.get("seeds") or []) + list(recalled.get("index") or []):
        out[seed["id"]] = seed
    resume = recalled.get("resume")
    if isinstance(resume, dict) and "id" in resume:
        out[resume["id"]] = resume
    return out


def _circle_counts(recalled, rendered_cap=None):
    """recall の戻り値 (`circles`・`seeds`・`index`) から円ごとの件数を導く (層化 計画 §7)。数えるだけで判定はしない。
    ① = 描画された発芽 = min(発芽した全件, `rendered_cap`) - 容量で落ちた (`rendered_cap` = ego_weight が決める ① の最大件数
    `engine.manas.rendered_fired_count()`。None なら上限なし)、② = 描画された行数、③ = 描画された項目 (群の代表 / 1 件の種子)、
    ④ = 全域の件数の行が描画されたか。`circles` / `index` が無い戻り値は 0 と空の cut。"""
    circles = recalled.get("circles") or {}
    cut = dict(circles.get("cut") or {})
    groups = len(circles.get("groups") or [])
    index = len(recalled.get("index") or [])
    fired = len(recalled.get("seeds") or [])
    if rendered_cap is not None:
        fired = min(fired, rendered_cap)
    return {
        "①": fired - int(cut.get("germinated", 0)),
        "②": len(circles.get("siblings") or []),
        "③_groups": groups,
        "③_singletons": index - groups,
        "④": 1 if circles.get("global") else 0,
        "cut": cut,
    }


def run_recall_harness(state_dir=None):
    """想起 fixture を実 MemoryEngine に通し、kind ごとの precision / recall・性向の混入・
    他 project の混入を数えて dict で返す。"""
    import json

    from raiya.engine import MemoryEngine

    owned = None
    if state_dir is None:
        owned = tempfile.mkdtemp(prefix="receipt_recall_")
        state_dir = owned
    seeds, queries = _load_recall_fixture()
    by_id = {s["id"]: s for s in seeds}
    from raiya.snapshot import write_state

    write_state(state_dir, {"seeds": seeds, "alaya_meta": {"life": RECALL_FIXTURE_LIFE}})
    engine = MemoryEngine(state_dir=state_dir, new_life=False)

    per_query = []
    agg = {}
    disposition_leaks = 0
    cross_project_leaks = 0
    index_sizes = []
    for q in queries:
        recalled = _call_recall(engine, q)
        returned = _returned_seeds(recalled)
        expected = set(q.get("expected_seed_ids") or [])
        returned_ids = set(returned)
        tp = expected & returned_ids
        leaks_d = sorted(i for i in returned_ids if by_id[i]["kind"] == "性向")
        leaks_p = sorted(
            i for i in returned_ids if by_id[i]["project"] not in (None, q.get("project"))
        )
        disposition_leaks += len(leaks_d)
        cross_project_leaks += len(leaks_p)
        index_sizes.append(len(recalled.get("index") or []))
        for i in expected:
            k = by_id[i]["kind"]
            agg.setdefault(k, {"tp": 0, "returned": 0, "expected": 0})["expected"] += 1
        for i in returned_ids:
            k = by_id[i]["kind"]
            agg.setdefault(k, {"tp": 0, "returned": 0, "expected": 0})["returned"] += 1
        for i in tp:
            agg[by_id[i]["kind"]]["tp"] += 1
        per_query.append(
            {
                "label": q["label"],
                "project": q.get("project"),
                "expected": sorted(expected),
                "returned": sorted(returned_ids),
                "distractors_returned": sorted(set(q.get("distractor_seed_ids") or []) & returned_ids),
                "disposition_leaks": leaks_d,
                "cross_project_leaks": leaks_p,
                "narrative_head": (recalled.get("narrative") or "").split("\n", 1)[0],
                "narrative_len": len(recalled.get("narrative") or ""),
                "circle_counts": _circle_counts(recalled, engine.manas.rendered_fired_count()),
            }
        )
    by_kind = {}
    for k, v in sorted(agg.items()):
        by_kind[k] = {
            "tp": v["tp"],
            "returned": v["returned"],
            "expected": v["expected"],
            "precision": (v["tp"] / v["returned"]) if v["returned"] else 0.0,
            "recall": (v["tp"] / v["expected"]) if v["expected"] else 0.0,
        }
    if owned is not None:
        shutil.rmtree(owned, ignore_errors=True)
    return {
        "fixture": {"seeds": len(seeds), "queries": len(queries)},
        "by_kind": by_kind,
        "disposition_leaks": disposition_leaks,
        "cross_project_leaks": cross_project_leaks,
        "index_sizes": index_sizes,
        "queries": per_query,
    }


def _receipts_dir():
    return os.path.join(BASE_DIR, "receipts")


def _parse_out_dir(argv):
    """`--out DIR` / `--out=DIR` を読む。`--out` が無ければ None (= 既定の receipts/)。

    `--out` が最後の引数で値が無い・`--out=` が空のときは SystemExit (stderr に理由) にする。
    receipts/ へ黙って戻すと、一時ディレクトリに書くつもりの実行が受領証を上書きする。
    """
    for i, arg in enumerate(argv):
        if arg == "--out":
            if i + 1 >= len(argv) or not argv[i + 1]:
                sys.exit("receipt_harness.py: --out には出力先ディレクトリが要ります")
            return argv[i + 1]
        if arg.startswith("--out="):
            value = arg[len("--out="):]
            if not value:
                sys.exit("receipt_harness.py: --out= には出力先ディレクトリが要ります")
            return value
    return None


def main(out_dir=None):
    """CLI エントリ (`python3 scripts/receipt_harness.py [--out DIR] [--recall-before]`)。決定論
    fixture のみ実行し (LLM 呼び出しなし)、`out_dir` (None なら CLI の `--out`、それも
    無ければ receipts/) の deterministic.json / recall.json へ書き出す。
    unittest discover には拾われない (test*.py という名前ではない)。

    `out_dir` を渡すのは第三分→第四分の互証 (tests/test_receipt_reproduces.py): 今の
    コードで流し直した結果を、一時ディレクトリに書いて receipts/ の記録と比べる
    (七文書による検証層 設計書 §1.3)。`out_dir` を渡したときは argv の `--out` を読まない。
    """
    import datetime
    import json

    # 引数の誤りは fixture を流す前に止める。
    if out_dir is None:
        out_dir = _parse_out_dir(sys.argv[1:]) or _receipts_dir()

    deterministic = run_deterministic_harness()
    p_m3_measured = run_p_m3_harness()
    p_m3_judgment = judge_p_m3(p_m3_measured)

    receipt = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "§5.1_決定論fixture": deterministic,
        "P-M3": p_m3_judgment,
        "形の検査": run_kind_form_harness(),
        "休眠の抽出": run_dormancy_extract_harness(),
        "取り下げ": run_withdrawal_harness(),
        "双子照合": run_twin_match_harness(),
        "層化": run_layered_harness(),
        "圧縮": run_compact_harness(),
    }

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "deterministic.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)

    # 想起 fixture (改修 5 E7)。`--recall-before` は改修前のコードで 1 度だけ採り、
    # 以後は recall.json に書く (before は固定したまま比較の基準にする)。
    recall_name = "recall_before.json" if "--recall-before" in sys.argv[1:] else "recall.json"
    recall_receipt = {
        "generated_at": receipt["generated_at"],
        "想起fixture": run_recall_harness(),
    }
    recall_path = os.path.join(out_dir, recall_name)
    with open(recall_path, "w", encoding="utf-8") as f:
        json.dump(recall_receipt, f, ensure_ascii=False, indent=2)

    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    print("\n--- written to {} and {} ---".format(out_path, recall_path))
    return receipt


if __name__ == "__main__":
    main()
