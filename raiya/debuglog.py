"""debuglog.py -- hooks の経過を残す `debug.log` (設計書 2026-10-23-debug-log-design.md)

`<state_dir>/debug` (通常ファイル) が有るときだけ書く。中身が `2` で始まればレベル 2 (会話本文・
`<private>` の中身・LLM の生出力を含む)、それ以外はレベル 1 (メタデータのみ)。フラグが無ければ
何も書かない (レベル 0)。`debug.log` は記憶の入力にならない: engine・統合・inspect の想起系は
どれも読まない。`hooks.log` の「本文を書かない」規則の唯一の例外。

規律:
- `emit` は決して例外を出さない・`makedirs` しない (state dir が無ければ何も書かない)。
- `.lock` は取らない。1 行を組み立てて `O_APPEND` の fd に書く (行単位で追記される)。
- 標準ライブラリと `ledger.elide_middle` だけ。`backend` は import しない (pin あり)。
"""
import datetime
import json
import os
import stat

from .ledger import elide_middle

LOG_NAME = "debug.log"
FLAG_NAME = "debug"
FLAG_READ_BYTES = 16
L1_STR_MAX = 256                  # レベル 1: これを超える str は {"len": N} に置き換える
L1_LINE_MAX = 4 * 1024            # レベル 1: 1 行の上限 (バイト)
L2_FIELD_MAX = 65536              # レベル 2: 本文 1 フィールドの上限 (文字・elide_middle)
LINE_MAX = 1024 * 1024            # 全レベル: 1 行の上限 (UTF-8 バイト)
ROTATE_BYTES = {1: 1024 * 1024, 2: 16 * 1024 * 1024}


def level(state_dir):
    """0 (オフ) / 1 / 2。`<state_dir>/debug` が通常ファイルのときだけオン (os.stat 1 回 + 16 バイト read)。"""
    path = os.path.join(state_dir, FLAG_NAME)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return 0
    except (OSError, ValueError, TypeError):
        return 0
    try:
        with open(path, "rb") as f:
            head = f.read(FLAG_READ_BYTES)
    except OSError:
        return 1
    return 2 if head.startswith(b"2") else 1


def _len_marker(value):
    if isinstance(value, str):
        return {"len": len(value)}
    try:
        return {"len": len(json.dumps(value, ensure_ascii=False, default=str))}
    except (TypeError, ValueError):
        return {"len": -1}


def _shrink_l1(value):
    """レベル 1: 長い str を {"len": N} へ (入れ子の list / dict も辿る)。"""
    if isinstance(value, str):
        return {"len": len(value)} if len(value) > L1_STR_MAX else value
    if isinstance(value, dict):
        return {str(k): _shrink_l1(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shrink_l1(v) for v in value]
    return value


def _elide_l2(value):
    """レベル 2: 長い str を elide_middle で詰める (入れ子の list / dict も辿る)。"""
    if isinstance(value, str):
        return elide_middle(value, L2_FIELD_MAX)[0]
    if isinstance(value, dict):
        return {str(k): _elide_l2(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_elide_l2(v) for v in value]
    return value


def _encode(record):
    return (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")


def _build_line(lvl, event, fields):
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "level": lvl,
        "pid": os.getpid(),
        "event": event,
    }
    if lvl >= 2:
        for key, value in fields.items():
            if isinstance(value, str) and len(value) > L2_FIELD_MAX:
                record[key + "_len"] = len(value)
            record[key] = _elide_l2(value)
    else:
        for key, value in fields.items():
            record[key] = _shrink_l1(value)
    data = _encode(record)
    limit = L1_LINE_MAX if lvl < 2 else LINE_MAX
    if len(data) > limit:
        # 大きいフィールドから {"len": N} へ置き換えて収める (共通キーは最後まで残す)。
        protected = {"ts", "level", "pid", "event", "line_capped"}
        record["line_capped"] = True
        candidates = sorted(
            (k for k in record if k not in protected),
            key=lambda k: -len(json.dumps(record[k], ensure_ascii=False, default=str)),
        )
        for key in candidates:
            record[key] = _len_marker(record[key])
            data = _encode(record)
            if len(data) <= limit:
                break
    if len(data) > LINE_MAX:  # 保険: 共通キーだけでも収まらない異常値
        minimal = {k: record[k] for k in ("ts", "level", "pid", "event")}
        minimal["line_capped"] = True
        data = _encode(minimal)
    return data


def _open_rotating(path, rotate_bytes):
    """O_APPEND で開く。サイズが上限を超え、かつ path がこの fd と同じ inode なら `.1` へ回して開き直す。"""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if st.st_size > rotate_bytes and os.stat(path).st_ino == st.st_ino:
            os.replace(path, path + ".1")
            os.close(fd)
            fd = None
            fd = os.open(path, flags, 0o600)
    except OSError:
        pass  # 回せなくても追記は続ける
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    return fd


def emit(state_dir, level, event, **fields):
    """debug.log へ 1 行追記する。決して例外を出さない。level 0 / state dir が無いときは何もしない。"""
    try:
        if level not in (1, 2) or not os.path.isdir(state_dir):
            return
        data = _build_line(level, event, fields)
        fd = _open_rotating(os.path.join(state_dir, LOG_NAME), ROTATE_BYTES[level])
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    break
                view = view[written:]
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 -- デバッグログが本処理を壊してはならない
        pass


def read_events(state_dir):
    """`debug.log.1` -> `debug.log` の順 (古い順) で、JSON として読める dict の行だけ返す。
    無い・読めない・壊れた行は黙って飛ばす (台帳と同じ fail-soft)。何も作らない。"""
    events = []
    base = os.path.join(state_dir, LOG_NAME)
    for path in (base + ".1", base):
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        for line in blob.split(b"\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events
