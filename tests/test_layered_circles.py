"""同心円と容量 (層化設計書 B3・B4・B8-B11・plan 2026-10-25-layered-memory-implementation §2・§3・§6・§8) の決定論 pin (スライス 2)。

`alaya._circle_verdicts` / `_index_verdicts(fired_ids=)`、`manas.compose_injection`、`engine.recall` / `explain_recall` の
`circles`、`raiya-inspect explain` の節、MCP `memory_recall` の結果。群の fixture は
`tests/fixtures/layered_groups_20261025.json` (スライス 1)。LLM は使わない。
テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya.snapshot import alaya_snapshot, write_state  # noqa: E402
from raiya import storage  # noqa: E402
from tests._state_fixture import clear_state, manas_store_at, table_bodies, table_digest  # noqa: E402
from tests._state_fixture import ro as storage_ro  # noqa: E402
from raiya import inspect_cli  # noqa: E402
from raiya import manas as manas_mod  # noqa: E402
from raiya.alaya import (  # noqa: E402
    INDEX_REASON_FIRED,
    INDEX_REASON_GLOBAL_GROUP,
    INDEX_REASON_GROUP_FIRED,
    INDEX_REASON_GROUP_MEMBER,
    _twin_shadows,
)
from raiya.engine import INDEX_REASON_BUDGET, INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from raiya.manas import (  # noqa: E402
    INJECTION_MAX_CHARS,
    NARRATIVE_HEADER,
    SIBLING_IDS_MAX,
    ManasStore,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "layered_groups_20261025.json")
MCP_SERVER_PATH = os.path.join(os.path.dirname(__file__), "..", "raiya", "mcp_server.py")
P = "/home/dev/p"
Q = "/home/dev/q"
# fixture の所在の群 (#13 = 休眠・#14 = /usr/bin は在る)・双子 (#15 E / #16 H)・全域の群 (#17/#18) を全部叩く問い
Q_ALL = "所在甲 所在乙 双子甲 双子乙 全域甲 全域乙"


def _seed(seed_id, conditions, kind="決定", sansho="圓成實", strength=1, project=P, content=None, **extra):
    seed = {
        "id": seed_id, "content": content if content is not None else "種子{}".format(seed_id), "strength": strength,
        "conditions": list(conditions), "polarity": "無記", "sansho": sansho, "kind": kind, "project": project,
        "superseded_by": None, "withdrawn": None, "evidences": [], "history": [], "planted_at": 1,
        "last_fired": None, "fire_count": 0, "miss_streak": 0,
    }
    seed.update(extra)
    return seed


def _load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _ids(seeds):
    return [s["id"] for s in seeds]


def _tree_digest(path):
    out = {}
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


class _StateCase(unittest.TestCase):
    """自分の mkdtemp に alaya.json (非正準の JSON) を置く。消すのはその mkdtemp だけ。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_circles_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.state = os.path.join(self.tmpdir, "state")
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(self.state)

    def write(self, seeds=None, base=None, extra=None):
        """state を (書き直して) 置く。`extra` = 追加の論理スナップショット節 (imprint_log / ego_weight 等)。"""
        data = copy.deepcopy(base if base is not None else _load_fixture())
        if seeds is not None:
            data["seeds"] = seeds
            data["next_id"] = max([s["id"] for s in seeds] + [0]) + 1
        clear_state(self.state)  # write_state は既存 state を拒否する
        snap = alaya_snapshot(data)
        snap.update(extra or {})
        write_state(self.state, snap)  # 詰めた (非正準) JSON: 読む動詞のバイト不変 pin が同値保存でも検出できる
        return self.engine()

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = inspect_cli.main(["--state-dir", self.state] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def assert_explain_equals_recall(self, eng, text, **kw):
        x = eng.explain_recall(text, **kw)
        r = eng.recall(text, **kw)
        self.assertEqual(_ids(x["seeds"]), _ids(r["seeds"]))
        self.assertEqual(_ids(x["index"]), _ids(r["index"]))
        self.assertEqual(x["narrative"], r["narrative"])
        self.assertEqual(x["circles"], r["circles"])
        self.assertEqual(x["resume"], r["resume"])
        self.assertLessEqual(len(r["narrative"]), r["circles"]["max_chars"])
        self.assertEqual(r["circles"]["chars"], len(r["narrative"]))
        return x, r


# ---------------------------------------------------------------- 群は書かれない (pin 2)
class TestGroupsAreNeverWritten(_StateCase):
    def test_recall_adds_no_group_or_circle_keys_to_state(self):
        eng = self.write()
        before = [json.loads(b) for b in table_bodies(self.state, "seeds", order="pos")]
        eng.recall(Q_ALL, project=P)
        eng.recall("無関係", project=P)
        eng.recall("無関係", project=None)
        after = [json.loads(b) for b in table_bodies(self.state, "seeds", order="pos")]
        self.assertEqual(len(after), len(before))
        for b, a in zip(before, after):
            self.assertEqual(set(a), set(b), b["id"])
        for key in ("groups", "circles", "group", "cut", "label"):
            self.assertTrue(all(key not in s for s in after))
        with storage_ro(self.state) as conn:  # 群・円の列も表も無い (派生は DB に置かない)
            self.assertEqual(
                [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
                sorted(storage.REQUIRED_TABLES),
            )
            self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(seeds)")], ["pos", "id", "kind", "project", "live", "body"])
        self.assertNotIn("groups_cache.json", os.listdir(self.state))

    def test_recall_leaves_imprint_log_byte_identical(self):
        eng = self.write(extra={"imprint_log": [{"decision": "reject", "reason": "r", "seed_id": None}]})
        before = table_bodies(self.state, "imprint_log")
        digest = table_digest(self.state)["imprint_log"]
        eng.recall(Q_ALL, project=P)
        eng.explain_recall(Q_ALL, project=P)
        self.assertEqual(table_bodies(self.state, "imprint_log"), before)
        self.assertEqual(table_digest(self.state)["imprint_log"], digest)

    def test_only_observation_fields_change_in_the_seeds_table(self):
        eng = self.write()
        before = [json.loads(b) for b in table_bodies(self.state, "seeds", order="pos")]
        eng.recall(Q_ALL, project=P)
        after = [json.loads(b) for b in table_bodies(self.state, "seeds", order="pos")]
        observed = {"last_fired", "fire_count", "miss_streak"}
        for b, a in zip(before, after):
            changed = {k for k in b if b[k] != a[k]}
            self.assertLessEqual(changed, observed, b["id"])

    def test_explain_verb_in_every_variant_writes_nothing(self):
        self.write()
        eng = self.engine()
        eng.recall(Q_ALL, project=P, session_id="s1")
        before = _tree_digest(self.state)
        before_tables = table_digest(self.state)
        with mock.patch.object(manas_mod, "INJECTION_MAX_CHARS", 300):
            for argv in (["explain", Q_ALL, "--project", P], ["--json", "explain", "無関係", "--project", P],
                         ["explain", "無関係", "--project", "", "--session-start"], ["explain", "方針", "--context", "検証"],
                         ["show", "--all"], ["injected"], ["why", "13"], ["why", "16"]):
                code, _, err = self.run_cli(*argv)
                self.assertIn(code, (0, 1), (argv, err))
        after = _tree_digest(self.state)
        self.assertEqual({k: v for k, v in after.items() if k != ".lock"}, {k: v for k, v in before.items() if k != ".lock"})
        self.assertEqual(table_digest(self.state), before_tables)

class TestIndexWithGroups(_StateCase):
    def test_group_collapses_to_representative_with_mark(self):
        eng = self.write()
        r = eng.recall("無関係", project=P)
        self.assertEqual(_ids(r["index"]), [1, 4, 6, 15, 8, 10, 11, 12, 14, 9, 19])
        self.assertIn("- 決定: 種子1 (強度3) [群「方針」3 件・代表 #1]", r["narrative"])
        self.assertIn("【記録の索引 (全 11 件)】", r["narrative"])
        self.assertIn("- 決定: 種子8 (強度1)\n", r["narrative"])  # 1 件の種子は今の行のまま (印なし)
        self.assertEqual([g["representative"] for g in r["circles"]["groups"]], [1, 4, 6, 15, 14])

    def test_reasons_follow_the_inject_order(self):
        eng = self.write()
        reasons = {x["id"]: x["reason"] for x in eng.explain_recall("方針 検証", project=P)["index_rejected"]}
        self.assertEqual(reasons[1], INDEX_REASON_FIRED)                       # 発芽 (群の前)
        self.assertEqual(reasons[5], INDEX_REASON_GROUP_MEMBER.format("共通乙", 4))
        self.assertNotIn(11, reasons)   # 生きた圓成實が無い群は群として扱わず、成員は 1 件ずつ今の規則で ③ に載る (F4)
        self.assertEqual(reasons[16], "圓成實の双子 #15 がある")                   # 双子 (発芽・群の前)
        self.assertEqual(reasons[17], INDEX_REASON_GLOBAL_GROUP.format("全域乙"))
        self.assertEqual(reasons[23], "取り下げ中")
        self.assertEqual(reasons[24], "superseded")
        self.assertEqual(reasons[25], "project 違い")
        self.assertEqual(reasons[20], "kind の上限 0")

    def test_group_with_fired_member_is_not_listed_again(self):
        eng = self.write()
        x = eng.explain_recall("共通甲 共通乙", project=P)
        reasons = {r["id"]: r["reason"] for r in x["index_rejected"]}
        self.assertEqual(reasons[4], INDEX_REASON_FIRED)
        # #1 の群は発芽していないので代表 #1 が残る。発芽した群 (4,5) (6,7) は ③ に代表も出ない。
        self.assertEqual(_ids(x["index"]), [1, 15, 8, 10, 11, 12, 14, 9, 19])
        # 縁に触れた #13 (所在乙) は在る #14 が発芽しなければ ③ の代表は #14
        x = eng.explain_recall("所在甲", project=P)
        reasons = {r["id"]: r["reason"] for r in x["index_rejected"]}
        self.assertEqual(reasons[14], INDEX_REASON_FIRED)
        self.assertEqual(reasons[13], INDEX_REASON_GROUP_FIRED.format("所在乙", 14))

    def test_array_order_breaks_ties_between_equal_singletons_and_representatives(self):
        seeds = [_seed(5, ["単独5"]), _seed(3, ["単独3"]), _seed(9, ["群甲", "群乙"]), _seed(2, ["群甲", "群乙"]),
                 _seed(7, ["別甲", "別乙"]), _seed(8, ["別甲", "別乙"])]
        eng = self.write(seeds)
        # 同 kind・同クラス・同強度: 配列順 (id 順ではない)。代表は群の並び (同点は配列順) の先頭。
        self.assertEqual(_ids(eng.recall("無関係", project=P)["index"]), [5, 3, 9, 7])

    def test_kind_limit_applies_per_entry_not_per_seed(self):
        seeds = []
        for i in range(12):   # 12 個の 2 件の群 (= 24 件の種子)。項目は 12 で kind 上限 10 に当たる
            seeds.append(_seed(2 * i + 1, ["群{}甲".format(i), "群{}乙".format(i)], strength=3))
            seeds.append(_seed(2 * i + 2, ["群{}甲".format(i), "群{}乙".format(i)]))
        eng = self.write(seeds)
        x = eng.explain_recall("無関係", project=P)
        self.assertEqual(len(x["index"]), 10)
        self.assertIn("(上位 10 件 / 全 12 件。載っていない 2 件も記録に在る)", x["narrative"])
        reasons = [r["reason"] for r in x["index_rejected"] if r["id"] in (21, 23)]
        self.assertEqual(reasons, ["索引の上限で落ちた", "索引の上限で落ちた"])

    def test_no_group_state_keeps_the_old_index_block_byte_for_byte(self):
        seeds = [_seed(i, ["単独{}".format(i)], strength=1 + i % 3, project=P if i % 2 else None) for i in range(1, 13)]
        seeds.append(_seed(13, ["所在単独"], kind="所在", sansho="依他起", strength=2))
        eng = self.write(seeds)
        r = eng.recall("無関係", project=P)
        store = manas_store_at(os.path.join(self.tmpdir, "m.json"))
        total = eng.alaya.seed_count(project=P, purpose="inject")
        old = NARRATIVE_HEADER + "\n\n" + store.build_index_text(r["index"], total)
        self.assertEqual(r["narrative"], old)
        self.assertIn("上位 11 件 / 全 13 件", r["narrative"].split("\n")[2])  # 決定 10 件上限 + 所在 1 = 11 項目 / 全 13
        self.assertEqual(r["circles"]["groups"], [])
        self.assertEqual(r["circles"]["siblings"], [])
        self.assertEqual(r["circles"]["global"], [])


# ---------------------------------------------------------------- ② 同じ群の他
class TestSiblingLines(_StateCase):
    def test_sibling_line_follows_first_rendered_fired_member(self):
        eng = self.write()
        r = eng.recall(Q_ALL, project=P)
        lines = r["narrative"].split("\n")
        i = lines.index("- 所在: 控えは /usr/bin にある (どの機械にもある path。代表 #13 が休眠・#14 は在る)(強度2)")
        self.assertEqual(lines[i + 1], "  亦如是 [群「所在乙」2 件]: #13")   # ② は休眠を見ない (B10)
        j = lines.index("- 決定: 種子15(強度2)")
        self.assertEqual(lines[j + 1], "  亦如是 [群「双子乙」2 件]: 他に 1 件")  # 双子 #16 は id に並べず k に入る
        self.assertEqual(r["circles"]["siblings"], [
            {"label": "所在乙", "kind": "所在", "project": P, "size": 2, "of": 14, "listed": [13], "more": 0},
            {"label": "双子乙", "kind": "決定", "project": P, "size": 2, "of": 15, "listed": [], "more": 1},
        ])

    def test_twin_hidden_is_never_listed_nor_representative(self):
        seeds = _load_fixture()["seeds"]
        shadows = _twin_shadows(seeds)
        hidden = {s["id"] for s in seeds if id(s) in shadows}
        self.assertEqual(hidden, {16})
        eng = self.write()
        for q in ("無関係", "双子甲 双子乙", Q_ALL):
            x = eng.explain_recall(q, project=P)
            for sl in x["circles"]["siblings"]:
                self.assertFalse(hidden & set(sl["listed"]))
            for g in x["circles"]["groups"]:
                self.assertNotIn(g["representative"], hidden)
            self.assertFalse(hidden & set(_ids(x["index"])))

    def test_k_counts_unrendered_fired_members_and_caps_ids_at_five(self):
        seeds = [_seed(i, ["共有甲", "共有乙", "個別{}".format(i)], content="種子{}".format(i)) for i in range(1, 10)]
        eng = self.write(seeds)
        x = eng.explain_recall("個別1", project=P)   # #1 だけ発芽。残り 8 件 -> 5 件並べて 他に 3 件
        sl = x["circles"]["siblings"][0]
        self.assertEqual((sl["of"], sl["listed"], sl["more"], sl["size"]), (1, [2, 3, 4, 5, 6], 3, 9))
        self.assertEqual(len(sl["listed"]), SIBLING_IDS_MAX)
        self.assertIn("  亦如是 [群「共有乙」9 件]: #2 #3 #4 #5 #6 他に 3 件", x["narrative"])

    def test_group_without_enjou_members_fall_through_as_singletons(self):   # B8 (F4)
        seeds = [_seed(1, ["推定甲", "推定乙"], sansho="依他起", strength=2),
                 _seed(2, ["推定甲", "推定乙", "推定丙"], sansho="依他起", strength=2)]
        eng = self.write(seeds)
        x = eng.explain_recall("推定甲", project=P)
        self.assertEqual(_ids(x["seeds"]), [1, 2])
        self.assertEqual(x["circles"]["siblings"], [])                    # ② は出ない (圓成實が居ない)
        y = eng.explain_recall("無関係", project=P)
        self.assertEqual(_ids(y["index"]), [1, 2])                        # ③ には 1 件ずつ (推定) で載る
        self.assertEqual(y["circles"]["groups"], [])
        self.assertIn("- 決定(推定): 種子1 (強度2)\n- 決定(推定): 種子2 (強度2)", y["narrative"])
        self.assertNotIn("[群", y["narrative"])
        # 未分類 (sansho 欄なし) は今の class の門で落ちる
        un = _seed(3, ["未甲", "未乙"])
        del un["sansho"]
        eng2 = self.write([un, _seed(4, ["未甲", "未乙"], sansho="依他起", strength=2)])
        z = eng2.explain_recall("無関係", project=P)
        self.assertEqual(_ids(z["index"]), [4])
        # 全域の圓成實の無い群は ④ には数える
        g = self.write([_seed(5, ["全甲", "全乙"], sansho="依他起", project=None),
                        _seed(6, ["全甲", "全乙"], sansho="依他起", project=None)])
        self.assertEqual(g.explain_recall("無関係", project=P)["circles"]["global"],
                         [{"kind": "決定", "groups": 1, "seeds": 2}])

    def test_dispositions_group_only_with_env_and_resume_group_never(self):   # B9
        seeds = [_seed(1, ["性向甲", "性向乙"], kind="性向"), _seed(2, ["性向甲", "性向乙", "性向丙"], kind="性向"),
                 _seed(5, ["性向乙", "性向丙"], kind="性向"),   # 問いに 1 語しか触れず発芽しない (群の成員・② に並ぶ)
                 _seed(3, ["再開甲", "再開乙"], kind="再開点"), _seed(4, ["再開甲", "再開乙"], kind="再開点")]
        eng = self.write(seeds)
        x = eng.explain_recall("無関係", project=P)
        self.assertEqual((x["index"], x["circles"]["groups"], x["narrative"]), ([], [], ""))
        s = eng.explain_recall("性向甲 性向乙 再開甲 再開乙", project=P)   # 既定では性向は発芽もしない -> ② も無い
        self.assertEqual((s["seeds"], s["circles"]["siblings"]), ([], []))
        with mock.patch.dict(os.environ, {INJECT_DISPOSITIONS_ENV: "1"}):
            y = eng.explain_recall("無関係", project=P)
            self.assertEqual([g["kind"] for g in y["circles"]["groups"]], ["性向"])
            self.assertEqual(_ids(y["index"]), [1])
            z = eng.explain_recall("性向甲 性向乙", project=P)   # 発芽した性向は ② が出る (性向が有効なときだけ)
            self.assertEqual([sl["kind"] for sl in z["circles"]["siblings"]], ["性向"])
            self.assertEqual(eng.explain_recall("再開甲 再開乙", project=P)["circles"]["siblings"], [])
        self.assertEqual([g["kind"] for g in eng.explain_recall("無関係", project=P)["circles"]["groups"]], [])


# ---------------------------------------------------------------- ④ 全域の群
class TestGlobalLine(_StateCase):
    def test_line_shape_and_only_for_str_project(self):
        eng = self.write()
        r = eng.recall("無関係", project=P)
        self.assertIn("\n\n【全域の群 (件数のみ)】 決定 1 群・2 件", r["narrative"])
        self.assertEqual(r["circles"]["global"], [{"kind": "決定", "groups": 1, "seeds": 2}])
        # 全域の群の成員は ③ に出ない (理由は ④)、全域の 1 件の種子 #19 は今どおり ③ に出る
        self.assertNotIn(17, _ids(r["index"]))
        self.assertIn(19, _ids(r["index"]))
        g = eng.recall("無関係", project=None)
        self.assertEqual(g["circles"]["global"], [])
        self.assertNotIn("全域の群", g["narrative"])
        self.assertIn("- 決定: 種子17 (強度2) [群「全域乙」2 件・代表 #17]", g["narrative"])  # project=None では ③ に出る
        self.assertEqual(_ids(g["index"]), [17, 19])

    def test_never_names_another_project_and_counts_only_global(self):
        eng = self.write()
        for q in ("無関係", "方針 検証 順序", Q_ALL):
            r = eng.recall(q, project=P)
            self.assertNotIn(Q, r["narrative"])
            self.assertNotIn("種子25", r["narrative"])
            self.assertNotIn("種子26", r["narrative"])
            self.assertEqual(r["circles"]["global"], [{"kind": "決定", "groups": 1, "seeds": 2}])
        r = eng.recall("無関係", project=Q)
        self.assertNotIn("種子1 ", r["narrative"])
        self.assertEqual(r["circles"]["global"], [{"kind": "決定", "groups": 1, "seeds": 2}])  # 全域だけ。P の群は数えない

    def test_global_groups_are_counted_per_kind_in_kind_order_and_ignore_gates(self):
        seeds = [_seed(1, ["a", "b"], project=None), _seed(2, ["a", "b"], project=None),
                 _seed(3, ["c", "d"], kind="所在", project=None, content="/nonexistent/raiya-x/y"),
                 _seed(4, ["c", "d"], kind="所在", project=None, content="/nonexistent/raiya-x/z"),
                 _seed(5, ["e", "f"], kind="性向", project=None), _seed(6, ["e", "f"], kind="性向", project=None),
                 _seed(7, ["g", "h"], sansho="依他起", project=None), _seed(8, ["g", "h"], sansho="依他起", project=None),
                 _seed(9, ["a", "b"], project=Q), _seed(10, ["a", "b"], project=Q)]
        eng = self.write(seeds)
        r = eng.recall("無関係", project=P)
        self.assertEqual(r["circles"]["global"], [
            {"kind": "決定", "groups": 2, "seeds": 4},     # 依他起だけの群 (B8) も、数えるだけなので ④ には入る
            {"kind": "所在", "groups": 1, "seeds": 2},     # 休眠も見ない
            {"kind": "性向", "groups": 1, "seeds": 2},     # B9 の門も見ない
        ])
        self.assertIn("【全域の群 (件数のみ)】 決定 2 群・4 件 / 所在 1 群・2 件 / 性向 1 群・2 件", r["narrative"])

    def test_no_line_when_there_is_no_global_group(self):
        eng = self.write([_seed(1, ["a", "b"]), _seed(2, ["a", "b"])])
        r = eng.recall("無関係", project=P)
        self.assertEqual(r["circles"]["global"], [])
        self.assertNotIn("全域の群", r["narrative"])


# ---------------------------------------------------------------- 休眠 (B10)
class TestDormantRepresentative(_StateCase):
    def _spy(self):
        real = os.stat
        touched = []

        def spy(p, *a, **k):
            if isinstance(p, str) and (
                p.startswith("/nonexistent/raiya") or p == "/usr/bin"
                or (p.startswith(self.tmpdir) and not p.startswith(self.state))   # state の保存・読込は数えない
            ):
                touched.append(p)
            return real(p, *a, **k)

        return touched, mock.patch("raiya.alaya.os.stat", side_effect=spy)

    def test_dormant_representative_hands_over_to_next_member(self):
        eng = self.write()
        x = eng.explain_recall("無関係", project=P)
        self.assertEqual(_ids([s for s in x["index"] if s["kind"] == "所在"][:1]), [14])
        reasons = {r["id"]: r["reason"] for r in x["index_rejected"]}
        self.assertIn("休眠", reasons[13])
        self.assertIn("/nonexistent/raiya-fixture/design.md", reasons[13])
        rep = [g for g in x["circles"]["groups"] if g["kind"] == "所在"][0]
        self.assertEqual((rep["representative"], rep["member_ids"], rep["size"]), (14, [13, 14], 2))  # 休眠も件数に数える

    def test_all_dormant_group_is_not_in_the_index_and_not_moved_to_the_global_line(self):
        seeds = [_seed(i, ["所在甲", "所在乙"], kind="所在", content="資料は {} にある".format(
            os.path.join(self.tmpdir, "gone", "f{}.md".format(i)))) for i in (1, 2, 3)]
        eng = self.write(seeds)
        x = eng.explain_recall("無関係", project=P)
        self.assertEqual((x["index"], x["circles"]["groups"], x["circles"]["global"]), ([], [], []))
        self.assertTrue(all("休眠" in r["reason"] for r in x["index_rejected"]))
        self.assertEqual(x["narrative"], "")

    def test_members_after_the_representative_are_never_statted(self):
        seeds = [_seed(1, ["所在甲", "所在乙"], kind="所在", content="資料は {} にある".format(os.path.join(self.tmpdir, "gone1"))),
                 _seed(2, ["所在甲", "所在乙"], kind="所在", content="資料は {} にある".format(self.tmpdir)),
                 _seed(3, ["所在甲", "所在乙"], kind="所在", content="資料は {} にある".format(os.path.join(self.tmpdir, "never3")))]
        eng = self.write(seeds)
        touched, patcher = self._spy()
        with patcher:
            x = eng.explain_recall("無関係", project=P)     # 縁に触れない問い
        third = os.path.join(self.tmpdir, "never3")
        # ③ の代表選びは group の並び (1, 2, 3) の #2 で止まる。発芽側の休眠検査は縁の前に全員 stat する (今のまま) ので
        # #3 が stat されるのは発芽で 1 回だけ。
        self.assertEqual(touched.count(third), 1)
        self.assertEqual(touched.count(os.path.join(self.tmpdir, "gone1")), 2)
        self.assertEqual(touched.count(self.tmpdir), 2)
        self.assertEqual(x["circles"]["groups"][0]["representative"], 2)
        reasons = {r["id"]: r["reason"] for r in x["index_rejected"]}
        self.assertEqual(reasons[3], INDEX_REASON_GROUP_MEMBER.format("所在乙", 2))

    def test_group_representative_follows_group_order(self):
        # 倉庫の順 [#1 強度1, #2 強度3] と群の並び [#2, #1] が違う: 代表は群の並びの先頭 #2。
        seeds = [_seed(1, ["所在甲", "所在乙"], kind="所在", strength=1, content="資料は {} にある".format(self.tmpdir)),
                 _seed(2, ["所在甲", "所在乙"], kind="所在", strength=3, content="資料は {} にある".format(self.tmpdir))]
        eng = self.write(seeds)
        x, r = self.assert_explain_equals_recall(eng, "無関係", project=P)
        self.assertEqual(r["circles"]["groups"][0]["member_ids"], [2, 1])
        self.assertEqual(_ids(r["index"]), [2])
        self.assertIn("・代表 #2]", r["narrative"])
        self.assertNotIn("代表 #1", r["narrative"])
        reasons = {e["id"]: e["reason"] for e in x["index_rejected"]}
        self.assertEqual(reasons[1], INDEX_REASON_GROUP_MEMBER.format("所在乙", 2))

    def test_dormant_first_member_hands_over_in_group_order_not_store_order(self):
        gone2, gone1, never = (os.path.join(self.tmpdir, n) for n in ("gone2", "gone1", "never3"))
        # 倉庫の順 #1 #2 #3、群の並び #2 (強度3) #3 (強度2) #1 (強度1)。#2 が休眠 → 次は群の並びの #3 (倉庫の次の #3 でも #1 でもよい
        # ので区別のため #3 は在る path にし、#1 は後ろなので stat されない)。
        seeds = [_seed(1, ["所在甲", "所在乙"], kind="所在", strength=1, content="資料は {} にある".format(gone1)),
                 _seed(2, ["所在甲", "所在乙"], kind="所在", strength=3, content="資料は {} にある".format(gone2)),
                 _seed(3, ["所在甲", "所在乙"], kind="所在", strength=2, content="資料は {} にある".format(self.tmpdir))]
        eng = self.write(seeds)
        touched, patcher = self._spy()
        with patcher:
            x = eng.explain_recall("無関係", project=P)
        self.assertEqual(x["circles"]["groups"][0]["member_ids"], [2, 3, 1])
        self.assertEqual(x["circles"]["groups"][0]["representative"], 3)
        self.assertEqual(touched.count(gone2), 2)      # 発芽 1 回 + 代表選び 1 回
        self.assertEqual(touched.count(self.tmpdir), 2)
        self.assertEqual(touched.count(gone1), 1)      # 代表より後ろ: 発芽の 1 回だけ (代表選びでは stat されない)
        reasons = {r["id"]: r["reason"] for r in x["index_rejected"]}
        self.assertEqual(reasons[1], INDEX_REASON_GROUP_MEMBER.format("所在乙", 3))

    def test_at_most_two_stats_per_path_per_recall(self):
        eng = self.write()
        for q in ("無関係", "所在甲", "所在乙 双子甲", Q_ALL, "方針 /usr/bin"):
            for verb in (eng.recall, eng.explain_recall):
                touched, patcher = self._spy()
                with patcher:
                    verb(q, project=P)
                for path in set(touched):
                    self.assertLessEqual(touched.count(path), 2, (q, verb.__name__, path))
        touched, patcher = self._spy()
        with patcher:
            eng.recall("無関係", project=P)
        self.assertEqual(touched.count("/nonexistent/raiya-fixture/design.md"), 2)
        self.assertEqual(touched.count("/usr/bin"), 2)

    def test_path_that_comes_back_undorms_the_group_next_recall(self):
        gone = os.path.join(self.tmpdir, "later.md")
        seeds = [_seed(1, ["所在甲", "所在乙"], kind="所在", content="資料は {} にある".format(gone)),
                 _seed(2, ["所在甲", "所在乙"], kind="所在", content="資料は {} にある".format(gone + ".x"))]
        eng = self.write(seeds)
        self.assertEqual(eng.recall("無関係", project=P)["index"], [])
        with open(gone, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertEqual(_ids(eng.recall("無関係", project=P)["index"]), [1])


# ---------------------------------------------------------------- explain == recall (pin 4)
class TestExplainEqualsRecall(_StateCase):
    def test_across_group_twin_dormant_withdrawn_cases(self):
        eng = self.write()
        for q, kw in [("無関係", {}), ("方針 検証", {}), ("共通甲 共通乙", {}), (Q_ALL, {}), ("双子甲", {}),
                      ("所在甲", {}), ("方針", {"session_start": True}), ("無関係", {"project": None}),
                      ("全域甲 全域乙", {"project": None}), ("無関係", {"project": Q}), ("順序", {"context": ["検証", "方針"]})]:
            kw = dict(kw)
            kw.setdefault("project", P)
            with self.subTest(q=q, kw=kw):
                self.assert_explain_equals_recall(eng, q, **kw)

    def test_with_dispositions_enabled(self):
        eng = self.write()
        with mock.patch.dict(os.environ, {INJECT_DISPOSITIONS_ENV: "1"}):
            for q in ("無関係", "性向甲 性向乙"):
                self.assert_explain_equals_recall(eng, q, project=P)

    def test_when_the_budget_cuts(self):
        eng = self.write()
        for limit in (3200, 700, 500, 450, 300, 200):
            with mock.patch.object(manas_mod, "INJECTION_MAX_CHARS", limit):
                for q in ("無関係", Q_ALL, "方針 検証"):
                    with self.subTest(limit=limit, q=q):
                        x, r = self.assert_explain_equals_recall(eng, q, project=P, session_start=True)
                        self.assertEqual(r["circles"]["max_chars"], limit)

    def test_budget_dropped_entries_are_reported_by_explain_as_budget(self):
        eng = self.write()
        with mock.patch.object(manas_mod, "INJECTION_MAX_CHARS", 330):
            x = eng.explain_recall("無関係", project=P)
        shown = set(_ids(x["index"]))
        dropped = [r for r in x["index_rejected"] if r["reason"] == INDEX_REASON_BUDGET]
        self.assertTrue(dropped)
        self.assertEqual(len(dropped), x["circles"]["cut"]["index"])
        self.assertFalse(shown & {r["id"] for r in dropped})
        # 落ちたのは ③ の末尾 (並びの上位だけが連続して残る)
        self.assertEqual(_ids(x["index"]), [1, 4, 6, 15, 8, 10, 11, 12, 14, 9, 19][: len(shown)])

    def test_circles_are_json_native(self):
        eng = self.write()
        r = eng.recall(Q_ALL, project=P)
        self.assertEqual(json.loads(json.dumps(r["circles"], ensure_ascii=False)), r["circles"])
        self.assertEqual(set(r), {"seeds", "narrative", "index", "resume", "circles", "alaya_warning", "manas_warning"})
        self.assertEqual(set(r["circles"]), {"siblings", "groups", "global", "cut", "chars", "max_chars"})
        self.assertEqual(set(r["circles"]["cut"]), {"germinated", "siblings", "index", "global"})
        self.assertEqual(r["circles"]["max_chars"], INJECTION_MAX_CHARS)

    def test_recall_index_and_seeds_are_deep_copies(self):
        eng = self.write()
        r = eng.recall("無関係", project=P)
        r["index"][0]["content"] = "改竄"
        r["circles"]["groups"][0]["member_ids"].append(999)
        again = eng.explain_recall("無関係", project=P)
        self.assertNotEqual(again["index"][0]["content"], "改竄")
        self.assertNotIn(999, again["circles"]["groups"][0]["member_ids"])

    def test_injection_log_gets_cut_after_project_and_index_ids_are_the_rendered_ones(self):
        eng = self.write()
        with mock.patch.object(manas_mod, "INJECTION_MAX_CHARS", 330):
            r = eng.recall("無関係", project=P, session_id="s1")
        row = eng.injection_log(1)[0]
        self.assertEqual(list(row)[:9], ["life", "ego_weight", "injected_text", "seed_ids", "index_ids", "ts", "session_id", "project", "cut"])
        self.assertEqual(row["index_ids"], _ids(r["index"]))
        self.assertEqual(row["cut"], r["circles"]["cut"])
        self.assertGreater(row["cut"]["index"], 0)
        self.assertEqual(row["seed_ids"], _ids(r["seeds"]))


# ---------------------------------------------------------------- 容量 (pin 9)
def _entry(seed, group=None):
    return {"seed": seed, "group": group}


class TestComposeInjectionBudget(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_compose_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = manas_store_at(os.path.join(self.tmpdir, "manas.json"))

    def scenario(self, resume=None):
        fired = [_seed(1, ["a"], strength=3, content="発芽その一" * 4), _seed(2, ["a"], strength=2, content="発芽その二" * 4)]
        group = {"label": "群ラベル", "kind": "決定", "project": P, "size": 9, "representative": 30,
                 "member_ids": list(range(30, 39))}
        entries = [_entry(_seed(30 + i, ["x"], content="索引の項目{}".format(i) * 5), group if i == 0 else None) for i in range(4)]
        siblings = [{"group": {"label": "同じ群", "kind": "決定", "project": P, "size": 8, "representative": 1,
                               "member_ids": list(range(1, 9))}, "fired_ids": [1], "listable_ids": [3, 4, 5, 6, 7, 8]}]
        glob = [{"kind": "決定", "groups": 2, "seeds": 5}]
        return dict(fired=fired, entries=entries, entry_total=4, siblings=siblings, global_counts=glob, resume=resume)

    def compose(self, max_chars=None, **overrides):
        args = self.scenario(overrides.pop("resume", None))
        args.update(overrides)
        return self.store.compose_injection(max_chars=max_chars, **args)

    def test_everything_fits_in_the_documented_order_and_shape(self):
        out = self.compose(resume={"content": "続き"})
        blocks = out["text"].split("\n\n")
        self.assertEqual(blocks[0], NARRATIVE_HEADER)
        self.assertTrue(blocks[1].startswith("【この入力に触れた記録】"))
        self.assertTrue(blocks[2].startswith("【記録の索引 (全 4 件)】"))
        self.assertEqual(blocks[3], "【全域の群 (件数のみ)】 決定 2 群・5 件")
        self.assertEqual(blocks[4], "- 再開点: 続き")
        lines = blocks[1].split("\n")
        self.assertEqual(lines[2], "  亦如是 [群「同じ群」8 件]: #3 #4 #5 #6 #7 他に 2 件")  # 並べる id は 5 件・k = 8 - 1 - 5
        self.assertIn("[群「群ラベル」9 件・代表 #30]", blocks[2])
        self.assertEqual(out["cut"], {"germinated": 0, "siblings": 0, "index": 0, "global": 0})
        self.assertEqual(out["shown_seed_ids"], [1, 2])
        self.assertEqual(_ids(out["index_seeds"]), [30, 31, 32, 33])
        self.assertEqual([g["representative"] for g in out["groups"]], [30])
        self.assertEqual(out["global"], [{"kind": "決定", "groups": 2, "seeds": 5}])
        self.assertEqual(out["sibling_lines"][0]["listed"], [3, 4, 5, 6, 7])

    def test_sibling_line_variants_for_zero_k_and_no_ids(self):
        zero = self.compose(siblings=[{"group": {"label": "L", "kind": "決定", "project": P, "size": 3, "representative": 1,
                                                  "member_ids": [1, 2, 3]}, "fired_ids": [1], "listable_ids": [2, 3]}])
        self.assertIn("\n  亦如是 [群「L」3 件]: #2 #3\n", zero["text"])      # k = 0 は「他に 0 件」を書かない
        only_more = self.compose(siblings=[{"group": {"label": "L", "kind": "決定", "project": P, "size": 3, "representative": 1,
                                                       "member_ids": [1, 2, 3]}, "fired_ids": [1], "listable_ids": []}])
        self.assertIn("\n  亦如是 [群「L」3 件]: 他に 2 件\n", only_more["text"])
        nothing = self.compose(siblings=[{"group": {"label": "L", "kind": "決定", "project": P, "size": 1, "representative": 1,
                                                     "member_ids": [1]}, "fired_ids": [1], "listable_ids": []}])
        self.assertNotIn("亦如是", nothing["text"])
        self.assertEqual(nothing["cut"]["siblings"], 0)                           # 出す物が無い行は cut にも数えない

    def test_cut_order_is_outermost_first(self):
        full = self.compose()
        text_len = len(full["text"])
        # ④ だけが落ちる
        g = self.compose(max_chars=text_len - 1)
        self.assertEqual(g["cut"], {"germinated": 0, "siblings": 0, "index": 0, "global": 1})
        self.assertEqual(g["global"], [])
        self.assertNotIn("全域の群", g["text"])
        # ④ の次は ③ の末尾 (「上位 3 件 / 全 4 件」に見出しが組み直される。③ が欠けても ④ が残りの余白に入るか
        # どうかは、円ごとに続けて試す規則 (§3) どおり余白しだいなので、④ も入らない境目で測る)
        three = self.compose(entries=self.scenario()["entries"][:3], global_counts=[])
        i = self.compose(max_chars=len(three["text"]))
        self.assertEqual(i["cut"], {"germinated": 0, "siblings": 0, "index": 1, "global": 1})
        self.assertIn("(上位 3 件 / 全 4 件。載っていない 1 件も記録に在る)", i["text"])
        self.assertEqual(_ids(i["index_seeds"]), [30, 31, 32])                  # 並びの上位が連続して残る
        no_global = self.compose(global_counts=[])
        # ③ が全部落ちるまで詰めても ① と ② (id つき) は残る
        base = self.compose(entries=[], global_counts=[])
        b = self.compose(max_chars=len(base["text"]))
        self.assertEqual((b["cut"]["index"], b["cut"]["global"]), (4, 1))
        self.assertEqual(b["sibling_lines"][0]["listed"], [3, 4, 5, 6, 7])
        # ② は id の無い形に落ちる (行は残る)
        bare = self.compose(max_chars=len(base["text"]) - 1)
        self.assertEqual(bare["cut"]["siblings"], 0)
        self.assertEqual(bare["sibling_lines"][0]["listed"], [])
        self.assertEqual(bare["sibling_lines"][0]["more"], 7)                      # 8 - 1 (描画された発芽成員)
        self.assertIn("  亦如是 [群「同じ群」8 件]: 他に 7 件", bare["text"])
        self.assertEqual(bare["cut"]["index"], 4)
        # ② の行が丸ごと落ちる (① は全部残る)
        only_fired = self.compose(entries=[], global_counts=[], siblings=[])
        gone = self.compose(max_chars=len(only_fired["text"]))
        self.assertEqual(gone["cut"], {"germinated": 0, "siblings": 1, "index": 4, "global": 1})
        self.assertEqual(gone["sibling_lines"], [])
        self.assertEqual(gone["shown_seed_ids"], [1, 2])
        # 最後に ① の末尾が落ちる (最初の ① の行は枠と一緒に残る)
        one = self.compose(max_chars=len(only_fired["text"]) - 1)
        self.assertEqual(one["cut"]["germinated"], 1)
        self.assertEqual(one["shown_seed_ids"], [1])

    def test_sibling_whose_anchor_line_was_cut_is_dropped_and_counted(self):
        args = self.scenario()
        args["siblings"][0]["fired_ids"] = [2]    # anchor = 2 番目の ①
        args["siblings"][0]["listable_ids"] = [3, 4]
        base = self.store.compose_injection(args["fired"][:1], [], None, [], [], None)
        out = self.store.compose_injection(max_chars=len(base["text"]), **args)
        self.assertEqual(out["shown_seed_ids"], [1])
        self.assertEqual(out["sibling_lines"], [])
        self.assertEqual(out["cut"]["siblings"], 1)
        self.assertEqual(out["cut"]["germinated"], 1)

    def test_resume_line_is_cut_only_when_frame_plus_resume_exceed(self):
        content = "続きの内容" * 1000
        out = self.store.compose_injection([], resume={"content": content}, max_chars=3200)
        self.assertEqual(len(out["text"]), 3200)
        m = re.fullmatch(r"- 再開点: (.*)…\(他 (\d+) 字\)", out["text"].split("\n\n", 1)[1], re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), content[: len(m.group(1))])
        self.assertEqual(len(m.group(1)) + int(m.group(2)), len(content))
        short = self.store.compose_injection([], resume={"content": "短い"}, max_chars=3200)
        self.assertEqual(short["text"], NARRATIVE_HEADER + "\n\n- 再開点: 短い")      # 収まるなら切らない
        # 再開点が枠と合わせて上限を使い切ると、その後ろの円 (①②③④) は入らない
        args = self.scenario(resume={"content": content})
        out = self.store.compose_injection(max_chars=3200, **args)
        self.assertEqual(len(out["text"]), 3200)
        self.assertEqual(out["shown_seed_ids"], [])
        self.assertEqual(out["cut"], {"germinated": 2, "siblings": 1, "index": 4, "global": 1})   # ② は anchor の ① が無く落ちる

    def test_invariant_over_many_budgets_and_inputs(self):
        for resume in (None, {"content": "短い"}, {"content": "長い" * 700}):
            args = self.scenario(resume)
            for max_chars in list(range(180, 3300, 53)) + [3200]:
                out = self.store.compose_injection(max_chars=max_chars, **args)
                self.assertLessEqual(len(out["text"]), max_chars, (max_chars, resume))
                self.assertEqual(out["cut"]["index"], len(args["entries"]) - len(out["index_seeds"]))
                self.assertEqual(out["cut"]["germinated"], 2 - len(out["shown_seed_ids"]))

    def test_default_budget_is_3200_and_empty_inputs_give_empty_text(self):
        self.assertEqual(INJECTION_MAX_CHARS, 3200)
        self.assertEqual(SIBLING_IDS_MAX, 5)
        out = self.store.compose_injection([])
        self.assertEqual(out["text"], "")
        self.assertEqual(out["cut"], {"germinated": 0, "siblings": 0, "index": 0, "global": 0})
        self.assertEqual(self.store.build_injection([]), "")

    def test_build_injection_wrapper_matches_the_old_assembly(self):
        seeds = [_seed(1, ["a"], strength=2), _seed(2, ["b"], sansho="依他起", strength=2)]
        index = [_seed(3, ["c"]), _seed(4, ["d"], sansho="依他起")]
        got = self.store.build_injection(seeds, index=index, index_total=7, resume={"content": "続き"})
        old = "\n\n".join([NARRATIVE_HEADER, self.store.build_narrative(seeds),
                           self.store.build_index_text(index, 7), "- 再開点: 続き"])
        self.assertEqual(got, old)
        self.assertEqual(self.store.build_injection(seeds, index=index), "\n\n".join(
            [NARRATIVE_HEADER, self.store.build_narrative(seeds), self.store.build_index_text(index)]))

    def test_inject_log_row_gets_cut_after_project(self):
        text = self.store.inject([_seed(1, ["a"])], 3, index=[_seed(2, ["b"])], ts="t", session_id="s", project=P)
        row = self.store.state["log"][-1]
        self.assertEqual(list(row), ["life", "ego_weight", "injected_text", "seed_ids", "index_ids", "ts", "session_id",
                                     "project", "cut"])
        self.assertEqual((row["injected_text"], row["index_ids"], row["seed_ids"]), (text, [2], [1]))
        self.assertEqual(row["cut"], {"germinated": 0, "siblings": 0, "index": 0, "global": 0})


class TestBudgetInvariantOnLargeSyntheticState(_StateCase):
    def build(self, rng):
        pool = ["語{}".format(i) for i in range(70)]
        seeds = []
        for i in range(1, 331):
            kind = rng.choice(["決定", "決定", "所在", "回避策"])
            conds = rng.sample(pool, rng.choice([2, 2, 3]))
            seeds.append(_seed(
                i, conds, kind=kind, sansho=rng.choice(["圓成實", "圓成實", "依他起"]), strength=rng.choice([1, 2, 3]),
                project=rng.choice([P, P, P, None]), content="内容{}".format(i) + "あ" * rng.randint(20, 220)))
        seeds.append(_seed(900, ["再開甲"], kind="再開点", content="再開点の内容" + "い" * 3000))
        return seeds

    def test_narrative_never_exceeds_the_budget_and_the_budget_is_exercised(self):
        rng = random.Random(20261025)
        eng = self.write(self.build(rng))
        pool = ["語{}".format(i) for i in range(70)]
        cut_runs = 0
        for n in range(40):
            query = " ".join(rng.sample(pool, rng.choice([1, 3, 12, 30])))
            for session_start in (False, True):
                x, r = self.assert_explain_equals_recall(eng, query, project=P, session_start=session_start)
                self.assertLessEqual(len(r["narrative"]), INJECTION_MAX_CHARS)
                cut = r["circles"]["cut"]
                self.assertEqual(cut["index"], len([q for q in x["index_rejected"] if q["reason"] == INDEX_REASON_BUDGET]))
                cut_runs += 1 if any(cut.values()) else 0
        self.assertGreater(cut_runs, 0)    # 容量に当たる入力が実際にあった (空振りの pin ではない)


# ---------------------------------------------------------------- inspect explain (§6)
class TestExplainCli(_StateCase):
    def test_new_sections_in_order_and_formats(self):
        self.write()
        code, out, err = self.run_cli("explain", Q_ALL, "--project", P)
        self.assertEqual(code, 0, err)
        order = ["発芽:", "同じ群 (②):", "索引:", "全域 (④・件数のみ):", "落選 (発芽):", "落選 (索引):", "容量: 上限 3200 字・注入 ", "注入される文:"]
        pos = [out.index("\n" + h) for h in order]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("\n  群「所在乙」(2 件) ← #14: #13\n", out)
        self.assertIn("\n  群「双子乙」(2 件) ← #15: 他に 1 件\n", out)
        self.assertIn("\n全域 (④・件数のみ):\n  決定: 1 群・2 件\n", out)
        self.assertRegex(out, r"\n容量: 上限 3200 字・注入 \d+ 字・落とした ① 0 / ② 0 / ③ 0 / ④ 0\n")

    def test_group_entries_in_the_index_section_are_marked(self):
        self.write()
        code, out, _ = self.run_cli("explain", "無関係", "--project", P)
        self.assertEqual(code, 0)
        self.assertIn("\n  #1 決定 圓成實: 種子1 [群「方針」3 件・代表]\n", out)
        self.assertIn("\n  #8 決定 圓成實: 種子8\n", out)     # 1 件の種子に印は付かない
        self.assertIn("\n同じ群 (②):\n  (なし)\n", out)
        self.assertIn("群「方針」の成員 (代表 #1)", out)

    def test_budget_line_and_empty_global_section(self):
        self.write()
        with mock.patch.object(manas_mod, "INJECTION_MAX_CHARS", 330):
            code, out, _ = self.run_cli("explain", "無関係", "--project", P)
        self.assertEqual(code, 0)
        m = re.search(r"\n容量: 上限 330 字・注入 (\d+) 字・落とした ① 0 / ② 0 / ③ (\d+) / ④ 1\n", out)
        self.assertIsNotNone(m, out)
        self.assertLessEqual(int(m.group(1)), 330)
        self.assertGreater(int(m.group(2)), 0)
        self.assertIn(INDEX_REASON_BUDGET, out)
        code, out, _ = self.run_cli("explain", "無関係", "--project", "")     # 全域だけ = ④ は無い
        self.assertIn("\n全域 (④・件数のみ):\n  (なし)\n", out)

    def test_json_carries_circles(self):
        self.write()
        code, out, _ = self.run_cli("--json", "explain", Q_ALL, "--project", P)
        data = json.loads(out)
        self.assertEqual(set(data["circles"]), {"siblings", "groups", "global", "cut", "chars", "max_chars"})

    def test_labels_are_escaped(self):
        seeds = [_seed(1, ["a\x1b[31m", "b"], content="ひとつ"), _seed(2, ["a\x1b[31m", "b"], content="ふたつ")]
        self.write(seeds)
        code, out, _ = self.run_cli("explain", "無関係", "--project", P)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertIn("a\\x1b[31m", out)
        code, out, _ = self.run_cli("explain", "a", "--project", P)
        self.assertNotIn("\x1b", out)


# ---------------------------------------------------------------- MCP (pin 15)
class TestMcpRecallCarriesCircles(_StateCase):
    def test_tools_literal_is_unchanged(self):
        with open(MCP_SERVER_PATH, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        segment = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets):
                segment = ast.get_source_segment(source, node.value)
        self.assertIsNotNone(segment)
        self.assertEqual(hashlib.sha256(segment.encode("utf-8")).hexdigest(), MCP_TOOLS_SHA256)

    def test_memory_recall_result_has_circles_and_is_not_an_error(self):
        try:
            import anyio  # noqa: F401
            import mcp  # noqa: F401
        except ImportError:  # pragma: no cover - mcp SDK が無い環境
            self.skipTest("公式 mcp SDK が未インストール")
        from mcp_wire import StdioServer, make_env
        self.write()
        server = StdioServer(make_env(self.state))
        try:
            server.initialize()
            called = server.call_tool(2, "memory_recall", {"text": Q_ALL, "project": P})
            self.assertFalse(called["result"]["isError"])
            body = json.loads(called["result"]["content"][0]["text"])
        finally:
            code, stderr = server.close()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(set(body), {"seeds", "narrative", "index", "resume", "circles", "alaya_warning", "manas_warning"})
        self.assertEqual(set(body["circles"]), {"siblings", "groups", "global", "cut", "chars", "max_chars"})
        self.assertEqual(body["circles"]["global"], [{"kind": "決定", "groups": 1, "seeds": 2}])
        self.assertEqual(body["circles"]["chars"], len(body["narrative"]))

    def test_handle_tool_call_returns_the_engine_result_with_circles(self):
        try:
            from raiya import mcp_server
        except ImportError:  # pragma: no cover
            self.skipTest("公式 mcp SDK が未インストール")
        eng = self.write()
        got = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "無関係", "project": P})
        self.assertIn("circles", got)
        self.assertEqual(got["circles"]["groups"][0]["label"], "方針")


MCP_TOOLS_SHA256 = "6fa900a9d356f249b89ae24c14c23c2b49101d6661fde1e17519dafc3cb39e8b"   # mcp_server.TOOLS (2026-11-06 #108 で memory_imprint の説明の fail-safe を proposal の中身に限ると書き換えた版; その前は 2026-11-04 #134 W1 で memory_imprint の説明に 利用者が原因と回避を 1 本の発話 (span) で述べた場合は 回避策 になる例外を追記した版; その前は 2026-10-29 で memory_imprint の説明に 回避策 の訂正と対の要件を追記した版; その前は #38 slice 3 で audit / introspect_material の警告の説明を raiya.db の行単位の読み飛ばしに書き換えた版; その前は #71 で memory_introspect_material の説明に警告キーと読むだけを追記した版)


class TestEgoLimitedFiredSeeds(_StateCase):
    """F1: ③ の重複除去と ② は ① が実際に描画する発芽種子だけを見る (ego_weight が ① の件数を決める)。"""

    def _low_ego_engine(self, seeds):
        return self.write(seeds, extra={"ego_weight": 0.2})

    def test_ego_limited_fired_seeds_are_not_silently_dropped(self):
        seeds = [_seed(i, ["語{}".format(i)], strength=8 - i, content="単独{}".format(i)) for i in range(1, 5)]
        seeds += [_seed(10, ["群A", "群B", "語10"], content="群の10"), _seed(11, ["群A", "群B", "語11"], content="群の11")]
        eng = self._low_ego_engine(seeds)
        q = "語1 語2 語3 語4 語10"
        x, r = self.assert_explain_equals_recall(eng, q, project=P)
        self.assertEqual(_ids(r["seeds"]), [1, 2, 3, 4, 10])        # 発芽は全件 (観測欄はこの全件に付く)
        self.assertEqual(r["narrative"].count("単独"), 4)             # ① は 1 件 + ③ に #2-#5
        self.assertIn("- 決定: 単独1(強度7)", r["narrative"])
        self.assertEqual(_ids(r["index"]), [2, 3, 4, 10])
        reasons = {q_["id"]: q_["reason"] for q_ in x["index_rejected"]}
        self.assertEqual(reasons[1], INDEX_REASON_FIRED)               # ① に出る #1 だけが「① に出る」
        for i in (2, 3, 4):
            self.assertNotIn(i, reasons)                               # ③ に載っている (「① に出る」とは言わない)
        # 発芽したが描画されなかった #10 を持つ群: ② ではなく ③ の項目 (代表 #10)
        self.assertEqual(x["circles"]["siblings"], [])
        self.assertEqual([g["representative"] for g in x["circles"]["groups"]], [10])
        self.assertIn("2 件・代表 #10]", r["narrative"])
        fire_counts = {s["id"]: s["fire_count"] for s in eng.alaya.list_seeds()}
        self.assertEqual([fire_counts[i] for i in (1, 2, 3, 4, 10, 11)], [1, 1, 1, 1, 1, 0])

    def test_group_with_a_rendered_fired_member_still_gets_sibling_line(self):
        seeds = [_seed(1, ["群A", "群B", "語1"], strength=5), _seed(2, ["群A", "群B", "語2"], strength=1)]
        eng = self._low_ego_engine(seeds)
        x = eng.explain_recall("語1 語2", project=P)
        self.assertEqual(_ids(x["seeds"]), [1, 2])
        reasons = {q_["id"]: q_["reason"] for q_ in x["index_rejected"]}
        self.assertEqual(reasons[1], INDEX_REASON_FIRED)
        self.assertEqual(reasons[2], INDEX_REASON_GROUP_FIRED.format("群A", 1))
        self.assertEqual([(s["of"], s["listed"]) for s in x["circles"]["siblings"]], [(1, [2])])


class TestStrictOutwardCut(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_strict_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = manas_store_at(os.path.join(self.tmpdir, "manas.json"))
        self.fired = [_seed(1, ["a"], strength=3, content="発芽その一" * 4)]
        self.entries = [_entry(_seed(30 + i, ["x"], content="索引の項目{}".format(i) * 8)) for i in range(3)]
        self.glob = [{"kind": "決定", "groups": 1, "seeds": 2}]

    def test_global_line_after_index_fully_cut(self):
        base = self.store.compose_injection(self.fired)
        line_len = len("\n\n" + "【全域の群 (件数のみ)】 決定 1 群・2 件")
        out = self.store.compose_injection(self.fired, self.entries, 3, [], self.glob, None,
                                           max_chars=len(base["text"]) + line_len)
        # ④ の行だけなら入る余白があるが、③ が全部落ちたので ④ も描画しない (外向きに厳密)
        self.assertEqual(out["cut"], {"germinated": 0, "siblings": 0, "index": 3, "global": 1})
        self.assertEqual((out["global"], out["index_seeds"]), ([], []))
        self.assertNotIn("全域の群", out["text"])

    def test_global_line_after_partial_index_cut(self):
        full = self.store.compose_injection(self.fired, self.entries, 3, [], self.glob, None)
        two = self.store.compose_injection(self.fired, self.entries[:2], 3, [], [], None)
        out = self.store.compose_injection(self.fired, self.entries, 3, [], self.glob, None,
                                           max_chars=len(two["text"]) + 10)
        self.assertGreater(len(full["text"]), len(two["text"]) + 10)
        self.assertEqual(out["cut"]["index"], 1)
        self.assertEqual((out["cut"]["global"], out["global"]), (1, []))

    def test_cut_germinated_drops_siblings_index_and_global(self):
        fired = [_seed(1, ["a"], strength=3, content="あ" * 30), _seed(2, ["a"], strength=2, content="い" * 30)]
        sib = [{"group": {"label": "L", "kind": "決定", "project": P, "size": 3, "representative": 1,
                          "member_ids": [1, 2, 3]}, "fired_ids": [1], "listable_ids": [3]}]
        first_only = self.store.compose_injection(fired[:1])
        out = self.store.compose_injection(fired, self.entries, 3, sib, self.glob, None,
                                           max_chars=len(first_only["text"]) + 5)
        self.assertEqual(out["shown_seed_ids"], [1])
        self.assertEqual(out["cut"], {"germinated": 1, "siblings": 1, "index": 3, "global": 1})
        self.assertEqual((out["sibling_lines"], out["index_seeds"], out["global"]), ([], [], []))

    def test_cut_siblings_drops_index_and_global(self):
        sib = [{"group": {"label": "L", "kind": "決定", "project": P, "size": 9, "representative": 1,
                          "member_ids": list(range(1, 10))}, "fired_ids": [1], "listable_ids": [2, 3, 4]}]
        base = self.store.compose_injection(self.fired)
        out = self.store.compose_injection(self.fired, self.entries, 3, sib, self.glob, None,
                                           max_chars=len(base["text"]) + 3)
        self.assertEqual(out["cut"], {"germinated": 0, "siblings": 1, "index": 3, "global": 1})

    def test_resume_is_dropped_when_it_cannot_fit_even_cut_to_nothing(self):
        header = len(NARRATIVE_HEADER)
        for resume in ({"content": "続き" * 900}, {"content": "短い"}):
            for max_chars in range(header, header + 400, 7):
                out = self.store.compose_injection(self.fired, self.entries, 3, [], self.glob, resume, max_chars=max_chars)
                self.assertLessEqual(len(out["text"]), max_chars, (max_chars, resume))
        tiny = self.store.compose_injection([], resume={"content": "続き" * 900}, max_chars=header + 3)
        self.assertNotIn("- 再開点", tiny["text"])  # 見出しの文面にも 再開点 が出る (#100)
        self.assertLessEqual(len(tiny["text"]), header + 3)


class TestGlobalGroupWithFiredMember(_StateCase):
    def test_global_group_with_fired_member_project_str(self):   # F5
        seeds = [_seed(1, ["甲", "乙"], project=None), _seed(2, ["甲", "乙", "丙"], project=None),
                 _seed(3, ["乙", "丙"], project=None)]
        eng = self.write(seeds)
        x, r = self.assert_explain_equals_recall(eng, "甲", project=P)
        self.assertEqual(_ids(r["seeds"]), [1, 2])
        self.assertEqual([(s["of"], s["listed"], s["more"]) for s in r["circles"]["siblings"]], [(1, [3], 0)])
        self.assertEqual(r["circles"]["global"], [{"kind": "決定", "groups": 1, "seeds": 3}])
        self.assertIn("  亦如是 [群「乙」3 件]: #3", r["narrative"])
        self.assertIn("【全域の群 (件数のみ)】 決定 1 群・3 件", r["narrative"])
        reasons = {q_["id"]: q_["reason"] for q_ in x["index_rejected"]}
        self.assertEqual(reasons[3], INDEX_REASON_GLOBAL_GROUP.format("乙"))



if __name__ == "__main__":
    unittest.main()
