"""群 (層化設計書 B1・plan 2026-10-25-layered-memory-implementation §1・§6・§8) の決定論 pin (スライス 1)。

`alaya.seed_groups` (純関数)、`engine.seed_provenance()["group"]`、`raiya-inspect why` の「群:」ブロック。
テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。LLM は使わない。
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import inspect_cli  # noqa: E402
from raiya.alaya import AlayaStore, _twin_shadows, seed_groups  # noqa: E402
from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.snapshot import alaya_snapshot, write_state  # noqa: E402
from tests._state_fixture import alaya_store_at, table_digest  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "layered_groups_20261025.json")
P = "/home/dev/p"
Q = "/home/dev/q"


def _seed(seed_id, conditions, kind="決定", sansho="圓成實", strength=1, project=P, superseded_by=None, withdrawn=None):
    return {
        "id": seed_id, "content": "種子{}".format(seed_id), "strength": strength, "conditions": list(conditions),
        "polarity": "無記", "sansho": sansho, "kind": kind, "project": project, "superseded_by": superseded_by,
        "withdrawn": withdrawn, "evidences": [], "history": [], "planted_at": 1, "last_fired": None,
        "fire_count": 0, "miss_streak": 0,
    }


def _load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _ids(groups):
    return [g["member_ids"] for g in groups]


def _tree_digest(path):
    out = {}
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


class TestSeedGroupsRules(unittest.TestCase):
    def test_two_shared_conditions_make_an_edge_and_one_does_not(self):
        groups = seed_groups([_seed(1, ["a", "b"]), _seed(2, ["a", "b", "c"]), _seed(3, ["a", "z"])])
        self.assertEqual(_ids(groups), [[1, 2]])

    def test_chain_is_one_group_even_when_ends_share_one_word(self):
        seeds = [_seed(1, ["a", "b", "c"]), _seed(2, ["b", "c", "d"]), _seed(3, ["c", "d", "e"])]
        groups = seed_groups(seeds)
        self.assertEqual(_ids(groups), [[1, 2, 3]])
        self.assertEqual(groups[0]["size"], 3)
        self.assertEqual(groups[0]["label"], "c")  # 3 件が持つ縁

    def test_case_and_substring_do_not_count_as_shared(self):
        self.assertEqual(seed_groups([_seed(1, ["builder", "x"]), _seed(2, ["Builder", "x"])]), [])
        self.assertEqual(seed_groups([_seed(1, ["build", "x"]), _seed(2, ["builder", "x"])]), [])

    def test_duplicate_and_non_str_conditions_do_not_inflate_sharing(self):
        seeds = [_seed(1, ["a", "a", "b"]), _seed(2, ["a", 7, None, ""])]
        self.assertEqual(seed_groups(seeds), [])
        seeds = [_seed(1, ["", ""]), _seed(2, ["", ""])]
        self.assertEqual(seed_groups(seeds), [])

    def test_kind_project_and_global_are_separate_buckets(self):
        seeds = [
            _seed(1, ["a", "b"]), _seed(2, ["a", "b"], kind="所在"), _seed(3, ["a", "b"], project=Q),
            _seed(4, ["a", "b"], project=None), _seed(5, ["a", "b"], project=None),
        ]
        groups = seed_groups(seeds)
        self.assertEqual(_ids(groups), [[4, 5]])
        self.assertIsNone(groups[0]["project"])

    def test_superseded_and_withdrawn_are_not_members(self):
        w = {"by": "human", "at": "t", "life": 1, "reason": "r", "replaced_by": None}
        seeds = [_seed(1, ["a", "b"]), _seed(2, ["a", "b"], superseded_by=1), _seed(3, ["a", "b"], withdrawn=w)]
        self.assertEqual(seed_groups(seeds), [])
        seeds.append(_seed(4, ["a", "b"]))
        self.assertEqual(_ids(seed_groups(seeds)), [[1, 4]])

    def test_singletons_are_not_returned(self):
        self.assertEqual(seed_groups([_seed(1, ["a", "b"])]), [])
        self.assertEqual(seed_groups([]), [])

    def test_class_does_not_split_a_bucket(self):
        groups = seed_groups([_seed(1, ["a", "b"], sansho="依他起"), _seed(2, ["a", "b"])])
        self.assertEqual(_ids(groups), [[2, 1]])  # 圓成實 が先

    def test_label_prefers_unique_over_most_shared(self):
        # 群 {1,2} の候補: x (3 件目の群にも出る) と y。x を持つ群は他にもあるので、不共の y を選ぶ。
        seeds = [
            _seed(1, ["x", "y", "p"]), _seed(2, ["x", "y", "q"]),
            _seed(3, ["x", "m", "n"]), _seed(4, ["x", "m", "n"]), _seed(5, ["x", "m", "n"]),
        ]
        groups = seed_groups(seeds)
        by_ids = {tuple(g["member_ids"]): g["label"] for g in groups}
        self.assertEqual(by_ids[(1, 2)], "y")
        self.assertEqual(by_ids[(3, 4, 5)], "m")  # m, n は 3 件・不共。同数なら _n の順で m

    def test_single_seed_does_not_count_as_other_group_for_uniqueness(self):
        # x は 1 件の種子 #3 にも出るが、それは「他の群」ではない → x も不共で、最多 (2 件) の x が選ばれる。
        seeds = [_seed(1, ["x", "y", "y2"]), _seed(2, ["x", "y"]), _seed(3, ["x", "z"])]
        groups = seed_groups(seeds)
        self.assertEqual(_ids(groups), [[1, 2]])
        self.assertEqual(groups[0]["label"], "x")  # x: 2 件・y: 2 件 -> 同数、_n の順 x < y

    def test_label_most_shared_when_no_unique(self):
        # 群 {1,2,3} の候補 a (3 件)・b / k1 / k2 (2 件)。どれも別の群 {4,5} {6,7} にも出る → 不共なし → 最多の a。
        seeds = [
            _seed(1, ["a", "b", "k1"]), _seed(2, ["a", "b", "k2"]), _seed(3, ["a", "k1", "k2"]),
            _seed(4, ["a", "r1", "r2"]), _seed(5, ["b", "r1", "r2"]),
            _seed(6, ["k1", "s1", "s2"]), _seed(7, ["k2", "s1", "s2"]),
        ]
        by_ids = {tuple(g["member_ids"]): g["label"] for g in seed_groups(seeds)}
        self.assertEqual(by_ids[(1, 2, 3)], "a")

    def test_label_tie_uses_normalized_order_then_raw_string(self):
        # 全角 "Ａ" の _n は "A"。生の文字列順なら "B" < "Ａ" だが、_n の順なら "Ａ" が先。
        groups = seed_groups([_seed(1, ["B", "Ａ"]), _seed(2, ["B", "Ａ"])])
        self.assertEqual(groups[0]["label"], "Ａ")
        # _n が同じなら保存されていた文字列の最小 ("X" < "Ｘ")
        groups = seed_groups([_seed(1, ["Ｘ", "X"]), _seed(2, ["X", "Ｘ"])])
        self.assertEqual(groups[0]["label"], "X")

    def test_representative_is_first_and_ties_keep_array_order(self):
        seeds = [_seed(5, ["a", "b"]), _seed(3, ["a", "b"]), _seed(9, ["a", "b"], strength=4)]
        g = seed_groups(seeds)[0]
        self.assertEqual(g["member_ids"], [9, 5, 3])
        self.assertEqual(g["representative"], 9)
        seeds = [_seed(5, ["a", "b"]), _seed(3, ["a", "b"])]
        self.assertEqual(seed_groups(seeds)[0]["member_ids"], [5, 3])

    def test_group_order_is_representative_sort_key_then_array_order(self):
        seeds = [
            _seed(1, ["a", "b"], kind="所在"), _seed(2, ["a", "b"], kind="所在"),
            _seed(3, ["c", "d"]), _seed(4, ["c", "d"]),
            _seed(5, ["e", "f"]), _seed(6, ["e", "f"]),
        ]
        self.assertEqual(_ids(seed_groups(seeds)), [[3, 4], [5, 6], [1, 2]])  # 決定 が 所在 より先、同順位は配列順

    def test_bucket_independence(self):
        base = [_seed(1, ["a", "b"]), _seed(2, ["a", "b"], kind="所在"), _seed(3, ["a", "b"]), _seed(4, ["b", "c"])]
        before = [g for g in seed_groups(base) if g["kind"] == "決定" and g["project"] == P]
        noise = [_seed(10, ["a", "b"], project=Q), _seed(11, ["a", "b"], project=Q), _seed(12, ["a", "b"], project=None)]
        after = [g for g in seed_groups(base + noise) if g["kind"] == "決定" and g["project"] == P]
        self.assertEqual(before, after)

    def test_pure_does_not_mutate_input(self):
        seeds = _load_fixture()["seeds"]
        snapshot = copy.deepcopy(seeds)
        groups = seed_groups(seeds)
        self.assertEqual(seeds, snapshot)
        groups[0]["member_ids"].append(999)  # 返り値を壊しても入力は無関係
        self.assertEqual(seeds, snapshot)
        self.assertEqual(seed_groups(seeds), seed_groups(snapshot))

    def test_empty_string_condition_twin_vs_group(self):
        # 双子は空文字の縁も数える (変えない) が、群は空文字の縁を無視する。
        seeds = [_seed(1, ["", "x"]), _seed(2, ["", "x"], sansho="依他起")]
        self.assertEqual(len(_twin_shadows(seeds)), 1)
        self.assertEqual(seed_groups(seeds), [])

    def test_seed_without_int_id_is_not_a_member(self):
        seeds = [_seed(1, ["a", "b"]), _seed(None, ["a", "b"]), _seed(True, ["a", "b"]), _seed("3", ["a", "b"]), _seed(4, ["a", "b"])]
        groups = seed_groups(seeds)
        self.assertEqual(_ids(groups), [[1, 4]])
        self.assertNotIn(None, groups[0]["member_ids"])

    def test_groups_return_keys(self):
        g = seed_groups([_seed(1, ["a", "b"]), _seed(2, ["a", "b"])])[0]
        self.assertEqual(set(g), {"label", "kind", "project", "member_ids", "representative", "size"})


class TestFixtureGroups(unittest.TestCase):
    def test_fixture_groups(self):
        groups = seed_groups(_load_fixture()["seeds"])
        got = {(g["kind"], g["project"], tuple(g["member_ids"])): g["label"] for g in groups}
        self.assertEqual(got, {
            ("決定", P, (1, 2, 3)): "方針",
            ("決定", P, (4, 5)): "共通乙",   # 不共なし -> 同数 -> _n の順
            ("決定", P, (6, 7)): "丙群乙",
            ("決定", P, (15, 16)): "双子乙",
            ("決定", None, (17, 18)): "全域乙",
            ("決定", Q, (25, 26)): "方針",
            ("回避策", P, (11, 12)): "推定乙",
            ("所在", P, (13, 14)): "所在乙",
            ("性向", P, (20, 21)): "性向乙",
        })
        members = {i for g in groups for i in g["member_ids"]}
        for excluded in (8, 9, 10, 19, 22, 23, 24):  # 1 件の種子・再開点・取り下げ中・superseded
            self.assertNotIn(excluded, members)

    def test_every_twin_shadow_is_in_the_same_group_as_its_e(self):
        seeds = _load_fixture()["seeds"]
        shadows = _twin_shadows(seeds)
        self.assertTrue(shadows)
        groups = seed_groups(seeds)
        for seed in seeds:
            e_id = shadows.get(id(seed))
            if e_id is None:
                continue
            same = [g for g in groups if seed["id"] in g["member_ids"]]
            self.assertEqual(len(same), 1, seed["id"])
            self.assertIn(e_id, same[0]["member_ids"])

    def test_twin_property_on_synthetic_seeds(self):
        seeds = [_seed(1, ["a", "b"]), _seed(2, ["a", "b", "c"], sansho="依他起"), _seed(3, ["b", "c", "a"], sansho="依他起")]
        shadows = _twin_shadows(seeds)
        self.assertEqual(len(shadows), 2)
        group = seed_groups(seeds)[0]
        for h in seeds:
            if id(h) in shadows:
                self.assertIn(h["id"], group["member_ids"])
                self.assertIn(shadows[id(h)], group["member_ids"])


class TestWithdrawSplitsGroup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_layered_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = os.path.join(self.tmpdir, "alaya.json")  # alaya_store_at はファイル名を無視する (raiya.db)
        write_state(self.tmpdir, alaya_snapshot(_load_fixture()))
        self.store = alaya_store_at(self.path, os.path.join(self.tmpdir, "imprint_log.json"))
        self.record = {"by": "human", "at": "2026-10-25T00:00:00+00:00", "life": 3, "reason": "r", "replaced_by": None}

    def _p_decisions(self):
        return {tuple(g["member_ids"]): g for g in seed_groups(self.store.state["seeds"]) if g["project"] == P and g["kind"] == "決定"}

    def test_withdraw_shrinks_then_dissolves_then_restore_brings_back(self):
        self.assertIn((1, 2, 3), self._p_decisions())
        self.store.withdraw(2, self.record)  # 鎖の真ん中を抜くと 1 と 3 は 1 語しか共有しない -> 割れる
        groups = self._p_decisions()
        self.assertNotIn((1, 2, 3), groups)
        self.assertFalse([k for k in groups if set(k) & {1, 2, 3}])
        self.store.restore(2, self.record)
        self.assertIn((1, 2, 3), self._p_decisions())
        self.store.withdraw(3, self.record)  # 端を抜くと縮む
        self.assertIn((1, 2), self._p_decisions())
        self.store.restore(3, self.record)
        self.assertIn((1, 2, 3), self._p_decisions())

    def test_withdrawing_one_of_two_dissolves_the_group(self):
        self.store.withdraw(5, self.record)
        self.assertNotIn((4, 5), self._p_decisions())
        self.store.restore(5, self.record)
        self.assertIn((4, 5), self._p_decisions())


class _StateCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_layered_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.state = os.path.join(self.tmpdir, "state")
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(self.state)
        write_state(self.state, alaya_snapshot(_load_fixture()))  # 詰めた形 = save が書く形と違う (非正準)

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


class TestProvenanceGroup(_StateCase):
    def test_live_member_gets_group_dict(self):
        prov = self.engine().seed_provenance(2)
        self.assertEqual(prov["group"], {
            "label": "方針", "kind": "決定", "project": P, "member_ids": [1, 2, 3], "representative": 1, "size": 3,
        })

    def test_global_and_other_project_groups_are_their_own(self):
        eng = self.engine()
        self.assertEqual(eng.seed_provenance(18)["group"]["member_ids"], [17, 18])
        self.assertIsNone(eng.seed_provenance(18)["group"]["project"])
        self.assertEqual(eng.seed_provenance(26)["group"]["member_ids"], [25, 26])

    def test_singleton_and_reopen_point_have_no_group(self):
        eng = self.engine()
        for seed_id in (8, 9, 10, 19, 22):
            self.assertIsNone(eng.seed_provenance(seed_id)["group"], seed_id)

    def test_superseded_and_withdrawn_seeds_have_no_group(self):
        eng = self.engine()
        self.assertIsNone(eng.seed_provenance(24)["group"])  # superseded
        self.assertIsNone(eng.seed_provenance(23)["group"])  # 取り下げ中

    def test_withdrawn_via_state_has_no_group_and_shrinks_the_other(self):
        # 取り下げ中の種子を持つ別の state を snapshot で作り (store の内部を外から書き換えない)、群から外れることを見る。
        self.assertIsNotNone(self.engine().seed_provenance(3)["group"])
        state = _load_fixture()
        for seed in state["seeds"]:
            if seed["id"] == 3:
                seed["withdrawn"] = {"by": "human", "at": "t", "life": 1, "reason": "r", "replaced_by": None}
        other = os.path.join(self.tmpdir, "state_withdrawn")
        write_state(other, alaya_snapshot(state))
        prov = MemoryEngine(state_dir=other, new_life=False).seed_provenance(3)
        self.assertIsNone(prov["group"])

    def test_provenance_duplicate_id_uses_the_returned_seed(self):
        state = _load_fixture()
        single = _seed(1, ["孤立丙", "孤立丁"])
        state["seeds"].insert(0, single)  # id 1 が重複: 1 件の種子が先、群のある種子が後
        other = os.path.join(self.tmpdir, "state_dup")
        write_state(other, alaya_snapshot(state))
        prov = MemoryEngine(state_dir=other, new_life=False).seed_provenance(1)
        self.assertEqual(prov["seed"]["conditions"], ["孤立丙", "孤立丁"])
        self.assertIsNone(prov["group"])

    def test_engine_does_not_reach_into_alaya_state(self):
        path = os.path.join(os.path.dirname(__file__), "..", "raiya", "engine.py")
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("self.alaya.state", f.read())

    def test_provenance_does_not_add_fields_to_seed(self):
        prov = self.engine().seed_provenance(1)
        self.assertNotIn("group", prov["seed"])


class TestWhyBlock(_StateCase):
    def test_why_prints_group_block_between_withdrawal_and_history(self):
        code, out, _ = self.run_cli("why", "2")
        self.assertEqual(code, 0)
        self.assertIn("群: 「方針」 kind=決定 project={} 3 件\n    成員: #1 #2 #3 (並びの先頭 #1)\n".format(P), out)
        self.assertLess(out.index("取り下げ: なし"), out.index("群: 「"))
        self.assertLess(out.index("群: 「"), out.index("履歴:"))

    def test_why_global_group_shows_global_label(self):
        _, out, _ = self.run_cli("why", "17")
        self.assertIn("群: 「全域乙」 kind=決定 project=(全域) 2 件", out)

    def test_why_no_group(self):
        for seed_id in ("8", "23", "24"):
            _, out, _ = self.run_cli("why", seed_id)
            self.assertIn("群: なし\n", out)
            self.assertNotIn("群: 「", out)

    def test_why_json_carries_group(self):
        _, out, _ = self.run_cli("--json", "why", "1")
        self.assertEqual(json.loads(out)["group"]["member_ids"], [1, 2, 3])

    def test_why_escapes_label(self):
        text = inspect_cli._group_lines({"label": "a\x1b[31mb", "kind": "決定", "project": "p\nq", "member_ids": [1, 2], "representative": 1, "size": 2})
        joined = "\n".join(text)
        self.assertNotIn("\x1b", joined)
        self.assertEqual(len(text), 2)


class TestReadVerbsLeaveGroupedStateUntouched(_StateCase):
    def test_four_inspect_verbs_and_why_write_nothing(self):
        eng = self.engine()
        rec = eng.record_turn("方針の話", "了解", session_id="s1", project=P)
        turn = LedgerTurn(user_text="方針の話 p/", assistant_text="了解", id=rec["ledger_id"], project=P)
        proposal = {"content": "方針の話", "conditions": ["方針の話"], "polarity": "無記", "kind": "所在",
                    "evidence": [{"source": "user", "span": "方針の話 p/"}]}
        planted = eng.imprint([turn], [proposal])[0]["seed"]["id"]
        eng.recall("方針 検証", project=P, session_id="s1")
        before = _tree_digest(self.state)
        before_tables = table_digest(self.state)
        self.assertIn("raiya.db", before)
        self.assertTrue(eng.seed_provenance(planted)["decisions"])  # imprint_log に行がある
        eng.inspect_seeds()
        eng.injection_log(5)
        for seed_id in (1, 2, 8, 13, 23, 24, planted):
            eng.seed_provenance(seed_id)
        eng.explain_recall("方針 検証", project=P)
        for argv in (["show", "--all"], ["injected"], ["why", "1"], ["why", "24"], ["--json", "why", "2"], ["why", str(planted)],
                     ["explain", "方針 検証", "--project", P]):
            self.run_cli(*argv)
        after = _tree_digest(self.state)
        self.assertEqual({k: v for k, v in after.items() if k != ".lock"}, {k: v for k, v in before.items() if k != ".lock"})
        self.assertEqual(table_digest(self.state), before_tables)
        self.assertNotIn("groups_cache.json", after)


if __name__ == "__main__":
    unittest.main()
