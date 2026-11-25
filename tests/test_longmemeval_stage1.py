"""scripts/longmemeval_stage1.py (LongMemEval 段 1・決定論) を手書きの小さな英語の例で回す。LLM は使わない。

データ本文は使わない (LongMemEval 形の欄だけを真似た手書きの例)。出力は自作の tmp だけ。
"""
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

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import longmemeval_stage1 as lm  # noqa: E402


def _t(role, content, ha=None):
    d = {"role": role, "content": content}
    if ha is not None:
        d["has_answer"] = ha
    return d


def _q(qid, qtype, question, answer, sessions):
    return {
        "question_id": qid, "question_type": qtype, "question": question, "answer": answer,
        "question_date": "2023/05/30 (Tue) 10:00", "answer_session_ids": ["s1"],
        "haystack_dates": ["2023/05/20 (Sat) 10:00"] * len(sessions),
        "haystack_session_ids": ["s{}".format(i + 1) for i in range(len(sessions))],
        "haystack_sessions": sessions,
    }


def _fixture():
    garden = [_t("user", "Tell me about gardening tools for tomatoes.", False), _t("assistant", "Use a trowel.", False)]
    return [
        # q1: user の根拠 1 本。縁 A = switched, tuesdays / 縁 B = yoga
        _q("q1", "single-session-user", "What replaced the gym on Tuesdays, instead of lifting?", "yoga", [
            [_t("user", "I switched to yoga classes on Tuesdays and skipped the gym.", True),
             _t("assistant", "Great choice, yoga is relaxing.", False)],
            garden,
        ]),
        # q2: _abs で has_answer 0 本 (assistant が先頭・同じ role が続く・空の turn を含む)
        _q("q2_abs", "multi-session", "Which pottery wheel did I buy?", "You did not mention it.", [
            [_t("assistant", "Welcome back.", False), _t("user", "Hello there.", False),
             _t("user", "", False), _t("assistant", "How can I help?", False)],
        ]),
        # q3: _abs だが has_answer がある
        _q("q3_abs", "multi-session", "Which bicycle did I sell?", "not mentioned", [
            [_t("user", "I sold my old bicycle to a neighbour last month.", True), _t("assistant", "Okay.", False)],
        ]),
        # q4: 根拠が assistant だけ
        _q("q4", "single-session-assistant", "Which museum did you recommend?", "the Louvre", [
            [_t("user", "Any suggestions for Paris?", False),
             _t("assistant", "I recommend the Louvre museum for its paintings.", True)],
        ]),
        # q5: user の根拠が疑問文だけ (kind 決定 の形が無い)
        _q("q5", "knowledge-update", "Do I still go to yoga?", "yes", [
            [_t("user", "Do I still go to yoga?", True), _t("assistant", "You said so.", False)],
        ]),
    ]


def _write(tmp, data, name="data.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _run(argv):
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = lm.main(argv)
    return code, err.getvalue()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_lme_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ.pop("RAIYA_INJECT_DISPOSITIONS", None)   # 漏れの検出の基準を毎回「無し」にそろえる
        self.env_before = (os.environ.get("RAIYA_CONFIG"), os.environ.get("RAIYA_INJECT_DISPOSITIONS"))

    def tearDown(self):
        self.assertEqual(self.env_before, (os.environ.get("RAIYA_CONFIG"), os.environ.get("RAIYA_INJECT_DISPOSITIONS")))

    def run_fixture(self, data=None, extra=(), out_name="out"):
        data_path = _write(self.tmp, data if data is not None else _fixture())
        out = os.path.join(self.tmp, out_name)
        code, err = _run(["--data", data_path, "--out", out, "--name", "r.json"] + list(extra))
        self.assertEqual(code, 0, err)
        with open(os.path.join(out, "r.json"), encoding="utf-8") as f:
            return json.load(f)


class TestRoundTrips(unittest.TestCase):
    def test_pairing_rules_and_shape_counts(self):
        sessions = [
            [_t("user", "u1"), _t("assistant", "a1"), _t("user", "u2")],                  # 末尾の user
            [_t("assistant", "a0"), _t("user", "u3"), _t("assistant", "a3")],              # 先頭が assistant
            [_t("user", "u4"), _t("user", ""), _t("assistant", "a4")],                    # user が続く・空
        ]
        rts, shape = lm.build_round_trips(["s1", "s2", "s3"], sessions)
        got = [(r["session_id"], r["user"], r["assistant"]) for r in rts]
        self.assertEqual(
            got,
            [("s1", "u1", "a1"), ("s1", "u2", ""), ("s2", "", "a0"), ("s2", "u3", "a3"),
             ("s3", "u4", ""), ("s3", "", "a4")],
        )
        self.assertEqual(shape["sessions"], 3)
        self.assertEqual(shape["first_assistant"], 1)
        self.assertEqual(shape["non_alternating"], 2)   # s2 (先頭 assistant は交互でない) と s3
        self.assertEqual(shape["empty_turns"], 1)


class TestConditions(unittest.TestCase):
    TURN = "I switched to yoga classes on Tuesdays and skipped the gym."

    def test_arm_a_is_turn_and_question_words(self):
        self.assertEqual(lm.choose_conditions("A", self.TURN, "What class did I switch to on Tuesdays?", "yoga"),
                         ["Tuesdays"])

    def test_arm_b_is_turn_and_answer_words_and_never_reads_question(self):
        self.assertEqual(lm.choose_conditions("B", self.TURN, "What class did I switch to on Tuesdays?", "yoga"),
                         ["yoga"])
        self.assertEqual(lm.choose_conditions("B", self.TURN, "unrelated words", "yoga"), ["yoga"])

    def test_stopwords_short_words_and_digits_are_dropped_and_answer_may_be_int(self):
        self.assertEqual(lm.choose_conditions("A", "the and was 42 ab", "the and was 42 ab", ""), [])
        self.assertEqual(lm.choose_conditions("B", "I have 3 cats", "q", 3), [])
        self.assertEqual(lm.choose_conditions("B", "I have three cats", "q", "Cats"), ["cats"])

    def test_cap(self):
        words = " ".join("word{}x".format(i) for i in range(20))
        self.assertEqual(len(lm.choose_conditions("A", words, words, "")), lm.MAX_CONDITIONS)


class TestClassify(unittest.TestCase):
    def kw(self, **over):
        base = dict(has_marker=True, has_user_marker=True, is_abs=False, planted=True, injected=True, failed=False)
        base.update(over)
        return lm.classify(**base)

    def test_precedence_follows_the_stated_order(self):
        self.assertEqual(self.kw(), "injected")
        self.assertEqual(self.kw(injected=False), "planted_not_injected")
        self.assertEqual(self.kw(planted=False, injected=False), "all_rejected")
        self.assertEqual(self.kw(is_abs=True), "abs")
        self.assertEqual(self.kw(is_abs=True, has_user_marker=False), "assistant_only")
        self.assertEqual(self.kw(has_user_marker=False), "assistant_only")
        self.assertEqual(self.kw(has_marker=False, has_user_marker=False, is_abs=True), "no_marker")
        self.assertEqual(self.kw(failed=True), "failed")

    def test_merged_only_precedence(self):
        self.assertEqual(self.kw(is_abs=True, planted=False, injected=False, merged_only=True), "abs")
        self.assertEqual(self.kw(planted=False, injected=False, merged_only=True), "merged_into_distractor")
        self.assertEqual(self.kw(planted=True, injected=True, merged_only=True), "injected")
        self.assertEqual(self.kw(planted=True, injected=False, merged_only=True), "planted_not_injected")


class TestRenderedFirstCircle(unittest.TestCase):
    def test_keeps_given_order_within_class_limited_then_cut_from_the_tail(self):
        # `explain_recall` の `seeds` は発芽の順 (class → 一致した縁の数 → 強度)。class だけで安定に並べる (T4)
        seeds = [
            {"id": 1, "sansho": "依他起", "strength": 5},
            {"id": 2, "sansho": "圓成實", "strength": 1},
            {"id": 3, "sansho": "圓成實", "strength": 3},
            {"id": 4, "sansho": "依他起", "strength": 2},
        ]
        self.assertEqual(lm.rendered_first_circle(seeds, 0, limit=3), [2, 3, 1])
        self.assertEqual(lm.rendered_first_circle(seeds, 1, limit=3), [2, 3])
        self.assertEqual(lm.rendered_first_circle(seeds, 0, limit=10), [2, 3, 1, 4])
        self.assertEqual(lm.rendered_first_circle(seeds, 9, limit=3), [])

    def test_rendered_first_circle_keeps_given_order_within_class(self):
        self.assertEqual(lm.rendered_first_circle(
            [{"id": 2, "sansho": "圓成實", "strength": 1}, {"id": 1, "sansho": "圓成實", "strength": 3}], 0, limit=5), [2, 1])
        self.assertEqual(lm.rendered_first_circle(
            [{"id": 5, "sansho": "依他起", "strength": 9}, {"id": 6, "sansho": "圓成實", "strength": 1}], 0, limit=5), [6, 5])


class TestFailClosed(_Base):
    def _assert_refused(self, data_path):
        out = os.path.join(self.tmp, "never")
        code, err = _run(["--data", data_path, "--out", out])
        self.assertNotEqual(code, 0)
        self.assertEqual(len([l for l in err.strip().splitlines() if l.strip()]), 1, err)
        self.assertFalse(os.path.exists(out))

    def test_missing_file(self):
        self._assert_refused(os.path.join(self.tmp, "nope.json"))

    def test_broken_json(self):
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w") as f:
            f.write("{not json")
        self._assert_refused(p)

    def test_missing_field_in_the_last_question_writes_nothing(self):
        data = _fixture()
        del data[-1]["answer"]
        self._assert_refused(_write(self.tmp, data))

    def test_missing_field_in_a_turn(self):
        data = _fixture()
        del data[0]["haystack_sessions"][0][0]["role"]
        self._assert_refused(_write(self.tmp, data))

    def test_not_a_list(self):
        self._assert_refused(_write(self.tmp, {"a": 1}))


class TestRun(_Base):
    def test_classification_per_arm(self):
        r = self.run_fixture()
        rows = {row["question_id"]: row for row in r["rows"]}
        cls = lambda qid, arm: rows[qid]["arms"][arm]["class"]  # noqa: E731
        self.assertEqual(cls("q1", "A-a"), "injected")
        self.assertEqual(cls("q1", "A-b"), "planted_not_injected")   # 性向は既定で注入しない
        self.assertEqual(cls("q1", "A-c"), "injected")
        self.assertEqual(cls("q2_abs", "A-a"), "no_marker")
        self.assertEqual(cls("q3_abs", "A-a"), "abs")
        self.assertEqual(cls("q4", "A-a"), "assistant_only")
        self.assertEqual(cls("q5", "A-a"), "all_rejected")           # 疑問文の span は 決定 の形が無い

    def test_summary_sums_to_total_per_type_and_arm(self):
        r = self.run_fixture()
        n_by_type = {}
        for row in r["rows"]:
            n_by_type[row["question_type"]] = n_by_type.get(row["question_type"], 0) + 1
        for qtype, arms in r["summary"]["by_type"].items():
            for arm, counts in arms.items():
                self.assertEqual(sum(counts["classes"].values()), n_by_type[qtype], (qtype, arm))
        for arm, counts in r["summary"]["by_arm"].items():
            self.assertEqual(sum(counts["classes"].values()), len(r["rows"]), arm)
        self.assertEqual(set(r["summary"]["by_arm"]), {"A-a", "A-b", "A-c", "B-a", "B-b", "B-c"})

    def test_row_fields(self):
        r = self.run_fixture()
        row = [x for x in r["rows"] if x["question_id"] == "q1"][0]
        self.assertEqual(row["evidence_turns"], {"user": 1, "assistant": 0})
        arm = row["arms"]["A-a"]
        p = arm["proposals"][0]
        for key in ("decision", "reason", "kind", "is_new", "seed_id", "role", "conditions"):
            self.assertIn(key, p)
        self.assertEqual((p["decision"], p["kind"], p["is_new"], p["role"]), ("圓成實", "決定", True, "user"))
        self.assertEqual(arm["seed_ids"], [p["seed_id"]])
        self.assertTrue(arm["germinated"])
        self.assertEqual(arm["rendered_first_circle"], arm["seed_ids"])
        self.assertEqual(arm["rendered_index"], [])        # ① に出たので ③ には重ならない
        self.assertIn("cut", arm["circles"])
        self.assertGreater(arm["circles"]["chars"], 0)
        self.assertTrue(arm["narrative_has_evidence_text"])

    def test_arm_b_does_not_see_the_question(self):
        r = self.run_fixture()
        row = [x for x in r["rows"] if x["question_id"] == "q1"][0]
        self.assertTrue(row["arms"]["A-a"]["germinated"])
        self.assertFalse(row["arms"]["B-a"]["germinated"])      # 縁 yoga は質問文に無い
        self.assertEqual(row["arms"]["B-a"]["proposals"][0]["conditions"], ["yoga"])
        # 発芽しなくても ③ に載る。順位は 0 始まり
        self.assertEqual(row["arms"]["B-a"]["rendered_index"], [{"seed_id": row["arms"]["B-a"]["seed_ids"][0], "rank": 0}])
        self.assertEqual(row["arms"]["B-a"]["class"], "injected")

    def test_dispositions_arm_differs_only_by_env(self):
        r = self.run_fixture()
        row = [x for x in r["rows"] if x["question_id"] == "q1"][0]
        self.assertEqual(row["arms"]["A-b"]["seed_ids"], row["arms"]["A-c"]["seed_ids"])
        self.assertFalse(row["arms"]["A-b"]["germinated"])
        self.assertTrue(row["arms"]["A-c"]["germinated"])
        self.assertTrue(row["arms"]["A-b"]["germination_reasons"])   # 落ちた理由を持つ

    def test_marker_counts_and_data_shape(self):
        r = self.run_fixture()
        shape = r["summary"]["data_shape"]
        self.assertEqual(shape["questions"], 5)
        self.assertEqual(shape["questions_without_marker"], 1)
        self.assertEqual(shape["questions_assistant_only_marker"], 1)
        self.assertEqual(shape["abs"], 2)
        self.assertEqual(shape["first_assistant_sessions"], 1)
        self.assertEqual(shape["non_alternating_sessions"], 1)
        self.assertEqual(shape["empty_turns"], 1)
        self.assertEqual(shape["marker_turns"], {"user": 3, "assistant": 1})

    def test_assistant_only_gets_no_user_span_and_a_weak_seed(self):
        r = self.run_fixture()
        row = [x for x in r["rows"] if x["question_id"] == "q4"][0]
        p = row["arms"]["A-a"]["proposals"][0]
        self.assertEqual((p["role"], p["decision"]), ("assistant", "依他起"))
        self.assertFalse(row["arms"]["A-a"]["germinated"])    # 依他起 は強度 1 では発芽しない

    def test_line_chars_and_frame(self):
        r = self.run_fixture()
        self.assertGreater(r["meta"]["frame_chars"]["narrative_header"], 0)
        self.assertGreater(r["summary"]["by_arm"]["A-a"]["line_chars"]["max"], 0)

    def test_distractor_arm_makes_at_least_one_distractor_candidate(self):
        r = self.run_fixture(extra=["--distractor-per-type", "1"])
        row = [x for x in r["rows"] if x["question_id"] == "q1"][0]
        d = row["arms"]["A-a+distract"]
        self.assertGreaterEqual(d["distractors"]["candidates"], 1)    # garden の user turn
        self.assertIn("A-a", row["arms"])
        self.assertEqual(r["meta"]["distractor_per_type"], 1)
        self.assertEqual(r["summary"]["by_arm"]["A-a+distract"]["questions"], 4)   # 型ごとに先頭 1 問 = 4 型で 4 問

    def test_two_runs_match_except_time(self):
        a = self.run_fixture(out_name="o1")
        b = self.run_fixture(out_name="o2")
        for r in (a, b):
            r["meta"].pop("generated_at", None)
            r.pop("timing", None)
        self.assertEqual(a, b)
        self.assertIn("timing_keys", a["meta"])

    def test_meta_states_the_rules(self):
        r = self.run_fixture()
        for key in ("condition_rule", "classification_order", "arms", "limits", "content_rule", "round_trip_rule",
                    "data_sha256", "timing_keys"):
            self.assertIn(key, r["meta"])


def _merge_fixture():
    q = "What replaced the gym on Tuesdays, instead of lifting?"
    return [_q("m1", "single-session-user", q, "yoga", [
        [_t("user", "I switched to yoga classes on Tuesdays and skipped the gym too.", False), _t("assistant", "Nice.", False)],
        [_t("user", "I switched to yoga classes on Tuesdays and skipped the gym.", True), _t("assistant", "Great.", False)],
    ])]


class TestDistractorAccounting(_Base):
    def test_evidence_merged_into_earlier_distractor_is_not_counted_as_evidence_injected(self):
        r = self.run_fixture(data=_merge_fixture(), extra=["--distractor-per-type", "1"])
        row = r["rows"][0]
        plain = row["arms"]["A-a"]
        self.assertEqual(plain["class"], "injected")                       # 邪魔なしなら根拠の種子が出る
        arm = row["arms"]["A-a+distract"]
        p = arm["proposals"][0]
        self.assertFalse(p["is_new"])                                       # 先に植わった邪魔の種子へ併合された
        self.assertEqual(p["seed_origin"], "distractor")
        self.assertEqual(arm["seed_ids"], [])                               # 根拠が自分で植えた種子は無い
        self.assertEqual(arm["merged_into_distractor_ids"], [p["seed_id"]])
        self.assertEqual(arm["class"], "merged_into_distractor")
        self.assertEqual(r["summary"]["by_arm"]["A-a+distract"]["classes"]["merged_into_distractor"], 1)
        self.assertEqual(r["summary"]["by_arm"]["A-a+distract"]["evidence_merged_into_distractor_questions"], 1)
        self.assertEqual(r["summary"]["by_arm"]["A-a"]["evidence_merged_into_distractor_questions"], 0)

    def test_merged_with_evidence_text_is_counted(self):
        r = self.run_fixture(data=_merge_fixture(), extra=["--distractor-per-type", "1"])
        by = r["summary"]["by_arm"]
        self.assertEqual(by["A-a+distract"]["merged_with_evidence_text_questions"], 1)
        self.assertEqual(by["A-a"]["merged_with_evidence_text_questions"], 0)

    def test_distractor_conditions_use_the_same_rule_as_evidence(self):
        q = "What replaced the gym on Tuesdays, instead of lifting?"
        d1 = "I love gardening on Tuesdays with fresh tomatoes."          # 質問文と共有する語 Tuesdays
        d2 = "Tell me about pottery wheels and kilns."                      # 共有する語なし
        d3 = "The gym was crowded on Tuesdays, so I lifted at home."       # gym, Tuesdays, lifted(質問は lifting)
        data = [_q("d1", "single-session-user", q, "yoga", [
            [_t("user", d1, False), _t("assistant", "ok", False)],
            [_t("user", d2, False), _t("assistant", "ok", False)],
            [_t("user", d3, False), _t("assistant", "ok", False)],
            [_t("user", "I did yoga on Tuesdays.", True), _t("assistant", "ok", False)],
        ])]
        r = self.run_fixture(data=data, extra=["--distractor-per-type", "1"])
        for edge in ("A", "B"):
            got = [x["conditions"] for x in r["rows"][0]["arms"]["{}-a+distract".format(edge)]["distractor_proposals"]]
            want = [lm.choose_conditions(edge, s, q, "yoga") for s in (d1, d2, d3)]
            self.assertEqual(got, want)
        self.assertEqual(want[1], [])
        self.assertTrue(r["rows"][0]["arms"]["A-a+distract"]["distractor_proposals"][0]["conditions"])


class TestContentRule(_Base):
    def test_head120_takes_first_sentence_up_to_120_chars(self):
        self.assertEqual(lm.make_content("I like art. I also paint.", "head120"), "I like art.")
        self.assertEqual(lm.make_content("x" * 300, "head120"), "x" * 120)
        self.assertEqual(lm.make_content("a  b\nc", "span"), "a  b\nc")

    def test_unknown_rule_raises(self):
        with self.assertRaises(ValueError):
            lm.make_content("a", "zzz")

    def test_cli_head120_is_recorded_in_meta(self):
        r = self.run_fixture(extra=["--content-rule", "head120"])
        self.assertIn("head120", r["meta"]["content_rule"])
        self.assertTrue(self.run_fixture(out_name="out2")["meta"]["content_rule"].startswith("span:"))


class TestContentRuleReachesSeeds(_Base):
    def test_cli_rule_reaches_build_state(self):
        seen = []
        real = lm._build_state

        def spy(*a, **k):
            seen.append(a[5] if len(a) > 5 else k.get("content_rule"))
            return real(*a, **k)

        with mock.patch.object(lm, "_build_state", spy):
            self.run_fixture(extra=["--content-rule", "head120"])
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"head120"})

    def _contents(self, rule):
        q = copy.deepcopy(_fixture()[0])
        q["haystack_sessions"][0][0]["content"] = "I switched to yoga classes on Tuesdays. I skipped the gym too."
        rts, _ = lm.build_round_trips(q["haystack_session_ids"], q["haystack_sessions"])
        plan = lm._plan(rts, False)
        engine, tmp, _, _ = lm._build_state(q, rts, plan, "A", "決定", rule)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return [x["content"] for x in engine.inspect_seeds()]

    def test_content_rule_decides_the_planted_content(self):
        with lm._env(RAIYA_CONFIG=""):
            full = self._contents("span")
            head = self._contents("head120")
        self.assertEqual(full, ["I switched to yoga classes on Tuesdays. I skipped the gym too."])
        self.assertEqual(head, ["I switched to yoga classes on Tuesdays."])


class TestOutRequired(_Base):
    def test_out_is_required_and_nothing_is_written(self):
        data_path = _write(self.tmp, _fixture())
        before = sorted(os.listdir(self.tmp))
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                lm.main(["--data", data_path, "--name", "zz_missing_out_probe.json"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)
