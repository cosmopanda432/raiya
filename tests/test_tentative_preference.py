"""spec 2026-11-07-tentative-preference-design §7 (#121 A'): 決定 の描く逐語が推量 (文末が「かも」系) だけなら、
①③の見出しを「決定(推量)」にして要約を出さない。描画のみ: kind・保存・sansho は不変。
LLM は使わない。テストが消すのは自作 tmp だけ (mkdtemp)。"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, inspect_cli, markers  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.manas import _render_index_line, _render_seed_line  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402

PROJ = "/home/dev/raiya"


def _seed(spans, kind="決定", conditions=("PostgreSQL",), sansho="圓成實", assistant=None, content="PostgreSQL を採用"):
    evidences = [{"source": "user", "span": s, "life": 1, "ledger_id": 3} for s in spans]
    if assistant:
        evidences.append({"source": "assistant", "span": assistant, "life": 1, "ledger_id": 3})
    return {"id": 1, "kind": kind, "content": content, "strength": 1, "sansho": sansho,
            "conditions": list(conditions), "evidences": evidences}


def _shown(seed):
    return [e for e in seed["evidences"] if e["source"] == "user"]


class TestConjectureOnly(unittest.TestCase):
    def lines(self, seed):
        return _render_seed_line(seed), _render_index_line(seed)

    def test_markers_table(self):
        self.assertEqual(markers.MARKERS["ja"]["conjecture"],
                         ("かも", "かもしれない", "かもしれません", "かもね", "かもな"))
        self.assertEqual(markers.MARKERS["en"]["conjecture"], ())
        self.assertEqual(alaya.CONJECTURE_MARKERS, markers.union("conjecture"))
        # R3b (2026-11-26): 英語の推量の印は文中一致の対の役 (文末一致の conjecture の en は () のまま)
        self.assertEqual(markers.MARKERS["ja"]["conjecture_in_sentence"], ())
        self.assertEqual(markers.MARKERS["en"]["conjecture_in_sentence"], ("maybe", "perhaps", "probably"))
        self.assertEqual(alaya.CONJECTURE_IN_SENTENCE_MARKERS, markers.union("conjecture_in_sentence"))

    def test_n1_is_conjecture_in_both_circles(self):
        s = _seed(["DB は PostgreSQL がいいかも"])
        self.assertTrue(alaya.conjecture_only(s, _shown(s)))
        self.assertEqual(self.lines(s), (
            "- 決定(推量): DB は PostgreSQL がいいかも(強度1・往復 3)",
            "- 決定(推量): DB は PostgreSQL がいいかも (強度1・往復 3)"))
        for line in self.lines(s):
            self.assertNotIn("要約", line)
            self.assertNotIn("PostgreSQL を採用", line)

    def test_assertion_sentence_with_condition_keeps_decision(self):
        s = _seed(["DB は PostgreSQL にした。キャッシュは Redis がいいかも"])
        self.assertFalse(alaya.conjecture_only(s, _shown(s)))
        self.assertEqual(self.lines(s)[0],
                         "- 決定: DB は PostgreSQL にした。キャッシュは Redis がいいかも(強度1・要約: PostgreSQL を採用・往復 3)")

    def test_condition_only_in_conjecture_sentence_with_condition_free_assertion(self):
        s = _seed(["今日は晴れた。DB は PostgreSQL がいいかも"])
        self.assertTrue(alaya.conjecture_only(s, _shown(s)))
        self.assertIn("決定(推量): ", self.lines(s)[0])
        self.assertIn("決定(推量): ", self.lines(s)[1])

    def test_mid_word_kamo_is_not_a_mark(self):
        for text in ("SQLite とかもあるけど、Postgres にした。", "かもめが PostgreSQL を見た"):
            s = _seed([text], conditions=("Postgres", "PostgreSQL"))
            self.assertFalse(alaya.conjecture_only(s, _shown(s)), text)
            self.assertTrue(self.lines(s)[0].startswith("- 決定: "), text)

    def test_every_word_form_with_trailing_punctuation(self):
        for word in ("かもしれない", "かもしれません", "かもね", "かもな", "かも"):
            for tail in ("", "。", "！", "."):
                s = _seed(["DB は PostgreSQL がいい" + word + tail])
                self.assertTrue(alaya.conjecture_only(s, _shown(s)), (word, tail))
                self.assertTrue(self.lines(s)[0].startswith("- 決定(推量): "), (word, tail))

    def test_question_sentence_is_not_counted(self):
        s = _seed(["PostgreSQL がいいかも？"])
        self.assertFalse(alaya.conjecture_only(s, _shown(s)))
        self.assertTrue(self.lines(s)[0].startswith("- 決定: "))

    def test_question_sentence_tag_kept_with_conjecture(self):
        s = _seed(["DB は PostgreSQL がいいかも。Celery は？"], conditions=("PostgreSQL", "Celery"))
        self.assertTrue(alaya.conjecture_only(s, _shown(s)))
        self.assertEqual(self.lines(s)[0],
                         "- 決定(推量): DB は PostgreSQL がいいかも。Celery は？(強度1・一部問い: Celery・往復 3)")
        self.assertEqual(self.lines(s)[1],
                         "- 決定(推量): DB は PostgreSQL がいいかも。Celery は？ (強度1・一部問い: Celery・往復 3)")

    def test_other_kinds_unchanged(self):
        for kind in ("性向", "所在", "回避策", "再開点"):
            s = _seed(["DB は PostgreSQL がいいかも"], kind=kind, content="x")
            self.assertFalse(alaya.conjecture_only(s, _shown(s)), kind)
            for line in self.lines(s):
                self.assertNotIn("推量", line, kind)

    def test_mixed_assistant_decision_gets_partial_tag(self):
        s = _seed(["DB は PostgreSQL がいいかも"], sansho="依他起", assistant="PostgreSQL を使います")
        self.assertEqual(self.lines(s), (
            "- 決定(推量): DB は PostgreSQL がいいかも(強度1・一部推定・往復 3)",
            "- 決定(推量): DB は PostgreSQL がいいかも (強度1・一部推定・往復 3)"))

    def test_non_dict_and_no_shown_are_false(self):
        self.assertFalse(alaya.conjecture_only(None, []))
        self.assertFalse(alaya.conjecture_only(_seed(["x かも"]), []))


class TestEngineRecall(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".raiya"))
        patcher = mock.patch.dict(os.environ, {"HOME": home, "RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.addCleanup(patcher.stop)
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def test_storage_unchanged_and_explain_equals_recall(self):
        text = "MySQL ではなく PostgreSQL がいいかも"
        turn = LedgerTurn(user_text=text, assistant_text="了解しました。", id=0, project=PROJ)
        prop = {"content": "PostgreSQL を採用", "conditions": ["PostgreSQL"], "polarity": "無記", "kind": "決定",
                "evidence": [{"source": "user", "span": text}]}
        res = self.eng.imprint([turn], [prop])[0]
        self.assertEqual((res["seed"]["kind"], res["seed"]["sansho"]), ("決定", "圓成實"), res)
        before = self.eng.inspect_seeds()
        for prompt in ("PostgreSQL について", "全く別の話題"):
            recalled = self.eng.recall(prompt, project=PROJ)
            lines = [ln for ln in recalled["narrative"].split("\n") if ln.startswith("- ")]
            self.assertEqual(len(lines), 1, lines)
            self.assertTrue(lines[0].startswith("- 決定(推量): " + text), lines)
            self.assertNotIn("要約", lines[0])
            self.assertEqual(self.eng.explain_recall(prompt, project=PROJ)["narrative"], recalled["narrative"])
        after = self.eng.inspect_seeds()
        self.assertEqual((after[0]["kind"], after[0]["content"]), ("決定", "PostgreSQL を採用"))
        self.assertEqual(set(after[0]), set(before[0]))
        self.assertEqual(after[0]["evidences"], before[0]["evidences"])


class TestQaAdditions(unittest.TestCase):
    def lines(self, seed):
        return _render_seed_line(seed), _render_index_line(seed)

    def ev(self, *pairs):
        return [{"source": "user", "span": sp, "life": 1, "ledger_id": lid} for sp, lid in pairs]

    def test_kamo_then_assertion_stays_decision(self):
        s = _seed(["かもしれないけど PostgreSQL にした"])
        self.assertFalse(alaya.conjecture_only(s, _shown(s)))
        self.assertEqual(self.lines(s)[0], "- 決定: かもしれないけど PostgreSQL にした(強度1・要約: PostgreSQL を採用・往復 3)")

    def test_old_assertion_not_rendered_stays_conjecture_without_summary(self):
        s = _seed([])
        s["evidences"] = self.ev(("PostgreSQL にした", 2), ("PostgreSQL がいいかも", 3))
        self.assertEqual(self.lines(s), (
            "- 決定(推量): PostgreSQL がいいかも(強度1・往復 3)",
            "- 決定(推量): PostgreSQL がいいかも (強度1・往復 3)"))

    def test_keep_summary_when_old_assertion_would_vanish(self):
        s = _seed([], conditions=["Redis", "PostgreSQL", "Celery"], content="Redis を採用")
        s["evidences"] = self.ev(("キャッシュは Redis にした", 2), ("PostgreSQL がいいかも。Celery は？", 3))
        self.assertEqual(self.lines(s), (
            "- 決定(推量): PostgreSQL がいいかも。Celery は？(強度1・要約: Redis を採用・一部問い: Celery・往復 3)",
            "- 決定(推量): PostgreSQL がいいかも。Celery は？ (強度1・要約: Redis を採用・一部問い: Celery・往復 3)"))

    def test_empty_or_missing_conditions_are_true(self):
        for conds in ([], None):
            s = _seed(["DB は PostgreSQL がいいかも"])
            s["conditions"] = conds
            self.assertTrue(alaya.conjecture_only(s, _shown(s)), conds)

    def test_empty_or_none_span_falls_back(self):
        for span in ("", None):
            s = _seed(["x"])
            s["evidences"][0]["span"] = span
            self.assertFalse(alaya.conjecture_only(s, _shown(s)))
            for line in self.lines(s):
                self.assertNotIn("推量", line)

    def test_legacy_seed_without_sansho(self):
        s = _seed(["DB は PostgreSQL がいいかも"])
        del s["sansho"]
        self.assertEqual(_render_seed_line(s), "- 決定: PostgreSQL を採用(強度1・往復 3)")

    def test_whitespace_and_newlines(self):
        for text, want in (("DB は PostgreSQL がいいかも\u3000", True), ("DB は\nPostgreSQL がいいかも\n", True),
                           ("PostgreSQL\nがいいかも", False)):
            s = _seed([text])
            self.assertEqual(alaya.conjecture_only(s, _shown(s)), want, repr(text))

    def test_english_maybe_is_conjecture(self):
        # 推量の印 `maybe` (conjecture_in_sentence・2026-11-26) が文中で当たる。`might` は印にしない
        s = _seed(["Maybe PostgreSQL might be better"])
        self.assertTrue(alaya.conjecture_only(s, _shown(s)))
        s = _seed(["PostgreSQL might be better"])
        self.assertFalse(alaya.conjecture_only(s, _shown(s)))


class TestInspectCli(unittest.TestCase):
    def test_show_text_and_json(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".raiya"))
        patcher = mock.patch.dict(os.environ, {"HOME": home, "RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.addCleanup(patcher.stop)
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        seed = _seed(["DB は PostgreSQL がいいかも"])
        seed["project"] = PROJ
        write_state(state, {"seeds": [seed], "alaya_meta": {"life": 1}})

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = inspect_cli.main(["--state-dir", state] + list(argv))
            return code, out.getvalue()

        code, text = run("show", "--all")
        self.assertEqual(code, 0)
        self.assertIn("- 決定(推量): ", text)
        code, js = run("--json", "show", "--all")
        data = json.loads(js)
        seeds = data["seeds"] if isinstance(data, dict) else data
        self.assertEqual((seeds[0]["kind"], seeds[0]["content"]), ("決定", "PostgreSQL を採用"))


if __name__ == "__main__":
    unittest.main()
