"""core.py -- hooks のホストに依らない共通部分 (#62・pi 拡張設計書 §2.6)

`claude_code.py` (Claude Code の hooks) と `raiya_hook.py` (汎用 hook の入口 `raiya-hook`) の
両方が使う。stdin/stdout の形・exit code・ホスト固有の欄 (agent_id 等) はここに置かない。
置くもの: skip 判定 (`HARNESS_TAGS` ほか)、有界な `hooks.log` の書き手、session_id の検査、
セッションの印、本文処理 (`prepare_record`: 設計書 §2.4 の 1〜5)、統合の実行 (`_run_consolidation`)。

規律: `hooks.log` へは例外クラス名・失敗カテゴリ・長さだけを書く (会話本文は書かない)。
`backend` はここでも import しない (`_exc_summary` が失敗経路でだけ遅延 import する)。
"""
import datetime
import json
import os
import re
import sys
import time
from collections import Counter

from .. import debuglog
from ..engine import StateUnreadable
from ..ledger import elide_middle, strip_pasted, strip_private

SESSIONS_MAX_KEEP = 50
TRUNCATE_LIMIT = 8000
MIN_PROMPT_LEN = 2
CONSOLIDATE_EVERY = 5        # この件数の往復が台帳に溜まるごとに統合 (改修4 §8)
# CONSOLIDATE_EVERY <= engine.CONSOLIDATE_WINDOW_MAX (1 回の統合が読む窓の上限) を保つこと:
# 超えると毎回の統合で消費しきれない往復が残り続け、pending が 0 へ戻らなくなる。
RECALL_CONTEXT_TURNS = 5     # 想起の文脈窓 = 直前までのプロンプト件数
# Claude Code が user 枠に差し込む harness 由来テキスト。ユーザー発話ではないので
# 想起・台帳・熏習の対象にしない (圓成實=「ユーザーが言った」の意味を守る)。
HARNESS_TAGS = frozenset(
    {"task-notification", "system-reminder", "local-command-stdout", "local-command-caveat",
     "command-name", "ide_selection", "ide_opened_file",
     # サブエージェントの報告 (SendMessage / hand-back) が user 枠へ届く形。2026-10-08 本番台帳
     # id 9/14/15 (2291〜5688 字) が人間の発話として記帳されていた (設計書 §10.6 追補)。
     "agent-message"}
)
_LEADING_TAG_RE = re.compile(r"^<\s*([A-Za-z_][\w-]*)")


def _is_harness_injected(text):
    """先頭 (前後の空白を除く) が HARNESS_TAGS のタグで始まるか (改修4 §8)。

    タグ名の照合は小文字化して行う (`<IDE_SELECTION>`)。`<div> を使いたい` のような
    ふつうの発話や、文中に `<` を含むだけの発話は対象にしない。
    """
    m = _LEADING_TAG_RE.match((text or "").lstrip())
    return bool(m) and m.group(1).lower() in HARNESS_TAGS


def _is_skippable_prompt(text):
    stripped = (text or "").strip()
    return (
        (not stripped)
        or stripped.startswith("/")
        or len(stripped) < MIN_PROMPT_LEN
        or _is_harness_injected(stripped)
    )


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hooks_log_path(state_dir):
    return os.path.join(state_dir, "hooks.log")


def _exc_summary(e):
    """例外を hooks.log 用の有界な1行要約にする(記憶機能設計書§10.3d)。

    hooks.log は長命な平文ログなので、backend の stderr / HTTP body / 会話本文が
    載りうる例外文字列(str(e))は書かない。書くのは有界な事実だけ:
    例外クラス名・BackendError なら reason(定型の失敗カテゴリ)・本文の長さ。
    """
    # engine.py の propose() と同じ理由 (呼ばれない限り backend/urllib を読み込まない) で
    # 遅延 import する。この関数は失敗経路でしか呼ばれないので、正常経路 (同期の
    # recall を含む) は backend/urllib をロードしない。
    from ..backend import BackendError

    # isinstance で厳密に判定する (duck typing 禁止: urllib.error.URLError も
    # .reason 属性を持つため、hasattr(e, "reason") 判定だと誤分類し .detail で
    # AttributeError になる)。
    if isinstance(e, BackendError):
        return "BackendError: {} (detail_len={})".format(e.reason, len(e.detail))
    return "{} (msg_len={})".format(type(e).__name__, len(str(e)))


# inspect_cli._esc とは別様式にしてある: core は leaf、かつ hooks.log の `\n` -> `\\n` 形はテストで固定。
def _escape_char(c):
    """印字不能な 1 文字を 1 行に収まる字面にする (`\n` -> `\\n`、`\x1b`、`\ud83d` ...)。"""
    return c.encode("unicode_escape").decode("ascii")


def _log(state_dir, message):
    try:
        os.makedirs(state_dir, exist_ok=True)
        # message (RAIYA_BACKEND 等 operator env 由来の値が reason に載ることもある)
        # が改行 / CR / ESC 等の制御文字を含んでも1エントリ=1行を保つため、印字不能な文字は
        # すべてバックスラッシュ記法でエスケープする (記憶機能設計書§10.3d: hooks.log の偽の
        # 追加行・端末制御=ログ偽装を防ぐ)。孤立サロゲートも `\ud83d` の字面になる。
        safe_message = "".join(c if c.isprintable() else _escape_char(c) for c in str(message))
        with open(_hooks_log_path(state_dir), "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write("{} {}\n".format(_now_iso(), safe_message))
    except OSError:
        pass


def _sessions_dir(state_dir):
    return os.path.join(state_dir, "sessions")


def _safe_session_id(session_id):
    """ファイル名・ログ 1 行として安全な session_id のみ受け付ける。

    path traversal (`/` `\\` `..`) と、印字不能 (`str.isprintable()` が False: 制御文字 CR/LF/NUL/ESC・孤立サロゲート・
    非 ASCII の空白/区切り/書式文字 例 NBSP, U+2028, ZWJ) を含むものは None。ASCII 許可リストにはしない
    (印字可能な非 ASCII の ID は通す)。
    """
    if not session_id or not isinstance(session_id, str):
        return None
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        return None
    if not session_id.isprintable():
        return None
    return session_id


def _prune_sessions(sdir, keep=SESSIONS_MAX_KEEP):
    try:
        names = [n for n in os.listdir(sdir) if n.endswith(".json")]
    except OSError:
        return
    if len(names) <= keep:
        return
    paths = [os.path.join(sdir, n) for n in names]
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for stale in paths[keep:]:
        try:
            os.remove(stale)
        except OSError:
            pass


def _truncate(text, limit):
    """台帳に記録する本文の切り詰め: 先頭+末尾を残し、間に `<elided N chars>` の印を挟む
    (設計書 §10.6 処方 8)。戻り値: (本文, 切り詰めたか)。印は台帳に残るが、統合の三性
    ゲートはそれを `ledger.GATE_CUT` に置き換えて読むので、印や切れ目を跨ぐ引用は接地しない。"""
    return elide_middle(text, limit)


def _stderr_line(message):
    """stderr に 1 行。state dir の外で、失敗しても hook を壊さない。"""
    try:
        sys.stderr.write(message.replace("\n", " ") + "\n")
    except Exception:  # noqa: BLE001
        pass


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def _run_consolidation(engine, state_dir, session_id, debug_level=0, sub="stop", errors=None):
    """統合 (LLM は提案のみ) -> 三性ゲート -> 保存。失敗は有界に記録して黙る。

    `debug_level` > 0 のときだけ `engine.consolidate(debug=<dict>)` で診断を受け取り、
    debug.log へ `consolidate` イベントを 1 行出す (成功・空窓・失敗のどれでも)。

    戻り値: `engine.consolidate()` の結果 dict。握りつぶした失敗では None を返し、`errors` (list) が
    渡されていればその例外を append する (raiya-hook が `fail:<Class>` を返すため)。
    `hooks.log` の行は従来どおり。
    """
    parse_notes = []
    log_sid = _safe_session_id(session_id)  # hooks.log へは検査済みの値だけ (不正は None)
    dbg = {} if debug_level else None
    started = time.monotonic() if dbg is not None else None
    try:
        if dbg is None:
            result = engine.consolidate(notes=parse_notes)
        else:
            result = engine.consolidate(notes=parse_notes, debug=dbg)
    except StateUnreadable:
        raise  # 状態 DB を読めない失敗は _main_impl が 1 行 (`状態DB 読めず category=…`) で記録する
    except Exception as e:
        _log(state_dir, "session={} consolidate失敗: {}".format(log_sid, _exc_summary(e)))
        if dbg is not None:
            _emit_consolidate(state_dir, debug_level, sub, session_id, dbg, started, parse_notes, None, exc=e)
        if errors is not None:
            errors.append(e)
        return None
    if dbg is not None:
        _emit_consolidate(state_dir, debug_level, sub, session_id, dbg, started, parse_notes, result)
    if not result["window"]:
        return result
    counts = Counter(d["decision"] for d in result["decisions"])
    supersedes = sum(1 for d in result["decisions"] if d.get("superseded"))
    # sessions= は窓に混ざった session の種類数 (七文書 §2.3 同依)。先頭の session= は統合を
    # 起動したセッションでしかなく、窓の往復の所依ではない。
    line = "session={} consolidate window={} sessions={} proposals={} 圓成實={} 依他起={} 遍計所執={} reject={} supersede={}".format(
        log_sid, len(result["window"]), result.get("window_sessions", 0), len(result["proposals"]),
        counts.get("圓成實", 0), counts.get("依他起", 0), counts.get("遍計所執", 0), counts.get("reject", 0), supersedes,
    )
    if counts.get("取り下げ照合", 0) > 0:
        line += " 取り下げ照合={}".format(counts["取り下げ照合"])
    if counts.get("双子照合", 0) > 0:
        line += " 双子照合={}".format(counts["双子照合"])
    recorded_only = sum(1 for d in result["decisions"] if d.get("user_span_match") is not None or d.get("echo_match") is not None or d.get("settled_match") is not None)
    if recorded_only > 0:  # D / R / SC' (#101・#105・#152): 強めずに記録だけ残した提案の数
        line += " 記録のみ={}".format(recorded_only)
    if result.get("skipped"):
        line += " skipped={}".format(result["skipped"])
    _log(state_dir, line)
    # proposals=0 が「LLM が空配列 [] を正常に返した」のか「出力を読み取れなかった」
    # のか、上の行だけでは区別できない。区別が要るのは寛容パースが実際に働いた
    # (=parse_notes が非空の) ときだけなので、健全なセッション(notes 無し)はログを
    # 増やさない(静けさ=健全のまま)。生の LLM 出力は書かない(理由と件数のみ)。
    if parse_notes:
        _log(state_dir, "session={} parse_notes: {}".format(log_sid, "; ".join(parse_notes)))
    return result


def _emit_consolidate(state_dir, debug_level, sub, session_id, dbg, started, parse_notes, result, exc=None):
    """`consolidate` イベント。engine が埋めた dbg と戻り値 (失敗時は例外) から組む。
    例外の本文 (`str(e)`・BackendError なら detail を含む) はレベル 2 だけ。"""
    safe = _safe_session_id(session_id)
    ids = dbg.get("window_ids") or []
    fields = {
        "sub": sub, "session_id": safe, "session_id_ok": safe is not None,
        "window_ids": [ids[0], ids[-1]] if ids else [], "window_n": dbg.get("window_n"),
        "window_sessions": dbg.get("window_sessions"),
        "prompt_len": dbg.get("prompt_len"),
        "llm_ms": dbg.get("llm_ms"), "raw_len": dbg.get("raw_len"), "parse_notes": list(parse_notes),
        "dur_ms": _ms(started),
    }
    if "backend" in dbg:  # 空窓は backend を import する前に返る = backend / model のキーを出さない
        fields["backend"] = dbg["backend"]
        fields["model"] = dbg.get("model")
    if result is not None:
        decisions = result.get("decisions") or []
        counts = Counter(d["decision"] for d in decisions)
        fields["counts"] = {
            "圓成實": counts.get("圓成實", 0), "依他起": counts.get("依他起", 0),
            "遍計所執": counts.get("遍計所執", 0), "reject": counts.get("reject", 0),
            "supersede": sum(1 for d in decisions if d.get("superseded")),
            "取り下げ照合": counts.get("取り下げ照合", 0),
            "双子照合": counts.get("双子照合", 0),
        }
        fields["skipped"] = result.get("skipped")
    if exc is not None:
        fields["exc"] = type(exc).__name__
        try:
            fields["exc_summary"] = _exc_summary(exc)
        except Exception:  # noqa: BLE001
            fields["exc_summary"] = type(exc).__name__
    if debug_level >= 2:
        fields["prompt"] = dbg.get("prompt")
        fields["raw"] = dbg.get("raw")
        if result is not None:
            fields["proposals"] = result.get("proposals")
            fields["decisions"] = result.get("decisions")
        if exc is not None:
            fields["error"] = str(exc)
    debuglog.emit(state_dir, debug_level, "consolidate", **fields)


# --- セッションの印 (raiya-hook・設計書 §2.7) ---
# `sessions/<safe_id>.json` = {"ts"} だけ。Claude Code adapter のキャッシュ ({prompt, ts, recent}) とは
# 別物で、`_read_session_cache` はこれを読まない ("prompt" が無い)。印がある <=> この session で
# 生を進めた、を保つため、生を進めたときにだけ書く。


def has_session_mark(state_dir, session_id):
    safe = _safe_session_id(session_id)
    if safe is None:
        return False
    return os.path.exists(os.path.join(_sessions_dir(state_dir), "{}.json".format(safe)))


def write_session_mark(state_dir, session_id):
    """印を書き、SESSIONS_MAX_KEEP で刈る。書けなくても例外にしない (戻り値: 書けたか)。"""
    safe = _safe_session_id(session_id)
    if safe is None:
        return False
    sdir = _sessions_dir(state_dir)
    try:
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "{}.json".format(safe)), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time()}, f)
        _prune_sessions(sdir)
    except OSError:
        return False
    return True


# --- 本文処理 (設計書 §2.4 の 1〜5。claude_code.cmd_stop と raiya_hook record が共有) ---


class RecordPrep:
    """`prepare_record` の結果。`skip` が None でなければ台帳へ書かない (skip コード)。"""

    __slots__ = ("skip", "user_text", "assistant_text", "grounding_text", "user_truncated",
                 "assistant_truncated", "grounding_truncated", "user_len_after_private",
                 "asst_len_after_private", "grounding_len")

    def __init__(self):
        self.skip = None
        self.user_text = ""
        self.assistant_text = ""
        self.grounding_text = ""
        self.user_truncated = False
        self.assistant_truncated = False
        self.grounding_truncated = False
        self.user_len_after_private = 0
        self.asst_len_after_private = 0
        self.grounding_len = 0


def prepare_record(user_text, assistant_text, demoted=False, normalize=None):
    """往復の本文を台帳へ書く形にする (純粋・I/O 無し)。

    判定の順 (設計書 §2.4 step 3 の「生の user_text」= `strip_private` 前):
    1. 生の user が skippable → `skip:skippable_user`。2. 両側に `strip_private`。
    3. assistant が空 → `skip:empty_assistant`。4. `strip_pasted(user)` が skippable → `skip:skippable_after_strip`。
    5. 根拠: 通常は `_truncate(derived)`、降格 (`demoted`) なら ""。6. 記録の user/assistant を `_truncate` で切る
    (除去は切る前。先に切ると対にならない塊が残る)。
    raiya-hook も _clean (孤立サロゲートの字面化) 前の生の値をここへ渡して判定を揃える (重複実装ではない)。
    `normalize` (任意) は skip 判定が全部済んだ後・`_truncate` の前に 3 本文へ掛ける (raiya-hook は `_clean`)。
    `*_len_after_private` / `grounding_len` は normalize 前の長さのまま (stop は normalize 無しで不変)。
    """
    prep = RecordPrep()
    if _is_skippable_prompt(user_text):
        prep.skip = "skip:skippable_user"
        return prep
    user_text = strip_private(user_text)
    assistant_text = strip_private(assistant_text)
    derived = strip_pasted(user_text)
    prep.user_len_after_private = len(user_text)
    prep.asst_len_after_private = len(assistant_text)
    prep.grounding_len = len(derived)
    if not assistant_text.strip():
        prep.skip = "skip:empty_assistant"
        return prep
    if _is_skippable_prompt(derived):
        prep.skip = "skip:skippable_after_strip"
        return prep
    if normalize is not None:  # skip 判定は全て済んだ後・切る前 (切った後に伸ばすと TRUNCATE_LIMIT を超える)
        user_text, assistant_text, derived = normalize(user_text), normalize(assistant_text), normalize(derived)
    if demoted:
        prep.grounding_text, prep.grounding_truncated = "", False
    else:
        prep.grounding_text, prep.grounding_truncated = _truncate(derived, TRUNCATE_LIMIT)
    prep.user_text, prep.user_truncated = _truncate(user_text, TRUNCATE_LIMIT)
    prep.assistant_text, prep.assistant_truncated = _truncate(assistant_text, TRUNCATE_LIMIT)
    return prep
