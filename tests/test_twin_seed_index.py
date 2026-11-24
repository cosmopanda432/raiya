"""Issue #14 原因 1: 圓成實の双子の表示時の除外 (注入経路のみ・状態不変)。
spec: docs/superpowers/specs/2026-10-23-twin-seed-index-design.md §2 / §6.2。LLM は使わない。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.alaya import GERMINATION_REASON_TWIN, INDEX_REASON_TWIN  # noqa: E402
from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from tests.test_inspect import P, Q, _ids, _seed, _tree_digest, _write_state  # noqa: E402

E, H = "圓成實", "依他起"


class _TwinCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_twin_")
        self.state = os.path.join(self.tmpdir, "state")
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def engine(self, seeds):
        _write_state(self.state, seeds)
        return MemoryEngine(state_dir=self.state, new_life=False)

    def explain(self, seeds, text="無関係", project=P):
        return self.engine(seeds).explain_recall(text, project=project)

    @staticmethod
    def reasons(result):
        return {r["id"]: r["reason"] for r in result["index_rejected"]}


class TestTwinIndex(_TwinCase):
    def _pair(self, e_id, h_id, **kw):
        e_kw = {"kind": "回避策", "sansho": E}
        e_kw.update(kw.get("e", {}))
        h_kw = {"kind": "回避策", "sansho": H}
        h_kw.update(kw.get("h", {}))
        return [
            _seed(e_id, kw.get("e_conds", ["CI", "mypy"]), **e_kw),
            _seed(h_id, kw.get("h_conds", ["mypy", "CI", "extra"]), **h_kw),
        ]

    def test_twin_hidden_when_enjojitsu_planted_first(self):
        r = self.explain(self._pair(1, 2))
        self.assertEqual(_ids(r["index"]), [1])
        self.assertEqual(self.reasons(r)[2], INDEX_REASON_TWIN.format(1))

    def test_twin_hidden_when_etaki_planted_first(self):
        r = self.explain(list(reversed(self._pair(2, 1))))  # 依他起 (id 1) が先、圓成實 (id 2) が後
        self.assertEqual(_ids(r["index"]), [2])
        self.assertEqual(self.reasons(r)[1], INDEX_REASON_TWIN.format(2))

    def test_twin_filter_writes_nothing(self):
        seeds = self._pair(1, 2)
        eng = self.engine(seeds)
        before = _tree_digest(self.state)
        eng.explain_recall("mypy CI", project=P)
        self.assertEqual(_tree_digest(self.state), before)
        eng.recall("mypy CI", project=P)
        for s in eng.inspect_seeds(project=P):
            self.assertIsNone(s["superseded_by"])
            self.assertEqual(s["history"], [])

    def test_one_shared_condition_is_not_twin(self):
        r = self.explain(self._pair(1, 2, h_conds=["CI", "other"]))
        self.assertEqual(_ids(r["index"]), [1, 2])

    def test_duplicate_condition_on_enjojitsu_counts_once(self):
        r = self.explain(self._pair(1, 2, e_conds=["猫", "猫"], h_conds=["猫"]))
        self.assertEqual(_ids(r["index"]), [1, 2])

    def test_duplicate_condition_on_etaki_counts_once(self):
        r = self.explain(self._pair(1, 2, e_conds=["猫", "犬"], h_conds=["猫", "猫"]))
        self.assertEqual(_ids(r["index"]), [1, 2])

    def test_non_str_conditions_ignored(self):
        # 発芽 (既存の挙動) は非 str の縁で落ちるため、索引の判定を直接見る
        eng = self.engine(self._pair(1, 2, e_conds=[1, 2, "CI"], h_conds=[1, 2, "x"]))
        self.assertEqual(_ids(eng.alaya.index(project=P, purpose="inject")), [1, 2])

    def test_twin_hidden_even_when_only_etaki_condition_matches(self):
        seeds = self._pair(1, 2, e_conds=["CI", "mypy"], h_conds=["CI", "mypy", "沼"], h={"strength": 2})
        r = self.explain(seeds, text="沼")
        self.assertEqual(r["germinated"], [])
        reasons = {x["id"]: x["reason"] for x in r["germination_rejected"]}
        self.assertEqual(reasons[2], GERMINATION_REASON_TWIN.format(1))
        self.assertNotIn(1, [g["id"] for g in r["germinated"]])

    def test_shared_conditions_different_kind_is_not_twin(self):
        r = self.explain(self._pair(1, 2, h={"kind": "所在"}))
        self.assertEqual(_ids(r["index"]), [1, 2])

    def test_different_project_is_not_twin(self):
        seeds = self._pair(1, 2, h={"project": Q})
        eng = self.engine(seeds)
        self.assertEqual(_ids(eng.explain_recall("x", project=P)["index"]), [1])
        self.assertEqual(_ids(eng.explain_recall("x", project=Q)["index"]), [2])

    def test_global_and_project_are_not_twins(self):
        eng = self.engine(self._pair(1, 2, e={"project": None}))
        self.assertEqual(_ids(eng.explain_recall("x", project=P)["index"]), [1, 2])

    def test_global_twins_are_twins(self):
        r = self.explain(self._pair(1, 2, e={"project": None}, h={"project": None}), project=None)
        self.assertEqual(_ids(r["index"]), [1])

    def test_superseded_enjojitsu_does_not_hide(self):
        r = self.explain(self._pair(1, 2, e={"superseded_by": 9}))
        self.assertEqual(_ids(r["index"]), [2])

    def test_unclassified_neither_hides_nor_is_hidden(self):
        unclassified = _seed(1, ["CI", "mypy"], kind="回避策")
        del unclassified["sansho"]
        etaki = _seed(2, ["CI", "mypy"], kind="回避策", sansho=H)
        r = self.explain([unclassified, etaki])
        self.assertEqual(_ids(r["index"]), [2])  # 未分類は隠さない (自身は class で落ちる)
        self.assertNotIn("双子", self.reasons(r)[1])
        # 未分類の依他起側は隠れない: 発芽 (縁 1 語・強度 2) で理由が双子にならない
        e = _seed(3, ["CI", "mypy"], kind="回避策")
        h = _seed(4, ["CI", "mypy"], kind="回避策", sansho=H, strength=2)
        del h["sansho"]
        g = self.engine([e, h]).explain_recall("CI", project=P)
        self.assertTrue(all("双子" not in x["reason"] for x in g["germination_rejected"]))
        # kind / project 欄が無い種子は 性向 / 全域として判定される
        e_old = _seed(5, ["猫", "犬"], kind="性向")
        h_old = _seed(6, ["猫", "犬"], kind="性向", sansho=H, strength=2)
        for s in (e_old, h_old):
            del s["kind"], s["project"]
        os.environ[INJECT_DISPOSITIONS_ENV] = "1"
        g = self.engine([e_old, h_old]).explain_recall("猫 犬", project=None)
        self.assertEqual({x["id"]: x["reason"] for x in g["germination_rejected"]}.get(6), GERMINATION_REASON_TWIN.format(5))

    def test_substring_condition_is_not_shared(self):
        r = self.explain(self._pair(1, 2, e_conds=["矛", "CI"], h_conds=["沼矛", "CI"]))
        self.assertEqual(_ids(r["index"]), [1, 2])

    def test_shadow_id_is_lowest(self):
        seeds = [
            _seed(7, ["CI", "mypy"], kind="回避策", sansho=E),
            _seed(3, ["CI", "mypy"], kind="回避策", sansho=E),
            _seed(9, ["CI", "mypy"], kind="回避策", sansho=H),
        ]
        r = self.explain(seeds)
        self.assertEqual(self.reasons(r)[9], INDEX_REASON_TWIN.format(3))

    def test_consolidate_index_is_unfiltered(self):
        eng = self.engine(self._pair(1, 2))
        ids = sorted(s["id"] for s in eng.alaya.index(project=P, purpose="consolidate"))
        self.assertEqual(ids, [1, 2])

    def test_seed_count_excludes_twin(self):
        eng = self.engine(self._pair(1, 2) + [_seed(3, ["z"], kind="回避策")])
        self.assertEqual(eng.alaya.seed_count(project=P, purpose="inject"), 2)
        self.assertEqual(eng.alaya.seed_count(project=P, purpose="consolidate"), 3)


class TestTwinGermination(_TwinCase):
    def test_twin_does_not_germinate(self):
        seeds = [
            _seed(1, ["CI", "mypy"], kind="回避策", sansho=E),
            _seed(2, ["CI", "mypy"], kind="回避策", sansho=H, strength=2),
            _seed(3, ["猫"], kind="回避策", sansho=H, strength=2),  # 双子でない依他起
        ]
        r = self.explain(seeds, text="CI と 猫")
        self.assertEqual(sorted(g["id"] for g in r["germinated"]), [1, 3])
        reasons = {x["id"]: x["reason"] for x in r["germination_rejected"]}
        self.assertEqual(reasons[2], GERMINATION_REASON_TWIN.format(1))


class TestTwinExplainEqualsRecall(_TwinCase):
    def test_explain_matches_recall_with_twins(self):
        seeds = [
            _seed(1, ["CI", "mypy"], kind="回避策", sansho=E),
            _seed(2, ["CI", "mypy"], kind="回避策", sansho=H, strength=2),
            _seed(3, ["猫"], kind="所在", sansho=H, strength=2),
            _seed(4, ["猫"], kind="所在", sansho=E),
        ]
        eng = self.engine(seeds)
        ex = eng.explain_recall("CI 猫", project=P)
        rc = eng.recall("CI 猫", project=P)
        self.assertEqual(_ids(ex["seeds"]), _ids(rc["seeds"]))
        self.assertEqual(_ids(ex["index"]), _ids(rc["index"]))
        self.assertEqual(ex["narrative"], rc["narrative"])
        self.assertNotIn(2, _ids(rc["seeds"]))


if __name__ == "__main__":
    unittest.main()
