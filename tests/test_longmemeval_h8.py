"""scripts/longmemeval_h8.py (H8: 却下の印を疑の文の外に限る測定) を手書きの小さな英語の例で回す。LLM は使わない。

データ本文は使わない (LongMemEval 形の欄だけを真似た手書きの例)。出力は自作の tmp だけ。消すのも自作の tmp だけ。
設計書 2026-11-27-reject-marker-outside-doubt-design §8 T3。
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

import longmemeval_h8 as h8  # noqa: E402


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


FARE = ("My daily train fare is $6. Can you help me figure out how much I would save if I cycled to the "
        "station instead of taking the train?")


def _fare_data():
    return [_q("h1", "How much is my daily train fare?", "$6", [[_t("user", FARE, True), _t("assistant", "ok", False)]])]


class TestRejectScope(unittest.TestCase):
    def test_reject_scope_counts(self):
        corpora = {"user": ["Can I use a key instead of a password?", "Use TOML instead of YAML.", "No marker here."]}
        self.assertEqual(h8.reject_scope_counts(corpora), {"user": {"texts": 3, "whole": 2, "outside_doubt": 1}})


class TestProposalKinds(unittest.TestCase):
    def test_proposal_kinds_shape(self):
        rows = h8.proposal_kinds(_fare_data())
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), {"question_id", "index", "declared", "sansho", "kind"})
            self.assertEqual((row["question_id"], row["index"]), ("h1", 0))
        by_declared = {row["declared"]: row for row in rows}
        self.assertEqual(set(by_declared), {"決定", "性向"})
        self.assertEqual(by_declared["決定"]["kind"], "決定")
        # 申告 性向: 却下の印は問いの文の中だけ (T1 の後は 決定 に上がらない)
        self.assertEqual(by_declared["性向"]["kind"], "性向")

    def test_kind_is_the_resolved_kind_not_the_declared_one(self):
        # 問いでない文に却下の印がある申告 性向 は KP (a) で 決定 に上がる (kind が申告と違う)
        data = [_q("h2", "Which cache do I use, Redis?", "Redis", [[
            _t("user", "Use Redis instead of Memcached.", True), _t("assistant", "ok", False)]])]
        by_declared = {row["declared"]: row for row in h8.proposal_kinds(data)}
        self.assertEqual((by_declared["性向"]["sansho"], by_declared["性向"]["kind"]), ("圓成實", "決定"))
        self.assertEqual(by_declared["決定"]["kind"], "決定")


class TestCompare(unittest.TestCase):
    def test_compare_reports_changed_kind(self):
        def row(kind):
            return {"question_id": "q", "index": 0, "declared": "性向", "sansho": "圓成實", "kind": kind}
        old = {"proposals": [row("決定")], "injected": {"A-b": {"injected": 1}}}
        new = {"proposals": [row("性向")], "injected": {"A-b": {"planted_not_injected": 1}}}
        diff = h8.compare(old, new)
        self.assertEqual(len(diff["kind_changed"]), 1)
        self.assertEqual(diff["kind_changed"][0]["old_kind"], "決定")
        self.assertEqual(diff["kind_changed"][0]["new_kind"], "性向")
        self.assertEqual(tuple(diff["kind_totals"]["性向/決定"]), (1, 0))
        self.assertEqual(tuple(diff["kind_totals"]["性向/性向"]), (0, 1))
        self.assertEqual(tuple(diff["injected"]["A-b"]["injected"]), (1, 0))
        self.assertEqual(tuple(diff["injected"]["A-b"]["planted_not_injected"]), (0, 1))


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_lmh8_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_path = os.path.join(self.tmp, "data.json")
        with open(self.data_path, "w", encoding="utf-8") as f:
            json.dump(_fare_data(), f)

    def _run(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return h8.main(argv)

    def test_main_writes_only_out(self):
        out = os.path.join(self.tmp, "out")
        self.assertEqual(self._run(["--data", self.data_path, "--out", out, "--quiet"]), 0)
        self.assertEqual(os.listdir(out), ["h8.json"])
        with open(os.path.join(out, "h8.json"), encoding="utf-8") as f:
            result = json.load(f)
        self.assertEqual(result["schema"], "longmemeval-h8/1")
        for key in ("meta", "reject_scope", "proposals", "injected"):
            self.assertIn(key, result)
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["data.json", "out"])

    def test_compare_mode_writes_only_out(self):
        a, b = os.path.join(self.tmp, "a.json"), os.path.join(self.tmp, "b.json")
        for path, kind in ((a, "決定"), (b, "性向")):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"proposals": [{"question_id": "q", "index": 0, "declared": "性向", "sansho": "圓成實",
                                          "kind": kind}], "injected": {}}, f)
        out = os.path.join(self.tmp, "cmp")
        self.assertEqual(self._run(["--compare", a, b, "--out", out]), 0)
        self.assertEqual(len(os.listdir(out)), 1)
        with open(os.path.join(out, os.listdir(out)[0]), encoding="utf-8") as f:
            diff = json.load(f)
        self.assertEqual(diff["kind_totals"]["性向/決定"], [1, 0])

    def test_does_not_import_backend(self):
        # 別プロセスで main を走らせ、raiya.backend が読まれていないことを見る (同じプロセスでは他のテストが読んでいることがある)
        code = (
            "import sys; sys.path[:0] = [{root!r}, {scripts!r}]\n"
            "import longmemeval_h8 as m\n"
            "rc = m.main(['--data', {data!r}, '--out', {out!r}, '--quiet'])\n"
            "assert rc == 0, rc\n"
            "assert 'raiya.backend' not in sys.modules\n"
        ).format(root=_ROOT, scripts=os.path.join(_ROOT, "scripts"), data=self.data_path,
                 out=os.path.join(self.tmp, "o"))
        env = dict(os.environ, RAIYA_CONFIG="", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
