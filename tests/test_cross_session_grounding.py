"""#145 X1 / X2b: 別の座 (session) の縁・span の貼り合わせを植えない
(spec docs/superpowers/specs/2026-11-06-cross-session-grounding-design.md §7)。

LLM は呼ばない (`raiya.backend.call_llm` を mock する)。テストは自分で作った
`tempfile.mkdtemp` だけを作り、それだけを消す。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import sansho  # noqa: E402
from raiya.alaya import CONDITION_FROM_OTHER_SESSION  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

T = sansho.Turn


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _fresh_store(self):
        d = tempfile.mkdtemp(dir=self.tmpdir)  # 各 store が自分の raiya.db を持つ
        store = alaya_store_at(os.path.join(d, "alaya.json"), os.path.join(d, "imprint_log.json"))
        store.begin_new_life()
        return store

    def _mix_window(self):
        # #87 の形: 縁「フォント作成処理」は座 a の往復にだけ、span は座 b の往復にある。
        window = [
            T("フォント作成処理に当てはまるものはないかな", ""),
            T("ちょっと保留 ロードマップに追記して", ""),
        ]
        proposal = {
            "content": "フォント作成処理の具体化は保留し、ロードマップに追記する。",
            "conditions": ["フォント作成処理", "ロードマップ"],
            "polarity": "無記",
            "kind": "性向",
            "evidence": [{"source": "user", "span": "ちょっと保留 ロードマップに追記して"}],
        }
        return window, proposal


class TestCrossSessionGrounding(_Base):
    def test_condition_only_in_other_session_is_record_only(self):
        window, proposal = self._mix_window()
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b"])
        self.assertEqual(r["decision"], "reject")
        self.assertIn(CONDITION_FROM_OTHER_SESSION, r["reason"])
        self.assertIsNone(r["seed"])
        self.assertEqual(self.store.list_seeds(), [])
        row = self.store.list_imprint_log()[-1]
        self.assertEqual(row["decision"], "reject")
        self.assertIn(CONDITION_FROM_OTHER_SESSION, row["reason"])

    def test_session_map_off_or_single_seat_is_unchanged(self):
        for kw in ({}, {"session_by_turn": None}, {"session_by_turn": ["a", "a"]}):
            store = self._fresh_store()
            window, proposal = self._mix_window()
            r = store.imprint(window, proposal, **kw)
            self.assertEqual(r["decision"], "圓成實", kw)
            self.assertEqual(len(store.list_seeds()), 1)

    def test_all_none_seats_is_one_seat_unchanged(self):
        window, proposal = self._mix_window()
        r = self.store.imprint(window, proposal, session_by_turn=[None, None])
        self.assertEqual(r["decision"], "圓成實")

    def test_none_vs_str_seat_are_different_seats(self):
        window, proposal = self._mix_window()
        r = self.store.imprint(window, proposal, session_by_turn=[None, "b"])
        self.assertEqual(r["decision"], "reject")
        self.assertIn(CONDITION_FROM_OTHER_SESSION, r["reason"])

    def test_replacement_for_is_exempt(self):
        window, proposal = self._mix_window()
        r = self.store.imprint(window, proposal, replacement_for=7, session_by_turn=["a", "b"])
        self.assertEqual(r["decision"], "圓成實")

    def test_same_verbatim_span_in_two_seats_passes_with_condition_in_either(self):
        window = [T("ロードマップに追記して 保留", ""), T("ロードマップに追記して", "")]
        proposal = {
            "content": "ロードマップに追記する",
            "conditions": ["ロードマップ", "保留"],
            "polarity": "無記",
            "kind": "性向",
            "evidence": [{"source": "user", "span": "ロードマップに追記して"}],
        }
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b"])
        self.assertEqual(r["decision"], "圓成實")
        self.assertNotIn("別の座", r["reason"])
        self.assertEqual(r["grounded_conditions"], ["ロードマップ", "保留"])

    def test_seq75_shape_narrows_to_user_seat_and_logs_new_decision(self):
        window = [
            T("公開は来月にする", ""),
            T("別の話", "公開時期は E-1 です"),
        ]
        proposal = {
            "content": "公開は来月",
            "conditions": ["公開"],
            "polarity": "無記",
            "kind": "性向",
            "evidence": [
                {"source": "user", "span": "公開は来月にする"},
                {"source": "assistant", "span": "公開時期は E-1 です"},
            ],
        }
        base = self._fresh_store()
        self.assertEqual(base.imprint(window, proposal)["decision"], "依他起")

        r = self.store.imprint(window, proposal, session_by_turn=["x", "y"])
        self.assertEqual(r["decision"], "圓成實")
        self.assertEqual(r["sansho"], "圓成實")
        self.assertEqual([s["span"] for s in r["spans"]], ["公開は来月にする"])
        self.assertIn("別の座の span 1 本を外した", r["reason"])
        seeds = self.store.list_seeds()
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["sansho"], "圓成實")
        row = self.store.list_imprint_log()[-1]
        self.assertEqual(row["decision"], "圓成實")
        self.assertIn("別の座の span 1 本を外した", row["reason"])
        self.assertEqual(len(row["proposal"]["evidence"]), 2)  # 元の提案のまま
        self.assertEqual(len(proposal["evidence"]), 2)

    def test_length_mismatch_fails_open(self):
        window, proposal = self._mix_window()
        r = self.store.imprint(window, proposal, session_by_turn=["a"])
        self.assertEqual(r["decision"], "圓成實")
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_narrowing_then_condition_only_in_dropped_seat_rejects(self):
        window = [T("猫が好き", ""), T("犬が好き", "")]
        proposal = {
            "content": "猫と犬", "conditions": ["猫", "犬"], "polarity": "無記", "kind": "性向",
            "evidence": [{"source": "user", "span": "猫が好き"}, {"source": "user", "span": "犬が好き"}],
        }
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b"])
        self.assertEqual(r["decision"], "reject")
        self.assertIn("別の座の span 1 本を外した", r["reason"])
        self.assertIn(CONDITION_FROM_OTHER_SESSION, r["reason"])
        self.assertEqual(self.store.list_seeds(), [])
        row = self.store.list_imprint_log()[-1]
        self.assertEqual(row["decision"], "reject")
        self.assertIn(CONDITION_FROM_OTHER_SESSION, row["reason"])

    def test_assistant_only_spans_keep_latest_seat(self):
        window = [T("あ", "猫がいる"), T("い", "犬がいる")]
        proposal = {
            "content": "犬", "conditions": ["犬"], "polarity": "無記", "kind": "性向",
            "evidence": [{"source": "assistant", "span": "猫がいる"}, {"source": "assistant", "span": "犬がいる"}],
        }
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b"])
        self.assertEqual(r["decision"], "依他起")
        self.assertEqual([x["span"] for x in r["spans"]], ["犬がいる"])
        self.assertIn("別の座の span 1 本を外した", r["reason"])
        store = self._fresh_store()
        proposal2 = dict(proposal, conditions=["猫"])  # 縁は外した座にだけ
        r2 = store.imprint(window, proposal2, session_by_turn=["a", "b"])
        self.assertEqual(r2["decision"], "reject")
        self.assertIn(CONDITION_FROM_OTHER_SESSION, r2["reason"])

    def test_main_seat_is_latest_user_seat_not_later_assistant_seat(self):
        window = [T("猫が好き", ""), T("犬が好き", ""), T("x", "鳥が好きです")]
        proposal = {
            "content": "犬", "conditions": ["犬"], "polarity": "無記", "kind": "性向",
            "evidence": [
                {"source": "user", "span": "猫が好き"},
                {"source": "user", "span": "犬が好き"},
                {"source": "assistant", "span": "鳥が好きです"},
            ],
        }
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b", "c"])
        self.assertEqual(r["decision"], "圓成實")
        self.assertEqual([x["span"] for x in r["spans"]], ["犬が好き"])
        self.assertIn("別の座の span 2 本を外した", r["reason"])

    def test_kept_span_turn_index_is_repointed_into_kept_seats(self):
        # 座 a,b,b,c: 逐語は a と b にあるが、座 {b} に絞ると a の往復 (project P) から取ってはいけない。
        window = [
            LedgerTurn(user_text="ロードマップに追記して", assistant_text="", id=10, life=1, project="/p"),
            LedgerTurn(user_text="ロードマップに追記して 保留", assistant_text="", id=11, life=1, project="/q"),
            LedgerTurn(user_text="雑談", assistant_text="", id=12, life=1, project="/q"),
            LedgerTurn(user_text="別件", assistant_text="", id=13, life=1, project="/r"),
        ]
        proposal = {
            "content": "ロードマップ", "conditions": ["ロードマップ"], "polarity": "無記", "kind": "性向",
            "evidence": [{"source": "user", "span": "ロードマップに追記して"}, {"source": "user", "span": "保留"}],
        }
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b", "b", "c"])
        self.assertEqual(r["decision"], "圓成實")
        self.assertEqual(r["project"], "/q")
        self.assertEqual([x["turn_index"] for x in r["spans"]], [1, 1])
        seed = self.store.list_seeds()[0]
        self.assertEqual(seed["project"], "/q")
        self.assertEqual({e["ledger_id"] for e in seed["evidences"]}, {11})

    def test_repoint_does_not_change_strength_without_cross_seat_hit(self):
        window = [
            LedgerTurn(user_text="雑談", assistant_text="", id=1, life=1),
            LedgerTurn(user_text="猫が好き", assistant_text="", id=2, life=1),
            LedgerTurn(user_text="猫が好き 犬も", assistant_text="", id=3, life=1),
        ]
        proposal = {
            "content": "猫", "conditions": ["猫"], "polarity": "無記", "kind": "性向",
            "evidence": [{"source": "user", "span": "猫が好き"}, {"source": "user", "span": "犬も"}],
        }
        ref = self._fresh_store()
        ref.imprint(window, proposal, session_by_turn=None)
        r = self.store.imprint(window, proposal, session_by_turn=["a", "b", "b"])
        self.assertEqual(r["decision"], "圓成實")
        seed, ref_seed = self.store.list_seeds()[0], ref.list_seeds()[0]
        self.assertEqual(seed["strength"], ref_seed["strength"])
        self.assertEqual(seed["strength"], 2)
        self.assertEqual([e["ledger_id"] for e in seed["evidences"]], [2, 3])


class TestConsolidateWiresSessions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, sessions):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("フォント作成処理に当てはまるものはないかな", "", session_id=sessions[0])
        eng.record_turn("ちょっと保留 ロードマップに追記して", "", session_id=sessions[1])
        raw = json.dumps([{
            "action": "new", "content": "フォント作成処理の具体化は保留し、ロードマップに追記する。",
            "conditions": ["フォント作成処理", "ロードマップ"], "polarity": "無記", "kind": "性向",
            "evidence": [{"source": "user", "span": "ちょっと保留 ロードマップに追記して"}],
        }], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            result = eng.consolidate()
        return eng, result

    def test_two_sessions_window_rejects(self):
        eng, result = self._run(["sess-a", "sess-b"])
        self.assertEqual(result["window_sessions"], 2)
        d = result["decisions"][0]
        self.assertEqual(d["decision"], "reject")
        self.assertIn(CONDITION_FROM_OTHER_SESSION, d["reason"])
        self.assertEqual(eng.audit()["seed_count"], 0)

    def test_gate_cut_proposal_in_two_sessions_is_quarantined_without_error(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好き", "", session_id="sess-a")
        eng.record_turn("犬が好き", "", session_id="sess-b")
        raw = json.dumps([{
            "action": "new", "content": "x", "conditions": ["猫"], "polarity": "無記", "kind": "性向",
            "evidence": [{"source": "user", "span": "猫が\ufffc好き"}],
        }], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            result = eng.consolidate()
        self.assertEqual(result["decisions"][0]["decision"], "遍計所執")
        self.assertEqual(eng.audit()["seed_count"], 0)

    def test_one_session_window_still_plants(self):
        eng, result = self._run(["sess-a", "sess-a"])
        self.assertEqual(result["decisions"][0]["decision"], "圓成實")
        self.assertEqual(eng.audit()["seed_count"], 1)


if __name__ == "__main__":
    unittest.main()
