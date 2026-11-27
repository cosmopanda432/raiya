"""Issue #1 R' / R'-new の決定論 pin (spec 2026-11-27-request-span-reinforce-design §5)。

注入済みの 依他起 の種子 S を、利用者の依頼文 (題だけ) + assistant の答え (値) で強める / 二重に植える形を
記録のみにする。alaya.imprint を直に呼ぶ (LLM なし)。テストが消すのは自作 tmp だけ。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ["RAIYA_CONFIG"] = ""

from raiya import alaya, sansho  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

P = "/proj"

REQ1 = "検査用のログの保存期間 について、いまの決定をそのまま 1 文で復唱して。ファイルは開かずに。"
ECHO1 = "検査用ログの保存期間は30日とする、というのがいまの決定です。"
REQ2 = "検査用の夜間バッチの実行時刻 について、いまの決定をそのまま 1 文で復唱して。ファイルは開かずに。"
ECHO2 = "検査用の夜間バッチは、実行時刻を 3 時にする、という決定が承認されています。"

EX1 = dict(before="検査用のログの保存期間を決めたい", rec="**推奨: 案2（30日）**",
           content="検査用ログの保存期間は30日を採用。", conds=["検査用のログ", "30日"])
EX2 = dict(before="検査用の夜間バッチの実行時刻を決めたい", rec="**推奨: 案2 の 3 時**",
           content="夜間バッチは 3 時を採用。", conds=["夜間バッチ", "3 時"])
# §8.11 の足場 (m_table.py の形)
PY = dict(before="使う python の版を決めたい", rec="**推奨: 案2（python 3.11）**", content="python は 3.11 を採用。", conds=["python", "3.11"])
PY31 = dict(before="使う python の版を決めたい", rec="**推奨: 案2（python 3.1）**", content="python は 3.1 を採用。", conds=["python", "3.1"])
K1000 = dict(before="検査用のログの上限を決めたい", rec="**推奨: 案2（1,000件）**", content="検査用のログの上限は1,000件を採用。", conds=["検査用のログ", "1,000件"])
AN2 = dict(before="検査用の夜間バッチの実行時刻を決めたい", rec="**推奨: 案2 の 3 時**", content="夜間バッチは案2 の 3 時を採用。", conds=["夜間バッチ", "案2", "3 時"])
API = dict(before="使う api の版を決めたい", rec="**推奨: 案2（api v2）**", content="api は v2 を採用。", conds=["api", "v2"])
TWO = dict(before="検査用のログの保存期間を決めたい", rec="**推奨: 案2（30日、延長の上限は60日）**",
           content="検査用ログの保存期間は30日、延長の上限は60日を採用。", conds=["検査用のログ", "30日", "60日"])
PY3 = dict(PY, conds=["python", "3", "3.11"])   # 数字だけの縁 `3` を持つ (§8.13)
K1 = dict(K1000, conds=["検査用のログ", "1", "000", "1,000件"])
API2 = dict(API, conds=["api", "2", "v2"])
RETRY = dict(before="リトライの回数を決めたい", rec="**推奨: 案2（リトライ 3 回）**", content="リトライは 3 回を採用。", conds=["リトライ", "3"])
RETRY2 = dict(before="リトライの回数と間隔を決めたい", rec="**推奨: 案2（リトライ 3 回・間隔 30秒）**",
              content="リトライは 3 回、間隔は30秒を採用。", conds=["リトライ", "3", "30秒"])
S3K = dict(before="s3 のログの保存期間を決めたい", rec="**推奨: 案2（s3 は 30日）**", content="s3 のログの保存期間は30日を採用。", conds=["s3", "30日"])


def _turn(user, asst, i):
    return LedgerTurn(user_text=user, assistant_text=asst, id=i, project=P)


class _Base(unittest.TestCase):
    def setUp(self):
        self.store, self.sid = self._plant(EX1)

    def _plant(self, ex):
        """S を承認の対で植える (決定 / 依他起 / 強度 2)。次の life を始めて返す。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = alaya_store_at(os.path.join(tmp, "alaya.json"), os.path.join(tmp, "imprint_log.json"))
        store.begin_new_life()
        w1 = [_turn(ex["before"], "3 案あります。" + ex["rec"], 1), _turn("GO", "了解", 2)]
        r1 = store.imprint(
            w1,
            {"content": ex["content"], "conditions": ex["conds"], "polarity": "無記", "kind": "決定", "action": "new",
             "evidence": [{"source": "assistant", "span": ex["rec"]}, {"source": "user", "span": "GO"}]},
            injected_by_turn={}, session_by_turn=["s1", "s1"])
        self.assertEqual((r1["kind"], r1["sansho"], r1["seed"]["strength"]), ("決定", "依他起", 2), r1["reason"])
        sid = r1["seed"]["id"]
        self.assertTrue(alaya.approval_only(store.get_by_ids([sid])[0]))
        store.begin_new_life()
        return store, sid

    def _prop(self, user_span, asst_span, conds, content="検査用ログの保存期間は30日を採用。", sid=None, **kw):
        p = {"content": content, "conditions": list(conds), "polarity": "無記", "kind": "決定",
             "evidence": [{"source": "user", "span": user_span}, {"source": "assistant", "span": asst_span}]}
        if sid is not None:
            p.update({"action": "reinforce", "target_id": sid})
        p.update(kw)
        return p

    def _run(self, user, asst, proposal, injected="both", store=None, sid=None, window=None, **kw):
        store = store or self.store
        sid = sid or self.sid
        w = window or [_turn(user, asst, 4), _turn("GO", "了解しました", 5)]
        if injected == "both":
            injected = {0: {sid}, 1: {sid}}
        return store.imprint(w, proposal, injected_by_turn=injected, session_by_turn=["s3", "s3"], **kw)

    def _case(self, ex, user, asst, conds, content, reinforce=False, action="new"):
        """足場 ex の S を植え、user / asst の往復の提案 (action new か reinforce) を imprint して (r, store, sid) を返す。"""
        store, sid = self._plant(ex)
        kw = {"sid": sid} if reinforce else {"action": action}
        r = self._run(user, asst, self._prop(user, asst, conds, content=content or "検査用ログの保存期間は30日を採用。", **kw), store=store, sid=sid)
        return r, store, sid

    def strength(self, store=None, sid=None):
        return (store or self.store).get_by_ids([sid or self.sid])[0]["strength"]


class TestRequestEchoReinforce(_Base):
    def test_request_echo_reinforce_is_record_only(self):  # 例 1
        r = self._run(REQ1, ECHO1, self._prop(REQ1, ECHO1, ["検査用のログ", "30日"], sid=self.sid))
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIsNone(r["seed"])
        self.assertIsNone(r["is_new"])
        self.assertIn("R':", r["reason"])
        self.assertIn("30日", r["reason"])
        self.assertEqual(self.strength(), 2)
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertTrue(alaya.approval_only(self.store.get_by_ids([self.sid])[0]))
        self.assertEqual(self.store.list_imprint_log()[-1]["seed_id"], self.sid)

    def test_request_echo_one_shared_condition_is_record_only(self):  # 例 2
        store, sid = self._plant(EX2)
        p = self._prop(REQ2, ECHO2, ["夜間バッチ", "実行時刻"], content="夜間バッチは実行時刻を3時にする決定を採用。", sid=sid)
        r = self._run(REQ2, ECHO2, p, store=store, sid=sid)
        self.assertEqual(r["echo_match"], sid)
        self.assertIn("3 時", r["reason"])
        self.assertIn("user の span は値を言っていない", r["reason"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(self.strength(store, sid), 2)

    def test_value_in_question_only_is_record_only(self):
        user = "検査用のログの保存期間 について。30日だっけ？"
        asst = "はい、検査用のログの保存期間は30日です。"
        r = self._run(user, asst, self._prop(user, asst, ["検査用のログ", "30日"], sid=self.sid))
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIn("R':", r["reason"])

    def test_reconfirm_without_value_is_record_only(self):  # §3.6 の受け入れる取りこぼし
        user = "検査用のログは前のとおりで進めて。"
        asst = "検査用のログの保存期間は30日のままで進めます。"
        r = self._run(user, asst, self._prop(user, asst, ["検査用のログ", "30日"], sid=self.sid))
        self.assertEqual(r["echo_match"], self.sid)
        self.assertEqual(self.strength(), 2)

    def test_merge_path_is_record_only(self):
        r = self._run(REQ1, ECHO1, self._prop(REQ1, ECHO1, ["検査用のログ", "30日"], content=EX1["content"], action="new"))
        self.assertEqual(r["echo_match"], self.sid)
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.strength(), 2)

    def test_user_restates_value_reinforces(self):
        user = "検査用のログの保存期間は30日で行く。"
        asst = "検査用のログは30日で進めます。"
        r = self._run(user, asst, self._prop(user, asst, ["検査用のログ", "30日"], sid=self.sid))
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertEqual(self.strength(), 3)
        self.assertNotIn("echo_match", r)

    def test_partial_coverage_reinforces(self):  # e2、本番 648 型
        ex = dict(before="公開の手順を決めたい", rec="**推奨: 案2（履歴整理・公開・動作確認）**",
                  content="履歴を整理して公開し動作確認する。", conds=["履歴", "公開", "動作確認"])
        store, sid = self._plant(ex)
        user, asst = "リポジトリを公開にして", "履歴を消してから公開しました"
        p = self._prop(user, asst, ["履歴", "公開"], content="履歴を消して公開する。", sid=sid)
        r = self._run(user, asst, p, store=store, sid=sid)
        self.assertTrue(r.get("reinforced"), r["reason"])
        self.assertNotIn("echo_match", r)

    def test_user_brings_new_condition_reinforces(self):  # e4、本番 536 型
        user = "検査用のログの保存期間は延長しない。監査用は別にする。"
        asst = "検査用ログの保存期間は30日のままです。"
        p = self._prop(user, asst, ["検査用のログ", "30日", "監査用"], sid=self.sid)
        r = self._run(user, asst, p)
        self.assertNotIn("echo_match", r, r["reason"])

    def test_not_injected_reinforces(self):
        for injected in (None, {}, {0: {999}, 1: {999}}):
            with self.subTest(injected=injected):
                self.setUp()
                r = self._run(REQ1, ECHO1, self._prop(REQ1, ECHO1, ["検査用のログ", "30日"], sid=self.sid), injected=injected)
                self.assertTrue(r.get("reinforced"), r["reason"])
                self.assertEqual(self.strength(), 3)

    def test_injected_only_at_user_turn_reinforces(self):
        w = [_turn(REQ1, "了解", 4), _turn("続き", ECHO1, 5)]
        r = self._run(REQ1, ECHO1, self._prop(REQ1, ECHO1, ["検査用のログ", "30日"], sid=self.sid),
                      injected={0: {self.sid}}, window=w)
        self.assertNotIn("echo_match", r, r["reason"])

    def test_replacement_for_is_excluded(self):
        r = self._run(REQ1, ECHO1, self._prop(REQ1, ECHO1, ["検査用のログ", "30日"], sid=self.sid), replacement_for=99)
        self.assertNotIn("echo_match", r)

    def test_approval_pair_proposal_keeps_r_note(self):
        store, sid = self._plant(EX2)
        p = {"content": "夜間バッチは 3 時を採用。", "conditions": ["夜間バッチ", "3 時"], "polarity": "無記", "kind": "決定",
             "action": "reinforce", "target_id": sid,
             "evidence": [{"source": "assistant", "span": ECHO2}, {"source": "user", "span": "GO"}]}
        r = self._run(REQ2, ECHO2, p, store=store, sid=sid)
        self.assertEqual(r["echo_match"], sid)
        self.assertIn("R:", r["reason"])
        self.assertNotIn("R':", r["reason"])

    def test_injected_echo_values_pure(self):
        s = self.store.get_by_ids([self.sid])[0]
        w = sansho.as_window([_turn(REQ1, ECHO1, 4), _turn("GO", "了解しました", 5)])
        d = sansho.evaluate_proposal(self._prop(REQ1, ECHO1, ["検査用のログ", "30日"], sid=self.sid), w)
        c1 = "検査用ログの保存期間は30日を採用。"
        self.assertEqual(alaya._injected_echo_values(s, d["spans"], d["grounded_conditions"], w, c1), ["30日"])
        self.assertEqual(alaya._injected_echo_values(s, d["spans"], d["grounded_conditions"], w, "保存期間の決定を確認した。"), [])  # e5
        self.assertEqual(alaya._injected_echo_values(s, d["spans"], d["grounded_conditions"], w, None), [])
        a_only = [sp for sp in d["spans"] if sp.get("sansho") != "圓成實"]
        self.assertEqual(alaya._injected_echo_values(s, a_only, d["grounded_conditions"], w, c1), [])
        store2, sid2 = self._plant(EX2)
        s2 = store2.get_by_ids([sid2])[0]
        a2 = [{"sansho": "依他起", "span": ECHO2, "turn_index": 0}]
        # V = [夜間バッチ, 3 時] の 夜間バッチ に ASCII の連が無い = e7
        self.assertEqual(alaya._injected_echo_values(s2, a2, [], w, "夜間バッチは 3 時を採用。"), [])
        self.assertEqual(alaya._injected_echo_values(dict(s, conditions=[]), d["spans"], d["grounded_conditions"], w, c1), [])

    # --- §5 末尾「使う人に効きそうな入力」の固定 ---
    def test_paraphrased_echo_not_covered(self):  # §3.7 (a)
        user = "保存期間 について、いまの決定をそのまま 1 文で復唱して。"
        asst = "検査用ログの保存期間は30日です。"
        r = self._run(user, asst, self._prop(user, asst, ["保存期間", "30日"], sid=self.sid))
        self.assertEqual(r["kind"], "決定")
        self.assertNotIn("R':", r["reason"])  # 検査用のログ が span のどこにも無い = e2 で外れる
        self.assertIs(r["is_new"], True, r["reason"])

    def test_seed_without_value_condition_not_caught(self):  # §3.7 (b)
        ex = dict(before="検査用のログの扱いを決めたい", rec="**推奨: 案2（検査用のログの保存期間は30日）**",
                  content="検査用のログの保存期間を30日にする。", conds=["検査用のログ", "保存期間"])
        store, sid = self._plant(ex)
        user, asst = "検査用のログの保存期間 について、いまの決定を復唱して。", "検査用のログの保存期間は30日です。"
        r = self._run(user, asst, self._prop(user, asst, ["検査用のログ", "保存期間"], sid=sid), store=store, sid=sid)
        self.assertNotIn("R':", r["reason"])

    def test_substring_condition_counts(self):  # §3.7 (c)
        seed = {"conditions": ["git"]}
        w = sansho.as_window([_turn("公開して", "GitHub に公開しました", 4)])
        spans = [{"sansho": "圓成實", "span": "公開して", "turn_index": 0},
                 {"sansho": "依他起", "span": "GitHub に公開しました", "turn_index": 0}]
        self.assertEqual(alaya._injected_echo_values(seed, spans, [], w, "GitHub に公開した。"), ["git"])

    def test_added_ascii_value_echoed_by_answer_reinforces(self):  # §3.7 (d) の閉じた側 (e6)
        user = REQ1 + "あとリトライは 3 回に。"
        asst = ECHO1 + "リトライは 3 回にします。"
        p = self._prop(user, asst, ["検査用のログ", "30日", "リトライ"], sid=self.sid)
        r = self._run(user, asst, p)
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("echo_match", r)

    def test_added_jp_value_echoed_by_answer_is_record_only(self):  # §3.7 (d) の残り
        user = REQ1 + "あとリトライは毎回通知に。"
        asst = ECHO1 + "リトライは毎回通知にします。"
        p = self._prop(user, asst, ["検査用のログ", "30日", "通知"], sid=self.sid)
        r = self._run(user, asst, p)
        self.assertEqual(r["echo_match"], self.sid, r["reason"])

    # --- 検収の重い 1: 値の交代は止めない (§8) ---
    def test_value_change_reinforce_misaction(self):
        ex2 = (EX2, "検査用の夜間バッチの実行時刻は 4 時にして。", "夜間バッチは 3 時から 4 時に変えます。",
               ["夜間バッチ", "4 時"], "夜間バッチは 4 時を採用。")
        ex1 = (EX1, "検査用のログの保存期間を60日に延ばして。", "検査用のログの保存期間を30日から60日に変更します。",
               ["検査用のログ", "60日"], "検査用のログの保存期間は60日。")
        for ex, user, asst, conds, content in (ex2, ex1):
            with self.subTest(conds=conds):
                store, sid = self._plant(ex)
                r = self._run(user, asst, self._prop(user, asst, conds, content=content, sid=sid), store=store, sid=sid)
                self.assertNotIn("echo_match", r, r["reason"])
                self.assertIs(r["is_new"], True, r["reason"])
                self.assertEqual(len(store.list_seeds()), 2)

    def test_value_change_content_mentions_old(self):
        ex2 = (EX2, "検査用の夜間バッチの実行時刻は 4 時にして。", "夜間バッチは 3 時から 4 時に変えます。",
               ["夜間バッチ", "4 時"], "夜間バッチの実行時刻を 3 時から 4 時に変更。")
        ex1 = (EX1, "検査用のログの保存期間を60日に延ばして。", "検査用のログの保存期間を30日から60日に変更します。",
               ["検査用のログ", "60日"], "検査用のログの保存期間は30日から60日に変更。")
        for ex, user, asst, conds, content in (ex2, ex1):
            with self.subTest(conds=conds):
                store, sid = self._plant(ex)
                r = self._run(user, asst, self._prop(user, asst, conds, content=content, action="new"), store=store, sid=sid)
                self.assertIs(r["is_new"], True, r["reason"])
                self.assertEqual(len(store.list_seeds()), 2)

    def _plant_jp(self):
        ex = dict(before="検査用のログの集計を決めたい", rec="**推奨: 案2（日次）**",
                  content="検査用のログの集計は日次を採用。", conds=["検査用のログ", "日次"])
        return self._plant(ex)

    def test_jp_value_change_content_mentions_old(self):  # e7
        store, sid = self._plant_jp()
        user, asst = "検査用のログの集計は週次にして。", "検査用のログの集計を日次から週次に変えます。"
        p = self._prop(user, asst, ["検査用のログ", "週次"], content="検査用のログの集計を日次から週次に変更。", action="new")
        r = self._run(user, asst, p, store=store, sid=sid)
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_jp_valued_echo_not_caught(self):  # L1 の固定 (受け入れる取りこぼし)
        store, sid = self._plant_jp()
        user = "検査用のログの集計 について、いまの決定をそのまま 1 文で復唱して。"
        asst = "検査用のログの集計は日次とする、というのが決定です。"
        p = self._prop(user, asst, ["検査用のログ", "日次"], content="検査用のログの集計は日次を採用。", sid=sid)
        r = self._run(user, asst, p, store=store, sid=sid)
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("R':", r["reason"])

    def test_ascii_to_jp_value_change_old_in_content_is_record_only(self):  # L2 の固定 (受け入れる取りこぼし)
        store, sid = self._plant(EX2)
        user, asst = "検査用の夜間バッチの実行時刻は夕方にして。", "夜間バッチは 3 時から夕方に変えます。"
        p = self._prop(user, asst, ["夜間バッチ", "夕方"], content="夜間バッチの実行時刻を 3 時から夕方に変更。", action="new")
        r = self._run(user, asst, p, store=store, sid=sid)
        self.assertEqual(r["echo_match"], sid, r["reason"])

    def test_ascii_to_jp_value_change_reinforce_planted(self):  # e5 だけで外れる形 (e6 は共有する連が無く立たない)
        store, sid = self._plant(EX2)
        user, asst = "検査用の夜間バッチの実行時刻は夕方にして。", "夜間バッチは 3 時から夕方に変えます。"
        p = self._prop(user, asst, ["夜間バッチ", "夕方"], content="夜間バッチは夕方を採用。", sid=sid)
        r = self._run(user, asst, p, store=store, sid=sid)
        self.assertNotIn("echo_match", r, r["reason"])
        self.assertIs(r["is_new"], True, r["reason"])

    # --- 再検収 中-1 / §8.10: e6 の S の側は縁の連だけ ---
    def test_value_change_new_run_in_seed_content_planted(self):  # 中-1 例 1・例 2 (S の content に新しい値の連が在る)
        ex_a = dict(EX1, content="検査用のログの保存期間は30日、延長の上限は60日を採用。")
        ex_b = dict(EX1, content="検査用のログの保存期間は案2の30日を採用。")
        cases = (
            (ex_a, "検査用のログの保存期間を60日にして。", "検査用のログの保存期間は30日から60日に変えます。",
             ["検査用のログ", "60日"], "検査用のログの保存期間は30日から60日に変更。"),
            (ex_b, "検査用のログの保存期間を2日にして。", "検査用のログの保存期間は30日から2日に変えます。",
             ["検査用のログ", "2日"], "検査用のログの保存期間は30日から2日に変更。"),
        )
        for ex, user, asst, conds, content in cases:
            with self.subTest(user=user):
                store, sid = self._plant(ex)
                r = self._run(user, asst, self._prop(user, asst, conds, content=content, action="new"), store=store, sid=sid)
                self.assertIs(r["is_new"], True, r["reason"])
                self.assertNotIn("echo_match", r)
                self.assertEqual(len(store.list_seeds()), 2)

    def test_request_names_content_only_token_not_caught(self):  # L6 の固定 (受け入れる取りこぼし)
        ex = dict(EX1, content="検査用ログの保存期間は案2の30日を採用。")
        store, sid = self._plant(ex)
        user = "検査用のログの保存期間 は案2 のままで、復唱して。"
        asst = "検査用のログの保存期間は案2 の30日です。"
        r = self._run(user, asst, self._prop(user, asst, ["検査用のログ", "30日"], action="new"), store=store, sid=sid)
        self.assertNotIn("R':", r["reason"])
        self.assertNotIn("echo_match", r)

    def _value_change_ex2(self, user):
        store, sid = self._plant(EX2)
        asst = "夜間バッチは 3 時から 4 時に変えます。"
        p = self._prop(user, asst, ["夜間バッチ", "4 時"], content="夜間バッチの実行時刻を 3 時から 4 時に変更。", action="new")
        return self._run(user, asst, p, store=store, sid=sid), store

    def test_e6_reads_question_sentence(self):  # e6 の user 側は疑の文を含む全文
        r, store = self._value_change_ex2("検査用の夜間バッチの実行時刻 の件。4 時にできる？")
        self.assertEqual(r["kind"], "決定")
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_fullwidth_value_change_planted(self):  # NFKC (全角の数字も連になる)
        r, store = self._value_change_ex2("検査用の夜間バッチの実行時刻は ４ 時にして。")
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_e6_seed_side_excludes_evidences(self):
        ex = dict(before="検査用のログの上限を決めたい", rec="**推奨: 案2（1,000件）**",
                  content="検査用のログの上限は1,000件を採用。", conds=["検査用のログ", "1,000件"])
        store, sid = self._plant(ex)
        user = "検査用のログの上限は2,000件にして。"
        asst = "検査用のログの上限は1,000件から2,000件に変えます。"
        p = self._prop(user, asst, ["検査用のログ", "2,000件"], content="検査用のログの上限は1,000件から2,000件に変更。", action="new")
        r = self._run(user, asst, p, store=store, sid=sid)
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    # --- §8.11 e6 (縁の出現を塗る) ---
    def test_value_change_new_run_in_seed_conditions_planted(self):  # 三度目の中-1 (a)〜(d)
        cases = (
            ("R3a new", EX1, "検査用のログの保存期間は30時間にして。", "検査用のログの保存期間を30日から30時間に変えます。",
             ["検査用のログ", "30時間"], "検査用のログの保存期間を30日から30時間に変更。", False),
            ("R3a' reinforce", EX1, "検査用のログの保存期間は30時間にして。", "検査用のログの保存期間を30日から30時間に変えます。",
             ["検査用のログ", "30時間"], "検査用のログの保存期間を30日から30時間に変更。", True),
            ("R3b", AN2, "検査用の夜間バッチの実行時刻は 2 時にして。", "夜間バッチは案2 の 3 時から 2 時に変えます。",
             ["夜間バッチ", "2 時"], "夜間バッチの実行時刻を案2 の 3 時から 2 時に変更。", False),
            ("R3c", K1000, "検査用のログの上限は1,000,000件にして。", "検査用のログの上限を1,000件から1,000,000件に変えます。",
             ["検査用のログ", "1,000,000件"], "検査用のログの上限を1,000件から1,000,000件に変更。", False),
            ("R3d", PY, "python は 3.3 にして。", "python を 3.11 から 3.3 に下げます。",
             ["python", "3.3"], "python を 3.11 から 3.3 に変更。", False),
        )
        for name, ex, user, asst, conds, content, reinforce in cases:
            with self.subTest(name):
                r, store, _ = self._case(ex, user, asst, conds, content, reinforce)
                self.assertIs(r["is_new"], True, r["reason"])
                self.assertNotIn("echo_match", r)
                self.assertEqual(len(store.list_seeds()), 2)

    def test_value_change_unit_only_planted(self):  # C1 (30日 -> 30分)
        r, store, _ = self._case(EX1, "検査用のログの保存期間は30分にして。", "検査用のログの保存期間を30日から30分に変えます。",
                                 ["検査用のログ", "30分"], "検査用のログの保存期間を30日から30分に変更。")
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_value_change_extending_condition_in_question_planted(self):  # C11・C12・C13・C15 (問いの形。境の条件)
        cases = (
            ("C11", EX1, "検査用のログの保存期間 の件。130日にできる？", "検査用のログの保存期間を30日から130日に変えます。",
             ["検査用のログ", "130日"], "検査用のログの保存期間を30日から130日に変更。"),
            ("C12", EX2, "検査用の夜間バッチの実行時刻 の件。3 時半にできる？", "夜間バッチは 3 時から 3 時半に変えます。",
             ["夜間バッチ", "3 時半"], "夜間バッチの実行時刻を 3 時から 3 時半に変更。"),
            ("C13", PY31, "python の版 の件。3.10 にできる？", "python を 3.1 から 3.10 に上げます。",
             ["python", "3.10"], "python を 3.1 から 3.10 に変更。"),
            ("C15", EX1, "検査用のログの保存期間 の件。30日間にできる？", "検査用のログの保存期間を30日から30日間に変えます。",
             ["検査用のログ", "30日間"], "検査用のログの保存期間を30日から30日間に変更。"),
        )
        for name, ex, user, asst, conds, content in cases:
            with self.subTest(name):
                r, store, _ = self._case(ex, user, asst, conds, content)
                self.assertEqual(r["kind"], "決定")
                self.assertIs(r["is_new"], True, r["reason"])
                self.assertEqual(len(store.list_seeds()), 2)

    def test_request_number_outside_conditions_is_record_only(self):  # X1 (assistant 側も塗る)
        user = "検査用のログの保存期間 について、いまの決定を 30 字以内で復唱して。"
        r, _, sid = self._case(EX1, user, ECHO1, ["検査用のログ", "30日"], None, reinforce=True)
        self.assertEqual(r["echo_match"], sid)
        self.assertIn("R':", r["reason"])

    def test_request_and_answer_share_number_outside_conditions_not_caught(self):  # L3' の固定 (C16・受け入れる取りこぼし)
        user = "検査用のログの保存期間 について、いまの決定を 30 字以内で復唱して。"
        asst = "30 字以内で言うと、検査用ログの保存期間は30日です。"
        r, _, _ = self._case(EX1, user, asst, ["検査用のログ", "30日"], None, reinforce=True)
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("echo_match", r)

    def test_e6_casefold_assistant_side(self):  # C9' (軽い 3)
        r, store, _ = self._case(API, "api は v3 にして。", "API を V2 から V3 に上げます。", ["api", "v3"], "api を v2 から v3 に変更。")
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_mask_conditions_pure(self):
        m = alaya._mask_conditions
        self.assertEqual(m("130日にできる？", ["30日"]), "130日にできる？")
        self.assertEqual(m("30日だっけ？", ["30日"]), "   だっけ？")
        self.assertEqual(m("3時半", ["3時"]), "3時半")
        self.assertEqual(m("案2の3時", ["案2", "3時"]), "  の  ")
        self.assertEqual(m("3.110", ["3.11"]), "3.110")              # 右の ASCII の境
        self.assertEqual(m("30日と30日", ["30日"]), "   と   ")        # すべての出現を塗る
        self.assertEqual(m("3.11", ["3", "3.11"]), "    ")             # 位置は塗る前の文字列で決める

    # --- §8.13: 数字だけの縁は e6 で塗らない ---
    def test_value_change_in_question_with_digit_only_conditions_planted(self):
        cases = (
            ("3.3 new", PY3, "python の版 の件。3.3にできる？", "python の版を3.11から3.3に変えます。",
             ["python", "3.3"], "python の版を 3.11 から 3.3 に変更。", False),
            ("3.3 reinforce", PY3, "python の版 の件。3.3にできる？", "python の版を3.11から3.3に変えます。",
             ["python", "3.3"], "python の版を 3.11 から 3.3 に変更。", True),
            ("1,000,000件", K1, "検査用のログの上限 の件。1,000,000件にできる？", "検査用のログの上限を1,000件から1,000,000件に変えます。",
             ["検査用のログ", "1,000,000件"], "検査用のログの上限を1,000件から1,000,000件に変更。", False),
            ("v2.2", API2, "api の版 の件。v2.2 にできる？", "api を v2 から v2.2 に上げます。",
             ["api", "v2.2"], "api を v2 から v2.2 に変更。", False),
        )
        for name, ex, user, asst, conds, content, reinforce in cases:
            with self.subTest(name):
                r, store, _ = self._case(ex, user, asst, conds, content, reinforce)
                self.assertEqual(r["kind"], "決定")
                self.assertIs(r["is_new"], True, r["reason"])
                self.assertEqual(len(store.list_seeds()), 2)

    def test_digit_only_condition_request_echo_is_record_only(self):  # 数字だけの縁の種子への普通の復唱の請は止める
        user = "リトライの回数 について、いまの決定を復唱して。"
        asst = "リトライは 3 回です。"
        r, _, sid = self._case(RETRY, user, asst, ["リトライ", "3"], "リトライは 3 回を採用。", reinforce=True)
        self.assertEqual(r["echo_match"], sid, r["reason"])

    def test_digit_only_condition_said_by_both_not_caught(self):  # L11 の固定 (受け入れる取りこぼし)
        user = "リトライ について、いまの決定を 3 行で復唱して。"
        asst = "3 行で言うと、リトライは 3 回です。"
        r, _, _ = self._case(RETRY, user, asst, ["リトライ", "3"], "リトライは 3 回を採用。", reinforce=True)
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("echo_match", r)

    def test_digit_only_condition_named_by_user_with_other_value_reinforces(self):  # e8 は数字だけの縁も見る (軽い 3)
        user = "リトライは 3 回のままで。間隔 はどうなってる？"
        asst = "リトライの間隔は30秒です。"
        r, _, _ = self._case(RETRY2, user, asst, ["リトライ", "30秒"], "リトライの間隔は30秒を採用。", reinforce=True)
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("R':", r["reason"])

    def test_e6_masks_user_side(self):  # user 側の塗り (軽い 4): 利用者が縁を問いの文で言い、答えが数字を縁の外でも言う
        user = "検査用のログの保存期間 の件。30日だっけ？"
        asst = "30 字以内で言うと、検査用のログの保存期間は30日です。"
        r, _, sid = self._case(EX1, user, asst, ["検査用のログ", "30日"], None, reinforce=True)
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["echo_match"], sid, r["reason"])

    # --- §8.12 e8: 数字を含む S の縁が利用者の言に出ていれば止めない ---
    def test_two_valued_seed_value_named_by_user_reinforces(self):  # L8a・L8b (HEAD と同じ = S への reinforce)
        user = "検査用のログの保存期間を60日にして。"
        asst = "検査用のログの保存期間を30日から60日に変えます。"
        for name, reinforce in (("L8a new", False), ("L8b reinforce", True)):
            with self.subTest(name):
                r, store, _ = self._case(TWO, user, asst, ["検査用のログ", "60日"], "検査用のログの保存期間を30日から60日に変更。", reinforce)
                self.assertTrue(r["reinforced"], r["reason"])
                self.assertNotIn("R':", r["reason"])
                self.assertEqual(len(store.list_seeds()), 1)

    def test_english_topic_request_echo_is_record_only(self):  # PYQ・PYQ2 (数字を含まない題 python は e8 の対象外)
        asst = "python は 3.11 を採用、というのがいまの決定です。"
        for name, user, reinforce in (("PYQ", "python は何だっけ、復唱して。", True),
                                      ("PYQ2", "python の版 について、いまの決定を復唱して。", False)):
            with self.subTest(name):
                r, _, sid = self._case(PY, user, asst, ["python", "3.11"], "python は 3.11 を採用。", reinforce)
                self.assertEqual(r["echo_match"], sid, r["reason"])

    def test_two_valued_seed_request_echo_is_record_only(self):  # TWOQ
        user = "検査用のログの保存期間 について、いまの決定を復唱して。"
        asst = "検査用ログの保存期間は30日、延長の上限は60日です。"
        r, _, sid = self._case(TWO, user, asst, ["検査用のログ", "30日"], "検査用ログの保存期間は30日、延長の上限は60日を採用。", reinforce=True)
        self.assertEqual(r["echo_match"], sid, r["reason"])

    def test_digit_topic_request_not_caught(self):  # S3Q (L10 の固定・受け入れる取りこぼし)
        user = "s3 のログの保存期間 について、いまの決定を復唱して。"
        asst = "s3 のログの保存期間は30日です。"
        r, _, _ = self._case(S3K, user, asst, ["s3", "30日"], "s3 のログの保存期間は30日を採用。", reinforce=True)
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("echo_match", r)

    def test_answer_repeats_request_number_not_caught(self):  # L3
        asst = "1 文で言うと、検査用ログの保存期間は30日です。"
        r = self._run(REQ1, asst, self._prop(REQ1, asst, ["検査用のログ", "30日"], sid=self.sid))
        self.assertTrue(r["reinforced"], r["reason"])
        self.assertNotIn("echo_match", r)


class TestRequestEchoNew(_Base):
    def setUp(self):
        super().setUp()
        self.store2, self.sid2 = self._plant(EX2)

    def _r2(self, proposal, **kw):
        return self._run(REQ2, ECHO2, proposal, store=self.store2, sid=self.sid2, **kw)

    def test_new_action_request_echo_not_planted(self):
        p = self._prop(REQ2, ECHO2, ["夜間バッチ", "実行時刻"], content="夜間バッチは実行時刻を3時にする決定を採用。", action="new")
        r = self._r2(p)
        self.assertEqual(r["echo_match"], self.sid2)
        self.assertIn("R':", r["reason"])
        self.assertEqual(len(self.store2.list_seeds()), 1)

    def test_supersede_fallback_not_planted(self):
        p = self._prop(REQ2, ECHO2, ["夜間バッチ", "実行時刻"], content="夜間バッチは実行時刻を3時にする決定を採用。",
                       action="supersede", target_id=999)
        r = self._r2(p)
        self.assertEqual(r["echo_match"], self.sid2)
        self.assertEqual(len(self.store2.list_seeds()), 1)

    def test_value_change_planted(self):  # e5
        user, asst = "検査用の夜間バッチの実行時刻は 4 時にして。", "夜間バッチは 3 時から 4 時に変えます。"
        p = self._prop(user, asst, ["夜間バッチ", "4 時"], content="夜間バッチは 4 時を採用。", action="new")
        r = self._run(user, asst, p, store=self.store2, sid=self.sid2)
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(self.store2.list_seeds()), 2)

    def test_enjoujitsu_seed_untouched(self):  # §6 問い 4
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = alaya_store_at(os.path.join(tmp, "alaya.json"), os.path.join(tmp, "imprint_log.json"))
        store.begin_new_life()
        w1 = [_turn("夜間バッチは 3 時にする。", "了解", 1)]
        r1 = store.imprint(w1, {"content": "夜間バッチは 3 時を採用。", "conditions": ["夜間バッチ", "3 時"], "polarity": "無記",
                                "kind": "決定", "action": "new",
                                "evidence": [{"source": "user", "span": "夜間バッチは 3 時にする。"}]},
                           injected_by_turn={}, session_by_turn=["s1"])
        self.assertEqual((r1["kind"], r1["sansho"]), ("決定", "圓成實"), r1["reason"])
        sid = r1["seed"]["id"]
        store.begin_new_life()
        p = self._prop(REQ2, ECHO2, ["夜間バッチ", "実行時刻"], content="夜間バッチは実行時刻を3時にする決定を採用。", action="new")
        r = self._run(REQ2, ECHO2, p, store=store, sid=sid)
        self.assertNotIn("echo_match", r, r["reason"])
        self.assertIs(r["is_new"], True)

    def _new2(self):
        return self._prop(REQ2, ECHO2, ["夜間バッチ", "実行時刻"], content="夜間バッチは実行時刻を3時にする決定を採用。", action="new")

    def test_new_action_not_injected_planted(self):
        r = self._r2(self._new2(), injected={0: {999}, 1: {999}})
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(self.store2.list_seeds()), 2)

    def test_new_action_withdrawn_seed_planted(self):
        self.store2.withdraw(self.sid2, {"by": "human", "at": "2026-10-25T00:00:00+00:00", "life": 3,
                                         "reason": "r", "replaced_by": None})
        r = self._r2(self._new2())
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(self.store2.list_seeds()), 2)

    def test_new_action_other_project_seed_planted(self):  # §8.5
        w = [LedgerTurn(user_text=REQ2, assistant_text=ECHO2, id=4, project="/other"),
             LedgerTurn(user_text="GO", assistant_text="了解しました", id=5, project="/other")]
        r = self._r2(self._new2(), window=w)
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(r["project"], "/other")
        self.assertEqual(len(self.store2.list_seeds()), 2)

    def test_new_action_other_kind_seed_planted(self):  # §8.5 (kind)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = alaya_store_at(os.path.join(tmp, "alaya.json"), os.path.join(tmp, "imprint_log.json"))
        store.begin_new_life()
        r1 = store.imprint(
            [_turn(EX2["before"], "3 案あります。" + EX2["rec"], 1)],
            {"content": EX2["content"], "conditions": EX2["conds"], "polarity": "無記", "kind": "性向",
             "evidence": [{"source": "assistant", "span": EX2["rec"]}]},
            injected_by_turn={}, session_by_turn=["s1"])
        self.assertEqual((r1["kind"], r1["sansho"]), ("性向", "依他起"), r1["reason"])
        sid = r1["seed"]["id"]
        store.begin_new_life()
        r = self._run(REQ2, ECHO2, self._new2(), store=store, sid=sid)
        self.assertEqual(r["kind"], "決定")
        self.assertIs(r["is_new"], True, r["reason"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_disposition_kind_untouched(self):
        p = self._prop(REQ2, ECHO2, ["夜間バッチ", "実行時刻"], content="夜間バッチは実行時刻を3時にする決定を採用。",
                       action="new", kind="性向")
        r = self._r2(p)
        self.assertNotIn("R':", r.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
