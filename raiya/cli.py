#!/usr/bin/env python3
"""cli.py -- 頼耶: 八識アーキテクチャ対話エージェント CLI

facade (`engine.MemoryEngine`) の1利用者に格下げ (記憶機能設計書§4.1。旧
cosmicanalysis/Eight_Consciousness/agent.py の直系)。三能変パイプライン:
    ユーザー発言
      -> 阿頼耶識 (engine.recall) 種子照合・発芽
      -> 末那識   (recall が返す narrative を強制注入)
      -> 第六識   (backend.call_llm 経由で LLM) 応答生成
      -> 熏習: engine.propose() で提案を抽出し engine.imprint() で三性ゲートを通す

起動: python3 -m raiya.cli  (project = 起動時 cwd の git トップ。改修5 §4.2)

環境変数: backend.py / README.md を参照。RAIYA_STATE_DIR で永続化先を上書きできる
(既定 ./state、主にテスト・複数人格の切り替え用)。

コマンド:
    /種子   種子倉庫一覧
    /透視   直前の応答の内部状態 (発芽した種子・末那識の注入内容・quarantine・非採用理由)
    /内観   修行 = 自己監査 + ego_weight 減少
    /終     終了 (今生の死。阿頼耶識は残る)
"""
import os
import sys

from . import backend, config, ledger
from . import sansho
from .alaya import seed_conditions
from .engine import MemoryEngine, StateNeedsMigration, StateUnreadable
from .project import resolve_project

MAX_HISTORY_TURNS = 6  # プロンプトに含める直近往復数
INSIGHT_REJECTED_LOG_COUNT = 5  # /透視 で表示する直近の非採用 (quarantine/却下) 理由の件数


def build_history_text(history):
    if not history:
        return "(なし)"
    lines = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        lines.append("ユーザー: {}".format(turn["user"]))
        lines.append("アシスタント: {}".format(turn["assistant"]))
    return "\n".join(lines)


def build_seed_text(seeds):
    if not seeds:
        return "(発芽した種子なし)"
    lines = []
    for s in seeds:
        lines.append(
            "- {}(強度{}・縁: {})".format(
                s["content"], s["strength"], "、".join(seed_conditions(s))
            )
        )
    return "\n".join(lines)


def build_conversation_prompt(manas_text, seed_text, history_text, user_input):
    return (
        "[末那識注入 - 自己物語]\n{manas}\n\n"
        "[阿頼耶識より発芽した記憶]\n{seeds}\n\n"
        "[これまでの会話]\n{history}\n\n"
        "[今回の発言]\nユーザー: {user_input}\n\n"
        "上記を踏まえて、現代日本語で自然に応答してください。"
        "前置きや役割の説明、思考過程は書かず、応答本文のみを述べてください。"
    ).format(
        manas=manas_text or "(まだ我執は形成されていない)",
        seeds=seed_text,
        history=history_text,
        user_input=user_input,
    )


def build_introspection_prompt(manas_log, germinated_history):
    log_text = (
        "\n".join(
            "- 第{life}生: 注入内容「{text}」".format(
                life=e["life"], text=e["injected_text"] or "(空)"
            )
            for e in manas_log
        )
        or "(注入ログなし)"
    )
    hist_text = (
        "\n".join(
            "- {}(強度{}・発芽{}回)".format(s["content"], s["strength"], s.get("fire_count", 0))
            for s in germinated_history
        )
        or "(発芽履歴なし)"
    )
    return (
        "あなたの末那識(自己物語)注入ログ:\n{log}\n\n"
        "あなたの阿頼耶識で発芽してきた種子:\n{hist}\n\n"
        "これらを踏まえて、「最近の私はこういう思い込みで返答に色をつけていたかもしれない」"
        "という自己申告文を、一人称・現代日本語で3〜5文程度で述べてください。"
        "説明や前置きは不要で、自己申告文のみを述べてください。"
    ).format(log=log_text, hist=hist_text)


class Agent:
    def __init__(self, project=None):
        self.engine = MemoryEngine()
        # 改修5 §4.2: CLI の project は起動時の cwd (git トップ)。hooks の stdin cwd・MCP stdio の
        # os.getcwd() と同じ解決。recall の絞り込みと imprint の帰属の両方に使う。
        self.project = project if project is not None else resolve_project(os.getcwd())
        self.history = []
        self.last_turn = None
        for w in self.engine.warnings:
            print("[警告] {}".format(w))
        self.life = self.engine.life

    def run(self):
        print("=== 頼耶 起動 - 第{}生 ===".format(self.life))
        print("コマンド: /種子 /透視 /内観 /終")
        while True:
            try:
                line = input("あなた> ")
            except EOFError:
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line == "/終":
                print("頼耶> 今生の幕を閉じます。阿頼耶識は残ります。")
                break
            elif line == "/種子":
                self.cmd_seeds()
            elif line == "/透視":
                self.cmd_insight_view()
            elif line == "/内観":
                self.cmd_introspect()
            else:
                self.cmd_converse(line)

    def cmd_seeds(self):
        seeds = self.engine.alaya.list_seeds()
        if not seeds:
            print("頼耶> (種子はまだありません)")
            return
        print("頼耶> 種子倉庫 ({} 件):".format(len(seeds)))
        for s in seeds:
            last_fired = s.get("last_fired")
            last_fired_text = "未発芽" if last_fired is None else "第{}生".format(last_fired)
            print(
                "  [{id}] {content} | 強度={strength} 縁={conditions} 極性={polarity} "
                "発芽{fire_count}回 最終発芽={last_fired}".format(
                    id=s["id"],
                    content=s["content"],
                    strength=s["strength"],
                    conditions="、".join(seed_conditions(s)),
                    polarity=s.get("polarity", "無記"),
                    fire_count=s.get("fire_count", 0),
                    last_fired=last_fired_text,
                )
            )

    def cmd_insight_view(self):
        if not self.last_turn:
            print("頼耶> (直前の応答がまだありません)")
            return
        print("頼耶> [透視] 発芽した種子:")
        if self.last_turn["fired_seeds"]:
            for s in self.last_turn["fired_seeds"]:
                print("  - [{}] {}(強度{})".format(s["id"], s["content"], s["strength"]))
        else:
            print("  (なし)")
        print("頼耶> [透視] 末那識注入内容:")
        print(
            "  "
            + (
                self.last_turn["manas_text"].replace("\n", "\n  ")
                if self.last_turn["manas_text"]
                else "(なし)"
            )
        )
        audit = self.engine.audit()
        for key in ("alaya_warning", "manas_warning", "imprint_log_warning"):
            if audit.get(key):
                print("頼耶> [透視] 警告: {}".format(audit[key]))
        print(
            "頼耶> [透視] quarantine (遍計所執=非接地): 保持 {retained} 件 / 累計 {total} 件".format(
                retained=audit["quarantine_retained"], total=audit["quarantine_total"]
            )
        )
        rejected_log = [
            e for e in audit["imprint_log_tail"] if e["decision"] not in ("圓成實", "依他起")
        ]
        print("頼耶> [透視] 直近の非採用理由:")
        if rejected_log:
            for e in rejected_log[-INSIGHT_REJECTED_LOG_COUNT:]:
                print("  - [{}] {}".format(e["decision"], e["reason"]))
        else:
            print("  (なし)")

    def cmd_introspect(self):
        material = self.engine.introspect_material()
        # 警告は利用者向けの出力だけ。LLM のプロンプト (build_introspection_prompt) には渡さない。
        for key in ("alaya_warning", "manas_warning"):
            if material.get(key):
                print("頼耶> [内観] 警告: {}".format(material[key]))
        prompt = build_introspection_prompt(material["manas_log"], material["germinated_history"])
        try:
            report = backend.call_llm(prompt, max_tokens=512)
        except backend.BackendError as e:
            print("頼耶> [内観失敗] {}".format(e))
            return
        report = report.strip()
        if not report:
            print("頼耶> [内観失敗] 申告文が空のため記帳しません")
            return
        print("頼耶> [内観]")
        print(report)
        result = self.engine.record_introspection(report)
        print("頼耶> (我執係数 ego_weight を {:.2f} に低減しました)".format(result["ego_weight"]))

    def cmd_converse(self, user_input):
        # 1 プロセス = 1 生なので、最初の往復が session の開始 (再開点はここで 1 度だけ出る)。
        # 記憶側 (recall・熏習) には hooks と同じく <private> を除いた本文だけを渡す (#24)。会話 LLM の
        # プロンプトと history は生の入力のまま: private は assistant への発話であり、記憶への発話ではない。
        recalled = self.engine.recall(
            ledger.strip_private(user_input), project=self.project, session_start=not self.history
        )
        fired_seeds = recalled["seeds"]
        manas_text = recalled["narrative"]
        history_text = build_history_text(self.history)
        seed_text = build_seed_text(fired_seeds)
        prompt = build_conversation_prompt(manas_text, seed_text, history_text, user_input)
        try:
            response = backend.call_llm(prompt, max_tokens=1024)
        except backend.BackendError as e:
            print("頼耶> [応答生成失敗] {}".format(e))
            return
        response = response.strip()
        print("頼耶> {}".format(response))
        self.history.append({"user": user_input, "assistant": response})
        self.last_turn = {"fired_seeds": fired_seeds, "manas_text": manas_text}
        self.imprint_from_turn(user_input, response)

    def imprint_from_turn(self, user_input, response):
        # 熏習 API は往復オブジェクト (Turn) を必須引数に取る (KUN-7 能熏四義)。
        # ここが提案 (engine.propose) を三性ゲート (engine.imprint) へ渡す唯一の配線。
        # engine.record_turn と同じ順・同じ規則で本文を作る: private は両側から、貼り付けは user の根拠から除く。
        # どちらかが空なら提案しない (cmd_stop の 処方 2 / 処方 7 と同じ: LLM も呼ばない)。
        user = ledger.strip_pasted(ledger.strip_private(user_input))
        assistant = ledger.strip_private(response)
        if not user.strip() or not assistant.strip():
            return
        turn = sansho.Turn(user_text=user, assistant_text=assistant)
        try:
            proposals = self.engine.propose(turn)
        except backend.BackendError:
            return  # 熏習失敗時はスキップして会話を継続する (種子は書き込まない)
        if not proposals:
            return
        self.engine.imprint(turn, proposals, project=self.project)


def main():
    if not config.startup_check("raiya-cli"):
        sys.exit(2)
    try:
        agent = Agent()
    except StateUnreadable as e:
        print("raiya-cli: 状態DB 読めず category={} (何も書かずに終了)".format(e.category), file=sys.stderr)
        sys.exit(2)
    except StateNeedsMigration:
        print("raiya-cli: 旧 JSON 版の state です (移行しない。脇に退けてから起動。何も作らずに終了)", file=sys.stderr)
        sys.exit(2)
    agent.run()


if __name__ == "__main__":
    main()
