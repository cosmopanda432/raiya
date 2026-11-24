"""lock.py -- 複数の MemoryEngine インスタンス/プロセスが同じ RAIYA_STATE_DIR を
安全に共有するためのファイルロック(記憶機能設計書 波4・多重書き手対策)。

背景: MCP サーバ(セッションごとに長命の MemoryEngine を1個)と Claude Code hooks
(短命プロセス。呼ばれるたびに新規 MemoryEngine)が同じ RAIYA_STATE_DIR を書くと、
古い in-memory state で新しい書込を上書きしてしまう恐れがある。`engine.py` の
公開動詞は「lock 取得 → ディスクから再読込 → 操作 → save → 解放」の順で実行し、
この待ち合わせを `StateLock` (`state_dir/.lock` 上の `fcntl.flock`) が担う。

標準ライブラリのみ。`fcntl` が無い OS (Windows 等) では no-op にフォールバックし、
プロセスにつき1回だけ警告する(単一書き手前提=旧来の挙動に戻るだけで、動作は壊さない)。
"""
import os
import sys

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover -- このリポの CI は POSIX のみ
    fcntl = None
    _HAS_FCNTL = False

_warned = False

LOCK_FILE_NAME = ".lock"


def _warn_once():
    global _warned
    if not _warned:
        print(
            "[raiya] 警告: fcntl が無い環境のためファイルロックを無効化します"
            "(単一書き手前提に戻ります)。",
            file=sys.stderr,
        )
        _warned = True


class StateLock:
    """`state_dir/.lock` を使ったプロセス間排他ロック (flock LOCK_EX)。

    `with StateLock(state_dir):` の間、同じ state_dir を指す他プロセス/他
    インスタンスの StateLock はブロックされる。`fcntl` が使えない環境では
    no-op (警告1回のみ) にフォールバックする。
    """

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.lock_path = os.path.join(state_dir, LOCK_FILE_NAME)
        self._fh = None

    def __enter__(self):
        if not _HAS_FCNTL:
            _warn_once()
            return self
        self._fh = open(self.lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False
