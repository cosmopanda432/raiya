"""ledger.py -- 台帳: 全往復の追記専用ログ (業の記録)

熏習の材料となる往復 (ユーザー発話 + アシスタント応答) を 1 行 = 1 往復の JSONL に
追記する。削除も上書きもしない (七文書抽出仕様 KUN-1 剎那滅 = 事象粒度・追記専用、
倶舎論の「業力は壊劫を超えて残る」= 削除しない)。統合 (engine.consolidate) は
`consolidated_upto` より後ろの未統合の窓だけを読み、性向を種子として提案する。

保存 (保存層 sqlite3 化 #38 スライス 3): 1 往復 = `state_dir/raiya.db` の `ledger` 表の 1 行
(`body` = {"id", "life", "session_id", "ts", "user_text", "grounding_text", "assistant_text"} の JSON 本文)、
meta = `meta` 表の `ledger.next_id` / `ledger.consolidated_upto`。`ledger` 表は DB のトリガで追記専用。
ストア自身はファイルを知らない (I/O は `storage.py` の backend が持つ。旧 JSON 版は退役済み)。

記録と根拠を分ける (設計書 §10.6 処方 5/6): `user_text` は**記録** = 貼り付け
(`<pasted_content id="X">…</pasted_content id="X">`) を含めて残す。`grounding_text` は
**根拠** = `user_text` から貼り付けを剥がした本文で、統合の三性ゲートと統合プロンプトは
こちらを user 発話として読む (`engine.consolidate`)。`<private>…</private>` は本人が
「記憶させない」と明示した塊なので、記録 (user_text/assistant_text) からも根拠からも除く
(`engine.record_turn` が `strip_private` → `strip_pasted` の順で作る)。`grounding_text` の
無い旧形式の行は `LedgerTurn.grounding_text = None` になり、従来どおり `user_text` を根拠に
する (空文字 "" は「全部が貼り付けだった」= 根拠なし、で None とは区別する。欄は在るが型が
違う壊れた行も "" = 根拠なしに倒す)。

切り詰めの印 (処方 8): 長い本文は `elide_middle` で先頭+末尾を残し、間に
`<elided N chars>` を挟む。この印は台帳 (hook の TRUNCATE_LIMIT) にも残るため、統合の
ゲートに渡す本文では `ELISION_RE` で拾って `GATE_CUT` に置き換える (engine.consolidate)。

fail-soft (行単位): JSON でない行・型が不正な行 (user_text/assistant_text が str でない等) は
読み飛ばし (件数を warning に。DB の行は書き直さない)。DB 単位の失敗 (meta 欠落など) は
`storage.StateUnreadable` で fail-closed (空の台帳に倒さない)。`ledger_rejects` 表 (移行時に読めなかった
JSONL の行) が空でなければ、それも warning に出る。

行の走査は遅延する: 区間の入口は meta だけを読み、`ledger` 表の全行を走査するのは行を読む動詞
(`unconsolidated` / `unconsolidated_bases` / `unconsolidated_with_bases` / `unconsolidated_with_stamps` / `count` / `tail` /
`pending_count`) だけ。台帳は削除されず伸び続けるため、台帳を読まない動詞 -- とりわけ同期 hook の
`engine.recall` (<1 秒の予算) -- が毎回全行を読む形にはしない。よって壊れた行の `warning` が
確定するのは最初の行走査の時点。
標準ライブラリのみ。
"""
import bisect
import copy
import json
import re
import time
from dataclasses import dataclass
from typing import Optional

from . import sansho


@dataclass(frozen=True)
class LedgerTurn(sansho.Turn):
    """台帳に記録済みの往復。`sansho.Turn` のサブクラスなので熏習 API (KUN-7 能熏四義:
    往復オブジェクト必須) の型検査をそのまま通り、`id` で台帳の行を指せる。"""

    id: int = 0
    life: int = 0
    # 貼り付けを剥がした根拠本文。None = 旧形式の行 (欄が無い) で user_text を根拠にする。
    # "" は「本文が全て貼り付けだった」= 根拠なし (None と区別する。`or` で潰さない)。
    grounding_text: Optional[str] = None
    # 往復が属する project (改修 5 §4.2: hook の cwd を git の最上位に正規化したもの)。
    # None = 欄が無い行・cwd が来なかった行・MCP/CLI の往復 (全域)。
    project: Optional[str] = None


# --- 記録/根拠の本文を作る純関数 (設計書 §10.6 処方 5/6/8。決定論・標準ライブラリのみ) ---
# どちらも**線形時間**で走査する (`<private>` の除去は同期 hook の recall (<1 秒) の経路に乗る)。
# 1 本の正規表現 `<open>.*?<close>` で書くと、閉じの無い開きタグが k 個あるとき各開きから
# 末尾まで読み直して O(k·n) になる (400KB・閉じ無し 4000 個で約 7 秒を実測)。
#
# 貼り付けの印 (Claude Code が hook の prompt に残す実測の形)。閉じタグにも開きと**同じ** id が
# 付く非標準形なので id で対にする (空の id `id=""` も受け付ける)。id は正規表現に埋め込まず
# 文字列として比べる (`a.*(` のような id でも安全)。id が食い違う・閉じが無い塊は無変更。
_PASTED_OPEN_RE = re.compile(r'<pasted_content id="([^"]*)">')
_PASTED_CLOSE_RE = re.compile(r'</pasted_content id="([^"]*)">')
# `<private>` は大文字小文字を区別せず、タグ内の空白を許す (`<Private>` / `<private >` /
# `< / private >`)。受け入れる限界 (tests で現状を pin): 入れ子は一番内側の閉じで終わる
# (`<private>a<private>b</private>c</private>` -> `c</private>` が残る)。貼り付けの塊を
# 跨ぐ `<private>` も単に最初の閉じまでを除く (塊の境界は見ない)。
_PRIVATE_OPEN_RE = re.compile(r"<\s*private\s*>", re.IGNORECASE)
_PRIVATE_CLOSE_RE = re.compile(r"<\s*/\s*private\s*>", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# 切り詰めの印。統合プロンプト (engine) と台帳 (hook) で同じ形を使う。
ELISION_FORMAT = "<elided {} chars>"
ELISION_RE = re.compile(r"<elided \d+ chars>")
# ゲートに渡す本文で切り詰めの印を置き換える区切り。U+FFFC を改行で挟む。この区切りは
# ゲート用の写しにだけ現れ、台帳にもプロンプトにも書かない (engine.imprint が plain Turn に
# 残す往復の写し = imprint_log / evidence の本文には現れうる)。`sansho._n` を通っても消えない
# 文字なので、U+FFFC を含まない span は切れ目を跨げない (印ごとの引用・先頭の末尾と末尾の
# 先頭を繋いだ引用は逐語一致しない)。ただし span か縁が U+FFFC を含めば区切りに一致しうる
# ため、そうした提案は engine.consolidate / engine.imprint が本文を空にした窓で判定し、丸ごと遍計所執に倒す
# (`GATE_CUT_CHAR`。本人の本文に U+FFFC が在っても同じ = 閉じる側)。
GATE_CUT_CHAR = "\ufffc"
GATE_CUT = "\n" + GATE_CUT_CHAR + "\n"


def _private_blocks(text):
    """除く区間 [(start, end)] を返す。閉じが見つからなければそこで打ち切る (それより後ろの
    開きにも閉じは無い) = 各位置を高々一定回しか読まない。"""
    blocks = []
    pos = 0
    while True:
        opened = _PRIVATE_OPEN_RE.search(text, pos)
        if opened is None:
            return blocks
        closed = _PRIVATE_CLOSE_RE.search(text, opened.end())
        if closed is None:
            return blocks
        blocks.append((opened.start(), closed.end()))
        pos = closed.end()


def _pasted_blocks(text):
    """除く区間 [(start, end)] を返す。閉じタグを 1 回の走査で id ごとに集め、各開きには
    その後ろにある同じ id の最初の閉じを二分探索で対にする (O(n + k log k))。"""
    closes = {}
    for m in _PASTED_CLOSE_RE.finditer(text):
        closes.setdefault(m.group(1), []).append(m)
    if not closes:
        return []
    starts = {key: [m.start() for m in ms] for key, ms in closes.items()}
    blocks = []
    pos = 0
    while True:
        opened = _PASTED_OPEN_RE.search(text, pos)
        if opened is None:
            return blocks
        key = opened.group(1)
        i = bisect.bisect_left(starts.get(key, []), opened.end())
        if key not in closes or i == len(closes[key]):
            pos = opened.end()  # 対になる閉じが無い開きは残す
            continue
        blocks.append((opened.start(), closes[key][i].end()))
        pos = closes[key][i].end()


def _remove(find_blocks, text):
    """find_blocks が返す区間を除く。1 つも無ければ本文を一切変えない (記録を無用に書き換えない)。
    除いた場合だけ、本文全体の 3 連以上の改行を空行 1 つへ畳み (継ぎ目に限らない)、前後の
    空白を落とす。ゲートは空白差を同一視するので、この整形は判定に影響しない。"""
    if not isinstance(text, str):
        return ""
    blocks = find_blocks(text)
    if not blocks:
        return text
    parts = []
    pos = 0
    for start, end in blocks:
        parts.append(text[pos:start])
        pos = end
    parts.append(text[pos:])
    return _BLANK_LINES_RE.sub("\n\n", "".join(parts)).strip()


def strip_private(text):
    """`<private>…</private>` を除く (処方 6: 記録からも根拠からも除く)。閉じの無い塊は残す。"""
    return _remove(_private_blocks, text)


def strip_pasted(text):
    """開き/閉じの id が一致する `<pasted_content id="X">…</pasted_content id="X">` を除く
    (処方 5: 根拠 = 本人が打った部分だけ)。食い違い・閉じ無しは無変更。"""
    return _remove(_pasted_blocks, text)


def elide_middle(text, limit):
    """limit 字を超える本文を先頭+末尾で残し、間に `<elided N chars>` を挟む (処方 8)。

    戻り値: (本文, 切り詰めたか)。N は省いた字数。先頭に limit の半分 (切り捨て)、末尾に残り。
    """
    text = text or ""
    if len(text) <= limit:
        return text, False
    head = limit // 2
    tail = limit - head
    elided = len(text) - limit
    return text[:head] + "\n" + ELISION_FORMAT.format(elided) + "\n" + text[len(text) - tail:], True


def gate_text(text):
    """ゲートに渡す本文: 切り詰めの印を GATE_CUT に置き換える (engine.consolidate が使う)。"""
    return ELISION_RE.sub(GATE_CUT, text or "")


def _tool_ref_of(item):
    """tool_refs の 1 要素を読み手の形へ (不正なら None)。知らない kind・必須欄の型違反・相対 path は捨てる。
    余計な欄は許す (読み手は寛容: 凍結 spec §3 の「足すのは非破壊」) が、返す dict には持ち込まない。"""
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    if kind == "file":
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            return None
        out = {"kind": "file", "path": path}
        start, end = item.get("line_start"), item.get("line_end")
        if start is not None:
            if not _is_int(start) or start < 1:
                return None
            out["line_start"] = start
        if end is not None:
            # 書き手 (raiya_hook._valid_tool_ref) と同じ: line_end ≥ line_start (line_start が無ければ ≥ 1)。
            if not _is_int(end) or end < (start if "line_start" in out else 1):
                return None
            out["line_end"] = end
        return out
    if kind == "command":
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return None
        return {"kind": "command", "name": name}
    return None


def read_tool_refs(row):
    """台帳の行の `tool_refs` を読み手の形で返す純関数 (公開 API 凍結 spec §3・compact spec §4.5)。
    キーが無い・list でない → None。`[]` → `[]`。不正な要素はその要素だけ捨てる。所在だけで本文は無い。"""
    refs = row.get("tool_refs") if isinstance(row, dict) else None
    if not isinstance(refs, list):
        return None
    return [ref for ref in (_tool_ref_of(item) for item in refs) if ref is not None]


def _default_meta():
    return {"next_id": 1, "consolidated_upto": 0}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_row(row):
    """台帳の 1 行 (parse 済み) を読み手の形へ補正する。壊れた行 (dict でない・id が整数でない・
    user_text/assistant_text が str でない) は None。life が整数でなければ 0 へ、grounding_text /
    project も下の規則で補正する (行自体は読み飛ばさない)。"""
    if (
        not isinstance(row, dict)
        or not _is_int(row.get("id"))
        or not isinstance(row.get("user_text"), str)
        or not isinstance(row.get("assistant_text"), str)
    ):
        return None
    if not _is_int(row.get("life")):
        row["life"] = 0
    # 根拠欄が無い (旧形式) なら None = user_text を根拠にする。欄は在るが型が
    # 違う (壊れた) なら "" = 根拠なし (閉じる側に倒す: 生の user_text へ戻すと
    # 貼り付けが根拠に混ざる)。行自体は読み飛ばさない (life と同じ補正の扱い)。
    if "grounding_text" not in row:
        row["grounding_text"] = None
    elif not isinstance(row["grounding_text"], str):
        row["grounding_text"] = ""
    # project 欄: 無い・非 str は None (全域)。行自体は読み飛ばさない。
    if not isinstance(row.get("project"), str):
        row["project"] = None
    return row


class LedgerStore:
    """1 インスタンス = 1 台帳 (in-memory の論理)。ファイルも DB も知らない。

    meta (`next_id` / `consolidated_upto`) と追記バッファを持ち、既存の行は backend が区間の間だけ注入する
    `_row_reader` (本文テキストの iterable を返す callable) 経由で読む。`append` はバッファに積むだけで、
    backend の flush (engine では `_locked` の COMMIT) まで書かない。standalone の呼び手は `commit()`。"""

    def __init__(self):
        self.warning = None
        # 行走査で確定した warning。最初の走査で決まり、以後は消さない。
        self._row_warning = None
        self._rows_scanned = False
        # `ledger_rejects` 表の件数由来の warning (区間の入口で backend が決める)。
        self._rejects_warning = None
        # commit 前の追記行 (id 昇順)。読み手は `_read_rows` 経由で見える。保存時に直列化する。
        self._pending_rows = []
        self.meta = _default_meta()
        # backend が bind する (storage.py)。None = どの DB にも結ばれていない (純粋な in-memory)。
        self._backend = None
        self._row_reader = None
        self._snap = None

    _normalize_row = staticmethod(_normalize_row)
    _default_meta = staticmethod(_default_meta)

    # --- 永続化 (実体は storage.py) ---
    def reload(self):
        """DB から meta を読み直す (standalone の呼び手用。engine は区間の入口で backend が読み込む)。
        追記バッファは捨てる (ロールバック)。"""
        if self._backend is not None:
            self._backend.reload_store(self)

    def commit(self):
        """バッファした追記と meta を DB へ書く (standalone の呼び手用)。短い自己完結の書く区間を 1 つ開いて
        COMMIT する (engine の動詞の中では呼ばない: 区間の flush が唯一の書き込み点)。"""
        if self._backend is None:
            raise RuntimeError("LedgerStore は DB に結ばれていない (storage.open_ledger を使う)")
        self._backend.commit_store(self)

    def _read_body_texts(self):
        if self._row_reader is None:
            return []
        return self._row_reader()

    # --- 追記 (KUN-1 剎那滅) ---
    def append(self, user_text, assistant_text, life, session_id=None, ts=None, grounding_text=None, project=None,
               tool_refs=None):
        """1 往復を追記する。`grounding_text` は根拠本文 (None = 欄を書かない = user_text が根拠)。
        `project` は往復の属する project (str のときだけ欄を書く。改修 5 §4.2)。
        `tool_refs` (任意・list のときだけ欄を書く): tool が触れた所在 (file / command 名) だけ。
        公開 API 凍結 spec §3。形の検査は呼び出し側 (raiya-hook) が済ませ、台帳は渡された list を残すだけ。
        剥がしの規則は呼び出し側 (engine.record_turn) が適用する: 台帳は渡された値を残すだけ。"""
        row = {
            "id": self.meta["next_id"],
            "life": life,
            "session_id": session_id,
            "ts": time.time() if ts is None else ts,
            # 型が違う値 (int/None 等) は空文字へ補正する (`x or ""` は falsy な値しか
            # 補正できず 123 のような truthy な非文字列を通してしまうため isinstance で
            # 見る。raiya/hooks/claude_code.py の
            # `prompt if isinstance(prompt, str) else ""` と同じ型)。
            "user_text": user_text if isinstance(user_text, str) else "",
            "assistant_text": assistant_text if isinstance(assistant_text, str) else "",
        }
        if isinstance(grounding_text, str):
            row["grounding_text"] = grounding_text
        if isinstance(project, str):
            row["project"] = project
        if isinstance(tool_refs, list):
            row["tool_refs"] = tool_refs
        # 行は commit まで書かない (バッファ)。保存時に 1 往復 = 1 行で追記する (storage の flush)。
        self._pending_rows.append(row)
        self.meta["next_id"] += 1
        return row["id"]

    # --- 読み出し ---
    def _read_rows(self):
        """全行を dict のリストで返す (id 昇順・同 id は挿入順)。台帳が伸びた分だけ時間がかかる。

        読めない本文 (JSON でない・id が整数でない・user_text/assistant_text が str でない) は
        読み飛ばし、件数を warning に記録する (DB の行は書き直さない)。warning が確定するのは
        **最初の走査**の時点で、以後の走査では書き換えない。life が整数でなければ行自体は
        読み飛ばさずに 0 へ補正する。
        """
        rows = []
        skipped = 0
        for raw in self._read_body_texts():
            try:
                row = json.loads(raw)
            except (ValueError, TypeError):
                skipped += 1
                continue
            row = self._normalize_row(row)
            if row is None:
                skipped += 1
                continue
            rows.append(row)
        if not self._rows_scanned:
            self._rows_scanned = True
            if skipped:
                self._row_warning = "台帳の読めない行 {} 件を読み飛ばしました".format(skipped)
                self.warning = "; ".join(w for w in (self.warning, self._row_warning) if w)
        # まだ commit していない追記 (`append` のバッファ) も、同じ補正を通して末尾に見せる。
        for pending in self._pending_rows:
            rows.append(self._normalize_row(copy.deepcopy(pending)))
        return rows

    def count(self):
        return len(self._read_rows())

    def consolidated_upto(self):
        return self.meta["consolidated_upto"]

    def _unconsolidated_rows(self, limit=None):
        """未統合の生の行 (id > consolidated_upto) を古い順に。行の走査は 1 回。"""
        upto = self.meta["consolidated_upto"]
        rows = [r for r in self._read_rows() if r["id"] > upto]
        if limit is not None:
            rows = rows[:limit]
        return rows

    @staticmethod
    def _turn_of(r):
        return LedgerTurn(
            user_text=r.get("user_text") or "",
            assistant_text=r.get("assistant_text") or "",
            id=r["id"],
            life=r.get("life") or 0,
            grounding_text=r.get("grounding_text"),
            project=r.get("project"),
        )

    @staticmethod
    def _basis_of(r):
        return (
            r.get("project") if isinstance(r.get("project"), str) else None,
            r.get("session_id") if isinstance(r.get("session_id"), str) else None,
        )

    def unconsolidated(self, limit=None):
        """未統合の往復 (id > consolidated_upto) を古い順に LedgerTurn で返す。"""
        return [self._turn_of(r) for r in self._unconsolidated_rows(limit)]

    def unconsolidated_bases(self, limit=None):
        """`unconsolidated(limit)` と同じ行・同じ順で、各行の所依 `(project, session_id)` を返す。

        七文書による検証層 設計書 §2.3 (同依): 同じ窓に並んでいても所依 (project × session) が
        別なら一つの因果として読まない。そのための観測窓 (audit の `basis_counts` /
        consolidate の `window_sessions`)。非 str はどちらも None。`LedgerTurn` は変えない
        (KUN-7 の型検査と frozen の等値性に触らない)。
        """
        return [self._basis_of(r) for r in self._unconsolidated_rows(limit)]

    def unconsolidated_with_stamps(self, limit=None):
        """`(往復, 所依 (project, session_id), ts)` を 1 回の行走査で返す (engine.consolidate の読みフェーズ用。
        台帳は伸び続けるので走査を重ねない)。`ts` は行の値そのまま (数でなければ None)。#105 R 用。"""
        rows = self._unconsolidated_rows(limit)
        stamps = [
            r.get("ts") if isinstance(r.get("ts"), (int, float)) and not isinstance(r.get("ts"), bool) else None
            for r in rows
        ]
        return [self._turn_of(r) for r in rows], [self._basis_of(r) for r in rows], stamps

    def unconsolidated_with_bases(self, limit=None):
        """`(unconsolidated(limit), unconsolidated_bases(limit))` を 1 回の行走査で返す
        (`unconsolidated_with_stamps` の ts を除いた薄い包み)。"""
        turns, bases, _ = self.unconsolidated_with_stamps(limit)
        return turns, bases

    def pending_count(self):
        return len(self.unconsolidated())

    def _last_row_id(self):
        """最後に払い出した id (空の台帳なら 0)。mark_consolidated の上限切り詰めに使う。

        `meta["next_id"]` は `append` が id を払い出すたびに +1 され、meta が壊れていた
        場合も `_load_meta` が台帳の行から再構成する (= 常に「最後に払い出した id + 1」)。
        よって全行の再走査は要らない。呼び出し元は `consolidate` の書込フェーズ
        (= N 往復ごとの `mark_consolidated`) だけだが、その 1 回の統合の中で既に
        `unconsolidated()` / `pending_count()` が jsonl を頭から走査している。ここで
        `_read_rows()` を呼ぶと同じ走査をもう一度重ねることになるので meta から取る。
        """
        return self.meta["next_id"] - 1

    def last_id(self):
        """最後に払い出した id (空の台帳なら 0)。meta から取る (行は読まない)。engine が公開で使う。"""
        return self._last_row_id()

    def mark_consolidated(self, upto_id):
        """統合済みカーソルを進める (単調増加のみ。巻き戻さない)。

        台帳の最終行を超える値は最終行の id に切り詰める (空の台帳なら 0)。切り詰めない
        と、まだ存在しない後続の追記が生まれた瞬間から統合済み扱いになってしまう。
        """
        if not _is_int(upto_id):
            return
        upto_id = min(upto_id, self._last_row_id())
        if upto_id > self.meta["consolidated_upto"]:
            self.meta["consolidated_upto"] = upto_id

    def rows_by_ids(self, ids):
        """指定 id の生の行を `{id: row}` で返す (deep copy・1 回の行走査)。無い id は含まない。
        観測 (`engine.seed_provenance`) が種子の根拠の行を引くためのもの。"""
        wanted = {i for i in ids if _is_int(i)}
        if not wanted:
            return {}
        return {r["id"]: copy.deepcopy(r) for r in self._read_rows() if r["id"] in wanted}

    def turns_by_ids(self, ids):
        """指定 id の往復を `LedgerTurn` で id 昇順に返す (1 回の行走査)。無い id は含まれない
        (呼び手が長さを比べて欠けを知る)。`raiya-correct replace` が置き換えの窓を引くためのもの。"""
        wanted = {i for i in ids if _is_int(i)}
        if not wanted:
            return []
        rows = [r for r in self._read_rows() if r["id"] in wanted]
        return [self._turn_of(r) for r in sorted(rows, key=lambda r: r["id"])]

    def tail(self, n):
        """直近 n 件を dict のリストで返す (LedgerTurn ではなく生の行)。

        n が正の整数でなければ空リスト (n<=0 や非整数だと `rows[-n:]` が意図と逆に
        先頭側にずれる・全件を返す等の事故になるため)。
        """
        if not _is_int(n) or n <= 0:
            return []
        rows = self._read_rows()
        return copy.deepcopy(rows[-n:])
