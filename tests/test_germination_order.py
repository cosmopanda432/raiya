"""① の発芽順 (設計書 2026-11-25-recall-capacity-design §6.3・T4): class → 一致した縁の異なり語の数 (多いほど先) → 強度降順 → 倉庫の順。

上限 (`GERMINATION_MAX`) と ① の描画件数 (ego_weight) は、その順の先頭から取る。LLM は使わない。
テストは `_StateCase` が自作する tmp だけを作り、それだけを消す。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import alaya  # noqa: E402
from raiya.engine import INJECT_DISPOSITIONS_ENV  # noqa: E402
from test_layered_circles import _seed, _StateCase  # noqa: E402


def _s(seed_id, conditions, strength, kind="決定", sansho="圓成實", span=None):
    span = span if span is not None else "逐語{}の発話".format(seed_id)
    source = "user" if sansho == "圓成實" else "assistant"
    return _seed(seed_id, conditions, kind=kind, sansho=sansho, strength=strength, project=None,
                 evidences=[{"source": source, "span": span, "ledger_id": seed_id, "life": 1}])


def _ids(rows):
    return [r["id"] for r in rows]


class TestGerminationOrder(_StateCase):
    def test_more_distinct_matches_first_within_class(self):
        eng = self.write([_s(1, ["kafka"], 3), _s(2, ["kafka", "queue"], 1)])
        x = eng.explain_recall("kafka queue?", project=None)
        self.assertEqual(_ids(x["seeds"]), [2, 1])
        self.assertEqual(_ids(x["germinated"]), [2, 1])
        self.assertLess(x["narrative"].index("逐語2の発話"), x["narrative"].index("逐語1の発話"))

    def test_class_still_comes_first(self):
        eng = self.write([_s(1, ["kafka", "queue"], 5, sansho="依他起"), _s(2, ["kafka"], 1)])
        x = eng.explain_recall("kafka queue?", project=None)
        self.assertEqual(_ids(x["seeds"]), [2, 1])

    def test_duplicate_conditions_count_once(self):
        eng = self.write([_s(1, ["Kafka", "kafka"], 1), _s(2, ["kafka"], 2)])
        self.assertEqual(_ids(eng.explain_recall("kafka", project=None)["seeds"]), [2, 1])

    def test_equal_matches_fall_back_to_strength_then_store_order(self):
        eng = self.write([_s(1, ["kafka"], 1), _s(2, ["kafka"], 3), _s(3, ["kafka"], 3)])
        self.assertEqual(_ids(eng.explain_recall("kafka", project=None)["seeds"]), [2, 3, 1])

    def test_cap_keeps_the_most_matching(self):
        seeds = [_s(i, ["kafka"], 5) for i in range(1, 6)] + [_s(6, ["kafka", "queue"], 1)]
        eng = self.write(seeds)
        x = eng.explain_recall("kafka queue", project=None)
        self.assertIn(6, _ids(x["germinated"]))
        capped = [r for r in x["germination_rejected"] if r["reason"] == alaya.GERMINATION_REASON_MAX]
        self.assertEqual(len(capped), 1)
        self.assertIn(capped[0]["id"], [1, 2, 3, 4, 5])

    def test_first_circle_limit_takes_head_of_germination_order(self):
        eng = self.write([_s(1, ["kafka"], 3), _s(2, ["kafka", "queue"], 1)], extra={"ego_weight": 0.2})
        x = eng.explain_recall("kafka queue?", project=None)
        first_circle = x["narrative"].split("【記録の索引")[0]   # ① に出なかった種子は ③ に落ちる (落ちた先の逐語は索引に在る)
        self.assertIn("逐語2の発話", first_circle)
        self.assertNotIn("逐語1の発話", first_circle)
        self.assertIn(1, _ids(x["index"]))
        self.assertNotIn(2, _ids(x["index"]))

    def test_disposition_with_two_matches_ranks_above_one_match_decision(self):
        # 既知の限り: 性向 は縁 2 語で発芽するので、同じ class の 1 語の 決定 より常に先になる (既定では性向は発芽しない)
        with mock.patch.dict(os.environ, {INJECT_DISPOSITIONS_ENV: "1"}):
            eng = self.write([_s(1, ["kafka"], 3), _s(2, ["kafka", "queue"], 1, kind="性向")])
            x = eng.explain_recall("kafka queue?", project=None)
        self.assertEqual(_ids(x["seeds"]), [2, 1])


if __name__ == "__main__":
    unittest.main()
