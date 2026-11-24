"""不在応答 spec §8: 発芽閾値を 決定・回避策 に限り evidence で決める (`form_in_user_evidence`)。
H (`_resolve_kind`)・E (imprint の越境判定)・閾値が同じ述語を通ること。LLM は使わない。テストが消すのは自作 tmp だけ。
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import engine as engine_mod  # noqa: E402
from raiya import sansho  # noqa: E402
from raiya.alaya import GERMINATION_REASON_STRENGTH, GERMINATION_REASON_TWIN, form_in_user_evidence  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402
from tests.test_withdrawal import _seed, _write_state  # noqa: E402

P = "/home/dev/p"
U_REJECT = {"source": "user", "span": "通知アイコンは 🔔 ではなく 📣 を使う"}
A_REJECT = {"source": "assistant", "span": "通知アイコンは 📣 で進めます ではなく"}
A_PLAIN = {"source": "assistant", "span": "了解しました"}
U_CORR = {"source": "user", "span": "それは違う、元に戻してください"}
A_CORR = {"source": "assistant", "span": "それは違う、元に戻してください"}
U_PLAIN = {"source": "user", "span": "デプロイを手で流した"}


def _etaki(seed_id, kind, evidences, strength=1, conditions=("猫",), sansho_class="依他起"):
    s = _seed(seed_id, conditions, kind=kind, sansho_class=sansho_class, strength=strength, project=P,
              evidences=[dict(e) for e in evidences])
    return s


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.state = os.path.join(self.tmpdir, "state")

    def store(self, seeds):
        _write_state(self.state, seeds, life=1)
        return alaya_store_at(os.path.join(self.state, "alaya.json"), os.path.join(self.state, "imprint_log.json"))

    def germinated_ids(self, seeds, text="猫"):
        return [s["id"] for s in self.store(seeds).germinate(text, project=P)]

    def reason(self, store, seed_id, text="猫"):
        for seed, fired, reason, _m in store._germination_verdicts(text, project=P):
            if seed["id"] == seed_id:
                return fired, reason
        raise AssertionError("seed not found")


class TestFormInUserEvidence(unittest.TestCase):
    def test_predicate(self):
        f = form_in_user_evidence
        self.assertTrue(f("決定", [U_REJECT]))
        self.assertFalse(f("決定", [A_REJECT, A_PLAIN]))
        self.assertFalse(f("決定", [{"span": U_REJECT["span"]}]))  # source 欄の無い旧 evidence
        self.assertFalse(f("決定", [{"source": "user", "span": ""}]))
        self.assertFalse(f("決定", None))
        self.assertTrue(f("回避策", [U_PLAIN, U_CORR]))
        self.assertFalse(f("回避策", [U_PLAIN, A_CORR]))
        for kind in ("性向", "所在", "再開点"):
            self.assertFalse(f(kind, [U_REJECT, U_CORR]))

    def test_marker_matched_after_normalisation(self):
        spaced = {"source": "user", "span": "通知は 🔔  ではなく   📣"}
        self.assertTrue(form_in_user_evidence("決定", [spaced]))


class TestThreshold(_Case):
    def test_1_decision_user_form_germinates_at_strength_1(self):
        self.assertEqual(self.germinated_ids([_etaki(1, "決定", [U_REJECT, A_PLAIN])]), [1])

    def test_2_decision_form_only_in_assistant_or_nowhere_is_short(self):
        for evs in ([A_REJECT, {"source": "user", "span": "了解"}], [A_PLAIN, {"source": "user", "span": "了解"}]):
            store = self.store([_etaki(1, "決定", evs)])
            self.assertEqual(store.germinate("猫", project=P), [])
            self.assertEqual(self.reason(store, 1), (False, GERMINATION_REASON_STRENGTH))

    def test_3_workaround_entry_in_user_germinates_assistant_only_does_not(self):
        self.assertEqual(self.germinated_ids([_etaki(1, "回避策", [U_PLAIN, U_CORR, A_PLAIN])]), [1])
        store = self.store([_etaki(1, "回避策", [U_PLAIN, A_CORR])])
        self.assertEqual(store.germinate("猫", project=P), [])
        self.assertEqual(self.reason(store, 1), (False, GERMINATION_REASON_STRENGTH))

    def test_4_other_kinds_stay_at_two(self):
        for kind in ("性向", "所在", "再開点"):
            with self.subTest(kind=kind):
                store = self.store([_etaki(1, kind, [U_REJECT, U_CORR])])
                self.assertEqual(store._threshold(store.get_by_ids([1])[0]), 2)
        store = self.store([_etaki(1, "所在", [U_REJECT, U_CORR])])
        self.assertEqual(store.germinate("猫", project=P), [])  # 強度 1 < 2 で落ちる

    def test_5_old_seeds_keep_threshold(self):
        no_sansho = _etaki(1, "決定", [U_REJECT])
        del no_sansho["sansho"]
        self.assertEqual(self.germinated_ids([no_sansho]), [])
        no_source = _etaki(1, "決定", [{"span": U_REJECT["span"]}])
        self.assertEqual(self.germinated_ids([no_source]), [])
        self.assertEqual(alaya_mod.GERMINATION_THRESHOLD_BY_CLASS, {"圓成實": 1, "依他起": 2})

    def test_6_enjojitsu_stays_at_one_and_threshold_never_raised(self):
        store = self.store([_etaki(1, "決定", [A_PLAIN], sansho_class="圓成實")])
        self.assertEqual(store._threshold(store.get_by_ids([1])[0]), 1)
        store = self.store([_etaki(1, "決定", [A_PLAIN])])
        self.assertEqual(store._threshold(store.get_by_ids([1])[0]), 2)
        store = self.store([_etaki(1, "決定", [U_REJECT])])
        self.assertEqual(store._threshold(store.get_by_ids([1])[0]), 1)

    def test_7_twin_hidden_h_stays_hidden_with_twin_reason(self):
        e = _etaki(1, "決定", [U_REJECT], conditions=("猫", "犬"), sansho_class="圓成實")
        h = _etaki(2, "決定", [U_REJECT, A_PLAIN], conditions=("猫", "犬"))
        store = self.store([e, h])
        self.assertEqual([s["id"] for s in store.germinate("猫 犬", project=P)], [1])
        fired, reason = self.reason(store, 2, "猫 犬")
        self.assertFalse(fired)
        self.assertEqual(reason, GERMINATION_REASON_TWIN.format(1))


class TestOrder(_Case):
    def test_germinated_etaki_after_enjojitsu_in_order(self):
        e = _etaki(1, "決定", [U_REJECT], sansho_class="圓成實")
        e["content"] = "通知アイコンは 📣 を採用"
        h = _etaki(2, "決定", [U_REJECT, A_PLAIN], conditions=("猫",))
        h["content"] = "通知は 📣 にする (別件)"
        h["evidences"][0] = {"source": "user", "span": "音は ではなく 無音 にする"}
        _write_state(self.state, [h, e], life=1)
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        recalled = eng.recall("猫", project=P)
        self.assertEqual([s["id"] for s in recalled["seeds"]], [1, 2])
        n = recalled["narrative"]
        self.assertIn("通知アイコンは 🔔 ではなく 📣 を使う", n)
        self.assertIn("音は ではなく 無音 にする", n)
        self.assertLess(n.index("通知アイコンは 🔔 ではなく 📣 を使う"), n.index("音は ではなく 無音 にする"))


class TestSharedPredicateWithE(_Case):
    """(8) 同じ span の組で、imprint 後の種子の閾値 1 と E の越境可否が一致する。"""

    DECISION_TURN = sansho.Turn(user_text="通知アイコンは 🔔 ではなく 📣 を使う 猫", assistant_text="了解しました。猫は 📣 で進めます")
    WORKAROUND_TURN = sansho.Turn(user_text="デプロイを手で流した。それは違う、元に戻してください 猫", assistant_text="デプロイを手で流しました")

    def _plant_etaki(self, store, turn, kind, content, spans):
        prop = {"content": content, "conditions": ["猫"], "polarity": "無記", "kind": kind,
                "evidence": [{"source": s, "span": t} for s, t in spans]}
        return store.imprint([turn], prop)

    def _cross(self, store, kind, content, turn, spans, target_id):
        """圓成實 の同 kind 種子 target_id に対する 依他起 の明示 supersede が通るか (= E)。"""
        prop = {"content": content, "conditions": ["猫"], "polarity": "無記", "kind": kind, "action": "supersede",
                "target_id": target_id, "evidence": [{"source": s, "span": t} for s, t in spans]}
        return store.imprint([turn], prop)

    def _check(self, kind, turn, content, spans_with_form, spans_without_form):
        store = self.store([])
        planted = self._plant_etaki(store, turn, kind, content, spans_with_form)
        seed = planted["seed"]
        self.assertEqual(seed["sansho"], "依他起")
        self.assertEqual(seed["kind"], kind)
        self.assertEqual(seed["strength"], 1)
        self.assertTrue(form_in_user_evidence(kind, seed["evidences"]))
        self.assertEqual(store._threshold(seed), 1)
        # E: 同じ evidence_entries を入力にする述語と、閾値 1 が一致する。形が user に無い組は 性向 に落ちる (kind 保持の条件)。
        demoted = self._plant_etaki(self.store([]), turn, kind, content, spans_without_form)
        self.assertEqual(demoted["seed"]["sansho"], "依他起")
        self.assertEqual(demoted["seed"]["kind"], "性向")
        self.assertFalse(form_in_user_evidence(demoted["seed"]["kind"], demoted["seed"]["evidences"]))
        self.assertEqual(store._threshold(demoted["seed"]), 2)

    def test_8_decision(self):
        self._check("決定", self.DECISION_TURN, "通知アイコンは 📣 ではなく 🔔 を使わない",
                    [("user", "通知アイコンは 🔔 ではなく 📣 を使う"), ("assistant", "了解しました")],
                    # 混在 (依他起): user の span「通知アイコンは」は却下側の印も縁 (猫) も持たない → 性向。
                    # (旧は user span「猫」だったが、縁を含む user の span は #114 U で 決定 の形になるので差し替え)
                    [("assistant", "了解しました。猫は 📣 で進めます"), ("user", "通知アイコンは")])

    def test_8_decision_assistant_only_without_form(self):
        store = self.store([])
        demoted = self._plant_etaki(store, self.DECISION_TURN, "決定", "通知アイコンは 📣 ではなく 🔔 を使わない",
                                    [("assistant", "了解しました。猫は 📣 で進めます")])
        self.assertEqual((demoted["seed"]["sansho"], demoted["seed"]["kind"]), ("依他起", "性向"))
        self.assertEqual(store._threshold(demoted["seed"]), 2)

    def test_8_workaround(self):
        self._check("回避策", self.WORKAROUND_TURN, "デプロイを手で流す → 本番が壊れた原因は順序 → 順序を守る",
                    [("user", "デプロイを手で流した"), ("user", "それは違う、元に戻してください"),
                     ("assistant", "デプロイを手で流しました")],
                    [("user", "デプロイを手で流した"), ("assistant", "デプロイを手で流しました")])

    def test_8_e_cross_equals_predicate(self):
        """E: 依他起 の提案は、形を user の span が担うときだけ 圓成實 の同 kind 種子を supersede できる。"""
        store = self.store([])
        e = self._plant_etaki(store, sansho.Turn(user_text="通知アイコンは 🔔 ではなく 📣 を使う 猫", assistant_text=""),
                              "決定", "通知アイコンは 📣 ではなく 🔔 を使わない", [("user", "通知アイコンは 🔔 ではなく 📣 を使う")])
        self.assertEqual(e["seed"]["sansho"], "圓成實")
        r = self._cross(store, "決定", "通知アイコンは 📣 ではなく 🔔 を使わない", self.DECISION_TURN,
                        [("user", "通知アイコンは 🔔 ではなく 📣 を使う"), ("assistant", "了解しました")], e["seed"]["id"])
        self.assertEqual(r["sansho"], "依他起")
        self.assertTrue(r["superseded"])
        # 形が assistant の span にしか無い組は 性向 に落ち (述語が偽)、同 kind でないので 圓成實 の 決定 を supersede しない。
        store2 = self.store([])
        e2 = self._plant_etaki(store2, sansho.Turn(user_text="通知アイコンは 🔔 ではなく 📣 を使う 猫", assistant_text=""),
                               "決定", "通知アイコンは 📣 ではなく 🔔 を使わない", [("user", "通知アイコンは 🔔 ではなく 📣 を使う")])
        r2 = self._cross(store2, "決定", "通知アイコンは 📣 ではなく 🔔 を使わない", self.DECISION_TURN,
                         [("assistant", "了解しました。猫は 📣 で進めます"), ("user", "通知アイコンは")], e2["seed"]["id"])   # 旧: user span「猫」(#114 U で縁を含まない span に差し替え)
        self.assertEqual(r2["kind"], "性向")
        self.assertFalse(r2.get("superseded"))

    def test_one_sided_decision_threshold_two_when_mixed_one_when_reject_in_user_span(self):
        """#114 U (8): 片側の 決定 (縁を含む user の span・却下側の印なし) が 依他起 (混在) なら閾値 2。
        却下側の印が user の span にあれば 1 (不変)。述語 form_in_user_evidence は却下側の印だけを見る。"""
        turn = sansho.Turn(user_text="DB は SQLite にした 猫", assistant_text="了解しました")
        store = self.store([])
        r = self._plant_etaki(store, turn, "決定", "DB は SQLite にした",
                              [("user", "DB は SQLite にした 猫"), ("assistant", "了解しました")])
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("依他起", "決定"))
        self.assertFalse(form_in_user_evidence("決定", r["seed"]["evidences"]))
        self.assertEqual(store._threshold(r["seed"]), 2)
        turn2 = sansho.Turn(user_text="MySQL ではなく SQLite にした 猫", assistant_text="了解しました")
        store2 = self.store([])
        r2 = self._plant_etaki(store2, turn2, "決定", "DB は SQLite にした",
                               [("user", "MySQL ではなく SQLite にした 猫"), ("assistant", "了解しました")])
        self.assertEqual((r2["seed"]["sansho"], r2["seed"]["kind"]), ("依他起", "決定"))
        self.assertTrue(form_in_user_evidence("決定", r2["seed"]["evidences"]))
        self.assertEqual(store2._threshold(r2["seed"]), 1)

    def test_e_not_widened_by_one_sided_decision(self):
        """#114 U (9): 依他起 の片側の 決定 (却下側の印が user の span に無い) は、圓成實 の 決定 を supersede / reinforce できない。"""
        content = "DB は SQLite にした"
        turn = sansho.Turn(user_text="DB は SQLite にした 猫", assistant_text="了解しました")
        mixed = [("user", "DB は SQLite にした 猫"), ("assistant", "了解しました")]
        store = self.store([])
        e = self._plant_etaki(store, sansho.Turn(user_text="MySQL ではなく SQLite にした 猫", assistant_text=""),
                              "決定", "DB は SQLite にした", [("user", "MySQL ではなく SQLite にした 猫")])
        self.assertEqual((e["seed"]["sansho"], e["seed"]["kind"]), ("圓成實", "決定"))
        before = copy.deepcopy(store.get_by_ids([e["seed"]["id"]])[0])
        r = self._cross(store, "決定", content, turn, mixed, e["seed"]["id"])
        self.assertEqual((r["sansho"], r["kind"]), ("依他起", "決定"))
        self.assertFalse(r.get("superseded"))
        self.assertEqual(store.get_by_ids([e["seed"]["id"]])[0], before)   # 圓成實 の種子は不変
        # reinforce も同様 (越境は new に倒れる)
        prop = {"content": content, "conditions": ["猫"], "polarity": "無記", "kind": "決定", "action": "reinforce",
                "target_id": e["seed"]["id"], "evidence": [{"source": s, "span": t} for s, t in mixed]}
        r2 = store.imprint([turn], prop)
        self.assertEqual(store.get_by_ids([e["seed"]["id"]])[0], before)
        self.assertNotEqual((r2.get("seed") or {}).get("id"), e["seed"]["id"])

    def test_8_e_cross_workaround(self):
        content = "デプロイを手で流す → 本番が壊れた原因は順序 → 順序を守る"
        corr = "それは違う、元に戻してください"
        e_turn = sansho.Turn(user_text="デプロイを手で流した。%s 猫" % corr, assistant_text="")
        e_spans = [("user", "デプロイを手で流した"), ("user", corr)]
        store = self.store([])
        e = self._plant_etaki(store, e_turn, "回避策", content, e_spans)
        self.assertEqual((e["seed"]["sansho"], e["seed"]["kind"]), ("圓成實", "回避策"))
        r = self._cross(store, "回避策", content, self.WORKAROUND_TURN, e_spans + [("assistant", "デプロイを手で流しました")], e["seed"]["id"])
        self.assertEqual(r["sansho"], "依他起")
        self.assertTrue(r["superseded"])
        self.assertEqual(r["seed"]["kind"], "回避策")
        self.assertEqual(store._threshold(r["seed"]), 1)
        # 訂正が assistant の span にしか無い組は 性向 に落ち、越境しない。
        store2 = self.store([])
        e2 = self._plant_etaki(store2, e_turn, "回避策", content, e_spans)
        r2 = self._cross(store2, "回避策", content, self.WORKAROUND_TURN,
                         [("user", "デプロイを手で流した"), ("assistant", "デプロイを手で流しました")], e2["seed"]["id"])
        self.assertEqual(r2["kind"], "性向")
        self.assertFalse(r2.get("superseded"))


class TestExplainAndRender(_Case):
    def _engine(self):
        seeds = [_etaki(1, "決定", [U_REJECT, A_PLAIN]), _etaki(2, "回避策", [U_PLAIN, U_CORR, A_PLAIN], conditions=("犬",))]
        for s in seeds:
            s["content"] = "通知アイコンは 📣 を採用" if s["id"] == 1 else "デプロイ順序を守る"
        _write_state(self.state, seeds, life=1)
        return MemoryEngine(state_dir=self.state, new_life=False)

    def test_9_explain_equals_recall_and_10_rendering(self):
        eng = self._engine()
        for text in ("猫", "猫 犬", "無関係"):
            with self.subTest(text=text):
                explained = eng.explain_recall(text, project=P)
                recalled = eng.recall(text, project=P)
                self.assertEqual([s["id"] for s in explained["seeds"]], [s["id"] for s in recalled["seeds"]])
                self.assertEqual(explained["narrative"], recalled["narrative"])
        recalled = MemoryEngine(state_dir=self.state, new_life=False).recall("猫", project=P)
        self.assertEqual([s["id"] for s in recalled["seeds"]], [1])
        self.assertEqual(recalled["seeds"][0]["sansho"], "依他起")
        self.assertIn("一部推定)", recalled["narrative"])
        self.assertNotIn("(推定)", recalled["narrative"])


class TestConsolidatePrompt(unittest.TestCase):
    def test_11_prompt_has_rephrase_line_after_user_quote_preference(self):
        prompt = engine_mod._build_consolidate_prompt([], {"related": [], "related_total": 0, "groups": [], "groups_total": 0})
        quote = "ユーザー発言からの引用を優先してください。\n"
        line = ("- ユーザー発言だけで記録の内容が裏付けられるときは、アシスタントの言い直し(同じ内容の復唱・要約)を evidence に加えないでください。"
                "アシスタント発言の引用は、ユーザー発言に無い内容を裏付けるときだけにしてください。\n")
        self.assertIn(quote + line, prompt)


if __name__ == "__main__":
    unittest.main()
