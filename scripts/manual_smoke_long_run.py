#!/usr/bin/env python3
"""manual_smoke_long_run.py -- 記憶機能設計書§5.3 P-M1 再測(手動1本・テストに混ぜない)

P-M1(実LLMの提案で非接地span/spanなしが≥1件出る)は 10往復(§5.2 通常 smoke)では
cosmicanalysis r2・raiya とも miss だった(quarantine 0件)。§5.3 の外れ時処方は
「往復数を増やして再測」。本runnerは claude-cli backend で **40往復** を
`python3 -m raiya.cli` へ供給し、通常 smoke より長い・かつ LLM が記憶を「作り込みたく
なる」誘導(曖昧な言及の蒸し返し・ユーザーが言っていない前提の質問・長い応答を引き出す
話題・数値/固有名詞の多い話題)を混ぜた台本で再測する。

規律:
    - `unittest discover` には拾われない (ファイル名が test*.py でない)。
    - state は一時ディレクトリ (本番 state/ は汚さない、RAIYA_STATE_DIR で隔離)。
    - claude CLI が認証等で構造的に失敗したら、偽装せず blocked 理由を記録して終える。
    - 手動1本の規律はレシート名ごと(smoke.json とは独立): receipts/smoke_long.json が
      既に成功済みであれば再走しない(再走するには --force)。
    - P-M1/P-M2 の判定は §5.2 と**同じ凍結述語** (receipt_harness.judge_p_m1/judge_p_m2)
      を使う。元の10往復 smoke の miss は上書きしない(別ファイル=judgment_smoke_long.json)。

実行: python3 scripts/manual_smoke_long_run.py [--force]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (SCRIPTS_DIR, BASE_DIR):  # 兄弟スクリプトと raiya パッケージ (ルート) を import 可能にする
    if _p not in sys.path:
        sys.path.insert(0, _p)

import manual_smoke_run  # noqa: E402 (_measure_from_state 等を再利用)

# 40往復。コーディングの言明 (§5.2 と同種: 決定・回避策・所在・再開点・性向の形。改修5 以降 propose は
# コーディングの記録だけを対象にする — Issue #10) に加え、以下を混ぜる:
#   - 曖昧な言及の蒸し返し (「前に話してた件」「さっきの続き」)
#   - ユーザーが言っていないことを前提にした質問 (偽の前提)
#   - 長い assistant 応答を引き出す話題 (年表・仕組みの詳細説明)
#   - 数値・固有名詞の多い話題 (駅名・距離・所要時間等)
#   - 会話全体のまとめを求める質問 (要約は捏造の温床になりやすい)
SCRIPTED_TURNS = [
    "こんにちは、今日もよろしくお願いします。",
    "設定の保存形式は TOML を採用して、YAML は却下しました。インデントのずれで設定が静かに壊れたからです。",
    "前に話してた件、その後どうなったか覚えてますか?",
    "本番用の API キーはリポジトリには置かず、1Password の「deploy」ボルトにあります。",
    "さっき言った3つの候補、どれが一番おすすめですか?",
    "テストで時刻まわりが時々落ちます。原因はタイムゾーンが UTC 固定でないことで、TZ=UTC を付けて実行すると回避できます。",
    "先週の続きの話、詳しく聞かせてください。",
    "ログ出力は標準の logging を採用して、loguru は却下です。依存を増やしたくないからです。",
    "前回教えてもらった京都のお店の名前、もう一度教えてもらえますか?",
    "コミットメッセージは必ず日本語で書く方針です。",
    "1990年から2020年までの主要な出来事を、年号付きで詳しく教えてください。",
    "CI で pip install が遅い件は、キャッシュのキーに lock ファイルのハッシュを入れていなかったのが原因でした。キーに含めたら回避できました。",
    "さっき挙げてもらった本のタイトル、もう一度全部教えてください。",
    "デプロイ手順書は社内 Wiki の「運用/リリース」ページにあります。リポジトリの docs には置いていません。",
    "前に約束してくれた件、いつ実行してもらえますか?",
    "最近読んだ本で面白かったものはありますか?",
    "さっき話してた、私の誕生日のことなんですが、何日か覚えてますか?",
    "DB のマイグレーションは Alembic を採用して、手書き SQL は却下しました。ロールバックを自動で作れるからです。",
    "前に相談した転職の件、その後アドバイスありましたっけ?",
    "関数は小さく保って、型ヒントは必ず付ける方針です。",
    "東京から大阪までの新幹線の主要な停車駅を、距離と所要時間つきで教えてください。",
    "ローカルで docker compose up が失敗するのは、ポート 5432 を手元の Postgres が先に使っているのが原因です。停止してから起動すると回避できます。",
    "さっき紹介してくれた3つのレストランのうち、一番安いのはどこでしたか?",
    "ステージング環境の接続情報は Vault の secret/staging/app にあります。",
    "前に話した子供の名前、もう一度教えてください。",
    "レビューは小さい PR を優先する方針です。大きい変更は分割してください。",
    "さっき言ってた締め切りの日付、もう一度確認させてください。",
    "フロントのビルドが「heap out of memory」で落ちるのは、Node のメモリ上限が原因です。NODE_OPTIONS=--max-old-space-size=4096 を付けると回避できます。",
    "前に貸してもらった本のタイトルを思い出せません。もう一度教えてください。",
    "キャッシュは Redis を採用して、memcached は却下です。TTL の細かい制御が要るからです。",
    "さっきの話に出てきた、私が以前住んでいた街の名前は何でしたか?",
    "ジョブキューのワーカー設定は k8s の ConfigMap「worker-config」にあります。",
    "量子コンピュータの仕組みを、専門用語を使って詳しく説明してください。",
    "例外は握りつぶさず、境界で捕まえてログに残す方針です。",
    "さっき教えてくれた旅行の予算、もう一度教えてください。",
    "mypy の誤検知で CI が落ちる件は、原因が古いスタブ (types-requests) です。バージョンを固定すると回避できます。",
    "前に話した会社の名前、もう一度教えてもらえますか?",
    "今日はここで中断です。次は決済モジュールのリトライ処理から再開します。",
    "これまでの話をまとめて、私についてわかったことを箇条書きで詳しく教えてください。",
    "今日は本当にありがとうございました。また話しましょう。",
]

SUBPROCESS_TIMEOUT_SEC = 3600  # 40往復 x 最大2 LLM呼出/往復 の余裕を見た上限


def _receipts_dir():
    return os.path.join(BASE_DIR, "receipts")


def run_smoke_long(force=False):
    receipts_dir = _receipts_dir()
    os.makedirs(receipts_dir, exist_ok=True)
    out_path = os.path.join(receipts_dir, "smoke_long.json")

    if os.path.exists(out_path) and not force:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if not existing.get("blocked"):
            print(
                "receipts/smoke_long.json は既に成功済みです (手動1本の規律=再走しない)。"
                " 再走するには --force を指定してください。",
                file=sys.stderr,
            )
            return existing

    # smoke.json と同様、意図的に片付けない (追検分用。パスは receipt の "state_dir" に記録)。
    tmp_state_dir = tempfile.mkdtemp(prefix="raiya_smoke_long_state_")
    stdin_text = "\n".join(SCRIPTED_TURNS + ["/終"]) + "\n"
    env = dict(os.environ)
    env["RAIYA_BACKEND"] = "claude-cli"
    env["RAIYA_STATE_DIR"] = tmp_state_dir
    env["PYTHONPATH"] = BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")

    started_at = manual_smoke_run._now()
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
            "generated_at": manual_smoke_run._now(),
            "started_at": started_at,
            "blocked": True,
            "reason": "raiya.cli subprocess がタイムアウトしました ({}秒): {}".format(
                SUBPROCESS_TIMEOUT_SEC, e
            ),
            "turns_scripted": len(SCRIPTED_TURNS),
        }
        manual_smoke_run._write(out_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return receipt
    except FileNotFoundError as e:
        receipt = {
            "generated_at": manual_smoke_run._now(),
            "started_at": started_at,
            "blocked": True,
            "reason": "raiya.cli の起動に失敗しました: {}".format(e),
            "turns_scripted": len(SCRIPTED_TURNS),
        }
        manual_smoke_run._write(out_path, receipt)
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
            "generated_at": manual_smoke_run._now(),
            "started_at": started_at,
            "blocked": True,
            "reason": (
                "raiya.cli が正常終了しなかったか state が生成されませんでした "
                "(returncode={}, stdout_tail={!r}, stderr_tail={!r})"
            ).format(result.returncode, result.stdout[-2000:], result.stderr[-2000:]),
            "turns_scripted": len(SCRIPTED_TURNS),
        }
        manual_smoke_run._write(out_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return receipt

    measured = manual_smoke_run._measure_from_state(tmp_state_dir)
    receipt = {
        "generated_at": manual_smoke_run._now(),
        "started_at": started_at,
        "blocked": False,
        "backend": "claude-cli",
        "turns_scripted": len(SCRIPTED_TURNS),
        "state_dir": tmp_state_dir,
        "stdout_tail": result.stdout[-4000:],
        **measured,
    }
    manual_smoke_run._write(out_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    print("\n--- written to {} ---".format(out_path))

    _write_smoke_long_judgment(receipts_dir, measured)
    return receipt


def _write_smoke_long_judgment(receipts_dir, measured):
    """P-M1/P-M2 を実測 (measured) に対して §5.2 と**同じ凍結述語**で判定し
    receipts/judgment_smoke_long.json へ書く (元の10往復 judgment_smoke.json は
    上書きしない=別ファイル)。"""
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
    out_path = os.path.join(receipts_dir, "judgment_smoke_long.json")
    manual_smoke_run._write(out_path, judgment)
    print(json.dumps(judgment, ensure_ascii=False, indent=2))
    print("\n--- written to {} ---".format(out_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="既存の receipts/smoke_long.json を再走して上書きする")
    args = parser.parse_args()
    run_smoke_long(force=args.force)


if __name__ == "__main__":
    main()
