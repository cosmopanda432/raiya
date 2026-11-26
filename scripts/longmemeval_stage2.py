"""longmemeval_stage2.py -- LongMemEval を使った「段 2」測定: en の印の表 (語ごとの当たり・kind の移り・札・注入の数)

設計書 2026-11-26-en-marker-table-design §1・§8 T3。LLM は使わない・決定論。段 1 (`longmemeval_stage1.py`) の
`load_data` / `build_round_trips` / `choose_conditions` / `run_question` を使い、en の表を変種ごとに差し替えて数える。

使い方:
    python3 scripts/longmemeval_stage2.py --data PATH/longmemeval_s_cleaned.json --out DIR [--name stage2.json]
        [--variants P0,P1,P2,REC] [--quiet]

- データは引数で受け取る。リポジトリへ写さない・ネットワークに出ない。結果の JSON は `--out` (必須) にだけ書く。
- 実 LLM は呼ばない (`raiya.backend` を import しない)。`RAIYA_CONFIG=""` を強いる。注入の数 (`injected`) は段 1 の
  `run_question` を、`en_tables(variant)` の下で呼んで数える (状態は `tempfile.mkdtemp` の下に作って段 1 が消す)。
- ファイル名が test*.py でないので `unittest discover` には拾われない。

出力 JSON (`schema` = "longmemeval-stage2/1"): meta / corpora / candidates / kind_shift / labels / injected。
"""
import argparse
import contextlib
import datetime
import json
import os
import platform
import sys
from unittest import mock

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (BASE_DIR, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import longmemeval_stage1 as lm1  # noqa: E402

DEFAULT_NAME = "stage2.json"
SCHEMA = "longmemeval-stage2/1"
DEFAULT_VARIANTS = ("P0", "P1", "P2", "REC")
CORPORA = ("evidence_user", "user", "assistant")
COLUMNS = ("substring", "word", "final", "in_sentence")
DECLARED_KINDS = ("決定", "性向")

# 役 → alaya の和集合の定数名 (en_tables が差し替える)
ROLE_CONSTANTS = {
    "decision_reject": "DECISION_REJECT_MARKERS",
    "workaround_cause": "WORKAROUND_CAUSE_MARKERS",
    "preference_suffix": "PREFERENCE_SUFFIXES",
    "user_correction": "USER_CORRECTION_MARKERS",
    "decision_assert": "DECISION_ASSERT_MARKERS",
    "hearsay": "HEARSAY_MARKERS",
    "workaround_avoid": "WORKAROUND_AVOID_MARKERS",
    "self_stated_cause": "SELF_STATED_CAUSE_MARKERS",
    "conjecture": "CONJECTURE_MARKERS",
    "decision_assert_in_sentence": "DECISION_ASSERT_IN_SENTENCE_MARKERS",
    "conjecture_in_sentence": "CONJECTURE_IN_SENTENCE_MARKERS",
}

# --- 候補の語 (§1.3・§5。casefold 形) ----------------------------------------------------------------

_PREFERENCE_P1 = (
    "i prefer", "i'd prefer", "i would prefer", "i'd rather", "i would rather",
    "i want you to", "i'd like you to", "i would like you to",
)
_PREFERENCE_P2 = _PREFERENCE_P1 + ("i want to", "i'd like to", "i would like to")
_PREFERENCE_P3 = _PREFERENCE_P2 + ("i want", "i'd like")
_ASSERT_RECOMMENDED = (
    "we decided to", "i decided to", "we've decided to", "i've decided to", "we decided on", "i decided on",
    "we'll go with", "i'll go with", "we're going with", "i'm going with", "let's go with", "we went with",
    "we chose", "we settled on", "i settled on", "let's use",
)
_ASSERT_EXCLUDED = ("i went with", "we use", "i'm using", "we're using", "i'll use", "i chose", "i ended up")
_HEARSAY_RECOMMENDED = (
    "you mentioned that", "you told me that", "as you mentioned", "as you said", "you said you", "you've mentioned",
    "you had mentioned", "you were planning to", "the user said", "the user mentioned", "you asked me to",
)
_HEARSAY_EXCLUDED = ("you said that", "you mentioned", "according to the record", "according to the records", "as recorded")
_CAUSE_RECOMMENDED = ("the cause is", "the cause was", "the root cause", "was caused by", "is caused by")
_CAUSE_EXCLUDED = ("because", "due to")
_AVOID_RECOMMENDED = (
    "works around it", "worked around it", "avoids it", "avoided it", "avoids the issue", "avoided the issue",
    "avoids the problem", "avoided the problem", "gets around it", "got around it",
)
_AVOID_EXCLUDED = ("fixed it", "fixes it", "solved it", "it works", "that works")
_CONJECTURE_RECOMMENDED = ("maybe", "perhaps", "probably")
_CONJECTURE_EXCLUDED = ("might", "could be", "i think", "not sure", "i'm not sure")
_REJECT_RECOMMENDED = ("instead of", "rather than", "decided against", "ruled out")
_REJECT_NOW = ("instead of", "rather than", "rejected", "decided against", "ruled out")
_CAUSE_WORKAROUND = ("because", "caused by", "root cause", "due to", "→")
_CORRECTION = ("that's wrong", "not what i asked", "revert that", "undo that")

CANDIDATES = {
    "decision_assert_in_sentence": _ASSERT_RECOMMENDED + _ASSERT_EXCLUDED,
    "conjecture_in_sentence": _CONJECTURE_RECOMMENDED + _CONJECTURE_EXCLUDED,
    "preference_suffix": _PREFERENCE_P3 + ("please",),
    "hearsay": _HEARSAY_RECOMMENDED + _HEARSAY_EXCLUDED + ("you said that",),
    "self_stated_cause": _CAUSE_RECOMMENDED + _CAUSE_EXCLUDED,
    "workaround_avoid": _AVOID_RECOMMENDED + _AVOID_EXCLUDED,
    "decision_reject": _REJECT_NOW,
    "workaround_cause": _CAUSE_WORKAROUND,
    "user_correction": _CORRECTION,
}


def _empty_tables():
    return {role: () for role in ROLE_CONSTANTS}


def _variant(**overrides):
    table = _empty_tables()
    table.update(decision_reject=_REJECT_NOW, workaround_cause=_CAUSE_WORKAROUND, user_correction=_CORRECTION)
    table.update(overrides)
    return table


VARIANTS = {
    # P0 = 30e1f2c の en の表の写し (T4〜T7 の後も変わらない)
    "P0": _variant(),
    "P1": _variant(preference_suffix=_PREFERENCE_P1),
    "P2": _variant(preference_suffix=_PREFERENCE_P2),
    "P3": _variant(preference_suffix=_PREFERENCE_P3),
    # REC = §5 の推奨列の全部 (decision_reject は rejected を外す)
    "REC": _variant(
        decision_reject=_REJECT_RECOMMENDED,
        preference_suffix=_PREFERENCE_P1,
        decision_assert_in_sentence=_ASSERT_RECOMMENDED,
        hearsay=_HEARSAY_RECOMMENDED,
        self_stated_cause=_CAUSE_RECOMMENDED,
        workaround_avoid=_AVOID_RECOMMENDED,
        conjecture_in_sentence=_CONJECTURE_RECOMMENDED,
    ),
}


@contextlib.contextmanager
def en_tables(variant):
    """`markers.MARKERS["en"]` を `VARIANTS[variant]` の写しに差し替え、alaya の和集合の定数を計算し直して差し替える。
    抜けると入る前の値に戻る。"""
    from raiya import alaya, markers

    if variant not in VARIANTS:
        raise KeyError("変種は {} のどれか: {!r}".format(sorted(VARIANTS), variant))
    table = {role: tuple(words) for role, words in VARIANTS[variant].items()}
    with mock.patch.dict(markers.MARKERS, {"en": table}):
        patches = [mock.patch.object(alaya, const, markers.union(role)) for role, const in ROLE_CONSTANTS.items()]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield


# --- コーパスと語ごとの当たり ----------------------------------------------------------------------


def build_corpora(data):
    """鍵は evidence_user (has_answer の user の turn) / user (全 user) / assistant (全 assistant)。重複の本文と空白だけの本文は除く。"""
    seen = {name: set() for name in CORPORA}
    out = {name: [] for name in CORPORA}

    def add(name, text):
        if text.strip() and text not in seen[name]:
            seen[name].add(text)
            out[name].append(text)

    for q in data:
        for session in q["haystack_sessions"]:
            for turn in session:
                if turn["role"] == "user":
                    add("user", turn["content"])
                    if turn.get("has_answer") is True:
                        add("evidence_user", turn["content"])
                else:
                    add("assistant", turn["content"])
    return out


def _prepare(texts):
    """本文ごとに (`sansho._n` 済み, 問いでない文の列 = 末尾の 。.!！」 空白 引用符 ) を外したもの) を作る (語ごとに文を切り直さないため)。"""
    from raiya import alaya

    prepared = []
    for text in texts:
        n, pieces = alaya._sentence_pieces(text)
        sentences = [n[lo:hi].rstrip("。.!！」 \"')") for lo, hi, question in pieces if not question]
        prepared.append((n, sentences))
    return prepared


def _count_prepared(prepared, marker):
    from raiya import alaya

    m = alaya._marker_fold(marker)
    counts = dict.fromkeys(COLUMNS, 0)
    for n, sentences in prepared:
        if m in alaya._marker_fold(n):
            counts["substring"] += 1
        if alaya._has_marker(n, (marker,)):
            counts["word"] += 1
        if any(alaya._marker_fold(s).endswith(m) for s in sentences):
            counts["final"] += 1
        if any(alaya._has_marker(s, (marker,)) for s in sentences):
            counts["in_sentence"] += 1
    return counts


def count_candidate(texts, marker):
    """texts (本文の列) のうち、marker に当たる本文の数 (§1.3 の 4 列)。
    substring = 今の部分一致 (casefold) / word = R2 の境つき / final = 問いでない文の末尾 / in_sentence = 問いでない文の中 (境つき・R3)。"""
    return _count_prepared(_prepare(texts), marker)


# --- kind の移りと札 (純関数) -----------------------------------------------------------------------


def _evidence_proposals(data, declared):
    """根拠の user の往復ごとに、縁の腕 A で提案を作る。往復 1 件の窓を返す。縁が 0 語なら飛ばす。"""
    from raiya.engine import _imprint_gate_window
    from raiya.ledger import LedgerTurn

    for q in data:
        rts, _ = lm1.build_round_trips(q["haystack_session_ids"], q["haystack_sessions"])
        for rt in rts:
            if not (rt["user_ha"] and rt["user"].strip()):
                continue
            span = lm1._span_of("user", rt["user"])
            conds = lm1.choose_conditions("A", span, q["question"], q["answer"]) if span.strip() else []
            if not conds:
                continue
            proposal = {
                "content": span, "conditions": conds, "polarity": "無記", "kind": declared, "action": "new",
                "evidence": [{"source": "user", "span": span}],
            }
            window = _imprint_gate_window([LedgerTurn(user_text=rt["user"], assistant_text=rt["assistant"], id=1, life=1)])
            yield proposal, window, span, conds


def _judge(proposal, window):
    from raiya import alaya, sansho

    decision = sansho.evaluate_proposal(proposal, window)
    kind, _note = alaya._resolve_kind(proposal, decision["spans"], decision["grounded_conditions"], window)
    return decision, kind


def kind_shift(data, variant, declared):
    """鍵 "<sansho>/<kind>" → 本数。縁の腕 A・往復 1 件の窓・純関数 (`evaluate_proposal` → `_resolve_kind`。imprint の L は通さない)。"""
    counts = {}
    with en_tables(variant):
        for proposal, window, _span, _conds in _evidence_proposals(data, declared):
            decision, kind = _judge(proposal, window)
            key = "{}/{}".format(decision["sansho"], kind)
            counts[key] = counts.get(key, 0) + 1
    return counts


def label_counts(data, variant):
    """決定 として残る根拠の提案 (申告 決定) を種子 {"kind", "conditions", "evidences"} にして札を数える。
    decision = 決定 で残る本数 / doubt_only = `doubt_judgement` の only が空でない / doubt_keep = keep_summary / conjecture = `conjecture_only`。"""
    from raiya import alaya

    counts = {"decision": 0, "doubt_only": 0, "doubt_keep": 0, "conjecture": 0}
    with en_tables(variant):
        for proposal, window, span, conds in _evidence_proposals(data, "決定"):
            decision, kind = _judge(proposal, window)
            if decision["sansho"] != "圓成實" or kind != "決定":
                continue
            evidence = {"source": "user", "span": span}
            seed = {"kind": "決定", "conditions": list(conds), "evidences": [evidence]}
            counts["decision"] += 1
            only, keep = alaya.doubt_judgement(seed, [evidence])
            if only:
                counts["doubt_only"] += 1
            if keep:
                counts["doubt_keep"] += 1
            if alaya.conjecture_only(seed, [evidence]):
                counts["conjecture"] += 1
    return counts


def injected(data, variant, edges=("A",)):
    """腕 (例 "A-a") → {クラス: 問いの数}。段 1 の `run_question` を `en_tables(variant)` の下で呼んで数える (実エンジン)。"""
    from raiya import config

    arms = {}
    with lm1._env(RAIYA_CONFIG=""):
        config._reset()
        try:
            with en_tables(variant):
                for q in data:
                    row, _shape, _timing = lm1.run_question(q, edges, False)
                    for arm, arm_row in row["arms"].items():
                        classes = arms.setdefault(arm, {})
                        classes[arm_row["class"]] = classes.get(arm_row["class"], 0) + 1
        finally:
            config._reset()
    return arms


# --- 実行 -------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="LongMemEval 段 2 (en の印の表・LLM 不使用・決定論) の測定")
    ap.add_argument("--data", required=True, help="longmemeval_*.json の path (リポジトリの外)")
    ap.add_argument("--out", required=True, help="結果の dir (必須。リポジトリの外を指す)")
    ap.add_argument("--name", default=DEFAULT_NAME, help="結果のファイル名")
    ap.add_argument("--variants", default=",".join(DEFAULT_VARIANTS), help="変種 (カンマ区切り)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    variants = tuple(v for v in args.variants.split(",") if v)
    if not variants or any(v not in VARIANTS for v in variants):
        print("--variants は {} のカンマ区切り".format("/".join(sorted(VARIANTS))), file=sys.stderr)
        return 2
    try:
        data, sha, nbytes = lm1.load_data(args.data)
    except lm1.DataError as e:
        print(str(e), file=sys.stderr)
        return 1
    progress = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    from raiya import config

    with lm1._env(RAIYA_CONFIG=""):
        config._reset()
        try:
            progress("corpora ...")
            corpora = build_corpora(data)
            candidates = {}
            prepared = {name: _prepare(texts) for name, texts in corpora.items()}
            for role, words in CANDIDATES.items():
                candidates[role] = {
                    word: {name: _count_prepared(prepared[name], word) for name in CORPORA} for word in words
                }
            result = {
                "schema": SCHEMA,
                "meta": {
                    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "data_sha256": sha, "data_bytes": nbytes, "questions": len(data), "variants": list(variants),
                    "python": platform.python_version(),
                },
                "corpora": {name: len(texts) for name, texts in corpora.items()},
                "candidates": candidates,
                "kind_shift": {},
                "labels": {},
                "injected": {},
            }
            for v in variants:
                progress("variant {} ...".format(v))
                result["kind_shift"][v] = {d: kind_shift(data, v, d) for d in DECLARED_KINDS}
                result["labels"][v] = label_counts(data, v)
                result["injected"][v] = injected(data, v)
        finally:
            config._reset()
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, args.name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")
    print("wrote {}".format(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
