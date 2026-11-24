"""spec 2026-10-30-question-as-evidence §7 L: 圓成實 の 所在・性向 は形を担う user の span が無ければ記録のみ。
§9 L': 4 つの形の判定は疑の文を除いた残りに対して行う (問いの中にしか無い縁・path は形にならない)。
LLM は使わない。テストが消すのは自作 tmp だけ。"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
Q = "前に決めた DB はどれだった？"
A = "SQLite を使うと決めました"


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, spans, kind, **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    p.update(extra)
    return p


class TestFormInUserSpansPure(unittest.TestCase):
    f = staticmethod(alaya_mod.form_in_user_spans_for_kind)

    def test_place(self):
        self.assertFalse(self.f("所在", [Q], []))
        # #149: 縁 1 語は 所在 の形にしない (名 ≠ 處)。形は 絶対 path / URL / 區切りの語 (`/` を含む語)。
        self.assertFalse(self.f("所在", ["config.yaml だよ"], ["config.yaml"]))
        self.assertTrue(self.f("所在", ["config/ に置く"], ["config"]))
        self.assertTrue(self.f("所在", ["見て /home/x/a.py"], []))
        self.assertTrue(self.f("所在", ["https://example.com/a"], []))

    def test_disposition_marker_and_conditions(self):
        self.assertFalse(self.f("性向", [Q], ["SQLite"]))
        self.assertTrue(self.f("性向", ["簡潔に書いてほしい"], []))
        self.assertFalse(self.f("性向", [Q], []))

    def test_conditions_case_and_space_insensitive(self):
        self.assertTrue(self.f("性向", ["use  sqlite  here"], ["SQLite"]))
        self.assertTrue(self.f("性向", ["SQLITE 版"], ["sqlite"]))

    def test_other_kind_not_applicable(self):
        self.assertTrue(self.f("決定", [], []))

    def test_non_sequence_inputs_are_empty(self):
        self.assertFalse(self.f("性向", ["xyz s"], "SQLite"))
        self.assertFalse(self.f("所在", "/a/b", []))


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def assert_rejected(self, r, const):
        self.assertEqual(r["decision"], "reject")
        self.assertIn(const, r["reason"])
        self.assertIsNone(r["seed"])
        self.assertEqual(self.store.list_seeds(), [])
        last = self.store.list_imprint_log()[-1]
        self.assertEqual(last["decision"], "reject")
        self.assertIn(const, last["reason"])


class TestL(_Case):
    def test_1_disposition_question_only_rejected(self):
        w = [_turn(Q, A)]
        p = _prop("DB は SQLite", ["SQLite"], [("user", Q)], "性向")
        r = self.store.imprint(w, p)
        self.assert_rejected(r, alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN)

    def test_2_condition_only_in_question_rejected(self):
        # spec §9 L': 縁が疑の文の中にしか無い user の span は形を担わない
        w = [_turn("前に決めた SQLite はどれだった？", A)]
        p = _prop("DB は SQLite", ["SQLite"], [("user", "前に決めた SQLite はどれだった？")], "性向")
        r = self.store.imprint(w, p)
        self.assert_rejected(r, alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN)

    def test_2b_condition_outside_question_planted(self):
        t = "SQLite で行く？ やはり SQLite で行く。"
        w = [_turn(t, A)]
        p = _prop("DB は SQLite", ["SQLite"], [("user", t)], "性向")
        self.assertTrue(self.store.imprint(w, p)["is_new"])   # KP で 決定 に上がり得るので kind は見ない

    def test_2c_place_path_only_in_question_rejected(self):
        t = "設定は /home/dev/raiya/cfg.yaml だっけ？"
        w = [_turn(t, "")]
        p = _prop("設定は /home/dev/raiya/cfg.yaml", ["cfg"], [("user", t)], "所在")
        r = self.store.imprint(w, p)
        self.assert_rejected(r, alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN)

    def test_2d_preference_marker_only_in_question_rejected(self):
        t = "簡潔に書いてほしい？"
        r = self.store.imprint([_turn(t, "了解 簡潔")], _prop("簡潔に", ["簡潔"], [("user", t)], "性向"))
        self.assert_rejected(r, alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN)

    def test_2e_multiline_question_then_assertion_planted(self):
        t = "SQLite でいい？\nやはり SQLite で行く"
        r = self.store.imprint([_turn(t, "")], _prop("DB は SQLite", ["SQLite"], [("user", t)], "性向"))
        self.assertTrue(r["is_new"])   # kind は見ない (KP が 決定 に上げ得る)

    def test_2e_multiline_assertion_then_question_rejected(self):
        t = "やはり SQLite で行く？\n前の DB はどれ？"
        r = self.store.imprint([_turn(t, "")], _prop("DB は SQLite", ["SQLite"], [("user", t)], "性向"))
        self.assert_rejected(r, alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN)

    def test_2f_place_path_in_assertion_planted(self):
        t = "設定は /home/dev/raiya/cfg.yaml だよ"
        r = self.store.imprint([_turn(t, "")], _prop("設定", ["設定"], [("user", t)], "所在"))
        self.assertTrue(r["is_new"])

    def test_2g_reinforce_with_question_only_span_record_only(self):
        t0 = "DB は SQLite にしてほしい"
        r0 = self.store.imprint([_turn(t0, "", 0)], _prop("DB は SQLite", ["SQLite"], [("user", t0)], "性向"))
        self.assertTrue(r0["is_new"])
        sid = r0["seed"]["id"]
        before = copy.deepcopy(self.store.list_seeds())
        q = "前に決めた SQLite はどれだった？"
        p = _prop("DB は SQLite", ["SQLite"], [("user", q)], "性向", action="reinforce", target_id=sid)
        r = self.store.imprint([_turn(q, "", 1)], p)
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN, r["reason"])
        self.assertIsNone(r["seed"])
        self.assertEqual(self.store.list_seeds(), before)   # 標的の種子は evidence・縁・強度とも不変
        self.assertEqual(self.store.list_imprint_log()[-1]["decision"], "reject")

    def test_3_marker_without_conditions_planted(self):
        w = [_turn("簡潔に書いてほしい", "了解 要約")]
        p = _prop("簡潔に", ["要約"], [("user", "簡潔に書いてほしい")], "性向")
        self.assertTrue(self.store.imprint(w, p)["is_new"])

    def test_3b_correction_marker_without_user_condition_planted(self):
        w = [_turn("それは違う、元に戻してください", "デプロイ手順です")]
        p = _prop("戻す", ["デプロイ"], [("user", "それは違う、元に戻してください")], "性向")
        self.assertTrue(self.store.imprint(w, p)["is_new"])

    def test_4_place_without_path_rejected(self):
        w = [_turn("設定ファイルはどこだった？", "設定は /home/dev/raiya/cfg.yaml です")]
        p = _prop("設定は /home/dev/raiya/cfg.yaml", ["cfg"], [("user", "設定ファイルはどこだった？")], "所在")
        r = self.store.imprint(w, p)
        self.assert_rejected(r, alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN)

    def test_4b_place_with_condition_only_in_user_span_is_recorded_only(self):
        # #149 (旧: 縁が user の span にあれば path 無しでも植わった): 縁 1 語は 所在 の形にしない。
        w = [_turn("設定ファイルは config.yaml だよ", "")]
        p = _prop("設定ファイル", ["config.yaml"], [("user", "設定ファイルは config.yaml だよ")], "所在")
        r = self.store.imprint(w, p)
        self.assertIsNone(r["seed"])
        self.assertIn(alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, r["reason"])

    def test_4c_place_with_locator_word_in_user_span_planted_without_absolute_path(self):
        w = [_turn("設定ファイルは config/ に置く", "")]
        p = _prop("設定ファイル", ["config"], [("user", "設定ファイルは config/ に置く")], "所在")
        self.assertTrue(self.store.imprint(w, p)["is_new"])

    def test_5_place_with_path_or_url_planted(self):
        w = [_turn("設定は /home/dev/raiya/cfg.yaml だよ", "")]
        p = _prop("設定", ["設定"], [("user", "設定は /home/dev/raiya/cfg.yaml だよ")], "所在")
        self.assertTrue(self.store.imprint(w, p)["is_new"])
        w = [_turn("資料は https://example.com/doc だよ", "")]
        p = _prop("資料", ["資料"], [("user", "資料は https://example.com/doc だよ")], "所在")
        self.assertTrue(self.store.imprint(w, p)["is_new"])

    def test_6_etaki_unchanged(self):
        w = [_turn(Q, A)]
        p = _prop("DB は SQLite", ["SQLite"], [("user", Q), ("assistant", A)], "性向")
        r = self.store.imprint(w, p)
        self.assertEqual(r["seed"]["sansho"], "依他起")
        w = [_turn("設定は？", "設定は /home/dev/raiya/cfg.yaml です", ledger_id=1)]
        p = _prop("設定は /home/dev/raiya/cfg.yaml", ["設定"], [("user", "設定は？"), ("assistant", "設定は /home/dev/raiya/cfg.yaml です")], "所在")
        self.assertEqual(self.store.imprint(w, p)["seed"]["sansho"], "依他起")

    def _no_l(self, r):
        for c in (alaya_mod.PLACE_NEEDS_FORM_IN_USER_SPAN, alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN):
            self.assertNotIn(c, r["reason"])

    def test_7a_decision_with_reject_marker_not_gated_by_l(self):
        w = [_turn("MySQL ではなく PG にする", "DB はそれで")]
        p = _prop("MySQL ではなく PG を採る", ["DB"], [("user", "MySQL ではなく PG にする")], "決定")
        r = self.store.imprint(w, p)
        self.assertEqual(r["kind"], "決定")
        self.assertTrue(r["is_new"])
        self._no_l(r)

    def test_7b_workaround_with_pair_not_gated_by_l(self):
        y = "それは違う、元に戻してください"
        w = [_turn("デプロイを手で流した。" + y, "順序の問題です")]
        p = _prop("手で流す → 壊れた → 戻す", ["順序"], [("user", "デプロイを手で流した"), ("user", y)], "回避策")
        r = self.store.imprint(w, p)
        self.assertEqual(r["kind"], "回避策")
        self.assertTrue(r["is_new"])
        self._no_l(r)

    def test_7c_decision_without_marker_demotes_then_l_rejects(self):
        w = [_turn("どれにする？", "SQLite で")]
        p = _prop("SQLite採用", ["SQLite"], [("user", "どれにする？")], "決定")
        r = self.store.imprint(w, p)
        self.assertEqual(r["kind"], "性向")
        self.assertEqual(r["decision"], "reject")
        self.assertIn("性向 として受理", r["reason"])  # _resolve_kind (H) の注記が先
        self.assertIn(alaya_mod.DISPOSITION_NEEDS_FORM_IN_USER_SPAN, r["reason"])
        self.assertEqual(self.store.list_seeds(), [])

    def test_7d_resume_point_is_gated_by_rp2b_not_l_and_passes_on_form(self):
        # 再開点 は L (所在・性向) ではなく RP2b (#115・test_resume_point_form_rp) で検査される。縁 裁定 が問いでない文にあるので植わる
        w = [_turn("次 = 裁定待ち 裁定", "", ledger_id=2)]
        r = self.store.imprint(w, _prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点"))
        self.assertTrue(r["is_new"])

    def test_8_replacement_for_skips(self):
        w = [_turn(Q, A)]
        p = _prop("DB は SQLite", ["SQLite"], [("user", Q)], "性向")
        r = self.store.imprint(w, p, replacement_for=99)
        self.assertTrue(r["is_new"])

    def test_9_no_conditions(self):
        # 縁 0 語の提案は ゲートが先に却下する (L の前)。縁が assistant 側だけ = 印のみで判定される。
        w = [_turn(Q, A)]
        r = self.store.imprint(w, _prop("DB", [], [("user", Q)], "性向"))
        self.assertEqual(r["decision"], "reject")
        self.assertIsNone(r["seed"])
        w = [_turn("簡潔に書いてほしい", "了解 要約")]
        self.assertTrue(self.store.imprint(w, _prop("簡潔に", ["要約"], [("user", "簡潔に書いてほしい")], "性向"))["is_new"])

    def test_10_condition_case_space(self):
        w = [_turn("use  sqlite  here", "SQLite OK")]
        p = _prop("DB", ["SQLite"], [("user", "use  sqlite  here")], "性向")
        self.assertTrue(self.store.imprint(w, p)["is_new"])


if __name__ == "__main__":
    unittest.main()
