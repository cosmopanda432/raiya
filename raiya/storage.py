"""storage.py -- sqlite3 の永続化 (保存層の sqlite3 化 #38 スライス 3。設計書 `2026-10-27-sqlite-storage-design.md` §3 P1〜P11, P15, P16)。

stdlib の `sqlite3` だけ。`backend` (LLM) を import しない (AST + fresh-process pin: tests/test_storage.py)。

器: `state_dir/raiya.db` 1 ファイル。`PRAGMA application_id = APPLICATION_ID` (ASCII "RAIY")・`PRAGMA user_version = 1`。
ジャーナルは DELETE (WAL は `mode=ro` で開くだけで -wal/-shm と本体のバイトが変わるので不採用。§3.1)。各行 = 正確な
JSON 本文 `body` (`json.dumps(ensure_ascii=False, separators=(",", ":"))`、鍵順保持。台帳だけは `append` が今まで書いてきた
行テキストそのもの = 既定の区切り) + 検索用の派生列。派生列は書き手が `body` と同じ文で同じ読み関数
(`alaya.seed_kind` / `alaya._seed_project` / `alaya.is_live`) から書く。`body` 以外から dict は作らない。JSON1・generated
column・STRICT・RETURNING・UPSERT は使わない。

表 (P4): `meta(key PK, value)` (value は JSON 本文。型名は TEXT: 型名 JSON は NUMERIC 親和性になり "1.0" が整数 1 に化ける)・
`seeds(pos PK, id, kind, project, live, body)` idx (id), (project, live)・`quarantine(seq PK, id, body)`・
`ledger(seq PK, id NOT NULL, project, session_id, body)` idx (id)・`ledger_rejects(seq PK, source_line, raw)`・
`manas_log(seq PK, body)`・`introspections(seq PK, body)`・`imprint_log(seq PK, seed_id, body)` idx (seed_id)。
追記専用はトリガで強制する (P6): ledger / ledger_rejects / introspections / imprint_log は UPDATE も DELETE も
`RAISE(ABORT)`、seeds / meta は DELETE だけ。消してよいのは quarantine (`QUARANTINE_MAX`) と manas_log (`MANAS_LOG_MAX`) の
古い順の間引き 2 つだけ (U1: 今の振る舞いを保つ)。

区間 (P8): `SqliteBackend.section(write, with_ledger)` は **flock → 接続 → トランザクション → 全件ロード → 本体 → 差分 flush →
COMMIT** (read は `mode=ro` + `BEGIN` + ROLLBACK で PRAGMA も発行しない)。接続は区間ごと (flock の後に開き、解放前に閉じる)。
ロードは seeds 全件 (`ORDER BY pos`、WHERE 無し = P15)・quarantine 末尾 `QUARANTINE_MAX`・meta・manas_log 末尾
`MANAS_LOG_MAX` + introspections 全件。台帳は meta だけ (`with_ledger=True` のとき)。台帳の行と imprint_log の既存の行は
ロードせず、区間の間だけ使える reader で遅延して読む (recall は読まない)。本体が例外なら ROLLBACK してメモリを DB から
読み直し、元の例外をそのまま再送出する。

差分 flush (P9): 書く区間では、ロード時 (`_backfill` の後) の `pos → 正規化本文 (sort_keys=True)` を控え (読む区間は正規化 snapshot を作らない)、flush で変わった行だけ UPDATE、
ロード範囲外は INSERT、縮んでいたら例外 (消さない防護)。meta は値が変わったキーだけ。追記バッファ (imprint_log / quarantine /
manas_log / introspections / 台帳の `_pending_rows`) は flush で直列化する (append 時ではない)。

失敗 (P11): DB 単位 (NOTADB / CORRUPT / `application_id` 違い / 未知の `user_version` / 表・必須 meta の欠落 / `mode=ro` の
hot journal) は `StateUnreadable(category)` で fail-closed (何も書かない・空ストアに倒さない・隣に DB を作らない)。行単位
(parse 不能・今の述語に落ちる body) は読み飛ばして数え、該当ストアの warning に出す (書き直さない)。

standalone のストア (`open_alaya` / `open_manas` / `open_ledger`): `commit()` = 短い自己完結の書く区間 (flock → BEGIN IMMEDIATE →
そのストアだけ差分 flush → COMMIT)。`reload()` = 短い読む区間。engine の動詞の中では呼ばない (区間の COMMIT が唯一の書き込み点)。

バックアップ (P11): `request_backup()` された区間が COMMIT した後、flock を持ったまま `quick_check == ok` なら
`Connection.backup()` → `raiya.db.bak.tmp` → fsync → `os.replace` → `raiya.db.bak`。recall 経路では決して走らない。失敗は
`backup_error` に残る (engine が `notes` へ)。
"""
import contextlib
import copy
import json
import os
import sqlite3
import time

from .alaya import QUARANTINE_MAX, AlayaStore, _seed_project, is_live, seed_kind
from .ledger import LedgerStore
from .lock import StateLock
from .manas import MANAS_LOG_MAX, ManasStore

DB_NAME = "raiya.db"
BAK_NAME = "raiya.db.bak"
APPLICATION_ID = 0x52414959  # ASCII "RAIY" (1380010329。符号付き 32 bit に収まる)
USER_VERSION = 1
BUSY_TIMEOUT_SEC = 5.0

# 旧 JSON 版の state を示すファイル (P12)。`sessions/` `hooks.log` `debug.log` は数えない。
LEGACY_NAMES = ("alaya.json", "manas.json", "imprint_log.json", "ledger.jsonl", "ledger_meta.json")

REQUIRED_TABLES = ("meta", "seeds", "quarantine", "ledger", "ledger_rejects", "manas_log", "introspections", "imprint_log")
REQUIRED_META = (
    "alaya.life", "alaya.next_id", "alaya.next_quarantine_id", "alaya.extra",
    "ledger.next_id", "ledger.consolidated_upto",
    "manas.ego_weight", "manas.extra",
    "db.origin", "db.created_by_version", "db.migration_receipt_sha256",
)
_ALAYA_KNOWN = ("life", "next_id", "seeds", "quarantine", "next_quarantine_id")
_MANAS_KNOWN = ("ego_weight", "log", "introspections")

# テスト用: 読む区間を閉じる直前に `conn.total_changes` を渡す (P16: 同値 UPDATE はバイトを変えないので、
# 「読む動詞は書かない」は sha256 に加えてこれが 0 であることでも確かめる)。None なら呼ばない。
READ_CLOSE_HOOK = None

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE seeds(pos INTEGER PRIMARY KEY, id INTEGER, kind TEXT, project TEXT, live INTEGER NOT NULL, body TEXT NOT NULL);
CREATE INDEX seeds_id ON seeds(id);
CREATE INDEX seeds_project_live ON seeds(project, live);
CREATE TABLE quarantine(seq INTEGER PRIMARY KEY, id INTEGER, body TEXT NOT NULL);
CREATE TABLE ledger(seq INTEGER PRIMARY KEY, id INTEGER NOT NULL, project TEXT, session_id TEXT, body TEXT NOT NULL);
CREATE INDEX ledger_id ON ledger(id);
CREATE TABLE ledger_rejects(seq INTEGER PRIMARY KEY, source_line INTEGER, raw TEXT);
CREATE TABLE manas_log(seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE introspections(seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE imprint_log(seq INTEGER PRIMARY KEY, seed_id INTEGER, body TEXT NOT NULL);
CREATE INDEX imprint_log_seed ON imprint_log(seed_id);
"""


def _trigger_sql():
    out = []
    for table in ("ledger", "ledger_rejects", "introspections", "imprint_log"):
        out.append("CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;".format(t=table))
        out.append("CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;".format(t=table))
    for table in ("seeds", "meta"):
        out.append("CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is never deleted'); END;".format(t=table))
    return "\n".join(out)


class StateUnreadable(Exception):
    """状態 DB を読めない (DB 単位の失敗)。何も書かず、空ストアに倒さず、隣に DB も作らない (P11)。

    `category`: not_a_database / corrupt / wrong_application_id / newer_version / unknown_version / missing_table /
    missing_meta / bad_meta / hot_journal / missing_db / busy / unreadable。"""

    def __init__(self, category, detail=None):
        self.category = category
        self.detail = detail
        super().__init__("状態DB 読めず category={}".format(category))


class StateNeedsMigration(Exception):
    """state dir が旧 JSON 版 (`raiya.db` が無く旧ファイルがある)。移行しない: 脇に退けてから起動する (P12、2026-10-27 裁定)。"""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        super().__init__("旧 JSON 版の state です (移行しない。脇に退けてから起動): {}".format(state_dir))


def state_status(state_dir):
    """`db` (raiya.db または raiya.db.bak がある) / `legacy` (DB 無しで旧 JSON ファイルかその .bak がある) / `none`。

    `os.stat` のみ (P12)。`raiya.db.bak` だけが残っている dir は `db` に数える: `none` にすると、バックアップの隣に
    新しい空の DB を作ってしまう (開くときに `StateUnreadable("missing_db")` になる)。"""
    def exists(name):
        return os.path.exists(os.path.join(state_dir, name))

    if exists(DB_NAME) or exists(BAK_NAME):
        return "db"
    if any(exists(n) or exists(n + ".bak") for n in LEGACY_NAMES):
        return "legacy"
    return "none"


# --- JSON / 派生列 ---
def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def seed_derived(seed):
    """seeds 表の派生列 `(id, kind, project, live)`。読み関数は alaya のものそのまま。dict でなければ空。"""
    if not isinstance(seed, dict):
        return (None, None, None, 0)
    return (
        seed.get("id") if _is_int(seed.get("id")) else None,
        seed_kind(seed),
        _seed_project(seed),
        1 if is_live(seed) else 0,
    )


def ledger_derived(row):
    """ledger 表の派生列 `(id, project, session_id)`。id が整数でない行は 0 (読み手が本文を弾く。索引キーに過ぎない)。"""
    if not isinstance(row, dict):
        return (0, None, None)
    return (
        row["id"] if _is_int(row.get("id")) else 0,
        row.get("project") if isinstance(row.get("project"), str) else None,
        row.get("session_id") if isinstance(row.get("session_id"), str) else None,
    )


def _entry_id(entry):
    return entry.get("id") if isinstance(entry, dict) and _is_int(entry.get("id")) else None


def _log_seed_id(entry):
    return entry.get("seed_id") if isinstance(entry, dict) and _is_int(entry.get("seed_id")) else None


def _created_by_version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return "unknown"


def _default_meta_values(origin="fresh"):
    return {
        "alaya.life": 0,
        "alaya.next_id": 1,
        "alaya.next_quarantine_id": 1,
        "alaya.extra": {},
        "ledger.next_id": 1,
        "ledger.consolidated_upto": 0,
        "manas.ego_weight": 1.0,
        "manas.extra": {},
        "db.origin": origin,
        "db.created_by_version": _created_by_version(),
        "db.migration_receipt_sha256": None,
    }


# --- 新規 DB の作成 ---
def _fsync_path(path, directory=False):
    flags = os.O_RDONLY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_db(state_dir, origin="fresh", fill=None, meta_overrides=None):
    """新しい `raiya.db` を作る: `raiya.db.tmp-<pid>` に書き → fsync → `os.replace`。既に `raiya.db` があれば `FileExistsError`。

    `fill(conn)` (任意) は同じトランザクションの中で行を足す (snapshot.write_state 用)。`meta_overrides` で meta の値を上書き。
    呼び手が flock を持っているか、新規 dir (fixture) であること。"""
    final = os.path.join(state_dir, DB_NAME)
    if os.path.exists(final):
        raise FileExistsError(final)
    os.makedirs(state_dir, exist_ok=True)
    tmp = "{}.tmp-{}".format(final, os.getpid())
    if os.path.exists(tmp):
        os.unlink(tmp)
    conn = sqlite3.connect(tmp, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA application_id={}".format(APPLICATION_ID))
        conn.execute("PRAGMA user_version={}".format(USER_VERSION))
        conn.execute("BEGIN IMMEDIATE")
        for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        for stmt in [s for s in _trigger_sql().split("\n") if s.strip()]:
            conn.execute(stmt)
        values = _default_meta_values(origin)
        values.update(meta_overrides or {})
        for key in REQUIRED_META:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, _dumps(values[key])))
        if fill is not None:
            fill(conn)
        conn.execute("COMMIT")
    except BaseException:
        try:
            with contextlib.suppress(Exception):
                conn.close()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise
    try:
        conn.close()
        _fsync_path(tmp)
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        _fsync_path(state_dir)
    return final


# --- 失敗の分類 ---
def _classify(exc, db_path):
    """sqlite3 の例外を `StateUnreadable` の category にする。"""
    msg = str(exc).lower()
    journal = db_path + "-journal"
    try:
        has_journal = os.path.getsize(journal) > 0
    except OSError:
        has_journal = False
    if "not a database" in msg:
        return "not_a_database"
    if "malformed" in msg or "corrupt" in msg:
        return "corrupt"
    if isinstance(exc, sqlite3.OperationalError):
        if has_journal and ("readonly" in msg or "unable to open" in msg or "disk i/o" in msg):
            return "hot_journal"
        if "locked" in msg or "busy" in msg:
            return "busy"
        if "unable to open" in msg and not os.path.exists(db_path):
            return "missing_db"
    return "unreadable"


def _ro_uri(path):
    # sqlite の URI はパスを percent-encode する。? # % を含むパスでも壊れないよう最低限を符号化する。
    p = os.path.abspath(path)
    for ch, enc in (("%", "%25"), ("?", "%3f"), ("#", "%23")):
        p = p.replace(ch, enc)
    return "file:{}?mode=ro".format(p)


SQLITE_HEADER_SIZE = 100


def _check_header_size(db_path):
    """ファイルサイズだけで見分けられる壊れ方を、sqlite を開く前に弾く (#81)。sqlite は 0 バイトのファイルを
    「有効な空 DB」として開くので、そのままでは `_check_schema` が `wrong_application_id` と誤分類する。
    0 バイト = 中身が DB でない (`not_a_database`)、1..99 バイト = ヘッダ (100 バイト) の途中で切れた (`corrupt`)。
    ファイルには触れない (stat のみ)。無ければ何もしない (呼び手が `missing_db` を決める)。"""
    try:
        size = os.stat(db_path).st_size
    except OSError:
        return
    if size == 0:
        raise StateUnreadable("not_a_database")
    if size < SQLITE_HEADER_SIZE:
        raise StateUnreadable("corrupt")


def _check_schema(conn):
    """トランザクションの中で: application_id / user_version / 表 / 必須 meta の存在。違えば `StateUnreadable`。"""
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if app_id != APPLICATION_ID:
        raise StateUnreadable("wrong_application_id")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != USER_VERSION:
        raise StateUnreadable("newer_version" if version > USER_VERSION else "unknown_version")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise StateUnreadable("missing_table", ",".join(missing))


def _read_meta(conn):
    """meta を `{key: JSON から戻した値}` で返す。必須キーが 1 つでも無い・JSON でなければ `StateUnreadable`。"""
    raw = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    missing = [k for k in REQUIRED_META if k not in raw]
    if missing:
        raise StateUnreadable("missing_meta", ",".join(missing))
    out = {}
    for key in REQUIRED_META:
        try:
            out[key] = json.loads(raw[key])
        except (ValueError, TypeError):
            raise StateUnreadable("bad_meta", key)
    return out, {k: raw[k] for k in REQUIRED_META}


def _meta_int(meta, key, minimum):
    v = meta[key]
    if not _is_int(v) or v < minimum:
        raise StateUnreadable("bad_meta", key)
    return v


def _meta_dict(meta, key):
    v = meta[key]
    if not isinstance(v, dict):
        raise StateUnreadable("bad_meta", key)
    return v


# --- ロード ---
def load_alaya(conn, store, write=True):
    meta, raw = _read_meta(conn)
    life = _meta_int(meta, "alaya.life", -(2 ** 62))
    next_id = _meta_int(meta, "alaya.next_id", -(2 ** 62))
    next_q = _meta_int(meta, "alaya.next_quarantine_id", -(2 ** 62))
    extra = _meta_dict(meta, "alaya.extra")
    bad_seeds = 0
    seeds, positions, texts = [], [], {}
    for pos, body in conn.execute("SELECT pos, body FROM seeds ORDER BY pos"):
        try:
            seed = json.loads(body)
            AlayaStore._validate_seed(seed)
        except (ValueError, TypeError):
            bad_seeds += 1
            continue
        AlayaStore._backfill_seed(seed)
        seeds.append(seed)
        positions.append(pos)
        if write:  # 差分 flush の基準は書く区間だけが使う (読む区間では正規化ダンプを作らない)
            texts[pos] = _canon(seed)
    bad_q = 0
    quarantine = []
    for seq, body in conn.execute("SELECT seq, body FROM quarantine ORDER BY seq DESC LIMIT ?", (QUARANTINE_MAX,)):
        try:
            entry = json.loads(body)
        except (ValueError, TypeError):
            bad_q += 1
            continue
        if not isinstance(entry, dict):
            bad_q += 1
            continue
        quarantine.append(entry)
    quarantine.reverse()
    state = {"life": life, "next_id": next_id, "seeds": seeds, "quarantine": quarantine, "next_quarantine_id": next_q}
    for k, v in extra.items():
        if k not in _ALAYA_KNOWN:
            state[k] = v
    store.state = state
    store.warning = None
    if bad_seeds or bad_q:
        store.warning = "読めない行を読み飛ばしました (種子 {} 件・quarantine {} 件)。DB の行は書き直しません".format(bad_seeds, bad_q)
    store.imprint_log_warning = None
    store._imprint_log_pending = []
    store._quarantine_pending = []
    store._snap = {
        "write": write,  # 読む区間の snapshot は texts が空: flush に使えない (flush_alaya が明示的に拒む)
        "positions": positions,
        "texts": texts,
        "meta": {
            "alaya.life": _dumps(life), "alaya.next_id": _dumps(next_id),
            "alaya.next_quarantine_id": _dumps(next_q), "alaya.extra": _dumps(extra),
        },
    }


def load_manas(conn, store, write=True):
    meta, raw = _read_meta(conn)
    ego = meta["manas.ego_weight"]
    if not isinstance(ego, (int, float)) or isinstance(ego, bool):
        raise StateUnreadable("bad_meta", "manas.ego_weight")
    extra = _meta_dict(meta, "manas.extra")
    bad = 0
    log = []
    for (body,) in conn.execute("SELECT body FROM manas_log ORDER BY seq DESC LIMIT ?", (MANAS_LOG_MAX,)):
        try:
            entry = json.loads(body)
        except (ValueError, TypeError):
            bad += 1
            continue
        if not isinstance(entry, dict):
            bad += 1
            continue
        log.append(entry)
    log.reverse()
    intro = []
    for (body,) in conn.execute("SELECT body FROM introspections ORDER BY seq"):
        try:
            entry = json.loads(body)
        except (ValueError, TypeError):
            bad += 1
            continue
        if not isinstance(entry, dict):
            bad += 1
            continue
        intro.append(entry)
    state = {"ego_weight": ego, "log": log, "introspections": intro}
    for k, v in extra.items():
        if k not in _MANAS_KNOWN:
            state[k] = v
    store.state = state
    store.warning = None
    if bad:
        store.warning = "読めない行 {} 件を読み飛ばしました (manas_log / introspections)。DB の行は書き直しません".format(bad)
    store._log_pending = []
    store._introspections_pending = []
    store._snap = {"meta": {"manas.ego_weight": _dumps(ego), "manas.extra": _dumps(extra)}}


def load_ledger(conn, store, write=True):
    meta, raw = _read_meta(conn)
    next_id = _meta_int(meta, "ledger.next_id", 1)
    upto = _meta_int(meta, "ledger.consolidated_upto", 0)
    # 最後に払い出した id (= next_id - 1) を超える consolidated_upto はそこまで切り詰める (今の読み方と同じ)。
    upto = min(upto, next_id - 1)
    store.meta = {"next_id": next_id, "consolidated_upto": upto}
    rejects = conn.execute("SELECT COUNT(*) FROM ledger_rejects").fetchone()[0]
    store._rejects_warning = (
        "台帳の読めない行 {} 件 (移行時に ledger.jsonl から)".format(rejects) if rejects else None
    )
    store._row_warning = None
    store._rows_scanned = False
    store.warning = store._rejects_warning
    store._pending_rows = []
    store._snap = {"meta": {"ledger.next_id": _dumps(next_id), "ledger.consolidated_upto": _dumps(upto)}}


# --- flush ---
def _update_meta(conn, snap_meta, new_values):
    for key, value in new_values.items():
        text = _dumps(value)
        if snap_meta.get(key) != text:
            conn.execute("UPDATE meta SET value=? WHERE key=?", (text, key))
            snap_meta[key] = text


def flush_alaya(conn, store):
    snap = store._snap
    if snap is None:
        raise RuntimeError("AlayaStore が一度も DB から読み込まれていない")
    if not snap.get("write", True):
        raise RuntimeError("AlayaStore は読む区間で読み込まれた (差分 flush の基準が無い): 書く区間で読み直してから commit する")
    state = store.state
    seeds = state["seeds"]
    positions = snap["positions"]
    texts = snap["texts"]
    if len(seeds) < len(positions):
        raise RuntimeError("種子が減った (保存層は何も消さない): {} < {}".format(len(seeds), len(positions)))
    for i, seed in enumerate(seeds):
        if i < len(positions):
            pos = positions[i]
            text = _canon(seed)
            if text != texts[pos]:
                d = seed_derived(seed)
                conn.execute(
                    "UPDATE seeds SET id=?, kind=?, project=?, live=?, body=? WHERE pos=?",
                    (d[0], d[1], d[2], d[3], _dumps(seed), pos),
                )
                texts[pos] = text
        else:
            d = seed_derived(seed)
            cur = conn.execute(
                "INSERT INTO seeds(id, kind, project, live, body) VALUES (?, ?, ?, ?, ?)",
                (d[0], d[1], d[2], d[3], _dumps(seed)),
            )
            positions.append(cur.lastrowid)
            texts[cur.lastrowid] = _canon(seed)
    if store._quarantine_pending:
        for entry in store._quarantine_pending:
            conn.execute("INSERT INTO quarantine(id, body) VALUES (?, ?)", (_entry_id(entry), _dumps(entry)))
        store._quarantine_pending = []
        # 今の間引きを保つ (U1): 新しい順に QUARANTINE_MAX 件より古い行だけを消す。
        conn.execute(
            "DELETE FROM quarantine WHERE seq < (SELECT seq FROM quarantine ORDER BY seq DESC LIMIT 1 OFFSET ?)",
            (QUARANTINE_MAX - 1,),
        )
    extra = {k: v for k, v in state.items() if k not in _ALAYA_KNOWN}
    _update_meta(conn, snap["meta"], {
        "alaya.life": state["life"], "alaya.next_id": state["next_id"],
        "alaya.next_quarantine_id": state["next_quarantine_id"], "alaya.extra": extra,
    })
    if store._imprint_log_pending:
        for entry in store._imprint_log_pending:
            conn.execute("INSERT INTO imprint_log(seed_id, body) VALUES (?, ?)", (_log_seed_id(entry), _dumps(entry)))
        store._imprint_log_pending = []


def flush_manas(conn, store):
    snap = store._snap
    if snap is None:
        raise RuntimeError("ManasStore が一度も DB から読み込まれていない")
    state = store.state
    if store._log_pending:
        for entry in store._log_pending:
            conn.execute("INSERT INTO manas_log(body) VALUES (?)", (_dumps(entry),))
        store._log_pending = []
        conn.execute(
            "DELETE FROM manas_log WHERE seq < (SELECT seq FROM manas_log ORDER BY seq DESC LIMIT 1 OFFSET ?)",
            (MANAS_LOG_MAX - 1,),
        )
    for entry in store._introspections_pending:
        conn.execute("INSERT INTO introspections(body) VALUES (?)", (_dumps(entry),))
    store._introspections_pending = []
    extra = {k: v for k, v in state.items() if k not in _MANAS_KNOWN}
    _update_meta(conn, snap["meta"], {"manas.ego_weight": state["ego_weight"], "manas.extra": extra})


def flush_ledger(conn, store):
    snap = store._snap
    if snap is None:
        raise RuntimeError("LedgerStore が一度も DB から読み込まれていない")
    for row in store._pending_rows:
        d = ledger_derived(row)
        conn.execute(
            "INSERT INTO ledger(id, project, session_id, body) VALUES (?, ?, ?, ?)",
            (d[0], d[1], d[2], json.dumps(row, ensure_ascii=False)),
        )
    store._pending_rows = []
    _update_meta(conn, snap["meta"], {
        "ledger.next_id": store.meta["next_id"], "ledger.consolidated_upto": store.meta["consolidated_upto"],
    })


_LOADERS = {AlayaStore: load_alaya, ManasStore: load_manas, LedgerStore: load_ledger}
_FLUSHERS = {AlayaStore: flush_alaya, ManasStore: flush_manas, LedgerStore: flush_ledger}


class SqliteBackend:
    """1 state dir の sqlite 版 backend (`lock()` / `open_stores()` / `section()`)。"""

    kind = "sqlite"

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.db_path = os.path.join(state_dir, DB_NAME)
        self.alaya = None
        self.manas = None
        self.ledger = None
        self._conn = None
        self._writing = False
        self._backup_requested = False
        self.backup_error = None
        self.last_read_total_changes = None
        # 区間ごとの内訳 (ms)。`timings` = 直近の区間、`timing_totals` = 累計 (recall_latency_harness 用)。
        self.timings = {}
        self.timing_totals = {"sections": 0, "load_ms": 0.0, "flush_ms": 0.0, "commit_ms": 0.0}

    # --- lock / 構築 ---
    def lock(self):
        """`state_dir/.lock` の flock (コンテキストマネージャ)。"""
        return StateLock(self.state_dir)

    def open_stores(self):
        """3 つの純粋なストアを作って backend に結び、`(alaya, manas, ledger)` を返す。呼び手が lock を持っているときに呼ぶこと。

        `legacy` の dir は `StateNeedsMigration` (何も作らない)。`none` なら新しい DB を作る (`db.origin=fresh`)。
        `db` なら I/O は無い (検査は最初の区間のトランザクションの中)。ストアはまだ空で、区間の入口で読み込まれる。"""
        status = state_status(self.state_dir)
        if status == "legacy":
            raise StateNeedsMigration(self.state_dir)
        if status == "none":
            create_db(self.state_dir, origin="fresh")
        self.alaya = AlayaStore()
        self.manas = ManasStore()
        self.ledger = LedgerStore()
        for store in (self.alaya, self.manas, self.ledger):
            store._backend = self
        self.alaya._imprint_log_reader = self._read_imprint_log
        self.ledger._row_reader = self._ledger_bodies
        return self.alaya, self.manas, self.ledger

    # --- 接続 ---
    def _connect(self, write):
        """接続を開く (flock の後)。write: rw + `journal_mode=DELETE`。read: `mode=ro` (PRAGMA は発行しない)。"""
        _check_header_size(self.db_path)
        try:
            if write:
                if not os.path.exists(self.db_path):
                    raise StateUnreadable("missing_db")
                conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=BUSY_TIMEOUT_SEC)
            else:
                conn = sqlite3.connect(_ro_uri(self.db_path), uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_SEC)
        except sqlite3.Error as e:
            raise StateUnreadable(_classify(e, self.db_path)) from e
        return conn

    def _unreadable(self, exc):
        return StateUnreadable(_classify(exc, self.db_path), str(exc))

    @staticmethod
    def _close_quietly(conn, rollback=True):
        if conn is None:
            return
        if rollback:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def _reading(self):
        """区間の接続があればそれを、無ければ一時の `mode=ro` 接続を貸す (区間の外から台帳・imprint_log を読む呼び手用)。"""
        if self._conn is not None:
            yield self._conn
            return
        conn = self._connect(False)
        try:
            conn.execute("BEGIN")
            _check_schema(conn)
            yield conn
        except sqlite3.Error as e:
            raise self._unreadable(e) from e
        finally:
            self._close_quietly(conn)

    def _load_stores(self, conn, stores, write=True):
        _check_schema(conn)
        for store in stores:
            _LOADERS[type(store)](conn, store, write)

    # --- reader (区間の間だけ / 区間の外では一時接続) ---
    def _ledger_bodies(self):
        try:
            with self._reading() as conn:
                return [r[0] for r in conn.execute("SELECT body FROM ledger ORDER BY id, seq")]
        except sqlite3.Error as e:
            raise self._unreadable(e) from e

    def _read_imprint_log(self, n=None, seed_id=None):
        try:
            with self._reading() as conn:
                if seed_id is not None:
                    cur = conn.execute("SELECT body FROM imprint_log WHERE seed_id=? ORDER BY seq", (seed_id,))
                elif n is not None:
                    cur = conn.execute(
                        "SELECT body FROM (SELECT seq, body FROM imprint_log ORDER BY seq DESC LIMIT ?) ORDER BY seq", (n,)
                    )
                else:
                    cur = conn.execute("SELECT body FROM imprint_log ORDER BY seq")
                bodies = [r[0] for r in cur]
        except sqlite3.Error as e:
            raise self._unreadable(e) from e
        rows, bad = [], 0
        for body in bodies:
            try:
                row = json.loads(body)
            except (ValueError, TypeError):
                bad += 1
                continue
            if not isinstance(row, dict):
                bad += 1
                continue
            rows.append(row)
        if bad and self.alaya is not None:
            self.alaya.imprint_log_warning = "imprint_log の読めない行 {} 件を読み飛ばしました。DB の行は書き直しません".format(bad)
        return rows

    # --- 区間 ---
    def request_backup(self):
        """今の書く区間が COMMIT した後に `raiya.db.bak` を作らせる (consolidate が `skipped is None` のとき)。"""
        self._backup_requested = True

    def quick_check(self):
        """区間の接続で `PRAGMA quick_check`。ok なら None、そうでなければ警告文 (audit が alaya_warning へ出す)。"""
        conn = self._conn
        if conn is None:
            raise RuntimeError("quick_check は区間の中で呼ぶ")
        rows = [r[0] for r in conn.execute("PRAGMA quick_check")]
        if rows == ["ok"]:
            return None
        return "状態DB の整合性検査 (quick_check) が ok ではありません: " + " / ".join(str(r) for r in rows[:3])

    def _timing_start(self):
        self.timings = {"load_ms": 0.0, "flush_ms": 0.0, "commit_ms": 0.0}

    def _timing_end(self):
        t = self.timing_totals
        t["sections"] += 1
        for k in ("load_ms", "flush_ms", "commit_ms"):
            t[k] += self.timings.get(k, 0.0)

    @contextlib.contextmanager
    def section(self, write, with_ledger=False):
        """flock → 接続 → トランザクション → 読み込み → (本体) → write なら差分 flush → COMMIT → 解放。

        本体が例外なら ROLLBACK し、メモリを DB から読み直して元の例外を再送出する (何も書かれない)。
        `with_ledger=False` なら台帳ストアには触れない (`recall` は台帳に触れない)。"""
        if self._conn is not None:
            raise RuntimeError("section は入れ子にできない")
        stores = [self.alaya, self.manas] + ([self.ledger] if with_ledger else [])
        with self.lock():
            self._backup_requested = False
            self.backup_error = None
            self._timing_start()
            conn = None
            try:
                conn = self._connect(write)
                self._conn = conn
                self._writing = write
                try:
                    if write:
                        conn.execute("PRAGMA journal_mode=DELETE")  # 通常 no-op。手で WAL にされた DB の修復 (P2)
                        conn.execute("BEGIN IMMEDIATE")
                    else:
                        conn.execute("BEGIN")
                    t0 = time.perf_counter()
                    self._load_stores(conn, stores, write)
                    self.timings["load_ms"] = (time.perf_counter() - t0) * 1000
                except sqlite3.Error as e:
                    raise self._unreadable(e) from e
                try:
                    yield
                except BaseException:
                    self._conn = None
                    self._close_quietly(conn)
                    conn = None
                    self._discard(stores)
                    raise
                if write:
                    try:
                        t0 = time.perf_counter()
                        for store in stores:
                            _FLUSHERS[type(store)](conn, store)
                        t1 = time.perf_counter()
                        conn.execute("COMMIT")
                        t2 = time.perf_counter()
                    except sqlite3.Error as e:
                        self._conn = None
                        self._close_quietly(conn)
                        conn = None
                        self._discard(stores)
                        raise self._unreadable(e) from e
                    except BaseException:
                        self._conn = None
                        self._close_quietly(conn)
                        conn = None
                        self._discard(stores)
                        raise
                    self.timings["flush_ms"] = (t1 - t0) * 1000
                    self.timings["commit_ms"] = (t2 - t1) * 1000
                    if self._backup_requested:
                        self._backup(conn)
                else:
                    total = conn.total_changes
                    self.last_read_total_changes = total
                    conn.execute("ROLLBACK")
                    if READ_CLOSE_HOOK is not None:
                        READ_CLOSE_HOOK(total)
                self._timing_end()
            finally:
                self._conn = None
                self._writing = False
                self._backup_requested = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _discard(self, stores):
        """失敗した区間のメモリ (保存されなかった変更・追記バッファ) を捨てる: DB から読み直す。失敗しても元の例外を隠さない。"""
        conn = None
        try:
            conn = self._connect(False)
            conn.execute("BEGIN")
            self._load_stores(conn, stores)
        except Exception:
            pass
        finally:
            self._close_quietly(conn)

    # --- バックアップ ---
    def _backup(self, conn):
        """`Connection.backup()` → tmp → fsync → `os.replace`。`quick_check` が ok でなければ古い .bak を守って skip。"""
        bak = os.path.join(self.state_dir, BAK_NAME)
        tmp = bak + ".tmp"
        try:
            bad = self.quick_check()
            if bad is not None:
                self.backup_error = "quick_check が ok でないため raiya.db.bak を更新しません"
                return
            if os.path.exists(tmp):
                os.unlink(tmp)
            dest = sqlite3.connect(tmp)
            try:
                conn.backup(dest)
            finally:
                dest.close()
            _fsync_path(tmp)
            os.replace(tmp, bak)
        except Exception as e:
            self.backup_error = "raiya.db.bak の作成に失敗しました ({})".format(type(e).__name__)
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    # --- standalone ---
    def reload_store(self, store):
        """standalone のストア 1 つを DB から読み直す (短い読む区間)。"""
        if self._conn is not None:
            raise RuntimeError("section の中では reload しない")
        with self.lock():
            conn = None
            try:
                conn = self._connect(False)
                conn.execute("BEGIN")
                self._load_stores(conn, [store])
            except sqlite3.Error as e:
                raise self._unreadable(e) from e
            finally:
                self._close_quietly(conn)

    def commit_store(self, store):
        """standalone のストア 1 つの差分を書く (短い自己完結の書く区間: flock → BEGIN IMMEDIATE → flush → COMMIT)。"""
        if self._conn is not None:
            raise RuntimeError("section の中では commit しない")
        with self.lock():
            conn = None
            try:
                conn = self._connect(True)
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("BEGIN IMMEDIATE")
                _check_schema(conn)
                _FLUSHERS[type(store)](conn, store)
                conn.execute("COMMIT")
            except sqlite3.Error as e:
                self._close_quietly(conn)
                conn = None
                raise self._unreadable(e) from e
            except BaseException:
                self._close_quietly(conn)
                conn = None
                raise
            finally:
                if conn is not None:
                    conn.close()


def _open_standalone(state_dir, attr):
    os.makedirs(state_dir, exist_ok=True)
    backend = SqliteBackend(state_dir)
    with backend.lock():
        backend.open_stores()
    store = getattr(backend, attr)
    backend.reload_store(store)
    return store


def open_alaya(state_dir):
    """standalone の `AlayaStore` (DB から読み込み済み・`commit()` で書く)。engine を介さない呼び手 (受領書ハーネス・テスト) 用。"""
    return _open_standalone(state_dir, "alaya")


def open_manas(state_dir):
    """standalone の `ManasStore`。"""
    return _open_standalone(state_dir, "manas")


def open_ledger(state_dir):
    """standalone の `LedgerStore`。"""
    return _open_standalone(state_dir, "ledger")
