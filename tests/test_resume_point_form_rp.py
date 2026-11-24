"""spec 2026-10-31-resume-point-and-kind-name-design §5.1/§7 (#115, RP2b): 圓成實 の 再開点 は user の span の疑の文を除いた
残りに縁が 1 語以上無ければ記録のみ。人の置き換え・依他起は不変。LLM は使わない。テストが消すのは自作 tmp だけ。"""
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
RP = alaya_mod.RESUME_NEEDS_FORM_IN_USER_SPAN


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, spans, kind="再開点", **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    p.update(extra)
    return p


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def assert_rejected(self, r, seeds_before=0):
        self.assertEqual(r["decision"], "reject")
        self.assertIn(RP, r["reason"])
        self.assertIsNone(r["seed"])
        self.assertEqual(len(self.store.list_seeds()), seeds_before)
        last = self.store.list_imprint_log()[-1]
        self.assertEqual(last["decision"], "reject")
        self.assertIn(RP, last["reason"])


class TestRp2b(_Case):
    def test_x19_question_only_rejected(self):
        t = "前に決めた DB はどれだった？"
        r = self.store.imprint([_turn(t)], _prop("DB を確認する", ["DB"], [("user", t)]))
        self.assert_rejected(r)

    def test_x19_two_questions_rejected(self):
        t = "認証・キャッシュ・ログはどうする？ Which database did we choose?"
        r = self.store.imprint([_turn(t)], _prop("認証を確認", ["認証", "database"], [("user", t)]))
        self.assert_rejected(r)

    def test_x19_request_without_condition_in_span_rejected(self):
        t = "前に決めたことを全部確認して。"
        # 縁 決定 は assistant の本文にだけあり (ゲートは通る)、user の span には無い
        r = self.store.imprint([_turn(t, "決定一覧を出します")], _prop("決定の確認", ["決定"], [("user", t)]))
        self.assertEqual(r["grounded_conditions"], ["決定"])
        self.assert_rejected(r)

    def test_assertive_resume_plants(self):
        t = "次 = 裁定待ち"
        r = self.store.imprint([_turn(t)], _prop("次 = 裁定待ち", ["裁定"], [("user", t)]))
        self.assertTrue(r["is_new"])

    def test_mixed_question_and_statement_plants(self):
        t = "次は認証をどうする？ まず JWT の設定を終わらせる。"
        r = self.store.imprint([_turn(t)], _prop("JWT の設定を終わらせる", ["JWT"], [("user", t)]))
        self.assertTrue(r["is_new"])

    def test_two_user_spans_one_question_one_assertive_plants(self):
        q, a = "前に決めた DB はどれだった？", "次は JWT の設定を終わらせる"
        w = [_turn(q + " " + a)]
        r = self.store.imprint(w, _prop("JWT の設定", ["DB", "JWT"], [("user", q), ("user", a)]))
        self.assertTrue(r["is_new"])

    def test_preference_marker_does_not_block(self):
        t = "次は JWT の設定を終わらせたいので、続きはそこから。"
        r = self.store.imprint([_turn(t)], _prop("JWT の設定", ["JWT"], [("user", t)]))
        self.assertTrue(r["is_new"])

    def test_replacement_for_with_question_plants(self):
        t = "前に決めた DB はどれだった？"
        r = self.store.imprint([_turn(t)], _prop("DB を確認する", ["DB"], [("user", t)]), replacement_for=99)
        self.assertTrue(r["is_new"])

    def test_assistant_span_new_resume_is_still_rejected_by_b_not_rp2b(self):
        r = self.store.imprint([_turn("お願い", "次は JWT を設定します")], _prop("JWT", ["JWT"], [("assistant", "次は JWT を設定します")]))
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya_mod.RESUME_NEW_ENJOUJITSU_ONLY, r["reason"])
        self.assertNotIn(RP, r["reason"])

    def test_assistant_supersede_of_existing_resume_point_unchanged(self):
        t = "次 = 裁定待ち"
        first = self.store.imprint([_turn(t)], _prop("次 = 裁定待ち", ["裁定"], [("user", t)]))
        sid = first["seed"]["id"]
        w = [_turn("お願い", "次は JWT を設定します", ledger_id=1)]
        r = self.store.imprint(w, _prop("JWT を設定", ["JWT"], [("assistant", "次は JWT を設定します")]))
        self.assertTrue(r.get("superseded"))
        self.assertEqual(r["seed"]["id"], sid)
        self.assertNotIn(RP, r["reason"])

    def test_enjoujitsu_question_only_supersede_rejected_and_existing_untouched(self):
        t = "次 = 裁定待ち"
        first = self.store.imprint([_turn(t)], _prop("次 = 裁定待ち", ["裁定"], [("user", t)]))
        q = "前に決めた DB はどれだった？"
        r = self.store.imprint([_turn(q, ledger_id=1)], _prop("DB を確認", ["DB"], [("user", q)], action="supersede",
                                                              target_id=first["seed"]["id"]))
        self.assert_rejected(r, seeds_before=1)
        self.assertEqual(self.store.list_seeds()[0]["content"], "次 = 裁定待ち")

    def test_other_kinds_are_not_affected(self):
        t = "前に決めた DB はどれだった？"
        # 所在 は L の枠 (縁が span にあれば形あり) のまま。RP2b の理由は付かない
        r = self.store.imprint([_turn(t)], _prop("DB", ["DB"], [("user", t)], kind="所在"))
        self.assertNotIn(RP, r["reason"])


class TestPlantPrecheckDoesNotMirror(_Case):
    def test_question_only_resume_passes_precheck_because_human_path_is_exempt(self):
        t = "前に決めた DB はどれだった？"
        w = [_turn(t)]
        p = _prop("DB を確認する", ["DB"], [("user", t)])
        self.assertIsNone(alaya_mod.plant_precheck(w, p, seeds=[]))
        self.assertTrue(self.store.imprint(w, p, replacement_for=99)["is_new"])


if __name__ == "__main__":
    unittest.main()
