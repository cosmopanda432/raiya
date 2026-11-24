"""spec 2026-11-04-resume-hearsay-design §5.2/§7 (#129, RH2): assistant の span だけで伝聞の印を持つ 再開点 の提案は、
既存の 再開点 を置き換えず記録のみ。LLM は使わない。テストが消すのは自作 tmp だけ。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import markers  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
RH = alaya_mod.RESUME_HEARSAY_RECORD_ONLY
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "hearsay_markers_20261104.json")

USER1 = "今日はここで中断です。次は決済モジュールのリトライ処理から再開します。"
USER1_SPAN = "次は決済モジュールのリトライ処理から再開します。"
HEARSAY = ("次回は決済モジュールのリトライ処理から再開する予定だと伝えました。"
           "再開時には、ファイルや Git の状態を確認してから進めることが記録されています。")
DOER = "次回はまずファイルと Git を確認してから決済モジュールのリトライ処理を再開します。"
CONDS = ["決済モジュール", "リトライ処理"]


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, spans, kind="再開点", **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    p.update(extra)
    return p


class TestHearsayTable(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            self.fixture = json.load(f)

    def test_tables(self):
        self.assertIn("hearsay", markers.ROLES)
        self.assertEqual(alaya_mod.HEARSAY_MARKERS, markers.union("hearsay"))
        self.assertEqual(markers.MARKERS["en"]["hearsay"], ())
        for w in ("伝え", "記録", "伝えました", "記録しました"):
            self.assertNotIn(w, markers.MARKERS["ja"]["hearsay"])

    def test_every_hit_matches(self):
        self.assertTrue(self.fixture["hit"])
        for t in self.fixture["hit"]:
            self.assertTrue(alaya_mod._has_marker(alaya_mod.sansho._n(t), alaya_mod.HEARSAY_MARKERS), t)

    def test_no_miss_matches(self):
        self.assertTrue(self.fixture["miss"])
        for t in self.fixture["miss"]:
            self.assertFalse(alaya_mod._has_marker(alaya_mod.sansho._n(t), alaya_mod.HEARSAY_MARKERS), t)

    def test_known_hits_match(self):
        # spec 2026-11-04-resume-hearsay-design §7 の利用者裁定 (A): ja の表は現状のまま。行う者の形なのに当たる文は
        # 受け入れた既知の限界として記録する。表が変われば (当たらなくなれば) ここで気づく。
        self.assertTrue(self.fixture["known_hits"])
        for t in self.fixture["known_hits"]:
            self.assertTrue(alaya_mod._has_marker(alaya_mod.sansho._n(t), alaya_mod.HEARSAY_MARKERS), t)

    def test_issue_129_span_is_a_hit(self):
        self.assertIn(HEARSAY, self.fixture["hit"])


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def plant_user_resume(self):
        r = self.store.imprint([_turn(USER1, "承知しました。", 1)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("user", USER1_SPAN)]))
        self.assertTrue(r["is_new"])
        return r["seed"]

    def assert_hearsay_rejected(self, r, before):
        self.assertEqual(r["decision"], "reject")
        self.assertIn(RH, r["reason"])
        self.assertIsNone(r["seed"])
        seeds = self.store.list_seeds()
        self.assertEqual(len(seeds), 1)
        s = seeds[0]
        for k in ("content", "sansho", "strength", "evidences"):
            self.assertEqual(s.get(k), before.get(k), k)
        last = self.store.list_imprint_log()[-1]
        self.assertEqual(last["decision"], "reject")
        self.assertIn(RH, last["reason"])


class TestRh2(_Case):
    def _snap(self, seed):
        return json.loads(json.dumps(self.store.list_seeds()[0], ensure_ascii=False))

    def test_issue_129_hearsay_does_not_supersede(self):
        self.plant_user_resume()
        before = self._snap(None)
        self.assertEqual(before["sansho"], "圓成實")
        r = self.store.imprint([_turn("まとめて", HEARSAY, 2)],
                               _prop("決済モジュールのリトライ処理から再開し、まずファイルと Git の状態を確認する。",
                                     CONDS + ["Git"], [("assistant", "再開時には、ファイルや Git の状態を確認してから進めることが記録されています。")]))
        self.assert_hearsay_rejected(r, before)

    def test_doer_form_still_supersedes(self):
        self.plant_user_resume()
        r = self.store.imprint([_turn("進めて", DOER, 2)],
                               _prop("まずファイルと Git を確認してから再開する。", CONDS + ["Git"], [("assistant", DOER)]))
        self.assertNotEqual(r["decision"], "reject")
        seeds = self.store.list_seeds()
        live = [s for s in seeds if not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["content"], "まずファイルと Git を確認してから再開する。")
        self.assertEqual(live[0]["sansho"], "依他起")

    def test_hearsay_rejected_even_when_existing_is_ita_ki(self):
        self.plant_user_resume()
        # assistant の行う者の形で supersede → 既存は 依他起 (assistant だけ)
        self.store.imprint([_turn("進めて", DOER, 2)],
                           _prop("まずファイルと Git を確認してから再開する。", CONDS + ["Git"], [("assistant", DOER)]))
        before = self._snap(None)
        self.assertEqual(before["sansho"], "依他起")
        r = self.store.imprint([_turn("まとめて", HEARSAY, 3)],
                               _prop("リトライ処理から再開し Git を確認する。", CONDS + ["Git"],
                                     [("assistant", "再開時には、ファイルや Git の状態を確認してから進めることが記録されています。")]))
        self.assert_hearsay_rejected(r, before)

    def test_mixed_user_and_hearsay_span_is_not_covered(self):
        self.plant_user_resume()
        user2 = "これまでの話をまとめてください。決済モジュールのリトライ処理について。"
        r = self.store.imprint([_turn(user2, HEARSAY, 2)],
                               _prop("決済モジュールのリトライ処理から再開し Git を確認する。", CONDS + ["Git"],
                                     [("user", "決済モジュールのリトライ処理について。"),
                                      ("assistant", "再開時には、ファイルや Git の状態を確認してから進めることが記録されています。")]))
        self.assertNotEqual(r["decision"], "reject")
        self.assertNotIn(RH, r.get("reason") or "")
        live = [s for s in self.store.list_seeds() if not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        self.assertIn("Git", live[0]["content"])

    def test_no_existing_resume_point_keeps_b_reason(self):
        r = self.store.imprint([_turn("まとめて", HEARSAY, 1)],
                               _prop("決済モジュールのリトライ処理から再開する。", CONDS,
                                     [("assistant", "次回は決済モジュールのリトライ処理から再開する予定だと伝えました。")]))
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya_mod.RESUME_NEW_ENJOUJITSU_ONLY, r["reason"])
        self.assertNotIn(RH, r["reason"])

    def test_replacement_for_is_not_subject_to_rh2(self):
        seed = self.plant_user_resume()
        r = self.store.imprint([_turn("まとめて", HEARSAY, 2)],
                               _prop("決済モジュールのリトライ処理から再開する。", CONDS,
                                     [("assistant", "次回は決済モジュールのリトライ処理から再開する予定だと伝えました。")],
                                     ), replacement_for=seed["id"])
        self.assertNotIn(RH, r.get("reason") or "")
        self.assertNotEqual(r["decision"], "reject")

    def test_other_kind_is_not_stopped_by_rh2(self):
        self.plant_user_resume()  # 生きた 再開点 がある状態で
        sp = "設定は /home/dev/raiya/config.yaml にあると伝えました。"
        r = self.store.imprint([_turn("進めて", sp, 2)],
                               _prop("設定は /home/dev/raiya/config.yaml", ["config.yaml"], [("assistant", sp)], kind="所在"))
        self.assertNotIn(RH, r.get("reason") or "")

    def test_user_span_with_hearsay_marker_still_supersedes(self):
        self.plant_user_resume()
        u = "前回は決済の件だと言っていました。次はリトライ処理から再開します。"
        self.assertTrue(alaya_mod._has_marker(alaya_mod.sansho._n(u), alaya_mod.HEARSAY_MARKERS))
        r = self.store.imprint([_turn(u, "承知しました。", 2)],
                               _prop("リトライ処理から再開する。", ["リトライ処理"], [("user", u)]))
        self.assertNotIn(RH, r.get("reason") or "")
        self.assertNotEqual(r["decision"], "reject")
        live = [x for x in self.store.list_seeds() if not x.get("superseded_by")]
        self.assertEqual([x["content"] for x in live], ["リトライ処理から再開する。"])

    def test_marker_outside_span_does_not_fire(self):
        self.plant_user_resume()
        # 往復の本文には「と伝えました」があるが、提案の span は印の手前で切れている
        r = self.store.imprint([_turn("まとめて", HEARSAY, 2)],
                               _prop("リトライ処理から再開する。", ["リトライ処理"], [("assistant", "次回は決済モジュールのリトライ処理から再開する予定だ")]))
        self.assertNotIn(RH, r.get("reason") or "")
        self.assertNotEqual(r["decision"], "reject")
        live = [x for x in self.store.list_seeds() if not x.get("superseded_by")]
        self.assertEqual([x["content"] for x in live], ["リトライ処理から再開する。"])


class TestIssue139(_Case):
    RULE = "次は決済モジュールのリトライ処理から再開し、まずファイルや Git の状態を確認することになっています。"
    DOER139 = "次は決済モジュールのリトライ処理から再開し、まずファイルや Git の状態を確認します。"

    def test_rule_telling_form_is_hearsay_and_rejected(self):
        self.plant_user_resume()
        before = json.loads(json.dumps(self.store.list_seeds()[0], ensure_ascii=False))
        self.assertEqual(before["sansho"], "圓成實")
        r = self.store.imprint([_turn("進めて", self.RULE, 2)],
                               _prop("決済モジュールのリトライ処理から再開し、ファイルと Git を確認する。", CONDS + ["Git"],
                                     [("assistant", self.RULE)]))
        self.assert_hearsay_rejected(r, before)

    def test_doer_form_of_same_sentence_still_supersedes(self):
        # 受け入れた穴 (spec §7.1「残る穴」): 行う者の形の言い直しは通る。
        self.plant_user_resume()
        r = self.store.imprint([_turn("進めて", self.DOER139, 2)],
                               _prop("決済モジュールのリトライ処理から再開し、ファイルと Git を確認する。", CONDS + ["Git"],
                                     [("assistant", self.DOER139)]))
        self.assertNotEqual(r["decision"], "reject")
        live = [s for s in self.store.list_seeds() if not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["sansho"], "依他起")


class TestIssue143(_Case):
    # spec 2026-11-04-resume-hearsay-design §7.2: 現在形の伝聞「と伝えています」(#142 C-2)。
    C2 = ("直近では「今日はここで中断し、次回は決済モジュールのリトライ処理から再開する」と伝えています。"
          "これが記録上の再開点です。")
    DOER143 = "次回は決済モジュールのリトライ処理から再開しましょう。"

    def test_present_tense_telling_is_hearsay_and_rejected(self):
        self.plant_user_resume()
        before = json.loads(json.dumps(self.store.list_seeds()[0], ensure_ascii=False))
        self.assertEqual(before["sansho"], "圓成實")
        r = self.store.imprint([_turn("再開点は？", self.C2, 2)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("assistant", self.C2)]))
        self.assert_hearsay_rejected(r, before)

    def test_doer_form_still_supersedes(self):
        # 受け入れた穴: 「再開しましょう」は表に当たらず通る。
        self.plant_user_resume()
        r = self.store.imprint([_turn("進めて", self.DOER143, 2)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("assistant", self.DOER143)]))
        self.assertNotEqual(r["decision"], "reject")
        live = [s for s in self.store.list_seeds() if not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["sansho"], "依他起")


class TestIssue146(_Case):
    # spec 2026-11-04-resume-hearsay-design §7.3: 「と話していました」「と話しています」(40 往復 #20)。
    PAST = "次回は「決済モジュールのリトライ処理」から再開すると話していました。"
    PRESENT = "次回はリトライ処理から再開すると話しています。"
    DOER146 = "次回は決済モジュールのリトライ処理から再開ですね。"

    def _rejected(self, span):
        self.plant_user_resume()
        before = json.loads(json.dumps(self.store.list_seeds()[0], ensure_ascii=False))
        self.assertEqual(before["sansho"], "圓成實")
        r = self.store.imprint([_turn("再開点は？", span, 2)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("assistant", span)]))
        self.assert_hearsay_rejected(r, before)

    def test_past_telling_is_hearsay_and_rejected(self):
        self._rejected(self.PAST)

    def test_present_telling_is_hearsay_and_rejected(self):
        self._rejected(self.PRESENT)

    def test_doer_form_still_supersedes(self):
        # 受け入れた穴: 「再開ですね」は表に当たらず通る。
        self.plant_user_resume()
        r = self.store.imprint([_turn("進めて", self.DOER146, 2)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("assistant", self.DOER146)]))
        self.assertNotEqual(r["decision"], "reject")
        live = [s for s in self.store.list_seeds() if not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["sansho"], "依他起")


class TestIssue153(_Case):
    # spec 2026-11-04-resume-hearsay-design §7.4 (NV): 引用の と + 言う動詞 + てい(ます|ました) の 19 語 × 2 形。
    PRESENT = "次回は「決済モジュールのリトライ処理」から再開すると述べています。"
    PAST = "次回は「決済モジュールのリトライ処理」から再開すると述べていました。"
    DOER153 = "次は決済モジュールのリトライ処理から再開します。"
    VERBS = ("言っ", "伝え", "話し", "述べ", "語っ", "おっしゃっ", "仰っ", "説明し", "報告し", "宣言し",
             "明言し", "表明し", "発言し", "指示し", "依頼し", "答え", "回答し", "聞い", "伺っ")

    def _rejected(self, span):
        self.plant_user_resume()
        before = json.loads(json.dumps(self.store.list_seeds()[0], ensure_ascii=False))
        self.assertEqual(before["sansho"], "圓成實")
        r = self.store.imprint([_turn("再開点は？", span, 2)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("assistant", span)]))
        self.assert_hearsay_rejected(r, before)

    def test_present_form_is_hearsay_and_rejected(self):
        self._rejected(self.PRESENT)

    def test_past_form_is_hearsay_and_rejected(self):
        self._rejected(self.PAST)

    def test_doer_form_still_supersedes(self):
        self.plant_user_resume()
        r = self.store.imprint([_turn("進めて", self.DOER153, 2)],
                               _prop("次回は決済モジュールのリトライ処理から再開する。", CONDS, [("assistant", self.DOER153)]))
        self.assertNotEqual(r["decision"], "reject")
        live = [s for s in self.store.list_seeds() if not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["sansho"], "依他起")

    def test_table_has_all_nineteen_verbs_in_both_forms(self):
        table = markers.MARKERS["ja"]["hearsay"]
        self.assertEqual(len(self.VERBS), 19)
        for v in self.VERBS:
            for tail in ("います", "いました"):
                self.assertIn("と" + v + "て" + tail, table)

    def test_writing_verbs_are_not_in_table(self):
        table = markers.MARKERS["ja"]["hearsay"]
        for w in ("と書いています", "と書いていました", "と記しています", "と記していました"):
            self.assertNotIn(w, table)

    def test_table_has_no_duplicates(self):
        table = markers.MARKERS["ja"]["hearsay"]
        self.assertEqual(len(table), len(set(table)))


if __name__ == "__main__":
    unittest.main()
