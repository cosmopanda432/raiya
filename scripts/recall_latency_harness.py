"""recall_latency_harness.py -- recall 同期経路の読み遅延の計測器 (休眠と蓄積 設計書 §3.1)

LLM 不使用・決定論の入力 (乱数の種固定)。N 件の合成種子を持つ raiya.db (sqlite3) を自作の tmp に作り、
recall を (a) 構築+1 回 (b) 構築済み engine で 1 回 (c) hook 子プロセスの壁時計 で計る。
所在の content に path を書いた版 (`path`: 偶数 id の実体だけ作る = 半分が休眠) と、path を
書かない版 (`nopath`: stat 0 回の基準線) を同じ N・同じ乱数で作って並べる。

`receipt_harness.main` からは呼ばない。ファイル名が test*.py でないので `unittest discover`
にも拾われない。結果 (`receipts/recall_latency.json`) は環境依存のため
`tests/test_receipt_reproduces.py` の比較に入れない (smoke*.json と同格)。採り直すのは
recall の経路を変えたときだけで、人が commit する。

層化 (計画 §5): 各行に group_stats (群の数・最大の大きさ・seed_groups 単独の中央値 ms)・narrative_len・exceeds_group_ms。
N=10k で group_ms > 100 ms なら キャッシュより先に seed_groups を直す (group_first_n_exceeding_100ms)。

使い方: python3 scripts/recall_latency_harness.py [--out DIR] [--sizes 100,1000,10000] [--repeats 7] [--no-hook]

`--logs` (Issue #36/#37 の第一段): 種子数を固定 (既定 N=1000・`nopath`) し、imprint_log 表の行数と
manas_log 表の注入ログ行数を振って recall の費用を測る。軸ごとに分け (片方 0) 、現実的な最悪の組も測り、
読み (load) / 差分の書き込み (flush) / COMMIT (fsync を含む) / 残り の内訳を、保存層 (storage.SqliteBackend) が区間ごとに
数える `timing_totals` から取る (sqlite3 化 #38 スライス 3。raiya/ のパッチはもう要らない)。
行はすべて合成 (本番 state は読まない)。結果は `receipts/recall_latency_logs.json` (これも比較しない)。
  python3 scripts/recall_latency_harness.py --logs [--n 1000] [--imprint-sizes 0,1000,10000,50000]
      [--manas-sizes 0,416,1000] [--combos 10000:1000,50000:1000] [--repeats 7] [--no-hook] [--out DIR]
本番の state (~/.raiya) は読まない。state はすべて tempfile.mkdtemp() の下。
"""
import argparse
import copy
import datetime
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest import mock

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)  # リポジトリのルート (scripts/ の親)
for _p in (SCRIPTS_DIR, BASE_DIR):  # 兄弟スクリプトと raiya パッケージ (ルート) を import 可能にする
    if _p not in sys.path:
        sys.path.insert(0, _p)


SEEDS_FIXTURE = os.path.join(BASE_DIR, "tests", "fixtures", "recall_seeds_20261017.json")
DEFAULT_OUT = os.path.join(BASE_DIR, "receipts")
OUT_NAME = "recall_latency.json"
LOGS_OUT_NAME = "recall_latency_logs.json"

RNG_SEED = 20261024
LIFE = 52
HOOK_REPEATS = 5
DEFAULT_SIZES = (100, 1000, 10000)
DEFAULT_REPEATS = 7
KIND_MIX = (("決定", 0.40), ("所在", 0.25), ("性向", 0.20), ("回避策", 0.10), ("再開点", 0.05))
PROJECT_MIX = (("P0", 0.30), ("P1", 0.30), ("P2", 0.30), (None, 0.10))
SOFT_MS = 1000
HARD_MS = 10000
GROUP_MS_LIMIT = 100  # 層化 計画 §5 二次の規則: N=10k で seed_groups 単独が これを超えたら キャッシュより先に関数を直す
BUDGET_SOURCE = {
    "soft": "README.md:218/:314 「<1秒目安」、raiya/engine.py 「<1 秒の予算」",
    "hard": "raiya/hooks/settings.example.json の hook timeout 10 s、docs/設計書.md「同期・timeout 10s」",
}
VARIANTS = ("path", "nopath")
NOPATH_CONTENT = "資料は共有の場所にある。"

# --logs: 本番 (2026-10-24) の行あたりの大きさに合わせる。imprint_log 約 1.1 KB/行、manas 注入ログ約 3.1 KB/行。
LOGS_DEFAULT_N = 1000
LOGS_DEFAULT_IMPRINT = (0, 1000, 10000, 50000)
LOGS_DEFAULT_MANAS = (0, 416, 1000)
LOGS_DEFAULT_COMBOS = ((10000, 1000), (50000, 1000))
LOGS_DEFAULT_QUIET = ((0, 0), (10000, 0), (10000, 1000))  # 発芽しない入力で測る組 (保存が 1 回になる側)
QUIET_PROMPT = "zzqx qwv"  # どの種子の縁にも当たらない入力 (種子の縁は日本語)
IMPRINT_ROW_BYTES = 1100
MANAS_ROW_BYTES = 3100
ROW_JITTER = 0.3  # 行の大きさを目標の ±30% で散らす


def _isolate_env():
    """設定ファイル・環境変数を計測に混ぜない (receipt_harness.py と同じ作法)。import 時でなく実行時に行う。"""
    os.environ["RAIYA_CONFIG"] = ""
    for name in ("RAIYA_INJECT_DISPOSITIONS", "RAIYA_HOOK_ACTIVE", "RAIYA_HOOKS"):
        os.environ.pop(name, None)


def _pick(rng, mix):
    x = rng.random()
    acc = 0.0
    for value, weight in mix:
        acc += weight
        if x < acc:
            return value
    return mix[-1][0]


def _plan(n):
    """(kind, project 名) を N 件。両 variant で同じ乱数列を使う。"""
    rng = random.Random(RNG_SEED)
    return [(_pick(rng, KIND_MIX), _pick(rng, PROJECT_MIX)) for _ in range(n)]


def _template_seeds():
    with open(SEEDS_FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)["seeds"]


def _build_seeds(n, variant, plan, projects, loc_dir):
    template = _template_seeds()
    seeds = []
    for i in range(n):
        kind, pname = plan[i]
        seed = copy.deepcopy(template[i % len(template)])
        seed["id"] = i + 1
        seed["kind"] = kind
        seed["project"] = projects[pname] if pname is not None else None
        seed["superseded_by"] = None
        if kind == "所在":
            if variant == "path":
                seed["content"] = "資料は {} にある。".format(os.path.join(loc_dir, "{}.md".format(i + 1)))
            else:
                seed["content"] = NOPATH_CONTENT
        seeds.append(seed)
    return seeds


def _make_inputs(seeds, p0):
    """P0 (と全域) の種子から 20 件の縁を「、」で連ねた prompt と、5 件の context。"""
    rng = random.Random(RNG_SEED + 1)
    pool = [s for s in seeds if s["project"] in (p0, None) and s.get("conditions")]
    if not pool:
        pool = [s for s in seeds if s.get("conditions")]
    picked = [rng.choice(pool) for _ in range(20)]
    prompt = "、".join(rng.choice(s["conditions"]) for s in picked)
    context = ["、".join(rng.choice(s["conditions"]) for s in [rng.choice(pool)]) for _ in range(5)]
    return prompt, context


def _median_max(values):
    if not values:
        return None, None
    return round(statistics.median(values), 3), round(max(values), 3)


class _Case:
    """1 つの (N, variant) 用の state dir と、毎回の前に state を戻す仕掛け。

    合成直後の `raiya.db` を root 配下に 1 本コピーしておき (`pristine`)、reset はそれを書き戻すだけ
    (計時の外。DB を足し引きして戻すより確実で、種子数に依らず形が同じ)。
    """

    def __init__(self, root, n, variant, plan, projects, p0, loc_dir, extra_snapshot=None):
        self.state_dir = os.path.join(root, "state_{}_{}".format(n, variant))
        os.makedirs(os.path.join(self.state_dir, "sessions"))
        self.loc_dir = loc_dir
        self.p0 = p0
        seeds = _build_seeds(n, variant, plan, projects, loc_dir)
        from raiya.snapshot import write_state

        snapshot = {"seeds": seeds, "alaya_meta": {"life": LIFE, "next_id": n + 1}}
        snapshot.update(extra_snapshot or {})
        write_state(self.state_dir, snapshot)
        self.db_path = os.path.join(self.state_dir, "raiya.db")
        self.pristine = os.path.join(root, "pristine_{}_{}_{}.db".format(n, variant, id(self)))
        shutil.copyfile(self.db_path, self.pristine)
        self.db_bytes = os.path.getsize(self.db_path)
        self.seeds = seeds
        self.prompt, self.context = _make_inputs(seeds, p0)
        self.session_path = os.path.join(self.state_dir, "sessions", "bench.json")

    def reset(self):
        """計時の外で raiya.db(.bak) / セッションキャッシュを合成直後に戻す。"""
        for name in ("raiya.db", "raiya.db.bak", "raiya.db-journal"):
            path = os.path.join(self.state_dir, name)
            if os.path.exists(path):
                os.remove(path)
        shutil.copyfile(self.pristine, self.db_path)
        with open(self.session_path, "w", encoding="utf-8") as f:
            json.dump({"prompt": "前の prompt", "ts": time.time(), "recent": []}, f, ensure_ascii=False)

    def recall(self, engine):
        return engine.recall(self.prompt, context=self.context, project=self.p0, session_start=True)


def _count_stats(case):
    """recall 1 回の間に loc_dir 配下へ出た os.stat の回数 (計測器の中だけで数える)。"""
    real = os.stat
    prefix = case.loc_dir
    counter = {"n": 0}

    def counting(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            if os.fsdecode(path).startswith(prefix):
                counter["n"] += 1
        return real(path, *args, **kwargs)

    case.reset()
    engine = MemoryEngine(state_dir=case.state_dir, new_life=False)
    case.reset()
    with mock.patch("os.stat", side_effect=counting):
        case.recall(engine)
    return counter["n"]


def _hook_env(case):
    env = dict(os.environ)
    for name in ("RAIYA_INJECT_DISPOSITIONS", "RAIYA_HOOK_ACTIVE", "RAIYA_HOOKS"):
        env.pop(name, None)
    env["RAIYA_CONFIG"] = ""
    env["RAIYA_STATE_DIR"] = case.state_dir
    return env


def _time_hook(case):
    payload = json.dumps(
        {"session_id": "bench", "prompt": case.prompt, "cwd": case.p0, "hook_event_name": "UserPromptSubmit"},
        ensure_ascii=False,
    )
    case.reset()
    t0 = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "raiya.hooks.claude_code", "recall"],
        input=payload,
        text=True,
        env=_hook_env(case),
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    return (time.perf_counter() - t0) * 1000.0


def _group_stats(case, repeats):
    """seed_groups を合成の種子全件に単独で回す (層化 計画 §5)。groups / max_size / group_ms (中央値)。"""
    from raiya.alaya import seed_groups
    groups = seed_groups(case.seeds)  # 空回し
    times = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        seed_groups(case.seeds)
        times.append((time.perf_counter() - t0) * 1000.0)
    return {
        "groups": len(groups),
        "max_size": max((g["size"] for g in groups), default=0),
        "group_ms": round(statistics.median(times), 3),
    }


def _narrative_len(case):
    """合成直後の state での recall 1 回が返した注入文の字数。"""
    case.reset()
    engine = MemoryEngine(state_dir=case.state_dir, new_life=False)
    return len(case.recall(engine)["narrative"])


def measure_case(case, repeats, with_hook):
    # 1 回空回し (import・ファイルキャッシュを温める)。
    case.reset()
    case.recall(MemoryEngine(state_dir=case.state_dir, new_life=False))

    construct_ms = []
    recall_ms = []
    for _ in range(repeats):
        case.reset()
        t0 = time.perf_counter()
        engine = MemoryEngine(state_dir=case.state_dir, new_life=False)
        case.recall(engine)
        construct_ms.append((time.perf_counter() - t0) * 1000.0)
    for _ in range(repeats):
        case.reset()
        engine = MemoryEngine(state_dir=case.state_dir, new_life=False)
        case.reset()
        t0 = time.perf_counter()
        case.recall(engine)
        recall_ms.append((time.perf_counter() - t0) * 1000.0)
    stat_calls = _count_stats(case)
    narrative_len = _narrative_len(case)
    group_stats = _group_stats(case, repeats)
    hook = []
    if with_hook:
        _time_hook(case)  # 空回し
        hook = [_time_hook(case) for _ in range(HOOK_REPEATS)]
    case.reset()
    c_med, c_max = _median_max(construct_ms)
    r_med, r_max = _median_max(recall_ms)
    h_med, h_max = _median_max(hook)
    return {
        "construct_recall_ms": {"median": c_med, "max": c_max},
        "recall_ms": {"median": r_med, "max": r_max},
        "stat_calls": stat_calls,
        "narrative_len": narrative_len,
        "group_stats": group_stats,
        "hook_ms": {"median": h_med, "max": h_max} if with_hook else None,
        "db_bytes": case.db_bytes,
        "exceeds_soft": (h_med > SOFT_MS) if with_hook else None,
        "exceeds_hard": (h_max > HARD_MS) if with_hook else None,
        "exceeds_group_ms": group_stats["group_ms"] > GROUP_MS_LIMIT,
    }


# ---------------------------------------------------------------- --logs (Issue #36 / #37)

_WORDS = ("決定", "回避策", "所在", "資料", "設計書", "共有", "場所", "確認", "却下", "採用", "実装", "計測",
          "受領書", "種子", "台帳", "注入", "休眠", "双子", "接地", "引用", "条件", "縁", "発芽", "索引")


def _sentence(rng, chars):
    """合成の日本語文を chars 文字ぶん。実在の文面は使わない。"""
    out = []
    n = 0
    while n < chars:
        w = rng.choice(_WORDS)
        out.append(w)
        n += len(w)
    return "".join(out)[:chars]


def _file_json_bytes(data):
    """`_save_json` / `ManasStore.save` と同じ形 (ensure_ascii=False, indent=2) で直列化したバイト列。"""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _fit_row(make_row, wrap, target):
    """pad (日本語の文字数) を調整し、直列化した行 1 件が target バイト前後になる行を返す。

    make_row(pad) は pad 以外が固定の純関数。日本語は UTF-8 で 3 バイト/字なので、pad=0 で 1 回測って
    差を 3 で割れば ±数バイトに収まる。wrap(row) はその行 1 件だけを入れた state (None なら空の state)。
    """
    row_cost = len(_file_json_bytes(wrap(make_row(0)))) - len(_file_json_bytes(wrap(None)))
    return make_row(max(0, (target - row_cost) // 3))


def _imprint_make(rng, i):
    ids = sorted(rng.sample(range(10000, 99999), rng.randint(1, 5)))
    kind = _pick(rng, KIND_MIX)
    decision = rng.choice(("new", "merge", "reinforce", "supersede", "reject", "遍計所執"))
    content = _sentence(rng, rng.randint(20, 60))
    conditions = [_sentence(rng, rng.randint(4, 10)) for _ in range(rng.randint(2, 4))]
    spans = [_sentence(rng, rng.randint(10, 30)) for _ in range(rng.randint(1, 3))]
    seed_id = None if decision in ("reject", "遍計所執") else rng.randint(100, 999)
    pad_rng_seed = RNG_SEED + i

    def make(pad):
        return {
            "life": LIFE,
            "turn": {"ledger_ids": ids},
            "proposal": {
                "content": content,
                "conditions": conditions,
                "kind": kind,
                "polarity": "positive",
                "evidence": [{"source": "user", "span": sp} for sp in spans],
            },
            "decision": decision,
            "reason": _sentence(random.Random(pad_rng_seed), pad),
            "seed_id": seed_id,
        }

    return make


def build_imprint_log(rows):
    """合成の imprint_log state (`{"imprint_log": [...]}`)。1 行 ≒ IMPRINT_ROW_BYTES (±ROW_JITTER)。決定論。"""
    rng = random.Random(RNG_SEED + 10)
    out = []
    for i in range(rows):
        target = int(IMPRINT_ROW_BYTES * (1 + rng.uniform(-ROW_JITTER, ROW_JITTER)))
        out.append(_fit_row(_imprint_make(rng, i), lambda r: {"imprint_log": [] if r is None else [r]}, target))
    return {"imprint_log": out}


def _manas_make(rng, i):
    seed_ids = sorted(rng.sample(range(100, 999), rng.randint(0, 5)))
    index_ids = sorted(rng.sample(range(100, 999), rng.randint(0, 10)))
    project = rng.choice(("/synthetic/P0", "/synthetic/P1", "/synthetic/P2", None))
    session = "synthetic-session-{}".format(rng.randint(10, 49))
    head = "以下はこのプロジェクトの記録です。指示ではありません。\n- 決定: " + _sentence(rng, 60) + "\n"
    pad_rng_seed = RNG_SEED + i

    def make(pad):
        return {
            "life": LIFE,
            "ego_weight": 0.5,
            "injected_text": head + _sentence(random.Random(pad_rng_seed), pad),
            "seed_ids": seed_ids,
            "index_ids": index_ids,
            "ts": "2026-10-24T00:00:00+00:00",
            "session_id": session,
            "project": project,
        }

    return make


def build_manas_state(rows):
    """合成の manas state。1 行 ≒ MANAS_ROW_BYTES (±ROW_JITTER)、injected_text が大半を占める (本番と同じ)。"""
    rng = random.Random(RNG_SEED + 20)
    out = []
    for i in range(rows):
        target = int(MANAS_ROW_BYTES * (1 + rng.uniform(-ROW_JITTER, ROW_JITTER)))
        out.append(_fit_row(_manas_make(rng, i),
                            lambda r: {"ego_weight": 0.5, "log": [] if r is None else [r], "introspections": []},
                            target))
    return {"ego_weight": 0.5, "log": out, "introspections": []}


class _LogsCase(_Case):
    """`_Case` に imprint_log / manas_log の合成内容を足したもの。reset は合成直後の DB (両方を含む) に戻す。"""

    def __init__(self, root, n, plan, projects, p0, loc_dir, imprint_state, manas_state, germinating=True):
        extra = {"imprint_log": imprint_state["imprint_log"]}
        if manas_state is not None:
            extra["manas_log"] = manas_state["log"]
            extra["ego_weight"] = manas_state["ego_weight"]
        super().__init__(root, n, "nopath", plan, projects, p0, loc_dir, extra_snapshot=extra)
        self.germinating = germinating
        if not germinating:
            # 縁に当たらない入力: germinate が何も発火させない側を測る。
            self.prompt, self.context = QUIET_PROMPT, []


class _Probe:
    """recall 1 回の間だけ、保存層が数える区間の内訳 (load / flush / commit) を取る。

    パッチは要らない: `SqliteBackend.timing_totals` (累計) を recall の前後で引く。engine は構築済みのものを渡す。
    """

    KEYS = ("load", "flush", "commit")

    def __init__(self, engine):
        self.backend = engine._backend
        self.ms = {k: 0.0 for k in self.KEYS}
        self.sections = 0

    def __enter__(self):
        self._before = dict(self.backend.timing_totals)
        return self

    def __exit__(self, *exc):
        after = self.backend.timing_totals
        for k in self.KEYS:
            self.ms[k] = after[k + "_ms"] - self._before[k + "_ms"]
        self.sections = after["sections"] - self._before["sections"]
        return False


def _breakdown(case, repeats):
    """recall(構築済み engine) を repeats 回、計器つきで回し、各段の中央値 (ms) と区間数を返す。"""
    samples = []
    for _ in range(repeats):
        case.reset()
        engine = MemoryEngine(state_dir=case.state_dir, new_life=False)
        with _Probe(engine) as probe:
            t0 = time.perf_counter()
            case.recall(engine)
            total = (time.perf_counter() - t0) * 1000.0
        # 残り = その回の recall 全体 - (読み + 書き + COMMIT)。回ごとに引いてから中央値を取る (中央値の差ではない)。
        rest = total - sum(probe.ms.values())
        samples.append((total, dict(probe.ms), probe.sections, rest))
    out = {}
    for key in _Probe.KEYS:
        out[key + "_ms"] = round(statistics.median(s[1][key] for s in samples), 3)
    out["sections"] = samples[0][2]
    out["instrumented_recall_ms"] = round(statistics.median(s[0] for s in samples), 3)
    out["rest_ms"] = round(statistics.median(s[3] for s in samples), 3)
    return out


def _table_body_bytes(case, table):
    """表の body 列の UTF-8 バイト数の合計 (読むだけ。計時の外)。"""
    import sqlite3

    from raiya import storage

    conn = sqlite3.connect(storage._ro_uri(case.db_path), uri=True)
    try:
        return conn.execute("SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))), 0) FROM {}".format(table)).fetchone()[0]
    finally:
        conn.close()


def measure_logs_case(case, repeats, with_hook):
    row = measure_case(case, repeats, with_hook)
    row.pop("exceeds_soft", None)
    row.pop("exceeds_hard", None)
    row["exceeds_soft"] = (row["hook_ms"]["median"] > SOFT_MS) if with_hook else (row["recall_ms"]["median"] > SOFT_MS)
    row["breakdown"] = _breakdown(case, repeats)
    case.reset()
    # reset 直後 (= recall 前) の合成 DB の大きさ。
    row["db_bytes"] = os.path.getsize(case.db_path)
    row["imprint_log_body_bytes"] = _table_body_bytes(case, "imprint_log")
    row["manas_log_body_bytes"] = _table_body_bytes(case, "manas_log")
    return row


def _logs_grid(imprint_sizes, manas_sizes, combos):
    """(imprint 行数, manas 行数) の並び。軸ごと (片方 0) → 組。重複は除く。"""
    grid = [(0, 0)]
    grid += [(i, 0) for i in imprint_sizes if i != 0]
    grid += [(0, m) for m in manas_sizes if m != 0]
    grid += [c for c in combos]
    seen = []
    for c in grid:
        if c not in seen:
            seen.append(c)
    return seen


def run_logs(n, imprint_sizes, manas_sizes, combos, repeats, with_hook, quiet_combos=LOGS_DEFAULT_QUIET):
    with mock.patch.dict(os.environ):
        _isolate_env()
        return _run_logs(n, imprint_sizes, manas_sizes, combos, repeats, with_hook, quiet_combos)


def _run_logs(n, imprint_sizes, manas_sizes, combos, repeats, with_hook, quiet_combos):
    global MemoryEngine
    from raiya.engine import MemoryEngine  # noqa: E402

    root = tempfile.mkdtemp(prefix="recall_latency_logs_")
    try:
        projects = {}
        for name in ("P0", "P1", "P2"):
            d = os.path.join(root, "proj_" + name)
            os.makedirs(d)
            projects[name] = os.path.realpath(d)
        loc_dir = os.path.join(root, "loc")
        os.makedirs(loc_dir)
        plan = _plan(n)
        grid = [(c, True) for c in _logs_grid(imprint_sizes, manas_sizes, combos)]
        grid += [(c, False) for c in quiet_combos]
        last_imprint = (None, None)  # 直近 1 つだけ持つ (50,000 行を複数抱えない)
        rows = []
        for (i_rows, m_rows), germinating in grid:
            if last_imprint[0] != i_rows:
                last_imprint = (i_rows, build_imprint_log(i_rows))
            manas_state = build_manas_state(m_rows) if m_rows else None
            case = _LogsCase(root, n, plan, projects, projects["P0"], loc_dir,
                             last_imprint[1], manas_state, germinating)
            row = {"n": n, "variant": "nopath", "imprint_log_rows": i_rows, "manas_log_rows": m_rows,
                   "germinating": germinating}
            row.update(measure_logs_case(case, repeats, with_hook))
            rows.append(row)
            shutil.rmtree(case.state_dir, ignore_errors=True)  # 自作 root 配下の自作 dir だけ
        return {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "environment": {
                "python": platform.python_version(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "cpu_count": os.cpu_count(),
            },
            "params": {
                "n": n,
                "variant": "nopath",
                "imprint_log_rows": list(imprint_sizes),
                "manas_log_rows": list(manas_sizes),
                "combos": [list(c) for c in combos],
                "quiet_combos": [list(c) for c in quiet_combos],
                "repeats": repeats,
                "hook_repeats": HOOK_REPEATS if with_hook else 0,
                "rng_seed": RNG_SEED,
                "life": LIFE,
                "imprint_row_bytes_target": IMPRINT_ROW_BYTES,
                "manas_row_bytes_target": MANAS_ROW_BYTES,
                "row_jitter": ROW_JITTER,
                "with_hook": with_hook,
                "breakdown_note": "load_ms = 区間の入口の読み (全ストア)、flush_ms = 差分の書き込み (変わった行だけ UPDATE / 追記行だけ INSERT)、"
                                  "commit_ms = COMMIT (synchronous=FULL の fsync を含む)。rest_ms = 回ごとの (計器つき recall - (load + flush + commit)) の中央値。"
                                  "recall は書く区間を 1 つだけ持つ (sections=1)。manas_log の追記 1 行と、発芽した種子の観測欄の更新だけが書かれる。"
                                  "recall は imprint_log を読まず書かない。"
                                  "germinating=true の行は入力を種子の縁から作るので必ず発芽し、発芽した種子の観測欄の更新を含む側を示す。"
                                  "germinating=false の行は縁に当たらない入力で、発芽しない側を示す。",
            },
            "budget": {"soft_ms": SOFT_MS, "hard_ms": HARD_MS, "source": BUDGET_SOURCE},
            "rows": rows,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _parse_rows(text):
    try:
        sizes = tuple(int(x) for x in text.split(",") if x.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("行数は整数のコンマ区切りです: {!r}".format(text))
    if not sizes or any(n < 0 for n in sizes):
        raise argparse.ArgumentTypeError("行数は 0 以上の整数が 1 つ以上要ります")
    return sizes


def _parse_combos(text):
    try:
        combos = tuple(tuple(int(v) for v in x.split(":")) for x in text.split(",") if x.strip())
        ok = all(len(c) == 2 and min(c) >= 0 for c in combos)
    except ValueError:
        ok = False
    if not ok:
        raise argparse.ArgumentTypeError("--combos は imprint行数:manas行数 のコンマ区切りです: {!r}".format(text))
    return combos


def _first_exceeding(rows, key):
    ns = sorted(r["n"] for r in rows if r.get(key))
    return ns[0] if ns else None


def run(sizes, repeats, with_hook):
    """環境変数の変更はこの呼び出しの間だけ (呼び出し元の os.environ は戻す)。"""
    with mock.patch.dict(os.environ):
        _isolate_env()
        return _run(sizes, repeats, with_hook)


def _run(sizes, repeats, with_hook):
    global MemoryEngine
    from raiya.engine import MemoryEngine  # noqa: E402  (環境を整えた後に import する)

    root = tempfile.mkdtemp(prefix="recall_latency_")
    try:
        projects = {}
        for name in ("P0", "P1", "P2"):
            d = os.path.join(root, "proj_" + name)
            os.makedirs(d)
            projects[name] = os.path.realpath(d)
        loc_dir = os.path.join(root, "loc")
        os.makedirs(loc_dir)
        rows = []
        for n in sizes:
            plan = _plan(n)
            for i in range(n):
                if plan[i][0] == "所在" and (i + 1) % 2 == 0:
                    with open(os.path.join(loc_dir, "{}.md".format(i + 1)), "w", encoding="utf-8") as f:
                        f.write("x")
            for variant in VARIANTS:
                case = _Case(root, n, variant, plan, projects, projects["P0"], loc_dir)
                row = {"n": n, "variant": variant}
                row.update(measure_case(case, repeats, with_hook))
                rows.append(row)
        first_soft_by = {v: _first_exceeding([r for r in rows if r["variant"] == v], "exceeds_soft") for v in VARIANTS}
        first_group_by = {v: _first_exceeding([r for r in rows if r["variant"] == v], "exceeds_group_ms")
                          for v in VARIANTS}
        first_hard_by = {v: _first_exceeding([r for r in rows if r["variant"] == v], "exceeds_hard") for v in VARIANTS}
        return {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "environment": {
                "python": platform.python_version(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "cpu_count": os.cpu_count(),
            },
            "params": {
                "sizes": list(sizes),
                "repeats": repeats,
                "hook_repeats": HOOK_REPEATS if with_hook else 0,
                "rng_seed": RNG_SEED,
                "life": LIFE,
                "kind_mix": [[k, w] for k, w in KIND_MIX],
                "project_mix": [[k, w] for k, w in PROJECT_MIX],
                "variants": list(VARIANTS),
                "with_hook": with_hook,
            },
            "budget": {"soft_ms": SOFT_MS, "hard_ms": HARD_MS, "group_ms": GROUP_MS_LIMIT, "source": BUDGET_SOURCE},
            "rows": rows,
            "first_n_exceeding_soft": min((v for v in first_soft_by.values() if v is not None), default=None),
            "first_n_exceeding_hard": min((v for v in first_hard_by.values() if v is not None), default=None),
            "group_first_n_exceeding_100ms": min((v for v in first_group_by.values() if v is not None),
                                                 default=None),
            "first_n_exceeding_by_variant": {"soft": first_soft_by, "hard": first_hard_by,
                                             "group_ms": first_group_by},
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _parse_sizes(text):
    try:
        sizes = tuple(int(x) for x in text.split(",") if x.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("--sizes は整数のコンマ区切りです: {!r}".format(text))
    if not sizes or any(n <= 0 for n in sizes):
        raise argparse.ArgumentTypeError("--sizes は正の整数が 1 つ以上要ります")
    return sizes


def main(argv=None):
    parser = argparse.ArgumentParser(description="recall 同期経路の読み遅延の計測器 (LLM 不使用)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="出力先ディレクトリ (既定: receipts/)")
    parser.add_argument("--sizes", type=_parse_sizes, default=None)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--no-hook", action="store_true", help="hook 子プロセスの計測を省く")
    parser.add_argument("--logs", action="store_true",
                        help="imprint_log / manas ログの大きさの軸を測る (--sizes は使わない。結果は recall_latency_logs.json)")
    parser.add_argument("--n", type=int, default=None, help="--logs の種子数 (既定 1000)")
    parser.add_argument("--imprint-sizes", type=_parse_rows, default=None)
    parser.add_argument("--manas-sizes", type=_parse_rows, default=None)
    parser.add_argument("--combos", type=_parse_combos, default=None)
    parser.add_argument("--quiet-combos", type=_parse_combos, default=None,
                        help="--logs で、縁に当たらない入力 (発芽しない) でも測る imprint行数:manas行数 の組")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats は 1 以上です")
    logs_only = ("n", "imprint_sizes", "manas_sizes", "combos", "quiet_combos")
    if not args.logs:
        given = [k for k in logs_only if getattr(args, k) is not None]
        if given:
            parser.error("--{} は --logs と一緒に使います".format(given[0].replace("_", "-")))
    if args.sizes is None:
        args.sizes = DEFAULT_SIZES
    elif args.logs:
        parser.error("--sizes は --logs では使えません (--imprint-sizes / --manas-sizes を使う)")
    if args.logs:
        n = LOGS_DEFAULT_N if args.n is None else args.n
        if n < 1:
            parser.error("--n は 1 以上です")
        pick = lambda v, d: d if v is None else v  # noqa: E731
        receipt = run_logs(n, pick(args.imprint_sizes, LOGS_DEFAULT_IMPRINT), pick(args.manas_sizes, LOGS_DEFAULT_MANAS),
                           pick(args.combos, LOGS_DEFAULT_COMBOS), args.repeats, not args.no_hook,
                           pick(args.quiet_combos, LOGS_DEFAULT_QUIET))
        os.makedirs(args.out, exist_ok=True)
        out_path = os.path.join(args.out, LOGS_OUT_NAME)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(receipt, f, ensure_ascii=False, indent=2)
        print("written to {}".format(out_path))
        return receipt
    receipt = run(args.sizes, args.repeats, not args.no_hook)
    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, OUT_NAME)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)
    print("written to {}".format(out_path))
    return receipt


if __name__ == "__main__":
    main()
