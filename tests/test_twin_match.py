"""双子照合 (#78): 依他起の再提案が生きた圓成實 E に隠された双子 H へ落ちるとき、積まず記録だけ残す。
spec: docs/superpowers/specs/2026-10-26-paste-recall-and-twin-reinforce-design.md §3 P5〜P7 / §4 C3。
LLM は使わない。テストは自作の tempfile.mkdtemp だけを作り、それだけを消す (tests.test_withdrawal の _TmpCase)。
"""
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import sansho  # noqa: E402
from tests.test_withdrawal import _TmpCase, _record, _seed  # noqa: E402

CONDS = ["猫", "犬"]


def _etaki(content="猫が好き", conditions=CONDS, kind="所在", **extra):
    """assistant 側に接地する (= 依他起の) 提案。"""
    text = "{} {}".format(content, "".join(conditions))
    turn = sansho.Turn(user_text="", assistant_text=text)
    proposal = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
                "evidence": {"source": "assistant", "span": content}}
    proposal.update(extra)
    return turn, proposal


def _user(content="猫が好き", conditions=CONDS, kind="所在", **extra):
    text = "{} p/ {}".format(content, "".join(conditions))  # #149: 所在 の形は 區切りの語 (`/` を含む語)
    turn = sansho.Turn(user_text=text, assistant_text="")
    proposal = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
                "evidence": {"source": "user", "span": content + " p/"}}
    proposal.update(extra)
    return turn, proposal


def _pair(**h_extra):
    e = _seed(1, CONDS, content="猫が好き", project=None)
    h = _seed(2, CONDS, content="猫が好き", project=None, sansho_class="依他起", strength=2)
    h.update(h_extra)
    return [e, h]


class TestTwinMatch(_TmpCase):
    def _assert_matched(self, store, result, before_h):
        self.assertEqual(result["decision"], "双子照合")
        self.assertEqual(result["sansho"], "依他起")
        self.assertIsNone(result["seed"])
        self.assertIsNone(result["is_new"])
        self.assertEqual(result["twin_match"], 2)
        self.assertNotIn("reinforced", result)
        self.assertIn("生きた圓成實 #1 に隠された双子 #2 への再提案のため積まない", result["reason"])
        rows = store.list_imprint_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["decision"], rows[0]["seed_id"]), ("双子照合", 2))
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(store.get_by_ids([2])[0], before_h)  # H は 1 バイトも変わらない
        self.assertEqual(self.store().get_by_ids([2])[0], before_h)  # 保存後も

    def test_merge_candidate_twin_is_matched_not_reinforced(self):
        store = self.store(_pair())
        before_h = copy.deepcopy(store.get_by_ids([2])[0])
        turn, proposal = _etaki()
        self._assert_matched(store, store.imprint(turn, proposal), before_h)

    def test_without_e_ordinary_reinforce(self):
        store = self.store(_pair()[1:])
        turn, proposal = _etaki()
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "依他起")
        self.assertIs(r["is_new"], False)
        self.assertTrue(r["reinforced"])
        self.assertNotIn("twin_match", r)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 3)

    def test_explicit_reinforce_target_twin_is_matched(self):
        store = self.store(_pair())
        before_h = copy.deepcopy(store.get_by_ids([2])[0])
        turn, proposal = _etaki(content="全く別の言い回し", conditions=["猫"], action="reinforce", target_id=2)
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(r["twin_match"], 2)
        self.assertEqual(store.get_by_ids([2])[0], before_h)
        self.assertEqual(store.list_imprint_log()[-1]["seed_id"], 2)

    def test_supersede_target_twin_goes_through(self):
        store = self.store(_pair())
        turn, proposal = _etaki(action="supersede", target_id=2)
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "依他起")
        self.assertTrue(r["superseded"])
        self.assertEqual(r["seed"]["id"], 2)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 2)

    def test_withdrawn_e_no_longer_shadows(self):
        store = self.store(_pair())
        store.withdraw(1, _record())
        turn, proposal = _etaki()
        r = store.imprint(turn, proposal)
        self.assertTrue(r["reinforced"])
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 3)

    def test_gate_class_is_used_not_the_proposal_field(self):
        store = self.store(_pair())
        turn, proposal = _etaki(sansho="圓成實")  # 自己申告は読まない
        self.assertEqual(store.imprint(turn, proposal)["decision"], "双子照合")


class TestTwinShadowsConsultation(_TmpCase):
    """P6/P7: 圓成實の提案・人の置き換え・再開点では影を引かない。"""

    def _count_calls(self, store, turn, proposal, **kw):
        with mock.patch.object(alaya_mod, "_twin_shadows", wraps=alaya_mod._twin_shadows) as spy:
            result = store.imprint(turn, proposal, **kw)
        return spy.call_count, result

    def test_enjoujitsu_proposal_does_not_consult(self):
        store = self.store(_pair())
        n, r = self._count_calls(store, *_user())
        self.assertEqual(n, 0)
        self.assertTrue(r["reinforced"])
        self.assertEqual(store.get_by_ids([1])[0]["strength"], 2)

    def test_replacement_for_does_not_consult(self):
        store = self.store(_pair())
        n, r = self._count_calls(store, *_etaki(), replacement_for=9)
        self.assertEqual(n, 0)
        self.assertTrue(r["reinforced"])

    def test_resume_point_does_not_consult(self):
        store = self.store([_seed(1, ["再"], kind="再開点", content="前", project=None)])
        n, r = self._count_calls(store, *_etaki(content="次の再開点", conditions=["次"], kind="再開点"))
        self.assertEqual(n, 0)
        self.assertTrue(r["superseded"])

    def test_etaki_proposal_consults_once(self):
        store = self.store(_pair())
        n, _ = self._count_calls(store, *_etaki())
        self.assertEqual(n, 1)


class TestTwinMatchEdges(_TmpCase):
    """P5/P6 の境界: 最初の H は植わる・別 project / superseded の E は隠さない・target/併合の順序。"""

    def test_first_etaki_with_e_present_plants_new_h(self):
        store = self.store([_seed(1, CONDS, content="猫が好き", project=None)])
        r = store.imprint(*_etaki(content="猫は犬より静か", conditions=["猫", "犬", "静"]))
        self.assertEqual(r["decision"], "依他起")
        self.assertIs(r["is_new"], True)
        self.assertNotIn("twin_match", r)
        self.assertEqual(len(store.list_seeds()), 2)

    def test_global_h_not_shadowed_by_project_e(self):
        e = _seed(1, CONDS, content="猫が好き", project="/p")
        h = _seed(2, CONDS, content="猫が好き", project=None, sansho_class="依他起", strength=2)
        store = self.store([e, h])
        r = store.imprint(*_etaki())
        self.assertNotIn("twin_match", r)
        self.assertTrue(r["reinforced"])
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 3)

    def test_superseded_e_does_not_shadow(self):
        pair = _pair()
        pair[0]["superseded_by"] = 99
        store = self.store(pair)
        r = store.imprint(*_etaki())
        self.assertNotIn("twin_match", r)
        self.assertTrue(r["reinforced"])
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 3)

    def test_reinforce_target_h_when_e_withdrawn(self):
        store = self.store(_pair())
        store.withdraw(1, _record())
        turn, proposal = _etaki(action="reinforce", target_id=2)
        r = store.imprint(turn, proposal)
        self.assertNotEqual(r["decision"], "双子照合")
        self.assertTrue(r["reinforced"])
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 3)

    def test_merge_tie_prefers_store_order_then_twin_match(self):
        """同点 (content 同一 = 類似度 1.0) は store 順の先頭が勝つ。隠れた H が先なら 双子照合、隠れない H が先ならそれが reinforce。"""
        e = _seed(1, CONDS, content="猫が好き", project=None)
        h2 = _seed(2, CONDS, content="猫が好き", project=None, sansho_class="依他起", strength=2)  # E と縁 2 件共有 = 隠れる
        h3 = _seed(3, ["猫"], content="猫が好き", project=None, sansho_class="依他起", strength=2)  # 共有 1 件 = 隠れない。提案と 1 語共有 = 併合候補 (#104)
        store = self.store([e, h2, h3])
        before_h3 = copy.deepcopy(store.get_by_ids([3])[0])
        r = store.imprint(*_etaki())
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(r["twin_match"], 2)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 2)
        self.assertEqual(store.get_by_ids([3])[0], before_h3)

    def test_merge_tie_unshadowed_first_is_reinforced(self):
        store = self.store([
            _seed(1, CONDS, content="猫が好き", project=None),
            # 縁 1 語 ["猫"]: 提案と 1 語共有すれば併合でき (min(2, 1)=1, #104)、E とは 1 件共有 = 隠れない。
            _seed(3, ["猫"], content="猫が好き", project=None, sansho_class="依他起", strength=2),
            _seed(2, CONDS, content="猫が好き", project=None, sansho_class="依他起", strength=2),
        ])
        before_h2 = copy.deepcopy(store.get_by_ids([2])[0])
        r = store.imprint(*_etaki())
        self.assertNotIn("twin_match", r)
        self.assertTrue(r["reinforced"])
        self.assertEqual(r["seed"]["id"], 3)
        self.assertEqual(store.get_by_ids([3])[0]["strength"], 3)
        self.assertEqual(store.get_by_ids([2])[0], before_h2)

    def test_mixed_user_and_assistant_spans_hit_twin_match(self):
        store = self.store(_pair())
        before_h = copy.deepcopy(store.get_by_ids([2])[0])
        turn = sansho.Turn(user_text="猫が好き 犬", assistant_text="犬と猫")
        proposal = {"content": "猫が好き", "conditions": list(CONDS), "polarity": "無記", "kind": "所在",
                    "evidence": [{"source": "user", "span": "猫が好き"}, {"source": "assistant", "span": "犬と猫"}]}
        r = store.imprint(turn, proposal)
        self.assertEqual(r["sansho"], "依他起")
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(store.get_by_ids([2])[0], before_h)
        rows = store.list_imprint_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal"]["content"], "猫が好き")

    def _mixed_h(self):
        return {"evidences": [{"source": "user", "span": "猫が好き"}, {"source": "assistant", "span": "猫が好き"}]}

    def test_assistant_only_onto_twin_hidden_h_with_user_span(self):
        """#101 D との順序: 双子照合 (#78) が先。H が user span を持っていても decision は 双子照合 のまま。"""
        store = self.store(_pair(**self._mixed_h()))
        before_h = copy.deepcopy(store.get_by_ids([2])[0])
        turn, proposal = _etaki()
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(r["twin_match"], 2)
        self.assertNotIn("user_span_match", r)
        self.assertEqual(store.get_by_ids([2])[0], before_h)
        self.assertEqual(store.list_imprint_log()[-1]["decision"], "双子照合")

    def test_assistant_only_onto_h_with_user_span_without_e_is_user_span_match(self):
        store = self.store(_pair(**self._mixed_h())[1:])
        before_h = copy.deepcopy(store.get_by_ids([2])[0])
        turn, proposal = _etaki()
        r = store.imprint(turn, proposal)
        self.assertEqual(r["user_span_match"], 2)
        self.assertNotIn("twin_match", r)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], before_h["strength"])
        self.assertEqual(store.get_by_ids([2])[0], before_h)

    def test_explicit_reinforce_onto_twin_hidden_h_with_user_span_is_twin_match(self):
        store = self.store(_pair(**self._mixed_h()))
        turn, proposal = _etaki(action="reinforce", target_id=2)
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "双子照合")
        self.assertNotIn("user_span_match", r)


if __name__ == "__main__":
    unittest.main()
