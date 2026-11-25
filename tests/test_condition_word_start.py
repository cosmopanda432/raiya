"""発芽の縁の照合の境 (設計書 2026-11-25-recall-capacity-design §4.1 案 8d・T1) の決定論 pin。

縁が ASCII 英数字で始まるなら語頭の境を要し、2 字以下の縁は語末の境も要する。3 字以上の前方一致
(git → github) は既知の限界として残す。門 (sansho) の接地は部分一致のまま。LLM は使わない。
テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import alaya as alaya_mod  # noqa: E402
from test_layered_circles import _seed, _StateCase  # noqa: E402

def occurs(key, text_n):
    return alaya_mod._occurs_as_word(key, text_n)


class TestOccursAsWord(unittest.TestCase):
    def test_ascii_condition_inside_word_does_not_occur(self):
        self.assertFalse(occurs("art", "let's start now"))
        self.assertFalse(occurs("l", "sqlite版を使う"))
        self.assertFalse(occurs("mit", "commit して"))
        self.assertFalse(occurs("d", "<pasted_content>"))

    def test_short_condition_needs_both_boundaries(self):
        self.assertFalse(occurs("l", "l13 を読む"))
        self.assertFalse(occurs("pi", "pip install"))
        self.assertFalse(occurs("id", "idx を見る"))
        self.assertTrue(occurs("l", "l の形"))
        self.assertTrue(occurs("l", "l' を裁定"))
        self.assertTrue(occurs("pi", "pi拡張"))
        self.assertTrue(occurs("l2", "l2 の件"))

    def test_long_condition_keeps_word_start_prefix(self):
        # 既知の限界: 3 字以上は右を見ない
        self.assertTrue(occurs("hook", "check the hooks"))
        self.assertTrue(occurs("git", "github の issue"))
        self.assertTrue(occurs("python", "python3 -m raiya"))

    def test_boundaries_are_symbols_cjk_and_text_edges(self):
        self.assertTrue(occurs("sansho", "raiya/sansho.py"))
        self.assertTrue(occurs("art", "my_art"))
        self.assertTrue(occurs("art", "art class"))
        self.assertTrue(occurs("raiya", "raiya-hook"))

    def test_non_ascii_or_symbol_first_condition_keeps_substring(self):
        self.assertTrue(occurs("キュー", "メッセージキューは"))
        self.assertTrue(occurs("-con", "raiya-hook-contract"))
        # 設計書は ("-c", "raiya-hook-contract") を真としたが、規則 (2 字以下で ASCII で終わる縁は語末の境) では偽。規則を取る
        self.assertTrue(occurs("-c", "a -c flag"))
        self.assertFalse(occurs("-c", "raiya-hook-contract"))
        self.assertFalse(occurs("", "abc"))

    def test_a_later_position_can_satisfy_the_boundary(self):
        # 最初の出現は語の途中、2 つ目が語頭
        self.assertTrue(occurs("art", "start art"))
        self.assertTrue(occurs("l", "l13 l"))


def _art_seed(seed_id=1, conditions=("art",), kind="決定"):
    return _seed(seed_id, list(conditions), kind=kind, project=None,
                 evidences=[{"source": "user", "span": "I bought art supplies.", "ledger_id": 1, "life": 1}])


class TestGermination(_StateCase):
    def test_mid_word_condition_does_not_fire(self):
        eng = self.write([_art_seed()])
        x = eng.explain_recall("When did I start?", project=None)
        self.assertNotIn(1, [g["id"] for g in x["germinated"]])
        self.assertEqual([r["reason"] for r in x["germination_rejected"] if r["id"] == 1], ["縁の一致が足りない (0/1)"])
        y = eng.explain_recall("my art class", project=None)
        self.assertIn(1, [g["id"] for g in y["germinated"]])

    def test_consolidation_view_uses_same_rule(self):
        eng = self.write([_art_seed()])
        self.assertEqual(eng.alaya.consolidation_view({None}, ["let's start"])["related"], [])
        self.assertEqual([s["id"] for s in eng.alaya.consolidation_view({None}, ["art class"])["related"]], [1])


class TestLatency(_StateCase):
    def test_three_mid_word_conditions_worst_case(self):
        # 縁が語の途中にだけ当たる種子 300 件 × 5 万字 (メモ化の前は 1.0〜1.07 s)
        eng = self.write([_art_seed(i, ("bet", "pha", "lph")) for i in range(1, 301)])
        text = ("alphabet " * 5600)[:50000]
        t = time.perf_counter()
        eng.explain_recall(text, project=None)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_pattern_scans_once_per_distinct_condition(self):
        # 壁時計に頼らない費用の pin: 同じ縁を持つ種子 300 件でも、本文の走査は異なる縁ごとに 1 回
        eng = self.write([_art_seed(i, ("bet", "pha", "lph")) for i in range(1, 301)])
        calls = []
        real = alaya_mod._word_pattern

        def spy(key):
            pat = real(key)
            outer = pat

            class Counted:
                def search(self, text):
                    calls.append(key)
                    return outer.search(text)

            return Counted()

        with mock.patch.object(alaya_mod, "_word_pattern", spy):
            eng.explain_recall("alphabet bet", project=None)
            self.assertEqual(sorted(calls), ["bet", "lph", "pha"])
            calls.clear()
            eng.alaya.consolidation_view({None}, ["alphabet bet"])
            self.assertEqual(sorted(calls), ["bet", "lph", "pha"])


if __name__ == "__main__":
    unittest.main()
