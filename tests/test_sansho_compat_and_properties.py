import ast
import os
import shutil
import sys
import tempfile
import unicodedata
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import sansho  # noqa: E402
from raiya.alaya import VALID_POLARITIES, AlayaStore  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.sansho import Turn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402


def _string_args_of_calls(filename, attr_names):
    """Copy string-literal arguments from existing test source via AST.

    CJK must not be retyped: look-alike compatibility kanji are different
    code points. This helper only returns slices already present in filename.
    """
    path = os.path.join(os.path.dirname(__file__), filename)
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = []
    wanted = set(attr_names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = getattr(func, "attr", None) or getattr(func, "id", None)
        if attr not in wanted:
            continue
        args = []
        ok = True
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                args.append(a.value)
            else:
                ok = False
                break
        if ok and args:
            out.append((attr, args))
    return out


def _compat_block_bounds():
    """Last hyphen-separated block of sansho._CJK_RANGE is U+F900-U+FAFF."""
    cps = [ord(c) for c in sansho._CJK_RANGE]
    return cps[-3], cps[-1]


def _first_nfkc_non_invariant_compat():
    start, end = _compat_block_bounds()
    for cp in range(start, end + 1):
        ch = chr(cp)
        folded = unicodedata.normalize("NFKC", ch)
        if folded != ch:
            return ch, folded
    raise AssertionError("no NFKC-non-invariant char in compatibility block")


def _first_nfkc_invariant_compat():
    start, end = _compat_block_bounds()
    for cp in range(start, end + 1):
        ch = chr(cp)
        if unicodedata.normalize("NFKC", ch) == ch:
            return ch
    raise AssertionError("no NFKC-invariant char in compatibility block")


def _labels():
    enjou, etaki, henge = sansho.VALID_SANSHO
    return enjou, etaki, henge


def _fullwidth_en_fixture():
    """Copy the fullwidth Latin pair from test_sansho.py via AST.

    Returns (fullwidth_condition, halfwidth_condition, fullwidth_user_text,
    halfwidth_user_text). Halfwidth forms are NFKC of the copied literals,
    never retyped.
    """
    path = os.path.join(os.path.dirname(__file__), "test_sansho.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = getattr(func, "attr", None) or getattr(func, "id", None)
        if attr != "ground_conditions":
            continue
        cond = None
        user = None
        if node.args and isinstance(node.args[0], ast.List) and node.args[0].elts:
            elt = node.args[0].elts[0]
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                cond = elt.value
        if (
            len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            user = node.args[1].value
        if cond and user and cond != unicodedata.normalize("NFKC", cond):
            return (
                cond,
                unicodedata.normalize("NFKC", cond),
                user,
                unicodedata.normalize("NFKC", user),
            )
    raise AssertionError("fullwidth en fixture not found in test_sansho.py")




class TestCompatKanjiAndNfkcNonInvariant(unittest.TestCase):
    """(a) CJK compatibility ideographs / NFKC non-invariants.

    `_n` documents NFKC as the first step. Fullwidth Latin is already pinned
    in test_sansho.py; the compatibility block named in `_CJK_RANGE` is not.
    Characters are taken from that range (chr of bounds already in sansho.py),
    never typed as literals.
    """

    def test_nfkc_non_invariant_compat_ideograph_folds_in_n(self):
        compat, unified = _first_nfkc_non_invariant_compat()
        self.assertNotEqual(compat, unified)
        self.assertNotEqual(ord(compat), ord(unified[0]))
        self.assertEqual(sansho._n(compat), sansho._n(unified))

    def test_compat_form_span_against_unified_user_is_enjou(self):
        # After NFKC the two forms are the same string, so a compat-form span
        # is treated as grounded in unified-form user text (and vice versa).
        compat, unified = _first_nfkc_non_invariant_compat()
        enjou, etaki, henge = _labels()
        self.assertEqual(sansho.classify_sansho(compat, unified, ""), enjou)
        self.assertEqual(sansho.classify_sansho(unified, compat, ""), enjou)
        self.assertNotEqual(sansho.classify_sansho(compat, unified, ""), henge)

    def test_compat_form_absent_from_both_sides_is_henge(self):
        compat, _unified = _first_nfkc_non_invariant_compat()
        enjou, etaki, henge = _labels()
        klass = sansho.classify_sansho(compat, "xyz", "abc")
        self.assertEqual(klass, henge)
        self.assertNotEqual(klass, enjou)
        self.assertNotEqual(klass, etaki)

    def test_nfkc_invariant_compat_ideograph_is_not_rewritten_by_n(self):
        # Some code points in the compatibility block have no NFKC mapping.
        # `_n` must leave that single character itself (no silent rewrite).
        invariant = _first_nfkc_invariant_compat()
        self.assertEqual(unicodedata.normalize("NFKC", invariant), invariant)
        self.assertEqual(sansho._n(invariant), invariant)

    def test_fullwidth_latin_pair_from_existing_fixture_classifies_as_enjou(self):
        # `_n` equality of the pair is already pinned; classify_sansho with the
        # same pair (fullwidth span vs halfwidth user_text) was not.
        enjou, etaki, henge = _labels()
        full = half = None
        for attr, args in _string_args_of_calls("test_sansho.py", ("_n",)):
            if len(args) == 1 and args[0] and args[0] != unicodedata.normalize("NFKC", args[0]):
                full = args[0]
            elif full is not None and len(args) == 1 and sansho._n(args[0]) == sansho._n(full):
                half = args[0]
                break
        self.assertIsNotNone(full)
        self.assertIsNotNone(half)
        self.assertNotEqual(full, half)
        self.assertEqual(sansho.classify_sansho(full, half, ""), enjou)
        self.assertEqual(sansho.classify_sansho(half, full, ""), enjou)


class TestUngroundedSpanNeverPromoted(unittest.TestCase):
    """(e) Property: a span that is not a `_n` substring of either side must
    never be classified as the first two VALID_SANSHO values (user-grounded /
    assistant-only). evaluate_proposal must not adopt those decisions either.
    """

    def _ungrounded_cases(self):
        cases = []
        for attr, args in _string_args_of_calls("test_sansho.py", ("classify_sansho",)):
            if len(args) < 3:
                continue
            span, user, assistant = args[0], args[1], args[2]
            cases.append((span + "Q", user, assistant))
            cases.append(("Q" + span, user, assistant))
            cases.append((span[::-1], user, assistant))
            cases.append((span + span[:1] + "Z", user, assistant))
        compat, unified = _first_nfkc_non_invariant_compat()
        cases.append((compat, "xyz", "abc"))
        cases.append((unified, "xyz", "abc"))
        cases.append(("", "hello world", "hello world"))
        cases.append(("   ", "hello world", "hello world"))
        cases.append(("UNGROUNDED_MARKER_xyz", "hello world", "abc"))
        return cases

    def test_property_ungrounded_span_never_becomes_enjou_or_etaki(self):
        enjou, etaki, henge = _labels()
        polarity = VALID_POLARITIES[-1]
        seen = 0
        adopted = (enjou, etaki)
        for span, user, assistant in self._ungrounded_cases():
            nspan = sansho._n(span)
            if nspan.strip() and (
                nspan in sansho._n(user) or nspan in sansho._n(assistant)
            ):
                continue
            seen += 1
            klass = sansho.classify_sansho(span, user, assistant)
            self.assertEqual(klass, henge)
            self.assertNotIn(klass, adopted)

            turn = Turn(user_text=user, assistant_text=assistant)
            proposal = {
                "content": span or "x",
                "conditions": ["x"],
                "polarity": polarity,"kind": "所在",
                "evidence": {"source": "user", "span": span},
            }
            result = sansho.evaluate_proposal(proposal, turn)
            self.assertNotIn(result["sansho"], adopted)
            self.assertNotIn(result["decision"], adopted)
            self.assertEqual(result["sansho"], henge)
            self.assertEqual(result["decision"], henge)
        self.assertGreaterEqual(seen, 10)



class TestGerminateUsesSameNAsGate(unittest.TestCase):
    """germinate used .lower() only; the three-natures gate uses `_n` (NFKC).

    A seed whose en is the fullwidth Latin already stored by ground_conditions
    must fire when recalled with the NFKC-equivalent halfwidth text (and the
    reverse). Strings are copied from test_sansho.py via AST, never retyped.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.full_cond, self.half_cond, self.full_user, self.half_user = (
            _fullwidth_en_fixture()
        )
        self.polarity = VALID_POLARITIES[-1]

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _store(self):
        return alaya_store_at(
            os.path.join(self.tmpdir, "alaya.json"),
            os.path.join(self.tmpdir, "imprint_log.json"),
        )

    def _plant(self, store, condition, user_text):
        store.begin_new_life()
        user_text = user_text + " p/"  # #149: 所在 の形 (`/` を含む語)
        turn = Turn(user_text=user_text, assistant_text="")
        proposal = {
            "content": condition,
            "conditions": [condition],
            "polarity": self.polarity,"kind": "所在",
            "evidence": {"source": "user", "span": user_text},
        }
        first = store.imprint(turn, proposal)
        second = store.imprint(turn, proposal)
        self.assertIsNotNone(second.get("seed"))
        self.assertGreaterEqual(second["seed"]["strength"], 2)
        self.assertEqual(first["seed"]["conditions"], [condition])
        return second["seed"]

    def test_fullwidth_en_germinates_on_halfwidth_recall(self):
        store = self._store()
        self._plant(store, self.full_cond, self.full_user)
        fired = store.germinate(self.half_cond)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["conditions"], [self.full_cond])

    def test_halfwidth_en_germinates_on_fullwidth_recall(self):
        store = self._store()
        self._plant(store, self.half_cond, self.half_user)
        fired = store.germinate(self.full_cond)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["conditions"], [self.half_cond])

    def test_memory_engine_recall_fullwidth_seed_with_halfwidth_text(self):
        engine = MemoryEngine(state_dir=self.tmpdir)
        full_user = self.full_user + " p/"  # #149: 所在 の形 (`/` を含む語)
        turn = Turn(user_text=full_user, assistant_text="")
        proposal = {
            "content": self.full_cond,
            "conditions": [self.full_cond],
            "polarity": self.polarity,"kind": "所在",
            "evidence": {"source": "user", "span": full_user},
        }
        engine.imprint(turn, [proposal])
        engine.imprint(turn, [proposal])
        recalled = engine.recall(self.half_cond)
        self.assertEqual(len(recalled["seeds"]), 1)
        self.assertEqual(recalled["seeds"][0]["conditions"], [self.full_cond])


if __name__ == "__main__":
    unittest.main()
