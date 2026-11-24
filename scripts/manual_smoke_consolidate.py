#!/usr/bin/env python3
"""manual_smoke_consolidate.py -- 改修4 hooks の consolidate 実測 (手動1本)

対話本文は台本で固定し、ライブなのは `engine.consolidate` の LLM 呼び出しだけ。
流れは hooks の本番経路に合わせる:

    record_turn (stop と同じ <private> 除去・貼り付けの根拠化・8000字切り詰め)
      -> 未統合が CONSOLIDATE_EVERY (5) 件で consolidate
      -> 端数は session_end と同じく consolidate

失敗にするのはゲートの破れだけ。モデルの三性内訳・却下・skipped・P-M1/P-M2 は記録する。
決定の却下印が content にしか無い件数は `decision_marker_outside_span` に記録する (成否にはしない)。
全統合が BackendError または unparseable の受領証は `blocked: true` とし、成功として再走を止めない。
受領証は receipts/smoke_consolidate.json。smoke.json / smoke_long.json は書かない。

ライブ実行は openai-api / gpt-6-luna。鍵 (OPENAI_API_KEY または RAIYA_OPENAI_API_KEY)
が無いときは受領証を書かずに終了する。`call_llm` を渡したドライランは鍵を見ない。

実行: python3 scripts/manual_smoke_consolidate.py [--force]
"""
import argparse
import datetime
import json
import os
import sys
import tempfile
from collections import Counter

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (SCRIPTS_DIR, BASE_DIR):  # 兄弟スクリプトと raiya パッケージ (ルート) を import 可能にする
    if _p not in sys.path:
        sys.path.insert(0, _p)

from raiya import sansho  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.hooks.claude_code import (  # noqa: E402
    CONSOLIDATE_EVERY,
    TRUNCATE_LIMIT,
    _is_skippable_prompt,
    _truncate,
)
from raiya.ledger import gate_text, strip_pasted, strip_private  # noqa: E402

RECEIPT_NAME = "smoke_consolidate.json"
PROJECT_A = "/proj/raiya-consolidate-a"
PROJECT_B = "/proj/raiya-consolidate-b"

# ゲートが見える本文には出さない高エントロピーな印。種子の span / content に出たら破れ。
PASTE_TOKEN = "PASTEONLY9f3a7c"
PRIVATE_TOKEN = "PRIVATEONLY9f3a7c"
MIDDLE_TOKEN = "MIDDLEONLY9f3a7c"
HEAD_EDGE = "HEADJOIN9f3a7c"
TAIL_EDGE = "TAILJOIN9f3a7c"
JOIN_TOKEN = HEAD_EDGE + TAIL_EDGE
FORBIDDEN_TOKENS = (PASTE_TOKEN, PRIVATE_TOKEN, MIDDLE_TOKEN, JOIN_TOKEN)

SPAN_FLOCK = "ロックは flock を採用する (新しいロックファイルは却下: 既存の .lock で足りる)。"
SPAN_PORTALOCKER = "portalocker は今回は採らない"
SPAN_SQLITE = "別リポジトリでは sqlite を採用する (postgres は却下: 手元にサーバが無い)。"
SPAN_TAB = "タブ幅を4のまま"
SPAN_WORKAROUND = "統合がタイムアウトした原因は子プロセスのフック再入だったため、RAIYA_HOOK_ACTIVE を立てて回避した。"
SPAN_LOCATION = "本番の state はホームディレクトリの .raiya にあり、リポジトリの外です。"
SPAN_RESUME = "中断点は consolidate の受領証で、次の一手は session_end の端数を確認することです。"

MODEL = "gpt-6-luna"
BACKEND = "openai-api"


def _receipts_dir():
    return os.path.join(BASE_DIR, "receipts")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def build_long_user():
    """8000字切り詰めの頭と尾を繋がないと成立しない文、および省略される中間だけにある印を置く。

    台帳に残るのは先頭 limit//2 と末尾の残りだけ。JOIN_TOKEN は原文でも省略後でも
    部分文字列にならない (中間、またはゲート用の U+FFFC が間に入る)。
    """
    limit = TRUNCATE_LIMIT
    head_len = limit // 2
    tail_len = limit - head_len
    middle_len = max(len(MIDDLE_TOKEN), 100)
    prefix = "長いログの要点は、切り詰めの外側にある文だけが根拠になることです。"
    if head_len <= len(prefix) + len(HEAD_EDGE) or tail_len <= len(TAIL_EDGE):
        raise RuntimeError("TRUNCATE_LIMIT が短すぎて切り詰め罠を置けません")
    if middle_len <= len(MIDDLE_TOKEN):
        middle = MIDDLE_TOKEN
    else:
        middle = MIDDLE_TOKEN + ("中" * (middle_len - len(MIDDLE_TOKEN)))
    head = prefix + ("頭" * (head_len - len(prefix) - len(HEAD_EDGE))) + HEAD_EDGE
    tail = TAIL_EDGE + ("尾" * (tail_len - len(TAIL_EDGE)))
    text = head + middle + tail
    truncated, did = _truncate(text, limit)
    gated = gate_text(truncated)
    if not did or JOIN_TOKEN in text or JOIN_TOKEN in gated or MIDDLE_TOKEN in gated:
        raise RuntimeError("切り詰め罠の配置に失敗しました")
    if HEAD_EDGE not in gated or TAIL_EDGE not in gated:
        raise RuntimeError("切り詰め後の頭と尾に端の印が残っていません")
    return text


def scripted_turns():
    """8往復。5件目で stop 相当の consolidate、残り3件は session_end 相当。

    最初の窓は project が2つ。後の窓に貼り付け・private・切り詰めを置く。
    アシスタントだけが言う別案 (portalocker) はユーザー本文に無い。
    """
    long_user = build_long_user()
    return [
        {
            "label": "decision",
            "project": PROJECT_A,
            "user": SPAN_FLOCK,
            "assistant": "flock を採用し、新しいロックファイルは却下ですね。代替案の {}。".format(SPAN_PORTALOCKER),
        },
        {
            "label": "workaround",
            "project": PROJECT_A,
            "user": SPAN_WORKAROUND,
            "assistant": "原因はフック再入で、回避は RAIYA_HOOK_ACTIVE を立てることですね。",
        },
        {
            "label": "location",
            "project": PROJECT_A,
            "user": SPAN_LOCATION,
            "assistant": "本番 state の所在はホームディレクトリの .raiya ですね。",
        },
        {
            "label": "resume",
            "project": PROJECT_A,
            "user": SPAN_RESUME,
            "assistant": "再開点は受領証、次の一手は session_end の確認ですね。",
        },
        {
            "label": "other_project",
            "project": PROJECT_B,
            "user": SPAN_SQLITE,
            "assistant": "sqlite を採用し、postgres は却下ですね。",
        },
        {
            "label": "paste_and_private",
            "project": PROJECT_A,
            "user": (
                "公開する方針は{}にする。\n"
                '<pasted_content id="log1">貼り付けだけの決定として本番DBをOracleに変える '
                "(MySQLは却下) {}</pasted_content id=\"log1\">\n"
                "<private>秘密の好みはタブ幅を1にしたい {}</private>"
            ).format(SPAN_TAB, PASTE_TOKEN, PRIVATE_TOKEN),
            "assistant": "タブ幅は4のまま、という方針ですね。添付と非公開の部分は根拠にしません。",
        },
        {
            "label": "elision",
            "project": PROJECT_A,
            "user": long_user,
            "assistant": "長いログは切り詰めの外側だけを見ます。",
        },
        {
            "label": "close",
            "project": PROJECT_A,
            "user": "今日の作業はここまでで、受領証を残して終える。",
            "assistant": "受領証を残して終える、了解しました。",
        },
    ]


def _record_like_stop(engine, turn):
    """cmd_stop と同じ順で private を除き、根拠から貼り付けを剥がし、切ってから record_turn する。"""
    user = strip_private(turn["user"])
    assistant = strip_private(turn["assistant"])
    grounding_full = strip_pasted(user)
    if (
        _is_skippable_prompt(user)
        or _is_skippable_prompt(grounding_full)
        or not assistant.strip()
    ):
        raise RuntimeError("台本の往復 {} は hook が台帳に入れない形です".format(turn["label"]))
    grounding, _ = _truncate(grounding_full, TRUNCATE_LIMIT)
    user_rec, _ = _truncate(user, TRUNCATE_LIMIT)
    assistant_rec, _ = _truncate(assistant, TRUNCATE_LIMIT)
    recorded = engine.record_turn(
        user_rec, assistant_rec, "consolidate-smoke",
        grounding_text=grounding, project=turn["project"],
    )
    return {
        "label": turn["label"],
        "project": turn["project"],
        "ledger_id": recorded["ledger_id"],
        "pending_after": recorded["pending"],
        "gate_user": gate_text(grounding),
        "gate_assistant": gate_text(assistant_rec),
    }


def _consolidate_once(engine):
    notes = []
    try:
        result = engine.consolidate(notes=notes)
    except Exception as e:
        reason = getattr(e, "reason", None)
        if reason is None:
            raise
        return {
            "window": [],
            "proposals": [],
            "decisions": [],
            "skipped": "backend_error",
            "error_reason": reason,
            "parse_notes": list(notes),
        }
    return {
        "window": list(result.get("window") or []),
        "proposals": list(result.get("proposals") or []),
        "decisions": list(result.get("decisions") or []),
        "skipped": result.get("skipped"),
        "parse_notes": list(notes),
    }


def find_gate_breaches(seeds, visible_texts, recall, other_project_contents):
    """ゲートの破れだけを返す。モデルが隔離や却下を出したことは破れにしない。

    - 種子の span が、ゲートに渡したどの本文にも無い (貼り付け・private・切れ目を跨ぐ引用が採用された)
    - 禁止印 (貼り付け / private / 省略された中間 / 頭と尾の結合) が種子の span か content にある
    - project A の recall (性向注入オフ) に、他 project の種子・その content・性向が混ざる
    """
    breaches = []

    def grounded(span):
        n = sansho._n(span or "")
        if not n.strip():
            return False
        return any(n in sansho._n(text or "") for text in visible_texts)

    for seed in seeds:
        spans = [(ev.get("span") or "") for ev in (seed.get("evidences") or [])]
        blob = "\n".join(spans + [seed.get("content") or ""])
        for token in FORBIDDEN_TOKENS:
            if token and token in blob:
                breaches.append({
                    "type": "forbidden_token",
                    "token": token,
                    "seed_id": seed.get("id"),
                    "kind": seed.get("kind"),
                    "project": seed.get("project"),
                })
        for span in spans:
            if span.strip() and not grounded(span):
                breaches.append({
                    "type": "ungrounded_span",
                    "seed_id": seed.get("id"),
                    "span": span[:200],
                })

    recalled_items = list(recall.get("seeds") or []) + list(recall.get("index") or [])
    if recall.get("resume"):
        recalled_items.append(recall["resume"])
    for item in recalled_items:
        project = item.get("project")
        if isinstance(project, str) and project != PROJECT_A:
            breaches.append({
                "type": "cross_project",
                "seed_id": item.get("id"),
                "project": project,
                "kind": item.get("kind"),
            })
        if item.get("kind") == "性向":
            breaches.append({
                "type": "disposition",
                "seed_id": item.get("id"),
                "where": "recall",
            })
    narrative = recall.get("narrative") or ""
    for content in other_project_contents:
        if content and content in narrative:
            breaches.append({"type": "cross_project_narrative", "content": content[:200]})
    for token in FORBIDDEN_TOKENS:
        if token in narrative:
            breaches.append({"type": "forbidden_token_narrative", "token": token})
    return breaches


def _seed_spans(seed):
    """alaya の evidences と受領証の spans の両方から span 文字列を取る。"""
    evidences = seed.get("evidences")
    if isinstance(evidences, list):
        return [ev.get("span") or "" for ev in evidences if isinstance(ev, dict)]
    return [span for span in (seed.get("spans") or []) if isinstance(span, str)]


def decision_markers_outside_span(seeds):
    """決定の種子のうち、却下側の印が content にあり、接地 span には無いもの。

    kind の形式検査は content を見る。ゲートは span と縁しか見ないので、
    LLM が content に「却下」等を足しただけではゲート破れにしない。
    """
    from raiya.alaya import DECISION_REJECT_MARKERS

    found = []
    for seed in seeds:
        if seed.get("kind") != "決定":
            continue
        content = seed.get("content") or ""
        span_blob = "\n".join(_seed_spans(seed))
        in_content = [marker for marker in DECISION_REJECT_MARKERS if marker in content]
        in_span = [marker for marker in DECISION_REJECT_MARKERS if marker in span_blob]
        if in_content and not in_span:
            found.append({"seed_id": seed.get("id"), "markers": in_content})
    return found


_UNJUDGED_SKIPS = ("backend_error", "unparseable")


def _no_judged_consolidation(consolidations):
    """統合が1回以上走り、どの回も提案を判定していない。

    BackendError と、配列として読めない出力 (unparseable) はどちらも窓を消費しない。
    全部がそうなら提案 0 のまま gate.ok になるので、成功として再走を止めない。
    1回でも判定できていれば (skipped が None や already_consolidated) blocked にしない。
    """
    return bool(consolidations) and all(step.get("skipped") in _UNJUDGED_SKIPS for step in consolidations)


def _summarize(imprint_log, seeds, consolidations):
    decisions = [e.get("decision") for e in imprint_log]
    class_counts = {
        "圓成實": decisions.count("圓成實"),
        "依他起": decisions.count("依他起"),
        "遍計所執": decisions.count("遍計所執"),
        "reject": decisions.count("reject"),
    }
    reject_reasons = Counter(e.get("reason") or "" for e in imprint_log if e.get("decision") == "reject")
    proposal_kinds = Counter(
        (e.get("proposal") or {}).get("kind")
        for e in imprint_log
        if (e.get("proposal") or {}).get("kind")
    )
    seed_kinds = Counter((s.get("kind") or "性向") for s in seeds)
    outside = decision_markers_outside_span(seeds)
    measured = {
        "proposal_total": len(imprint_log),
        "class_counts": class_counts,
        "reject_reason_distribution": dict(reject_reasons),
        "quarantine_count": class_counts["遍計所執"],
        "seed_count": len(seeds),
        "proposal_kind_counts": dict(proposal_kinds),
        "seed_kind_counts": dict(seed_kinds),
        "decision_marker_outside_span": len(outside),
        "decision_marker_outside_span_ids": [row["seed_id"] for row in outside],
    }
    steps = []
    for step in consolidations:
        step_decisions = [d.get("decision") for d in step.get("decisions") or []]
        steps.append({
            "window": step.get("window") or [],
            "proposal_count": len(step.get("proposals") or []),
            "skipped": step.get("skipped"),
            "error_reason": step.get("error_reason"),
            "parse_notes": step.get("parse_notes") or [],
            "class_counts": dict(Counter(step_decisions)),
        })
    return measured, steps


def _recall_for_gate(engine):
    """性向注入を切った状態で project A / B を想起する。環境変数は戻す。"""
    saved = os.environ.pop("RAIYA_INJECT_DISPOSITIONS", None)
    try:
        recall_a = engine.recall("sqlite postgres", project=PROJECT_A)
        recall_b = engine.recall("sqlite", project=PROJECT_B)
    finally:
        if saved is not None:
            os.environ["RAIYA_INJECT_DISPOSITIONS"] = saved
    return recall_a, recall_b


def _apply_live_backend():
    os.environ["RAIYA_BACKEND"] = BACKEND
    os.environ["RAIYA_MODEL"] = MODEL
    os.environ["RAIYA_PROPOSE_MODEL"] = MODEL


def live_key():
    return os.environ.get("RAIYA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""


def run(call_llm=None, state_dir=None, receipts_dir=None, force=False):
    receipts_dir = receipts_dir or _receipts_dir()
    os.makedirs(receipts_dir, exist_ok=True)
    out_path = os.path.join(receipts_dir, RECEIPT_NAME)
    if os.path.exists(out_path) and not force:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if not existing.get("blocked"):
            print(
                "receipts/{} は既にあります (手動1本の規律=再走しない)。"
                " 再走するには --force を指定してください。".format(RECEIPT_NAME),
                file=sys.stderr,
            )
            return existing

    own_state = state_dir is None
    if own_state:
        state_dir = tempfile.mkdtemp(prefix="raiya_smoke_consolidate_")
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    engine.begin_new_life()

    patched = None
    if call_llm is not None:
        import raiya.backend as backend

        patched = backend.call_llm

        def _wrapped(prompt, max_tokens=1024, purpose=None):
            return call_llm(prompt, max_tokens=max_tokens, purpose=purpose)

        backend.call_llm = _wrapped
    else:
        _apply_live_backend()

    try:
        prepared = []
        consolidations = []
        for turn in scripted_turns():
            prepared.append(_record_like_stop(engine, turn))
            if prepared[-1]["pending_after"] >= CONSOLIDATE_EVERY:
                consolidations.append(_consolidate_once(engine))
        consolidations.append(_consolidate_once(engine))
    finally:
        if patched is not None:
            import raiya.backend as backend

            backend.call_llm = patched

    # 保存形式を知らない読み取り (engine の read-only 動詞): 全種子 (superseded / 取り下げも含む) と imprint_log 全件。
    seeds = engine.inspect_seeds(include_superseded=True, include_withdrawn=True)
    imprint_log = engine.imprint_log_rows()

    recall_a, recall_b = _recall_for_gate(engine)
    visible = []
    for row in prepared:
        visible.append(row["gate_user"])
        visible.append(row["gate_assistant"])
    other_contents = [s.get("content") or "" for s in seeds if s.get("project") == PROJECT_B]
    breaches = find_gate_breaches(seeds, visible, recall_a, other_contents)
    measured, steps = _summarize(imprint_log, seeds, consolidations)

    import receipt_harness

    slim = {
        k: measured[k]
        for k in ("proposal_total", "class_counts", "reject_reason_distribution", "quarantine_count", "seed_count")
    }
    receipt = {
        "generated_at": _now(),
        "blocked": _no_judged_consolidation(steps),
        "dry_run": call_llm is not None,
        "backend": None if call_llm is not None else BACKEND,
        "model": None if call_llm is not None else MODEL,
        "state_dir": state_dir,
        "turns": [
            {"label": row["label"], "project": row["project"], "ledger_id": row["ledger_id"]}
            for row in prepared
        ],
        "consolidations": steps,
        "measured": measured,
        "seeds": [
            {
                "id": s.get("id"),
                "kind": s.get("kind"),
                "project": s.get("project"),
                "sansho": s.get("sansho"),
                "content": s.get("content"),
                "spans": [ev.get("span") for ev in (s.get("evidences") or [])],
            }
            for s in seeds
        ],
        "recall_project_a": {
            "seed_ids": [s.get("id") for s in recall_a.get("seeds") or []],
            "index_ids": [s.get("id") for s in recall_a.get("index") or []],
            "kinds": [s.get("kind") for s in (recall_a.get("seeds") or []) + (recall_a.get("index") or [])],
        },
        "recall_project_b_seed_ids": [s.get("id") for s in recall_b.get("seeds") or []],
        "P-M1": receipt_harness.judge_p_m1(slim),
        "P-M2": receipt_harness.judge_p_m2(slim),
        "gate": {"ok": not breaches, "breaches": breaches},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)
    print(json.dumps(
        {
            "gate": receipt["gate"],
            "measured": measured,
            "consolidations": steps,
            "P-M1": receipt["P-M1"]["hit"],
            "P-M2": receipt["P-M2"]["hit"],
            "receipt": out_path,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="既存の receipts/{} を再走して上書きする".format(RECEIPT_NAME))
    args = parser.parse_args(argv)
    if not live_key():
        print(
            "OPENAI_API_KEY (または RAIYA_OPENAI_API_KEY) が無いため、ライブ実行の前で止めました。"
            " 受領証は書いていません。",
            file=sys.stderr,
        )
        print(
            "実行: RAIYA_BACKEND={backend} RAIYA_MODEL={model} RAIYA_PROPOSE_MODEL={model} "
            "OPENAI_API_KEY=... python3 scripts/manual_smoke_consolidate.py".format(backend=BACKEND, model=MODEL),
            file=sys.stderr,
        )
        return 2
    receipt = run(force=args.force)
    if receipt.get("blocked"):
        return 0
    return 0 if receipt.get("gate", {}).get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
