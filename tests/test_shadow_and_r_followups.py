"""QA 追補: 縁の共有フィルタ (#104) が 取り下げ照合 / 双子照合 を隠さないこと、D が R に勝つこと、
R の適用範囲、`_injected_by_turn` の読み方。LLM は使わない。テストが消すのは自作 tmp だけ。
"""
import copy
import datetime
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import engine as engine_mod  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402
from tests.test_withdrawal import _record, _seed, _write_state  # noqa: E402

USER_EV = [{"source": "user", "span": "設定は config.yaml に置く"}]


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=None)


def _prop(content, conditions, spans, kind="所在", **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    p.update(extra)
    return p


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def store(self, seeds=None):
        state = os.path.join(self.tmpdir, "state")
        os.makedirs(state, exist_ok=True)
        if seeds is not None:
            _write_state(state, seeds, life=1)
        return alaya_store_at(os.path.join(state, "alaya.json"), os.path.join(state, "imprint_log.json"))


class TestShadowsIgnoreSharedConditionFilter(_Case):
    def test_withdrawn_with_disjoint_conditions_is_still_matched(self):
        w = _seed(1, ["配置"], content="設定ファイルは config.yaml に置く", project=None)
        w["withdrawn"] = _record()
        store = self.store([w])
        before = copy.deepcopy(store.get_by_ids([1])[0])
        win = [_turn("設定ファイルは config.yaml に置く p/ 場所")]
        r = store.imprint(win, _prop("設定ファイルは config.yaml に置く", ["場所"],
                                     [("user", "設定ファイルは config.yaml に置く p/ 場所")]))
        self.assertEqual(r["decision"], "取り下げ照合")
        self.assertEqual(r["withdrawn_match"], 1)
        self.assertIsNone(r["seed"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(store.get_by_ids([1])[0], before)

    def test_withdrawn_sharing_only_one_of_three_words_is_still_matched(self):
        """2 語規則 (x9_3s) は門番に適用しない: W と 1 語しか共有しなくても 取り下げ照合。"""
        w = _seed(1, ["配置", "余分", "他"], content="設定ファイルは config.yaml に置く", project=None)
        w["withdrawn"] = _record()
        store = self.store([w])
        win = [_turn("設定ファイルは config.yaml に置く p/ 配置 甲 乙")]
        r = store.imprint(win, _prop("設定ファイルは config.yaml に置く", ["配置", "甲", "乙"],
                                     [("user", "設定ファイルは config.yaml に置く p/ 配置")]))
        self.assertEqual(r["decision"], "取り下げ照合")
        self.assertEqual(r["withdrawn_match"], 1)
        self.assertEqual(len(store.list_seeds()), 1)

    def test_twin_hidden_h_with_disjoint_conditions_is_still_matched(self):
        e = _seed(1, ["猫", "犬"], content="猫が好き", project=None)
        h = _seed(2, ["猫", "犬"], content="猫が好き", project=None, sansho_class="依他起", strength=2)
        store = self.store([e, h])
        before_h = copy.deepcopy(store.get_by_ids([2])[0])
        win = [_turn("", "猫が好き 鳥")]
        r = store.imprint(win, _prop("猫が好き", ["鳥"], [("assistant", "猫が好き")]))
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(r["twin_match"], 2)
        self.assertEqual(store.get_by_ids([2])[0], before_h)

    def test_non_sharing_best_any_does_not_block_merge_into_sharing_seed(self):
        a = _seed(1, ["鳥"], content="猫が好き", project=None, sansho_class="依他起", strength=2)
        b = _seed(2, ["猫", "犬"], content="猫が好きです", project=None, sansho_class="依他起", strength=2)
        store = self.store([a, b])
        win = [_turn("", "猫が好き 猫")]
        r = store.imprint(win, _prop("猫が好き", ["猫"], [("assistant", "猫が好き")]))
        self.assertTrue(r.get("reinforced"), r)
        self.assertEqual(r["seed"]["id"], 2)
        self.assertEqual(store.get_by_ids([1])[0]["strength"], 2)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 3)


class TestCandidatePredicate(_Case):
    X = "設定ファイルは config.yaml に置く"

    def _w(self):
        w = _seed(1, ["配置", "余分", "他"], content="設定ファイルは config.yml に置いておく", project=None)
        w["withdrawn"] = _record()
        return w

    def _imprint(self, store, conds):
        text = self.X + " p/ " + " ".join(conds)  # #149: 所在 の形は 區切りの語 (`/` を含む語)
        win = [_turn(text)]
        return store.imprint(win, _prop(self.X, conds, [("user", text)]))

    def test_non_sharing_live_higher_sim_does_not_mask_withdrawn(self):
        live = _seed(2, ["場所"], content=self.X, project=None)
        store = self.store([self._w(), live])
        before = store.get_by_ids([2])[0]["strength"]
        r = self._imprint(store, ["配置"])
        self.assertEqual(r["decision"], "取り下げ照合", r)
        self.assertEqual(r["withdrawn_match"], 1)
        self.assertEqual(len(store.list_seeds()), 2)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], before)

    def test_sharing_live_scoring_higher_than_withdrawn_is_merged(self):
        live = _seed(2, ["配置"], content=self.X, project=None)
        store = self.store([self._w(), live])
        r = self._imprint(store, ["配置"])
        self.assertTrue(r.get("reinforced"), r)
        self.assertEqual(r["seed"]["id"], 2)
        self.assertNotIn("withdrawn_match", r)

    def test_same_class_d_dissimilar_target(self):
        s = _seed(1, ["配置"], content="全く別の内容 abc", project=None, sansho_class="依他起", strength=2,
                  evidences=[{"source": "user", "span": "config.yaml"}, {"source": "assistant", "span": "別"}])
        store = self.store([s])
        win = [_turn("質問 鳥", self.X + " 鳥")]
        r = store.imprint(win, _prop(self.X, ["鳥"], [("assistant", self.X)], action="reinforce", target_id=1))
        self.assertEqual(r.get("user_span_match"), 1, r)
        self.assertIsNone(r["seed"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(store.get_by_ids([1])[0]["strength"], 2)


class TestRPrecedenceAndScope(_Case):
    SPAN = "設定は config.yaml に置く"

    def _seed_mixed(self, **kw):
        s = _seed(1, ["配置"], content=self.SPAN, project=None, sansho_class="依他起", strength=2,
                  evidences=[{"source": "user", "span": "config.yaml"}, {"source": "assistant", "span": self.SPAN}])
        s.update(kw)
        return s

    def _echo(self, store, injected, **extra):
        win = [_turn("質問 配置", self.SPAN + " 配置")]
        return store.imprint(win, _prop(self.SPAN, ["配置"], [("assistant", self.SPAN)], **extra),
                             injected_by_turn=injected)

    def test_d_wins_over_r(self):
        store = self.store([self._seed_mixed()])
        for extra in ({}, {"action": "reinforce", "target_id": 1}):
            r = self._echo(store, {0: {1}}, **extra)
            self.assertEqual(r.get("user_span_match"), 1, extra)
            self.assertNotIn("echo_match", r)
        self.assertEqual(store.get_by_ids([1])[0]["strength"], 2)

    def test_injected_withdrawn_is_still_withdrawn_match(self):
        s = _seed(1, ["配置"], content=self.SPAN, project=None, sansho_class="依他起", strength=2)
        s["withdrawn"] = _record()
        store = self.store([s])
        r = self._echo(store, {0: {1}})
        self.assertEqual(r["decision"], "取り下げ照合")
        self.assertNotIn("echo_match", r)

    def test_injected_twin_h_is_still_twin_match(self):
        e = _seed(1, ["配置", "場所"], content=self.SPAN, project=None)
        h = _seed(2, ["配置", "場所"], content=self.SPAN, project=None, sansho_class="依他起", strength=2)
        store = self.store([e, h])
        win = [_turn("質問", self.SPAN + " 配置 場所")]
        r = store.imprint(win, _prop(self.SPAN, ["配置", "場所"], [("assistant", self.SPAN)]),
                          injected_by_turn={0: {1, 2}})
        self.assertEqual(r["decision"], "双子照合")
        self.assertNotIn("echo_match", r)

    def test_r_needs_every_span_turn_injected(self):
        s = _seed(1, ["配置"], content=self.SPAN, project=None, sansho_class="依他起", strength=2)
        win = [_turn("q0", "設定は config.yaml に 配置", 0), _turn("q1", "置く で決まり 配置", 1)]
        spans = [("assistant", "設定は config.yaml に"), ("assistant", "置く で決まり")]
        store = self.store([s])
        r = store.imprint(win, _prop(self.SPAN, ["配置"], spans), injected_by_turn={0: {1}})
        self.assertEqual([sp["turn_index"] for sp in r["spans"]], [0, 1])
        self.assertNotIn("echo_match", r)
        self.assertTrue(r.get("reinforced"), r)
        r2 = store.imprint(win, _prop(self.SPAN, ["配置"], spans), injected_by_turn={0: {1}, 1: {1}})
        self.assertEqual(r2.get("echo_match"), 1, r2)

    def test_r_off_for_resume_point(self):
        s = _seed(1, ["配置"], kind="再開点", content=self.SPAN, project=None, sansho_class="依他起", strength=2)
        store = self.store([s])
        r = self._echo(store, {0: {1}}, kind="再開点", action="reinforce", target_id=1)
        self.assertNotIn("echo_match", r)

    def test_r_off_for_replacement_for(self):
        s = _seed(1, ["配置"], content=self.SPAN, project=None, sansho_class="依他起", strength=2)
        store = self.store([s])
        win = [_turn("質問 配置", self.SPAN + " 配置")]
        r = store.imprint(win, _prop(self.SPAN, ["配置"], [("assistant", self.SPAN)]),
                          replacement_for=1, injected_by_turn={0: {1}})
        self.assertNotIn("echo_match", r)


class TestInjectedByTurnReading(unittest.TestCase):
    T = datetime.datetime(2026, 1, 1, 12, 0, 10, tzinfo=datetime.timezone.utc).timestamp()

    def _log(self, ts):
        return [{"session_id": "s", "ts": ts, "seed_ids": [1], "index_ids": []}]

    def test_second_resolution_cut(self):
        log = self._log("2026-01-01T12:00:10+00:00")
        self.assertEqual(engine_mod._injected_by_turn(log, [("s", self.T + 0.7)]), {0: {1}})
        self.assertEqual(engine_mod._injected_by_turn(log, [("s", self.T - 0.1)]), {})

    def test_naive_iso_is_read_as_utc(self):
        log = self._log("2026-01-01T12:00:10")
        self.assertEqual(engine_mod._injected_by_turn(log, [("s", self.T)]), {0: {1}})

    def test_bool_or_missing_ts_line_is_skipped(self):
        log = self._log(True) + [{"session_id": "s", "seed_ids": [1], "index_ids": []}]
        self.assertEqual(engine_mod._injected_by_turn(log, [("s", self.T + 5)]), {})


if __name__ == "__main__":
    unittest.main()
