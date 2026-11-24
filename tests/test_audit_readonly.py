"""#64 / #65: audit は読むだけ (何も書かない)。recall / audit は per-store の警告キーを返す。

保存層 sqlite3 化 (#38) スライス 3: 読めない行 (行単位の失敗) は読み飛ばして該当ストアの警告に数え、DB の行は
書き直さない。audit だけが `PRAGMA quick_check` を走らせ、ok でなければ `alaya_warning` に出す。DB 単位の失敗
(必須 meta の欠落など) は `StateUnreadable` で fail-closed (audit は何も書かない)。tmp dir は自作のものだけを消す。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import storage  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import mutate, table_bodies, table_digest  # noqa: E402

P = "/home/dev/p"


def _digest(path):
    out = {}
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_audit_ro_")
        self.state = os.path.join(self.tmpdir, "state")
        os.makedirs(self.state)
        p = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)  # 自作 tmp のみ

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)

    def populate(self):
        eng = self.engine()
        rec = eng.record_turn("猫が好きです 猫", "了解", session_id="s1", project=P)
        turn = LedgerTurn(user_text="猫が好きです 猫", assistant_text="了解", id=rec["ledger_id"], project=P)
        eng.imprint([turn], [{"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
                              "evidence": [{"source": "user", "span": "猫が好きです"}]}])
        eng.recall("猫", project=P)  # manas_log に 1 行作る

    def add_bad_seed_row(self):
        mutate(self.state, lambda c: c.execute(
            "INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, '{壊れた')"))


class TestAuditReadOnly(_Case):
    def test_audit_with_an_unreadable_seed_row_writes_nothing_and_reports_every_time(self):
        self.populate()
        self.add_bad_seed_row()
        before = _digest(self.state)
        before_tables = table_digest(self.state)
        r1 = self.engine().audit()
        self.assertIn("読めない行", r1["alaya_warning"])
        self.assertEqual(_digest(self.state), before)
        r2 = self.engine().audit()
        self.assertIn("読めない行", r2["alaya_warning"])
        self.assertEqual(_digest(self.state), before)
        self.assertEqual(table_digest(self.state), before_tables)

    def test_audit_on_healthy_state_leaves_every_byte(self):
        self.populate()
        before = _digest(self.state)
        totals = []
        with mock.patch.object(storage, "READ_CLOSE_HOOK", totals.append):
            r = self.engine().audit()
        self.assertIsNone(r["alaya_warning"])
        self.assertIsNone(r["manas_warning"])
        self.assertIsNone(r["ledger_warning"])
        self.assertIsNone(r["imprint_log_warning"])
        self.assertEqual(set(totals), {0})
        self.assertEqual(_digest(self.state), before)

    def test_recall_reports_the_unreadable_row_warning_and_still_leaves_it_alone(self):
        self.populate()
        self.add_bad_seed_row()
        r = self.engine().recall("x")
        self.assertIn("読めない行", r["alaya_warning"])
        self.assertIsNone(r["manas_warning"])
        self.assertIn("読めない行", self.engine().recall("x")["alaya_warning"])  # 書き直さないので毎回出る
        self.assertIn("{壊れた", table_bodies(self.state, "seeds", order="pos"))

    def test_fresh_dir_audit_creates_only_lock_and_the_db(self):
        self.engine().begin_new_life()
        before = _digest(self.state)
        self.engine().audit()
        self.assertEqual(sorted(os.listdir(self.state)), sorted(list(before) + ([] if ".lock" in before else [".lock"])))
        after = _digest(self.state)
        self.assertEqual({k: v for k, v in after.items() if k != ".lock"}, {k: v for k, v in before.items() if k != ".lock"})
        self.assertEqual(sorted(k for k in after if k != ".lock"), ["raiya.db"])

    def test_missing_ledger_meta_key_is_unreadable_not_rebuilt(self):
        # 旧 JSON は meta 欠落を行から再構成して consolidated_upto=0 に倒した。sqlite 版は DB 単位の失敗 = fail-closed。
        self.populate()
        mutate(self.state, lambda c: c.execute("DELETE FROM meta WHERE key='ledger.consolidated_upto'"))
        before = _digest(self.state)
        with self.assertRaises(storage.StateUnreadable) as cm:
            self.engine().audit()  # 必須 meta は全部、どのストアを読む区間でも検査する
        self.assertEqual(cm.exception.category, "missing_meta")
        self.assertEqual(_digest(self.state), before)

    def test_corrupt_manas_row_audit_writes_nothing_and_warns(self):
        self.populate()
        mutate(self.state, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('{bad')"))
        before = _digest(self.state)
        r = self.engine().audit()
        self.assertTrue(r["manas_warning"])
        self.assertEqual(_digest(self.state), before)
        self.assertTrue(self.engine().audit()["manas_warning"])
        self.assertTrue(self.engine().recall("x", project=P)["manas_warning"])

    def test_audit_reports_quick_check_failure_in_alaya_warning(self):
        self.populate()
        with mock.patch.object(storage.SqliteBackend, "quick_check", return_value="状態DB の整合性検査 (quick_check) が ok ではありません: x"):
            r = self.engine().audit()
        self.assertIn("quick_check", r["alaya_warning"])

    def test_audit_runs_a_real_quick_check_that_is_ok_on_a_healthy_db(self):
        self.populate()
        eng = self.engine()
        seen = []
        real = storage.SqliteBackend.quick_check

        def spy(backend):
            seen.append(real(backend))
            return seen[-1]

        with mock.patch.object(storage.SqliteBackend, "quick_check", spy):
            eng.audit()
        self.assertEqual(seen, [None])  # None = ok


class TestIntrospectMaterialReadOnly(_Case):
    """#71: introspect_material は読むだけ。警告は audit と同じ意味で毎回返す。"""

    def test_healthy_state_leaves_every_byte_and_creates_only_lock(self):
        self.populate()
        before = _digest(self.state)
        r = self.engine().introspect_material()
        self.assertTrue(r["manas_log"])
        self.assertIsNone(r["alaya_warning"])
        self.assertIsNone(r["manas_warning"])
        after = _digest(self.state)
        self.assertEqual({k: v for k, v in after.items() if k != ".lock"}, {k: v for k, v in before.items() if k != ".lock"})
        self.assertEqual(sorted(set(after) - set(before)), [] if ".lock" in before else [".lock"])

    def test_unreadable_seed_row_writes_nothing_and_warns_every_call(self):
        self.populate()
        self.add_bad_seed_row()
        before = _digest(self.state)
        for _ in range(2):
            r = self.engine().introspect_material()
            self.assertIn("読めない行", r["alaya_warning"])
            self.assertEqual(_digest(self.state), before)

    def test_corrupt_manas_row_writes_nothing_and_warns(self):
        self.populate()
        mutate(self.state, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('{bad')"))
        before = _digest(self.state)
        for _ in range(2):
            r = self.engine().introspect_material()
            self.assertTrue(r["manas_warning"])
            self.assertEqual(_digest(self.state), before)


if __name__ == "__main__":
    unittest.main()
