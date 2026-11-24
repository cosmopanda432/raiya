"""#104 (c): 併合・reinforce は縁を min(2, 小さい側の縁数) 語以上共有する種子にだけ (x9_3s で 1 語 → 2 語に)。類似度の式・閾値は変えない。

縁が片側 0 語なら判定材料が無いので止めない (従来どおり)。テストが消すのは自作 tmp だけ。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
TEXT_2 = "モジュール2の言語はGo2を採用 (Pythonは却下: 理由の記載なし)"
TEXT_3 = "モジュール3の言語はRust3を採用 (Pythonは却下: 理由の記載なし)"


def _turn(text):
    return LedgerTurn(user_text=text, assistant_text="", id=0, project=PROJ)


def _prop(text, conditions, **kw):
    p = {"content": text, "conditions": list(conditions), "polarity": "無記", "kind": "決定",
         "evidence": [{"source": "user", "span": text}]}
    p.update(kw)
    return p


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def plant_module2(self):
        r = self.store.imprint([_turn(TEXT_2)], _prop(TEXT_2, ["モジュール2", "Go2"]))
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("圓成實", "決定"))
        return r["seed"]["id"]


class TestSharedConditionMerge(_Case):
    def test_issue_104_similar_content_without_shared_condition_is_new(self):
        sid = self.plant_module2()
        seed = self.store.get_by_ids([sid])[0]
        self.assertGreaterEqual(alaya_mod._merge_similarity(seed, TEXT_3, ["モジュール3", "Rust3"]),
                                alaya_mod.MERGE_SIMILARITY_THRESHOLD)
        r = self.store.imprint([_turn(TEXT_3)], _prop(TEXT_3, ["モジュール3", "Rust3"]))
        self.assertTrue(r["is_new"])
        self.assertNotEqual(r["seed"]["id"], sid)
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 1)

    def test_similar_content_with_shared_condition_still_merges(self):
        sid = self.plant_module2()
        r = self.store.imprint([_turn(TEXT_2)], _prop(TEXT_2, ["モジュール2", "Go2"]))
        self.assertTrue(r.get("reinforced"))
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 2)

    def test_shared_condition_is_case_insensitive(self):
        r1 = self.store.imprint([_turn("Config は YAML で書く p/ 配置")],
                                _prop("Config は YAML で書く", ["Config"], kind="所在",
                                      evidence=[{"source": "user", "span": "Config は YAML で書く p/"}]))
        r2 = self.store.imprint([_turn("config は YAML で書く p/ 配置")],
                                _prop("config は YAML で書く", ["config"], kind="所在",
                                      evidence=[{"source": "user", "span": "config は YAML で書く p/"}]))
        self.assertTrue(r2.get("reinforced"))
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["strength"], 2)

    def test_seed_without_conditions_keeps_old_behaviour(self):
        r1 = self.store.imprint([_turn(TEXT_2)], _prop(TEXT_2, ["モジュール2"]))
        sid = r1["seed"]["id"]
        for s in self.store.state["seeds"]:  # 縁の無い旧形式の種子
            if s["id"] == sid:
                s["conditions"] = []
        r = self.store.imprint([_turn(TEXT_3)], _prop(TEXT_3, ["モジュール3", "Rust3"]))
        self.assertTrue(r.get("reinforced"))


TEXT_D2 = "モジュール2の言語はGo2を採用 (Pythonは却下: ユーザーの明示的な決定)"
TEXT_D3 = "モジュール3の言語はRust3を採用 (Pythonは却下: ユーザーの明示的な決定)"


class TestTwoWordRule(_Case):
    """x9_3s: 却下側の語 "Python" 1 語の共有だけで別モジュールの決定が併合された。"""

    def plant(self, text=TEXT_D2, conds=("モジュール2", "Go2", "Python")):
        r = self.store.imprint([_turn(text)], _prop(text, conds))
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("圓成實", "決定"))
        return r["seed"]["id"]

    def test_x9_3s_one_shared_word_does_not_merge(self):
        sid = self.plant()
        seed = self.store.get_by_ids([sid])[0]
        self.assertGreaterEqual(alaya_mod._merge_similarity(seed, TEXT_D3, ["モジュール3", "Rust3", "Python"]),
                                alaya_mod.MERGE_SIMILARITY_THRESHOLD)
        r = self.store.imprint([_turn(TEXT_D3)], _prop(TEXT_D3, ["モジュール3", "Rust3", "Python"]))
        self.assertTrue(r["is_new"])
        self.assertNotEqual(r["seed"]["id"], sid)
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 1)

    def test_x9_3s_explicit_reinforce_one_shared_word_is_new(self):
        sid = self.plant()
        r = self.store.imprint([_turn(TEXT_D3)], _prop(TEXT_D3, ["モジュール3", "Rust3", "Python"],
                                                       action="reinforce", target_id=sid))
        self.assertTrue(r["is_new"])
        self.assertIn("縁を必要語数共有しない", r["reason"])
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 1)

    def test_two_shared_of_three_merges(self):
        sid = self.plant()
        text = "モジュール2の言語はRust3を採用 (Pythonは却下: ユーザーの明示的な決定)"
        r = self.store.imprint([_turn(text)], _prop(text, ["モジュール2", "Rust3", "Python"]))
        self.assertTrue(r.get("reinforced"))
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 2)

    def test_explicit_reinforce_two_shared_works(self):
        sid = self.plant()
        r = self.store.imprint([_turn(TEXT_D2)], _prop(TEXT_D2, ["Go2", "Python"], action="reinforce", target_id=sid))
        self.assertTrue(r.get("reinforced"))

    def test_smaller_side_one_condition_requires_that_one(self):
        sid = self.plant(TEXT_D2, ("Go2",))
        r = self.store.imprint([_turn(TEXT_D3)], _prop(TEXT_D3, ["モジュール3", "Rust3", "Python"]))
        self.assertTrue(r["is_new"])  # 共有 0 -> 併合しない
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 1)
        r2 = self.store.imprint([_turn(TEXT_D2)], _prop(TEXT_D2, ["Go2", "モジュール2", "Python"]))
        self.assertTrue(r2.get("reinforced"))  # その 1 語を共有 -> 併合
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 2)

    def test_single_grounded_condition_degrades_to_one_word(self):
        """提案の縁が接地後 1 語だけ (Python) なら、小さい側 1 語 = 必要語数 1 に落ちて併合する (利用者裁定: 1 語の側はその語を要る)。"""
        sid = self.plant()
        r = self.store.imprint([_turn(TEXT_D3)], _prop(TEXT_D3, ["モジュールX", "RustX", "Python"]))
        self.assertTrue(r.get("reinforced"))
        self.assertEqual(r["seed"]["id"], sid)
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 2)

    def test_case_duplicate_conditions_count_once(self):
        """["Python", "python"] は小文字化・重複除去で 1 語 = 必要語数 1 (seed 側の Python 1 語共有で併合)。"""
        sid = self.plant()
        r = self.store.imprint([_turn(TEXT_D3)], _prop(TEXT_D3, ["Python", "python"]))
        self.assertTrue(r.get("reinforced"))
        self.assertEqual(r["seed"]["id"], sid)
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 2)

    def test_shares_condition_helper(self):
        seed = {"conditions": ["A", "b", "c"]}
        f = alaya_mod._shares_condition
        self.assertFalse(f(seed, ["a", "x"]))
        self.assertTrue(f(seed, ["a", "B"]))
        self.assertTrue(f({"conditions": ["a"]}, ["A", "x", "y"]))
        self.assertFalse(f({"conditions": ["a"]}, ["x"]))
        self.assertTrue(f({"conditions": []}, ["x"]))
        self.assertTrue(f(seed, []))


class TestSharedConditionExplicitReinforce(_Case):
    def test_target_without_shared_condition_falls_to_new_with_note(self):
        sid = self.plant_module2()
        # 内容の似ていない別件の user 提案を、同 class の target として名指す (D/R は user span のある提案には効かない)
        r = self.store.imprint([_turn(TEXT_3)], _prop(TEXT_3, ["モジュール3", "Rust3"], action="reinforce", target_id=sid))
        self.assertTrue(r["is_new"])
        self.assertIn("縁を必要語数共有しない", r["reason"])
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 1)

    def test_target_with_shared_condition_reinforces(self):
        sid = self.plant_module2()
        r = self.store.imprint([_turn(TEXT_2)], _prop(TEXT_2, ["Go2"], action="reinforce", target_id=sid))
        self.assertTrue(r.get("reinforced"))
        self.assertEqual(self.store.get_by_ids([sid])[0]["strength"], 2)

    def test_d_wins_over_shared_condition_for_user_span_target(self):
        sid = self.plant_module2()
        w = [LedgerTurn(user_text="質問", assistant_text="Rust3 を採用し Python は却下", id=0, project=PROJ)]
        p = {"content": "Rust3 を採用 (Python は却下)", "conditions": ["Rust3"], "polarity": "無記", "kind": "決定",
             "action": "reinforce", "target_id": sid,
             "evidence": [{"source": "assistant", "span": "Rust3 を採用し Python は却下"}]}
        r = self.store.imprint(w, p)
        self.assertEqual(r["user_span_match"], sid)
        self.assertIsNone(r["is_new"])


if __name__ == "__main__":
    unittest.main()
