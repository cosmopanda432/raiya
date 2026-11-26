"""spec 2026-11-26-en-marker-table-design §4.2 / §8 T1: 印の照合の畳み (R1a: 両側 casefold + `’‘` → `'`) と
ASCII 英数字の端の語の境 (R2b)。ja の判定は構成上不変。LLM は使わない。テストは何も消さない (tmp を作らない)。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, sansho  # noqa: E402
from raiya.markers import MARKERS  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
MISS_FIXTURES = (
    "decision_assert_markers_20261031.json",
    "hearsay_markers_20261104.json",
    "user_correction_markers_20261029.json",
    "workaround_avoid_markers_20261104.json",
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _fixture_texts():
    texts = []
    for name in MISS_FIXTURES:
        with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
            texts.extend(_strings(json.load(f)))
    with open(os.path.join(FIXTURES, "recall_seeds_20261017.json"), "r", encoding="utf-8") as f:
        for seed in json.load(f)["seeds"]:
            if isinstance(seed.get("content"), str):
                texts.append(seed["content"])
            for ev in seed.get("evidences") or []:
                if isinstance(ev, dict) and isinstance(ev.get("span"), str):
                    texts.append(ev["span"])
    return texts


class TestFoldAndBoundary(unittest.TestCase):
    def test_curly_apostrophe_matches(self):
        self.assertTrue(alaya._has_marker(sansho._n("That’s wrong"), alaya.USER_CORRECTION_MARKERS))

    def test_left_boundary_blocks_inside_word(self):
        self.assertFalse(alaya._has_marker("the bill is overdue to the bank", ("due to",)))
        self.assertTrue(alaya._has_marker("pip fails due to the proxy", ("due to",)))

    def test_right_boundary_blocks_inflection(self):
        self.assertFalse(alaya._has_marker("we used to", ("we use",)))
        self.assertTrue(alaya._has_marker("we use it", ("we use",)))

    def test_symbol_edge_keeps_substring(self):
        self.assertTrue(alaya._has_marker("a→b", ("→",)))

    def test_ends_with_left_boundary(self):
        self.assertFalse(alaya._ends_with_marker("the wiki prefer", ("i prefer",)))
        self.assertTrue(alaya._ends_with_marker("so i prefer", ("i prefer",)))

    def test_final_matchers_fold_apostrophe(self):
        with mock.patch.object(alaya, "WORKAROUND_AVOID_MARKERS", ("that's it",)):
            self.assertTrue(alaya._has_avoid_final("Restarting fixes the cache. That’s it."))

    def test_other_apostrophes_are_not_folded(self):
        # 既知の限界 (§9 の 7): U+02BC は畳まない
        self.assertFalse(alaya._has_marker("Thatʼs wrong", alaya.USER_CORRECTION_MARKERS))

    def test_cjk_neighbour_is_a_boundary(self):
        self.assertTrue(alaya._has_marker("設定はinstead ofで", ("instead of",)))

    def test_later_occurrence_is_found_after_a_blocked_one(self):
        self.assertTrue(alaya._has_marker("we used to. we use it", ("we use",)))


class TestJapaneseUnchanged(unittest.TestCase):
    def test_ja_markers_have_no_ascii_alnum_edge_or_apostrophe(self):
        for role, table in MARKERS["ja"].items():
            for m in table:
                folded = alaya._marker_fold(m)
                self.assertNotIn(folded[0], alaya._ASCII_WORD_CHARS, (role, m))
                self.assertNotIn(folded[-1], alaya._ASCII_WORD_CHARS, (role, m))
                self.assertNotIn("'", m, (role, m))

    def test_ja_judgement_unchanged_on_fixtures(self):
        texts = _fixture_texts()
        self.assertTrue(texts)
        checked = 0
        for t in texts:
            for x in (t, sansho._n(t)):
                folded = x.casefold()
                for role, table in MARKERS["ja"].items():
                    self.assertEqual(
                        alaya._has_marker(x, table), any(m.casefold() in folded for m in table), (role, x))
                    self.assertEqual(
                        alaya._ends_with_marker(x, table), any(folded.endswith(m.casefold()) for m in table), (role, x))
                    checked += 1
        self.assertGreater(checked, 1000)

    def test_ascii_then_ja_marker(self):
        # `_n` で詰まった「Flaskではなく」の左は ASCII 英字だが、ja の印には境を課さない
        self.assertTrue(alaya._has_marker(sansho._n("Flask ではなく FastAPI"), alaya.DECISION_REJECT_MARKERS))


if __name__ == "__main__":
    unittest.main()
