"""alaya.py -- 第八識(阿頼耶識): 種子倉庫

LLM に一切依存しない純粋ロジック。種子の発芽・熏習を管理する。
種子は削除も減衰もされない (KUN-3 恒隨轉。枯死は改修4 で撤去)。miss_streak は観測用。
永続化は JSON ファイル (呼び出し側が state/alaya.json 等のパスを渡す)。

種子スキーマ (記憶機能設計書 §3.1):
    id           連番
    content      傾向・記憶の一文。LLM の要約。併合/reinforce では不変。書き換わるのは
                 supersede のときだけで、旧値は history に残る (改修4 §5)
    strength     熏習強度 (熏習のたび、接地した往復の数だけ加算。各 span は
                 sansho.classify_span_in_window が返す 1 往復に帰属する -- 同じ span が
                 窓内の複数の往復に現れても 1 往復ぶんにしかならない。同じ往復から何本
                 span を引いても +1。1 往復の提案は常に +1)
    conditions   縁 = 発芽条件の話題語リスト (往復本文に実在が確認された語のみ)
    polarity     善 / 不善 / 無記 (改修5 で併合/reinforce の条件からは外れた。表示用に残す)
    kind         決定 / 回避策 / 所在 / 再開点 / 性向 (改修5 §3)。提案が自己申告するが、形の
                 検査 (`_resolve_kind`・§4.3) で読み替え/却下される。欄の無い提案は 性向
    project      種子が属する project (改修5 §4.2)。接地した往復の project 集合が 1 種類なら
                 それ、全て None なら None (全域)、2 種類以上なら却下 (project 跨ぎ)
    superseded_by 横断 supersede (改修5 §5.2) で隠された種子が指す新しい種子の id。None が通常。
                 立っても削除も減衰もしない: 注入索引・発芽・併合候補・target から外れるだけ
    withdrawn    人間が取り下げた記録 {by, at, reason, ledger_ids, spans, life, replaced_by}。None が通常。
                 キーが無い旧種子は None と読む (移行なし)。立つと発芽・索引・併合・target から外れるだけ
                 (削除も減衰もしない)。history に reason="withdrawn" (record 全体を埋め込む) / "restored"
    sansho       圓成實 / 依他起 のいずれか。熏習時に sansho.py が決定論で確定する。併合/
                 reinforce では不変 (依他起は圓成實を強化しない=昇格は無い。越境禁止は典拠なしの運用規則)。
                 supersede で content ごと置換されるときだけ提案のクラスに従う (圓成實は
                 何でも置換でき、依他起は依他起しか置換できない=越境禁止。ただし 再開点 だけは
                 例外で、クラスを問わず既存を置換する=改修5 §12.2 改訂・WM-3)。遍計所執は
                 seeds に構造的に入らず quarantine へ
    evidences    [{life, source: "user"|"assistant", span, ledger_id}] の追記専用リスト。
                 source は三性判定から導出する (提案の自称は読まない=EXE-15)。ledger_id が
                 None (台帳に無い往復) のときだけ turn スナップショットを併せ持つ
    history      [{life, content, conditions, sansho, polarity, reason}] の追記専用リスト。
                 supersede で置換される直前の旧値を積む (改修4 §5)
    planted_at   植えられた生 (life) 番号
    last_fired   最後に発芽した生番号 (未発芽なら None)
    fire_count   発芽回数
    miss_streak  連続不発芽の生の数 (観測用。枯死判定には使わない)

quarantine (遍計所執=非接地の観測用倉庫。germinate の対象外。§3.4):
    id / content / conditions / polarity / sansho(常に"遍計所執") / life / reason

imprint_log (熏習の決定を1提案=1行で記帳する追記専用ログ。§3.3 手順5・KUN-1 剎那滅):
    {life, turn, proposal, decision, reason, seed_id}
    seed_id は決定が書き込んだ種子 (reject / 遍計所執 は None。inspect spec §2.2 より前の行には無い)
    turn 欄は熏習した窓の形で 3 通り (plain Turn 1 件=従来のスナップショット / 全て台帳の
    往復={"ledger_ids": [...]} / それ以外={"turns": [スナップショット...]})

波3 (raiya 独立リポ化) での変更: 記憶機能設計書§4.3「状態は RAIYA_STATE_DIR の JSON 2+1
ファイル (alaya/manas/imprint_log)」に合わせ、imprint_log を alaya.json から**別ファイルへ
分離**した (cosmicanalysis/Eight_Consciousness 版の波1〜2は同居していた。新規リポにつき
移行ロジックは持たない)。`AlayaStore(path, imprint_log_path)` は両方必須。
"""
import copy
import functools
import itertools
import os
import re
import unicodedata

from . import markers, sansho

# --- 定数 (ここを変更すれば挙動を調整できる) ---
# 発芽閾値 (改修4 §5): ユーザーが逐語そう言った圓成實は 1 件でも発芽する。アシスタント
# 発話にのみ接地した依他起 (推定) は 2 件の裏付けを待つ (遅延発火)。未分類は安全側の 2。
GERMINATION_THRESHOLD_BY_CLASS = {"圓成實": 1, "依他起": 2}
GERMINATION_THRESHOLD_UNCLASSIFIED = 2
GERMINATION_MAX = 5         # 1 往復で発芽させる種子の最大数
INDEX_MAX = 30              # 想起時に注入する性向索引の上限件数 (改修4 §6)
# 索引注入の allowlist。依他起を常時載せる自己強化ループを閉じる。統合の索引は別 consumer。
INJECTION_INDEX_CLASSES = ("圓成實",)
QUARANTINE_MAX = 10         # quarantine(遍計所執の観測用倉庫)の保持上限 (記憶機能設計書§3.4
                             # 「上限付き」)。超過時は古い順に落とす。総件数は
                             # next_quarantine_id-1 が append-only な連番のため常に導出可能
                             # (evict しても id は巻き戻らない=audit 用の別カウンタは不要)。
VALID_POLARITIES = ("善", "不善", "無記")
# --- 改修5 (プロジェクト記憶): kind・形の検査・索引の上限 ---
# 記憶の単位 (spec §3.2)。順序は索引の並び順にもなる (kind → クラス → 強度降順)。
# 可熏性: 新しい kind はこの列挙への 1 行で足せる。
VALID_KINDS = ("決定", "回避策", "所在", "再開点", "性向")
DEFAULT_KIND = "性向"            # kind 欄の無い提案 (MCP memory_imprint / CLI propose) の扱い
# 形の検査 (spec §4.3): 価値の判定も LLM に任せず、決定論の文字列検査で切る。
# 決定 の形 (却下側の印 or 断定の形) の検査は `_resolve_kind`。却下側の印は「必須」ではなく (i) の一方
# (旧「両側必須」§11.6 は 2026-10-30-decision-without-rejection §7 U で改訂)。全言語の和集合 (marker-table spec)
DECISION_REJECT_MARKERS = markers.union("decision_reject")
WORKAROUND_CAUSE_MARKERS = markers.union("workaround_cause")   # 回避策 は原因が要る
PREFERENCE_SUFFIXES = markers.union("preference_suffix")   # 好みの形 (付録 A)
USER_CORRECTION_MARKERS = markers.union("user_correction")   # 回避策 の入口: ユーザーの逐語の訂正
WORKAROUND_AVOID_MARKERS = markers.union("workaround_avoid")   # 回避策 の自述の入口: 回避の言い切り (W1・#134)
SELF_STATED_CAUSE_MARKERS = markers.union("self_stated_cause")   # 自述の入口の原因の印 (「原因」だけ・W1 案 B)
HEARSAY_MARKERS = markers.union("hearsay")   # 再開点 の伝聞の言 (RH2・#129)
CONJECTURE_MARKERS = markers.union("conjecture")   # 決定 の描画で「決定(推量)」にする推量の印 (文末一致・描画のみ・#121 A')
DECISION_ASSERT_MARKERS = markers.union("decision_assert")   # 性向 の申告を 決定 へ上げる断定の印 (文末一致・#117)
# 注入索引の kind 別上限 (spec §6.3)。合計 30 = INDEX_MAX。再開点は索引に入れずセッション先頭で
# 1 件だけ出す (`resume_point`)。性向は既定で注入しない (§7・§11.2)。
INDEX_KIND_LIMITS = {"決定": 10, "回避策": 10, "所在": 10, "再開点": 0, "性向": 0}
# 注入索引に載せるクラス (kind 別・§12.5): 決定系はアシスタントが文にすることが多く依他起に
# なるので載せる (「(推定)」は user の span を持たない種子だけ)。性向は従来どおり圓成實のみ (改修4 で塞いだ自己強化ループは
# 性向で起きた)。
INJECTION_INDEX_CLASSES_BY_KIND = {
    "決定": ("圓成實", "依他起"),
    "回避策": ("圓成實", "依他起"),
    "所在": ("圓成實", "依他起"),
    "再開点": ("圓成實", "依他起"),
    "性向": INJECTION_INDEX_CLASSES,
}
# 落選理由 (観測 `raiya-inspect explain` 用・inspect spec §1.4)。判定は `_germination_verdicts` /
# `_index_verdicts` の 1 か所だけで行い、ここはその理由の表示文字列。
GERMINATION_REASON_SUPERSEDED = "superseded"
GERMINATION_REASON_WITHDRAWN = "取り下げ中"
GERMINATION_REASON_PROJECT = "project 違い"
GERMINATION_REASON_STRENGTH = "強度不足"
GERMINATION_REASON_RESUME = "再開点は発芽しない"
GERMINATION_REASON_DISPOSITION = "性向は既定で除外"
GERMINATION_REASON_CONDITIONS = "縁の一致が足りない ({}/{})"
GERMINATION_REASON_TWIN = "圓成實の双子 #{} がある"
GERMINATION_REASON_DORMANT = "休眠 (所在の path がこの機械に無い: {})"
GERMINATION_REASON_MAX = "発芽の上限で落ちた"
INDEX_REASON_SUPERSEDED = "superseded"
INDEX_REASON_WITHDRAWN = "取り下げ中"
INDEX_REASON_PROJECT = "project 違い"
INDEX_REASON_KIND_ZERO = "kind の上限 0"
INDEX_REASON_CLASS = "class が注入不可"
INDEX_REASON_TWIN = "圓成實の双子 #{} がある"
INDEX_REASON_DORMANT = "休眠 (所在の path がこの機械に無い: {})"
INDEX_REASON_MAX = "索引の上限で落ちた"
# 層化 (layered-memory plan §2.3): 同心円の外側は内側を繰り返さない・群は代表 1 件に畳む。
INDEX_REASON_FIRED = "発芽した (① に出る)"
INDEX_REASON_GLOBAL_GROUP = "全域の群「{}」は件数だけ (④)"
INDEX_REASON_GROUP_FIRED = "群「{}」は発芽した #{} の ② に出る"
INDEX_REASON_GROUP_MEMBER = "群「{}」の成員 (代表 #{})"
DISPOSITION_GERMINATION_CONDITIONS = 2   # 性向の発芽に要る縁の一致語数 (§6.2。弱い種子は条件を厳しく)
HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS = 2  # 横断 supersede に要る縁の共有語数 (§11.4 / §12.1)
# 併合・明示 reinforce に要る縁の共有語数の上限 (#104 再試験 x9_3s の裁定 2026-10-30)。実際の要求数は
# min(これ, 小さい側の縁の数)。双子照合 (#78) / 横断 supersede と同じ 2 語基準。ストップワード表は持たない。
MERGE_SHARED_CONDITIONS = HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS
VALID_ACTIONS = ("new", "reinforce", "supersede")  # 提案の action (改修4 §5)
# 併合(同趣旨)判定の類似度閾値 (記憶機能設計書 §3.5・裁定枠d: 初期値0.5。波2の fixture で
# 較正済み。以後は定数として凍結し、判断で動かさない)。
MERGE_SIMILARITY_THRESHOLD = 0.5
_D_NOTE = "user の逐語を持つ種子 #{} に assistant だけの提案が一致: 記録のみ、強度は変えない"
_EC2_NOTE = "EC2': user の span が無いか (疑の文を除いた残りが) 形を担わず、assistant の span は注入済み #{} の復唱 (縁共有) のため記録のみ、強度は変えない"
_EC2_TARGET_NOTE = "EC2': user の span が無いか (疑の文を除いた残りが) 形を担わず、assistant の span は注入済みの target_id #{} の復唱 (縁共有 1 語) のため記録のみ、強度は変えない"
_SC_NOTE = "圓成實 の種子 #{} と同じ内容の 依他起 の提案: 記録のみ、別に植えない"
_R_NOTE = "R: 同 session で注入済みの種子 #{} を assistant だけが言い直した提案: 記録のみ、強度は変えない"


def _default_state():
    return {
        "life": 0,
        "next_id": 1,
        "seeds": [],
        "quarantine": [],
        "next_quarantine_id": 1,
    }


def _default_imprint_log_state():
    return {"imprint_log": []}


def _ledger_id(turn):
    """往復が指す台帳の行 id。台帳に無い往復なら None (alaya は ledger を import しない)。

    `LedgerTurn` の既定値 `id=0` は「まだ台帳に記録されていない」を意味する (台帳の id は
    next_id=1 始まりで 0 を配らない) ため None に倒す -- 存在し得ない行を指させず、
    スナップショットを保持する側に倒す。
    """
    return getattr(turn, "id", None) or None


def seed_kind(seed):
    """種子の kind。欄が無い/不正なら 性向 (fail-soft の読み方。移行はしない=E8)。"""
    kind = seed.get("kind")
    return kind if kind in VALID_KINDS else DEFAULT_KIND


def has_user_span(seed):
    """種子が user 接地の span (evidence の source == "user" = 判定から導出した値) を 1 本でも持つか。
    `source` 欄の無い旧形式の evidence は user の span と数えない。注入の「(推定)」ラベルの唯一の根拠
    (spec 2026-10-30 §7 G・manas / inspect が共有)。"""
    return any(
        _is_user_span_evidence(e) for e in (seed.get("evidences") or [])
    )


def form_in_user_evidence(kind, evidences):
    """決定・回避策 の形を担う印が user の span (evidence の source == "user") にあるか。純関数。
    spec 2026-10-30-absent-response-seeding §8: 発芽閾値 (`_threshold`) と imprint の越境判定 E が共有する
    唯一の述語。決定 = 却下側の印 (DECISION_REJECT_MARKERS)、回避策 = 入口 (USER_CORRECTION_MARKERS) だけを
    見る (対の判定に要る turn_index は evidence に無い)。`source` 欄の無い旧 evidence・空の span は数えない。
    他の kind は偽。印の照合は `sansho._n` で正規化した span に当てる。"""
    if kind == "決定":
        table = DECISION_REJECT_MARKERS
    elif kind == "回避策":
        table = USER_CORRECTION_MARKERS
    else:
        return False
    return any(
        isinstance(e, dict) and e.get("source") == "user" and isinstance(e.get("span"), str) and bool(e.get("span"))
        and _has_marker(sansho._n(e["span"]), table)
        for e in (evidences or [])
    )


# 所在 の形に使う URL (manas._place_text と共有する唯一の正規表現)。
URL_RE = re.compile(r"https?://[^\s<>\"'「」()（）、。,\]}…]+")

RESUME_HEARSAY_RECORD_ONLY = "再開点: 伝聞の言だけの提案は 再開点 を置き換えない"
RESUME_NEEDS_FORM_IN_USER_SPAN = "圓成實 の 再開点 は user の逐語に問いでない文と縁が要る"
PROMOTION_NOTE = "性向 の申告に断定の印: 決定 として受理"
SELF_STATED_WORKAROUND_NOTE = "性向 の申告に 原因 → 回避 の形 (user の逐語): 回避策 として受理"
PLACE_NEEDS_FORM_IN_USER_SPAN = "圓成實 の 所在 は user の逐語に path か URL が要る"
DISPOSITION_NEEDS_FORM_IN_USER_SPAN = "圓成實 の 性向 は user の逐語に好みの印か縁が要る"
CONDITION_FROM_OTHER_SESSION = "縁が span と別の座 (session) の往復にしか接地していないため、別々の座の言を貼り合わせた提案は植えない"


def _is_locator_token(tok):
    """區切りの語 (spec 2026-11-08-place-form-path-only-design §7 (i')): 空白で区切った 1 語が `/` を含み、
    `/` だけでなく、`/` で分けた区画の全てが「数字だけか空」ではないもの。`tests/`・`docs/x.md`・`./a`・`and/or`・
    `tests/に置く`・`猫が好き/` は真、`release_meeting.md`・`alaya.py`・`2026/11/08`・`1/2`・`/` は偽。純関数。
    `sansho._n` は CJK 隣接の空白を詰めるので日付・分数にも日本語が貼り付く (`2026/11/08に決めた`・`比率は1/2`)。
    区画の「数字だけ」は、その区画の ASCII の字が 1 字以上あって全て数字のものとして読む (貼り付いた日本語は無視。
    ASCII の字が 1 字も無い区画 `猫が好き` は数字だけではない)。"""
    if not isinstance(tok, str) or "/" not in tok:
        return False
    return not all(seg == "" or _numeric_segment(seg) for seg in tok.split("/"))


def _numeric_segment(seg):
    ascii_part = "".join(ch for ch in seg if ord(ch) < 128)
    return ascii_part.isdigit()


def form_in_user_spans_for_kind(kind, user_spans, conditions):
    """圓成實 の 所在・性向 が「形を担う user の span」を持つか (spec 2026-10-30-question-as-evidence §7 L)。純関数。
    注意: `form_in_user_evidence` (決定・回避策 用) とは別物。あちらは evidence (source == "user") を入力にするが、
    こちらは imprint がゲート判定 (decision["spans"] の sansho == "圓成實") から選んだ user 接地の span の文字列の列
    (`user_spans`) と、提案の縁 `conditions` (decision["grounded_conditions"]) を入力にする。
    所在 = span の 1 本以上にローカル絶対 path か http(s) URL、または 區切りの語 (`_is_locator_token`、#149; 縁 1 語は形にしない)。性向 = span の 1 本以上に好みの印
    (`PREFERENCE_SUFFIXES`、および 回避策 の入口と認めた訂正の印 `USER_CORRECTION_MARKERS` = 回避策 から落ちた 性向 でも user が形を担う) か縁の 1 語以上 (`sansho._n` で正規化、casefold で大文字小文字は無視)。他の kind は真 (対象外)。"""
    user_spans = user_spans if isinstance(user_spans, (list, tuple)) else []
    conditions = conditions if isinstance(conditions, (list, tuple)) else []
    if kind not in ("所在", "性向"):
        return True
    spans = [sansho._n(x) for x in user_spans if isinstance(x, str) and x]
    if kind == "所在" and any(extract_local_paths(t) or URL_RE.search(t) for t in spans):
        return True
    if kind == "所在":
        return any(_is_locator_token(w) for t in spans for w in t.split())
    if kind == "性向" and any(
        _has_marker(t, PREFERENCE_SUFFIXES) or _has_marker(t, USER_CORRECTION_MARKERS) for t in spans
    ):
        return True
    conds = [sansho._n(c).casefold() for c in conditions if isinstance(c, str)]
    conds = [c for c in conds if c.strip()]   # 空白だけの縁はどの span にも一致させない
    return any(c in t.casefold() for t in spans for c in conds)


def seed_conditions(seed):
    """種子の縁 (str のみ)。欄が list でない (null / 数 / str / dict) 場合は []、list 内の非 str は
    落とす (fail-soft の読み方。保存値は書き換えない=E8)。str を渡すと 1 文字ずつ縁になるため、
    保存された縁を読む箇所は必ずここを通す。"""
    value = seed.get("conditions")
    return [c for c in value if isinstance(c, str)] if isinstance(value, list) else []


def _seed_project(seed):
    project = seed.get("project")
    return project if isinstance(project, str) else None


def is_superseded(seed):
    return seed.get("superseded_by") is not None


def is_withdrawn(seed):
    """人間が取り下げた種子 (欄 `withdrawn` が None でない)。キーが無い旧種子は None と読む (移行なし)。"""
    return seed.get("withdrawn") is not None


def is_live(seed):
    """生きた種子 = superseded でも取り下げ中でもない。"""
    return not is_superseded(seed) and not is_withdrawn(seed)


class WithdrawalRefused(Exception):
    """取り下げ / 復帰の拒否。メッセージは stderr 向けの日本語文。state は書き換えない。"""


def _history_entry(seed, life, reason, **extra):
    # 履歴は忠実に写す: list は複製、欄が無ければ [] (従来どおり)、壊れた非 list は
    # 深いコピーを生のまま残す (反復しない)
    stored = seed.get("conditions", [])
    stored = list(stored) if isinstance(stored, list) else copy.deepcopy(stored)
    entry = {
        "life": life,
        "content": seed.get("content", ""),
        "conditions": stored,
        "sansho": seed.get("sansho"),
        "polarity": seed.get("polarity"),
        "reason": reason,
    }
    entry.update(extra)
    return entry


def withdrawal_basis_problem(seed, ledger_ids, spans):
    """取り下げの根拠が種子の evidences に属すかの所属検査 (宣言の真偽は判定しない)。純関数。

    戻り値 `(problem | None, matched_spans)`。problem は最初の問題の日本語文。matched_spans は
    一致した台帳外 evidence の保存済み span (引数の文字列ではなく記録)。"""
    evidences = [e for e in (seed.get("evidences") or []) if isinstance(e, dict)]
    in_ledger = {e.get("ledger_id") for e in evidences if _is_int(e.get("ledger_id"))}
    off_ledger = [
        e for e in evidences
        if not _is_int(e.get("ledger_id")) and isinstance(e.get("span"), str) and e.get("span")
    ]
    if not in_ledger and not off_ledger:
        return "種子 #{} には名指せる根拠が無い".format(seed.get("id")), []
    if not ledger_ids and not spans:
        return "根拠が無い (--ledger-id か --span が要る)", []
    for lid in ledger_ids:
        if not _is_int(lid) or lid not in in_ledger:
            return "台帳 #{} は種子 #{} の根拠に無い".format(lid, seed.get("id")), []
    matched = []
    for span in spans:
        hit = None
        if isinstance(span, str):
            for e in off_ledger:
                stored = e.get("span")
                if isinstance(stored, str) and sansho._n(span) == sansho._n(stored):
                    hit = stored
                    break
        if hit is None:
            return "span は種子 #{} の台帳外の根拠に無い".format(seed.get("id")), []
        if hit not in matched:
            matched.append(hit)
    if in_ledger and not ledger_ids:
        return "種子 #{} は台帳の根拠を持つので --ledger-id が要る".format(seed.get("id")), []
    return None, matched


def _live_resume_point(seeds, project, exclude_id=None):
    """同 project の生きた 再開点 のうち id 最大の 1 件 (再開点 の特則・imprint と precheck で共有)。"""
    existing = None
    for seed in seeds:
        if exclude_id is not None and seed.get("id") == exclude_id:
            continue
        if _seed_kind(seed) == "再開点" and _seed_project(seed) == project and is_live(seed):
            if existing is None or seed["id"] > existing["id"]:
                existing = seed
    return existing


RESUME_NEW_ENJOUJITSU_ONLY = "再開点 の新規は 圓成實 のみ"


def plant_precheck(window, proposal, seeds=None, exclude_id=None):
    """提案が `imprint` で植わる (新規 / 併合 / 再開点の置換) かの事前検査。純関数・何も書かない。

    `imprint` と同じ規則だけを同じ順に再利用する (新しい規則は足さない): 三性ゲート
    (`sansho.evaluate_proposal`) -> kind の形の検査 (`_resolve_kind`) -> project (`_window_project`)
    -> (`seeds` が渡されたとき) 再開点 の新規は 圓成實 のみ (#101 B)。
    `seeds` = 現在の種子の列 (読むだけ)。`exclude_id` = この呼び出しの直後に取り下げる種子 id
    (取り下げ後の状態で判定するため生きた種子から除く)。`seeds` が None なら B は検査しない (状態を見ない)。
    RP2b (#115) は写さない: この関数は人の置き換え (`replacement_for`) からしか呼ばれず、imprint 側が人の置き換えを除外するため
    (写すと「precheck が None ⇔ imprint が植える」が崩れる)。L も写さない。`_resolve_kind` は `allow_promotion=False` (imprint も置き換えでは昇格しない)。
    植わるなら None、植わらないなら理由の日本語文。窓は呼び出し側がゲート用に整えたもの。"""
    window = sansho.as_window(window)
    decision = sansho.evaluate_proposal(proposal, window)
    if decision["decision"] == "reject":
        return decision["reason"]
    if decision["decision"] == "遍計所執":
        return "遍計所執 (接地しない提案): {}".format(decision["reason"])
    kind, kind_note = _resolve_kind(proposal, decision["spans"], decision["grounded_conditions"], window, allow_promotion=False)
    if kind is None:
        return kind_note
    indexes = {s["turn_index"] for s in decision["spans"]}
    status, value = _window_project(window, indexes)
    if status == "reject":
        return value
    if (
        seeds is not None
        and kind == "再開点"
        and decision["decision"] != "圓成實"
        and _live_resume_point(seeds, value, exclude_id) is None
    ):
        return RESUME_NEW_ENJOUJITSU_ONLY
    return None


def project_matches(seed, project):
    """発芽・索引の第一条件 (待眾緣・§6.2): 全域 (None) の種子は常に真。`project` が集合なら
    そのどれかに属すれば真 (統合窓は複数 project が混ざりうる・§4.2)。"""
    seed_project = _seed_project(seed)
    if seed_project is None:
        return True
    if isinstance(project, (set, frozenset, list, tuple)):
        return seed_project in project
    return seed_project == project


# 旧名 (alaya 内部と既存の呼び出し元のため残す)。他モジュールは公開名を使う。
_seed_kind = seed_kind
_is_superseded = is_superseded
_project_matches = project_matches


def _has_marker(text, marker_list):
    """text に marker_list のどれかが含まれるか。両側を casefold して比べる (比較のためだけ)。"""
    folded = (text or "").casefold()
    return any(m.casefold() in folded for m in marker_list)


def _ends_with_marker(text, marker_list):
    folded = (text or "").casefold()
    return any(folded.endswith(m.casefold()) for m in marker_list)


def _has_preference_suffix(content):
    body = (content or "").strip().rstrip("。.")
    return _ends_with_marker(body, PREFERENCE_SUFFIXES)


def _twin_shadows(seeds):
    """依他起の双子の判定 (twin-seed-index spec §2.2/§2.3)。戻り値 `{id(H): E の seed id}`。

    H = 生きている依他起、E = 生きている圓成實で、同 kind ∧ 同 project ∧ 縁 (str・重複除去・完全一致) を
    `HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS` 語以上共有するもの。H を隠す E のうち id 最小を返す。
    純粋関数 (種子に何も書かない)。(kind, project) ごとの縁の転置表で数え、総当たりはしない。
    sansho 欄の無い種子は E にも H にもならない。"""
    buckets = {}
    for seed in seeds:
        if seed.get("sansho") != "圓成實" or not is_live(seed):
            continue
        table = buckets.setdefault((_seed_kind(seed), _seed_project(seed)), {})
        for cond in set(seed_conditions(seed)):
            table.setdefault(cond, []).append(seed["id"])
    shadows = {}
    if not buckets:
        return shadows
    for seed in seeds:
        if seed.get("sansho") != "依他起" or not is_live(seed):
            continue
        table = buckets.get((_seed_kind(seed), _seed_project(seed)))
        if not table:
            continue
        counts = {}
        for cond in set(seed_conditions(seed)):
            for e_id in table.get(cond, ()):
                if e_id != seed.get("id"):
                    counts[e_id] = counts.get(e_id, 0) + 1
        hits = [e_id for e_id, n in counts.items() if n >= HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS]
        if hits:
            shadows[id(seed)] = min(hits)
    return shadows


def _seed_groups_by_seed(seeds):
    """群の計算本体 (layered-memory plan §1)。戻り値 `(groups, {id(seed): group_index})`。純粋関数・何も書かない。

    `is_live` の種子だけが対象。bucket = (kind, project) (全域は独立)。辺 = 相異なる縁 (str・空でない) を
    `HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS` 語以上、完全一致で共有すること (casefold / `sansho._n` / 部分一致は使わない)。
    「2 語以上共有」は「同じ縁のペアを持つ」と同値なので、ペア鍵の union-find で O(Σ C(k,2)) で連結を取る。
    1 件だけの連結は群として返さない。"""
    # id が int でない種子 (bool 含む) は成員にしない (member_ids を None 混じりにしない)。
    live = [s for s in seeds if is_live(s) and _is_int(s.get("id"))]
    parent = list(range(len(live)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    conds_of = []
    holders = {}
    bucket_members = {}
    for i, seed in enumerate(live):
        conds = sorted({c for c in seed_conditions(seed) if c})
        conds_of.append(conds)
        bucket = (_seed_kind(seed), _seed_project(seed))
        bucket_members.setdefault(bucket, []).append(i)
        for pair in itertools.combinations(conds, 2):
            key = (bucket, pair)
            j = holders.setdefault(key, i)
            if j != i:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    components = {}
    for i in range(len(live)):
        components.setdefault(find(i), []).append(i)
    # 群 = (bucket, 成員の live index の列)。成員は _index_sort_key の安定ソート。
    raw = []
    for members in components.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda i: _index_sort_key(live[i]))
        raw.append(members)
    raw.sort(key=lambda m: (_index_sort_key(live[m[0]]), m[0]))

    # 不共の判定用: bucket ごとに、縁 -> その縁を持つ群 (size >= 2) の番号の集合
    cond_groups = {}
    for gi, members in enumerate(raw):
        seed = live[members[0]]
        bucket = (_seed_kind(seed), _seed_project(seed))
        table = cond_groups.setdefault(bucket, {})
        for i in members:
            for c in conds_of[i]:
                table.setdefault(c, set()).add(gi)

    groups = []
    by_seed = {}
    for gi, members in enumerate(raw):
        rep = live[members[0]]
        bucket = (_seed_kind(rep), _seed_project(rep))
        counts = {}
        for i in members:
            for c in conds_of[i]:
                counts[c] = counts.get(c, 0) + 1
        candidates = [c for c, n in counts.items() if n >= 2]
        table = cond_groups[bucket]
        unique = [c for c in candidates if table[c] == {gi}]
        pool = unique or candidates
        label = min(pool, key=lambda c: (-counts[c], sansho._n(c), c))
        groups.append({
            "label": label,
            "kind": bucket[0],
            "project": bucket[1],
            "member_ids": [live[i].get("id") for i in members],
            "representative": rep.get("id"),
            "size": len(members),
        })
        for i in members:
            by_seed[id(live[i])] = gi
    return groups, by_seed


def seed_groups(seeds):
    """群 (層化設計書 B1)。純関数・何も書かない。戻り値 list[dict] (群の並び = 代表の _index_sort_key → 配列順)。

    各 dict: label / kind / project / member_ids (_index_sort_key の安定ソート) / representative (= member_ids[0]) / size。
    1 件だけのものは返さない。`is_live` でない種子は成員にならない。"""
    return _seed_groups_by_seed(seeds)[0]


_PATH_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-+@%~/")
_PATH_REJECT_CHARS = frozenset("*?[]{}$\\<>\ufffc")
_DOTS_ONLY = re.compile(r"\.+\Z")


def extract_local_paths(text):
    """所在の content からローカル絶対 path を抽出する (dormancy spec §4.1)。純粋関数・stat なし・`~` 未展開。

    書かれたとおりの文字列を、出現順・重複除去の tuple で返す。URL / `host:/path` / 相対 path /
    glob・変数・`<elided …>` と接する path / 途中が省略された path は対象外 (照合不能 = 休眠させない)。"""
    if not isinstance(text, str):
        return ()
    text = text.replace("\u2026", "...")
    n = len(text)
    out = []
    i = 0
    while i < n:
        ch = text[i]
        anchor = False
        if ch == "/":
            anchor = i + 1 < n and text[i + 1] in _PATH_CHARS and text[i + 1] != "/"
        elif ch == "~" and text.startswith("~/", i):
            anchor = i + 2 < n and text[i + 2] in _PATH_CHARS and text[i + 2] != "/"
        if anchor and i > 0:
            prev = text[i - 1]
            if prev in _PATH_CHARS or prev == ":" or prev in _PATH_REJECT_CHARS:
                anchor = False
        if not anchor:
            i += 1
            continue
        j = i
        while j < n and text[j] in _PATH_CHARS:
            j += 1
        path = text[i:j]
        i = j
        if j < n and text[j] in _PATH_REJECT_CHARS:
            continue
        comps = path.split("/")
        last = max((k for k, c in enumerate(comps) if c), default=None)
        if last is None:
            continue
        c = comps[last]
        if len(c) >= 3 and _DOTS_ONLY.match(c):
            path = path[: len("/".join(comps[:last]))]  # 原文の接頭辞に切る
        elif c.endswith(".") and not _DOTS_ONLY.match(c) and not c.endswith("..") and last == len(comps) - 1:
            path = path[:-1]  # 文末の句点 (path が `/` で終わるときは取らない)
        comps = path.split("/")
        if any(c and (_DOTS_ONLY.match(c) or c.endswith("..") or "..." in c) for c in comps):
            continue
        if len([c for c in comps if c]) < 2:
            continue
        if path not in out:
            out.append(path)
    return tuple(out)


def _absent_local_paths(seed):
    """休眠の判定 (dormancy spec §4.2)。所在の content に書かれたローカル絶対 path が**全部**この機械に無いとき
    照合した path の組 (書かれたとおり) を返し、それ以外は `()`。純粋 (種子に何も書かない・キャッシュなし)。
    「無い」は stat の FileNotFoundError / NotADirectoryError だけ。他の OSError・ValueError は「ある」(判定不能)。"""
    if _seed_kind(seed) != "所在":
        return ()
    content = seed.get("content")
    if not isinstance(content, str):
        return ()
    paths = extract_local_paths(content)
    if not paths:
        return ()
    for path in paths:
        target = path
        if path.startswith("~/"):
            try:
                target = os.path.expanduser(path)
            except Exception:
                return ()
            if target.startswith("~") or not os.path.isabs(target):
                return ()
        try:
            os.stat(target)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError):
            return ()
        return ()
    return tuple(paths)


def _span_text(item):
    """span 項目 (str か `evaluate_proposal` の span dict) の本文。それ以外は None。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and isinstance(item.get("span"), str):
        return item["span"]
    return None


def _has_avoid_final(remainder):
    """W1 案 B: remainder (疑の文を除いた残り) を文に切り、問いでない文のどれかの文末 (末尾の 。.!！」 空白 を外して casefold) が
    `WORKAROUND_AVOID_MARKERS` で終わるか (`_has_decision_assert_final` と同じ方式。「回避できますよね」「…ますけど」「…ますように」を通さない)。"""
    n, pieces = _sentence_pieces(remainder)
    for lo, hi, question in pieces:
        if question:
            continue
        sentence = n[lo:hi].rstrip("。.!！」 ").casefold()
        if any(sentence.endswith(m.casefold()) for m in WORKAROUND_AVOID_MARKERS):
            return True
    return False


def _self_stated_workaround(grounded_spans, window=None):
    """W1 (#134・spec 2026-11-04-self-stated-workaround-design §5.2/§7): 利用者が自分で述べた「原因 → 回避」の形。純関数。
    span が 1 本以上あり全 span が dict で 圓成實 (str・依他起 が 1 本でもあれば偽 = §9a 裁定 B)。そのうえ 1 本の span の
    疑の文を除いた残り (`_span_remainders_fn(window)` の 1 片 = 同じ往復の同じ span の残り全体) に 原因の印 (`SELF_STATED_CAUSE_MARKERS` = 「原因」だけ) と、問いでない文の文末一致の回避の印 (`_has_avoid_final`) の両方があり、
    好みの印が無いとき真。印は 圓成實 の span (`sansho._n` 済み) にだけ当てる。content・source・kind の申告は読まない。"""
    spans = list(grounded_spans or ())
    if not spans or not all(isinstance(sp, dict) and sp.get("sansho") == "圓成實" for sp in spans):
        return False
    remainders_of = _span_remainders_fn(window)
    for sp in spans:
        for r in remainders_of(sp):
            if (_has_marker(r, SELF_STATED_CAUSE_MARKERS) and _has_avoid_final(r)
                    and not _has_marker(r, PREFERENCE_SUFFIXES)):
                return True
    return False


def _workaround_form_note(span_results, window=None):
    """回避策 の形 (入口 = ユーザーの逐語の訂正・対 = 行為 X → 結果 Y)。純関数。
    spec 2026-10-29-workaround-entry-and-pair-design §1・§2。満たせば None、満たさなければ 性向 への注記。
    印は 圓成實 と判定された span (`sansho._n` 済み) にだけ当てる。content・evidence の source 欄・kind の
    自己申告は読まない。span の判定 (`sansho` 欄) が無い項目 (str) は 圓成實 と見なさない。
    #134 W1: 入口は「訂正の入口 (対つき) **または** 自述の形 (`_self_stated_workaround`・`window` = ゲートの窓)」。自述の形を満たせば None。"""
    if _self_stated_workaround(span_results, window):
        return None
    items = []   # (正規化した span, 判定, turn_index) 重複は畳む
    seen = set()
    for item in span_results:
        text = _span_text(item)
        if text is None or not isinstance(item, dict):
            continue
        norm = sansho._n(text)
        key = (norm, item.get("sansho"), item.get("turn_index"))
        if key in seen:
            continue
        seen.add(key)
        items.append(key)
    corrections = [
        it for it in items if it[1] == "圓成實" and _has_marker(it[0], USER_CORRECTION_MARKERS)
    ]
    if not corrections:
        return "回避策 にユーザーの訂正の印が無いため 性向 として受理"
    for y_text, _y_cls, y_idx in corrections:
        for x_text, x_cls, x_idx in items:
            if x_text == y_text or x_text in y_text or y_text in x_text:
                continue
            if not isinstance(x_idx, int) or not isinstance(y_idx, int):
                continue
            if x_cls == "圓成實" and x_idx <= y_idx:
                return None
            if x_cls == "依他起" and x_idx < y_idx:
                return None
    return "回避策 が 行為 → 結果 の対 (別の span 2 本) を持たないため 性向 として受理"


# 疑の文 (spec 2026-10-30-decision-without-rejection §7「疑の文の除外」): user の本文を文に分ける規則。
# NFKC の後 (！→!・．→.・｡→。) の文字で書く (全角の ？ だけは NFKC の前に別の字へ置き換える)。語表ではなく句読点の形だけ。
_SENTENCE_LINE_BREAKS = "\n\r\v\f\x85\u2028\u2029"
_FULLWIDTH_QUESTION = "\ufdd0"   # 全角の ？ (U+FF1F) の NFKC 前の置き場 (非文字。NFKC は動かさない)
_SENTENCE_END_RUN = "。.!?" + _FULLWIDTH_QUESTION   # 終端の連なり (「?!」「…?」) に含める字
_SENTENCE_CLOSERS = "」』）)]】〕〉》}\"'\u201d\u2019\u203a\u00bb"   # 終端の後ろの閉じ括弧・引用符 (「…か？」」)
_MID_TOKEN_FOLLOWERS = "=&_-/"   # 半角「?」の左が空白でない ASCII で、直後がこれ (または ASCII 英数字、または英数字が続く「.」) なら語の途中の「?」


_FULLWIDTH_TABLE = {0xFF1F: _FULLWIDTH_QUESTION}


def _is_ascii_alnum(c):
    return c.isascii() and c.isalnum()


def _is_mid_token_question(s, i):
    """s[i] (半角の「?」) が語の途中か: 左が空白でない ASCII で、かつ直後が ASCII 英数字か `=&_-/`、または英数字が
    続く「.」 (`https://x.io/?b=1`・`foo()?.bar`)。左が CJK・空白なら語の途中でない (`どう?OK`)。
    `?.` `?...` の「.」は英数字が続かないので語の途中でない。"""
    if i == 0 or not "!" <= s[i - 1] <= "~" or i + 1 >= len(s):
        return False
    nxt = s[i + 1]
    if _is_ascii_alnum(nxt) or nxt in _MID_TOKEN_FOLLOWERS:
        return True
    return nxt == "." and i + 2 < len(s) and _is_ascii_alnum(s[i + 2])


def _sentence_pieces(text):
    """text を文に分ける。戻り値 `(n, pieces)`: `n = sansho._n(text)` (ゲートが照合する正規化済みの本文)、
    `pieces` = `[(start, end, is_question)]` (`n` の添字。文と文の間の空白は含まない)。純関数・線形 1 回走査。

    `sansho._n` は空白 (改行を含む) を 1 個に畳み CJK 隣接の空白を消すので、改行の情報は `_n` の後には無い。
    よって切るのは `_n` の前: NFKC のあと (改行はまだ在る) 1 回の走査で文末を決め、各文を `_n` にかけて
    `n` の添字に直す。文末 = 「。」「!」「?」と、後ろが空白か本文の終わりの「.」 (`config.yaml`・`v1.2` は
    切らない)、改行。「?」は元が全角でも半角でも文末だが、**元が半角** の「?」は左が空白でない ASCII で、かつ直後が
    ASCII 英数字・`=&_-/`・英数字が続く「.」のとき語の途中 (`https://x.io/a?b=1`・`foo?.bar`) で文末でない。
    全角の「？」は NFKC の前に別の字へ置き換えて持ち越し、常に文末とする (`どう？OK`・`する？Redis は…`・`？…`)。
    左が CJK の半角「?」 (`どう?OK`) も文末。終端の直後に続く終端・閉じ括弧・引用符は同じ文に入れる
    (「…か？」」・`か？！`・`!?`・`?...`)。is_question = 文末の記号列に「?」(全角含む) を含む。"""
    n = sansho._n(text)
    s = unicodedata.normalize("NFKC", (text or "").translate(_FULLWIDTH_TABLE))
    size = len(s)
    bounds = []   # (core_start, core_end, is_question) — core は前後の空白を含まない
    start = 0     # 現在の文の始まり (空白を含みうる)
    i = 0

    def close(end, question):
        core_start = start
        while core_start < end and s[core_start].isspace():
            core_start += 1
        core_end = end
        while core_end > core_start and s[core_end - 1].isspace():
            core_end -= 1
        if core_start < core_end:
            bounds.append((core_start, core_end, question))

    while i < size:
        c = s[i]
        if c in _SENTENCE_LINE_BREAKS:
            close(i, False)
            start = i + 1
            i += 1
        elif (
            c in "。!" or c == _FULLWIDTH_QUESTION
            or (c == "." and (i + 1 == size or s[i + 1].isspace()))
            or (c == "?" and not _is_mid_token_question(s, i))
        ):
            question = c in ("?", _FULLWIDTH_QUESTION)
            j = i + 1
            while j < size and (s[j] in _SENTENCE_END_RUN or s[j] in _SENTENCE_CLOSERS):
                question = question or s[j] in ("?", _FULLWIDTH_QUESTION)
                j += 1
            close(j, question)
            start = j
            i = j
        else:
            i += 1
    close(size, False)

    # 文ごとに `_n` をかけて `n` の添字へ。文の間の空白は、前後どちらかが CJK なら `_n` が消す。
    pieces = []
    pos = 0
    prev_end = 0
    prev_char = "a"   # 先頭・末尾の空白は前後が非 CJK として扱う (`_n` と同じ)
    for core_start, core_end, question in bounds + [(size, size, False)]:
        gap = s[prev_end:core_start]
        nxt = s[core_start] if core_start < size else "a"
        if gap and sansho._CJK_ADJACENT_WS_RE.sub("", prev_char + " " + nxt) != prev_char + nxt:
            pos += 1   # 空白が 1 個残る
        if core_start == core_end:
            break
        length = len(sansho._n(s[core_start:core_end].replace(_FULLWIDTH_QUESTION, "?")))
        pieces.append((pos, pos + length, question))
        pos += length
        prev_end = core_end
        prev_char = s[core_end - 1]
    return n, pieces


def _remainder_from_offset(n_span, n, pieces, offset):
    """`n` の `offset` から始まる span (`n_span`) から 疑の文を除いた残り。疑の文に当たらなければ `n_span`
    そのまま。残りが無ければ ""。部分は "\n" でつなぐ (縁が除いた文を跨いで一致しない)。"""
    end = offset + len(n_span)
    kept = []
    dropped = False
    for lo, hi, question in pieces:
        if hi <= offset:
            continue
        if lo >= end:
            break
        if question:
            dropped = True
        else:
            kept.append(n[max(lo, offset):min(hi, end)])
    return "\n".join(kept) if dropped else n_span


def _decision_form_in_grounded_spans(grounded_spans, conditions, window=None):
    """決定 の形 (spec 2026-10-30-decision-without-rejection §7 U と「疑の文の除外」)。純関数。圓成實 と判定された
    span だけを見る (判定の無い str・圓成實 でない dict は数えない)。(i) その span の 1 本以上に却下側の印がある
    (span 全体に当てる。疑の文でも変わらない)、または (ii) 1 本以上が 提案の縁 `conditions` の 1 語以上を含み、
    かつ同じ span に好みの印が無い (断定の形)。(ii) は span から **疑の文** (窓の user の本文を文に分けたとき、
    文末の記号列に ？/? を含む文) を除いた残りに当てる: 縁も好みの印も残りだけを見る。残りが空なら (ii) は
    成り立たない。語の途中の **半角** 「?」(`https://x.io/a?b=1`・`foo?.bar`) は文末でなく、全角の「？」は常に文末。
    `window` = ゲートが判定した窓 (`sansho.as_window` 済み。本文は `user_text` だけを読む = assistant の本文に現れ
    ても数えない)。読む往復は span が接地した往復 (span dict の `turn_index`) と **同じ project** のもの
    (`_window_project` と同じ比べ方: str でなければ None。別 project の発話では救わない = 同依)。`turn_index` が
    使えない (無い・bool・範囲外) ときは窓の全往復。その往復の中で span 全体が疑の文の外に出ていれば、span 全体で
    判定する (問いの後で確定した「SQLite で行く？」→「やはり SQLite で行く」を落とさない)。無ければ往復ごとに
    最初の出現で残りを取り、どれかで (ii) が立てばよい。往復ごとに本文を 1 回だけ切る (線形)。`window` が窓でない・
    読める往復のどの user の本文にも span が見つからないときは、span 自身 (改行を残した生の span) を文に分ける。
    照合は `sansho._n` で正規化した span に当て、縁は `sansho._n` + casefold (`form_in_user_spans_for_kind` と同じ)。"""
    conditions = conditions if isinstance(conditions, (list, tuple)) else []
    conds = [sansho._n(c).casefold() for c in conditions if isinstance(c, str)]
    conds = [c for c in conds if c.strip()]   # 空白だけの縁はどの span にも一致させない

    def holds(remainder):
        folded = remainder.casefold()
        return any(c in folded for c in conds) and not _has_marker(remainder, PREFERENCE_SUFFIXES)

    remainders_of = _span_remainders_fn(window)
    for sp in grounded_spans:
        if not (isinstance(sp, dict) and sp.get("sansho") == "圓成實"):
            continue
        if _has_marker(sansho._n(_span_text(sp) or ""), DECISION_REJECT_MARKERS):
            return True
        if any(holds(r) for r in remainders_of(sp)):
            return True
    return False


def _span_remainders_fn(window=None):
    """圓成實 の span 1 本の「疑の文を除いた残り」を返す関数を作る (`_decision_form_in_grounded_spans` から切り出し、
    #117 の 決定_assert と #115 の 再開点 の形が同じ切り方を共有する)。戻り値 `remainders_of(sp)` = 残り (正規化済み・
    casefold 前) の list。規則は `_decision_form_in_grounded_spans` の docstring のとおり: span が接地した往復と同じ project の
    user の本文で span 全体が疑の文の外に出ていれば `[n_span]`、無ければ往復ごとに最初の出現の残り、どの本文にも見つからなければ
    span 自身 (生の span) を文に分けた残り 1 本。往復ごとに本文を 1 回だけ切る (関数ごとのキャッシュ)。純関数。"""
    turns = window if isinstance(window, (list, tuple)) else ()

    def project_of(index):
        project = getattr(turns[index], "project", None)
        return project if isinstance(project, str) else None

    parsed = {}   # 往復の添字 -> (n, pieces, kept)。user の本文が str でなければ None

    def parse(index):
        if index not in parsed:
            text = getattr(turns[index], "user_text", None)
            if isinstance(text, str):
                n, pieces = _sentence_pieces(text)
                parsed[index] = (n, pieces, "\n".join(n[lo:hi] for lo, hi, q in pieces if not q))
            else:
                parsed[index] = None
        return parsed[index]

    def remainders_of(sp):
        raw = _span_text(sp) or ""
        n_span = sansho._n(raw)
        indexes = range(len(turns))
        at = sp.get("turn_index") if isinstance(sp, dict) else None
        if isinstance(at, int) and not isinstance(at, bool) and 0 <= at < len(turns):
            same = project_of(at)
            indexes = [i for i in indexes if project_of(i) == same]
        found = False
        remainders = []
        if n_span.strip():
            for index in indexes:
                entry = parse(index)
                if entry is None:
                    continue
                n, pieces, kept = entry
                offset = n.find(n_span)
                if offset < 0:
                    continue
                found = True
                if n_span in kept:
                    return [n_span]
                remainders.append(_remainder_from_offset(n_span, n, pieces, offset))
        if found:
            return remainders
        n, pieces = _sentence_pieces(raw)
        return [_remainder_from_offset(n, n, pieces, 0)]

    return remainders_of


def _resume_form_in_user_spans(grounded_spans, conditions, window=None):
    """#115 (RP2b): 圓成實 の 再開点 が「問いでない文と縁」を user の逐語に持つか。純関数。圓成實 と判定された span の
    どれか 1 本について、疑の文を除いた残り (`_span_remainders_fn`・窓つき) が空でなく、提案の縁 `conditions` の 1 語以上を含む
    (`sansho._n` + casefold・空白だけの縁は数えない)。好みの印は除かない (「次は X したい」は正当な 再開点)。"""
    conds = [sansho._n(c).casefold() for c in (conditions if isinstance(conditions, (list, tuple)) else []) if isinstance(c, str)]
    conds = [c for c in conds if c.strip()]
    remainders_of = _span_remainders_fn(window)
    for sp in grounded_spans or ():
        if not (isinstance(sp, dict) and sp.get("sansho") == "圓成實"):
            continue
        for r in remainders_of(sp):
            folded = r.casefold()
            if folded.strip() and any(c in folded for c in conds):
                return True
    return False


_FIXED_PHRASE_BEFORE_NISHITA = tuple("気目耳口手")


def _has_decision_assert_final(remainder):
    """#117 (KP2b): remainder (疑の文を除いた残り) を文に切り、どれかの文末 (末尾の 。.!！」 と空白を外して casefold) が
    `decision_assert` の印で終わるか。印は文末一致 (「にした」⊂「にしたほうがいい」「にしたら」を通さない)。問いの文は数えない。"""
    n, pieces = _sentence_pieces(remainder)
    for lo, hi, question in pieces:
        if question:
            continue
        sentence = n[lo:hi].rstrip("。.!！」 ").casefold()
        for m in DECISION_ASSERT_MARKERS:
            m = m.casefold()
            if not sentence.endswith(m):
                continue
            # 慣用句 (気にした・目にした・耳にした・口にした・手にした、〜にしました も同じ) は決定でない
            if m in ("にした", "にしました") and sentence[-len(m) - 1:-len(m)] in _FIXED_PHRASE_BEFORE_NISHITA:
                continue
            return True
    return False


def _untouched_user_sentence(seed, new_conds, touched_text):
    """部分交代の門 SP1'' (#122, spec 2026-11-02-partial-supersede-design §5/§7)。旧い種子 `seed` の記のうち、新しい言が
    触れていないものの最初の 1 文 (`sansho._n` 済みの原文の綴り) を返す。無ければ None。純関数・何も書かない。
    `touched_text` は str か、呼ぶと str を返す関数 (門が効きえないときは呼ばない = 窓を読まない)。
    読むのは `source == "user"` の span (`has_user_span` と同じ見方。assistant の span は読まない)。各 span を
    `_sentence_pieces` で文に切り (`window` なし = `doubt_judgement` と同じ)、疑の文は記でないので除く。残る文のうち旧の縁
    (`seed_conditions`) を 1 語以上含む文が「この種子の記」。記の単位は文でなく **事**: 記を担う各文が含む旧縁の集合が語を共有する文を
    連結した成分を 1 つの群にする (強化で重なった同文・部分集合の言い直しは同じ群)。
    VC (spec §10): 各文をさらに「、」「,」(数字に挟まれた `,` = 桁区切りは除く) で句に切り、核 (旧縁のうち 新の縁 `new_conds` にも `touched_text` にも無いもの = 値) を
    1 語も含まない句は同じ文の前の句 (先頭なら次の句) に戻す。記・成分・触れられたの判定は句を単位に同じ。値を 2 つ持つ 1 文だけが割れる。
    `touched_text` は成分 1 つかつ記の文に「、」「,」が無いとき (句に切る余地が無いとき) は呼ばない。
    既知の限界: 「と」で結ぶ 1 句 2 事 (a')、1 事を読点で分け新が題にも旧値にも触れない言い換えは止める (e) (assistant の echo は触れたに数えない、U §10.1)。呼び出し側は `grounded` を窓の user 本文で接地した縁に絞って `new_conds` に渡す (U)。群が 2 つ以上のときだけ門が効く (一括の種子。1 つの記の値交代は常に「遍」)。
    文が「触れられた」= 新の縁 `new_conds` を含むか、旧の縁のうち新しい言の本文 `touched_text` (新提案の圓成實 span の疑の文を
    除いた残り) にも現れる語を含む。**群の全文が触れられていない群** があれば全体は交代できない (餘如前)。
    旧に user の文が無ければ None (門は通す)。照合は `sansho._n` + casefold、空白だけの縁は数えない。"""
    old = []
    for c in seed_conditions(seed):
        folded = sansho._n(c).casefold()
        if folded.strip() and folded not in old:
            old.append(folded)
    def collect(core):
        """記を担う文 (原文の綴り, casefold, その文が含む旧縁の集合)。core が None なら文のまま、あれば VC (spec §10):
        文を「、」「,」で句に切り、核を 1 語も含まない句は同じ文の前の句 (先頭なら次の句) に戻す。"""
        found = []
        for evidence in seed.get("evidences") or []:
            if not has_user_span({"evidences": [evidence]}):
                continue
            n, pieces = _sentence_pieces(evidence["span"])
            for lo, hi, question in pieces:
                if question:
                    continue
                parts = [[lo, hi]]
                if core is not None:
                    parts, start = [], lo
                    for i in range(lo, hi):
                        if n[i] in "、," and not (n[i] == "," and i > lo and i + 1 < hi and n[i - 1].isdigit() and n[i + 1].isdigit()):
                            if i > start:
                                parts.append([start, i])
                            start = i + 1
                            while start < hi and n[start].isspace():
                                start += 1
                    if hi > start:
                        parts.append([start, hi])
                    merged = []
                    for part in parts:
                        if merged and not any(w in n[part[0]:part[1]].casefold() for w in core):
                            merged[-1][1] = part[1]
                        else:
                            merged.append(part)
                    if len(merged) > 1 and not any(w in n[merged[0][0]:merged[0][1]].casefold() for w in core):
                        merged[1][0] = merged[0][0]
                        merged.pop(0)
                    parts = merged
                for a, b in parts:
                    sent = n[a:b].casefold()
                    key = {w for w in old if w in sent}
                    if key:
                        found.append((n[a:b], sent, key))
        return found

    def components(found):
        # 事 = 縁を共有して連結した成分 (部分集合の言い直しは同じ成分。縁を 1 文で両方言うと 1 成分になる = 既知の限界)
        groups = []   # [縁の和集合, [(原文の綴り, casefold)]]
        for text, sent, key in found:
            joined = [g for g in groups if g[0] & key]
            merged = [set(key), [(text, sent)]]
            for g in joined:
                merged[0] |= g[0]
                merged[1] = g[1] + merged[1]
                groups.remove(g)
            groups.append(merged)
        return groups

    sentences = collect(None)
    if len(components(sentences)) < 2 and not any("、" in sent or "," in sent for _t, sent, _k in sentences):
        return None   # 事が 1 つで句に切る余地も無い: 改めるのは常に「遍」(全体の交代)。門は一括の記にだけ効く (窓を読まない)
    if callable(touched_text):
        touched_text = touched_text()
    touched_text = (touched_text or "").casefold()
    new = [f for f in (sansho._n(c).casefold() for c in new_conds if isinstance(c, str)) if f.strip()]
    old_in_new = [w for w in old if w in touched_text]
    # VC: 核 = 新の縁にも新しい言にも触れられていない旧縁 (値)。題・理由の句は値の句に付き、値を 2 つ持つ文だけが割れる
    core = [w for w in old if w not in new and w not in old_in_new]
    groups = components(collect(core))
    if len(groups) < 2:
        return None
    for _words, members in groups:
        if not any(any(w in sent for w in new) or any(w in sent for w in old_in_new) for _text, sent in members):
            return members[0][0]
    return None


def doubt_judgement(seed, shown=()):
    """決定 の種子の縁を、user の全 span の疑の文で分ける。戻り値 `(only, keep_summary)`。
    spec 2026-10-30-doubt-in-decision-summary §7 (#120)。読み時の派生・純関数で、何も書かない (不変条件 2・4)。
    `manas` の ①・③ が札「一部問い」を立てる唯一の根拠 (`seed_kind(seed)` が 決定 で user の span を持つ種子だけ)。
    読むのは種子の **全** user span (`has_user_span` と同じ見方の `source == "user"`。assistant の span は読まない)。
    各 span に `_sentence_pieces` を当て、疑の文 (文末の記号列に ？/? を含む文) と、それを除いた残りに分ける
    (`_remainder_from_offset(n, n, pieces, 0)`。`window` を使わない = `recall` は ledger を読まない。種子ごとに 1 回だけ回す)。
    照合は `sansho._n` + casefold (`_decision_form_in_grounded_spans` と同じ)。空白だけの縁は数えず、綴り違いだけの
    重複した縁は最初の 1 つだけ数える。
    - `only` (D) = 問いのみの縁: どれかの user span の **疑の文** に現れ (文を跨いだ一致は数えない)、かつ どの user span の
      残りにも現れない縁。`conditions` の順・seed の綴り。LLM が `?` を切った span では疑の文が無いので立たない (既知の限界)。
    - S = 問いのみでない縁: どれかの user span の残りに現れる縁 (どの span にも現れない縁は入れない)。
    - `keep_summary` = D があり、かつ S のうち **描く逐語 `shown`** (user の evidence の list。manas が描く最新の接地往復の
      span) の残りに現れない縁が 1 語以上ある。要約を外すと断定の部分が行から消えるときだけ True (§7 追補)。
      `shown` は同じ `seed["evidences"]` の項目 (同一の object) で渡す。"""
    if not isinstance(seed, dict) or seed_kind(seed) != "決定" or not has_user_span(seed):
        return [], False
    conditions = seed.get("conditions")
    if not isinstance(conditions, (list, tuple)):
        return [], False
    shown_ids = {id(e) for e in shown or ()}
    questions = []       # 各 user span の疑の文 (正規化済み・casefold。文は "\n" でつなぐ = 縁が文を跨いで一致しない)
    remainders = []      # 各 user span の疑の文を除いた残り (casefold)
    shown_remainders = []
    for evidence in seed.get("evidences") or []:
        if not has_user_span({"evidences": [evidence]}):
            continue
        n, pieces = _sentence_pieces(evidence["span"])
        questions.append("\n".join(n[lo:hi] for lo, hi, question in pieces if question).casefold())
        remainder = _remainder_from_offset(n, n, pieces, 0).casefold()
        remainders.append(remainder)
        if id(evidence) in shown_ids:
            shown_remainders.append(remainder)
    only = []
    outside_shown = False   # S の縁で、描く逐語の疑の文の外に無いものがあるか
    seen = set()
    for condition in conditions:
        if not isinstance(condition, str):
            continue
        folded = sansho._n(condition).casefold()
        if not folded.strip() or folded in seen:
            continue
        seen.add(folded)
        if any(folded in text for text in remainders):
            if not any(folded in text for text in shown_remainders):
                outside_shown = True
        elif any(folded in text for text in questions):
            only.append(condition)
    return only, bool(only) and outside_shown


def conjecture_only(seed, shown=()):
    """#121 A' (spec 2026-11-07-tentative-preference-design §7): 決定 の描く逐語 `shown` (user の evidence の列) が推量だけか。
    読み時の派生・純関数 (ledger を読まない・何も書かない)。各 span を `_sentence_pieces` で文に切り、疑の文を除く。
    (a) 文末 (末尾の 。.!！」 と空白を外して casefold) が推量の印で終わる文が 1 つ以上あり、
    (b) 縁 (`sansho._n` + casefold) を含み推量の印で終わらない文が 1 つも無いとき真。kind が 決定 以外は False。"""
    if not isinstance(seed, dict) or seed_kind(seed) != "決定":
        return False
    conditions = seed.get("conditions")
    folded = []
    if isinstance(conditions, (list, tuple)):
        for c in conditions:
            if isinstance(c, str):
                f = sansho._n(c).casefold()
                if f.strip():
                    folded.append(f)
    conjectured = False
    for evidence in shown or ():
        if not isinstance(evidence, dict) or not isinstance(evidence.get("span"), str):
            continue
        n, pieces = _sentence_pieces(evidence["span"])
        for lo, hi, question in pieces:
            if question:
                continue
            sentence = n[lo:hi].rstrip("。.!！」 ").casefold()
            if any(sentence.endswith(m.casefold()) for m in CONJECTURE_MARKERS):
                conjectured = True
            elif any(f in sentence for f in folded):
                return False
    return conjectured


def doubt_only_conditions(seed):
    """`doubt_judgement(seed)[0]`: 決定 の縁のうち問いの文にしか無いもの (conditions の順・seed の綴り、無ければ `[]`)。"""
    return doubt_judgement(seed)[0]


def _span_seats(span_result, window, seats):
    """ゲートが付けた class でこの span が接地するすべての往復の座の集合 (#145)。`classify_sansho` と同じ照合。"""
    return frozenset(
        seats[i]
        for i, t in enumerate(window)
        if sansho.classify_sansho(span_result["span"], t.user_text, t.assistant_text) == span_result["sansho"]
    )


def _main_seat(span_results, window, seats):
    """X2b の主の座: user (圓成實) の span が接地した最も後の往復の座、無ければ span が接地した最も後の往復の座。"""
    users = [r for r in span_results if r["sansho"] == "圓成實"]
    pool = users or span_results
    latest = max(
        i
        for r in pool
        for i, t in enumerate(window)
        if sansho.classify_sansho(r["span"], t.user_text, t.assistant_text) == r["sansho"]
    )
    return seats[latest]


def _resolve_kind(proposal, grounded_spans=(), conditions=(), window=None, allow_promotion=True):
    """形の検査 (spec §4.3)。戻り値 (kind, note): kind が None なら却下 (note が理由)。
    note は読み替えが起きたときの注記 (無ければ None)。`sansho.py` には触れない。
    決定 は 圓成實 と判定された span (`grounded_spans`・`sansho._n` 済みで照合) に (i) 却下側の印、または
    (ii) 断定の形 (提案の縁 `conditions` の 1 語以上を含み、好みの印を含まない span) を要求する。(ii) は span の
    **疑の文** (窓の user の本文を文に分けたとき文末の記号列に ？/? を含む文) を除いた残りで判定する (spec 同 §7「疑の文の
    除外」。(i) は span 全体)。content の却下側の印は要らない (旧「両側必須」§11.6 / twin-seed-index spec §3.2 / 2026-10-30 spec §7 H は
    2026-10-30-decision-without-rejection §7 U で改訂)。どちらも無ければ 性向。content の好みの印の検査は残る。
    既定 `grounded_spans=()` は「圓成實 の引用なし」= 決定 は 性向 に落ちる。判定の無い str 項目は 圓成實 と見なさない。
    `conditions` の既定 `()` は (ii) が成り立たない (= 却下側の印だけが頼り)。呼び出し側は
    `decision["grounded_conditions"]` を渡す。`window` = ゲートが判定した窓 (呼び出し側は評価に使った窓をそのまま
    渡す)。span が接地した往復と同じ project の user の本文のどれかで疑の文の外に出る span は (ii) を満たしうる (別 project の本文は読まない。詳細は `_decision_form_in_grounded_spans`)。既定 None は span 自身を文に分ける。
    `grounded_spans` の要素は str か `evaluate_proposal` の span dict (`span`/`sansho`/`turn_index`)。
    回避策 の形の検査 (`_workaround_form_note`) は dict の 判定 を要するので、str だけだと 性向 に落ちる。
    #117: 申告が 性向 (欄の字面。欄なし・落とした 決定/回避策 は対象外) で `allow_promotion` なら `_promote_disposition`
    (人の置き換え `replacement_for` / `plant_precheck` は False)。"""
    raw = proposal.get("kind") if isinstance(proposal, dict) else None
    if raw is None:
        return DEFAULT_KIND, None
    if raw not in VALID_KINDS:
        return None, "kind {!r} は VALID_KINDS に無いため却下".format(raw)
    content = proposal.get("content") or ""
    if raw == "決定" and not _decision_form_in_grounded_spans(grounded_spans, conditions, window):
        return DEFAULT_KIND, "決定 に 圓成實 の引用の却下側の印も断定の形も無いため 性向 として受理"
    if raw == "回避策" and not _has_marker(content, WORKAROUND_CAUSE_MARKERS):
        return DEFAULT_KIND, "回避策 に原因の印が無いため 性向 として受理"
    if raw == "回避策":
        form_note = _workaround_form_note(grounded_spans, window)
        if form_note:
            return DEFAULT_KIND, form_note
    if raw != DEFAULT_KIND and _has_preference_suffix(content):
        return DEFAULT_KIND, "{} の文末が好みの形のため 性向 として受理".format(raw)
    if raw == DEFAULT_KIND and allow_promotion:
        return _promote_disposition(content, grounded_spans, conditions, window)
    return raw, None


def _promote_disposition(content, grounded_spans, conditions, window):
    """#117 (KP2a+b + KP6): 申告が 性向 (kind 欄の字面) のときだけ。全 span が 圓成實 (str は数えない・空は不可) で、
    content の文末が好みの形でなく、(a) 圓成實 の span 全体に却下側の印、または (b) 1 本の残り (疑の文を除く) に縁があり好みの印が
    無い (= 決定 の存続条件 (ii)) うえ文末に `decision_assert` の印 → 決定 に昇格 (昇格 ⊆ 決定 の存続)。
    印が無く (ii) の縁の条件だけ成り立つときは kind を変えず注記だけ (KP6・頻度を数える)。"""
    spans = list(grounded_spans or ())
    if not spans or not all(isinstance(sp, dict) and sp.get("sansho") == "圓成實" for sp in spans):
        return DEFAULT_KIND, None
    if _has_preference_suffix(content):
        return DEFAULT_KIND, None
    if _self_stated_workaround(spans, window):   # W1 (#134): KP (決定 への上げ) より先。content の原因の印は見ない
        return "回避策", SELF_STATED_WORKAROUND_NOTE
    conds = [sansho._n(c).casefold() for c in (conditions if isinstance(conditions, (list, tuple)) else []) if isinstance(c, str)]
    conds = [c for c in conds if c.strip()]
    remainders_of = _span_remainders_fn(window)
    form = False
    for sp in spans:
        n_span = sansho._n(_span_text(sp) or "")
        if _has_marker(n_span, USER_CORRECTION_MARKERS):
            continue   # 訂正の印のある span は 決定 へ上げない (spec §5.2・(a)(b) とも)
        # (a) は 決定 の (i) と同じ span 全体の却下の印。ただし昇格は申告の上げなので、同じ span に好みの印があれば上げない
        # (e1 #5「採らない」+「使いたい」・spec §5.2。決定 の存続より狭い = 昇格 ⊆ 存続)。
        if _has_marker(n_span, DECISION_REJECT_MARKERS) and not _has_marker(n_span, PREFERENCE_SUFFIXES):
            return "決定", PROMOTION_NOTE
        for r in remainders_of(sp):
            folded = r.casefold()
            if any(c in folded for c in conds) and not _has_marker(r, PREFERENCE_SUFFIXES):
                form = True
                if _has_decision_assert_final(r):
                    return "決定", PROMOTION_NOTE
    if form:
        return DEFAULT_KIND, "性向 の申告に 決定 の形あり (読み替えない)"
    return DEFAULT_KIND, None


def _window_project(window, turn_indexes):
    """接地した往復の project 集合から種子の project を決める (spec §4.2)。
    戻り値 ("ok", project) か ("reject", reason)。"""
    projects = {getattr(window[i], "project", None) for i in turn_indexes}
    projects = {p if isinstance(p, str) else None for p in projects}
    if len(projects) > 1:
        return "reject", "project 跨ぎ ({} 種類) のため却下".format(len(projects))
    return "ok", next(iter(projects)) if projects else None


def _kind_rank(kind):
    try:
        return VALID_KINDS.index(kind)
    except ValueError:
        return len(VALID_KINDS)


def _index_sort_key(seed):
    """索引の並び = kind → クラス → 強度降順 (spec §6.3)。"""
    return (_kind_rank(_seed_kind(seed)),) + sansho.sansho_sort_key(seed)


_RUN_FIND = re.compile(r"[0-9a-z_\-./]+|[\u30a1-\u30fa\u30fc-\u30ff\u3005\u3400-\u4dbf\u4e00-\u9fff]+")


def _run_groups(text):
    """`sansho._n(..).lower()` した本文から、ASCII の組 `[0-9a-z_./-]+` と非 ASCII の組 (カタカナ (中黒 ・ U+30FB と ゠ U+30A0 は区切り)・々・CJK 拡張 A・漢字)
    の最長一致を別々に取り出した list (字の種類の境でも切る。両端の strip 前)。"""
    return _RUN_FIND.findall(sansho._n(text).lower())


def _input_runs(text):
    """入力の「連なり」の集合 (純粋)。`_run_groups` の各組の両端の `-./_` を落とし、空は捨て、ASCII の組は 3 字以上かつ
    数字だけでないもの、非 ASCII の組は 2 字以上のものだけ残す。str でなければ空集合 (index-reach spec §7、#106)。"""
    runs = set()
    if not isinstance(text, str):
        return runs
    for group in _run_groups(text):
        if ord(group[0]) < 128:
            group = group.strip("-./_")
            if len(group) >= 3 and not group.isdigit():
                runs.add(group)
        elif len(group) >= 2:
            runs.add(group)
    return runs


def _is_user_span_evidence(evidence):
    """user 接地の span (`source == "user"` で span が空でない str の dict)。`has_user_span` と共有する述語。"""
    return (
        isinstance(evidence, dict) and evidence.get("source") == "user"
        and isinstance(evidence.get("span"), str) and bool(evidence.get("span"))
    )


def _index_relevance(seed, runs, lengths=None):
    """索引の「今の入力との関わり」(純粋・大きいほど関わる)。`runs` (= `_input_runs`) のうち、種子の user の span
    (`_is_user_span_evidence`、`sansho._n(..).lower()`) のどれかに部分文字列として在るものの字数の和 (同じ連なりは 1 回)。
    assistant の span・content・縁は読まない。`runs` が空なら 0。計算量は入力の長さに依らない: span を入力と同じ切り方で
    組に分け、各組の部分文字列 (長さは `runs` に在る長さだけ) を `runs` の集合で引く (`lengths` = 長さの昇順、省略時は計算)。"""
    if not runs:
        return 0
    evidences = seed.get("evidences")
    if not isinstance(evidences, (list, tuple)):
        return 0
    if lengths is None:
        lengths = sorted({len(r) for r in runs})
    hit = set()
    for e in evidences:
        if not _is_user_span_evidence(e):
            continue
        for piece in _run_groups(e["span"]):
            n = len(piece)
            sizes = [size for size in lengths if size <= n]
            pending = len(runs) - len(hit)   # hit ⊆ runs。数だけ比べる (片ごとに runs を舐めると入力の長さに比例する)
            if not pending or not sizes:
                continue
            if pending <= sum(n - size + 1 for size in sizes):
                hit.update([r for r in runs if r not in hit and r in piece])   # 連なりの数のほうが少ない: run in piece
                continue
            for size in sizes:
                for i in range(n - size + 1):
                    sub = piece[i:i + size]
                    if sub in runs:
                        hit.add(sub)
    return sum(len(r) for r in hit)


def _is_int(value):
    # ledger._is_int と同じ意味 (int かつ bool でない)。alaya は ledger を import しない。
    return isinstance(value, int) and not isinstance(value, bool)


_ASCII_WORD_CHARS = frozenset("0123456789abcdefghijklmnopqrstuvwxyz")
SHORT_CONDITION_MAX_CHARS = 2  # この字数以下の縁は語末の境も要する (工学の定数。設計書 2026-11-25-recall-capacity §4.1 案 8d)


@functools.lru_cache(maxsize=4096)
def _word_pattern(key):
    left = "(?<![0-9a-z])" if key[0] in _ASCII_WORD_CHARS else ""
    right = "(?![0-9a-z])" if len(key) <= SHORT_CONDITION_MAX_CHARS and key[-1] in _ASCII_WORD_CHARS else ""
    return re.compile(left + re.escape(key) + right)


def _occurs_as_word(key, text_n):
    """縁 `key` (`sansho._n(..).lower()` 済み) が本文 `text_n` に「現前」するか (純粋・案 8d)。部分一致のうえ、
    key が ASCII 英数字で始まるなら当たる位置の直前が ASCII 英数字でないこと (語頭の境)、2 字以下で ASCII
    英数字で終わるなら直後も ASCII 英数字でないこと (語末の境) を要する。記号・非 ASCII の字・本文の端は境。
    3 字以上の前方一致 (git → github) は当たる (既知の限界)。言語は見ない (字の種類だけ)。"""
    if not key or key not in text_n:
        return False
    return _word_pattern(key).search(text_n) is not None


def _matched_conditions(seed, texts_n, memo=None):
    """発芽の縁の照合 (純粋)。`texts_n` は `sansho._n(..).lower()` 済みの本文の列。一致した縁の語を
    重複も数えて返す (一致数の数え方は改修5 から不変)。照合は `_occurs_as_word` (語の途中は当たらない)。
    `memo` (dict) を渡すと、同じ `texts_n` に対する縁ごとの結果を覚える (1 回の照合のパスの中だけ・
    呼び側が作って捨てる。本文は保持しない)。種子が縁を共有するとき、本文の走査は異なる縁ごとに 1 回で済む。"""
    out = []
    for cond in seed_conditions(seed):
        if not cond:
            continue
        if memo is None:
            hit = any(_occurs_as_word(sansho._n(cond).lower(), t) for t in texts_n)
        else:
            hit = memo.get(cond)
            if hit is None:
                hit = memo[cond] = any(_occurs_as_word(sansho._n(cond).lower(), t) for t in texts_n)
        if hit:
            out.append(cond)
    return out


def _needed_conditions(kind):
    """発芽に要る縁の一致語数 (性向 2 語・それ以外 1 語)。"""
    return DISPOSITION_GERMINATION_CONDITIONS if kind == DEFAULT_KIND else 1


class AlayaStore:
    """第八識の種子倉庫 (in-memory の論理)。ファイルも DB も知らない。

    永続化 (sqlite の読み書き・差分 flush) は `storage.py` の backend が持つ: 区間の入口で `state` に読み込み、
    COMMIT 時に差分だけ書く。動詞途中では保存しない。追記単位 (`append_imprint_log` / `_append_quarantine`)
    は明示のバッファに積み、保存時に直列化する。imprint_log の既存の行は区間の間だけ backend が注入する
    reader 経由で遅延して読む (recall は読まない)。standalone の呼び手は `commit()`。"""

    _default_state = staticmethod(_default_state)
    _default_imprint_log_state = staticmethod(_default_imprint_log_state)

    def __init__(self):
        self.warning = None
        # imprint_log の読めない行の警告 (読んだ時点で確定する。backend の reader が立てる)。
        self.imprint_log_warning = None
        self.state = self._default_state()
        # 追記バッファ (保存時に直列化する。保存済みの行はここに無い)。
        self._imprint_log_pending = []
        self._quarantine_pending = []
        # backend が bind する (storage.py)。
        self._backend = None
        self._imprint_log_reader = None   # callable(n=None, seed_id=None) -> list[dict]。区間の間だけ有効
        self._snap = None

    # --- 永続化 (実体は storage.py) ---
    @staticmethod
    def _validate(data):
        if not isinstance(data, dict):
            raise ValueError("alaya state はオブジェクトである必要があります")
        if "seeds" not in data or not isinstance(data["seeds"], list):
            raise ValueError("alaya state に seeds リストがありません")
        if "next_id" not in data or "life" not in data:
            raise ValueError("alaya state に next_id / life がありません")
        # bool is a subclass of int; reject it so True/False cannot pass as life.
        if not isinstance(data["life"], int) or isinstance(data["life"], bool):
            raise ValueError("alaya state の life は整数である必要があります")
        if not isinstance(data["next_id"], int) or isinstance(data["next_id"], bool):
            raise ValueError("alaya state の next_id は整数である必要があります")
        for seed in data["seeds"]:
            AlayaStore._validate_seed(seed)

    @staticmethod
    def _validate_seed(seed):
        """1 件の種子の形 (行単位の述語。DB の行はこれに落ちたら読み飛ばして数える)。"""
        if not isinstance(seed, dict):
            raise ValueError("alaya state の seeds の要素はオブジェクトである必要があります")
        if "strength" not in seed or not isinstance(seed["strength"], (int, float)) or isinstance(seed["strength"], bool):
            raise ValueError("alaya state の seed.strength は数値である必要があります")

    @staticmethod
    def _backfill_seed(seed):
        """旧形式の種子に新欄を補う (後方互換。読む側の補完で、DB の行は書き換えない)。"""
        seed.setdefault("history", [])
        seed.setdefault("miss_streak", 0)
        seed.setdefault("evidences", [])
        return seed

    @staticmethod
    def _backfill(data):
        """旧形式 state に新欄を補う (後方互換)。種子ごとの history/miss_streak/evidences も補う。"""
        data.setdefault("quarantine", [])
        data.setdefault("next_quarantine_id", 1)
        data.pop("imprint_log", None)  # 別ファイル運用: 同居していた旧データは持ち込まない
        for seed in data["seeds"]:
            AlayaStore._backfill_seed(seed)
        return data

    # --- 永続化 (imprint_log) ---
    @staticmethod
    def _validate_imprint_log(data):
        if not isinstance(data, dict) or not isinstance(data.get("imprint_log"), list):
            raise ValueError("imprint_log state に imprint_log リストがありません")

    def reload(self):
        """DB から状態を読み直す (standalone の呼び手用。engine は区間の入口で backend が読み込む)。"""
        if self._backend is not None:
            self._backend.reload_store(self)

    def commit(self):
        """今の状態と追記バッファを DB へ書く (standalone の呼び手用)。短い自己完結の書く区間を 1 つ開いて
        COMMIT する (engine の動詞の中では呼ばない: 区間の flush が唯一の書き込み点)。"""
        if self._backend is None:
            raise RuntimeError("AlayaStore は DB に結ばれていない (storage.open_alaya を使う)")
        self._backend.commit_store(self)

    # --- 生 (life) 管理 ---
    @property
    def current_life(self):
        return self.state["life"]

    def begin_new_life(self):
        """直前の生の発芽状況から miss_streak を更新し (観測用)、生番号を進める。減衰・削除はしない

        戻り値: 新しい生番号 (int)
        """
        prev_life = self.state["life"]
        if prev_life > 0:
            for seed in self.state["seeds"]:
                # 直前の生に植えられたばかりの種子は判定対象外 (発芽の機会がまだ無かった)
                if seed.get("planted_at", 0) >= prev_life:
                    continue
                if seed.get("last_fired") == prev_life:
                    seed["miss_streak"] = 0
                else:
                    seed["miss_streak"] = seed.get("miss_streak", 0) + 1
        self.state["life"] = prev_life + 1
        return self.state["life"]

    # --- 発芽 (種子生現行) ---
    def _threshold(self, seed):
        # §8: 依他起 でも 決定・回避策 の形を user の span が担うなら 1 (下げるだけ。class・sansho は不変)。
        if (
            seed.get("sansho") == "依他起"
            and seed_kind(seed) in ("決定", "回避策")
            and form_in_user_evidence(seed_kind(seed), seed.get("evidences"))
        ):
            return GERMINATION_THRESHOLD_BY_CLASS["圓成實"]
        return GERMINATION_THRESHOLD_BY_CLASS.get(seed.get("sansho"), GERMINATION_THRESHOLD_UNCLASSIFIED)

    def _germination_verdicts(self, input_text, context_texts=(), project=None, include_dispositions=True, shadows=None):
        """発芽の判定 (`germinate` と観測 `engine.explain_recall` の唯一の判定箇所・inspect spec §3.1)。

        戻り値: 全種子の `(seed, fired, reason, matched_conditions)` のリスト。**状態は変えない**
        (seed は内部 state の dict そのもの。呼び出し側が書き換えないこと)。並び = 発芽する種子
        (本番の発芽順 = クラス→strength 降順) → 上限で落ちた候補 (同じ順) → それ以外 (倉庫の順)。
        `reason` は発芽したとき None、落ちたときは最初に引っかかった理由 (GERMINATION_REASON_*)。
        判定の順は superseded → 取り下げ → project → 強度 → 再開点 → 性向 → 双子 → 休眠 → 縁 → 上限。どれも独立の条件なので
        順序で発芽の結果は変わらない (理由の表示だけが決まる)。`shadows` は呼び手が計算済みの `_twin_shadows` を渡すとき
        だけ指定する (`_circle_verdicts` が 1 recall で 1 回だけ計算するため。省略時は自分で計算する)。`matched_conditions` は縁の判定まで
        進んだ種子で一致した縁の語 (重複も数える = 一致数の数え方は改修5 から不変)。
        """
        if isinstance(context_texts, str):
            raise TypeError(
                "germinate() の context_texts は文字列の列 (list/tuple) を取ります: str は渡せません"
            )
        texts_n = [sansho._n(t or "").lower() for t in [input_text] + list(context_texts or [])]
        match_memo = {}
        candidates = []
        rejected = []
        if shadows is None:
            shadows = _twin_shadows(self.state["seeds"])
        for seed in self.state["seeds"]:
            if _is_superseded(seed):
                rejected.append((seed, False, GERMINATION_REASON_SUPERSEDED, []))
                continue
            if is_withdrawn(seed):
                rejected.append((seed, False, GERMINATION_REASON_WITHDRAWN, []))
                continue
            if not _project_matches(seed, project):
                rejected.append((seed, False, GERMINATION_REASON_PROJECT, []))
                continue
            if seed["strength"] < self._threshold(seed):
                rejected.append((seed, False, GERMINATION_REASON_STRENGTH, []))
                continue
            kind = _seed_kind(seed)
            if kind == "再開点":
                rejected.append((seed, False, GERMINATION_REASON_RESUME, []))
                continue
            if kind == DEFAULT_KIND and not include_dispositions:
                rejected.append((seed, False, GERMINATION_REASON_DISPOSITION, []))
                continue
            if id(seed) in shadows:
                rejected.append((seed, False, GERMINATION_REASON_TWIN.format(shadows[id(seed)]), []))
                continue
            absent = _absent_local_paths(seed)
            if absent:
                rejected.append((seed, False, GERMINATION_REASON_DORMANT.format(", ".join(absent)), []))
                continue
            needed = _needed_conditions(kind)
            matched = _matched_conditions(seed, texts_n, match_memo)
            if len(matched) >= needed:
                candidates.append((seed, matched))
            else:
                rejected.append(
                    (seed, False, GERMINATION_REASON_CONDITIONS.format(len(matched), needed), matched)
                )
        candidates.sort(key=lambda pair: sansho.sansho_sort_key(pair[0]))
        verdicts = [(seed, True, None, matched) for seed, matched in candidates[:GERMINATION_MAX]]
        verdicts += [(seed, False, GERMINATION_REASON_MAX, matched) for seed, matched in candidates[GERMINATION_MAX:]]
        return verdicts + rejected

    def germinate(self, input_text, context_texts=(), project=None, include_dispositions=True):
        """入力 (と文脈窓) に縁が触れ、クラス別閾値以上の種子を発芽させる (改修4 §5・改修5 §6.2)。

        縁は input_text と context_texts の各要素を**それぞれ**正規化した文字列に照合する
        (連結しない: 連結するとテキスト境界を跨いだ偽の一致が起こる)。閾値は
        GERMINATION_THRESHOLD_BY_CLASS (圓成實 1 / 依他起 2)。発芽順=クラス→strength
        降順、最大 GERMINATION_MAX 件。遍計所執は quarantine にのみ存在し seeds に無い。

        改修5 (kind 別の発芽規則):
          - 第一条件は `project` の一致 (全域 None の種子は常に可)。
          - 決定 / 回避策 / 所在 は縁 1 語の一致。
          - 性向 は縁 DISPOSITION_GERMINATION_CONDITIONS (2) 語の一致。`include_dispositions=False`
            なら発芽させない (engine.recall が RAIYA_INJECT_DISPOSITIONS を読んで決める)。
          - 再開点 はここでは発芽させない (`resume_point` がセッション先頭で 1 件出す)。
          - superseded_by が立った種子・取り下げ中の種子は発芽しない (削除も減衰もしないが表には出ない)。
        """
        fired = [
            seed
            for seed, did_fire, _, _ in self._germination_verdicts(
                input_text, context_texts, project, include_dispositions
            )
            if did_fire
        ]
        self._mark_fired(fired)
        return [copy.deepcopy(s) for s in fired]

    def _mark_fired(self, seeds):
        """発芽した種子の観測欄を更新する (メモリ上だけ: 保存は呼び手の `_locked` の 1 回、standalone なら `commit()`)。"""
        life = self.state["life"]
        for seed in seeds:
            seed["last_fired"] = life
            seed["fire_count"] = seed.get("fire_count", 0) + 1
            seed["miss_streak"] = 0

    @staticmethod
    def _kind_limits(include_dispositions):
        """注入索引の kind 別上限。`include_dispositions=True` (RAIYA_INJECT_DISPOSITIONS=1 の従来動作)
        では性向の上限を INDEX_MAX にする (全体の limit が効く)。"""
        if not include_dispositions:
            return INDEX_KIND_LIMITS
        limits = dict(INDEX_KIND_LIMITS)
        limits[DEFAULT_KIND] = INDEX_MAX
        return limits

    def _index_candidates(self, project, purpose, include_dispositions=False):
        """索引の候補 (上限適用前・並び替え済み)。superseded・取り下げ中の除外と project 絞り込みは両 purpose 共通。
        inject: kind 別の許可クラス (INJECTION_INDEX_CLASSES_BY_KIND) と kind 別上限 0 の kind を除く。
        consolidate: 全 kind・全クラス (LLM に reinforce/supersede の候補として見せる・§11.2)。"""
        # 判定は `_index_verdicts` だけで行い、ここは候補の集合を元の並び (倉庫の順を安定ソート) で返す。
        picked = {
            id(seed)
            for seed, _, reason in self._index_verdicts(project, purpose, include_dispositions)
            if reason in (None, INDEX_REASON_MAX)
        }
        out = [seed for seed in self.state["seeds"] if id(seed) in picked]
        out.sort(key=_index_sort_key)
        return out

    def _index_verdicts(self, project, purpose, include_dispositions=False, limit=INDEX_MAX, fired_ids=frozenset()):
        """索引の判定 (`index` / `seed_count` / `engine.explain_recall` の唯一の判定箇所・inspect spec §3.1)。

        戻り値: 全種子の `(seed, included, reason)` のリスト。**状態は変えない** (seed は内部 state の
        dict そのもの)。並び = 載る種子 (本番の索引順) → 上限で落ちた候補 (索引の並び順) → 候補に
        ならなかった種子 (倉庫の順)。`reason` は載るとき None、落ちたときは最初に引っかかった理由
        (INDEX_REASON_*)。候補 (= `seed_count` が数える limit 前の件数) は reason が None か
        INDEX_REASON_MAX のもの。

        `purpose="consolidate"`: superseded → 取り下げ → project → 上限 (全 kind・全クラス・群なし)。
        `purpose="inject"` (layered-memory plan §2.3): superseded → 取り下げ → project → kind 0 → class → 双子 →
        発芽 (`fired_ids` = ① に描画される種子の id。出ない種子は ③ に落ちる) → 群 → 休眠 → 上限。群の段は代表の選び方が休眠に
        依存するので順序に依存する (他の検査は互いに独立で、順序は理由の表示だけを決める)。群の成員は代表 1 件
        (③ の 1 項目) に畳まれ、上限 (kind 別・`limit`) は項目単位で当たる。
        """
        if purpose == "inject":
            return self._inject_index_plan(project, include_dispositions, limit, fired_ids)["verdicts"]
        candidates = []
        rejected = []
        for seed in self.state["seeds"]:
            if _is_superseded(seed):
                rejected.append((seed, False, INDEX_REASON_SUPERSEDED))
                continue
            if is_withdrawn(seed):
                rejected.append((seed, False, INDEX_REASON_WITHDRAWN))
                continue
            if not _project_matches(seed, project):
                rejected.append((seed, False, INDEX_REASON_PROJECT))
                continue
            candidates.append(seed)
        candidates.sort(key=_index_sort_key)
        included = candidates[:limit]
        over = candidates[len(included):]
        verdicts = [(seed, True, None) for seed in included]
        verdicts += [(seed, False, INDEX_REASON_MAX) for seed in over]
        return verdicts + rejected

    @staticmethod
    def _group_members(seeds, by_seed):
        """群の番号 -> 成員の種子 (群の並び = `_index_sort_key` の安定ソート)。"""
        members_of = {}
        for seed in seeds:
            gi = by_seed.get(id(seed))
            if gi is not None:
                members_of.setdefault(gi, []).append(seed)
        for members in members_of.values():
            members.sort(key=_index_sort_key)
        return members_of

    def _inject_index_plan(self, project, include_dispositions, limit, fired_ids, shadows=None, grouping=None,
                          input_text=None):
        """inject 用の索引判定の本体。戻り値 dict: verdicts (`_index_verdicts` の形)・entries (載る項目
        `[(seed, group 番号 or None)]`・③ の順)・total (limit 前の項目数)。状態は変えない。
        `shadows` / `grouping` (= `(groups, by_seed, members_of)`) は `_circle_verdicts` が 1 回だけ計算して渡す。
        `input_text` (今の入力だけ・省略可) があれば、並びは kind → 入力との関わり (`_index_relevance`、多いほど先) →
        クラス → 強度降順 → 倉庫の順 (index-reach spec §7、#106)。None / 連なりが空なら従来の並びと完全に同じ。"""
        seeds = self.state["seeds"]
        limits = self._kind_limits(include_dispositions)
        if shadows is None:
            shadows = _twin_shadows(seeds)
        if grouping is None:
            groups, by_seed = _seed_groups_by_seed([s for s in seeds if _project_matches(s, project)])
            grouping = (groups, by_seed, self._group_members(seeds, by_seed))
        groups, by_seed, members_of = grouping
        pos = {id(seed): i for i, seed in enumerate(seeds)}
        no_enjou = {
            gi for gi, members in members_of.items() if not any(m.get("sansho") == "圓成實" for m in members)
        }
        slots = []          # 種子ごとの [seed, reason or None]。None は後段 (群) で決まる
        slot_of = {}
        reached = {}        # 群番号 -> 双子までの門を通った成員 (群の並び)
        singles = []
        for seed in seeds:
            slot = [seed, None]
            slots.append(slot)
            slot_of[id(seed)] = slot
            if _is_superseded(seed):
                slot[1] = INDEX_REASON_SUPERSEDED
                continue
            if is_withdrawn(seed):
                slot[1] = INDEX_REASON_WITHDRAWN
                continue
            if not _project_matches(seed, project):
                slot[1] = INDEX_REASON_PROJECT
                continue
            kind = _seed_kind(seed)
            if limits.get(kind, 0) <= 0:
                slot[1] = INDEX_REASON_KIND_ZERO
                continue
            if seed.get("sansho") not in INJECTION_INDEX_CLASSES_BY_KIND.get(kind, ()):
                slot[1] = INDEX_REASON_CLASS
                continue
            if id(seed) in shadows:
                slot[1] = INDEX_REASON_TWIN.format(shadows[id(seed)])
                continue
            if _is_int(seed.get("id")) and seed["id"] in fired_ids:
                slot[1] = INDEX_REASON_FIRED
                continue
            gi = by_seed.get(id(seed))
            if gi in no_enjou:
                gi = None   # 生きた圓成實が無い群 (B8) は群として扱わず、成員を 1 件ずつ今の規則で判定する
            if gi is None:
                absent = _absent_local_paths(seed)
                if absent:
                    slot[1] = INDEX_REASON_DORMANT.format(", ".join(absent))
                    continue
                singles.append(seed)
            else:
                reached.setdefault(gi, []).append(seed)
        candidates = [(seed, None) for seed in singles]
        for gi, members in reached.items():
            group = groups[gi]
            label = group["label"]
            all_members = members_of[gi]
            # reached は倉庫の順に溜まるので、代表選び (と B10 の stat 順) は群の並びに直す。
            order = {id(m): i for i, m in enumerate(all_members)}
            members = sorted(members, key=lambda m: order[id(m)])
            fired_members = [m["id"] for m in all_members if m.get("id") in fired_ids]
            if isinstance(project, str) and group["project"] is None:
                for m in members:
                    slot_of[id(m)][1] = INDEX_REASON_GLOBAL_GROUP.format(label)
                continue
            if fired_members:
                for m in members:
                    slot_of[id(m)][1] = INDEX_REASON_GROUP_FIRED.format(label, min(fired_members))
                continue
            rep = None
            for m in members:
                if rep is not None:
                    slot_of[id(m)][1] = INDEX_REASON_GROUP_MEMBER.format(label, rep["id"])
                    continue
                absent = _absent_local_paths(m)
                if absent:
                    slot_of[id(m)][1] = INDEX_REASON_DORMANT.format(", ".join(absent))
                else:
                    rep = m
            if rep is not None:
                candidates.append((rep, gi))
        runs = _input_runs(input_text)
        if runs:
            lengths = sorted({len(r) for r in runs})   # 1 回だけ作る

            def order_key(pair):
                seed = pair[0]
                return (_kind_rank(_seed_kind(seed)), -_index_relevance(seed, runs, lengths)) + sansho.sansho_sort_key(seed)
        else:
            def order_key(pair):
                return _index_sort_key(pair[0])
        candidates.sort(key=lambda pair: (order_key(pair), pos[id(pair[0])]))
        taken = {}
        picked = []
        capped = []
        for pair in candidates:
            kind = _seed_kind(pair[0])
            if taken.get(kind, 0) >= limits.get(kind, 0):
                capped.append(pair)
                continue
            taken[kind] = taken.get(kind, 0) + 1
            picked.append(pair)
        included = picked[:limit]
        over = sorted(picked[len(included):] + capped, key=order_key)
        verdicts = [(seed, True, None) for seed, _ in included]
        verdicts += [(seed, False, INDEX_REASON_MAX) for seed, _ in over]
        verdicts += [(slot[0], False, slot[1]) for slot in slots if slot[1] is not None]
        # 理由が付かないまま残る種子は無い (候補は included / over のどちらかに入る)。
        return {"verdicts": verdicts, "entries": included, "total": len(candidates)}

    def _circle_verdicts(self, input_text, context_texts=(), project=None, include_dispositions=True,
                         fired_limit=None):
        """recall / explain_recall の唯一の判定 (layered-memory plan §2.1)。状態は変えない。

        種子の走査・`_twin_shadows`・`_seed_groups_by_seed` を 1 回ずつ行い、`_germination_verdicts` と
        inject 用の `_index_verdicts` (fired_ids つき) を 1 回ずつ呼ぶ。休眠の stat は path ごとに高々 2 回
        (発芽で 1 回・③ の代表選びで 1 回)。戻り値 dict の seed は内部 state の dict そのもの (呼び手は書き換えない)。
        `fired_limit` = ① の描画件数 (None なら発芽した全件)。germination / fired_ids (発芽した全件) / index (3 つ組の列) / entries (`[{"seed","group"}]`・③ の順) / entry_total /
        siblings (② の材料) / global_counts (④ の材料。project が str のときだけ)。"""
        seeds = self.state["seeds"]
        shadows = _twin_shadows(seeds)
        germination = self._germination_verdicts(
            input_text, context_texts, project, include_dispositions, shadows=shadows
        )
        fired = [seed for seed, did_fire, _, _ in germination if did_fire]
        fired_ids = {seed["id"] for seed in fired if _is_int(seed.get("id"))}
        # ① が実際に描画する発芽種子 (ego_weight が件数 `fired_limit` を決める)。③ の重複除去と ② はこれだけを見る:
        # 発芽したが ① に出ない種子は今までどおり ③ に落ちる (情報を落とさない)。
        rendered = fired if fired_limit is None else sorted(fired, key=sansho.sansho_sort_key)[:fired_limit]
        rendered_ids = {seed["id"] for seed in rendered if _is_int(seed.get("id"))}
        groups, by_seed = _seed_groups_by_seed([s for s in seeds if _project_matches(s, project)])
        members_of = self._group_members(seeds, by_seed)
        plan = self._inject_index_plan(
            project, include_dispositions, INDEX_MAX, rendered_ids, shadows=shadows, grouping=(groups, by_seed, members_of),
            input_text=input_text,
        )
        hidden_ids = {seed.get("id") for seed in seeds if id(seed) in shadows}
        siblings = []
        for gi, group in enumerate(groups):
            members = members_of.get(gi, [])
            group_fired = [m["id"] for m in members if m["id"] in rendered_ids]
            if not group_fired or not any(m.get("sansho") == "圓成實" for m in members):
                continue
            siblings.append({
                "group": copy.deepcopy(group),
                "fired_ids": group_fired,
                "listable_ids": [m["id"] for m in members if m["id"] not in rendered_ids and m["id"] not in hidden_ids],
            })
        global_counts = []
        if isinstance(project, str):
            for kind in VALID_KINDS:
                sizes = [g["size"] for g in groups if g["project"] is None and g["kind"] == kind]
                if sizes:
                    global_counts.append({"kind": kind, "groups": len(sizes), "seeds": sum(sizes)})
        return {
            "germination": germination,
            "fired_ids": fired_ids,
            "index": plan["verdicts"],
            "entries": [
                {
                    "seed": seed,
                    # ③ の群の代表 = 休眠を見て選ばれた種子 (構造上の先頭 `group["representative"]` とは違うことがある)
                    "group": dict(copy.deepcopy(groups[gi]), representative=seed.get("id")) if gi is not None else None,
                }
                for seed, gi in plan["entries"]
            ],
            "entry_total": plan["total"],
            "siblings": siblings,
            "global_counts": global_counts,
        }

    def index(self, project=None, purpose="inject", limit=INDEX_MAX, include_dispositions=False):
        """想起/統合の索引 (改修4 §6・改修5 §6.3)。deep copy の list。

        `project`: 現在の project (str)、None (全域の種子のみ)、または統合窓の project 集合。
        全域 (`project=None`) の種子は常に載る。
        `purpose="inject"`: kind ごとの上限 (INDEX_KIND_LIMITS)・kind 別の許可クラス。再開点と
        性向は上限 0 = 載らない。`include_dispositions=True` (従来動作) なら性向 (圓成實) も載る。
        `purpose="consolidate"`: 全 kind・全クラスを `limit` まで。
        順序 = kind → クラス → 強度降順。
        """
        return [
            copy.deepcopy(seed)
            for seed, included, _ in self._index_verdicts(project, purpose, include_dispositions, limit=limit)
            if included
        ]

    def consolidation_view(self, projects, texts, related_max=30, groups_max=20):
        """統合プロンプトの既存記録の入力 (層化 B5・読むだけ・何も書かない)。

        `projects`: 窓の project 集合 (None 含む)。`texts`: プロンプトに載る本文そのもの (呼び手が
        切り詰め済みで渡す)。`related`: 生きた (superseded でも取り下げ中でもない) ∧ project が合う ∧
        一致した縁が `_needed_conditions(kind)` 以上の種子 (発芽と同じ照合。強度・GERMINATION_MAX・
        kind/クラスの門・双子・休眠は使わない)。`_index_sort_key` の安定ソートで先頭 `related_max` 件。
        `groups`: 生きた ∧ project が合う種子への `seed_groups` の先頭 `groups_max` 群 (群の並びのまま)。
        各群は群の dict の深いコピーに `rep` (構造上の代表の種子・stat はしない) を足したもの。
        戻り値: {"related", "related_total", "groups", "groups_total"}。深いコピーは返す分だけ。
        """
        texts_n = [sansho._n(t or "").lower() for t in (texts or [])]
        live = [s for s in self.state["seeds"] if is_live(s) and _project_matches(s, projects)]
        match_memo = {}
        related = [
            s for s in live if len(_matched_conditions(s, texts_n, match_memo)) >= _needed_conditions(_seed_kind(s))
        ]
        related.sort(key=_index_sort_key)
        groups, by_seed = _seed_groups_by_seed(live)
        shown_groups = []
        for gi, g in enumerate(groups[:groups_max]):
            row = copy.deepcopy(g)
            # 代表は id でなく同一性で引く (id が重複しても別の種子を混ぜない・`group_of` と同じ)。
            rep = next((s for s in live if by_seed.get(id(s)) == gi and s["id"] == g["representative"]), None)
            row["rep"] = copy.deepcopy(rep)
            shown_groups.append(row)
        return {
            "related": [copy.deepcopy(s) for s in related[:related_max]],
            "related_total": len(related),
            "groups": shown_groups,
            "groups_total": len(groups),
        }

    def resume_point(self, project):
        """同 project (または全域) の、superseded でも取り下げ中でもない 再開点 を 1 件返す (無ければ None)。
        同 project の 再開点 があればその中で id 最大、無ければ全域 (project が str でない) の中で id 最大 (#135 S2)。
        同 project に 1 件の特則 (§5.3) で通常は高々 1 件。imprint の `_live_resume_point` は別 (完全一致)。"""
        found = None
        found_global = None
        for seed in self.state["seeds"]:
            if _seed_kind(seed) != "再開点" or not is_live(seed):
                continue
            if _seed_project(seed) is None:
                if found_global is None or seed["id"] > found_global["id"]:
                    found_global = seed
                continue
            if _seed_project(seed) != project:
                continue
            if found is None or seed["id"] > found["id"]:
                found = seed
        if found is None:
            found = found_global
        return copy.deepcopy(found) if found is not None else None

    def seed_count(self, project=None, purpose="inject", include_dispositions=False):
        """`index(project, purpose)` の limit 前の該当件数 (ヘッダの「全 M 件」用)。"""
        return len(self._index_candidates(project, purpose, include_dispositions))

    # --- 熏習 (現行熏種子) ---
    @staticmethod
    def _turn_record(window):
        """imprint_log の turn 欄: plain Turn 1 件=従来スナップショット / 全て台帳の往復=ledger_ids /
        それ以外=スナップショットの list。"""
        ids = [_ledger_id(t) for t in window]
        if len(window) == 1 and ids[0] is None:
            return {"user_text": window[0].user_text, "assistant_text": window[0].assistant_text}
        if all(i is not None for i in ids):
            return {"ledger_ids": ids}
        return {"turns": [{"user_text": t.user_text, "assistant_text": t.assistant_text} for t in window]}

    def _find_seed(self, seed_id):
        """target_id で種子を引く (無ければ None)。bool は int の派生型で `True == 1` が
        成り立ち、`target_id: true` が id=1 の種子を書き換えてしまうため明示的に除外する。"""
        if not isinstance(seed_id, int) or isinstance(seed_id, bool):
            return None
        for seed in self.state["seeds"]:
            if seed["id"] == seed_id:
                return seed
        return None

    def group_of(self, seed_id):
        """読み取り専用: `get_by_ids` と同じ種子 (id が最初に一致するもの) を見つけ、その bucket の生きた種子で
        群を計算し、その種子を含む群の deep copy を返す (1 件・生きていない・無ければ None)。
        照合は object の同一性で行い、id の再照合はしない。何も書かない。"""
        if not _is_int(seed_id):
            return None
        target = next((s for s in self.state["seeds"] if s["id"] == seed_id), None)
        if target is None or not is_live(target):
            return None
        bucket = (_seed_kind(target), _seed_project(target))
        peers = [s for s in self.state["seeds"] if is_live(s) and (_seed_kind(s), _seed_project(s)) == bucket]
        groups, by_seed = _seed_groups_by_seed(peers)
        gi = by_seed.get(id(target))
        return copy.deepcopy(groups[gi]) if gi is not None else None

    def withdraw(self, seed_id, record):
        """種子を取り下げる (欄 `withdrawn` を立て history に積む)。record の deep copy を返す。
        **save しない** (呼び元の `_locked` が永続化する)。superseded の種子も取り下げられる。"""
        seed = self._find_seed(seed_id)
        if seed is None:
            raise WithdrawalRefused("種子 #{} は無い".format(seed_id))
        if is_withdrawn(seed):
            raise WithdrawalRefused("種子 #{} は既に取り下げ済み".format(seed_id))
        seed["withdrawn"] = copy.deepcopy(record)
        seed.setdefault("history", []).append(
            _history_entry(seed, self.current_life, "withdrawn", withdrawn=copy.deepcopy(record))
        )
        return copy.deepcopy(record)

    def set_replaced_by(self, seed_id, new_id):
        """置き換えが書いた種子 id を、取り下げ record と最後の withdrawn history 行に立てる。save しない。"""
        seed = self._find_seed(seed_id)
        if seed is None or not is_withdrawn(seed):
            raise WithdrawalRefused("種子 #{} は取り下げられていない".format(seed_id))
        if not isinstance(seed["withdrawn"], dict):
            raise WithdrawalRefused("種子 #{} の取り下げ記録の形が不正".format(seed_id))
        seed["withdrawn"]["replaced_by"] = new_id
        for row in reversed(seed.get("history", [])):
            if isinstance(row, dict) and row.get("reason") == "withdrawn" and isinstance(row.get("withdrawn"), dict):
                row["withdrawn"]["replaced_by"] = new_id
                break

    def restore(self, seed_id, record):
        """取り下げを戻す (欄を None に、history に復帰行)。record の deep copy を返す。save しない。"""
        seed = self._find_seed(seed_id)
        if seed is None:
            raise WithdrawalRefused("種子 #{} は無い".format(seed_id))
        if not is_withdrawn(seed):
            raise WithdrawalRefused("種子 #{} は取り下げられていない".format(seed_id))
        seed["withdrawn"] = None
        seed.setdefault("history", []).append(
            _history_entry(seed, self.current_life, "restored", restored=copy.deepcopy(record))
        )
        return copy.deepcopy(record)

    def imprint(self, turns, proposal, replacement_for=None, injected_by_turn=None, session_by_turn=None):
        """提案を熏習する (記憶機能設計書§3.3〜§3.5・改修4 §5)。

        1. `sansho.evaluate_proposal(proposal, window)` で三性ゲート判定 (窓=Turn または
           Turn の列。それ以外は TypeError = KUN-7)。
        2. 判定を問わず imprint_log へ 1 行追記 (KUN-1)。
        3. 遍計所執 -> quarantine。縁が全滅 -> 却下。
        4. 圓成實/依他起: proposal["action"] に従う。
           - "supersede" + target_id: 提案クラスの rank が target 以下 (圓成實は何でも、
             依他起は依他起のみ=越境禁止) なら、旧値を history に追記して content/conditions/
             polarity/sansho を置換、evidences 追記、strength += 接地した往復の数。
           - "reinforce" + target_id: 同クラス∧同 kind∧同 project∧superseded でない∧取り下げ中でない なら
             strength += 接地した往復の数・evidences 追記・content 不変 (polarity は見ない=改修5 §5.1)。
           - 条件を満たさない supersede/reinforce と "new": 類似度併合 (同クラス∧同 kind∧同
             project∧superseded でない∧取り下げ中でない∧類似度>=閾値 -> reinforce と同じ) か新規作成 (strength =
             接地した往復の数)。満たさなかった理由は reason に注記する。
           - supersede が成立したら横断 supersede (改修5 §5.2): 同 project∧同 kind で接地済み縁を
             2 語以上共有する他の種子に superseded_by を立てる (越境は不可)。
        `session_by_turn` (#145 X1/X2b・既定 None = 効かない): 窓と同じ順の座 (session_id、str 以外は None という
        1 つの座) のリスト。座が 2 つ以上混ざる窓では、span の座の集合の共通部分が空なら主の座の span だけ残して
        判定し直し (X2b)、ゲートが接地させた縁が span の座のどの往復にも無ければ記録のみ (X1)。人の置き換えでは効かない。
        改修5: kind は形の検査 (`_resolve_kind`・§4.3) で読み替え/却下、project は接地した往復から
        (`_window_project`・§4.2。跨げば却下)、再開点 は同 project 1 件の特則 (§5.3)。

        strength は span の本数ではなく**接地した往復の数**で数える (改修4)。同じ往復から
        同趣旨の引用を 2 本挙げれば strength 2 = 依他起でも即発芽、という抜け道を塞ぐため:
        裏付けの厚みを決めるのは提案の書き方ではなく、別々の往復で観測されたかどうか。
        evidences には (重複を除いた) span を全て記録する。
        種子は削除されない。content が変わるのは supersede だけで、旧値は history に残る。

        `replacement_for` (int の取り下げ種子 id・既定 None): 人間の置き換えとして呼ばれたとき、理由に由来の注記を足す。
        規則は変えない。
        imprint は save しない (永続化は呼び元の `_locked` の 1 回だけ。standalone の呼び手は `commit()`)。

        戻り値: {"sansho","decision","grounded_conditions","reason","spans","seed","is_new",
        "kind","project"} (+ 取り下げ照合 時 "withdrawn_match": W の id・seed=None・is_new=None、
        双子照合 時 "twin_match": H の id・seed=None・is_new=None・H は不変 (#78)、
        user span 0 本の提案が user の逐語を持つ種子へ併合されるとき "user_span_match": その id・seed=None・is_new=None・種子は不変 (decision は三性のまま、#101 D)、同 session で注入済みの種子への assistant だけの言い直し (R, #105) は "echo_match" で同形、依他起 の提案 (kind 決定・回避策・性向 のみ、所在 は除外 §7.2) が同 kind・同 project の生きた 圓成實 と content 同一 (`sansho._n` + strip + casefold・§7.1) ∧ 縁共有 なら新規の直前に "settled_match" (SC', #152) で同形、+ supersede 時 "superseded": True と "also_superseded": [ids]、
        reinforce/併合時 "reinforced": True、遍計所執時 "quarantine_entry": 記帳した観測用エントリ。
        却下 (decision "reject") のときは kind/project が無いことがある)。
        """
        window = sansho.as_window(turns)
        decision = sansho.evaluate_proposal(proposal, window)
        life = self.state["life"]
        log_entry = {
            "life": life,
            "turn": self._turn_record(window),
            "proposal": copy.deepcopy(proposal),
            "decision": decision["decision"],
            "reason": decision["reason"],
            # 決定が書き込んだ種子の id (inspect spec §2.2)。new = 新しい id、merge/reinforce/supersede =
            # 書き込んだ先の id。reject と 遍計所執 は None のまま。古い行には欄が無い (書き換えない)。
            "seed_id": None,
        }
        self.append_imprint_log(log_entry)

        result = dict(decision)
        result["seed"] = None
        result["is_new"] = None

        if decision["decision"] == "reject":
            return result

        content = proposal.get("content") or ""
        polarity = proposal.get("polarity")
        if polarity not in VALID_POLARITIES:
            polarity = "無記"

        def _note(text):
            log_entry["reason"] = log_entry["reason"] + " / " + text
            result["reason"] = log_entry["reason"]

        def _reject(text):
            log_entry["decision"] = "reject"
            result["decision"] = "reject"
            _note(text)
            return result

        if decision["decision"] == "遍計所執":
            # 隔離は形の検査より先 (改修5 §4.3「遍計所執は従来どおり先に quarantine」): 非接地の観測記録を
            # kind の形で失わない。隔離行の kind は自己申告が列挙にあればそれ、無ければ 性向。
            raw_kind = proposal.get("kind")
            entry = {
                "id": self.state["next_quarantine_id"],
                "content": content,
                "conditions": list(decision["grounded_conditions"]),
                "polarity": polarity,
                "sansho": "遍計所執",
                "kind": raw_kind if raw_kind in VALID_KINDS else DEFAULT_KIND,
                "project": None,  # 非接地なので往復に帰属しない
                "life": life,
                "reason": decision["reason"],
            }
            self.state["next_quarantine_id"] += 1
            self._append_quarantine(entry)
            result["quarantine_entry"] = copy.deepcopy(entry)
            result["kind"] = entry["kind"]
            result["project"] = None
            return result

        # #145 X2b -> X1: 別の座 (session) の言の貼り合わせ。遍計所執の隔離の後・形の検査の前。
        if session_by_turn is not None and replacement_for is None:
            seats = [s if isinstance(s, str) else None for s in session_by_turn]
            if len(seats) == len(window) and len(set(seats)) > 1:
                span_seats = [_span_seats(r, window, seats) for r in decision["spans"]]
                if not frozenset.intersection(*span_seats):
                    main = _main_seat(decision["spans"], window, seats)
                    keep = [i for i, st in enumerate(span_seats) if main in st]
                    dropped = len(span_seats) - len(keep)
                    kept_spans = {id(decision["spans"][i]) for i in keep}
                    narrowed = dict(proposal)
                    narrowed["evidence"] = [
                        {"source": r["source"], "span": r["span"]} for r in decision["spans"] if id(r) in kept_spans
                    ]
                    decision = sansho.evaluate_proposal(narrowed, window)
                    span_seats = [span_seats[i] for i in keep]
                    log_entry["decision"] = result["decision"] = decision["decision"]
                    result["sansho"] = decision["sansho"]
                    result["spans"] = decision["spans"]
                    result["grounded_conditions"] = decision["grounded_conditions"]
                    log_entry["reason"] = result["reason"] = decision["reason"]
                    _note("別の座の span {} 本を外した".format(dropped))
                allowed = frozenset.intersection(*span_seats)
                for cond in decision["grounded_conditions"]:
                    if not any(
                        seats[i] in allowed and sansho.ground_conditions([cond], t.user_text, t.assistant_text)
                        for i, t in enumerate(window)
                    ):
                        return _reject(CONDITION_FROM_OTHER_SESSION)
                # 往復の付け替え: ゲートの turn_index (最初の接地) の座が S の外にある span だけ、S の座で接地する
                # 最初の往復に直す (ledger_id・strength・project が外した座の往復から取られないように)。S の中にある
                # span は turn_index をそのまま (strength を変えない)。
                repointed = []
                for r in decision["spans"]:
                    if seats[r["turn_index"]] in allowed:
                        repointed.append(r)
                        continue
                    first = min(
                        i
                        for i, t in enumerate(window)
                        if seats[i] in allowed
                        and sansho.classify_sansho(r["span"], t.user_text, t.assistant_text) == r["sansho"]
                    )
                    repointed.append(dict(r, turn_index=first))
                decision = dict(decision, spans=repointed)
                result["spans"] = repointed

        # 形の検査 (改修5 §4.3): kind の読み替え/却下は決定論の文字列検査。身分 (sansho) の判定の後、
        # 接地した提案にだけ効く (列挙に無い kind は却下、決定/回避策/好みの形は 性向 に読み替え)。
        kind, kind_note = _resolve_kind(
            proposal, decision["spans"], decision["grounded_conditions"], window, allow_promotion=(replacement_for is None)
        )
        if kind is None:
            return _reject(kind_note)
        if kind_note:
            _note(kind_note)
        if replacement_for is not None:
            # 人間の置き換え (raiya-correct replace): 由来を理由に残す。B4 の影はこの呼び出しでは飛ばす。
            _note("置き換え: 取り下げた #{} の代わり (人間の宣言)".format(replacement_for))
        result["kind"] = kind

        sansho_class = decision["decision"]
        grounded = decision["grounded_conditions"]
        # 提案内の重複 span を畳む。比較は三性ゲートと同じ正規化 (`sansho._n`) 済みの
        # 文字列 + 接地した往復で行う (空白差だけの重複も同じ引用とみなす)。
        evidence_entries = []
        seen_spans = set()
        grounded_turn_indexes = set()
        for span_result in decision["spans"]:
            key = (sansho._n(span_result["span"]), span_result["turn_index"])
            if key in seen_spans:
                continue
            seen_spans.add(key)
            grounded_turn_indexes.add(span_result["turn_index"])
            turn = window[span_result["turn_index"]]
            entry = {
                "life": life,
                # source は判定から導出する。span_result["source"] (提案の自称) は読まない
                # -- 身分は自称できない (EXE-15)。
                "source": "user" if span_result["sansho"] == "圓成實" else "assistant",
                "span": span_result["span"],
                "ledger_id": _ledger_id(turn),
            }
            if entry["ledger_id"] is None:
                entry["turn"] = {"user_text": turn.user_text, "assistant_text": turn.assistant_text}
            evidence_entries.append(entry)
        # 加算する強度 = 接地した往復の数 (台帳の窓なら「別々の往復で裏付けられた件数」)。
        # span の本数で数えると、同じ一往復から同趣旨の引用を 2 本並べるだけで依他起が
        # 発芽閾値 2 に届いてしまう = 裏付けの厚みを提案側 (LLM) が決められてしまう。
        gained = len(grounded_turn_indexes)

        # 種子の project = 接地した往復の project 集合 (改修5 §4.2)。跨いでいれば却下。
        status, project = _window_project(window, grounded_turn_indexes)
        if status == "reject":
            return _reject(project)
        result["project"] = project

        # L (spec 2026-10-30-question-as-evidence §7): 圓成實 の 所在・性向 は形を担う user の span が無ければ
        # 記録のみ。入力はゲート判定の span と縁だけ (提案の自称は読まない)。人の置き換えは除外。
        # L' (spec §9): 4 つの形の判定は疑の文を除いた残り (U/RP2b/KP/EC6 と同じ `_span_remainders_fn(window)`) に対して行う。
        if sansho_class == "圓成實" and kind in ("所在", "性向") and replacement_for is None:
            l_remainders_of = _span_remainders_fn(window)
            user_spans = [
                piece
                for sp in decision["spans"]
                if sp.get("sansho") == "圓成實"
                for r in l_remainders_of(sp)
                for piece in r.split("\n")
                if piece.strip()
            ]
            if not form_in_user_spans_for_kind(kind, user_spans, grounded):
                return _reject(PLACE_NEEDS_FORM_IN_USER_SPAN if kind == "所在" else DISPOSITION_NEEDS_FORM_IN_USER_SPAN)
        # RP2b (#115, spec 2026-10-31-resume-point-and-kind-name-design §7): 圓成實 の 再開点 は user の span の疑の文を除いた
        # 残りに縁が 1 語以上無ければ記録のみ (新規・supersede とも。再開点 の特則より前)。依他起 (assistant の span) は不変。
        if (
            sansho_class == "圓成實"
            and kind == "再開点"
            and replacement_for is None
            and not _resume_form_in_user_spans(decision["spans"], grounded, window)
        ):
            return _reject(RESUME_NEEDS_FORM_IN_USER_SPAN)

        def _reinforce(seed):
            log_entry["seed_id"] = seed["id"]
            seed["strength"] += gained
            seed.setdefault("evidences", []).extend(evidence_entries)
            result["seed"] = copy.deepcopy(seed)
            result["is_new"] = False
            result["reinforced"] = True
            return result

        def _mergeable(seed):
            """併合/reinforce の候補になれるか (改修5 §5.1): 同クラス ∧ 同 kind ∧ 同 project ∧
            superseded でない。取り下げ中かどうかは見ない (B4: 呼び手が最良候補の生死で分岐する)。
            polarity は見ない (LLM が毎回付けるので揺れる)。"""
            return (
                seed.get("sansho") == sansho_class
                and _seed_kind(seed) == kind
                and _seed_project(seed) == project
                and not _is_superseded(seed)
            )

        def _target_fits(seed):
            """target の kind / project が提案と違う、または superseded なら new として扱う
            (他 project の種子を書き換えさせない)。クラスは呼び出し側で別途見る。"""
            return (
                _seed_kind(seed) == kind
                and _seed_project(seed) == project
                and not _is_superseded(seed)
                and not is_withdrawn(seed)
            )

        def _hist(seed, reason, **extra):
            return _history_entry(seed, life, reason, **extra)

        # 双子照合 (#78・spec 2026-10-26-paste-recall-and-twin-reinforce-design.md P5〜P7): 依他起の提案が
        # 生きた圓成實 E に隠された双子 H へ落ちるなら、植えず・強度を変えず記録だけ残す。
        # 影は imprint 1 回につき 1 回だけ読み取り時に導く (書かない)。圓成實の提案・人の置き換え
        # (replacement_for)・再開点では引かない。supersede は積算でないので対象外 (従来どおり通す)。
        twin_on = sansho_class == "依他起" and replacement_for is None and kind != "再開点"
        shadows = _twin_shadows(self.state["seeds"]) if twin_on else {}

        def _twin_match(seed):
            """seed が影の H なら 双子照合 の結果を返す (H は不変)。そうでなければ None。"""
            e_id = shadows.get(id(seed))
            if e_id is None:
                return None
            _note("生きた圓成實 #{} に隠された双子 #{} への再提案のため積まない".format(e_id, seed["id"]))
            log_entry["decision"] = result["decision"] = "双子照合"
            log_entry["seed_id"] = seed["id"]
            result["sansho"] = sansho_class
            result["seed"] = None
            result["is_new"] = None
            result["twin_match"] = seed["id"]
            return result

        # D / E (#101 裁定 §6): 植え付け・置換の権利は class の順位でなく user の逐語 span の有無で決める。
        # 判定はゲートの span 判定 (decision["spans"]) だけを読む。提案の自称 (source / sansho 欄) は読まない。
        proposal_has_user_span = any(sp.get("sansho") == "圓成實" for sp in decision["spans"])

        def _may_cross(seed):
            """E: 依他起の提案が 圓成實 の seed (同 kind は _target_fits が見る) に作用してよいか。"""
            return (
                sansho_class == "依他起"
                and seed.get("sansho") == "圓成實"
                and kind in ("決定", "回避策")
                and form_in_user_evidence(kind, evidence_entries)
            )

        def _holds_user_span(seed):
            """D: seed の evidences に user 接地の span (source == "user" = 判定から導出した値) があるか。
            欄の無い古い形式は不明として数えない (モジュール関数 has_user_span に委譲)。"""
            return has_user_span(seed)

        def _echoes_injection(seed):
            """(EC2' (#119) は新規植え付けの直前に別途、縁の共有で同じ「復唱」を user の span が形を担わない提案にも広げる。)
            R (#105): assistant だけの提案が、その assistant の span が接地した往復と同じ session に
            注入済みの種子を指すか。`injected_by_turn` (往復の添字 -> 注入済み種子 id の集合) は呼び手
            (engine.consolidate) が manas_log から作る。None (MCP imprint 等) なら R は効かない。"""
            if injected_by_turn is None or replacement_for is not None or kind == "再開点" or proposal_has_user_span:
                return False
            indexes = [sp.get("turn_index") for sp in decision["spans"]]
            if not indexes or any(i is None for i in indexes):
                return False
            return all(seed["id"] in injected_by_turn.get(i, ()) for i in indexes)

        def _record_only(seed, note, key):
            _note(note)
            log_entry["seed_id"] = seed["id"]
            result["seed"] = None
            result["is_new"] = None
            result[key] = seed["id"]
            return result

        def sp_touched_text():
            """SP1'' の「新しい言の本文」: 圓成實 の span ごとの疑の文を除いた残りの連結 (EC2'/RP2b/KP と同じ切り方)。"""
            if "v" not in sp_cache:
                remainders_of = _span_remainders_fn(window)
                sp_cache["v"] = "\n".join(
                    r for sp in decision["spans"] if sp.get("sansho") == "圓成實" for r in remainders_of(sp)
                )
            return sp_cache["v"]

        def sp_user_grounded():
            """U (#122 §10.1): 門に渡す新の縁 = ゲートの grounded のうち窓の user 本文で接地したものだけ
            (`sansho.ground_conditions_in_window` の user 側だけ = assistant 本文を空にして同じ照合)。"""
            if "u" not in sp_cache:
                sp_cache["u"] = [
                    c for c in grounded if any(sansho.ground_conditions([c], t.user_text, "") for t in window)
                ]
            return sp_cache["u"]

        sp_cache = {}
        sp_excluded_target = None
        action = proposal.get("action")
        if action not in VALID_ACTIONS:
            action = "new"
        target = self._find_seed(proposal.get("target_id"))

        # 再開点 の特則 (改修5 §5.3): 同 project に 1 件。既存があれば action を問わずそれを
        # target にした supersede に読み替える。クラスは問わない: 依他起の再開点が圓成實の再開点を
        # 置換してよい (2026-10-22 に §12.2 の却下を覆した。WM-3 勝義無性=世代交代可能性を保つ。
        # 旧値は history に残る)。他の kind の越境禁止と、クラス跨ぎの併合/reinforce 禁止は残す。
        if kind == "再開点":
            existing = _live_resume_point(self.state["seeds"], project)
            # B (#101): 既存が無く新規になる 再開点 は 圓成實 (全 span が user 接地) のみ。
            if existing is None and sansho_class != "圓成實":
                return _reject(RESUME_NEW_ENJOUJITSU_ONLY)
            # RH2 (#129, spec 2026-11-04-resume-hearsay-design §5.2/§7): user の span が無く、ゲートが接地させた assistant の span の
            # どれかに伝聞の印があれば、既存を置き換えず記録のみ。既存の class・user の span は問わない。人の置き換えは除外。
            if (
                existing is not None
                and not proposal_has_user_span
                and replacement_for is None
                and any(sp.get("sansho") != "圓成實" and _has_marker(sansho._n(sp["span"]), HEARSAY_MARKERS) for sp in decision["spans"])
            ):
                return _reject(RESUME_HEARSAY_RECORD_ONLY)
            if existing is not None:
                if action != "supersede" or target is not existing:
                    _note("再開点: 同 project の既存 #{} を target にした supersede に読み替える".format(existing["id"]))
                action = "supersede"
                target = existing

        if action == "supersede":
            if target is None:
                _note("supersede 不可: target_id {!r} が存在しないため new として扱う".format(proposal.get("target_id")))
            # 未分類 (sansho 欄の無い旧形式種子) は既定 0 = 圓成實と同じ最強ランクに置く。
            # sansho.py の発芽順が未分類を最弱に置くのとは逆向きだが、ここは「誰が誰を
            # 置換してよいか」の判定なので、素性の分からない種子ほど置換されにくい側
            # (依他起では置換できない) に倒すのが fail-closed。
            # 再開点だけは越境を見ない (上の特則で target は同 project の既存 1 件に固定済み)。
            elif (
                kind != "再開点"
                and sansho.SANSHO_RANK[sansho_class] > sansho.SANSHO_RANK.get(target.get("sansho"), 0)
                and not _may_cross(target)
            ):
                _note("supersede 不可: 依他起は圓成實を置換できない(越境)ため new として扱う")
            elif kind != "再開点" and not proposal_has_user_span and _holds_user_span(target):
                _note("supersede 不可: user の逐語を持つ種子は assistant だけの提案では置換できない")
            elif is_withdrawn(target):
                _note("supersede 不可: target が取り下げ中のため併合候補として扱う")
            elif not _target_fits(target):
                _note("supersede 不可: target の kind/project が違うか superseded のため new として扱う")
            elif (
                kind != "再開点"
                and replacement_for is None
                and (sp_untouched := _untouched_user_sentence(target, sp_user_grounded(), sp_touched_text)) is not None
            ):
                # SP1'' (#122): 旧の user の文に、新しい言が触れていない記が残るなら部分交代 = 全体を置換せず new。
                # U (#122 §10.1) = 改める範囲を定めるのは user の言 (Q-C 導出 3); assistant の復唱 (所覺) は「触れた」に数えない。
                # 倒した提案は target に類似併合しない (新は新規に植える)。他の種子への併合は従来どおり。
                sp_excluded_target = target
                _note("supersede 不可: 旧の記「{}」に新しい言が触れていない(部分交代)ため new として扱う".format(sp_untouched))
            else:
                target.setdefault("history", []).append(_hist(target, "superseded"))
                target["content"] = content
                target["conditions"] = list(grounded)
                target["polarity"] = polarity
                target["sansho"] = sansho_class
                target["strength"] += gained
                target.setdefault("evidences", []).extend(evidence_entries)
                # 横断 supersede (改修5 §5.2): 同 project ∧ 同 kind ∧ 新しい提案の接地済み縁を
                # HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS 語以上共有する他の種子に superseded_by を
                # 立てる。語の一致は完全一致 (部分文字列・大文字小文字の違いは共有に数えない)。
                # 越境規則に従う: 依他起の提案は圓成實の種子を隠せない。削除も減衰もしない。
                also = []
                grounded_set = set(grounded)
                for seed in self.state["seeds"]:
                    if seed is target or not is_live(seed):
                        continue
                    if _seed_kind(seed) != kind or _seed_project(seed) != project:
                        continue
                    if sansho.SANSHO_RANK[sansho_class] > sansho.SANSHO_RANK.get(seed.get("sansho"), 0):
                        continue
                    shared = grounded_set & set(seed_conditions(seed))
                    if len(shared) < HORIZONTAL_SUPERSEDE_SHARED_CONDITIONS:
                        continue
                    if kind != "再開点" and replacement_for is None:
                        untouched = _untouched_user_sentence(seed, sp_user_grounded(), sp_touched_text)
                        if untouched is not None:
                            _note("横断 supersede 見送り: #{} の記「{}」に新しい言が触れていない(部分交代)".format(seed["id"], untouched))
                            continue
                    seed.setdefault("history", []).append(
                        _hist(seed, "superseded_by", superseded_by=target["id"])
                    )
                    seed["superseded_by"] = target["id"]
                    also.append(seed["id"])
                if also:
                    _note("横断 supersede: #{} が {} を隠した".format(target["id"], also))
                log_entry["seed_id"] = target["id"]
                result["seed"] = copy.deepcopy(target)
                result["is_new"] = False
                result["superseded"] = True
                result["also_superseded"] = also
                return result

        if action == "reinforce":
            if target is None:
                _note("reinforce 不可: target_id {!r} が存在しないため new として扱う".format(proposal.get("target_id")))
            elif target.get("sansho") != sansho_class and not _may_cross(target):
                # D は越境判定より先に効く (#105): user span 0 本の提案が user の逐語を持つ種子を指すなら、
                # 越境で new に倒さず記録のみ (出力は併合経路の D と同形)。target と提案の類似度は見ない。
                # 取り下げ中・superseded・他 project の target は従来どおり new。双子として隠された H は同 class なのでこの腕に来ない。
                # kind は H で 性向 に読み替わりうるので、target の kind が提案の読み替え後 kind か自称 kind と同じなら対象とする。
                if (
                    kind != "再開点"
                    and replacement_for is None
                    and not proposal_has_user_span
                    and _holds_user_span(target)
                    and _seed_project(target) == project
                    and not _is_superseded(target)
                    and not is_withdrawn(target)
                    and _seed_kind(target) in (kind, proposal.get("kind"))
                ):
                    return _record_only(target, _D_NOTE.format(target["id"]), "user_span_match")
                _note("reinforce 不可: クラスが一致しない(越境)ため new として扱う")
            elif is_withdrawn(target):
                _note("reinforce 不可: target が取り下げ中のため併合候補として扱う")
            elif not _target_fits(target):
                _note("reinforce 不可: target の kind/project が違うか superseded のため new として扱う")
            else:
                # 順序: 取り下げ (上の腕) → 双子照合 (#78) → D → R。双子として隠された H は従来どおり 双子照合。
                twin = _twin_match(target)
                if twin is not None:
                    return twin
                # D: 同クラスでも越境腕と同じく記録のみ (利用者裁定 2026-10-30)。類似度・縁の共有は見ない。
                if replacement_for is None and not proposal_has_user_span and _holds_user_span(target):
                    return _record_only(target, _D_NOTE.format(target["id"]), "user_span_match")
                if _echoes_injection(target):
                    return _record_only(target, _R_NOTE.format(target["id"]), "echo_match")
                if not _shares_condition(target, grounded):
                    # D / R の後に置く: user の逐語を持つ種子・注入済みの種子への誤 target は記録のみが安全側 (人の裁定)。
                    _note("reinforce 不可: target と縁を必要語数共有しないため new として扱う")
                else:
                    return _reinforce(target)

        # new: 同クラス∧同 kind∧同 project∧superseded でない∧類似度>=閾値 の既存種子があれば
        # 併合 (§3.5・改修5 §5.1)、無ければ新規。併合時 polarity は既存の値を保つ。
        # 候補は生きた種子 ∪ 取り下げ中の種子 (B4)。最良が取り下げ中の W なら植えない (取り下げ照合)。
        # 取り下げ前に W へ併合されていた提案だけが止まり、より弱い生きた種子を強めることもない。
        # 無効: 人間の置き換え (replacement_for) と 再開点 (取り下げ前も併合経路に来ない)。
        # クラス跨ぎの言い直しは止まらない (取り下げ前と同じ。R5-3 の限界)。ただし 依他起 → 圓成實 の向きで content が同一 (正規化後) のものだけ
        # SC' (#152) が新規植え付けの直前で止める (下の SC' ブロック。越境禁止は不変)。
        shadow_on = replacement_for is None and kind != "再開点"
        # 候補集合から最良 1 件を選ぶ (best_any / best_seed の二本立てはやめた: 縁を共有しない生きた種子が
        # 類似度で勝って、取り下げ中の W を隠すのを防ぐ)。
        #   生きた (双子に隠されていない) 種子: 類似度 >= 閾値 ∧ 縁を min(2, 小さい側の縁数) 語以上共有 (片側の縁が 0 語なら共有扱い、#104)。
        #   取り下げ中の種子 (shadow_on) と双子に隠された H: 類似度 >= 閾値のみ。これらは併合先でなく門番で、
        #   縁の共有を求めると縁の語の差し替えで人の取り下げ・双子照合を迂回できてしまう (HIGH の再発)。
        # 最良が W なら 取り下げ照合、H なら 双子照合、それ以外は D → R → reinforce。無ければ new。
        best_seed = None
        best_score = 0.0
        for seed in self.state["seeds"]:
            if (
                not _mergeable(seed)
                or (is_withdrawn(seed) and not shadow_on)
                # 門で倒した target は生きた種子への類似併合から外す。双子として隠された H は双子照合のために残す。
                or (seed is sp_excluded_target and shadows.get(id(seed)) is None)
            ):
                continue
            score = _merge_similarity(seed, content, grounded)
            if score < MERGE_SIMILARITY_THRESHOLD:
                continue
            guard = is_withdrawn(seed) or shadows.get(id(seed)) is not None
            if not guard and not _shares_condition(seed, grounded):
                continue
            if score > best_score:
                best_score = score
                best_seed = seed
        if best_seed is not None:
            if is_withdrawn(best_seed):  # shadow_on のときだけ候補に入る
                w_id = best_seed["id"]
                _note("取り下げ中の種子 #{} と同趣旨 (類似度 {:.2f}) のため植えない".format(w_id, best_score))
                log_entry["decision"] = result["decision"] = "取り下げ照合"
                log_entry["seed_id"] = w_id
                result["sansho"] = sansho_class
                result["seed"] = None
                result["is_new"] = None
                result["withdrawn_match"] = w_id
                return result
            # 順序: 取り下げ照合 → 双子照合 (#78) → D → R。
            twin = _twin_match(best_seed)
            if twin is not None:
                return twin
            # D (併合経路): user span 0 本の提案は user の逐語を持つ種子を強められない。#78 の双子照合と同じ扱い =
            # 植えず・強度を変えず記録だけ残す。人の置き換え (replacement_for) は必ず種子を植えるので対象外。
            if replacement_for is None and not proposal_has_user_span and _holds_user_span(best_seed):
                return _record_only(best_seed, _D_NOTE.format(best_seed["id"]), "user_span_match")
            # R (#105): 同 session で注入済みの種子を assistant だけが言い直した = 自己強化。記録のみ。
            if _echoes_injection(best_seed):
                return _record_only(best_seed, _R_NOTE.format(best_seed["id"]), "echo_match")
            return _reinforce(best_seed)

        # SC' (#152, spec 2026-11-10-request-span-crossing-design §7 / §7.1 / §7.2): 新規植え付けの直前。依他起 の提案 (kind は 決定・回避策・性向 のみ = 再開点・所在 を除く。所在 は値が span に在る §7.2。人の置き換えも除く) が、
        # 同 kind・同 project の生きた 圓成實 E と content が同一 (`sansho._n` + strip + casefold で等しい・空は当たりにしない) ∧ 縁を必要語数共有
        # (`_shares_condition`) なら植えず記録のみ (`settled_match` = E の id)。複数なら id 最小。門 SP1'' が new に倒した target は E にしない。
        # 順は 取り下げ照合 → 双子照合 → D → R → SC' → EC2'。target_id・提案の自称は読まない。類似度は使わない (値を 1 字替えた提案を止めないため、§7.1)。
        if sansho_class == "依他起" and replacement_for is None and kind in ("決定", "回避策", "性向"):
            sc_norm = sansho._n(content).strip().casefold() if isinstance(content, str) else ""
            sc_best = None
            if sc_norm:
                for seed in self.state["seeds"]:
                    if (
                        seed.get("sansho") != "圓成實"
                        or not is_live(seed)
                        or seed is sp_excluded_target
                        or _seed_kind(seed) != kind
                        or _seed_project(seed) != project
                    ):
                        continue
                    ec = seed.get("content")
                    if not isinstance(ec, str) or sansho._n(ec).strip().casefold() != sc_norm:
                        continue
                    if not _shares_condition(seed, grounded):
                        continue
                    if sc_best is None or seed["id"] < sc_best["id"]:
                        sc_best = seed
            if sc_best is not None:
                return _record_only(sc_best, _SC_NOTE.format(sc_best["id"]), "settled_match")

        # EC2' (#119, spec 2026-10-31-resume-point-and-kind-name-design §7, EC6 = §9; EC9 #124): 新規の直前。user の span が無い、または user の span がどれも 所在・性向 の
        # 形を担わず (疑の文を除いた残りで判定。依頼文・問いだけ)、assistant の span が接地した往復に注入済みの種子と縁を共有する (復唱) なら植えず記録のみ。
        # 順は 取り下げ照合 → 双子照合 → D → R → SC' → EC2'。人の置き換え・injected_by_turn なしでは効かない。user の span 0 本 (assistant だけ) の提案も
        # EC9 (#124, spec 2026-11-05-assistant-only-echo-new-design §8) で対象 (ec_user_spans が空 = (i) は成り立つ)。縁共有は min(2, 小さい側) のまま (例外 #148 (a): target_id が指す注入済みの種子 (action reinforce / supersede) だけ 1 語)。
        # 開いたまま = Q2 (往復の前に注入が無い提案は素通り)。
        if (
            injected_by_turn is not None
            and replacement_for is None
            and kind in ("所在", "性向")
        ):
            # (i) は疑の文を除いた残りで判定する (EC6, spec §9): 圓成實 の span ごとに窓の user の本文で切った残りを文ごとに渡す
            # (文ごと = `sansho._n` が CJK 隣接の空白を消すので、連結すると縁が文境を跨いで一致しうる)。
            ec_remainders_of = _span_remainders_fn(window)
            ec_user_spans = [
                piece
                for sp in decision["spans"]
                if sp.get("sansho") == "圓成實"
                for r in ec_remainders_of(sp)
                for piece in r.split("\n")
                if piece.strip()
            ]
            if not form_in_user_spans_for_kind(kind, ec_user_spans, grounded):
                # 接地した往復 = assistant の span (圓成實 でない span) の往復だけ (spec §5.3/§7「その span が接地した往復」)
                indexes = [sp.get("turn_index") for sp in decision["spans"] if sp.get("sansho") != "圓成實"]
                if indexes and all(i in injected_by_turn for i in indexes):
                    injected_ids = set()
                    for i in indexes:
                        injected_ids.update(injected_by_turn[i])
                    # #148 (a): 提案が target_id で名指した (action reinforce / supersede) 注入済みの種子だけ、要る共有語数を 1 にする。
                    relaxed_id = (
                        target["id"]
                        if target is not None and proposal.get("action") in ("reinforce", "supersede")
                        else None
                    )
                    default_hits = []
                    relaxed_hit = None
                    for sid in injected_ids:
                        seed = self._find_seed(sid)
                        if seed is None or not is_live(seed):
                            continue
                        if _shares_conditions_strict(seed, grounded):
                            default_hits.append(seed)
                        elif relaxed_id is not None and seed["id"] == relaxed_id and _shares_conditions_strict(seed, grounded, 1):
                            relaxed_hit = seed
                    # 従来の 2 語規則で通る種子を優先 (変種の注記は (a) で初めて通った提案だけを数える、spec §11 観測)。
                    if default_hits:
                        hit = min(default_hits, key=lambda sd: sd["id"])
                        return _record_only(hit, _EC2_NOTE.format(hit["id"]), "echo_match")
                    if relaxed_hit is not None:
                        return _record_only(relaxed_hit, _EC2_TARGET_NOTE.format(relaxed_hit["id"]), "echo_match")

        new_seed = {
            "id": self.state["next_id"],
            "content": content,
            "strength": gained,
            "conditions": list(grounded),
            "polarity": polarity,
            "sansho": sansho_class,
            "kind": kind,
            "project": project,
            "superseded_by": None,
            "evidences": evidence_entries,
            "history": [],
            "planted_at": life,
            "last_fired": None,
            "fire_count": 0,
            "miss_streak": 0,
        }
        self.state["next_id"] += 1
        self.state["seeds"].append(new_seed)
        log_entry["seed_id"] = new_seed["id"]
        result["seed"] = copy.deepcopy(new_seed)
        result["is_new"] = True
        return result

    def _append_quarantine(self, entry):
        """遍計所執の観測記録を 1 行足す (上限超過は古い順に落とす: 記憶機能設計書§3.4「quarantine(上限付き)」)。"""
        self.state["quarantine"].append(entry)
        self._quarantine_pending.append(entry)  # 保存時に直列化する
        if len(self.state["quarantine"]) > QUARANTINE_MAX:
            self.state["quarantine"] = self.state["quarantine"][-QUARANTINE_MAX:]

    def list_seeds(self):
        """全種子を strength 降順で返す (deep copy のリスト)。"""
        return sorted(
            (copy.deepcopy(s) for s in self.state["seeds"]), key=lambda s: s["strength"], reverse=True
        )

    def get_by_ids(self, ids):
        id_set = set(ids)
        return [copy.deepcopy(s) for s in self.state["seeds"] if s["id"] in id_set]

    def list_quarantine(self):
        """遍計所執(非接地)として記帳された観測用リストを返す (deep copy のリスト)。"""
        return [copy.deepcopy(q) for q in self.state["quarantine"]]

    def quarantine_total(self):
        """quarantine へこれまでに記帳された累計件数。

        保持上限 (QUARANTINE_MAX) による evict とは無関係に増え続ける「累計」で、
        `list_quarantine()` の件数 (= 保持中の件数、上限で頭打ち) とは別の数字。
        next_quarantine_id は append-only な連番でカウンタ列を持たずに導出できる。
        """
        return self.state["next_quarantine_id"] - 1

    def append_imprint_log(self, entry):
        """熏習の決定ログへ 1 行追記する (イベントログ意味論: 追記のみ・削除なし)。

        バッファに積むだけ (保存時に直列化する: `log_entry` は append 後にも `_note` / `seed_id` で変わる)。
        保存済みの行は読み込まない (遅延: 読む動詞だけが reader 経由で読む)。呼び出し側の契約は
        「recall は書かない / imprint は提案 1 件につき 1 行足す」だけ。
        """
        self._imprint_log_pending.append(entry)

    def _imprint_log_persisted(self, n=None, seed_id=None):
        """保存済みの imprint_log 行 (古い順の dict のリスト)。bind されていなければ空。"""
        if self._imprint_log_reader is None:
            return []
        return self._imprint_log_reader(n=n, seed_id=seed_id)

    def imprint_log_rows_for_seed(self, seed_id):
        """`seed_id` 欄がこの種子の imprint_log 行 (古い順の deep copy)。欄の無い古い行は結ばない。"""
        rows = self._imprint_log_persisted(seed_id=seed_id) + self._imprint_log_pending
        return [
            copy.deepcopy(row)
            for row in rows
            if isinstance(row, dict) and _is_int(row.get("seed_id")) and row.get("seed_id") == seed_id
        ]

    def list_imprint_log(self, n=None):
        """熏習の決定ログを返す (deep copy のリスト)。n を指定すると直近 n 件。

        各行は turn/proposal という nested dict を持つ。呼び出し側がそれを書き換えても
        内部状態には影響しない (copy.deepcopy で完全に独立したコピーにする)。
        """
        if n is not None and _is_int(n) and n > 0:
            persisted = self._imprint_log_persisted(n=n)
        else:
            persisted = self._imprint_log_persisted()
        log = persisted + self._imprint_log_pending
        if n is not None:
            log = log[-n:]
        return copy.deepcopy(log)


# --- 併合類似度 (記憶機能設計書§3.5): 文字2-gram Jaccard の写し。標準ライブラリのみ ---
def _char_ngrams(text, n=2):
    """文字 n-gram 集合。NFKC 正規化 + 空白除去した文字列を n 文字ずつスライドして集合化する。

    n 未満の長さはそのまま1要素とする。空文字は空集合。
    """
    s = "".join(unicodedata.normalize("NFKC", text or "").split())
    if not s:
        return frozenset()
    if len(s) < n:
        return frozenset({s})
    return frozenset(s[i : i + n] for i in range(len(s) - n + 1))


def _jaccard(a, b):
    """|a∩b| / |a∪b|。両方空集合なら 0.0 (証跡が無いものは似ているとみなさない)。"""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _cond_key(c):
    """#147: 縁の共有判定の鍵。`sansho._n(c).casefold()` の末尾が `/` で長さ 2 以上なら `/` を 1 つ剥がす
    (`/` 単独は剥がさない、`//` は `/` になる)。鍵が空文字なら呼び側は数えない。"""
    k = sansho._n(c).casefold()
    if len(k) >= 2 and k.endswith("/"):
        k = k[:-1]
    return k


def _shares_condition(seed, conditions, min_shared=None):
    """#104: 併合/reinforce は縁を min(`MERGE_SHARED_CONDITIONS`, 小さい側の縁の数) 語以上共有する種子にだけ
    (語の正規化は #147 以降 `_cond_key` (下記)・空の鍵は除外)。どちらかの縁が 0 語なら判定材料が
    無いので止めない (従来どおり)。1 語だけの共有 (例: 却下側の "Python") では併合させない (x9_3s)。
    `min_shared` は min(need, len(a), len(b)) の need を `MERGE_SHARED_CONDITIONS` の代わりに置く (EC2' が target_id の種子にだけ渡す、#148 (a))。

    #147: 共有の鍵は `_cond_key` = `sansho._n` + casefold + 末尾の `/` を 1 つ剥がしたもの (`tests/` = `tests`、`Pi` = `pi/`)。
    比較のためだけで、保存値 (種子の conditions・提案の縁・imprint_log) は逐語のまま。部分一致・他の記号の剥がしは使わない
    (`L` と `L'`、`test` と `tests` は別。`L`/`L'` の形検査は別の判定)。群・横断 supersede は完全一致のまま、`_merge_similarity` の縁 Jaccard は従来どおり小文字化のみ。"""
    a = set(k for k in (_cond_key(c) for c in seed_conditions(seed) if isinstance(c, str)) if k)
    b = set(k for k in (_cond_key(c) for c in (conditions if isinstance(conditions, list) else []) if isinstance(c, str)) if k)
    if not a or not b:
        return True
    need = MERGE_SHARED_CONDITIONS if min_shared is None else min_shared
    return len(a & b) >= min(need, len(a), len(b))


def _shares_conditions_strict(seed, conditions, min_shared=None):
    """EC2' (#119): `_shares_condition` と同じ基準 (min(`MERGE_SHARED_CONDITIONS`, 小さい側の縁の数) 語以上) だが、
    どちらかの縁が 0 語なら偽 (判定材料が無いのを「共有」と数えない。併合の「止めない」側の既定とは逆向き)。
    `min_shared` は min(need, len(a), len(b)) の need を `MERGE_SHARED_CONDITIONS` の代わりに置く (EC2' が target_id の種子にだけ渡す、#148 (a))。"""
    if not [c for c in seed_conditions(seed) if c] or not [c for c in (conditions if isinstance(conditions, list) else []) if isinstance(c, str) and c]:
        return False
    return _shares_condition(seed, conditions, min_shared)


def _merge_similarity(seed, content, conditions):
    """併合類似度 (§3.5) = content の文字2-gram Jaccard と縁 Jaccard の max。"""
    char_sim = _jaccard(_char_ngrams(seed.get("content", "")), _char_ngrams(content))
    cond_sim = _jaccard(
        set(c.lower() for c in seed_conditions(seed) if c),
        set(c.lower() for c in (conditions if isinstance(conditions, list) else []) if isinstance(c, str) and c),
    )
    return max(char_sim, cond_sim)
