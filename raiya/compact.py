"""compact.py -- 会話窓の圧縮の純関数 (Issue #63)。spec: 2026-10-27-compact-design.md

窓の往復のうち、どれを逐語で残し、どれを「(他に N 往復・台帳 id a–b)」の 1 行に畳むかを決める。
要約は作らない (B 型の否定)。円 (① 発芽の根拠 → ② 再開点の根拠 → ③ 直近) の候補は呼び手 (engine.compact) が
組んで渡し、ここは「詰める・描く」だけを持つ。`manas.compose_injection` は流用せず、その規則
(貪欲に詰める・円の中で最初に入らない項目でその円を切る・外側の円は丸ごと落とす・落とした数を cut に残す)
を同じ語 (`fits` / `stopped` / `cut`) で写す。

純粋の葉: 標準ライブラリと `raiya.ledger` の `strip_pasted` / `read_tool_refs` だけを import する
(I/O・engine・backend・hooks は import しない。AST pin: tests/test_compact.py)。状態を持たず何も書かない。
"""
from .ledger import read_tool_refs, strip_pasted

# 見出し。文面は内容扱いで凍結しない。
COMPACT_HEADER = "以下は会話窓の圧縮 (略) です。往復は台帳の逐語で、要約ではありません。省いた往復は台帳 id で引けます。"

# 降格 / 貼り付けのみの往復 (根拠欄が "") は本文を描かない。user_text を描くと貼り付けの中身が LLM の窓に戻る。
NO_GROUNDING_MARK = "(本文を根拠にしない往復: 貼り付けのみ / 降格)"

CIRCLES = ("germinated", "resume", "recent")  # 内 → 外

# 落ちた行の理由コード (文面は engine の COMPACT_REASON_* が持つ)。
R_NO_CIRCLE = "no_circle"
R_BUDGET = "budget"
R_OUTER_CUT = "outer_cut"
R_UNKNOWN = "unknown"


def elided_line(first, last, n):
    """省略の行。n は窓の中の件数で `last - first + 1` ではない (他セッションの行のぶん台帳 id に隙間がある)。"""
    if n == 1:
        return "(他に 1 往復・台帳 id {})".format(first)
    return "(他に {} 往復・台帳 id {}–{})".format(n, first, last)


def _user_side(row):
    """user 側の描き方 (凍結した根拠欄の意味。`grounding_text or user_text` とは書かない)。"""
    grounding = row.get("grounding_text")
    if isinstance(grounding, str):
        body = grounding
    else:
        body = strip_pasted(row.get("user_text") or "")  # 古い行 (None): 残る貼り付けを窓に戻さない
    return body if body.strip() else NO_GROUNDING_MARK


def _tool_line(row):
    parts = []
    for ref in read_tool_refs(row) or []:
        if ref["kind"] == "file":
            text = ref["path"]
            if "line_start" in ref:  # line_end だけの ref は path だけを描く
                text += ":{}".format(ref["line_start"])
                if "line_end" in ref:
                    text += "-{}".format(ref["line_end"])
            parts.append(text)
        else:
            parts.append(ref["name"])
    return ", ".join(parts)


def render_turn(row):
    """1 往復 = 1 塊。assistant は台帳のまま (`<elided N chars>` もそのまま・GATE_CUT への置換はしない)。"""
    i = row["id"]
    lines = [
        "[台帳 #{} ユーザー] {}".format(i, _user_side(row)),
        "[台帳 #{} アシスタント] {}".format(i, row.get("assistant_text") or ""),
    ]
    tools = _tool_line(row)
    if tools:
        lines.append("[台帳 #{} ツール] {}".format(i, tools))
    return "\n".join(lines)


def _render(known, keep, rendered):
    """(narrative, elided) を描く。known は既知 id の昇順。keep に挟まれずに続く省略の行を 1 本にする。"""
    blocks = [COMPACT_HEADER]
    elided = []
    run = []

    def _flush():
        if run:
            line = elided_line(run[0], run[-1], len(run))
            elided.append({"from": run[0], "to": run[-1], "n": len(run), "line": line})
            blocks.append(line)
            del run[:]

    for i in known:
        if i in keep:
            _flush()
            blocks.append(rendered[i])
        else:
            run.append(i)
    _flush()
    return "\n\n".join(blocks), elided


def plan_compaction(rows_by_id, window_ids, circles, budget_chars):
    """残す往復を決めて narrative を描く。

    rows_by_id: {id: 台帳の行}。window_ids: 窓の全 id (昇順・重複なし)。rows_by_id に無い id は unknown。
    circles: [(円の名前, [id, ...]), ...] を内 → 外の順に。各円の id は**詰める順**。窓の既知 id 以外は無視する。
    ある id は最も内側の円に属する (外の円で重ねて数えない)。budget_chars: None = 上限なし、int = narrative の len の上限。
    戻り値: {"keep", "elided", "narrative", "turn_decisions", "cut"}。窓が空なら narrative は ""。
    予算が床 (見出し + 全部を省略した形) より小さいときは床を返す (keep = []・例外なし)。
    """
    cut = {name: 0 for name in CIRCLES}
    if not window_ids:
        return {"keep": [], "elided": [], "narrative": "", "turn_decisions": [], "cut": cut}
    known = [i for i in window_ids if i in rows_by_id]
    known_set = set(known)
    if not known:  # 窓が全部 unknown: 圧縮するものが無いので見出しだけの narrative も出さない
        unknown = [{"id": i, "decision": "unknown", "circle": None, "reason": R_UNKNOWN, "chars": 0} for i in window_ids]
        return {"keep": [], "elided": [], "narrative": "", "turn_decisions": unknown, "cut": cut}
    rendered = {i: render_turn(rows_by_id[i]) for i in known}

    owner = {}
    candidates = []
    for name, ids in circles:
        mine = []
        for i in ids:
            if i in known_set and i not in owner:
                owner[i] = name
                mine.append(i)
        candidates.append((name, mine))

    def fits(keep):
        if budget_chars is None:
            return True
        return len(_render(known, keep, rendered)[0]) <= budget_chars

    keep = set()
    reasons = {}
    stopped = False
    for name, mine in candidates:
        if stopped:
            cut[name] = len(mine)
            for i in mine:
                reasons[i] = R_OUTER_CUT
            continue
        for pos, i in enumerate(mine):
            if fits(keep | {i}):
                keep.add(i)
                continue
            rest = mine[pos:]  # 最初に入らなかった候補でこの円を切る
            cut[name] = len(rest)
            for j in rest:
                reasons[j] = R_BUDGET
            stopped = True
            break

    narrative, elided = _render(known, keep, rendered)
    decisions = []
    for i in window_ids:
        if i not in known_set:
            decisions.append({"id": i, "decision": "unknown", "circle": None, "reason": R_UNKNOWN, "chars": 0})
        elif i in keep:
            decisions.append({"id": i, "decision": "keep", "circle": owner[i], "reason": None, "chars": len(rendered[i])})
        else:
            decisions.append({
                "id": i, "decision": "elided", "circle": owner.get(i),
                "reason": reasons.get(i, R_NO_CIRCLE), "chars": len(rendered[i]),
            })
    return {"keep": sorted(keep), "elided": elided, "narrative": narrative, "turn_decisions": decisions, "cut": cut}
