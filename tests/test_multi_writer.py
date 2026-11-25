import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from raiya import lock  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.sansho import Turn  # noqa: E402
from tests._state_fixture import ro, table_bodies  # noqa: E402

# 内容・条件がお互いに全く似ていない話題 (2-gram Jaccard が MERGE_SIMILARITY_THRESHOLD
# を超えて誤併合しないよう、意図的に語彙を大きく違えてある)。
TOPICS = [
    ("コーヒーが好きです", "コーヒー"),
    ("猫を飼っています", "猫"),
    ("ハイキングによく行きます", "ハイキング"),
    ("辛い料理が好きです", "辛い料理"),
    ("京都に旅行しました", "京都"),
    ("Pythonをよく書きます", "Python"),
    ("読書が趣味です", "読書"),
    ("ジャズを聴きます", "ジャズ"),
]

_IMPRINT_ONCE_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from raiya import MemoryEngine
from raiya.sansho import Turn

state_dir = sys.argv[2]
user_text = sys.argv[3]
condition = sys.argv[4]

engine = MemoryEngine(state_dir=state_dir)
# #149: 所在 の形 (`/` を含む語) を足す
user_text = user_text + " p/"
turn = Turn(user_text=user_text, assistant_text="")
proposal = {
    "content": user_text[:-3],
    "conditions": [condition],
    "polarity": "無記","kind": "所在",
    "evidence": {"source": "user", "span": user_text},
}
engine.imprint(turn, [proposal])
"""


class TestMultiWriterSameProcess(unittest.TestCase):
    """(1) 同一 dir の engine A/B: A.imprint の書込が B.audit に反映されること。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_engine_b_sees_engine_a_imprint_on_next_verb_call(self):
        engine_a = MemoryEngine(state_dir=self.tmpdir)
        engine_b = MemoryEngine(state_dir=self.tmpdir)

        # engine_b 構築時点 (imprint 前) の audit はまだ0件 (前提の確認)。
        self.assertEqual(engine_b.audit()["seed_count"], 0)

        turn = Turn(user_text="コーヒーが好きです p/", assistant_text="")
        proposal = {
            "content": "コーヒーが好き",
            "conditions": ["コーヒー"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "コーヒーが好きです p/"},
        }
        engine_a.imprint(turn, [proposal])

        # engine_b は構築後に一度も reload していないはずだが、audit() 自体が
        # 内部で reload するため、他インスタンス (engine_a) の書込が見える。
        audit = engine_b.audit()
        self.assertEqual(audit["seed_count"], 1)
        self.assertEqual(audit["class_counts"]["圓成實"], 1)

    def test_engine_a_recall_sees_engine_b_imprint(self):
        engine_a = MemoryEngine(state_dir=self.tmpdir)
        engine_b = MemoryEngine(state_dir=self.tmpdir)

        turn = Turn(user_text="猫を飼っています p/", assistant_text="")
        proposal = {
            "content": "猫を飼っている",
            "conditions": ["猫"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "猫を飼っています p/"},
        }
        engine_b.imprint(turn, [proposal])
        # 圓成實は 1 件で発芽する (改修4)。2 回目の熏習は併合の回帰確認。
        engine_b.imprint(turn, [proposal])

        recalled = engine_a.recall("猫の話をしたい")
        contents = [s["content"] for s in recalled["seeds"]]
        self.assertIn("猫を飼っている", contents)


class TestMultiWriterAcrossProcesses(unittest.TestCase):
    """(2) 複数プロセスで同時 imprint N 件 -> 件数が合い DB が壊れない。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmpdir, "state")
        self.script_path = os.path.join(self.tmpdir, "_imprint_once.py")
        with open(self.script_path, "w", encoding="utf-8") as f:
            f.write(_IMPRINT_ONCE_SCRIPT)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_processes_imprinting_do_not_corrupt_or_drop_writes(self):
        n = len(TOPICS)
        procs = []
        for user_text, condition in TOPICS:
            procs.append(
                subprocess.Popen(
                    [sys.executable, self.script_path, REPO_ROOT, self.state_dir, user_text, condition]
                )
            )
        for p in procs:
            rc = p.wait(timeout=60)
            self.assertEqual(rc, 0)

        # DB が壊れていないこと (quick_check == ok・全本文が parse できる = 破損した書込が無い)。
        with ro(self.state_dir) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchall(), [("ok",)])
        log_data = {"imprint_log": [json.loads(b) for b in table_bodies(self.state_dir, "imprint_log")]}
        alaya_data = {"seeds": [json.loads(b) for b in table_bodies(self.state_dir, "seeds", order="pos")]}

        # 件数が合うこと (N プロセス全員の熏習が取りこぼされず記帳されている)。
        # imprint_log は追記専用 (KUN-1 剎那滅) で件数は単調増加のみ。判定は決定を
        # 保持する imprint_log の件数・内容で行う。
        self.assertEqual(len(log_data["imprint_log"]), n)
        logged_contents = [entry["proposal"]["content"] for entry in log_data["imprint_log"]]
        self.assertEqual(sorted(logged_contents), sorted(t[0] for t in TOPICS))
        # 全プロセスの提案が破損・取り違えなく同じ decision (圓成實) で記帳されている
        # こと (competing プロセスの書込がお互いの proposal/decision を上書きしていない)。
        decisions = {entry["decision"] for entry in log_data["imprint_log"]}
        self.assertEqual(decisions, {"圓成實"})
        # seeds 表にも N 件全てが種子として残っていること。種子は削除も減衰もされない
        # (KUN-3 恒隨轉。旧来の枯死(kusshi)判定は改修4 で撤去済みなので、begin_new_life()
        # が life を何度進めても種子は消えない)。TOPICS は意図的に非類似な語彙
        # (2-gram Jaccard が MERGE_SIMILARITY_THRESHOLD を超えない) なので誤併合も
        # 起きず、N プロセス=N 種子が厳密に成立する。imprint_log の件数一致とは独立した、
        # 書込の取りこぼしが無いことのもう1つの確認軸として厳密な等号で確認する。
        self.assertIsInstance(alaya_data["seeds"], list)
        self.assertEqual(len(alaya_data["seeds"]), n)


class TestLockFileLocation(unittest.TestCase):
    """(3) lock ファイルが state_dir 内に作られること。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_lock_file_is_created_inside_state_dir_after_a_verb_call(self):
        # MemoryEngine.__init__ 自体が begin_new_life() で save() するため、construction
        # 自体も lock で保護している (= .lock はここで既に作られる)。念のため、明示的な
        # 動詞呼び出し (audit) の後にも存在し続けることを確認する。
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, ".lock")))
        engine = MemoryEngine(state_dir=self.tmpdir)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, ".lock")))
        engine.audit()
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, ".lock")))

    def test_gitignore_and_state_manual_mention_the_lock_file(self):
        with open(os.path.join(REPO_ROOT, ".gitignore"), "r", encoding="utf-8") as f:
            gitignore = f.read()
        with open(os.path.join(REPO_ROOT, "manual", "state.md"), "r", encoding="utf-8") as f:
            state_manual = f.read()
        self.assertIn(".lock", gitignore)
        self.assertIn(".lock", state_manual)


class TestStateLockFallbackWithoutFcntl(unittest.TestCase):
    """fcntl が無い環境では no-op + 警告1回にフォールバックすること。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_has_fcntl = lock._HAS_FCNTL
        self._saved_warned = lock._warned

    def tearDown(self):
        lock._HAS_FCNTL = self._saved_has_fcntl
        lock._warned = self._saved_warned
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_fcntl_fallback_is_noop_and_warns_exactly_once(self):
        lock._HAS_FCNTL = False
        lock._warned = False
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with lock.StateLock(self.tmpdir):
                pass
            with lock.StateLock(self.tmpdir):
                pass
        output = buf.getvalue()
        self.assertEqual(output.count("fcntl が無い環境"), 1)
        # ロックファイル自体も作らない (no-op)。
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, ".lock")))


if __name__ == "__main__":
    unittest.main()
