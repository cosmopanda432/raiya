#!/usr/bin/env python3
"""manual_smoke_run.py -- 記憶機能設計書§5.2 実LLM smoke(手動1本・テストに混ぜない)

claude-cli backend でコーディングの会話 (10往復・決定/回避策/所在/再開点/性向の言明を含む) を
`python3 -m raiya.cli` へ subprocess 経由で供給し、生成された raiya.db (sqlite3)
を外側から計測して receipts/smoke.json へ書き出す。

規律:
    - `unittest discover` には拾われない (ファイル名が test*.py でない)。
    - state は一時ディレクトリ (本番 state/ は汚さない、RAIYA_STATE_DIR で隔離)。
    - claude CLI が認証等で構造的に失敗したら、偽装せず blocked 理由を記録して終える
      (P-M1/P-M2 は judged 不能と記す=README 側の責務)。
    - 手動1本の規律: receipts/smoke.json が既に存在し成功していれば再走しない
      (再走するには --force)。

実行: python3 scripts/manual_smoke_run.py [--force]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (SCRIPTS_DIR, BASE_DIR):  # 兄弟スクリプトと raiya パッケージ (ルート) を import 可能にする
    if _p not in sys.path:
        sys.path.insert(0, _p)

# コーディングの会話 (10往復)。改修5 以降 propose はコーディングの続きで忘れて困るものだけを記録する
# (決定・回避策・所在・再開点・性向。Issue #10) ので、日常会話ではなくその種の内容を言明する。
SCRIPTED_TURNS = [
    "こんにちは、はじめまして。よろしくお願いします。",
    "設定の保存形式は TOML を採用して、YAML は却下しました。理由は、インデントのずれで設定が静かに壊れたからです。",
    "テストで時刻まわりが時々落ちます。原因はタイムゾーンが UTC 固定でないことで、TZ=UTC を付けて実行すると回避できます。",
    "本番用の API キーはリポジトリには置かず、1Password の「deploy」ボルトにあります。",
    "ログ出力は標準の logging を採用して、loguru は却下です。依存を増やしたくないからです。",
    "CI で pip install が遅い件は、キャッシュのキーに lock ファイルのハッシュを入れていなかったのが原因でした。キーに含めたら回避できました。",
    "コミットメッセージは必ず日本語で書く方針です。",
    "デプロイ手順書は社内 Wiki の「運用/リリース」ページにあります。リポジトリの docs には置いていません。",
    "今日はここまでにします。次は認証ミドルウェアのテストから再開して、トークン期限切れのケースを足します。",
    "今日は色々相談できて助かりました。ありがとうございました。",
]

SUBPROCESS_TIMEOUT_SEC = 1800  # 10往復 x 最大2 LLM呼出/往復 の余裕を見た上限


def _receipts_dir():
    return os.path.join(BASE_DIR, "receipts")


def _write(path, receipt):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _measure_from_state(state_dir):
    """state dir を read-only の engine 動詞 (`inspect_seeds` / `imprint_log_rows` / `audit`) で読み、
    外側から集計する (保存形式 = ファイルの名前や JSON の形を知らない)。
    """
    from raiya.engine import MemoryEngine

    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    log = engine.imprint_log_rows()
    seeds = engine.inspect_seeds(include_superseded=True, include_withdrawn=True)
    quarantine_retained = engine.audit()["quarantine_retained"]
    decision_counts = Counter(e["decision"] for e in log)
    reject_reasons = Counter(e["reason"] for e in log if e["decision"] == "reject")
    quarantine_reasons = Counter(e["reason"] for e in log if e["decision"] == "遍計所執")
    class_counts = {
        "圓成實": decision_counts.get("圓成實", 0),
        "依他起": decision_counts.get("依他起", 0),
        "遍計所執": decision_counts.get("遍計所執", 0),
        "reject": decision_counts.get("reject", 0),
    }
    return {
        "proposal_total": len(log),
        "class_counts": class_counts,
        "reject_reason_distribution": dict(reject_reasons),
        "quarantine_reason_distribution": dict(quarantine_reasons),
        "quarantine_count": class_counts["遍計所執"],  # 累計 (imprint_log 集計。保持上限の影響を受けない)
        "quarantine_retained_count": quarantine_retained,
        "seed_count": len(seeds),
    }


def run_smoke(force=False):
    receipts_dir = _receipts_dir()
    os.makedirs(receipts_dir, exist_ok=True)
    out_path = os.path.join(receipts_dir, "smoke.json")

    if os.path.exists(out_path) and not force:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if not existing.get("blocked"):
            print(
                "receipts/smoke.json は既に成功済みです (手動1本の規律=再走しない)。"
                " 再走するには --force を指定してください。",
                file=sys.stderr,
            )
            return existing

    # 意図的に片付けない (他の一時ディレクトリと違い): 手動1本の smoke は結果を人が
    # 目視で追検分できることに価値があるため、生成された raiya.db (種子・imprint_log・manas_log を含む)
    # を実行後も tmp_state_dir に残す (パスは receipts/smoke.json の
    # "state_dir" に記録)。
    tmp_state_dir = tempfile.mkdtemp(prefix="raiya_smoke_state_")
    stdin_text = "\n".join(SCRIPTED_TURNS + ["/終"]) + "\n"
    env = dict(os.environ)
    env["RAIYA_BACKEND"] = "claude-cli"
    env["RAIYA_STATE_DIR"] = tmp_state_dir
    env["PYTHONPATH"] = BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")

    started_at = _now()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "raiya.cli"],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            # Issue #29 (refs #26): cwd をリポジトリ直下にすると会話応答の `claude -p` が
            # AGENTS.md を読む。git 外の一時 state dir から起動する (import は PYTHONPATH で解決)。
            cwd=tmp_state_dir,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        receipt = {
            "generated_at": _now(),
            "started_at": started_at,
            "blocked": True,
            "reason": "raiya.cli subprocess がタイムアウトしました ({}秒): {}".format(
                SUBPROCESS_TIMEOUT_SEC, e
            ),
            "turns_scripted": len(SCRIPTED_TURNS),
        }
        _write(out_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return receipt
    except FileNotFoundError as e:
        receipt = {
            "generated_at": _now(),
            "started_at": started_at,
            "blocked": True,
            "reason": "raiya.cli の起動に失敗しました: {}".format(e),
            "turns_scripted": len(SCRIPTED_TURNS),
        }
        _write(out_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return receipt

    # 子が走ったか: 保存形式 (ファイル名) ではなく engine 越しに見る。ただし存在しない dir に
    # engine を作ると dir と .lock を作って零件で返るため、先に isdir で見る。子は
    # MemoryEngine(new_life=True) を作るので life >= 1 になっているはず。
    state_readable = False
    if os.path.isdir(tmp_state_dir):
        try:
            from raiya.engine import MemoryEngine

            state_readable = MemoryEngine(state_dir=tmp_state_dir, new_life=False).audit()["life"] >= 1
        except Exception:
            state_readable = False
    if not state_readable or result.returncode != 0:
        receipt = {
            "generated_at": _now(),
            "started_at": started_at,
            "blocked": True,
            "reason": (
                "raiya.cli が正常終了しなかったか state が生成されませんでした "
                "(returncode={}, stdout_tail={!r}, stderr_tail={!r})"
            ).format(result.returncode, result.stdout[-2000:], result.stderr[-2000:]),
            "turns_scripted": len(SCRIPTED_TURNS),
        }
        _write(out_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return receipt

    measured = _measure_from_state(tmp_state_dir)
    receipt = {
        "generated_at": _now(),
        "started_at": started_at,
        "blocked": False,
        "backend": "claude-cli",
        "turns_scripted": len(SCRIPTED_TURNS),
        "state_dir": tmp_state_dir,  # 意図的に未削除 (上記コメント参照)。追検分用
        "stdout_tail": result.stdout[-4000:],
        **measured,
    }
    _write(out_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    print("\n--- written to {} ---".format(out_path))

    _write_smoke_judgment(receipts_dir, measured)
    return receipt


def _write_smoke_judgment(receipts_dir, measured):
    """P-M1/P-M2 を実測 (measured) に対して機械述語で判定し receipts/judgment_smoke.json
    へ書く。measured から stdout_tail 等の transcript を落とした軽量な dict のみを
    埋め込む (judge_p_m1/judge_p_m2 が読むキーは quarantine_count/class_counts のみ)。
    """
    import receipt_harness

    slim_measured = {
        k: v
        for k, v in measured.items()
        if k in ("proposal_total", "class_counts", "reject_reason_distribution", "quarantine_count", "seed_count")
    }
    judgment = {
        "P-M1": receipt_harness.judge_p_m1(slim_measured),
        "P-M2": receipt_harness.judge_p_m2(slim_measured),
    }
    out_path = os.path.join(receipts_dir, "judgment_smoke.json")
    _write(out_path, judgment)
    print(json.dumps(judgment, ensure_ascii=False, indent=2))
    print("\n--- written to {} ---".format(out_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="既存の receipts/smoke.json を再走して上書きする")
    args = parser.parse_args()
    run_smoke(force=args.force)


if __name__ == "__main__":
    main()
