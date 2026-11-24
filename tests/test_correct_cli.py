"""`raiya-correct` (スライス b) の pin: plan §8 の 4, 5, 10〜13。
engine verb (withdraw_seed / replace_seed / restore_seed)、plant_precheck、turns_by_ids、correct_cli。
LLM は使わない。テストは自作の tempfile.mkdtemp だけを作り、それだけを消す (cwd・BASE_DIR は消さない)。
"""
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import config, correct_cli, sansho  # noqa: E402
from raiya.alaya import AlayaStore, WithdrawalRefused, is_withdrawn, plant_precheck  # noqa: E402
from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from tests._state_fixture import alaya_store_at, mutate, table_bodies, table_digest  # noqa: E402
from raiya import storage  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
P = "/home/dev/p"
USER1 = "古い設計書は docs/old にある"
USER2 = "新しい設計書は docs/new にある"


def _seed(seed_id, conditions, evidences, kind="所在", content=None):
    return {
        "id": seed_id, "content": content or "種子{}".format(seed_id), "strength": 1,
        "conditions": list(conditions), "polarity": "無記", "sansho": "圓成實", "kind": kind,
        "project": P, "superseded_by": None, "evidences": evidences, "history": [],
        "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0,
    }


def _digest(path):
    out = {}
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


def _run(argv):
    """correct_cli.main を実行して (code, stdout, stderr) を返す。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = correct_cli.main(argv)
        except SystemExit as exc:  # argparse のエラー
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class _Case(unittest.TestCase):
    """台帳に 2 行 (#1 古い設計書 / #2 新しい設計書)、種子 #1 (台帳 #1 が根拠) と #2 (台帳外の根拠) を持つ state。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="raiya_correct_")
        self.state = os.path.join(self.tmpdir, "state")
        env = {k: v for k, v in os.environ.items() if k not in (INJECT_DISPOSITIONS_ENV, "USER", "LOGNAME", "RAIYA_STATE_DIR")}
        env["RAIYA_CONFIG"] = ""
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        config._reset()
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        eng.record_turn(USER1, "了解", session_id="s", project=P)
        eng.record_turn(USER2, "了解", session_id="s", project=P)
        state = {
            "life": 3, "next_id": 3, "quarantine": [], "next_quarantine_id": 1,
            "seeds": [
                _seed(1, ["docs/old"], [{"life": 1, "source": "user", "span": USER1, "ledger_id": 1}]),
                _seed(2, ["x"], [{"life": 1, "source": "user", "span": "古い引用", "ledger_id": None,
                                  "turn": {"user_text": "古い引用", "assistant_text": ""}}]),
            ],
        }
        self.write_alaya(state)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_alaya(self, state):
        """seeds 表と alaya の meta を `state` (旧 alaya.json の形の dict) で置き換える。台帳は触らない (自作 tmp の DB にだけ使う)。"""
        def go(conn):
            conn.execute("DELETE FROM seeds")
            for seed in state["seeds"]:
                d = storage.seed_derived(seed)
                conn.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (?, ?, ?, ?, ?)",
                             (d[0], d[1], d[2], d[3], storage._dumps(seed)))
            for key, value in (("alaya.life", state["life"]), ("alaya.next_id", state["next_id"]),
                               ("alaya.next_quarantine_id", state["next_quarantine_id"])):
                conn.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(value), key))

        mutate(self.state, go)
        for name in os.listdir(self.state):
            if name.endswith(".bak"):
                os.remove(os.path.join(self.state, name))

    def load_seeds(self):
        return {s["id"]: s for s in (json.loads(b) for b in table_bodies(self.state, "seeds", order="pos"))}

    def engine(self):
        return MemoryEngine(state_dir=self.state, new_life=False)

    def replace_argv(self, **over):
        argv = ["--state-dir", self.state, "replace", "1", "--by", "太郎", "--reason", "古い",
                "--ledger-id", "1", "--window-ledger-id", "2",
                "--new-content", "設計書は docs/new にある", "--new-kind", "所在",
                "--new-condition", "docs/new", "--new-span", USER2]
        return argv

    def scratch_store(self, name):
        d = os.path.join(self.tmpdir, name)
        os.makedirs(d)
        return alaya_store_at(os.path.join(d, "alaya.json"), os.path.join(d, "imprint_log.json"))

    def digest_no_lock(self):
        d = _digest(self.state)
        d.pop(".lock", None)
        return d


class TestTurnsByIds(_Case):
    def test_ascending_and_missing_absent(self):
        eng = self.engine()
        eng.ledger.reload()
        turns = eng.ledger.turns_by_ids([2, 99, 1, 1, True])
        self.assertEqual([t.id for t in turns], [1, 2])
        self.assertEqual(turns[0].user_text, USER1)
        self.assertEqual(eng.ledger.turns_by_ids([]), [])


class TestPlantPrecheck(_Case):
    def _window(self):
        eng = self.engine()
        eng.ledger.reload()
        return eng.ledger.turns_by_ids([2])

    def test_none_for_grounded_proposal_and_message_for_others(self):
        w = self._window()
        ok = {"content": "c", "conditions": ["docs/new"], "kind": "所在", "evidence": [{"source": "", "span": USER2}]}
        self.assertIsNone(plant_precheck(w, ok))
        bad_span = dict(ok, evidence=[{"source": "", "span": "無い引用"}])
        self.assertIsInstance(plant_precheck(w, bad_span), str)
        bad_kind = dict(ok, kind="でたらめ")
        self.assertIn("VALID_KINDS", plant_precheck(w, bad_kind))
        mixed = [dataclass_replace(w[0], user_text="甲 a", project="/a"),
                 dataclass_replace(w[0], user_text="乙 b", project="/b", id=9)]
        cross = {"content": "c", "conditions": ["a", "b"], "kind": "所在",
                 "evidence": [{"source": "", "span": "甲 a"}, {"source": "", "span": "乙 b"}]}
        self.assertIn("project 跨ぎ", plant_precheck(mixed, cross) or "")

    def test_precheck_writes_nothing(self):
        before = self.digest_no_lock()
        plant_precheck(self._window(), {"content": "c", "conditions": [], "evidence": []})
        self.assertEqual(before, self.digest_no_lock())

    def test_matrix_precheck_none_iff_imprint_plants(self):
        w = self._window()
        base = {"content": "設計書は docs/new にある", "conditions": ["docs/new"], "kind": "所在", "polarity": "無記"}
        cases = [
            dict(base, evidence=[{"source": "", "span": USER2}]),
            dict(base, evidence=[{"source": "", "span": "無い引用"}]),
            dict(base, evidence=[]),
            dict(base, evidence=[{"source": "", "span": USER2}], kind="でたらめ"),
            dict(base, evidence=[{"source": "", "span": USER2}], kind="決定"),   # 性向に読み替え (植わる)
            dict(base, evidence=[{"source": "", "span": USER2}], conditions=["無い縁"]),  # 縁が全滅
            dict(base, evidence=[{"source": "", "span": USER2}, {"source": "", "span": "無い"}]),
        ]
        for i, proposal in enumerate(cases):
            store = self.scratch_store("m{}".format(i))
            planted = store.imprint(w, proposal, replacement_for=7)["seed"] is not None
            self.assertEqual(plant_precheck(w, proposal) is None, planted, proposal)


class TestReplaceNotStoppedByRh2(_Case):
    """#129 RH2: 人の置き換え (raiya-correct replace) は伝聞の印つきの assistant の span でも RH2 で止まらない。"""

    HS = "次回は docs/new の見直しから再開する予定だと伝えました。"

    def _plant_resume(self):
        self.engine().record_turn("まとめて", self.HS, session_id="s", project=P)  # ledger #3
        self.write_alaya({"life": 3, "next_id": 4, "quarantine": [], "next_quarantine_id": 1,
                          "seeds": list(self.load_seeds().values()) + [_seed(3, ["docs/new"], [], kind="再開点", content="前の再開点")]})

    def test_real_replace_path_is_not_stopped(self):
        self._plant_resume()
        argv = ["--state-dir", self.state, "replace", "1", "--by", "太郎", "--reason", "古い",
                "--ledger-id", "1", "--window-ledger-id", "3",
                "--new-content", "docs/new の見直しから再開する", "--new-kind", "再開点",
                "--new-condition", "docs/new", "--new-span", self.HS]
        code, out, err = _run(argv)
        self.assertEqual(code, 0, err)
        self.assertIn("(再開点の置換)", out)
        seeds = self.load_seeds()
        new_id = seeds[1]["withdrawn"]["replaced_by"]
        self.assertIsNotNone(new_id)
        self.assertEqual(seeds[new_id]["content"], "docs/new の見直しから再開する")

    def test_same_proposal_without_replacement_for_is_stopped(self):
        from raiya.alaya import RESUME_HEARSAY_RECORD_ONLY
        from raiya.ledger import LedgerTurn
        store = self.scratch_store("rh2")
        store.state["seeds"] = [_seed(3, ["docs/new"], [], kind="再開点", content="前の再開点")]
        store.state["next_id"] = 4
        t = LedgerTurn(user_text="まとめて", assistant_text=self.HS, id=3, project=P)
        prop = {"content": "docs/new の見直しから再開する", "conditions": ["docs/new"], "kind": "再開点", "polarity": "無記",
                "evidence": [{"source": "", "span": self.HS}], "action": "new"}
        r = store.imprint([t], prop)
        self.assertEqual(r["decision"], "reject")
        self.assertIn(RESUME_HEARSAY_RECORD_ONLY, r["reason"])


def dataclass_replace(turn, **kw):
    import dataclasses
    return dataclasses.replace(turn, **kw)


class TestReplacementNote(_Case):
    def test_imprint_replacement_for_appends_note_only(self):
        eng = self.engine()
        eng.ledger.reload()
        turns = eng.ledger.turns_by_ids([2])
        prop = {"content": "c", "conditions": ["docs/new"], "kind": "所在", "evidence": [{"source": "", "span": USER2}]}
        plain = self.scratch_store("n").imprint(turns, prop)
        store2 = self.scratch_store("n2")
        rep = store2.imprint(turns, prop, replacement_for=5)
        self.assertNotIn("置き換え", plain["reason"])
        self.assertTrue(rep["reason"].endswith("置き換え: 取り下げた #5 の代わり (人間の宣言)"))
        self.assertEqual(rep["decision"], plain["decision"])


class TestEngineVerbs(_Case):
    def test_withdraw_then_restore(self):
        eng = self.engine()
        out = eng.withdraw_seed(1, " 太郎 ", " 誤り ", ledger_ids=[1])
        self.assertEqual(out["withdrawn"]["by"], "太郎")
        self.assertEqual(out["withdrawn"]["ledger_ids"], [1])
        self.assertEqual(out["withdrawn"]["life"], 3)
        self.assertTrue(is_withdrawn(self.load_seeds()[1]))
        eng.restore_seed(1, "太郎", "戻す")
        seed = self.load_seeds()[1]
        self.assertFalse(is_withdrawn(seed))
        self.assertEqual([h["reason"] for h in seed["history"]], ["withdrawn", "restored"])

    def test_withdraw_by_span_stores_recorded_span(self):
        out = self.engine().withdraw_seed(2, "太郎", "誤り", spans=["古い引用"])
        self.assertEqual(out["withdrawn"]["spans"], ["古い引用"])
        self.assertEqual(out["withdrawn"]["ledger_ids"], [])

    def test_blank_declaration_is_value_error_and_writes_nothing(self):
        before = self.digest_no_lock()
        for by, reason in (("", "r"), ("  ", "r"), ("b", " "), (None, "r")):
            with self.assertRaises(ValueError):
                self.engine().withdraw_seed(1, by, reason, ledger_ids=[1])
        self.assertEqual(before, self.digest_no_lock())

    def test_replace_is_atomic_and_self_describing(self):
        out = self.engine().replace_seed(
            1, "太郎", "古い", [1], [],
            {"content": "設計書は docs/new にある", "conditions": ["docs/new"], "kind": "所在", "spans": [USER2]},
            window_ledger_ids=[2],
        )
        seeds = self.load_seeds()
        self.assertEqual(out["replacement"]["seed_id"], 3)
        self.assertTrue(out["replacement"]["is_new"])
        self.assertEqual(seeds[1]["withdrawn"]["replaced_by"], 3)
        self.assertEqual(out["withdrawn"]["replaced_by"], 3)
        self.assertEqual(seeds[1]["history"][-1]["withdrawn"]["replaced_by"], 3)
        self.assertEqual(seeds[3]["evidences"][0]["ledger_id"], 2)
        eng = self.engine()
        rows = eng.alaya.list_imprint_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal"]["replaces"], 1)
        self.assertEqual(rows[0]["proposal"]["declared_by"], "太郎")
        self.assertEqual(rows[0]["seed_id"], 3)
        self.assertIn("置き換え: 取り下げた #1 の代わり (人間の宣言)", rows[0]["reason"])

    def test_replace_default_window_is_ledger_ids(self):
        out = self.engine().replace_seed(
            1, "太郎", "古い", [1], [],
            {"content": "古い設計書は docs/old にある", "conditions": ["docs/old"], "kind": "所在", "spans": [USER1]},
        )
        self.assertEqual(out["replacement"]["seed_id"], 3)

    def test_replace_old_workaround_without_correction_demotes(self):
        # 2026-10-29 spec §6-4: replace にも同じ規則。窓に訂正が無ければ 回避策 は黙って 性向 になる (注記は imprint_log に残る)
        out = self.engine().replace_seed(
            1, "太郎", "古い", [1], [],
            {"content": "古い設計書 → docs/new が原因", "conditions": ["docs/new"], "kind": "回避策", "spans": [USER2]},
            window_ledger_ids=[2],
        )
        seeds = self.load_seeds()
        self.assertEqual(seeds[out["replacement"]["seed_id"]]["kind"], "性向")
        rows = self.engine().alaya.list_imprint_log()
        self.assertIn("回避策 にユーザーの訂正の印が無いため 性向 として受理", rows[0]["reason"])

    def test_replace_disposition_with_assert_form_is_not_promoted(self):
        # #117: 人の置き換え (replacement_for) は 性向 → 決定 の昇格をしない (人が選んだ kind を尊重)
        eng = self.engine()
        eng.record_turn("DB は SQLite にした。", "了解", session_id="s", project=P)
        out = eng.replace_seed(
            1, "太郎", "古い", [1], [],
            {"content": "DB は SQLite", "conditions": ["SQLite"], "kind": "性向", "spans": ["DB は SQLite にした。"]},
            window_ledger_ids=[3],
        )
        self.assertEqual(self.load_seeds()[out["replacement"]["seed_id"]]["kind"], "性向")
        rows = self.engine().alaya.list_imprint_log()
        self.assertNotIn("断定の印", rows[0]["reason"])

    def test_replace_with_correction_row_keeps_workaround(self):
        # §6-4: 人は --ledger-id で訂正を含む行を窓に足せば 回避策 が残る
        eng = self.engine()
        eng.record_turn("古い設計書 は docs/old。それは違う、元に戻してください", "了解", session_id="s", project=P)
        out = eng.replace_seed(
            1, "太郎", "古い", [1], [],
            {"content": "古い設計書 → docs/old が原因", "conditions": ["docs/old"], "kind": "回避策",
             "spans": ["古い設計書 は docs/old", "元に戻してください"]},
            window_ledger_ids=[3],
        )
        self.assertEqual(self.load_seeds()[out["replacement"]["seed_id"]]["kind"], "回避策")

    def test_replace_without_window_is_value_error(self):
        with self.assertRaises(ValueError):
            self.engine().replace_seed(2, "太郎", "r", [], ["古い引用"], {"content": "c", "conditions": [], "kind": "所在", "spans": []})

    def test_exception_inside_op_skips_save(self):
        # `_locked` は op が raise したら flush も COMMIT もしない (拒否 = 何も書かない) ことの直接 pin
        from raiya.alaya import AlayaStore
        from raiya.ledger import LedgerStore
        from raiya.manas import ManasStore
        eng = self.engine()
        before = table_digest(self.state)
        flushers = {AlayaStore: mock.Mock(), ManasStore: mock.Mock(), LedgerStore: mock.Mock()}
        with mock.patch.dict(storage._FLUSHERS, flushers):
            with self.assertRaises(WithdrawalRefused):
                eng.withdraw_seed(99, "太郎", "r", ledger_ids=[1])
        for f in flushers.values():
            f.assert_not_called()
        self.assertEqual(table_digest(self.state), before)

class TestRefusalsAreByteIdentical(_Case):
    """pin 4: 拒否 (exit 1) は state dir の全ファイルをバイト同一に保つ (`.lock` を除く)。"""

    def _assert_refused(self, argv, fragment):
        before = self.digest_no_lock()
        code, out, err = _run(["--state-dir", self.state] + argv)
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn(fragment, err)
        self.assertEqual(before, self.digest_no_lock())

    def test_each_refusal(self):
        by = ["--by", "太郎", "--reason", "r"]
        self._assert_refused(["withdraw", "99"] + by + ["--ledger-id", "1"], "種子 #99 は無い")
        self._assert_refused(["withdraw", "1"] + by + ["--ledger-id", "2"], "台帳 #2 は種子 #1 の根拠に無い")
        self._assert_refused(["withdraw", "1"] + by + ["--ledger-id", "77"], "台帳に行 #77 が無い")
        self._assert_refused(["withdraw", "2"] + by + ["--span", "別の引用"], "span は種子 #2")
        self._assert_refused(["withdraw", "1"] + by + ["--span", USER1], "台帳外の根拠に無い")
        self._assert_refused(["restore", "1"] + by, "取り下げられていない")

    def test_already_withdrawn_refused(self):
        self.engine().withdraw_seed(1, "太郎", "r", ledger_ids=[1])
        self._assert_refused(["withdraw", "1", "--by", "b", "--reason", "r", "--ledger-id", "1"], "既に取り下げ済み")

    def test_replace_refusals_write_nothing(self):
        argv = self.replace_argv()[2:]  # --state-dir を外す (_assert_refused が付ける)
        self._assert_refused([a if a != USER2 else "ゲートに無い引用" for a in argv], "ゲートを通らない")
        self._assert_refused([("99" if a == "2" else a) for a in argv], "台帳に行 #99 が無い (置き換えの窓)")
        # U+FFFC を含む span は blank window で拒否 (必ず遍計所執)
        self._assert_refused([a if a != USER2 else "新しい￼設計書" for a in argv], "ゲートを通らない")
        self.assertEqual(self.engine().alaya.list_imprint_log(), [])
        # 取り下げ済み / 無い種子 / 根拠に無い台帳 id (replace 経由でも同じ)
        self._assert_refused([("99" if a == "1" and i == 1 else a) for i, a in enumerate(argv)], "種子 #99 は無い")
        self._assert_refused([("2" if a == "1" and argv[i - 1] == "--ledger-id" else a) for i, a in enumerate(argv)],
                             "台帳 #2 は種子 #1 の根拠に無い")
        self.engine().withdraw_seed(1, "太郎", "r", ledger_ids=[1])
        self.write_alaya({"life": 3, "next_id": 3, "quarantine": [], "next_quarantine_id": 1,
                          "seeds": list(self.load_seeds().values())})
        self._assert_refused(argv, "既に取り下げ済み")


class TestReplaceCli(_Case):
    def test_replace_text_output_and_state(self):
        code, out, err = _run(self.replace_argv())
        self.assertEqual(code, 0, err)
        self.assertIn("取り下げ: #1", out)
        self.assertIn("置き換え: #3 (新規) クラス=圓成實 kind=所在 project=", out)
        self.assertIn("根拠: 台帳 #1", out)
        self.assertNotIn("二度と", out)

    def test_replace_json(self):
        code, out, _ = _run(["--json"] + self.replace_argv())
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["replacement"]["seed_id"], 3)
        self.assertEqual(data["withdrawn"]["replaced_by"], 3)

    def test_withdraw_and_restore_text(self):
        code, out, _ = _run(["--state-dir", self.state, "withdraw", "1", "--by", "太郎", "--reason", "誤り", "--ledger-id", "1"])
        self.assertEqual(code, 0)
        for fragment in ("取り下げ: #1", "判定者: 太郎", "時刻: ", "理由: 誤り", "根拠: 台帳 #1", "生: 3"):
            self.assertIn(fragment, out)
        code, out, _ = _run(["--state-dir", self.state, "restore", "1", "--by", "花子", "--reason", "戻す"])
        self.assertEqual(code, 0)
        self.assertIn("復帰: #1", out)
        self.assertIn("判定者: 花子", out)
        self.assertNotIn("二度と", out)

    def test_refusal_message_is_escaped(self):
        code, _, err = _run(["--state-dir", self.state, "restore", "1", "--by", "b", "--reason", "r"])
        self.assertEqual(code, 1)
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("raiya-correct: "))
        with mock.patch.object(MemoryEngine, "restore_seed", side_effect=WithdrawalRefused("a\x1b[31m\nb")):
            code, _, err = _run(["--state-dir", self.state, "restore", "1", "--by", "b", "--reason", "r"])
        self.assertEqual(code, 1)
        self.assertIn("\\x1b", err)
        self.assertNotIn("\x1b", err)
        self.assertEqual(err.count("\n"), 1)

    def test_text_output_escapes_control_chars(self):
        code, out, _ = _run(["--state-dir", self.state, "withdraw", "1", "--by", "a\x1b[31mb", "--reason", "x\ny",
                             "--ledger-id", "1"])
        self.assertEqual(code, 0)
        self.assertIn("判定者: a\\x1b[31mb", out)
        self.assertIn("理由: x\\x0ay", out)
        self.assertNotIn("\x1b", out)
        self.assertEqual(out.count("\n"), 6)  # 見出し + 判定者/時刻/理由/根拠/生 だけ

    def test_json_output_equals_verb_return(self):
        code, out, _ = _run(["--json", "--state-dir", self.state, "withdraw", "1", "--by", "b", "--reason", "r", "--ledger-id", "1"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["withdrawn"], self.load_seeds()[1]["withdrawn"])
        self.assertEqual(data["seed_id"], 1)


class TestReplaceVariants(_Case):
    def _plant(self, seed):
        state = {"life": 3, "next_id": 4, "quarantine": [], "next_quarantine_id": 1,
                 "seeds": list(self.load_seeds().values()) + [seed]}
        self.write_alaya(state)

    def test_replace_merge_label_and_replaced_by(self):
        self._plant(_seed(3, ["docs/new"], [], content="設計書は docs/new にある"))
        code, out, err = _run(self.replace_argv())
        self.assertEqual(code, 0, err)
        self.assertIn("置き換え: #3 (併合)", out)
        seeds = self.load_seeds()
        self.assertEqual(seeds[1]["withdrawn"]["replaced_by"], 3)
        self.assertEqual(sorted(seeds), [1, 2, 3])

    def test_replace_resume_point_supersedes_live_one(self):
        self._plant(_seed(3, ["docs/new"], [], kind="再開点", content="前の再開点"))
        argv = [("再開点" if a == "所在" else a) for a in self.replace_argv()]
        code, out, err = _run(argv)
        self.assertEqual(code, 0, err)
        self.assertIn("置き換え: #3 (再開点の置換)", out)
        seeds = self.load_seeds()
        self.assertEqual(seeds[1]["withdrawn"]["replaced_by"], 3)
        self.assertEqual(seeds[3]["content"], "設計書は docs/new にある")

    def test_replace_resume_point_without_other_is_new(self):
        argv = [("再開点" if a == "所在" else a) for a in self.replace_argv()]
        code, out, err = _run(argv)
        self.assertEqual(code, 0, err)
        self.assertIn("置き換え: #3 (新規)", out)

    def test_cross_project_window_refused_without_writing(self):
        self.engine().record_turn("別件の話 foo", "了解", session_id="s", project="/other")
        for name in os.listdir(self.state):
            if name.endswith(".bak"):
                os.remove(os.path.join(self.state, name))
        argv = self.replace_argv()[2:]
        argv = [a for a in argv]
        i = argv.index("--new-span")
        argv = argv[:i] + ["--new-span", USER2, "--new-span", "別件の話 foo", "--new-condition", "foo",
                           "--window-ledger-id", "3"]
        before = self.digest_no_lock()
        code, _, err = _run(["--state-dir", self.state] + argv)
        self.assertEqual(code, 1, err)
        self.assertIn("project 跨ぎ", err)
        self.assertEqual(before, self.digest_no_lock())


class TestReplaceFailureAndWindowValidation(_Case):
    PROPOSAL = {"content": "設計書は docs/new にある", "conditions": ["docs/new"], "kind": "所在", "spans": [USER2]}

    def test_replace_failure_after_imprint(self):
        # 非正準化は setUp 済み。imprint の後 (set_replaced_by) で失敗しても全ファイルが同一 (.bak も)
        eng = self.engine()
        eng.record_turn("x", "y", project=P)  # 台帳 meta を保存済みの形にしておく (以降 digest を取る)
        before = self.digest_no_lock()
        with mock.patch.object(AlayaStore, "set_replaced_by", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                eng.replace_seed(1, "太郎", "r", [1], [], dict(self.PROPOSAL), window_ledger_ids=[2])
        self.assertEqual(before, self.digest_no_lock())
        self.assertFalse(is_withdrawn(self.load_seeds()[1]))
        self.assertEqual(sorted(self.load_seeds()), [1, 2])

    def test_replace_window_ids_rejects_non_int(self):
        before = self.digest_no_lock()
        for bad in ([True], ["2"], [2, None]):
            with self.assertRaises(ValueError):
                self.engine().replace_seed(1, "太郎", "r", [1], [], dict(self.PROPOSAL), window_ledger_ids=bad)
        self.assertEqual(before, self.digest_no_lock())


class TestExitCodesAndNoFallback(_Case):
    def test_usage_errors_exit_2_without_building_engine(self):
        before = self.digest_no_lock()
        base = ["--state-dir", self.state]
        cases = [
            ["withdraw", "1", "--reason", "r", "--ledger-id", "1"],                  # --by 無し (argparse)
            ["withdraw", "1", "--by", "  ", "--reason", "r", "--ledger-id", "1"],     # 空の --by
            ["withdraw", "1", "--by", "b", "--reason", " ", "--ledger-id", "1"],      # 空の --reason
            ["withdraw", "1", "--by", "b", "--reason", "r"],                          # 根拠フラグ無し
            ["withdraw", "x", "--by", "b", "--reason", "r", "--ledger-id", "1"],      # SEED_ID が int でない
            ["replace", "2", "--by", "b", "--reason", "r", "--span", "古い引用",
             "--new-content", "c", "--new-kind", "所在", "--new-condition", "w", "--new-span", "s"],  # 窓 id 無し
            ["replace", "1", "--by", "b", "--reason", "r", "--ledger-id", "1",
             "--new-content", "c", "--new-condition", "w", "--new-span", "s"],        # --new-kind 無し
        ]
        for argv in cases:
            with mock.patch.object(correct_cli, "MemoryEngine", side_effect=AssertionError("engine を作ってはならない")):
                code, _, _ = _run(base + argv)
            self.assertEqual(code, 2, argv)
        self.assertEqual(before, self.digest_no_lock())

    def test_no_raiya_db_exit_2_and_nothing_created(self):
        empty = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty)
        code, _, err = _run(["--state-dir", empty, "restore", "1", "--by", "b", "--reason", "r"])
        self.assertEqual(code, 2)
        self.assertIn("raiya.db が無い", err)
        self.assertEqual(os.listdir(empty), [])

    def test_by_has_no_env_fallback(self):
        with mock.patch.dict(os.environ, {"USER": "taro", "LOGNAME": "taro"}):
            code, _, _ = _run(["--state-dir", self.state, "withdraw", "1", "--reason", "r", "--ledger-id", "1"])
        self.assertEqual(code, 2)
        self.assertFalse(is_withdrawn(self.load_seeds()[1]))

    def test_by_is_stripped_and_stored_verbatim(self):
        _run(["--state-dir", self.state, "withdraw", "1", "--by", "  Taro K  ", "--reason", " r ", "--ledger-id", "1"])
        record = self.load_seeds()[1]["withdrawn"]
        self.assertEqual((record["by"], record["reason"]), ("Taro K", "r"))

    def test_exit_zero_on_success(self):
        code, _, _ = _run(["--state-dir", self.state, "withdraw", "1", "--by", "b", "--reason", "r", "--ledger-id", "1"])
        self.assertEqual(code, 0)


class TestNoLogFiles(_Case):
    def test_no_hooks_or_debug_log_after_all_verbs(self):
        by = ["--by", "太郎", "--reason", "r"]
        _run(["--state-dir", self.state, "withdraw", "2"] + by + ["--span", "古い引用"])
        _run(["--state-dir", self.state, "restore", "2"] + by)
        _run(self.replace_argv())
        _run(["--state-dir", self.state, "withdraw", "99"] + by + ["--ledger-id", "1"])
        for name in ("hooks.log", "debug.log", "debug.log.1"):
            self.assertFalse(os.path.exists(os.path.join(self.state, name)), name)


class TestCorrectCliNeverLoadsBackend(_Case):
    def test_source_has_no_backend_import_anywhere(self):
        with open(os.path.join(REPO_ROOT, "raiya", "correct_cli.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(a.name == "backend" or a.name.endswith(".backend") for a in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").split(".")[-1] == "backend")
                self.assertFalse(any(a.name == "backend" for a in node.names))

    def test_backend_not_in_sys_modules_after_all_subcommands(self):
        code = (
            "import sys, contextlib, io\n"
            "from raiya import correct_cli\n"
            "s = sys.argv[1]\n"
            "by = ['--by', 'x', '--reason', 'r']\n"
            "with contextlib.redirect_stdout(io.StringIO()):\n"
            "    assert correct_cli.main(['--state-dir', s, 'withdraw', '1'] + by + ['--ledger-id', '1']) == 0\n"
            "    assert correct_cli.main(['--state-dir', s, 'restore', '1'] + by) == 0\n"
            "    assert correct_cli.main(['--state-dir', s, 'replace', '1'] + by + ['--ledger-id', '1', '--window-ledger-id', '2',\n"
            "        '--new-content', '設計書は docs/new にある', '--new-kind', '所在', '--new-condition', 'docs/new',\n"
            "        '--new-span', sys.argv[2]]) == 0\n"
            "print('raiya.backend' in sys.modules)\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "RAIYA_STATE_DIR"}
        env["RAIYA_CONFIG"] = ""
        result = subprocess.run([sys.executable, "-c", code, self.state, USER2], capture_output=True, text=True,
                                cwd=REPO_ROOT, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class TestMcpUntouched(unittest.TestCase):
    def _tree(self):
        with open(os.path.join(REPO_ROOT, "raiya", "mcp_server.py"), encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_tools_are_the_existing_five(self):
        names = []
        for node in self._tree().body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets):
                for elt in node.value.elts:
                    for key, val in zip(elt.keys, elt.values):
                        if isinstance(key, ast.Constant) and key.value == "name":
                            names.append(val.value)
        self.assertEqual(names, ["memory_recall", "memory_imprint", "memory_audit",
                                 "memory_introspect_material", "memory_record_introspection"])

    def test_mcp_server_does_not_import_correct_cli(self):
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                self.assertFalse(any("correct_cli" in a.name for a in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("correct_cli", node.module or "")
                self.assertFalse(any(a.name == "correct_cli" for a in node.names))


class TestPyprojectEntry(unittest.TestCase):
    def test_script_registered(self):
        with open(os.path.join(REPO_ROOT, "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn('raiya-correct = "raiya.correct_cli:main"', f.read())


if __name__ == "__main__":
    unittest.main()
