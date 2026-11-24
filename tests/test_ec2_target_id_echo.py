"""#148 (a): EC2' の縁共有は、提案の target_id が指す注入済みの種子 (action reinforce / supersede) だけ 1 語で足りる。

spec 2026-11-08-condition-token-identity-design.md §11。それ以外は min(2, 小さい側) のまま。
alaya.imprint を直接呼ぶ。テストが消すのは自作 tmp だけ。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
USER_REQ = "前に決めたことを全部確認して。"


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


class _Base(unittest.TestCase):
    SEED_KIND = "所在"
    SEED_CONDS = ["設定ファイル", "config", "配置"]
    SEED_SPAN = "設定ファイルは config/ に置く。配置はそこ。"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()
        self.sid = self._plant(self.SEED_SPAN, self.SEED_CONDS, "設定ファイルは config/ に置く", self.SEED_KIND)
        self.other = self._plant("別件は docs/ にまとめる。資料の置き場。", ["別件", "docs", "資料"], "別件は docs/ にまとめる", "所在")

    def _plant(self, span, conds, content, kind):
        r = self.store.imprint(
            [_turn(span)],
            {"content": content, "conditions": conds, "polarity": "無記", "kind": kind,
             "evidence": [{"source": "user", "span": span}]})
        self.assertTrue(r["is_new"], r)
        return r["seed"]["id"]

    def _propose(self, assist, conds, kind="所在", user=USER_REQ, **kw):
        inj = kw.pop("injected_by_turn", None)
        rf = kw.pop("replacement_for", None)
        p = {"content": "設定ファイルを config に置く", "conditions": list(conds), "polarity": "無記", "kind": kind,
             "evidence": [{"source": "user", "span": user}, {"source": "assistant", "span": assist}]}
        p.update(kw)
        return self.store.imprint([_turn(user, assist, ledger_id=1)], p, replacement_for=rf, injected_by_turn=inj)

    def n_seeds(self):
        return len(self.store.list_seeds())

    def strength(self, sid):
        return self.store.get_by_ids([sid])[0]["strength"]


class TestTargetIdEcho(_Base):

    def test_x33_shape_two_vs_three_sharing_one_is_echo(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertIsNone(r["seed"])
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIn("target_id #%d" % self.sid, r["reason"])
        self.assertIn("縁共有 1 語", r["reason"])
        self.assertEqual(self.n_seeds(), 2)
        self.assertEqual(self.strength(self.sid), 1)
        self.assertIn("縁共有 1 語", self.store.list_imprint_log()[-1]["reason"])

    def test_supersede_also_relaxed(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="supersede", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIn("縁共有 1 語", r["reason"])
        self.assertEqual(self.n_seeds(), 2)

    def test_x29_shape_decision_seed_demoted_to_disposition(self):
        # 注入済みの 決定 (縁 3) への reinforce。提案は 性向 (縁 2、共有 1)。
        conds = ["設定ファイル", "config", "配置"]
        store = self.store
        r = store.imprint(
            [_turn("設定ファイルは config/ に置かない。配置は別にする。")],
            {"content": "設定ファイルは config/ に置かない", "conditions": conds, "polarity": "無記", "kind": "決定",
             "evidence": [{"source": "user", "span": "設定ファイルは config/ に置かない。配置は別にする。"}]})
        dec = r["seed"]["id"]
        self.assertEqual(r["seed"]["kind"], "決定")
        assist = "確認: 設定ファイルは config に触れず、補足の話だけです"
        r = self._propose(assist, ["設定ファイル", "補足"], kind="性向", action="reinforce", target_id=dec,
                          injected_by_turn={0: {dec}})
        self.assertIsNone(r["seed"])
        self.assertEqual(r["echo_match"], dec)
        self.assertIn("target_id #%d" % dec, r["reason"])
        self.assertEqual(self.strength(dec), 1)

    def test_one_word_via_cond_key(self):
        sid2 = self._plant("設定は configs/ に置く。資料も同じ。", ["configs", "設定", "資料"], "設定は configs/ に置く", "所在")
        assist = "確認: configs/ の話で、別の話題 を扱います"
        r = self._propose(assist, ["configs/", "別の話題"], action="reinforce", target_id=sid2,
                          injected_by_turn={0: {sid2}})
        self.assertEqual(r["echo_match"], sid2)
        self.assertIn("縁共有 1 語", r["reason"])

    def test_no_partial_match_config_vs_configs(self):
        assist = "確認: configs/ の話で、別の話題 を扱います"
        r = self._propose(assist, ["configs/", "別の話題"], action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])

    def test_target_not_injected_plants_new(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.other}})
        self.assertTrue(r["is_new"])
        self.assertEqual(self.n_seeds(), 3)

    def test_injected_but_zero_shared_plants_new(self):
        assist = "確認: 別の場所 と 別の話題 の話です"
        r = self._propose(assist, ["別の場所", "別の話題"], action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.assertEqual(self.n_seeds(), 3)

    def test_action_new_with_one_shared_plants_new(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.assertIsNone(r.get("echo_match"))

    def test_action_new_with_target_id_does_not_relax(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="new", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.assertNotIn("target_id", r["reason"])

    def test_injected_by_turn_none_plants_new(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="reinforce", target_id=self.sid)
        self.assertTrue(r["is_new"])

    def test_missing_target_id_does_not_relax(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="reinforce", target_id=9999,
                          injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])

    def test_other_injected_seed_keeps_two_word_rule(self):
        # target は self.other、注入済みは self.sid (共有 1 語)。sid は target でないので緩めない。
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "別の場所"], action="reinforce", target_id=self.other,
                          injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])

    def test_two_shared_uses_plain_note(self):
        assist = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
        r = self._propose(assist, ["設定ファイル", "config"], action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertEqual(r["echo_match"], self.sid)
        self.assertNotIn("縁共有 1 語", r["reason"])


class TestTargetIdEchoGuards(_Base):
    ASSIST = "確認: 設定ファイルは config に置くのではなく別の場所の話です"
    CONDS = ["設定ファイル", "別の場所"]

    def test_replacement_for_disables_target_relax(self):
        r = self._propose(self.ASSIST, self.CONDS, action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}}, replacement_for=self.other)
        self.assertTrue(r["is_new"])
        self.assertIsNone(r.get("echo_match"))
        self.assertNotIn("target_id #", r["reason"])

    def test_bool_target_id_does_not_relax(self):
        self.assertEqual(self.sid, 1)
        r = self._propose(self.ASSIST, self.CONDS, action="reinforce", target_id=True,
                          injected_by_turn={0: {1}})
        self.assertTrue(r["is_new"])
        self.assertNotIn("縁共有 1 語", r["reason"])

    def test_withdrawn_target_not_relaxed(self):
        self.store.state["seeds"][0]["withdrawn"] = {"by": "x", "reason": "r"}
        r = self._propose(self.ASSIST, self.CONDS, action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertIsNone(r.get("echo_match"))
        self.assertNotIn("縁共有 1 語", r["reason"])

    def test_superseded_target_not_relaxed(self):
        self.store.state["seeds"][0]["superseded_by"] = 99
        r = self._propose(self.ASSIST, self.CONDS, action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid}})
        self.assertIsNone(r.get("echo_match"))
        self.assertNotIn("縁共有 1 語", r["reason"])

    def test_default_match_preferred_over_relaxed_target(self):
        # target (sid, 小さい id) は 1 語共有、other は 2 語共有 → other が hit、従来の注記
        self.store.state["seeds"][1]["conditions"] = ["設定ファイル", "別の場所", "資料"]
        r = self._propose(self.ASSIST, self.CONDS, action="reinforce", target_id=self.sid,
                          injected_by_turn={0: {self.sid, self.other}})
        self.assertEqual(r["echo_match"], self.other)
        self.assertNotIn("縁共有 1 語", r["reason"])
        self.assertNotIn("target_id #", r["reason"])


class TestSharesConditionDefault(unittest.TestCase):
    def test_default_unchanged_and_min_shared(self):
        seed = {"conditions": ["a", "b", "c"]}
        self.assertFalse(alaya._shares_condition(seed, ["a", "x"]))
        self.assertTrue(alaya._shares_condition(seed, ["a", "b"]))
        self.assertTrue(alaya._shares_condition(seed, ["a", "x"], 1))
        self.assertFalse(alaya._shares_condition(seed, ["y", "x"], 1))
        self.assertTrue(alaya._shares_condition(seed, []))  # 従来: 判定材料なしは止めない
        self.assertFalse(alaya._shares_conditions_strict(seed, [], 1))
        self.assertFalse(alaya._shares_conditions_strict({"conditions": []}, ["a"], 1))
        self.assertFalse(alaya._shares_conditions_strict(seed, ["a", "x"]))
        self.assertTrue(alaya._shares_conditions_strict(seed, ["a", "x"], 1))

    def test_cond_key_one_word(self):
        seed = {"conditions": ["configs/", "p"]}
        self.assertTrue(alaya._shares_conditions_strict(seed, ["configs", "q"], 1))
        self.assertFalse(alaya._shares_conditions_strict(seed, ["config", "q"], 1))


if __name__ == "__main__":
    unittest.main()
