"""spec 2026-10-30-doubt-in-decision-summary §7 (#120): 決定 の縁が問いの文にしか無いとき、描画だけ要約を外して
札「一部問い: <縁>」を添える。kind・保存・sansho は変えず、`recall` は ledger を読まない。
LLM は使わない。テストが消すのは自作 tmp だけ (mkdtemp)。"""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, inspect_cli  # noqa: E402
from raiya import manas as manas_mod  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerStore, LedgerTurn  # noqa: E402
from raiya.manas import NARRATIVE_HEADER, _render_entry_line, _render_index_line, _render_seed_line  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import manas_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
E1 = "We use PostgreSQL for the database. I'd like FastAPI for the API layer. Maybe try Celery for jobs?"
E1_CONDS = ["PostgreSQL", "FastAPI", "Celery"]


def _seed(span_list, kind="決定", conditions=None, content="Celery を採用", assistant=None, sansho="圓成實"):
    evidences = [{"source": "user", "span": s, "life": 1, "ledger_id": 3} for s in span_list]
    if assistant:
        evidences.append({"source": "assistant", "span": assistant, "life": 1, "ledger_id": 3})
    return {"id": 1, "kind": kind, "content": content, "strength": 1, "sansho": sansho,
            "conditions": list(E1_CONDS if conditions is None else conditions), "evidences": evidences}


class TestDoubtOnlyConditions(unittest.TestCase):
    """純関数 `alaya.doubt_only_conditions` と、それを使う ①・③ の描画。"""

    def lines(self, seed):
        return _render_seed_line(seed), _render_index_line(seed)

    def test_1_e1_label_replaces_summary(self):
        s = _seed([E1])
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertEqual(self.lines(s), (
            "- 決定: {}(強度1・一部問い: Celery・往復 3)".format(E1),
            "- 決定: {} (強度1・一部問い: Celery・往復 3)".format(E1)))
        for line in self.lines(s):
            self.assertNotIn("Celery を採用", line)
            self.assertNotIn("要約", line)

    def test_2_no_question_sentence_keeps_summary(self):
        s = _seed(["We use PostgreSQL. Use Celery for jobs."])
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertEqual(self.lines(s)[0], "- 決定: We use PostgreSQL. Use Celery for jobs.(強度1・要約: Celery を採用・往復 3)")

    def test_3_question_without_any_condition_keeps_summary(self):
        s = _seed(["認証は JWT にする。これでいい？"], conditions=["JWT"], content="JWT を採用")
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertEqual(self.lines(s), (
            "- 決定: 認証は JWT にする。これでいい？(強度1・要約: JWT を採用・往復 3)",
            "- 決定: 認証は JWT にする。これでいい？ (強度1・要約: JWT を採用・往復 3)"))

    def test_4_condition_outside_question_in_another_user_span_keeps_summary(self):
        s = _seed([E1, "やはり Celery で行く。"])
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertIn("要約: Celery を採用", _render_seed_line(s))
        self.assertNotIn("一部問い", _render_seed_line(s))

    def test_4b_reads_all_user_spans_not_only_the_latest_turn(self):
        older = {"source": "user", "span": "Celery で行く。", "life": 1, "ledger_id": 1}
        latest = {"source": "user", "span": E1, "life": 1, "ledger_id": 9}
        s = _seed([])
        s["evidences"] = [older, latest]
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertIn("要約: Celery を採用", _render_seed_line(s))

    def test_5_mixed_adds_partial_after_label_in_both_paths(self):
        s = _seed([E1], assistant="Celery を入れましょう", sansho="依他起")
        self.assertEqual(self.lines(s), (
            "- 決定: {}(強度1・一部問い: Celery・一部推定・往復 3)".format(E1),
            "- 決定: {} (強度1・一部問い: Celery・一部推定・往復 3)".format(E1)))

    def test_5b_assistant_span_is_not_read(self):
        # assistant の span が疑の文の外に Celery を持っていても、問いのみの判定は user の span だけで決まる
        s = _seed([E1], assistant="Celery で行きます。")
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])

    def test_6_other_kinds_get_no_label(self):
        for kind in ("回避策", "所在", "性向", "再開点"):
            s = _seed([E1], kind=kind)
            self.assertEqual(alaya.doubt_only_conditions(s), [], kind)
            for line in self.lines(s):
                self.assertNotIn("一部問い", line, kind)

    def test_6b_missing_or_invalid_kind_reads_as_disposition(self):
        s = _seed([E1])
        del s["kind"]
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        for line in self.lines(s):
            self.assertNotIn("一部問い", line)

    def test_7_mid_token_question_mark_is_not_a_question(self):
        s = _seed(["Celery は https://x.io/?b=1 を見る"], conditions=["Celery"])
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertNotIn("一部問い", _render_seed_line(s))

    def test_8_fullwidth_question_mark_labels(self):
        s = _seed(["PostgreSQL にする。Celery も入れる？"], conditions=["PostgreSQL", "Celery"], content="Celery を採用")
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertEqual(self.lines(s)[0], "- 決定: PostgreSQL にする。Celery も入れる？(強度1・一部問い: Celery・往復 3)")

    def test_9_multiple_conditions_in_seed_order_with_seed_spelling(self):
        span = "DB は SQLite にする。celery と redis は入れる？ POSTGRES は？"
        s = _seed([span], conditions=["Redis", "SQLite", "Celery", "unrelated"])
        self.assertEqual(alaya.doubt_only_conditions(s), ["Redis", "Celery"])
        self.assertTrue(_render_seed_line(s).endswith("(強度1・一部問い: Redis・Celery・往復 3)"))
        self.assertTrue(_render_index_line(s).endswith(" (強度1・一部問い: Redis・Celery・往復 3)"))

    def test_10_span_with_question_mark_cut_by_llm_gets_no_label(self):
        s = _seed(["We use PostgreSQL for the database. I'd like FastAPI for the API layer. Maybe try Celery for jobs"])
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertNotIn("一部問い", _render_seed_line(s))

    def test_11_whitespace_only_condition_is_never_listed(self):
        s = _seed([E1], conditions=["  ", "　", "Celery"])
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertIn("一部問い: Celery・往復 3)", _render_seed_line(s))

    def test_condition_absent_from_every_span_is_not_counted(self):
        s = _seed([E1], conditions=["Kafka"])
        self.assertEqual(alaya.doubt_only_conditions(s), [])

    def test_no_user_span_has_no_label_and_old_forms_are_unchanged(self):
        s = _seed([], assistant="Celery？", sansho="依他起")
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertEqual(_render_seed_line(s), "- 決定(推定): Celery を採用(強度1・往復 3)")

    def test_label_value_is_whitespace_collapsed(self):
        s = _seed(["PostgreSQL にする。Celery\tを入れる？"], conditions=["PostgreSQL", "Celery \u3000 を\n入れる"])
        self.assertTrue(_render_seed_line(s).endswith("一部問い: Celery を 入れる・往復 3)"), _render_seed_line(s))

    def test_malformed_seed_does_not_raise(self):
        self.assertEqual(alaya.doubt_only_conditions({}), [])
        self.assertEqual(alaya.doubt_only_conditions({"kind": "決定", "conditions": "x", "evidences": "y"}), [])
        self.assertEqual(alaya.doubt_only_conditions(
            {"kind": "決定", "conditions": [1, None], "evidences": [None, {"source": "user", "span": 5}]}), [])

    # --- F3: 失敗経路 (ガードに届く入力) ---
    def test_conditions_not_a_list_with_valid_user_span_gives_empty_and_keeps_summary(self):
        for bad in (None, "Celery", 5, {"a": 1}):
            s = _seed([E1])
            s["conditions"] = bad
            self.assertEqual(alaya.doubt_only_conditions(s), [], bad)
            self.assertIn("要約: Celery を採用", _render_seed_line(s), bad)

    def test_non_str_conditions_are_skipped_but_valid_ones_count(self):
        self.assertEqual(alaya.doubt_only_conditions(_seed([E1], conditions=[1, None, "Celery"])), ["Celery"])

    def test_blank_condition_does_not_hide_behind_blank_remainder(self):
        s = _seed(["Maybe Celery?"], conditions=["  ", "Celery"], content="x")
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertTrue(_render_seed_line(s).endswith("(強度1・一部問い: Celery・往復 3)"))
        self.assertNotIn("・・", _render_seed_line(s))
        self.assertNotIn(": )", _render_seed_line(s))

    def test_junk_evidence_items_are_skipped_and_only_the_valid_span_decides(self):
        s = _seed([])
        s["evidences"] = [None, "x", {"source": "user", "span": 5}, {"source": "user", "span": ""},
                          {"source": "assistant", "span": "Celery で行く。"},
                          {"source": "user", "span": E1}]
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertIn("一部問い: Celery", _render_seed_line(s))

    def test_non_dict_seed_gives_empty(self):
        for bad in (None, "x", 5, [], ("x",)):
            self.assertEqual(alaya.doubt_only_conditions(bad), [], bad)

    # --- F4・F5 ---
    def test_condition_spanning_two_sentences_is_not_a_question_only_condition(self):
        s = _seed(["Is it A? B is fine."], conditions=["A? B"])
        self.assertEqual(alaya.doubt_only_conditions(s), [])
        self.assertIn("要約: Celery を採用", _render_seed_line(s))

    def test_duplicate_conditions_are_listed_once_with_first_spelling(self):
        self.assertEqual(alaya.doubt_only_conditions(_seed([E1], conditions=["Celery", "Celery"])), ["Celery"])
        s = _seed([E1], conditions=["Celery", "celery", "CELERY"])
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertTrue(_render_seed_line(s).endswith("一部問い: Celery・往復 3)"))
        s = _seed([E1], conditions=["celery", "Celery"])
        self.assertEqual(alaya.doubt_only_conditions(s), ["celery"])

    # --- 現状の挙動の pin ---
    def test_pin_nfkc_condition_matches_and_keeps_seed_spelling(self):
        self.assertEqual(alaya.doubt_only_conditions(_seed([E1], conditions=["Ｃｅｌｅｒｙ"])), ["Ｃｅｌｅｒｙ"])

    def test_pin_one_character_condition_counts(self):
        s = _seed(["Maybe a lot of jobs? Done."], conditions=["a"])
        self.assertEqual(alaya.doubt_only_conditions(s), ["a"])

    def test_pin_condition_that_is_a_substring_of_another_is_listed_separately(self):
        s = _seed(["Use Postgres. Maybe Celery?"], conditions=["Cele", "Celery", "Postgres"])
        self.assertEqual(alaya.doubt_only_conditions(s), ["Cele", "Celery"])

    # --- ③ 群の代表行・容量 ---
    def test_group_representative_line_keeps_the_label(self):
        s = _seed([E1])
        s["id"] = 7
        group = {"label": "Celery", "kind": "決定", "project": PROJ, "size": 3, "representative": 7, "member_ids": [7, 8, 9]}
        self.assertEqual(
            _render_entry_line({"seed": s, "group": group}),
            "- 決定: {} (強度1・一部問い: Celery・往復 3) [群「Celery」3 件・代表 #7]".format(E1))
        self.assertEqual(_render_entry_line({"seed": s, "group": None}), _render_index_line(s))

    # --- F2 裁定 (2026-10-31): 断定の部分が行から消えるときは要約を残す ---
    def seed2(self, spans, conditions, content="要約X", assistant=None):
        """spans = [(span, ledger_id)] を古い順に。最新の接地往復は最後の ledger_id。"""
        s = _seed([], conditions=conditions, content=content, assistant=assistant)
        users = [{"source": "user", "span": t, "life": 1, "ledger_id": i} for t, i in spans]
        s["evidences"] = users + s["evidences"]
        return s

    def test_keep_1_all_assertive_conditions_in_shown_span_drops_summary(self):
        s = _seed([E1])   # x17 e1 型
        self.assertEqual(self.lines(s), (
            "- 決定: {}(強度1・一部問い: Celery・往復 3)".format(E1), "- 決定: {} (強度1・一部問い: Celery・往復 3)".format(E1)))

    def test_keep_2_empty_S_drops_summary(self):
        s = self.seed2([("Maybe try Redis?", 1)], ["Redis", "Kafka"])   # x16 e2 #6 型。Kafka はどの span にも無い
        self.assertEqual(alaya.doubt_judgement(s, [s["evidences"][0]]), (["Redis"], False))
        self.assertEqual(self.lines(s), ("- 決定: Maybe try Redis?(強度1・一部問い: Redis・往復 1)",
                                         "- 決定: Maybe try Redis? (強度1・一部問い: Redis・往復 1)"))

    def test_keep_3_assertion_only_in_an_older_span_keeps_summary(self):   # fixture #28 型
        s = self.seed2([("だってアマテラスが不在はシステムの危機だし", 1), ("これは緊急集会の扱いで別じゃね？", 2)],
                       ["アマテラス", "不在", "緊急集会"], content="緊急集会を別に扱う")
        self.assertEqual(self.lines(s), (
            "- 決定: これは緊急集会の扱いで別じゃね？(強度1・要約: 緊急集会を別に扱う・一部問い: 緊急集会・往復 2)",
            "- 決定: これは緊急集会の扱いで別じゃね？ (強度1・要約: 緊急集会を別に扱う・一部問い: 緊急集会・往復 2)"))

    def test_keep_4_reverse_older_question_latest_assertion_drops_summary(self):
        s = self.seed2([("Maybe try Celery?", 1), ("We use PostgreSQL for the database.", 2)],
                       ["PostgreSQL", "Celery"], content="PostgreSQL と Celery")
        self.assertEqual(alaya.doubt_only_conditions(s), ["Celery"])
        self.assertEqual(self.lines(s), (
            "- 決定: We use PostgreSQL for the database.(強度1・一部問い: Celery・往復 2)",
            "- 決定: We use PostgreSQL for the database. (強度1・一部問い: Celery・往復 2)"))

    def test_keep_5_assertion_only_in_a_question_of_the_latest_span_keeps_summary(self):
        s = self.seed2([("Postgres にする。Celery を使う？", 1), ("Postgres でいい？", 2)],
                       ["Postgres", "Celery"], content="Postgres を採用")
        self.assertEqual(self.lines(s), (
            "- 決定: Postgres でいい？(強度1・要約: Postgres を採用・一部問い: Celery・往復 2)",
            "- 決定: Postgres でいい？ (強度1・要約: Postgres を採用・一部問い: Celery・往復 2)"))

    def test_keep_6_mixed_order_is_summary_then_doubt_then_partial(self):
        s = self.seed2([("だってアマテラスが不在はシステムの危機だし", 1), ("これは緊急集会の扱いで別じゃね？", 2)],
                       ["アマテラス", "不在", "緊急集会"], content="要約X", assistant="緊急集会を別にします")
        self.assertEqual(self.lines(s), (
            "- 決定: これは緊急集会の扱いで別じゃね？(強度1・要約: 要約X・一部問い: 緊急集会・一部推定・往復 2)",
            "- 決定: これは緊急集会の扱いで別じゃね？ (強度1・要約: 要約X・一部問い: 緊急集会・一部推定・往復 2)"))

    def test_keep_7_shown_spans_are_the_latest_turn_only_and_joined(self):
        # 最新の往復の user span が 2 本 (描く逐語は両方): 片方の残りに S の縁があれば要約は外す
        s = self.seed2([("Postgres にする。", 1), ("Celery を使う？", 2), ("Postgres で行く。", 2)],
                       ["Postgres", "Celery"], content="要約X")
        self.assertEqual(self.lines(s)[0], "- 決定: Celery を使う？ … Postgres で行く。(強度1・一部問い: Celery・往復 2)")

    def test_keep_8_judgement_scans_each_span_once(self):
        s = self.seed2([("だってアマテラスが不在はシステムの危機だし", 1), ("これは緊急集会の扱いで別じゃね？", 2)],
                       ["アマテラス", "不在", "緊急集会"])
        with mock.patch.object(alaya, "_sentence_pieces", wraps=alaya._sentence_pieces) as spy:
            manas_mod._doubt_tag(s)
        self.assertEqual(spy.call_count, 2)   # user span の数だけ (D と keep を同じ走査で出す)

    def test_keep_9_any_condition_of_S_outside_shown_keeps_summary_in_any_order(self):
        # Postgres は古い span の断定にだけ、Redis は最新の span の断定にある。S の 1 語でも描く逐語の外なら要約を残す
        spans = [("Postgres にする。", 1), ("Redis で行く。Celery は？", 2)]
        for conds in (["Postgres", "Redis", "Celery"], ["Celery", "Redis", "Postgres"], ["Redis", "Postgres", "Celery"]):
            s = self.seed2(spans, conds, content="要約X")
            self.assertEqual(alaya.doubt_judgement(s, [s["evidences"][1]]), (["Celery"], True), conds)
            self.assertEqual(self.lines(s), (
                "- 決定: Redis で行く。Celery は？(強度1・要約: 要約X・一部問い: Celery・往復 2)",
                "- 決定: Redis で行く。Celery は？ (強度1・要約: 要約X・一部問い: Celery・往復 2)"), conds)

    def test_keep_10_no_S_condition_outside_shown_drops_summary_in_any_order(self):
        spans = [("Postgres にする。", 1), ("Postgres と Redis で行く。Celery は？", 2)]
        for conds in (["Postgres", "Redis", "Celery"], ["Celery", "Redis", "Postgres"]):
            s = self.seed2(spans, conds)
            self.assertEqual(alaya.doubt_judgement(s, [s["evidences"][1]]), (["Celery"], False), conds)

    def test_judgement_empty_D_gives_false_even_when_S_is_outside_shown(self):
        s = self.seed2([("Postgres にする。", 1), ("Redis で行く。", 2)], ["Postgres", "Redis"])
        self.assertEqual(alaya.doubt_judgement(s, [s["evidences"][1]]), ([], False))
        self.assertIn("要約: 要約X", _render_seed_line(s))   # 札なし = 従来の要約つき

    def test_judgement_accepts_shown_none(self):
        s = self.seed2([("だってアマテラスが不在はシステムの危機だし", 1), ("これは緊急集会の扱いで別じゃね？", 2)],
                       ["アマテラス", "不在", "緊急集会"])
        self.assertEqual(alaya.doubt_judgement(s, None), (["緊急集会"], True))   # shown 無し = 描く span の残りに S は無い
        self.assertEqual(alaya.doubt_judgement(s), (["緊急集会"], True))

    def test_condition_across_two_question_sentences_is_not_in_D(self):
        s = self.seed2([("Aか？Bか？", 1)], ["か？B"])   # 文の間に空白が無い形でないと、"" でつなぐ変異を検出できない
        self.assertEqual(alaya.doubt_judgement(s, [s["evidences"][0]]), ([], False))

    def test_judgement_of_non_decision_or_malformed_is_empty(self):
        self.assertEqual(alaya.doubt_judgement(_seed([E1], kind="性向"), []), ([], False))
        self.assertEqual(alaya.doubt_judgement(None), ([], False))
        s = _seed([E1])
        s["conditions"] = None
        self.assertEqual(alaya.doubt_judgement(s, []), ([], False))


class TestComposeWithLabel(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_doubt_compose_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = manas_store_at(os.path.join(self.tmpdir, "manas.json"))

    def seeds(self, n, span=E1):
        out = []
        for i in range(n):
            s = _seed([span], conditions=["PostgreSQL", "Celery"])
            s["id"] = i + 1
            out.append(s)
        return out

    def test_labelled_seeds_respect_max_chars_and_cut_counts(self):
        fired = self.seeds(1)
        entries = [{"seed": s, "group": None} for s in self.seeds(6)[1:]]
        full = self.store.compose_injection(fired, entries, len(entries), max_chars=100000)
        self.assertEqual(full["cut"], {"germinated": 0, "siblings": 0, "index": 0, "global": 0})
        self.assertEqual(full["text"].count("一部問い: Celery"), 6)
        self.assertNotIn("Celery を採用", full["text"])
        seen_cuts = set()
        for limit in range(len(full["text"]) - 1, len(NARRATIVE_HEADER), -7):
            out = self.store.compose_injection(fired, entries, len(entries), max_chars=limit)
            self.assertLessEqual(len(out["text"]), limit)
            self.assertEqual(out["cut"]["index"], len(entries) - len(out["index_seeds"]))
            self.assertEqual(out["cut"]["germinated"], 1 - len(out["shown_seed_ids"]))
            self.assertEqual(out["text"].count("一部問い: Celery"), len(out["shown_seed_ids"]) + len(out["index_seeds"]))
            self.assertNotIn("Celery を採用", out["text"])
            seen_cuts.add(out["cut"]["index"])
        self.assertTrue({1, 2, 3, 4} <= seen_cuts, seen_cuts)

    def test_limit_below_the_first_germinated_line_cuts_it(self):
        fired = self.seeds(2)
        header_only = self.store.compose_injection([], [], None)
        limit = len(NARRATIVE_HEADER) + 10
        out = self.store.compose_injection(fired, [], None, max_chars=limit)
        self.assertLessEqual(len(out["text"]), limit)
        self.assertEqual(out["cut"]["germinated"], 2)
        self.assertEqual(header_only["text"], "")


def _long_question_span(n_chars):
    unit = "We keep PostgreSQL for storage here. Maybe try Celery for jobs? "
    return (unit * (n_chars // len(unit) + 1))[:n_chars]


class TestRecallCostAndInspect(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".raiya"))
        patcher = mock.patch.dict(os.environ, {"HOME": home, "RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.addCleanup(patcher.stop)
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def seeds15(self, n_spans=5, span_len=3000):
        seeds = []
        for i in range(1, 16):
            ev = [{"source": "user", "span": _long_question_span(span_len) + str(j), "life": 1, "ledger_id": j + 1}
                  for j in range(n_spans)]
            # 描く逐語は最新の往復の span (短い) だけ。長い span は札の判定が全部読む
            ev.append({"source": "user", "span": "短い。Topic%d は？" % i, "life": 1, "ledger_id": n_spans + 10})
            seeds.append({
                "id": i, "content": "要約%d" % i, "strength": 3,
                "conditions": (["話題語", "Topic%d" % i, "Celery"] if i <= 5 else ["無関係%d" % i, "Topic%d" % i, "Celery%d" % i]),
                "polarity": "無記", "sansho": "圓成實", "evidences": ev, "history": [], "planted_at": 1,
                "last_fired": None, "fire_count": 0, "miss_streak": 0, "kind": "決定", "project": PROJ,
                "superseded_by": None})
        return seeds

    def test_recall_judges_each_rendered_item_once(self):
        """compose_injection の fits() は項目を足すたびに render() を呼び直す。札の判定が行ごとに 1 度で済むことを
        呼び出し回数で pin する (時間に依らず検出するため)。"""
        write_state(self.state, {"seeds": self.seeds15(), "alaya_meta": {"life": 5}})
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        calls = []
        original = manas_mod.doubt_judgement

        def counting(seed, shown=()):
            calls.append(seed.get("id"))
            return original(seed, shown)

        with mock.patch.object(manas_mod, "doubt_judgement", counting):
            r = eng.recall("話題語 について", project=PROJ)
        rendered = len(r["seeds"]) + len(r["index"])
        self.assertGreater(len(r["seeds"]), 0)
        self.assertGreater(len(r["index"]), 0)
        self.assertIn("一部問い: Topic", r["narrative"])
        self.assertLessEqual(len(calls), rendered, (len(calls), rendered))
        self.assertEqual(len(calls), len(set(calls)), "同じ種子の札を 2 度判定している")

    def test_recall_does_not_read_ledger_rows_for_labelled_seeds(self):
        write_state(self.state, {"seeds": self.seeds15(2, 500), "alaya_meta": {"life": 5}})
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        calls = []
        original = LedgerStore._read_rows

        def counting(self_):
            calls.append(1)
            return original(self_)

        with mock.patch.object(LedgerStore, "_read_rows", counting):
            r = eng.recall("話題語 について", project=PROJ)
        self.assertIn("一部問い: Topic", r["narrative"])
        self.assertEqual(calls, [])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inspect_cli.main(["--state-dir", self.state] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_inspect_text_shows_content_and_label_when_summary_is_kept(self):   # #28 型
        seed = _seed([])
        seed["evidences"] = [
            {"source": "user", "span": "だってアマテラスが不在はシステムの危機だし", "life": 1, "ledger_id": 1},
            {"source": "user", "span": "これは緊急集会の扱いで別じゃね？", "life": 1, "ledger_id": 2}]
        seed.update({"conditions": ["アマテラス", "不在", "緊急集会"], "content": "緊急集会を別に扱う", "project": PROJ,
                     "superseded_by": None, "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0,
                     "miss_streak": 0, "polarity": "無記"})
        write_state(self.state, {"seeds": [seed], "alaya_meta": {"life": 5}})
        for argv in (["show", "--project", PROJ], ["why", "1"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("要約: 緊急集会を別に扱う・一部問い: 緊急集会", out, argv)

    def test_inspect_text_hides_content_json_and_explain_show_it(self):
        seed = _seed([E1])
        seed.update({"project": PROJ, "superseded_by": None, "history": [], "planted_at": 1, "last_fired": None,
                     "fire_count": 0, "miss_streak": 0, "polarity": "無記"})
        write_state(self.state, {"seeds": [seed], "alaya_meta": {"life": 5}})
        for argv in (["show", "--project", PROJ], ["why", "1"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("一部問い: Celery", out, argv)
            self.assertNotIn("Celery を採用", out, argv)
        for argv in (["--json", "show"], ["--json", "why", "1"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("Celery を採用", out, argv)
        code, out, _ = self.run_cli("explain", "PostgreSQL", "--project", PROJ)
        self.assertEqual(code, 0)
        self.assertIn("Celery を採用", out)
        self.assertIn("一部問い: Celery", out)
        code, out, _ = self.run_cli("--json", "explain", "PostgreSQL", "--project", PROJ)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([g["content"] for g in data["germinated"]], ["Celery を採用"])
        self.assertIn("一部問い: Celery", data["narrative"])
        self.assertNotIn("Celery を採用", data["narrative"])


class TestRecallRendersLabel(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".raiya"))
        patcher = mock.patch.dict(os.environ, {"HOME": home, "RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.addCleanup(patcher.stop)
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def plant_e1(self):
        turn = LedgerTurn(user_text=E1, assistant_text="了解しました。", id=0, project=PROJ)
        prop = {"content": "Celery を採用", "conditions": list(E1_CONDS), "polarity": "無記", "kind": "決定",
                "evidence": [{"source": "user", "span": E1}]}
        res = self.eng.imprint([turn], [prop])[0]
        self.assertEqual(res["seed"]["kind"], "決定", res)
        self.assertEqual(res["seed"]["sansho"], "圓成實", res)

    def body(self, r):
        return [ln for ln in r["narrative"].split("\n") if ln.startswith("- ")]

    def test_12_recall_germinated_and_index_show_label_and_store_is_untouched(self):
        self.plant_e1()
        before = self.eng.inspect_seeds()
        germinated = self.eng.recall("PostgreSQL について", project=PROJ)
        self.assertEqual(len(germinated["seeds"]), 1)
        self.assertEqual(self.body(germinated), ["- 決定: {}(強度1・一部問い: Celery)".format(E1)])
        indexed = self.eng.recall("全く別の話題", project=PROJ)
        self.assertEqual(indexed["seeds"], [])
        self.assertEqual(self.body(indexed), ["- 決定: {} (強度1・一部問い: Celery)".format(E1)])
        for r in (germinated, indexed):
            self.assertNotIn("Celery を採用", r["narrative"])
        after = self.eng.inspect_seeds()
        # recall の観測欄 (miss_streak など) 以外は不変: content・key 集合・evidences・conditions
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["content"], "Celery を採用")
        self.assertEqual(set(after[0]), set(before[0]))
        self.assertEqual(after[0]["evidences"], before[0]["evidences"])
        self.assertEqual(after[0]["conditions"], before[0]["conditions"])
        self.assertFalse(any("問い" in k for k in after[0]))

    def test_12d_real_engine_assertion_only_in_older_span_keeps_summary(self):   # fixture #28 型
        t0 = LedgerTurn(user_text="だってアマテラスが不在はシステムの危機だし", assistant_text="はい。", id=0, project=PROJ)
        t1 = LedgerTurn(user_text="これは緊急集会の扱いで別じゃね？", assistant_text="なるほど。", id=1, project=PROJ)
        prop = {"content": "天照の不在はシステムの危機として扱い、緊急集会を別に扱う", "polarity": "無記", "kind": "決定",
                "conditions": ["アマテラス", "不在", "緊急集会"],
                "evidence": [{"source": "user", "span": "だってアマテラスが不在はシステムの危機だし"},
                             {"source": "user", "span": "これは緊急集会の扱いで別じゃね？"}]}
        seed = self.eng.imprint([t0, t1], [prop])[0]["seed"]
        self.assertEqual((seed["kind"], seed["sansho"]), ("決定", "圓成實"), seed)
        before = self.eng.inspect_seeds()
        want = ["- 決定: これは緊急集会の扱いで別じゃね？(強度2・要約: {}・一部問い: 緊急集会・往復 1)".format(prop["content"])]
        for prompt in ("アマテラス について",):
            recalled = self.eng.recall(prompt, project=PROJ)
            self.assertEqual(self.body(recalled), want)
            self.assertEqual(self.eng.explain_recall(prompt, project=PROJ)["narrative"], recalled["narrative"])
        indexed = self.eng.recall("全く別の話題", project=PROJ)
        self.assertEqual(self.body(indexed), [want[0].replace("じゃね？(", "じゃね？ (")])
        self.assertEqual(self.eng.explain_recall("全く別の話題", project=PROJ)["narrative"], indexed["narrative"])
        after = self.eng.inspect_seeds()
        self.assertEqual(after[0]["content"], prop["content"])
        self.assertEqual(set(after[0]), set(before[0]))

    def test_12b_explain_recall_equals_recall(self):
        self.plant_e1()
        for prompt in ("PostgreSQL について", "全く別の話題"):
            explained = self.eng.explain_recall(prompt, project=PROJ)
            recalled = self.eng.recall(prompt, project=PROJ)
            self.assertEqual(explained["narrative"], recalled["narrative"])
            self.assertIn("一部問い: Celery", recalled["narrative"])

    def test_12c_inspect_show_and_explain_json_still_expose_content(self):
        self.plant_e1()
        shown = self.eng.inspect_seeds()
        self.assertEqual(shown[0]["content"], "Celery を採用")
        explained = self.eng.explain_recall("PostgreSQL について", project=PROJ)
        self.assertEqual([g["content"] for g in explained["germinated"]], ["Celery を採用"])


if __name__ == "__main__":
    unittest.main()
