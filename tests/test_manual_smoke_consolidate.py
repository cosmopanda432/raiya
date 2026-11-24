"""manual_smoke_consolidate.py のドライラン。

ライブの LLM は呼ばない。`call_llm` を台本の窓ごとに固定 JSON へ差し替え、
record_turn → 5件で consolidate → session_end の consolidate、
およびゲート破れの検出を確認する。
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import manual_smoke_consolidate as smoke  # noqa: E402
from raiya.engine import CONSOLIDATE_MAX_TOKENS  # noqa: E402

REPO_RECEIPTS = os.path.join(smoke.BASE_DIR, "receipts")


def _dir_digest(path):
    digest = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        digest.update(name.encode())
        with open(full, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def _proposal(kind, content, span, conditions, source="user"):
    return {
        "kind": kind,
        "action": "new",
        "target_id": None,
        "content": content,
        "conditions": conditions,
        "polarity": "無記",
        "evidence": [{"source": source, "span": span}],
    }


def _fake_llm(calls):
    def call_llm(prompt, max_tokens=1024, purpose=None):
        calls.append({"max_tokens": max_tokens, "purpose": purpose, "prompt": prompt})
        if "タブ幅" in prompt or "長いログの要点" in prompt:
            payload = [
                _proposal("性向", "タブ幅を4のままにすることを好む", smoke.SPAN_TAB, ["タブ幅", "方針"]),
                _proposal("決定", "貼り付けだけの決定", smoke.PASTE_TOKEN, []),
                _proposal("性向", "秘密の好み", smoke.PRIVATE_TOKEN, []),
                _proposal("決定", "切れ目を跨いだ文", smoke.JOIN_TOKEN, []),
                _proposal("所在", "省略された中間", smoke.MIDDLE_TOKEN, []),
            ]
        else:
            payload = [
                _proposal("決定", smoke.SPAN_FLOCK, smoke.SPAN_FLOCK, ["flock"]),
                _proposal("決定", smoke.SPAN_PORTALOCKER, smoke.SPAN_PORTALOCKER, ["portalocker"], source="assistant"),
                _proposal("決定", smoke.SPAN_SQLITE, smoke.SPAN_SQLITE, ["sqlite"]),
                _proposal("決定", smoke.SPAN_FLOCK, smoke.SPAN_FLOCK, ["存在しない縁語XYZ"]),
            ]
        return json.dumps(payload, ensure_ascii=False)

    return call_llm


class TestConsolidateSmokeDryRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state = os.path.join(self.tmpdir, "state")
        self.receipts = os.path.join(self.tmpdir, "receipts")
        self._repo_digest = _dir_digest(REPO_RECEIPTS)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.assertEqual(_dir_digest(REPO_RECEIPTS), self._repo_digest)

    def test_dry_run_consolidates_at_five_then_session_end_and_blocks_traps(self):
        calls = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            receipt = smoke.run(
                call_llm=_fake_llm(calls),
                state_dir=self.state,
                receipts_dir=self.receipts,
            )
        self.assertTrue(receipt["dry_run"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["purpose"] == "propose" for c in calls))
        self.assertTrue(all(c["max_tokens"] == CONSOLIDATE_MAX_TOKENS for c in calls))
        self.assertEqual([step["window"] for step in receipt["consolidations"]], [[1, 2, 3, 4, 5], [6, 7, 8]])
        self.assertTrue(all(step["skipped"] is None for step in receipt["consolidations"]))

        measured = receipt["measured"]
        self.assertEqual(measured["class_counts"], {"圓成實": 3, "依他起": 1, "遍計所執": 4, "reject": 1})
        self.assertEqual(measured["seed_count"], 4)
        self.assertEqual(measured["proposal_kind_counts"].get("性向"), 2)
        # spec 2026-10-30 §7 H: assistant だけの span の 決定 (portalocker) は 性向 に読み替わる
        self.assertEqual(measured["seed_kind_counts"].get("性向"), 2)
        self.assertEqual(measured["seed_kind_counts"].get("決定"), 2)
        self.assertTrue(receipt["P-M1"]["hit"])
        self.assertFalse(receipt["P-M2"]["hit"])
        self.assertTrue(receipt["gate"]["ok"], receipt["gate"]["breaches"])
        self.assertFalse(receipt["blocked"])
        self.assertEqual(measured["decision_marker_outside_span"], 0)
        self.assertEqual(measured["decision_marker_outside_span_ids"], [])

        blobs = []
        for seed in receipt["seeds"]:
            blobs.append(seed.get("content") or "")
            blobs.extend(seed.get("spans") or [])
        blob = "\n".join(blobs)
        for token in smoke.FORBIDDEN_TOKENS:
            self.assertNotIn(token, blob)

        projects = {seed["content"]: seed["project"] for seed in receipt["seeds"]}
        self.assertEqual(projects[smoke.SPAN_SQLITE], smoke.PROJECT_B)
        self.assertEqual(projects[smoke.SPAN_FLOCK], smoke.PROJECT_A)
        sqlite_ids = [s["id"] for s in receipt["seeds"] if s["content"] == smoke.SPAN_SQLITE]
        self.assertEqual(len(sqlite_ids), 1)
        self.assertNotIn(sqlite_ids[0], receipt["recall_project_a"]["seed_ids"])
        self.assertNotIn(sqlite_ids[0], receipt["recall_project_a"]["index_ids"])
        self.assertIn(sqlite_ids[0], receipt["recall_project_b_seed_ids"])
        self.assertNotIn("性向", receipt["recall_project_a"]["kinds"])

        written = os.path.join(self.receipts, smoke.RECEIPT_NAME)
        self.assertTrue(os.path.isfile(written))
        self.assertFalse(os.path.exists(os.path.join(self.receipts, "smoke.json")))
        self.assertFalse(os.path.exists(os.path.join(self.receipts, "smoke_long.json")))

    def test_existing_receipt_short_circuits_without_calling_llm(self):
        os.makedirs(self.receipts)
        out = os.path.join(self.receipts, smoke.RECEIPT_NAME)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"blocked": False, "generated_at": "existing-marker", "gate": {"ok": True, "breaches": []}}, f)

        def _explode(*args, **kwargs):
            raise AssertionError("call_llm が呼ばれてはいけない")

        with contextlib.redirect_stderr(io.StringIO()):
            receipt = smoke.run(call_llm=_explode, state_dir=self.state, receipts_dir=self.receipts, force=False)
        self.assertEqual(receipt["generated_at"], "existing-marker")

    def test_backend_error_on_every_window_is_blocked_and_does_not_lock_a_rerun(self):
        from raiya.backend import BackendError

        calls = []

        def _fail(prompt, max_tokens=1024, purpose=None):
            calls.append(purpose)
            raise BackendError("openai-api が空の応答を返しました (model=gpt-6-luna, finish_reason=length)")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            first = smoke.run(call_llm=_fail, state_dir=self.state, receipts_dir=self.receipts)
        # 失敗した統合は窓を消費しない。未統合が 5 件のままなので、5件目以降の各往復と
        # session_end がそれぞれ propose を呼ぶ。
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(purpose == "propose" for purpose in calls))
        self.assertTrue(first["blocked"])
        self.assertTrue(first["gate"]["ok"])
        self.assertEqual(first["measured"]["proposal_total"], 0)
        self.assertEqual(first["measured"]["seed_count"], 0)
        self.assertGreaterEqual(len(first["consolidations"]), 2)
        self.assertTrue(all(step["skipped"] == "backend_error" for step in first["consolidations"]))
        self.assertTrue(all(step["error_reason"] for step in first["consolidations"]))
        self.assertNotIn(smoke.SPAN_FLOCK, first["consolidations"][0]["error_reason"])

        after_first = len(calls)
        again = os.path.join(self.tmpdir, "state2")
        with contextlib.redirect_stdout(io.StringIO()):
            second = smoke.run(call_llm=_fail, state_dir=again, receipts_dir=self.receipts, force=False)
        self.assertGreater(len(calls), after_first)
        self.assertTrue(second["blocked"])
        self.assertNotEqual(second["generated_at"], first["generated_at"])

    def test_unparseable_on_every_window_is_blocked_and_does_not_lock_a_rerun(self):
        calls = []

        def _unreadable(prompt, max_tokens=1024, purpose=None):
            calls.append(purpose)
            return "これは配列ではありません"

        with contextlib.redirect_stdout(io.StringIO()):
            first = smoke.run(call_llm=_unreadable, state_dir=self.state, receipts_dir=self.receipts)
        # 読めない出力は窓を消費しない。未統合が 5 件のままなので、5件目以降の各往復と
        # session_end がそれぞれ propose を呼ぶ。窓は未消費のまま伸びる。
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(purpose == "propose" for purpose in calls))
        self.assertTrue(first["blocked"])
        self.assertTrue(first["gate"]["ok"])
        self.assertEqual(first["measured"]["proposal_total"], 0)
        self.assertEqual(first["measured"]["seed_count"], 0)
        windows = [step["window"] for step in first["consolidations"]]
        self.assertEqual(
            windows,
            [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 4, 5, 6, 7, 8]],
        )
        self.assertTrue(all(step["skipped"] == "unparseable" for step in first["consolidations"]))
        self.assertTrue(all(step["parse_notes"] for step in first["consolidations"]))

        after_first = len(calls)
        again = os.path.join(self.tmpdir, "state-unparseable")
        with contextlib.redirect_stdout(io.StringIO()):
            second = smoke.run(call_llm=_unreadable, state_dir=again, receipts_dir=self.receipts, force=False)
        self.assertGreater(len(calls), after_first)
        self.assertTrue(second["blocked"])
        self.assertNotEqual(second["generated_at"], first["generated_at"])

    def test_one_unparseable_window_keeps_turns_for_the_next_and_is_not_blocked(self):
        calls = []
        flock = json.dumps(
            [_proposal("決定", smoke.SPAN_FLOCK, smoke.SPAN_FLOCK, ["flock"])],
            ensure_ascii=False,
        )

        def _fail_once(prompt, max_tokens=1024, purpose=None):
            calls.append(purpose)
            if len(calls) == 1:
                return "これは配列ではありません"
            return flock

        with contextlib.redirect_stdout(io.StringIO()):
            receipt = smoke.run(call_llm=_fail_once, state_dir=self.state, receipts_dir=self.receipts)
        self.assertEqual(len(calls), 3)
        self.assertFalse(receipt["blocked"])
        self.assertTrue(receipt["gate"]["ok"], receipt["gate"]["breaches"])
        steps = receipt["consolidations"]
        self.assertEqual([step["window"] for step in steps], [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 6], [7, 8]])
        self.assertEqual([step["skipped"] for step in steps], ["unparseable", None, None])
        planted = [s for s in receipt["seeds"] if s.get("content") == smoke.SPAN_FLOCK]
        self.assertEqual(len(planted), 1)
        self.assertEqual(planted[0]["sansho"], "圓成實")

        def _explode(*args, **kwargs):
            raise AssertionError("blocked でない受領証は再走を止める")

        with contextlib.redirect_stderr(io.StringIO()):
            again = smoke.run(call_llm=_explode, state_dir=self.state, receipts_dir=self.receipts, force=False)
        self.assertEqual(again["generated_at"], receipt["generated_at"])
        self.assertFalse(again["blocked"])

    def test_empty_array_consumes_the_window_and_locks_a_rerun(self):
        calls = []

        def _empty(prompt, max_tokens=1024, purpose=None):
            calls.append(purpose)
            return "[]"

        with contextlib.redirect_stdout(io.StringIO()):
            receipt = smoke.run(call_llm=_empty, state_dir=self.state, receipts_dir=self.receipts)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(purpose == "propose" for purpose in calls))
        self.assertFalse(receipt["blocked"])
        self.assertTrue(receipt["gate"]["ok"], receipt["gate"]["breaches"])
        self.assertEqual(receipt["measured"]["proposal_total"], 0)
        self.assertEqual(receipt["measured"]["seed_count"], 0)
        steps = receipt["consolidations"]
        self.assertEqual([step["window"] for step in steps], [[1, 2, 3, 4, 5], [6, 7, 8]])
        self.assertTrue(all(step["skipped"] is None for step in steps))

        def _explode(*args, **kwargs):
            raise AssertionError("空配列の受領証は再走を止める")

        with contextlib.redirect_stderr(io.StringIO()):
            again = smoke.run(call_llm=_explode, state_dir=self.state, receipts_dir=self.receipts, force=False)
        self.assertEqual(again["generated_at"], receipt["generated_at"])
        self.assertFalse(again["blocked"])

    def test_forbidden_token_in_admitted_content_is_a_gate_breach_and_locks_a_rerun(self):
        calls = []
        tainted = smoke.SPAN_FLOCK + smoke.PASTE_TOKEN
        payload = json.dumps(
            [_proposal("決定", tainted, smoke.SPAN_FLOCK, ["flock"])],
            ensure_ascii=False,
        )

        def _taint(prompt, max_tokens=1024, purpose=None):
            calls.append(purpose)
            return payload

        with contextlib.redirect_stdout(io.StringIO()):
            receipt = smoke.run(call_llm=_taint, state_dir=self.state, receipts_dir=self.receipts)
        self.assertEqual(len(calls), 2)
        self.assertFalse(receipt["blocked"])
        self.assertFalse(receipt["gate"]["ok"])
        types = {b["type"] for b in receipt["gate"]["breaches"]}
        self.assertIn("forbidden_token", types)
        self.assertIn("forbidden_token_narrative", types)
        planted = [s for s in receipt["seeds"] if smoke.PASTE_TOKEN in (s.get("content") or "")]
        self.assertEqual(len(planted), 1)
        self.assertEqual(planted[0]["sansho"], "圓成實")
        self.assertEqual(planted[0]["spans"], [smoke.SPAN_FLOCK])

        def _explode(*args, **kwargs):
            raise AssertionError("ゲート破れの受領証は再走を止める")

        with contextlib.redirect_stderr(io.StringIO()):
            again = smoke.run(call_llm=_explode, state_dir=self.state, receipts_dir=self.receipts, force=False)
        self.assertEqual(again["generated_at"], receipt["generated_at"])
        self.assertFalse(again["gate"]["ok"])
        self.assertEqual(again["gate"]["breaches"], receipt["gate"]["breaches"])

    def test_force_rewrites_a_non_blocked_receipt(self):
        os.makedirs(self.receipts)
        out = os.path.join(self.receipts, smoke.RECEIPT_NAME)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"blocked": False, "generated_at": "existing-marker", "gate": {"ok": True, "breaches": []}}, f)

        calls = []

        def _empty(prompt, max_tokens=1024, purpose=None):
            calls.append(purpose)
            return "[]"

        with contextlib.redirect_stdout(io.StringIO()):
            receipt = smoke.run(call_llm=_empty, state_dir=self.state, receipts_dir=self.receipts, force=True)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(purpose == "propose" for purpose in calls))
        self.assertNotEqual(receipt["generated_at"], "existing-marker")
        self.assertFalse(receipt["blocked"])
        self.assertTrue(receipt["gate"]["ok"], receipt["gate"]["breaches"])
        with open(out, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["generated_at"], receipt["generated_at"])


class TestDecisionMarkerOutsideSpan(unittest.TestCase):
    def test_marker_only_in_content_is_counted_and_marker_inside_span_is_not(self):
        seeds = [
            {
                "id": 1,
                "kind": "決定",
                "content": "flock を採用し、新しいロックファイルは却下 (既存の .lock で足りる)。",
                "spans": [smoke.SPAN_FLOCK],
            },
            {
                "id": 6,
                "kind": "決定",
                "content": "公開時はタブ幅4を維持し、変更は却下。",
                "spans": ["公開する方針はタブ幅を4のままにする。"],
            },
            {
                "id": 7,
                "kind": "決定",
                "content": "長いログでは切り詰めの外側にある文だけを根拠に採用し、内側の文は却下。",
                "spans": ["長いログの要点は、切り詰めの外側にある文だけが根拠になることです。"],
            },
            {"id": 2, "kind": "回避策", "content": "原因は再入のため回避した。", "spans": ["原因"]},
        ]
        found = smoke.decision_markers_outside_span(seeds)
        self.assertEqual([row["seed_id"] for row in found], [6, 7])
        self.assertEqual(found[0]["markers"], ["却下"])
        self.assertEqual(found[1]["markers"], ["却下"])

    def test_live_receipt_seeds_six_and_seven_are_outside_span(self):
        path = os.path.join(REPO_RECEIPTS, smoke.RECEIPT_NAME)
        with open(path, encoding="utf-8") as f:
            receipt = json.load(f)
        found = smoke.decision_markers_outside_span(receipt["seeds"])
        self.assertEqual([row["seed_id"] for row in found], [6, 7])
        self.assertEqual(receipt["measured"]["decision_marker_outside_span"], 2)
        self.assertEqual(receipt["measured"]["decision_marker_outside_span_ids"], [6, 7])


class TestGateBreachDetector(unittest.TestCase):
    def test_flags_forbidden_token_cross_project_and_disposition(self):
        seeds = [{
            "id": 9,
            "kind": "決定",
            "project": smoke.PROJECT_A,
            "content": "普通の決定",
            "evidences": [{"span": smoke.PASTE_TOKEN}],
        }]
        breaches = smoke.find_gate_breaches(seeds, ["公開する方針だけ"], {"seeds": [], "index": [], "narrative": ""}, [])
        self.assertTrue(any(b["type"] == "forbidden_token" for b in breaches))
        self.assertTrue(any(b["type"] == "ungrounded_span" for b in breaches))

        recall = {
            "seeds": [{"id": 3, "kind": "決定", "project": smoke.PROJECT_B, "content": "sqlite"}],
            "index": [{"id": 4, "kind": "性向", "project": smoke.PROJECT_A, "content": "好み"}],
            "narrative": "別リポジトリでは sqlite を採用する",
            "resume": None,
        }
        breaches = smoke.find_gate_breaches(
            [], ["本文"], recall, ["別リポジトリでは sqlite を採用する"],
        )
        types = {b["type"] for b in breaches}
        self.assertIn("cross_project", types)
        self.assertIn("disposition", types)
        self.assertIn("cross_project_narrative", types)

    def test_clean_seed_is_not_a_breach(self):
        seeds = [{
            "id": 1,
            "kind": "決定",
            "project": smoke.PROJECT_A,
            "content": smoke.SPAN_FLOCK,
            "evidences": [{"span": smoke.SPAN_FLOCK}],
        }]
        breaches = smoke.find_gate_breaches(
            seeds, [smoke.SPAN_FLOCK], {"seeds": [], "index": [], "narrative": ""}, [],
        )
        self.assertEqual(breaches, [])


class TestMainStopsWithoutKey(unittest.TestCase):
    def test_main_does_not_write_a_receipt_when_the_key_is_absent(self):
        keys = ("OPENAI_API_KEY", "RAIYA_OPENAI_API_KEY")
        saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
        before = _dir_digest(REPO_RECEIPTS)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                code = smoke.main([])
        finally:
            os.environ.update(saved)
        self.assertEqual(code, 2)
        self.assertEqual(_dir_digest(REPO_RECEIPTS), before)
