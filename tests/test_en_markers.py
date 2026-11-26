"""spec 2026-11-26-en-marker-table-design §5・§8 T4〜T7: en の印の表の hit / miss / known_hits (fixture = tests/fixtures/en_markers_20261126.json)。
fixture の文は作文 (LongMemEval の文は写さない)。LLM は使わない。store を作るテストは自作 tmp だけを消す。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, sansho  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.manas import _render_index_line, _render_seed_line  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "en_markers_20261126.json")
HEARSAY_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "hearsay_markers_20261104.json")
PROJ = "/home/dev/raiya"


def _load():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)


def _spans(text):
    return [{"span": text, "sansho": "圓成實", "turn_index": 0}]


class _Fixture(unittest.TestCase):
    ROLE = None

    @classmethod
    def setUpClass(cls):
        cls.fx = _load()[cls.ROLE]

    def judge(self, text):
        raise NotImplementedError

    def check(self, key, want):
        self.assertTrue(self.fx[key], key)
        for text in self.fx[key]:
            self.assertEqual(bool(self.judge(text)), want, text)


class TestPreference(_Fixture):
    ROLE = "preference_suffix"

    def judge(self, text):
        return alaya._has_marker(sansho._n(text), alaya.PREFERENCE_SUFFIXES)

    def test_preference_hits(self):
        self.check("hit", True)

    def test_preference_misses(self):
        self.check("miss", False)

    def test_preference_known_hits(self):
        self.check("known_hits", True)


class TestAssertInSentence(_Fixture):
    ROLE = "decision_assert_in_sentence"

    def judge(self, text):
        return alaya._has_decision_assert_final(text)

    def test_assert_hits(self):
        self.check("hit", True)

    def test_assert_misses(self):
        self.check("miss", False)

    def test_assert_known_hits(self):
        self.check("known_hits", True)


class TestKindPromotion(unittest.TestCase):
    def kind(self, span, conditions, declared="性向"):
        return alaya._resolve_kind({"kind": declared, "content": "x"}, _spans(span), conditions)[0]

    def test_kp_promotes_en_assert(self):
        self.assertEqual(self.kind("We'll go with PostgreSQL for the DB.", ["PostgreSQL"]), "決定")

    def test_preference_blocks_promotion(self):
        self.assertEqual(self.kind("I'd prefer PostgreSQL. We'll go with PostgreSQL.", ["PostgreSQL"]), "性向")

    def test_mixed_language_span_promotes(self):
        # 縁は 1 文目、印は 2 文目。同じ残りの中なので KP の (b) が立つ (§9 の 5)
        self.assertEqual(self.kind("DB は PostgreSQL。We'll go with it.", ["PostgreSQL"]), "決定")


class TestHearsay(_Fixture):
    ROLE = "hearsay"

    def judge(self, text):
        return alaya._has_marker(sansho._n(text), alaya.HEARSAY_MARKERS)

    def test_hearsay_hits(self):
        self.check("hit", True)

    def test_hearsay_misses(self):
        self.check("miss", False)
        # 既存の fixture の英文の miss (en が空だったときの pin) も当たらない
        with open(HEARSAY_FIXTURE, "r", encoding="utf-8") as f:
            english = [t for t in json.load(f)["miss"] if t.isascii()]
        self.assertEqual(len(english), 8)
        for text in english:
            self.assertFalse(self.judge(text), text)

    def test_hearsay_known_hits(self):
        self.check("known_hits", True)


class TestCause(_Fixture):
    ROLE = "self_stated_cause"

    def judge(self, text):
        return alaya._has_marker(sansho._n(text), alaya.SELF_STATED_CAUSE_MARKERS)

    def test_cause_hits(self):
        self.check("hit", True)

    def test_cause_misses(self):
        self.check("miss", False)

    def test_cause_known_hits(self):
        self.check("known_hits", True)


class TestAvoid(_Fixture):
    ROLE = "workaround_avoid"

    def judge(self, text):
        return alaya._has_avoid_final(text)

    def test_avoid_hits(self):
        self.check("hit", True)

    def test_avoid_misses(self):
        self.check("miss", False)

    def test_avoid_known_hits(self):
        self.check("known_hits", True)


class TestW1(unittest.TestCase):
    def test_w1_en_raises_workaround(self):
        forms = _load()["w1_forms"]
        self.assertEqual(len(forms), 3)
        for form in forms:
            text = form["text"]
            window = sansho.as_window([LedgerTurn(user_text=text, assistant_text="ok", id=0, project=PROJ)])
            proposal = {"content": "x", "conditions": form["conditions"], "polarity": "無記", "kind": "性向",
                        "evidence": [{"source": "user", "span": text}]}
            decision = sansho.evaluate_proposal(proposal, window)
            kind, _note = alaya._resolve_kind(proposal, decision["spans"], decision["grounded_conditions"], window)
            self.assertEqual(kind, form["kind"], text)


class TestRh2En(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()
        user = "Today stops here. Next I resume from the retry module."
        r = self.store.imprint(
            [LedgerTurn(user_text=user, assistant_text="Understood.", id=1, project=PROJ)],
            self._prop("Resume from the retry module.", [("user", "Next I resume from the retry module.")]))
        self.assertTrue(r["is_new"])

    def _prop(self, content, spans):
        return {"content": content, "conditions": ["retry module"], "polarity": "無記", "kind": "再開点",
                "evidence": [{"source": s, "span": t} for s, t in spans]}

    def imprint_assistant(self, text):
        return self.store.imprint(
            [LedgerTurn(user_text="Please continue.", assistant_text=text, id=2, project=PROJ)],
            self._prop("Resume from the retry module and check git status.", [("assistant", text)]))

    def test_rh2_en_record_only(self):
        before = self.store.list_seeds()
        r = self.imprint_assistant("You mentioned that the next step is the retry module.")
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya.RESUME_HEARSAY_RECORD_ONLY, r["reason"])
        self.assertEqual(self.store.list_seeds(), before)

    def test_rh2_en_doer_form_is_not_stopped_by_rh2(self):
        r = self.imprint_assistant("Next, I'll resume the retry module.")
        self.assertNotIn(alaya.RESUME_HEARSAY_RECORD_ONLY, r.get("reason") or "")


def _decision_seed(span, condition):
    evidence = {"source": "user", "span": span, "life": 1, "ledger_id": 3}
    seed = {"id": 1, "kind": "決定", "content": condition + " を採用", "strength": 1, "sansho": "圓成實",
            "conditions": [condition], "evidences": [evidence]}
    return seed, [evidence]


class TestConjecture(unittest.TestCase):
    def test_conjecture_pairs(self):
        fx = _load()["conjecture_in_sentence"]
        self.assertTrue(fx["hit"] and fx["miss"])
        for key, want in (("hit", True), ("miss", False)):
            for text, condition in fx[key]:
                seed, shown = _decision_seed(text, condition)
                self.assertEqual(alaya.conjecture_only(seed, shown=shown), want, text)

    def test_recall_renders_conjecture_en(self):
        seed, _shown = _decision_seed("Maybe PostgreSQL for the DB.", "PostgreSQL")
        for line in (_render_seed_line(seed), _render_index_line(seed)):
            self.assertTrue(line.startswith("- 決定(推量): Maybe PostgreSQL for the DB."), line)
            self.assertNotIn("要約", line)
        asserted, _ = _decision_seed("We use PostgreSQL. Maybe Redis later.", "PostgreSQL")
        self.assertTrue(_render_seed_line(asserted).startswith("- 決定: "))


class TestReject(_Fixture):
    ROLE = "decision_reject"

    def judge(self, text):
        return alaya._has_marker(sansho._n(text), alaya.DECISION_REJECT_MARKERS)

    def test_reject_hits(self):
        self.check("hit", True)

    def test_reject_misses(self):
        self.check("miss", False)

    def test_passive_rejected_does_not_promote(self):
        span = _spans("The PR got rejected by the linter.")
        kind = alaya._resolve_kind({"kind": "性向", "content": "x"}, span, ["PR"])[0]
        self.assertEqual(kind, "性向")


if __name__ == "__main__":
    unittest.main()
