#!/usr/bin/env python3
"""manual_longmemeval_stage3.py -- LongMemEval 段 3 (実 LLM・手動 1 本)

統合 (`engine.consolidate`) と回答の LLM を入れて、「情報の更新 (knowledge-update)」と「答えない (_abs)」を測る。
第一に測るのは書く側 (統合の LLM が種子を作れるか・古い値をその場で置き換えられるか・二重になるか・ゲートの破れ)。
読む側は問いの時の想起 (段 1 の分類に写す) と、注入に種子があるときだけ呼ぶ回答。正誤は機械の下付けと人の目で決め、
判定の LLM は使わない。設計書: docs/superpowers/specs/2026-11-27-longmemeval-stage3-design.md

手動 1 本の規律: 実 LLM を呼ぶのでテスト (`unittest discover`) には拾わせず、どの agent も走らせない。成功した結果が
`--out` にあれば `--force` なしでは走らない。結果 (問いの本文・span・注入文を含む) はリポジトリの外の `--out` にだけ置き、
リポジトリには写さない (`receipts/` にも書かない)。

実行 (鍵が要る。ライブは openai-api / gpt-6-luna):
    OPENAI_API_KEY=... PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/manual_longmemeval_stage3.py \\
        --data /home/dev/MyDocuments/raiya/bench/longmemeval/longmemeval_oracle.json \\
        --out /home/dev/MyDocuments/raiya/bench/longmemeval/results --name stage3_oracle.json [--limit-per-group N] [--force]
"""
import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (SCRIPTS_DIR, BASE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import longmemeval_stage1 as lm  # noqa: E402
from longmemeval_stage1 import DataError, _env, _reason_key, build_round_trips, load_data  # noqa: E402,F401
from raiya import sansho  # noqa: E402
from raiya.backend import BackendError  # noqa: E402
from raiya.engine import CONSOLIDATE_WINDOW_MAX, MemoryEngine  # noqa: E402
from raiya.hooks.claude_code import _context_window  # noqa: E402
from raiya.hooks.core import CONSOLIDATE_EVERY, _is_skippable_prompt, _safe_session_id, prepare_record  # noqa: E402
from raiya.ledger import gate_text  # noqa: E402

GROUPS = ("ku", "abs_marked", "abs_unmarked")
SCHEMA = "longmemeval-stage3/1"
DEFAULT_NAME = "stage3_oracle.json"
BACKEND = "openai-api"
MODEL = "gpt-6-luna"
PROJECTION_AFTER = 10       # consolidate がこの回数に達したら、残りの時間の見込みを出す
ABORT_AFTER_FAILURES = 10   # consolidate の失敗 (backend_error / unparseable) が問いをまたいで連続でこの回数になったら止める
_UNJUDGED = ("backend_error", "unparseable")


# --- 問いの選び方とセッションの並び (T1) ------------------------------------------------------------


def select_questions(data):
    """knowledge-update か、question_id が `_abs` で終わる問いを、データの順のまま (重複なしで) 返す。"""
    return [q for q in data if q["question_type"] == "knowledge-update" or q["question_id"].endswith("_abs")]


def validate_stage3(data):
    """選んだ問いの haystack_dates が haystack_sessions と同じ長さの str の list で、question_date が str であること。"""
    for q in select_questions(data):
        qid = q["question_id"]
        dates = q.get("haystack_dates")
        if (not isinstance(dates, list) or len(dates) != len(q["haystack_sessions"])
                or not all(isinstance(d, str) for d in dates)):
            raise DataError("問い {} の haystack_dates が haystack_sessions と同じ長さの str の list でありません".format(qid))
        if not isinstance(q.get("question_date"), str):
            raise DataError("問い {} の question_date が str でありません".format(qid))


def _has_marker(q):
    return any(t.get("has_answer") is True for s in q["haystack_sessions"] for t in s)


def question_group(q):
    if q["question_id"].endswith("_abs"):
        return "abs_marked" if _has_marker(q) else "abs_unmarked"
    return "ku"


def session_order(q):
    """セッションの添字を haystack_dates の字の順に並べる (同じ日時は元の順)。"""
    dates = q["haystack_dates"]
    return sorted(range(len(dates)), key=lambda i: dates[i])


# --- hooks と同じ流れの再生 (T2) ---------------------------------------------------------------------

DECISION_KEYS = ("decision", "reason", "kind", "is_new", "superseded", "also_superseded", "reinforced",
                 "user_span_match", "echo_match", "settled_match")


@contextlib.contextmanager
def patched_llm(call_llm):
    """`raiya.backend.call_llm` を `call_llm` に差し替える (抜けるとき・例外でも元に戻す)。"""
    import raiya.backend as backend

    original = backend.call_llm

    def _wrapped(prompt, max_tokens=1024, purpose=None):
        return call_llm(prompt, max_tokens=max_tokens, purpose=purpose)

    backend.call_llm = _wrapped
    try:
        yield
    finally:
        backend.call_llm = original


def needs_wait(last_record_epoch, now_epoch):
    """前の record_turn の後に読んだ時計と、いまの時計が同じ整数秒なら真 (注入ログの ts は秒に切り捨てられるため)。"""
    if last_record_epoch is None:
        return False
    return int(last_record_epoch) == int(now_epoch)


def _trim_decision(decision):
    out = {k: decision.get(k) for k in DECISION_KEYS}
    seed = decision.get("seed")
    out["seed_id"] = seed.get("id") if isinstance(seed, dict) else None
    return out


def consolidate_once(engine, trigger):
    """`engine.consolidate` を 1 回呼び、結果を測り手の形にする。`.reason` を持つ例外は skipped="backend_error"。"""
    notes, debug = [], {}
    result, error_reason = None, None
    try:
        result = engine.consolidate(notes=notes, debug=debug)
    except Exception as e:
        error_reason = getattr(e, "reason", None)
        if error_reason is None:
            raise
    if result is None:
        window, skipped, proposals, decisions = list(debug.get("window_ids") or []), "backend_error", [], []
    else:
        window, skipped = list(result.get("window") or []), result.get("skipped")
        proposals, decisions = list(result.get("proposals") or []), list(result.get("decisions") or [])
    kept = {k: v for k, v in debug.items() if k != "prompt" and (k != "raw" or skipped == "unparseable")}
    return {
        "trigger": trigger, "skipped": skipped, "error_reason": error_reason, "parse_notes": list(notes),
        "window": window, "proposals": proposals, "decisions": [_trim_decision(d) for d in decisions], "debug": kept,
    }


def replay_question(engine, q, sleep=time.sleep, clock=time.time):
    """1 問を hooks と同じ流れで台帳に流す。セッションは日付の順。セッションごとに 1 つの life、user の本文があるプロンプトごと
    に recall (skip 対象は除く)、往復ごとに prepare_record → record_turn、未統合が CONSOLIDATE_EVERY に達したら consolidate、
    セッションの終わりに consolidate。recall の前に、前の記録と同じ秒なら次の秒まで眠る。"""
    out = {"turns": [], "texts": {}, "consolidations": [], "recalls": 0, "lives": 0, "skipped_turns": 0, "consumed": set()}
    last_epoch = None
    for rank, si in enumerate(session_order(q)):
        sid = q["haystack_session_ids"][si]
        rts, _shape = build_round_trips([sid], [q["haystack_sessions"][si]])
        life_begun, previous = False, None
        for rt in rts:
            user = rt["user"]
            if user != "":
                first_prompt = not life_begun   # hooks の is_first_prompt_of_session と同じ: skip 対象でも最初のプロンプトは最初
                if first_prompt:
                    engine.begin_new_life()
                    out["lives"] += 1
                    life_begun = True
                context = _context_window(previous)
                if not _is_skippable_prompt(user):
                    now = clock()
                    if needs_wait(last_epoch, now):
                        sleep(int(last_epoch) + 1 - now + 0.01)
                    engine.recall(user, context=context, project=None, session_start=first_prompt,
                                  session_id=_safe_session_id(sid))
                    out["recalls"] += 1
                previous = {"prompt": user, "recent": context}
            prep = prepare_record(user, rt["assistant"])
            if prep.skip:
                out["skipped_turns"] += 1
                continue
            rec = engine.record_turn(prep.user_text, prep.assistant_text, session_id=sid,
                                     grounding_text=prep.grounding_text, project=None)
            last_epoch = clock()
            lid = rec["ledger_id"]
            out["turns"].append({"ledger_id": lid, "session_id": sid, "session_rank": rank,
                                 "user_ha": rt["user_ha"], "assistant_ha": rt["assistant_ha"]})
            out["texts"][lid] = {"user": gate_text(prep.grounding_text), "assistant": gate_text(prep.assistant_text)}
            if rec["pending"] >= CONSOLIDATE_EVERY:
                out["consolidations"].append(consolidate_once(engine, "every"))
        out["consolidations"].append(consolidate_once(engine, "session_end"))
    for c in out["consolidations"]:
        if c["skipped"] is None:
            out["consumed"].update(c["window"])
    return out


# --- 書く側の測り (T3): 種子と往復の結び付け・KU の 11 類・抽出・ゲート ------------------------------------

KU_CLASSES = ("failed", "incomplete", "single_evidence_session", "none", "old_only", "new_only", "superseded_in_place",
              "superseded_horizontal", "merged", "both_live", "other")
RECORD_ONLY_KEYS = ("user_span_match", "echo_match", "settled_match")
_CJK_RE = re.compile("[\u3040-\u30ff\u3400-\u9fff]")


def seed_links(seeds):
    """種子 id -> 結び付け。往復との結び付けは本文でなく evidences[].ledger_id で行う。"""
    out = {}
    for seed in seeds:
        ids = {e["ledger_id"] for e in (seed.get("evidences") or [])
               if isinstance(e, dict) and isinstance(e.get("ledger_id"), int) and not isinstance(e.get("ledger_id"), bool)}
        content = seed.get("content") or ""
        out[seed["id"]] = {
            "ledger_ids": ids, "first_ledger_id": _first_ledger_id(seed), "superseded_by": seed.get("superseded_by"),
            "withdrawn": seed.get("withdrawn"), "kind": seed.get("kind") or "性向", "sansho": seed.get("sansho"),
            "content": content, "content_has_cjk": bool(_CJK_RE.search(content)),
        }
    return out


def _first_ledger_id(seed):
    for e in (seed.get("evidences") or []):
        if isinstance(e, dict) and isinstance(e.get("ledger_id"), int) and not isinstance(e.get("ledger_id"), bool):
            return e["ledger_id"]
    return None


def decisions_touching(replay, ledger_id):
    """`skipped is None` の consolidate のうち窓に ledger_id を含むものの、その往復の本文に span が在る提案と決定の対。"""
    texts = (replay.get("texts") or {}).get(ledger_id) or {}
    bodies = [sansho._n(texts.get("user") or ""), sansho._n(texts.get("assistant") or "")]
    out = []
    for c in replay["consolidations"]:
        if c["skipped"] is not None or ledger_id not in c["window"]:
            continue
        debug = c.get("debug") or {}
        related = list(debug.get("related_ids") or []) + list(debug.get("group_rep_ids") or [])
        for p, d in zip(c["proposals"], c["decisions"]):
            spans = [sansho._n(e.get("span") or "") for e in (p.get("evidence") or [])]
            if any(sp and any(sp in b for b in bodies) for sp in spans):
                out.append({"action": p.get("action"), "target_id": p.get("target_id"), "kind": p.get("kind"),
                            "content": p.get("content"), "decision": d, "related_ids": related})
    return out


def _evidence_session_ids(q):
    """has_answer の turn を持つセッションの id を、日付の順で (古い -> 新しい)。"""
    return [q["haystack_session_ids"][i] for i in session_order(q)
            if any(t.get("has_answer") is True for t in q["haystack_sessions"][i])]


def _norm_content(text):
    return sansho._n(text or "").strip().casefold()


def ku_outcome(q, replay, links, consolidated_upto):
    """KU の行方 (11 類、判定の順に先に当たったもの)。純関数 (engine を受けない)。"""
    out = {"class": None, "old_seed_ids": [], "new_seed_ids": [], "old_shown": None, "new_decisions": [],
           "split": None, "content_from_new": None}
    if "error" in replay:
        out["class"] = "failed"
        return out
    ev_sessions = _evidence_session_ids(q)
    ev_turns = [t for t in replay["turns"] if t["user_ha"] or t["assistant_ha"]]
    if any(t["ledger_id"] > consolidated_upto for t in ev_turns):
        out["class"] = "incomplete"
        return out
    if len(ev_sessions) < 2:
        out["class"] = "single_evidence_session"
        return out
    old_ids = {t["ledger_id"] for t in ev_turns if t["session_id"] == ev_sessions[0]}
    new_ids = {t["ledger_id"] for t in ev_turns if t["session_id"] == ev_sessions[-1]}
    old_seeds = sorted(i for i, l in links.items() if l["ledger_ids"] & old_ids)
    new_seeds = sorted(i for i, l in links.items() if l["ledger_ids"] & new_ids)
    out["old_seed_ids"], out["new_seed_ids"] = old_seeds, new_seeds
    new_decisions = [d for lid in sorted(new_ids) for d in decisions_touching(replay, lid)]
    out["new_decisions"] = new_decisions
    related = None
    for d in new_decisions:
        related = (related or set()) | set(d["related_ids"])
    if related is None:   # 触れる決定が無い: 新しい値の往復を含む consolidate の debug で見る
        for c in replay["consolidations"]:
            if c["skipped"] is None and new_ids & set(c["window"]):
                debug = c.get("debug") or {}
                related = (related or set()) | set(debug.get("related_ids") or []) | set(debug.get("group_rep_ids") or [])
    out["old_shown"] = None if related is None else any(i in related for i in old_seeds)
    both = [i for i in old_seeds if i in new_seeds]

    def split():
        if not out["old_shown"]:
            return "not_shown"
        for d in new_decisions:
            dec = d["decision"]
            if (d["action"] in ("reinforce", "supersede") and d["target_id"] in old_seeds
                    and (dec.get("is_new") or any(dec.get(k) for k in RECORD_ONLY_KEYS))):
                return "flipped_by_rule"
        return "shown_llm_new"

    if not old_seeds and not new_seeds:
        cls = "none"
    elif not new_seeds:
        cls = "old_only"
    elif not old_seeds:
        cls = "new_only"
    elif any(d["decision"].get("superseded") and d["decision"].get("seed_id") in both for d in new_decisions):
        cls = "superseded_in_place"
    elif any(links[i]["superseded_by"] in new_seeds for i in old_seeds):
        cls = "superseded_horizontal"
    elif both:
        cls = "merged"
        mine = {_norm_content(d["content"]) for d in new_decisions}
        out["content_from_new"] = any(_norm_content(links[i]["content"]) in mine for i in both)
    elif all(links[i]["superseded_by"] is None and links[i]["withdrawn"] is None for i in old_seeds + new_seeds):
        cls = "both_live"
    else:
        cls = "other"
    out["class"] = cls
    if cls in ("both_live", "new_only"):
        out["split"] = split()
    return out


def extraction_counts(replay, links):
    """抽出率 (根拠の往復と他の往復が種子の evidence に入った本数)・kind・action・class・reject の理由・記録のみの鍵。"""
    in_seed = set()
    for l in links.values():
        in_seed |= l["ledger_ids"]
    ev = [t["ledger_id"] for t in replay["turns"] if t["user_ha"] or t["assistant_ha"]]
    other = [t["ledger_id"] for t in replay["turns"] if not (t["user_ha"] or t["assistant_ha"])]
    props, decs = [], []
    for c in replay["consolidations"]:
        if c["skipped"] is None:
            props += c["proposals"]
            decs += c["decisions"]
    return {
        "evidence_turns": len(ev), "evidence_turns_in_seeds": sum(1 for i in ev if i in in_seed),
        "other_turns": len(other), "other_turns_in_seeds": sum(1 for i in other if i in in_seed),
        "proposal_kinds": dict(Counter(p.get("kind") for p in props)),
        "saved_kinds": dict(Counter(l["kind"] for l in links.values())),
        "actions": dict(Counter(p.get("action") for p in props)),
        "classes": dict(Counter(d.get("decision") for d in decs)),
        "reject_reasons": dict(Counter(_reason_key(d.get("reason")) for d in decs if d.get("decision") == "reject")),
        "record_only": {k: sum(1 for d in decs if d.get(k)) for k in RECORD_ONLY_KEYS if any(d.get(k) for d in decs)},
        "content_cjk_seeds": sum(1 for l in links.values() if l["content_has_cjk"]),
    }


def gate_breaches(seeds, texts):
    """受理された種子 (圓成實・依他起) の evidence の span が、その往復のゲートの本文のどちらにも無いもの (不変条件 1 の破れ)。"""
    everything = [sansho._n(t.get(side) or "") for t in texts.values() for side in ("user", "assistant")]
    out = []
    for seed in seeds:
        if seed.get("sansho") not in ("圓成實", "依他起"):
            continue
        for e in (seed.get("evidences") or []):
            span = sansho._n(e.get("span") or "")
            if not span.strip():
                continue
            t = texts.get(e.get("ledger_id"))
            bodies = [sansho._n(t.get("user") or ""), sansho._n(t.get("assistant") or "")] if t is not None else everything
            if not any(span in b for b in bodies):
                out.append({"seed_id": seed.get("id"), "span": (e.get("span") or "")[:200]})
    return out


# --- 問いの時の想起と回答 (T4) -----------------------------------------------------------------------

ARMS = (("default", "0"), ("dispositions", "1"))
ABSTAIN_PHRASE = "I don't know"
ANSWER_PROMPT = (
    "Below are memory notes kept by a memory system about past conversations with the user. "
    "The notes may mix Japanese and English.\n"
    "Current date: {question_date}\n"
    "\n"
    "Notes:\n"
    "{narrative}\n"
    "\n"
    "Question: {question}\n"
    "Answer using only the notes. If the notes do not contain the information needed, reply exactly: " + ABSTAIN_PHRASE + ".\n"
    "Answer in at most two sentences."
)
_OR_RE = re.compile(r"^(.*?)\s*\(or\s+(.*?)\)\s*$")


def question_recall(engine, q):
    """問いを新しいセッションの最初のプロンプトと見て (begin_new_life を 1 回)、腕ごとに explain_recall (状態を変えない) を引く。"""
    engine.begin_new_life()
    out = {}
    for name, value in ARMS:
        with _env(RAIYA_INJECT_DISPOSITIONS=value):
            rec = engine.explain_recall(q["question"], project=None, session_start=True)
        first = lm.rendered_first_circle(rec["seeds"], rec["circles"]["cut"]["germinated"])
        index = [s["id"] for s in rec["index"]]
        out[name] = {"first_circle_ids": first, "index_ids": index, "has_seed_line": bool(first or index),
                     "narrative": rec["narrative"], "chars": rec["circles"]["chars"], "cut": dict(rec["circles"]["cut"])}
    return out


def answer_prompt(q, narrative):
    return ANSWER_PROMPT.format(question_date=q["question_date"], narrative=narrative, question=q["question"])


def _fold(text):
    return " ".join(str(text).casefold().split())


def auto_label(answer_key, model_answer):
    """機械の下付け: abstained (答えが I don't know で始まる) / key_hit (正答を含む。`X (or Y)` は X か Y) / other。"""
    answer = _fold(model_answer)
    if answer.startswith(_fold(ABSTAIN_PHRASE)):
        return "abstained"
    key = _fold(answer_key)
    keys = [key]
    m = _OR_RE.match(key)
    if m:
        keys += [m.group(1), m.group(2)]
    if any(k and k in answer for k in keys):
        return "key_hit"
    return "other"


def answer_arm(q, arm, call):
    """注入に種子が無い腕は呼ばない。有れば call(answer_prompt, purpose=None) (呼び手が raiya.backend.call_llm を渡す)。"""
    if not arm["has_seed_line"]:
        return {"answer": None, "auto_label": "no_injection", "error": None}
    try:
        answer = call(answer_prompt(q, arm["narrative"]), purpose=None)
    except BackendError as e:
        return {"answer": None, "auto_label": "error", "error": e.reason}
    return {"answer": answer, "auto_label": auto_label(q["answer"], answer), "error": None}


def injection_class(q, replay, links, arm):
    """段 1 の分類 (lm.classify) に写す。planted = 最初の evidence が根拠の往復である種子が在る。"""
    ev = {t["ledger_id"] for t in replay["turns"] if t["user_ha"] or t["assistant_ha"]}
    has_user = any(t["user_ha"] for t in replay["turns"])
    planted = [i for i, l in links.items() if l["first_ledger_id"] in ev]
    touching = [i for i, l in links.items() if l["ledger_ids"] & ev]
    shown = set(arm["first_circle_ids"]) | set(arm["index_ids"])
    return lm.classify(bool(ev), has_user, q["question_id"].endswith("_abs"), bool(planted),
                       any(i in shown for i in planted), merged_only=bool(touching) and not planted)


# --- ランナー (T5) -----------------------------------------------------------------------------------


class RunAborted(Exception):
    """consolidate の失敗が連続で ABORT_AFTER_FAILURES 回になって止まった (鍵の失効・モデル名の誤りで呼び出しを捨てないため)。"""


def out_is_inside_repo(out_dir):
    out, base = os.path.realpath(out_dir), os.path.realpath(BASE_DIR)
    return out == base or out.startswith(base + os.sep)


def name_has_path(name):
    """--name が path (絶対 path・`..`・下位 dir) を含むなら真。os.path.join は絶対 path で out_dir を捨てるため。"""
    return os.path.basename(name) != name or name in ("", ".", "..")


def live_key():
    return os.environ.get("RAIYA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _json_default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError("not JSON serializable: {}".format(type(obj).__name__))


def _roundtrip(obj):
    """JSON を通した形にする (一気に走らせた行と、partial から読み直した行を同じ型にする)。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=_json_default))


def _git_head():
    try:
        done = subprocess.run(["git", "-C", BASE_DIR, "rev-parse", "HEAD"], capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _seed_row(seed, link):
    return {"id": seed["id"], "kind": link["kind"], "sansho": seed.get("sansho"), "content": seed.get("content"),
            "conditions": list(seed.get("conditions") or []), "superseded_by": seed.get("superseded_by"),
            "withdrawn": seed.get("withdrawn"), "ledger_ids": sorted(link["ledger_ids"]),
            "spans": [e.get("span") for e in (seed.get("evidences") or []) if isinstance(e, dict)]}


def measure_question(q, call, sleep):
    """1 問を測って行 (dict) と、その問いの consolidate の skipped の列を返す。state は自作の tmp の下だけ (終わりに消す)。"""
    group = question_group(q)
    tmp = tempfile.mkdtemp(prefix="raiya_lme3_state_")
    try:
        engine = MemoryEngine(state_dir=tmp, new_life=False)
        replay = replay_question(engine, q, sleep=sleep)
        seeds = engine.inspect_seeds(include_superseded=True, include_withdrawn=True)
        links = seed_links(seeds)
        upto = engine.audit()["ledger"]["consolidated_upto"]
        row = {
            "question_id": q["question_id"], "question_type": q["question_type"], "group": group, "failed": None,
            "answer_key": q["answer"], "turns": len(replay["turns"]), "skipped_turns": replay["skipped_turns"],
            "lives": replay["lives"], "recalls": replay["recalls"],
            "consolidations": replay["consolidations"], "consumed": sorted(replay["consumed"]),
            "seeds": [_seed_row(sd, links[sd["id"]]) for sd in seeds],
            "ku": ku_outcome(q, replay, links, upto) if group == "ku" else None,
            "extraction": extraction_counts(replay, links),
            "gate_breaches": gate_breaches(seeds, replay["texts"]),
            "arms": {},
        }
        recall = question_recall(engine, q)
        for name, _ in ARMS:
            arm = recall[name]
            row["arms"][name] = dict(arm, **{"class": injection_class(q, replay, links, arm)}, **answer_arm(q, arm, call))
        return _roundtrip(row), [c["skipped"] for c in replay["consolidations"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _failed_row(q, exc):
    group = question_group(q)
    return {"question_id": q["question_id"], "question_type": q["question_type"], "group": group,
            "failed": "fail:{}".format(exc.__class__.__name__), "answer_key": q["answer"], "consolidations": [],
            "seeds": [], "ku": ku_outcome(q, {"error": "fail"}, {}, 0) if group == "ku" else None,
            "extraction": None, "gate_breaches": [], "arms": {}}


def update_streak(streak, qid, skips):
    """(連続の失敗の数, 今の連続に失敗を足した問いの id の列) を、1 問ぶんの consolidate の skipped の列で更新する。
    backend_error / unparseable で +1 (qid を列の末尾に 1 回だけ足す)、LLM を呼ばなかった empty は変えず、
    それ以外 (統合できた) は (0, []) に戻す。"""
    count, ids = streak[0], list(streak[1])
    for skipped in skips:
        if skipped in _UNJUDGED:
            count += 1
            if qid not in ids:
                ids.append(qid)
        elif skipped != "empty":
            count, ids = 0, []
    return count, ids


def _select(data, limit_per_group):
    chosen, seen = [], Counter()
    for q in select_questions(data):
        g = question_group(q)
        if limit_per_group is not None and seen[g] >= limit_per_group:
            continue
        seen[g] += 1
        chosen.append(q)
    return chosen


def _load_partial(path):
    """partial の行を読む。壊れた行 (書きかけの尾) は捨て、あれば有効な行だけで書き直す。"""
    rows, torn = [], False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    torn = True
        if torn:
            _write_lines(path, rows)
    return rows


def _write_lines(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=_json_default) + "\n")


def _sum_counts(items):
    total = {}
    for item in items:
        for k, v in (item or {}).items():
            if isinstance(v, dict):
                sub = total.setdefault(k, {})
                for kk, vv in v.items():
                    sub[kk] = sub.get(kk, 0) + vv
            else:
                total[k] = total.get(k, 0) + v
    return total


def summarize(rows):
    done = [r for r in rows if not r["failed"]]
    steps = [c for r in rows for c in r["consolidations"]]
    called = [c for c in steps if c["skipped"] != "empty"]
    by_group = {g: sum(1 for r in rows if r["group"] == g) for g in GROUPS}
    ku_rows = [r for r in rows if r["group"] == "ku"]
    arms, vacuous, answers = {}, {}, 0
    for name, _ in ARMS:
        ic, al = {g: {} for g in GROUPS}, {g: {} for g in GROUPS}
        for r in done:
            a = r["arms"][name]
            ic[r["group"]][a["class"]] = ic[r["group"]].get(a["class"], 0) + 1
            al[r["group"]][a["auto_label"]] = al[r["group"]].get(a["auto_label"], 0) + 1
            answers += a["auto_label"] != "no_injection"
        arms[name] = {"injection_class": ic, "auto_label": al}
        vacuous[name] = sum(1 for r in done if r["group"] == "abs_unmarked" and r["arms"][name]["auto_label"] == "no_injection")
    ku_classes = {c: sum(1 for r in ku_rows if r["ku"]["class"] == c) for c in KU_CLASSES}
    splits = Counter(r["ku"].get("split") for r in ku_rows if r["ku"].get("split"))
    from_new = Counter(str(r["ku"]["content_from_new"]).lower() for r in ku_rows if r["ku"].get("content_from_new") is not None)
    breaches = [dict(b, question_id=r["question_id"]) for r in rows for b in r["gate_breaches"]]
    failed_rows = [r["question_id"] for r in rows if any(c["skipped"] in _UNJUDGED for c in r["consolidations"])]
    skipped = Counter("consolidated" if c["skipped"] is None else c["skipped"] for c in steps)
    return {
        "questions": {"total": len(rows), "by_group": by_group, "failed": len(rows) - len(done)},
        "ku_classes": ku_classes, "ku_split": dict(splits), "ku_content_from_new": dict(from_new),
        "extraction": _sum_counts(r["extraction"] for r in done),
        "arms": arms, "vacuous_abstain": vacuous,
        "calls": {"consolidate": len(called), "answer": answers},
        "llm_ms": lm._dist([c["debug"]["llm_ms"] for c in called if c["debug"].get("llm_ms") is not None]),
        "prompt_len": lm._dist([c["debug"]["prompt_len"] for c in called if c["debug"].get("prompt_len") is not None]),
        "raw_len": lm._dist([c["debug"]["raw_len"] for c in called if c["debug"].get("raw_len") is not None]),
        "skipped": dict(skipped),
        "gate": {"ok": not breaches, "breaches": breaches},
        "rows_with_failed_consolidations": {"count": len(failed_rows), "question_ids": failed_rows},
        "blocked": bool(called) and all(c["skipped"] in _UNJUDGED for c in called),
    }


def _review_rows(rows):
    out = []
    for r in rows:
        for name, _ in ARMS:
            a = (r.get("arms") or {}).get(name)
            if a and (a["auto_label"] == "other" or (r["group"] == "ku" and a["auto_label"] == "key_hit")):
                out.append([r["question_id"], r["group"], name, a["auto_label"], r["answer_key"], a["answer"], ""])
    return out


def _write_review(path, rows):
    cols = ["question_id", "group", "arm", "auto_label", "answer_key", "model_answer", "human_label"]
    clean = lambda v: "" if v is None else str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    with open(path, "w", encoding="utf-8") as f:
        for line in [cols] + _review_rows(rows):
            f.write("\t".join(clean(v) for v in line) + "\n")


def _meta(data_path, sha, dry_run, n_questions, limit_per_group):
    import raiya.backend as backend
    from raiya.hooks.core import TRUNCATE_LIMIT

    backends = None if dry_run else {"propose": backend.describe("propose")[0], "answer": backend.describe(None)[0]}
    models = None if dry_run else {"propose": backend.describe("propose")[1], "answer": backend.describe(None)[1]}
    return {
        "schema": SCHEMA, "generated_at": _now(), "data_file": data_path, "data_sha256": sha, "dry_run": dry_run,
        "backend": backends, "model": models, "git_head": _git_head(), "questions": n_questions,
        "limit_per_group": limit_per_group,
        "replay_flow": ("セッションは haystack_dates の字の順 (同じ日時は元の順)。セッションごとに begin_new_life を 1 回、user の本文が"
                        "あるプロンプトごとに recall (skip 対象を除く。前の記録と同じ秒なら次の秒まで待つ)、往復ごとに prepare_record → "
                        "record_turn、未統合が CONSOLIDATE_EVERY に達したら consolidate、セッションの終わりに consolidate。"
                        "session_start は、そのセッションの最初のプロンプト (user の本文が空でない最初の往復) にだけ真 "
                        "(最初が skip 対象なら、そのセッションで真は渡らない。hooks の is_first_prompt_of_session と同じ)。"
                        "consolidate の失敗が連続 ABORT_AFTER_FAILURES 回で止まるとき、連続に失敗を足した問いの行を partial から落とす。"
                        "最後の session_end の後に未統合が残れば incomplete。問いの時は begin_new_life を 1 回して腕ごとに explain_recall"),
        "ku_class_order": list(KU_CLASSES), "arms": [list(a) for a in ARMS], "groups": list(GROUPS),
        "answer_prompt": ANSWER_PROMPT,
        "auto_label_rule": ("casefold と空白の畳み。答えが `i don't know` で始まれば abstained / 正答 (str に直す。`X (or Y)` は X か Y) を"
                            "含めば key_hit / 他は other。回答を呼ばなかった腕は no_injection、BackendError は error"),
        "consolidate_every": CONSOLIDATE_EVERY, "consolidate_window_max": CONSOLIDATE_WINDOW_MAX,
        "request_timeout_sec_default": backend.REQUEST_TIMEOUT_SEC, "hook_truncate_limit": TRUNCATE_LIMIT,
        "abort_after_failures": ABORT_AFTER_FAILURES, "projection_after": PROJECTION_AFTER,
    }


def run(data_path, out_dir, name=DEFAULT_NAME, call_llm=None, force=False, limit_per_group=None, sleep=time.sleep,
        progress=None):
    """§4.6 の 2〜6 の順。データの検証を LLM の前に全部済ませる。結果は out_dir の `name` / `name.partial.jsonl` / `<stem>_review.tsv`。"""
    say = progress or (lambda msg: None)
    if out_is_inside_repo(out_dir):
        raise ValueError("--out がリポジトリの中です (結果の本文をリポジトリに入れない)")
    if name_has_path(name):
        raise ValueError("--name は file 名だけ (path を含めない): {!r}".format(name))
    data, sha, _nbytes = load_data(data_path)
    validate_stage3(data)
    chosen = _select(data, limit_per_group)
    result_path = os.path.join(out_dir, name)
    partial_path = result_path + ".partial.jsonl"
    stem = name[:-5] if name.endswith(".json") else name
    review_path = os.path.join(out_dir, stem + "_review.tsv")

    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as f:
                existing = json.load(f)
        except ValueError:
            raise DataError("既存の結果 {} が JSON として読めません (--force で再走)".format(result_path))
        blocked = bool((existing.get("summary") or {}).get("blocked"))
        if not blocked and not force:
            print("{} は既にあります (手動1本の規律=再走しない)。再走するには --force を指定してください。".format(result_path),
                  file=sys.stderr)
            return existing
        os.makedirs(out_dir, exist_ok=True)
        _write_lines(partial_path, [])   # blocked か --force: partial を空から書き直す
    os.makedirs(out_dir, exist_ok=True)
    kept = _load_partial(partial_path)
    by_id = {r["question_id"]: r for r in kept}
    todo = [q for q in chosen if q["question_id"] not in by_id]

    dry_run = call_llm is not None
    started = time.monotonic()
    ran, ms, calls_done, projected = 0, [], 0, False
    streak = (0, [])
    import raiya.backend as backend

    answer_call = lambda prompt, purpose=None: backend.call_llm(prompt, purpose=purpose)   # noqa: E731 (差し替えは呼ぶ時に引く)
    env = {"RAIYA_CONFIG": ""} if dry_run else {"RAIYA_CONFIG": "", "RAIYA_BACKEND": BACKEND, "RAIYA_MODEL": MODEL,
                                                "RAIYA_PROPOSE_MODEL": MODEL}
    with _env(**env), (patched_llm(call_llm) if dry_run else contextlib.nullcontext()):
        meta = _meta(data_path, sha, dry_run, len(chosen), limit_per_group)
        for q in todo:
            try:
                row, skips = measure_question(q, answer_call, sleep)
            except Exception as e:   # 1 問の例外はその問いだけ failed の行にして続ける
                row, skips = _failed_row(q, e), []
            streak = update_streak(streak, q["question_id"], skips)
            if streak[0] >= ABORT_AFTER_FAILURES:
                # (i) この問いの行は書かない (ii) 連続に失敗を足した問いの行を partial から落とす (iii) 止まる (続きで走らせ直す)
                dropped = set(streak[1])
                _write_lines(partial_path, [r for r in _load_partial(partial_path) if r["question_id"] not in dropped])
                raise RunAborted("consolidate の失敗が連続 {} 回 (問い {} で停止。失敗を足した問い {} の行は partial から落とした。"
                                 "結果は書かない。同じコマンドで続きから走る)".format(streak[0], q["question_id"], streak[1]))
            with open(partial_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")
            by_id[q["question_id"]] = row
            ran += 1
            for c in row["consolidations"]:
                if c["skipped"] != "empty":
                    calls_done += 1
                    if c["debug"].get("llm_ms") is not None:
                        ms.append(c["debug"]["llm_ms"])
            say("[{}/{}] {} ({})".format(len(by_id), len(chosen), q["question_id"], row["failed"] or "ok"))
            if not projected and calls_done >= PROJECTION_AFTER and ms:
                projected = True
                left = len(todo) - ran
                est_calls = calls_done / ran * left
                p50 = statistics.median(ms) / 1000.0
                say("見込み: consolidate {} 回で p50 {:.1f} 秒。残り {} 問 (約 {:.0f} 回) で約 {:.0f} 分".format(
                    calls_done, p50, left, est_calls, est_calls * p50 / 60.0))

    rows = [by_id[q["question_id"]] for q in chosen]
    doc = {"meta": meta, "summary": summarize(rows), "rows": rows,
           "timing": {"elapsed_sec": round(time.monotonic() - started, 3), "questions_this_run": ran,
                      "resumed_rows": len(chosen) - ran}}
    doc = _roundtrip(doc)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    _write_review(review_path, rows)
    return doc


def main(argv=None, call_llm=None, sleep=time.sleep):
    """`call_llm` を渡すとドライラン (鍵を見ない)。`sleep` はテストが差し替える (秒の待ち)。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True, help="結果の置き場 (リポジトリの外)")
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit-per-group", type=int, default=None, metavar="N")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if call_llm is None and not live_key():
        print("OPENAI_API_KEY (または RAIYA_OPENAI_API_KEY) が無いため、ライブ実行の前で止めました。何も読まず・書いていません。",
              file=sys.stderr)
        print("実行: OPENAI_API_KEY=... PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/manual_longmemeval_stage3.py "
              "--data PATH/longmemeval_oracle.json --out DIR [--name {}] [--limit-per-group N]".format(DEFAULT_NAME), file=sys.stderr)
        return 2
    if out_is_inside_repo(args.out):
        print("--out がリポジトリの中です: {} (結果は外の dir に置く)".format(args.out), file=sys.stderr)
        return 2
    if name_has_path(args.name):
        print("--name は file 名だけにしてください (path を含めない): {!r}".format(args.name), file=sys.stderr)
        return 2
    progress = None if args.quiet else (lambda msg: print(msg, file=sys.stderr))
    try:
        result = run(args.data, args.out, name=args.name, call_llm=call_llm, force=args.force,
                     limit_per_group=args.limit_per_group, sleep=sleep, progress=progress)
    except DataError as e:
        print("データの誤り: {}".format(e), file=sys.stderr)
        return 1
    except RunAborted as e:
        print(str(e), file=sys.stderr)
        return 3
    summary = result["summary"]
    if not args.quiet:
        print(json.dumps({"ku_classes": summary["ku_classes"], "gate": summary["gate"]["ok"], "blocked": summary["blocked"],
                          "calls": summary["calls"], "result": os.path.join(args.out, args.name)}, ensure_ascii=False, indent=2))
    if summary["blocked"]:
        return 0
    return 0 if summary["gate"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
