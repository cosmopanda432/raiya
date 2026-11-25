"""① の行の縮め (設計書 2026-11-25-recall-capacity-design §6.4・T3 6b): 入らない行は要約なし → 名だけの順に試し、逐語は切らない。

テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。LLM は使わない。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import manas as manas_mod  # noqa: E402
from raiya.manas import (  # noqa: E402
    GERMINATED_HEADER,
    NARRATIVE_HEADER,
    _index_header,
    _render_entry_line,
    _render_seed_line,
    _seed_line_forms,
)
from test_layered_circles import _seed as _plain_seed, _StateCase  # noqa: E402
from tests._state_fixture import manas_store_at  # noqa: E402


def _s(seed_id=1, span="Kafka を使う", content="キューは Kafka", strength=1, ledger_id=7, **extra):
    seed = {"id": seed_id, "kind": "決定", "sansho": "圓成實", "strength": strength, "conditions": ["kafka"],
            "content": content, "evidences": [{"source": "user", "span": span, "life": 1, "ledger_id": ledger_id}]}
    seed.update(extra)
    return seed


def _empty():
    return {"no_summary": [], "name_only": []}


def _expected(*lines):
    return "\n\n".join([NARRATIVE_HEADER, "\n".join([GERMINATED_HEADER] + list(lines))])


class TestForms(unittest.TestCase):
    def test_three_forms_of_a_plain_line(self):
        self.assertEqual(_seed_line_forms(_s()), (
            "- 決定: Kafka を使う(強度1・要約: キューは Kafka・往復 7)",
            "- 決定: Kafka を使う(強度1・往復 7)",
            "- 決定: #1 (逐語 9 字・容量で省略・強度1・往復 7)"))

    def test_first_form_equals_render_seed_line(self):
        seeds = [_s(), _s(2, span="これでいいかな？", content="c"), _s(3, span="u", assistant=None),
                 _s(4, sansho="依他起", evidences=[{"source": "assistant", "span": "/home/x/a.py", "ledger_id": 3}]),
                 {"id": 5, "kind": "決定", "content": "c", "strength": 1, "evidences": []},
                 _s(6, sansho="依他起", kind="所在", content="/home/x/a.py を見る",
                    evidences=[{"source": "user", "span": "u1", "ledger_id": 1}, {"source": "assistant", "span": "a1", "ledger_id": 2}])]
        for seed in seeds:
            self.assertEqual(_seed_line_forms(seed)[0], _render_seed_line(seed))

    def _keep_seed(self):
        return _s(span="これは緊急集会の扱いで別じゃね？", content="緊急集会を別に扱う", conditions=["アマテラス", "不在", "緊急集会"],
                  evidences=[{"source": "user", "span": "だってアマテラスが不在はシステムの危機だし", "life": 1, "ledger_id": 1},
                             {"source": "user", "span": "これは緊急集会の扱いで別じゃね？", "life": 1, "ledger_id": 2}])

    def test_keep_summary_lines_have_no_second_form(self):
        forms = _seed_line_forms(self._keep_seed())
        self.assertIn("要約:", forms[0])
        self.assertIn("一部問い:", forms[0])
        self.assertIsNone(forms[1])
        self.assertIn("一部問い: ", forms[2])
        self.assertNotIn("要約", forms[2])

    def test_lines_without_summary_have_no_second_form(self):
        mixed = _s(sansho="依他起", kind="所在", content="/home/x/a.py を見る",
                   evidences=[{"source": "user", "span": "u1", "ledger_id": 1}, {"source": "assistant", "span": "a1", "ledger_id": 2}])
        guess = _s(2, sansho="依他起", kind="所在", content="/home/x/a.py を見る", strength=2,
                   evidences=[{"source": "assistant", "span": "a1", "ledger_id": 3}])
        legacy = {"id": 3, "kind": "決定", "content": "c", "strength": 1, "evidences": []}
        for seed in (mixed, guess, legacy):
            self.assertIsNone(_seed_line_forms(seed)[1])
        self.assertEqual(_seed_line_forms(guess)[2], "- 所在(推定): #2 (内容 12 字・容量で省略・強度2・往復 3)")

    def test_judges_doubt_once(self):
        with mock.patch.object(manas_mod, "doubt_judgement", wraps=manas_mod.doubt_judgement) as spy:
            _seed_line_forms(self._keep_seed())
        self.assertEqual(spy.call_count, 1)


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_shortened_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = manas_store_at(os.path.join(self.tmpdir, "manas.json"))

    def test_nothing_shortened_when_it_fits(self):
        out = self.store.compose_injection([_s()])
        self.assertEqual(out["shortened"], _empty())
        self.assertIn(_seed_line_forms(_s())[0], out["text"])

    def test_no_summary_when_full_line_does_not_fit(self):
        seed = _s(content="要約" * 200)
        want = _expected(_seed_line_forms(seed)[1])
        out = self.store.compose_injection([seed], max_chars=len(want))
        self.assertEqual(out["text"], want)
        self.assertEqual(out["shortened"], {"no_summary": [1], "name_only": []})
        self.assertEqual(out["cut"]["germinated"], 0)

    def test_name_only_when_second_form_does_not_fit(self):
        seed = _s(span="あ" * 2000)
        want = _expected(_seed_line_forms(seed)[2])
        out = self.store.compose_injection([seed], max_chars=len(want))
        self.assertEqual(out["text"], want)
        self.assertEqual(out["shortened"], {"no_summary": [], "name_only": [1]})
        self.assertEqual(out["cut"]["germinated"], 0)

    def test_short_line_is_cut_not_renamed(self):
        fired = [_plain_seed(1, ["a"], strength=3, content="発芽その一" * 4), _plain_seed(2, ["a"], strength=2, content="発芽その二" * 4)]
        full = self.store.compose_injection(fired)
        out = self.store.compose_injection(fired, max_chars=len(full["text"]) - 1)
        self.assertEqual(out["cut"]["germinated"], 1)
        self.assertEqual(out["shortened"], _empty())

    def test_each_line_independently_and_outer_circles_follow(self):
        a = _s(1, span="あ" * 2000)
        b = _s(2, span="短い", content="短")
        item = {"seed": _plain_seed(30, ["x"], content="索引の項目"), "group": None}
        want = "\n\n".join([NARRATIVE_HEADER,
                            "\n".join([GERMINATED_HEADER, _seed_line_forms(a)[2], _seed_line_forms(b)[0]]),
                            _index_header(1, 1) + "\n" + _render_entry_line(item)])
        out = self.store.compose_injection([a, b], [item], 1, max_chars=len(want))
        self.assertEqual(out["text"], want)
        self.assertEqual(out["cut"], {"germinated": 0, "siblings": 0, "index": 0, "global": 0})
        self.assertEqual(out["shortened"], {"no_summary": [], "name_only": [1]})

    def test_doubt_is_judged_once_per_seed_even_when_the_line_is_shortened(self):
        # 縮めのために (2)(3) を組んでも、札の判定は 1 種子 1 回 (test_doubt_in_decision_summary の「行ごとに 1 度」の compose 版)
        seed = TestForms()._keep_seed()
        seed["content"] = "緊急集会を別に扱う" * 100   # (1) は入らない
        want = _expected(_seed_line_forms(seed)[2])
        with mock.patch.object(manas_mod, "doubt_judgement", wraps=manas_mod.doubt_judgement) as spy:
            out = self.store.compose_injection([seed], max_chars=len(want))
        self.assertEqual(out["shortened"], {"no_summary": [], "name_only": [1]})
        self.assertEqual(out["text"], want)
        self.assertEqual(spy.call_count, 1)

    def test_nothing_fits_still_cuts(self):
        out = self.store.compose_injection([_s()], max_chars=len(NARRATIVE_HEADER))
        self.assertEqual(out["cut"]["germinated"], 1)
        self.assertEqual(out["shortened"], _empty())


class TestRecall(_StateCase):
    def test_circles_and_log_carry_shortened(self):
        seed = _plain_seed(1, ["kafka"], kind="決定", project=None,
                           evidences=[{"source": "user", "span": "あ" * 4000, "ledger_id": 1, "life": 1}])
        eng = self.write([seed])
        r = eng.recall("kafka", project=None)
        self.assertEqual(r["circles"]["shortened"]["name_only"], [1])
        self.assertEqual(eng.injection_log(1)[-1]["shortened"], r["circles"]["shortened"])
        self.assertEqual(eng.explain_recall("kafka", project=None)["circles"], r["circles"])


if __name__ == "__main__":
    unittest.main()
