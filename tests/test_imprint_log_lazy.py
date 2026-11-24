"""#36 / #37: recall は imprint_log を読まず書かず、manas を 1 回だけ書く。

契約 (ストレージ実装に依らない): recall は imprint log に何も足さない / imprint は提案 1 件につき 1 行足す /
読みは古い順 / 動詞をまたいだキャッシュは無い (他の engine の追記が次の動詞で見える) / 何も消えない。

保存層 sqlite3 化 (#38) スライス 3: JSON 実装の pin (ファイルのバイト・mtime・`.bak` 復旧・書き直し) は
imprint_log 表の pin に置き換えた: 追記専用トリガ、読めない行は読み飛ばして警告 (行は書き直さない・消えない)、
recall / audit / inspect は表を 1 行も変えない。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import inspect_cli  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import alaya_store_at, mutate, ro, table_bodies, table_digest  # noqa: E402

P = "/home/dev/p"


def _seed(seed_id, conditions):
    return {
        "id": seed_id, "content": "種子{}".format(seed_id), "strength": 1, "conditions": list(conditions),
        "polarity": "無記", "sansho": "圓成實", "kind": "所在", "project": P, "superseded_by": None,
        "evidences": [], "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0,
    }


def _prop(span="猫が好きです/"):
    return {"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
            "evidence": [{"source": "user", "span": span}]}


def _turn():
    return LedgerTurn(user_text="猫が好きです/ 猫", assistant_text="")


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_lazy_")
        self.state = os.path.join(self.tmpdir, "state")
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)

    def write_state(self, seeds, log_rows):
        write_state(self.state, {"seeds": seeds, "alaya_meta": {"life": 3, "next_id": len(seeds) + 1},
                                 "imprint_log": log_rows})

    def log_bodies(self):
        return table_bodies(self.state, "imprint_log")


class TestRecallDoesNotTouchImprintLog(_Case):
    def _rows(self, eng):
        return eng.alaya.list_imprint_log()

    def test_recall_leaves_log_rows_unchanged_germinating_and_not(self):
        self.write_state([_seed(1, ["猫"])], [{"life": 1, "decision": "圓成實", "seed_id": 1}])
        for text, fires in (("猫の話", True), ("全く関係ない", False)):
            eng = self.engine()
            before = self._rows(eng)
            digest = table_digest(self.state)["imprint_log"]
            out = eng.recall(text, project=P)
            self.assertEqual(bool(out["seeds"]), fires)
            self.assertEqual(self._rows(self.engine()), before)
            self.assertEqual(table_digest(self.state)["imprint_log"], digest)

    def test_recall_persists_one_manas_row_in_one_write_section(self):
        self.write_state([_seed(1, ["猫"])], [])
        eng = self.engine()
        totals_before = eng._backend.timing_totals["sections"]
        eng.recall("猫の話", project=P, session_id="s1")
        self.assertEqual(eng._backend.timing_totals["sections"] - totals_before, 1)
        log = self.engine().injection_log(10)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["session_id"], "s1")
        self.engine().recall("猫の話", project=P)
        self.assertEqual(len(self.engine().injection_log(10)), 2)


class TestImprintLogTable(_Case):
    def test_standalone_store_commit_never_touches_existing_rows(self):
        self.write_state([_seed(1, ["猫"])], [{"life": 1, "decision": "圓成實", "seed_id": 1}])
        before = self.log_bodies()
        store = alaya_store_at(os.path.join(self.state, "alaya.json"))  # 行を読まないまま書く区間を回す
        store.commit()
        self.assertEqual(self.log_bodies(), before)

    def test_a_fresh_state_has_an_empty_imprint_log_table(self):
        self.engine()
        self.assertEqual(self.log_bodies(), [])
        self.engine().recall("x", project=P)
        self.assertEqual(self.log_bodies(), [])

    def test_rows_cannot_be_updated_or_deleted(self):
        self.write_state([], [{"life": 1, "decision": "reject"}])
        with ro(self.state) as conn:  # ro 接続は書けない
            import sqlite3
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM imprint_log")
        import sqlite3

        from raiya import storage
        conn = sqlite3.connect(os.path.join(self.state, storage.DB_NAME))
        try:
            for sql in ("UPDATE imprint_log SET body='{}'", "DELETE FROM imprint_log"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
        finally:
            conn.close()
        self.assertEqual(len(self.log_bodies()), 1)


class TestImprintLogContract(_Case):
    def test_imprint_appends_one_row_per_proposal_and_persists(self):
        eng = self.engine()
        eng.imprint(_turn(), [_prop(), _prop("犬が好きです")], project=P)
        rows = self.engine().alaya.list_imprint_log()
        self.assertEqual([r["decision"] for r in rows], ["圓成實", "遍計所執"])
        self.engine().imprint(_turn(), [_prop()], project=P)
        self.assertEqual(len(self.engine().alaya.list_imprint_log()), 3)
        self.assertEqual(len(self.engine().alaya.list_imprint_log(n=2)), 2)

    def test_other_engines_append_is_visible_to_next_verb(self):
        long_lived = self.engine()
        long_lived.imprint(_turn(), [_prop()], project=P)
        self.assertEqual(len(long_lived.audit()["imprint_log_tail"]), 1)
        self.engine().imprint(_turn(), [_prop("犬が好きです")], project=P)  # 別プロセス相当
        tail = long_lived.audit()["imprint_log_tail"]
        self.assertEqual([r["decision"] for r in tail], ["圓成實", "遍計所執"])
        long_lived.imprint(_turn(), [_prop()], project=P)  # 古いキャッシュで上書きしない
        self.assertEqual(len(self.engine().alaya.list_imprint_log()), 3)

    def test_seed_provenance_decisions_and_rows_for_seed(self):
        eng = self.engine()
        res = eng.imprint(_turn(), [_prop(), _prop("犬が好きです")], project=P)
        sid = res[0]["seed"]["id"]
        prov = self.engine().seed_provenance(sid)
        self.assertEqual([d["decision"] for d in prov["decisions"]], ["圓成實"])
        rows = self.engine().alaya.imprint_log_rows_for_seed(sid)
        self.assertEqual([r["seed_id"] for r in rows], [sid])
        self.assertEqual(self.engine().alaya.imprint_log_rows_for_seed(999), [])

    def test_seed_id_column_is_derived_from_the_body(self):
        eng = self.engine()
        res = eng.imprint(_turn(), [_prop(), _prop("犬が好きです")], project=P)
        sid = res[0]["seed"]["id"]
        with ro(self.state) as conn:
            self.assertEqual(conn.execute("SELECT seed_id FROM imprint_log ORDER BY seq").fetchall(), [(sid,), (None,)])


class TestUnreadableRowVisibility(_Case):
    """読めない行 (JSON として読めない body) は読み飛ばして警告に数える。行は書き直さず、消えない。"""

    def _two_imprints(self):
        eng = self.engine()
        eng.imprint(_turn(), [_prop()], project=P)
        eng.imprint(_turn(), [_prop("犬が好きです")], project=P)
        mutate(self.state, lambda c: c.execute("INSERT INTO imprint_log(seed_id, body) VALUES (NULL, '{broken')"))

    def test_audit_reports_the_unreadable_row_every_time_and_writes_nothing(self):
        self._two_imprints()
        digest = table_digest(self.state)
        for _ in range(2):
            rep = self.engine().audit()
            self.assertIn("imprint_log の読めない行 1 件", rep["imprint_log_warning"])
            self.assertEqual([r["decision"] for r in rep["imprint_log_tail"]], ["圓成實", "遍計所執"])
            self.assertEqual(table_digest(self.state), digest)

    def test_imprint_after_audit_appends_and_leaves_the_bad_row(self):
        self._two_imprints()
        self.engine().audit()
        self.engine().imprint(_turn(), [_prop("猫が好きです/")], project=P)
        bodies = self.log_bodies()
        self.assertEqual(len(bodies), 4)
        self.assertEqual(bodies[2], "{broken")  # 追記は末尾、壊れた行は元の位置のまま
        self.assertIn("読めない行", self.engine().audit()["imprint_log_warning"])

    def test_why_prints_imprint_log_warning_once(self):
        self._two_imprints()
        sid = self.engine().alaya.list_seeds()[0]["id"]
        # why は seed_id 列で絞って読む: 警告に数えられるのは、その種子の行 (seed_id 列がその種子) の読めない body。
        mutate(self.state, lambda c: c.execute("INSERT INTO imprint_log(seed_id, body) VALUES (?, '{broken2')", (sid,)))
        digest = table_digest(self.state)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inspect_cli.main(["--state-dir", self.state, "why", str(sid)])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue().count("imprint_log の読めない行"), 1)
        self.assertEqual(table_digest(self.state), digest)  # 観測は書かない

    def test_recall_does_not_read_or_rewrite_the_log(self):
        self._two_imprints()
        digest = table_digest(self.state)["imprint_log"]
        out = self.engine().recall("猫の話", project=P)
        self.assertIsNone(out["alaya_warning"])
        self.assertEqual(table_digest(self.state)["imprint_log"], digest)

    def test_op_exception_after_append_does_not_leak_pending_rows(self):
        self._two_imprints()
        before = self.log_bodies()
        real = alaya_mod.AlayaStore.append_imprint_log

        def boom(store, entry):
            real(store, entry)
            raise RuntimeError("boom")

        eng = self.engine()
        with mock.patch.object(alaya_mod.AlayaStore, "append_imprint_log", boom):
            with self.assertRaises(RuntimeError):
                eng.imprint(_turn(), [_prop()], project=P)
        eng.recall("猫の話", project=P)  # 同じ engine。持ち越された追記があっても recall はログを書かない
        self.assertEqual(self.log_bodies(), before)
        self.assertEqual(len(eng.audit()["imprint_log_tail"]), 2)  # 幻の行は無い


if __name__ == "__main__":
    unittest.main()
