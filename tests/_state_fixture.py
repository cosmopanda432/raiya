"""テスト fixture 用: state dir の永続ファイルを消してから `snapshot.write_state` で書き直すための補助。

`write_state` は既存の state を拒否する (記憶を上書きしない) ので、同じテストが fixture を書き直すときは
テスト側でこれを呼ぶ。パッケージ (raiya/) には置かない: 永続層のファイルを消す経路をコアに持たないため。
消すのは呼び手が渡した自作 tmp の state dir の中の、永続ファイル (raiya.db とその .bak・-journal を含む) だけ。
"""
import os
import tempfile

from raiya.snapshot import RESERVED_FILES, STATE_FILES


def _guard(state_dir):
    """tmp 配下の dir だけを許す。本物の state (~/.raiya / RAIYA_STATE_DIR) は tmp 配下でも拒否する。"""
    real = os.path.realpath(state_dir)
    tmp = os.path.realpath(tempfile.gettempdir())
    if os.path.commonpath([real, tmp]) != tmp or real == tmp:
        raise ValueError("clear_state は tmp 配下の dir にしか使えない: {}".format(state_dir))
    forbidden = [os.path.expanduser("~/.raiya"), os.environ.get("RAIYA_STATE_DIR")]
    for f in forbidden:
        if f and real == os.path.realpath(f):
            raise ValueError("clear_state は本物の state dir には使えない: {}".format(state_dir))


def clear_state(state_dir):
    _guard(state_dir)
    for name in STATE_FILES + RESERVED_FILES:
        for suffix in ("", ".bak"):
            path = os.path.join(state_dir, name + suffix)
            if os.path.exists(path):
                os.remove(path)


# --- sqlite 版 (保存層 #38 スライス 3) のテスト補助 ---
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import sqlite3  # noqa: E402

from raiya import storage  # noqa: E402


def db_path(state_dir):
    return os.path.join(state_dir, storage.DB_NAME)


def alaya_store_at(path, imprint_log_path=None):
    """standalone の `AlayaStore` (`path` の dir の `raiya.db` に結ぶ。ファイル名は無視する)。"""
    return storage.open_alaya(os.path.dirname(path))


def manas_store_at(path):
    return storage.open_manas(os.path.dirname(path))


def ledger_store_at(jsonl_path, meta_path=None):
    return storage.open_ledger(os.path.dirname(jsonl_path))


@contextlib.contextmanager
def ro(state_dir):
    """`mode=ro` の接続を貸す (テストが DB の中身を読むためだけ。書けない)。"""
    conn = sqlite3.connect(storage._ro_uri(db_path(state_dir)), uri=True, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def mutate(state_dir, fn):
    """テストが「壊れた/古い形の行」を作るための直接書き込み: トリガを一旦外して `fn(conn)` を実行し、戻す。
    自作 tmp の DB にだけ使う (`_guard` と同じ規律。永続層にトリガを外す経路をコアには持たない)。"""
    conn = sqlite3.connect(db_path(state_dir), isolation_level=None)
    try:
        triggers = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
        conn.execute("BEGIN IMMEDIATE")
        for name, _ in triggers:
            conn.execute("DROP TRIGGER {}".format(name))
        fn(conn)
        for _, sql in triggers:
            conn.execute(sql)
        conn.execute("COMMIT")
    finally:
        conn.close()


def table_bodies(state_dir, table, order="seq"):
    """表の `body` を順に返す (JSON を parse しない生テキスト)。"""
    import json  # noqa: F401
    with ro(state_dir) as conn:
        return [r[0] for r in conn.execute("SELECT body FROM {} ORDER BY {}".format(table, order))]


def table_digest(state_dir):
    """全表の全列を主キー順に並べた sha256 (バイト pin の代わり: 表ごとの内容の同一性)。"""
    out = {}
    with ro(state_dir) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in tables:
            h = hashlib.sha256()
            for row in conn.execute("SELECT * FROM {} ORDER BY 1".format(t)):
                h.update(repr(row).encode("utf-8"))
            out[t] = h.hexdigest()
    return out


def meta_value(state_dir, key):
    import json
    with ro(state_dir) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_meta(state_dir, key, value):
    import json
    mutate(state_dir, lambda c: c.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(value, ensure_ascii=False), key)))


def edit_seeds(state_dir, fn):
    """seeds 表の本文を dict のリストとして `fn(seeds)` に渡して書き戻す (派生列も本文から再計算)。"""
    import json

    def go(conn):
        rows = conn.execute("SELECT pos, body FROM seeds ORDER BY pos").fetchall()
        seeds = [json.loads(b) for _, b in rows]
        fn(seeds)
        for (pos, _), seed in zip(rows, seeds):
            d = storage.seed_derived(seed)
            conn.execute("UPDATE seeds SET id=?, kind=?, project=?, live=?, body=? WHERE pos=?",
                         (d[0], d[1], d[2], d[3], storage._dumps(seed), pos))

    mutate(state_dir, go)


def edit_ledger_rows(state_dir, fn):
    """ledger 表の本文を dict のリストとして `fn(rows)` に渡して書き戻す (台帳の行テキストの形で)。"""
    import json

    def go(conn):
        rows = conn.execute("SELECT seq, body FROM ledger ORDER BY seq").fetchall()
        parsed = [json.loads(b) for _, b in rows]
        fn(parsed)
        for (seq, _), row in zip(rows, parsed):
            d = storage.ledger_derived(row)
            conn.execute("UPDATE ledger SET id=?, project=?, session_id=?, body=? WHERE seq=?",
                         (d[0], d[1], d[2], json.dumps(row, ensure_ascii=False), seq))

    mutate(state_dir, go)


def add_ledger_body(state_dir, body, row_id=0):
    """ledger 表へ生の本文を 1 行足す (壊れた本文のテスト用。INSERT はトリガに許される)。"""
    mutate(state_dir, lambda c: c.execute(
        "INSERT INTO ledger(id, project, session_id, body) VALUES (?, NULL, NULL, ?)", (row_id, body)))


def ledger_rows(state_dir):
    """台帳の行 (dict) を古い順に返す。DB がまだ無ければ空 (hooks が何も書いていない状態の観測用)。"""
    import json
    if not os.path.exists(db_path(state_dir)):
        return []
    return [json.loads(b) for b in table_bodies(state_dir, "ledger")]


def alaya_state(state_dir):
    """旧 alaya.json の形 (`seeds` / `quarantine` の dict のリスト) を DB から組み立てる (観測用。読むだけ)。"""
    import json
    return {
        "seeds": [json.loads(b) for b in table_bodies(state_dir, "seeds", order="pos")],
        "quarantine": [json.loads(b) for b in table_bodies(state_dir, "quarantine")],
    }
