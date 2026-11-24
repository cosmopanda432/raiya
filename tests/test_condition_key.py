"""#147 (a): 縁の共有判定の鍵 = sansho._n + casefold + 末尾 `/` を 1 つ剥がす (保存は逐語・部分一致なし)。
テストが消すのは自作 tmp だけ。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"


def _seed(*conds):
    return {"conditions": list(conds)}


class TestConditionKey(unittest.TestCase):
    def both(self, seed_conds, prop_conds):
        a = alaya._shares_condition(_seed(*seed_conds), list(prop_conds))
        b = alaya._shares_conditions_strict(_seed(*seed_conds), list(prop_conds))
        return a, b

    def test_trailing_slash_shared(self):
        self.assertEqual(self.both(["tests/"], ["tests"]), (True, True))
        self.assertEqual(self.both(["tests"], ["tests/"]), (True, True))

    def test_casefold_and_slash(self):
        self.assertEqual(self.both(["Pi"], ["pi/"]), (True, True))

    def test_distinct_names_stay_distinct(self):
        for x, y in (("L", "L'"), ("test", "tests"), ("DB", "DBus")):
            self.assertEqual(self.both([x], [y]), (False, False), (x, y))

    def test_fullwidth_slash_key(self):
        self.assertTrue(alaya._shares_condition({"conditions": ["tests／"]}, ["tests"]))

    def test_only_one_slash_stripped(self):
        self.assertEqual(self.both(["a//"], ["a"]), (False, False))

    def test_slash_only_condition(self):
        self.assertEqual(self.both(["/"], ["x"]), (False, False))
        self.assertEqual(self.both(["/"], ["/"]), (True, True))

    def test_stored_values_untouched(self):
        s = _seed("tests/")
        props = ["tests"]
        alaya._shares_condition(s, props)
        self.assertEqual(s["conditions"], ["tests/"])
        self.assertEqual(props, ["tests"])

    def test_empty_side_semantics_unchanged(self):
        self.assertTrue(alaya._shares_condition(_seed(), ["x"]))
        self.assertFalse(alaya._shares_conditions_strict(_seed(), ["x"]))


class TestEc2WithSlashVariant(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()
        u = "テストは tests/ に置く。"
        r = self.store.imprint(
            [LedgerTurn(user_text=u, assistant_text="", id=0, project=PROJ)],
            {"content": "テストは tests/ に置く", "conditions": ["tests/"], "polarity": "無記", "kind": "所在",
             "evidence": [{"source": "user", "span": u}]})
        self.assertEqual(r["seed"]["sansho"], "圓成實")
        self.sid = r["seed"]["id"]

    def _propose(self, conds):
        u = "テストの置き場所は tests/ だっけ？"
        a = "テストは tests/ に置く"
        w = [LedgerTurn(user_text=u, assistant_text=a, id=1, project=PROJ)]
        p = {"content": "テストは tests/ に置く", "conditions": list(conds), "polarity": "無記", "kind": "所在",
             "evidence": [{"source": "user", "span": u}, {"source": "assistant", "span": a}]}
        return self.store.imprint(w, p, injected_by_turn={0: {self.sid}})

    def test_slash_variant_is_recorded_only(self):
        r = self._propose(["tests"])
        self.assertIsNone(r["seed"])
        self.assertEqual(r["echo_match"], self.sid)
        self.assertIn("EC2'", r["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.store.list_seeds()[0]["conditions"], ["tests/"])

    def test_unrelated_condition_still_not_echo(self):
        r = self._propose(["置き場所"])
        self.assertIsNotNone(r["seed"])
        self.assertNotIn("EC2'", r["reason"])
        self.assertIsNone(r.get("echo_match"))


class TestMergeWithSlashVariant(unittest.TestCase):
    U0 = "テストは tests/ と fixtures に置く。"
    CONTENT = "テストは tests/ と fixtures に置く"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()
        r = self.store.imprint(
            [LedgerTurn(user_text=self.U0, assistant_text="", id=0, project=PROJ)],
            self._prop(["tests/", "fixtures"], self.U0))
        self.seed = r["seed"]
        self.assertEqual(self.seed["sansho"], "圓成實")
        self.sid = self.seed["id"]

    def _prop(self, conds, span, **extra):
        p = {"content": self.CONTENT, "conditions": list(conds), "polarity": "無記", "kind": "所在",
             "evidence": [{"source": "user", "span": span}]}
        p.update(extra)
        return p

    def _second(self, **extra):
        u = "もう一度言う。テストは tests/ と fixtures に置く。"
        w = [LedgerTurn(user_text=u, assistant_text="", id=1, project=PROJ)]
        return self.store.imprint(w, self._prop(["tests", "fixtures"], u, **extra))

    def test_merge_counts_slash_variant_as_shared(self):
        r = self._second()
        self.assertEqual(len(self.store.list_seeds()), 1, r)
        seed = self.store.list_seeds()[0]
        self.assertEqual(seed["id"], self.sid)
        self.assertGreater(seed["strength"], self.seed["strength"])
        self.assertIn("tests/", seed["conditions"])

    def test_explicit_reinforce_counts_slash_variant(self):
        r = self._second(action="reinforce", target_id=self.sid)
        self.assertNotIn("reinforce 不可", r["reason"])
        self.assertNotIn("new として扱う", r["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1, r)
        self.assertGreater(self.store.list_seeds()[0]["strength"], self.seed["strength"])
