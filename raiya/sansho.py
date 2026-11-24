"""sansho.py -- 三性(さんしょう)ゲート: 熏習の資格判定

唯識の三性 (圓成實性/依他起性/遍計所執性) を、LLM の自己申告でなく決定論の逐語接地で
決める純粋ロジック。LLM に一切依存しない (七文書抽出仕様 IF-1 見分或時非量 = LLM の見分は
時に非真。ゆえに記憶の身分を LLM 自身に自称させない)。

三性の判定規則 (IF-2/3/4・KUN-5):
    圓成實 (えんじょうじつ)     -- 提案の span がユーザー発話への逐語接地
    依他起 (えたき)             -- 提案の span がアシスタント発話にのみ逐語接地 (条件付き生成物)
    遍計所執 (へんげしょしゅう) -- span が往復本文のどちらにも無い・空 (非接地=捏造)

判定の対象は 1 往復とは限らない (改修4 §4): `evaluate_proposal` は往復の窓 (Turn の列) と
複数 span を取り、各 span を窓内のいずれかの往復へ逐語接地する。1 つでも非接地の span が
あれば提案全体が遍計所執 (部分的な捏造も通さない)。全 span 接地なら、全 span が
ユーザー発話に接地していれば圓成實、1 つでも assistant にだけ接地していれば依他起
(圓滿成就 T1585_008:347「二空所顯圓滿成就，諸法實性，名圓成實」・2026-10-22 裁定)。

決定論・純粋関数のみ。曖昧一致 (編集距離・埋め込み) は使わない: 1 文字でも内容が違えば
不一致のまま (嘘を通す方向に働かない)。正規化 `_n` は空白差のみを同一視する
(CJK 隣接空白の除去畳み規則の写し。標準ライブラリのみ)。

設計書: 記憶機能設計書 §3.3 (本モジュールが実装する三性ゲート規則の正本)。
出自: cosmicanalysis/src/Eight_Consciousness/sansho.py (波1〜2)。標準ライブラリのみで
自己完結しているため raiya への移植で変更なし (「機構は持ち出す、語彙と統治は置いていく」の
機構そのもの)。
"""
import re
import unicodedata
from dataclasses import dataclass

# --- 正規化 (判定不変。空白差のみを同一視し、内容差は1文字でも通さない) ---
_WS_RE = re.compile(r"\s+")
# CJK 隣接空白の除去畳み。隣接のどちらか一方でも CJK なら (`\s+` で既に1個へ畳んだ) 空白を
# 除去する。除去対象の空白は必ず片側が CJK であることを要求するため、英単語同士の融合
# (the rapist -> therapist 型の偽の果) は構造的に起こらない。
# クラス = かな+CJK記号/句読点(U+3001-U+30FF)・CJK統合漢字(U+3400-U+9FFF)・
# CJK互換漢字(U+F900-U+FAFF)。全角空白(U+3000)は NFKC で半角空白化されるためクラスに
# 含めない(空白側で畳まれる)。範囲は \u エスケープで書く(互換漢字と通常の CJK統合漢字は
# 見た目が同一な字が多く、手打ちのリテラル文字はどちらの符号位置か目視で判別できないため)。
_CJK_RANGE = "\u3001-\u30ff\u3400-\u9fff\uf900-\ufaff"  # かな+CJK記号/句読点・CJK統合漢字・CJK互換漢字 (\u エスケープ表記=LOW-2)
_CJK_ADJACENT_WS_RE = re.compile("(?<=[" + _CJK_RANGE + "]) | (?=[" + _CJK_RANGE + "])")


def _n(text):
    """判定不変の正規化: NFKC -> 空白連を1個へ -> CJK隣接空白の除去。

    空白差(改行・全角空白等)だけを同一視し、1文字でも内容が違えば不一致のまま
    (嘘を通す方向に働かない)。曖昧一致(編集距離・埋め込み)は使わない。
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = _WS_RE.sub(" ", s)
    s = _CJK_ADJACENT_WS_RE.sub("", s)
    return s


# --- 往復オブジェクト (KUN-7 能熏四義) ---
@dataclass(frozen=True)
class Turn:
    """一往復 (ユーザー発話 + アシスタント応答) を表す不変オブジェクト。

    熏習 API (alaya.AlayaStore.imprint) はこの型を必須引数に取る。末那識の注入文・
    内観の自己申告文は user/assistant の往復を持たない (どちらも単なる str) ため Turn を
    作れず、熏習できない -- 種が種を強化する自己循環 (我慢の構造) を API の型で禁じる。
    """

    user_text: str
    assistant_text: str


VALID_SANSHO = ("圓成實", "依他起", "遍計所執")

# 発芽順=クラス→強度降順のランク (§3.4)。遍計所執は quarantine 行きで germinate 対象外だが、
# 未分類 (sansho キーの無い旧形式データ等) は最下位に置く(安全側=クラス優先を壊さない)。
SANSHO_RANK = {"圓成實": 0, "依他起": 1, "遍計所執": 2}
_UNCLASSIFIED_RANK = len(SANSHO_RANK)


def sansho_sort_key(seed):
    """発芽順=クラス→強度降順のソートキー。alaya.germinate / manas.build_narrative で共有する。"""
    return (SANSHO_RANK.get(seed.get("sansho"), _UNCLASSIFIED_RANK), -seed.get("strength", 0))


# --- 三性ゲート本体 (§3.3) ---


def classify_sansho(span, user_text, assistant_text):
    """span (逐語引用) の三性を判定する (IF-2/3/4)。

    `_n(span)` が `_n(user_text)` の部分文字列なら圓成實。user には無く
    `_n(assistant_text)` の部分文字列なら依他起。どちらにも無い・span が空・空白のみ
    なら遍計所執。曖昧一致は使わない (1 文字でも改変されていれば不一致=遍計所執)。
    """
    n_span = _n(span or "")
    if not n_span.strip():
        return "遍計所執"
    if n_span in _n(user_text or ""):
        return "圓成實"
    if n_span in _n(assistant_text or ""):
        return "依他起"
    return "遍計所執"


def ground_conditions(conditions, user_text, assistant_text):
    """縁 (conditions) のうち往復本文 (user_text または assistant_text) に実在する語だけを残す。

    KUN-5 待眾緣: 縁は実在するものだけ。落ちた語は返り値に含まれない
    (呼び出し側で全滅なら却下する=evaluate_proposal)。戻り値は元の表記のまま
    (正規化は照合にのみ使い、格納値は書き換えない)。
    """
    un = _n(user_text or "")
    an = _n(assistant_text or "")
    grounded = []
    for c in conditions or []:
        if not c:
            continue
        nc = _n(c)
        if not nc.strip():
            continue
        if nc in un or nc in an:
            grounded.append(c)
    return grounded


def as_window(turns):
    """熏習の対象 (往復の窓) を Turn の tuple に正規化する (KUN-7 能熏四義)。

    Turn 1 件はそのまま 1 要素の窓。Turn の非空 list/tuple はそのまま。それ以外
    (str・空列・Turn でない要素を含む列) は TypeError: 末那識の注入文・内観の
    自己申告文は往復を持たないため熏習できない。
    """
    if isinstance(turns, Turn):
        return (turns,)
    if isinstance(turns, (list, tuple)) and turns and all(isinstance(t, Turn) for t in turns):
        return tuple(turns)
    raise TypeError(
        "sansho.evaluate_proposal() は Turn (往復) か Turn の列を必須引数に取ります: "
        "型 {!r} は往復オブジェクトではありません。末那識の注入文・内観の自己申告文は "
        "いずれも単なる str で user/assistant の往復を持たないため熏習できません "
        "(KUN-7 能熏四義)。".format(type(turns).__name__)
    )


def evidence_list(proposal):
    """提案の evidence を [{"source", "span"}] に正規化する (dict = 旧形式 1 span、list = 新形式)。

    dict でない要素 (str 等) は捨てずに {"source": "", "span": ""} へ落とす: 空 span は
    遍計所執 (非接地) と判定されるため、形の壊れた evidence を 1 つでも含む提案は全体が
    遍計所執になる。取り除くと接地済みの span だけが残り、判定が採用の側へ動いてしまう
    (嘘を通す方向に働かない)。
    """
    raw = (proposal or {}).get("evidence")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            out.append({"source": str(item.get("source") or ""), "span": str(item.get("span") or "")})
        else:
            out.append({"source": "", "span": ""})
    return out


def classify_span_in_window(span, window):
    """窓内の各往復で classify_sansho を評価し、最良クラスとその往復の位置を返す。

    圓成實 > 依他起 > 遍計所執 の順。最初に最良を出した往復の index を返す
    (どこにも無ければ (遍計所執, None))。
    """
    best = "遍計所執"
    best_index = None
    for index, turn in enumerate(window):
        cls = classify_sansho(span, turn.user_text, turn.assistant_text)
        if SANSHO_RANK[cls] < SANSHO_RANK[best]:
            best, best_index = cls, index
            if best == "圓成實":
                break
    return best, best_index


def ground_conditions_in_window(conditions, window):
    """縁を往復ごとに接地判定し、1 往復でも含む語を残す (順序保持)。

    往復本文を連結して照合しない: 連結すると往復境界を跨いだ偽の一致
    (往復1末尾 + 往復2先頭) が起こり、嘘を通す方向に働く。
    """
    grounded = []
    for c in conditions or []:
        if any(ground_conditions([c], t.user_text, t.assistant_text) for t in window):
            grounded.append(c)
    return grounded


def evaluate_proposal(proposal, turns):
    """提案を三性ゲートで判定する (§3.3・改修4 §4)。純粋ロジック・LLM 非介在・決定論。

    引数:
        proposal -- {"content", "conditions", "polarity", "evidence"} の dict。evidence は
                    {"source", "span"} (旧形式) か、その list (新形式・複数 span)。
                    proposal 自身に "sansho" キーがあっても読まない (EXE-15)。
        turns    -- Turn または Turn の列 (往復の窓)。それ以外は TypeError (KUN-7)。

    規則: 各 span を窓内のいずれかの往復に逐語接地する。span が 0 件、または 1 つでも
    非接地なら提案全体が遍計所執 (部分的な捏造も通さない)。全 span 接地なら、全 span が
    ユーザー発話に接地していれば圓成實、1 つでも assistant にだけ接地していれば依他起
    (圓滿成就 T1585_008:347・2026-10-22 裁定。any でなく all)。縁が窓内のどの往復にも
    無ければ reject。

    戻り値: {"sansho", "decision", "grounded_conditions", "reason",
             "spans": [{"source", "span", "sansho", "turn_index"}]}。
    """
    window = as_window(turns)
    proposal = proposal or {}
    span_results = []
    for item in evidence_list(proposal):
        cls, index = classify_span_in_window(item["span"], window)
        span_results.append(
            {"source": item["source"], "span": item["span"], "sansho": cls, "turn_index": index}
        )
    grounded = ground_conditions_in_window(proposal.get("conditions") or [], window)
    ungrounded = [s for s in span_results if s["sansho"] == "遍計所執"]

    if not span_results or ungrounded:
        if not span_results or all(not s["span"].strip() for s in ungrounded):
            reason = "span が空です (遍計所執=非接地)"
        elif len(span_results) == 1:
            reason = "span が往復本文(user/assistant)のいずれにも逐語一致しません(遍計所執=非接地)"
        else:
            reason = "span {}/{} 件が往復本文(user/assistant)のいずれにも逐語一致しません(遍計所執=非接地)".format(
                len(ungrounded), len(span_results)
            )
        return {
            "sansho": "遍計所執",
            "decision": "遍計所執",
            "grounded_conditions": grounded,
            "reason": reason,
            "spans": span_results,
        }

    # span_results は空でない (空なら上の分岐で遍計所執として返っている) ので all() は空列を受けない。
    sansho_class = "圓成實" if all(s["sansho"] == "圓成實" for s in span_results) else "依他起"

    if not grounded:
        return {
            "sansho": sansho_class,
            "decision": "reject",
            "grounded_conditions": [],
            "reason": "縁(conditions)が往復本文のいずれにも見つからず、発芽条件を持てないため却下",
            "spans": span_results,
        }

    return {
        "sansho": sansho_class,
        "decision": sansho_class,
        "grounded_conditions": grounded,
        "reason": "{} として熏習(縁 {} 件接地)".format(sansho_class, len(grounded)),
        "spans": span_results,
    }
