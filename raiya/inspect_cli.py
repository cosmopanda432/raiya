"""inspect_cli.py -- `raiya-inspect`: 何を覚え、何を注入し、なぜそうなったかを実ファイルから読む

spec: 2026-10-20-inspect-cli-design.md。観測だけの窓 (書く窓は作らない)。

    raiya-inspect [--state-dir DIR] [--json] show     [--project P] [--kind K] [--all]
    raiya-inspect [--state-dir DIR] [--json] injected [--last N]
    raiya-inspect [--state-dir DIR] [--json] why      SEED_ID
    raiya-inspect [--state-dir DIR] [--json] explain  TEXT [--project P] [--context T ...] [--session-start]
    raiya-inspect [--state-dir DIR] [--json] debug    [--last N] [--session S] [--event E] [--raw]
    raiya-inspect [--state-dir DIR] serve [--port N]   (127.0.0.1・GET のみ・読むだけの Web 表示)

規律:
- 読むだけ。engine は `MemoryEngine(new_life=False)` で作り (True だと生が 1 つ進む)、動詞は全て
  `_read_locked` (save しない) を通る。state dir に `raiya.db` が無ければ (`state_status` が `db` 以外) engine を作らずに
  exit 2 (engine の構築は state dir と `.lock` を作るので、作る前に止める)。
- `backend` を import しない (LLM の自己申告は根拠にしない。直接の読み取りだけで確かめる)。
- 出力は端末だけ (`hooks.log` には書かない)。記録された文字列 (session / project / 本文) は制御文字を
  `\\xNN` 等に置き換えてから出す (`_esc`)。`--json` は JSON の escape に任せる。
- `explain` の `--project` 省略は cwd の project (`resolve_project(os.getcwd())`。raiya-cli と同じ)、
  `""` は全域の種子だけ。`show` の `--project` 省略は全件 (engine の既定 `ALL_PROJECTS`)。
- `debug` は hooks の経過ログ `debug.log` (+ `.1`) を `debuglog.read_events` で直接読む。state の検査より
  前に分岐し、`MemoryEngine` を作らない (`.lock` も作らない)。state dir が無ければ exit 2、`debug.log` が
  無ければ案内を出して exit 0。`--raw` の JSON 行も `_esc` を通す (`--json` は配列で JSON に任せる)。
- 終了コード: 0 = 正常、1 = 対象なし (`why` の id が無い)、2 = 引数 / state の誤り。
"""
import argparse
import contextlib
import datetime
import html
import io
import json
import os
import sys
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import config, debuglog
from .alaya import VALID_KINDS, seed_conditions, seed_kind
# 頌行は注入文とバイト一致させる必要があるので manas の関数を使う (複製・公開化しない)。kind の表示は alaya.seed_kind 1 本
from .manas import _render_seed_line
from .engine import ALL_PROJECTS, MemoryEngine, StateNeedsMigration, StateUnreadable, resolve_state_dir
from .storage import state_status
from .project import resolve_project

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
DEFAULT_LAST = 10
SERVE_DEFAULT_PORT = 8766  # mcp_server.DEFAULT_HTTP_PORT (8765) と別 (test で固定)
SERVE_HOST = "127.0.0.1"  # --host は無い: 外へは開かない
DEBUG_CONFIRM = "conversation-text"
DEBUG_DEFAULT_LAST = 20
DEBUG_VALUE_MAX = 80
NO_DEBUG_LOG = "(デバッグログなし — `<state_dir>/debug` を置くとオン)"
NO_RECORD = "(記録なし)"
GLOBAL_LABEL = "(全域)"


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="raiya-inspect",
        description="頼耶の state を読むだけの観測コマンド (書き換えない)。",
    )
    parser.add_argument("--state-dir", default=None, help="既定: RAIYA_STATE_DIR > 設定ファイルの state_dir > ./state")
    parser.add_argument("--json", action="store_true", help="機械向けの JSON で出す")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="種子の一覧")
    show.add_argument("--project", default=None, help='P の種子と全域の種子。"" は全域だけ。省略は全件')
    show.add_argument("--kind", choices=VALID_KINDS, default=None)
    show.add_argument("--all", action="store_true", help="superseded・取り下げの種子も含める")

    injected = sub.add_parser("injected", help="注入ログの直近 N 件 (新しい順)")
    injected.add_argument("--last", type=int, default=DEFAULT_LAST)

    why = sub.add_parser("why", help="種子の出所を台帳と imprint_log で辿る")
    why.add_argument("seed_id", type=int, metavar="SEED_ID")

    explain = sub.add_parser("explain", help="recall の予行演習 (状態を変えない)")
    explain.add_argument("text", metavar="TEXT")
    explain.add_argument(
        "--project", default=None,
        help='P の種子と全域の種子 (recall と同じ)。省略は cwd の project (raiya-cli と同じ)。"" は全域の種子だけ',
    )
    explain.add_argument("--context", action="extend", nargs="+", default=[], metavar="T")
    explain.add_argument("--session-start", action="store_true")

    debug = sub.add_parser("debug", help="hooks の経過ログ debug.log の直近 N 件 (新しい順)")
    debug.add_argument("--last", type=int, default=DEBUG_DEFAULT_LAST)
    debug.add_argument("--session", default=None, help="session_id が一致するイベントだけ")
    debug.add_argument("--event", default=None, help="event 名が一致するものだけ (recall / stop / consolidate / guard / hook_end)")
    debug.add_argument("--raw", action="store_true", help="JSON 行をそのまま (制御文字は逃がす)")

    serve = sub.add_parser("serve", help="読むだけの Web 表示 (127.0.0.1 のみ・GET のみ)")
    serve.add_argument("--port", type=int, default=SERVE_DEFAULT_PORT, help="既定 {} (0 = 空きポート)".format(SERVE_DEFAULT_PORT))
    return parser


# --- 表示の部品 ---
def _esc(value):
    """記録された文字列を端末へ出す前に制御文字 (Unicode の C* 類: 改行・ESC・ゼロ幅など) を
    `\\xNN` / `\\uNNNN` に置き換える (記録の中身で端末を操作させない)。非 str は str() してから。"""
    text = value if isinstance(value, str) else str(value)
    out = []
    for ch in text:
        if unicodedata.category(ch).startswith("C"):
            code = ord(ch)
            out.append("\\x{:02x}".format(code) if code < 0x100 else "\\u{:04x}".format(code) if code < 0x10000 else "\\U{:08x}".format(code))
        else:
            out.append(ch)
    return "".join(out)


def _esc_lines(text):
    """複数行の記録 (注入文): 行ごとに _esc (改行だけは行の区切りとして残す)。"""
    return [_esc(line) for line in (text if isinstance(text, str) else str(text)).split("\n")]


def _project_label(project):
    return _esc(project) if isinstance(project, str) else GLOBAL_LABEL


def _iso(ts):
    """台帳の ts (epoch 秒) を UTC ISO に。数値でなければそのまま文字列にする。"""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pass
    return str(ts)


def _provenance_summary(seed):
    """由来: 台帳 #28 #31 (user 1 件 / assistant 2 件)。"""
    evidences = [e for e in seed.get("evidences") or [] if isinstance(e, dict)]
    ledger_ids = []
    off_ledger = 0
    for e in evidences:
        lid = e.get("ledger_id")
        if isinstance(lid, int) and not isinstance(lid, bool):
            if lid not in ledger_ids:
                ledger_ids.append(lid)
        else:
            off_ledger += 1
    users = sum(1 for e in evidences if e.get("source") == "user")
    assistants = sum(1 for e in evidences if e.get("source") == "assistant")
    parts = []
    if ledger_ids:
        parts.append("台帳 " + " ".join("#{}".format(i) for i in ledger_ids))
    if off_ledger:
        parts.append("台帳外 {} 件".format(off_ledger))
    if not parts:
        parts.append("根拠なし")
    return "{} (user {} 件 / assistant {} 件)".format(" / ".join(parts), users, assistants)


def _seed_head_lines(seed):
    """種子 1 レコードの ①云何 (id・kind・sansho・project の見出し 1 行) と ②頌 (注入文に出る行と同じ
    `manas._render_seed_line` の行、続けて縁)。show / why が共有する (K1: 云何 → 頌 → 論曰 の順)。"""
    head = "#{} {} project={} {}".format(
        seed.get("id"), _esc(seed_kind(seed)), _project_label(seed.get("project")), _esc(seed.get("sansho"))
    )
    if seed.get("superseded_by") is not None:
        head += " (superseded→#{})".format(seed.get("superseded_by"))
    if seed.get("withdrawn") is not None:
        head += " (取り下げ)"
    conditions = seed_conditions(seed)
    return [
        head,
        "    {}".format(_esc(_render_seed_line(seed))),
        "    縁: {}".format(" / ".join(_esc(c) for c in conditions) if conditions else "(なし)"),
    ]


def _seed_block(seed):
    """show の 1 レコード: 云何 → 頌 → 論曰 (強度 → 由来 = evidences の要約)。"""
    return "\n".join(_seed_head_lines(seed) + [
        "    強度: {}".format(seed.get("strength")),
        "    由来: {}".format(_provenance_summary(seed)),
    ])


def _group_lines(group):
    """why の「群:」ブロック (層化 plan §6)。全文字列は _esc を通す。「代表」ではなく「並びの先頭」と書く。"""
    if not isinstance(group, dict):
        return ["群: なし"]
    ids = group.get("member_ids") or []
    return [
        "群: 「{}」 kind={} project={} {} 件".format(
            _esc(group.get("label")), _esc(group.get("kind")), _project_label(group.get("project")), group.get("size")),
        "    成員: {} (並びの先頭 #{})".format(" ".join("#{}".format(i) for i in ids), group.get("representative")),
    ]


def _withdrawn_lines(seed):
    """why の「取り下げ:」ブロック。保存された record をそのまま描く (全文字列は _esc を通す)。"""
    w = seed.get("withdrawn")
    if w is None:
        return ["取り下げ: なし"]
    if not isinstance(w, dict):
        return ["取り下げ: 中 (記録の形が不正)"]
    lines = ["取り下げ: 中 (判定者={} 時刻={} life={})".format(
        _esc(w.get("by")), _esc(w.get("at")), _esc(w.get("life")))]
    lines.append("    理由: {}".format(_esc(w.get("reason"))))
    basis = []
    raw_ids, raw_spans = w.get("ledger_ids") or [], w.get("spans") or []
    if not isinstance(raw_ids, list) or not isinstance(raw_spans, list):
        lines.append("    根拠: (記録の形が不正)")
        raw_ids, raw_spans = [], []
    ids = [i for i in raw_ids if isinstance(i, int) and not isinstance(i, bool)]
    if ids:
        basis.append("台帳 " + " ".join("#{}".format(i) for i in ids))
    spans = [x for x in raw_spans if isinstance(x, str)]
    if spans:
        basis.append("span: " + " ".join("「{}」".format(_esc(x)) for x in spans))
    if basis:
        lines.append("    根拠: {}".format(" / ".join(basis)))
    if w.get("replaced_by") is not None:
        lines.append("    置き換え: #{}".format(_esc(w.get("replaced_by"))))
    return lines


def _history_line(h):
    line = "    life={} {}: {}".format(h.get("life"), _esc(h.get("reason")), _esc(h.get("content", "")))
    strength = h.get("strength")
    if isinstance(strength, (int, float)) and not isinstance(strength, bool):
        line += " (強度 {})".format(strength)
    rec = h.get("withdrawn") if isinstance(h.get("withdrawn"), dict) else (
        h.get("restored") if isinstance(h.get("restored"), dict) else None)
    if rec is not None:
        line += " (判定者={} 理由={})".format(_esc(rec.get("by")), _esc(rec.get("reason")))
    return line


def _emit_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


# --- サブコマンド ---
def _cmd_show(engine, args):
    project = ALL_PROJECTS if args.project is None else (args.project or None)
    seeds = engine.inspect_seeds(project=project, kind=args.kind, include_superseded=args.all, include_withdrawn=args.all)
    if args.json:
        _emit_json(seeds)
        return EXIT_OK
    if not seeds:
        print("(種子なし)")
        return EXIT_OK
    print("\n\n".join(_seed_block(s) for s in seeds))
    print("\n全 {} 件".format(len(seeds)))
    return EXIT_OK


def _log_field(entry, key, none_label):
    if key not in entry:
        return NO_RECORD
    value = entry.get(key)
    return none_label if value is None else _esc(value)


def _cmd_injected(engine, args):
    if args.last <= 0:
        print("raiya-inspect: --last は 1 以上", file=sys.stderr)
        return EXIT_USAGE
    entries = list(reversed(engine.injection_log(args.last)))
    if args.json:
        _emit_json(entries)
        return EXIT_OK
    if not entries:
        print("(注入ログなし)")
        return EXIT_OK
    blocks = []
    for entry in entries:
        blocks.append("\n".join([
            "時刻={} session={} project={} life={}".format(
                _log_field(entry, "ts", NO_RECORD),
                _log_field(entry, "session_id", "(なし)"),
                _log_field(entry, "project", GLOBAL_LABEL),
                entry.get("life"),
            ),
            "    seed_ids: {}".format(entry.get("seed_ids", [])),
            "    index_ids: {}".format(entry.get("index_ids", [])),
            "    injected_text:",
            "\n".join("      " + line for line in _esc_lines(entry.get("injected_text") or "(空)")),
        ]))
    print("\n\n".join(blocks))
    return EXIT_OK


def _cmd_why(engine, args):
    prov = engine.seed_provenance(args.seed_id)
    # imprint_log.json は seed_provenance の中で遅延読込されるので、破損の警告は読んだ後に確定する。
    late = engine.alaya.imprint_log_warning
    if late:
        print("raiya-inspect: 警告: {}".format(_esc(late)), file=sys.stderr)
    if prov is None:
        print("raiya-inspect: 種子 #{} は無い".format(args.seed_id), file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.json:
        _emit_json(prov)
        return EXIT_OK
    seed = prov["seed"]
    dormant = prov.get("dormant")
    dormant_line = ("休眠: {}(この機械に無い)".format(", ".join(_esc(p) for p in dormant["absent_paths"]))
                    if dormant else "休眠: なし")
    # ①云何 → ②頌 → (状態: 休眠・取り下げ・群) → ③論曰 (強度 → 履歴 → 根拠 = evidences → 決定 = imprint_log → 統合)
    lines = (_seed_head_lines(seed) + ["", dormant_line] + _withdrawn_lines(seed) + _group_lines(prov.get("group"))
             + ["", "強度: {}".format(seed.get("strength")),
                "由来: {}".format(_provenance_summary(seed)), "", "履歴:"])
    history = seed.get("history") or []
    if history:
        for h in history:
            lines.append(_history_line(h))
    else:
        lines.append("    (なし)")
    lines += ["", "根拠:"]
    if not prov["evidences"]:
        lines.append("    (なし)")
    for e in prov["evidences"]:
        lines.append("  - source={} ledger_id={} life={}".format(
            e["source"], e["ledger_id"] if e["ledger_id"] is not None else "(台帳外)", e["life"]))
        lines.append("    span: {}".format(_esc(e["span"])))
        if e["row"] is not None:
            lines.append("    台帳: session={} project={} ts={}".format(
                _esc(e["row"].get("session_id")) if e["row"].get("session_id") is not None else "(なし)",
                _project_label(e["row"].get("project")),
                _iso(e["row"].get("ts")),
            ))
        if e["found"]:
            ex = e["excerpt"]
            lines.append("    {} の {} 文字目: …{}【{}】{}…".format(
                e["body_field"], e["position"], _esc(ex["before"]), _esc(ex["span"]), _esc(ex["after"])))
        else:
            lines.append("    不一致: {}".format(e["problem"]))
    lines += ["", "決定:"]
    if prov["decisions"]:
        for d in prov["decisions"]:
            lines.append("    life={} {}: {}".format(d.get("life"), _esc(d.get("decision")), _esc(d.get("reason"))))
    else:
        lines.append("    決定の記録なし(この機能より前の種子)")
    lines += ["", "統合:"]
    if prov["consolidated"] is None:
        lines.append("    台帳の根拠なし (consolidated_upto={})".format(prov["consolidated_upto"]))
    else:
        lines.append("    {} (consolidated_upto={})".format(
            "統合済" if prov["consolidated"] else "未統合の根拠あり", prov["consolidated_upto"]))
    print("\n".join(lines))
    return EXIT_OK


def _explain_project(arg):
    """explain の --project: 省略 = cwd の project (raiya-cli と同じ `resolve_project(os.getcwd())`)、
    "" = 全域の種子だけ (None)、それ以外 = その project。"""
    if arg is None:
        return resolve_project(os.getcwd())
    return arg or None


def _cmd_explain(engine, args):
    result = engine.explain_recall(
        args.text, context=list(args.context), project=_explain_project(args.project),
        session_start=args.session_start,
    )
    if args.json:
        _emit_json(result)
        return EXIT_OK
    # 表示は explain_recall の戻り値だけで描く (2 度目の lock・別の時点の state を混ぜない)。
    resume = result["resume"] if isinstance(result["resume"], dict) else None
    resume_id = resume["id"] if resume is not None else None

    def _one_line(s):
        return "#{} {} {}: {}".format(s["id"], seed_kind(s), _esc(s.get("sansho")), _esc(s.get("content", "")))

    lines = ["project: {}".format(_project_label(_explain_project(args.project))), "発芽:"]
    for g in result["germinated"]:
        lines.append("  {}  縁: {}".format(_one_line(g), " / ".join(_esc(c) for c in g["matched_conditions"])))
    if not result["germinated"]:
        lines.append("  (なし)")
    circles = result["circles"]
    lines.append("同じ群 (②):")
    for sl in circles["siblings"]:
        parts = ["#{}".format(i) for i in sl["listed"]]
        if sl["more"] > 0:
            parts.append("他に {} 件".format(sl["more"]))
        lines.append("  群「{}」({} 件) ← #{}: {}".format(_esc(sl["label"]), sl["size"], sl["of"], " ".join(parts)))
    if not circles["siblings"]:
        lines.append("  (なし)")
    group_of_rep = {g["representative"]: g for g in circles["groups"]}
    lines.append("索引:")
    for s in result["index"]:
        g = group_of_rep.get(s["id"])
        mark = " [群「{}」{} 件・代表]".format(_esc(g["label"]), g["size"]) if g else ""
        lines.append("  " + _one_line(s) + mark)
    if not result["index"]:
        lines.append("  (なし)")
    lines.append("全域 (④・件数のみ):")
    for g in circles["global"]:
        lines.append("  {}: {} 群・{} 件".format(_esc(g["kind"]), g["groups"], g["seeds"]))
    if not circles["global"]:
        lines.append("  (なし)")
    if args.session_start:
        lines.append("再開点:")
        lines.append("  " + (_one_line(resume) if resume is not None else "(なし)"))
    lines.append("落選 (発芽):")
    for r in result["germination_rejected"]:
        note = " (再開点の枠に回る)" if r["id"] == resume_id else ""
        matched = "  縁: {}".format(" / ".join(_esc(c) for c in r["matched_conditions"])) if r["matched_conditions"] else ""
        lines.append("  {} -- {}{}{}".format(_one_line(r), _esc(r["reason"]), note, matched))
    if not result["germination_rejected"]:
        lines.append("  (なし)")
    lines.append("落選 (索引):")
    for r in result["index_rejected"]:
        lines.append("  {} -- {}".format(_one_line(r), _esc(r["reason"])))
    if not result["index_rejected"]:
        lines.append("  (なし)")
    cut = circles["cut"]
    lines.append("容量: 上限 {} 字・注入 {} 字・落とした ① {} / ② {} / ③ {} / ④ {}".format(
        circles["max_chars"], circles["chars"], cut["germinated"], cut["siblings"], cut["index"], cut["global"]))
    lines.append("注入される文:")
    lines.append("\n".join("  " + line for line in _esc_lines(result["narrative"] or "(空 = 注入しない)")))
    print("\n".join(lines))
    return EXIT_OK


_DEBUG_SKIP_KEYS = frozenset({"ts", "level", "pid", "sub", "event", "outcome", "dur_ms", "session_id"})


def _debug_value(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > DEBUG_VALUE_MAX:
        text = text[:DEBUG_VALUE_MAX] + "…(+{})".format(len(text) - DEBUG_VALUE_MAX)
    return _esc(text)


def _debug_summary(event):
    head = "{} L{} {} {}".format(
        _esc(event.get("ts", NO_RECORD)), _esc(event.get("level", "?")),
        _esc(event.get("sub")) if event.get("sub") is not None else "-", _esc(event.get("event")),
    )
    if "outcome" in event:
        head += " outcome={}".format(_esc(event["outcome"]))
    if "dur_ms" in event:
        head += " dur_ms={}".format(_esc(event["dur_ms"]))
    if event.get("session_id") is not None:
        head += " session={}".format(_esc(event["session_id"]))
    extras = ["{}={}".format(_esc(k), _debug_value(v)) for k, v in event.items() if k not in _DEBUG_SKIP_KEYS]
    return head + ("  " + " ".join(extras) if extras else "")


def _cmd_debug(state_dir, args):
    """読むだけ。engine を作らない (`.lock` も作らない)。ファイルは何も作らない。"""
    if args.last <= 0:
        print("raiya-inspect: --last は 1 以上", file=sys.stderr)
        return EXIT_USAGE
    if not os.path.isdir(state_dir):
        print("raiya-inspect: {} が無い (何も作らずに終了)".format(state_dir), file=sys.stderr)
        return EXIT_USAGE
    events = debuglog.read_events(state_dir)
    events = [
        e for e in events
        if (args.event is None or e.get("event") == args.event)
        and (args.session is None or e.get("session_id") == args.session)
    ]
    events = list(reversed(events))[: args.last]
    if args.json:
        _emit_json(events)
        return EXIT_OK
    if not events:
        base = os.path.join(state_dir, debuglog.LOG_NAME)
        print("(該当イベントなし)" if os.path.exists(base) or os.path.exists(base + ".1") else NO_DEBUG_LOG)
        return EXIT_OK
    for e in events:
        print(_esc(json.dumps(e, ensure_ascii=False)) if args.raw else _debug_summary(e))
    return EXIT_OK


# --- serve: 読むだけの Web 表示 (spec: 2026-10-25-web-viewer-design.md) ---
# 各リクエストは CLI と同じ argv を組み、`main()` をそのまま呼んで stdout/stderr を捕まえ、
# html.escape して <pre> に入れるだけ (読み取り・整形のロジックは増やさない)。
# 書く動詞 (correct_cli) は import しない。`backend` も読まない。
_SERVE_PAGES = ("show", "injected", "why", "explain", "debug")


def _q(query, key):
    values = query.get(key)
    return values[0] if values else None


def _serve_argv(path, query):
    """GET の path + query から CLI の argv (サブコマンド以降) を作る。None = 未知の path。
    値は `--opt=値` / `--` の後ろで渡し、`-` で始まる入力が別の option に化けないようにする。"""
    if path == "show":
        argv = ["show"]
        if _q(query, "global") == "1":
            argv.append("--project=")
        elif _q(query, "project"):
            argv.append("--project=" + _q(query, "project"))
        if _q(query, "kind"):
            argv.append("--kind=" + _q(query, "kind"))
        if _q(query, "all"):
            argv.append("--all")
        return argv
    if path == "injected":
        return ["injected"] + (["--last=" + _q(query, "last")] if _q(query, "last") else [])
    if path == "why":
        return ["why", "--", _q(query, "seed_id") or ""]
    if path == "explain":
        argv = ["explain"]
        if _q(query, "global") == "1":
            argv.append("--project=")
        elif _q(query, "project"):
            argv.append("--project=" + _q(query, "project"))
        for c in query.get("context", []):
            if c:
                argv.append("--context=" + c)
        if _q(query, "session_start"):
            argv.append("--session-start")
        return argv + ["--", _q(query, "text") or ""]
    if path == "debug":
        argv = ["debug"]
        if _q(query, "last"):
            argv.append("--last=" + _q(query, "last"))
        if _q(query, "session"):
            argv.append("--session=" + _q(query, "session"))
        if _q(query, "event"):
            argv.append("--event=" + _q(query, "event"))
        if _q(query, "raw"):
            argv.append("--raw")
        return argv
    return None


def _run_cli_captured(state_dir, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(["--state-dir", state_dir] + argv)
        except SystemExit as e:  # argparse の誤り
            code = e.code if isinstance(e.code, int) else EXIT_USAGE
    return code, out.getvalue(), err.getvalue()


def _form(action, fields, note=""):
    rows = []
    for name, label, kind in fields:
        if kind == "check":
            rows.append('<label><input type="checkbox" name="{}" value="1"> {}</label>'.format(name, html.escape(label)))
        else:
            rows.append('<label>{} <input type="text" name="{}"></label>'.format(html.escape(label), name))
    return '<form method="get" action="/{}"><h2>{}</h2>{}<input type="submit" value="見る"></form>'.format(
        action, action, "<br>".join(rows) + (("<br>" + html.escape(note)) if note else ""))


def _page(title, body_html):
    nav = " | ".join('<a href="/{0}">{0}</a>'.format(p) for p in _SERVE_PAGES if p != "debug")
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>{}</title></head><body>'
        '<p><a href="/">raiya-inspect</a> | {}</p>{}</body></html>'
    ).format(html.escape(title), nav, body_html)


def _pre(text):
    return "<pre>{}</pre>".format(html.escape(text))


_KIND_NOTE = "種類: " + " / ".join(VALID_KINDS)
_FORMS = {
    "show": [("project", "project (空 = 全件)", "text"), ("global", "全域だけ", "check"),
             ("kind", _KIND_NOTE, "text"), ("all", "superseded・取り下げも含める", "check")],
    "injected": [("last", "last (件数)", "text")],
    "why": [("seed_id", "種子 id", "text")],
    "explain": [("text", "text", "text"), ("project", "project (空 = cwd)", "text"), ("global", "全域だけ", "check"),
                ("context", "context (1 件)", "text"), ("session_start", "session-start", "check")],
}


def _index_page():
    forms = "".join(_form(name, fields) for name, fields in _FORMS.items())
    debug = ('<h2>debug</h2><p>debug.log (レベル 2 は会話本文を含む) は既定では出さない。'
             '見るなら <code>/debug?confirm={}</code></p>').format(DEBUG_CONFIRM)
    return _page("raiya-inspect", "<h1>raiya-inspect (読むだけ)</h1>" + forms + debug)


def _make_handler(state_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "raiya-inspect"
        timeout = 10  # 無通信の接続が単一スレッドのサーバを止めないように

        def log_message(self, fmt, *a):  # 会話本文が query に入りうるのでアクセスログは出さない
            pass

        def _send(self, status, body):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 405:
                self.send_header("Allow", "GET")
            try:
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def send_error(self, code, message=None, explain=None):
            if code == 501:  # 未対応の method も GET 以外は 405
                code = 405
            self._send(code, _page(str(code), _pre("{} (読むだけの窓: GET のみ)".format(code))))

        def _not_allowed(self):
            self._send(405, _page("405", _pre("405 GET のみ")))

        do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

        def _host_ok(self):
            # DNS rebinding 対策: Host が自分の loopback 名 + 実ポートのときだけ受ける
            port = self.server.server_address[1]
            if len(self.headers.get_all("Host") or []) != 1:
                return False
            return self.headers.get("Host") in ("127.0.0.1:{}".format(port), "localhost:{}".format(port))

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, _page("403", _pre("403 Host が違う")))
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            path = parsed.path.strip("/")
            if path == "":
                return self._send(200, _index_page())
            if path not in _SERVE_PAGES:
                return self._send(404, _page("404", _pre("404 そのページは無い")))
            if path == "debug" and _q(query, "confirm") != DEBUG_CONFIRM:
                return self._send(404, _page("debug", _pre(
                    "debug.log はレベル 2 だと会話本文を含むため既定では出さない。\n"
                    "見るなら /debug?confirm={} を付ける。".format(DEBUG_CONFIRM))))
            if path == "explain" and _q(query, "text") is None:  # CLI の usage と同じ (text 必須)
                return self._send(400, _page(path, _form(path, _FORMS[path]) + _pre("text が要る")))
            argv = _serve_argv(path, query)
            # 単一スレッドの HTTPServer: リクエストは直列に処理されるので、プロセス全体の
            # redirect_stdout/redirect_stderr の捕捉は競合しない (ThreadingHTTPServer にしないこと)。
            try:
                code, out, err = _run_cli_captured(state_dir, argv)
            except Exception as e:  # 本文は返さず型名だけ
                return self._send(500, _page("500", _pre("500 {}".format(type(e).__name__))))
            status = {EXIT_OK: 200, EXIT_NOT_FOUND: 404}.get(code, 400)
            # 設定 issue の detail (パス・キー名) は HTTP に出さない: その 1 行は固定文に置き換える
            # 行は "\n" だけで分ける (describe_issues が置き換えるのは "\n" だけ。\r・U+0085・U+2028 で割ると残りが漏れる)
            err = "\n".join(("raiya-inspect: 設定ファイルに問題あり (詳細は serve を起動した端末の stderr)"
                             if "設定ファイルが不正" in ln else ln) for ln in err.split("\n"))
            text = out + (("\n--- stderr ---\n" + err) if err else "")
            body = _pre(text)
            forms = _form(path, _FORMS[path]) if path in _FORMS else ""
            self._send(status, _page(path, forms + body))

    return Handler


def make_server(state_dir, port=SERVE_DEFAULT_PORT):
    return HTTPServer((SERVE_HOST, port), _make_handler(state_dir))


def _state_ok(state_dir):
    """engine を作る前の検査 (engine の構築は state dir と `.lock` を作るので、作る前に止める)。`raiya.db` がある
    (`state_status` == `db`) ときだけ True。旧 JSON 版 (`legacy`) の案内の詳細は移行スライス (4) で足す。"""
    status = state_status(state_dir)
    if status == "db":
        return True
    if status == "legacy":
        print("raiya-inspect: {} は旧 JSON 版の state です (移行しない。脇に退けてから起動。何も作らずに終了)".format(state_dir), file=sys.stderr)
    else:
        print("raiya-inspect: {} に raiya.db が無い (何も作らずに終了)".format(state_dir), file=sys.stderr)
    return False


def _cmd_serve(state_dir, args):
    if not _state_ok(state_dir):
        return EXIT_USAGE
    if not 0 <= args.port <= 65535:
        print("raiya-inspect: 待ち受けできない (--port は 0〜65535)", file=sys.stderr)
        return EXIT_USAGE
    try:
        server = make_server(state_dir, args.port)
    except OSError as e:
        print("raiya-inspect: 待ち受けできない ({})".format(type(e).__name__), file=sys.stderr)
        return EXIT_USAGE
    print("raiya-inspect: http://{}:{}/ (読むだけ・Ctrl-C で終了)".format(SERVE_HOST, server.server_address[1]), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


_COMMANDS = {"show": _cmd_show, "injected": _cmd_injected, "why": _cmd_why, "explain": _cmd_explain}


def main(argv=None):
    args = _build_parser().parse_args(argv)
    # 設定ファイルは state dir の外・読むだけ。報告は stderr (state dir には何も書かない)。
    if not config.startup_check("raiya-inspect", explicit_state_dir=bool(args.state_dir)):
        return EXIT_USAGE
    state_dir = resolve_state_dir(args.state_dir)
    if args.command == "debug":
        return _cmd_debug(state_dir, args)  # engine を作らない・alaya.json 不要 (debug.log を直接読む)
    if args.command == "serve":
        return _cmd_serve(state_dir, args)
    # engine の構築は state dir を makedirs し `.lock` を作る。読むだけのコマンドが空の state を
    # 作らないよう、alaya.json が無ければ構築せずに止める。
    if not _state_ok(state_dir):
        return EXIT_USAGE
    try:
        engine = MemoryEngine(state_dir=state_dir, new_life=False)
        for warning in engine.warnings:
            print("raiya-inspect: 警告: {}".format(warning.strip()), file=sys.stderr)
        return _COMMANDS[args.command](engine, args)
    except StateUnreadable as e:
        print("raiya-inspect: 状態DB 読めず category={} (何も書かずに終了)".format(e.category), file=sys.stderr)
        return EXIT_USAGE
    except StateNeedsMigration:
        print("raiya-inspect: {} は旧 JSON 版の state です (移行しない。脇に退けてから起動。何も作らずに終了)".format(state_dir), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
