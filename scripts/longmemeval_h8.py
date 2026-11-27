"""longmemeval_h8.py -- LongMemEval を使った H8 (却下の印を疑の文の外に限る) の測定

設計書 2026-11-27-reject-marker-outside-doubt-design §8 T3。LLM は使わない・決定論。段 1・段 2
(`longmemeval_stage1.py` / `longmemeval_stage2.py`) の関数を import して、**今の木の規則で**数える (en の表は差し替えない)。
前後の比較は、古いコミットの木 (`git archive <commit> raiya scripts` で別の dir に取り出し、このスクリプトを写したもの) と
今の木で同じスクリプトを走らせ、`--compare OLD NEW` で 2 つの JSON の差を出す。

使い方:
    python3 scripts/longmemeval_h8.py --data PATH/longmemeval_s_cleaned.json --out DIR [--name h8.json] [--quiet]
    python3 scripts/longmemeval_h8.py --compare OLD.json NEW.json --out DIR [--quiet]      # DIR/h8-compare.json

- データは引数で受け取る。リポジトリへ写さない・ネットワークに出ない。結果の JSON は `--out` (必須) にだけ書く。
- 実 LLM は呼ばない (`raiya.backend` を import しない)。`RAIYA_CONFIG=""` を強いる。
- ファイル名が test*.py でないので `unittest discover` には拾われない。

出力 JSON (`schema` = "longmemeval-h8/1"): meta / reject_scope / proposals / injected。
"""
import argparse
import datetime
import json
import os
import platform
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (BASE_DIR, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import longmemeval_stage1 as lm1  # noqa: E402
import longmemeval_stage2 as lm2  # noqa: E402

DEFAULT_NAME = "h8.json"
COMPARE_NAME = "h8-compare.json"
SCHEMA = "longmemeval-h8/1"


def reject_scope_counts(corpora):
    """コーパス名 → {"texts": 本文の数, "whole": span 全体に却下の印のある数, "outside_doubt": 疑の文を除いた残りに印のある数}。
    `outside_doubt` は窓なし (`_sentence_pieces(本文)` の残り。`form_in_user_evidence` の形)。"""
    from raiya import alaya, sansho

    out = {}
    for name, texts in corpora.items():
        whole = outside = 0
        for text in texts:
            if not alaya._has_marker(sansho._n(text), alaya.DECISION_REJECT_MARKERS):
                continue
            whole += 1
            n, pieces = alaya._sentence_pieces(text)
            if alaya._has_marker(alaya._remainder_from_offset(n, n, pieces, 0), alaya.DECISION_REJECT_MARKERS):
                outside += 1
        out[name] = {"texts": len(texts), "whole": whole, "outside_doubt": outside}
    return out


def proposal_kinds(data):
    """根拠の提案ごとの {"question_id", "index", "declared", "sansho", "kind"} の list (今の木の規則)。
    `index` は `longmemeval_stage2._evidence_proposals` が問いごとに出す順の番号 (0 から)。"""
    rows = []
    for q in data:
        for declared in lm2.DECLARED_KINDS:
            for index, (proposal, window, _span, _conds) in enumerate(lm2._evidence_proposals([q], declared)):
                decision, kind = lm2._judge(proposal, window)
                rows.append({"question_id": q["question_id"], "index": index, "declared": declared,
                             "sansho": decision["sansho"], "kind": kind})
    return rows


def injected_counts(data):
    """腕 (例 "A-a") → {クラス: 問いの数}。段 1 の `run_question(q, ("A",), False)` を全問で回して数える (実エンジン・今の木の規則)。"""
    from raiya import config

    arms = {}
    with lm1._env(RAIYA_CONFIG=""):
        config._reset()
        try:
            for q in data:
                row, _shape, _timing = lm1.run_question(q, ("A",), False)
                for arm, arm_row in row["arms"].items():
                    classes = arms.setdefault(arm, {})
                    classes[arm_row["class"]] = classes.get(arm_row["class"], 0) + 1
        finally:
            config._reset()
    return arms


def compare(old, new):
    """2 つの結果 (本ファイルの出力 JSON を読んだ dict) の差。
    kind_changed = 同じ (question_id, index, declared) で kind が違う項の list (旧・新の kind を付ける)、
    kind_totals = "<declared>/<kind>" → (旧の本数, 新の本数)、injected = 腕 → クラス → (旧, 新)。
    kind_changed は旧と新の両方にある項だけを出す。片方にしか無い項は kind_changed に出ず、kind_totals の本数の差にだけ現れる。"""
    def keyed(result):
        return {(r["question_id"], r["index"], r["declared"]): r for r in result.get("proposals", [])}

    old_rows, new_rows = keyed(old), keyed(new)
    changed = []
    for key in sorted(new_rows):
        if key in old_rows and old_rows[key]["kind"] != new_rows[key]["kind"]:
            r = dict(new_rows[key])
            del r["kind"]
            r["old_kind"], r["new_kind"] = old_rows[key]["kind"], new_rows[key]["kind"]
            changed.append(r)

    def totals(rows):
        counts = {}
        for r in rows.values():
            k = "{}/{}".format(r["declared"], r["kind"])
            counts[k] = counts.get(k, 0) + 1
        return counts

    t_old, t_new = totals(old_rows), totals(new_rows)
    kind_totals = {k: (t_old.get(k, 0), t_new.get(k, 0)) for k in sorted(set(t_old) | set(t_new))}
    i_old, i_new = old.get("injected", {}), new.get("injected", {})
    inj = {}
    for arm in sorted(set(i_old) | set(i_new)):
        o, n = i_old.get(arm, {}), i_new.get(arm, {})
        inj[arm] = {c: (o.get(c, 0), n.get(c, 0)) for c in sorted(set(o) | set(n))}
    return {"kind_changed": changed, "kind_totals": kind_totals, "injected": inj}


def _write(out_dir, name, obj):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="LongMemEval H8 (却下の印を疑の文の外に限る・LLM 不使用・決定論) の測定")
    ap.add_argument("--data", help="longmemeval_*.json の path (リポジトリの外。--compare のときは要らない)")
    ap.add_argument("--out", required=True, help="結果の dir (必須。リポジトリの外を指す)")
    ap.add_argument("--name", default=DEFAULT_NAME, help="結果のファイル名")
    ap.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="2 つの結果 JSON の差を --out/{} に書く".format(COMPARE_NAME))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    progress = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    if args.compare:
        results = []
        for path in args.compare:
            try:
                with open(path, encoding="utf-8") as f:
                    results.append(json.load(f))
            except (OSError, ValueError) as e:
                print("結果を読めません: {} ({})".format(path, e.__class__.__name__), file=sys.stderr)
                return 1
        print("wrote {}".format(_write(args.out, COMPARE_NAME, compare(results[0], results[1]))))
        return 0
    if not args.data:
        print("--data か --compare が要る", file=sys.stderr)
        return 2
    try:
        data, sha, nbytes = lm1.load_data(args.data)
    except lm1.DataError as e:
        print(str(e), file=sys.stderr)
        return 1
    from raiya import config

    with lm1._env(RAIYA_CONFIG=""):
        config._reset()
        try:
            progress("corpora ...")
            corpora = lm2.build_corpora(data)
            result = {
                "schema": SCHEMA,
                "meta": {
                    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "data_sha256": sha, "data_bytes": nbytes, "questions": len(data),
                    "python": platform.python_version(),
                },
                "reject_scope": reject_scope_counts(corpora),
                "proposals": [],
                "injected": {},
            }
            progress("proposals ...")
            result["proposals"] = proposal_kinds(data)
            progress("injected ...")
            result["injected"] = injected_counts(data)
        finally:
            config._reset()
    print("wrote {}".format(_write(args.out, args.name, result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
