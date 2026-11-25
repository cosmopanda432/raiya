"""longmemeval_stage1.py -- LongMemEval を使った想起の「段 1」測定 (LLM 不使用・決定論)

問いごとに、根拠になる発話 (`has_answer` の turn) から作った「正解の提案」を `MemoryEngine.imprint` に渡し、質問文で
`explain_recall` を引いて、根拠の種子がどこで落ちるか (門 / 発芽 ① / 索引 ③ / 容量の打ち切り) を問いごと・
question_type ごとに数える。点数を出すのが目的ではない。想起の作りに穴があるかを見るための数え方 (候補の洗い出し)。

使い方:
    python3 scripts/longmemeval_stage1.py --data PATH/longmemeval_s_cleaned.json --out DIR [--name FILE]
        [--limit-per-type N] [--distractor-per-type N] [--edges A,B]

- データは引数で受け取る。リポジトリへ写さない・ネットワークに出ない。結果の集計 JSON も既定ではリポジトリの外
  (`--out` 必須) へ出し、`receipts/` には書かない。
- 実 LLM は呼ばない (`propose` / `consolidate` を使わず `raiya.backend` を import しない)。`MemoryEngine` は公開の動詞
  (`record_turn` / `imprint` / `explain_recall`) だけを使う。状態は問いごと・腕ごとに `tempfile.mkdtemp` の下に作り、
  自分で作った dir だけを消す。`RAIYA_CONFIG=""` を強いる (利用者の config.yaml を読まない)。
- ファイル名が test*.py でないので `unittest discover` には拾われない。

腕 (集計 JSON の `arms`):
    縁の選び方 = A (根拠の turn と質問文の両方に在る語) / B (根拠の turn と answer の両方に在る語。質問文は見ない)
    設定      = a (kind=決定・既定) / b (kind=性向・既定) / c (kind=性向・RAIYA_INJECT_DISPOSITIONS=1)
    腕の名前  = "A-a" など。`--distractor-per-type N` を付けると、型ごとの先頭 N 問に「邪魔の種子」(その問いの user の
                turn のうち has_answer でないもの全部を、同じ規則で種子にしたもの) を足した腕 "A-a+distract" 等も走る。
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import sys
import tempfile
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (BASE_DIR,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_NAME = "stage1.json"
SCHEMA = "longmemeval-stage1/1"

EDGE_ARMS = ("A", "B")
# 設定の腕: (名前, kind, RAIYA_INJECT_DISPOSITIONS の値)
CONFIG_ARMS = (("a", "決定", "0"), ("b", "性向", "0"), ("c", "性向", "1"))
DISTRACT_SUFFIX = "+distract"

MAX_CONDITIONS = 8          # 提案 1 件の縁の数の上限 (turn の先頭に近い順に取る)
MIN_WORD_CHARS = 3          # 縁にする語の最短字数 (alaya._input_runs の ASCII 3 字以上に揃える)
HOOK_TRUNCATE_LIMIT = 8000  # raiya.hooks.core.TRUNCATE_LIMIT の写し (hook はこの字数で本文を切る。本測定は切らない)

CLASSES = ("no_marker", "assistant_only", "abs", "merged_into_distractor", "all_rejected", "planted_not_injected", "injected", "failed")
CLASSIFICATION_ORDER = (
    "failed (1 問の例外 = fail:<Class>) → no_marker (has_answer の turn が 0 本) → assistant_only (根拠が assistant だけ) "
    "→ abs (question_id が _abs で終わる) → merged_into_distractor (根拠の提案が自分で種子を植えず、先に植わった邪魔の種子へ併合された問い。邪魔の腕だけ) → all_rejected (根拠の提案が 1 本も種子にならない。縁 0 語の no_condition を含む) "
    "→ planted_not_injected (植わったが ① にも ③ にも出ない) → injected (① か ③ に出た)。先に当たったものが問いの分類"
)

# 縁にしない英語の機能語・疑問語 (測り手の規則。語は小文字で比べる)。結果を左右するので meta に全文を書く。
STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren as at be because been before being below between both but
by can cannot could couldn did didn do does doesn doing don down during each few for from further get gets got had hadn has
hasn have haven having he her here hers herself him himself his how i if in into is isn it its itself just let like ll me
might more most must my myself no nor not now of off on once only or other our ours ourselves out over own re same shall she
should shouldn so some such than that the their theirs them themselves then there these they this those through to too under
until up us very was wasn we were weren what when where which while who whom why will with won would wouldn you your yours
yourself yourselves ve d s t m yes okay sure please thanks thank
""".split())

_WORD_RE = re.compile(r"[A-Za-z0-9]+")

REQUIRED_QUESTION_KEYS = ("question_id", "question_type", "question", "answer", "haystack_session_ids", "haystack_sessions")
TIMING_KEYS = ("meta.generated_at", "timing")


class DataError(Exception):
    """データの読み込み・検証の失敗 (1 行の理由を持つ)。"""


# --- データの読み込みと検証 (出力より先に全部済ませる) ---------------------------------------------


def load_data(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise DataError("データを読めません: {} ({})".format(path, e.__class__.__name__))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DataError("データが JSON として壊れています: {}".format(path))
    validate(data)
    return data, hashlib.sha256(raw).hexdigest(), len(raw)


def validate(data):
    if not isinstance(data, list) or not data:
        raise DataError("データは問いの非空の list である必要があります")
    seen = set()
    for qi, q in enumerate(data):
        if not isinstance(q, dict):
            raise DataError("問い #{} が dict でありません".format(qi))
        for key in REQUIRED_QUESTION_KEYS:
            if key not in q:
                raise DataError("問い #{} に必須の欄 {} がありません".format(qi, key))
        for key in ("question_id", "question_type", "question"):
            if not isinstance(q[key], str):
                raise DataError("問い #{} の {} が str でありません".format(qi, key))
        if isinstance(q["answer"], bool) or not isinstance(q["answer"], (str, int, float)):
            raise DataError("問い #{} の answer が str / 数でありません".format(qi))
        if q["question_id"] in seen:
            raise DataError("question_id {} が重複しています".format(q["question_id"]))
        seen.add(q["question_id"])
        ids, sessions = q["haystack_session_ids"], q["haystack_sessions"]
        if not isinstance(ids, list) or not isinstance(sessions, list) or len(ids) != len(sessions):
            raise DataError("問い {} の haystack_session_ids と haystack_sessions が揃っていません".format(q["question_id"]))
        if not all(isinstance(i, str) for i in ids):
            raise DataError("問い {} の haystack_session_ids に str でないものがあります".format(q["question_id"]))
        asids = q.get("answer_session_ids")
        if asids is not None and not (isinstance(asids, list) and all(isinstance(i, str) for i in asids)):
            raise DataError("問い {} の answer_session_ids が str の list でありません".format(q["question_id"]))
        for si, session in enumerate(sessions):
            if not isinstance(session, list):
                raise DataError("問い {} のセッション #{} が list でありません".format(q["question_id"], si))
            for ti, turn in enumerate(session):
                if not isinstance(turn, dict) or "role" not in turn or "content" not in turn:
                    raise DataError("問い {} のセッション #{} の turn #{} に role / content がありません".format(
                        q["question_id"], si, ti))
                if turn["role"] not in ("user", "assistant") or not isinstance(turn["content"], str):
                    raise DataError("問い {} のセッション #{} の turn #{} の role / content の型が違います".format(
                        q["question_id"], si, ti))
                if "has_answer" in turn and not isinstance(turn["has_answer"], bool):
                    raise DataError("問い {} のセッション #{} の turn #{} の has_answer が bool でありません".format(
                        q["question_id"], si, ti))


# --- 往復の組み方 ----------------------------------------------------------------------------------


def build_round_trips(session_ids, sessions):
    """セッションごとに turn を往復に組む。user の turn と直後の assistant の turn で 1 往復。後ろに assistant が無い user は
    assistant_text=""、先頭などで assistant が単独なら user_text="" の往復。戻り値: (往復の list, 形の数え)。
    往復 = {session_id, user, assistant, user_ha, assistant_ha}。形の数え = sessions / first_assistant / non_alternating
    (role が u,a,u,a… の交互でないセッション。先頭が assistant のものも数える) / adjacent_same_role (同じ role が隣り合うセッション) / empty_turns (本文が空白だけの turn)。"""
    rts = []
    shape = {"sessions": 0, "first_assistant": 0, "non_alternating": 0, "adjacent_same_role": 0, "empty_turns": 0}
    for sid, session in zip(session_ids, sessions):
        shape["sessions"] += 1
        if session and session[0]["role"] != "user":
            shape["first_assistant"] += 1
        if any(t["role"] != ("user" if i % 2 == 0 else "assistant") for i, t in enumerate(session)):
            shape["non_alternating"] += 1
        if any(a["role"] == b["role"] for a, b in zip(session, session[1:])):
            shape["adjacent_same_role"] += 1
        shape["empty_turns"] += sum(1 for t in session if not t["content"].strip())
        i = 0
        while i < len(session):
            t = session[i]
            if t["role"] == "user":
                nxt = session[i + 1] if i + 1 < len(session) and session[i + 1]["role"] == "assistant" else None
                rts.append({
                    "session_id": sid, "user": t["content"], "assistant": nxt["content"] if nxt else "",
                    "user_ha": t.get("has_answer") is True, "assistant_ha": bool(nxt) and nxt.get("has_answer") is True,
                })
                i += 2 if nxt else 1
            else:
                rts.append({
                    "session_id": sid, "user": "", "assistant": t["content"],
                    "user_ha": False, "assistant_ha": t.get("has_answer") is True,
                })
                i += 1
    return rts, shape


# --- 縁の選び方 ------------------------------------------------------------------------------------


def _words(text):
    """text の語 (英数字の連なり) を、出てきた順・小文字での重複なしで [(小文字, 原表記)] にする。"""
    seen, out = set(), []
    for w in _WORD_RE.findall(text or ""):
        low = w.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append((low, w))
    return out


def _usable(low):
    return len(low) >= MIN_WORD_CHARS and not low.isdigit() and low not in STOPWORDS


def choose_conditions(arm, turn_text, question, answer):
    """提案の縁を決める。A = turn と質問文の両方に在る語、B = turn と answer の両方に在る語 (B は質問文を読まない)。
    語 = 英数字の連なり。3 字以上・数字だけでない・STOPWORDS でない。比べるのは小文字、渡すのは turn の表記 (門は大文字小文字を
    区別して接地を見る)。並びは turn に出てきた順で、先頭から MAX_CONDITIONS 語まで。"""
    if arm == "A":
        other = {low for low, _ in _words(question)}
    elif arm == "B":
        other = {low for low, _ in _words(str(answer))}
    else:
        raise ValueError("縁の腕は A か B")
    picked = [orig for low, orig in _words(turn_text) if low in other and _usable(low)]
    return picked[:MAX_CONDITIONS]


# --- 分類と ① の導出 -------------------------------------------------------------------------------


def classify(has_marker, has_user_marker, is_abs, planted, injected, failed=False, merged_only=False):
    """問いの分類 (CLASSIFICATION_ORDER の順に先に当たったもの)。"""
    if failed:
        return "failed"
    if not has_marker:
        return "no_marker"
    if not has_user_marker:
        return "assistant_only"
    if is_abs:
        return "abs"
    if not planted:
        return "merged_into_distractor" if merged_only else "all_rejected"
    if not injected:
        return "planted_not_injected"
    return "injected"


def rendered_first_circle(seeds, cut_germinated, limit=None):
    """① に描かれた種子の id。発芽した種子 (`explain_recall` の `seeds` = 発芽の順) を class だけで安定に並べ、先頭 `limit` 件
    (= round(ego_weight × MAX_NARRATIVE_SEEDS)) を取り、`circles.cut.germinated` の分を末尾から除く。"""
    from raiya import sansho

    if limit is None:
        limit = default_first_circle_limit()
    ordered = sorted(seeds, key=lambda s: sansho.sansho_sort_key(s)[0])[:limit]
    keep = max(0, len(ordered) - cut_germinated)
    return [s["id"] for s in ordered[:keep]]


def shortened_evidence(circles, ev_ids):
    """① で縮めて描かれた種子のうち根拠の種子 (`ev_ids`) の id: `{"no_summary": [...], "name_only": [...]}` (純粋・T3 6b)。
    `circles` に `shortened` が無ければ両方 `[]`。"""
    shortened = circles.get("shortened") or {}
    keep = set(ev_ids)
    return {form: [i for i in shortened.get(form, []) if i in keep] for form in ("no_summary", "name_only")}


def default_first_circle_limit():
    """新しい状態の ego_weight (= INITIAL_EGO_WEIGHT) での ① の描画件数。状態の中身は読まない (定数から)。"""
    from raiya import manas

    return max(1, round(manas.INITIAL_EGO_WEIGHT * manas.MAX_NARRATIVE_SEEDS))


# --- 1 問ぶんの測定 --------------------------------------------------------------------------------


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _span_of(role, text):
    """ゲートが実際に読む本文 (engine._imprint_gate_turn と同じ剥がし) から span を作る。"""
    from raiya.ledger import strip_pasted, strip_private

    return strip_private(text) if role == "assistant" else strip_pasted(strip_private(text))


CONTENT_RULES = ("span", "head120")
HEAD120_CHARS = 120


def make_content(span, rule):
    """提案の content を span から作る。"span" = span そのまま。"head120" = 空白を畳んだ最初の 1 文 (最大 120 字)。"""
    if rule == "span":
        return span
    if rule == "head120":
        flat = " ".join(span.split())
        m = re.search(r"[.?!](?=\s|$)", flat)
        first = flat[:m.end()] if m else flat
        return first[:HEAD120_CHARS]
    raise ValueError("content rule must be one of {}: {!r}".format(CONTENT_RULES, rule))


def _reason_key(reason):
    return re.sub(r"\d+", "N", str(reason or ""))[:160]


def _plan(rts, with_distractors):
    """imprint に出す往復と提案の予定 (haystack の順)。[(rt の添字, [(role, 'evidence'|'distractor')])]"""
    plan = []
    for i, rt in enumerate(rts):
        items = []
        if rt["user_ha"]:
            items.append(("user", "evidence"))
        elif with_distractors and rt["user"].strip():
            items.append(("user", "distractor"))
        if rt["assistant_ha"]:
            items.append(("assistant", "evidence"))
        if items:
            plan.append((i, items))
    return plan


def _build_state(q, rts, plan, edge, kind, content_rule="span"):
    """腕 (edge, kind) の状態を作って提案を植える。戻り値: (engine, tmp, proposals の記録, 邪魔の数え)。呼び側が tmp を消す。"""
    from raiya.engine import MemoryEngine
    from raiya.ledger import LedgerTurn

    tmp = tempfile.mkdtemp(prefix="raiya_lme_state_")
    try:
        engine = MemoryEngine(state_dir=tmp)
        records = []
        origin = {}   # 種子 id -> その種子を新しく植えた提案の目的 (evidence / distractor)
        dist = {"candidates": 0, "no_condition": 0, "proposed": 0, "new": 0, "merged": 0, "recorded_only": 0, "rejected": 0}
        for rt_index, items in plan:
            rt = rts[rt_index]
            lid = engine.record_turn(rt["user"], rt["assistant"], session_id=rt["session_id"])["ledger_id"]
            window = [LedgerTurn(user_text=rt["user"], assistant_text=rt["assistant"], id=lid, life=engine.life)]
            proposals, metas = [], []
            for role, purpose in items:
                span = _span_of(role, rt[role])
                conds = choose_conditions(edge, span, q["question"], q["answer"]) if span.strip() else []
                meta = {"role": role, "purpose": purpose, "conditions": conds, "span_chars": len(span),
                        "span_has_question_mark": ("?" in span) or ("？" in span),
                        "_span_head": span if purpose == "evidence" else None}   # 独立の観測用 (出力には出さない)
                if purpose == "distractor":
                    dist["candidates"] += 1
                if not span.strip():
                    meta.update(decision="no_span", reason="根拠の本文が剥がした後に空", kind=None, is_new=None, seed_id=None)
                    records.append(meta)
                    continue
                if not conds:
                    meta.update(decision="no_condition", reason="縁が 0 語 (測り手の規則で空)", kind=None, is_new=None, seed_id=None)
                    if purpose == "distractor":
                        dist["no_condition"] += 1
                    records.append(meta)
                    continue
                proposals.append({
                    "content": make_content(span, content_rule), "conditions": conds, "polarity": "無記", "kind": kind, "action": "new",
                    "evidence": [{"source": role, "span": span}],
                })
                metas.append(meta)
                records.append(meta)
            if not proposals:
                continue
            results = engine.imprint(window, proposals, project=None)
            for meta, res in zip(metas, results):
                seed = res.get("seed")
                meta.update(
                    decision=res.get("decision"), reason=res.get("reason"), kind=res.get("kind"),
                    is_new=res.get("is_new"), seed_id=seed["id"] if isinstance(seed, dict) else None,
                )
                if meta["seed_id"] is not None:
                    if meta["is_new"]:
                        origin[meta["seed_id"]] = meta["purpose"]
                    meta["seed_origin"] = origin.get(meta["seed_id"])
                if meta["purpose"] == "distractor":
                    dist["proposed"] += 1
                    if meta["seed_id"] is not None:
                        dist["new" if meta["is_new"] else "merged"] += 1
                    elif meta["decision"] in ("reject", "遍計所執"):
                        dist["rejected"] += 1
                    else:
                        dist["recorded_only"] += 1
        return engine, tmp, records, dist
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _line_lengths(narrative):
    return [len(line) for line in narrative.split("\n") if line.startswith("- ")]


def _measure(engine, q, records, inject_env):
    """explain_recall を引いて、根拠の種子の行方を数える (状態を変えない)。"""
    with _env(RAIYA_INJECT_DISPOSITIONS=inject_env):
        out = engine.explain_recall(q["question"], project=None)
    ev_records = [r for r in records if r["purpose"] == "evidence"]
    ev_ids, merged_ids = [], []
    for r in ev_records:
        sid = r.get("seed_id")
        if sid is None:
            continue
        if r.get("seed_origin") == "distractor":   # 先に植わった邪魔の種子へ併合された: 描かれても根拠の文ではない
            if sid not in merged_ids:
                merged_ids.append(sid)
        elif sid not in ev_ids:
            ev_ids.append(sid)
    ev_set = set(ev_ids)
    germinated_ids = [g["id"] for g in out["germinated"] if g["id"] in ev_set]
    rendered1_all = rendered_first_circle(out["seeds"], out["circles"]["cut"]["germinated"])
    rendered1 = [i for i in rendered1_all if i in ev_set]
    rendered3 = [{"seed_id": s["id"], "rank": rank} for rank, s in enumerate(out["index"]) if s["id"] in ev_set]
    g_reason = {str(r["id"]): r["reason"] for r in out["germination_rejected"] if r["id"] in ev_set}
    i_reason = {str(r["id"]): r["reason"] for r in out["index_rejected"] if r["id"] in ev_set}
    injected_ids = set(rendered1) | {r["seed_id"] for r in rendered3}
    lost = [i for i in ev_ids if i not in injected_ids]
    first_loss = None
    if ev_ids and lost:
        sid = lost[0]
        if sid in germinated_ids:
            cut = out["circles"]["cut"]["germinated"]
            first_loss = {"seed_id": sid, "stage": "first_circle_cut",
                          "reason": "発芽したが ① に描かれない ({})".format(
                              "注入文の容量で切れた" if cut > 0 else "① の件数の上限で切れた")}
        else:
            first_loss = {"seed_id": sid, "stage": "germination_then_index",
                          "reason": "発芽: {} / 索引: {}".format(g_reason.get(str(sid), "?"), i_reason.get(str(sid), "載っていない"))}
    elif not ev_ids and merged_ids:
        first_loss = {"seed_id": merged_ids[0], "stage": "merged_into_distractor",
                      "reason": "根拠の提案が先に植わった邪魔の種子へ併合された"}
    elif not ev_ids and ev_records:
        first_loss = {"seed_id": None, "stage": "gate", "reason": ev_records[0].get("reason")}
    # 独立の観測: 描かれた ①/③ に根拠の逐語 (先頭 30 字・空白を畳んだもの) があるか。導出の取り違えの検出用
    flat = " ".join(out["narrative"].split())
    heads = [" ".join(r_span.split())[:30] for r_span in (r.get("_span_head") for r in ev_records) if r_span]
    has_text = any(h and h in flat for h in heads)
    return {
        "seed_ids": ev_ids,
        "merged_into_distractor_ids": merged_ids,
        "germinated": bool(germinated_ids),
        "germinated_ids": germinated_ids,
        "rendered_first_circle": rendered1,
        "rendered_index": rendered3,
        "injected": bool(injected_ids),
        "germination_reasons": g_reason,
        "index_reasons": i_reason,
        "first_loss": first_loss,
        "circles": {"cut": dict(out["circles"]["cut"]), "shortened": dict(out["circles"].get("shortened") or {}),
                    "chars": out["circles"]["chars"], "max_chars": out["circles"]["max_chars"]},
        "shortened_evidence": shortened_evidence(out["circles"], ev_ids),
        "fired_total": len(out["seeds"]),
        "index_rendered_total": len(out["index"]),
        "narrative_has_evidence_text": has_text,
        "line_chars": _line_lengths(out["narrative"]),
    }


def _public_proposals(records):
    keys = ("role", "purpose", "decision", "reason", "kind", "is_new", "seed_id", "conditions", "span_chars",
            "span_has_question_mark", "seed_origin")
    return [{k: r.get(k) for k in keys} for r in records if r["purpose"] == "evidence"]


def _distractor_proposals(records):
    """邪魔の候補ごとの縁と判定 (縁の規則が根拠と同じかを後から照合できるように出す)。span の本文は出さない。"""
    return [{"conditions": r["conditions"], "decision": r["decision"], "seed_id": r.get("seed_id")}
            for r in records if r["purpose"] == "distractor"]


def run_question(q, edges, distract, content_rule="span"):
    """1 問を全腕で測って行 (dict) を返す。`distract` なら邪魔の腕も走らせる。例外は呼び側 (run_all) が受ける。"""
    rts, shape = build_round_trips(q["haystack_session_ids"], q["haystack_sessions"])
    turns = [t for s in q["haystack_sessions"] for t in s]
    ev_turns = {"user": 0, "assistant": 0}
    for t in turns:
        if t.get("has_answer") is True:
            ev_turns[t["role"]] += 1
    is_abs = q["question_id"].endswith("_abs")
    has_marker = ev_turns["user"] + ev_turns["assistant"] > 0
    has_user_marker = ev_turns["user"] > 0
    row = {
        "question_id": q["question_id"], "question_type": q["question_type"], "is_abs": is_abs,
        "evidence_turns": ev_turns, "user_turns": sum(1 for t in turns if t["role"] == "user"),
        "round_trips": len(rts),
        "evidence_turn_chars": sorted(len(t["content"]) for t in turns if t.get("has_answer") is True),
        "arms": {},
    }
    timing = {}
    modes = [False] + ([True] if distract else [])
    for with_d in modes:
        plan = _plan(rts, with_d)
        for edge in edges:
            for kind in ("決定", "性向"):
                t0 = time.perf_counter()
                engine, tmp, records, dist = _build_state(q, rts, plan, edge, kind, content_rule)
                try:
                    for cname, ckind, env in CONFIG_ARMS:
                        if ckind != kind:
                            continue
                        arm = "{}-{}{}".format(edge, cname, DISTRACT_SUFFIX if with_d else "")
                        m = _measure(engine, q, records, env)
                        planted = bool(m["seed_ids"])
                        arm_row = {
                            "class": classify(has_marker, has_user_marker, is_abs, planted, m["injected"],
                                              merged_only=bool(m["merged_into_distractor_ids"])),
                            "proposals": _public_proposals(records),
                        }
                        arm_row.update(m)
                        if with_d:
                            arm_row["distractors"] = dict(dist)
                            arm_row["distractor_proposals"] = _distractor_proposals(records)
                        row["arms"][arm] = arm_row
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                timing["{}-{}{}".format(edge, kind, DISTRACT_SUFFIX if with_d else "")] = round(time.perf_counter() - t0, 4)
    return row, shape, timing


def _failed_row(q, exc, edges, distract):
    is_abs = q["question_id"].endswith("_abs")
    arms = {}
    for edge in edges:
        for cname, _, _ in CONFIG_ARMS:
            for suffix in ([""] + ([DISTRACT_SUFFIX] if distract else [])):
                arms["{}-{}{}".format(edge, cname, suffix)] = {"class": "failed", "error": "fail:{}".format(exc.__class__.__name__)}
    return {"question_id": q["question_id"], "question_type": q["question_type"], "is_abs": is_abs,
            "error": "fail:{}".format(exc.__class__.__name__), "arms": arms}


# --- 集計 ------------------------------------------------------------------------------------------


def _dist(values):
    values = sorted(values)
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p90": None, "max": None}
    q = (lambda p: values[min(len(values) - 1, int(p * (len(values) - 1) + 0.5))])
    return {"n": len(values), "mean": round(statistics.fmean(values), 2), "p50": q(0.5), "p90": q(0.9), "max": values[-1]}


def _summ_arm(rows, arm):
    sel = [r["arms"][arm] for r in rows if arm in r["arms"]]
    classes = {c: 0 for c in CLASSES}
    for a in sel:
        classes[a["class"]] += 1
    ok = [a for a in sel if a["class"] != "failed"]
    decisions, reasons, kinds = {}, {}, {}
    q_rejects = 0
    no_cond_q = 0
    for a in ok:
        props = a["proposals"]
        if props and all(p["decision"] == "no_condition" for p in props):
            no_cond_q += 1
        for p in props:
            decisions[p["decision"]] = decisions.get(p["decision"], 0) + 1
            if p["decision"] == "reject":
                key = _reason_key(p["reason"])
                reasons[key] = reasons.get(key, 0) + 1
                q_rejects += 1 if p["span_has_question_mark"] else 0
            if p["kind"] is not None:
                k = "{}:{}".format(p["role"], p["kind"])
                kinds[k] = kinds.get(k, 0) + 1
    cut = {name: sum(1 for a in ok if a["circles"]["cut"].get(name, 0) > 0)
           for name in ("germinated", "siblings", "index", "global")}
    out = {
        "questions": len(sel),
        "classes": classes,
        "no_condition_questions": no_cond_q,
        "proposal_decisions": dict(sorted(decisions.items())),
        "reject_reasons": dict(sorted(reasons.items())),
        "reject_with_question_mark_span": q_rejects,
        "proposal_kinds_after_reading": dict(sorted(kinds.items())),
        "evidence_merged_into_distractor_questions": sum(1 for a in ok if a["merged_into_distractor_ids"]),
        "merged_with_evidence_text_questions": sum(
            1 for a in ok if a["class"] == "merged_into_distractor" and a["narrative_has_evidence_text"]),
        "germinated_questions": sum(1 for a in ok if a["germinated"]),
        "rendered_first_circle_questions": sum(1 for a in ok if a["rendered_first_circle"]),
        "rendered_index_questions": sum(1 for a in ok if a["rendered_index"]),
        "injected_questions": sum(1 for a in ok if a["injected"]),
        "germinated_but_not_rendered_questions": sum(
            1 for a in ok if set(a["germinated_ids"]) - set(a["rendered_first_circle"])),
        "abs_questions_with_injection": sum(1 for r in rows if arm in r["arms"] and r["is_abs"]
                                            and r["arms"][arm].get("injected")),
        "cut_questions": cut,
        "first_circle_no_summary_evidence_questions": sum(
            1 for a in ok if a["shortened_evidence"]["no_summary"]),
        "first_circle_name_only_evidence_questions": sum(
            1 for a in ok if a["shortened_evidence"]["name_only"]),
        "injected_without_evidence_text_in_narrative": sum(
            1 for a in ok if a["injected"] and not a["narrative_has_evidence_text"]),
        "chars": _dist([a["circles"]["chars"] for a in ok]),
        "line_chars": _dist([n for a in ok for n in a["line_chars"]]),
        "first_circle_lines": _dist([len(a["rendered_first_circle"]) for a in ok]),
        "first_loss_reasons": {},
    }
    fl = {}
    for a in ok:
        f = a.get("first_loss")
        if f:
            key = "{}: {}".format(f["stage"], _reason_key(f["reason"]))
            fl[key] = fl.get(key, 0) + 1
    out["first_loss_reasons"] = dict(sorted(fl.items()))
    dsel = [a["distractors"] for a in ok if "distractors" in a]
    if dsel:
        out["distractors"] = {k: _dist([d[k] for d in dsel]) for k in dsel[0]}
    return out


def summarize(rows, shapes, answer_session_mismatch):
    arms = sorted({a for r in rows for a in r["arms"]})
    types = sorted({r["question_type"] for r in rows})
    ev_chars = [c for r in rows if "evidence_turn_chars" in r for c in r["evidence_turn_chars"]]
    marker = {"user": sum(r["evidence_turns"]["user"] for r in rows if "evidence_turns" in r),
              "assistant": sum(r["evidence_turns"]["assistant"] for r in rows if "evidence_turns" in r)}
    done = [r for r in rows if "evidence_turns" in r]
    return {
        "data_shape": {
            "questions": len(rows),
            "by_question_type": {t: sum(1 for r in rows if r["question_type"] == t) for t in types},
            "abs": sum(1 for r in rows if r["is_abs"]),
            "marker_turns": marker,
            "questions_without_marker": sum(1 for r in done if r["evidence_turns"]["user"] + r["evidence_turns"]["assistant"] == 0),
            "questions_assistant_only_marker": sum(
                1 for r in done if r["evidence_turns"]["user"] == 0 and r["evidence_turns"]["assistant"] > 0),
            "answer_session_mismatch_questions": answer_session_mismatch,
            "sessions": shapes["sessions"],
            "first_assistant_sessions": shapes["first_assistant"],
            "non_alternating_sessions": shapes["non_alternating"],
            "adjacent_same_role_sessions": shapes["adjacent_same_role"],
            "empty_turns": shapes["empty_turns"],
            "evidence_turn_chars": _dist(ev_chars),
            "evidence_turns_over_hook_truncate_limit": sum(1 for c in ev_chars if c > HOOK_TRUNCATE_LIMIT),
            "user_turns_per_question": _dist([r["user_turns"] for r in done]),
            "failed_questions": sum(1 for r in rows if "error" in r),
        },
        "by_arm": {a: _summ_arm(rows, a) for a in arms},
        "by_type": {t: {a: _summ_arm([r for r in rows if r["question_type"] == t], a)
                        for a in arms if any(a in r["arms"] for r in rows if r["question_type"] == t)} for t in types},
    }


# --- 全体 ------------------------------------------------------------------------------------------


def _select_distract(data, per_type):
    chosen, count = set(), {}
    for q in data:
        t = q["question_type"]
        if count.get(t, 0) < per_type:
            chosen.add(q["question_id"])
            count[t] = count.get(t, 0) + 1
    return chosen


def _limit_per_type(data, per_type):
    if per_type is None:
        return list(data)
    count, out = {}, []
    for q in data:
        t = q["question_type"]
        if count.get(t, 0) < per_type:
            out.append(q)
            count[t] = count.get(t, 0) + 1
    return out


def _frame_chars():
    """注入文の固定の見出しの字数 (毎回 3200 字の一部を使う)。枠 = NARRATIVE_HEADER、① ③ の見出しは別に数える。"""
    from raiya import manas

    return {"narrative_header": len(manas.NARRATIVE_HEADER), "first_circle_header": len(manas.GERMINATED_HEADER),
            "index_header_without_counts": len(manas.INDEX_HEADER)}


def run_all(data, edges, distract_per_type, progress=None, content_rule="span"):
    distract_ids = _select_distract(data, distract_per_type) if distract_per_type else set()
    rows, timing = [], {}
    shapes = {"sessions": 0, "first_assistant": 0, "non_alternating": 0, "adjacent_same_role": 0, "empty_turns": 0}
    mismatch = 0
    t_all = time.perf_counter()
    for n, q in enumerate(data):
        t0 = time.perf_counter()
        try:
            row, shape, qtiming = run_question(q, edges, q["question_id"] in distract_ids, content_rule)
            for k in shapes:
                shapes[k] += shape[k]
            ha_sessions = {sid for sid, s in zip(q["haystack_session_ids"], q["haystack_sessions"])
                           if any(t.get("has_answer") is True for t in s)}
            if ha_sessions and q.get("answer_session_ids") is not None and ha_sessions != set(q["answer_session_ids"]):
                mismatch += 1
        except Exception as e:  # noqa: BLE001 -- 1 問の例外はその問いだけ fail:<Class> にして続ける
            row, qtiming = _failed_row(q, e, edges, q["question_id"] in distract_ids), {}
        rows.append(row)
        timing[q["question_id"]] = {"seconds": round(time.perf_counter() - t0, 4), "states": qtiming}
        if progress and (n + 1) % 25 == 0:
            progress("{}/{} 問 ({:.0f} 秒)".format(n + 1, len(data), time.perf_counter() - t_all))
    return rows, shapes, mismatch, {"total_seconds": round(time.perf_counter() - t_all, 2), "per_question": timing}


CONTENT_RULE_TEXT = {
    "span": "span: 案 X: content = span (根拠の turn の全文。<private>・貼り付けは engine の門と同じ規則で剥がした後)",
    "head120": "head120: content = span の空白を畳んだ最初の 1 文 (最初の [.?!] で終わり、最大 120 字)。接地する span は全文のまま",
}


def _meta(args, edges, sha, nbytes, data, selected):
    return {
        "schema": SCHEMA,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "timing_keys": list(TIMING_KEYS),
        "data_file": os.path.basename(args.data),
        "data_sha256": sha,
        "data_bytes": nbytes,
        "questions_in_file": len(data),
        "questions_run": len(selected),
        "limit_per_type": args.limit_per_type,
        "distractor_per_type": args.distractor_per_type,
        "python": platform.python_version(),
        "arms": {
            "edges": {"A": "根拠の turn と質問文の両方に在る語", "B": "根拠の turn と answer の両方に在る語 (質問文を見ない)"},
            "configs": {n: {"kind": k, "RAIYA_INJECT_DISPOSITIONS": e} for n, k, e in CONFIG_ARMS},
            "edges_run": list(edges),
            "distract_suffix": DISTRACT_SUFFIX,
            "distract_rule": "その問いの user の turn のうち has_answer でないもの全部を、正解と同じ規則 (縁の腕ごと) で種子にする。"
                             "haystack の順に、根拠と邪魔を同じ列で植える",
        },
        "condition_rule": {
            "word": "英数字の連なり ([A-Za-z0-9]+)。比べるのは小文字、渡すのは turn の表記",
            "min_chars": MIN_WORD_CHARS, "digits_only_dropped": True,
            "stopwords": sorted(STOPWORDS),
            "order": "turn に出てきた順・小文字での重複なし",
            "cap": MAX_CONDITIONS,
            "empty": "縁が 0 語の提案は imprint に渡さず decision=no_condition と数える (門の落ちと分ける)",
        },
        "content_rule": CONTENT_RULE_TEXT[args.content_rule],
        "round_trip_rule": "user の turn と直後の assistant の turn で 1 往復。後ろに assistant が無ければ assistant_text=\"\"、"
                           "assistant が単独なら user_text=\"\" の往復。record_turn は根拠 (と邪魔の腕では user の turn) の往復だけを"
                           "haystack の順に流し、返った ledger_id で LedgerTurn を組んで imprint に渡す (窓 = その 1 往復)。"
                           "haystack_dates は使わない",
        "classification_order": CLASSIFICATION_ORDER,
        "limits": {
            "first_circle_limit": default_first_circle_limit(),
            "first_circle_limit_basis": "新しい状態の ego_weight (定数) から。状態の中身は読まない",
            "hook_truncate_limit_chars": HOOK_TRUNCATE_LIMIT,
            "hook_truncate_note": "実 hook は 8000 字で本文を切る (中略の印)。本測定は record_turn に全文を渡す (record_turn 自体に長さ制限は無い)",
            "not_reached_by_public_imprint": "session_by_turn / injected_by_turn は公開の imprint から渡せない: X1/X2b・R・EC2' は働かない",
            "kind_is_declared_by_the_measurer": "kind は測り手が入れる値 (決定 / 性向)。LLM がどの kind を付けるかは段 1 では測れない",
        },
        "frame_chars": _frame_chars(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="LongMemEval 段 1 (LLM 不使用・決定論) の測定")
    ap.add_argument("--data", required=True, help="longmemeval_*.json の path (リポジトリの外)")
    ap.add_argument("--out", required=True, help="結果の dir (必須。リポジトリの外を指す)")
    ap.add_argument("--name", default=DEFAULT_NAME, help="結果のファイル名")
    ap.add_argument("--limit-per-type", type=int, default=None, help="question_type ごとに先頭から N 問だけ走らせる")
    ap.add_argument("--distractor-per-type", type=int, default=0, help="型ごとの先頭 N 問に邪魔の種子の腕を足す (0 = 無し)")
    ap.add_argument("--edges", default="A,B", help="縁の腕 (A,B)")
    ap.add_argument("--content-rule", choices=CONTENT_RULES, default="span", help="提案の content の作り方 (既定 span = 従来)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    edges = tuple(e for e in args.edges.split(",") if e)
    if not edges or any(e not in EDGE_ARMS for e in edges):
        print("--edges は A か B (カンマ区切り)", file=sys.stderr)
        return 2
    try:
        data, sha, nbytes = load_data(args.data)
    except DataError as e:
        print(str(e), file=sys.stderr)
        return 1
    from raiya import config

    selected = _limit_per_type(data, args.limit_per_type)
    progress = None if args.quiet else (lambda m: print(m, file=sys.stderr))
    with _env(RAIYA_CONFIG=""):
        config._reset()
        try:
            rows, shapes, mismatch, timing = run_all(selected, edges, args.distractor_per_type, progress, args.content_rule)
            meta = _meta(args, edges, sha, nbytes, data, selected)
        finally:
            config._reset()
    result = {
        "meta": meta, "summary": summarize(rows, shapes, mismatch), "rows": rows, "timing": timing,
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, args.name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")
    print("wrote {}".format(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
