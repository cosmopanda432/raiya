"""読めない行がある DB に対する hook の往復と engine の動詞 (#38 スライス 4)。

sqlite 版は行単位の失敗を読み飛ばして数えるだけで、行を保持用に退避も書き直しもしない (JSON 版の `.corrupt-<ts>` 退避は退役)。
- hook の recall -> stop -> session_end は "ok" で終わり、dir には raiya.db (+ consolidate 後の .bak) / .lock / sessions / hooks.log しか増えない。
- 壊れた本文はバイト単位で同じまま残る。
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

from raiya.engine import MemoryEngine  # noqa: E402
from raiya.hooks import claude_code as hooks  # noqa: E402
from tests._state_fixture import mutate, table_bodies  # noqa: E402

BROKEN = "{broken"
PROMPT = "猫が好きだという話をこれからします"
# raiya.db.bak は consolidate (session_end) 後の正規のバックアップ。*corrupt* は決して作られない。
ALLOWED = {"raiya.db", "raiya.db.bak", ".lock", "sessions", "hooks.log"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="raiya-unreadable-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)  # 自作 tmp のみ
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["RAIYA_CONFIG"] = ""
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RAIYA_OPENAI_API_KEY"):  # 実 LLM に届かせない二重の守り
            os.environ.pop(k, None)

        def go(conn):
            conn.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, ?)", (BROKEN,))
            conn.execute("INSERT INTO manas_log(body) VALUES (?)", (BROKEN,))
            conn.execute("INSERT INTO imprint_log(seed_id, body) VALUES (NULL, ?)", (BROKEN,))

        MemoryEngine(self.dir, new_life=False)  # DB を作る
        mutate(self.dir, go)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def assert_unchanged_and_no_side_files(self):
        for table, order in (("seeds", "pos"), ("manas_log", "rowid"), ("imprint_log", "rowid")):
            self.assertEqual([b for b in table_bodies(self.dir, table, order=order) if b == BROKEN], [BROKEN], table)
        extra = set(os.listdir(self.dir)) - ALLOWED
        self.assertEqual(extra, set())
        self.assertEqual([n for n in os.listdir(self.dir) if "corrupt" in n], [])


class TestHookCycleOverUnreadableRows(_Base):
    def test_recall_stop_session_end_ok_and_rows_untouched(self):
        from raiya import backend

        # consolidate (session_end) は call_llm を 1 回呼ぶ。実 LLM・ネット・subprocess へは出さない。
        with mock.patch.object(backend, "call_llm", return_value="[]") as llm, \
                mock.patch.object(backend.urllib.request, "urlopen", side_effect=AssertionError("network")), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hooks.cmd_recall({"prompt": PROMPT, "session_id": "s1"}, self.dir), "ok")
            self.assertEqual(hooks.cmd_stop({"session_id": "s1", "last_assistant_message": "了解しました"}, self.dir), "ok")
            self.assertEqual(hooks.cmd_session_end({"session_id": "s1"}, self.dir), "ok")
        self.assertEqual(llm.call_count, 1)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "raiya.db.bak")))
        with open(os.path.join(self.dir, "hooks.log"), encoding="utf-8") as f:
            self.assertNotIn("consolidate失敗", f.read())
        self.assert_unchanged_and_no_side_files()


class TestEngineOverUnreadableRows(_Base):
    def test_recall_new_life_record_turn_leave_rows_and_dir_alone(self):
        from raiya import backend

        # recall / begin_new_life / record_turn は LLM を呼ばない (呼べば失敗させる)。
        with mock.patch.object(backend, "call_llm", side_effect=AssertionError("llm")):
            eng = MemoryEngine(self.dir, new_life=False)
            eng.recall("猫の話")
            eng.begin_new_life()
            eng.record_turn("猫が好きです", "了解", session_id="s")
        self.assert_unchanged_and_no_side_files()


if __name__ == "__main__":
    unittest.main()
