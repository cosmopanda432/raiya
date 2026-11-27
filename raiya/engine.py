"""engine.py -- facade MemoryEngine (記憶機能設計書§4.2)

alaya/sansho/manas/ledger を束ね、外部エージェントが使う動詞だけを公開する。決定論の
動詞 (recall/imprint/record_turn/introspect_material/record_introspection/audit) は
どれも backend (LLM 抽象層) に依存しない。LLM を使うのは propose() と consolidate()
だけで、どちらも**呼び出されたときにだけ backend を遅延 import する** (consolidate は
窓が空なら import すらしない): mcp_server.py のようにこれらを一度も呼ばない利用者の
プロセスには、backend (延いては LLM) が一切読み込まれないことを保証する
(サーバ内に LLM を置かない=記憶機能設計書§4.3)。
"""
import copy
import dataclasses
import datetime
import json
import os
import re
import time

from . import config, sansho, storage
from .alaya import (
    VALID_ACTIONS,
    VALID_KINDS,
    VALID_POLARITIES,
    WithdrawalRefused,  # noqa: F401 (再 export: raiya-correct が engine から取る)
    _absent_local_paths,
    is_superseded,
    is_withdrawn,
    plant_precheck,
    project_matches,
    seed_conditions,
    seed_kind,
    withdrawal_basis_problem,
)
from .ledger import (
    GATE_CUT_CHAR,
    LedgerTurn,
    _is_int,
    elide_middle,
    gate_text,
    read_tool_refs,
    strip_pasted,
    strip_private,
)
from . import compact as _compact_module
from . import manas as _manas_module
from .storage import StateNeedsMigration, StateUnreadable  # noqa: F401 (再 export: 入口が engine から取る)

DEFAULT_STATE_DIR = "./state"

CONSOLIDATE_MAX = 5           # 1 回の統合で採用する提案の上限
# 1 回の統合で読む往復の上限。残りは次回の統合が拾う (`upto = window[-1].id` で窓の
# 末尾までしか mark しないため、部分消費は正しく続きから再開される)。上限が無いと、
# backend が落ちている間 mark されずに溜まった未統合分が毎回まるごと再送され、
# プロンプトが際限なく膨らんで二度と成功しなくなる。
CONSOLIDATE_WINDOW_MAX = 10
CONSOLIDATE_TURN_CHARS = 2000  # 統合プロンプトに載せる各往復の片側あたりの文字数上限
# 統合プロンプトに載せる既存記録の上限 (層化 B5): 台帳の語に触れた種子の行数 / 群の見出しの行数。
CONSOLIDATE_RELATED_MAX = 30
CONSOLIDATE_GROUPS_MAX = 20
# 統合の応答に許す上限トークン数。propose (一往復・種子 0〜2 件) より大きく取る: 統合は
# 窓 (最大 CONSOLIDATE_WINDOW_MAX 往復) から最大 CONSOLIDATE_MAX 件の性向を逐語 span 付きで
# 書き出すため出力が長く、かつ reasoning 系モデルでは思考トークンもこの上限
# (max_completion_tokens) に含まれる。足りないと出力が途中で切れ、その統合は丸ごと
# 失敗する (backend が finish_reason=length を BackendError にする)。
# openai-api backend はこの値を使わず backend.OPENAI_MAX_COMPLETION_TOKENS を送る (Issue #9)。
CONSOLIDATE_MAX_TOKENS = 4096
# 性向を注入するか (改修5 §7 / §11.2)。既定は注入しない。"1" で従来動作 (発芽した性向の narrative と
# 索引に性向が載る)。engine.recall が読むので hooks にも MCP にも効く。
INJECT_DISPOSITIONS_ENV = "RAIYA_INJECT_DISPOSITIONS"
# compact (#63) の ③ 直近の件数。`raiya.hooks.core.RECALL_CONTEXT_TURNS` と同じ値 (engine は hooks を import
# できない = 循環・層の向き。一致は tests/test_engine_compact.py が pin する)。内容扱いで凍結しない。
COMPACT_RECENT_TURNS = 5
# compact の `turns[].reason` (落ちた行の理由。文面は内容扱い。`GERMINATION_REASON_*` と同じ流儀)。
COMPACT_REASON_NO_CIRCLE = "どの円にも入らない"
COMPACT_REASON_BUDGET = "容量で落ちた"
COMPACT_REASON_OUTER_CUT = "内側の円が容量で切れた"
COMPACT_REASON_UNKNOWN = "台帳に無い"
_COMPACT_REASONS = {
    _compact_module.R_NO_CIRCLE: COMPACT_REASON_NO_CIRCLE,
    _compact_module.R_BUDGET: COMPACT_REASON_BUDGET,
    _compact_module.R_OUTER_CUT: COMPACT_REASON_OUTER_CUT,
    _compact_module.R_UNKNOWN: COMPACT_REASON_UNKNOWN,
}
# audit の log_check (七文書による検証層 設計書 §3.3 四大教法): hooks.log の自己報告
# `ledger_id=N` (stop の記録行) を台帳の実物と照合する。hooks.log は state_dir 直下 (raiya/hooks/claude_code.py
# の `_hooks_log_path` と同じ名前。hooks を import すると循環するので名前だけ持つ)。末尾の
# 高々 LOG_CHECK_TAIL_BYTES だけを読む (伸び続けるログの長さを audit のコストにしない)。
HOOKS_LOG_NAME = "hooks.log"
LOG_CHECK_TAIL_BYTES = 64 * 1024
# stop の記録行の形 (`hooks/claude_code.py` cmd_stop の `session={} ledger_id={} pending={}` を
# `_log` が `<ISO 時刻> ` 付きの 1 行で書いたもの) に行頭から行末まで一致する行だけを証言として
# 読む。行の途中 (別の行の session id の中など) の `ledger_id=` は拾わない。session id に空白が
# 入っていると記録行そのものが読めなくなるが、それは「記録なし」/古い id = 照合できない側に倒れる。
_LOG_LEDGER_ID_RE = re.compile(rb"^\S+ session=\S* ledger_id=(\d+) pending=\d+\r?$", re.M)


# `inspect_seeds(project=…)` の「project を指定しない = 全件」を表す番兵 (inspect spec §3)。
# recall 系の `project=None` は「全域の種子だけ」(§12.3) なので None では表せない。
ALL_PROJECTS = object()
# `seed_provenance` が根拠の span の前後に出す文字数 (inspect spec §1.3)。
PROVENANCE_EXCERPT_CHARS = 40


def _inject_dispositions():
    """env (空でない値) > 設定ファイルの inject_dispositions > False。env は `== "1"` だけが真
    (`"0"` はファイルの true も打ち消す)。recall と explain_recall は同じ関数を通る。"""
    env = os.environ.get(INJECT_DISPOSITIONS_ENV)
    if env:
        return env == "1"
    return config.get().values.get("inject_dispositions", False)


def resolve_state_dir(state_dir=None):
    """state dir の決定: 引数 → RAIYA_STATE_DIR (空でない値) → 設定ファイルの state_dir → DEFAULT_STATE_DIR。"""
    return (
        state_dir
        or os.environ.get("RAIYA_STATE_DIR")
        or config.get().values.get("state_dir")
        or DEFAULT_STATE_DIR
    )


_resolve_state_dir = resolve_state_dir  # 旧名 (hooks 等の既存の呼び出し元のため残す)


class MemoryEngine:
    """頼耶の facade。1 インスタンス = 1 RAIYA_STATE_DIR (sqlite3 の `raiya.db` 1 ファイル: alaya/manas/imprint_log
    と、追記専用の台帳。保存層は `raiya.storage`)。

    決定論の公開動詞 (LLM を使う propose/consolidate を除く) は `_locked()` を経由し、
    `state_dir/.lock` の flock で排他しつつディスクから再読込してから操作・保存する
    (記憶機能設計書 波4)。consolidate は LLM 呼び出しの前後を `_locked` で挟む。
    読むだけの動詞 (`audit` / `introspect_material` / `compact` / inspect の 5 動詞: inspect_seeds / injection_log / seed_provenance / explain_recall / imprint_log_rows) は `_read_locked()` を
    経由し、排他と再読込だけ行って保存しない (#64 / #71)。
    これにより、同じ RAIYA_STATE_DIR を複数の MemoryEngine インスタンス/プロセス
    (MCP サーバ+hooks 等) が書いても、直前の他インスタンスの書込を取りこぼさない。
    ただし同時書込のクラッシュ整合性・マルチテナント分離までは範囲外(記憶機能設計書§9)。
    """

    def __init__(self, state_dir=None, new_life=True):
        """`new_life=True` (既定): 構築のたびに `begin_new_life()` で新しい生を開始する
        (CLI/MCP サーバの従来どおりの挙動=1プロセス起動=1生。既定値のため既存呼び出し元
        は無変更で不変)。

        `new_life=False`: 構築時に生を進めない (現在の `life` をそのまま使う)。hooks
        専用: hooks は Claude Code のプロンプトのたびに新規 `MemoryEngine` を構築する
        短命プロセスのため、常に `new_life=True` だと「1 ターン=1 生」になってしまい、
        CLI/MCP の「1 生=1 セッション」という意味論が壊れる。改修4 で種子は削除も減衰も
        されなくなった (KUN-3 恒隨轉) ので生の二重計上で種子が消えることはもう無いが、
        `planted_at` / `miss_streak` (連続不発芽の観測用カウンタ) と末那識の注入ログは
        今も生を単位に数えるため、意味論が壊れれば観測値がそのまま狂う。hooks は
        `new_life=False` で構築し、そのセッションの最初のプロンプトでのみ明示的に
        `begin_new_life()` を1回呼ぶことで「1 生=1 Claude Code セッション」を保つ。
        """
        self.state_dir = _resolve_state_dir(state_dir)
        # `legacy` の state dir (旧 JSON) は `.lock` も作らずに `StateNeedsMigration` (#86: 入口の案内「何も作らずに終了」と
        # hooks / inspect / correct に揃える。os.stat のみ)。lock 内の `open_stores` も同じ判定をする (この判定の後に
        # 旧ファイルが置かれた競合では `.lock` だけが残る)。
        if storage.state_status(self.state_dir) == "legacy":
            raise StateNeedsMigration(self.state_dir)
        os.makedirs(self.state_dir, exist_ok=True)
        # 永続化は backend 経由 (sqlite3: `raiya.db`)。ストアは純粋な in-memory の論理で、backend が区間ごとに読み書きする。
        # `none` なら新しい DB をここで作る。他プロセスの構築と同時に起きても二重に作らないよう、lock の中で行う (公開動詞と同じ排他)。
        self._backend = storage.SqliteBackend(self.state_dir)
        with self._backend.lock():
            self.alaya, self.manas, self.ledger = self._backend.open_stores()
        # 構築時点の snapshot のみ。呼び出しごとの警告は recall() / audit() の戻り値
        # (alaya_warning / manas_warning / ledger_warning / imprint_log_warning) を見る。
        self.warnings = []

        def _snapshot_warnings():
            self.warnings = [w for w in (self.alaya.warning, self.manas.warning, self.ledger.warning) if w]

        if new_life:
            # begin_new_life() は動詞途中で保存しない。構築時は 1 つの書く区間で生を進めて COMMIT する。
            def _begin():
                life = self.alaya.begin_new_life()
                _snapshot_warnings()
                return life

            self.life = self._locked(_begin)
        else:
            def _current():
                _snapshot_warnings()
                return self.alaya.current_life

            self.life = self._read_locked(_current)

    # --- 多重書き手対策 (記憶機能設計書 波4) ---
    def _locked(self, op, with_ledger=False):
        """lock 取得 -> ディスクから再読込 -> op() 実行 -> 1 回の save (backend.section 経由) -> 解放、の共通手順。

        MCP サーバ (セッションごとに長命の1 MemoryEngine) と hooks (呼ばれるたびに
        新規の短命 MemoryEngine) が同じ RAIYA_STATE_DIR を書く場合、このインスタンスが
        構築された後に他インスタンス/他プロセスが書き込んだ内容を、動詞を実行する
        前に必ず反映させる (でなければ古い in-memory state で上書きしてしまう)。
        `propose()` は disk 状態を変更しない (LLM 呼び出しのみ) ためこの経路を通らない。

        `with_ledger=True` のときだけ台帳 (LedgerStore) も同じ規律に含める。台帳の meta
        (next_id / consolidated_upto) を読んで進める動詞 -- `record_turn` /
        `consolidate` -- だけが True を渡す (`audit` は台帳を読むが保存しないので
        `_read_locked(with_ledger=True)`)。ここで reload しないと、他の
        書き手が進めた consolidated_upto を古い in-memory meta の save で巻き戻し、
        統合済みの往復を再統合してしまう。`recall` は台帳に一切触らない: 台帳は削除
        されずに伸び続けるので、行を数える動詞 (`pending_count` 等) の走査コストを
        同期 hook の `recall` (<1 秒の予算) が背負わないようにする
        (`LedgerStore.reload()` 自体は meta しか読まない)。
        """
        # 区間 (lock → 再読込 → op → 保存) は backend が持つ。op が例外なら何も保存しない (動詞全体のロールバック)。
        with self._backend.section(write=True, with_ledger=with_ledger):
            return op()

    def _read_locked(self, op, with_ledger=False):
        """lock 取得 -> ディスクから再読込 -> op() 実行 -> 解放。**save しない** (inspect spec §3)。

        観測の動詞 (`inspect_seeds` / `injection_log` / `seed_provenance` / `explain_recall` / `audit` / `introspect_material` / `imprint_log_rows` / `compact`) 専用。
        書く区間 (`_locked`) は op の後に差分を書いて COMMIT する。観測の動詞をそちらに通すと、書かないはずの観測が
        write 接続・`BEGIN IMMEDIATE`・fsync を要する (相分を見分が変えない)。ここは `mode=ro` の接続で、書けない。
        op は状態を変えてはならない (変えても保存されず、区間の終わりの ROLLBACK で消える)。
        """
        with self._backend.section(write=False, with_ledger=with_ledger):
            return op()

    def begin_new_life(self):
        """新しい生を開始する (hooks 専用動詞。記憶機能設計書 波4)。決定論。

        `new_life=False` で構築した MemoryEngine (hooks) が、Claude Code セッションの
        最初のプロンプトでのみ明示的にこれを1回呼ぶことで「1 生=1 セッション」を保つ。
        他の動詞と同じ lock 規律 (排他→reload→操作→backend.section で 1 回 save→解放) に従う。台帳は触らない。
        生が進んでも種子は削除も減衰もされない (KUN-3 恒隨轉)。更新されるのは観測用の
        `miss_streak` (連続不発芽の生の数) と生番号だけ。
        """
        def _op():
            return self.alaya.begin_new_life()

        self.life = self._locked(_op)
        return self.life

    # --- 決定論 (§4.2) ---
    def recall(self, text, context=None, project=None, session_start=False, session_id=None):
        """text (+ 文脈窓) -> 発芽種(ラベル付き)+記録の索引+注入文。決定論 (改修4 §6/§7・改修5 §6/§7)。

        台帳は読まない (同期 hook の予算 <1 秒を守るため。`_locked` の `with_ledger` 参照)。
        `project`: 現在の project (hooks は `project.resolve_project(cwd)`)。None なら全域
        (`project=null`) の種子のみ (§12.3: 絞り込み無しにはしない)。
        `session_start`: True のときだけ同 project の 再開点 を 1 件 narrative に出す (§6.3)。
        性向は既定で発芽も索引も無し (`RAIYA_INJECT_DISPOSITIONS=1` で従来動作)。
        注入索引は kind 別の許可クラス (決定系は 依他起 も載り、「(推定)」は user の span を持たない種子だけ=§12.5)。
        `session_id`: 注入ログの行に残す session (hooks が stdin の値を渡す。MCP/CLI は None)。
        注入ログには `ts` (UTC ISO) と `project` (str 以外は None) も残す (inspect spec §2.1)。
        戻り値: {"seeds", "narrative", "index", "resume", "circles", "alaya_warning", "manas_warning"}。
        `alaya_warning` / `manas_warning` = この呼び出しの reload 直後の `alaya.warning` / `manas.warning`
        (読めない行がある呼び出しにだけ str、他は None。読めない行は毎回読み飛ばして警告するだけで、退避も書き直しもしない)。`seeds` = 発芽した全件 (①)、`index` = 注入文の
        ③ に描画された種子 (深いコピー・描画の順。発芽した種子は入らず、群は代表 1 件)。`circles` =
        {"siblings", "groups", "global", "cut", "shortened", "chars", "max_chars"} (② ③ の群 ④ と容量で落ちた数。layered-memory plan §6)。
        判定は `alaya._circle_verdicts` の 1 回、注入文は `manas.compose_injection` の 1 か所 (`explain_recall` と共通)。
        """
        include_dispositions = _inject_dispositions()
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

        def _op():
            alaya_warning = self.alaya.warning
            manas_warning = self.manas.warning
            # `context or ()` のまま渡す (list() で包まない): str を渡されたら
            # alaya の型ガードに TypeError を出させるため。包むと str が
            # 1 文字ずつの列に化けてガードをすり抜ける。
            verdicts = self.alaya._circle_verdicts(
                text, context or (), project=project, include_dispositions=include_dispositions,
                fired_limit=self.manas.rendered_fired_count(),
            )
            fired_seeds = [seed for seed, did_fire, _, _ in verdicts["germination"] if did_fire]
            self.alaya._mark_fired(fired_seeds)
            fired = [copy.deepcopy(s) for s in fired_seeds]
            resume = self.alaya.resume_point(project) if session_start else None
            composed = self.manas.inject_composed(
                fired, self.life, entries=verdicts["entries"], entry_total=verdicts["entry_total"],
                siblings=verdicts["siblings"], global_counts=verdicts["global_counts"], resume=resume,
                ts=ts,
                session_id=session_id if isinstance(session_id, str) else None,
                project=project if isinstance(project, str) else None,
            )
            return {
                "seeds": fired,
                "narrative": composed["text"],
                "index": [copy.deepcopy(s) for s in composed["index_seeds"]],
                "resume": resume,
                "circles": _circles(composed),
                "alaya_warning": alaya_warning,
                "manas_warning": manas_warning,
            }

        return self._locked(_op)

    def imprint(self, turns, proposals, project=None):
        """往復の窓 (Turn または Turn の列) + 提案 -> 決定リスト。決定論(§3.3・改修4 §4)。

        `project` (改修5 §4.2): 呼び出し側 (MCP `memory_imprint` 等) が往復の project を知っている
        ときに渡す。`project` 属性を持たない plain `Turn` は `LedgerTurn(project=…)` に包む
        (`id=0` = 台帳外のスナップショット扱いはそのまま)。`project` 属性を既に持つ往復は変えない。
        None なら何もしない (plain Turn の種子は全域 = `project=null`)。

        三性ゲートに渡す本文は record_turn / consolidate と同じ規則で作った写し (`_imprint_gate_window`):
        `<private>…</private>` は user/assistant の両側から、貼り付けブロックは user の根拠から除き
        (`grounding_text` を持つ往復はそれが根拠)、`<elided N chars>` は GATE_CUT に置き換える。
        span / 縁に U+FFFC を含む提案は本文を空にした窓 (`_blank_window`) で判定する = 必ず遍計所執
        (imprint_log には元の提案が残る)。plain Turn の imprint_log / evidence に残る本文も、この写し
        (private・貼り付けを除き、印は GATE_CUT) である。MCP の往復本文は呼び出し側の自己申告なので、
        これは経路を揃えて private を保存しないためのもので、呼び出し側の誠実さを保証しない。
        """
        if isinstance(project, str):
            window = sansho.as_window(turns)
            turns = [
                t if hasattr(t, "project") else LedgerTurn(user_text=t.user_text, assistant_text=t.assistant_text, project=project)
                for t in window
            ]

        def _op():
            # 呼び出し側が組んだ Turn にも台帳の往復と同じ規則を当てる (#17 #19)。型検査 (as_window) は
            # 従来どおり alaya.imprint と同じ TypeError。
            gate_window = _imprint_gate_window(sansho.as_window(turns))
            return [
                self.alaya.imprint(_blank_window(gate_window) if _quotes_gate_cut(p) else gate_window, p)
                for p in proposals
            ]

        return self._locked(_op)

    def record_turn(self, user_text, assistant_text, session_id=None, grounding_text=None, project=None,
                    tool_refs=None):
        """往復を台帳に追記する (改修4 §3)。決定論。LLM は呼ばない。

        記録と根拠を分ける (設計書 §10.6 処方 5/6): `<private>…</private>` は user/assistant の
        両側から除いて記録する (本人の「記憶させない」は、assistant がそれを引用し返した
        依他起側の根拠にも残さない)。根拠欄 `grounding_text` = 貼り付け
        (`<pasted_content id="X">…</pasted_content id="X">`) を剥がした user 本文。記録の
        user_text には貼り付けを残す。剥がしはここで行う (hook 側で済ませていても冪等)。
        除いた塊があれば記録は空行の連なりと前後の空白が整形される (`ledger._remove`)。

        `grounding_text`: 省略 (None) なら user_text から導く。str を渡すとそれを
        (`<private>` だけ除いて) そのまま使う -- 呼び出し側が切り詰める前の本文から根拠を
        作ったとき用 (hook の stop: 先に切ると貼り付けのタグが省略側に落ちて対にならず、
        貼り付けの中身が根拠に残る)。

        `imprint` (MCP/CLI) も呼び出し側が組んだ Turn に同じ規則を当てる (`_imprint_gate_window`:
        `<private>` を両側から除き、user の根拠から貼り付けを除き、切り詰めの印を GATE_CUT に置き換える)。

        `project`: 往復の属する project (改修 5 §4.2。hook が `project.resolve_project(cwd)` で
        決めて渡す。None = 欄を書かない = 全域)。

        `tool_refs`: 往復で tool が触れた所在の list (公開 API 凍結 spec §3。None = 欄を書かない)。
        本文ではないので `strip_private` はかけない (秘密の path を送らないのはホストの責任)。形の検査は
        呼び出し側 (raiya-hook record) が済ませる。

        戻り値: {"ledger_id": 払い出した行の id, "pending": 未統合の往復数}。
        """
        user_text = strip_private(user_text)
        assistant_text = strip_private(assistant_text)
        if grounding_text is None:
            grounding = strip_pasted(user_text)
        else:
            grounding = strip_private(grounding_text)

        def _op():
            ledger_id = self.ledger.append(
                user_text, assistant_text, self.alaya.current_life, session_id, grounding_text=grounding,
                project=project if isinstance(project, str) else None, tool_refs=tool_refs,
            )
            return {"ledger_id": ledger_id, "pending": self.ledger.pending_count()}

        return self._locked(_op, with_ledger=True)

    def introspect_material(self):
        """末那識ログ+発芽履歴。決定論。**読むだけ** (`_read_locked`: `.lock` 以外は何も書かない・#71)。

        `alaya_warning` / `manas_warning`: この呼び出しの reload 時点の警告 (audit と同じ意味。
        読めない行は毎回読み飛ばして警告するだけで、退避も書き直しもしない)。LLM のプロンプトには入れないこと。
        """

        def _op():
            recent_log = self.manas.recent_log(10)
            germinated_ids = set()
            for e in recent_log:
                germinated_ids.update(e.get("seed_ids", []))
            germinated_history = self.alaya.get_by_ids(germinated_ids)
            return {
                "manas_log": recent_log,
                "germinated_history": germinated_history,
                "alaya_warning": self.alaya.warning,
                "manas_warning": self.manas.warning,
            }

        return self._read_locked(_op)

    def record_introspection(self, report):
        """自己申告文を記帳・ego_weight 減少。決定論。

        `report` が str でない・空/空白のみのときは ValueError (記帳せず ego_weight も変えない)。
        空の申告で我執だけが減る抜け道を MCP/CLI のどちらからも塞ぐ。
        """
        if not isinstance(report, str) or not report.strip():
            raise ValueError("report は空でない文字列で指定してください")

        def _op():
            self.manas.record_introspection(self.life, report)
            new_weight = self.manas.reduce_ego_weight()
            return {"ego_weight": new_weight}

        return self._locked(_op)

    # ---- 人間の訂正 (取り下げ / 置き換え / 復帰) -- raiya-correct 専用 (MCP には出さない) ----
    # 書く判断は人間の宣言 (--by / --reason / 根拠の台帳 id)。プログラムは宣言の真偽を判定せず、
    # 根拠が種子の evidences に属すかの所属検査だけを行う。拒否は op の中で WithdrawalRefused を
    # raise する: `_locked` は op が例外なら save を呼ばない (with ブロックを抜けるだけ) ので何も書かない。

    @staticmethod
    def _clean_declaration(by, reason):
        by = by.strip() if isinstance(by, str) else ""
        reason = reason.strip() if isinstance(reason, str) else ""
        if not by:
            raise ValueError("by は空でない文字列で指定してください")
        if not reason:
            raise ValueError("reason は空でない文字列で指定してください")
        return by, reason

    @staticmethod
    def _clean_basis(ledger_ids, spans):
        ledger_ids = list(ledger_ids)
        if any(not _is_int(i) for i in ledger_ids):
            raise ValueError("ledger_ids は整数で指定してください")
        ids = sorted(set(ledger_ids))
        spans = list(spans)
        if any(not isinstance(sp, str) for sp in spans):
            raise ValueError("spans は文字列で指定してください")
        return ids, spans

    def _withdraw_checked(self, seed_id, by, reason, ledger_ids, spans):
        """取り下げの検査 1〜4 を順に行い、通れば record を返す (op の中で呼ぶ)。拒否は WithdrawalRefused。"""
        found = self.alaya.get_by_ids([seed_id]) if _is_int(seed_id) else []
        if not found:
            raise WithdrawalRefused("種子 #{} は無い".format(seed_id))
        seed = found[0]
        if is_withdrawn(seed):
            raise WithdrawalRefused("種子 #{} は既に取り下げ済み".format(seed_id))
        rows = self.ledger.rows_by_ids(ledger_ids)
        for lid in ledger_ids:
            if lid not in rows:
                raise WithdrawalRefused("台帳に行 #{} が無い".format(lid))
        problem, matched = withdrawal_basis_problem(seed, ledger_ids, spans)
        if problem is not None:
            raise WithdrawalRefused(problem)
        return {
            "by": by,
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "ledger_ids": list(ledger_ids),
            "spans": list(matched),
            "life": self.alaya.current_life,
            "replaced_by": None,
        }

    def withdraw_seed(self, seed_id, by, reason, ledger_ids=(), spans=()):
        """人間が種子を取り下げる (欄 `withdrawn` を立てる。種子は消えない)。1 回の `_locked`。

        検査の順 (いずれも拒否は WithdrawalRefused): 種子がある -> 既に取り下げ済みでない -> 台帳に
        全 id の行がある -> 根拠が種子の evidences に属す (`withdrawal_basis_problem`)。
        `by` / `reason` が空なら ValueError (state は読まない)。戻り `{"seed_id", "withdrawn": record}`。
        """
        by, reason = self._clean_declaration(by, reason)
        ids, spans = self._clean_basis(ledger_ids, spans)

        def _op():
            record = self._withdraw_checked(seed_id, by, reason, ids, spans)
            return {"seed_id": seed_id, "withdrawn": self.alaya.withdraw(seed_id, record)}

        return self._locked(_op, with_ledger=True)

    def replace_seed(self, seed_id, by, reason, ledger_ids, spans, proposal, window_ledger_ids=None):
        """取り下げと、その代わりの種子の植え付けを 1 回の `_locked` で行う。

        `proposal`: {"content", "conditions", "kind", "spans", "polarity"(省略可・既定 無記)}。engine が
        `{"content","conditions","kind","polarity","evidence":[{"source":"","span":s}...],"action":"new",
        "declared_by":by,"replaces":seed_id}` に組む (最後の 2 キーはどの規則も読まない。imprint_log 行を
        自己記述的にするだけ)。窓は `window_ledger_ids` (無ければ `ledger_ids`) の台帳の往復で、取り下げる
        種子の evidences には照合しない (置き換えの接地だから)。三性ゲートは通常どおり本文だけを見る。
        書く前に全検査 + 窓の用意 + `plant_precheck` を済ませ、通らなければ何も書かない。
        戻り `{"seed_id","withdrawn","replacement":{"decision","sansho","seed_id","is_new","kind","project","reason"}}`。
        """
        by, reason = self._clean_declaration(by, reason)
        ids, spans = self._clean_basis(ledger_ids, spans)
        if window_ledger_ids is None:
            window_ids = list(ids)
        else:
            window_ids, _ = self._clean_basis(window_ledger_ids, ())
        if not window_ids:
            raise ValueError("置き換えの窓 (window_ledger_ids) が要る")
        if not isinstance(proposal, dict):
            raise ValueError("proposal は dict で指定してください")
        polarity = proposal.get("polarity")
        full = {
            "content": proposal.get("content"),
            "conditions": list(proposal.get("conditions") or []),
            "kind": proposal.get("kind"),
            "polarity": polarity if polarity in VALID_POLARITIES else "無記",
            "evidence": [{"source": "", "span": sp} for sp in (proposal.get("spans") or [])],
            "action": "new",
            "declared_by": by,
            "replaces": seed_id,
        }

        def _op():
            record = self._withdraw_checked(seed_id, by, reason, ids, spans)
            turns = self.ledger.turns_by_ids(window_ids)
            have = {t.id for t in turns}
            for wid in window_ids:
                if wid not in have:
                    raise WithdrawalRefused("台帳に行 #{} が無い (置き換えの窓)".format(wid))
            gate = _gate_window(turns)
            if _quotes_gate_cut(full):
                gate = _blank_window(turns)
            problem = plant_precheck(gate, full, seeds=self.alaya.list_seeds(), exclude_id=seed_id)
            if problem is not None:
                raise WithdrawalRefused("置き換えの提案がゲートを通らない: {}".format(problem))
            # ここから state を変える (メモリ上だけ。ディスクへは `_locked` の save が 1 回で書く)。
            withdrawn = self.alaya.withdraw(seed_id, record)
            # この op の中では誰も保存しない (動詞途中の save は全廃)。`_locked` の保存が唯一の書き込みなので、
            # 途中 (set_replaced_by など) で失敗しても何もディスクに載らない (DB も .bak も変わらない)。
            result = self.alaya.imprint(gate, full, replacement_for=seed_id)
            seed = result.get("seed")
            if seed is None:
                raise RuntimeError("plant_precheck を通った置き換えが植わらなかった (プログラムの誤り)")
            self.alaya.set_replaced_by(seed_id, seed["id"])
            withdrawn["replaced_by"] = seed["id"]
            return {
                "seed_id": seed_id,
                "withdrawn": withdrawn,
                "replacement": {
                    "decision": result["decision"],
                    "sansho": result["sansho"] if "sansho" in result else result["decision"],
                    "seed_id": seed["id"],
                    "is_new": result["is_new"],
                    "reinforced": bool(result.get("reinforced")),
                    "superseded": bool(result.get("superseded")),
                    "kind": result.get("kind"),
                    "project": result.get("project"),
                    "reason": result["reason"],
                },
            }

        return self._locked(_op, with_ledger=True)

    def restore_seed(self, seed_id, by, reason):
        """取り下げを戻す (欄を None に・history に復帰行)。台帳は要らない。戻り `{"seed_id","restored": record}`。"""
        by, reason = self._clean_declaration(by, reason)

        def _op():
            record = {
                "by": by,
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "reason": reason,
                "life": self.alaya.current_life,
            }
            return {"seed_id": seed_id, "restored": self.alaya.restore(seed_id, record)}

        return self._locked(_op)

    def audit(self):
        """現在の生 (`life`)・クラス別件数・quarantine 保持/累計・台帳の進捗と warning・imprint_log 末尾。
        決定論(CON-6 証自証分)。

        読むだけ (`_read_locked(with_ledger=True)`): 台帳 meta も reload するが **save しない**。
        報告する consolidated_upto / pending は他の書き手の最新の書込を織り込んだ値。
        sqlite3 版: 読めない行は読み飛ばして警告に数え、DB の行は書き直さない (退避も修復も無い)ので、警告は毎回出る。
        DB 単位の失敗 (必須 meta の欠落など) は `StateUnreadable` (この動詞も何も書かない)。
        `PRAGMA quick_check` を走らせ、ok でなければ `alaya_warning` に足す (audit だけ。recall では走らない)。
        `alaya_warning` / `manas_warning`: 構築ではなくこの呼び出しの reload 時点の警告。

        `ledger_warning`: 台帳の fail-soft 警告 (読めない本文を読み飛ばした/移行時に読めなかった行がある)。壊れた行の warning は最初の行走査で確定するため
        (`LedgerStore` の docstring)、構築時点では未確定でも、この動詞が
        `count()`/`unconsolidated_bases()` で行を読む時点までには確定している = audit が
        「台帳が壊れていないか」を観測できる唯一の窓口になる。
        """

        def _op():
            seeds = self.alaya.list_seeds()
            class_counts = {"圓成實": 0, "依他起": 0}
            kind_counts = {k: 0 for k in VALID_KINDS}
            project_counts = {}
            superseded_count = 0
            withdrawn_count = 0
            for s in seeds:
                cls = s.get("sansho")
                class_counts[cls] = class_counts.get(cls, 0) + 1
                kind = seed_kind(s)
                kind_counts[kind] += 1
                project = s.get("project") if isinstance(s.get("project"), str) else None
                project_counts[project or "(全域)"] = project_counts.get(project or "(全域)", 0) + 1
                if s.get("superseded_by") is not None:
                    superseded_count += 1
                if is_withdrawn(s):
                    withdrawn_count += 1
            imprint_log_tail = self.alaya.list_imprint_log(n=10)
            turns = self.ledger.count()
            # 未統合の行の所依 (project × session_id)。pending もこの走査から数える
            # (pending_count() でもう一度全行を読まない)。
            bases = self.ledger.unconsolidated_bases()
            return {
                # 現在の生 (manual/usage.md・manual/hooks.md: MCP 経由でも life を観測できる)。_locked が reload 済みなので
                # 他プロセス (hooks) が進めた最新の生番号。
                "life": self.alaya.current_life,
                "seed_count": len(seeds),
                "class_counts": class_counts,
                # 改修5 §9: kind 別・project 別の件数と、横断 supersede で索引から外れた件数。
                "kind_counts": kind_counts,
                "project_counts": project_counts,
                "superseded_count": superseded_count,
                "withdrawn_count": withdrawn_count,
                "quarantine_retained": len(self.alaya.list_quarantine()),
                "quarantine_total": self.alaya.quarantine_total(),
                "ledger": {
                    "turns": turns,
                    "consolidated_upto": self.ledger.consolidated_upto(),
                    "pending": len(bases),
                },
                # 七文書 §2.3 (同依): 未統合の行を (project, session_id) ごとに数える。
                "basis_counts": _basis_counts(bases),
                # 七文書 §3.3 (四大教法): hooks.log の `ledger_id=` の証言を台帳の最終 id と照合する。
                "log_check": _log_check(
                    os.path.join(self.state_dir, HOOKS_LOG_NAME), self.ledger.last_id()
                ),
                # count()/unconsolidated_bases() の後に読む (行走査で warning が確定した後)。
                "ledger_warning": self.ledger.warning,
                "history_total": sum(len(s.get("history", [])) for s in seeds),
                "imprint_log_tail": imprint_log_tail,
                # list_imprint_log の後に読む (imprint_log は遅延読込: 読めない行の警告は読んだ時点で確定)。
                "imprint_log_warning": self.alaya.imprint_log_warning,
                # audit だけが quick_check を走らせる (読む区間の接続で。非 ok は alaya_warning へ)。
                "alaya_warning": _join_warnings(self.alaya.warning, self._backend.quick_check()),
                "manas_warning": self.manas.warning,
            }

        return self._read_locked(_op, with_ledger=True)

    # --- 観測 (inspect spec §3。決定論・読むだけ・`_read_locked` で save しない) ---
    def inspect_seeds(self, project=ALL_PROJECTS, kind=None, include_superseded=False, include_withdrawn=False):
        """種子の一覧 (deep copy・id 昇順)。状態は変えない。

        `project`: 既定 `ALL_PROJECTS` = 全件。str = その project の種子と全域の種子。None = 全域の
        種子だけ (recall と同じ意味)。`kind`: None なら全 kind、`VALID_KINDS` 以外は ValueError。
        欄の無い古い種子は読む側の既定 (kind=性向・project=全域) で絞り込む (行は書き換えない=E8)。
        `include_superseded=False` なら superseded_by の立った種子を除く。`include_withdrawn=False` なら
        取り下げ中 (`withdrawn` が立った) 種子を除く。
        """
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError("kind {!r} は VALID_KINDS に無い".format(kind))

        def _op():
            out = []
            for seed in sorted(self.alaya.list_seeds(), key=lambda s: s["id"]):
                if not include_superseded and is_superseded(seed):
                    continue
                if not include_withdrawn and is_withdrawn(seed):
                    continue
                if project is not ALL_PROJECTS and not project_matches(seed, project):
                    continue
                if kind is not None and seed_kind(seed) != kind:
                    continue
                out.append(seed)
            return out

        return self._read_locked(_op)

    def injection_log(self, n=10):
        """末那識の注入ログの末尾 n 件 (deep copy・古い順)。n が正の整数でなければ []。"""
        if not _is_int(n) or n <= 0:
            return []
        return self._read_locked(lambda: self.manas.recent_log(n))

    def imprint_log_rows(self, n=None):
        """熏習の決定ログ (imprint_log) の行 (deep copy・古い順)。**読むだけ** (`_read_locked`)。

        `n` が正の整数ならその末尾 n 件、None なら全件。それ以外 (0・負・非整数) は []。
        ストアの内部やファイルを外から開かずにログを数える/確かめる呼び手 (手動スモークの計測・テスト) 用。
        """
        if n is not None and (not _is_int(n) or n <= 0):
            return []
        return self._read_locked(lambda: self.alaya.list_imprint_log(n))

    def seed_provenance(self, seed_id):
        """種子の出所を実ファイルの間で辿る (inspect spec §1.3)。無い id (非整数を含む) は None。

        戻り値: {"seed", "evidences": [...], "decisions": [...], "consolidated", "consolidated_upto"}。
        - evidences: 種子の `evidences[]` 1 件ごとに、台帳の行の `session_id`/`project`/`ts` (`row`。台帳に
          無い往復・行が見つからないときは None)、根拠の本文の欄 (`body_field`: user なら
          `grounding_text` (行に欄が無ければ `user_text`。`""` は根拠なしのまま)、assistant なら
          `assistant_text`)、span が `sansho._n` 正規化後の本文のどこにあるか (`found`/`position`/
          `excerpt` = 前後 PROVENANCE_EXCERPT_CHARS 文字)、見つからない理由 (`problem`)。
        - decisions: `seed_id` がこの種子の imprint_log 行の `{life, decision, reason}` (古い順)。
          `seed_id` 欄の無い古い行は結ばない (内容の一致で推測しない=同依)。
        - consolidated: 根拠の ledger_id が全て consolidated_upto 以下なら True、そうでなければ False、
          台帳の根拠が 1 件も無ければ None。
        - dormant: 読み取り時の派生 (休眠設計書 §7)。`alaya._absent_local_paths(seed)` が空でなければ
          `{"absent_paths": [書かれたとおりの path, ...]}`、そうでなければ None (所在以外は常に None)。
          書かない・キャッシュしない。
        - group: 読み取り時の派生 (層化 plan §6)。生きた種子なら、同じ bucket (kind, project) の生きた種子で
          `seed_groups` を回した結果のうち、この種子を含む群の dict (label/kind/project/member_ids/representative/size)、
          1 件だけなら None。生きていない種子 (superseded・取り下げ中) は None。書かない・キャッシュしない。
        """
        if not _is_int(seed_id):
            return None

        def _op():
            found = self.alaya.get_by_ids([seed_id])
            if not found:
                return None
            seed = found[0]
            absent = _absent_local_paths(seed)
            group = self.alaya.group_of(seed_id)
            evidences = [e for e in seed.get("evidences") or [] if isinstance(e, dict)]
            ledger_ids = [e.get("ledger_id") for e in evidences if _is_int(e.get("ledger_id"))]
            rows = self.ledger.rows_by_ids(ledger_ids)
            upto = self.ledger.consolidated_upto()
            decisions = [
                {"life": row.get("life"), "decision": row.get("decision"), "reason": row.get("reason")}
                for row in self.alaya.imprint_log_rows_for_seed(seed_id)
            ]
            return {
                "seed": seed,
                "evidences": [_provenance_evidence(e, rows) for e in evidences],
                "decisions": decisions,
                "consolidated": all(i <= upto for i in ledger_ids) if ledger_ids else None,
                "consolidated_upto": upto,
                "dormant": {"absent_paths": list(absent)} if absent else None,
                "group": group,
            }

        return self._read_locked(_op, with_ledger=True)

    def explain_recall(self, text, context=None, project=None, session_start=False):
        """recall の予行演習 (inspect spec §1.4)。**状態を一切変えない** (発芽の記録も注入ログも書かない)。

        `recall` と同じく `alaya._circle_verdicts` (germinate / index と同じ判定) → `manas.compose_injection`
        (inject と同じ組み立て) を通すので、同じ state・同じ入力の `recall` と `seeds` / `index` の id と順序・
        `narrative`・`circles` が一致する (違いは recall だけが発芽の観測欄と注入ログを書くこと)。
        戻り値: {"seeds", "index", "resume", "narrative", "circles", "germinated": [{"id", "kind", "sansho",
        "content", "matched_conditions"}], "germination_rejected": [{"id", "kind", "sansho", "content", "reason",
        "matched_conditions"}], "index_rejected": [{"id", "kind", "sansho", "content", "reason"}],
        "include_dispositions"}。落選は種子ごとに最初に引っかかった理由 1 つ (③ の項目が注入文の容量で落ちたときは
        `INDEX_REASON_BUDGET`)。kind は読む側の既定 (欄が無ければ 性向) で返す。表示側 (inspect_cli) はこの戻り値
        だけで描ける (2 度目の lock を取らない)。
        """
        include_dispositions = _inject_dispositions()

        def _op():
            verdicts = self.alaya._circle_verdicts(
                text, context or (), project=project, include_dispositions=include_dispositions,
                fired_limit=self.manas.rendered_fired_count(),
            )
            g_verdicts = verdicts["germination"]
            fired = [copy.deepcopy(s) for s, did_fire, _, _ in g_verdicts if did_fire]
            resume = self.alaya.resume_point(project) if session_start else None
            composed = self.manas.compose_injection(
                fired, verdicts["entries"], verdicts["entry_total"], verdicts["siblings"],
                verdicts["global_counts"], resume,
            )
            index = [copy.deepcopy(s) for s in composed["index_seeds"]]
            shown = {id(s) for s in composed["index_seeds"]}
            budget_dropped = [
                dict(_seed_summary(e["seed"]), reason=INDEX_REASON_BUDGET)
                for e in verdicts["entries"] if id(e["seed"]) not in shown
            ]
            return {
                "seeds": fired,
                "index": index,
                "resume": resume,
                "narrative": composed["text"],
                "circles": _circles(composed),
                "germinated": [
                    dict(_seed_summary(s), matched_conditions=list(m)) for s, did_fire, _, m in g_verdicts if did_fire
                ],
                "germination_rejected": [
                    dict(_seed_summary(s), reason=r, matched_conditions=list(m))
                    for s, did_fire, r, m in g_verdicts if not did_fire
                ],
                "index_rejected": budget_dropped + [
                    dict(_seed_summary(s), reason=r) for s, included, r in verdicts["index"]
                    if not included
                ],
                "include_dispositions": include_dispositions,
            }

        return self._read_locked(_op)

    def compact(self, window_ledger_ids, text, project=None, budget_chars=None):
        """会話窓の圧縮 (#63・spec 2026-10-27-compact-design.md)。決定論・**読むだけ** (`_read_locked`・何も書かない)。

        窓 (台帳 id の list/tuple) のうち、逐語で残す往復を内 → 外の円 ① `text` で発芽した種子の根拠行 → ② 再開点の
        根拠行 → ③ 窓の直近 `COMPACT_RECENT_TURNS` 往復、の順に容量 (`budget_chars` = narrative の len。None =
        上限なし) まで詰めて決め、残りは「(他に N 往復・台帳 id a–b)」の 1 行にする。要約は作らず LLM も呼ばない。
        発芽の判定は `alaya._germination_verdicts` だけ (explain_recall と同じ門。context は渡さない)。発芽の観測欄・
        注入ログ・生・台帳のカーソルには触れない (`_mark_fired` / `inject_composed` / `germinate` を呼ばない)。
        窓の行は project で絞らない (project は発芽と再開点にだけ効く)。台帳に読める行が無い id は `unknown_ids`。
        型違反は `TypeError`、負の予算は `ValueError`。戻り値の形は spec §3.2。
        """
        if not isinstance(window_ledger_ids, (list, tuple)) or not all(_is_int(i) for i in window_ledger_ids):
            raise TypeError("compact() の window_ledger_ids は整数の list/tuple です")
        if not isinstance(text, str):
            raise TypeError("compact() の text は str です")
        if project is not None and not isinstance(project, str):
            raise TypeError("compact() の project は str か None です")
        if budget_chars is not None:
            if not _is_int(budget_chars):
                raise TypeError("compact() の budget_chars は int か None です")
            if budget_chars < 0:
                raise ValueError("compact() の budget_chars は 0 以上です")
        ids = sorted(set(window_ledger_ids))
        include_dispositions = _inject_dispositions()

        def _op():
            result = {
                "keep": [], "elided": [], "unknown_ids": [], "narrative": "", "turns": [], "seeds": [],
                "cut": {name: 0 for name in _compact_module.CIRCLES}, "chars": 0, "budget_chars": budget_chars,
                "window_sessions": 0, "ledger_warning": None, "alaya_warning": None,
            }
            if not ids:
                result["alaya_warning"] = self.alaya.warning
                result["ledger_warning"] = self.ledger.warning
                return result
            rows = self.ledger.rows_by_ids(ids)
            known = [i for i in ids if i in rows]
            known_set = set(known)
            verdicts = self.alaya._germination_verdicts(text, (), project, include_dispositions=include_dispositions)
            fired = [s for s, did_fire, _, _ in verdicts if did_fire]
            resume = self.alaya.resume_point(project)
            seed_rows = []  # (circle, seed, 窓の中の根拠の台帳 id)
            seeds_out = []
            for circle, group in (("germinated", fired), ("resume", [resume] if resume is not None else [])):
                for seed in group:
                    in_window, outside, off = [], 0, 0
                    for e in seed.get("evidences") or []:
                        if not isinstance(e, dict):
                            continue
                        lid = e.get("ledger_id")
                        if not _is_int(lid):
                            off += 1
                        elif lid in known_set:
                            if lid not in in_window:
                                in_window.append(lid)
                        else:
                            outside += 1
                    seed_rows.append((circle, seed, in_window))
                    seeds_out.append({
                        "id": seed["id"], "kind": seed_kind(seed), "sansho": seed.get("sansho"), "circle": circle,
                        "in_window": list(in_window), "outside_window": outside, "off_ledger": off,
                    })
            circles = [
                (name, [i for c, _, ws in seed_rows if c == name for i in ws])
                for name in ("germinated", "resume")
            ]
            circles.append(("recent", sorted(known, reverse=True)[:COMPACT_RECENT_TURNS]))  # 詰める順は新しい方から
            plan = _compact_module.plan_compaction(rows, ids, circles, budget_chars)
            cited = {}
            for _, seed, ws in seed_rows:
                for i in ws:
                    cited.setdefault(i, []).append(seed["id"])
            turns = []
            for d in plan["turn_decisions"]:
                row = rows.get(d["id"])
                turns.append({
                    "id": d["id"], "decision": d["decision"], "circle": d["circle"],
                    "seed_ids": cited.get(d["id"], []),
                    "reason": _COMPACT_REASONS[d["reason"]] if d["reason"] else None,
                    "session_id": _session_of(row) if row else None,
                    "chars": d["chars"],
                    "tool_refs": read_tool_refs(row) if row and d["decision"] == "keep" else None,
                })
            result.update({
                "keep": plan["keep"], "elided": plan["elided"],
                "unknown_ids": [i for i in ids if i not in known_set],
                "narrative": plan["narrative"], "turns": turns, "seeds": seeds_out, "cut": plan["cut"],
                "chars": len(plan["narrative"]),
                "window_sessions": len({_session_of(rows[i]) for i in known}),
                "alaya_warning": self.alaya.warning, "ledger_warning": self.ledger.warning,
            })
            return result

        return self._read_locked(_op, with_ledger=True)

    # --- LLM を使う動詞 (backend はメソッドの中で遅延 import する) ---
    def propose(self, turn, notes=None):
        """往復 -> 提案(backend 使用)。§3.2 の JSON 契約 (evidence 必須) で抽出する。

        backend への依存はこのメソッドの中でのみ遅延 import する。mcp_server.py の
        ように propose() を一度も呼ばない利用者は、このプロセスに backend/LLM を
        絶対に読み込まない。

        `notes`: 省略可のリスト。渡すと `_parse_proposals` の診断メモ(寛容パースが
        働いた理由)をそのまま受け取る。呼び出し元の挙動には影響しない (hooks 用)。
        """
        from . import backend  # 遅延 import: 呼ばれない限り backend は読み込まれない

        prompt = _build_propose_prompt(turn.user_text, turn.assistant_text)
        raw = backend.call_llm(prompt, max_tokens=512, purpose="propose")
        return _parse_proposals(raw, notes=notes)

    def consolidate(self, notes=None, debug=None):
        """台帳の未統合窓 + 既存性向の索引 -> 性向の提案 (backend) -> 三性ゲート -> 保存 (改修4 §7)。

        LLM 呼び出しは lock の外で行う (数秒かかるため、hooks/MCP を待たせない)。
        窓は追記専用なので再読込は不要だが、応答待ちの間に別プロセスが同じ窓を統合して
        いれば (consolidated_upto が進んでいれば) 書込を skip する。提案 0 件でも窓は
        消費済みとして mark する。backend は propose() と同じく関数内で遅延 import する。
        1 回で読む往復は CONSOLIDATE_WINDOW_MAX 件まで (残りは次回の統合が拾う)。

        skip 判定は 2 段: 窓の末尾が既に統合済みなら `skipped="already_consolidated"`。
        末尾は未統合でも窓の先頭が既に統合済み (別の書き手が *部分的に重なる* 窓の前半だけ
        を統合した) なら `skipped="partial_overlap"` として何も書かない (imprint せず
        cursor も進めず backup も作らない)。重なった往復を二度熏習しないための裁定で、
        未統合の尾は次の統合 (Stop / session_end) が読み直す (#103)。代償は LLM 呼び出し 1 回。
        x9_3 (2026-10-30) で skip した側の往復が次の統合の窓で種子になることを確認済み。

        LLM の出力をそもそも配列として読み取れなかったときは窓を消費しない
        (`skipped="unparseable"`・`consolidated_upto` は進めない): 途中で切れた JSON を
        「提案 0 件」として窓ごと捨てると、内容の濃い統合ほど失われる。再試行の回数は
        数えない -- 統合は未統合が CONSOLIDATE_EVERY 件以上ある間、Stop のたびに高々 1 回
        しか走らないので、改修4 以前の「毎往復 propose」と同じ上限で自然に有界になる。
        LLM が正しく空配列 [] を返した場合 (健全) は従来どおり窓を消費する。

        ゲートとプロンプトが user 発話として読むのは台帳の**根拠欄** (`grounding_text`。旧形式の
        行で欄が無ければ `user_text`) = 貼り付けは根拠にならない (設計書 §10.6 処方 5)。ゲートに
        渡す本文ではさらに、台帳に残った切り詰めの印 (`<elided N chars>`) を `GATE_CUT` に
        置き換える (処方 8): 印ごとの引用・切れ目を跨いで先頭と末尾を繋いだ引用は逐語一致しない。
        span か縁 (conditions) のどれかが区切りの字 (U+FFFC) を含む提案は区切りに一致しうる
        ので、提案**全体**を本文を空にした窓 (`_blank_window`) で判定する = 必ず遍計所執
        (通常の quarantine 経路・imprint_log に元の提案ごと記帳)。閉じる側に倒す: 本文の
        U+FFFC だけを消した窓で判定する形は、LLM が U+FFFC の縁を 1 つ足すだけでその窓へ
        切り替えさせ、本人の本文中の U+FFFC を跨ぐ span を接地させられた (QA 実測)。
        `classify_sansho` は変えない (窓の Turn を作り替えて渡すだけ)。

        `notes`: 省略可のリスト。`_parse_consolidation` の診断メモを受け取る (hooks 用)。
        戻り値: {"window": 窓の台帳 id, "proposals", "decisions",
                 "skipped": None / "empty" / "already_consolidated" / "partial_overlap" / "unparseable",
                 "window_sessions": 窓の中の異なる session_id の数 (None も 1 種。空窓は 0)}。
        `window_sessions` は七文書 §2.3 (同依): 起動したセッションの id だけを見て「その
        セッションで起きた統合」と読まないための数。

        `debug`: 省略可の dict (`notes` と同じ型の診断引数・hooks の debug.log 用)。dict のときだけ、
        2 つの `_locked` の間 (lock の外) で埋める: `window_ids`/`window_sessions`/`window_n`、
        LLM 呼び出しの前に `backend`/`model`/`prompt`/`prompt_len`、呼び出しの後 (例外でも)
        `llm_ms`/`raw`/`raw_len` (例外時は raw=None)。dict 以外は無視。engine は何も書かない。
        """

        def _read():
            # 窓と各行の所依を 1 回の行走査で読む (台帳は伸び続けるので走査を重ねない)。
            window, bases, stamps = self.ledger.unconsolidated_with_stamps(limit=CONSOLIDATE_WINDOW_MAX)
            if not window:
                return window, None, 0, []
            # 統合の索引は全クラス・全 kind (依他起や性向も reinforce/supersede させるため・§11.2)。
            # 窓は複数 project が混ざりうる (§4.2) ので、窓の project 集合 + 全域の種子を見せる。
            projects = {getattr(t, "project", None) for t in window}
            # 窓の所依 (七文書 §2.3 同依): 窓に混ざった session の種類数。None も 1 種。
            sessions = {sid for _, sid in bases}
            # 照合に使う本文 = プロンプトに載る文字列 (切り詰め後・層化 B5) の、切り詰めの印を GATE_CUT に
            # 置き換えたもの (印の語 `elided`/`chars`/数字が縁に当たらず、切れ目を跨ぐ一致も無い)。
            texts = [gate_text(_truncate_for_prompt(_grounding(t))) for t in window] + [
                gate_text(_truncate_for_prompt(t.assistant_text)) for t in window
            ]
            view = self.alaya.consolidation_view(
                projects, texts, related_max=CONSOLIDATE_RELATED_MAX, groups_max=CONSOLIDATE_GROUPS_MAX
            )
            return window, view, len(sessions), [(sid, ts) for (_, sid), ts in zip(bases, stamps)]

        window, view, window_sessions, window_stamps = self._locked(_read, with_ledger=True)
        if isinstance(debug, dict):
            debug.update(
                window_ids=[t.id for t in window], window_sessions=window_sessions, window_n=len(window),
            )
            if window:  # 空窓は窓の欄だけ (既存の pin)。既存記録の id は窓があるときだけ。
                debug.update(
                    related_ids=[s["id"] for s in view["related"]],
                    group_rep_ids=[g["representative"] for g in view["groups"]],
                )
        if not window:
            return {"window": [], "proposals": [], "decisions": [], "skipped": "empty", "window_sessions": 0}

        from . import backend  # 遅延 import: 空窓なら読み込まれない

        prompt = _build_consolidate_prompt(_prompt_window(window), view)
        if isinstance(debug, dict):
            name, model = backend.describe("propose")
            debug.update(backend=name, model=model, prompt=prompt, prompt_len=len(prompt))
        raw = None
        started = time.monotonic()
        try:
            raw = backend.call_llm(prompt, max_tokens=CONSOLIDATE_MAX_TOKENS, purpose="propose")
        finally:
            if isinstance(debug, dict):
                debug.update(
                    llm_ms=int((time.monotonic() - started) * 1000), raw=raw,
                    raw_len=len(raw) if isinstance(raw, str) else None,
                )
        proposals = _parse_consolidation(raw, notes=notes)
        upto = window[-1].id
        window_ids = [t.id for t in window]
        if proposals is None:
            # 配列として読み取れなかった (途中で切れた JSON 等)。窓は未統合のまま残し、
            # 次の統合で読み直す。理由は notes 経由で hooks.log に有界に残る。
            return {"window": window_ids, "proposals": [], "decisions": [], "skipped": "unparseable", "window_sessions": window_sessions}

        def _write():
            done = self.ledger.consolidated_upto()
            if done >= upto:
                return {"window": window_ids, "proposals": proposals, "decisions": [], "skipped": "already_consolidated", "window_sessions": window_sessions}
            if done >= window[0].id:
                # 応答待ちの間に別の書き手が窓の前半だけを統合した (#103)。重なった往復を
                # 二度熏習しないよう何も書かない (cursor もそのまま・backup も作らない)。
                # 未統合の尾は次の統合が読み直す。
                return {"window": window_ids, "proposals": proposals, "decisions": [], "skipped": "partial_overlap", "window_sessions": window_sessions}
            gate_window = _gate_window(window)
            # #105 R: 窓の各往復の前に同じ session へ注入された種子 (manas_log は `_locked` で再読込済み)。
            injected = _injected_by_turn(self.manas.state["log"], window_stamps)
            # #145 X1/X2b: 窓と同じ順の座 (session_id)。
            seats = [sid if isinstance(sid, str) else None for sid, _ in window_stamps]
            decisions = [
                self.alaya.imprint(
                    _blank_window(window) if _quotes_gate_cut(p) else gate_window,
                    p,
                    injected_by_turn=injected,
                    session_by_turn=seats,
                )
                for p in proposals
            ]
            self.ledger.mark_consolidated(upto)
            # この区間が COMMIT したら、lock を持ったまま raiya.db.bak を作る (P11。consolidate の後だけ・recall では決して走らない)。
            self._backend.request_backup()
            return {"window": window_ids, "proposals": proposals, "decisions": decisions, "skipped": None, "window_sessions": window_sessions}

        result = self._locked(_write, with_ledger=True)
        if isinstance(notes, list) and self._backend.backup_error:
            notes.append(self._backend.backup_error)  # バックアップの失敗は統合の結果を変えない。notes へ。
        return result


INDEX_REASON_BUDGET = "注入文の容量で落ちた"     # ③ の項目が `manas.INJECTION_MAX_CHARS` に入らなかった (explain 用)


def _session_of(row):
    """台帳の行の session_id (str 以外は None。`LedgerStore._basis_of` と同じ補正)。"""
    return row.get("session_id") if isinstance(row.get("session_id"), str) else None


def _epoch_of(ts):
    """注入ログの ts (recall が書く UTC ISO 文字列。秒精度) / 台帳の ts (epoch 数) -> epoch 秒 (読めなければ None)。
    注入の ts は秒に切り捨てられるので、同じ秒内の往復との前後は注入が先と読む (R の判定は秒の粒度)。"""
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            parsed = datetime.datetime.fromisoformat(ts)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    return None


def _injected_by_turn(log, stamps):
    """#105 R: 窓の往復の添字 -> その往復の前 (ts <=) に同じ session へ注入された種子 id の集合。

    注入 = manas_log の `seed_ids` ∪ `index_ids`。往復か注入行のどちらかに session_id / ts が無ければ
    その往復は載せない (R は効かない = 従来どおり。fail-open)。`stamps` = [(session_id, ts)] 窓と同じ順。
    """
    by_session = {}
    for e in log:
        sid, ts = e.get("session_id"), _epoch_of(e.get("ts"))
        if not isinstance(sid, str) or ts is None:
            continue
        ids = {i for i in list(e.get("seed_ids") or []) + list(e.get("index_ids") or []) if isinstance(i, int)}
        by_session.setdefault(sid, []).append((ts, ids))
    out = {}
    for index, (sid, ts) in enumerate(stamps):
        if not isinstance(sid, str) or ts is None:
            continue
        ids = set()
        for inj_ts, inj_ids in by_session.get(sid, ()):
            if inj_ts <= ts:
                ids |= inj_ids
        if ids:
            out[index] = ids
    return out


def _join_warnings(*warnings):
    """None を除いて 1 つの警告文にする (全部 None なら None)。"""
    parts = [w for w in warnings if w]
    return " / ".join(parts) if parts else None


def _circles(composed):
    """`compose_injection` の戻り値から recall / explain の `circles` を作る (JSON そのままの型)。"""
    text = composed["text"]
    return {
        "siblings": composed["sibling_lines"],
        "groups": composed["groups"],
        "global": composed["global"],
        "cut": dict(composed["cut"]),
        "shortened": {form: list(ids) for form, ids in composed["shortened"].items()},
        "chars": len(text),
        "max_chars": _manas_module.INJECTION_MAX_CHARS,
    }


def _seed_summary(seed):
    """explain_recall の 1 行ぶん: id / kind (読む側の既定込み) / sansho / content。"""
    return {"id": seed["id"], "kind": seed_kind(seed), "sansho": seed.get("sansho"), "content": seed.get("content", "")}


def _provenance_evidence(evidence, rows):
    """`seed_provenance` の根拠 1 件: 台帳の行 (または台帳外の往復のスナップショット) の本文で span を探す。

    本文は三性ゲートが読んだ欄: user は根拠欄 `grounding_text` (行に欄が無い=None なら `user_text`。
    `""` は「根拠なし」のまま。`or` で user_text へ戻さない)、assistant は `assistant_text`。
    探すのは `sansho._n` 正規化後の文字列同士の完全一致 (ゲートと同じ。曖昧一致はしない)。
    """
    source = evidence.get("source")
    span = evidence.get("span") if isinstance(evidence.get("span"), str) else ""
    ledger_id = evidence.get("ledger_id")
    out = {
        "life": evidence.get("life"),
        "source": source,
        "span": span,
        "ledger_id": ledger_id,
        "row": None,
        "body_field": None,
        "found": False,
        "position": None,
        "excerpt": None,
        "problem": None,
    }
    body = None
    if _is_int(ledger_id):
        row = rows.get(ledger_id)
        if row is None:
            out["problem"] = "台帳に行 #{} が無い".format(ledger_id)
            return out
        out["row"] = {"session_id": row.get("session_id"), "project": row.get("project"), "ts": row.get("ts")}
        if source == "user":
            grounding = row.get("grounding_text")
            out["body_field"] = "user_text" if grounding is None else "grounding_text"
            body = row.get("user_text") if grounding is None else grounding
        else:
            out["body_field"] = "assistant_text"
            body = row.get("assistant_text")
    else:
        turn = evidence.get("turn") if isinstance(evidence.get("turn"), dict) else None
        if turn is None:
            out["problem"] = "台帳の行もスナップショットも無い"
            return out
        out["body_field"] = "turn.user_text" if source == "user" else "turn.assistant_text"
        body = turn.get("user_text") if source == "user" else turn.get("assistant_text")
    body_n = sansho._n(body if isinstance(body, str) else "")
    span_n = sansho._n(span)
    position = body_n.find(span_n) if span_n else -1
    if position < 0:
        out["problem"] = "span が台帳の本文に見つからない" if _is_int(ledger_id) else "span が往復のスナップショットに見つからない"
        return out
    end = position + len(span_n)
    out["found"] = True
    out["position"] = position
    out["excerpt"] = {
        "before": body_n[max(0, position - PROVENANCE_EXCERPT_CHARS):position],
        "span": span_n,
        "after": body_n[end:end + PROVENANCE_EXCERPT_CHARS],
    }
    return out


def _basis_counts(bases):
    """`(project, session_id)` のリスト -> `[{"project", "session_id", "pending"}]`。

    件数の多い順、同数なら project・session_id の文字列順。None は `"(全域)"` / `"(不明)"`
    と表示する (並べ替えも表示文字列で行う: None と str は比べられない)。
    """
    counts = {}
    for project, session_id in bases:
        key = (
            project if isinstance(project, str) else "(全域)",
            session_id if isinstance(session_id, str) else "(不明)",
        )
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return [{"project": p, "session_id": sid, "pending": n} for (p, sid), n in ordered]


def _log_check(log_path, ledger_last_id):
    """hooks.log 末尾の最後の `ledger_id=<整数>` を台帳の最終 id と比べる (fail-soft)。

    出所 (ログが自分で「記録した」と言っていること) では受けず、台帳の実物との照合の
    結果だけで受ける (T0001_003:71)。status:
    - "一致": 等しい。
    - "台帳が先行": 台帳のほうが大きい (record_turn の lock 解放後・ログ行の前に別の書き手が
      追記した / ログの書込に失敗した、で普通に起こる。異常としない)。
    - "不一致": ログのほうが大きい (存在しない行を記録したと言っている)。
    - "記録なし": hooks.log が無い・読めない・`ledger_id=` の行が無い。
    末尾 LOG_CHECK_TAIL_BYTES だけを読み、切り詰めたときは行頭の欠けた最初の行を捨てる
    (欠けた行の `ledger_id=` を読むと別の行の値を取りうる)。読み取りの失敗は例外にしない。
    """
    log_ledger_id = None
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size > LOG_CHECK_TAIL_BYTES:
                # 1 byte 手前から読み、最初の改行までを捨てる: 切り口がちょうど行頭に
                # 当たった場合 (直前の byte が改行) もその行を残せる。
                f.seek(size - LOG_CHECK_TAIL_BYTES - 1)
                data = f.read()
                newline = data.find(b"\n")
                data = data[newline + 1:] if newline >= 0 else b""
            else:
                f.seek(0)
                data = f.read()
        found = _LOG_LEDGER_ID_RE.findall(data)
        if found:
            log_ledger_id = int(found[-1])
    except Exception:
        log_ledger_id = None
    if log_ledger_id is None:
        status = "記録なし"
    elif log_ledger_id == ledger_last_id:
        status = "一致"
    elif log_ledger_id < ledger_last_id:
        status = "台帳が先行"
    else:
        status = "不一致"
    return {"status": status, "log_ledger_id": log_ledger_id, "ledger_last_id": ledger_last_id}


# 記録の種類 (kind) の説明。propose (CLI の一往復) と consolidate (hooks の台帳窓) の両プロンプトで共有する
# (改修5 §3.3): どちらの経路でも LLM は同じ 5 kind で提案し、形の検査 (alaya._resolve_kind) が同じに効く。
KIND_GUIDE = (
    "記録の種類 (kind) は次の 5 つです:\n"
    "- 決定: 採用したもの (片側だけの決定も 決定)。content には採用側を書き、却下側はユーザーが述べたときだけ"
    "添える (例:「A を採用」「A を採用 (B は却下: 理由)」)。ユーザーが言っていない却下側を作文しない。"
    "却下側を述べた発話があれば evidence の span に逐語で含める。"
    "ユーザーが短い承認 (「GO」「推奨でGO」など) でアシスタントの推奨を採ったときは、推奨を述べたアシスタントの発言と承認のユーザー発言を、"
    "別々の span として evidence に逐語で含める。"
    "このとき conditions(縁) には、採った案の名か値 (推奨の発言に逐語で現れる語) を 1 つ以上入れ、content には何についての決定か (題) も書く。\n"
    "- 回避策: ハマった症状 → 原因 → 回避 (原因を必ず書く)。ユーザーが訂正した発話と、"
    "それより前の対象の行為を述べた発話を、別々の span として evidence に逐語で含める"
    "(無ければ 性向 として扱われる)。ユーザー自身が原因と回避を述べた発話は、その 1 本を span に含めてよい。\n"
    "- 所在: 何がどこにあるか (リポジトリの外にある・探しにくいもの)。\n"
    "- 再開点: 中断時点と次の一手。\n"
    "- 性向: 持続する好み・方針。\n"
)
# 抽出しないもの (再取得できる事実) の範囲の説明。propose と consolidate の両プロンプトで共有する
# (改修5 §3.1): どちらの経路でも LLM は「コーディングの続きで忘れて困るもの」だけを記録対象にする。
SCOPE_GUIDE = (
    "抽出しないもの: コード・git 履歴・設定ファイルの内容など、リポジトリを読めば再取得できる事実。"
    "一回限りの出来事でも、決定・原因・所在のように再取得できないものは抽出してください。\n"
)
KIND_JSON_FIELD = '"kind": "決定 または 回避策 または 所在 または 再開点 または 性向", '


def _build_propose_prompt(user_text, assistant_text):
    return (
        "以下の一往復の会話から、コーディングの続きで忘れて困るものを記録として0〜2個抽出してください。\n"
        "{kind_guide}\n"
        "{scope_guide}\n"
        "ユーザー発言: {user_text}\n"
        "アシスタント応答: {assistant_text}\n\n"
        "次のJSON配列形式のみを出力してください(説明文やコードフェンスは不要です):\n"
        '[{{{kind_field}"content": "一文で記録を要約", "conditions": ["話題語1", "話題語2"], '
        '"polarity": "善または不善または無記", '
        '"evidence": {{"source": "userまたはassistant", "span": "上記本文からの逐語引用"}}}}]\n'
        "evidence.span は上記のユーザー発言・アシスタント応答のどちらかから一言一句そのまま"
        "引用してください(要約や言い換えは不可。裏付けが無ければ evidence.span は空文字にしてください)。"
        "conditions(縁)は上記のユーザー発言・アシスタント応答に逐語で現れる語を 1〜3 個"
        "(縁が本文に無ければその記録は出さない)。縁には主題語 (対象の固有名・ファイル名・機能名) を選んでください。"
        "身分(圓成實・依他起・遍計所執)はこちらで判定するため出力しないでください。"
        "記録が無ければ空配列 [] を返してください。"
    ).format(kind_guide=KIND_GUIDE, scope_guide=SCOPE_GUIDE, kind_field=KIND_JSON_FIELD, user_text=user_text, assistant_text=assistant_text)


def _grounding(turn):
    """往復の根拠本文。`grounding_text` が None (旧形式の行・台帳外の Turn) なら user_text。
    "" (全部が貼り付けだった) は "" のまま = 根拠なし (`or` で user_text に戻さない)。"""
    grounding = getattr(turn, "grounding_text", None)
    return turn.user_text if grounding is None else grounding


def _prompt_window(window):
    """統合プロンプトに見せる窓: user 発話を根拠本文に差し替える (貼り付けは LLM にも見せない)。"""
    return [dataclasses.replace(t, user_text=_grounding(t)) for t in window]


def _gate_window(window):
    """三性ゲートに渡す窓: user 発話 = 根拠本文、両側の切り詰めの印を GATE_CUT に置き換える。

    長さ・順序・id は元の窓と同じ (= alaya.imprint の turn_index / ledger_id はそのまま
    台帳の行を指す。evidence が保存するのは ledger_id だけで、この差し替えた本文は残らない)。
    """
    return [
        dataclasses.replace(t, user_text=gate_text(_grounding(t)), assistant_text=gate_text(t.assistant_text))
        for t in window
    ]


def _imprint_gate_turn(t):
    """imprint 用: 呼び出し側が組んだ往復 1 件 -> ゲートに渡す写し (id/life/project は保つ)。

    record_turn と同じ順: assistant = `<private>` 除去。user は `grounding_text` があればそれ
    (`<private>` だけ除く。None 以外の非 str は strip_private が "" にする = 根拠なし。`or` で
    user_text に戻さない)、無ければ `<private>` 除去 -> 貼り付け除去。最後に印を GATE_CUT へ。
    `grounding_text` を持たない旧形式の台帳行が来た場合も貼り付けを剥がす (統合の `_grounding` は
    旧行の user_text をそのまま使うので、ここだけ厳しい側に倒れる。現状その呼び出し元は無い)。
    """
    assistant = gate_text(strip_private(t.assistant_text))
    grounding = getattr(t, "grounding_text", None)
    if grounding is not None:
        user = strip_private(grounding)
    else:
        user = strip_pasted(strip_private(t.user_text))
    user = gate_text(user)
    changes = {"user_text": user, "assistant_text": assistant}
    if hasattr(t, "grounding_text"):
        changes["grounding_text"] = user
    return dataclasses.replace(t, **changes)


def _imprint_gate_window(window):
    """imprint 用の窓の写し (長さ・順序・id は元のまま)。"""
    return [_imprint_gate_turn(t) for t in window]


def _proposal_texts(proposal):
    """提案のうちゲートが本文と照合する文字列 (span と縁) を正規化して返す。"""
    conditions = proposal.get("conditions") if isinstance(proposal, dict) else None
    conditions = conditions if isinstance(conditions, list) else []
    texts = [item["span"] for item in sansho.evidence_list(proposal)]
    texts += [c for c in conditions if isinstance(c, str)]
    return [sansho._n(t) for t in texts]


def _quotes_gate_cut(proposal):
    """提案の span か縁のどれかが区切りの字 (U+FFFC) を含むか (正規化後で見る)。"""
    return any(GATE_CUT_CHAR in t for t in _proposal_texts(proposal))


def _blank_window(window):
    """本文を全て空にした窓 (長さ・順序・id は元のまま)。どの span も接地しないので、
    この窓で判定した提案は必ず遍計所執 (縁も接地しない)。"""
    return [dataclasses.replace(t, user_text="", assistant_text="") for t in window]


def _truncate_for_prompt(text, limit=CONSOLIDATE_TURN_CHARS):
    """プロンプト表示用の切り詰め: 先頭+末尾を残し間に `<elided N chars>` (処方 8)。
    ゲートは台帳の全文を見るので、この印はゲートに届かない。"""
    return elide_middle(text, limit)[0]


def _build_consolidate_prompt(window, view):
    """統合プロンプト (改修4 §7・改修5 §3.3)。抽出対象は 5 kind、各提案に kind を求める。

    除外は「リポジトリを読めば再取得できる事実」(本有) だけに限る: 決定・回避策・所在・再開点は
    一回限りの出来事だが再取得できない (始起) ので抽出する。各往復の見出しに project を出す
    (窓は複数 project が混ざりうる・§4.2)。索引の行にも kind を出す。
    """
    related_lines = [
        "#{} [{}] [{}] {} | 縁: {}".format(
            s["id"], seed_kind(s), s.get("sansho"), s.get("content", ""), ", ".join(seed_conditions(s))
        )
        for s in view["related"]
    ] or ["(無し)"]
    if view["related_total"] > len(view["related"]):
        related_lines.append("(他に {} 件)".format(view["related_total"] - len(view["related"])))
    group_lines = []
    for g in view["groups"]:
        rep = g.get("rep") or {}
        project = g.get("project")
        group_lines.append(
            "#{} [{}] [{}] {} | 群「{}」{} 件 (project: {})".format(
                g["representative"], seed_kind(rep if rep.get("kind") else g), rep.get("sansho"),
                rep.get("content", ""), g.get("label"), g.get("size"),
                project if isinstance(project, str) else "(全域)",
            )
        )
    group_lines = group_lines or ["(無し)"]
    if view["groups_total"] > len(view["groups"]):
        group_lines.append("(他に {} 群)".format(view["groups_total"] - len(view["groups"])))
    turn_blocks = []
    for t in window:
        project = getattr(t, "project", None)
        turn_blocks.append(
            "--- 往復 {} (project: {}) ---\nユーザー: {}\nアシスタント: {}".format(
                t.id, project if isinstance(project, str) else "(無し)",
                _truncate_for_prompt(t.user_text), _truncate_for_prompt(t.assistant_text),
            )
        )
    return (
        "以下は最近の会話の台帳 (往復ごと) と、既に記憶している記録のうちこの台帳の語に触れたもの・記録の群の見出しです。\n"
        "台帳から、コーディングの続きで忘れて困るものを記録として 0〜{max}個抽出してください。"
        "{kind_guide}\n"
        "{scope_guide}\n"
        "【既存の記録: この台帳の語に触れたもの】\n{index}\n\n"
        "【既存の記録の群 (見出し)】\n{groups}\n\n"
        "【台帳】\n{turns}\n\n"
        "次のJSON配列形式のみを出力してください(説明文やコードフェンスは不要です):\n"
        '[{{{kind_field}'
        '"action": "new または reinforce または supersede", "target_id": 既存記録の番号または null, '
        '"content": "一文で記録を要約", "conditions": ["話題語1", "話題語2"], '
        '"polarity": "善または不善または無記", '
        '"evidence": [{{"source": "user または assistant", "span": "台帳からの逐語引用"}}]}}]\n'
        "規則:\n"
        "- evidence.span は台帳のユーザー発言・アシスタント発言から一言一句そのまま引用してください"
        "(要約や言い換えは不可)。裏付けが無い記録は出力しないでください。ユーザー発言からの引用を優先してください。\n"
        "- ユーザー発言だけで記録の内容が裏付けられるときは、アシスタントの言い直し(同じ内容の復唱・要約)を evidence に加えないでください。"
        "アシスタント発言の引用は、ユーザー発言に無い内容を裏付けるときだけにしてください。\n"
        "- conditions(縁)は台帳のユーザー発言・アシスタント発言に逐語で現れる語を 1〜3 個"
        "(縁が本文に無ければその記録は出さない)。縁には主題語 (対象の固有名・ファイル名・機能名) を選び、"
        "「方針」「作業」のような一般語は避けてください。\n"
        "- 既存の記録が再確認されたら action を reinforce にし target_id を付けてください。"
        "既存の記録が古くなった・矛盾したら action を supersede にし target_id と置き換える内容を書いてください。"
        "それ以外は new です。\n"
        "- target_id は上に # で示した記録の番号だけです (群の代表の番号を含む)。群そのものは target にできません。\n"
        "- 身分(圓成實・依他起・遍計所執)はこちらで判定するため出力しないでください。\n"
        "- 記録が無ければ空配列 [] を返してください。"
    ).format(
        max=CONSOLIDATE_MAX, kind_guide=KIND_GUIDE, scope_guide=SCOPE_GUIDE, kind_field=KIND_JSON_FIELD,
        index="\n".join(related_lines), groups="\n".join(group_lines), turns="\n\n".join(turn_blocks),
    )


def _note(notes, message):
    """`notes` (診断専用の省略可リスト) へ理由文字列を1件追記する。

    `notes` が非 list (None・タプル等) のときは黙って何もしない。呼び出し元が
    誤った型を渡しても、寛容パース本体(fail-soft)を絶対に壊さないための番人
    (`notes is not None` だけの判定だと非 list を渡された場合に .append で例外に
    なり、有料の LLM 呼び出し後に往復ごと失われてしまう=診断用引数のせいで熏習が
    落ちるのは fail-soft パス上で許されない失敗モードのため、isinstance で守る)。
    """
    if isinstance(notes, list):
        notes.append(message)


def _extract_json_array(raw_text, notes=None):
    """LLM 出力からコードフェンスを剥がし、最初の '[' 〜 最後の ']' を JSON 配列として読む。
    失敗は notes に有界な理由だけ記して None を返す (`_parse_proposals` と共有)。"""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        _note(notes, "角括弧([ ])が出力中に見つからない(len={})".format(len(text)))
        return None
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError) as e:
        _note(notes, "JSON解析失敗: {}".format(type(e).__name__))
        return None


def _parse_proposals(raw_text, notes=None):
    """LLM 出力から種子の提案リストを寛容にパースする。失敗時は空リストを返す。

    既知キー (content/conditions/polarity/evidence/kind) だけを allowlist で読み取るため、
    LLM が "sansho" (身分) を出力しても構造的に無視される (七文書抽出仕様 EXE-15
    異熟果=無記: 提案は自分の身分を自称できない。身分は alaya.imprint 内の
    sansho.evaluate_proposal が決定論で判定する)。

    `notes`: 省略可のリスト。渡すと、寛容パースが働いた各分岐(括弧が見つからない/
    JSON 解析失敗/上限2件で切り捨て/項目検証で却下)で理由を表す短い文字列を
    追記する(診断専用・hooks.log 用)。生の LLM 出力そのものは追記しない
    (hooks.log は長期保存の平文ログのため、理由と件数などの有界な事実のみを記す)。
    `notes` の有無・中身は返り値(proposals)に一切影響しない — 「LLM が空配列を
    返した」(健全)と「出力を読み取れなかった」(異常)を区別する目的のみに使う。
    非 list (None 含む) を渡しても例外にならない (`_note` 参照)。

    注記: `_extract_json_array` は必ず先頭 "[" 〜 末尾 "]" で切り出すため、JSON として
    parse に成功した時点で結果は文法上必ず list になる ("[" で始まる有効な JSON 値は
    配列以外にありえない)。よって「配列でない JSON」を検出する分岐はかつて存在
    したが実際には到達不能な防御コードだったため削除した (2026-08-26 QA 指摘)。
    """
    data = _extract_json_array(raw_text, notes)
    if data is None:
        return []
    if len(data) > 2:
        _note(notes, "{}件中2件のみ採用(上限2)".format(len(data)))
    proposals = []
    rejected = 0
    for item in data[:2]:
        if not isinstance(item, dict):
            rejected += 1
            continue
        content = item.get("content")
        if not content:
            rejected += 1
            continue
        conditions = item.get("conditions") or []
        if not isinstance(conditions, list):
            conditions = []
        polarity = item.get("polarity") or "無記"
        evidence_raw = item.get("evidence")
        source = ""
        span = ""
        if isinstance(evidence_raw, dict):
            source = evidence_raw.get("source") or ""
            span = evidence_raw.get("span") or ""
        proposal = {
            "content": str(content),
            "conditions": [str(c) for c in conditions],
            "polarity": str(polarity),
            "evidence": {"source": str(source), "span": str(span)},
        }
        if isinstance(item.get("kind"), str):
            proposal["kind"] = item["kind"]  # 形の検査 (列挙・読み替え) は alaya.imprint が行う (改修5 §3.3)
        proposals.append(proposal)
    if rejected:
        _note(notes, "項目検証で{}件を却下(dict でない/content 欠落)".format(rejected))
    return proposals


def _parse_consolidation(raw_text, notes=None):
    """統合提案の寛容パース (改修4 §7・改修5 §3.3)。allowlist のキーだけ読む (sansho は構造的に無視、kind は通す)。

    action は VALID_ACTIONS 以外なら "new"、target_id は int (bool 除く) 以外なら None、
    evidence は dict->[dict]。list の非 dict 要素は捨てずに {"source": "", "span": ""} へ
    落とす (sansho.evidence_list と同じ fail-closed)。上限 CONSOLIDATE_MAX 件。

    戻り値は「読み取れなかった」= None と「LLM が空配列を返した」= [] を区別する
    (`_parse_proposals` は従来どおり両方 [] を返す。propose は窓を消費しないため
    区別が要らない)。呼び出し元の `consolidate` は None のとき窓を消費しない。
    """
    data = _extract_json_array(raw_text, notes)
    if data is None:
        return None
    if len(data) > CONSOLIDATE_MAX:
        _note(notes, "{}件中{}件のみ採用(上限{})".format(len(data), CONSOLIDATE_MAX, CONSOLIDATE_MAX))
    proposals = []
    rejected = 0
    for item in data[:CONSOLIDATE_MAX]:
        if not isinstance(item, dict) or not item.get("content"):
            rejected += 1
            continue
        conditions = item.get("conditions") or []
        if not isinstance(conditions, list):
            conditions = []
        action = item.get("action")
        if action not in VALID_ACTIONS:
            action = "new"
        target_id = item.get("target_id")
        if not _is_int(target_id):  # bool は int の派生型なので除く (ledger._is_int と同じ規則)
            target_id = None
        evidence_raw = item.get("evidence")
        if isinstance(evidence_raw, dict):
            evidence_raw = [evidence_raw]
        evidence = []
        if isinstance(evidence_raw, list):
            for e in evidence_raw:
                if isinstance(e, dict):
                    evidence.append({"source": str(e.get("source") or ""), "span": str(e.get("span") or "")})
                else:
                    # 形の壊れた要素 (str 等) は捨てずに空 span へ落とす: 空 span は
                    # 非接地=遍計所執と判定されるので、提案は全体が quarantine へ行く。
                    # ここで取り除くと接地済みの span だけが残って判定が採用の側へ動き、
                    # `imprint`/MCP 経由 (sansho.evidence_list) と結論が食い違う
                    # (改修4 §13 裁定1 と同じ fail-closed をパーサ側にも効かせる)。
                    evidence.append({"source": "", "span": ""})
        proposal = {
            "content": str(item["content"]),
            "conditions": [str(c) for c in conditions],
            "polarity": str(item.get("polarity") or "無記"),
            "action": action,
            "target_id": target_id,
            "evidence": evidence,
        }
        # kind (改修5 §3.3): allowlist に足す (足さないと未知キーとして剥がされ imprint に届かない)。
        # 値の検査 (VALID_KINDS・形) は alaya.imprint がする。欄が無ければ付けない (= 性向として受理)。
        if isinstance(item.get("kind"), str):
            proposal["kind"] = item["kind"]
        proposals.append(proposal)
    if rejected:
        _note(notes, "項目検証で{}件を却下(dict でない/content 欠落)".format(rejected))
    return proposals
