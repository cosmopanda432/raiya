"""supersede の強度 (旧い値の厚みを新しい値に継がせない) の pin。

設計書 2026-11-28-supersede-strength-design.md §3.2・§1.4 (W-n の列)・§5 T1。
LLM は使わない。テストは `_TmpCase` の自作 tempfile.mkdtemp だけを作り、それだけを消す。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ["RAIYA_CONFIG"] = ""

from raiya import alaya, sansho  # noqa: E402
from raiya.alaya import GERMINATION_REASON_STRENGTH  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests.test_twin_match import _etaki, _pair, _user  # noqa: E402
from tests.test_withdrawal import _TmpCase, _seed  # noqa: E402

CONDS = ["猫", "犬"]


class TestSameClaim(unittest.TestCase):
    def test_same_content_conditions_class(self):
        seed = {"content": "猫が好き", "conditions": ["猫", "犬"], "sansho": "圓成實"}
        self.assertIs(alaya._same_claim(seed, "猫が好き", ["犬", "猫"], "圓成實"), True)

    def test_casefold_and_trailing_slash(self):
        seed = {"content": "Cats OK", "conditions": ["src/"], "sansho": "依他起"}
        self.assertIs(alaya._same_claim(seed, "cats ok", ["src"], "依他起"), True)
        seed = {"content": "cats ok", "conditions": ["src"], "sansho": "依他起"}
        self.assertIs(alaya._same_claim(seed, "Cats OK", ["src/"], "依他起"), True)

    def test_conditions_differ(self):
        seed = {"content": "猫が好き", "conditions": ["猫"], "sansho": "圓成實"}
        self.assertIs(alaya._same_claim(seed, "猫が好き", ["猫", "鳥"], "圓成實"), False)

    def test_class_differs(self):
        seed = {"content": "猫が好き", "conditions": ["猫"], "sansho": "依他起"}
        self.assertIs(alaya._same_claim(seed, "猫が好き", ["猫"], "圓成實"), False)

    def test_punctuation_differs(self):
        seed = {"content": "猫が好き", "conditions": ["猫"], "sansho": "圓成實"}
        self.assertIs(alaya._same_claim(seed, "猫が好き。", ["猫"], "圓成實"), False)

    def test_empty_content_never_same(self):
        seed = {"content": "", "conditions": ["猫"], "sansho": "圓成實"}
        self.assertIs(alaya._same_claim(seed, "", ["猫"], "圓成實"), False)


def _base(**kw):
    kw.setdefault("strength", 3)
    return _seed(1, CONDS, project=None, content="猫が好き", **kw)


class TestSupersedeStrength(_TmpCase):
    def _a(self, store, **extra):
        turn, proposal = _user(content="犬が好き", action="supersede", target_id=1, **extra)
        return store.imprint(turn, proposal)

    def _seed1(self, store):
        return store.get_by_ids([1])[0]

    def _verdict(self, store, text, seed_id=1):
        for seed, fired, reason, _m in store._germination_verdicts(text, project=None):
            if seed["id"] == seed_id:
                return fired, reason
        self.fail("seed not in verdicts")

    def test_value_change_counts_only_this_imprint(self):
        store = self.store([_base()])
        r = self._a(store)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(seed["history"][-1]["reason"], "superseded")
        self.assertEqual(seed["history"][-1]["strength"], 3)

    def test_two_turn_window_counts_two(self):
        store = self.store([_base()])
        window = [sansho.Turn(user_text="犬が好き p/ 猫", assistant_text=""),
                  sansho.Turn(user_text="犬が好き p/ 犬", assistant_text="")]
        proposal = {"content": "犬が好き", "conditions": ["猫", "犬"], "polarity": "無記", "kind": "所在",
                    "action": "supersede", "target_id": 1,
                    "evidence": [{"source": "user", "span": "犬が好き p/ 猫"}, {"source": "user", "span": "犬が好き p/ 犬"}]}
        r = store.imprint(window, proposal)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 2)
        self.assertEqual(seed["history"][-1]["strength"], 3)

    def test_same_claim_keeps_strength(self):
        store = self.store([_base()])
        turn, proposal = _user(content="猫が好き", action="supersede", target_id=1)
        r = store.imprint(turn, proposal)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 3)
        self.assertEqual(seed["history"][-1]["strength"], 3)

    def test_same_content_new_conditions_resets(self):
        store = self.store([_base()])
        turn, proposal = _user(content="猫が好き", conditions=["猫", "鳥"], action="supersede", target_id=1)
        store.imprint(turn, proposal)
        self.assertEqual(self._seed1(store)["strength"], 1)

    def test_punctuation_change_resets(self):
        store = self.store([_base()])
        turn, proposal = _user(content="猫が好き。", action="supersede", target_id=1)
        store.imprint(turn, proposal)
        self.assertEqual(self._seed1(store)["strength"], 1)

    def test_class_change_resets(self):
        store = self.store([_base(sansho_class="依他起")])
        turn, proposal = _user(content="猫が好き", action="supersede", target_id=1)
        store.imprint(turn, proposal)
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(seed["sansho"], "圓成實")

    def test_etaki_place_waits_for_second_turn(self):
        store = self.store([_base(sansho_class="依他起", strength=2)])
        turn, proposal = _etaki(content="犬が好き", action="supersede", target_id=1)
        store.imprint(turn, proposal)
        self.assertEqual(self._seed1(store)["strength"], 1)
        self.assertEqual(self._verdict(store, "猫 犬"), (False, GERMINATION_REASON_STRENGTH))
        turn, proposal = _etaki(content="犬が好き", action="reinforce", target_id=1)
        store.imprint(turn, proposal)
        self.assertEqual(self._seed1(store)["strength"], 2)
        self.assertEqual(self._verdict(store, "猫 犬"), (True, None))

    def test_resume_point_counts_only_this_imprint(self):
        store = self.store([_seed(1, CONDS, kind="再開点", sansho_class="依他起", strength=5, project=None,
                                  content="猫の続き")])
        turn, proposal = _etaki(content="犬の続き", kind="再開点")
        r = store.imprint(turn, proposal)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 1)
        self.assertEqual([h.get("strength") for h in seed["history"]], [5])
        self.assertEqual(self._verdict(store, "猫 犬"), (False, GERMINATION_REASON_STRENGTH))

    def test_cross_class_supersede_E(self):
        ev = [{"life": 1, "source": "user", "span": "猫と犬は猫にする", "ledger_id": None,
               "turn": {"user_text": "猫と犬は猫にする", "assistant_text": ""}}]
        store = self.store([_seed(1, CONDS, kind="決定", strength=2, project=None, content="猫と犬は猫にする", evidences=ev)])
        turn = sansho.Turn(user_text="猫と犬は猫ではなく犬にする", assistant_text="猫と犬は犬にします。")
        proposal = {"content": "猫と犬は犬にする", "conditions": CONDS, "polarity": "無記", "kind": "決定",
                    "action": "supersede", "target_id": 1,
                    "evidence": [{"source": "user", "span": "猫と犬は猫ではなく犬にする"},
                                 {"source": "assistant", "span": "猫と犬は犬にします。"}]}
        store.imprint(turn, proposal)
        seed = self._seed1(store)
        self.assertEqual(seed["sansho"], "依他起")
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(seed["history"][-1]["strength"], 2)
        self.assertEqual(store._threshold(seed), 1)
        self.assertEqual(self._verdict(store, "猫 犬"), (True, None))

    def test_old_generation_form_keeps_threshold_one(self):
        t = {"user_text": "猫ではなく犬にする", "assistant_text": "猫ではなく犬にします。"}
        ev = [{"life": 1, "source": "user", "span": "猫ではなく犬にする", "ledger_id": None, "turn": dict(t)},
              {"life": 1, "source": "assistant", "span": "猫ではなく犬にします。", "ledger_id": None, "turn": dict(t)}]
        store = self.store([_seed(1, CONDS, kind="決定", sansho_class="依他起", strength=3, project=None,
                                  content="猫ではなく犬", evidences=ev)])
        turn = sansho.Turn(user_text="犬は鳥にする", assistant_text="犬は鳥にします。")
        proposal = {"content": "犬は鳥にする", "conditions": ["犬", "鳥"], "polarity": "無記", "kind": "決定",
                    "action": "supersede", "target_id": 1,
                    "evidence": [{"source": "user", "span": "犬は鳥にする"},
                                 {"source": "assistant", "span": "犬は鳥にします。"}]}
        store.imprint(turn, proposal)
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(store._threshold(seed), 1)
        self.assertEqual(self._verdict(store, "犬 鳥"), (True, None))

    def test_horizontal_supersede_keeps_strength(self):
        store = self.store([_seed(1, CONDS, strength=1, project=None, content="猫が好き"),
                            _seed(2, CONDS, strength=4, project=None, content="猫犬")])
        self._a(store)
        self.assertEqual(self._seed1(store)["strength"], 1)
        s2 = store.get_by_ids([2])[0]
        self.assertEqual(s2["strength"], 4)
        self.assertEqual(s2["superseded_by"], 1)
        self.assertNotIn("strength", s2["history"][-1])

    def test_old_history_rows_untouched(self):
        seed = _base(strength=7)
        seed["history"] = [{"life": 1, "content": "鳥", "conditions": ["鳥"], "sansho": "圓成實",
                            "polarity": "無記", "reason": "superseded"}]
        store = self.store([seed])
        self._a(store)
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 1)
        self.assertNotIn("strength", seed["history"][0])
        self.assertEqual(seed["history"][1]["strength"], 7)

    def test_float_strength_copied(self):
        store = self.store([_base(strength=2.5)])
        self._a(store)
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(seed["history"][-1]["strength"], 2.5)

    def test_twin_supersede_same_claim_keeps_strength(self):
        store = self.store(_pair())
        turn, proposal = _etaki(action="supersede", target_id=2)
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "依他起")
        self.assertTrue(r["superseded"])
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 2)
        store = self.store(_pair())
        turn, proposal = _etaki(action="reinforce", target_id=2)
        r = store.imprint(turn, proposal)
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 2)

    def test_echo_supersede_same_claim_keeps_strength(self):
        store = self.store([_base(sansho_class="依他起", strength=2)])
        turn, proposal = _etaki(action="supersede", target_id=1)
        store.imprint([turn], proposal, injected_by_turn={0: {1}})
        self.assertEqual(self._seed1(store)["strength"], 2)
        self.assertEqual(self._verdict(store, "猫 犬"), (True, None))

    def test_approval_pair_supersede_counts_two(self):
        store = self.store([])
        proj = "/proj"

        def row(user, asst, i):
            return LedgerTurn(user_text=user, assistant_text=asst, id=i, project=proj)

        conds_old = ["検査用のログ", "保存期間", "30日"]
        rec_old = "**推奨: 案2（保存期間30日）**"
        r = store.imprint(
            [row("検査用のログの保存期間を決めたい", "3 案あります。" + rec_old, 1), row("GO", "了解", 2)],
            {"content": "検査用ログの保存期間は30日を採用。", "conditions": conds_old, "polarity": "無記", "kind": "決定",
             "action": "new", "evidence": [{"source": "assistant", "span": rec_old}, {"source": "user", "span": "GO"}]},
            injected_by_turn={}, session_by_turn=["s1", "s1"])
        self.assertEqual(r["seed"]["strength"], 2)
        sid = r["seed"]["id"]
        store.begin_new_life()
        rec_new = "**推奨: 案3（保存期間60日）**"
        r = store.imprint(
            [row("検査用のログの保存期間を決め直したい", "3 案あります。" + rec_new, 3), row("GO", "了解", 4)],
            {"content": "検査用ログの保存期間は60日を採用。", "conditions": ["検査用のログ", "保存期間", "60日"],
             "polarity": "無記", "kind": "決定", "action": "supersede", "target_id": sid,
             "evidence": [{"source": "assistant", "span": rec_new}, {"source": "user", "span": "GO"}]},
            injected_by_turn={}, session_by_turn=["s2", "s2"])
        self.assertTrue(r.get("superseded"), r["reason"])
        seed = store.get_by_ids([sid])[0]
        self.assertEqual(seed["strength"], 2)
        self.assertTrue(alaya.approval_only(seed))
        self.assertEqual(seed["history"][-1]["strength"], 2)

    def test_same_claim_reads_grounded_not_proposal_conditions(self):
        store = self.store([_base()])
        turn, proposal = _user(content="猫が好き", action="supersede", target_id=1)
        proposal["conditions"] = ["猫", "犬", "鳥"]  # 「鳥」は往復の本文に無い = ゲートの grounded は [猫, 犬]
        r = store.imprint(turn, proposal)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual(seed["conditions"], ["猫", "犬"])
        self.assertEqual(seed["strength"], 3)
        self.assertEqual(seed["history"][-1]["strength"], 3)

    def test_proposal_sansho_field_not_read(self):
        # 自称が実際と食い違う 2 形 (不変条件 1): 付けない場合と同じ強度・同じ class。
        store = self.store([_base(sansho_class="依他起")])
        turn, proposal = _user(content="猫が好き", action="supersede", target_id=1, sansho="依他起")
        store.imprint(turn, proposal)
        seed = self._seed1(store)
        self.assertEqual((seed["strength"], seed["sansho"]), (1, "圓成實"))
        store = self.store([_base(sansho_class="依他起", strength=2)])
        turn, proposal = _etaki(content="猫が好き", action="supersede", target_id=1, sansho="圓成實")
        store.imprint(turn, proposal)
        seed = self._seed1(store)
        self.assertEqual((seed["strength"], seed["sansho"]), (2, "依他起"))
        store = self.store([_base()])
        turn, proposal = _user(content="犬が好き", action="supersede", target_id=1, sansho="圓成實")
        store.imprint(turn, proposal)
        seed = self._seed1(store)
        self.assertEqual((seed["strength"], seed["sansho"]), (1, "圓成實"))

    def test_resume_point_same_claim_keeps_strength(self):
        store = self.store([_seed(1, CONDS, kind="再開点", sansho_class="依他起", strength=5, project=None,
                                  content="猫の続き")])
        turn, proposal = _etaki(content="猫の続き", kind="再開点")
        r = store.imprint(turn, proposal)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual(seed["strength"], 5)
        self.assertEqual(seed["history"][-1]["strength"], 5)

    def test_two_value_changes_history_strengths(self):
        store = self.store([_base()])
        self._a(store)
        turn, proposal = _user(content="鳥が好き", action="supersede", target_id=1)
        r = store.imprint(turn, proposal)
        self.assertTrue(r["superseded"])
        seed = self._seed1(store)
        self.assertEqual([h["strength"] for h in seed["history"] if h["reason"] == "superseded"], [3, 1])
        self.assertEqual(seed["strength"], 1)

    def test_replace_seed_rare_resume_path(self):
        # L8 (設計書 §3.4 (d)): 生きた 再開点 が 2 件あり片方を人が置き換えると、特則が残りの 1 件への supersede に読み替える。
        store = self.store([
            _seed(1, CONDS, kind="再開点", strength=4, project=None, content="犬の続き",
                  evidences=[{"life": 1, "source": "user", "span": "犬の続き", "ledger_id": None,
                              "turn": {"user_text": "犬の続き", "assistant_text": ""}}]),
            _seed(2, CONDS, kind="再開点", sansho_class="依他起", strength=5, project=None, content="猫の続き"),
        ])
        del store
        eng = self.engine()
        rec = eng.record_turn("犬の続きは猫と犬の話", "了解", session_id="s1", project=None)
        out = eng.replace_seed(
            1, "太郎", "古い", [], ["犬の続き"],
            {"content": "犬の続きは新しい", "conditions": CONDS, "kind": "再開点", "spans": ["猫と犬の話"]},
            window_ledger_ids=[rec["ledger_id"]],
        )
        self.assertTrue(out["replacement"]["superseded"], out["replacement"]["reason"])
        self.assertEqual(out["replacement"]["seed_id"], 2)
        seed = {s["id"]: s for s in self.engine().alaya.list_seeds()}[2]
        self.assertEqual(seed["content"], "犬の続きは新しい")
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(seed["history"][-1]["strength"], 5)

    def test_engine_imprint_returns_reset_strength(self):
        self.store([_base()])
        turn, proposal = _user(content="犬が好き", action="supersede", target_id=1)
        out = self.engine().imprint(turn, [proposal])
        self.assertTrue(out[0]["superseded"])
        self.assertEqual(out[0]["seed"]["strength"], 1)


if __name__ == "__main__":
    unittest.main()
