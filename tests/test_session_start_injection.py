"""#135 S2/S3 (spec 2026-11-04-session-start-injection-design §5.2/§7): 再開点 の行に時の札 `(往復 N)`、
`resume_point` は同 project を全域より先に選ぶ。描画と読み取りだけ (保存は変えない)。LLM は使わない。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.alaya import AlayaStore  # noqa: E402
from raiya.manas import NARRATIVE_HEADER, ManasStore  # noqa: E402


def _ev(source, lid, life=1):
    e = {"source": source, "span": "s", "life": life}
    if lid is not None:
        e["ledger_id"] = lid
    return e


def _resume(sid, evidences, content="次 = 実装", project=None):
    s = {"id": sid, "kind": "再開点", "content": content, "strength": 1, "sansho": "圓成實",
         "conditions": ["a"], "evidences": evidences}
    if project is not None:
        s["project"] = project
    return s


def _text(resume, max_chars=None):
    return ManasStore().compose_injection([], resume=resume, max_chars=max_chars)["text"]


class TestResumeTag(unittest.TestCase):
    def test_last_evidence_used_not_user_preferred(self):
        # #138 T2: 旧規則 (user の span を優先 = 往復 3) から、最後の evidence (往復 9) へ
        r = _resume(1, [_ev("user", 3), _ev("assistant", 9)])
        self.assertEqual(_text(r), NARRATIVE_HEADER + "\n\n- 再開点: 次 = 実装 (往復 9)")

    def test_last_evidence_when_no_user(self):
        r = _resume(1, [_ev("assistant", 2), _ev("assistant", 6)])
        self.assertTrue(_text(r).endswith("- 再開点: 次 = 実装 (往復 6)"))

    def test_no_tag_without_int_ledger_id(self):
        for evs in ([_ev("user", None)], [_ev("user", True)], [], [_ev("user", "7")]):
            r = _resume(1, evs)
            self.assertTrue(_text(r).endswith("\n\n- 再開点: 次 = 実装"), evs)

    def test_tag_kept_when_content_cut(self):
        r = _resume(1, [_ev("user", 12)], content="あ" * 400)
        limit = len(NARRATIVE_HEADER) + 2 + 80
        out = _text(r, limit)
        self.assertLessEqual(len(out), limit)
        line = out.split("\n\n", 1)[1]
        self.assertRegex(line, r"^- 再開点: あ*…\(他 \d+ 字\) \(往復 12\)$")
        self.assertIn("…(他", line)

    def test_line_dropped_when_tag_does_not_fit(self):
        r = _resume(1, [_ev("user", 12)], content="あ" * 400)
        # 枠 + 最小形 (札なし) なら入るが、札を含めると入らない上限
        bare = _resume(1, [_ev("user", None)], content="あ" * 400)
        limit = len(NARRATIVE_HEADER) + 2 + len("- 再開点: …(他 400 字)")
        self.assertIn("再開点", _text(bare, limit))
        out = _text(r, limit)
        self.assertNotIn("再開点: ", out)

    def test_other_lines_unchanged(self):
        from raiya.manas import _render_global_line, _render_sibling_line
        self.assertNotIn("往復", _render_global_line([{"kind": "決定", "groups": 1, "seeds": 2}]))
        self.assertNotIn("往復", _render_sibling_line({"label": "g", "size": 3, "listed": [8], "more": 0}))

    def test_resume_tag_skips_non_dict_evidences(self):
        r = _resume(1, ["x", 3, None, _ev("assistant", 4)])
        self.assertTrue(_text(r).endswith("- 再開点: 次 = 実装 (往復 4)"))
        r = _resume(1, ["x", 3, None])
        self.assertTrue(_text(r).endswith("\n\n- 再開点: 次 = 実装"))

    def test_non_list_evidences_render_without_tag(self):
        for evs in (5, "abc", {"source": "user", "ledger_id": 3}):
            r = _resume(1, evs)
            self.assertTrue(_text(r).endswith("\n\n- 再開点: 次 = 実装"), evs)


class TestResumeRecencyLastEvidence(unittest.TestCase):
    """#138 T2 (spec 2026-11-04-resume-recency-tag-design §4.2/§5): 再開点 の N = 最後の dict の evidence の `ledger_id`。"""

    def test_last_assistant_after_user_wins(self):
        r = _resume(1, [_ev("user", 5), _ev("assistant", 9)])
        self.assertTrue(_text(r).endswith("- 再開点: 次 = 実装 (往復 9)"))

    def test_last_user(self):
        r = _resume(1, [_ev("assistant", 9), _ev("user", 5)])
        self.assertTrue(_text(r).endswith("- 再開点: 次 = 実装 (往復 5)"))

    def test_trailing_non_dict_evidence_ignored(self):
        # 「最後の dict の evidence」: 末尾の dict でない要素は飛ばす (dict の最後 = 往復 4)
        r = _resume(1, [_ev("user", 2), _ev("assistant", 4), "x", None])
        self.assertTrue(_text(r).endswith("- 再開点: 次 = 実装 (往復 4)"))

    def test_no_tag_when_last_is_not_int_even_if_earlier_is(self):
        for last in (_ev("assistant", None), _ev("assistant", True), _ev("assistant", "7")):
            r = _resume(1, [_ev("user", 5), last])
            self.assertTrue(_text(r).endswith("\n\n- 再開点: 次 = 実装"), last)

    def test_overwrite_via_imprint_points_at_new_generation(self):
        import shutil
        import tempfile
        from raiya.engine import MemoryEngine
        from tests.test_project_memory import RAIYA, _fake_home, _proposal, _turn
        _fake_home(self)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        eng = MemoryEngine(state_dir=tmp)
        c1, c2 = "次 = §11 の裁定待ち", "次 = E7 の receipt"
        eng.imprint([_turn(c1 + " 裁定", "", RAIYA, ledger_id=5)], [_proposal(c1, ["裁定"], c1, kind="再開点")])
        out = eng.recall("続き", project=RAIYA, session_start=True)
        self.assertTrue(out["narrative"].endswith("(往復 5)"), out["narrative"])
        res = eng.imprint([_turn("質問 receipt", c2, RAIYA, ledger_id=9)],
                          [_proposal(c2, ["receipt"], c2, source="assistant", kind="再開点")])[0]
        self.assertTrue(res.get("superseded"), res)
        out = eng.recall("続き", project=RAIYA, session_start=True)
        self.assertTrue(out["narrative"].endswith("- 再開点(推定): " + c2 + " (往復 9)"), out["narrative"])
        ex = eng.explain_recall("続き", project=RAIYA, session_start=True)
        self.assertEqual(ex["narrative"], out["narrative"])

    def test_new_n_kept_when_shrunk(self):
        r = _resume(1, [_ev("user", 5), _ev("assistant", 9)], content="あ" * 400)
        limit = len(NARRATIVE_HEADER) + 2 + 80
        out = _text(r, limit)
        self.assertLessEqual(len(out), limit)
        self.assertRegex(out.split("\n\n", 1)[1], r"^- 再開点: あ*…\(他 \d+ 字\) \(往復 9\)$")


class TestResumePointPriority(unittest.TestCase):
    def _store(self, seeds):
        st = AlayaStore.__new__(AlayaStore)
        st.state = {"seeds": seeds}
        return st

    def test_same_project_beats_global_with_larger_id(self):
        st = self._store([_resume(5, [], project=None), _resume(2, [], project="/p")])
        self.assertEqual(st.resume_point("/p")["id"], 2)

    def test_only_global_or_only_project(self):
        self.assertEqual(self._store([_resume(5, [])]).resume_point("/p")["id"], 5)
        self.assertEqual(self._store([_resume(2, [], project="/p")]).resume_point("/p")["id"], 2)

    def test_other_project_never_shown(self):
        self.assertIsNone(self._store([_resume(2, [], project="/q")]).resume_point("/p"))

    def test_largest_id_within_side(self):
        st = self._store([_resume(2, [], project="/p"), _resume(4, [], project="/p"), _resume(9, [])])
        self.assertEqual(st.resume_point("/p")["id"], 4)


from tests.test_engine_compact import WINDOW, TEXT, P, _Case, _seed, _row  # noqa: E402


class TestEnginePaths(_Case):
    def _seeds(self):
        g = _seed(5, [], kind="再開点", project=None, evidences=[8], content="c5")
        l = _seed(2, [], kind="再開点", project=P, evidences=[4], content="c2")
        g["evidences"][0]["ledger_id"] = 50
        l["evidences"][0]["ledger_id"] = 20
        return [g, l]

    def test_recall_session_start_prefers_project_resume(self):
        eng = self.write(self._seeds())
        ex = eng.explain_recall("続き", project=P, session_start=True)
        out = eng.recall("続き", project=P, session_start=True)
        self.assertEqual(out["resume"]["id"], 2)
        self.assertTrue(out["narrative"].endswith("- 再開点: c2 (往復 20)"), out["narrative"])
        self.assertEqual(ex["narrative"], out["narrative"])
        self.assertEqual(ex["resume"]["id"], 2)

    def test_compact_prefers_project_resume(self):
        g = _seed(5, [], kind="再開点", project=None, evidences=[8], content="c5")
        l = _seed(2, [], kind="再開点", project=P, evidences=[4], content="c2")
        result = self.write([g, l]).compact(WINDOW, TEXT, project=P)
        self.assertEqual([(s["id"], s["circle"]) for s in result["seeds"]], [(2, "resume")])
        self.assertEqual(next(t for t in result["turns"] if t["id"] == 4)["circle"], "resume")
        self.assertNotEqual(next(t for t in result["turns"] if t["id"] == 8)["circle"], "resume")


class TestResumeCertaintyTag(unittest.TestCase):
    """#137 C1 (spec 2026-11-04-resume-certainty-tag-design §5.2/§7): sansho == 依他起 の 再開点 だけ `(推定)`。描画のみ。"""

    def _r(self, sansho, ev=None, content="次 = 実装"):
        r = _resume(1, ev if ev is not None else [_ev("user", 3)], content=content)
        if sansho is None:
            del r["sansho"]
        else:
            r["sansho"] = sansho
        return r

    def test_inferred_has_tag(self):
        self.assertTrue(_text(self._r("依他起")).endswith("\n\n- 再開点(推定): 次 = 実装 (往復 3)"))

    def test_grounded_has_no_tag(self):
        self.assertTrue(_text(self._r("圓成實")).endswith("\n\n- 再開点: 次 = 実装 (往復 3)"))

    def test_missing_sansho_has_no_tag(self):
        self.assertTrue(_text(self._r(None)).endswith("\n\n- 再開点: 次 = 実装 (往復 3)"))

    def test_evidences_are_not_read(self):
        # 依他起 + user の span あり (世代混在) でも札は付く
        out = _text(self._r("依他起", [_ev("user", 1), _ev("assistant", 2)]))
        self.assertIn("- 再開点(推定): ", out)

    def test_tag_kept_when_shrunk_and_fits(self):
        r = self._r("依他起", [_ev("user", 12)], content="あ" * 400)
        limit = len(NARRATIVE_HEADER) + 2 + 90
        out = _text(r, limit)
        self.assertLessEqual(len(out), limit)
        self.assertIn("- 再開点(推定): ", out)
        self.assertIn("…(他 ", out)
        self.assertTrue(out.endswith(" (往復 12)"))

    def test_inferred_line_dropped_when_tag_prefix_does_not_fit(self):
        # 札なしの最小形なら収まり、(推定) の 4 字ぶん溢れる上限: 依他起 は行を落とす (札なしへ戻さない)、圓成實 は残る
        limit = len(NARRATIVE_HEADER) + 2 + len("- 再開点: …(他 400 字)")
        out = _text(self._r("依他起", [_ev("user", None)], content="あ" * 400), limit)
        self.assertLessEqual(len(out), limit)
        self.assertNotIn("再開点:", out.replace(NARRATIVE_HEADER, ""))
        self.assertNotIn("再開点(推定)", out)
        kept = _text(self._r("圓成實", [_ev("user", None)], content="あ" * 400), limit)
        self.assertLessEqual(len(kept), limit)
        self.assertIn("- 再開点: …(他 400 字)", kept)

    def test_after_inferred_overwrites_grounded_via_imprint(self):
        import shutil
        import tempfile
        from raiya.engine import MemoryEngine
        from tests.test_project_memory import RAIYA, _fake_home, _proposal, _turn
        _fake_home(self)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        eng = MemoryEngine(state_dir=tmp)
        c1, c2 = "次 = §11 の裁定待ち", "次 = E7 の receipt"
        eng.imprint([_turn(c1 + " 裁定", "", RAIYA)], [_proposal(c1, ["裁定"], c1, kind="再開点")])
        self.assertTrue(eng.recall("続き", project=RAIYA, session_start=True)["narrative"].count("(推定)") == 0)
        turn = _turn("質問 receipt", c2, RAIYA)
        res = eng.imprint([turn], [_proposal(c2, ["receipt"], c2, source="assistant", kind="再開点")])[0]
        self.assertTrue(res.get("superseded"), res)
        out = eng.recall("続き", project=RAIYA, session_start=True)
        self.assertEqual(out["resume"]["sansho"], "依他起")
        self.assertIn("- 再開点(推定): " + c2, out["narrative"])
        ex = eng.explain_recall("続き", project=RAIYA, session_start=True)
        self.assertEqual(ex["narrative"], out["narrative"])


if __name__ == "__main__":
    unittest.main()
