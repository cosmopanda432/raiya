"""snapshot.py -- 論理スナップショットから**新規**の state dir を書く (保存層 sqlite3 化 #38 スライス 2・3)。

fixture 書き手 (受領書ハーネス・遅延ハーネス・テスト) と、スライス 4 の移行が使う、保存形式に依らない
入口。呼び手は state dir の中のファイル名・JSON の形・テーブルを知らなくてよい。スライス 3 からこのモジュールは
`raiya.db` の書き手 (`storage.create_db` が `raiya.db.tmp-<pid>` に書き → fsync → `os.replace`)。

論理スナップショット (すべて省略可。省略した節は既定値のまま):

    {
      "seeds": [seed dict, ...],            # alaya の種子 (形は alaya の seed のまま。検査しない)
      "quarantine": [entry dict, ...],      # 遍計所執の観測記録
      "alaya_meta": {"life": int, "next_id": int, "next_quarantine_id": int},   # 省いた値は種子等から導く
      "alaya_extra": {...},                 # alaya state の既知でない最上位キー (alaya.extra)
      "imprint_log": [row dict, ...],       # 熏習の決定ログ (古い順)
      "manas_log": [row dict, ...],         # 末那識の注入ログ
      "ego_weight": number,
      "manas_extra": {...},                 # manas state の既知でない最上位キー (manas.extra)
      "introspections": [dict, ...],
      "ledger_rows": [row dict, ...],       # 台帳の行 (id 昇順。挿入順 = seq)
      "ledger_meta": {"next_id": int, "consolidated_upto": int},
      "ledger_rejects": [{"source_line": int, "raw": str}, ...],   # 移行時に読めなかった JSONL の行 (原文のまま)
    }

意図して**検査しない**: 種子や行の中身 (変な形の種子が往復することを確かめるテストが使う)。検査するのは
「既に state がある dir には書かない」ことだけ (`StateExists`: `raiya.db` / `raiya.db.bak` / 旧 JSON ファイルのどれか)。
種子は配列順に `pos` を振り、行は渡された順に `seq` を振る。派生列は `storage` の読み関数から書く。`origin` は
`db.origin` meta (`fresh` / `migrated`)。

stdlib のみ。`backend` (LLM) を import しない (AST pin: tests/test_storage.py)。
"""
import json
import os

from . import storage

STATE_FILES = ("alaya.json", "imprint_log.json", "manas.json", "ledger.jsonl", "ledger_meta.json")
# sqlite3 版の永続ファイル。あれば既存 state とみなして拒否する。
RESERVED_FILES = ("raiya.db", "raiya.db.bak", "raiya.db-journal")


class StateExists(FileExistsError):
    """書き込み先の state dir に既に state がある。既存の記憶は上書きしない。"""


def _max_int_id(rows):
    ids = [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), int) and not isinstance(r.get("id"), bool)]
    return max(ids) if ids else 0


def write_state(state_dir, snapshot, origin="fresh"):
    """`snapshot` から新しい state dir (`raiya.db`) を書く。`state_dir` が無ければ作る。既存の state があれば `StateExists`。"""
    existing = [
        n for n in STATE_FILES + tuple(f + ".bak" for f in STATE_FILES) + RESERVED_FILES
        if os.path.exists(os.path.join(state_dir, n))
    ]
    if existing:
        raise StateExists("state dir に既に state がある: {} ({})".format(state_dir, ", ".join(existing)))
    os.makedirs(state_dir, exist_ok=True)
    snap = snapshot or {}
    overrides = {}

    seeds = list(snap.get("seeds") or [])
    quarantine = list(snap.get("quarantine") or [])
    ameta = dict(snap.get("alaya_meta") or {})
    overrides["alaya.life"] = ameta.get("life", 0)
    overrides["alaya.next_id"] = ameta.get("next_id", _max_int_id(seeds) + 1)
    overrides["alaya.next_quarantine_id"] = ameta.get("next_quarantine_id", _max_int_id(quarantine) + 1)
    if "alaya_extra" in snap:
        overrides["alaya.extra"] = dict(snap["alaya_extra"] or {})
    if any(k in snap for k in ("manas_log", "ego_weight", "introspections", "manas_extra")):
        overrides["manas.ego_weight"] = snap.get("ego_weight", 1.0)
    if "manas_extra" in snap:
        overrides["manas.extra"] = dict(snap["manas_extra"] or {})
    rows = list(snap.get("ledger_rows") or [])
    lmeta = dict(snap.get("ledger_meta") or {})
    overrides["ledger.next_id"] = lmeta.get("next_id", _max_int_id(rows) + 1)
    overrides["ledger.consolidated_upto"] = lmeta.get("consolidated_upto", 0)

    def fill(conn):
        for seed in seeds:
            d = storage.seed_derived(seed)
            conn.execute(
                "INSERT INTO seeds(id, kind, project, live, body) VALUES (?, ?, ?, ?, ?)",
                (d[0], d[1], d[2], d[3], storage._dumps(seed)),
            )
        for entry in quarantine:
            conn.execute("INSERT INTO quarantine(id, body) VALUES (?, ?)", (storage._entry_id(entry), storage._dumps(entry)))
        for entry in snap.get("imprint_log") or []:
            conn.execute("INSERT INTO imprint_log(seed_id, body) VALUES (?, ?)", (storage._log_seed_id(entry), storage._dumps(entry)))
        for entry in snap.get("manas_log") or []:
            conn.execute("INSERT INTO manas_log(body) VALUES (?)", (storage._dumps(entry),))
        for entry in snap.get("introspections") or []:
            conn.execute("INSERT INTO introspections(body) VALUES (?)", (storage._dumps(entry),))
        for row in rows:
            d = storage.ledger_derived(row)
            conn.execute(
                "INSERT INTO ledger(id, project, session_id, body) VALUES (?, ?, ?, ?)",
                (d[0], d[1], d[2], json.dumps(row, ensure_ascii=False)),
            )
        for rej in snap.get("ledger_rejects") or []:
            conn.execute("INSERT INTO ledger_rejects(source_line, raw) VALUES (?, ?)", (rej.get("source_line"), rej.get("raw")))

    storage.create_db(state_dir, origin=origin, fill=fill, meta_overrides=overrides)


def alaya_snapshot(state):
    """alaya の状態 dict (`{"life", "next_id", "seeds", "quarantine", "next_quarantine_id"}` の形。fixture 用に
    一部の鍵だけでもよい) を論理スナップショットの alaya 系の節へ写す。"""
    snap = {"seeds": list(state.get("seeds") or []), "quarantine": list(state.get("quarantine") or [])}
    meta = {k: state[k] for k in ("life", "next_id", "next_quarantine_id") if k in state}
    if meta:
        snap["alaya_meta"] = meta
    return snap

