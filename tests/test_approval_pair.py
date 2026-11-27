"""承認の対 (T1)・描画 (T3)・③ の関わり (T4)
(spec docs/superpowers/specs/2026-11-27-ratified-decision-design.md §3.3〜§3.5・§5)。

LLM は呼ばない。テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。
"""
import copy
import random
import os
import shutil
import sys
import tempfile
import unittest

os.environ["RAIYA_CONFIG"] = ""

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, markers, sansho  # noqa: E402
from raiya.alaya import APPROVAL_PAIR_NOTE  # noqa: E402
from raiya import manas  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402
from tests.test_index_reach import _naive, _rand_text  # noqa: E402

PROJ = "/proj/approval"
COND = ["SQLite", "案3"]
RECOMMEND = "推奨: 案3 で SQLite を使う。"


def _turn(user, assistant="", ledger_id=0):
    return LedgerTurn(user_text=user, assistant_text=assistant, id=ledger_id, project=PROJ)


def _proposal(evidence, conds=COND, kind="決定", content="案3 で SQLite を使う", **kw):
    p = {
        "content": content, "conditions": list(conds), "polarity": "無記", "kind": kind,
        "evidence": [{"source": s, "span": sp} for s, sp in evidence],
    }
    p.update(kw)
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = self._store()

    def _store(self):
        d = tempfile.mkdtemp(dir=self.tmpdir)
        store = alaya_store_at(os.path.join(d, "alaya.json"), os.path.join(d, "imprint_log.json"))
        store.begin_new_life()
        return store

    def prev_window(self, go="推奨でGO", rec=RECOMMEND):
        return [_turn("どうしますか", rec, 1), _turn(go, "了解です", 2)]

    def prev_evidence(self, go="推奨でGO", rec=RECOMMEND):
        return [("assistant", rec), ("user", go)]


class TestApprovalPair(_Base):
    def test_prev_row_pair_plants_decision(self):
        r = self.store.imprint(self.prev_window(), _proposal(self.prev_evidence()))
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.assertEqual(r["seed"]["strength"], 2)
        self.assertIn(APPROVAL_PAIR_NOTE, r["reason"])

    def test_same_row_restatement_pair_strength_one(self):
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("GO", "案3 の SQLite で実装しました", 2)]
        r = self.store.imprint(window, _proposal([("assistant", "案3 の SQLite で実装しました"), ("user", "GO")]))
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["seed"]["strength"], 1)

    def test_same_row_new_statement_is_not_pair(self):
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("GO", "次は Redis を推奨します", 2)]
        r = self.store.imprint(
            window, _proposal([("assistant", "次は Redis を推奨します"), ("user", "GO")], conds=["Redis"], content="Redis を使う"))
        self.assertEqual(r["kind"], "性向")

    def test_later_go_preferred_over_earlier(self):
        window = [_turn("GO", RECOMMEND, 1), _turn("GO", "", 2)]
        r = self.store.imprint(window, _proposal([("assistant", RECOMMEND), ("user", "GO")]))
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["seed"]["strength"], 2)
        user_ev = [e for e in r["seed"]["evidences"] if e["source"] == "user"]
        self.assertEqual([e["ledger_id"] for e in user_ev], [2])

    def test_approval_span_repointed_to_pair_row(self):
        window = [_turn("GO", "別件の話です", 1), _turn("何を決めますか", RECOMMEND, 2), _turn("GO", "", 3)]
        r = self.store.imprint(window, _proposal([("assistant", RECOMMEND), ("user", "GO")]))
        self.assertEqual(r["kind"], "決定")
        user_ev = [e for e in r["seed"]["evidences"] if e["source"] == "user"]
        self.assertEqual([e["ledger_id"] for e in user_ev], [3])
        self.assertEqual(r["seed"]["strength"], 2)
        self.assertEqual(r["spans"][1]["turn_index"], 2)

    def test_no_approval_span_cited_stays_disposition(self):
        r = self.store.imprint(self.prev_window(), _proposal([("assistant", RECOMMEND)]))
        self.assertEqual(r["kind"], "性向")

    def test_long_turn_is_not_approval(self):
        go = "GO。ただし後で案3を詳しく検討して結果を共有してください"
        self.assertGreater(len(go), 25)
        r = self.store.imprint(self.prev_window(go=go), _proposal([("assistant", RECOMMEND), ("user", "GO")]))
        self.assertEqual(r["kind"], "性向")

    def test_question_only_turn_is_not_approval(self):
        r = self.store.imprint(self.prev_window(go="GO でいい？"), _proposal([("assistant", RECOMMEND), ("user", "GO")]))
        self.assertEqual(r["kind"], "性向")

    def test_correction_turn_is_not_approval(self):
        go = "GO ただしやめてください"
        r = self.store.imprint(self.prev_window(go=go), _proposal([("assistant", RECOMMEND), ("user", go)]))
        self.assertEqual(r["kind"], "性向")

    def test_english_go_phrase_is_not_approval(self):
        for go in ("no go", "go back"):
            store = self._store()
            r = store.imprint(self.prev_window(go=go), _proposal([("assistant", RECOMMEND), ("user", go)]))
            self.assertEqual(r["kind"], "性向", go)

    def test_ascii_word_with_go_is_not_approval(self):
        r = self.store.imprint(
            self.prev_window(go="shipper GO"), _proposal([("assistant", RECOMMEND), ("user", "shipper GO")]))
        self.assertEqual(r["kind"], "性向")

    def test_other_session_next_turn_is_not_pair(self):
        # X2b/X1 が先に拒むので、ここは「座の違う承認が 決定 にならない」ことの全体の確認だけ。
        # `_approval_pair` の座の条件そのものは test_approval_pair_seat_condition_directly と test_other_session_between_is_not_pair が固定する。
        r = self.store.imprint(
            self.prev_window(), _proposal(self.prev_evidence()), session_by_turn=["s", "t"])
        self.assertNotEqual(r.get("kind"), "決定")
        self.assertEqual([s for s in self.store.list_seeds() if s.get("kind") == "決定"], [])

    def test_other_session_between_is_not_pair(self):
        # 座 t の GO が間に挟まっても、座 s の次の往復 (承認でない) とは対にならない
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("GO", "", 2), _turn("別件です", "", 3), _turn("GO", "", 4)]
        r = self.store.imprint(
            window, _proposal(self.prev_evidence(go="GO")), session_by_turn=["s", "t", "s", "s"])
        self.assertNotEqual(r.get("kind"), "決定")

    def test_assistant_span_without_condition_is_not_pair(self):
        window = [_turn("どうしますか", "案3でよいですか？", 1), _turn("GO", "", 2)]
        r = self.store.imprint(window, _proposal([("assistant", "案3でよいですか？"), ("user", "GO")], conds=["案3"]))
        self.assertEqual(r["kind"], "性向")

    def test_declared_disposition_not_promoted_by_pair(self):
        r = self.store.imprint(self.prev_window(), _proposal(self.prev_evidence(), kind="性向"))
        self.assertEqual(r["kind"], "性向")

    def test_pair_decision_threshold_stays_two(self):
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("GO", "案3 の SQLite で実装しました", 2)]
        r = self.store.imprint(window, _proposal([("assistant", "案3 の SQLite で実装しました"), ("user", "GO")]))
        self.assertEqual(r["seed"]["strength"], 1)
        fired = self.store.germinate("SQLite と案3 の件", project=PROJ)
        self.assertEqual(fired, [])

    def test_pair_decision_cannot_cross_supersede(self):
        span = "案3 で SQLite にした。"
        z = self.store.imprint([_turn(span, "", 1)], _proposal([("user", span)], content="既に決めた案3"))["seed"]
        self.assertEqual((z["kind"], z["sansho"]), ("決定", "圓成實"))
        r = self.store.imprint(
            self.prev_window(), _proposal(self.prev_evidence(), content="案3 を採る SQLite", action="supersede", target_id=z["id"]))
        self.assertEqual(r["kind"], "決定")
        self.assertTrue(r["is_new"])
        self.assertFalse(r.get("superseded"))
        self.assertIsNone(self.store.get_by_ids([z["id"]])[0]["superseded_by"])

    def _plant_injected(self):
        span = "案3 で SQLite にした。"
        return self.store.imprint([_turn(span, "", 1)], _proposal([("user", span)], content="既に決めた案3"))["seed"]

    def test_ec2_applies_to_pair_decision(self):
        x = self._plant_injected()
        before = len(self.store.list_seeds())
        r = self.store.imprint(
            self.prev_window(), _proposal(self.prev_evidence(), content="案3 を採る SQLite"), injected_by_turn={0: {x["id"]}})
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["echo_match"], x["id"])
        self.assertIsNone(r["seed"])
        self.assertEqual(len(self.store.list_seeds()), before)

    def test_r_blocks_reinforce_by_pair_on_injected_seed(self):
        y = self.store.imprint(self.prev_window(), _proposal(self.prev_evidence()))["seed"]
        self.assertEqual(y["kind"], "決定")
        window = [_turn("続き", RECOMMEND, 10), _turn("GO", "", 11)]
        r = self.store.imprint(
            window, _proposal([("assistant", RECOMMEND), ("user", "GO")], action="reinforce", target_id=y["id"]),
            injected_by_turn={0: {y["id"]}})
        self.assertEqual(r["echo_match"], y["id"])
        self.assertEqual(self.store.get_by_ids([y["id"]])[0]["strength"], y["strength"])

    def test_reinforce_by_pair_on_uninjected_seed_still_strengthens(self):
        y = self.store.imprint(self.prev_window(), _proposal(self.prev_evidence()))["seed"]
        window = [_turn("続き", RECOMMEND, 10), _turn("GO", "", 11)]
        r = self.store.imprint(
            window, _proposal([("assistant", RECOMMEND), ("user", "GO")], action="reinforce", target_id=y["id"]),
            injected_by_turn={0: set()})
        self.assertTrue(r.get("reinforced"))
        self.assertGreater(self.store.get_by_ids([y["id"]])[0]["strength"], y["strength"])

    def test_plant_precheck_agrees_with_imprint_on_repointed_project(self):
        # 窓の最初の GO は project A、対の往復 (推奨 + GO) は project B。imprint は承認の span を対の往復へ付け替えてから project を判定するので、
        # precheck も同じ付け替えをしないと「project 跨ぎ」で拒んで食い違う。
        def row(user, assistant, ledger_id, project):
            return LedgerTurn(user_text=user, assistant_text=assistant, id=ledger_id, project=project)

        window = [row("GO", "別件の話です", 1, "/proj/A"), row("どうしますか", RECOMMEND, 2, "/proj/B"), row("GO", "", 3, "/proj/B")]
        proposal = _proposal([("assistant", RECOMMEND), ("user", "GO")])
        r = self.store.imprint(window, proposal, replacement_for=99)
        self.assertTrue(r["is_new"])
        self.assertEqual(r["seed"]["project"], "/proj/B")
        self.assertIsNone(alaya.plant_precheck(window, proposal))

    def test_plant_precheck_agrees_with_imprint_when_not_pair(self):
        window = self.prev_window(go="shipper GO")
        proposal = _proposal(self.prev_evidence(go="shipper GO"))
        r = self._store().imprint(window, proposal, replacement_for=99)
        self.assertEqual(r["kind"], "性向")
        self.assertEqual(alaya.plant_precheck(window, proposal) is None, bool(r["is_new"]))

    def test_approval_markers_role(self):
        self.assertEqual(markers.union("approval"), ("推奨で", "推奨どおり", "go"))
        self.assertEqual(markers.MARKERS["ja"]["approval"], ("推奨で", "推奨どおり"))
        self.assertEqual(markers.MARKERS["en"]["approval"], ("go",))

    def test_fullwidth_go_counts_as_approval(self):
        r = self.store.imprint(self.prev_window(go="ＧＯ"), _proposal(self.prev_evidence(go="ＧＯ")))
        self.assertEqual(r["kind"], "決定")

    def test_japanese_scope_words_with_go_count(self):
        go = "実装GO"
        r = self.store.imprint(self.prev_window(go=go), _proposal(self.prev_evidence(go=go)))
        self.assertEqual(r["kind"], "決定")

    def test_go_language_short_turn_counts(self):
        # 既知の限界: 推奨の直後の「Goで書いて」は承認として当たる
        go = "Goで書いて"
        r = self.store.imprint(self.prev_window(go=go), _proposal(self.prev_evidence(go=go)))
        self.assertEqual(r["kind"], "決定")

    def test_approval_two_turns_later_not_pair(self):
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("なぜですか", "理由です", 2), _turn("GO", "", 3)]
        r = self.store.imprint(window, _proposal(self.prev_evidence(go="GO")))
        self.assertEqual(r["kind"], "性向")

    def test_approval_pair_seat_condition_directly(self):
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("推奨でGO", "了解です", 2)]
        spans = [
            {"span": RECOMMEND, "sansho": "依他起", "turn_index": 0},
            {"span": "推奨でGO", "sansho": "圓成實", "turn_index": 1},
        ]
        self.assertEqual(alaya._approval_pair(spans, COND, window, ["s", "s"]), (1, 1))
        self.assertEqual(alaya._approval_pair(spans, COND, window, None), (1, 1))
        self.assertIsNone(alaya._approval_pair(spans, COND, window, ["s", "t"]))
        self.assertIsNone(alaya._approval_pair(spans, COND, window, [None, "t"]))

    def test_is_approval_text_basics(self):
        for ok in ("GO", "Go", "ＧＯ", "推奨でGO", "推奨どおり"):
            self.assertTrue(alaya._is_approval_text(ok), ok)
        for ng in ("", "goal で", "no go", "shipper GO", "OK"):
            self.assertFalse(alaya._is_approval_text(ng), ng)


def _seed(kind, sansho_class, evidences, content="案3 で SQLite を使う", conds=("データベース", "案3"), strength=1):
    return {
        "id": 7, "kind": kind, "sansho": sansho_class, "content": content, "conditions": list(conds), "strength": strength,
        "project": PROJ, "superseded_by": None, "history": [],
        "evidences": [dict(e, life=1) for e in evidences],
    }


def _ev(source, span, ledger_id):
    return {"source": source, "span": span, "ledger_id": ledger_id}


class TestApprovalPairRendering(_Base):
    def planted(self):
        return self.store.imprint(self.prev_window(), _proposal(self.prev_evidence()))["seed"]

    def test_pair_decision_line_heads_content(self):
        seed = self.planted()
        line = manas._render_seed_line(seed)
        self.assertTrue(line.startswith("- 決定(承認): "), line)
        self.assertIn("承認: 推奨でGO", line)
        self.assertIn("案3 で SQLite を使う", line)
        self.assertIn("一部推定", line)
        self.assertNotIn("要約:", line)

    def test_pair_decision_index_line(self):
        line = manas._render_index_line(self.planted())
        self.assertTrue(line.startswith("- 決定(承認): "), line)
        self.assertIn("承認: 推奨でGO", line)
        self.assertIn("案3 で SQLite を使う", line)
        self.assertNotIn("要約:", line)

    def test_pair_decision_no_summaryless_form(self):
        base, nosum, parts = manas._seed_line_parts(self.planted())
        self.assertIsNone(nosum)
        self.assertEqual(parts[:2], ("決定(承認)", "内容"))
        self.assertEqual(parts[2], len("案3 で SQLite を使う"))

    def test_pair_decision_name_only_form(self):
        seed = self.planted()
        name = manas._seed_line_forms(seed)[2]
        self.assertTrue(name.startswith("- 決定(承認): #%d (内容 %d 字・" % (seed["id"], len("案3 で SQLite を使う"))), name)
        self.assertIn("・一部推定", name)
        self.assertNotIn("一部問い", name)

    def test_user_scoped_approval_not_relabeled(self):
        seed = _seed("決定", "依他起", [_ev("assistant", "#148 の方針は候補 (a) です。", 4), _ev("user", "#148 は候補 (a) で GO", 5)])
        self.assertFalse(alaya.approval_only(seed))
        self.assertFalse(manas._render_seed_line(seed).startswith("- 決定(承認)"))

    def test_enjoujitsu_decision_with_go_not_relabeled(self):
        seed = _seed("決定", "圓成實", [_ev("assistant", "実装してよいですか。", 4), _ev("user", "実装GO", 5)])
        self.assertFalse(alaya.approval_only(seed))
        self.assertFalse(manas._render_seed_line(seed).startswith("- 決定(承認)"))

    def test_enjoujitsu_decision_with_assistant_go_evidence_not_relabeled(self):
        seed = _seed("決定", "圓成實", [
            _ev("assistant", "再点火してよければ GO をください。", 1), _ev("user", "GO", 2), _ev("user", "本番の再点火\nGO", 3)])
        self.assertFalse(alaya.approval_only(seed))
        self.assertFalse(manas._render_seed_line(seed).startswith("- 決定(承認)"))
        self.assertFalse(manas._render_index_line(seed).startswith("- 決定(承認)"))

    def test_other_user_span_with_content_not_relabeled(self):
        seed = _seed("決定", "依他起", [
            _ev("user", "AGENTS.mdを整理してファイルを分けて", 1638), _ev("assistant", "整理します。", 1638), _ev("user", "GO", 1636)])
        self.assertFalse(alaya.approval_only(seed))
        self.assertFalse(manas._render_seed_line(seed).startswith("- 決定(承認)"))

    def test_approval_only_requires_decision_kind_and_assistant_evidence(self):
        pair = [_ev("assistant", RECOMMEND, 1), _ev("user", "推奨でGO", 2)]
        self.assertTrue(alaya.approval_only(_seed("決定", "依他起", pair)))
        self.assertFalse(alaya.approval_only(_seed("性向", "依他起", pair)))
        self.assertFalse(alaya.approval_only(_seed("決定", "依他起", [_ev("user", "推奨でGO", 2)])))
        self.assertFalse(alaya.approval_only(_seed("決定", "依他起", [_ev("assistant", RECOMMEND, 1)])))


PAIR = {
    "kind": "決定", "sansho": "依他起", "conditions": ["データベース", "案3"],
    "evidences": [
        {"source": "assistant", "span": "推奨: 案3 で SQLite を使う。", "ledger_id": 1},
        {"source": "user", "span": "推奨でGO", "ledger_id": 2},
    ],
}
SQLITE_RUNS = alaya._input_runs("SQLite の件")


class TestApprovalPairRelevance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apprel_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = MemoryEngine(state_dir=self.tmp)

    def test_pair_seed_relevance_counts_assistant_span(self):
        self.assertEqual(SQLITE_RUNS, {"sqlite"})
        self.assertEqual(alaya._index_relevance(PAIR, SQLITE_RUNS), 6)

    def test_non_pair_decision_ignores_assistant_span(self):
        seed = copy.deepcopy(PAIR)
        seed["evidences"][1]["span"] = "Redis にする"
        self.assertEqual(alaya._index_relevance(seed, SQLITE_RUNS), 0)

    def test_disposition_with_pair_evidence_ignores_assistant_span(self):
        seed = copy.deepcopy(PAIR)
        seed["kind"] = "性向"
        self.assertEqual(alaya._index_relevance(seed, SQLITE_RUNS), 0)

    def test_enjoujitsu_with_assistant_go_evidence_ignores_assistant_span(self):
        seed = copy.deepcopy(PAIR)
        seed["sansho"] = "圓成實"
        self.assertEqual(alaya._index_relevance(seed, SQLITE_RUNS), 0)

    def test_pair_seed_relevance_matches_naive_over_both_sources(self):
        rng = random.Random(1127)
        pool = [chr(c) for c in range(0x4e00, 0x4e00 + 12)] + list("アイウエKafkaabc01/") + ["々"]
        for _ in range(300):
            runs = alaya._input_runs(_rand_text(rng, rng.randint(5, 60), pool))
            seed = {"kind": "決定", "sansho": "依他起", "evidences": [
                {"source": "user", "span": "GO"},
                {"source": "assistant", "span": _rand_text(rng, rng.randint(5, 80), pool)},
            ]}
            expected = _naive({"evidences": [dict(e, source="user") for e in seed["evidences"]]}, runs)
            self.assertEqual(alaya._index_relevance(seed, runs), expected)

    # --- store 経由 (explain_recall) ---
    def _plant(self, user, assistant, conds, content, kind="決定"):
        turn = LedgerTurn(user_text=user, assistant_text=assistant, id=0, project=PROJ)
        ev = [{"source": "user", "span": user}]
        if assistant:
            ev.append({"source": "assistant", "span": assistant})
        r = self.eng.imprint([turn], [{"content": content, "conditions": conds, "polarity": "無記", "kind": kind, "evidence": ev}])
        self.assertTrue(r[0].get("is_new"), r)
        return r[0]["seed"]

    def _store_with_pair(self, assistant="推奨: 案3 で SQLite を使う。データベースは SQLite。", go="推奨でGO"):
        self.enj = [
            self._plant("メッセージキュー は語%02d にする" % i, "", ["語%02d" % i], "キューは語%02d" % i)["id"] for i in range(1, 11)
        ]
        w = [LedgerTurn(user_text="どうしますか", assistant_text=assistant, id=1, project=PROJ),
             LedgerTurn(user_text=go, assistant_text="了解", id=2, project=PROJ)]
        r = self.eng.imprint(w, [{"content": "案3 で SQLite を使う", "conditions": ["データベース", "案3"], "polarity": "無記",
                                  "kind": "決定", "evidence": [{"source": "assistant", "span": assistant}, {"source": "user", "span": go}]}])
        seed = r[0]["seed"]
        self.assertEqual((seed["kind"], seed["sansho"], seed["strength"]), ("決定", "依他起", 2), r[0])
        self.pair_id = seed["id"]

    def _index_ids(self, text):
        r = self.eng.explain_recall(text, project=PROJ)
        return [x["id"] if isinstance(x, dict) else x for x in r["index"]], r

    def test_pair_seed_reaches_index_over_unrelated_enjoujitsu(self):
        self._store_with_pair()
        ids, r = self._index_ids("SQLite の件")
        self.assertEqual(ids[0], self.pair_id)
        self.assertEqual(r["seeds"], [])

    def test_unrelated_input_keeps_enjoujitsu_first(self):
        self._store_with_pair()
        ids, r = self._index_ids("メッセージキュー の件")
        self.assertEqual(ids[0], self.enj[0])
        self.assertNotIn(self.pair_id, ids)
        self.assertEqual(r["seeds"], [])

    def test_general_word_lifts_pair_seed_over_shorter_match(self):
        # 既知の限界 (index-reach 裁定 3 の例外): assistant の span は長いので、一般語だけで承認の対の種子が関わりの正の種子を抜く
        enj = self._plant("ファイル は A方式 にする", "", ["A方式"], "ファイルは A方式")["id"]
        self._plant("メッセージキュー は語01 にする", "", ["語01"], "キューは語01")
        w = [LedgerTurn(user_text="どうしますか", assistant_text="推奨: ファイルの修正は案3 で行う。データベース更新も含む。", id=1, project=PROJ),
             LedgerTurn(user_text="GO", assistant_text="了解", id=2, project=PROJ)]
        r = self.eng.imprint(w, [{"content": "案3 で行う", "conditions": ["データベース", "案3"], "polarity": "無記", "kind": "決定",
                                  "evidence": [{"source": "assistant", "span": "推奨: ファイルの修正は案3 で行う。データベース更新も含む。"},
                                               {"source": "user", "span": "GO"}]}])
        pair = r[0]["seed"]
        self.assertEqual(pair["sansho"], "依他起")
        ids, _ = self._index_ids("ファイル を修正")
        self.assertLess(ids.index(pair["id"]), ids.index(enj))


class TestApprovalNegation(_Base):
    """T5 (spec 2026-11-27-ratified-decision-design §7): 短い否定・却下を承認にしない (N1・N3)・縁を言い直す承認は承認の対の種子にしない ((a))。"""

    def test_reject_marker_turn_is_not_approval(self):
        for text in ("推奨でなく案3", "推奨ではなく案2"):
            self.assertFalse(alaya._is_approval_text(text), text)

    def test_negation_after_approval_word_is_not_approval(self):
        for text in ("推奨では進めない", "推奨でない", "推奨どおりではない", "まだGOしない", "GOしないで", "GOじゃない",
                     "GOはまだ", "やっぱりGOしない", "GOしません"):
            with self.subTest(text=text):
                self.assertFalse(alaya._is_approval_text(text))

    def test_negation_before_approval_word_kept(self):
        self.assertTrue(alaya._is_approval_text("問題ないのでGO"))

    def test_polite_tail_kept(self):
        for text in ("推奨で進めて", "推奨でお願いします", "GOです", "推奨どおりで"):
            with self.subTest(text=text):
                self.assertTrue(alaya._is_approval_text(text))

    def test_negation_after_go_drops_benign_tail(self):
        # 受け入れる取りこぼし
        self.assertFalse(alaya._is_approval_text("GO、問題ない"))

    def test_negation_outside_table_still_counts(self):
        # 受け入れる誤当たり (既知の限界。語表に無い否定)
        self.assertTrue(alaya._is_approval_text("GOは見送り"))

    def test_approval_tail(self):
        self.assertEqual(alaya._approval_tail("まだGOしない"), "しない")
        self.assertEqual(alaya._approval_tail("推奨でGO"), "")
        self.assertEqual(alaya._approval_tail("関係なし"), "")

    def test_negated_go_not_planted_as_pair(self):
        r = self.store.imprint(
            self.prev_window(go="まだGOしない"), _proposal(self.prev_evidence(go="まだGOしない")))
        self.assertEqual(r["kind"], "性向")

    def test_go_with_question_sentence_pairs(self):
        r = self.store.imprint(
            self.prev_window(go="GO。案2は？"), _proposal([("assistant", RECOMMEND), ("user", "GO")]))
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(r["sansho"], "依他起")
        self.assertIn(APPROVAL_PAIR_NOTE, r["reason"])

    def test_restated_decision_not_approval_only(self):
        window = [_turn("どうしますか", RECOMMEND, 1), _turn("案3でGO", "了解です", 2)]
        r = self.store.imprint(window, _proposal([("assistant", RECOMMEND), ("user", "案3でGO")]))
        seed = r["seed"]
        self.assertEqual((seed["kind"], seed["sansho"]), ("決定", "依他起"))
        self.assertNotIn(APPROVAL_PAIR_NOTE, r["reason"])
        self.assertFalse(alaya.approval_only(seed))
        self.assertFalse(manas._render_seed_line(seed).startswith("- 決定(承認)"))
        self.assertEqual(alaya._index_relevance(seed, {"sqlite"}), 0)

    def test_rejection_turn_not_rendered_as_approval(self):
        seed = _seed("決定", "依他起", [_ev("assistant", "推奨: 案2 で Redis を使う。", 1), _ev("user", "推奨でなく案3", 2)],
                     conds=("案3",))
        self.assertFalse(alaya.approval_only(seed))

    def test_condition_inside_approval_word_not_counted(self):
        seed = copy.deepcopy(PAIR)
        seed["conditions"] = ["推奨", "SQLite"]
        self.assertTrue(alaya.approval_only(seed))

    def test_approval_negation_role(self):
        self.assertEqual(
            markers.union("approval_negation"),
            ("ない", "ません", "まだ", "待って", "待て", "保留", "やめ", "止め", "だめ", "ダメ"))
        self.assertEqual(markers.MARKERS["en"]["approval_negation"], ())


if __name__ == "__main__":
    unittest.main()
