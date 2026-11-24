"""raiya_hook.py -- 汎用 hook の入口 `raiya-hook` (契約 v1・Issue #62。`compact` は v1.1・Issue #63)

起動: `raiya-hook <recall|record|consolidate|session_end|compact>` (= `python -m raiya.hooks.raiya_hook <verb>`)。
stdin に JSON object 1 つ、stdout に JSON object 1 つ。

exit code (Claude Code adapter の「常に exit 0」とは別の契約):
    0  処理した。skip と内部失敗 (`outcome: "fail:<例外クラス名>"`・hooks.log に 1 行) を含む。
    2  契約拒否 (stdin が JSON object でない / `contract_version` が無いか 1 でない / 未知の欄 /
       型違反 / 不明な verb)。state dir が既にあるときだけ hooks.log に 1 行。
       無ければ何も作らない (state dir も hooks.log も)。`raiya.db` は作らない:
       `MemoryEngine` の構築より前に検査を全部終える。
    その他 (127 など) は起動できなかったこと (このモジュールは返さない)。

stdout は全動詞で `{"contract_version": 1, "outcome": "ok" | "skip:<code>" | "fail:<Class>", ...}`。
`skip:*` / `fail:*` は上の 2 欄だけ。exit 2 は `{"contract_version": 1, "outcome": "refused:<code>",
"field": <欄名 or null>}`。

処理順 (設計書 §2.1 / §9): 再帰ガード (`RAIYA_HOOK_ACTIVE=1` / `RAIYA_HOOKS=0`・stdin より前)
-> 設定ファイル / state_dir -> 旧 JSON 版の state -> verb -> stdin 解析 -> 検査 (版 -> 未知の欄 -> 型)
-> `MemoryEngine`。`backend` は import しない (統合は `engine.consolidate` が関数内で遅延 import する)。
`hooks.log` には会話本文を書かない (例外クラス名・カテゴリ・長さ・定型の拒否コードと欄名だけ)。
"""
import json
import os
import sys
import time

from .. import config, debuglog, storage
from ..alaya import seed_kind
from ..engine import DEFAULT_STATE_DIR, MemoryEngine, StateNeedsMigration, StateUnreadable, _resolve_state_dir
from ..ledger import strip_pasted, strip_private
from ..project import resolve_project
from .core import (
    CONSOLIDATE_EVERY, RECALL_CONTEXT_TURNS, TRUNCATE_LIMIT, _exc_summary, _is_skippable_prompt, _log, _ms,
    _run_consolidation, _safe_session_id, _stderr_line, has_session_mark, prepare_record, write_session_mark,
)

CONTRACT_VERSION = 1
VERBS = ("recall", "record", "consolidate", "session_end", "compact")

# verb ごとの許す top-level 欄 (この順が型検査の順でもある)。v1 は未知の欄を拒む。
ALLOWED_FIELDS = {
    "recall": ("contract_version", "session_id", "cwd", "prompt", "context", "session_start"),
    "record": ("contract_version", "session_id", "cwd", "user_text", "grounding_text", "assistant_text",
               "tool_refs"),
    "consolidate": ("contract_version", "session_id"),
    "session_end": ("contract_version", "session_id"),
    "compact": ("contract_version", "session_id", "cwd", "window_ledger_ids", "prompt", "budget_chars"),
}

TOOL_REFS_MAX_ITEMS = 64
TOOL_REFS_MAX_PATH_BYTES = 4096
TOOL_REFS_MAX_NAME_CHARS = 256
COMPACT_MAX_WINDOW_IDS = 1000  # 受け取った配列の長さ (重複を除く前)
_LOG_NAME_MAX = 64


class Refusal(Exception):
    """契約拒否 (exit 2)。`code` = version|field|type|verb|stdin、`field` = 欄名か None。"""

    def __init__(self, code, field=None):
        super().__init__(code)
        self.code = code
        self.field = field


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _bare(outcome):
    return {"contract_version": CONTRACT_VERSION, "outcome": outcome}


# --- 検査 (engine を作る前に全部終える) ---


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_tool_ref(ref):
    """凍結 spec (public-api-freeze §3) の形: file {path (絶対), line_start?, line_end?} / command {name}。
    欄は閉じている (未知の欄・未知の kind は拒否。v1 は書き手の側を厳しくする)。"""
    if not isinstance(ref, dict):
        return False
    kind = ref.get("kind")
    if kind == "file":
        if set(ref) - {"kind", "path", "line_start", "line_end"}:
            return False
        path = ref.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            return False
        try:
            if len(path.encode("utf-8")) > TOOL_REFS_MAX_PATH_BYTES:
                return False
        except UnicodeEncodeError:  # 孤立サロゲート
            return False
        start = ref.get("line_start")
        end = ref.get("line_end")
        if "line_start" in ref and not (_is_int(start) and start >= 1):
            return False
        if "line_end" in ref and not (_is_int(end) and end >= (start if "line_start" in ref else 1)):
            return False
        return True
    if kind == "command":
        if set(ref) - {"kind", "name"}:
            return False
        name = ref.get("name")
        if not isinstance(name, str) or not 0 < len(name) <= TOOL_REFS_MAX_NAME_CHARS:
            return False
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    return False


def _clean(text):
    """孤立サロゲート (妥当な JSON の `\\ud83d`。送り側が絵文字の途中で切ると出る) を `\\ud83d` の字面にする。
    残すと台帳・ログ・ファイル名の UTF-8 符号化で落ちる。"""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _validate(verb, payload):
    """payload を検査し、正規化した dict を返す。違反は Refusal。順序: 版 -> 未知の欄 -> 型。"""
    if not _is_int(payload.get("contract_version")) or payload["contract_version"] != CONTRACT_VERSION:
        raise Refusal("version", "contract_version")
    allowed = ALLOWED_FIELDS[verb]
    for key in payload:
        if key not in allowed:
            raise Refusal("field", key)

    def optional_str(name):
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            raise Refusal("type", name)
        return _clean(value) if value is not None else None

    def required_raw_str(name):
        """本文欄は生のまま返す (skip 判定を各段で生の値に対して行うため)。保存・ログ・debug へ出す前に _clean する。"""
        value = payload.get(name)
        if not isinstance(value, str):
            raise Refusal("type", name)
        return value

    session_id = optional_str("session_id")
    out = {"session_id": session_id}
    if verb in ("consolidate", "session_end"):
        return out
    out["cwd"] = optional_str("cwd")
    if verb == "compact":
        if "window_ledger_ids" not in payload:
            raise Refusal("field", "window_ledger_ids")
        ids = payload["window_ledger_ids"]
        if not isinstance(ids, list) or len(ids) > COMPACT_MAX_WINDOW_IDS or not all(_is_int(i) for i in ids):
            raise Refusal("type", "window_ledger_ids")
        out["window_ledger_ids"] = list(ids)
        if "prompt" not in payload:
            raise Refusal("field", "prompt")
        out["prompt"] = required_raw_str("prompt")
        budget = payload.get("budget_chars")
        if "budget_chars" in payload and (not _is_int(budget) or budget < 0):
            raise Refusal("type", "budget_chars")
        out["budget_chars"] = budget  # 欠ける = None (上限なし)
        return out
    if verb == "recall":
        out["prompt"] = required_raw_str("prompt")  # 生 (判定は _clean 前の値で。2026-10-27 裁定)
        context = payload.get("context", [])
        if not isinstance(context, list) or not all(isinstance(c, str) for c in context):
            raise Refusal("type", "context")
        out["context"] = list(context)  # 生 (prompt と同じ)
        session_start = payload.get("session_start", False)
        if not isinstance(session_start, bool):
            raise Refusal("type", "session_start")
        out["session_start"] = session_start
        return out
    # record
    out["user_text"] = required_raw_str("user_text")
    demoted = False
    if "grounding_text" in payload:
        grounding = payload["grounding_text"]
        if not isinstance(grounding, str) or grounding != "":
            raise Refusal("type", "grounding_text")  # v1 は呼び出し側が組んだ根拠を受けない
        demoted = True
    out["demoted"] = demoted
    out["assistant_text"] = required_raw_str("assistant_text")
    if "tool_refs" in payload:
        refs = payload["tool_refs"]
        if not isinstance(refs, list) or len(refs) > TOOL_REFS_MAX_ITEMS or not all(_valid_tool_ref(r) for r in refs):
            raise Refusal("type", "tool_refs")
        out["tool_refs"] = refs
    return out


def _bounded(session_id):
    """hooks.log に書く session_id (str 以外は None・長さを _LOG_NAME_MAX に切る)。"""
    return session_id[:_LOG_NAME_MAX] if isinstance(session_id, str) else None


def _log_session(session_id):
    """hooks.log の `session=` 値: _safe_session_id を通った値だけ (不正・非 str は None) を有界にして返す。"""
    return _bounded(_safe_session_id(session_id))


def _log_refusal(state_dir, payload, refusal):
    # 契約拒否は何も作らない: state dir がまだ無ければ (例: 既定の ./state を cwd に作ってしまう) hooks.log も書かない。
    if not os.path.isdir(state_dir):
        return
    session = None
    if isinstance(payload, dict):
        session = _log_session(payload.get("session_id"))
    field = refusal.field
    if isinstance(field, str):
        field = field[:_LOG_NAME_MAX]
    _log(state_dir, "session={} refused:{} field={}".format(session, refusal.code, field))


def _read_stdin():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, OSError):  # UnicodeDecodeError / JSONDecodeError は ValueError の子
        raise Refusal("stdin")
    if not isinstance(payload, dict):
        raise Refusal("stdin")
    return payload


# --- 動詞 ---


def _do_recall(args, state_dir, dbg=None):
    session_id = args["session_id"]
    safe_session_id = _safe_session_id(session_id)
    # `<private>` は照合にも文脈窓にも使わない。今回の prompt は貼り付けを残したまま照合し、
    # 文脈窓へは strip_pasted 後の本文だけを持ち越す (#44・claude_code._context_window と同じ)。
    # skip 判定は _clean 前の生の値で行う: 今回の prompt は生 / strip_private 後の 2 段だけ (strip_pasted 段は無い)、
    # 文脈窓の各項目は生 / strip_private 後 / strip_pasted 後の 3 段 (#44)。_clean は engine へ渡す直前。
    raw_prompt = args["prompt"]
    prompt = strip_private(raw_prompt)
    prompt_skippable = _is_skippable_prompt(raw_prompt) or _is_skippable_prompt(prompt)
    prompt = _clean(prompt)
    context = []
    for item in args["context"]:
        if _is_skippable_prompt(item):
            continue
        item = strip_private(item)
        if _is_skippable_prompt(item):
            continue
        item = strip_pasted(item)
        if not _is_skippable_prompt(item):
            context.append(_clean(item))
    context = context[-RECALL_CONTEXT_TURNS:]

    # 実効の session_start (設計書 §2.7): stdin が true、session_id が使え、印がまだ無い。
    effective_start = args["session_start"] and safe_session_id is not None and not has_session_mark(
        state_dir, safe_session_id)
    if dbg is not None:
        dbg.update(prompt_raw=_clean(raw_prompt), prompt=prompt, context=context, context_n=len(context),
                   session_start=bool(effective_start), cwd_given=bool(args["cwd"]))
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    if effective_start:
        engine.begin_new_life()
        write_session_mark(state_dir, safe_session_id)  # 生を進めたときにだけ書く (印 <=> 生を進めた)
    if dbg is not None:
        dbg.update(life=engine.life, life_begun=bool(effective_start))

    if prompt_skippable:
        if dbg is not None:
            dbg.update(skippable=True, project=resolve_project(args["cwd"]))
        return _bare("skip:skippable_prompt")

    project = resolve_project(args["cwd"])
    recall_started = time.monotonic()
    recalled = engine.recall(
        prompt, context=context, project=project, session_start=effective_start, session_id=safe_session_id,
    )
    if dbg is not None:
        resume_ = recalled.get("resume")
        dbg.update(
            recall_ms=_ms(recall_started), skippable=False, project=project,
            seed_ids=[x.get("id") for x in recalled.get("seeds") or []],
            index_ids=[x.get("id") for x in recalled.get("index") or []],
            resume_id=resume_.get("id") if isinstance(resume_, dict) else None,
            narrative=recalled["narrative"], narrative_len=len(recalled["narrative"] or ""),
            emitted=bool(recalled["narrative"]),
        )

    def brief(seed):
        return {"id": seed.get("id"), "sansho": seed.get("sansho"), "kind": seed_kind(seed)}

    resume = recalled.get("resume")
    out = _bare("ok")
    out.update(
        narrative=recalled["narrative"],
        seeds=[brief(s) for s in recalled.get("seeds") or []],
        index=[brief(s) for s in recalled.get("index") or []],
        resume={"id": resume.get("id")} if isinstance(resume, dict) else None,
        project=project,
        life=engine.life,
        life_begun=bool(effective_start),
        warnings=[w for w in (recalled.get("alaya_warning"), recalled.get("manas_warning")) if w is not None],
        context_turns=RECALL_CONTEXT_TURNS,
    )
    return out


def _do_record(args, state_dir, dbg=None):
    session_id = _log_session(args["session_id"])
    # 生の本文で判定する (孤立サロゲートは _clean で 6 字に伸びるので、_clean 後の判定は stop 経路とずれる)。
    # 保存・ログへ出る欄は skip 判定の後・切り詰めの前に normalize=_clean で整える。
    prep = prepare_record(args["user_text"], args["assistant_text"], demoted=args["demoted"], normalize=_clean)
    if dbg is not None:
        dbg.update(
            user_len=len(args["user_text"]), asst_len=len(args["assistant_text"]),
            user_len_after_private=prep.user_len_after_private, asst_len_after_private=prep.asst_len_after_private,
            grounding_len=prep.grounding_len, demoted=args["demoted"], skip=prep.skip,
            tool_refs_n=len(args["tool_refs"]) if "tool_refs" in args else None,
        )
    if prep.skip is not None:
        _log(state_dir, "session={} {}".format(session_id, prep.skip))
        return _bare(prep.skip)
    if prep.user_truncated or prep.assistant_truncated:
        _log(state_dir, "session={} 本文を{}字で切り詰めました".format(session_id, TRUNCATE_LIMIT))
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    project = resolve_project(args["cwd"])
    kwargs = {"tool_refs": args["tool_refs"]} if "tool_refs" in args else {}
    recorded = engine.record_turn(
        prep.user_text, prep.assistant_text, args["session_id"], grounding_text=prep.grounding_text, project=project,
        **kwargs
    )
    _log(state_dir, "session={} ledger_id={} pending={}".format(session_id, recorded["ledger_id"], recorded["pending"]))
    triggered = recorded["pending"] >= CONSOLIDATE_EVERY
    if dbg is not None:
        dbg.update(
            project=project, ledger_id=recorded["ledger_id"], pending=recorded["pending"],
            consolidate_due=triggered,
            truncated={"user": prep.user_truncated, "grounding": prep.grounding_truncated,
                       "assistant": prep.assistant_truncated},
            user_text=prep.user_text, grounding_text=prep.grounding_text, assistant_text=prep.assistant_text,
        )
    out = _bare("ok")
    out.update(
        ledger_id=recorded["ledger_id"], pending=recorded["pending"],
        consolidate_due=triggered, project=project, demoted=args["demoted"],
    )
    return out


def _do_compact(args, state_dir, dbg=None):
    """会話窓の圧縮 (設計書 §2.5.1)。読むだけ。prompt は strip_private -> _clean で、skippable でも skip にしない (§2.10-3)。"""
    session_id = _log_session(args["session_id"])
    text = _clean(strip_private(args["prompt"]))
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    project = resolve_project(args["cwd"])
    result = engine.compact(args["window_ledger_ids"], text, project=project, budget_chars=args["budget_chars"])
    # §2.9-9: 古い行の孤立サロゲートを backslashreplace で正規化して出し、chars は正規化後の長さに置き直す。
    narrative = _clean(result["narrative"])
    elided = [dict(e, line=_clean(e["line"])) if isinstance(e.get("line"), str) else dict(e)
              for e in result["elided"]]
    counts = {"window_n": len(args["window_ledger_ids"]), "keep": len(result["keep"]), "elided": len(elided),
              "unknown": len(result["unknown_ids"]), "chars": len(narrative)}
    _log(state_dir, "session={} compact window={} keep={} elided={} unknown={} chars={}".format(
        session_id, counts["window_n"], counts["keep"], counts["elided"], counts["unknown"], counts["chars"]))
    if dbg is not None:
        dbg.update(project=project, **counts)
    out = _bare("ok")
    out.update(
        keep=result["keep"], elided=elided, unknown_ids=result["unknown_ids"], narrative=narrative,
        cut=result["cut"], chars=len(narrative), budget_chars=result["budget_chars"],
        window_sessions=result["window_sessions"], project=project,
        warnings=[w for w in (result.get("ledger_warning"), result.get("alaya_warning")) if w is not None],
    )
    return out


_COUNT_KEYS = ("圓成實", "依他起", "遍計所執", "reject", "取り下げ照合", "双子照合")


def _do_consolidate(args, state_dir, verb, debug_level):
    # backend._call_command は子に env を足さない。利用者の llm.command が `pi -p …` だと子の pi が
    # 拡張を読み込み本番の state に書くので、engine.consolidate() の前に env で全 backend の子を止める (§9)。
    os.environ["RAIYA_HOOK_ACTIVE"] = "1"
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    errors = []
    result = _run_consolidation(engine, state_dir, _log_session(args["session_id"]), debug_level, verb, errors=errors)
    if result is None:
        return _bare("fail:{}".format(type(errors[0]).__name__ if errors else "Error"))
    decisions = result.get("decisions") or []
    counts = {key: sum(1 for d in decisions if d.get("decision") == key) for key in _COUNT_KEYS}
    counts["supersede"] = sum(1 for d in decisions if d.get("superseded"))
    out = _bare("ok")
    out.update(
        window=len(result.get("window") or []), window_sessions=result.get("window_sessions", 0) or 0,
        counts={k: counts[k] for k in ("圓成實", "依他起", "遍計所執", "reject", "supersede", "取り下げ照合", "双子照合")},
    )
    return out


# --- 入口 ---


def _debug_common(payload, sub):
    """debug.log の共通キー (claude_code と同じ)。session_id は検査を通った値だけ (保存される _clean 後の値)。"""
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    safe = _safe_session_id(_clean(sid)) if isinstance(sid, str) else None  # `\\ud83d` の字面は不可 -> None
    return {"sub": sub, "session_id": safe, "session_id_ok": safe is not None}


_RECALL_KEYS = ("project", "cwd_given", "session_start", "life", "life_begun", "recall_ms", "skippable",
                "context_n", "seed_ids", "index_ids", "resume_id", "narrative_len", "emitted")
_RECORD_KEYS = ("project", "user_len", "user_len_after_private", "grounding_len", "asst_len",
                "asst_len_after_private", "truncated", "ledger_id", "pending", "consolidate_due", "demoted",
                "skip", "tool_refs_n")


_COMPACT_KEYS = ("project", "window_n", "keep", "elided", "unknown", "chars")


def _emit_verb_event(state_dir, level, verb, payload, dbg):
    """recall / record の debug.log イベント。level 1 は長さ・id・フラグだけ、2 は本文も足す。"""
    keys = _RECALL_KEYS if verb == "recall" else _COMPACT_KEYS if verb == "compact" else _RECORD_KEYS
    fields = _debug_common(payload, verb)
    for key in keys:
        fields[key] = dbg.get(key)
    fields["dur_ms"] = _ms(dbg["t0"])
    if level >= 2 and verb != "compact":  # compact は件数と長さだけ (本文は level 2 でも書かない)
        extra = (("prompt_raw", "prompt", "context", "narrative") if verb == "recall"
                 else ("user_text", "grounding_text", "assistant_text"))
        for key in extra:
            fields[key] = dbg.get(key)
    debuglog.emit(state_dir, level, verb, **fields)


def _guard():
    if os.environ.get("RAIYA_HOOK_ACTIVE") == "1":
        return "hook_active"
    if os.environ.get("RAIYA_HOOKS") == "0":
        return "hooks_off"
    return None


def run(argv=None):
    """exit code を返す (`main` が sys.exit に渡す)。stdout の JSON はここで 1 つだけ出す。"""
    argv = sys.argv[1:] if argv is None else argv
    verb = argv[0] if argv else None

    guard = _guard()
    if guard is not None:
        # claude_code の guard 経路と同じ: debug.log は `<state_dir>/debug` フラグがあるときだけ。
        g_state_dir = os.environ.get("RAIYA_STATE_DIR") or DEFAULT_STATE_DIR
        g_level = debuglog.level(g_state_dir)
        if g_level:
            g_started = time.monotonic()
            debuglog.emit(g_state_dir, g_level, "guard", reason=guard, argv0=verb, sub=verb)
            debuglog.emit(g_state_dir, g_level, "hook_end", sub=verb, outcome="skip:guard", dur_ms=_ms(g_started))
        _emit(_bare("skip:guard"))
        return 0

    cfg = config.get()
    if config.state_dir_lost(cfg) and not os.environ.get("RAIYA_STATE_DIR"):
        _stderr_line("raiya: 設定ファイルが不正で state_dir を決められないため hook を止めました ({})".format(
            config.summary(cfg)))
        _emit(_bare("skip:state_dir_lost"))
        return 0
    state_dir = _resolve_state_dir()
    if storage.state_status(state_dir) == "legacy":
        _stderr_line("raiya: 旧 JSON 版の state (移行しない。脇に退けてから起動) のため hook を止めました")
        _emit(_bare("skip:legacy_state"))
        return 0

    dlevel = debuglog.level(state_dir)
    started = time.monotonic() if dlevel else None
    payload = None
    outcome = "ok"
    try:
        try:
            if verb not in VERBS:
                raise Refusal("verb")
            payload = _read_stdin()
            args = _validate(verb, payload)
        except Refusal as refusal:
            _log_refusal(state_dir, payload, refusal)
            outcome = "refused:{}".format(refusal.code)
            try:
                _emit({"contract_version": CONTRACT_VERSION, "outcome": outcome, "field": refusal.field})
            except Exception:  # noqa: BLE001 -- stdout が書けなくても拒否は exit 2 のまま
                pass
            return 2

        if cfg.issues:
            _log(state_dir, "config 不正 ({}) — 有効なキーと env で続行".format(config.summary(cfg)))
        session_id = _log_session(args["session_id"])
        dbg = {"t0": time.monotonic()} if dlevel and verb in ("recall", "record", "compact") else None
        try:
            if verb == "recall":
                out = _do_recall(args, state_dir, dbg)
            elif verb == "record":
                out = _do_record(args, state_dir, dbg)
            elif verb == "compact":
                out = _do_compact(args, state_dir, dbg)
            else:
                out = _do_consolidate(args, state_dir, verb, dlevel)
        except StateUnreadable as e:
            _log(state_dir, "状態DB 読めず category={}".format(e.category))
            out = _bare("fail:StateUnreadable")
        except StateNeedsMigration:
            _log(state_dir, "旧 JSON 版の state (移行しない。脇に退けてから起動)")
            out = _bare("fail:StateNeedsMigration")
        except Exception as e:  # noqa: BLE001 -- 内部失敗は exit 0 + fail:<Class> (設計書 §2.2)
            _log(state_dir, "session={} {} 失敗: {}".format(session_id, verb, _exc_summary(e)))
            out = _bare("fail:{}".format(type(e).__name__))
        outcome = out["outcome"]
        if dbg is not None:
            _emit_verb_event(state_dir, dlevel, verb, payload, dbg)  # hook_end より先 (時系列)
        _emit(out)
        return 0
    finally:
        if dlevel:
            try:
                debuglog.emit(state_dir, dlevel, "hook_end", sub=verb, outcome=outcome, dur_ms=_ms(started))
            except Exception:  # noqa: BLE001 -- デバッグログが hook を壊してはならない
                pass


def main(argv=None):
    try:
        code = run(argv)
    except Exception as e:  # noqa: BLE001 -- 想定外でも stdout に JSON 1 つ + exit 0 を保つ (D7)
        try:
            _emit(_bare("fail:{}".format(type(e).__name__)))
        except Exception:  # noqa: BLE001
            pass
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
