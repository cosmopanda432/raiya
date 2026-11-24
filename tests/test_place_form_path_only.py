"""#149 (spec 2026-11-08-place-form-path-only-design §7): 圓成實 の 所在 の形 = user の逐語に
(i) ローカル絶対 path か URL、または (i') 區切りの語 (空白で区切った語が `/` を含み、区画が全て数字だけではない語)。
縁 1 語は 所在 の形にしない (名 ≠ 處)。性向 の形は不変。L (imprint) と EC6/EC2' は同じ関数を読む。
LLM は使わない。テストが消すのは自作 tmp だけ。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import sansho  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, spans, kind, **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    p.update(extra)
    return p


LOCATORS = ["tests/", "docs/x.md", "./a", "and/or", "tests/に置く", "猫が好き/", "猫/犬", "2026/11/08x", "a//b"]
NON_LOCATORS = ["release_meeting.md", "alaya.py", "2026/11/08", "1/2", "/", "//", "", "1/2/3",
                # sansho._n が日本語を貼り付けた形 (日付・分数)
                "2026/11/08に決めた", "比率は1/2", "１/２"]


class TestLocatorToken(unittest.TestCase):
    def test_locator_tokens(self):
        for tok in LOCATORS:
            with self.subTest(tok=tok):
                self.assertTrue(alaya_mod._is_locator_token(tok))

    def test_non_locator_tokens(self):
        for tok in NON_LOCATORS:
            with self.subTest(tok=tok):
                # 呼び出し側 (form_in_user_spans_for_kind) は sansho._n (NFKC) 後の語を渡す
                self.assertFalse(alaya_mod._is_locator_token(sansho._n(tok)))

    def test_non_str_is_false(self):
        for tok in (None, 5, ["a/"]):
            self.assertFalse(alaya_mod._is_locator_token(tok))


class TestFormFunction(unittest.TestCase):
    f = staticmethod(alaya_mod.form_in_user_spans_for_kind)

    def test_place_locator_words_are_form(self):
        for t in ("設定は tests/ に置く", "see docs/x.md", "./a を見て", "and/or の話", "tests/に置く"):
            with self.subTest(t=t):
                self.assertTrue(self.f("所在", [t], []))

    def test_place_condition_alone_is_not_form(self):
        self.assertFalse(self.f("所在", ["release_meeting.md について確認して"], ["release_meeting.md"]))
        self.assertFalse(self.f("所在", ["tests に置く"], ["tests"]))

    def test_place_date_and_fraction_are_not_form(self):
        self.assertFalse(self.f("所在", ["2026/11/08 に決めた"], ["決めた"]))
        self.assertFalse(self.f("所在", ["比率は 1/2"], ["比率"]))

    def test_place_absolute_path_and_url_still_form(self):
        self.assertTrue(self.f("所在", ["見て /home/x/a.py"], []))
        self.assertTrue(self.f("所在", ["https://example.com/a"], []))

    def test_disposition_forms_unchanged(self):
        self.assertTrue(self.f("性向", ["簡潔に書いてほしい"], []))       # 好みの印
        self.assertTrue(self.f("性向", ["やめてください"], []))                # 訂正の印
        self.assertTrue(self.f("性向", ["use sqlite here"], ["SQLite"]))   # 縁 1 語
        self.assertFalse(self.f("性向", ["関係ない文"], ["SQLite"]))

    def test_other_kind_not_applicable(self):
        self.assertTrue(self.f("決定", [], []))


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def place(self, text, conds):
        return self.store.imprint([_turn(text)], _prop("所在の記録", conds, [("user", text)], "所在"))


class TestPlaceFormInImprint(_Case):
    def test_1_locator_word_with_condition_plants(self):
        r = self.place("設定は tests/ に置く", ["tests"])
        self.assertTrue(r["is_new"])
        self.assertEqual(r["seed"]["kind"], "所在")
        self.assertEqual(r["seed"]["sansho"], "圓成實")

    def test_2_condition_only_name_is_recorded_only(self):
        t = "release_meeting.md について確認して"
        r = self.place(t, ["release_meeting.md"])
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, r["reason"])
        self.assertIsNone(r["seed"])
        self.assertEqual(self.store.list_seeds(), [])
        last = self.store.list_imprint_log()[-1]
        self.assertEqual(last["decision"], "reject")
        self.assertIn(alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, last["reason"])

    def test_4_date_and_fraction_are_rejected(self):
        for t, c in (("2026/11/08 に決めた", "決めた"), ("比率は 1/2", "比率")):
            with self.subTest(t=t):
                r = self.place(t, [c])
                self.assertEqual(r["decision"], "reject")
                self.assertIn(alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, r["reason"])
                self.assertEqual(self.store.list_seeds(), [])

    def test_5_locator_words_plant(self):
        cases = (("and/or の話", "話"), ("./a を見て", "見て"), ("docs/x.md を読む", "読む"), ("ここは tests/", "ここ"))
        for t, c in cases:
            with self.subTest(t=t):
                self.setUp()
                r = self.place(t, [c])
                self.assertTrue(r["is_new"], r)

    def test_6_disposition_three_forms_unchanged(self):
        for kind_text, conds in (("簡潔に書いてほしい", ["簡潔"]),       # 好みの印
                                 ("簡潔にやめてください", ["簡潔"]),   # 訂正の印
                                 ("SQLite で行く", ["SQLite"])):          # 縁 1 語
            with self.subTest(t=kind_text):
                self.setUp()
                p = _prop("好み", conds, [("user", kind_text)], "性向")
                r = self.store.imprint([_turn(kind_text)], p)
                self.assertIsNotNone(r["seed"], r)

    def test_7_absolute_path_and_url_still_plant(self):
        t = "設定は /home/dev/raiya/cfg.yaml だよ"
        self.assertTrue(self.place(t, ["設定"])["is_new"])
        self.setUp()
        t = "資料は https://example.com/doc だよ"
        self.assertTrue(self.place(t, ["資料"])["is_new"])

    def test_8_path_only_in_question_still_rejected(self):
        r = self.place("設定は /home/dev/raiya/cfg.yaml だっけ？", ["設定"])
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, r["reason"])

    def test_9_replacement_for_skips_the_check(self):
        first = self.place("設定は tests/ に置く", ["tests"])
        sid = first["seed"]["id"]
        t = "release_meeting.md について確認して"
        r = self.store.imprint([_turn(t)], _prop("所在の記録", ["release_meeting.md"], [("user", t)], "所在"),
                               replacement_for=sid)
        self.assertNotIn(alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, r["reason"])
        self.assertIsNotNone(r["seed"])


class TestEc6NameOnlyUserSpan(_Case):
    """(3) x30 の再演: 名 (release_meeting.md) だけを言う user の span は EC6/EC2' で形を担わない。
    旧: 縁 1 語が 所在 の形になり、assistant の path 復唱と合説した提案が新規に植わっていた。"""

    PATH = "/var/tmp/x/notes/release_meeting.md"

    def test_3_mixed_name_only_user_span_with_echoed_path_is_recorded_only(self):
        first = "{} を読んで教えて。".format(self.PATH)
        r1 = self.store.imprint(
            [_turn(first, ledger_id=5)],
            _prop("次回判定会議の資料", ["release_meeting.md"], [("user", first)], "所在"))
        self.assertTrue(r1["is_new"])
        sid = r1["seed"]["id"]

        user = "次回リリース判定会議 (release_meeting.md) について、分かっていることと参照先を確認して。"
        assistant = "参照先は {} です。".format(self.PATH)
        p = _prop("次回判定会議の資料", ["release_meeting.md"],
                  [("user", user), ("assistant", assistant)], "所在")
        r2 = self.store.imprint([_turn(user, assistant, ledger_id=1)], p, injected_by_turn={0: {sid}})
        self.assertIsNone(r2["seed"], r2)
        self.assertIsNone(r2["is_new"])
        self.assertEqual(r2["echo_match"], sid)
        self.assertEqual(len(self.store.list_seeds()), 1)

    def _echo_setup(self):
        first = "{} を読んで教えて。".format(self.PATH)
        r1 = self.store.imprint(
            [_turn(first, ledger_id=5)],
            _prop("次回判定会議の資料", ["release_meeting.md"], [("user", first)], "所在"))
        return r1["seed"]["id"]

    def _echo_action(self, action):
        sid = self._echo_setup()
        user = "次回リリース判定会議 (release_meeting.md) について、分かっていることと参照先を確認して。"
        assistant = "参照先は {} です。".format(self.PATH)
        p = _prop("次回判定会議の資料", ["release_meeting.md"],
                  [("user", user), ("assistant", assistant)], "所在", action=action, target_id=sid)
        r = self.store.imprint([_turn(user, assistant, ledger_id=1)], p, injected_by_turn={0: {sid}})
        self.assertIsNone(r["seed"], r)
        self.assertIsNone(r["is_new"])
        self.assertEqual(r["echo_match"], sid)
        seeds = self.store.list_seeds()
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["strength"], 1)
        self.assertIsNone(seeds[0]["superseded_by"])

    def test_3c_reinforce_path_is_recorded_only(self):
        self._echo_action("reinforce")

    def test_3d_supersede_path_is_recorded_only(self):
        self._echo_action("supersede")

    def test_3b_same_with_locator_word_in_user_span_plants(self):
        first = "{} を読んで教えて。".format(self.PATH)
        r1 = self.store.imprint(
            [_turn(first, ledger_id=5)],
            _prop("次回判定会議の資料", ["release_meeting.md"], [("user", first)], "所在"))
        sid = r1["seed"]["id"]
        user = "次回リリース判定会議は notes/release_meeting.md にある。"
        assistant = "参照先は {} です。".format(self.PATH)
        p = _prop("次回判定会議の資料", ["release_meeting.md"],
                  [("user", user), ("assistant", assistant)], "所在")
        r2 = self.store.imprint([_turn(user, assistant, ledger_id=1)], p, injected_by_turn={0: {sid}})
        self.assertNotIn("echo_match", r2)
        self.assertTrue(r2["is_new"], r2)


if __name__ == "__main__":
    unittest.main()
