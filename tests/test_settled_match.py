"""#152 SC' (spec 2026-11-10-request-span-crossing-design §7): 依他起 の提案が、同 kind・同 project の生きた 圓成實 と
content が同一 (`sansho._n` + strip + casefold、spec §7.1) ∧ 縁共有なら、植えず記録のみ (`settled_match`)。テストが消すのは自作 tmp だけ。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ["RAIYA_CONFIG"] = ""
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import config  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

S1 = "キャッシュは Memcached に決めた。単純なキー値用途だから。"
CONTENT = "キャッシュに Memcached を採用（単純なキー値用途のため）"
CONDS = ["キャッシュ", "Memcached"]
ANSWER = "キャッシュには Memcached を使います。理由は単純なキー値用途だからです。"
REQUESTS = [
    "キャッシュに何を使うか、理由も含めて教えて。",
    "キャッシュについて、これまでに決めたことを理由も含めて全部挙げて。",
    "キャッシュの方針を確認して。",
    "キャッシュが Memcached になった経緯を見せて。",
]
NOTE = "圓成實 の種子 #1 と同じ内容の 依他起 の提案: 記録のみ、別に植えない"


def setUpModule():
    config._reset()


class SettledMatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya-settled-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def engine(self, new_life=True):
        return MemoryEngine(state_dir=self.tmp, new_life=new_life)

    def plant_first(self, conds=CONDS, kind="決定", project="p", content=CONTENT, user=S1):
        r = self.engine().imprint(
            [LedgerTurn(user_text=user, assistant_text="了解", id=1, project=project)],
            [{"content": content, "conditions": conds, "polarity": "無記", "kind": kind,
              "evidence": [{"source": "user", "span": user}]}],
        )[0]
        self.assertTrue(r["seed"] and r["seed"]["sansho"] == "圓成實", r)
        return r

    def propose(self, user, content=CONTENT, conds=CONDS, kind="決定", project="p", action="reinforce", target=1,
                extra=None, answer=ANSWER, tid=2):
        ev = [{"source": "assistant", "span": answer}]
        if user is not None:
            ev.insert(0, {"source": "user", "span": user})
        p = {"content": content, "conditions": conds, "polarity": "無記", "kind": kind, "action": action, "evidence": ev}
        if action != "new" and target is not None:
            p["target_id"] = target
        p.update(extra or {})
        return self.engine().imprint(
            [LedgerTurn(user_text=user or "続けて", assistant_text=answer, id=tid, project=project)], [p]
        )[0]

    def seeds(self):
        return MemoryEngine(state_dir=self.tmp, new_life=False).inspect_seeds()

    def assertRecordedOnly(self, r, eid=1):
        self.assertEqual(r.get("settled_match"), eid, r)
        self.assertIsNone(r["seed"])
        self.assertIsNone(r["is_new"])

    def assertPlanted(self, r):
        self.assertNotIn("settled_match", r)
        self.assertTrue(r["is_new"], r)


class TestStopped(SettledMatchBase):
    def test_request_forms_are_recorded_only(self):
        for i, user in enumerate(REQUESTS):
            with self.subTest(user=user):
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = tempfile.mkdtemp(prefix="raiya-settled-")
                self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
                self.plant_first()
                before = [dict(s) for s in self.seeds()]
                r = self.propose(user)
                self.assertRecordedOnly(r)
                after = self.seeds()
                self.assertEqual(len(after), 1)
                self.assertEqual(before[0]["strength"], after[0]["strength"])
                self.assertEqual(before[0]["content"], after[0]["content"])
                self.assertEqual(before[0]["evidences"], after[0]["evidences"])

    def test_imprint_log_row_has_seed_id_and_note(self):
        import sqlite3
        self.plant_first()
        self.propose(REQUESTS[0])
        con = sqlite3.connect(os.path.join(self.tmp, "raiya.db"))
        self.addCleanup(con.close)
        names = [r[1] for r in con.execute("PRAGMA table_info(imprint_log)")]
        rows = [dict(zip(names, r)) for r in con.execute("SELECT * FROM imprint_log")]
        hits = [r for r in rows if NOTE in str(r)]
        self.assertEqual(len(hits), 1, rows)
        self.assertEqual(hits[0].get("seed_id"), 1)

    def test_existing_seed_with_one_condition(self):
        self.plant_first(conds=CONDS[:1])
        r = self.propose(REQUESTS[0], conds=CONDS[:1])
        self.assertRecordedOnly(r)
        self.assertEqual(len(self.seeds()), 1)

    def test_existing_one_condition_proposal_two(self):
        self.plant_first(conds=CONDS[:1])
        r = self.propose(REQUESTS[0])
        self.assertRecordedOnly(r)

    def test_action_new_without_target(self):
        self.plant_first()
        r = self.propose(REQUESTS[0], action="new", target=None)
        self.assertRecordedOnly(r)
        self.assertEqual(len(self.seeds()), 1)


class TestNotStopped(SettledMatchBase):
    def test_value_change_is_planted(self):
        self.plant_first()
        r = self.propose(REQUESTS[0], content="キャッシュに Redis を採用", conds=["キャッシュ", "Redis"])
        self.assertPlanted(r)

    def test_added_clause_is_planted(self):
        self.plant_first()
        r = self.propose(REQUESTS[0], content="キャッシュの TTL は 60 秒にする", conds=["キャッシュ", "Memcached", "TTL"])
        self.assertPlanted(r)

    def test_different_matter_sharing_topic_word_is_planted(self):
        self.plant_first(conds=CONDS[:1])
        r = self.propose(REQUESTS[0], content="キャッシュのキーは sha256 にする", conds=["キャッシュ", "sha256"])
        self.assertPlanted(r)

    # §7.1: 値を 1 字替えた提案 (短い content・長い文の 1 字) は止めない。既存は 圓成實・縁共有あり。
    VALUE_CHANGES = [
        ("猫 → 犬", "猫が好き", ["猫"], "犬が好き", ["猫"], "性向"),
        ("日次 → 週次", "ログは日次で回す", ["ログ"], "ログは週次で回す", ["ログ", "週次"], "決定"),
        ("60 秒 → 90 秒", "キャッシュの TTL は 60 秒にする", ["TTL"], "キャッシュの TTL は 90 秒にする", ["TTL"], "決定"),
        ("v1 → v2 (長い文の 1 字)", "API の既定のバージョンは v1 にして、古いクライアントも当面は通す", ["API"],
         "API の既定のバージョンは v2 にして、古いクライアントも当面は通す", ["API"], "決定"),
    ]

    def test_one_char_value_changes_are_planted(self):
        for name, ec, econd, pc, pcond, kind in self.VALUE_CHANGES:
            with self.subTest(name=name):
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = tempfile.mkdtemp(prefix="raiya-settled-")
                self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
                self.plant_first(conds=econd, kind=kind, content=ec, user=ec + "。そう決めた。")
                ans = pc + "ということです。"
                r = self.propose("{} について教えて。".format("・".join(pcond)), content=pc, conds=pcond, kind=kind,
                                 action="new", target=None, answer=ans)
                self.assertNotIn("settled_match", r)
                self.assertEqual(r["sansho"], "依他起", r)
                self.assertTrue(r["is_new"], r)

    def test_whitespace_and_case_only_difference_is_recorded_only(self):
        self.plant_first()
        r = self.propose(REQUESTS[0], content="キャッシュに memcached を採用 （単純なキー値用途のため）", action="new", target=None)
        self.assertRecordedOnly(r)
        self.assertEqual(len(self.seeds()), 1)

    def test_empty_content_pair_is_not_a_hit(self):
        self.plant_first(content="")
        r = self.propose(REQUESTS[0], content="", action="new", target=None)
        self.assertNotIn("settled_match", r)

    def test_whitespace_only_content_is_not_a_hit(self):
        for blank in ("  ", "\u3000", " \n "):
            with self.subTest(blank=blank):
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = tempfile.mkdtemp(prefix="raiya-settled-")
                self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
                self.plant_first(content=blank)
                r = self.propose(REQUESTS[0], content=blank, action="new", target=None)
                self.assertNotIn("settled_match", r)
                self.assertTrue(r["is_new"], r)

    def test_identical_content_without_shared_condition_is_planted(self):
        self.plant_first()
        r = self.propose("理由を教えて。", conds=["理由", "単純"], action="new", target=None)
        self.assertNotIn("settled_match", r)
        self.assertEqual(r["kind"], "決定")
        self.assertTrue(r["is_new"], r)

    def test_lowest_id_on_several_hits(self):
        self.plant_first()
        r2 = self.engine().imprint(
            [LedgerTurn(user_text=S1, assistant_text="了解", id=2, project="p")],
            [{"content": CONTENT, "conditions": ["単純", "キー値用途"], "polarity": "無記", "kind": "決定",
              "evidence": [{"source": "user", "span": S1}]}],
        )[0]
        self.assertTrue(r2["is_new"], r2)
        self.assertEqual(len(self.seeds()), 2)
        r = self.propose(REQUESTS[0], conds=CONDS + ["単純", "キー値用途"], action="new", target=None, tid=3)
        self.assertRecordedOnly(r, 1)

    def test_sp_excluded_target_is_not_E(self):
        # SP1'': 旧 #1 の user の文「DB は SQLite…」に新しい言が触れない supersede は new に倒れる。倒された target は E にしない。
        old = "DB は SQLite にしました。キャッシュは Redis で行く。"
        c = "DB は SQLite、キャッシュは Redis を採用"
        self.plant_first(conds=["SQLite", "Redis"], content=c, user=old)
        user = "キャッシュは Redis ではなく Memcached に決めた"
        r = self.propose(user, content=c, conds=["Redis", "Memcached", "SQLite"], action="supersede", target=1,
                         answer="SQLite はそのままです")
        self.assertIn("部分交代", r["reason"], r)
        self.assertNotIn("settled_match", r)
        self.assertEqual(r["sansho"], "依他起", r)
        self.assertTrue(r["is_new"], r)

    def test_etaki_seed_with_identical_content_is_never_E(self):
        ans = "キャッシュには Memcached を使います。"
        p = {"content": CONTENT, "conditions": CONDS, "polarity": "無記", "kind": "決定",
             "evidence": [{"source": "assistant", "span": ans}]}
        r1 = self.engine().imprint([LedgerTurn(user_text="続けて", assistant_text=ans, id=1, project="p")], [p])[0]
        self.assertEqual(r1["sansho"], "依他起", r1)
        r2 = self.engine().imprint([LedgerTurn(user_text="続けて", assistant_text=ans, id=2, project="p")], [p])[0]
        self.assertNotIn("settled_match", r2)
        self.assertTrue(r2.get("reinforced"), r2)

    def test_settled_match_precedes_ec2_on_consolidate_path(self):
        # 性向 の 依他起 提案が注入済みの 圓成實 E と同一 content: EC2' (echo_match) でなく SC' (settled_match)。
        store = alaya_store_at(os.path.join(self.tmp, "alaya.json"), os.path.join(self.tmp, "imprint_log.json"))
        store.begin_new_life()
        u = "コーヒーが好き"
        r1 = store.imprint([LedgerTurn(user_text=u, assistant_text="", id=1, project="p")],
                           {"content": "コーヒーが好き", "conditions": ["コーヒー", "好き"], "polarity": "善", "kind": "性向",
                            "evidence": [{"source": "user", "span": u}]})
        self.assertEqual(r1["sansho"], "圓成實")
        a = "コーヒーが好きですね"
        p = {"content": "コーヒーが好き", "conditions": ["コーヒー", "好き"], "polarity": "善", "kind": "性向",
             "evidence": [{"source": "assistant", "span": a}]}
        r = store.imprint([LedgerTurn(user_text="お願い", assistant_text=a, id=2, project="p")], p,
                          injected_by_turn={0: {1}})
        self.assertEqual(r["kind"], "性向")
        self.assertEqual(r.get("settled_match"), 1, r)
        self.assertNotIn("echo_match", r)


    # §7.2: 所在 は対象外 (値 = 場所が span に在る)。
    def _place_case(self, new_path):
        u1 = "設定ファイルは /a/cfg.yaml にある。"
        r1 = self.engine().imprint(
            [LedgerTurn(user_text=u1, assistant_text="了解", id=1, project="p")],
            [{"content": "設定ファイルの場所", "conditions": ["/a/cfg.yaml"], "polarity": "無記", "kind": "所在",
              "evidence": [{"source": "user", "span": u1}]}],
        )[0]
        self.assertEqual(r1["seed"]["sansho"], "圓成實", r1)
        u2 = "設定ファイルは {} にある。".format(new_path)
        a2 = "{} を確認しました。".format(new_path)
        return self.engine().imprint(
            [LedgerTurn(user_text=u2, assistant_text=a2, id=2, project="p")],
            [{"content": "設定ファイルの場所", "conditions": [new_path], "polarity": "無記", "kind": "所在",
              "evidence": [{"source": "user", "span": u2}, {"source": "assistant", "span": a2}]}],
        )[0]

    def test_place_is_out_of_scope(self):
        # HEAD (SC' 無し) と同じ: 依他起 の 所在 は 圓成實 へ併合されず別に植わる。
        # (この形は path が違い縁を共有しないので、所在 が対象でも当たらない。除外そのものを固定するのは次のテスト。)
        r = self._place_case("/b/cfg.yaml")
        self.assertEqual(r["sansho"], "依他起", r)
        self.assertNotIn("settled_match", r)
        self.assertTrue(r["is_new"], r)

    def test_place_identical_content_and_path_is_out_of_scope(self):
        r = self._place_case("/a/cfg.yaml")
        self.assertEqual(r["sansho"], "依他起", r)
        self.assertNotIn("settled_match", r)

    def test_workaround_identical_content_is_recorded_only(self):
        store = alaya_store_at(os.path.join(self.tmp, "alaya.json"), os.path.join(self.tmp, "imprint_log.json"))
        store.begin_new_life()
        c = "docs/old を直接書き換えた → 壊れたため元に戻した"
        w = [LedgerTurn(user_text="docs/old を直接書き換えた。元に戻してください", assistant_text="", id=1, project="p")]
        r1 = store.imprint(w, {"content": c, "conditions": ["docs/old"], "polarity": "無記", "kind": "回避策",
                               "evidence": [{"source": "user", "span": "docs/old を直接書き換えた"},
                                            {"source": "user", "span": "元に戻してください"}]})
        self.assertEqual((r1["seed"]["sansho"], r1["seed"]["kind"]), ("圓成實", "回避策"), r1)
        w2 = [LedgerTurn(user_text="質問 docs/old", assistant_text="docs/old を生成物に書き換えました", id=2, project="p"),
              LedgerTurn(user_text="元に戻してください docs/old", assistant_text="", id=3, project="p")]
        r = store.imprint(w2, {"content": c, "conditions": ["docs/old"], "polarity": "無記", "kind": "回避策",
                               "evidence": [{"source": "assistant", "span": "docs/old を生成物に書き換えました"},
                                            {"source": "user", "span": "元に戻してください"}]})
        self.assertEqual(r["sansho"], "依他起", r)
        self.assertEqual(r.get("settled_match"), 1, r)
        self.assertEqual(len(store.list_seeds()), 1)

    def test_paraphrase_same_conditions_is_planted_known_limit(self):
        self.plant_first()
        r = self.propose(REQUESTS[0], content="キャッシュは Memcached を使う")
        self.assertNotIn("settled_match", r)

    def test_different_project_is_planted(self):
        self.plant_first(project="p")
        r = self.propose(REQUESTS[0], project="q", target=None, action="new")
        self.assertPlanted(r)

    def test_different_kind_is_planted(self):
        self.plant_first(kind="決定")
        r = self.propose(REQUESTS[0], kind="所在", action="new", target=None)
        self.assertNotIn("settled_match", r)

    def test_withdrawn_seed_keeps_its_own_behaviour(self):
        self.plant_first()
        eng = self.engine(new_life=False)
        eng.record_turn(S1, "了解", project="p")
        eng.withdraw_seed(1, "太郎", "誤り", ledger_ids=[1])
        r = self.propose(REQUESTS[0])
        # 取り下げ中の W は生きていないので SC' の E にならない (圓成實 W は 依他起 の併合候補でもない)。
        self.assertNotIn("settled_match", r)
        self.assertTrue(r["is_new"], r)

    def test_horizontally_superseded_seed_is_not_a_hit(self):
        # #1 は横断 supersede で隠れる (also_superseded == [1])。内容は #1 に残るが生きていないので E にならない。
        self.plant_first()
        u2 = "ログは日次で回す。そう決めた。"
        r2 = self.engine().imprint(
            [LedgerTurn(user_text=u2, assistant_text="了解", id=2, project="p")],
            [{"content": "ログは日次で回す", "conditions": ["ログ", "日次"], "polarity": "無記", "kind": "決定",
              "evidence": [{"source": "user", "span": u2}]}],
        )[0]
        self.assertTrue(r2["is_new"], r2)
        u3 = "ログは日次で回す。キャッシュは Memcached に決めた。"
        r3 = self.engine().imprint(
            [LedgerTurn(user_text=u3, assistant_text="了解", id=3, project="p")],
            [{"content": "ログは日次、キャッシュは Memcached", "conditions": ["キャッシュ", "Memcached", "ログ", "日次"],
              "polarity": "無記", "kind": "決定", "action": "supersede", "target_id": r2["seed"]["id"],
              "evidence": [{"source": "user", "span": u3}]}],
        )[0]
        self.assertTrue(r3.get("superseded"), r3)
        self.assertEqual(r3["also_superseded"], [1], r3)
        s1 = [x for x in MemoryEngine(state_dir=self.tmp, new_life=False).inspect_seeds(include_superseded=True) if x["id"] == 1][0]
        self.assertEqual(s1["content"], CONTENT)
        self.assertEqual(s1["superseded_by"], r2["seed"]["id"])
        r = self.propose(REQUESTS[0], action="new", target=None, tid=4)
        self.assertPlanted(r)

    def test_resume_point_untouched(self):
        self.plant_first(kind="再開点", content="次はキャッシュの Memcached を調べる", conds=CONDS,
                         user="次はキャッシュの Memcached を調べる。")
        r = self.propose(REQUESTS[0], kind="再開点", content="次はキャッシュの Memcached を調べる", action="new", target=None)
        self.assertNotIn("settled_match", r)


class TestUnaffectedPaths(SettledMatchBase):
    def test_assistant_only_still_d(self):
        self.plant_first()
        r = self.propose(None)
        self.assertEqual(r.get("user_span_match"), 1, r)
        self.assertNotIn("settled_match", r)

    def test_reject_marker_still_crosses_and_reinforces(self):
        self.plant_first()
        before = self.seeds()[0]["strength"]
        r = self.propose("キャッシュは Redis ではなく Memcached で行く。")
        self.assertNotIn("settled_match", r)
        self.assertEqual(r["seed"]["id"], 1, r)
        self.assertGreater(self.seeds()[0]["strength"], before)

    def test_settled_proposal_never_stopped(self):
        self.plant_first()
        r = self.engine().imprint(
            [LedgerTurn(user_text=S1, assistant_text="了解", id=2, project="p")],
            [{"content": CONTENT, "conditions": CONDS, "polarity": "無記", "kind": "決定",
              "evidence": [{"source": "user", "span": S1}]}],
        )[0]
        self.assertEqual(r["sansho"], "圓成實")
        self.assertNotIn("settled_match", r)
        self.assertTrue(r.get("reinforced"), r)

    def test_replacement_for_untouched(self):
        # SC' は replacement_for を持つ提案には効かない (人の置き換えは必ず植える)。Alaya 直 (engine は replacement_for を出さない)。
        store = alaya_store_at(os.path.join(self.tmp, "alaya.json"), os.path.join(self.tmp, "imprint_log.json"))
        store.begin_new_life()
        r1 = store.imprint([LedgerTurn(user_text=S1, assistant_text="了解", id=1, project="p")],
                           {"content": CONTENT, "conditions": CONDS, "polarity": "無記", "kind": "決定",
                            "evidence": [{"source": "user", "span": S1}]})
        self.assertEqual(r1["sansho"], "圓成實")
        ev = [{"source": "user", "span": REQUESTS[0]}, {"source": "assistant", "span": ANSWER}]
        r = store.imprint([LedgerTurn(user_text=REQUESTS[0], assistant_text=ANSWER, id=2, project="p")],
                          {"content": CONTENT, "conditions": CONDS, "polarity": "無記", "kind": "決定", "action": "new",
                           "evidence": ev},
                          replacement_for=1)
        self.assertNotIn("settled_match", r)
        self.assertTrue(r["is_new"], r)

    def test_twin_match_wins_where_it_applied(self):
        # 依他起 の双子 #2 が既に在る (縁 2 語共有で #1 の影) とき、#2 を指す再提案は 双子照合 のまま。
        self.plant_first()
        r2 = self.propose(REQUESTS[0], content="キャッシュは Memcached を使う", target=None, action="new")
        self.assertPlanted(r2)
        self.assertEqual(r2["seed"]["id"], 2)
        r3 = self.propose(REQUESTS[1], content="キャッシュは Memcached を使う", target=2, tid=3)
        self.assertEqual(r3.get("twin_match"), 2, r3)
        self.assertNotIn("settled_match", r3)


if __name__ == "__main__":
    unittest.main()
