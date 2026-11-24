"""correct_cli.py -- `raiya-correct`: 人間が種子を取り下げ・置き換え・復帰させる窓

plan: 2026-10-25-correction-withdrawal-implementation.md §1・§7。

    raiya-correct [--state-dir DIR] [--json] withdraw SEED_ID --by NAME --reason TEXT
                  (--ledger-id N ... | --span TEXT ... | 両方)
    raiya-correct [--state-dir DIR] [--json] replace  SEED_ID --by NAME --reason TEXT
                  (--ledger-id N ... | --span TEXT ...) --new-content TEXT --new-kind KIND
                  --new-condition W ... --new-span S ... [--window-ledger-id N ...] [--new-polarity P]
    raiya-correct [--state-dir DIR] [--json] restore  SEED_ID --by NAME --reason TEXT

規律:
- 書く判断は人間の宣言 (`--by` は明示必須・env / ユーザ名からの補完なし)。プログラムは宣言の真偽を判定せず、
  根拠が種子の evidences に属すかの所属検査だけを行う。三性ゲートは自己申告を読まない。
- 変更は engine の 1 verb = 1 回の `_locked` (`withdraw_seed` / `replace_seed` / `restore_seed`)。
  拒否は何も書かない。`backend` を import しない。`hooks.log` / `debug.log` は書かない。
- state dir の解決と起動は `inspect_cli.main` の写し。`raiya.db` が無ければ (`state_status` が `db` 以外) engine を作る前に exit 2。
- 終了コード: 0 = 書いた、1 = 拒否 (WithdrawalRefused・何も書かない)、2 = 引数 / state の誤り。
"""
import argparse
import json
import os
import sys

from . import config
from .alaya import VALID_KINDS, VALID_POLARITIES
from .engine import MemoryEngine, StateNeedsMigration, StateUnreadable, WithdrawalRefused, resolve_state_dir
from .storage import state_status
from .inspect_cli import _esc  # 記録された文字列の端末出力用 (inspect と同じ。inspect_cli は backend を読まない)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
PROG = "raiya-correct"


def _build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="頼耶の種子を人間が取り下げ・置き換え・復帰させるコマンド (種子は消えない)。",
    )
    parser.add_argument("--state-dir", default=None, help="既定: RAIYA_STATE_DIR > 設定ファイルの state_dir > ./state")
    parser.add_argument("--json", action="store_true", help="機械向けの JSON で出す")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p, basis):
        p.add_argument("seed_id", type=int, metavar="SEED_ID")
        p.add_argument("--by", required=True, help="判定者の名前 (必須・補完なし)")
        p.add_argument("--reason", required=True, help="理由 (必須)")
        if basis:
            p.add_argument("--ledger-id", dest="ledger_ids", action="append", type=int, default=[], metavar="N",
                           help="根拠の台帳の行 id (繰り返し可)")
            p.add_argument("--span", dest="spans", action="append", default=[], metavar="TEXT",
                           help="台帳外の根拠の引用 (繰り返し可・保存済み span と完全一致)")

    withdraw = sub.add_parser("withdraw", help="種子を取り下げる")
    _common(withdraw, True)

    replace = sub.add_parser("replace", help="種子を取り下げ、代わりの種子を植える")
    _common(replace, True)
    replace.add_argument("--new-content", required=True)
    replace.add_argument("--new-kind", required=True, choices=VALID_KINDS)
    replace.add_argument("--new-condition", dest="new_conditions", action="append", required=True, default=[], metavar="W")
    replace.add_argument("--new-span", dest="new_spans", action="append", required=True, default=[], metavar="S")
    replace.add_argument("--window-ledger-id", dest="window_ledger_ids", action="append", type=int, default=None, metavar="N",
                         help="置き換えの接地に使う台帳の行 (既定: --ledger-id)")
    replace.add_argument("--new-polarity", choices=VALID_POLARITIES, default="無記")

    restore = sub.add_parser("restore", help="取り下げを戻す")
    _common(restore, False)
    return parser


def _usage_error(message):
    print("{}: {}".format(PROG, _esc(message)), file=sys.stderr)
    return EXIT_USAGE


def _print_record(label, seed_id, record, life_key="life", extra_lines=()):
    print("{}: #{}".format(label, seed_id))
    print("    判定者: {}".format(_esc(record.get("by"))))
    print("    時刻: {}".format(_esc(record.get("at"))))
    print("    理由: {}".format(_esc(record.get("reason"))))
    for line in extra_lines:
        print("    {}".format(line))
    print("    生: {}".format(_esc(record.get(life_key))))


def _basis_line(record):
    parts = []
    if record.get("ledger_ids"):
        parts.append("台帳 " + " ".join("#{}".format(i) for i in record["ledger_ids"]))
    if record.get("spans"):
        parts.append("span: " + " ".join("「{}」".format(_esc(s)) for s in record["spans"]))
    return "根拠: " + " / ".join(parts) if parts else "根拠: (なし)"


def _replacement_label(rep):
    if rep.get("reinforced"):
        return "併合"
    if rep.get("superseded"):
        return "再開点の置換"
    return "新規" if rep.get("is_new") else "併合"


def _print_result(command, result):
    if command == "restore":
        _print_record("復帰", result["seed_id"], result["restored"])
        return
    record = result["withdrawn"]
    _print_record("取り下げ", result["seed_id"], record, extra_lines=[_basis_line(record)])
    rep = result.get("replacement")
    if rep:
        print("置き換え: #{} ({}) クラス={} kind={} project={}".format(
            rep["seed_id"], _replacement_label(rep), _esc(rep.get("sansho")), _esc(rep.get("kind")),
            _esc(rep["project"]) if isinstance(rep.get("project"), str) else "(全域)"))


def main(argv=None):
    args = _build_parser().parse_args(argv)
    # 判断のうち engine を作る前に決まるものは全部ここ (exit 2)。
    args.by = args.by.strip()
    args.reason = args.reason.strip()
    if not args.by:
        return _usage_error("--by が空 (判定者の名前は明示する)")
    if not args.reason:
        return _usage_error("--reason が空")
    if args.command in ("withdraw", "replace"):
        if not args.ledger_ids and not args.spans:
            return _usage_error("根拠が無い (--ledger-id か --span が要る)")
    if args.command == "replace":
        if not args.window_ledger_ids and not args.ledger_ids:
            return _usage_error("--window-ledger-id が要る (--ledger-id が無いので窓を決められない)")
        if not args.new_content.strip():
            return _usage_error("--new-content が空")
    # 設定ファイルは state dir の外・読むだけ。報告は stderr。
    if not config.startup_check(PROG, explicit_state_dir=bool(args.state_dir)):
        return EXIT_USAGE
    state_dir = resolve_state_dir(args.state_dir)
    # engine の構築は state dir を makedirs し `.lock` を作る。空の state を作らないよう先に止める。
    status = state_status(state_dir)
    if status != "db":
        if status == "legacy":
            print("{}: {} は旧 JSON 版の state です (移行しない。脇に退けてから起動。何も作らずに終了)".format(PROG, _esc(state_dir)), file=sys.stderr)
        else:
            print("{}: {} に raiya.db が無い (何も作らずに終了)".format(PROG, _esc(state_dir)), file=sys.stderr)
        return EXIT_USAGE
    try:
        engine = MemoryEngine(state_dir=state_dir, new_life=False)
    except StateUnreadable as e:
        print("{}: 状態DB 読めず category={} (何も書かずに終了)".format(PROG, e.category), file=sys.stderr)
        return EXIT_USAGE
    except StateNeedsMigration:
        print("{}: {} は旧 JSON 版の state です (移行しない。脇に退けてから起動。何も作らずに終了)".format(PROG, _esc(state_dir)), file=sys.stderr)
        return EXIT_USAGE
    for warning in engine.warnings:
        print("{}: 警告: {}".format(PROG, _esc(warning.strip())), file=sys.stderr)
    try:
        if args.command == "withdraw":
            result = engine.withdraw_seed(args.seed_id, args.by, args.reason, args.ledger_ids, args.spans)
        elif args.command == "replace":
            proposal = {
                "content": args.new_content,
                "conditions": args.new_conditions,
                "kind": args.new_kind,
                "polarity": args.new_polarity,
                "spans": args.new_spans,
            }
            result = engine.replace_seed(
                args.seed_id, args.by, args.reason, args.ledger_ids, args.spans, proposal,
                window_ledger_ids=args.window_ledger_ids,
            )
        else:
            result = engine.restore_seed(args.seed_id, args.by, args.reason)
    except WithdrawalRefused as exc:
        print("{}: {}".format(PROG, _esc(str(exc))), file=sys.stderr)
        return EXIT_REFUSED
    except StateUnreadable as e:
        print("{}: 状態DB 読めず category={} (何も書かずに終了)".format(PROG, e.category), file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_result(args.command, result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
