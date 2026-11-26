"""spec 2026-11-26-en-marker-table-design §4.2 R3b / §8 T2: 文中一致の対の役 (`decision_assert_in_sentence`・`conjecture_in_sentence`)。
表の語は alaya の定数にだけ mock で入れる (en の表の中身は T4・T6 の仕事)。LLM は使わない。テストは何も消さない。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, markers  # noqa: E402

ASSERT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "decision_assert_markers_20261031.json")


def _patch_assert(*words):
    return mock.patch.object(alaya, "DECISION_ASSERT_IN_SENTENCE_MARKERS", tuple(words))


def _patch_conj(*words):
    return mock.patch.object(alaya, "CONJECTURE_IN_SENTENCE_MARKERS", tuple(words))


def _seed(span):
    e = {"source": "user", "span": span}
    return {"kind": "決定", "conditions": ["Postgres"], "evidences": [e]}, [e]


class TestInSentenceAssert(unittest.TestCase):
    def test_in_sentence_assert_promotes(self):
        with _patch_assert("we'll go with"):
            self.assertTrue(alaya._has_decision_assert_final("We'll go with Postgres for the DB."))
            span = [{"span": "We'll go with Postgres for the DB.", "sansho": "圓成實", "turn_index": 0}]
            self.assertEqual(
                alaya._resolve_kind({"kind": "性向", "content": "x"}, span, ["Postgres"]),
                ("決定", alaya.PROMOTION_NOTE),
            )

    def test_in_sentence_assert_ignores_question(self):
        with _patch_assert("we'll go with"):
            self.assertFalse(alaya._has_decision_assert_final("Should we go with Postgres?"))
            self.assertFalse(alaya._has_decision_assert_final("We'll go with Postgres?"))

    def test_in_sentence_needs_word_boundary(self):
        with _patch_assert("let's use"):
            self.assertFalse(alaya._has_decision_assert_final("Let's user-test it first."))
            self.assertTrue(alaya._has_decision_assert_final("Let's use pytest."))

    def test_in_sentence_after_abbreviation(self):
        with _patch_assert("we'll go with"):
            self.assertTrue(alaya._has_decision_assert_final("We'll go with e.g. Postgres."))

    def test_empty_tables_change_nothing(self):
        with _patch_assert(), _patch_conj():
            self.assertFalse(alaya._has_decision_assert_final("We'll go with Postgres."))
            with open(ASSERT_FIXTURE, "r", encoding="utf-8") as f:
                fixture = json.load(f)
            ja = lambda t: any(ord(c) > 0x2FF for c in t)  # noqa: E731
            hits = [t for t in fixture["hit"] if ja(t)]
            misses = [t for t in fixture["miss"] if ja(t)]
            self.assertTrue(hits and misses)
            for t in hits:
                self.assertTrue(alaya._has_decision_assert_final(t), t)
            for t in misses:
                self.assertFalse(alaya._has_decision_assert_final(t), t)


class TestInSentenceConjecture(unittest.TestCase):
    def test_conjecture_in_sentence(self):
        with _patch_conj("maybe"):
            for span, want in (
                ("Maybe Postgres for the DB.", True),
                ("Use Postgres for the DB. Maybe Redis later.", False),
                ("Maybelline uses Postgres.", False),
            ):
                seed, shown = _seed(span)
                self.assertEqual(alaya.conjecture_only(seed, shown=shown), want, span)

    def test_may_hold_conjecture_includes_in_sentence(self):
        with _patch_conj("maybe"):
            self.assertTrue(alaya._may_hold_conjecture("maybe postgres"))
            self.assertFalse(alaya._may_hold_conjecture("postgres"))


class TestRoles(unittest.TestCase):
    def test_new_roles_exist_and_ja_empty(self):
        for role in ("decision_assert_in_sentence", "conjecture_in_sentence"):
            self.assertIn(role, markers.ROLES)
            self.assertEqual(markers.MARKERS["ja"][role], ())
        self.assertEqual(alaya.DECISION_ASSERT_IN_SENTENCE_MARKERS, markers.union("decision_assert_in_sentence"))
        self.assertEqual(alaya.CONJECTURE_IN_SENTENCE_MARKERS, markers.union("conjecture_in_sentence"))


if __name__ == "__main__":
    unittest.main()
