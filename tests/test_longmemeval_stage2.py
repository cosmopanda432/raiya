"""scripts/longmemeval_stage2.py (LongMemEval 段 2・en の印の表の測定) を手書きの小さな英語の例で回す。LLM は使わない。

データ本文は使わない (LongMemEval 形の欄だけを真似た手書きの例)。出力は自作の tmp だけ。消すのも自作の tmp だけ。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import longmemeval_stage2 as lm2  # noqa: E402
from raiya import alaya, markers  # noqa: E402


def _t(role, content, ha=None):
    d = {"role": role, "content": content}
    if ha is not None:
        d["has_answer"] = ha
    return d


def _q(qid, question, answer, sessions, qtype="single-session-user"):
    return {
        "question_id": qid, "question_type": qtype, "question": question, "answer": answer,
        "question_date": "2023/05/30 (Tue) 10:00", "answer_session_ids": ["s1"],
        "haystack_dates": ["2023/05/20 (Sat) 10:00"] * len(sessions),
        "haystack_session_ids": ["s{}".format(i + 1) for i in range(len(sessions))],
        "haystack_sessions": sessions,
    }


CIVIC_Q = "What car do I drive, the Civic?"


def _kind_data():
    return [_q("k1", CIVIC_Q, "Civic", [[
        _t("user", "I want to make sure the filter fits my Civic.", True), _t("assistant", "Sure.", False),
        _t("user", "My Civic gets 28 mpg in the city.", True), _t("assistant", "Nice.", False),
    ]])]


class TestCounting(unittest.TestCase):
    def test_count_candidate_columns(self):
        texts = ["We'll go with Postgres.", "I went with friends.", "Should we go with Redis?"]
        self.assertEqual(
            lm2.count_candidate(texts, "we'll go with"), {"substring": 1, "word": 1, "final": 0, "in_sentence": 1})

    def test_final_counts_only_sentence_end(self):
        self.assertEqual(
            lm2.count_candidate(["Setting TZ=UTC works around it.", "It works around it sometimes."], "works around it"),
            {"substring": 2, "word": 2, "final": 1, "in_sentence": 2})

    def test_final_column_matches_spec_definition(self):
        # 設計書 §1.3: 末尾の 。.!！」 空白 " ' ) を外し、境の無い endswith で数える (本番の _ends_with_marker とは別)。
        self.assertEqual(
            lm2.count_candidate(['He said "it works."'], "it works"),
            {"substring": 1, "word": 1, "final": 1, "in_sentence": 1})
        # 印の左が語の途中: word / in_sentence は 0 だが final は境なしで 1
        self.assertEqual(
            lm2.count_candidate(["Whoas you mentioned"], "as you mentioned"),
            {"substring": 1, "word": 0, "final": 1, "in_sentence": 0})

    def test_build_corpora(self):
        data = [_q("c1", "q", "a", [[
            _t("user", "Hello there", True), _t("assistant", "Hi", False),
            _t("user", "Hello there", False), _t("assistant", "   ", False), _t("user", "Other", False),
        ]])]
        corpora = lm2.build_corpora(data)
        self.assertEqual(corpora["evidence_user"], ["Hello there"])
        self.assertEqual(corpora["user"], ["Hello there", "Other"])
        self.assertEqual(corpora["assistant"], ["Hi"])


class TestTables(unittest.TestCase):
    def test_en_tables_restores(self):
        before_const = alaya.PREFERENCE_SUFFIXES
        before_table = dict(markers.MARKERS["en"])
        with lm2.en_tables("P1"):
            self.assertIn("i prefer", alaya.PREFERENCE_SUFFIXES)
        self.assertEqual(alaya.PREFERENCE_SUFFIXES, before_const)
        self.assertEqual(markers.MARKERS["en"], before_table)

    def test_en_tables_restores_on_exception(self):
        before_const = alaya.PREFERENCE_SUFFIXES
        with self.assertRaises(RuntimeError):
            with lm2.en_tables("P2"):
                raise RuntimeError("x")
        self.assertEqual(alaya.PREFERENCE_SUFFIXES, before_const)

    def test_variants_have_every_role(self):
        for v, table in lm2.VARIANTS.items():
            self.assertEqual(set(table), set(markers.ROLES), v)
        self.assertEqual(
            lm2.VARIANTS["P0"]["decision_reject"], ("instead of", "rather than", "rejected", "decided against", "ruled out"))
        self.assertEqual(lm2.VARIANTS["P0"]["preference_suffix"], ())
        self.assertNotIn("rejected", lm2.VARIANTS["REC"]["decision_reject"])

    def test_p0_matches_head_en_table(self):
        # 30e1f2c の en の表の字面。新しい 2 役 (文中一致) は 30e1f2c に無いので ()
        expected = {role: () for role in markers.ROLES}
        expected.update(
            decision_reject=("instead of", "rather than", "rejected", "decided against", "ruled out"),
            workaround_cause=("because", "caused by", "root cause", "due to", "→"),
            user_correction=("that's wrong", "not what i asked", "revert that", "undo that"),
        )
        self.assertEqual(dict(lm2.VARIANTS["P0"]), expected)

    def test_variants_swap_preference_table(self):
        with lm2.en_tables("P0"):
            self.assertEqual(alaya.PREFERENCE_SUFFIXES, markers.union("preference_suffix"))
            self.assertNotIn("i want to", alaya.PREFERENCE_SUFFIXES)
        with lm2.en_tables("P2"):
            self.assertIn("i want to", alaya.PREFERENCE_SUFFIXES)


class TestKindShiftAndLabels(unittest.TestCase):
    def test_kind_shift_counts(self):
        self.assertEqual(lm2.kind_shift(_kind_data(), "P0", "決定"), {"圓成實/決定": 2})
        self.assertEqual(lm2.kind_shift(_kind_data(), "P2", "決定"), {"圓成實/決定": 1, "圓成實/性向": 1})

    def test_label_counts_doubt(self):
        data = [_q("l1", CIVIC_Q, "Civic", [[
            _t("user", "Should I keep the Civic? My Civic gets 28 mpg.", True), _t("assistant", "ok", False)]])]
        self.assertEqual(lm2.label_counts(data, "P0"), {"decision": 1, "doubt_only": 0, "doubt_keep": 0, "conjecture": 0})
        only_question = [_q("l2", CIVIC_Q, "Civic", [[
            _t("user", "Should I sell the Civic? It is old.", True), _t("assistant", "ok", False)]])]
        self.assertEqual(lm2.label_counts(only_question, "P0")["decision"], 0)

    def test_label_counts_doubt_only_and_conjecture(self):
        # 縁が問いの文にしか無い 決定 は残らないので、縁が答えの文に在り、別の縁が問いにしか無い形で doubt_only を立てる
        data = [_q("l3", "Is the Civic or the Accord better?", "Civic", [[
            _t("user", "Is the Accord fast? The Civic is mine.", True), _t("assistant", "ok", False)]])]
        counts = lm2.label_counts(data, "P0")
        self.assertEqual(counts["decision"], 1)
        self.assertEqual(counts["doubt_only"], 1)
        maybe = [_q("l4", CIVIC_Q, "Civic", [[_t("user", "Maybe my Civic is fine.", True), _t("assistant", "ok", False)]])]
        self.assertEqual(lm2.label_counts(maybe, "P0")["conjecture"], 0)
        self.assertEqual(lm2.label_counts(maybe, "REC")["conjecture"], 1)


class TestInjected(unittest.TestCase):
    def test_injected_counts_classes_per_arm(self):
        data = [_q("i1", CIVIC_Q, "Civic", [[
            _t("user", "My Civic gets 28 mpg in the city.", True), _t("assistant", "Nice.", False)]])]
        arms = lm2.injected(data, "P0")
        self.assertEqual(set(arms), {"A-a", "A-b", "A-c"})
        self.assertEqual(arms["A-a"], {"injected": 1})

    def test_injected_follows_the_variant(self):
        # P2 では `i want to` が好みの印になり、申告 決定 が 性向 に落ちる (既定の注入に出ない)
        data = [_q("i2", CIVIC_Q, "Civic", [[
            _t("user", "I want to make sure the filter fits my Civic.", True), _t("assistant", "Sure.", False)]])]
        self.assertEqual(lm2.injected(data, "P0")["A-a"], {"injected": 1})
        self.assertEqual(lm2.injected(data, "P2")["A-a"], {"planted_not_injected": 1})
        self.assertEqual(lm2.injected(data, "P2")["A-c"], {"injected": 1})


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_lme2_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return lm2.main(argv)

    def test_main_writes_only_out(self):
        data_path = os.path.join(self.tmp, "data.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(_kind_data(), f)
        out = os.path.join(self.tmp, "out")
        code = self._run(["--data", data_path, "--out", out, "--variants", "P0,P2", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(out), ["stage2.json"])
        with open(os.path.join(out, "stage2.json"), encoding="utf-8") as f:
            result = json.load(f)
        self.assertEqual(result["schema"], "longmemeval-stage2/1")
        for key in ("meta", "corpora", "candidates", "kind_shift", "labels", "injected"):
            self.assertIn(key, result)
        self.assertEqual(result["kind_shift"]["P2"]["決定"], {"圓成實/決定": 1, "圓成實/性向": 1})
        self.assertEqual(sorted(os.listdir(self.tmp)), ["data.json", "out"])

    def test_bad_variant_is_refused(self):
        data_path = os.path.join(self.tmp, "data.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(_kind_data(), f)
        self.assertEqual(self._run(["--data", data_path, "--out", os.path.join(self.tmp, "o"), "--variants", "ZZ"]), 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "o")))

    def test_does_not_import_backend(self):
        # 別プロセスで main を走らせ、raiya.backend が読まれていないことを見る (同じプロセスでは他のテストが読んでいることがある)
        data_path = os.path.join(self.tmp, "data.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(_kind_data(), f)
        code = (
            "import sys; sys.path[:0] = [{root!r}, {scripts!r}]\n"
            "import longmemeval_stage2 as m\n"
            "rc = m.main(['--data', {data!r}, '--out', {out!r}, '--variants', 'P0', '--quiet'])\n"
            "assert rc == 0, rc\n"
            "assert 'raiya.backend' not in sys.modules\n"
        ).format(root=_ROOT, scripts=os.path.join(_ROOT, "scripts"), data=data_path, out=os.path.join(self.tmp, "o"))
        env = dict(os.environ, RAIYA_CONFIG="", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
