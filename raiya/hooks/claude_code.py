"""claude_code.py -- Claude Code hooks 統合(記憶機能設計書 波4・改修4 §8)

UserPromptSubmit(recall)= 想起注入 (発芽した種子の自己物語+性向の索引)、
Stop(stop、`async: true` 想定)= 往復を台帳へ追記し溜まっていれば統合、
SessionEnd(session_end)= 台帳に残った未統合の往復を統合、を Claude Code の hooks
経由で自動化する。

起動: `python3 -m raiya.hooks.claude_code <recall|stop|session_end>`
(`raiya/hooks/settings.example.json` 参照)。

統合の単位は 1 往復ではなく台帳の窓 (改修4 §3/§7。「熏習」は同時・同処の植え付けの名なので時間差のある統合には使わない: 起動設計書 §7.1-7): stop は LLM を呼ばず往復を台帳へ
追記するだけで、未統合が CONSOLIDATE_EVERY 件溜まったときと、session_end でセッションが
終わるときにだけ統合 (提案→三性ゲート→保存) が走る。

SessionEnd だけ `"async": true` を付けない (timeout 120 の同期実行)。統合は LLM 呼び出しを
含むので時間はかかるが、async にするとセッション終了の後片付けで、統合結果を書き出す前に
プロセスごと kill されうる (取りこぼした未統合は次のセッションの stop が拾うので致命では
ないが、まず落とさない側に倒す)。

規律 (最優先・全サブコマンド共通):
    - 常に exit 0 (fail-soft)。例外は `$RAIYA_STATE_DIR/hooks.log` に1行追記して黙る
      (Claude Code のセッションを止めない。例外文字列そのものは書かない=クラス名・
      失敗カテゴリ・長さのみ。`_exc_summary` 参照・記憶機能設計書§10.3d)。
    - `RAIYA_HOOK_ACTIVE=1` なら即 exit 0 (再帰ガード)。統合 (`engine.consolidate`) が
      `claude -p` を subprocess で呼ぶとき (`backend._call_claude_cli` は対話本文を含む全
      purpose でこれを立てる。Issue #26)、その子プロセスの env に必ずこれを立てる。子プロセス自体が新しい Claude Code
      起動でも、そこで発火した hooks はここで即座に止まる=無限ループを断つ。
    - `RAIYA_HOOKS=0` なら即 exit 0 (無効化)。hooks を読む別 CLI (grok 等) のランチャーは
      これを立てる (設計書 §10.6 処方 3)。
    - stdin に空でない `agent_id` がある (Claude Code のサブエージェント) なら recall も
      stop も何もしない (`_is_subagent_payload`・処方 9)。
    - stop は、応答が空の往復 (処方 2) と、`<private>` と貼り付けを除くと skip 対象に
      なる往復 (処方 7) を台帳に入れない。
    - `project` (改修5 §4.2) = stdin の `cwd` を `project.resolve_project` で git の最上位に正規化
      したもの。recall は `engine.recall(project=…)` に、stop は `record_turn(project=…)` に渡す。
      `cwd` が無ければ None (全域)。recall はセッション最初のプロンプトで `session_start=True`
      も渡す (再開点を 1 回だけ出す・§6.3)。
    - harness 由来の注入 (`<system-reminder>` 等) はユーザー発話ではないので、想起の
      文脈窓にも台帳にも入れない (`_is_harness_injected`・改修4 §8)。判定規則は
      「先頭 (前後の空白を除く) のタグ名」で、これを **text ブロックごとに** 適用する。
      セッションキャッシュ経由の本文 (`cmd_recall`/`cmd_stop` の skip 判定) と、
      transcript fallback 経由の本文 (`_find_last_user_text` の walk-back と
      `_extract_user_text_from_content` のブロック除去) の両方に効かせる: 実発話の
      後ろに注入ブロックが足された user エントリは、連結すると先頭がユーザー本文に
      なって判定をすり抜けるため、ブロック単位で落とさなければ注入本文が台帳へ載る
      (=統合で圓成實に接地しうる)。

セッションキャッシュ (`$RAIYA_STATE_DIR/sessions/<session_id>.json`
= {"prompt", "ts", "recent"}):
    recall (UserPromptSubmit) が毎回このキャッシュへ prompt を書く(短い/`/`始まりの
    prompt でも書く=キャッシュへの記録と、出力/熏習に使うかどうかの skip 判定は別)。
    `recent` は直前までの (skip 対象でない) プロンプトを古い順に最大
    RECALL_CONTEXT_TURNS 件保持し、そのまま想起の文脈窓になる (現在の prompt は含めない)。
    stop (Stop) は transcript の書込みが非同期で遅延しうるため、まずこのキャッシュを
    読んで user 本文を復元し、無ければ transcript の最後の user 発話へ fallback する。
    skip 判定 (空/短い/`/`始まり/harness 注入) は stop 側でも独立に再度行う。
"""
import json
import os
import sys
import time

from .. import config, debuglog, storage
from ..engine import DEFAULT_STATE_DIR, MemoryEngine, StateNeedsMigration, StateUnreadable, _resolve_state_dir
from ..ledger import strip_pasted, strip_private
from ..project import resolve_project
# ホストに依らない部分は core.py (#62)。ここで再 export する (既存のテスト・呼び出し元が
# `claude_code._truncate` 等で引くため。移した名前は core が唯一の定義)。
from .core import (  # noqa: F401
    CONSOLIDATE_EVERY, HARNESS_TAGS, MIN_PROMPT_LEN, RECALL_CONTEXT_TURNS, SESSIONS_MAX_KEEP, TRUNCATE_LIMIT,
    _LEADING_TAG_RE, _emit_consolidate, _exc_summary, _hooks_log_path, _is_harness_injected, _is_skippable_prompt,
    _log, _ms, _now_iso, _prune_sessions, _run_consolidation, _safe_session_id, _sessions_dir, _stderr_line,
    _truncate, prepare_record,
)


def _is_subagent_payload(payload):
    """stdin に空でない `agent_id` があるか = Claude Code のサブエージェントの hook か (処方 9)。

    サブエージェントの UserPromptSubmit は親と同じ session_id を持ちうる (未検証・推定) ため、
    recall が sessions/<sid>.json を上書きすると、親の Stop がサブエージェントへの委任文を
    「ユーザー発話」として台帳へ書いてしまう。よって recall と stop の両方で入口で止める。
    str 以外の真値 (壊れた payload) も記録しない側に倒す。別 CLI (grok 等) にはこの欄が
    付かないので、そちらは起動側が `RAIYA_HOOKS=0` を立てる (処方 3)。
    """
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str):
        return bool(agent_id.strip())
    return bool(agent_id)


# --- ログ・キャッシュ (state_dir 配下、いずれも fail-soft = 書けなくても例外にしない) ---


def _context_window(previous):
    """想起の文脈窓 = 直前までの (skip 対象でない) プロンプト (改修4 §6/§8)。

    `previous` は直前のキャッシュ dict (無ければ None)。古い順・最大
    RECALL_CONTEXT_TURNS 件で、現在の prompt は含めない。返り値はそのまま
    `engine.recall(text, context=...)` へ渡せる str のリスト: 非 str の要素は落とす
    (alaya.germinate は str の列を要求する: str 単体は TypeError、非 str 要素は照合を壊す)。
    キャッシュへ書く recent と想起へ渡す文脈窓は同じもの=ここだけで組み立てる。
    持ち越すのは `strip_pasted` 後の本文 (貼り付けは持ち越さない。剥がした後が skip 対象=
    貼り付けだけの prompt は積まない)。今回の prompt は貼り付けを含めたまま照合する
    (spec 2026-10-26-paste-recall-and-twin-reinforce-design.md §3 P1/P2, #44)。
    """
    previous = previous or {}
    recent = [r for r in (previous.get("recent") or []) if isinstance(r, str)]
    prompt = previous.get("prompt")
    if isinstance(prompt, str) and not _is_skippable_prompt(prompt):
        stripped = strip_pasted(prompt)
        if isinstance(stripped, str) and not _is_skippable_prompt(stripped):
            recent.append(stripped)
    return recent[-RECALL_CONTEXT_TURNS:]


def _write_session_cache(state_dir, session_id, prompt, recent=None):
    """prompt と文脈窓 (`recent`) をキャッシュへ書く。

    `recent` は `_context_window()` が組み立てたリスト (古い順・最大
    RECALL_CONTEXT_TURNS 件。現在の prompt は含めない)。呼び出し元が想起へ渡すのと
    同じリストをそのまま受け取る = キャッシュに残る窓と想起に使った窓が食い違わない。
    """
    session_id = _safe_session_id(session_id)
    if session_id is None:
        return
    recent = list(recent or [])
    sdir = _sessions_dir(state_dir)
    try:
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "{}.json".format(session_id))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"prompt": prompt, "ts": time.time(), "recent": recent}, f, ensure_ascii=False)
        _prune_sessions(sdir)
    except OSError:
        pass


def _read_session_cache(state_dir, session_id):
    """キャッシュ dict ({"prompt","ts","recent"}) を返す。無い/壊れていれば None。"""
    session_id = _safe_session_id(session_id)
    if session_id is None:
        return None
    path = os.path.join(_sessions_dir(state_dir), "{}.json".format(session_id))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "prompt" in data:
            recent = data.get("recent")
            data["recent"] = [r for r in recent if isinstance(r, str)] if isinstance(recent, list) else []
            return data
    except (OSError, ValueError):
        pass
    return None


def _read_session_cache_prompt(state_dir, session_id):
    """キャッシュされた prompt を返す。無い/壊れていれば None (transcript fallback へ)。

    非 str の prompt (壊れた/手書きのキャッシュ) は "" へ正規化する: そのまま返すと
    呼び出し側の `_is_skippable_prompt` が `int.strip()` で落ち、fail-soft の catch-all
    まで飛んで往復ごと取りこぼす (`cmd_recall` の `prompt if isinstance(prompt, str)`
    と同じ番人)。"" は「キャッシュはあるが空」= skip 扱いで、transcript へは戻らない。
    """
    data = _read_session_cache(state_dir, session_id)
    if data is None:
        return None
    prompt = data.get("prompt")
    return prompt if isinstance(prompt, str) else ""


# --- transcript (JSONL) 抽出 ---
# 実物の型は "user"/"assistant" (~/.claude/projects/*/*.jsonl で確認)。docs には
# "user_message"/"assistant_message" の表記もあるため両方に寛容にする。


def _read_transcript(transcript_path):
    lines = []
    if not transcript_path:
        return lines
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        pass
    return lines


def _extract_text_from_content(content):
    """message.content から平文を取り出す。文字列ならそのまま、リストなら
    type=="text" のブロックだけを連結する (tool_use/tool_result 等は除外)。

    `text` が str でないブロックは落とす (`_extract_user_text_from_content` の
    harness 判定と同じ fail-closed): 素通しすると連結の `"".join` が TypeError になり、
    fail-soft の catch-all まで飛んで往復ごと取りこぼす。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_user_text_from_content(content):
    """user エントリの message.content から平文を取り出す (harness 注入ブロックは落とす)。

    Claude Code は実発話の後ろに `<system-reminder>…` を別の text ブロックとして足す
    ことがある。そのまま連結すると先頭はユーザーの本文なので `_is_harness_injected` を
    すり抜け、注入本文が台帳へ載ってしまう (統合で圓成實に接地しうる=「ユーザーが
    言った」の意味が壊れる)。判定はセッションキャッシュ側と同じ「先頭のタグ」規則を
    ブロックごとに適用する。assistant 側は素の `_extract_text_from_content` のまま
    (注入は user 枠にしか差し込まれない)。
    """
    if isinstance(content, list):
        kept = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text = b.get("text")
                # text が str でない壊れたブロックは実発話ではないので落とす。
                # そのまま `_is_harness_injected` に渡すと .lstrip で AttributeError に
                # なり、fail-soft の catch-all まで飛んで往復ごと取りこぼす。
                if not isinstance(text, str) or _is_harness_injected(text):
                    continue
            kept.append(b)
        content = kept
    return _extract_text_from_content(content)


def _is_real_user_entry(entry):
    """type が user/user_message で、tool_result のみのブロックでないこと
    (tool 実行結果は user ロールとして transcript に載るが、ユーザー発話ではない)。"""
    if not isinstance(entry, dict):
        return False
    if entry.get("type") not in ("user", "user_message"):
        return False
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, list) and content and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return False
    return True


def _find_last_user_text(transcript_lines):
    for entry in reversed(transcript_lines):
        if not _is_real_user_entry(entry):
            continue
        message = entry.get("message") or {}
        text = _extract_user_text_from_content(message.get("content")).strip()
        if _is_skippable_prompt(text):
            # 空/短い/スラッシュコマンド/harness 注入だけのエントリはスキップして、
            # より前の実発話を探す (session cache 経由と同じ skip 規則を使う)。
            continue
        return text
    return ""


def _find_last_assistant_text(transcript_lines):
    for entry in reversed(transcript_lines):
        if not isinstance(entry, dict) or entry.get("type") not in ("assistant", "assistant_message"):
            continue
        message = entry.get("message") or {}
        text = _extract_text_from_content(message.get("content"))
        if text:
            return text
    return ""


# --- サブコマンド ---


def _debug_common(payload, sub):
    """debug.log の共通キー (level > 0 のときだけ呼ぶ)。`session_id_ok` は hooks.log が生の
    session_id を書くことへの突き合わせ用。"""
    raw = payload.get("session_id") if isinstance(payload, dict) else None
    safe = _safe_session_id(raw)
    return {"sub": sub, "session_id": safe, "session_id_ok": safe is not None}


def cmd_recall(payload, state_dir, debug_level=0):
    """UserPromptSubmit: 想起注入 (同期・<1s 目安)。

    注入するのは発芽した種子の自己物語と性向の索引 (改修4 §6)。発芽が無くても圓成實の
    索引があれば注入する (依他起・未分類は発芽したときだけ narrative に出る。どちらも
    無ければ何も出さない)。想起の照合には現在の prompt に加えて直前までのプロンプト
    (文脈窓) も使う。

    戻り値は outcome 文字列 (`"ok"` / `"skip:<code>"`・debug.log の hook_end 用)。
    `debug_level` > 0 のときだけ debug.log へ `recall` イベントを出す (0 なら何も余分に計算しない)。
    """
    dbg = {"t0": time.monotonic()} if debug_level else None
    try:
        return _cmd_recall(payload, state_dir, dbg)
    finally:
        if dbg is not None and dbg.get("reached"):
            _emit_recall(payload, state_dir, debug_level, dbg)


def _emit_recall(payload, state_dir, debug_level, dbg):
    fields = _debug_common(payload, "recall")
    fields.update(
        project=dbg.get("project"), cwd_given=bool(payload.get("cwd")),
        session_start=dbg.get("session_start"), life=dbg.get("life"), life_begun=dbg.get("life_begun"),
        engine_ms=dbg.get("engine_ms"), recall_ms=dbg.get("recall_ms"), skippable=dbg.get("skippable"),
        context_n=dbg.get("context_n"), seed_ids=dbg.get("seed_ids"), index_ids=dbg.get("index_ids"),
        resume_id=dbg.get("resume_id"), narrative_len=dbg.get("narrative_len"), emitted=dbg.get("emitted"),
        dur_ms=_ms(dbg["t0"]),
    )
    if debug_level >= 2:
        fields.update(
            prompt_raw=dbg.get("prompt_raw"), prompt=dbg.get("prompt"), context=dbg.get("context"),
            narrative=dbg.get("narrative"),
        )
    debuglog.emit(state_dir, debug_level, "recall", **fields)


def _cmd_recall(payload, state_dir, dbg):
    if _is_subagent_payload(payload):
        return "skip:subagent"  # 処方 9: キャッシュにも書かない (親セッションの Stop が読む本文を守る)
    session_id = payload.get("session_id")
    prompt = payload.get("prompt")
    if prompt is None:
        prompt = payload.get("user_input")  # docs 表記のキー名にも寛容
    # LOW-3: prompt が文字列でない (例: 数値) 場合、キャッシュ書込より前に空文字へ
    # 正規化する。素通りさせると後続の .strip()/len() で例外になり fail-soft が破れる。
    prompt = prompt if isinstance(prompt, str) else ""
    if dbg is not None:
        dbg["reached"] = True
        dbg["prompt_raw"] = prompt  # `<private>` を剥がす前 (レベル 2 だけが書く)
    # `<private>` (本人の「記憶させない」) はキャッシュ (= stop が台帳へ書く本文・次の文脈窓)
    # にも想起の照合にも使わない (設計書 §10.6 処方 6)。除去は線形時間 (同期 hook の予算内)。
    # 今回の prompt は貼り付けを含めたまま照合する。文脈窓 (recent) へは strip_pasted 後の本文だけを
    # 持ち越す (_context_window; spec 2026-10-26-paste-recall-and-twin-reinforce-design.md P1/P2, #44)。
    # 台帳では stop/record_turn が根拠から剥がす。
    prompt = strip_private(prompt)

    # このセッションの最初のプロンプトか (=sessions/<session_id>.json がまだ無いか) を
    # 書込前に判定する。session_id が無い/不正な入力では生を進めない
    # (_safe_session_id が None を返す=判定不能のため False のまま)。
    safe_session_id = _safe_session_id(session_id)
    is_first_prompt_of_session = safe_session_id is not None and not os.path.exists(
        os.path.join(_sessions_dir(state_dir), "{}.json".format(safe_session_id))
    )

    # 状態 DB を読めない (P11) ときに sessions/ を作らないよう、engine の構築 (= DB の検査) は
    # キャッシュ書込より前に行う。is_first_prompt_of_session は上でキャッシュの不在から判定済み。
    # hooks は毎ターン新規プロセスで MemoryEngine を構築するため、既定の new_life=True の
    # ままだと「1 ターン=1 生」になる。改修4 で種子は削除も減衰もされなくなった
    # (KUN-3 恒隨轉) ので生を二重計上しても種が消えることはもう無いが、planted_at /
    # miss_streak (連続不発芽の生の数) と末那識の注入ログは今も生を単位に数えるため、
    # 二重計上すれば観測値がそのまま狂う。new_life=False で構築し、このセッションの
    # 最初のプロンプトのときだけ明示的に begin_new_life() を1回呼ぶことで
    # 「1 生=1 Claude Code セッション」を保つ (CLI/MCP は既定 True のまま不変)。
    # engine_ms は構築 + begin_new_life だけ (間のキャッシュ I/O を含めない)。
    if dbg is not None:
        # 構築に失敗してもレベル 2 に prompt / session_start が残るよう、構築より前に入れる。
        dbg.update(prompt=prompt, session_start=is_first_prompt_of_session)
    engine_started = time.monotonic() if dbg is not None else None
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    engine_ms = _ms(engine_started) if dbg is not None else 0
    # skip 判定 (空/短い/スラッシュ/harness 注入) に関わらずキャッシュへ書く。stop 側が
    # transcript の非同期書込遅延を避けてこのキャッシュを主に読むための素材として残す
    # (skip 判定そのものは stop 側で改めて行う)。skip 対象のプロンプトは文脈窓 (recent)
    # には積まれない=想起の縁にも熏習の材料にもならない。
    previous = _read_session_cache(state_dir, session_id)
    # 文脈窓は 1 度だけ組み立て、キャッシュへ書く recent と想起へ渡す context の両方に
    # 使う (2 度組み立てると、片方だけを直す変更で両者が静かに食い違う)。
    context = _context_window(previous)
    _write_session_cache(state_dir, session_id, prompt, context)
    if dbg is not None:
        dbg.update(context=context, context_n=len(context))

    life_started = time.monotonic() if dbg is not None else None
    if is_first_prompt_of_session:
        engine.begin_new_life()
    if dbg is not None:
        dbg.update(engine_ms=engine_ms + _ms(life_started), life=engine.life, life_begun=is_first_prompt_of_session)

    if _is_skippable_prompt(prompt):
        if dbg is not None:
            dbg["skippable"] = True
            dbg["project"] = resolve_project(payload.get("cwd"))
        return "skip:skippable_prompt"

    project = resolve_project(payload.get("cwd"))
    recall_started = time.monotonic() if dbg is not None else None
    recalled = engine.recall(
        prompt, context=context,
        project=project,
        session_start=is_first_prompt_of_session,
        # 注入ログに session を残す (inspect spec §2.1)。sessions/<id>.json と同じ検査を通した値だけ
        # (非 str・空・パス区切りや ".." を含むものは None)。
        session_id=safe_session_id,
    )
    if dbg is not None:
        resume = recalled.get("resume")
        dbg.update(
            recall_ms=_ms(recall_started), skippable=False, project=project,
            seed_ids=[x.get("id") for x in recalled.get("seeds") or []],
            index_ids=[x.get("id") for x in recalled.get("index") or []],
            resume_id=resume.get("id") if isinstance(resume, dict) else None,
            narrative=recalled["narrative"], narrative_len=len(recalled["narrative"] or ""),
            emitted=bool(recalled["narrative"]),
        )
    if recalled["narrative"]:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": recalled["narrative"],
            }
        }
        print(json.dumps(output, ensure_ascii=False))
    return "ok"


def cmd_stop(payload, state_dir, debug_level=0):
    """Stop: 往復を台帳へ追記し、未統合が溜まっていれば統合する。async 実行想定。

    stdout は出さない。ここでは LLM を呼ばない (追記は決定論)。統合が走るのは未統合が
    CONSOLIDATE_EVERY 件に達したときだけ=統合の単位は 1 往復ではなく窓 (改修4 §3/§7)。

    戻り値は outcome 文字列 (`"ok"` / `"skip:<code>"`)。`debug_level` > 0 のときだけ
    debug.log へ `stop` イベントを出す (0 なら何も余分に計算しない)。
    """
    dbg = {"t0": time.monotonic()} if debug_level else None
    try:
        return _cmd_stop(payload, state_dir, debug_level, dbg)
    finally:
        if dbg is not None and dbg.get("reached") and not dbg.get("emitted"):
            _emit_stop(payload, state_dir, debug_level, dbg)


def _emit_stop(payload, state_dir, debug_level, dbg):
    dbg["emitted"] = True
    fields = _debug_common(payload, "stop")
    for key in ("project", "user_src", "asst_src", "user_len", "user_len_after_private", "grounding_len",
                "asst_len", "asst_len_after_private", "truncated", "ledger_id", "pending",
                "consolidate_triggered", "transcript_read"):
        fields[key] = dbg.get(key)
    fields["dur_ms"] = _ms(dbg["t0"])
    if debug_level >= 2:
        for key in ("user_text_raw", "assistant_text_raw", "user_text", "grounding_text", "assistant_text"):
            fields[key] = dbg.get(key)
    debuglog.emit(state_dir, debug_level, "stop", **fields)


def _cmd_stop(payload, state_dir, debug_level, dbg):
    if payload.get("stop_hook_active"):
        return "skip:stop_hook_active"  # Claude Code 自身の再帰ガード。true なら何もしない。

    session_id = payload.get("session_id")
    if _is_subagent_payload(payload):
        _log(state_dir, "session={} skip: サブエージェント (agent_id)".format(_safe_session_id(session_id)))
        return "skip:subagent"
    transcript_lines = None  # 遅延読込 (session cache が使えれば読まない)

    def _transcript():
        nonlocal transcript_lines
        if transcript_lines is None:
            transcript_lines = _read_transcript(payload.get("transcript_path"))
        return transcript_lines

    # user 本文: session cache 優先 (recall が書いたもの)、無ければ transcript fallback。
    user_text = _read_session_cache_prompt(state_dir, session_id)
    user_src = "cache"
    if user_text is None:
        user_text = _find_last_user_text(_transcript())
        user_src = "transcript"
    if dbg is not None:
        # 受け取った本文 (stop 自身の strip_private 等の前)。cache 経由なら recall が private を
        # 剥がした後なので private は無く、transcript 経由のときだけ含みうる (設計書 §9.1)。
        dbg.update(
            reached=True, user_src=user_src, user_text_raw=user_text, user_len=len(user_text),
            transcript_read=transcript_lines is not None,
        )

    # skip 判定は stop 側でも独立に再度行う (session cache 経由でも transcript
    # fallback でも、空/短い/スラッシュコマンド/harness 注入は台帳にも載せない)。
    if _is_skippable_prompt(user_text):
        _log(state_dir, "session={} skip: user_text が空/短い/スラッシュ/harness 注入".format(_safe_session_id(session_id)))
        return "skip:skippable_user"

    # assistant 本文: stdin の last_assistant_message を主に使う (transcript は
    # 非同期書込で遅延しうるため)。無ければ transcript の最後の assistant へ fallback。
    assistant_text = (payload.get("last_assistant_message") or "").strip()
    asst_src = "stdin"
    if not assistant_text:
        assistant_text = _find_last_assistant_text(_transcript())
        asst_src = "transcript"
    if dbg is not None:
        dbg.update(
            asst_src=asst_src, assistant_text_raw=assistant_text, asst_len=len(assistant_text),
            transcript_read=transcript_lines is not None,
        )

    # 本文処理 (除去は切り詰めの**前**・根拠は切る前の本文から貼り付けを剥がす) は core.prepare_record
    # (設計書 §2.4 の 1〜5。raiya-hook record と共有)。記録 (user_text) は貼り付けを残したまま切る。
    # record_turn も `<private>` を除く (冪等)。
    prep = prepare_record(user_text, assistant_text)
    if dbg is not None:
        dbg.update(
            user_len_after_private=prep.user_len_after_private, asst_len_after_private=prep.asst_len_after_private,
            grounding_len=prep.grounding_len,
        )

    # 処方 2: 応答が空の往復は台帳に入れない (片側しか無く熏習の材料として欠ける。本番では
    # 他 CLI の委任文 74 行が全てこの形だった)。transcript fallback の後、`<private>` を
    # 除いた後で見る (応答が private だけなら同じく空)。
    # (生の user の skippable は上の早期判定が先に返すので、prep.skip が skippable_user になることは無い。)
    if prep.skip == "skip:empty_assistant":
        _log(state_dir, "session={} skip: assistant_text が空".format(_safe_session_id(session_id)))
        return prep.skip
    # 処方 7: 貼り付けと `<private>` を除くと skip 対象 (空/短い/スラッシュ/harness 注入) に
    # なる往復は台帳に入れない (貼り付けだけ・private だけの往復)。生の本文での判定 (上) は
    # 先頭が harness タグの往復のために残す。
    if prep.skip is not None:
        _log(state_dir, "session={} skip: 貼り付け/private を除いた user_text が空/短い/スラッシュ/harness 注入".format(_safe_session_id(session_id)))
        return prep.skip

    grounding_text, grounding_truncated = prep.grounding_text, prep.grounding_truncated
    user_text, user_truncated = prep.user_text, prep.user_truncated
    assistant_text, assistant_truncated = prep.assistant_text, prep.assistant_truncated
    if user_truncated or assistant_truncated:
        _log(state_dir, "session={} 本文を{}字で切り詰めました".format(_safe_session_id(session_id), TRUNCATE_LIMIT))

    # stop は生を進めない (「1 生=1 セッション」の起点は recall のセッション最初の
    # プロンプトのみ)。new_life=False で構築する。
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    project = resolve_project(payload.get("cwd"))
    recorded = engine.record_turn(
        user_text, assistant_text, session_id, grounding_text=grounding_text,
        project=project,
    )
    _log(state_dir, "session={} ledger_id={} pending={}".format(_safe_session_id(session_id), recorded["ledger_id"], recorded["pending"]))
    triggered = recorded["pending"] >= CONSOLIDATE_EVERY
    if dbg is not None:
        dbg.update(
            project=project, ledger_id=recorded["ledger_id"], pending=recorded["pending"],
            consolidate_triggered=triggered,
            truncated={"user": user_truncated, "grounding": grounding_truncated, "assistant": assistant_truncated},
            user_text=user_text, grounding_text=grounding_text, assistant_text=assistant_text,
        )
        # 統合のイベントより先に出す (時系列を保つ)。
        _emit_stop(payload, state_dir, debug_level, dbg)
    if triggered:
        _run_consolidation(engine, state_dir, session_id, debug_level, "stop")
    return "ok"


def cmd_session_end(payload, state_dir, debug_level=0):
    """SessionEnd: 台帳に残った未統合の往復を統合する。窓が空なら何もしない (LLM も呼ばない)。

    stdout は出さない。payload の `reason` (clear/logout/... ) は見ない: どの終わり方でも
    やることは「残りを統合する」だけで、統合できなければ次のセッションの stop が拾う。
    """
    session_id = payload.get("session_id")
    engine = MemoryEngine(state_dir=state_dir, new_life=False)
    _run_consolidation(engine, state_dir, session_id, debug_level, "session_end")
    return "ok"  # 統合の失敗は consolidate イベントに出る (hook_end は ok)


SUBCOMMANDS = {"recall": cmd_recall, "stop": cmd_stop, "session_end": cmd_session_end}


def main(argv=None):
    """常に exit 0 (fail-soft)。例外は hooks.log に記録して黙る。"""
    try:
        _main_impl(argv)
    except Exception:
        # 万一 _main_impl 自体が (state_dir 解決前などで) 想定外に失敗しても、
        # このプロセスの exit code には影響させない。
        pass


def _main_impl(argv):
    argv = sys.argv[1:] if argv is None else argv
    sub = argv[0] if argv and argv[0] in SUBCOMMANDS else None

    # 再帰ガード・無効化は最優先 (サブコマンド判定・stdin 読込・設定ファイルの読み込みより前)。
    # 設定ファイルは読まない: guard の経路の state_dir は env / 既定だけで決める
    # (ファイルだけで state_dir を決めている利用者は guard のイベントが debug.log に残らない・
    # 設定ファイル設計書 §6/§10)。`debuglog.level` は os.stat 1 回 (フラグ無しなら 0)。
    guard = None
    if os.environ.get("RAIYA_HOOK_ACTIVE") == "1":
        guard = "hook_active"
    elif os.environ.get("RAIYA_HOOKS") == "0":
        guard = "hooks_off"
    if guard is not None:
        g_state_dir = os.environ.get("RAIYA_STATE_DIR") or DEFAULT_STATE_DIR
        g_level = debuglog.level(g_state_dir)
        if g_level:
            g_started = time.monotonic()
            debuglog.emit(g_state_dir, g_level, "guard", reason=guard, argv0=argv[0] if argv else None, sub=sub)
            debuglog.emit(g_state_dir, g_level, "hook_end", sub=sub, outcome="skip:guard", dur_ms=_ms(g_started))
        return

    # このプロセスで設定ファイルを読むのはここだけ (get() は 1 プロセス 1 回のメモ化)。
    cfg = config.get()
    if config.state_dir_lost(cfg) and not os.environ.get("RAIYA_STATE_DIR"):
        # state_dir を決めるはずの設定ファイルが壊れていて env も無い: ./state へ落とすと記憶が
        # プロジェクトごとに黙って分かれる (§5.3)。何も書かずに止める (hooks.log は書く場所が無い)。
        _stderr_line("raiya: 設定ファイルが不正で state_dir を決められないため hook を止めました ({})".format(
            config.summary(cfg)))
        return

    state_dir = _resolve_state_dir()
    # 旧 JSON 版の state (P12): hooks.log・stdin 読込・sessions/・.lock・debug.log のどれよりも前に判定し、
    # 何も書かずに固定 1 行を stderr へ (os.stat のみ)。再帰ガード・設定喪失の判定より後。
    if storage.state_status(state_dir) == "legacy":
        _stderr_line("raiya: 旧 JSON 版の state (移行しない。脇に退けてから起動) のため hook を止めました")
        return
    dlevel = debuglog.level(state_dir)
    started = time.monotonic() if dlevel else None
    if cfg.issues:
        _log(state_dir, "config 不正 ({}) — 有効なキーと env で続行".format(config.summary(cfg)))

    if sub is None:
        return

    outcome = "ok"
    payload = None
    error = None
    raw = None
    try:
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
        except Exception as e:
            _log(state_dir, "stdin 解析失敗 ({}): {}".format(sub, _exc_summary(e)))
            outcome = "fail:stdin:{}".format(type(e).__name__)
            error = str(e) if dlevel >= 2 else None
            return

        try:
            if dlevel:
                result = SUBCOMMANDS[sub](payload, state_dir, debug_level=dlevel)
            else:
                result = SUBCOMMANDS[sub](payload, state_dir)
            outcome = result if isinstance(result, str) else "ok"
        except StateUnreadable as e:
            # 状態 DB を読めない (P11): 何も書かず exit 0。カテゴリだけを 1 行 (パス・例外メッセージ・本文は書かない)。
            _log(state_dir, "状態DB 読めず category={}".format(e.category))
            outcome = "fail:StateUnreadable"
            error = str(e) if dlevel >= 2 else None
        except StateNeedsMigration:
            # 旧 JSON 版の state (移行しない。脇に退けてから起動): 何も作らず固定 1 行だけ、exit 0。通常は _main_impl 冒頭の
            # state_status で止まる。ここへ来るのは競合 (state_status の後・engine 構築の前に JSON ファイルが
            # 現れた) の場合だけ。
            _log(state_dir, "旧 JSON 版の state (移行しない。脇に退けてから起動)")
            outcome = "fail:StateNeedsMigration"
        except Exception as e:
            _log(state_dir, "{} 失敗: {}".format(sub, _exc_summary(e)))
            outcome = "fail:{}".format(type(e).__name__)
            error = str(e) if dlevel >= 2 else None
    finally:
        # プロセスが kill されない限り 1 hook = hook_end ちょうど 1 行 (level 0 では何もしない)。
        if dlevel:
            try:
                fields = {"sub": sub, "outcome": outcome, "dur_ms": _ms(started),
                          "config_src": "none" if cfg.source == config.SRC_NONE else cfg.source,
                          "config_issues": [i.category for i in cfg.issues]}
                if payload is not None:
                    fields.update(_debug_common(payload, sub))
                    fields["payload_keys"] = sorted(str(k) for k in payload)
                if dlevel >= 2:
                    if error is not None:
                        fields["error"] = error
                    if outcome.startswith("fail:stdin"):
                        fields["stdin_raw"] = raw
                debuglog.emit(state_dir, dlevel, "hook_end", **fields)
            except Exception:  # noqa: BLE001 -- デバッグログが hook を壊してはならない
                pass


if __name__ == "__main__":
    main()
