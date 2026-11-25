"""観測コマンド `raiya-inspect` の決定論 pin。spec: docs/superpowers/specs/2026-10-20-inspect-cli-design.md

§5 done の基準: explain_recall ≡ recall (id・順序・注入文)、7 つの落選理由と索引の落選理由、観測の動詞が
state dir を 1 バイトも変えない (sha256)、存在しない dir は exit 2 で作らない、why の結び方 (seed_id・
古い行・span の不一致)、注入ログの ts/session_id/project と古い行の (記録なし)、backend を読まない。
LLM は使わない。
"""
import ast
import contextlib
import hashlib
import html
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import receipt_harness  # noqa: E402
from raiya import alaya as alaya_mod  # noqa: E402
from raiya import inspect_cli  # noqa: E402
from raiya.engine import ALL_PROJECTS, INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya import storage  # noqa: E402
from tests._state_fixture import (  # noqa: E402
    alaya_store_at, clear_state, edit_ledger_rows, mutate, ro, set_meta, table_bodies, table_digest,
)

try:
    import anyio  # noqa: F401
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:  # pragma: no cover - 環境依存
    HAS_MCP = False

requires_mcp = unittest.skipUnless(HAS_MCP, "公式 mcp SDK が未インストール (pip install -e '.[mcp]')")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
P = "/home/dev/p"
Q = "/home/dev/q"


def _seed(seed_id, conditions, kind="所在", sansho="圓成實", strength=1, project=P, superseded_by=None, content=None):
    return {
        "id": seed_id,
        "content": content or "種子{}".format(seed_id),
        "strength": strength,
        "conditions": list(conditions),
        "polarity": "無記",
        "sansho": sansho,
        "kind": kind,
        "project": project,
        "superseded_by": superseded_by,
        "evidences": [],
        "history": [],
        "planted_at": 1,
        "last_fired": None,
        "fire_count": 0,
        "miss_streak": 0,
    }


def _write_state(state_dir, seeds, life=3):
    os.makedirs(state_dir, exist_ok=True)
    clear_state(state_dir)  # fixture の書き直し (write_state は既存 state を拒否する)
    write_state(state_dir, {"seeds": seeds, "alaya_meta": {"life": life}})


def _tree_digest(path):
    """state dir 以下の全ファイル (sessions/ 等のサブディレクトリも) の名前と sha256。"""
    out = {}
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


def _ids(seeds):
    return [s["id"] for s in seeds]


def _no_dispositions_env():
    return {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}


class _StateCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_inspect_")
        self.state = os.path.join(self.tmpdir, "state")
        patcher = mock.patch.dict(os.environ, _no_dispositions_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = inspect_cli.main(["--state-dir", self.state] + list(argv))
            except SystemExit as e:  # argparse の誤り
                code = e.code
        return code, out.getvalue(), err.getvalue()


class TestExplainEqualsRecall(_StateCase):
    """§5.2: explain_recall の発芽・索引・注入文は、同じ state・同じ入力の recall と一致する。"""

    def _assert_same(self, eng, text, context=None, project=None, session_start=False):
        explained = eng.explain_recall(text, context=context, project=project, session_start=session_start)
        recalled = eng.recall(text, context=context, project=project, session_start=session_start)
        self.assertEqual(_ids(explained["seeds"]), _ids(recalled["seeds"]))
        self.assertEqual(_ids(explained["index"]), _ids(recalled["index"]))
        self.assertEqual(explained["narrative"], recalled["narrative"])
        self.assertEqual(explained["resume"], recalled["resume"])
        self.assertEqual([g["id"] for g in explained["germinated"]], _ids(recalled["seeds"]))
        return explained, recalled

    def test_recall_fixture_queries_with_and_without_dispositions(self):
        seeds, queries = receipt_harness._load_recall_fixture()
        self.assertTrue(queries)
        for env_value in (None, "1"):
            _write_state(self.state, json.loads(json.dumps(seeds)), life=receipt_harness.RECALL_FIXTURE_LIFE)
            if env_value:
                os.environ[INJECT_DISPOSITIONS_ENV] = env_value
            else:
                os.environ.pop(INJECT_DISPOSITIONS_ENV, None)
            eng = self.engine()
            nonempty = 0
            for q in queries:
                with self.subTest(dispositions=env_value, query=q["label"]):
                    explained, _ = self._assert_same(
                        eng, q["prompt"], context=list(q.get("context") or []),
                        project=q.get("project"), session_start=bool(q.get("session_start")),
                    )
                    nonempty += bool(explained["seeds"]) or bool(explained["index"])
            self.assertGreater(nonempty, 0)  # 空振りの一致ではない

    def test_germination_max_overflow_matches_recall_order(self):
        # 同じ縁の 所在 7 件 (強度違い) -> 上限 5 件が発芽、2 件が「発芽の上限で落ちた」。
        seeds = [_seed(i, ["猫"], strength=i) for i in range(1, 8)]
        _write_state(self.state, seeds)
        eng = self.engine()
        explained, recalled = self._assert_same(eng, "猫の話", project=P)
        self.assertEqual(_ids(recalled["seeds"]), [7, 6, 5, 4, 3])
        over = [r for r in explained["germination_rejected"] if r["reason"] == alaya_mod.GERMINATION_REASON_MAX]
        self.assertEqual([r["id"] for r in over], [2, 1])
        self.assertEqual(over[0]["matched_conditions"], ["猫"])

    def test_session_start_resume_and_context(self):
        seeds = [_seed(1, ["猫"]), _seed(2, ["犬"], kind="再開点"), _seed(3, ["鳥"], sansho="依他起", strength=2)]
        _write_state(self.state, seeds)
        eng = self.engine()
        explained, _ = self._assert_same(eng, "猫", context=["鳥の話"], project=P, session_start=True)
        self.assertEqual(explained["resume"]["id"], 2)
        self.assertEqual(_ids(explained["seeds"]), [1, 3])


class TestRejectionReasons(_StateCase):
    """§1.4 の表: 7 つの発芽の落選理由と、索引の落選理由。それぞれ 1 件ずつ。"""

    def _reasons(self, result):
        return {r["id"]: r["reason"] for r in result["germination_rejected"]}

    def setUp(self):
        super().setUp()
        seeds = [
            _seed(1, ["猫"], superseded_by=9),                    # superseded
            _seed(2, ["猫"], project=Q),                          # project 違い
            _seed(3, ["猫"], sansho="依他起", strength=1),          # 強度不足 (依他起は 2 要る)
            _seed(4, ["猫"], kind="再開点"),                       # 再開点は発芽しない
            _seed(5, ["猫", "犬"], kind="性向"),                   # 性向は既定で除外
            _seed(6, ["鳥"]),                                     # 縁の一致が足りない (0/1)
        ] + [_seed(i, ["猫"], strength=20 - i) for i in range(10, 16)]  # 6 件 -> 1 件が上限で落ちる
        _write_state(self.state, seeds)

    def test_each_germination_reason(self):
        eng = self.engine()
        result = eng.explain_recall("猫の話", project=P)
        reasons = self._reasons(result)
        self.assertEqual(reasons[1], "superseded")
        self.assertEqual(reasons[2], "project 違い")
        self.assertEqual(reasons[3], "強度不足")
        self.assertEqual(reasons[4], "再開点は発芽しない")
        self.assertEqual(reasons[5], "性向は既定で除外")
        self.assertEqual(reasons[6], "縁の一致が足りない (0/1)")
        self.assertEqual(reasons[15], "発芽の上限で落ちた")
        self.assertEqual([g["id"] for g in result["germinated"]], [10, 11, 12, 13, 14])
        # 全種子が発芽か落選のどちらか 1 回だけに現れる
        all_ids = [g["id"] for g in result["germinated"]] + [r["id"] for r in result["germination_rejected"]]
        self.assertEqual(sorted(all_ids), sorted(s["id"] for s in eng.inspect_seeds(include_superseded=True)))

    def test_disposition_needs_two_conditions_when_enabled(self):
        os.environ[INJECT_DISPOSITIONS_ENV] = "1"
        result = self.engine().explain_recall("猫の話", project=P)
        self.assertEqual(self._reasons(result)[5], "縁の一致が足りない (1/2)")
        self.assertEqual(
            [r["matched_conditions"] for r in result["germination_rejected"] if r["id"] == 5], [["猫"]]
        )

    def test_index_reasons(self):
        seeds = [_seed(i, ["x"], kind="決定", strength=30 - i) for i in range(1, 12)]   # 11 件 -> 1 件上限
        seeds += [
            _seed(20, ["x"], kind="性向"),                       # kind の上限 0 (既定)
            _seed(21, ["x"], kind="所在", superseded_by=1),       # superseded
            _seed(22, ["x"], kind="所在", project=Q),             # project 違い
            _seed(23, ["x"], kind="性向", sansho="依他起"),        # class が注入不可 (性向は圓成實のみ)
        ]
        _write_state(self.state, seeds)
        eng = self.engine()
        result = eng.explain_recall("無関係", project=P)
        reasons = {r["id"]: r["reason"] for r in result["index_rejected"]}
        self.assertEqual(reasons[11], "索引の上限で落ちた")
        self.assertEqual(reasons[20], "kind の上限 0")
        self.assertEqual(reasons[21], "superseded")
        self.assertEqual(reasons[22], "project 違い")
        self.assertEqual(_ids(result["index"]), list(range(1, 11)))
        os.environ[INJECT_DISPOSITIONS_ENV] = "1"
        result = eng.explain_recall("無関係", project=P)
        reasons = {r["id"]: r["reason"] for r in result["index_rejected"]}
        self.assertEqual(reasons[23], "class が注入不可")
        self.assertIn(20, _ids(result["index"]))
        self.assertEqual(_ids(result["index"]), _ids(eng.recall("無関係", project=P)["index"]))


    def test_twin_reasons(self):
        # spec 2026-10-23-twin-seed-index §2.3: 圓成實の双子の理由 (発芽・索引 1 件ずつ)
        _write_state(self.state, [
            _seed(1, ["CI", "mypy"], kind="回避策"),
            _seed(2, ["CI", "mypy"], kind="回避策", sansho="依他起", strength=2),
        ])
        result = self.engine().explain_recall("CI の話", project=P)
        self.assertEqual(self._reasons(result)[2], "圓成實の双子 #1 がある")
        index_reasons = {r["id"]: r["reason"] for r in result["index_rejected"]}
        self.assertEqual(index_reasons[2], "圓成實の双子 #1 がある")
        # 圓成實 #1 は「CI の話」で発芽する = ① に出るので ③ の索引には繰り返さない (発芽 の門は 双子 の次)。
        self.assertEqual(_ids(result["index"]), [])
        self.assertEqual(index_reasons[1], "発芽した (① に出る)")


class TestReadVerbsDoNotWrite(_StateCase):
    """§5.3: 4 つの観測の動詞を呼んでも state dir の全ファイルの sha256 が変わらない。"""

    def test_state_dir_byte_identical_after_read_verbs(self):
        _write_state(self.state, [
            _seed(1, ["猫"]), _seed(2, ["犬"], kind="再開点"),
            _seed(3, ["鳥"], content="設計書は {} にある".format(os.path.join(self.tmpdir, "gone", "x.txt"))),
        ])
        eng = self.engine()
        eng.record_turn("猫が好き", "了解", session_id="s1", project=P)
        eng.recall("猫", project=P, session_id="s1")  # manas_log に 1 行作っておく
        with open(os.path.join(self.state, "debug.log"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-23T00:00:00+00:00", "level": 1, "pid": 1, "event": "hook_end"}) + "\n")
            f.write("{壊れた行\n")
        self.assertIsNotNone(eng.seed_provenance(3)["dormant"])  # stat の経路も通す
        before = _tree_digest(self.state)
        before_tables = table_digest(self.state)
        self.assertIn("raiya.db", before)
        self.assertIn("debug.log", before)
        # P16: 同内容の UPDATE はバイトを変えない (sha256 では捕まらない) ので、読む区間を閉じる直前の
        # `conn.total_changes == 0` も見る。
        totals = []
        with mock.patch.object(storage, "READ_CLOSE_HOOK", totals.append):
            eng.inspect_seeds()
            eng.injection_log(5)
            eng.seed_provenance(1)
            eng.seed_provenance(999)
            eng.imprint_log_rows()
            eng.imprint_log_rows(1)
            eng.explain_recall("猫", context=["犬"], project=P, session_start=True)
            eng.audit()
            eng.introspect_material()
            for argv in (["show", "--all"], ["injected"], ["why", "1"], ["why", "3"], ["--json", "why", "3"], ["explain", "猫", "--session-start"],
                         ["--json", "explain", "猫"], ["debug"], ["debug", "--raw"], ["--json", "debug"]):
                self.run_cli(*argv)
        self.assertGreater(len(totals), 10)
        self.assertEqual(set(totals), {0})  # どの読む区間も 1 行も書いていない
        self.assertEqual(_tree_digest(self.state), before)
        self.assertEqual(table_digest(self.state), before_tables)

    def test_a_read_only_connection_cannot_write(self):
        # P16 (a): 読む動詞の書込みは構造的に不可能 (`mode=ro`)。
        _write_state(self.state, [_seed(1, ["猫"])])
        self.engine()
        with ro(self.state) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE meta SET value='9' WHERE key='alaya.life'")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO imprint_log(seed_id, body) VALUES (1, '{}')")

    def test_imprint_log_rows_is_byte_identical_readonly(self):
        rows = [{"decision": "reject", "reason": "r{}".format(i)} for i in range(3)]
        write_state(self.state, {"seeds": [_seed(1, ["猫"])], "alaya_meta": {"life": 1}, "imprint_log": rows})
        eng = self.engine()
        before = _tree_digest(self.state)
        before_tables = table_digest(self.state)
        self.assertEqual(len(eng.imprint_log_rows()), 3)
        self.assertEqual(len(eng.imprint_log_rows(1)), 1)
        after = _tree_digest(self.state)
        after.pop(".lock", None)
        before.pop(".lock", None)
        self.assertEqual(after, before)
        self.assertEqual(table_digest(self.state), before_tables)
        # 読めない行がある場合: 読む動詞は読み飛ばして警告を立てるが、その行を直さない (バイト不変)
        mutate(self.state, lambda c: c.execute("INSERT INTO imprint_log(seed_id, body) VALUES (NULL, '{壊れた')"))
        before = _tree_digest(self.state)
        eng = self.engine()
        self.assertEqual(len(eng.imprint_log_rows()), 3)
        self.assertEqual(len(eng.imprint_log_rows(2)), 1)  # 末尾 2 行 (物理) のうち読める 1 行
        self.assertIn("1 件", eng.audit()["imprint_log_warning"])
        self.assertEqual(_tree_digest(self.state), before)

    def test_missing_state_dir_exits_2_and_creates_nothing(self):
        code, out, err = self.run_cli("show")
        self.assertEqual(code, 2)
        self.assertIn("raiya.db", err)
        self.assertFalse(os.path.exists(self.state))

    def test_existing_dir_without_raiya_db_exits_2_and_creates_nothing(self):
        os.makedirs(self.state)
        code, _, _ = self.run_cli("injected")
        self.assertEqual(code, 2)
        self.assertEqual(os.listdir(self.state), [])  # .lock も DB も作らない


class TestUnknownKindIsDisposition(_StateCase):
    """VALID_KINDS 外の kind は show / why / explain / audit のどこでも 性向 (alaya.seed_kind 1 つ)。"""

    def test_unknown_kind_reads_as_seikou_everywhere(self):
        seed = _seed(1, ["猫"], kind="不明", sansho="圓成實", strength=2, content="猫の話")
        _write_state(self.state, [seed])
        for argv in (["show", "--all"], ["why", "1"], ["explain", "猫"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("性向", out, argv)
            self.assertNotIn("不明", out, argv)
        self.assertEqual(self.engine().audit()["kind_counts"].get("性向"), 1)
        self.assertNotIn("不明", self.engine().audit()["kind_counts"])


class TestWhy(_StateCase):
    """§5.4: 新しい種子は imprint_log の決定行に結ばれ、古い行 (seed_id なし) は結ばない。"""

    def _plant_from_ledger(self, eng, user_text, span, conditions, assistant_text="了解"):
        rec = eng.record_turn(user_text, assistant_text, session_id="s1", project=P)
        turn = LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=rec["ledger_id"], project=P)
        proposal = {"content": span, "conditions": conditions, "polarity": "無記", "kind": "所在",
                    "evidence": [{"source": "user", "span": span}]}
        return eng.imprint([turn], [proposal])[0], rec["ledger_id"]

    def test_seed_links_to_decisions_rows_and_ledger(self):
        _write_state(self.state, [])
        eng = self.engine()
        first, lid1 = self._plant_from_ledger(eng, "state は ~/.raiya にある", "state は ~/.raiya にある", ["state"])
        second, lid2 = self._plant_from_ledger(eng, "やはり state は ~/.raiya にある", "state は ~/.raiya にある", ["state"])
        seed_id = first["seed"]["id"]
        self.assertEqual(second["seed"]["id"], seed_id)  # 併合
        prov = eng.seed_provenance(seed_id)
        self.assertEqual([d["decision"] for d in prov["decisions"]], ["圓成實", "圓成實"])
        self.assertEqual([e["ledger_id"] for e in prov["evidences"]], [lid1, lid2])
        e1 = prov["evidences"][0]
        self.assertTrue(e1["found"])
        self.assertEqual(e1["row"]["session_id"], "s1")
        self.assertEqual(e1["row"]["project"], P)
        self.assertEqual(e1["body_field"], "grounding_text")
        self.assertEqual(prov["evidences"][1]["excerpt"]["before"], "やはり")  # sansho._n 後 (CJK 隣接の空白は消える)
        self.assertFalse(prov["consolidated"])  # consolidated_upto=0
        set_meta(self.state, "ledger.consolidated_upto", lid2)
        self.assertTrue(eng.seed_provenance(seed_id)["consolidated"])
        code, out, _ = self.run_cli("why", str(seed_id))
        self.assertEqual(code, 0)
        self.assertIn("統合済", out)
        self.assertIn("…やはり【stateは~/.raiyaにある】", out)  # 正規化後の本文で示す
        # K1: 云何 → 頌 → 論曰 (強度 → 履歴 → 根拠 → 決定 → 統合)
        order = [out.index(m) for m in ("#{} 所在 project=".format(seed_id), "- 所在: ", "縁: state",
                                         "強度: 2", "履歴:", "根拠:", "決定:", "統合:")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(out.splitlines()[0].split(" project=")[0], "#{} 所在".format(seed_id))
        from raiya.manas import _render_seed_line
        self.assertIn(_render_seed_line(eng.seed_provenance(seed_id)["seed"]), out)  # 注入文と同じ行

    def test_old_rows_without_seed_id_are_not_linked(self):
        _write_state(self.state, [])
        eng = self.engine()
        result, _ = self._plant_from_ledger(eng, "state は ~/.raiya にある", "state は ~/.raiya にある", ["state"])

        def _old_shape(conn):
            for seq, body in conn.execute("SELECT seq, body FROM imprint_log").fetchall():
                row = json.loads(body)
                row.pop("seed_id", None)  # この機能より前の行の形
                conn.execute("UPDATE imprint_log SET seed_id=NULL, body=? WHERE seq=?", (json.dumps(row, ensure_ascii=False), seq))

        mutate(self.state, _old_shape)
        self.assertEqual(eng.seed_provenance(result["seed"]["id"])["decisions"], [])
        code, out, _ = self.run_cli("why", str(result["seed"]["id"]))
        self.assertEqual(code, 0)
        self.assertIn("決定の記録なし(この機能より前の種子)", out)

    def test_span_missing_from_ledger_is_reported(self):
        _write_state(self.state, [])
        eng = self.engine()
        result, _ = self._plant_from_ledger(eng, "state は ~/.raiya にある", "state は ~/.raiya にある", ["state"])
        edit_ledger_rows(self.state, lambda rows: rows[0].__setitem__("grounding_text", "書き換えられた本文"))
        prov = eng.seed_provenance(result["seed"]["id"])
        self.assertFalse(prov["evidences"][0]["found"])
        self.assertEqual(prov["evidences"][0]["problem"], "span が台帳の本文に見つからない")
        _, out, _ = self.run_cli("why", str(result["seed"]["id"]))
        self.assertIn("span が台帳の本文に見つからない", out)

    def _seed_with_evidences(self, evidences):
        seed = _seed(1, ["猫"])
        seed["evidences"] = evidences
        _write_state(self.state, [seed])
        return self.engine()

    def test_evidence_whose_ledger_row_is_missing_reports_problem(self):
        eng = self._seed_with_evidences([{"life": 1, "source": "user", "span": "猫が好き", "ledger_id": 42}])
        e = eng.seed_provenance(1)["evidences"][0]
        self.assertFalse(e["found"])
        self.assertEqual(e["problem"], "台帳に行 #42 が無い")
        self.assertIsNone(e["row"])
        self.assertIsNone(e["body_field"])
        self.assertIsNone(e["excerpt"])
        _, out, _ = self.run_cli("why", "1")
        self.assertIn("台帳に行 #42 が無い", out)

    def test_assistant_source_evidence_reads_assistant_text_of_the_row(self):
        seed = _seed(1, ["散歩"], sansho="依他起")
        seed["evidences"] = [{"life": 1, "source": "assistant", "span": "散歩に行きましょう", "ledger_id": 1}]

        def write_all():
            clear_state(self.state)
            write_state(self.state, {
                "seeds": [seed], "alaya_meta": {"life": 3},
                "ledger_rows": [{"id": 1, "life": 1, "session_id": "s1", "ts": 1.0, "user_text": "猫が好きです",
                                 "assistant_text": "散歩に行きましょう", "grounding_text": "猫が好きです", "project": P}],
            })

        write_all()
        e = self.engine().seed_provenance(1)["evidences"][0]
        self.assertTrue(e["found"])
        self.assertEqual(e["body_field"], "assistant_text")
        self.assertEqual(e["row"]["session_id"], "s1")
        self.assertIsNone(e["problem"])
        # user_text 側にだけ在る語は assistant 根拠としては見つからない
        seed["evidences"][0]["span"] = "猫が好きです"
        write_all()
        e = self.engine().seed_provenance(1)["evidences"][0]
        self.assertFalse(e["found"])
        self.assertEqual(e["problem"], "span が台帳の本文に見つからない")

    def test_evidence_without_ledger_id_or_turn_snapshot_reports_problem(self):
        eng = self._seed_with_evidences([
            {"life": 1, "source": "user", "span": "猫が好き"},
            {"life": 1, "source": "user", "span": "猫が好き", "ledger_id": None, "turn": "not a dict"},
        ])
        for e in eng.seed_provenance(1)["evidences"]:
            self.assertFalse(e["found"])
            self.assertEqual(e["problem"], "台帳の行もスナップショットも無い")
            self.assertIsNone(e["body_field"])
            self.assertIsNone(e["row"])

    def test_missing_and_invalid_ids(self):
        _write_state(self.state, [_seed(1, ["猫"])])
        eng = self.engine()
        self.assertIsNone(eng.seed_provenance(999))
        self.assertIsNone(eng.seed_provenance(True))  # bool は id 1 ではない
        self.assertEqual(self.run_cli("why", "999")[0], 1)
        self.assertEqual(self.run_cli("why", "abc")[0], 2)


class TestWhyDormant(_StateCase):
    """休眠設計書 §7: why は休眠を読み取り時に派生して出す。書かない。"""

    def _plant_location(self, eng, content):
        rec = eng.record_turn(content, "了解", session_id="s1", project=P)
        turn = LedgerTurn(user_text=content, assistant_text="了解", id=rec["ledger_id"], project=P)
        proposal = {"content": content, "conditions": ["設計書"], "polarity": "無記", "kind": "所在",
                    "evidence": [{"source": "user", "span": content}]}
        return eng.imprint([turn], [proposal])[0]["seed"]["id"]

    def _tmp(self):
        d = tempfile.mkdtemp(prefix="raiya_dormant_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)  # 自作の tmp だけを消す
        return d

    def _why_line(self, seed_id):
        code, out, _ = self.run_cli("why", str(seed_id))
        self.assertEqual(code, 0)
        return [ln for ln in out.splitlines() if ln.startswith("休眠:")], out

    def test_non_location_seed_is_not_dormant(self):
        _write_state(self.state, [_seed(1, ["猫"], kind="決定", content="/nonexistent_raiya_zz/a/b.txt を使わない")])
        eng = self.engine()
        self.assertIsNone(eng.seed_provenance(1)["dormant"])
        lines, out = self._why_line(1)
        self.assertEqual(lines, ["休眠: なし"])
        self.assertLess(out.index("休眠: なし"), out.index("履歴:"))

    def test_location_without_paths_is_not_dormant(self):
        _write_state(self.state, [_seed(1, ["猫"])])
        self.assertIsNone(self.engine().seed_provenance(1)["dormant"])
        self.assertEqual(self._why_line(1)[0], ["休眠: なし"])

    def test_absent_path_is_dormant_then_reappears(self):
        _write_state(self.state, [])
        eng = self.engine()
        base = self._tmp()
        path = os.path.join(base, "gone", "x.txt")
        sid = self._plant_location(eng, "設計書は {} にある".format(path))
        self.assertEqual(eng.seed_provenance(sid)["dormant"], {"absent_paths": [path]})
        lines, _ = self._why_line(sid)
        self.assertEqual(lines, ["休眠: {}(この機械に無い)".format(path)])
        code, out, _ = self.run_cli("--json", "why", str(sid))
        self.assertEqual(json.loads(out)["dormant"], {"absent_paths": [path]})
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertIsNone(eng.seed_provenance(sid)["dormant"])  # 状態は何も変えていない
        self.assertEqual(self._why_line(sid)[0], ["休眠: なし"])

    def test_location_with_one_present_path_is_not_dormant(self):
        _write_state(self.state, [])
        eng = self.engine()
        base = self._tmp()
        here = os.path.join(base, "here.txt")
        with open(here, "w", encoding="utf-8") as f:
            f.write("x")
        gone = os.path.join(base, "gone", "x.txt")
        sid = self._plant_location(eng, "設計書は {} と {} にある".format(here, gone))
        self.assertIsNone(eng.seed_provenance(sid)["dormant"])
        self.assertEqual(self._why_line(sid)[0], ["休眠: なし"])

    def test_unknown_id_does_not_stat(self):
        _write_state(self.state, [_seed(1, ["猫"])])
        eng = self.engine()
        with mock.patch("raiya.engine._absent_local_paths") as m:
            self.assertIsNone(eng.seed_provenance(999))
            self.assertEqual(self.run_cli("why", "999")[0], 1)
        m.assert_not_called()

    def test_tilde_path_uses_home(self):
        home = self._tmp()
        with mock.patch.dict(os.environ, {"HOME": home}):
            _write_state(self.state, [])
            eng = self.engine()
            sid = self._plant_location(eng, "設計書は ~/gone/memo.txt にある")
            self.assertEqual(eng.seed_provenance(sid)["dormant"], {"absent_paths": ["~/gone/memo.txt"]})  # ~ のまま
            self.assertEqual(self._why_line(sid)[0], ["休眠: ~/gone/memo.txt(この機械に無い)"])
            os.makedirs(os.path.join(home, "gone"))
            with open(os.path.join(home, "gone", "memo.txt"), "w", encoding="utf-8") as f:
                f.write("x")
            self.assertIsNone(eng.seed_provenance(sid)["dormant"])
            self.assertEqual(self._why_line(sid)[0], ["休眠: なし"])

    def test_why_on_dormant_location_leaves_state_byte_identical(self):
        _write_state(self.state, [])
        eng = self.engine()
        sid = self._plant_location(eng, "設計書は {} にある".format(os.path.join(self._tmp(), "gone", "x.txt")))
        before = _tree_digest(self.state)
        self.assertIsNotNone(eng.seed_provenance(sid)["dormant"])
        self.run_cli("why", str(sid))
        self.run_cli("--json", "why", str(sid))
        self.assertEqual(_tree_digest(self.state), before)


class TestImprintLogSeedId(_StateCase):
    """§2.2: imprint_log の行に seed_id (new / merge / supersede は書いた先、reject / 遍計所執 は None)。"""

    def test_seed_id_per_decision(self):
        store = alaya_store_at(os.path.join(self.tmpdir, "a.json"), os.path.join(self.tmpdir, "l.json"))
        turn = LedgerTurn(user_text="猫が好きです/ 猫", assistant_text="")

        def prop(span, **extra):
            p = {"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
                 "evidence": [{"source": "user", "span": span}]}
            p.update(extra)
            return p

        new = store.imprint(turn, prop("猫が好きです/"))
        merged = store.imprint(turn, prop("猫が好きです/"))
        fabricated = store.imprint(turn, prop("犬が好きです"))
        rejected = store.imprint(turn, prop("猫が好きです/", kind="不明な kind"))
        superseded = store.imprint(turn, prop("猫が好きです/", action="supersede", target_id=new["seed"]["id"]))
        log = store.list_imprint_log()
        self.assertEqual([r["decision"] for r in log], ["圓成實", "圓成實", "遍計所執", "reject", "圓成實"])
        sid = new["seed"]["id"]
        self.assertEqual([r["seed_id"] for r in log], [sid, sid, None, None, sid])
        self.assertTrue(merged.get("reinforced"))
        self.assertTrue(superseded.get("superseded"))
        self.assertEqual(fabricated["decision"], "遍計所執")
        self.assertEqual(rejected["decision"], "reject")
        self.assertEqual(list(log[0])[:5], ["life", "turn", "proposal", "decision", "reason"])


class TestInjectionLog(_StateCase):
    """§5.5: 新しい行には ts / session_id / project、古い行は (記録なし)。hooks の recall が session_id を残す。"""

    def test_new_entries_carry_ts_session_project_after_existing_keys(self):
        _write_state(self.state, [_seed(1, ["猫"])])
        eng = self.engine()
        eng.recall("猫", project=P, session_id="s9")
        eng.recall("猫")  # MCP/CLI 相当: session なし・project なし
        log = eng.injection_log(2)
        self.assertEqual(list(log[0]), ["life", "ego_weight", "injected_text", "seed_ids", "index_ids",
                                        "ts", "session_id", "project", "cut", "shortened"])  # cut = layered-memory 計画 §2.5・shortened = T3 6b
        self.assertEqual((log[0]["session_id"], log[0]["project"]), ("s9", P))
        self.assertRegex(log[0]["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual((log[1]["session_id"], log[1]["project"]), (None, None))

    def test_old_entries_show_no_record_and_newest_first(self):
        _write_state(self.state, [_seed(1, ["猫"])])
        eng = self.engine()
        eng.recall("猫", project=P, session_id="s-old")
        eng.recall("猫", project=P, session_id="s-new")

        def _old_shape(conn):
            seq, body = conn.execute("SELECT seq, body FROM manas_log ORDER BY seq LIMIT 1").fetchone()
            entry = json.loads(body)
            for key in ("ts", "session_id", "project"):
                entry.pop(key)
            conn.execute("UPDATE manas_log SET body=? WHERE seq=?", (json.dumps(entry, ensure_ascii=False), seq))

        mutate(self.state, _old_shape)
        code, out, _ = self.run_cli("injected", "--last", "5")
        self.assertEqual(code, 0)
        blocks = out.split("\n\n時刻=")
        self.assertIn("session=s-new", blocks[0])
        self.assertIn("時刻=(記録なし) session=(記録なし) project=(記録なし)", out)
        self.assertEqual(self.run_cli("injected", "--last", "0")[0], 2)

    def test_hooks_recall_records_session_id(self):
        from raiya.hooks import claude_code as hooks

        _write_state(self.state, [_seed(1, ["猫"])])
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "猫の話をしよう", "session_id": "sess-h", "cwd": self.tmpdir}, self.state)
        entry = self.engine().injection_log(1)[0]
        self.assertEqual(entry["session_id"], "sess-h")
        self.assertEqual(entry["project"], os.path.realpath(self.tmpdir))


class TestShowCli(_StateCase):
    def setUp(self):
        super().setUp()
        old = _seed(4, ["古"], project=None)
        for key in ("kind", "project", "superseded_by"):
            old.pop(key)  # 改修5 より前の行の形
        _write_state(self.state, [_seed(1, ["猫"]), _seed(2, ["犬"], project=Q, kind="決定"),
                                  _seed(3, ["鳥"], superseded_by=1), old])

    def test_project_filters_and_superseded(self):
        eng = self.engine()
        self.assertEqual(_ids(eng.inspect_seeds()), [1, 2, 4])
        self.assertEqual(_ids(eng.inspect_seeds(project=ALL_PROJECTS, include_superseded=True)), [1, 2, 3, 4])
        self.assertEqual(_ids(eng.inspect_seeds(project=P)), [1, 4])
        self.assertEqual(_ids(eng.inspect_seeds(project=None)), [4])
        self.assertEqual(_ids(eng.inspect_seeds(kind="性向")), [4])  # kind 欄なし = 性向
        with self.assertRaises(ValueError):
            eng.inspect_seeds(kind="不明")
        code, out, _ = self.run_cli("show", "--project", "")
        self.assertEqual(code, 0)
        self.assertIn("#4 性向 project=(全域) 圓成實", out)
        order = [out.index(m) for m in ("#4 性向 project=(全域) 圓成實", "- 性向: ", "縁: ", "強度: ", "由来: ")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("#1 ", out)
        _, out, _ = self.run_cli("show", "--all")
        self.assertIn("(superseded→#1)", out)
        code, out, _ = self.run_cli("--json", "show", "--kind", "決定")
        self.assertEqual((code, _ids(json.loads(out))), (0, [2]))
        self.assertEqual(self.run_cli("show", "--kind", "不明")[0], 2)
        # 表示は古い行を書き換えない (E8)
        self.assertNotIn("kind", json.loads(table_bodies(self.state, "seeds", order="pos")[3]))


class TestShowWhyRendering(_StateCase):
    def test_show_and_why_survive_non_dict_evidence(self):
        seed = _seed(1, ["猫"], kind="性向", content="壊れた根拠")
        seed["evidences"] = ["x", 3]
        _write_state(self.state, [seed])
        code, out, _ = self.run_cli("show", "--all")
        self.assertEqual(code, 0)
        self.assertIn("- 性向: 壊れた根拠(強度1)", out)
        self.assertIn("由来: 根拠なし", out)
        code, out, _ = self.run_cli("why", "1")
        self.assertEqual(code, 0)
        self.assertIn("- 性向: 壊れた根拠(強度1)", out)
        self.assertIn("由来: 根拠なし", out)

    def test_why_seed_line_equals_injected_line(self):
        _write_state(self.state, [_seed(1, ["猫"], content="猫が好き")])
        eng = self.engine()
        eng.recall("猫の話", project=P)
        injected = eng.injection_log(1)[0]["injected_text"]
        line = "- 所在: 猫が好き(強度1)"
        self.assertIn(line, injected)
        code, out, _ = self.run_cli("why", "1")
        self.assertEqual(code, 0)
        self.assertIn(line, out)

    def test_inferred_seed_shows_suiteishi_label(self):
        _write_state(self.state, [_seed(1, ["猫"], kind="決定", sansho="依他起", content="推定の内容")])
        code, out, _ = self.run_cli("show")
        self.assertEqual(code, 0)
        self.assertIn("- 決定(推定): 推定の内容(強度1)", out)


class TestVerdictsArePure(_StateCase):
    """§3.1: 判定 (`_germination_verdicts` / `_index_verdicts`) は状態を変えない。`germinate` が書くのは
    発芽した種子の last_fired / fire_count / miss_streak だけ。"""

    def _store(self):
        seeds = [
            _seed(1, ["猫"], superseded_by=9),
            _seed(2, ["猫"], project=Q),
            _seed(3, ["猫"], sansho="依他起", strength=1),
            _seed(4, ["猫"], kind="再開点"),
            _seed(5, ["猫", "犬"], kind="性向"),
            _seed(6, ["鳥"]),
            _seed(20, ["魚", "鯨"], kind="回避策"),                             # 双子の対 (入力に触れない)
            _seed(21, ["魚", "鯨"], kind="回避策", sansho="依他起", strength=2),
        ] + [_seed(i, ["猫"], strength=20 - i) for i in range(10, 17)]  # 7 件 -> 2 件が上限で落ちる
        for s in seeds:
            s["miss_streak"] = 2
            s["last_fired"] = 1
            s["fire_count"] = 4
        _write_state(self.state, seeds)
        return alaya_store_at(os.path.join(self.state, "alaya.json"), os.path.join(self.state, "imprint_log.json"))

    def test_verdicts_do_not_mutate_state(self):
        import copy

        store = self._store()
        digest = table_digest(self.state)
        before = copy.deepcopy(store.state)
        before_log = store.list_imprint_log()
        for dispositions in (False, True):
            for project in (P, None, {P, Q}):
                verdicts = store._germination_verdicts("猫の話", ["犬"], project=project, include_dispositions=dispositions)
                self.assertEqual(len(verdicts), len(before["seeds"]))
                for purpose in ("inject", "consolidate"):
                    store._index_verdicts(project, purpose, dispositions)
                    store._index_verdicts(project, purpose, dispositions, limit=3)
        self.assertEqual(store.state, before)
        self.assertEqual(store.list_imprint_log(), before_log)
        self.assertEqual(table_digest(self.state), digest)  # 何も保存しない

    def test_germinate_touches_only_fired_seeds(self):
        store = self._store()
        fired = {s["id"] for s in store.germinate("猫の話", project=P, include_dispositions=False)}
        self.assertEqual(fired, {10, 11, 12, 13, 14})
        store.commit()  # germinate は保存しない: 観測欄の変更は commit で初めてディスクに載る
        reread = alaya_store_at(os.path.join(self.state, "alaya.json"), os.path.join(self.state, "imprint_log.json"))
        for seed in reread.state["seeds"]:
            observed = (seed["last_fired"], seed["fire_count"], seed["miss_streak"])
            if seed["id"] in fired:
                self.assertEqual(observed, (3, 5, 0), seed["id"])
            else:
                self.assertEqual(observed, (1, 4, 2), seed["id"])


class TestInspectReviewFollowups(_StateCase):
    """QA 指摘への pin: .lock、台帳外の根拠、空の span、(なし)/(全域)、制御文字、explain の既定 project。"""

    def test_lock_is_the_only_file_created(self):
        _write_state(self.state, [_seed(1, ["猫"])])
        with open(os.path.join(self.state, "raiya.db"), "rb") as f:
            db_bytes = f.read()
        self.assertEqual(os.listdir(self.state), ["raiya.db"])
        code, _, _ = self.run_cli("show")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.state)), [".lock", "raiya.db"])
        self.assertEqual(os.path.getsize(os.path.join(self.state, ".lock")), 0)
        with open(os.path.join(self.state, "raiya.db"), "rb") as f:
            self.assertEqual(f.read(), db_bytes)

    def test_why_on_off_ledger_snapshot(self):
        from raiya.sansho import Turn

        _write_state(self.state, [])
        eng = self.engine()
        result = eng.imprint(Turn(user_text="猫が好きです p/ 猫", assistant_text=""), [
            {"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
             "evidence": [{"source": "user", "span": "猫が好きです p/"}]}])[0]
        prov = eng.seed_provenance(result["seed"]["id"])
        e = prov["evidences"][0]
        self.assertIsNone(e["ledger_id"])
        self.assertEqual(e["body_field"], "turn.user_text")
        self.assertTrue(e["found"])
        self.assertIsNone(e["row"])
        self.assertIsNone(prov["consolidated"])
        code, out, _ = self.run_cli("why", str(result["seed"]["id"]))
        self.assertEqual(code, 0)
        self.assertIn("ledger_id=(台帳外)", out)
        self.assertIn("台帳の根拠なし", out)
        self.assertIn("台帳外 1 件", out)

    def test_empty_span_is_not_found(self):
        seed = _seed(1, ["猫"])
        seed["evidences"] = [{"life": 1, "source": "user", "span": "", "ledger_id": None,
                              "turn": {"user_text": "猫", "assistant_text": ""}}]
        _write_state(self.state, [seed])
        e = self.engine().seed_provenance(1)["evidences"][0]
        self.assertFalse(e["found"])
        self.assertTrue(e["problem"])
        self.assertIsNone(e["position"])

    def test_new_entry_without_session_or_project_prints_none_labels(self):
        _write_state(self.state, [_seed(1, ["猫"], project=None)])
        self.engine().recall("猫")
        _, out, _ = self.run_cli("injected", "--last", "1")
        self.assertIn("session=(なし) project=(全域)", out)
        self.assertNotIn("(記録なし)", out)

    def test_control_characters_are_escaped(self):
        _write_state(self.state, [_seed(1, ["猫"], project="/p\x07q")])
        eng = self.engine()
        eng.recall("猫", project="/p\x07q", session_id="s\x1b[31m")
        for argv in (["injected"], ["show"], ["explain", "猫", "--project", "/p\x07q"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", out, argv)
            self.assertNotIn("\x07", out, argv)
            self.assertIn("/p\\x07q", out, argv)
        self.assertIn("session=s\\x1b[31m", self.run_cli("injected")[1])
        _, out, _ = self.run_cli("--json", "injected")
        self.assertEqual(json.loads(out)[0]["session_id"], "s\x1b[31m")  # JSON は escape に任せる

    def test_hooks_recall_records_only_safe_session_id(self):
        from raiya.hooks import claude_code as hooks

        _write_state(self.state, [_seed(1, ["猫"])])
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "猫の話をしよう", "session_id": "../evil", "cwd": self.tmpdir}, self.state)
            hooks.cmd_recall({"prompt": "猫の話をしよう", "session_id": 123, "cwd": self.tmpdir}, self.state)
        self.assertEqual([e["session_id"] for e in self.engine().injection_log(2)], [None, None])

    def test_explain_defaults_to_cwd_project(self):
        repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(repo, "sub"))
        real = os.path.realpath(repo)
        _write_state(self.state, [_seed(1, ["猫"], project=real), _seed(2, ["猫"], project=None)])
        with mock.patch("os.getcwd", return_value=os.path.join(repo, "sub")):
            _, out, _ = self.run_cli("--json", "explain", "猫")
            default = json.loads(out)
            _, out, _ = self.run_cli("--json", "explain", "猫", "--project", "")
            global_only = json.loads(out)
            _, human, _ = self.run_cli("explain", "猫")
        self.assertEqual(_ids(default["seeds"]), [1, 2])
        self.assertEqual(_ids(global_only["seeds"]), [2])
        self.assertEqual({r["id"]: r["reason"] for r in global_only["germination_rejected"]}, {1: "project 違い"})
        self.assertIn("project: " + real, human)
        # engine の既定は None (全域だけ) のまま
        self.assertEqual(_ids(self.engine().explain_recall("猫")["seeds"]), [2])

    def test_explain_rows_carry_kind_sansho_content(self):
        old = _seed(3, ["猫"], project=None, content="古い種子")
        old.pop("kind")
        _write_state(self.state, [_seed(1, ["猫"]), old])
        result = self.engine().explain_recall("猫", project=P)
        rows = result["germinated"] + result["germination_rejected"] + result["index_rejected"]
        for row in rows:
            self.assertTrue({"id", "kind", "sansho", "content"} <= set(row), row)
        self.assertIn({"id": 3, "kind": "性向", "sansho": "圓成實", "content": "古い種子",
                       "reason": "性向は既定で除外", "matched_conditions": []}, result["germination_rejected"])


class TestServe(_StateCase):
    """Issue #43: `serve` は 127.0.0.1・GET のみ・読むだけ。本文は CLI の出力そのまま (html エスケープ)。"""

    def setUp(self):
        super().setUp()
        _write_state(self.state, [_seed(1, ["猫"], content="<b>猫</b>&"), _seed(2, ["犬"])])
        eng = self.engine()
        eng.record_turn("猫が好き", "了解", session_id="s1", project=P)
        eng.recall("猫", project=P, session_id="s1")
        with open(os.path.join(self.state, "debug.log"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-23T00:00:00+00:00", "level": 2, "pid": 1, "event": "hook_end"}) + "\n")
        self.server = inspect_cli.make_server(self.state, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path, method="GET", host=None):
        req = urllib.request.Request("http://127.0.0.1:{}{}".format(self.port, path), method=method)
        if host is not None:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                self.last_headers = r.headers
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            self.last_headers = e.headers
            return e.code, e.read().decode("utf-8")

    @staticmethod
    def pre(body):
        start = body.index("<pre>") + len("<pre>")
        return html.unescape(body[start:body.index("</pre>", start)])

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_pages_equal_cli_text(self):
        cases = [
            ("/show?all=1", ["show", "--all"]),
            ("/show?" + urllib.parse.urlencode({"kind": "所在", "project": P}), ["show", "--kind", "所在", "--project", P]),
            ("/injected?last=3", ["injected", "--last", "3"]),
            ("/why?seed_id=1", ["why", "1"]),
            ("/explain?" + urllib.parse.urlencode({"text": "猫", "project": P, "context": "犬", "session_start": "1"}),
             ["explain", "猫", "--project", P, "--context", "犬", "--session-start"]),
        ]
        for url, argv in cases:
            status, body = self.get(url)
            code, out, err = self.run_cli(*argv)
            self.assertEqual((status, code), (200, 0), msg=url)
            self.assertEqual(self.pre(body), out + (("\n--- stderr ---\n" + err) if err else ""), msg=url)
        self.assertNotIn("<b>猫</b>", self.get("/show")[1])  # 種子本文の < は escape される

    def test_status_mapping(self):
        self.assertEqual(self.get("/why?seed_id=999")[0], 404)
        self.assertEqual(self.get("/why?seed_id=abc")[0], 400)
        self.assertEqual(self.get("/show?kind=nope")[0], 400)
        self.assertEqual(self.get("/injected?last=%2D1x")[0], 400)
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/")[0], 200)

    def test_dash_prefixed_input_is_not_an_option(self):
        status, body = self.get("/explain?" + urllib.parse.urlencode({"text": "--session-start", "global": "1"}))
        code, out, err = self.run_cli("explain", "--project=", "--", "--session-start")
        self.assertEqual((status, code), (200, 0))
        self.assertEqual(self.pre(body), out + (("\n--- stderr ---\n" + err) if err else ""))
        status, body = self.get("/explain?" + urllib.parse.urlencode({"text": "猫", "global": "1", "context": "--json"}))
        code, out, err = self.run_cli("explain", "--project=", "--context=--json", "--", "猫")
        self.assertEqual((status, code), (200, 0))
        self.assertEqual(self.pre(body), out + (("\n--- stderr ---\n" + err) if err else ""))
        self.assertEqual(self.get("/why?seed_id=-1")[0], 404)

    def test_debug_duplicate_confirm_uses_first(self):
        self.assertEqual(self.get("/debug?confirm=no&confirm=conversation-text")[0], 404)

    def test_host_header_rebinding_guard(self):
        status, body = self.get("/show", host="evil.example")
        self.assertEqual(status, 403)
        self.assertNotIn("猫", body)
        self.assertEqual(self.get("/show", host="evil.example:{}".format(self.port))[0], 403)
        self.assertEqual(self.get("/show", host="localhost:{}".format(self.port))[0], 200)
        self.assertEqual(self.get("/show", host="127.0.0.1:{}".format(self.port))[0], 200)

    def _raw(self, request_bytes):
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        return int(data.split(b" ", 2)[1])

    def test_host_header_duplicate_and_edge_cases(self):
        port = self.port
        good = "127.0.0.1:{}".format(port).encode()
        self.assertEqual(self._raw(b"GET /show HTTP/1.1\r\nHost: " + good + b"\r\nHost: evil.example\r\n\r\n"), 403)
        self.assertEqual(self._raw(b"GET /show HTTP/1.1\r\nHost: " + good + b"\r\nHost: " + good + b"\r\n\r\n"), 403)
        self.assertEqual(self._raw(b"GET /show HTTP/1.1\r\nHost: " + good + b"\r\n\r\n"), 200)
        self.assertEqual(self._raw(b"GET /show HTTP/1.0\r\n\r\n"), 403)
        for host in ("LOCALHOST:{}".format(port), "localhost.:{}".format(port), "[::1]:{}".format(port), "localhost"):
            self.assertEqual(self._raw("GET /show HTTP/1.1\r\nHost: {}\r\n\r\n".format(host).encode()), 403, msg=host)

    def test_global_param_exact_one(self):
        status, body = self.get("/show?global=1")
        code, out, err = self.run_cli("show", "--project=")
        self.assertEqual((status, code), (200, 0))
        self.assertEqual(self.pre(body), out + (("\n--- stderr ---\n" + err) if err else ""))
        status, body = self.get("/show?global=0")
        code, out0, _ = self.run_cli("show")
        self.assertEqual(self.pre(body), out0)
        self.assertNotEqual(out, out0)  # P の種子は全域だけでは出ない
        status, body = self.get("/explain?" + urllib.parse.urlencode({"text": "猫", "global": "0"}))
        code, out, err = self.run_cli("explain", "猫")
        self.assertEqual(self.pre(body), out + (("\n--- stderr ---\n" + err) if err else ""))

    def test_explain_without_text_is_400(self):
        self.assertEqual(self.get("/explain")[0], 400)
        self.assertEqual(self.get("/explain?project=")[0], 400)
        self.assertEqual(self.get("/explain?text=%E7%8C%AB")[0], 200)

    def test_real_config_issue_with_line_separators_not_leaked(self):
        from raiya import config
        markers = ["LEAKCR", "LEAKNEL", "LEAKLS"]
        cfg_path = os.path.join(self.tmpdir, "leak_config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write('version: 1\n"a\\rLEAKCR": 1\n"b\\x85LEAKNEL": 1\n"c\\u2028LEAKLS": 1\n')
        old = os.environ.get("RAIYA_CONFIG")
        self.addCleanup(config._reset)
        self.addCleanup(lambda: os.environ.__setitem__("RAIYA_CONFIG", "" if old is None else old))
        os.environ["RAIYA_CONFIG"] = cfg_path
        config._reset()
        status, body = self.get("/show")
        self.assertEqual(status, 200)
        self.assertIn("設定ファイルに問題あり", body)
        for m in markers + [cfg_path, "leak_config"]:
            self.assertNotIn(m, body)

    def test_security_headers(self):
        for url in ("/show", "/nope"):
            self.get(url)
            self.assertEqual(self.last_headers["Content-Security-Policy"], "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.assertEqual(self.last_headers["X-Content-Type-Options"], "nosniff")

    def test_idle_connection_does_not_freeze_server(self):
        import socket
        self.server.RequestHandlerClass.timeout = 0.5
        idle = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(idle.close)
        self.assertEqual(self.get("/show")[0], 200)

    def test_default_port_is_8766(self):
        self.assertEqual(inspect_cli.SERVE_DEFAULT_PORT, 8766)

    @requires_mcp
    def test_default_port_differs_from_mcp(self):
        from raiya import mcp_server  # 遅延 import (inspect_cli 本体は mcp_server を読まない)
        self.assertNotEqual(inspect_cli.SERVE_DEFAULT_PORT, mcp_server.DEFAULT_HTTP_PORT)

    def test_config_issue_detail_not_in_body(self):
        orig = inspect_cli.main

        def noisy(argv=None):
            sys.stderr.write("raiya-inspect: 設定ファイルが不正です (有効なキーと env で続行): /secret/path.yaml\n")
            return orig(argv)
        with mock.patch.object(inspect_cli, "main", noisy):
            status, body = self.get("/show")
        self.assertEqual(status, 200)
        self.assertNotIn("/secret/path.yaml", body)

    def test_port_in_use_and_out_of_range_exit_2(self):
        code, _, err = self.run_cli("serve", "--port", str(self.port))
        self.assertEqual(code, 2)
        self.assertIn("待ち受けできない", err)
        code, _, err = self.run_cli("serve", "--port", "70000")
        self.assertEqual(code, 2)
        self.assertIn("待ち受けできない", err)

    def test_trace_is_405_with_allow(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("TRACE", "/show")
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 405)
        self.assertEqual(r.getheader("Allow"), "GET")

    def test_unexpected_exception_is_500_without_message(self):
        with mock.patch.object(inspect_cli, "main", side_effect=RuntimeError("secret")):
            status, body = self.get("/show")
        self.assertEqual(status, 500)
        self.assertIn("RuntimeError", body)
        self.assertNotIn("secret", body)

    def test_non_get_is_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            self.assertEqual(self.get("/show", method=method)[0], 405, msg=method)

    def test_debug_requires_confirmation(self):
        status, body = self.get("/debug")
        self.assertEqual(status, 404)
        self.assertNotIn("hook_end", body)
        status, body = self.get("/debug?confirm=yes")
        self.assertEqual(status, 404)
        status, body = self.get("/debug?confirm=conversation-text")
        self.assertEqual(status, 200)
        self.assertIn("hook_end", body)

    def test_state_dir_byte_identical(self):
        before = _tree_digest(self.state)
        for url in ("/", "/show?all=1", "/injected", "/why?seed_id=1", "/why?seed_id=99", "/explain?text=%E7%8C%AB&session_start=1",
                    "/debug?confirm=conversation-text", "/debug", "/nope"):
            self.get(url)
        self.get("/show", method="POST")
        after = _tree_digest(self.state)
        self.assertEqual({k: v for k, v in after.items() if k != ".lock"}, {k: v for k, v in before.items() if k != ".lock"})
        if ".lock" in after and ".lock" not in before:
            self.assertEqual(after[".lock"], hashlib.sha256(b"").hexdigest())

    def test_serve_exits_2_without_raiya_db(self):
        os.remove(os.path.join(self.state, "raiya.db"))
        code, _, err = self.run_cli("serve", "--port", "0")
        self.assertEqual(code, 2)
        self.assertIn("raiya.db", err)

    def test_no_writing_or_llm_modules_imported(self):
        with open(inspect_cli.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * node.level) + (node.module or ""))
                names.update(("." * node.level) + (node.module or "") + "." + a.name for a in node.names)
        for name in names:
            self.assertNotIn("correct_cli", name)
            self.assertNotIn("backend", name)


class TestInspectDoesNotLoadBackend(unittest.TestCase):
    """§4: inspect_cli は backend を読まない (新しいプロセスで全サブコマンドを流してから sys.modules を見る)。"""

    def test_backend_not_in_sys_modules_after_all_subcommands(self):
        tmpdir = tempfile.mkdtemp(prefix="raiya_inspect_backend_")
        try:
            state = os.path.join(tmpdir, "state")
            _write_state(state, [_seed(1, ["猫"])])
            code = (
                "import sys, contextlib, io\n"
                "from raiya import inspect_cli\n"
                "s = sys.argv[1]\n"
                "with contextlib.redirect_stdout(io.StringIO()):\n"
                "    for argv in (['show'], ['injected'], ['why', '1'], ['explain', '猫'], ['debug']):\n"
                "        assert inspect_cli.main(['--state-dir', s] + argv) == 0, argv\n"
                "import threading, urllib.request\n"
                "srv = inspect_cli.make_server(s, 0)\n"
                "t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()\n"
                "for p in ('show', 'injected', 'why?seed_id=1', 'explain?text=x', 'debug?confirm=conversation-text'):\n"
                "    urllib.request.urlopen('http://127.0.0.1:%d/%s' % (srv.server_address[1], p), timeout=10).read()\n"
                "srv.shutdown(); srv.server_close()\n"
                "print('raiya.backend' in sys.modules, 'raiya.correct_cli' in sys.modules)\n"
            )
            env = {k: v for k, v in os.environ.items() if k != "RAIYA_STATE_DIR"}
            result = subprocess.run([sys.executable, "-c", code, state], capture_output=True, text=True,
                                    cwd=REPO_ROOT, env=env, timeout=30)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
