"""取り下げ (`withdrawn` 欄) のスライス a の pin: store・読み取り側の除外・表示。
spec: docs/superpowers/specs/2026-10-24-correction-withdrawal-design.md /
plan: docs/superpowers/plans/2026-10-25-correction-withdrawal-implementation.md §8 の 1〜3, 7〜9。
LLM は使わない。テストは自作の tempfile.mkdtemp だけを作り、それだけを消す。
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import inspect_cli, sansho  # noqa: E402
from raiya.alaya import (  # noqa: E402
    AlayaStore,
    GERMINATION_REASON_WITHDRAWN,
    INDEX_REASON_WITHDRAWN,
    WithdrawalRefused,
    is_live,
    is_withdrawn,
    withdrawal_basis_problem,
)
from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import clear_state  # noqa: E402
from tests._state_fixture import alaya_store_at, table_bodies, table_digest  # noqa: E402
from raiya import storage  # noqa: E402

P = "/home/dev/p"
AT = "2026-10-25T00:00:00+00:00"


def _record(by="太郎", reason="誤り", ledger_ids=(), spans=(), life=1, replaced_by=None):
    return {"by": by, "at": AT, "reason": reason, "ledger_ids": list(ledger_ids),
            "spans": list(spans), "life": life, "replaced_by": replaced_by}


def _seed(seed_id, conditions, kind="所在", sansho_class="圓成實", strength=1, project=P, content=None, evidences=None):
    return {
        "id": seed_id, "content": content or "種子{}".format(seed_id), "strength": strength,
        "conditions": list(conditions), "polarity": "無記", "sansho": sansho_class, "kind": kind,
        "project": project, "superseded_by": None,
        "evidences": evidences if evidences is not None else [], "history": [],
        "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0,
    }


def _write_state(state_dir, seeds, life=3):
    os.makedirs(state_dir, exist_ok=True)
    clear_state(state_dir)  # fixture の書き直し (write_state は既存 state を拒否する)
    write_state(state_dir, {"seeds": seeds, "alaya_meta": {"life": life}})


def _digest(path):
    out = {}
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


def _ids(seeds):
    return [s["id"] for s in seeds]


def _propose(content, conditions, kind="所在", span=None, **extra):
    # L (question-as-evidence §7): 既定の span は縁を含める (user の逐語に縁が無い 圓成實 所在 は植わらない)。
    span = span or "{} {} p/".format(content, "".join(conditions))  # #149: 所在 の形は 區切りの語 (`/` を含む語)
    turn = sansho.Turn(user_text="{} {}".format(span, "".join(conditions)), assistant_text="")
    proposal = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
                "evidence": {"source": "user", "span": span}}
    proposal.update(extra)
    return turn, proposal


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_withdrawal_")
        self.state = os.path.join(self.tmpdir, "state")
        patcher = mock.patch.dict(
            os.environ, {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def store(self, seeds=None):
        os.makedirs(self.state, exist_ok=True)
        if seeds is not None:
            _write_state(self.state, seeds)
        return alaya_store_at(os.path.join(self.state, "alaya.json"), os.path.join(self.state, "imprint_log.json"))

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)


class TestPredicates(unittest.TestCase):
    def test_missing_key_is_not_withdrawn(self):
        seed = _seed(1, ["猫"])
        self.assertFalse(is_withdrawn(seed))
        self.assertTrue(is_live(seed))
        seed["withdrawn"] = _record()
        self.assertTrue(is_withdrawn(seed))
        self.assertFalse(is_live(seed))
        seed["withdrawn"] = None
        self.assertTrue(is_live(seed))

    def test_superseded_is_not_live(self):
        seed = _seed(1, ["猫"])
        seed["superseded_by"] = 2
        self.assertFalse(is_live(seed))

    def test_history_entry_module_level_matches_old_shape(self):
        seed = _seed(1, ["猫", "犬"])
        entry = alaya_mod._history_entry(seed, 4, "superseded", extra=1)
        self.assertEqual(entry, {"life": 4, "content": "種子1", "conditions": ["猫", "犬"], "sansho": "圓成實",
                                 "polarity": "無記", "reason": "superseded", "extra": 1})


class TestStoreMethods(_TmpCase):
    def test_withdraw_sets_field_history_and_does_not_save(self):
        store = self.store([_seed(1, ["猫"])])
        before = _digest(self.state)
        rec = store.withdraw(1, _record())
        self.assertEqual(rec, _record())
        seed = store.get_by_ids([1])[0]
        self.assertEqual(seed["withdrawn"], _record())
        last = seed["history"][-1]
        self.assertEqual(last["reason"], "withdrawn")
        self.assertEqual(last["withdrawn"], _record())
        self.assertEqual(_digest(self.state), before)  # save しない

    def test_withdraw_refusals(self):
        store = self.store([_seed(1, ["猫"])])
        with self.assertRaises(WithdrawalRefused):
            store.withdraw(99, _record())
        with self.assertRaises(WithdrawalRefused):
            store.withdraw(True, _record())  # bool は id=1 ではない
        store.withdraw(1, _record())
        with self.assertRaises(WithdrawalRefused):
            store.withdraw(1, _record())

    def test_superseded_seed_can_be_withdrawn(self):
        seed = _seed(1, ["猫"])
        seed["superseded_by"] = 2
        store = self.store([seed, _seed(2, ["犬"])])
        store.withdraw(1, _record())
        self.assertTrue(is_withdrawn(store.get_by_ids([1])[0]))

    def test_set_replaced_by_updates_record_and_history(self):
        store = self.store([_seed(1, ["猫"])])
        store.withdraw(1, _record())
        store.set_replaced_by(1, 7)
        seed = store.get_by_ids([1])[0]
        self.assertEqual(seed["withdrawn"]["replaced_by"], 7)
        self.assertEqual(seed["history"][-1]["withdrawn"]["replaced_by"], 7)
        with self.assertRaises(WithdrawalRefused):
            store.set_replaced_by(99, 7)

    def test_restore_and_rewithdraw(self):
        store = self.store([_seed(1, ["猫"])])
        with self.assertRaises(WithdrawalRefused):
            store.restore(1, {"by": "太郎"})  # 取り下げられていない
        store.withdraw(1, _record())
        rr = {"by": "花子", "at": AT, "reason": "戻す", "life": 2}
        store.restore(1, rr)
        seed = store.get_by_ids([1])[0]
        self.assertIsNone(seed["withdrawn"])
        self.assertEqual([h["reason"] for h in seed["history"]], ["withdrawn", "restored"])
        self.assertEqual(seed["history"][0]["withdrawn"], _record())  # 復帰で何も失わない
        self.assertEqual(seed["history"][1]["restored"], rr)
        store.withdraw(1, _record(by="次郎"))  # 復帰後の再取り下げは可
        self.assertEqual(store.get_by_ids([1])[0]["withdrawn"]["by"], "次郎")
        with self.assertRaises(WithdrawalRefused):
            store.restore(99, rr)

    def test_basis_problem(self):
        ev = [{"life": 1, "source": "user", "span": "猫", "ledger_id": 5},
              {"life": 1, "source": "user", "span": "犬  が\n好き", "ledger_id": None}]
        seed = _seed(1, ["猫"], evidences=ev)
        self.assertEqual(withdrawal_basis_problem(seed, [5], []), (None, []))
        problem, _ = withdrawal_basis_problem(seed, [6], [])
        self.assertIn("#6", problem)
        problem, _ = withdrawal_basis_problem(seed, [], ["犬が好き"])
        self.assertIn("--ledger-id", problem)  # 混在: span だけでは迂回できない
        self.assertEqual(withdrawal_basis_problem(seed, [5], ["犬 が 好き"]), (None, ["犬  が\n好き"]))
        problem, _ = withdrawal_basis_problem(seed, [5], ["猫"])  # 台帳内の span は台帳外に一致しない
        self.assertIsNotNone(problem)
        problem, _ = withdrawal_basis_problem(_seed(2, ["猫"]), [], [])
        self.assertIn("名指せる根拠が無い", problem)
        off = _seed(3, ["猫"], evidences=[ev[1]])
        self.assertEqual(withdrawal_basis_problem(off, [], ["犬が好き"]), (None, ["犬  が\n好き"]))


    def test_basis_problem_rejects_empty_basis_on_off_ledger_seed(self):
        off = _seed(1, ["猫"], evidences=[{"life": 1, "source": "user", "span": "犬", "ledger_id": None}])
        problem, matched = withdrawal_basis_problem(off, [], [])
        self.assertIn("根拠が無い", problem)
        self.assertNotIn("名指せる", problem)
        self.assertEqual(matched, [])
        ledger = _seed(2, ["猫"], evidences=[{"life": 1, "source": "user", "span": "犬", "ledger_id": 3}])
        self.assertIsNotNone(withdrawal_basis_problem(ledger, [], [])[0])

    def test_basis_problem_ignores_unnameable_off_ledger_evidence(self):
        junk = [{"life": 1, "source": "user", "span": "", "ledger_id": None},
                {"life": 1, "source": "user", "span": 5, "ledger_id": None},
                {"life": 1, "source": "user", "ledger_id": None}]
        seed = _seed(1, ["猫"], evidences=junk)
        problem, _ = withdrawal_basis_problem(seed, [], [""])
        self.assertIn("名指せる根拠が無い", problem)
        problem, _ = withdrawal_basis_problem(seed, [], [])
        self.assertIn("名指せる根拠が無い", problem)

    def test_set_replaced_by_refuses_live_and_corrupt(self):
        store = self.store([_seed(1, ["猫"]), _seed(2, ["犬"])])
        before = copy.deepcopy(store.state["seeds"])
        with self.assertRaises(WithdrawalRefused):
            store.set_replaced_by(1, 9)  # 生きた種子
        store.state["seeds"][1]["withdrawn"] = "壊れた"
        before = copy.deepcopy(store.state["seeds"])
        with self.assertRaises(WithdrawalRefused):
            store.set_replaced_by(2, 9)
        self.assertEqual(store.state["seeds"], before)

    def test_set_replaced_by_targets_last_withdrawn_row_after_rewithdraw(self):
        store = self.store([_seed(1, ["猫"])])
        store.withdraw(1, _record(by="一"))
        store.restore(1, {"by": "花子"})
        store.withdraw(1, _record(by="二"))
        store.set_replaced_by(1, 7)
        rows = [h for h in store.get_by_ids([1])[0]["history"] if h["reason"] == "withdrawn"]
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["withdrawn"]["replaced_by"])
        self.assertEqual(rows[1]["withdrawn"]["replaced_by"], 7)

    def test_withdraw_refusal_leaves_seed_unchanged(self):
        store = self.store([_seed(1, ["猫"])])
        store.withdraw(1, _record())
        before = copy.deepcopy(store.state)
        with self.assertRaises(WithdrawalRefused):
            store.withdraw(1, _record(by="別人"))
        with self.assertRaises(WithdrawalRefused):
            store.withdraw(99, _record())
        self.assertEqual(store.state, before)


class TestReadSideExclusion(_TmpCase):
    """plan §8 pin 1: 取り下げ中の種子はどの読み手にも出ない。"""

    def test_germination_and_index_reasons(self):
        w = _seed(1, ["猫"])
        w["withdrawn"] = _record()
        store = self.store([w, _seed(2, ["猫"], project=P)])
        verdicts = {s["id"]: (fired, reason) for s, fired, reason, _ in store._germination_verdicts("猫", project=P)}
        self.assertEqual(verdicts[1], (False, GERMINATION_REASON_WITHDRAWN))
        self.assertEqual(verdicts[2], (True, None))
        self.assertEqual(_ids(store.germinate("猫", project=P)), [2])
        for purpose in ("inject", "consolidate"):
            with self.subTest(purpose=purpose):
                verdicts = {s["id"]: reason for s, _, reason in store._index_verdicts(P, purpose)}
                self.assertEqual(verdicts[1], INDEX_REASON_WITHDRAWN)
                self.assertIsNone(verdicts[2])
                self.assertEqual(_ids(store.index(P, purpose)), [2])
                self.assertEqual(store.seed_count(P, purpose), 1)

    def test_withdrawn_wins_over_project_mismatch(self):
        w = _seed(1, ["猫"], project="/other")
        w["withdrawn"] = _record()
        store = self.store([w])
        (_, _, reason, _), = store._germination_verdicts("猫", project=P)
        self.assertEqual(reason, GERMINATION_REASON_WITHDRAWN)

    def test_resume_point_skips_withdrawn(self):
        w = _seed(1, ["再"], kind="再開点")
        w["withdrawn"] = _record()
        store = self.store([w])
        self.assertIsNone(store.resume_point(P))
        w2 = _seed(2, ["再"], kind="再開点")
        store = self.store([w, w2])
        self.assertEqual(store.resume_point(P)["id"], 2)

    def test_merge_does_not_reinforce_withdrawn(self):
        store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        # スライス c 以降: W へ併合されていたはずの提案は植えず (取り下げ照合)、W も強めない。
        self.assertEqual(result["decision"], "取り下げ照合")
        self.assertIsNone(result["seed"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(store.get_by_ids([1])[0]["strength"], 1)

    def test_explicit_target_reinforce_and_supersede_fall_to_new(self):
        for action in ("reinforce", "supersede"):
            with self.subTest(action=action):
                shutil.rmtree(self.state, ignore_errors=True)
                store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
                store.withdraw(1, _record())
                before = copy.deepcopy(store.get_by_ids([1])[0])
                turn, proposal = _propose("全く別の話題", ["別"], action=action, target_id=1)
                result = store.imprint(turn, proposal)
                self.assertTrue(result["is_new"])
                self.assertEqual(store.get_by_ids([1])[0], before)

    def test_horizontal_supersede_skips_withdrawn(self):
        live = _seed(1, ["猫", "犬"], content="旧い話", project=None)
        wd = _seed(2, ["猫", "犬"], content="別の旧い話", project=None)
        store = self.store([live, wd])
        store.withdraw(2, _record())
        turn, proposal = _propose("新しい話", ["猫", "犬"], action="supersede", target_id=1)
        result = store.imprint(turn, proposal)
        self.assertTrue(result.get("superseded"))
        self.assertEqual(result["also_superseded"], [])
        self.assertIsNone(store.get_by_ids([2])[0]["superseded_by"])

    def test_twin_withdrawn_exact_unhides_inferred(self):
        e = _seed(1, ["猫", "犬"], sansho_class="圓成實")
        h = _seed(2, ["猫", "犬"], sansho_class="依他起", strength=2)
        store = self.store([e, h])
        self.assertEqual(_ids(store.index(P, "inject")), [1])  # H は E の影
        store.withdraw(1, _record())
        self.assertEqual(_ids(store.index(P, "inject")), [2])  # H が再浮上
        self.assertEqual(_ids(store.germinate("猫 犬", project=P)), [2])
        store.restore(1, {"by": "花子"})
        self.assertEqual(_ids(store.index(P, "inject")), [1])

    def test_withdrawn_resume_point_gets_fresh_one(self):
        """plan pin 2: 取り下げ中の 再開点 があっても、次の 再開点 は新規に植わる。"""
        store = self.store([_seed(1, ["再"], kind="再開点", content="前の再開点", project=None)])
        store.withdraw(1, _record())
        turn, proposal = _propose("次の再開点", ["次"], kind="再開点")
        result = store.imprint(turn, proposal)
        self.assertTrue(result["is_new"])
        self.assertNotIn("superseded", result)
        self.assertEqual(result["seed"]["id"], 2)
        self.assertIsNone(store.get_by_ids([1])[0]["superseded_by"])
        self.assertTrue(is_withdrawn(store.get_by_ids([1])[0]))
        self.assertEqual(store.resume_point(P)["id"], 2)

    def test_restore_brings_back_every_site(self):
        """plan pin 3: 復帰で全箇所に戻る。"""
        store = self.store([_seed(1, ["猫"], content="猫が好き", project=None), _seed(2, ["再"], kind="再開点", project=None)])
        store.withdraw(1, _record())
        store.withdraw(2, _record())
        self.assertEqual(store.germinate("猫", project=P), [])
        self.assertIsNone(store.resume_point(P))
        store.restore(1, {"by": "花子", "at": AT, "reason": "戻す", "life": 2})
        store.restore(2, {"by": "花子", "at": AT, "reason": "戻す", "life": 2})
        self.assertEqual(_ids(store.germinate("猫", project=P)), [1])
        self.assertEqual(_ids(store.index(P, "inject")), [1])
        self.assertEqual(_ids(store.index(P, "consolidate")), [1, 2])
        self.assertEqual(store.seed_count(P, "inject"), 1)
        self.assertEqual(store.resume_point(P)["id"], 2)
        turn, proposal = _propose("猫が好き", ["猫"])
        self.assertFalse(store.imprint(turn, proposal)["is_new"])  # 併合に戻る


class TestRecallAndExplain(_TmpCase):
    def _seeds(self):
        w = _seed(1, ["猫"])
        w["withdrawn"] = _record()
        return [w, _seed(2, ["猫"]), _seed(3, ["鳥"]), _seed(4, ["再"], kind="再開点")]

    def test_explain_equals_recall_with_withdrawn_present(self):
        """plan pin 8"""
        _write_state(self.state, self._seeds())
        eng = self.engine()
        for text, start in (("猫", False), ("猫 鳥", True), ("無関係", True)):
            with self.subTest(text=text):
                explained = eng.explain_recall(text, project=P, session_start=start)
                recalled = eng.recall(text, project=P, session_start=start)
                self.assertEqual(_ids(explained["seeds"]), _ids(recalled["seeds"]))
                self.assertEqual(_ids(explained["index"]), _ids(recalled["index"]))
                self.assertEqual(explained["narrative"], recalled["narrative"])
                self.assertEqual(explained["resume"], recalled["resume"])
                self.assertNotIn(1, _ids(recalled["seeds"]) + _ids(recalled["index"]))
        explained = eng.explain_recall("猫", project=P)
        self.assertIn((1, GERMINATION_REASON_WITHDRAWN), [(r["id"], r["reason"]) for r in explained["germination_rejected"]])
        self.assertIn((1, INDEX_REASON_WITHDRAWN), [(r["id"], r["reason"]) for r in explained["index_rejected"]])

    def test_recall_leaves_withdrawn_history_and_imprint_log_untouched(self):
        """plan pin 7"""
        seeds = self._seeds()
        seeds[0]["history"].append({"life": 1, "content": "x", "conditions": [], "sansho": "圓成實",
                                    "polarity": "無記", "reason": "withdrawn", "withdrawn": _record()})
        _write_state(self.state, seeds)
        eng = self.engine()
        eng.recall("猫", project=P)
        before_log = table_bodies(self.state, "imprint_log")
        before_digest = table_digest(self.state)
        eng.recall("猫", project=P)
        self.assertEqual(table_bodies(self.state, "imprint_log"), before_log)  # recall は imprint_log に書かない
        after_digest = table_digest(self.state)
        self.assertEqual(after_digest["imprint_log"], before_digest["imprint_log"])
        self.assertEqual(after_digest["ledger"], before_digest["ledger"])
        stored = json.loads(table_bodies(self.state, "seeds", order="pos")[0])
        self.assertEqual(stored["withdrawn"], _record())
        self.assertEqual(len(stored["history"]), 1)

    def test_old_state_without_withdrawn_key_is_unchanged(self):
        _write_state(self.state, [_seed(1, ["猫"]), _seed(2, ["鳥"])])
        eng = self.engine()
        self.assertEqual(_ids(eng.recall("猫", project=P)["seeds"]), [1])
        for body in table_bodies(self.state, "seeds", order="pos"):
            self.assertNotIn("withdrawn", json.loads(body))  # 移行しない

class TestInspectAndAudit(_TmpCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = inspect_cli.main(["--state-dir", self.state] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def _state(self):
        w = _seed(1, ["猫"], content="猫は黒い")
        w["withdrawn"] = _record(by="太郎", reason="記憶違い", ledger_ids=[28, 31], spans=["黒い"], replaced_by=2)
        w["history"].append({"life": 1, "content": "猫は黒い", "conditions": ["猫"], "sansho": "圓成實",
                             "polarity": "無記", "reason": "withdrawn", "withdrawn": _record()})
        _write_state(self.state, [w, _seed(2, ["猫"], content="猫は白い"), _seed(3, ["犬"])])

    def test_inspect_seeds_include_withdrawn(self):
        self._state()
        eng = self.engine()
        self.assertEqual(_ids(eng.inspect_seeds()), [2, 3])
        self.assertEqual(_ids(eng.inspect_seeds(include_withdrawn=True)), [1, 2, 3])

    def test_show_all_marker(self):
        self._state()
        _, out, _ = self.run_cli("show")
        self.assertNotIn("#1 ", out)
        _, out, _ = self.run_cli("show", "--all")
        self.assertIn("project={} 圓成實 (取り下げ)".format(P), out)
        code, out, _ = self.run_cli("--json", "show", "--all")
        self.assertEqual(json.loads(out)[0]["withdrawn"]["by"], "太郎")

    def test_why_block(self):
        self._state()
        code, out, _ = self.run_cli("why", "1")
        self.assertEqual(code, 0)
        self.assertIn("取り下げ: 中 (判定者=太郎 時刻={} life=1)".format(AT), out)
        self.assertIn("    理由: 記憶違い", out)
        self.assertIn("    根拠: 台帳 #28 #31 / span: 「黒い」", out)
        self.assertIn("    置き換え: #2", out)
        self.assertIn("withdrawn: 猫は黒い (判定者=太郎 理由=誤り)", out)
        self.assertLess(out.index("休眠: なし"), out.index("取り下げ: 中"))
        self.assertLess(out.index("取り下げ: 中"), out.index("履歴:"))
        _, out, _ = self.run_cli("why", "2")
        self.assertIn("\n取り下げ: なし\n", out)

    def test_why_escapes_control_characters(self):
        w = _seed(1, ["猫"])
        w["withdrawn"] = _record(by="a\x1b[31mb", reason="r\nline2")
        _write_state(self.state, [w])
        _, out, _ = self.run_cli("why", "1")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("r\nline2", out)

    def test_explain_shows_reason(self):
        self._state()
        _, out, _ = self.run_cli("explain", "猫", "--project", P)
        self.assertEqual(out.count("取り下げ中"), 2)  # 発芽と索引の両方の落選

    def test_inspect_verbs_leave_state_byte_identical_with_withdrawn_seed(self):
        self._state()
        eng = self.engine()
        eng.recall("猫", project=P, session_id="s1")
        turn, proposal = _propose("鳥が好き", ["鳥"])
        eng.imprint([turn], [proposal], project=P)  # imprint_log に 1 行以上
        self.assertTrue(table_bodies(self.state, "imprint_log"))
        before = _digest(self.state)
        before_tables = table_digest(self.state)
        totals = []
        with mock.patch.object(storage, "READ_CLOSE_HOOK", totals.append):
            eng.inspect_seeds(include_withdrawn=True)
            eng.seed_provenance(1)
            eng.explain_recall("猫", project=P)
            eng.injection_log(5)
            for argv in (["show", "--all"], ["why", "1"], ["--json", "why", "1"], ["explain", "猫", "--project", P], ["injected"]):
                self.run_cli(*argv)
        self.assertTrue(totals)
        self.assertEqual(set(totals), {0})
        self.assertEqual(_digest(self.state), before)
        self.assertEqual(table_digest(self.state), before_tables)

    def test_why_tolerates_corrupt_withdrawn_record(self):
        w = _seed(1, ["猫"])
        w["withdrawn"] = {"by": "太郎", "ledger_ids": 5, "spans": "abc"}
        _write_state(self.state, [w])
        code, out, err = self.run_cli("why", "1")
        self.assertEqual(code, 0)
        self.assertIn("記録の形が不正", out)
        self.assertNotIn("「a」", out)
        self.assertNotIn("Traceback", err)

    def test_audit_withdrawn_count(self):
        """plan pin 9"""
        self._state()
        audit = self.engine().audit()
        self.assertEqual(audit["withdrawn_count"], 1)
        self.assertEqual(audit["superseded_count"], 0)
        self.assertEqual(audit["seed_count"], 3)




class TestWithdrawalShadow(_TmpCase):
    """plan §5・§8 pin 6, 15: B4 の影 (再提案が取り下げ中の種子へ併合されていたなら植えない)。"""

    def test_reproposal_into_withdrawn_is_shadowed(self):
        store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _record())
        before = copy.deepcopy(store.get_by_ids([1])[0])
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        self.assertEqual(result["decision"], "取り下げ照合")
        self.assertEqual(result["sansho"], "圓成實")
        self.assertIsNone(result["seed"])
        self.assertIsNone(result["is_new"])
        self.assertEqual(result["withdrawn_match"], 1)
        self.assertIn("取り下げ中の種子 #1 と同趣旨 (類似度 1.00) のため植えない", result["reason"])
        row = store.list_imprint_log()[-1]
        self.assertEqual((row["decision"], row["seed_id"]), ("取り下げ照合", 1))
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(store.get_by_ids([1])[0], before)
        store.commit()
        reread = self.store()
        self.assertEqual(reread.list_imprint_log()[-1]["decision"], "取り下げ照合")

    def test_cross_class_restatement_is_not_shadowed(self):
        """R5-3 の限界: 取り下げた圓成實を依他起で言い直すと植わる (取り下げ前と同じ)。pin して直さない。"""
        store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _record())
        turn = sansho.Turn(user_text="", assistant_text="猫が好き 猫犬")
        proposal = {"content": "猫が好き", "conditions": ["猫", "犬"], "polarity": "無記", "kind": "所在",
                    "evidence": {"source": "assistant", "span": "猫が好き"}}
        result = store.imprint(turn, proposal)
        self.assertEqual(result["decision"], "依他起")
        self.assertTrue(result["is_new"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_best_withdrawn_match_does_not_reinforce_weaker_live_seed(self):
        w = _seed(1, ["猫", "犬"], content="猫が好き", project=None)
        live = _seed(2, ["猫", "犬", "鳥"], content="まるで違う文章", project=None)  # 類似度 2/3 < W の 1.0
        store = self.store([live, w])
        store.withdraw(1, _record())
        before_live = copy.deepcopy(store.get_by_ids([2])[0])
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        self.assertEqual(result["decision"], "取り下げ照合")
        self.assertEqual(result["withdrawn_match"], 1)
        self.assertEqual(store.get_by_ids([2])[0], before_live)

    def test_better_live_match_still_reinforces(self):
        w = _seed(1, ["猫", "犬", "鳥"], content="まるで違う文章", project=None)  # 類似度 2/3 < live の 1.0
        live = _seed(2, ["猫", "犬"], content="猫が好き", project=None)
        store = self.store([w, live])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        self.assertFalse(result["is_new"])
        self.assertEqual(result["seed"]["id"], 2)

    def test_replacement_is_not_shadowed_by_its_own_withdrawn(self):
        store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal, replacement_for=1)
        self.assertTrue(result["is_new"])
        self.assertNotEqual(result["decision"], "取り下げ照合")

    def test_resume_point_is_not_shadowed(self):
        store = self.store([_seed(1, ["再"], kind="再開点", content="次は裁定", project=None)])
        store.withdraw(1, _record())
        turn, proposal = _propose("次は裁定", ["再"], kind="再開点")
        result = store.imprint(turn, proposal)
        self.assertTrue(result["is_new"])
        self.assertNotEqual(result["decision"], "取り下げ照合")

    def test_other_project_withdrawn_does_not_shadow(self):
        store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project="/other")])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        self.assertTrue(store.imprint(turn, proposal)["is_new"])

    def test_shadow_runs_on_engine_consolidate(self):
        eng = self.engine()
        eng.record_turn("私は猫が好きです p/ 猫", "いいですね", session_id="s")
        raw = json.dumps(
            [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善", "kind": "所在",
              "evidence": [{"source": "user", "span": "私は猫が好きです p/"}]}], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            first = eng.consolidate()
        seed = first["decisions"][0]["seed"]
        eng.withdraw_seed(seed["id"], "太郎", "誤り", ledger_ids=[seed["evidences"][0]["ledger_id"]])
        eng.record_turn("私は猫が好きです p/ 猫", "いいですね", session_id="s")
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            second = eng.consolidate()
        self.assertEqual(second["decisions"][0]["decision"], "取り下げ照合")
        self.assertEqual(len(eng.alaya.list_seeds()), 1)

    def test_shadow_runs_on_engine_imprint(self):
        eng = self.engine()
        turn = sansho.Turn(user_text="私は猫が好きです p/ 猫", assistant_text="")
        proposal = {"content": "猫が好き", "conditions": ["猫"], "polarity": "善", "kind": "所在",
                    "evidence": {"source": "user", "span": "私は猫が好きです p/"}}
        first = eng.imprint([turn], [proposal])
        sid = first[0]["seed"]["id"]
        eng.withdraw_seed(sid, "太郎", "誤り", spans=["私は猫が好きです p/"])
        second = eng.imprint([turn], [proposal])
        self.assertEqual(second[0]["decision"], "取り下げ照合")

    def test_replace_with_withdrawn_best_still_merges_into_live(self):
        w = _seed(1, ["猫", "犬"], content="猫が好き", project=None)
        x = _seed(2, ["猫", "犬", "鳥"], content="まるで違う文章", project=None)  # 類似度 2/3 < W の 1.0
        store = self.store([w, x])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal, replacement_for=1)
        self.assertFalse(result["is_new"])
        self.assertEqual(result["seed"]["id"], 2)
        self.assertEqual(store.get_by_ids([2])[0]["strength"], 2)

    def test_replace_seed_engine_merges_into_live(self):
        w = _seed(1, ["猫", "犬"], content="猫が好き", project=None,
                  evidences=[{"life": 1, "source": "user", "span": "猫が好き", "ledger_id": None,
                              "turn": {"user_text": "猫が好き 猫犬", "assistant_text": ""}}])
        x = _seed(2, ["猫", "犬", "鳥"], content="まるで違う文章", project=None)
        self.store([w, x])
        eng = self.engine()
        eng.record_turn("猫が好き 猫犬", "はい", session_id="s")
        ledger_id = eng.ledger.tail(1)[0]["id"]
        r = eng.replace_seed(1, "太郎", "言い直し", [], ["猫が好き"],
                             {"content": "猫が好き", "conditions": ["猫", "犬"], "kind": "所在", "polarity": "無記",
                              "spans": ["猫が好き"]},
                             window_ledger_ids=[ledger_id])
        self.assertEqual(r["withdrawn"]["replaced_by"], 2)
        self.assertEqual(r["replacement"]["seed_id"], 2)
        self.assertFalse(r["replacement"]["is_new"])

    def test_explicit_target_into_withdrawn_same_topic_is_shadowed(self):
        for action in ("reinforce", "supersede"):
            with self.subTest(action=action):
                shutil.rmtree(self.state, ignore_errors=True)
                store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None)])
                store.withdraw(1, _record())
                before = copy.deepcopy(store.get_by_ids([1])[0])
                turn, proposal = _propose("猫が好き", ["猫", "犬"], action=action, target_id=1)
                result = store.imprint(turn, proposal)
                self.assertEqual(result["decision"], "取り下げ照合")
                self.assertIn("取り下げ中のため併合候補として扱う", result["reason"])
                self.assertNotIn("new として扱う", result["reason"])
                self.assertEqual(store.get_by_ids([1])[0], before)

    def test_tie_live_first_reinforces(self):
        live = _seed(1, ["猫", "犬"], content="猫が好き", project=None)
        w = _seed(2, ["猫", "犬"], content="猫が好き", project=None)
        store = self.store([live, w])
        store.withdraw(2, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        self.assertEqual(result["seed"]["id"], 1)
        self.assertFalse(result["is_new"])

    def test_tie_withdrawn_first_shadows(self):
        w = _seed(1, ["猫", "犬"], content="猫が好き", project=None)
        live = _seed(2, ["猫", "犬"], content="猫が好き", project=None)
        store = self.store([w, live])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        self.assertEqual(result["decision"], "取り下げ照合")
        self.assertEqual(result["withdrawn_match"], 1)

    def test_two_withdrawn_tie_reports_first(self):
        store = self.store([_seed(1, ["猫", "犬"], content="猫が好き", project=None),
                            _seed(2, ["猫", "犬"], content="猫が好き", project=None)])
        store.withdraw(1, _record())
        store.withdraw(2, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        self.assertEqual(store.imprint(turn, proposal)["withdrawn_match"], 1)

    def test_withdrawn_and_superseded_never_shadows(self):
        w = _seed(1, ["猫", "犬"], content="猫が好き", project=None)
        w["superseded_by"] = 9
        store = self.store([w])
        store.withdraw(1, _record())
        turn, proposal = _propose("猫が好き", ["猫", "犬"])
        result = store.imprint(turn, proposal)
        self.assertTrue(result["is_new"])
        self.assertNotEqual(result["decision"], "取り下げ照合")



class TestWithdrawalReceiptBlock(unittest.TestCase):
    def test_receipt_block_matches_expectations(self):
        import receipt_harness

        block = receipt_harness.run_withdrawal_harness()
        self.assertEqual(block["件数"], 8)
        self.assertEqual(block["一致"], block["件数"])
        for item in block["詳細"]:
            self.assertEqual(set(item), {"label", "expected", "got"})
        with open(os.path.join(receipt_harness.BASE_DIR, "receipts", "deterministic.json"), encoding="utf-8") as f:
            committed = json.load(f)["取り下げ"]
        self.assertEqual(committed, block)


if __name__ == "__main__":
    unittest.main()
