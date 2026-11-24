import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.alaya import AlayaStore, _merge_similarity, seed_conditions  # noqa: E402
from raiya.cli import build_seed_text  # noqa: E402
from raiya.engine import MemoryEngine, _build_consolidate_prompt  # noqa: E402
from raiya.manas import INITIAL_EGO_WEIGHT, ManasStore  # noqa: E402
from raiya.sansho import Turn  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import alaya_store_at, mutate, table_bodies  # noqa: E402


def _read_state(path):
    """旧 alaya.json の代わり: `path` のある dir の DB の seeds 表を `{"seeds": [...]}` で返す。"""
    return {"seeds": [json.loads(b) for b in table_bodies(os.path.dirname(path), "seeds", order="pos")]}


def _write_state(path, data):
    """`_read_state` で読んで直接書き換えた seeds (と任意の `next_id`) を seeds 表へ書き戻す。足した種子は INSERT する
    (トリガは `mutate` が一時的に外す。自作 tmp の DB にだけ使う)。"""
    from raiya import storage

    def go(conn):
        rows = conn.execute("SELECT pos FROM seeds ORDER BY pos").fetchall()
        for i, seed in enumerate(data["seeds"]):
            d = storage.seed_derived(seed)
            if i < len(rows):
                conn.execute("UPDATE seeds SET id=?, kind=?, project=?, live=?, body=? WHERE pos=?",
                             (d[0], d[1], d[2], d[3], storage._dumps(seed), rows[i][0]))
            else:
                conn.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (?, ?, ?, ?, ?)",
                             (d[0], d[1], d[2], d[3], storage._dumps(seed)))
        if "next_id" in data:
            conn.execute("UPDATE meta SET value=? WHERE key='alaya.next_id'", (json.dumps(data["next_id"]),))

    mutate(os.path.dirname(path), go)


class TestAlayaSeedElementTypes(unittest.TestCase):
    """seeds 表に dict でない本文・strength の無い dict が混ざっていた場合 (旧 JSON では「破損」で全体が空に倒れた)。
    sqlite 版は行単位: その行だけ読み飛ばして数え (alaya.warning)、DB の行は書き直さず、残りの種子と life はそのまま読む。
    begin_new_life (life>0) と MemoryEngine.recall は落ちない。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.alaya_path = os.path.join(self.tmpdir, "alaya.json")  # alaya_store_at はファイル名を無視する (raiya.db)
        self.log_path = os.path.join(self.tmpdir, "imprint_log.json")

    def _write(self, bodies, life=1):
        alaya_store_at(self.alaya_path, self.log_path)  # 新規 DB を作る

        def go(conn):
            for body in bodies:
                conn.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, ?)", (body,))
            conn.execute("UPDATE meta SET value=? WHERE key='alaya.life'", (json.dumps(life),))

        mutate(self.tmpdir, go)

    def _assert_skipped(self, store, bodies):
        self.assertIsNotNone(store.warning)
        self.assertIn("{} 件".format(len(bodies)), store.warning)
        self.assertEqual(store.list_seeds(), [])
        self.assertEqual(store.current_life, 1)  # life は meta から読む: 壊れた種子の行のために空へは倒れない
        self.assertEqual(store.begin_new_life(), 2)
        self.assertEqual(store.germinate("x"), [])
        self.assertEqual(table_bodies(self.tmpdir, "seeds", order="pos"), bodies)  # 読み飛ばすだけで、行は書き直さない

    def test_seeds_containing_string_is_skipped_row_by_row(self):
        bodies = ['"not-a-dict"']
        self._write(bodies)
        self._assert_skipped(alaya_store_at(self.alaya_path, self.log_path), bodies)

    def test_seeds_containing_number_is_skipped_row_by_row(self):
        bodies = ["1"]
        self._write(bodies)
        self._assert_skipped(alaya_store_at(self.alaya_path, self.log_path), bodies)

    def test_seeds_containing_null_is_skipped_row_by_row(self):
        bodies = ["null"]
        self._write(bodies)
        self._assert_skipped(alaya_store_at(self.alaya_path, self.log_path), bodies)

    def test_seed_dict_missing_strength_is_skipped_row_by_row(self):
        bodies = ['{"id":1,"content":"x"}']
        self._write(bodies)
        self._assert_skipped(alaya_store_at(self.alaya_path, self.log_path), bodies)

    def test_unparseable_body_is_skipped_and_never_rewritten(self):
        self._write(["{壊れた"])
        store = alaya_store_at(self.alaya_path, self.log_path)
        store.begin_new_life()
        store.commit()  # 書く区間でも、読めない行は触らない
        again = alaya_store_at(self.alaya_path, self.log_path)
        self.assertIsNotNone(again.warning)
        self.assertEqual(table_bodies(self.tmpdir, "seeds", order="pos"), ["{壊れた"])

    def test_memory_engine_non_dict_seed_does_not_crash(self):
        state_dir = os.path.join(self.tmpdir, "eng")
        os.makedirs(state_dir, exist_ok=True)
        write_state(state_dir, {"seeds": ["not-a-dict"], "alaya_meta": {"next_id": 1, "life": 1}})
        engine = MemoryEngine(state_dir=state_dir)
        self.assertIsInstance(engine.life, int)
        recalled = engine.recall("x")
        self.assertEqual(recalled["seeds"], [])
        self.assertIn("1 件", recalled["alaya_warning"])


class TestNonStrSeedConditions(unittest.TestCase):
    """Issue #25: a seed whose conditions (縁) contain a truthy non-str (hand-edited /
    corrupt alaya.json) crashed germination (sansho._n -> TypeError), _merge_similarity
    (AttributeError) and the consolidate / CLI displays (str.join). The non-str items
    are skipped wherever conditions are read; nothing rewrites the stored row (E8).
    """

    PROPOSAL = {
        "kind": "所在",
        "content": "設定は config.yaml にある p/",
        "conditions": ["config.yaml"],
        "evidence": {"source": "user", "span": "設定は config.yaml にある p/"},
    }

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.turn = Turn(user_text="設定は config.yaml にある p/", assistant_text="ok")
        self.engine.imprint([self.turn], [dict(self.PROPOSAL)])
        self.alaya_path = os.path.join(self.tmpdir, "alaya.json")
        data = _read_state(self.alaya_path)
        data["seeds"][0]["conditions"] = [42, ["x"], "config.yaml"]
        _write_state(self.alaya_path, data)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _stored_conditions(self):
        return _read_state(self.alaya_path)["seeds"][0]["conditions"]

    def test_recall_germinates_on_the_str_condition(self):
        recalled = self.engine.recall("config.yaml はどこ", project="")
        self.assertEqual([s["content"] for s in recalled["seeds"]], ["設定は config.yaml にある p/"])
        self.assertEqual(self._stored_conditions(), [42, ["x"], "config.yaml"])

    def test_only_non_str_conditions_do_not_germinate(self):
        data = _read_state(self.alaya_path)
        data["seeds"][0]["conditions"] = [42, ["config.yaml"]]
        _write_state(self.alaya_path, data)
        self.assertEqual(self.engine.recall("42 config.yaml", project="")["seeds"], [])

    def test_explain_recall_matches_recall(self):
        explained = self.engine.explain_recall("config.yaml はどこ", project="")
        recalled = self.engine.recall("config.yaml はどこ", project="")
        self.assertEqual([g["id"] for g in explained["germinated"]], [s["id"] for s in recalled["seeds"]])
        self.assertEqual(explained["germinated"][0]["matched_conditions"], ["config.yaml"])

    def test_imprint_merge_candidate_does_not_crash(self):
        results = self.engine.imprint([self.turn], [dict(self.PROPOSAL)])
        self.assertEqual(results[0]["sansho"], "圓成實")
        self.assertEqual(results[0]["seed"]["id"], 1)
        self.assertFalse(results[0]["is_new"])
        self.assertEqual(self._stored_conditions(), [42, ["x"], "config.yaml"])

    def test_cmd_seeds_shows_only_str_conditions(self):
        from raiya import cli
        saved = dict(os.environ)
        os.environ["RAIYA_STATE_DIR"] = self.tmpdir
        try:
            agent = cli.Agent(project="")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                agent.cmd_seeds()
        finally:
            os.environ.clear()
            os.environ["RAIYA_CONFIG"] = ""
            os.environ.update(saved)
        self.assertIn("縁=config.yaml", buf.getvalue())

    def test_supersede_keeps_faithful_history_with_non_str_conditions(self):
        turn = Turn(user_text="設定は yaml に移した p/ yaml", assistant_text="")
        results = self.engine.imprint([turn], [{
            "action": "supersede", "target_id": 1, "kind": "所在", "content": "設定は yaml にある",
            "conditions": ["yaml"], "evidence": {"source": "user", "span": "設定は yaml に移した p/"},
        }])
        self.assertTrue(results[0].get("superseded"))
        seed = _read_state(self.alaya_path)["seeds"][0]
        self.assertEqual(seed["history"][-1]["conditions"], [42, ["x"], "config.yaml"])
        self.assertEqual(seed["conditions"], ["yaml"])

    def test_consolidate_prompt_index_skips_non_str(self):
        seeds = self.engine.inspect_seeds()
        prompt = _build_consolidate_prompt([], {"related": seeds, "related_total": len(seeds), "groups": [], "groups_total": 0})
        self.assertIn("縁: config.yaml", prompt)

    def test_cli_seed_text_skips_non_str(self):
        self.assertIn("縁: config.yaml)", build_seed_text(self.engine.inspect_seeds()))


class TestNonListSeedConditions(unittest.TestCase):
    """Issue #27: a seed's conditions that is not a list at all (null / 42 / str / dict) must
    not crash readers, and a str must not be iterated per character. Stored rows stay as-is (E8).
    """

    SHAPES = (None, 42, "config.yaml", {"config.yaml": 1})
    PROPOSAL = TestNonStrSeedConditions.PROPOSAL

    def _make(self, shape):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        engine = MemoryEngine(state_dir=tmpdir, new_life=False)
        turn = Turn(user_text="設定は config.yaml にある p/", assistant_text="ok")
        engine.imprint([turn], [dict(self.PROPOSAL)])
        path = os.path.join(tmpdir, "alaya.json")
        data = _read_state(path)
        data["seeds"][0]["conditions"] = shape
        _write_state(path, data)
        return tmpdir, engine, turn, path

    @staticmethod
    def _stored(path):
        return _read_state(path)["seeds"][0]["conditions"]

    def test_helper(self):
        self.assertEqual(seed_conditions({"conditions": ["a", 1, ["b"], "c"]}), ["a", "c"])
        for shape in self.SHAPES:
            self.assertEqual(seed_conditions({"conditions": shape}), [])
        self.assertEqual(seed_conditions({}), [])

    def test_merge_similarity_proposal_side_guard(self):
        self.assertEqual(_merge_similarity({"conditions": ["a"]}, "x", [42, "a"]), 1.0)
        self.assertEqual(_merge_similarity({"conditions": ["a"]}, "x", 42), 0.0)

    def test_readers_survive_every_shape(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                tmpdir, engine, turn, path = self._make(shape)
                for text in ("cat", "config.yaml はどこ"):
                    recalled = engine.recall(text, project="")
                    explained = engine.explain_recall(text, project="")
                    self.assertEqual([g["id"] for g in explained["germinated"]], [s["id"] for s in recalled["seeds"]])
                    self.assertEqual(recalled["seeds"], [])
                prompt = _build_consolidate_prompt([], {"related": engine.inspect_seeds(), "related_total": len(engine.inspect_seeds()), "groups": [], "groups_total": 0})
                self.assertIn("| 縁: \n", prompt)
                self.assertIn("縁: )", build_seed_text(engine.inspect_seeds()))
                from raiya import inspect_cli
                for argv in (["show"], ["why", "1"]):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        code = inspect_cli.main(["--state-dir", tmpdir] + argv)
                    self.assertEqual(code, 0)
                    self.assertIn("縁: (なし)", buf.getvalue())
                    self.assertNotIn("縁: c / o", buf.getvalue())
                self.assertEqual(self._stored(path), shape)

    def test_merge_and_supersede_survive_every_shape(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                tmpdir, engine, turn, path = self._make(shape)
                results = engine.imprint([turn], [dict(self.PROPOSAL)])
                self.assertEqual(results[0]["seed"]["id"], 1)
                self.assertEqual(self._stored(path), shape)
                turn2 = Turn(user_text="設定は yaml に移した p/ yaml", assistant_text="")
                results = engine.imprint([turn2], [{
                    "action": "supersede", "target_id": 1, "kind": "所在", "content": "設定は yaml にある",
                    "conditions": ["yaml"], "evidence": {"source": "user", "span": "設定は yaml に移した p/"},
                }])
                self.assertTrue(results[0].get("superseded"))
                seed = _read_state(path)["seeds"][0]
                self.assertEqual(seed["history"][-1]["conditions"], shape)
                self.assertEqual(seed["conditions"], ["yaml"])
                from raiya import inspect_cli
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(inspect_cli.main(["--state-dir", tmpdir, "why", "1"]), 0)

    def test_cmd_seeds_every_shape(self):
        from raiya import cli
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                tmpdir, engine, turn, path = self._make(shape)
                saved = dict(os.environ)
                os.environ["RAIYA_STATE_DIR"] = tmpdir
                try:
                    agent = cli.Agent(project="")
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        agent.cmd_seeds()
                finally:
                    os.environ.clear()
                    os.environ["RAIYA_CONFIG"] = ""
                    os.environ.update(saved)
                line = [ln for ln in buf.getvalue().splitlines() if "[1]" in ln][0]
                self.assertIn("縁= 極性", line)
                self.assertNotIn("c、o、n", line)

    def test_merge_similarity_seed_side_str_is_not_walked(self):
        self.assertEqual(_merge_similarity({"conditions": "a"}, "zzzz", ["a"]), 0.0)

    def _make_pair(self, shape):
        """seed 1 = 正常な圓成實、seed 2 = 依他起で縁が壊れている (同 kind・同 project)。"""
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        engine = MemoryEngine(state_dir=tmpdir, new_life=False)
        turn = Turn(user_text="設定は config.yaml にある p/ 設定", assistant_text="ok")
        engine.imprint([turn], [{
            "kind": "所在", "content": "設定は config.yaml にある p/", "conditions": ["config.yaml", "設定"],
            "evidence": {"source": "user", "span": "設定は config.yaml にある p/"},
        }])
        path = os.path.join(tmpdir, "alaya.json")
        data = _read_state(path)
        data["seeds"].append({
            "id": 2, "content": "別の所在", "conditions": shape, "sansho": "依他起", "kind": "所在",
            "polarity": "無記", "strength": 5, "project": None, "evidences": [], "history": [],
            "planted_at": 0, "fire_count": 0, "last_fired": None, "miss_streak": 0,
        })
        data["next_id"] = 3
        _write_state(path, data)
        return tmpdir, engine, path

    def _seeds(self, path):
        return _read_state(path)["seeds"]

    def test_twin_shadows_h_side_non_list(self):
        for shape in (42, {"config.yaml": 1, "設定": 1}):
            with self.subTest(shape=shape):
                tmpdir, engine, path = self._make_pair(shape)
                text = "config.yaml 設定"
                recalled = engine.recall(text, project="")
                explained = engine.explain_recall(text, project="")
                self.assertEqual([s["id"] for s in recalled["seeds"]], [1])
                self.assertEqual([g["id"] for g in explained["germinated"]], [1])
                # 索引でも壊れた縁の H は双子として隠されない (縁 dict のキーを歩かない)
                self.assertEqual([i["id"] for i in engine.recall("hello", project="")["index"]], [1, 2])
                rejected = engine.explain_recall("hello", project="")["index_rejected"]
                self.assertFalse([r for r in rejected if "双子" in r["reason"]])

    def test_twin_shadows_e_side_str_not_walked(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        engine = MemoryEngine(state_dir=tmpdir, new_life=False)
        engine.imprint([Turn(user_text="場所は ab にある p/ a", assistant_text="")], [{
            "kind": "所在", "content": "場所は ab にある", "conditions": ["ab"],
            "evidence": {"source": "user", "span": "場所は ab にある p/"},
        }])
        engine.imprint([Turn(user_text="ok", assistant_text="鍵は a と b の棚にある")], [{
            "kind": "所在", "content": "鍵は棚にある", "conditions": ["a", "b"],
            "evidence": {"source": "assistant", "span": "鍵は a と b の棚にある"},
        }])
        path = os.path.join(tmpdir, "alaya.json")
        data = _read_state(path)
        self.assertEqual([x["sansho"] for x in data["seeds"]], ["圓成實", "依他起"])
        data["seeds"][0]["conditions"] = "ab"
        _write_state(path, data)
        self.assertEqual([i["id"] for i in engine.recall("hello", project="")["index"]], [1, 2])
        rejected = engine.explain_recall("hello", project="")["index_rejected"]
        self.assertFalse([r for r in rejected if "双子" in r["reason"]])

    def test_horizontal_supersede_skips_non_list_seed(self):
        for shape in (42, {"config.yaml": 1, "設定": 1}):
            with self.subTest(shape=shape):
                tmpdir, engine, path = self._make_pair(shape)
                turn = Turn(user_text="設定は config.yaml と 設定 に移した p/", assistant_text="")
                results = engine.imprint([turn], [{
                    "action": "supersede", "target_id": 1, "kind": "所在", "content": "設定は移した",
                    "conditions": ["config.yaml", "設定"],
                    "evidence": {"source": "user", "span": "設定は config.yaml と 設定 に移した p/"},
                }])
                self.assertTrue(results[0].get("superseded"))
                seed2 = self._seeds(path)[1]
                self.assertIsNone(seed2.get("superseded_by"))
                self.assertEqual(seed2["history"], [])
                self.assertEqual(seed2["conditions"], shape)

    def test_supersede_history_for_absent_conditions_key_is_empty_list(self):
        tmpdir, engine, turn, path = self._make(None)
        data = _read_state(path)
        del data["seeds"][0]["conditions"]
        _write_state(path, data)
        turn2 = Turn(user_text="設定は yaml に移した p/ yaml", assistant_text="")
        engine.imprint([turn2], [{
            "action": "supersede", "target_id": 1, "kind": "所在", "content": "設定は yaml にある",
            "conditions": ["yaml"], "evidence": {"source": "user", "span": "設定は yaml に移した p/"},
        }])
        self.assertEqual(self._seeds(path)[0]["history"][-1]["conditions"], [])


if __name__ == "__main__":
    unittest.main()
