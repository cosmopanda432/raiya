"""形の検査の語表 (raiya/markers.py) の pin。spec: docs/superpowers/specs/2026-10-24-marker-table-design.md §6.1。"""
import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, markers, sansho  # noqa: E402
from raiya.markers import LANGUAGES, MARKERS, ROLES  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "recall_seeds_20261017.json")
ASSERT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "decision_assert_markers_20261031.json")
CORRECTION_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "user_correction_markers_20261029.json")


class TestTables(unittest.TestCase):
    def test_ja_tables_are_the_previous_tuples(self):
        self.assertEqual(MARKERS["ja"]["decision_reject"], ("却下", "不採用", "ではなく", "でなく", "採らない"))
        self.assertEqual(MARKERS["ja"]["workaround_cause"], ("原因", "ため", "→"))
        self.assertEqual(MARKERS["ja"]["preference_suffix"], ("してほしい", "てほしい", "したい", "を好む"))

    def test_user_correction_tables(self):
        self.assertEqual(
            MARKERS["ja"]["user_correction"], ("元に戻してください", "やり直してください", "やめてください")
        )
        self.assertEqual(
            MARKERS["en"]["user_correction"], ("that's wrong", "not what i asked", "revert that", "undo that")
        )
        # miss fixture に当たって外した短い語は入れない
        for w in ("違う", "戻して", "やめて", "勝手に", "そうじゃなくて", "そうではなくて", "間違って", "やり直して"):
            self.assertNotIn(w, MARKERS["ja"]["user_correction"])

    def test_every_language_has_every_role(self):
        self.assertEqual(set(MARKERS), set(LANGUAGES))
        for lang in LANGUAGES:
            self.assertEqual(set(MARKERS[lang]), set(ROLES))
            for role in ROLES:
                self.assertIsInstance(MARKERS[lang][role], tuple)

    def test_en_preference_suffix_table(self):
        # spec 2026-11-26-en-marker-table-design §5.1 (P1): ja の「を好む」「てほしい」に当たる 8 形だけ。i want to / i'd like / please は入れない
        self.assertEqual(
            MARKERS["en"]["preference_suffix"],
            ("i prefer", "i'd prefer", "i would prefer", "i'd rather", "i would rather",
             "i want you to", "i'd like you to", "i would like you to"),
        )

    def test_en_decision_reject_excludes_bare_negation(self):
        for w in ("not", "no", "don't", "never", "avoid", "over", "vs"):
            self.assertNotIn(w, MARKERS["en"]["decision_reject"])

    def test_marker_forms(self):
        for lang in LANGUAGES:
            for role in ROLES:
                table = MARKERS[lang][role]
                self.assertEqual(len(set(table)), len(table), (lang, role))
                for m in table:
                    self.assertIsInstance(m, str)
                    self.assertTrue(m)
                    self.assertEqual(sansho._n(m), m)
                    self.assertEqual(m.casefold(), m)


class TestUnion(unittest.TestCase):
    def test_union_contains_ja_then_en(self):
        self.assertEqual(
            markers.union("decision_reject"),
            MARKERS["ja"]["decision_reject"] + MARKERS["en"]["decision_reject"],
        )
        self.assertEqual(len(markers.union("decision_reject")), 9)   # ja 5 + en 4 (rejected は 2026-11-26 に外した)
        self.assertNotIn("rejected", markers.MARKERS["en"]["decision_reject"])

    def test_union_dedups_shared_marker(self):
        self.assertEqual(
            markers.union("workaround_cause"),
            ("原因", "ため", "→", "because", "caused by", "root cause", "due to"),
        )

    def test_union_is_a_tuple_and_stable(self):
        for role in ROLES:
            self.assertEqual(markers.union(role), markers.union(role))
            self.assertIs(type(markers.union(role)), tuple)

    def test_unknown_role_raises_key_error(self):
        with self.assertRaises(KeyError):
            markers.union("nonexistent")

    def test_alaya_constants_are_the_union(self):
        self.assertEqual(alaya.DECISION_REJECT_MARKERS, markers.union("decision_reject"))
        self.assertEqual(alaya.WORKAROUND_CAUSE_MARKERS, markers.union("workaround_cause"))
        self.assertEqual(alaya.PREFERENCE_SUFFIXES, markers.union("preference_suffix"))
        self.assertEqual(alaya.USER_CORRECTION_MARKERS, markers.union("user_correction"))
        self.assertEqual(alaya.HEARSAY_MARKERS, markers.union("hearsay"))
        self.assertEqual(alaya.WORKAROUND_AVOID_MARKERS, markers.union("workaround_avoid"))
        self.assertEqual(alaya.SELF_STATED_CAUSE_MARKERS, markers.union("self_stated_cause"))


class TestCasefold(unittest.TestCase):
    def test_ja_markers_are_casefold_invariant(self):
        for role in ROLES:
            for m in MARKERS["ja"][role]:
                self.assertEqual(m.casefold(), m)

    def test_casefold_never_produces_ja_marker_chars(self):
        chars = {ch for role in ROLES for m in MARKERS["ja"][role] for ch in m}
        for c in range(0x110000):
            if 0xD800 <= c <= 0xDFFF:
                continue
            ch = chr(c)
            folded = ch.casefold()
            if folded != ch:
                self.assertFalse(set(folded) & chars, hex(c))


class TestCrossLanguage(unittest.TestCase):
    def test_en_markers_do_not_fire_on_production_seeds(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            seeds = json.load(f)["seeds"]
        texts = []
        for seed in seeds:
            if isinstance(seed.get("content"), str):
                texts.append(seed["content"])
            for ev in seed.get("evidences") or []:
                if isinstance(ev, dict) and isinstance(ev.get("span"), str):
                    texts.append(ev["span"])
        self.assertTrue(texts)
        for text in texts:
            for role, const, fn in (
                ("decision_reject", alaya.DECISION_REJECT_MARKERS, alaya._has_marker),
                ("workaround_cause", alaya.WORKAROUND_CAUSE_MARKERS, alaya._has_marker),
                ("preference_suffix", alaya.PREFERENCE_SUFFIXES, alaya._ends_with_marker),
                ("user_correction", alaya.USER_CORRECTION_MARKERS, alaya._has_marker),
                ("hearsay", alaya.HEARSAY_MARKERS, alaya._has_marker),
                ("workaround_avoid", alaya.WORKAROUND_AVOID_MARKERS, alaya._has_marker),
                ("self_stated_cause", alaya.SELF_STATED_CAUSE_MARKERS, alaya._has_marker),
                ("decision_assert_in_sentence", alaya.DECISION_ASSERT_IN_SENTENCE_MARKERS, alaya._has_marker),
                ("conjecture_in_sentence", alaya.CONJECTURE_IN_SENTENCE_MARKERS, alaya._has_marker),
            ):
                union_hit = fn(text, const)
                ja_hit = fn(text, MARKERS["ja"][role])
                if union_hit != ja_hit:
                    hit = [m for m in MARKERS["en"][role] if fn(text, (m,))]
                    self.fail("en marker fired on production text: role={} text={!r} en={}".format(role, text, hit))


class TestUserCorrectionFixture(unittest.TestCase):
    def setUp(self):
        with open(CORRECTION_FIXTURE, "r", encoding="utf-8") as f:
            self.fixture = json.load(f)

    def test_every_hit_sentence_has_a_marker(self):
        self.assertTrue(self.fixture["hit"])
        for text in self.fixture["hit"]:
            self.assertTrue(alaya._has_marker(sansho._n(text), alaya.USER_CORRECTION_MARKERS), text)

    def test_no_miss_sentence_has_a_marker(self):
        self.assertTrue(self.fixture["miss"])
        for text in self.fixture["miss"]:
            self.assertFalse(alaya._has_marker(sansho._n(text), alaya.USER_CORRECTION_MARKERS), text)

    def test_decision_reject_and_correction_are_pinned_separately(self):
        # 既存の decision_reject の例は訂正の印に当たらず、訂正の例は decision_reject の印を持つとは限らない
        self.assertTrue(alaya._has_marker("B ではなく A", alaya.DECISION_REJECT_MARKERS))
        self.assertFalse(alaya._has_marker("B ではなく A", alaya.USER_CORRECTION_MARKERS))
        self.assertFalse(alaya._has_marker("元に戻してください", alaya.DECISION_REJECT_MARKERS))


class TestDecisionAssertFixture(unittest.TestCase):
    """#117 KP2b: decision_assert は文末一致。ja の語は hit/miss fixture で決め、en は () 暫定。"""

    def setUp(self):
        with open(ASSERT_FIXTURE, "r", encoding="utf-8") as f:
            self.fixture = json.load(f)

    def test_tables(self):
        self.assertEqual(
            MARKERS["ja"]["decision_assert"], ("にした", "にしました", "に決めた", "に決定した", "で行く", "を採用した", "を採用する")
        )
        self.assertEqual(MARKERS["en"]["decision_assert"], ())
        self.assertEqual(alaya.DECISION_ASSERT_MARKERS, markers.union("decision_assert"))
        # R3b (2026-11-26): 文中一致の対の役。ja は空 (en は T4 で埋める)
        self.assertEqual(MARKERS["ja"]["decision_assert_in_sentence"], ())
        self.assertEqual(
            MARKERS["en"]["decision_assert_in_sentence"],
            ("we decided to", "i decided to", "we've decided to", "i've decided to", "we decided on", "i decided on",
             "we'll go with", "i'll go with", "we're going with", "i'm going with", "let's go with", "we went with",
             "we chose", "we settled on", "i settled on", "let's use"),
        )
        self.assertEqual(MARKERS["ja"]["conjecture_in_sentence"], ())
        self.assertEqual(alaya.DECISION_ASSERT_IN_SENTENCE_MARKERS, markers.union("decision_assert_in_sentence"))
        self.assertEqual(alaya.CONJECTURE_IN_SENTENCE_MARKERS, markers.union("conjecture_in_sentence"))

    def test_every_hit_has_a_sentence_final_marker(self):
        self.assertTrue(self.fixture["hit"])
        for text in self.fixture["hit"]:
            self.assertTrue(alaya._has_decision_assert_final(text), text)

    def test_no_miss_has_a_sentence_final_marker(self):
        self.assertTrue(self.fixture["miss"])
        for text in self.fixture["miss"]:
            self.assertFalse(alaya._has_decision_assert_final(text), text)

    def test_decision_assert_fixture_misses_fixed_phrases(self):
        for text in ("SQLite の性能を気にした。", "SQLite のエラーを目にした。", "その話を耳にした。", "SQLite の性能を気にしました。", "SQLite のエラーを目にしました。",
                     "SQLite の名を口にした。", "ログを手にした。"):
            self.assertIn(text, self.fixture["miss"])
            self.assertFalse(alaya._has_decision_assert_final(text), text)
        self.assertTrue(alaya._has_decision_assert_final("ビルドは後回しにした。"))
        self.assertTrue(alaya._has_decision_assert_final("「SQLite にした」"))

    def test_no_condition_misses_match_the_marker_but_never_promote(self):
        # 「それにした。」は印には当たる (縁が無いので _resolve_kind が上げない = test_kind_promotion_kp)
        for text in self.fixture["miss_no_condition"]:
            self.assertTrue(alaya._has_decision_assert_final(text), text)

    def test_no_decision_assert_final_on_production_seeds(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            seeds = json.load(f)["seeds"]
        texts = []
        for seed in seeds:
            if isinstance(seed.get("content"), str):
                texts.append(seed["content"])
            for ev in seed.get("evidences") or []:
                if isinstance(ev, dict) and isinstance(ev.get("span"), str):
                    texts.append(ev["span"])
        self.assertTrue(texts)
        for text in texts:
            self.assertFalse(alaya._has_decision_assert_final(text), text)


class TestAstPin(unittest.TestCase):
    def test_markers_imports_nothing(self):
        with open(os.path.join(REPO_ROOT, "raiya", "markers.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])


if __name__ == "__main__":
    unittest.main()
