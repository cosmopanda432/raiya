"""保存層の sqlite3 化 (#38) スライス 3: `raiya/storage.py` の pin (設計書 `2026-10-27-sqlite-storage-design.md` §3 P1〜P11, P15, P16)。

- 依存: `storage` / `snapshot` / `mcp_server` / `debuglog` は `backend` (LLM) を import しない
  (AST + fresh process)。`storage` は stdlib と raiya の純粋なモジュールだけを import し、システム `python3 -I` でも import できる。
- 器: 新規 DB の作り方 (`application_id` / `user_version` / 必須 meta / journal DELETE / synchronous FULL)・`state_status`・
  旧 JSON の dir は `StateNeedsMigration` (何も作らない)。
- 失敗: DB 単位は `StateUnreadable(category)` (NOTADB・application_id 違い・user_version 違い・表/meta の欠落・hot journal)、
  行単位は読み飛ばして警告 (行は書き直さない)。hooks / MCP / cli / inspect / correct の境界での扱い。
- 不変: 追記専用トリガ・間引き 2 つ・派生列が本文と一致・差分 flush (変えていない行はバイトも変えない)・例外でロールバック。
- バックアップ: consolidate が `skipped is None` で COMMIT したときだけ `raiya.db.bak`。
"""
import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import manas as manas_mod  # noqa: E402
from raiya import inspect_cli, correct_cli, sansho, storage  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import (  # noqa: E402
    add_ledger_body, alaya_store_at, db_path, ledger_rows, ledger_store_at, meta_value, mutate, ro, set_meta,
    table_bodies, table_digest,
)

P = "/home/dev/p"


def _source(name):
    with open(os.path.join(BASE, "raiya", name), "r", encoding="utf-8") as f:
        return f.read()


def _imports_backend(source):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[-1] == "backend" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == "backend" or any(a.name == "backend" for a in node.names):
                return True
    return False


def _prop(content="猫が好き", span="猫が好きです/", cond="猫", kind="所在"):
    return {"content": content, "conditions": [cond], "polarity": "無記", "kind": kind,
            "evidence": [{"source": "user", "span": span}]}


def _file_digest(path):
    import hashlib
    out = {}
    for root, _, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_storage_")
        self.state = os.path.join(self.tmp, "state")
        self.addCleanup(shutil.rmtree, self.tmp, True)  # 自作 tmp のみ
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, new_life=False):
        return MemoryEngine(state_dir=self.state, new_life=new_life)

    def populated(self):
        """種子 2・台帳 2・imprint_log・manas_log・隔離 1 を持つ state (engine の動詞だけで作る)。"""
        eng = self.engine()
        r1 = eng.record_turn("猫が好きです/ 猫", "了解", session_id="s1", project=P)
        eng.record_turn("犬も好きです 犬", "了解", session_id="s1", project=P)
        turn = LedgerTurn(user_text="猫が好きです/ 猫", assistant_text="了解", id=r1["ledger_id"], project=P)
        eng.imprint([turn], [_prop(), _prop("捏造", "存在しない引用")], project=P)
        eng.recall("猫", project=P)
        eng.record_introspection("内観の申告")
        return eng

    def garbage_db(self):
        self.engine()  # 作る
        with open(db_path(self.state), "wb") as f:
            f.write(b"this is not a database " * 400)


class TestNoBackendImport(unittest.TestCase):
    NAMES = ("storage.py", "snapshot.py", "mcp_server.py", "debuglog.py")

    def test_none_of_them_import_backend_anywhere(self):
        for name in self.NAMES:
            self.assertFalse(_imports_backend(_source(name)), name)

    def test_fresh_process_importing_storage_and_engine_does_not_load_backend(self):
        code = (
            "import sys; import raiya.storage, raiya.snapshot; "
            "from raiya.engine import MemoryEngine; "
            "sys.exit(1 if 'raiya.backend' in sys.modules else 0)"
        )
        env = dict(os.environ, RAIYA_CONFIG="", PYTHONPATH=BASE)
        self.assertEqual(subprocess.run([sys.executable, "-c", code], env=env, cwd=tempfile.gettempdir()).returncode, 0)

    def test_system_python_isolated_can_import_storage(self):
        # PyYAML も mcp も要らない: storage の import 連鎖は stdlib と raiya の純粋なモジュールだけ。
        code = "import sys; sys.path.insert(0, {!r}); import raiya.storage as s; print(s.USER_VERSION)".format(BASE)
        for exe in ("python3", sys.executable):
            found = shutil.which(exe) or exe
            r = subprocess.run([found, "-I", "-c", code], capture_output=True, text=True, cwd=tempfile.gettempdir())
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "1"), msg=r.stderr)

    def test_storage_imports_only_stdlib_and_pure_raiya_modules(self):
        allowed_raiya = {"alaya", "ledger", "lock", "manas", "__init__"}
        for node in ast.walk(ast.parse(_source("storage.py"))):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertIn(a.name.split(".")[0], sys.stdlib_module_names, a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.assertIn((node.module or "").split(".")[0] or "__init__", allowed_raiya | {""}, node.module)
                    if not node.module:
                        for a in node.names:
                            self.assertIn(a.name, {"__version__"}, a.name)
                else:
                    self.assertIn(node.module.split(".")[0], sys.stdlib_module_names, node.module)


class TestStateStatus(_Case):
    def touch(self, name):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, name), "w") as f:
            f.write("x")

    def test_none_for_missing_or_empty_dir_and_unrelated_files(self):
        self.assertEqual(storage.state_status(self.state), "none")
        os.makedirs(self.state)
        self.assertEqual(storage.state_status(self.state), "none")
        for name in ("hooks.log", "debug.log", ".lock"):
            self.touch(name)
        os.makedirs(os.path.join(self.state, "sessions"))
        self.assertEqual(storage.state_status(self.state), "none")
        self.assertEqual(sorted(os.listdir(self.state)), [".lock", "debug.log", "hooks.log", "sessions"])  # 何も作らない

    def test_db_for_raiya_db_or_a_lone_bak(self):
        self.touch("raiya.db")
        self.assertEqual(storage.state_status(self.state), "db")
        os.remove(os.path.join(self.state, "raiya.db"))
        self.touch("raiya.db.bak")  # バックアップだけの dir に新しい DB を作ってはいけない
        self.assertEqual(storage.state_status(self.state), "db")

    def test_legacy_for_any_old_json_file_or_its_bak(self):
        for name in storage.LEGACY_NAMES:
            for suffix in ("", ".bak"):
                shutil.rmtree(self.state, ignore_errors=True)
                self.touch(name + suffix)
                self.assertEqual(storage.state_status(self.state), "legacy", name + suffix)

    def test_db_wins_over_legacy_files(self):
        self.touch("alaya.json")
        self.touch("raiya.db")
        self.assertEqual(storage.state_status(self.state), "db")


class TestFreshDb(_Case):
    def test_empty_dir_gets_a_fresh_db_with_the_documented_header(self):
        self.engine(new_life=True)
        self.assertEqual(sorted(n for n in os.listdir(self.state) if n != ".lock"), ["raiya.db"])
        conn = sqlite3.connect(db_path(self.state))
        try:
            self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0], storage.APPLICATION_ID)
            self.assertEqual(storage.APPLICATION_ID, int.from_bytes(b"RAIY", "big"))
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, set(storage.REQUIRED_TABLES))
            meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
        finally:
            conn.close()
        self.assertEqual(sorted(meta), sorted(storage.REQUIRED_META))
        self.assertEqual(len(storage.REQUIRED_META), 11)
        self.assertEqual(meta["db.origin"], "fresh")
        self.assertIsNone(meta["db.migration_receipt_sha256"])
        self.assertEqual(meta["alaya.life"], 1)  # new_life=True の 1 回目

    def test_write_connections_are_synchronous_full_with_delete_journal(self):
        self.engine()
        backend = storage.SqliteBackend(self.state)
        conn = backend._connect(True)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL
        finally:
            conn.close()

    def test_no_temporary_file_is_left_behind(self):
        self.engine()
        self.assertEqual([n for n in os.listdir(self.state) if "tmp" in n or n.endswith("-journal")], [])

    def test_create_db_refuses_an_existing_db(self):
        self.engine()
        before = _file_digest(self.state)
        with self.assertRaises(FileExistsError):
            storage.create_db(self.state)
        self.assertEqual(_file_digest(self.state), before)

    def test_legacy_dir_raises_state_needs_migration_and_creates_no_db(self):
        os.makedirs(self.state)
        for name in ("alaya.json", "ledger.jsonl"):
            with open(os.path.join(self.state, name), "w") as f:
                f.write("{}")
        before = _file_digest(self.state)
        with self.assertRaises(storage.StateNeedsMigration):
            self.engine()
        self.assertFalse(os.path.exists(db_path(self.state)))
        self.assertEqual(_file_digest(self.state), before)  # .lock も作らない (#86)
        self.assertFalse(os.path.exists(os.path.join(self.state, ".lock")))

    def test_lone_bak_dir_is_unreadable_not_overwritten(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "raiya.db.bak"), "wb") as f:
            f.write(b"bak")
        with self.assertRaises(storage.StateUnreadable) as cm:
            self.engine()
        self.assertEqual(cm.exception.category, "missing_db")
        self.assertFalse(os.path.exists(db_path(self.state)))

    def test_application_id_is_ascii_raiy_and_documented(self):
        self.assertIn("RAIY", _source("storage.py"))


class TestStateUnreadableCategories(_Case):
    def setUp(self):
        super().setUp()
        self.populated()
        self.before = _file_digest(self.state)

    def assert_category(self, category, verb=None):
        verb = verb or (lambda: self.engine(new_life=False))
        with self.assertRaises(storage.StateUnreadable) as cm:
            verb()
        self.assertEqual(cm.exception.category, category)
        self.assertEqual(str(cm.exception), "状態DB 読めず category={}".format(category))
        return cm.exception

    def assert_nothing_written(self):
        after = _file_digest(self.state)
        after.pop(".lock", None)
        before = dict(self.before)
        before.pop(".lock", None)
        self.assertEqual(after, before)

    def test_not_a_database(self):
        with open(db_path(self.state), "wb") as f:
            f.write(b"this is not a database " * 400)
        self.before = _file_digest(self.state)
        self.assert_category("not_a_database")
        self.assert_nothing_written()
        self.assertEqual(sorted(n for n in os.listdir(self.state) if n != ".lock" and n != "sessions"), ["raiya.db"])

    def test_zero_byte_db_is_not_a_database_and_untouched(self):
        # #81: sqlite は 0 バイトを有効な空 DB として開く。wrong_application_id でなく not_a_database。
        open(db_path(self.state), "wb").close()
        self.before = _file_digest(self.state)
        for verb in (lambda: self.engine(new_life=False), lambda: self.engine(new_life=False).audit()):
            self.assert_category("not_a_database", verb)
        self.assert_nothing_written()
        self.assertEqual(os.path.getsize(db_path(self.state)), 0)

    def test_header_truncated_db_is_corrupt_and_untouched(self):
        # #81: 100 バイト未満 (ヘッダの途中) は corrupt。
        path = db_path(self.state)
        with open(path, "rb") as f:
            head = f.read(99)
        for n in (1, 50, 99):
            with self.subTest(size=n):
                with open(path, "wb") as f:
                    f.write(head[:n])
                self.before = _file_digest(self.state)
                self.assert_category("corrupt")
                self.assert_nothing_written()
                self.assertEqual(os.path.getsize(path), n)

    def test_wrong_application_id(self):
        conn = sqlite3.connect(db_path(self.state))
        conn.execute("PRAGMA application_id=12345")
        conn.close()
        self.before = _file_digest(self.state)
        self.assert_category("wrong_application_id")
        self.assert_nothing_written()

    def test_newer_user_version(self):
        conn = sqlite3.connect(db_path(self.state))
        conn.execute("PRAGMA user_version=2")
        conn.close()
        self.before = _file_digest(self.state)
        self.assert_category("newer_version")
        self.assert_nothing_written()

    def test_unknown_older_user_version(self):
        conn = sqlite3.connect(db_path(self.state))
        conn.execute("PRAGMA user_version=0")
        conn.close()
        self.assert_category("unknown_version")

    def test_missing_table(self):
        conn = sqlite3.connect(db_path(self.state))
        conn.execute("DROP TABLE introspections")
        conn.close()
        self.before = _file_digest(self.state)
        exc = self.assert_category("missing_table")
        self.assertIn("introspections", exc.detail)
        self.assert_nothing_written()

    def test_every_required_meta_key_is_required(self):
        for key in storage.REQUIRED_META:
            with self.subTest(key=key):
                shutil.rmtree(self.state, ignore_errors=True)
                self.populated()
                mutate(self.state, lambda c, k=key: c.execute("DELETE FROM meta WHERE key=?", (k,)))
                exc = self.assert_category("missing_meta")
                self.assertIn(key, exc.detail)

    def test_meta_value_that_is_not_json_or_has_the_wrong_type(self):
        set_meta(self.state, "alaya.life", "x")  # JSON としては読める ("x") が整数でない
        self.assert_category("bad_meta")
        set_meta(self.state, "alaya.life", 1)
        mutate(self.state, lambda c: c.execute("UPDATE meta SET value='{not json' WHERE key='manas.extra'"))
        self.assert_category("bad_meta")

    def test_hot_journal_is_unreadable_and_not_touched(self):
        # 1 MB 級の種子行を足してから、別プロセスが更新の途中で死ぬ (os._exit) = 書きかけの rollback journal が残る。
        def fill(conn):
            for i in range(1500):
                conn.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (?, '所在', NULL, 0, ?)",
                             (1000 + i, "{" + "y" * 1000))
        mutate(self.state, fill)
        code = (
            "import sqlite3, os\n"
            "c = sqlite3.connect({!r}, isolation_level=None)\n"
            "c.execute('BEGIN IMMEDIATE')\n"
            "c.execute(\"UPDATE seeds SET project='/x'\")\n"
            "os._exit(0)\n"
        ).format(db_path(self.state))
        subprocess.run([sys.executable, "-c", code], check=True)
        self.assertTrue(os.path.exists(db_path(self.state) + "-journal"), "テストの前提: hot journal が残る")
        before = _file_digest(self.state)
        self.assert_category("hot_journal")
        self.assertEqual(_file_digest(self.state), before)  # journal を巻き戻す/消すのは書き手の仕事。読み手は触らない
        with self.assertRaises(storage.StateUnreadable):
            self.engine().audit()
        self.assertEqual(_file_digest(self.state), before)

    def test_unreadable_is_raised_by_read_and_write_verbs_alike(self):
        with open(db_path(self.state), "wb") as f:
            f.write(b"garbage" * 1000)
        for verb in (
            lambda: self.engine(),
            lambda: self.engine(new_life=True),
        ):
            self.assert_category("not_a_database", verb)

    def test_corrupt_middle_of_the_file_is_corrupt_or_not_a_database(self):
        size = os.path.getsize(db_path(self.state))
        with open(db_path(self.state), "r+b") as f:
            f.seek(size // 2)
            f.write(b"\xff" * 4096)
        try:
            self.engine().audit()
        except storage.StateUnreadable as e:
            self.assertIn(e.category, ("corrupt", "not_a_database", "unreadable"))
        else:
            # ページによっては読めてしまう (quick_check が audit の警告に出す)。どちらでも空に倒れない。
            self.assertTrue(self.engine().audit()["seed_count"] >= 0)


class TestRowLevelWarnings(_Case):
    """行単位の失敗: 読み飛ばして該当ストアの warning に数え、DB の行は書き直さない・消えない。"""

    def setUp(self):
        super().setUp()
        self.populated()

    def digest_after(self, verb):
        before = table_digest(self.state)
        verb()
        self.assertEqual(table_digest(self.state), before)

    def test_seeds_table(self):
        good = len(self.engine().inspect_seeds())
        for body in ("{unparseable", "[1, 2]", '"str"', "null", '{"id": 99, "content": "強度なし"}'):
            mutate(self.state, lambda c, b=body: c.execute(
                "INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, ?)", (b,)))
        rep = self.engine().audit()
        self.assertIn("種子 5 件", rep["alaya_warning"])
        self.assertEqual(len(self.engine().inspect_seeds()), good)
        self.digest_after(lambda: self.engine().audit())
        self.engine().recall("猫", project=P)  # recall は書く (manas_log・観測欄) が、読めない種子の行には触れない
        self.assertIn("{unparseable", table_bodies(self.state, "seeds", order="pos"))

    def test_quarantine_table(self):
        mutate(self.state, lambda c: c.execute("INSERT INTO quarantine(id, body) VALUES (NULL, '{bad')"))
        mutate(self.state, lambda c: c.execute("INSERT INTO quarantine(id, body) VALUES (NULL, '[1]')"))
        rep = self.engine().audit()
        self.assertIn("quarantine 2 件", rep["alaya_warning"])
        self.digest_after(lambda: self.engine().audit())

    def test_manas_log_table(self):
        mutate(self.state, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('{bad')"))
        rep = self.engine().audit()
        self.assertIn("manas_log / introspections", rep["manas_warning"])
        self.assertIsNone(rep["alaya_warning"])
        self.assertTrue(self.engine().injection_log(10))  # 読める行は残る
        self.digest_after(lambda: self.engine().audit())

    def test_introspections_table(self):
        mutate(self.state, lambda c: c.execute("INSERT INTO introspections(body) VALUES ('{bad')"))
        self.assertIn("manas_log / introspections", self.engine().audit()["manas_warning"])

    def test_imprint_log_table(self):
        mutate(self.state, lambda c: c.execute("INSERT INTO imprint_log(seed_id, body) VALUES (NULL, '{bad')"))
        rep = self.engine().audit()
        self.assertIn("imprint_log の読めない行 1 件", rep["imprint_log_warning"])
        self.assertTrue(rep["imprint_log_tail"])
        self.digest_after(lambda: self.engine().audit())

    def test_ledger_bodies(self):
        add_ledger_body(self.state, "{bad", row_id=99)
        add_ledger_body(self.state, "[1]", row_id=100)
        rep = self.engine().audit()
        self.assertIn("台帳の読めない行 2 件を読み飛ばしました", rep["ledger_warning"])
        self.assertEqual(rep["ledger"]["turns"], 2)
        self.digest_after(lambda: self.engine().audit())

    def test_ledger_rejects_table_is_counted_and_never_deleted(self):
        mutate(self.state, lambda c: c.execute("INSERT INTO ledger_rejects(source_line, raw) VALUES (7, '{bad')"))
        self.assertIn("台帳の読めない行 1 件 (移行時に ledger.jsonl から)", self.engine().audit()["ledger_warning"])
        self.digest_after(lambda: self.engine().audit())

    def test_warnings_are_reported_on_every_call_because_nothing_is_repaired(self):
        mutate(self.state, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('{bad')"))
        for _ in range(3):
            self.assertTrue(self.engine().recall("猫", project=P)["manas_warning"])
        self.assertTrue(self.engine().audit()["manas_warning"])
        self.assertIn("{bad", table_bodies(self.state, "manas_log"))

    def test_audit_runs_quick_check_and_reports_non_ok(self):
        eng = self.engine()
        bad = "状態DB の整合性検査 (quick_check) が ok ではありません: x"
        with mock.patch.object(storage.SqliteBackend, "quick_check", return_value=bad):
            self.assertIn("quick_check", eng.audit()["alaya_warning"])
        self.assertIsNone(eng.audit()["alaya_warning"])

    def test_quick_check_outside_a_section_is_a_programming_error(self):
        backend = storage.SqliteBackend(self.state)
        with self.assertRaises(RuntimeError):
            backend.quick_check()


class TestAppendOnlyTriggers(_Case):
    def setUp(self):
        super().setUp()
        self.populated()
        add_ledger_body(self.state, json.dumps({"id": 50, "user_text": "u", "assistant_text": "a", "life": 1}), 50)
        mutate(self.state, lambda c: c.execute("INSERT INTO ledger_rejects(source_line, raw) VALUES (1, 'x')"))
        self.conn = sqlite3.connect(db_path(self.state), isolation_level=None)
        self.addCleanup(self.conn.close)

    def test_update_and_delete_raise_on_append_only_tables(self):
        for table, col in (("ledger", "body"), ("ledger_rejects", "raw"), ("introspections", "body"), ("imprint_log", "body")):
            for sql in ("UPDATE {} SET {}='x'".format(table, col), "DELETE FROM {}".format(table)):
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.IntegrityError) as cm:
                        self.conn.execute(sql)
                    self.assertIn("append-only", str(cm.exception))

    def test_seeds_and_meta_may_be_updated_but_never_deleted(self):
        self.conn.execute("UPDATE seeds SET project='/changed' WHERE pos=(SELECT MIN(pos) FROM seeds)")
        self.conn.execute("UPDATE meta SET value='1' WHERE key='alaya.next_id'")
        for table in ("seeds", "meta"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("DELETE FROM {}".format(table))

    def test_the_only_deletable_tables_are_the_two_evicted_logs(self):
        self.conn.execute("DELETE FROM quarantine")
        self.conn.execute("DELETE FROM manas_log")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0], 0)

    def test_inserts_are_allowed(self):
        self.conn.execute("INSERT INTO ledger(id, body) VALUES (77, '{}')")
        self.conn.execute("INSERT INTO imprint_log(seed_id, body) VALUES (NULL, '{}')")

    def test_a_read_only_connection_cannot_write_at_all(self):
        with ro(self.state) as conn:
            with self.assertRaises(sqlite3.OperationalError) as cm:
                conn.execute("INSERT INTO manas_log(body) VALUES ('{}')")
            self.assertIn("readonly", str(cm.exception))

    def test_the_trigger_set_is_exactly_the_documented_one(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        expected = {"{}_no_{}".format(t, op) for t in ("ledger", "ledger_rejects", "introspections", "imprint_log")
                    for op in ("update", "delete")} | {"seeds_no_delete", "meta_no_delete"}
        self.assertEqual(names, expected)


class TestEviction(_Case):
    def test_quarantine_keeps_the_newest_QUARANTINE_MAX_rows(self):
        eng = self.engine()
        total = alaya_mod.QUARANTINE_MAX + 4
        for i in range(total):
            eng.imprint(sansho.Turn(user_text="今日は晴れです", assistant_text=""),
                        [_prop("捏造{}".format(i), "捏造span{}".format(i))])
        bodies = [json.loads(b) for b in table_bodies(self.state, "quarantine")]
        self.assertEqual(len(bodies), alaya_mod.QUARANTINE_MAX)
        self.assertEqual([b["id"] for b in bodies], list(range(total - alaya_mod.QUARANTINE_MAX + 1, total + 1)))
        audit = self.engine().audit()
        self.assertEqual(audit["quarantine_total"], total)  # 累計は next_quarantine_id から
        self.assertEqual(audit["quarantine_retained"], alaya_mod.QUARANTINE_MAX)

    def test_manas_log_keeps_the_newest_MANAS_LOG_MAX_rows(self):
        rows = [{"life": 1, "n": i, "seed_ids": [], "index_ids": [], "injected_text": "t"} for i in range(manas_mod.MANAS_LOG_MAX)]
        write_state(self.state, {"seeds": [], "manas_log": rows, "alaya_meta": {"life": 1}})
        self.engine().recall("x", project=P)
        self.engine().recall("y", project=P)
        bodies = [json.loads(b) for b in table_bodies(self.state, "manas_log")]
        self.assertEqual(len(bodies), manas_mod.MANAS_LOG_MAX)
        self.assertEqual(bodies[0]["n"], 2)  # 古い 2 行が間引かれた
        self.assertNotIn("n", bodies[-1])  # 最新は recall の行

    def test_nothing_else_is_ever_thinned(self):
        self.populated_ledger = self.engine()
        for i in range(30):
            self.populated_ledger.record_turn("u{}".format(i), "a", session_id="s")
        self.assertEqual(len(ledger_rows(self.state)), 30)


class TestDerivedColumns(_Case):
    def test_every_derived_column_is_recomputed_from_body(self):
        eng = self.populated()
        eng.imprint([LedgerTurn(user_text="猫が好きです/ 猫", assistant_text="", id=1, project=P)],
                    [_prop("猫が好きだ", "猫が好きです/")], project=P)
        eng.withdraw_seed(1, "太郎", "取り下げ", ledger_ids=[1])
        add_ledger_body(self.state, json.dumps({"id": "x", "project": 3}), 0)
        with ro(self.state) as conn:
            for pos, id_, kind, project, live, body in conn.execute("SELECT pos, id, kind, project, live, body FROM seeds"):
                self.assertEqual((id_, kind, project, live), storage.seed_derived(json.loads(body)), pos)
            for seq, id_, project, session_id, body in conn.execute("SELECT seq, id, project, session_id, body FROM ledger"):
                self.assertEqual((id_, project, session_id), storage.ledger_derived(json.loads(body)), seq)
            for seq, id_, body in conn.execute("SELECT seq, id, body FROM quarantine"):
                self.assertEqual(id_, storage._entry_id(json.loads(body)), seq)
            for seq, seed_id, body in conn.execute("SELECT seq, seed_id, body FROM imprint_log"):
                self.assertEqual(seed_id, storage._log_seed_id(json.loads(body)), seq)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM seeds WHERE live=0").fetchone()[0], 0)  # 取り下げが live=0 に出る

    def test_derived_columns_use_the_readers_of_alaya(self):
        seed = {"id": 5, "kind": "決定", "project": P, "superseded_by": None, "withdrawn": None}
        self.assertEqual(storage.seed_derived(seed), (5, alaya_mod.seed_kind(seed), P, 1))
        self.assertEqual(storage.seed_derived({"id": True})[0], None)
        self.assertEqual(storage.seed_derived("x"), (None, None, None, 0))
        self.assertEqual(storage.seed_derived(dict(seed, superseded_by=7))[3], 0)
        self.assertEqual(storage.ledger_derived({"id": 3, "project": P, "session_id": "s"}), (3, P, "s"))
        self.assertEqual(storage.ledger_derived({"id": "3"}), (0, None, None))


class TestDiffFlush(_Case):
    def test_untouched_rows_keep_their_bytes_and_a_changed_row_is_rewritten_compactly(self):
        write_state(self.state, {"seeds": [], "alaya_meta": {"life": 1}})
        eng = self.engine()
        eng.imprint([LedgerTurn(user_text="猫が好きです/ 朝に散歩します/ 猫 散歩", assistant_text="", id=1)],
                    [_prop("猫が好き", "猫が好きです/", "猫"), _prop("朝に散歩する", "朝に散歩します/", "散歩")])
        bodies = table_bodies(self.state, "seeds", order="pos")
        spaced = [json.dumps(json.loads(b), ensure_ascii=False, indent=1) for b in bodies]  # 非正準な空白
        mutate(self.state, lambda c: [c.execute("UPDATE seeds SET body=? WHERE pos=?", (t, i + 1)) for i, t in enumerate(spaced)])
        # 1 件目だけ変える (発芽 = 観測欄の更新)。2 件目はバイトのまま。
        self.engine().recall("猫", project=P)
        after = table_bodies(self.state, "seeds", order="pos")
        self.assertNotEqual(after[0], spaced[0])
        self.assertEqual(after[0], storage._dumps(json.loads(after[0])))  # 書いた行は compact
        self.assertEqual(after[1], spaced[1])

    def test_backfilled_keys_are_not_written_until_the_seed_changes_for_another_reason(self):
        write_state(self.state, {"seeds": [], "alaya_meta": {"life": 1}})
        self.engine().imprint([LedgerTurn(user_text="猫が好きです/ 猫", assistant_text="", id=1)], [_prop()])

        def drop_key(seeds):
            del seeds[0]["miss_streak"]

        from tests._state_fixture import edit_seeds
        edit_seeds(self.state, drop_key)
        stripped = table_bodies(self.state, "seeds", order="pos")[0]
        self.engine().recall("全く関係ない", project=P)  # 読んだだけ (読み込み時に補う) なので種子の行は同じ
        self.assertEqual(table_bodies(self.state, "seeds", order="pos")[0], stripped)
        self.assertNotIn("miss_streak", stripped)
        self.engine().recall("猫", project=P)  # 発芽 = 別の理由で行が変わる -> 補った鍵も一緒に書かれる
        self.assertIn("miss_streak", table_bodies(self.state, "seeds", order="pos")[0])

    def test_shrinking_the_seed_list_raises_and_deletes_nothing(self):
        write_state(self.state, {"seeds": [], "alaya_meta": {"life": 1}})
        self.engine().imprint([LedgerTurn(user_text="猫が好きです/ 猫", assistant_text="", id=1)], [_prop()])
        store = alaya_store_at(os.path.join(self.state, "alaya.json"))
        store.state["seeds"].pop()
        before = table_digest(self.state)
        with self.assertRaises(RuntimeError):
            store.commit()
        self.assertEqual(table_digest(self.state), before)

    def test_float_repr_key_order_and_unknown_keys_round_trip(self):
        write_state(self.state, {"seeds": [], "ego_weight": 1.0, "alaya_extra": {"future_key": {"b": 1, "a": 2}},
                                 "manas_extra": {"x": [1.0, 2.5]}, "alaya_meta": {"life": 1}})
        self.engine().recall("x", project=P)
        self.engine().recall("y", project=P)
        with ro(self.state) as conn:
            raw = dict(conn.execute("SELECT key, value FROM meta"))
        self.assertEqual(raw["manas.ego_weight"], "1.0")  # 整数 1 に化けない (列の型名は TEXT)
        self.assertEqual(raw["alaya.extra"], '{"future_key":{"b":1,"a":2}}')  # 鍵順を保つ
        self.assertEqual(raw["manas.extra"], '{"x":[1.0,2.5]}')

    def test_unchanged_meta_is_not_rewritten(self):
        self.populated()
        totals = []
        with mock.patch.object(storage, "READ_CLOSE_HOOK", totals.append):
            self.engine().audit()
        self.assertEqual(set(totals), {0})  # 構築と audit の読む区間 2 つ。どちらも 0

    def test_exception_in_the_op_rolls_back_and_the_stores_are_reloaded(self):
        eng = self.populated()
        before = table_digest(self.state)
        life = eng.alaya.current_life
        with self.assertRaises(ValueError):
            with eng._backend.section(write=True):
                eng.alaya.begin_new_life()
                eng.manas.reduce_ego_weight()
                raise ValueError("x")
        self.assertEqual(table_digest(self.state), before)
        self.assertEqual(eng.alaya.current_life, life)  # メモリも DB から読み直されている
        self.assertEqual(eng.manas.ego_weight, self.engine().manas.ego_weight)

    def test_a_write_section_is_one_transaction_visible_atomically(self):
        eng = self.engine()
        with mock.patch.object(storage, "flush_manas", side_effect=sqlite3.OperationalError("disk full")):
            with mock.patch.dict(storage._FLUSHERS, {manas_mod.ManasStore: storage.flush_manas}):
                before = table_digest(self.state)
                with self.assertRaises(storage.StateUnreadable):
                    eng.begin_new_life()
        self.assertEqual(table_digest(self.state), before)  # 種子側の flush も一緒に巻き戻る


class TestStandaloneStores(_Case):
    def test_commit_is_a_short_self_contained_write_section(self):
        store = alaya_store_at(os.path.join(self.state, "alaya.json"))
        store.begin_new_life()
        self.assertEqual(meta_value(self.state, "alaya.life"), 0)
        store.commit()
        self.assertEqual(meta_value(self.state, "alaya.life"), 1)

    def test_reload_drops_uncommitted_changes(self):
        store = alaya_store_at(os.path.join(self.state, "alaya.json"))
        store.begin_new_life()
        store.reload()
        self.assertEqual(store.current_life, 0)

    def test_stores_are_pure_no_paths_and_no_io_in_init(self):
        import inspect
        for cls in (alaya_mod.AlayaStore, manas_mod.ManasStore):
            self.assertEqual(list(inspect.signature(cls.__init__).parameters), ["self"], cls.__name__)
        from raiya.ledger import LedgerStore
        self.assertEqual(list(inspect.signature(LedgerStore.__init__).parameters), ["self"])
        before = set(os.listdir(self.tmp))
        alaya_mod.AlayaStore()
        manas_mod.ManasStore()
        LedgerStore()
        self.assertEqual(set(os.listdir(self.tmp)), before)

    def test_unloaded_store_cannot_be_committed(self):
        os.makedirs(self.state)
        backend = storage.SqliteBackend(self.state)
        with backend.lock():
            backend.open_stores()
        with self.assertRaises(RuntimeError):
            backend.commit_store(backend.alaya)


class TestBackup(_Case):
    RAW = json.dumps([dict(_prop(), action="new")], ensure_ascii=False)

    def consolidate(self, eng, raw=None, notes=None):
        with mock.patch("raiya.backend.call_llm", return_value=raw or self.RAW):
            return eng.consolidate(notes=notes)

    def bak(self):
        return os.path.join(self.state, "raiya.db.bak")

    def test_no_backup_until_a_consolidation_commits(self):
        eng = self.engine()
        eng.record_turn("猫が好きです/ 猫", "了解", session_id="s")
        eng.recall("猫", project=P)
        eng.imprint([LedgerTurn(user_text="猫が好きです/", assistant_text="", id=1)], [_prop()])
        eng.audit()
        self.assertFalse(os.path.exists(self.bak()))

    def test_backup_is_a_valid_copy_after_a_consolidation_that_wrote(self):
        eng = self.engine()
        eng.record_turn("猫が好きです/ 猫", "了解", session_id="s")
        result = self.consolidate(eng)
        self.assertIsNone(result["skipped"])
        self.assertTrue(os.path.exists(self.bak()))
        self.assertFalse(os.path.exists(self.bak() + ".tmp"))
        conn = sqlite3.connect(self.bak())
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0], storage.APPLICATION_ID)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM seeds").fetchone()[0], 1)
            self.assertEqual(json.loads(conn.execute("SELECT value FROM meta WHERE key='ledger.consolidated_upto'").fetchone()[0]), 1)
        finally:
            conn.close()

    def test_skipped_consolidation_does_not_touch_the_backup(self):
        eng = self.engine()
        eng.record_turn("猫が好きです/ 猫", "了解", session_id="s")

        def race(prompt, **kwargs):
            other = self.engine()
            other.ledger.reload()
            other.ledger.mark_consolidated(1)
            other.ledger.commit()
            return self.RAW

        with mock.patch("raiya.backend.call_llm", side_effect=race):
            result = eng.consolidate()
        self.assertEqual(result["skipped"], "already_consolidated")
        self.assertFalse(os.path.exists(self.bak()))

    def test_empty_window_makes_no_backup(self):
        self.consolidate(self.engine())
        self.assertFalse(os.path.exists(self.bak()))

    def test_backup_is_not_replaced_when_quick_check_is_not_ok(self):
        eng = self.engine()
        eng.record_turn("猫が好きです/ 猫", "了解", session_id="s")
        self.consolidate(eng)
        first = _file_digest(self.state)["raiya.db.bak"]
        eng.record_turn("犬が好きです 犬", "了解", session_id="s")
        notes = []
        with mock.patch.object(storage.SqliteBackend, "quick_check", return_value="bad"):
            self.consolidate(eng, raw="[]", notes=notes)
        self.assertEqual(_file_digest(self.state)["raiya.db.bak"], first)
        self.assertTrue(any("raiya.db.bak" in n for n in notes))

    def test_backup_failure_goes_to_notes_and_does_not_change_the_result(self):
        eng = self.engine()
        eng.record_turn("猫が好きです/ 猫", "了解", session_id="s")
        notes = []
        with mock.patch("os.replace", side_effect=OSError("secret path")):
            result = self.consolidate(eng, notes=notes)
        self.assertIsNone(result["skipped"])
        self.assertEqual(len(result["decisions"]), 1)
        self.assertTrue(any("raiya.db.bak の作成に失敗" in n and "OSError" in n for n in notes))
        self.assertFalse(any("secret path" in n for n in notes))
        self.assertEqual(self.engine().audit()["ledger"]["consolidated_upto"], 1)  # 統合そのものは COMMIT 済み
        self.assertFalse(os.path.exists(self.bak() + ".tmp"))

    def test_second_backup_replaces_the_first(self):
        eng = self.engine()
        eng.record_turn("猫が好きです/ 猫", "了解", session_id="s")
        self.consolidate(eng)
        first = _file_digest(self.state)["raiya.db.bak"]
        eng.record_turn("犬が好きです 犬", "了解", session_id="s")
        self.consolidate(eng, raw="[]")
        self.assertNotEqual(_file_digest(self.state)["raiya.db.bak"], first)


class TestBoundaries(_Case):
    """境界ごとの StateUnreadable の扱い (hooks: exit 0 + 1 行 / MCP: isError / cli・inspect・correct: exit 2)。"""

    def run_module(self, module, args=(), stdin="", extra_env=None):
        env = dict(os.environ, PYTHONPATH=BASE, RAIYA_STATE_DIR=self.state, RAIYA_CONFIG="")
        env.update(extra_env or {})
        return subprocess.run([sys.executable, "-m", module] + list(args), input=stdin, capture_output=True, text=True,
                              env=env, cwd=self.tmp, timeout=60)

    def test_hook_exits_0_with_one_line_in_hooks_log_and_no_output(self):
        self.garbage_db()
        # recall が DB 検査前にキャッシュを書かなくなった (#79) ので、stop は transcript から本文を読む (state dir の外)。
        transcript = os.path.join(self.tmp, "transcript.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "猫の話をします"}}) + "\n")
        for sub in ("recall", "stop", "session_end"):
            with self.subTest(sub=sub):
                payload = json.dumps({"session_id": "s1", "prompt": "猫の話をします", "last_assistant_message": "了解",
                                      "transcript_path": transcript, "cwd": self.tmp})
                r = self.run_module("raiya.hooks.claude_code", [sub], stdin=payload)
                self.assertEqual(r.returncode, 0, msg=r.stderr)
                self.assertEqual(r.stdout, "")
        with open(os.path.join(self.state, "hooks.log"), encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "状態DB 読めず" in ln]
        self.assertEqual(len(lines), 3)
        for ln in lines:
            self.assertTrue(ln.endswith("状態DB 読めず category=not_a_database"), ln)
        with open(os.path.join(self.state, "hooks.log"), encoding="utf-8") as f:
            self.assertNotIn("猫の話", f.read())  # 会話本文は書かない

    def test_hook_does_not_replace_or_repair_the_unreadable_db(self):
        self.garbage_db()
        before = _file_digest(self.state)
        r = self.run_module("raiya.hooks.claude_code", ["stop"], stdin=json.dumps(
            {"session_id": "s1", "prompt": "x", "last_assistant_message": "y"}))
        self.assertEqual(r.returncode, 0)
        after = _file_digest(self.state)
        for name in ("hooks.log", ".lock"):
            after.pop(name, None)
            before.pop(name, None)
        self.assertEqual(after, before)

    def test_inspect_exits_2_and_writes_nothing(self):
        self.garbage_db()
        before = _file_digest(self.state)
        r = self.run_module("raiya.inspect_cli", ["--state-dir", self.state, "show"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("状態DB 読めず category=not_a_database", r.stderr)
        after = _file_digest(self.state)
        after.pop(".lock", None)
        before.pop(".lock", None)
        self.assertEqual(after, before)

    def test_correct_exits_2_and_writes_nothing(self):
        self.garbage_db()
        before = _file_digest(self.state)
        r = self.run_module("raiya.correct_cli", ["--state-dir", self.state, "restore", "1", "--by", "b", "--reason", "r"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("状態DB 読めず category=not_a_database", r.stderr)
        after = _file_digest(self.state)
        after.pop(".lock", None)
        before.pop(".lock", None)
        self.assertEqual(after, before)

    def test_cli_exits_2(self):
        self.garbage_db()
        r = self.run_module("raiya.cli", [], stdin="/終\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn("状態DB 読めず category=not_a_database", r.stderr)

    def test_inspect_and_correct_refuse_a_legacy_dir_without_creating_a_db(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "alaya.json"), "w") as f:
            f.write("{}")
        for module, args in (("raiya.inspect_cli", ["show"]), ("raiya.correct_cli", ["restore", "1", "--by", "b", "--reason", "r"])):
            r = self.run_module(module, ["--state-dir", self.state] + args)
            self.assertEqual(r.returncode, 2, msg=r.stderr)
            self.assertIn("旧 JSON 版", r.stderr)
        self.assertEqual(sorted(os.listdir(self.state)), ["alaya.json"])

    def test_inspect_and_correct_refuse_an_empty_dir_without_creating_a_db(self):
        os.makedirs(self.state)
        for module, args in (("raiya.inspect_cli", ["show"]), ("raiya.correct_cli", ["restore", "1", "--by", "b", "--reason", "r"])):
            r = self.run_module(module, ["--state-dir", self.state] + args)
            self.assertEqual(r.returncode, 2, msg=r.stderr)
            self.assertIn("raiya.db が無い", r.stderr)
        self.assertEqual(os.listdir(self.state), [])

    def test_inspect_in_process_main_returns_2(self):
        self.garbage_db()
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(inspect_cli.main(["--state-dir", self.state, "show"]), 2)
            self.assertEqual(correct_cli.main(["--state-dir", self.state, "restore", "1", "--by", "b", "--reason", "r"]), 2)
        self.assertEqual(err.getvalue().count("状態DB 読めず category=not_a_database"), 2)


class TestReadSections(_Case):
    def test_every_read_verb_closes_with_zero_total_changes(self):
        eng = self.populated()
        totals = []
        with mock.patch.object(storage, "READ_CLOSE_HOOK", totals.append):
            eng.audit()
            eng.introspect_material()
            eng.inspect_seeds()
            eng.injection_log()
            eng.seed_provenance(1)
            eng.explain_recall("猫", project=P)
            eng.imprint_log_rows()
        self.assertGreaterEqual(len(totals), 7)
        self.assertEqual(set(totals), {0})

    def test_read_verbs_create_only_the_empty_lock(self):
        self.populated()
        before = _file_digest(self.state)
        os.remove(os.path.join(self.state, ".lock"))
        eng = self.engine()
        eng.audit()
        eng.inspect_seeds()
        after = _file_digest(self.state)
        self.assertEqual(after, before)  # .lock (空) も含めて同じ
        self.assertEqual(os.path.getsize(os.path.join(self.state, ".lock")), 0)


class TestQaFollowUps(_Case):
    """QA の指摘への pin: 読む区間は差分 snapshot を作らない・manas の dict でない行・create_db の後始末・旧 JSON への hook・ego_weight。"""

    def run_module(self, module, args=(), stdin="", extra_env=None):
        env = dict(os.environ, PYTHONPATH=BASE, RAIYA_STATE_DIR=self.state, RAIYA_CONFIG="")
        env.update(extra_env or {})
        return subprocess.run([sys.executable, "-m", module] + list(args), input=stdin, capture_output=True, text=True,
                              env=env, cwd=self.tmp, timeout=60)

    def test_read_section_does_not_build_diff_snapshot(self):
        self.populated()
        with mock.patch.object(storage, "_canon", wraps=storage._canon) as canon:
            eng = self.engine()
            eng.audit()
            eng.inspect_seeds()
            eng.injection_log(5)
            self.assertEqual(canon.call_count, 0)
        with mock.patch.object(storage, "_canon", wraps=storage._canon) as canon:
            self.engine().recall("猫", project=P)  # 書く区間は従来どおり snapshot を作る
            self.assertGreater(canon.call_count, 0)

    def test_introspect_material_skips_non_dict_manas_log_row(self):
        self.populated()
        for body in ("[1]", "null", '"s"'):
            mutate(self.state, lambda c, b=body: c.execute("INSERT INTO manas_log(body) VALUES (?)", (b,)))
        mutate(self.state, lambda c: c.execute("INSERT INTO introspections(body) VALUES ('[1]')"))
        before = table_digest(self.state)
        mat = self.engine().introspect_material()
        self.assertEqual(mat["manas_warning"].count("4 件"), 1, mat["manas_warning"])
        self.assertTrue(all(isinstance(e, dict) for e in mat["manas_log"]))
        self.assertEqual(table_digest(self.state), before)

    def test_introspect_material_one_non_dict_row_counts_one(self):
        self.populated()
        mutate(self.state, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('[1]')"))
        mat = self.engine().introspect_material()
        self.assertIn("読めない行 1 件", mat["manas_warning"])
        self.assertEqual([b for b in table_bodies(self.state, "manas_log", order="seq")].count("[1]"), 1)

    def test_commit_after_read_only_verb_raises_clear_error(self):
        self.populated()
        eng = self.engine()
        eng.audit()
        before = table_digest(self.state)
        with self.assertRaises(RuntimeError) as cm:
            eng.alaya.commit()
        self.assertNotIsInstance(cm.exception, KeyError)
        self.assertEqual(table_digest(self.state), before)

    def test_create_db_failure_leaves_no_tmp_file_and_propagates(self):
        os.makedirs(self.state)
        with mock.patch("raiya.storage.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                storage.create_db(self.state)
        self.assertEqual([n for n in os.listdir(self.state) if ".tmp-" in n], [])
        self.assertFalse(os.path.exists(db_path(self.state)))

    def test_hook_on_legacy_state_writes_nothing_and_one_stderr_line(self):
        # P12 / #79: 旧 JSON 版は hooks.log・.lock・sessions/ より前に止まる。残るのは元のファイルだけ。
        os.makedirs(self.state)
        with open(os.path.join(self.state, "alaya.json"), "w") as f:
            f.write("{}")
        transcript = os.path.join(self.tmp, "transcript.jsonl")  # state dir の外
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "猫の話をします"}}) + "\n")
        before = _file_digest(self.state)
        for sub in ("stop", "recall", "session_end"):
            payload = json.dumps({"session_id": "s1", "prompt": "猫の話をします", "last_assistant_message": "了解犬",
                                  "transcript_path": transcript, "cwd": self.tmp})
            r = self.run_module("raiya.hooks.claude_code", [sub], stdin=payload)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(r.stdout, "")
            self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
            self.assertIn("旧 JSON 版の state (移行しない。脇に退けてから起動)", r.stderr)
            self.assertNotIn("猫の話", r.stderr)
            self.assertNotIn(self.state, r.stderr)
        self.assertEqual(sorted(os.listdir(self.state)), ["alaya.json"])
        self.assertEqual(_file_digest(self.state), before)

    def test_hook_legacy_check_comes_after_recursion_guard(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "alaya.json"), "w") as f:
            f.write("{}")
        r = self.run_module("raiya.hooks.claude_code", ["recall"], stdin="{}", extra_env={"RAIYA_HOOK_ACTIVE": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_first_prompt_on_truncated_db_is_still_first_after_restore(self):
        # #79 / 不変条件 7: 読めない DB での最初のプロンプトは sessions/<id>.json を作らない=最初のまま。
        # DB を戻したあとの最初の 1 回だけ生が進み、その後のプロンプトでは進まない。
        self.populated()
        path = db_path(self.state)
        with open(path, "rb") as f:
            good = f.read()
        life0 = self.engine(new_life=False).life
        with open(path, "wb") as f:
            f.write(good[:4096])
        cache = os.path.join(self.state, "sessions", "s7.json")
        payload = json.dumps({"session_id": "s7", "prompt": "猫の話をします", "cwd": self.tmp})
        r = self.run_module("raiya.hooks.claude_code", ["recall"], stdin=payload)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertFalse(os.path.exists(cache))
        with open(path, "wb") as f:
            f.write(good)
        r = self.run_module("raiya.hooks.claude_code", ["recall"], stdin=payload)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertTrue(os.path.exists(cache))
        r = self.run_module("raiya.hooks.claude_code", ["recall"], stdin=payload)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(self.engine(new_life=False).life, life0 + 1)

    def test_recall_on_zero_byte_db_logs_not_a_database_and_writes_nothing_else(self):
        os.makedirs(self.state)
        open(db_path(self.state), "wb").close()
        payload = json.dumps({"session_id": "s8", "prompt": "猫の話をします", "cwd": self.tmp})
        r = self.run_module("raiya.hooks.claude_code", ["recall"], stdin=payload)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))
        self.assertEqual(os.path.getsize(db_path(self.state)), 0)
        with open(os.path.join(self.state, "hooks.log"), encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("状態DB 読めず category=not_a_database"), lines[0])

    def test_recall_on_unreadable_db_does_not_create_sessions(self):
        # P11 / #79: engine 構築 (DB 検査) がキャッシュ書込より前。truncated な DB でも sessions/ は作らない。
        self.populated()
        path = db_path(self.state)
        with open(path, "rb") as f:
            head = f.read(4096)
        with open(path, "wb") as f:
            f.write(head)
        shutil.rmtree(os.path.join(self.state, "sessions"), ignore_errors=True)  # 自作 tmp 配下のみ
        before = _file_digest(self.state)
        payload = json.dumps({"session_id": "s9", "prompt": "猫の話をします", "cwd": self.tmp})
        r = self.run_module("raiya.hooks.claude_code", ["recall"], stdin=payload)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))
        after = _file_digest(self.state)
        after.pop(".lock", None)
        after.pop("hooks.log", None)
        before.pop(".lock", None)
        self.assertEqual(after, before)
        with open(os.path.join(self.state, "hooks.log"), encoding="utf-8") as f:
            self.assertIn("状態DB 読めず category=", f.read())

    def test_ego_weight_bool_or_string_is_bad_meta(self):
        self.populated()
        for value in (True, "x", [1]):
            with self.subTest(value=value):
                set_meta(self.state, "manas.ego_weight", value)
                before = _file_digest(self.state)
                with self.assertRaises(storage.StateUnreadable) as cm:
                    self.engine()
                self.assertEqual(cm.exception.category, "bad_meta")
                after = _file_digest(self.state)
                self.assertEqual(sorted(after), sorted(before))
                self.assertEqual(after[storage.DB_NAME], before[storage.DB_NAME])
                self.assertEqual([n for n in os.listdir(self.state) if n.endswith(("-journal", "-wal", "-shm"))], [])


if __name__ == "__main__":
    unittest.main()
