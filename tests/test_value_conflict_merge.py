"""#3 / #5: 値の食い違う 決定 を併合・強めせず新規に植え (T1)、双子の影で隠さず群から外す (T2) の pin。

設計書 2026-11-28-value-conflict-merge-design.md §1.3 (足場・fixture は逐語)・§3.2〜§3.4・§5。
alaya.imprint / 純関数を直に呼ぶ (LLM なし)。テストが消すのは自作 tmp だけ。
"""
import copy
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))
os.environ["RAIYA_CONFIG"] = ""

from raiya import alaya  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from _state_fixture import alaya_store_at  # noqa: E402
from test_layered_circles import _seed, _StateCase  # noqa: E402

P = "/proj"
OLD = ["検査用のログ", "保存期間", "30日"]
BOTH = ["検査用のログ", "保存期間", "30日", "60日"]
ONLY60 = ["検査用のログ", "保存期間", "60日"]
A60 = "検査用のログの保存期間を60日にします。"
DEFAULT_CONTENT = "検査用ログの保存期間を60日に変更。"
NOTE = "値の食い違い"

# 足場 (承認の対で植える。結果はどれも 決定・依他起・強度 2)
SCAFFOLDS = {
    "P": dict(before="検査用のログの保存期間を決めたい", rec="**推奨: 案2（保存期間30日）**",
              content="検査用ログの保存期間は30日を採用。", conds=OLD),
    "S3K": dict(before="s3 のログの保存期間を決めたい", rec="**推奨: 案2（s3 は 保存期間30日）**",
                content="s3 のログの保存期間は30日を採用。", conds=["s3", "保存期間", "30日"]),
    "PR": dict(before="リトライの回数を決めたい", rec="**推奨: 案2（リトライ回数12回）**",
               content="リトライ回数は12回を採用。", conds=["リトライ", "回数", "12回"]),
    "S2": dict(before="保存期間を決めたい", rec="**推奨: 案2（保存期間30日）**",
               content="保存期間は30日を採用。", conds=["保存期間", "30日"]),
}
E_USER = "検査用のログの保存期間は30日にした。"
E_CONTENT = "検査用ログの保存期間は30日を採用。"
H30_USER = "検査用のログの保存期間は30日にして。"
H30_ASST = "検査用のログの保存期間を30日にします。"

# fixture: 名 -> (user の発話, assistant の span (None = なし), 縁, content (None = 既定), 足場の id を injected にするか)
FX = {
    "D60": ("検査用のログの保存期間を60日にして。", A60, ONLY60),
    "D60b": ("検査用のログの保存期間を60日にして。", "検査用のログの保存期間を30日から60日にします。", BOTH),
    "N1a": ("検査用のログの保存期間を30日でなく60日にして。", A60, BOTH),
    "N1b": ("検査用のログの保存期間を30日から60日にして。", A60, BOTH),
    "N2a": ("検査用のログの保存期間は30日ではなく60日にして。", A60, BOTH),
    "N2b": ("検査用のログの保存期間は30日ではなく、60日にして。", A60, BOTH),
    "N2c": ("検査用のログの保存期間は30日は却下で60日にして。", A60, BOTH),
    "N2d": ("検査用のログの保存期間は30日は却下、60日にして。", A60, BOTH),
    "NEG": ("検査用のログの保存期間は30日はやめて60日にして。", A60, BOTH),
    "NEGc": ("検査用のログの保存期間は30日はやめて、60日にして。", A60, BOTH),
    "ORD1": ("検査用のログの保存期間は60日にして30日はやめる。", A60, BOTH),
    "ORD2": ("検査用のログの保存期間は60日にして。30日はやめる。", A60, BOTH),
    "BACK": ("検査用のログの保存期間は60日から30日に戻して。", A60, BOTH),
    "MS1": ("検査用のログの保存期間は30日だった。60日にして。", A60, BOTH),
    "TWOS": ("検査用のログの保存期間は30日。延長の上限は60日にして。", A60, BOTH),
    "TWOADD": ("検査用のログの保存期間は30日のまま延長の上限を60日にして。", A60, BOTH),
    "TWOADDc": ("検査用のログの保存期間は30日のまま、延長の上限を60日にして。", A60, BOTH),
    "DECL60": ("検査用のログの保存期間は60日です。", A60, ONLY60),
    "TQ": ("検査用のログの保存期間を60日にする？", A60, ONLY60),
    "ECHO": ("そのままで", A60, ONLY60),
    "D60u": ("検査用のログの保存期間を60日にして。", None, ONLY60),
    "N1bu": ("検査用のログの保存期間を30日から60日にして。", None, BOTH),
}
KEEP_USER = "検査用のログの保存期間は30日のままにします。"
KEEP_CONTENT = "検査用ログの保存期間は30日のまま。"
OTHER = {   # 足場, user, asst, 縁, content
    "S3K-60": ("S3K", "s3 のログの保存期間を60日にして。", "s3 のログの保存期間を60日にします。",
               ["s3", "保存期間", "60日"], "s3 のログの保存期間を60日に変更。"),
    "PR12": ("PR", "リトライの回数を24回にして。", "リトライの回数を24回にします。",
             ["リトライ", "回数", "24回"], "リトライ回数を24回に変更。"),
    "SYN": ("P", "検査用のログの保存期間を1か月にして。", "検査用のログの保存期間を1か月にします。",
            ["検査用のログ", "保存期間", "1か月"], "検査用ログの保存期間を1か月に変更。"),
    "S2-60": ("S2", "保存期間を60日にして。", "保存期間を60日にします。", ["保存期間", "60日"], "保存期間を60日に変更。"),
}


def _turn(user, asst, i):
    return LedgerTurn(user_text=user, assistant_text=asst, id=i, project=P)


def _said(text):
    n, p = alaya._sentence_pieces(text)
    return alaya._remainder_from_offset(n, n, p, 0)


def _vd(old, new, user):
    return alaya._value_divergence(old, new, [_said(user)], [user])


class _Helpers:
    """足場・提案の組み立て (TestCase に混ぜる)。"""

    def new_store(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = alaya_store_at(os.path.join(tmp, "alaya.json"), os.path.join(tmp, "imprint_log.json"))
        store.begin_new_life()
        return store

    def plant(self, name, store=None):
        """足場 `name` (P / S3K / PR / S2 = 承認の対、E = 圓成實、H30 = user の span を持つ 依他起) を植えて (store, sid) を返す。次の life へ。"""
        store = store or self.new_store()
        if name in SCAFFOLDS:
            ex = SCAFFOLDS[name]
            w = [_turn(ex["before"], "3 案あります。" + ex["rec"], 1), _turn("GO", "了解", 2)]
            r = store.imprint(
                w,
                {"content": ex["content"], "conditions": ex["conds"], "polarity": "無記", "kind": "決定", "action": "new",
                 "evidence": [{"source": "assistant", "span": ex["rec"]}, {"source": "user", "span": "GO"}]},
                injected_by_turn={}, session_by_turn=["s1", "s1"])
            self.assertEqual((r["kind"], r["sansho"], r["seed"]["strength"]), ("決定", "依他起", 2), r["reason"])
        elif name == "E":
            r = store.imprint(
                [_turn(E_USER, "了解しました。", 1)],
                {"content": E_CONTENT, "conditions": OLD, "polarity": "無記", "kind": "決定", "action": "new",
                 "evidence": [{"source": "user", "span": E_USER}]},
                injected_by_turn={}, session_by_turn=["s1"])
            self.assertEqual((r["kind"], r["sansho"], r["seed"]["strength"]), ("決定", "圓成實", 1), r["reason"])
        elif name == "H30":
            r = store.imprint(
                [_turn(H30_USER, "承知しました。" + H30_ASST, 1)],
                {"content": E_CONTENT, "conditions": OLD, "polarity": "無記", "kind": "決定", "action": "new",
                 "evidence": [{"source": "user", "span": H30_USER}, {"source": "assistant", "span": H30_ASST}]},
                injected_by_turn={}, session_by_turn=["s1"])
            self.assertEqual((r["kind"], r["sansho"], r["seed"]["strength"]), ("決定", "依他起", 1), r["reason"])
            self.assertTrue(alaya.has_user_span(r["seed"]))
        else:
            raise AssertionError(name)
        sid = r["seed"]["id"]
        store.begin_new_life()
        return store, sid

    def propose(self, store, sid, user, asst, conds, action="new", content=None, injected=None, **kw):
        w = [_turn(user, "承知しました。" + (asst if asst is not None else "了解です。"), 3)]
        ev = [{"source": "user", "span": user}]
        if asst is not None:
            ev.append({"source": "assistant", "span": asst})
        p = {"content": content or DEFAULT_CONTENT, "conditions": list(conds), "polarity": "無記", "kind": "決定", "evidence": ev}
        if action != "new":
            p.update({"action": action, "target_id": sid})
        else:
            p["action"] = "new"
        p.update(kw)
        return store.imprint(w, p, injected_by_turn=injected if injected is not None else {}, session_by_turn=["s2"])

    def run_fx(self, store, sid, name, action="new", **kw):
        user, asst, conds = FX[name][:3]
        injected = {0: {sid}} if name == "ECHO" else None
        return self.propose(store, sid, user, asst, conds, action=action, injected=injected, **kw)

    def strength(self, store, sid):
        return store.get_by_ids([sid])[0]["strength"]

    def assert_falls_to_new(self, scaffold, name, action, base_strength):
        store, sid = self.plant(scaffold)
        r = self.run_fx(store, sid, name, action)
        self.assertTrue(r["is_new"], "{} {} {}: {}".format(scaffold, name, action, r["reason"]))
        self.assertEqual(len(store.list_seeds()), 2, "{} {} {}".format(scaffold, name, action))
        self.assertEqual(self.strength(store, sid), base_strength, "{} {} {}".format(scaffold, name, action))
        return r, store, sid


class _Base(_Helpers, unittest.TestCase):
    pass


class TestValueDivergencePredicate(unittest.TestCase):
    def test_silent_change(self):
        u = "検査用のログの保存期間を60日にして。"
        self.assertEqual(alaya._value_divergence(OLD, ONLY60, [u], [u]), ["60日"])

    def test_old_value_from_assistant_only(self):
        self.assertEqual(_vd(OLD, BOTH, "検査用のログの保存期間を60日にして。"), ["60日"])

    def test_named_change_same_sentence(self):
        self.assertEqual(_vd(OLD, BOTH, FX["N1b"][0]), ["60日"])

    def test_named_change_after_comma(self):
        self.assertEqual(_vd(OLD, ["保存期間", "30日", "60日"], "保存期間は、30日ではなく60日にして。"), ["60日"])

    def test_comma_between_old_and_new_still_named(self):
        for name in ("N2b", "N2d", "NEGc"):
            self.assertEqual(_vd(OLD, BOTH, FX[name][0]), ["60日"], name)

    def test_rejection_marker_no_exception(self):
        for name in ("N1a", "N2a", "N2c"):
            self.assertEqual(_vd(OLD, BOTH, FX[name][0]), ["60日"], name)

    def test_reverse_order_not_named(self):
        for name in ("ORD1", "ORD2"):
            self.assertEqual(_vd(OLD, BOTH, FX[name][0]), [], name)

    def test_back_with_old_first_limit(self):
        """受け入れた限界 (設計書 §7 L18、裁定 2026-11-28): 旧い値を先に言い新しい値を後で退ける戻しの言い方は、(b) が後に言われた数字を新しい値と読む。"""
        t = "検査用のログの保存期間は30日に戻して、60日はやめる。"
        self.assertEqual(alaya._value_divergence(OLD, BOTH, [t], [t]), ["60日"])
        self.assertEqual(alaya._value_divergence(ONLY60, BOTH, [t], [t]), [])

    def test_keep_old_reject_new_limit(self):
        """受け入れた限界 (設計書 §7 L18 の 2 つ目の言い方、裁定 2026-11-28): 「30日で、60日にはしない」も (b) が後に言われた 60日 を新しい値と読む。"""
        t = "検査用のログの保存期間は30日で、60日にはしない。"
        self.assertEqual(alaya._value_divergence(OLD, BOTH, [t], [t]), ["60日"])
        self.assertEqual(alaya._value_divergence(ONLY60, BOTH, [t], [t]), [])

    def test_back_to_old_value(self):
        self.assertEqual(_vd(OLD, BOTH, FX["BACK"][0]), [])

    def test_two_sentences_not_named(self):
        self.assertEqual(_vd(OLD, BOTH, FX["TWOS"][0]), [])

    def test_cross_sentence_not_named(self):
        self.assertEqual(_vd(OLD, BOTH, FX["MS1"][0]), [])

    def test_add_value_same_sentence(self):
        for name in ("TWOADD", "TWOADDc"):
            self.assertEqual(_vd(OLD, BOTH, FX[name][0]), ["60日"], name)

    def test_unsaid_old_digit_and_new_digit(self):
        self.assertEqual(_vd(OLD, ["検査用のログ", "保存期間", "3世代"], "検査用のログの保存期間の件、3世代残して。"), ["3世代"])

    def test_keep_old_value(self):
        """守る行は 3 つ (C から旧の鍵を除く `key in old_keys`、(b) の `o_key != key`、(b) の `< j`)。`key in old_keys` だけ・`o_key != key` だけ・この 2 つを同時に外しても落ちない (KEEP の発話は 30日 が 1 度しか出ず、o と c が同じ縁でも `i < j` が成り立たない)。3 つ目の `< j` を `<= j` にしたものも同時に壊したときに初めて落ちる。"""
        self.assertEqual(_vd(OLD, OLD, KEEP_USER), [])

    def test_question_only_value(self):
        u = FX["TQ"][0]
        self.assertEqual(_said(u), "")
        self.assertEqual(_vd(OLD, ONLY60, u), [])

    def test_no_digit_value(self):
        self.assertEqual(_vd(["ログ", "頻度", "日次"], ["ログ", "頻度", "週次"], "ログの頻度を週次にして。"), [])

    def test_thousands_comma(self):
        u = "検査用のログの保存期間を60日にして。1,000件を超えたら消して。"
        self.assertEqual(_vd(OLD, ["検査用のログ", "保存期間", "60日", "1,000件"], u), ["60日", "1,000件"])

    def test_texts_from_spans_matches_texts(self):
        for s in ("検査用のログの保存期間は30日だった。60日にして。", "検査用のログの保存期間は30日にした。60日にする？"):
            self.assertEqual(alaya._vd_texts_from_spans([s])[2], alaya._vd_texts([_said(s)], [s])[2])
            self.assertEqual(alaya._vd_texts_from_spans([s])[0], alaya._vd_texts([_said(s)], [s])[0])
            self.assertEqual(alaya._vd_texts_from_spans([s])[1], alaya._vd_texts([_said(s)], [s])[1])
        # 疑の文は said から除かれ、user には残る
        said, user, units = alaya._vd_texts_from_spans(["検査用のログの保存期間は30日にした。60日にする？"])
        self.assertEqual(said, ["検査用のログの保存期間は30日にした。"])
        self.assertEqual(user, ["検査用のログの保存期間は30日にした。60日にする?"])   # `sansho._n` は NFKC で ？ を ? に畳む
        self.assertEqual(units, ["検査用のログの保存期間は30日にした。"])

    def test_non_str_and_empty_keys_ignored(self):
        u = "検査用のログの保存期間を60日にして。"
        self.assertEqual(alaya._value_divergence(OLD + [None, 30, ""], ONLY60 + [None, 5, " "], [u], [u]), ["60日"])


class TestImprintValueConflict(_Base):
    def test_new_d60_plants_new_not_merge(self):
        r, store, sid = self.assert_falls_to_new("P", "D60", "new", 2)
        self.assertEqual(r["reason"].count(NOTE), 1)
        self.assertEqual(store.list_imprint_log()[-1]["reason"].count(NOTE), 1)
        self.assertIn("#{}".format(sid), r["reason"])

    def test_reinforce_d60_falls_to_new(self):
        r, store, sid = self.assert_falls_to_new("P", "D60", "reinforce", 2)
        self.assertEqual(r["reason"].count(NOTE), 1)
        self.assertEqual(store.list_imprint_log()[-1]["reason"].count(NOTE), 1)

    def test_supersede_unchanged(self):
        store, sid = self.plant("P")
        r = self.run_fx(store, sid, "D60", "supersede")
        self.assertTrue(r.get("superseded"))
        self.assertEqual(store.get_by_ids([sid])[0]["content"], DEFAULT_CONTENT)
        self.assertNotIn(NOTE, r["reason"])

    def test_d60b_falls_to_new(self):
        for action in ("new", "reinforce"):
            self.assert_falls_to_new("P", "D60b", action, 2)

    def test_n2_falls_to_new(self):
        for name in ("N2a", "N2b", "N2c", "N2d"):
            for action in ("new", "reinforce"):
                self.assert_falls_to_new("P", name, action, 2)

    def test_negation_with_comma_falls_to_new(self):
        self.assert_falls_to_new("P", "NEGc", "new", 2)

    def test_twoadd_falls_to_new(self):
        for name in ("TWOADD", "TWOADDc"):
            self.assert_falls_to_new("P", name, "new", 2)

    def test_reverse_order_still_merges(self):
        store, sid = self.plant("P")
        r = self.run_fx(store, sid, "ORD1")
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(self.strength(store, sid), 3)
        self.assertNotIn(NOTE, r["reason"])

    def test_back_still_merges(self):
        store, sid = self.plant("P")
        r = self.run_fx(store, sid, "BACK")
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(self.strength(store, sid), 3)

    def test_assistant_keep_still_merges(self):
        """HEAD の固定。観た事実 (3 つ): (1) `key in old_keys or` を外すだけでは落ちない。(2) それに、`_vd_of` の said と user の両方に 依他起 の span も入れる (`if True:`) 壊しを足しても落ちない。(3) `key in old_keys or` を外す + said にだけ 依他起 の span を入れる (user は 圓成實 の span のまま) 2 か所を同時に壊すと落ちる。said だけに入れる壊しの単独は試していない。"""
        store, sid = self.plant("P")
        r = self.propose(store, sid, "検査用のログの保存期間を決めて。", KEEP_USER, OLD, content=KEEP_CONTENT)
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(self.strength(store, sid), 3)

    def test_echo_still_record_only(self):
        store, sid = self.plant("P")
        r = self.run_fx(store, sid, "ECHO")
        self.assertEqual(r["echo_match"], sid)
        self.assertEqual(self.strength(store, sid), 2)

    def test_question_form_unchanged(self):
        store, sid = self.plant("P")
        r = self.run_fx(store, sid, "TQ")
        self.assertEqual(r["kind"], "性向")
        self.assertTrue(r["is_new"])
        self.assertNotIn(NOTE, r["reason"])

    def test_kind_disposition_untouched(self):
        store = self.new_store()
        u0 = "検査用のログの保存期間は30日がいい。"
        r0 = store.imprint([_turn(u0, "了解しました。", 1)],
                           {"content": "検査用ログの保存期間は30日がいい。", "conditions": OLD, "polarity": "無記", "kind": "性向",
                            "action": "new", "evidence": [{"source": "user", "span": u0}]},
                           injected_by_turn={}, session_by_turn=["s1"])
        self.assertEqual(r0["kind"], "性向")
        sid = r0["seed"]["id"]
        store.begin_new_life()
        r = self.propose(store, sid, FX["D60"][0], A60, ONLY60, kind="性向")
        # HEAD の結果 (実装前に観測): 性向 は 圓成實 と 依他起 で class が違い new (強度 1 のまま・種子 2 つ)。VD の注記は付かない
        self.assertEqual(r["kind"], "性向")
        self.assertTrue(r["is_new"], r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(self.strength(store, sid), 1)
        self.assertNotIn(NOTE, r["reason"])

    def test_kind_disposition_same_class_still_merges(self):
        # 性向 は VD の対象外: 同じ class (圓成實) で縁を 2 語共有し値が違っても HEAD と同じく併合する (注記なし)
        store = self.new_store()
        u0 = "検査用のログの保存期間は30日がいい。"
        r0 = store.imprint([_turn(u0, "了解しました。", 1)],
                           {"content": "検査用ログの保存期間は30日がいい。", "conditions": OLD, "polarity": "無記", "kind": "性向",
                            "action": "new", "evidence": [{"source": "user", "span": u0}]},
                           injected_by_turn={}, session_by_turn=["s1"])
        sid = r0["seed"]["id"]
        store.begin_new_life()
        u1 = "検査用のログの保存期間は60日がいい。"
        r = store.imprint([_turn(u1, "了解しました。", 3)],
                          {"content": "検査用ログの保存期間は60日がいい。", "conditions": ONLY60, "polarity": "無記", "kind": "性向",
                           "action": "new", "evidence": [{"source": "user", "span": u1}]},
                          injected_by_turn={}, session_by_turn=["s2"])
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(self.strength(store, sid), 2)
        self.assertNotIn(NOTE, r["reason"])

    def test_withdrawn_old_value_still_withdrawal_match(self):
        # 人が取り下げた種子 (guard) には VD を当てない: 値が違っても取り下げ照合のまま (人の取り下げを迂回しない)
        store, sid = self.plant("P")
        store.withdraw(sid, {"by": "qa", "reason": "probe", "at": "2026-11-28"})
        store.begin_new_life()
        r = self.run_fx(store, sid, "D60", "new")
        self.assertEqual(r.get("withdrawn_match"), sid, r["reason"])
        self.assertIsNone(r["is_new"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertNotIn(NOTE, r["reason"])

    def test_twin_hidden_old_value_still_twin_match(self):
        # 双子に隠された H (guard) にも VD を当てない: 値が違っても双子照合のまま
        store, eid = self.plant("E")
        r0 = store.imprint(
            [_turn(H30_USER, "承知しました。" + H30_ASST, 2)],
            {"content": "検査用ログは30日保存。", "conditions": OLD, "polarity": "無記", "kind": "決定", "action": "new",
             "evidence": [{"source": "user", "span": H30_USER}, {"source": "assistant", "span": H30_ASST}]},
            injected_by_turn={}, session_by_turn=["s1"])
        hid = r0["seed"]["id"]
        self.assertEqual((r0["sansho"], r0["is_new"]), ("依他起", True), r0["reason"])
        self.assertEqual(alaya._twin_shadows(store.state["seeds"]).get(id(store._find_seed(hid))), eid)
        store.begin_new_life()
        r = self.run_fx(store, eid, "D60", "new")
        self.assertEqual(r.get("twin_match"), hid, r["reason"])
        self.assertIsNone(r["is_new"])
        self.assertNotIn(NOTE, r["reason"])

    def test_replacement_not_subject_to_vd(self):
        # 人の置き換え (replacement_for) には VD を当てない: HEAD と同じく併合 (強めて reinforced)
        store, sid = self.plant("P")
        user, asst, conds = FX["D60"][:3]
        w = [_turn(user, "承知しました。" + asst, 3)]
        p = {"content": DEFAULT_CONTENT, "conditions": list(conds), "polarity": "無記", "kind": "決定", "action": "new",
             "evidence": [{"source": "user", "span": user}, {"source": "assistant", "span": asst}]}
        r = store.imprint(w, p, replacement_for=999, injected_by_turn={}, session_by_turn=["s2"])
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(self.strength(store, sid), 3)
        self.assertNotIn(NOTE, r["reason"])

    def test_back_with_old_first_after_new_value(self):
        """受け入れた限界 (設計書 §7 L18、裁定 2026-11-28): 旧い値を先に言い新しい値を後で退ける戻しの言い方は、(b) が後に言われた数字を新しい値と読むので、退けた値の種子に併合される。直すときはこの期待を反転させる。"""
        store, sid = self.plant("P")
        r1 = self.run_fx(store, sid, "D60", "new")
        self.assertTrue(r1["is_new"], r1["reason"])
        sid60 = r1["seed"]["id"]
        store.begin_new_life()
        r2 = self.propose(store, sid, "検査用のログの保存期間は30日に戻して、60日はやめる。", "検査用のログの保存期間を30日に戻します。", BOTH)
        self.assertTrue(r2.get("reinforced"), r2["reason"])
        self.assertEqual(r2["seed"]["id"], sid60)
        self.assertIn("この種子には併合・強めをしない", r2["reason"])
        self.assertNotIn("new として扱う", r2["reason"])   # 別の種子に併合されたのに new と書かない
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(self.strength(store, sid), 2)
        self.assertEqual(self.strength(store, sid60), 2)

    def test_keep_old_reject_new_after_split_limit(self):
        """受け入れた限界 (設計書 §7 L18 の 2 つ目の言い方、裁定 2026-11-28): 「30日で、60日にはしない」も、(b) が後に言われた数字を新しい値と読むので、退けた値の種子に併合される。直すときはこの期待を反転させる。"""
        store, sid = self.plant("P")
        r1 = self.run_fx(store, sid, "D60", "new")
        self.assertTrue(r1["is_new"], r1["reason"])
        sid60 = r1["seed"]["id"]
        store.begin_new_life()
        r2 = self.propose(store, sid, "検査用のログの保存期間は30日で、60日にはしない。", "検査用のログの保存期間は30日のままにします。", BOTH)
        self.assertTrue(r2.get("reinforced"), r2["reason"])
        self.assertEqual(r2["seed"]["id"], sid60)
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(self.strength(store, sid), 2)
        self.assertEqual(self.strength(store, sid60), 2)

    def test_other_values_fall_to_new(self):
        for name, (scaffold, user, asst, conds, content) in OTHER.items():
            if name == "S2-60":
                continue
            store, sid = self.plant(scaffold)
            r = self.propose(store, sid, user, asst, conds, content=content)
            self.assertTrue(r["is_new"], "{}: {}".format(name, r["reason"]))
            self.assertEqual(len(store.list_seeds()), 2, name)
            self.assertEqual(self.strength(store, sid), 2, name)

    def test_two_condition_seed_unchanged(self):
        scaffold, user, asst, conds, content = OTHER["S2-60"]
        store, sid = self.plant(scaffold)
        r = self.propose(store, sid, user, asst, conds, content=content)
        self.assertTrue(r["is_new"])
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(self.strength(store, sid), 2)
        self.assertNotIn(NOTE, r["reason"])

    def test_fullwidth_digits(self):
        # sansho._n は NFKC で畳むので、全角の「６０日」は縁・照合のどちらでも半角と同じに読まれる
        u = "検査用のログの保存期間を６０日にして。"
        self.assertEqual(alaya._value_divergence(OLD, ["検査用のログ", "保存期間", "６０日"], [_said(u)], [u]), ["６０日"])

    def test_synonym_value_plants_new(self):
        scaffold, user, asst, conds, content = OTHER["SYN"]
        store, sid = self.plant(scaffold)
        r = self.propose(store, sid, user, asst, conds, content=content)
        self.assertTrue(r["is_new"])
        self.assertEqual(self.strength(store, sid), 2)


class TestImprintValueConflictEnjoujitsu(_Base):
    def test_rejection_marker_reinforce_falls_to_new(self):
        store, sid = self.plant("E")
        r = self.run_fx(store, sid, "N2a", "reinforce")
        self.assertTrue(r["is_new"], r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(self.strength(store, sid), 1)

    def test_rejection_marker_supersede_unchanged(self):
        store, sid = self.plant("E")
        r = self.run_fx(store, sid, "N2a", "supersede")
        self.assertTrue(r.get("superseded"), r["reason"])
        self.assertEqual(store.get_by_ids([sid])[0]["content"], DEFAULT_CONTENT)

    def test_user_only_proposal_falls_to_new(self):
        for name in ("D60u", "N1bu"):
            store, sid = self.plant("E")
            r = self.run_fx(store, sid, name, "new")
            self.assertTrue(r["is_new"], "{}: {}".format(name, r["reason"]))
            self.assertEqual(len(store.list_seeds()), 2, name)
            self.assertEqual(self.strength(store, sid), 1, name)

    def test_keep_still_merges(self):
        """test_keep_old_value と同じ。`key in old_keys` と `o_key != key` を同時に外しても落ちず、さらに (b) の `< j` を `<= j` にした 3 か所を同時に壊したときに初めて落ちる。"""
        store, sid = self.plant("E")
        r = self.propose(store, sid, KEEP_USER, KEEP_USER, OLD, content=KEEP_CONTENT)
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(self.strength(store, sid), 2)

    def test_reinforce_arm_matches_new_arm_for_sc(self):
        for action in ("reinforce", "new"):
            store, sid = self.plant("E")
            r = self.propose(store, sid, FX["N2a"][0], "検査用のログの保存期間を60日にします。", BOTH, action=action, content=E_CONTENT)
            self.assertEqual(r.get("settled_match"), sid, "{}: {}".format(action, r["reason"]))
            self.assertEqual(self.strength(store, sid), 1)
            self.assertEqual(len(store.list_seeds()), 1)


def _seeds_of(store):
    return sorted((copy.deepcopy(s) for s in store.list_seeds()), key=lambda s: s["id"])


class TestCarrierRead(_Helpers, _StateCase):
    """T2: 読む側。state は imprint で植えて作り、seeds を _StateCase.write に写す。"""

    def build(self, scaffold, name, action="new", extra=()):
        store, sid = self.plant(scaffold)
        r = self.run_fx(store, sid, name, action)
        for user, conds in extra:
            self.propose(store, sid, user, None, conds, action="new")
        return store, sid, r

    def seeds_and_engine(self, store):
        seeds = _seeds_of(store)
        eng = self.write(seeds=seeds)
        return seeds, eng

    def shadows(self, seeds):
        return alaya._twin_shadows(seeds)

    def narrative(self, eng, text="検査用のログの保存期間"):
        x, r = self.assert_explain_equals_recall(eng, text, project=P)
        return r["narrative"]

    def test_twin_not_hidden_when_value_differs(self):
        store, sid, r = self.build("E", "D60")
        seeds, eng = self.seeds_and_engine(store)
        new = r["seed"]
        self.assertEqual(new["sansho"], "依他起")
        self.assertNotIn(id(next(s for s in seeds if s["id"] == new["id"])), self.shadows(seeds))
        self.assertEqual(alaya.seed_groups(seeds), [])
        self.assertIn("60日", self.narrative(eng))

    def test_twin_still_hidden_when_same_value(self):
        store, sid, r = self.build("E", "BACK")
        seeds, eng = self.seeds_and_engine(store)
        new = next(s for s in seeds if s["id"] == r["seed"]["id"])
        self.assertEqual(self.shadows(seeds).get(id(new)), sid)
        self.assertEqual(len(alaya.seed_groups(seeds)), 1)

    def test_group_excludes_carrier(self):
        store, sid, r = self.build("E", "D60u")
        seeds, eng = self.seeds_and_engine(store)
        self.assertEqual(r["seed"]["sansho"], "圓成實")
        self.assertEqual(len(seeds), 2)
        self.assertEqual(alaya.seed_groups(seeds), [])
        self.assertIn("60日", self.narrative(eng))

    def test_question_only_still_hidden(self):
        store, sid, r = self.build("E", "MS1")
        seeds, eng = self.seeds_and_engine(store)
        new = next(s for s in seeds if s["id"] == r["seed"]["id"])
        self.assertEqual(self.shadows(seeds).get(id(new)), sid)

    def test_rev_shows_old_value(self):
        store, sid = self.plant("H30")
        r = self.propose(store, sid, "検査用のログの保存期間を60日にして。", None, ONLY60, content="検査用ログの保存期間を60日に変更。")
        self.assertEqual((r["sansho"], r["is_new"]), ("圓成實", True), r["reason"])
        seeds, eng = self.seeds_and_engine(store)
        h30 = next(s for s in seeds if s["id"] == sid)
        self.assertNotIn(id(h30), self.shadows(seeds))
        self.assertEqual(alaya.seed_groups(seeds), [])

    def test_direction(self):
        store, sid, r = self.build("P", "D60u")
        seeds, eng = self.seeds_and_engine(store)
        p = next(s for s in seeds if s["id"] == sid)
        self.assertEqual(self.shadows(seeds).get(id(p)), r["seed"]["id"])

    def test_chain_of_three(self):
        store, sid, r = self.build("E", "D60", extra=[("検査用のログの保存期間を90日にして。", ["検査用のログ", "保存期間", "90日"])])
        seeds, eng = self.seeds_and_engine(store)
        self.assertEqual(len(seeds), 3)
        self.assertEqual(self.shadows(seeds), {})
        self.assertEqual(alaya.seed_groups(seeds), [])

    def test_explain_equals_recall(self):
        """HEAD の固定 (engine.explain_recall = recall)。alaya の差分は recall と explain_recall が同じ `_twin_shadows` / `_seed_groups_by_seed` を通るので、片方だけに効く壊し方が作れず、壊して落とすことは試していない (構造からの推論)。"""
        for scaffold, name in (("E", "D60"), ("E", "BACK"), ("E", "D60u"), ("E", "MS1"), ("P", "D60u")):
            store, sid, r = self.build(scaffold, name)
            seeds, eng = self.seeds_and_engine(store)
            self.narrative(eng)


def _ev(span, i):
    return {"source": "user", "span": span, "life": 1, "ledger_id": i}


def _form_a_seeds():
    out = []
    for i in range(1, 301):
        out.append(_seed(i, ["検査用のログ", "保存期間", "{}日".format(i)], sansho="圓成實" if i % 2 else "依他起",
                         content="検査用ログの保存期間は{}日。".format(i),
                         evidences=[_ev("検査用のログの保存期間を{}日にして。".format(i), i)]))
    return out


def _form_b_seeds():
    out = []
    for i in range(1, 151):
        out.append(_seed(i, ["検査用のログ", "保存期間", "30日"], sansho="圓成實", content="検査用ログの保存期間は30日。",
                         evidences=[_ev("検査用のログの保存期間は30日にした。", i)]))
    for i in range(151, 301):
        out.append(_seed(i, ["検査用のログ", "保存期間", "30日", "{}日".format(i + 1000)], sansho="依他起",
                         content="検査用ログの保存期間を{}日に。".format(i + 1000),
                         evidences=[_ev("検査用のログの保存期間を30日から{}日にして。".format(i + 1000), i)]))
    return out


def _form_c_seeds():
    return [_seed(i, ["検査用のログ", "保存期間", "頻度"], sansho="圓成實" if i % 2 else "依他起",
                  content="検査用ログ{}".format(i), evidences=[_ev("検査用のログの保存期間の頻度{}。".format(chr(0x3042 + i % 40)), i)])
            for i in range(1, 301)]


class TestCarrierLatency(_StateCase):
    def timed_recall(self, seeds):
        eng = self.write(seeds=seeds)
        t = time.perf_counter()
        r = eng.recall("検査用のログの保存期間", project=P)
        return time.perf_counter() - t, r

    def test_300_worst_case_under_one_second(self):
        for name, make in (("A", _form_a_seeds), ("B", _form_b_seeds)):
            elapsed, _ = self.timed_recall(make())
            self.assertLess(elapsed, 1.0, name)

    def test_sentence_split_once_per_seed(self):
        # 形 B: 圓成實 の 150 個は縁の数字が全て相手に在る (C が空) ので切らない = 切るのは 依他起 の 150 個だけ (§1.7 の 150)
        for make, expected in ((_form_a_seeds, 300), (_form_b_seeds, 150)):
            seeds = make()
            spans = sum(len([e for e in s["evidences"] if e["source"] == "user"]) for s in seeds)
            with mock.patch.object(alaya, "_sentence_pieces", wraps=alaya._sentence_pieces) as spy:
                alaya._value_carriers(seeds)
            self.assertLessEqual(spy.call_count, spans)
            self.assertEqual(spy.call_count, expected)

    def test_no_digit_seeds_skip_split(self):
        """数字の縁を持たない種子は文に切らない。守るのは `_value_carriers` の「数字の縁の無い種子を落とす」行と「C が空の対を飛ばす」行の二重で、片方だけ壊しても落ちない (数え方は test_sentence_split_once_per_seed が C が空の対の飛ばしを単独で守る)。"""
        seeds = _form_c_seeds()
        with mock.patch.object(alaya, "_sentence_pieces", wraps=alaya._sentence_pieces) as spy:
            self.assertEqual(alaya._value_carriers(seeds), set())
        self.assertEqual(spy.call_count, 0)


if __name__ == "__main__":
    unittest.main()
