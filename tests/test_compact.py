"""`raiya.compact` (純関数) と `ledger.read_tool_refs` の pin。spec: docs/superpowers/specs/2026-10-27-compact-design.md §10 テスト 1-10・23(AST)。

engine は作らない。台帳の行は dict をそのまま渡す。LLM は使わない。
"""
import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import compact  # noqa: E402
from raiya.compact import COMPACT_HEADER, elided_line, plan_compaction, render_turn  # noqa: E402
from raiya.ledger import GATE_CUT_CHAR, read_tool_refs  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EN = "–"


def _row(i, user="u{}", assistant="a{}", grounding=None, tool_refs=None, session="s1"):
    row = {
        "id": i, "life": 1, "session_id": session, "ts": 1.0,
        "user_text": user.format(i), "assistant_text": assistant.format(i),
        "grounding_text": grounding,
    }
    if tool_refs is not None:
        row["tool_refs"] = tool_refs
    return row


def _rows(*ids, **kw):
    return {i: _row(i, **kw) for i in ids}


class TestElidedLine(unittest.TestCase):
    def test_single(self):
        self.assertEqual(elided_line(7, 7, 1), "(他に 1 往復・台帳 id 7)")

    def test_range_uses_en_dash_and_n_is_count(self):
        self.assertEqual(elided_line(10, 15, 3), "(他に 3 往復・台帳 id 10{}15)".format(EN))


class TestRunsAndOrder(unittest.TestCase):
    def test_runs_split_by_keep_and_gaps_do_not_split(self):
        rows = _rows(10, 12, 15, 20, 21)
        plan = plan_compaction(rows, [10, 12, 15, 20, 21], [("recent", [15])], None)
        self.assertEqual(plan["keep"], [15])
        self.assertEqual([(e["from"], e["to"], e["n"]) for e in plan["elided"]], [(10, 12, 2), (20, 21, 2)])
        self.assertEqual(plan["elided"][0]["line"], "(他に 2 往復・台帳 id 10{}12)".format(EN))

    def test_all_elided_is_one_run_with_gaps(self):
        plan = plan_compaction(_rows(10, 12, 15), [10, 12, 15], [], None)
        self.assertEqual(plan["keep"], [])
        self.assertEqual([e["line"] for e in plan["elided"]], ["(他に 3 往復・台帳 id 10{}15)".format(EN)])

    def test_unknown_does_not_split_run_nor_enter_line(self):
        rows = _rows(10, 12)
        plan = plan_compaction(rows, [10, 11, 12], [], None)
        self.assertEqual([(e["from"], e["to"], e["n"]) for e in plan["elided"]], [(10, 12, 2)])
        decisions = {d["id"]: d for d in plan["turn_decisions"]}
        self.assertEqual(decisions[11]["decision"], "unknown")
        self.assertEqual(decisions[11]["reason"], "unknown")
        self.assertEqual(decisions[11]["chars"], 0)
        self.assertNotIn("#11", plan["narrative"])
        self.assertNotIn("id 11", plan["narrative"])

    def test_render_order_is_ascending_and_packing_order_is_separate(self):
        rows = _rows(1, 2, 3)
        plan = plan_compaction(rows, [1, 2, 3], [("recent", [3, 1])], None)
        self.assertEqual(plan["keep"], [1, 3])
        n = plan["narrative"]
        self.assertLess(n.index("[台帳 #1 "), n.index("(他に 1 往復・台帳 id 2)"))
        self.assertLess(n.index("(他に 1 往復・台帳 id 2)"), n.index("[台帳 #3 "))

    def test_narrative_starts_with_header_and_blocks_are_blank_line_separated(self):
        plan = plan_compaction(_rows(1, 2), [1, 2], [("recent", [2])], None)
        self.assertTrue(plan["narrative"].startswith(COMPACT_HEADER + "\n\n"))
        self.assertEqual(plan["narrative"], "\n\n".join([COMPACT_HEADER, elided_line(1, 1, 1), render_turn(_row(2))]))

    def test_all_unknown_window_has_no_narrative(self):
        plan = plan_compaction({}, [3, 5], [("recent", [3])], 100)
        self.assertEqual((plan["keep"], plan["elided"], plan["narrative"]), ([], [], ""))
        self.assertEqual([(d["id"], d["decision"]) for d in plan["turn_decisions"]], [(3, "unknown"), (5, "unknown")])
        self.assertEqual(plan["cut"], {"germinated": 0, "resume": 0, "recent": 0})

    def test_empty_window(self):
        plan = plan_compaction({}, [], [], 0)
        self.assertEqual((plan["keep"], plan["elided"], plan["narrative"], plan["turn_decisions"]), ([], [], "", []))
        self.assertEqual(plan["cut"], {"germinated": 0, "resume": 0, "recent": 0})


class TestRenderTurn(unittest.TestCase):
    def test_grounding_str_is_rendered(self):
        text = render_turn(_row(12, user="生 <pasted_content id=\"x\">秘密</pasted_content id=\"x\">", grounding="根拠だけ"))
        self.assertIn("[台帳 #12 ユーザー] 根拠だけ", text)
        self.assertNotIn("秘密", text)

    def test_none_grounding_strips_pasted(self):
        user = 'こんにちは <pasted_content id="x">秘密の貼り付け</pasted_content id="x"> 以上'
        text = render_turn(_row(3, user=user, grounding=None))
        self.assertIn("こんにちは", text)
        self.assertNotIn("秘密の貼り付け", text)

    def test_missing_grounding_key_is_old_row(self):
        row = _row(3, user='a <pasted_content id="x">秘</pasted_content id="x"> b')
        del row["grounding_text"]
        self.assertNotIn("秘", render_turn(row))

    def test_empty_grounding_renders_marker_without_body(self):
        text = render_turn(_row(5, user="漏れてはいけない貼り付け", grounding=""))
        self.assertIn("[台帳 #5 ユーザー] (本文を根拠にしない往復: 貼り付けのみ / 降格)", text)
        self.assertNotIn("漏れてはいけない", text)

    def test_whitespace_only_grounding_renders_marker(self):
        text = render_turn(_row(5, user="漏れる", grounding=" \n\t "))
        self.assertIn("(本文を根拠にしない往復", text)
        self.assertNotIn("漏れる", text)

    def test_line_end_only_ref_renders_path_alone(self):
        text = render_turn(_row(6, tool_refs=[{"kind": "file", "path": "/a.py", "line_end": 5}]))
        self.assertTrue(text.endswith("[台帳 #6 ツール] /a.py"), text)
        self.assertNotIn("/a.py:", text)

    def test_paste_only_old_row_renders_marker(self):
        row = _row(5, user='<pasted_content id="x">全部貼り付け</pasted_content id="x">', grounding=None)
        text = render_turn(row)
        self.assertNotIn("全部貼り付け", text)
        self.assertIn("(本文を根拠にしない往復", text)

    def test_assistant_verbatim_keeps_elision_mark_no_gate_cut(self):
        text = render_turn(_row(4, assistant="head\n<elided 99 chars>\ntail"))
        self.assertIn("[台帳 #4 アシスタント] head\n<elided 99 chars>\ntail", text)
        self.assertNotIn(GATE_CUT_CHAR, text)

    def test_tool_line(self):
        refs = [
            {"kind": "file", "path": "/abs/a.py", "line_start": 10, "line_end": 20},
            {"kind": "file", "path": "/abs/b.py", "line_start": 3},
            {"kind": "file", "path": "/abs/c.py"},
            {"kind": "command", "name": "git"},
            {"kind": "file", "path": "relative.py"},  # 捨てられる
            "壊れた要素",
        ]
        text = render_turn(_row(6, tool_refs=refs))
        self.assertIn("[台帳 #6 ツール] /abs/a.py:10-20, /abs/b.py:3, /abs/c.py, git", text)
        self.assertNotIn("relative.py", text)

    def test_no_tool_line_when_absent_or_empty_or_all_broken(self):
        for refs in (None, [], ["x"]):
            self.assertNotIn("ツール]", render_turn(_row(6, tool_refs=refs)))


class TestBudget(unittest.TestCase):
    def _floor(self, rows, ids):
        return len(plan_compaction(rows, ids, [], None)["narrative"])

    def test_fits_and_cuts_at_first_miss_then_outer_circles_dropped(self):
        rows = _rows(1, 2, 3, 4, 5, 6)
        ids = [1, 2, 3, 4, 5, 6]
        circles = [("germinated", [1, 2]), ("resume", [3]), ("recent", [6, 5, 4])]
        full = plan_compaction(rows, ids, circles, None)
        self.assertEqual(full["keep"], ids)
        self.assertEqual(full["cut"], {"germinated": 0, "resume": 0, "recent": 0})
        # ① の 1 件目だけ入る予算。
        budget = len(plan_compaction(rows, ids, [("germinated", [1])], None)["narrative"])
        plan = plan_compaction(rows, ids, circles, budget)
        self.assertEqual(plan["keep"], [1])
        self.assertLessEqual(len(plan["narrative"]), budget)
        self.assertEqual(plan["cut"], {"germinated": 1, "resume": 1, "recent": 3})
        reasons = {d["id"]: d["reason"] for d in plan["turn_decisions"]}
        self.assertIsNone(reasons[1])
        self.assertEqual(reasons[2], "budget")
        self.assertEqual(reasons[3], "outer_cut")
        self.assertEqual(reasons[4], "outer_cut")

    def test_middle_circle_cut_stops_outer(self):
        rows = _rows(1, 2, 3, 4)
        circles = [("germinated", [1]), ("resume", [2]), ("recent", [4, 3])]
        budget = len(plan_compaction(rows, [1, 2, 3, 4], [("germinated", [1])], None)["narrative"])
        plan = plan_compaction(rows, [1, 2, 3, 4], circles, budget)
        self.assertEqual(plan["keep"], [1])
        self.assertEqual(plan["cut"], {"germinated": 0, "resume": 1, "recent": 2})

    def test_remeasured_after_each_add_run_lines_split(self):
        # 1 件残すと省略の行が 2 本に割れる (中央の 3 を残す)。足し算では測れない。
        rows = _rows(1, 2, 3, 4, 5)
        ids = [1, 2, 3, 4, 5]
        exact = len(plan_compaction(rows, ids, [("recent", [3])], None)["narrative"])
        ok = plan_compaction(rows, ids, [("recent", [3])], exact)
        self.assertEqual(ok["keep"], [3])
        self.assertEqual(len(ok["elided"]), 2)
        self.assertEqual(len(ok["narrative"]), exact)
        ng = plan_compaction(rows, ids, [("recent", [3])], exact - 1)
        self.assertEqual(ng["keep"], [])
        self.assertEqual(ng["cut"]["recent"], 1)

    def test_budget_below_floor_returns_floor(self):
        rows = _rows(1, 2, 3)
        ids = [1, 2, 3]
        floor = self._floor(rows, ids)
        plan = plan_compaction(rows, ids, [("germinated", [1]), ("resume", [2]), ("recent", [3])], floor - 1)
        self.assertEqual(plan["keep"], [])
        self.assertEqual(plan["cut"], {"germinated": 1, "resume": 1, "recent": 1})
        self.assertEqual(len(plan["elided"]), 1)
        self.assertIn(plan["elided"][0]["line"], plan["narrative"])
        self.assertGreater(len(plan["narrative"]), floor - 1)
        plan0 = plan_compaction(rows, ids, [("recent", [3])], 0)
        self.assertEqual(plan0["keep"], [])

    def test_none_keeps_all_candidates_and_no_circle_reason(self):
        rows = _rows(1, 2, 3)
        plan = plan_compaction(rows, [1, 2, 3], [("recent", [3])], None)
        self.assertEqual(plan["keep"], [3])
        reasons = {d["id"]: d["reason"] for d in plan["turn_decisions"]}
        self.assertEqual((reasons[1], reasons[2], reasons[3]), ("no_circle", "no_circle", None))
        self.assertEqual(plan["cut"], {"germinated": 0, "resume": 0, "recent": 0})  # no_circle は cut に数えない

    def test_innermost_circle_owns_a_row(self):
        rows = _rows(1, 2)
        plan = plan_compaction(rows, [1, 2], [("germinated", [1]), ("resume", [1, 2]), ("recent", [2, 1])], None)
        circles = {d["id"]: d["circle"] for d in plan["turn_decisions"]}
        self.assertEqual(circles, {1: "germinated", 2: "resume"})

    def test_candidates_outside_known_rows_are_ignored(self):
        plan = plan_compaction(_rows(1), [1], [("recent", [99, 1])], None)
        self.assertEqual(plan["keep"], [1])


class TestReadToolRefs(unittest.TestCase):
    def test_absent_nonlist_empty(self):
        self.assertIsNone(read_tool_refs({}))
        self.assertIsNone(read_tool_refs({"tool_refs": "x"}))
        self.assertIsNone(read_tool_refs({"tool_refs": {"kind": "file"}}))
        self.assertEqual(read_tool_refs({"tool_refs": []}), [])

    def test_elements_are_dropped_individually(self):
        refs = [
            {"kind": "file", "path": "/a", "line_start": 1, "line_end": 2, "extra": "許す"},
            {"kind": "file", "path": "rel"},
            {"kind": "file", "path": "/b", "line_start": 5, "line_end": 4},
            {"kind": "file", "path": "/c", "line_start": 0},
            {"kind": "file", "path": "/d", "line_start": True},
            {"kind": "command", "name": ""},
            {"kind": "command", "name": 3},
            {"kind": "url", "href": "http://x"},
            None,
            {"kind": "command", "name": "git"},
        ]
        self.assertEqual(read_tool_refs({"tool_refs": refs}), [
            {"kind": "file", "path": "/a", "line_start": 1, "line_end": 2},
            {"kind": "command", "name": "git"},
        ])

    def test_keeps_every_element_the_writer_accepts(self):
        # 書き手 (raiya_hook._valid_tool_ref) が受ける例: tests/test_raiya_hook_contract.py の test_tool_refs_accepted_edges と契約 fixture。
        good = [{"kind": "file", "path": "/" + "a" * 4095}, {"kind": "file", "path": "/a", "line_start": 3, "line_end": 3},
                {"kind": "file", "path": "/a", "line_start": 7}, {"kind": "file", "path": "/a", "line_end": 9},
                {"kind": "command", "name": "n" * 256}, {"kind": "file", "path": "/a"}]
        with open(os.path.join(REPO_ROOT, "tests", "fixtures", "raiya_hook_contract_v1", "record__tool_refs.stdin.json"),
                  encoding="utf-8") as f:
            good += json.load(f)["tool_refs"]
        from raiya.hooks.raiya_hook import _valid_tool_ref
        self.assertTrue(all(_valid_tool_ref(r) for r in good))
        self.assertEqual(read_tool_refs({"tool_refs": good}), good)

    def test_does_not_mutate_row(self):
        row = {"tool_refs": [{"kind": "file", "path": "/a", "extra": 1}]}
        read_tool_refs(row)
        self.assertEqual(row["tool_refs"], [{"kind": "file", "path": "/a", "extra": 1}])


class TestAstPin(unittest.TestCase):
    def test_compact_imports_only_stdlib_and_ledger(self):
        with open(os.path.join(REPO_ROOT, "raiya", "compact.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        internal = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse([a.name for a in node.names if a.name.split(".")[0] == "raiya"])
            elif isinstance(node, ast.ImportFrom):
                if node.level or (node.module or "").split(".")[0] == "raiya":
                    internal.append(("." * node.level) + (node.module or ""))
                    # `from . import x` は名前を見る
                    internal += [a.name for a in node.names]
        for forbidden in ("backend", "engine", "hooks", "config", "storage", "alaya", "manas"):
            for n in internal:
                self.assertNotIn(forbidden, n.replace(".", " ").split(), n)
        self.assertTrue(set(internal) <= {".ledger", "strip_pasted", "read_tool_refs"}, internal)
        self.assertIsNotNone(compact)


if __name__ == "__main__":
    unittest.main()
