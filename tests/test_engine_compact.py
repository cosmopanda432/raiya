"""`MemoryEngine.compact` の pin。spec: docs/superpowers/specs/2026-10-27-compact-design.md §10 テスト 11-22・23(sys.modules)・24。

一時 state dir (`tempfile.mkdtemp` で自作したものだけを消す)。`RAIYA_CONFIG=""`。LLM は使わない。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import compact as _compact_mod  # noqa: E402
from raiya import config, storage  # noqa: E402
from raiya.engine import (  # noqa: E402
    COMPACT_REASON_BUDGET,
    COMPACT_REASON_NO_CIRCLE,
    COMPACT_REASON_OUTER_CUT,
    COMPACT_REASON_UNKNOWN,
    COMPACT_RECENT_TURNS,
    INJECT_DISPOSITIONS_ENV,
    MemoryEngine,
)
from raiya.hooks.core import RECALL_CONTEXT_TURNS  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import add_ledger_body, table_digest  # noqa: E402

try:
    import anyio  # noqa: F401
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:  # pragma: no cover - 環境依存
    HAS_MCP = False

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
P = "/home/dev/p"
Q = "/home/dev/q"
TEXT = "猫の話"


def _seed(seed_id, conditions, kind="決定", sansho="圓成實", strength=1, project=P, superseded_by=None,
          evidences=None, withdrawn=None, content=None):
    return {
        "id": seed_id, "content": content or "種子{}".format(seed_id), "strength": strength,
        "conditions": list(conditions), "polarity": "無記", "sansho": sansho, "kind": kind, "project": project,
        "superseded_by": superseded_by, "withdrawn": withdrawn,
        "evidences": [{"life": 1, "source": "user", "span": "x", "ledger_id": i} for i in (evidences or [])],
        "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0,
    }


def _row(i, session=None, **extra):
    row = {
        "id": i, "life": 1, "session_id": session if session is not None else ("s1" if i % 2 else "s2"),
        "ts": float(i), "user_text": "ユーザー{}".format(i), "assistant_text": "返答{}".format(i),
        "grounding_text": "ユーザー{}".format(i), "project": P,
    }
    row.update(extra)
    return row


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_compact_")
        self.state = os.path.join(self.tmpdir, "state")
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        env["RAIYA_CONFIG"] = ""
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        config._reset()
        self.addCleanup(config._reset)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write(self, seeds, rows=None, life=3):
        rows = rows if rows is not None else [_row(i) for i in range(1, 15)]
        write_state(self.state, {"seeds": seeds, "alaya_meta": {"life": life}, "ledger_rows": rows})
        return MemoryEngine(state_dir=self.state, new_life=False)

    @staticmethod
    def turn(result, i):
        return next(t for t in result["turns"] if t["id"] == i)


# 窓: 10 以上が直近 5 件 (10-14)。2・4・8 は ①② でしか入れない。重複と台帳に無い id (777) を含む。
WINDOW = [4, 2, 8, 2, 10, 11, 12, 13, 14, 777]


class TestArguments(_Case):
    def test_type_violations(self):
        eng = self.write([])
        for bad in ("12", {1: 2}, None, 5):
            with self.assertRaises(TypeError, msg=repr(bad)):
                eng.compact(bad, TEXT)
        for bad in (True, "1", 1.5, None):
            with self.assertRaises(TypeError, msg=repr(bad)):
                eng.compact([1, bad], TEXT)
        for bad in (None, 5, ["猫"]):
            with self.assertRaises(TypeError):
                eng.compact([1], bad)
        for bad in (True, 1.5, "10"):
            with self.assertRaises(TypeError):
                eng.compact([1], TEXT, budget_chars=bad)
        with self.assertRaises(ValueError):
            eng.compact([1], TEXT, budget_chars=-1)
        with self.assertRaises(TypeError):
            eng.compact([1], TEXT, project=5)
        eng.compact((1,), TEXT)  # tuple は通る

    def test_dedupe_ascending_and_empty_window(self):
        eng = self.write([])
        result = eng.compact([3, 1, 3, 2], TEXT)
        self.assertEqual([t["id"] for t in result["turns"]], [1, 2, 3])
        empty = eng.compact([], TEXT, budget_chars=0)
        self.assertEqual(empty, {
            "keep": [], "elided": [], "unknown_ids": [], "narrative": "", "turns": [], "seeds": [],
            "cut": {"germinated": 0, "resume": 0, "recent": 0}, "chars": 0, "budget_chars": 0,
            "window_sessions": 0, "ledger_warning": None, "alaya_warning": None,
        })

    def test_result_shape(self):
        eng = self.write([_seed(1, ["猫"], evidences=[2])])
        result = eng.compact(WINDOW, TEXT)
        self.assertEqual(set(result), {
            "keep", "elided", "unknown_ids", "narrative", "turns", "seeds", "cut", "chars", "budget_chars",
            "window_sessions", "ledger_warning", "alaya_warning",
        })
        self.assertEqual(result["chars"], len(result["narrative"]))
        self.assertIsNone(result["budget_chars"])
        self.assertEqual(set(self.turn(result, 2)), {
            "id", "decision", "circle", "seed_ids", "reason", "session_id", "chars", "tool_refs",
        })


class TestCircles(_Case):
    def test_germinated_evidence_inside_outside_off_ledger(self):
        seed = _seed(1, ["猫"], evidences=[2, 4, 6])
        seed["evidences"].append({"life": 1, "source": "user", "span": "x", "ledger_id": None})
        eng = self.write([seed])
        result = eng.compact(WINDOW, TEXT, project=P)
        self.assertEqual(result["seeds"], [{
            "id": 1, "kind": "決定", "sansho": "圓成實", "circle": "germinated",
            "in_window": [2, 4], "outside_window": 1, "off_ledger": 1,
        }])
        self.assertEqual(self.turn(result, 2)["circle"], "germinated")
        self.assertEqual(self.turn(result, 4)["seed_ids"], [1])
        self.assertEqual(result["keep"], [2, 4, 10, 11, 12, 13, 14])
        self.assertNotIn(6, [t["id"] for t in result["turns"]])  # 窓の外は候補にしない

    def test_inherited_exclusions_are_not_candidates(self):
        twin_conditions = ["猫", "にゃー"]
        seeds = [
            _seed(1, twin_conditions, evidences=[10]),
            _seed(2, ["猫"], withdrawn={"by": "me", "reason": "r", "ts": 1}, evidences=[8]),
            _seed(3, ["猫"], superseded_by=1, evidences=[8]),
            _seed(4, ["猫"], project=Q, evidences=[8]),
            _seed(5, twin_conditions, sansho="依他起", strength=2, evidences=[8]),
            _seed(6, ["猫"], kind="所在", content="設計書は {} にある".format(os.path.join(self.tmpdir, "gone", "x.txt")),
                  evidences=[8]),
        ]
        eng = self.write(seeds)
        result = eng.compact(WINDOW, TEXT, project=P)
        self.assertEqual([s["id"] for s in result["seeds"]], [1])
        turn8 = self.turn(result, 8)
        self.assertEqual((turn8["decision"], turn8["circle"], turn8["seed_ids"]), ("elided", None, []))
        self.assertEqual(turn8["reason"], COMPACT_REASON_NO_CIRCLE)
        # 前提の確認: 同じ入力で explain_recall は 1 だけが発芽する。
        self.assertEqual([g["id"] for g in eng.explain_recall(TEXT, project=P)["germinated"]], [1])

    def test_resume_point_circle(self):
        seeds = [_seed(7, [], kind="再開点", project=P, evidences=[8]),
                 _seed(8, [], kind="再開点", project=P, evidences=[4], withdrawn={"by": "m", "reason": "r", "ts": 1})]
        result = self.write(seeds).compact(WINDOW, TEXT, project=P)
        self.assertEqual([(s["id"], s["circle"], s["in_window"]) for s in result["seeds"]], [(7, "resume", [8])])
        self.assertEqual(self.turn(result, 8)["circle"], "resume")
        self.assertEqual(self.turn(result, 4)["circle"], None)  # 取り下げた再開点は入らない
        self.assertIn(8, result["keep"])
        self.assertNotIn(4, result["keep"])

    def test_resume_point_global_and_project_scope(self):
        seeds = [_seed(7, [], kind="再開点", project=None, evidences=[8])]
        eng = self.write(seeds)
        self.assertEqual(eng.compact(WINDOW, TEXT, project=P)["turns"][2]["circle"], "resume")
        self.assertEqual(eng.compact(WINDOW, TEXT, project=None)["turns"][2]["circle"], "resume")

    def test_innermost_circle_owns_the_row(self):
        seeds = [_seed(1, ["猫"], evidences=[8]), _seed(7, [], kind="再開点", evidences=[8, 4])]
        result = self.write(seeds).compact(WINDOW, TEXT, project=P)
        t8 = self.turn(result, 8)
        self.assertEqual((t8["circle"], t8["seed_ids"]), ("germinated", [1, 7]))
        self.assertEqual(self.turn(result, 4)["circle"], "resume")
        self.assertEqual(self.turn(result, 4)["seed_ids"], [7])
        # 10 以降は ③ (どの種子も引かない)
        self.assertEqual(self.turn(result, 12)["circle"], "recent")
        self.assertEqual(self.turn(result, 12)["seed_ids"], [])

    def test_recent_is_five_largest_known_ids(self):
        eng = self.write([])
        self.assertEqual(COMPACT_RECENT_TURNS, RECALL_CONTEXT_TURNS)
        result = eng.compact(WINDOW, TEXT)
        self.assertEqual(result["keep"], [10, 11, 12, 13, 14])  # 777 は unknown なので数えない
        self.assertEqual(self.turn(result, 8)["reason"], COMPACT_REASON_NO_CIRCLE)
        self.assertEqual(self.turn(result, 8)["circle"], None)

    def test_unknown_ids(self):
        rows = [_row(i) for i in range(1, 15)]
        eng = self.write([], rows)
        add_ledger_body(self.state, json.dumps({"id": 15, "user_text": 5}), row_id=15)  # 読めない行
        result = eng.compact([13, 14, 15, 777], TEXT)
        self.assertEqual(result["unknown_ids"], [15, 777])
        for i in (15, 777):
            t = self.turn(result, i)
            self.assertEqual((t["decision"], t["reason"], t["chars"], t["circle"]), ("unknown", COMPACT_REASON_UNKNOWN, 0, None))
            self.assertNotIn(i, result["keep"])
        self.assertEqual(result["elided"], [])
        self.assertNotIn("#15", result["narrative"])
        self.assertNotIn("id 777", result["narrative"])

    def test_window_sessions_and_session_id(self):
        rows = [_row(1, session="a"), _row(2, session="b"), _row(3, session="a"), _row(4, session="x")]
        rows[3]["session_id"] = None
        eng = self.write([], rows)
        result = eng.compact([1, 2, 3, 4, 99], TEXT)
        self.assertEqual(result["window_sessions"], 3)  # a, b, None
        self.assertEqual([self.turn(result, i)["session_id"] for i in (1, 2, 3, 4)], ["a", "b", "a", None])
        self.assertIsNone(self.turn(result, 99)["session_id"])

    def test_tool_refs_only_on_keep_rows(self):
        refs = [{"kind": "file", "path": "/abs/a.py", "line_start": 1, "line_end": 2}, {"kind": "url"}]
        rows = [_row(i, tool_refs=refs) for i in range(1, 8)]
        eng = self.write([], rows)
        result = eng.compact([1, 2, 3, 4, 5, 6, 7], TEXT)
        self.assertEqual(result["keep"], [3, 4, 5, 6, 7])
        self.assertEqual(self.turn(result, 7)["tool_refs"], [{"kind": "file", "path": "/abs/a.py", "line_start": 1, "line_end": 2}])
        self.assertIsNone(self.turn(result, 1)["tool_refs"])
        self.assertIn("/abs/a.py:1-2", result["narrative"])

    def test_tool_refs_absent_on_keep_row_is_none(self):
        result = self.write([]).compact([1], TEXT)
        self.assertIsNone(self.turn(result, 1)["tool_refs"])

    def test_budget_reasons_and_cut(self):
        seeds = [_seed(1, ["猫"], evidences=[2, 4]), _seed(7, [], kind="再開点", evidences=[8])]
        eng = self.write(seeds)
        full = eng.compact(WINDOW, TEXT, project=P)
        self.assertEqual(full["keep"], [2, 4, 8, 10, 11, 12, 13, 14])
        self.assertEqual(full["cut"], {"germinated": 0, "resume": 0, "recent": 0})
        known = [2, 4, 8, 10, 11, 12, 13, 14]
        rows = {i: _row(i) for i in known}
        # ① の 1 件目 (#2) だけが入る予算。
        budget = len(_compact_mod.plan_compaction(rows, known, [("germinated", [2])], None)["narrative"])
        result = eng.compact(WINDOW, TEXT, project=P, budget_chars=budget)
        self.assertEqual(result["keep"], [2])
        self.assertEqual(result["chars"], budget)
        self.assertEqual(result["cut"], {"germinated": 1, "resume": 1, "recent": 5})
        self.assertEqual(result["budget_chars"], budget)
        reasons = {t["id"]: t["reason"] for t in result["turns"]}
        self.assertEqual(reasons[2], None)
        self.assertEqual(reasons[4], COMPACT_REASON_BUDGET)
        self.assertEqual({reasons[i] for i in (8, 10, 11, 12, 13, 14)}, {COMPACT_REASON_OUTER_CUT})
        self.assertEqual(reasons[777], COMPACT_REASON_UNKNOWN)

    def test_budget_equal_to_floor_and_below(self):
        seeds = [_seed(1, ["猫"], evidences=[2, 4]), _seed(7, [], kind="再開点", evidences=[8])]
        eng = self.write(seeds)
        floor = eng.compact(WINDOW, TEXT, project=P, budget_chars=0)
        self.assertEqual(floor["keep"], [])
        self.assertEqual(floor["cut"], {"germinated": 2, "resume": 1, "recent": 5})
        self.assertGreater(floor["chars"], 0)
        self.assertEqual(len(floor["elided"]), 1)
        equal = eng.compact(WINDOW, TEXT, project=P, budget_chars=floor["chars"])
        self.assertEqual((equal["keep"], equal["cut"], equal["narrative"]), (floor["keep"], floor["cut"], floor["narrative"]))
        self.assertEqual(equal["chars"], equal["budget_chars"])
        below = eng.compact(WINDOW, TEXT, project=P, budget_chars=floor["chars"] - 1)
        self.assertEqual((below["keep"], below["cut"]), ([], floor["cut"]))
        self.assertGreater(below["chars"], below["budget_chars"])

    def test_recent_no_refill(self):
        eng = self.write([_seed(1, ["猫"], evidences=[13])])
        result = eng.compact(list(range(1, 15)), TEXT, project=P)
        self.assertEqual(self.turn(result, 13)["circle"], "germinated")
        self.assertEqual([i for i in range(1, 15) if self.turn(result, i)["circle"] == "recent"], [10, 11, 12, 14])
        self.assertEqual(self.turn(result, 9)["reason"], COMPACT_REASON_NO_CIRCLE)
        self.assertEqual(result["keep"], [10, 11, 12, 13, 14])
        cut = eng.compact(list(range(1, 15)), TEXT, project=P, budget_chars=0)
        self.assertEqual(cut["cut"], {"germinated": 1, "resume": 0, "recent": 4})

    def test_other_project_row_cited_by_p_seed_is_kept(self):
        rows = [_row(i) for i in range(1, 15)]
        rows[7]["project"] = Q  # 台帳 #8
        result = self.write([_seed(1, ["猫"], evidences=[8])], rows).compact(WINDOW, TEXT, project=P)
        self.assertEqual((self.turn(result, 8)["circle"], self.turn(result, 8)["decision"]), ("germinated", "keep"))

    def test_evidence_counts_unknown_and_malformed_ledger_ids(self):
        seed = _seed(1, ["猫"], evidences=[2, 777])  # 777 = 窓にあるが台帳に無い
        for bad in (True, "2"):
            seed["evidences"].append({"life": 1, "source": "user", "span": "x", "ledger_id": bad})
        result = self.write([seed]).compact(WINDOW, TEXT, project=P)
        entry = result["seeds"][0]
        self.assertEqual((entry["in_window"], entry["outside_window"], entry["off_ledger"]), ([2], 1, 2))

    def test_non_str_session_id_is_none(self):
        rows = [_row(1, session="a"), _row(2), _row(3), _row(4)]
        rows[1]["session_id"] = ["x"]
        rows[2]["session_id"] = 7
        rows[3]["session_id"] = None
        result = self.write([], rows).compact([1, 2, 3, 4], TEXT)
        self.assertEqual([self.turn(result, i)["session_id"] for i in (1, 2, 3, 4)], ["a", None, None, None])
        self.assertEqual(result["window_sessions"], 2)

    def test_keep_row_with_empty_tool_refs_is_empty_list(self):
        rows = [_row(1, tool_refs=[]), _row(2, tool_refs=[{"kind": "url"}])]
        result = self.write([], rows).compact([1, 2], TEXT)
        self.assertEqual([self.turn(result, i)["tool_refs"] for i in (1, 2)], [[], []])
        self.assertNotIn("ツール]", result["narrative"])

    def test_all_unknown_window_has_no_narrative(self):
        result = self.write([_seed(1, ["猫"], evidences=[2])]).compact([777, 888], TEXT, project=P, budget_chars=10)
        self.assertEqual((result["keep"], result["elided"], result["narrative"], result["chars"]), ([], [], "", 0))
        self.assertEqual(result["cut"], {"germinated": 0, "resume": 0, "recent": 0})
        self.assertEqual(result["unknown_ids"], [777, 888])
        self.assertEqual([(t["id"], t["decision"]) for t in result["turns"]], [(777, "unknown"), (888, "unknown")])
        self.assertEqual(result["window_sessions"], 0)


class TestReadOnly(_Case):
    @staticmethod
    def _digest(path):
        out = {}
        for root, _, files in os.walk(path):
            for name in files:
                if name == ".lock":
                    continue
                with open(os.path.join(root, name), "rb") as f:
                    out[os.path.relpath(os.path.join(root, name), path)] = hashlib.sha256(f.read()).hexdigest()
        return out

    def test_compact_writes_nothing(self):
        seeds = [_seed(1, ["猫"], evidences=[2, 4]), _seed(7, [], kind="再開点", evidences=[8])]
        eng = self.write(seeds)
        eng.recall("猫", project=P, session_id="s1")  # manas_log に 1 行作っておく
        before = self._digest(self.state)
        before_tables = table_digest(self.state)
        audit_before = eng.audit()
        seed = next(s for s in eng.inspect_seeds() if s["id"] == 1)
        seed_before = (seed["fire_count"], seed["last_fired"], seed["miss_streak"])
        self.assertEqual(seed_before[0], 1)
        log_before = len(eng.injection_log(50))
        upto_before = audit_before["ledger"]["consolidated_upto"]
        totals = []
        with mock.patch.object(storage, "READ_CLOSE_HOOK", totals.append):
            for budget in (None, 0, 500):
                eng.compact(WINDOW, TEXT, project=P, budget_chars=budget)
            eng.compact([], TEXT)
        self.assertGreaterEqual(len(totals), 4)
        self.assertEqual(set(totals), {0})
        self.assertEqual(self._digest(self.state), before)
        self.assertEqual(table_digest(self.state), before_tables)
        audit_after = eng.audit()
        self.assertEqual(audit_after["life"], audit_before["life"])
        self.assertEqual(audit_after["basis_counts"], audit_before["basis_counts"])
        seed = next(s for s in eng.inspect_seeds() if s["id"] == 1)
        self.assertEqual((seed["fire_count"], seed["last_fired"], seed["miss_streak"]), seed_before)
        self.assertEqual(len(eng.injection_log(50)), log_before)
        self.assertEqual(eng.audit()["ledger"]["consolidated_upto"], upto_before)

    def test_does_not_use_write_paths(self):
        eng = self.write([_seed(1, ["猫"], evidences=[2])])
        with mock.patch.object(MemoryEngine, "_locked", side_effect=AssertionError("_locked")), \
                mock.patch.object(type(eng.alaya), "_mark_fired", side_effect=AssertionError("_mark_fired")), \
                mock.patch.object(type(eng.alaya), "germinate", side_effect=AssertionError("germinate")), \
                mock.patch.object(MemoryEngine, "begin_new_life", side_effect=AssertionError("begin_new_life")), \
                mock.patch.object(type(eng.manas), "inject_composed", side_effect=AssertionError("inject_composed")), \
                mock.patch.object(type(eng.manas), "compose_injection", side_effect=AssertionError("compose_injection")):
            eng.compact(WINDOW, TEXT, project=P)


class TestGerminationSingleSource(_Case):
    def _check(self):
        seeds = [
            _seed(1, ["猫"], evidences=[2]),
            _seed(2, ["猫"], kind="回避策", sansho="依他起", strength=2, evidences=[4]),
            _seed(3, ["猫", "犬"], kind="性向", sansho="依他起", strength=2, evidences=[8]),
            _seed(4, ["鳥"], evidences=[10]),
        ]
        eng = self.write(seeds)
        text = "猫と犬の話"
        expected = [g["id"] for g in eng.explain_recall(text, project=P)["germinated"]]
        result = eng.compact(WINDOW, text, project=P)
        self.assertEqual([s["id"] for s in result["seeds"] if s["circle"] == "germinated"], expected)
        return expected

    def test_equals_explain_recall_default(self):
        self.assertEqual(self._check(), [1, 2])

    def test_equals_explain_recall_with_dispositions(self):
        with mock.patch.dict(os.environ, {INJECT_DISPOSITIONS_ENV: "1"}):
            ids = self._check()
        self.assertIn(3, ids)


class TestHostIndependence(_Case):
    def test_backend_not_imported_after_compact_fresh_process(self):
        write_state(self.state, {"seeds": [_seed(1, ["猫"], evidences=[2])], "alaya_meta": {"life": 1},
                                 "ledger_rows": [_row(i) for i in range(1, 4)]})
        code = (
            "import os, sys\n"
            "sys.path.insert(0, {root!r})\n"
            "os.environ['RAIYA_CONFIG'] = ''\n"
            "from raiya.engine import MemoryEngine\n"
            "eng = MemoryEngine(state_dir={state!r}, new_life=False)\n"
            "r = eng.compact([1, 2, 3], '猫', project={p!r})\n"
            "print(r['keep'], 'raiya.backend' in sys.modules)\n"
        ).format(root=REPO_ROOT, state=self.state, p=P)
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[1, 2, 3] False")


class TestReceiptHarnessSection(_Case):
    """受領書の区画「圧縮」(`receipt_harness.run_compact_harness`) を、commit された receipts/ とは独立に pin する。"""

    def test_harness_is_deterministic_and_read_only(self):
        import receipt_harness

        first = receipt_harness.run_compact_harness()
        second = receipt_harness.run_compact_harness()
        self.assertEqual(first, second)
        self.assertIs(first["read_only"], True)
        self.assertIs(first["fired_equals_explain"], True)
        self.assertEqual(first["件数"], 5)
        self.assertEqual(json.loads(json.dumps(first, ensure_ascii=False)), first)  # JSON を通って変わらない
        cases = {c["label"]: c for c in first["詳細"]}
        full = cases["上限なし"]
        self.assertEqual(full["unknown_ids"], [99])  # 窓の重複は 1 回・台帳に無い id は unknown
        self.assertEqual([t[0] for t in full["turns"]], [1, 3, 4, 6, 8, 9, 99])
        self.assertEqual(cases["空の窓"]["narrative"], "")
        floor = cases["床より小さい予算"]
        self.assertEqual(floor["keep"], [])
        self.assertGreater(floor["chars"], floor["budget_chars"])
        self.assertNotIn("ログの全文", json.dumps(first, ensure_ascii=False))  # 貼り付けは窓に戻らない
        self.assertNotIn("秘密の貼り付け", json.dumps(first, ensure_ascii=False))

    def test_compact_receipt_case_labels_hold(self):
        import receipt_harness

        cases = {c["label"]: c for c in receipt_harness.run_compact_harness()["詳細"]}
        mid = cases["①の途中で切れる予算"]
        self.assertGreaterEqual(mid["cut"]["germinated"], 1)
        self.assertTrue({4, 8} & set(mid["keep"]))  # ① の候補 (a の窓の中の根拠) が 1 件以上残る
        third = cases["③だけが落ちる予算"]
        self.assertEqual(third["cut"]["germinated"], 0)
        self.assertEqual(third["cut"]["resume"], 0)
        self.assertGreaterEqual(third["cut"]["recent"], 1)
        full = cases["上限なし"]
        self.assertEqual([s[:2] for s in full["seeds"]], [[1, "germinated"], [2, "germinated"], [3, "resume"]])
        self.assertFalse({4, 5, 6, 7} & {s[0] for s in full["seeds"]})
        # 除外された種子 (d: 取り下げ) だけが引く行 1 は、どの円にも入らない。
        # 受領書の turns は seed_ids を持たないので、「d だけが引く」は fixture の側で pin する
        with open(receipt_harness.COMPACT_FIXTURE, encoding="utf-8") as f:
            fixture_seeds = json.load(f)["seeds"]
        d = next(s for s in fixture_seeds if s["id"] == 4)
        self.assertEqual([e["ledger_id"] for e in d["evidences"]], [1])
        self.assertIsNotNone(d["withdrawn"])
        self.assertEqual(
            [s["id"] for s in fixture_seeds if any(e.get("ledger_id") == 1 for e in s.get("evidences", []))], [4])
        self.assertIn([1, "elided", None, "どの円にも入らない"], full["turns"])


@unittest.skipUnless(HAS_MCP, "公式 mcp SDK が未インストール (pip install -e '.[mcp]')")
class TestMcpUnchanged(unittest.TestCase):
    def test_compact_is_not_an_mcp_tool(self):
        from raiya.mcp_server import TOOLS
        self.assertEqual(len(TOOLS), 5)
        self.assertFalse([t["name"] for t in TOOLS if "compact" in t["name"]])


if __name__ == "__main__":
    unittest.main()
