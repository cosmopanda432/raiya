"""保存層の sqlite3 化 (#38) スライス 2・3・4: 区間 (section) の境界の pin。
(旧 `tests/test_legacy_json.py`。スライス 4 で旧 JSON の読み手 `legacy_json` を退役したので、その固有のテストを除いて改名)

- `snapshot` は `backend` (LLM) を import しない (AST + fresh process)。
- 動詞途中の暗黙の書き込みは無い: ストアの動詞は `commit()` まで DB の何も変えず、`raiya/` の中に `.save()` 呼び出しは無い。
  `MemoryEngine._locked` (= `SqliteBackend.section(write=True)`) が唯一の書き込み点。
- 例外が出た動詞はロールバックする (設計書 P10 の受け入れた振る舞い変更): `engine.imprint` のループ途中で
  例外なら、先の提案も保存されない。
- `snapshot.write_state` は新規 state dir にだけ (`raiya.db` を) 書き、既存 state は拒否する。
"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import snapshot  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerStore, LedgerTurn  # noqa: E402
from raiya.manas import ManasStore  # noqa: E402
from raiya import storage  # noqa: E402
from tests._state_fixture import (  # noqa: E402
    alaya_store_at, clear_state, ledger_rows, ledger_store_at, manas_store_at, meta_value, table_bodies, table_digest,
)

P = "/home/dev/p"


def _source(name):
    with open(os.path.join(BASE, "raiya", name), "r", encoding="utf-8") as f:
        return f.read()


def _imports_backend(source):
    """ネストした関数内の import も含めて `backend` を import しているか (ast.walk)。"""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[-1] == "backend" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == "backend" or any(a.name == "backend" for a in node.names):
                return True
    return False


def _digest(path):
    out = {}
    for root, _, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


class TestNoBackendImport(unittest.TestCase):
    def test_snapshot_does_not_import_backend_anywhere(self):
        for name in ("snapshot.py",):
            self.assertFalse(_imports_backend(_source(name)), name)

    def test_fresh_process_importing_them_does_not_load_backend(self):
        code = (
            "import sys; import raiya.snapshot; "
            "from raiya.engine import MemoryEngine; "
            "sys.exit(1 if 'raiya.backend' in sys.modules else 0)"
        )
        env = {k: v for k, v in os.environ.items()}
        env["RAIYA_CONFIG"] = ""
        env["PYTHONPATH"] = BASE
        self.assertEqual(subprocess.run([sys.executable, "-c", code], env=env, cwd=tempfile.gettempdir()).returncode, 0)


class TestNoSaveCallInRaiya(unittest.TestCase):
    def test_no_save_call_anywhere(self):
        offenders = []
        for root, _, files in os.walk(os.path.join(BASE, "raiya")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ("save", "_save_json", "_replace_json")):
                        offenders.append("{}:{}".format(os.path.relpath(path, BASE), node.lineno))
        self.assertEqual(offenders, [])

    def test_imprint_has_no_save_parameter(self):
        import inspect

        self.assertNotIn("save", inspect.signature(alaya_mod.AlayaStore.imprint).parameters)


class TestStoreVerbsDoNotWriteUntilCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_sections_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _db_files(self):
        return sorted(n for n in os.listdir(self.tmp) if n != ".lock")

    def test_alaya_verbs_write_nothing_then_commit_writes(self):
        store = alaya_store_at(os.path.join(self.tmp, "alaya.json"))
        before = table_digest(self.tmp)
        store.begin_new_life()
        store.germinate("猫", project=P)
        store.imprint(LedgerTurn(user_text="猫が好きです p/ 猫", assistant_text=""),
                      {"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
                       "evidence": [{"source": "user", "span": "猫が好きです p/"}]})
        self.assertEqual(table_digest(self.tmp), before)  # commit までどの表も 1 行も変わらない
        store.commit()
        self.assertEqual(self._db_files(), ["raiya.db"])
        again = alaya_store_at(os.path.join(self.tmp, "alaya.json"))
        self.assertEqual(again.current_life, 1)
        self.assertEqual(len(again.list_seeds()), 1)
        self.assertEqual(len(again.list_imprint_log()), 1)

    def test_manas_verbs_write_nothing_then_commit_writes(self):
        store = manas_store_at(os.path.join(self.tmp, "manas.json"))
        before = table_digest(self.tmp)
        store.reduce_ego_weight()
        store.record_introspection(1, "内観")
        self.assertEqual(table_digest(self.tmp), before)
        store.commit()
        again = manas_store_at(os.path.join(self.tmp, "manas.json"))
        self.assertAlmostEqual(again.ego_weight, 0.85)
        self.assertEqual(len(again.state["introspections"]), 1)

    def test_ledger_append_is_buffered_visible_and_written_at_commit(self):
        store = ledger_store_at(os.path.join(self.tmp, "ledger.jsonl"), os.path.join(self.tmp, "ledger_meta.json"))
        before = table_digest(self.tmp)
        self.assertEqual(store.append("u1", "a1", life=1), 1)
        self.assertEqual(store.append("u2", "a2", life=1, project=P), 2)
        self.assertEqual(table_digest(self.tmp), before)
        self.assertEqual([t.id for t in store.unconsolidated()], [1, 2])  # バッファは読み手に見える
        self.assertEqual(store.last_id(), 2)
        self.assertEqual(store.tail(1)[0]["project"], P)
        store.mark_consolidated(1)
        self.assertEqual(table_digest(self.tmp), before)
        store.commit()
        self.assertEqual([r["id"] for r in ledger_rows(self.tmp)], [1, 2])
        again = ledger_store_at(os.path.join(self.tmp, "ledger.jsonl"), os.path.join(self.tmp, "ledger_meta.json"))
        self.assertEqual((again.last_id(), again.consolidated_upto()), (2, 1))
        store.commit()  # 2 回目は追記行を重ねない
        self.assertEqual(len(table_bodies(self.tmp, "ledger")), 2)

    def test_ledger_reload_drops_uncommitted_appends(self):
        store = ledger_store_at(os.path.join(self.tmp, "ledger.jsonl"), os.path.join(self.tmp, "ledger_meta.json"))
        store.append("u", "a", life=1)
        store.reload()
        self.assertEqual((store.count(), store.last_id()), (0, 0))

    def test_quarantine_eviction_is_unchanged(self):
        store = alaya_store_at(os.path.join(self.tmp, "alaya.json"))
        for i in range(alaya_mod.QUARANTINE_MAX + 3):
            store._append_quarantine({"id": i})
        self.assertEqual([q["id"] for q in store.list_quarantine()],
                         list(range(3, alaya_mod.QUARANTINE_MAX + 3)))


class TestVerbRollsBackOnException(unittest.TestCase):
    """設計書 P10 の受け入れた振る舞い変更: op の途中で例外なら動詞全体がロールバック (部分保存は無い)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_rollback_")
        self.state = os.path.join(self.tmp, "state")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        snapshot.write_state(self.state, {"seeds": [], "alaya_meta": {"life": 1}, "imprint_log": [],
                                          "ego_weight": 1.0})

    def _proposal(self, content, span, cond):
        return {"content": content, "conditions": [cond], "polarity": "無記", "kind": "所在",
                "evidence": [{"source": "user", "span": span}]}

    def test_exception_in_second_proposal_leaves_first_unsaved(self):
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        before = _digest(self.state)
        turn = LedgerTurn(user_text="猫が好きです 朝に散歩します 猫 散歩", assistant_text="")
        props = [self._proposal("猫が好き", "猫が好きです", "猫"), self._proposal("朝に散歩する", "朝に散歩します", "散歩")]
        real = alaya_mod.AlayaStore.append_imprint_log
        calls = []

        def boom(store, entry):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(store, entry)

        with mock.patch.object(alaya_mod.AlayaStore, "append_imprint_log", boom):
            with self.assertRaises(RuntimeError):
                eng.imprint(turn, props, project=P)
        self.assertEqual(len(calls), 2)  # 1 件目は in-memory では成功していた
        after = _digest(self.state)
        after.pop(".lock", None)
        before.pop(".lock", None)
        self.assertEqual(after, before)  # ディスクは 1 バイトも変わらない (.bak も出来ない)
        fresh = MemoryEngine(state_dir=self.state, new_life=False)
        self.assertEqual(fresh.inspect_seeds(), [])
        self.assertEqual(fresh.imprint_log_rows(), [])
        # 同じ engine の次の動詞は再読込するので、幻の種子は残らない
        self.assertEqual(eng.inspect_seeds(), [])

    def test_all_proposals_saved_together_when_no_exception(self):
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        turn = LedgerTurn(user_text="猫が好きです/ 朝に散歩します/ 猫 散歩", assistant_text="")
        eng.imprint(turn, [self._proposal("猫が好き", "猫が好きです/", "猫"),
                           self._proposal("朝に散歩する", "朝に散歩します/", "散歩")], project=P)
        fresh = MemoryEngine(state_dir=self.state, new_life=False)
        self.assertEqual(len(fresh.inspect_seeds()), 2)
        self.assertEqual(len(fresh.imprint_log_rows()), 2)
        self.assertEqual(len(fresh.imprint_log_rows(1)), 1)
        self.assertEqual(fresh.imprint_log_rows(0), [])

    def test_engine_construction_with_new_life_commits_the_life(self):
        MemoryEngine(state_dir=self.state, new_life=True)
        self.assertEqual(MemoryEngine(state_dir=self.state, new_life=False).audit()["life"], 2)


def _data_digest(path):
    """永続ファイル (.lock を除く。.bak を含む) の sha256。"""
    return {k: v for k, v in _digest(path).items() if k != ".lock"}


class _EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_lj_engine_")
        self.state = os.path.join(self.tmp, "state")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)

    def _proposal(self, content, span, cond):
        return {"content": content, "conditions": [cond], "polarity": "無記", "kind": "所在",
                "evidence": [{"source": "user", "span": span}]}


class TestLedgerSectionRollback(_EngineCase):
    def test_section_with_ledger_rolls_back_appended_row(self):
        eng = self.engine()
        eng.record_turn("一つ目", "了解", session_id="s", project=P)
        before = _digest(self.state)
        before.pop(".lock", None)
        with mock.patch.object(LedgerStore, "pending_count", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                eng.record_turn("二つ目", "了解", session_id="s", project=P)
        after = _digest(self.state)
        after.pop(".lock", None)
        self.assertEqual(after, before)  # jsonl にも meta にも .bak にも触れていない
        # 同じ engine の in-memory も section が読み直している (次の動詞の reload に頼らない)
        self.assertEqual(eng.ledger.count(), 1)
        self.assertEqual(eng.record_turn("二つ目", "了解", session_id="s", project=P)["ledger_id"], 2)

    def test_two_engines_interleaved_record_turn_ids_monotonic(self):
        a, b = self.engine(), self.engine()
        ids = [e.record_turn("u{}".format(i), "a", session_id="s", project=P)["ledger_id"]
               for i, e in enumerate([a, b, a, b, b, a])]
        self.assertEqual(ids, [1, 2, 3, 4, 5, 6])
        self.assertEqual([r["id"] for r in ledger_rows(self.state)], [1, 2, 3, 4, 5, 6])
        self.assertEqual(meta_value(self.state, "ledger.next_id"), 7)


class TestBufferedRowsVisibleToEveryReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_buffered_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.jsonl = os.path.join(self.tmp, "ledger.jsonl")  # ledger_store_at はファイル名を無視する (raiya.db)
        self.meta = os.path.join(self.tmp, "ledger_meta.json")

    def test_buffered_rows_visible_to_count_pending_ids_and_bases_before_any_row_is_written(self):
        store = ledger_store_at(self.jsonl, self.meta)
        store.append("u1", "a1", life=1, project=P, session_id="s1")
        store.append("u2", "a2", life=1, session_id="s2")
        self.assertEqual(table_bodies(self.tmp, "ledger"), [])  # 行はまだ書かれていない (バッファだけ)
        self.assertEqual(store.count(), 2)
        self.assertEqual(store.pending_count(), 2)
        self.assertEqual([t.id for t in store.turns_by_ids([2, 1, 9])], [1, 2])
        self.assertEqual(sorted(store.rows_by_ids([1, 2, 9])), [1, 2])
        self.assertEqual(store.rows_by_ids([1])[1]["user_text"], "u1")
        self.assertEqual(store.unconsolidated_bases(), [(P, "s1"), (None, "s2")])
        store.mark_consolidated(1)
        self.assertEqual(store.pending_count(), 1)
        self.assertEqual(store.unconsolidated_bases(), [(None, "s2")])

    def test_buffered_rows_follow_committed_rows(self):
        store = ledger_store_at(self.jsonl, self.meta)
        store.append("u1", "a1", life=1)
        store.commit()
        store.append("u2", "a2", life=1)
        self.assertEqual(store.count(), 2)
        self.assertEqual([t.id for t in store.turns_by_ids([1, 2])], [1, 2])
        self.assertEqual(len(store.unconsolidated_bases()), 2)
        self.assertEqual(sorted(store.rows_by_ids([1, 2])), [1, 2])


class TestConsolidateRollback(_EngineCase):
    def _two_turns(self):
        eng = self.engine()
        eng.record_turn("猫が好きです 猫", "了解", session_id="s", project=P)
        eng.record_turn("朝に散歩します 散歩", "了解", session_id="s", project=P)
        return eng

    def test_consolidate_llm_failure_leaves_content_and_cursor_unchanged(self):
        # 読み区間 (_locked) は同一内容でファイルを書き直すので「何も書かない」ではなく内容 (.bak 込み) の不変を見る。
        from raiya import backend
        eng = self._two_turns()
        before = _data_digest(self.state)
        with mock.patch("raiya.backend.call_llm", side_effect=backend.BackendError("down")):
            with self.assertRaises(backend.BackendError):
                eng.consolidate()
        self.assertEqual(_data_digest(self.state), before)
        self.assertEqual(self.engine().audit()["ledger"], {"turns": 2, "consolidated_upto": 0, "pending": 2})

    def test_consolidate_imprint_exception_mid_window_rolls_back(self):
        eng = self._two_turns()
        before = _data_digest(self.state)
        raw = json.dumps([
            dict(self._proposal("猫が好き", "猫が好きです", "猫"), action="new"),
            dict(self._proposal("朝に散歩する", "朝に散歩します", "散歩"), action="new"),
        ], ensure_ascii=False)
        real = alaya_mod.AlayaStore.append_imprint_log
        calls = []

        def boom(store, entry):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(store, entry)

        with mock.patch("raiya.backend.call_llm", return_value=raw), \
                mock.patch.object(alaya_mod.AlayaStore, "append_imprint_log", boom):
            with self.assertRaises(RuntimeError):
                eng.consolidate()
        self.assertEqual(len(calls), 2)
        self.assertEqual(_data_digest(self.state), before)  # 種子も imprint_log も無く、カーソルも 0 のまま
        fresh = self.engine()
        self.assertEqual(fresh.inspect_seeds(), [])
        self.assertEqual(fresh.imprint_log_rows(), [])
        self.assertEqual(fresh.audit()["ledger"]["consolidated_upto"], 0)


class TestSnapshotWriteState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_snapshot_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "new_state")

    def test_writes_a_state_an_engine_can_read(self):
        row = {"id": 1, "life": 1, "session_id": "s", "ts": 1.0, "user_text": "u", "assistant_text": "a"}
        snapshot.write_state(self.state, {
            "seeds": [], "alaya_meta": {"life": 4}, "imprint_log": [{"decision": "reject"}],
            "manas_log": [{"life": 1}], "ego_weight": 0.5, "introspections": [],
            "ledger_rows": [row], "ledger_meta": {"consolidated_upto": 0},
        })
        with mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""}):
            eng = MemoryEngine(state_dir=self.state, new_life=False)
            audit = eng.audit()
            self.assertEqual(audit["life"], 4)
            self.assertEqual(audit["ledger"]["pending"], 1)
            self.assertEqual(eng.imprint_log_rows(), [{"decision": "reject"}])
            self.assertEqual(eng.injection_log(5), [{"life": 1}])

    def test_refuses_an_existing_state_and_leaves_it_untouched(self):
        snapshot.write_state(self.state, {"seeds": []})
        before = _digest(self.state)
        with self.assertRaises(snapshot.StateExists):
            snapshot.write_state(self.state, {"seeds": [{"id": 1}]})
        self.assertEqual(_digest(self.state), before)

    def test_writes_only_the_sections_given(self):
        snapshot.write_state(self.state, {"seeds": []})
        self.assertEqual(os.listdir(self.state), ["raiya.db"])  # 保存層は raiya.db 一つ (旧 JSON は書かない)
        self.assertEqual(meta_value(self.state, "db.origin"), "fresh")

    def test_origin_is_recorded(self):
        snapshot.write_state(self.state, {"seeds": []}, origin="migrated")
        self.assertEqual(meta_value(self.state, "db.origin"), "migrated")

    def test_test_helper_clear_state_removes_only_state_files(self):
        snapshot.write_state(self.state, {"seeds": [], "imprint_log": []})
        with open(os.path.join(self.state, "keep.txt"), "w") as f:
            f.write("x")
        clear_state(self.state)
        self.assertEqual(os.listdir(self.state), ["keep.txt"])
        snapshot.write_state(self.state, {"seeds": []})  # 消した後は書ける

    def test_clear_state_refuses_non_tmp_path(self):
        import tempfile as _tf
        outside = os.path.join(os.path.expanduser("~"), "raiya_clear_state_guard_probe")
        real_tmp = os.path.realpath(_tf.gettempdir())
        if not os.path.realpath(outside).startswith(real_tmp + os.sep):  # home が tmp 配下ならこの枝は意味がない
            with self.assertRaises(ValueError):
                clear_state(outside)
            self.assertFalse(os.path.exists(outside))  # 拒否は何も作らない
        with self.assertRaises(ValueError):
            clear_state(os.path.realpath(_tf.gettempdir()))  # tmp そのものも不可
        with self.assertRaises(ValueError):
            clear_state(os.path.expanduser("~/.raiya"))
        # tmp 配下でも RAIYA_STATE_DIR と同じ dir は拒否 (自作 tmp に消してはいけない印の state を置く)
        guarded = os.path.join(self.tmp, "guarded")
        os.makedirs(guarded)
        snapshot.write_state(guarded, {"seeds": []})
        with mock.patch.dict(os.environ, {"RAIYA_STATE_DIR": guarded}):
            with self.assertRaises(ValueError):
                clear_state(guarded)
        self.assertEqual(os.listdir(guarded), ["raiya.db"])

    def test_write_state_refuses_bak_and_reserved_db(self):
        os.makedirs(self.state)
        for name in ("alaya.json.bak", "raiya.db"):
            path = os.path.join(self.state, name)
            with open(path, "w") as f:
                f.write("x")
            with self.assertRaises(snapshot.StateExists):
                snapshot.write_state(self.state, {"seeds": []})
            os.remove(path)
        snapshot.write_state(self.state, {"seeds": []})


if __name__ == "__main__":
    unittest.main()
