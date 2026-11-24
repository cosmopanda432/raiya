"""#101 裁定 B・D・E の決定論 pin (spec docs/superpowers/specs/2026-10-30-absent-response-seeding-design.md §6)。

植え付け・置換の権利は class の順位でなく user の逐語 span の有無で決める:
B 再開点 の新規は 圓成實 のみ / D user span 0 本の提案は user span を持つ種子を supersede・reinforce できない /
E 決定・回避策 に限り、形を担う span が 圓成實 なら 依他起 の提案が 圓成實 の同 kind を supersede・reinforce できる。
LLM は使わない。テストが消すのは自作 tmp だけ。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
OTHER = "/home/dev/cosmicanalysis"


def _turn(user_text, assistant_text="", project=PROJ, ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=project)


def _prop(content, conditions, spans, kind, action=None, target_id=None):
    """spans = [(source, span)]。source は自称で、判定は窓の逐語から導かれる。"""
    p = {
        "content": content,
        "conditions": list(conditions),
        "polarity": "無記",
        "kind": kind,
        "evidence": [{"source": s, "span": t} for s, t in spans],
    }
    if action is not None:
        p["action"] = action
        p["target_id"] = target_id
    return p


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(
            os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json")
        )
        self.store.begin_new_life()

    def seed(self, sid):
        return self.store.get_by_ids([sid])[0]

    def precheck(self, window, proposal, seeds=True):
        kw = {"seeds": self.store.list_seeds()} if seeds else {}
        return alaya_mod.plant_precheck(window, proposal, **kw)


class TestResumePointNewIsEnjoujitsuOnly(_Case):
    """B"""

    def test_enjoujitsu_new_resume_point_is_planted(self):
        w = [_turn("次 = 裁定待ち 裁定")]
        p = _prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点")
        self.assertIsNone(self.precheck(w, p))
        r = self.store.imprint(w, p)
        self.assertTrue(r["is_new"])
        self.assertEqual(r["seed"]["sansho"], "圓成實")

    def test_etaki_new_resume_point_is_rejected(self):
        w = [_turn("質問 続き", "次 = 推定の続き 続き")]
        p = _prop("次 = 推定の続き", ["続き"], [("assistant", "次 = 推定の続き")], "再開点")
        self.assertIn("再開点 の新規は 圓成實 のみ", self.precheck(w, p))
        r = self.store.imprint(w, p)
        self.assertEqual(r["decision"], "reject")
        self.assertIsNone(r["seed"])
        self.assertIn("再開点 の新規は 圓成實 のみ", r["reason"])
        self.assertIn("再開点 の新規は 圓成實 のみ", self.store.list_imprint_log()[-1]["reason"])
        self.assertEqual(self.store.list_seeds(), [])
        self.assertEqual(self.store.list_imprint_log()[-1]["decision"], "reject")

    def test_etaki_resume_point_still_supersedes_existing(self):
        r1 = self.store.imprint([_turn("次 = 裁定待ち 裁定")],
                                _prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点"))
        w = [_turn("質問 続き", "次 = 推定の続き 続き")]
        p = _prop("次 = 推定の続き", ["続き"], [("assistant", "次 = 推定の続き")], "再開点")
        self.assertIsNone(self.precheck(w, p))  # 既存があるので supersede = 植わる
        r2 = self.store.imprint(w, p)
        self.assertTrue(r2.get("superseded"))
        self.assertEqual(r2["seed"]["id"], r1["seed"]["id"])
        self.assertEqual(r2["seed"]["sansho"], "依他起")

    def test_etaki_resume_point_new_in_other_project_is_rejected(self):
        self.store.imprint([_turn("次 = 古事記 古事記", project=OTHER)],
                           _prop("次 = 古事記", ["古事記"], [("user", "次 = 古事記")], "再開点"))
        w = [_turn("質問 続き", "次 = 推定の続き 続き")]
        p = _prop("次 = 推定の続き", ["続き"], [("assistant", "次 = 推定の続き")], "再開点")
        self.assertEqual(self.store.imprint(w, p)["decision"], "reject")
        self.assertIsNotNone(self.precheck(w, p))

    def test_withdrawn_resume_point_is_not_a_target_so_etaki_new_is_rejected(self):
        r1 = self.store.imprint([_turn("次 = 裁定待ち 裁定")],
                                _prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点"))
        self.store.withdraw(r1["seed"]["id"], {"by": "t", "at": "x", "reason": "r"})
        w = [_turn("質問 続き", "次 = 推定の続き 続き")]
        p = _prop("次 = 推定の続き", ["続き"], [("assistant", "次 = 推定の続き")], "再開点")
        self.assertIsNotNone(self.precheck(w, p))
        self.assertEqual(self.store.imprint(w, p)["decision"], "reject")


class TestNoUserSpanCannotTouchUserSeed(_Case):
    """D"""

    def _mixed_etaki_seed(self):
        """user の逐語 + assistant の言い直しの混在 = 依他起、evidences に user span を持つ種子。"""
        w = [_turn("DB は PostgreSQL に決めました 決定", "PostgreSQL 16 で進めます 決定")]
        p = _prop("DB は PostgreSQL 16", ["決定"],
                  [("user", "DB は PostgreSQL に決めました"), ("assistant", "PostgreSQL 16 で進めます")], "性向")
        r = self.store.imprint(w, p)
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.assertTrue(any(e["source"] == "user" for e in r["seed"]["evidences"]))
        return r["seed"]["id"]

    def _assistant_only(self, **kw):
        # 縁・本文を元の種子と共有しない: 共有すると fall-to-new の類似度併合が元の種子へ戻してしまう (裁定は「new に倒す」)
        w = [_turn("質問 確定", "確定していない、と扱うのが妥当です 確定")]
        p = _prop("DB は未確定", ["確定"], [("assistant", "確定していない、と扱うのが妥当です")], "性向", **kw)
        return w, p

    def test_assistant_only_cannot_supersede_seed_with_user_span(self):
        sid = self._mixed_etaki_seed()
        w, p = self._assistant_only(action="supersede", target_id=sid)
        r = self.store.imprint(w, p)
        self.assertTrue(r["is_new"])
        self.assertIn("supersede 不可: user の逐語を持つ種子は assistant だけの提案では置換できない", r["reason"])
        self.assertEqual(self.seed(sid)["content"], "DB は PostgreSQL 16")
        self.assertEqual(self.seed(sid)["history"], [])

    def test_assistant_only_cannot_reinforce_seed_with_user_span(self):
        sid = self._mixed_etaki_seed()
        before = self.seed(sid)["strength"]
        w, p = self._assistant_only(action="reinforce", target_id=sid)
        r = self.store.imprint(w, p)
        # 2026-10-30 裁定: 同クラスの D も記録のみ (旧: note して new に倒す)
        self.assertEqual(r["user_span_match"], sid)
        self.assertIsNone(r["seed"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.seed(sid)["strength"], before)

    def test_assistant_only_can_supersede_assistant_only_seed(self):
        w1 = [_turn("質問 決定", "仮に SQLite とします 決定")]
        r1 = self.store.imprint(w1, _prop("DB は SQLite(推定)", ["決定"], [("assistant", "仮に SQLite とします")], "性向"))
        w, p = self._assistant_only(action="supersede", target_id=r1["seed"]["id"])
        r2 = self.store.imprint(w, p)
        self.assertTrue(r2.get("superseded"))
        self.assertEqual(self.seed(r1["seed"]["id"])["content"], "DB は未確定")

    def test_assistant_only_can_reinforce_assistant_only_seed(self):
        w1 = [_turn("質問 決定", "仮に SQLite とします 決定")]
        r1 = self.store.imprint(w1, _prop("DB は SQLite(推定)", ["決定"], [("assistant", "仮に SQLite とします")], "性向"))
        w2 = [_turn("質問 決定", "やはり SQLite とします 決定")]
        r2 = self.store.imprint(w2, _prop("DB は SQLite(推定)", ["決定"], [("assistant", "やはり SQLite とします")], "性向",
                                          action="reinforce", target_id=r1["seed"]["id"]))
        self.assertTrue(r2.get("reinforced"))
        self.assertEqual(self.seed(r1["seed"]["id"])["strength"], 2)

    def test_mixed_proposal_can_still_supersede_seed_with_user_span(self):
        sid = self._mixed_etaki_seed()
        w = [_turn("やはり MySQL です 決定", "MySQL に切り替えます 決定")]
        p = _prop("DB は MySQL", ["決定"], [("user", "やはり MySQL です"), ("assistant", "MySQL に切り替えます")],
                  "性向", action="supersede", target_id=sid)
        self.assertTrue(self.store.imprint(w, p).get("superseded"))

    def test_resume_point_special_case_is_exempt(self):
        r1 = self.store.imprint([_turn("次 = 裁定待ち 裁定")],
                                _prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点"))
        r2 = self.store.imprint([_turn("質問 続き", "次 = 推定の続き 続き")],
                                _prop("次 = 推定の続き", ["続き"], [("assistant", "次 = 推定の続き")], "再開点"))
        self.assertTrue(r2.get("superseded"))
        self.assertEqual(r2["seed"]["id"], r1["seed"]["id"])

    def test_target_without_source_field_does_not_trigger_d(self):
        w1 = [_turn("質問 決定", "仮に SQLite とします 決定")]
        r1 = self.store.imprint(w1, _prop("DB は SQLite(推定)", ["決定"], [("assistant", "仮に SQLite とします")], "性向"))
        for e in self.store.state["seeds"][0]["evidences"]:
            e.pop("source", None)
        w, p = self._assistant_only(action="supersede", target_id=r1["seed"]["id"])
        self.assertTrue(self.store.imprint(w, p).get("superseded"))


class TestFormBearingUserSpanMayCrossClass(_Case):
    """E"""

    def _decision_seed(self):
        w = [_turn("epoch 秒ではなく ISO 8601 を使う 時刻")]
        r = self.store.imprint(w, _prop("epoch 秒ではなく ISO 8601", ["時刻"],
                                        [("user", "epoch 秒ではなく ISO 8601 を使う")], "決定"))
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("圓成實", "決定"))
        return r["seed"]["id"]

    def _mixed_decision(self, **kw):
        w = [_turn("ISO 8601 ではなく RFC 3339 に統一 時刻", "RFC 3339 で統一します 時刻")]
        p = _prop("ISO 8601 ではなく RFC 3339", ["時刻"],
                  [("user", "ISO 8601 ではなく RFC 3339 に統一"), ("assistant", "RFC 3339 で統一します")], "決定", **kw)
        return w, p

    def test_rpi4_a1_shape_supersedes_instead_of_second_seed(self):
        sid = self._decision_seed()
        w, p = self._mixed_decision(action="supersede", target_id=sid)
        self.assertIsNone(self.precheck(w, p))
        r = self.store.imprint(w, p)
        self.assertTrue(r.get("superseded"))
        self.assertFalse(r["is_new"])
        self.assertEqual(r["seed"]["id"], sid)
        self.assertEqual(len(self.store.list_seeds()), 1)
        seed = self.seed(sid)
        self.assertEqual(seed["content"], "ISO 8601 ではなく RFC 3339")
        self.assertEqual(seed["sansho"], "依他起")
        self.assertEqual(seed["history"][0]["content"], "epoch 秒ではなく ISO 8601")
        self.assertEqual(seed["history"][0]["sansho"], "圓成實")
        self.assertNotIn("越境", r["reason"])

    def test_mixed_decision_reinforce_keeps_class_and_bumps_strength(self):
        sid = self._decision_seed()
        w, p = self._mixed_decision(action="reinforce", target_id=sid)
        r = self.store.imprint(w, p)
        self.assertTrue(r.get("reinforced"))
        self.assertFalse(r["is_new"])
        seed = self.seed(sid)
        self.assertEqual(seed["sansho"], "圓成實")
        self.assertEqual(seed["strength"], 2)
        self.assertEqual(seed["content"], "epoch 秒ではなく ISO 8601")
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_decision_marker_only_in_assistant_span_keeps_old_rule(self):
        sid = self._decision_seed()
        w = [_turn("RFC 3339 に統一 時刻", "ISO 8601 ではなく RFC 3339 で統一します 時刻")]
        for action, word in (("supersede", "supersede 不可"), ("reinforce", "reinforce 不可")):
            p = _prop("ISO 8601 ではなく RFC 3339", ["時刻"],
                      [("user", "RFC 3339 に統一"), ("assistant", "ISO 8601 ではなく RFC 3339 で統一します")],
                      "決定", action=action, target_id=sid)
            self.assertIsNone(self.precheck(w, p))  # 決定 の形は assistant の span でも通る(従来どおり)
            r = self.store.imprint(w, p)
            self.assertNotEqual(r["seed"]["id"], sid, action)  # 2 周目は 1 周目の新規 依他起 へ併合されうる
            self.assertIn(word, r["reason"])
            self.assertIn("越境", r["reason"])
        self.assertEqual(self.seed(sid)["content"], "epoch 秒ではなく ISO 8601")
        self.assertEqual(self.seed(sid)["strength"], 1)

    def test_other_kinds_keep_old_rule(self):
        w1 = [_turn("猫が好き 猫")]
        r1 = self.store.imprint(w1, _prop("猫が好き", ["猫"], [("user", "猫が好き")], "性向"))
        w = [_turn("猫 を見た 猫", "犬が好きなのでしょう 猫")]
        p = _prop("犬が好き", ["猫"], [("user", "猫 を見た"), ("assistant", "犬が好きなのでしょう")], "性向",
                  action="supersede", target_id=r1["seed"]["id"])
        r = self.store.imprint(w, p)
        self.assertTrue(r["is_new"])
        self.assertIn("越境", r["reason"])

    def _workaround_seed(self):
        w = [_turn("docs/old を直接書き換えた。元に戻してください")]
        p = _prop("docs/old を直接書き換えた → 壊れたため元に戻した", ["docs/old"],
                  [("user", "docs/old を直接書き換えた"), ("user", "元に戻してください")], "回避策")
        r = self.store.imprint(w, p)
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("圓成實", "回避策"))
        return r["seed"]["id"]

    def _mixed_workaround(self, **kw):
        w = [_turn("質問 docs/old", "docs/old を生成物に書き換えました"), _turn("元に戻してください docs/old")]
        p = _prop("docs/old を生成物に書き換えた → 壊れたため元に戻した", ["docs/old"],
                  [("assistant", "docs/old を生成物に書き換えました"), ("user", "元に戻してください")], "回避策", **kw)
        return w, p

    def test_mixed_workaround_with_user_correction_pair_supersedes(self):
        sid = self._workaround_seed()
        w, p = self._mixed_workaround(action="supersede", target_id=sid)
        self.assertIsNone(self.precheck(w, p))
        r = self.store.imprint(w, p)
        self.assertEqual(r["kind"], "回避策")
        self.assertTrue(r.get("superseded"))
        self.assertEqual(self.seed(sid)["sansho"], "依他起")
        self.assertEqual(len(self.seed(sid)["history"]), 1)
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_mixed_workaround_reinforce_keeps_class(self):
        sid = self._workaround_seed()
        w, p = self._mixed_workaround(action="reinforce", target_id=sid)
        r = self.store.imprint(w, p)
        self.assertTrue(r.get("reinforced"))
        self.assertEqual(self.seed(sid)["sansho"], "圓成實")
        self.assertEqual(self.seed(sid)["strength"], 3)  # 1 + 接地した往復 2

    def test_workaround_without_user_correction_falls_to_disposition_and_old_rule(self):
        sid = self._workaround_seed()
        w = [_turn("質問 docs/old", "docs/old を書き換えました。元に戻してください docs/old")]
        p = _prop("docs/old を書き換えた → 壊れたため元に戻した", ["docs/old"],
                  [("assistant", "docs/old を書き換えました。元に戻してください")], "回避策",
                  action="supersede", target_id=sid)
        r = self.store.imprint(w, p)
        self.assertEqual(r["kind"], "性向")
        self.assertTrue(r["is_new"])
        self.assertEqual(self.seed(sid)["sansho"], "圓成實")

    def test_cross_supersede_still_cannot_hide_enjoujitsu(self):
        """横断 supersede の越境 (依他起は圓成實を隠せない) は E でも変わらない。"""
        sid_a = self._decision_seed()
        w = [_turn("X を採用し Y は却下 採用", "了解、Z で進めます 採用")]
        r_old = self.store.imprint(w, _prop("X を採用し Y は却下", ["時刻", "採用"],
                                            [("user", "X を採用し Y は却下"), ("assistant", "Z で進めます")],
                                            "決定"))
        self.assertEqual(r_old["seed"]["sansho"], "依他起")
        w2 = [_turn("ISO 8601 ではなく RFC 3339 に統一 時刻 採用", "RFC 3339 で統一します 時刻 採用")]
        p = _prop("ISO 8601 ではなく RFC 3339", ["時刻", "採用"],
                  [("user", "ISO 8601 ではなく RFC 3339 に統一"), ("assistant", "RFC 3339 で統一します")], "決定",
                  action="supersede", target_id=r_old["seed"]["id"])
        r = self.store.imprint(w2, p)
        self.assertTrue(r.get("superseded"))
        self.assertIsNone(self.seed(sid_a).get("superseded_by"))


class TestPrecheckSeedsArgument(_Case):
    def test_exclude_id_and_none(self):
        self.store.imprint([_turn("次 = 裁定待ち 裁定")], _prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点"))
        w = [_turn("質問 続き", "次 = 推定の続き 続き")]
        p = _prop("次 = 推定の続き", ["続き"], [("assistant", "次 = 推定の続き")], "再開点")
        seeds = self.store.list_seeds()
        self.assertIn("再開点 の新規は 圓成實 のみ", alaya_mod.plant_precheck(w, p, seeds=seeds, exclude_id=1))
        self.assertIsNone(alaya_mod.plant_precheck(w, p, seeds=seeds, exclude_id=None))
        self.assertIsNone(alaya_mod.plant_precheck(w, p))


class TestSelfReportDoesNotLiftRights(_Case):
    """自称 (source / sansho 欄) は D を外さない。判定は窓の逐語から導く。"""

    _mixed_etaki_seed = TestNoUserSpanCannotTouchUserSeed._mixed_etaki_seed

    def _liar(self, content="DB は未確定", conditions=("確定",), **kw):
        w = [_turn("質問 確定", "確定していない、と扱うのが妥当です 確定")]
        p = _prop(content, conditions, [("user", "確定していない、と扱うのが妥当です")], "性向", **kw)
        p["sansho"] = "圓成實"
        return w, p

    def test_label_does_not_lift_supersede(self):
        sid = self._mixed_etaki_seed()
        w, p = self._liar(action="supersede", target_id=sid)
        r = self.store.imprint(w, p)
        self.assertEqual(r["sansho"], "依他起")
        self.assertTrue(r["is_new"])
        self.assertIn("supersede 不可: user の逐語を持つ種子は assistant だけの提案では置換できない", r["reason"])
        self.assertEqual(self.seed(sid)["content"], "DB は PostgreSQL 16")
        self.assertEqual(self.seed(sid)["history"], [])

    def test_label_does_not_lift_reinforce(self):
        sid = self._mixed_etaki_seed()
        before = self.seed(sid)["strength"]
        w, p = self._liar(action="reinforce", target_id=sid)
        r = self.store.imprint(w, p)
        self.assertEqual(r["user_span_match"], sid)  # 2026-10-30 裁定: 同クラスの D も記録のみ
        self.assertIsNone(r["seed"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.seed(sid)["strength"], before)

    def test_label_with_overlapping_content_is_recorded_only(self):
        sid = self._mixed_etaki_seed()
        before = self.seed(sid)["strength"]
        for action in ("reinforce", "supersede", None):
            kw = {} if action is None else {"action": action, "target_id": sid}
            w = [_turn("質問 決定", "PostgreSQL 16 で進めます 決定")]
            p = _prop("DB は PostgreSQL 16", ["決定"], [("user", "PostgreSQL 16 で進めます")], "性向", **kw)
            p["sansho"] = "圓成實"
            r = self.store.imprint(w, p)
            self.assertIsNone(r["seed"], action)
            self.assertIsNone(r["is_new"], action)
            self.assertEqual(r["user_span_match"], sid, action)
            self.assertNotIn("reinforced", r)
            self.assertIn("記録のみ、強度は変えない", r["reason"])
            self.assertEqual(self.seed(sid)["strength"], before)
            self.assertEqual(len(self.store.list_seeds()), 1)
            row = self.store.list_imprint_log()[-1]
            self.assertEqual(row["seed_id"], sid)
            self.assertEqual(row["decision"], "依他起")
            self.assertIn("記録のみ、強度は変えない", row["reason"])


class TestSelfReportDoesNotEnableCross(_Case):
    _decision_seed = TestFormBearingUserSpanMayCrossClass._decision_seed

    def test_label_does_not_enable_e_for_enjoujitsu_decision(self):
        sid = self._decision_seed()
        w = [_turn("RFC 3339 に統一 時刻", "ISO 8601 ではなく RFC 3339 で統一します 時刻")]
        p = _prop("ISO 8601 ではなく RFC 3339", ["時刻"],
                  [("user", "ISO 8601 ではなく RFC 3339 で統一します")], "決定", action="supersede", target_id=sid)
        p["sansho"] = "圓成實"
        r = self.store.imprint(w, p)
        self.assertEqual(r["sansho"], "依他起")
        self.assertNotEqual(r["seed"]["id"], sid)
        self.assertIn("越境", r["reason"])
        self.assertFalse(r.get("superseded"))
        self.assertEqual(self.seed(sid)["content"], "epoch 秒ではなく ISO 8601")
        self.assertEqual(self.seed(sid)["sansho"], "圓成實")

    def test_label_does_not_enable_e_for_mixed_with_marker_only_in_assistant_span(self):
        sid = self._decision_seed()
        w = [_turn("RFC 3339 に統一 時刻", "ISO 8601 ではなく RFC 3339 で統一します 時刻")]
        p = _prop("ISO 8601 ではなく RFC 3339", ["時刻"],
                  [("user", "RFC 3339 に統一"), ("user", "ISO 8601 ではなく RFC 3339 で統一します")], "決定",
                  action="supersede", target_id=sid)
        p["sansho"] = "圓成實"
        r = self.store.imprint(w, p)
        self.assertIn("越境", r["reason"])
        self.assertFalse(r.get("superseded"))
        self.assertEqual(self.seed(sid)["content"], "epoch 秒ではなく ISO 8601")


class TestHumanReplacementExemptFromUserSpanMatch(_Case):
    """merge 経路の D 記録のみは replacement_for (人間の置き換え) を免除する。"""

    def test_replacement_for_skips_user_span_match(self):
        w = [_turn("ログは logs/app にあります logs/app", "ログは logs/app に置きます logs/app", ledger_id=1)]
        r1 = self.store.imprint(w, _prop("ログは logs/app にある", ["logs/app"],
                                         [("user", "ログは logs/app にあります"), ("assistant", "ログは logs/app に置きます")],
                                         "所在"))
        sid = r1["seed"]["id"]
        self.assertTrue(any(e["source"] == "user" for e in self.seed(sid)["evidences"]))
        w2 = [_turn("どこ? logs/app", "ログは logs/app に置きます logs/app", ledger_id=2)]
        p2 = _prop("ログは logs/app にある", ["logs/app"], [("assistant", "ログは logs/app に置きます")], "所在")
        r = self.store.imprint(w2, p2, replacement_for=99)
        self.assertNotIn("user_span_match", r)
        self.assertIs(r["reinforced"], True)
        self.assertEqual(self.seed(sid)["strength"], 2)

    def test_human_replacement_merging_into_user_span_seed_is_reinforced(self):
        from raiya.engine import MemoryEngine

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        eng = MemoryEngine(state_dir=tmp, new_life=True)
        eng.record_turn("ログは logs/app にあります logs/app", "了解、ログは logs/app に置きます logs/app",
                        session_id="s", project=PROJ)
        eng.record_turn("古い場所 old/dir", "了解 old/dir", session_id="s", project=PROJ)
        eng.record_turn("どこ? logs/app", "ログは logs/app に置きます logs/app", session_id="s", project=PROJ)
        eng.imprint(
            [_turn("ログは logs/app にあります logs/app", "了解、ログは logs/app に置きます logs/app", ledger_id=1)],
            [_prop("ログは logs/app にある", ["logs/app"],
                   [("user", "ログは logs/app にあります"), ("assistant", "ログは logs/app に置きます")], "所在")],
        )
        eng.imprint(
            [_turn("古い場所 old/dir", "了解 old/dir", ledger_id=2)],
            [_prop("ログは old/dir にある", ["old/dir"], [("assistant", "了解 old/dir")], "所在")],
        )
        before = eng.alaya.get_by_ids([1])[0]["strength"]
        out = eng.replace_seed(
            2, "太郎", "移動", [2], [],
            {"content": "ログは logs/app にある", "conditions": ["logs/app"], "kind": "所在",
             "spans": ["ログは logs/app に置きます"]},
            window_ledger_ids=[3],
        )
        rep = out["replacement"]
        self.assertEqual(rep["seed_id"], 1)
        self.assertIs(rep["reinforced"], True)
        self.assertIs(rep["is_new"], False)
        after = MemoryEngine(state_dir=tmp, new_life=False).alaya.get_by_ids([1])[0]["strength"]
        self.assertEqual(after, before + 1)


class TestReplaceResumePointRefusedByB(unittest.TestCase):
    def test_assistant_only_replacement_of_only_resume_point_is_refused_and_writes_nothing(self):
        import hashlib
        from raiya.alaya import WithdrawalRefused, is_withdrawn
        from raiya.engine import MemoryEngine

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        eng = MemoryEngine(state_dir=tmp, new_life=True)
        eng.record_turn("次 = 裁定待ち 裁定", "了解", session_id="s", project=PROJ)
        eng.record_turn("質問 続き", "次 = 推定の続き 続き", session_id="s", project=PROJ)
        eng.imprint(
            [LedgerTurn(user_text="次 = 裁定待ち 裁定", assistant_text="了解", id=1, project=PROJ)],
            [_prop("次 = 裁定待ち", ["裁定"], [("user", "次 = 裁定待ち")], "再開点")],
        )
        self.assertTrue(eng.alaya.list_seeds()[0]["kind"] == "再開点")
        db = os.path.join(tmp, "raiya.db")

        def digest():
            with open(db, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        before = digest()
        with self.assertRaises(WithdrawalRefused) as cm:
            eng.replace_seed(
                1, "太郎", "r", [1], [],
                {"content": "次 = 推定の続き", "conditions": ["続き"], "kind": "再開点", "spans": ["次 = 推定の続き"]},
                window_ledger_ids=[2],
            )
        self.assertIn("再開点 の新規は 圓成實 のみ", str(cm.exception))
        self.assertFalse(is_withdrawn(eng.alaya.list_seeds()[0]))
        self.assertEqual(before, digest())


class TestAssistantOnlyReinforceCrossClassIsRecordedOnly(_Case):
    """#105: D は reinforce の越境判定より先に効く (assistant だけの echo は new を植えない)。"""

    def _user_decision(self, project=PROJ):
        w = [_turn("通知アイコンは 🔔 ではなく 📣 を使うと決めました。", project=project)]
        r = self.store.imprint(w, _prop("通知アイコンは 📣 (🔔 ではなく)", ["通知アイコン", "📣"],
                                        [("user", "通知アイコンは 🔔 ではなく 📣 を使うと決めました。")], "決定"))
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("圓成實", "決定"))
        return r["seed"]["id"]

    def _echo(self, sid, project=PROJ):
        w = [_turn("続けて", "📣 を採用し、🔔 は却下 通知アイコン 📣", project=project)]
        p = _prop("通知アイコンは 📣 を採用 (🔔 は却下)", ["通知アイコン", "📣"],
                  [("assistant", "📣 を採用し、🔔 は却下")], "決定", action="reinforce", target_id=sid)
        return w, p

    def test_echo_is_recorded_only(self):
        sid = self._user_decision()
        before = self.seed(sid)["strength"]
        r = self.store.imprint(*self._echo(sid))
        self.assertEqual(r["decision"], "依他起")
        self.assertIsNone(r["seed"])
        self.assertIsNone(r["is_new"])
        self.assertEqual(r["user_span_match"], sid)
        self.assertIn("記録のみ、強度は変えない", r["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.seed(sid)["strength"], before)
        row = self.store.list_imprint_log()[-1]
        self.assertEqual(row["seed_id"], sid)
        self.assertIn("記録のみ、強度は変えない", row["reason"])

    def test_withdrawn_target_keeps_existing_outcome(self):
        sid = self._user_decision()
        self.store.withdraw(sid, {"by": "t", "at": "x", "reason": "r"})
        r = self.store.imprint(*self._echo(sid))
        self.assertNotEqual(r.get("user_span_match"), sid)
        self.assertIn("越境", r["reason"])

    def test_other_project_target_is_not_matched(self):
        sid = self._user_decision(project=OTHER)
        r = self.store.imprint(*self._echo(sid))
        self.assertNotIn("user_span_match", r)
        self.assertIn("越境", r["reason"])
        self.assertTrue(r["is_new"])

    def test_etaki_target_without_user_span_still_reinforces(self):
        w = [_turn("質問 確定", "確定していない、と扱うのが妥当です 確定")]
        r1 = self.store.imprint(w, _prop("DB は未確定", ["確定"],
                                         [("assistant", "確定していない、と扱うのが妥当です")], "性向"))
        sid = r1["seed"]["id"]
        self.assertEqual(r1["seed"]["sansho"], "依他起")
        r = self.store.imprint(w, _prop("DB は未確定", ["確定"],
                                        [("assistant", "確定していない、と扱うのが妥当です")], "性向",
                                        action="reinforce", target_id=sid))
        self.assertTrue(r["reinforced"])
        self.assertEqual(self.seed(sid)["strength"], 2)


if __name__ == "__main__":
    unittest.main()
