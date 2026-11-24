"""#105 R の決定論 pin: 同 session で注入済みの種子を assistant だけが言い直しても強めない (記録のみ)。

alaya.imprint の `injected_by_turn` キーワード (None = R off) と、engine.consolidate が manas_log から
それを作る端から端まで。LLM は backend.call_llm を patch。テストが消すのは自作 tmp だけ。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya  # noqa: E402
from raiya import engine as engine_mod  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, span, **kw):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": "所在",
         "evidence": [{"source": "assistant", "span": span}]}
    p.update(kw)
    return p


class TestAlayaR(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()
        r = self.store.imprint([_turn("質問 配置", "設定は config.yaml に置く 配置")],
                               _prop("設定は config.yaml に置く", ["配置"], "設定は config.yaml に置く"))
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.sid = r["seed"]["id"]
        self.window = [_turn("続き 配置", "設定は config.yaml に置くのが良い 配置")]

    def _echo(self, injected, explicit):
        kw = {"action": "reinforce", "target_id": self.sid} if explicit else {}
        p = _prop("設定は config.yaml に置く", ["配置"], "設定は config.yaml に置くのが良い", **kw)
        return self.store.imprint(self.window, p, injected_by_turn=injected)

    def strength(self):
        return self.store.get_by_ids([self.sid])[0]["strength"]

    def test_injected_echo_is_recorded_only_both_paths(self):
        for explicit in (True, False):
            r = self._echo({0: {self.sid}}, explicit)
            self.assertIsNone(r["seed"], explicit)
            self.assertIsNone(r["is_new"], explicit)
            self.assertEqual(r["echo_match"], self.sid, explicit)
            self.assertNotIn("reinforced", r)
            self.assertIn("R:", r["reason"])
            self.assertEqual(self.strength(), 1)
            self.assertEqual(self.store.list_imprint_log()[-1]["seed_id"], self.sid)
            self.assertIn("R:", self.store.list_imprint_log()[-1]["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_not_injected_or_other_seed_or_keyword_absent_reinforces(self):
        for injected in (None, {}, {0: {999}}, {1: {self.sid}}):
            for explicit in (True, False):
                before = self.strength()
                r = self._echo(injected, explicit)
                self.assertTrue(r.get("reinforced"), (injected, explicit))
                self.assertEqual(self.strength(), before + 1)

    def test_user_span_proposal_is_not_blocked(self):
        w = [_turn("設定は config.yaml に置く 配置")]
        p = _prop("設定は config.yaml に置く", ["配置"], "設定は config.yaml に置く", action="reinforce", target_id=self.sid)
        p["evidence"][0]["source"] = "user"
        r = self.store.imprint(w, p, injected_by_turn={0: {self.sid}})
        self.assertNotIn("echo_match", r)

    def test_new_planting_is_not_blocked(self):
        p = _prop("全然別の話 ネットワーク", ["ネットワーク"], "ネットワーク")
        w = [_turn("質問", "ネットワーク の話 ネットワーク")]
        r = self.store.imprint(w, p, injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])


class TestEc2(unittest.TestCase):
    """#119 EC2' (spec 2026-10-31-resume-point-and-kind-name-design §7): user の span が形を担わず、assistant の span が
    注入済みの種子と縁を共有する提案は、新規に植えず記録のみ (echo_match)。"""

    USER_REQ = "前に決めたことを全部確認して。"
    ASSIST = "✅ **決定内容**: 設定ファイルを `config/` ディレクトリに配置する（強度1）"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()
        r = self.store.imprint(
            [_turn("設定ファイルは config/ に置く。")],
            {"content": "設定ファイルは config/ に置く", "conditions": ["設定ファイル", "config"], "polarity": "無記",
             "kind": "所在", "evidence": [{"source": "user", "span": "設定ファイルは config/ に置く。"}]})
        self.assertEqual(r["seed"]["sansho"], "圓成實")
        self.sid = r["seed"]["id"]

    def _propose(self, user_span=None, conds=("設定ファイル", "config"), kind="決定", **kw):
        user_span = self.USER_REQ if user_span is None else user_span
        window = [_turn(user_span, self.ASSIST, ledger_id=1)]
        p = {"content": "設定ファイルを config/ に配置する", "conditions": list(conds), "polarity": "無記", "kind": kind,
             "evidence": [{"source": "user", "span": user_span}, {"source": "assistant", "span": self.ASSIST}]}
        return self.store.imprint(window, p, **kw)

    def test_d2_6_request_plus_echo_is_recorded_only(self):
        r = self._propose(injected_by_turn={0: {self.sid}})
        self.assertIsNone(r["is_new"])
        self.assertIsNone(r["seed"])
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIn("EC2'", r["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.store.list_seeds()[0]["strength"], 1)
        last = self.store.list_imprint_log()[-1]
        self.assertEqual(last["seed_id"], self.sid)
        self.assertIn("EC2'", last["reason"])

    def test_question_user_span_is_recorded_only_ec6(self):
        # EC6: 「どこ？」は疑の文で残りが "" → 形を担わない (旧: 縁を含むので形ありとして植わっていた)
        r = self._propose(user_span="config の設定ファイルはどこ？", injected_by_turn={0: {self.sid}})
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIn("EC2'", r["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_user_span_with_form_plants_normally(self):
        r = self._propose(user_span="config/ の設定ファイルはここに置く。", kind="所在", injected_by_turn={0: {self.sid}})
        self.assertEqual(r["kind"], "所在")
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)

    # --- EC6 (#119, spec §9): (i) は疑の文を除いた残りで判定 ---

    DB_ECHO = "| **DB** | SQLite | 1 |"

    def _plant_db(self):
        r = self.store.imprint(
            [_turn("DB は SQLite に決めた。", ledger_id=5)],
            {"content": "DB は SQLite", "conditions": ["DB", "SQLite"], "polarity": "無記", "kind": "性向",
             "evidence": [{"source": "user", "span": "DB は SQLite に決めた。"}]})
        self.assertIsNotNone(r["seed"])
        return r["seed"]["id"]

    def _db_propose(self, window_user, user_span, injected, kind="決定", assistant=None):
        assistant = self.DB_ECHO if assistant is None else assistant
        window = [_turn(window_user, assistant, ledger_id=1)]
        ev = ([{"source": "user", "span": user_span}] if user_span else []) + [{"source": "assistant", "span": assistant}]
        p = {"content": "DB は SQLite", "conditions": ["DB", "SQLite"], "polarity": "無記", "kind": kind, "evidence": ev}
        return self.store.imprint(window, p, injected_by_turn=injected)

    def test_ec6_a_whole_question_span_is_recorded_only(self):
        sid = self._plant_db()
        q = "DB・キャッシュ・ログ・テスト・API は何に決めた？"
        r = self._db_propose(q, q, {0: {sid}})
        self.assertEqual(r["kind"], "性向")
        self.assertEqual(r["echo_match"], sid)
        self.assertIsNone(r["seed"])

    def test_ec6_b_fragment_of_question_sentence_is_recorded_only(self):
        sid = self._plant_db()
        r = self._db_propose("前に決めた DB と設定の置き場所はどれだった？", "DB", {0: {sid}})
        self.assertEqual(r["echo_match"], sid)

    def test_ec6_b_control_fragment_also_in_plain_sentence_plants(self):
        sid = self._plant_db()
        r = self._db_propose("DB は大事。前に決めた DB はどれだった？", "DB", {0: {sid}}, kind="性向")
        self.assertEqual(r["kind"], "性向")
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)

    def test_ec6_mixed_span_keeps_non_question_sentence(self):
        u = "設定ファイルは config/ に置く。どこだった？"
        r = self._propose(user_span=u, kind="所在", injected_by_turn={0: {self.sid}})
        self.assertEqual(r["kind"], "所在")
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)

    def test_ec6_condition_does_not_match_across_dropped_question(self):
        # 文ごとに渡す: 残りを連結すると「設定」+「ファイル」が問いを挟んで 設定ファイル に一致してしまう
        u = "メモは設定\nどこ？\nファイルにある"
        r = self._propose(user_span=u, kind="性向", injected_by_turn={0: {self.sid}})
        self.assertEqual(r["echo_match"], self.sid)

    def test_ec9_assistant_only_db_echo_now_recorded_like_question_span(self):
        # EC9 (#124, spec 2026-11-05-assistant-only-echo-new-design §8): user の span 0 本の復唱も EC2' の対象 (旧: 新規に植わる穴)
        sid = self._plant_db()
        only = self._db_propose("お願い", None, {0: {sid}})
        self.assertIn("echo_match", only)
        self.assertEqual(only["echo_match"], sid)
        self.assertIsNone(only["is_new"])
        self.setUp()
        sid = self._plant_db()
        q = "前に決めた DB はどれだった？"
        withq = self._db_propose(q, q, {0: {sid}})
        self.assertIn("echo_match", withq)
        self.assertEqual(withq["echo_match"], sid)

    def test_ec6_loop_question_then_request_with_echo_stays_closed(self):
        sid = self._plant_db()
        q = "DB・キャッシュ・ログ・テスト・API は何に決めた？"
        r1 = self._db_propose(q, q, {0: {sid}})
        self.assertEqual(r1["echo_match"], sid)
        r2 = self._db_propose(self.USER_REQ, self.USER_REQ, {0: {sid}})
        self.assertEqual(r2["echo_match"], sid)
        seeds = [sd for sd in self.store.list_seeds() if sd["id"] == sid]
        self.assertEqual(len(self.store.list_seeds()), 2)   # setUp の seed + DB の seed のみ
        self.assertEqual(seeds[0]["strength"], 1)

    def test_l_prime_question_with_condition_is_record_only_even_without_injection(self):
        # spec 2026-10-30-question-as-evidence §9 L': EC6 裁定の「L 不変」は L' で置き換わった。
        # 縁が疑の文の中にしか無い 性向 は、注入が無くても L で記録のみ (EC2' の理由ではない)。
        q = "前に決めた DB はどれだった？"
        window = [_turn(q, "SQLite を使うと決めました", ledger_id=1)]
        p = {"content": "DB は SQLite", "conditions": ["DB"], "polarity": "無記", "kind": "性向",
             "evidence": [{"source": "user", "span": q}]}
        r = self.store.imprint(window, p)
        self.assertEqual(r["decision"], "reject")
        self.assertIn(alaya.DISPOSITION_NEEDS_FORM_IN_USER_SPAN, r["reason"])
        self.assertNotIn("echo_match", r)

    def test_without_injected_by_turn_plants(self):
        for injected in (None, {}, {0: {999}}, {1: {self.sid}}):
            self.setUp()   # 毎回まっさらな store (前の回に植わった種子へ併合されないため。tmp は addCleanup で消える)
            r = self._propose(injected_by_turn=injected)
            self.assertTrue(r["is_new"], injected)
            self.assertEqual(len(self.store.list_seeds()), 2)

    def test_one_shared_condition_of_two_plants(self):
        r = self._propose(conds=("設定ファイル", "配置"), injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])

    def test_replacement_for_plants(self):
        r = self._propose(injected_by_turn={0: {self.sid}}, replacement_for=99)
        self.assertTrue(r["is_new"])

    def test_keyed_on_assistant_span_turn_only(self):
        w = [_turn(self.USER_REQ, "了解", ledger_id=1), _turn("続き", self.ASSIST, ledger_id=2)]
        p = {"content": "設定ファイルを config/ に配置する", "conditions": ["設定ファイル", "config"], "polarity": "無記",
             "kind": "決定", "evidence": [{"source": "user", "span": self.USER_REQ}, {"source": "assistant", "span": self.ASSIST}]}
        r = self.store.imprint(w, p, injected_by_turn={1: {self.sid}})
        self.assertEqual(r["echo_match"], self.sid)

    def test_not_applied_when_only_user_span_turn_injected(self):
        # 注入が user の依頼の往復にしか無い (assistant の復唱の往復には無い) → EC2' は効かず新規
        w = [_turn(self.USER_REQ, "了解", ledger_id=1), _turn("続き", self.ASSIST, ledger_id=2)]
        p = {"content": "設定ファイルを config/ に配置する", "conditions": ["設定ファイル", "config"], "polarity": "無記",
             "kind": "決定", "evidence": [{"source": "user", "span": self.USER_REQ}, {"source": "assistant", "span": self.ASSIST}]}
        r = self.store.imprint(w, p, injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)

    def test_superseded_or_withdrawn_injected_seed_does_not_block(self):
        self.store.state["seeds"][0]["superseded_by"] = 99
        r = self._propose(injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.setUp()
        self.store.state["seeds"][0]["withdrawn"] = {"by": "x", "reason": "r"}
        r = self._propose(injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])

    def test_direct_place_proposal_with_formless_user_span_is_recorded_only(self):
        window = [_turn(self.USER_REQ, self.ASSIST, ledger_id=1)]
        p = {"content": "設定ファイルを config/ に配置する", "conditions": ["設定ファイル", "config"], "polarity": "無記",
             "kind": "所在", "evidence": [{"source": "user", "span": self.USER_REQ}, {"source": "assistant", "span": self.ASSIST}]}
        r = self.store.imprint(window, p, injected_by_turn={0: {self.sid}})
        self.assertEqual(r["kind"], "所在")
        self.assertEqual(r["echo_match"], self.sid)

    def test_decision_with_condition_in_user_span_survives_and_ec2_not_applied(self):
        u = "認証は JWT に決めた。"
        window = [_turn(u, "JWT で実装します", ledger_id=1)]
        p = {"content": "認証は JWT", "conditions": ["JWT"], "polarity": "無記", "kind": "決定",
             "evidence": [{"source": "user", "span": u}, {"source": "assistant", "span": "JWT で実装します"}]}
        r = self.store.imprint(window, p, injected_by_turn={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.assertEqual(r["kind"], "決定")
        self.assertNotIn("echo_match", r)

    def _assist_only(self, kind="所在", conds=("設定ファイル", "config"), injected=None, **kw):
        window = [_turn("お願い", self.ASSIST, ledger_id=1)]
        p = {"content": "設定ファイルを config/ に配置する", "conditions": list(conds), "polarity": "無記",
             "kind": kind, "evidence": [{"source": "assistant", "span": self.ASSIST}]}
        rf = kw.pop("replacement_for", None)
        p.update(kw)
        return self.store.imprint(window, p, replacement_for=rf, injected_by_turn=injected)

    def test_ec9_assistant_only_two_shared_conditions_is_echo_match(self):
        for kind in ("所在", "性向"):
            self.setUp()
            before = self.store.list_seeds()[0]["strength"]
            r = self._assist_only(kind=kind, injected={0: {self.sid}})
            self.assertIn("echo_match", r, kind)
            self.assertEqual(r["echo_match"], self.sid, kind)
            self.assertIsNone(r["is_new"], kind)
            self.assertIsNone(r["seed"], kind)
            self.assertIn("EC2'", r["reason"], kind)
            self.assertEqual(len(self.store.list_seeds()), 1, kind)
            self.assertEqual(self.store.list_seeds()[0]["strength"], before, kind)
            self.assertEqual(self.store.list_imprint_log()[-1]["seed_id"], self.sid, kind)

    def test_ec9_assistant_only_one_shared_condition_still_plants(self):
        # fe1 型の穴: 縁 1 語共有 (min(2, 小さい側) = 2 に満たない) は素通り
        r = self._assist_only(conds=("config", "ディレクトリ"), injected={0: {self.sid}})
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)
        self.assertEqual(len(self.store.list_seeds()), 2)

    def test_ec9_assistant_only_without_prior_injection_still_plants(self):
        # Q2 は開いたまま: その往復より前に注入が無ければ素通り
        r = self._assist_only(injected={})
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)
        self.setUp()
        r = self._assist_only(injected={5: {self.sid}})
        self.assertTrue(r["is_new"])

    def test_ec9_injected_by_turn_none_still_plants(self):
        r = self._assist_only(injected=None)
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)

    def test_ec9_assistant_only_replacement_for_still_plants(self):
        r = self._assist_only(injected={0: {self.sid}}, replacement_for=self.sid)
        self.assertTrue(r["is_new"])
        self.assertNotIn("echo_match", r)

    def test_ec9_assistant_only_reinforce_missing_target_is_echo_match(self):
        r = self._assist_only(injected={0: {self.sid}}, action="reinforce", target_id=999)
        self.assertIn("echo_match", r)
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIsNone(r["is_new"])
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_ec9_assistant_only_supersede_turned_new_by_d_is_echo_match(self):
        # 対象が user の逐語を持つ種子 → D が supersede を new に倒す → EC2' が復唱として止める
        r = self._assist_only(injected={0: {self.sid}}, action="supersede", target_id=self.sid)
        self.assertIn("echo_match", r)
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIsNone(r["is_new"])
        live = [sd for sd in self.store.list_seeds() if not sd.get("superseded_by")]
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(len(live), 1)

    def test_strict_share_ignores_empty_strings(self):
        from raiya import alaya as am
        self.assertFalse(am._shares_conditions_strict({"conditions": ["", ""]}, ["config"]))
        self.assertFalse(am._shares_conditions_strict({"conditions": ["config"]}, ["", ""]))
        self.assertTrue(am._shares_conditions_strict({"conditions": ["config"]}, ["config"]))

    def test_smallest_shared_seed_id_is_reported(self):
        r2 = self.store.imprint(
            [_turn("設定ファイルは config/ に置く。", ledger_id=2)],
            {"content": "別の言い方: config/ に設定ファイルを置く運用", "conditions": ["設定ファイル", "config"], "polarity": "無記",
             "kind": "所在", "evidence": [{"source": "user", "span": "設定ファイルは config/ に置く。"}]})
        ids = {self.sid} | ({r2["seed"]["id"]} if r2["seed"] else set())
        r = self._propose(injected_by_turn={0: ids})
        self.assertEqual(r["echo_match"], min(ids))


class TestInjectedByTurn(unittest.TestCase):
    def test_same_session_and_not_after_turn(self):
        t0 = datetime.datetime(2026, 1, 1, 12, 0, 10, tzinfo=datetime.timezone.utc)

        def iso(sec):
            return (t0 + datetime.timedelta(seconds=sec)).isoformat(timespec="seconds")

        log = [
            {"session_id": "s1", "ts": iso(-5), "seed_ids": [1], "index_ids": [2]},
            {"session_id": "s1", "ts": iso(5), "seed_ids": [3], "index_ids": []},   # 往復の後
            {"session_id": "s2", "ts": iso(-5), "seed_ids": [4], "index_ids": []},  # 別 session
            {"session_id": None, "ts": iso(-5), "seed_ids": [5], "index_ids": []},  # session 無し
            {"session_id": "s1", "ts": "bad", "seed_ids": [6], "index_ids": []},    # ts 読めない
        ]
        out = engine_mod._injected_by_turn(log, [("s1", t0.timestamp()), (None, t0.timestamp()), ("s1", None)])
        self.assertEqual(out, {0: {1, 2}})


class TestEngineConsolidateR(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def _consolidate(self, proposals):
        with mock.patch("raiya.backend.call_llm", return_value=json.dumps(proposals, ensure_ascii=False)):
            return self.eng.consolidate()

    def _plant(self):
        self.eng.record_turn("質問 配置", "設定は config.yaml に置く 配置", session_id="s1", project=PROJ)
        r = self._consolidate([_prop("設定は config.yaml に置く", ["配置"], "設定は config.yaml に置く")])
        seed = r["decisions"][0]["seed"]
        self.assertEqual(seed["sansho"], "依他起")
        return seed["id"]

    def _echo_round(self, recall_session):
        sid = self._plant()
        rec = self.eng.recall("配置 の話", project=PROJ, session_id=recall_session)
        self.assertIn(sid, [s["id"] for s in rec["index"]])
        self.eng.record_turn("続き 配置", "設定は config.yaml に置くのが良い 配置", session_id="s1", project=PROJ)
        r = self._consolidate([_prop("設定は config.yaml に置く", ["配置"], "設定は config.yaml に置くのが良い",
                                     action="reinforce", target_id=sid)])
        return sid, r["decisions"][0]

    def _strength(self, sid):
        return MemoryEngine(state_dir=self.tmpdir, new_life=False).alaya.get_by_ids([sid])[0]["strength"]

    def test_echo_of_injected_seed_in_same_session_is_recorded_only(self):
        sid, d = self._echo_round("s1")
        self.assertEqual(d["echo_match"], sid)
        self.assertIsNone(d["seed"])
        self.assertEqual(self._strength(sid), 1)

    def test_injection_in_another_session_still_reinforces(self):
        sid, d = self._echo_round("s2")
        self.assertNotIn("echo_match", d)
        self.assertTrue(d.get("reinforced"))
        self.assertEqual(self._strength(sid), 2)


if __name__ == "__main__":
    unittest.main()
