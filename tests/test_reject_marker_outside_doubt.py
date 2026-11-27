"""H8 (spec 2026-11-27-reject-marker-outside-doubt-design §8 T1・T2): 却下の印 (decision_reject) を、疑の文を除いた残りに当てる。
T1 = 決定 の (i) と KP (a) (窓つき `_span_remainders_fn(window)`)。T2 = `form_in_user_evidence` の 決定 の枝 (窓なし)。
LLM は使わない。テストが消すのは自作 tmp (mkdtemp) だけ。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, sansho  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402
from tests.test_form_threshold import _etaki  # noqa: E402
from tests.test_withdrawal import _write_state  # noqa: E402

PROJ = "/home/dev/p"
OTHER = "/home/dev/q"
DEMOTED = "決定 に 圓成實 の引用の却下側の印も断定の形も無いため 性向 として受理"
KP6 = "性向 の申告に 決定 の形あり (読み替えない)"


def _window(*texts, projects=None):
    projects = projects or [PROJ] * len(texts)
    return sansho.as_window(
        [LedgerTurn(user_text=t, assistant_text="", id=i, project=p) for i, (t, p) in enumerate(zip(texts, projects))]
    )


def _resolve(kind, span, conds, window=None, turn_index=0, allow_promotion=True):
    items = [{"span": span, "sansho": "圓成實", "turn_index": turn_index}]
    return alaya._resolve_kind({"kind": kind, "content": "x"}, items, conds, window, allow_promotion=allow_promotion)


def _prop(content, conditions, spans, kind, **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    p.update(extra)
    return p


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()


class TestRejectOutsideDoubtResolveKind(unittest.TestCase):
    def test_ja_question_with_reject_is_disposition(self):
        q = "A でなく B でいい？"
        for conds in (["B"], ["zzz"]):
            kind, note = _resolve("決定", q, conds, _window(q))
            self.assertEqual(kind, "性向", conds)
            self.assertEqual(note, DEMOTED)

    def test_precheck_path_follows(self):
        q = "A でなく B でいい？"
        self.assertEqual(_resolve("決定", q, ["zzz"], _window(q), allow_promotion=False)[0], "性向")

    def test_kp_question_with_reject_is_not_promoted(self):
        q = "Flask ではなく FastAPI にする？"
        self.assertEqual(_resolve("性向", q, ["FastAPI"], _window(q)), ("性向", None))
        q = "Can I use a private key instead of a password?"
        self.assertEqual(_resolve("性向", q, ["key"], _window(q)), ("性向", None))

    def test_en_fact_plus_question_is_kp6(self):
        q = ("My daily train fare is $6. Can you help me figure out how much I would save if I cycled to the "
             "station instead of taking the train?")
        self.assertEqual(_resolve("性向", q, ["train"]), ("性向", KP6))

    def test_reject_outside_question_still_counts(self):
        q = "Flask ではなく FastAPI にする。どう思う？"
        self.assertEqual(_resolve("決定", q, ["zzz"], _window(q))[0], "決定")
        self.assertEqual(_resolve("性向", q, ["FastAPI"], _window(q)), ("決定", alaya.PROMOTION_NOTE))

    def test_fragment_is_judged_by_user_sentence(self):
        span = "Flask ではなく FastAPI"
        self.assertEqual(_resolve("決定", span, ["zzz"], _window("Flask ではなく FastAPI にする？"))[0], "性向")
        self.assertEqual(_resolve("決定", span, ["zzz"], None)[0], "決定")   # 窓なしは既知の限界 (§9 の 1)

    def test_same_project_later_statement_rescues(self):
        w = _window("Flask ではなく FastAPI にする？", "やはり Flask ではなく FastAPI にする。")
        self.assertEqual(_resolve("決定", "Flask ではなく FastAPI にする", ["zzz"], w, turn_index=1)[0], "決定")

    def test_other_project_does_not_rescue(self):
        w = _window("Flask ではなく FastAPI にする？", "やはり Flask ではなく FastAPI にする。", projects=[PROJ, OTHER])
        # span は往復 0 (問い) に接地。往復 1 は別 project なので救わない
        self.assertEqual(_resolve("決定", "Flask ではなく FastAPI にする", ["zzz"], w, turn_index=0)[0], "性向")

    def test_preference_guard_reads_whole_span(self):
        q = "Redis は採らない。Memcached を使ってほしい？"
        self.assertEqual(_resolve("性向", q, ["Redis"], _window(q)), ("性向", KP6))

    def test_en_rhetorical_question_is_disposition(self):
        q = "Wouldn't it make more sense to use OUT2 instead of GND?"
        self.assertEqual(_resolve("決定", q, ["zzz"])[0], "性向")   # 反語の主張は数えない = 既知の損失 (§9 の 3)

    def test_line_broken_question_keeps_marker(self):
        q = "FakeLLM でなく\n実 LLM でコードが生成できるか？"
        self.assertEqual(_resolve("決定", q, ["zzz"], _window(q))[0], "決定")   # 既知の限界 (§9 の 2)

    def test_question_without_mark_keeps_marker(self):
        q = "I wonder if we should use TOML instead of YAML."
        self.assertEqual(_resolve("決定", q, ["zzz"], _window(q))[0], "決定")   # 既知の限界 (§9 の 4)

    def test_why_question_premise_reject(self):
        q = "なぜ Redis ではなく Memcached を使うの？"
        # (a) 問いだけ
        self.assertEqual(_resolve("決定", q, ["zzz"], _window(q))[0], "性向")
        # (b) 同じ project の往復 0 に問いでない文で在れば W1 で残る
        span = "Redis ではなく Memcached を使う"
        w = _window("Redis ではなく Memcached を使う。", q)
        self.assertEqual(_resolve("決定", span, ["zzz"], w, turn_index=1)[0], "決定")
        # (c) 窓が問いの往復だけなら残らない
        self.assertEqual(_resolve("決定", span, ["zzz"], _window(q), turn_index=0)[0], "性向")


class TestRejectOutsideDoubtImprint(_StoreCase):
    def _imp(self, text, conds):
        turn = LedgerTurn(user_text=text, assistant_text="", id=0, project=PROJ)
        return self.store.imprint([turn], _prop("決めたこと", conds, [("user", text)], "決定"))

    def test_question_only_reject_is_record_only(self):
        for text, conds in (("A でなく B でいい？", ["B"]), ("Maybe try Celery for jobs?", ["Celery"])):
            r = self._imp(text, conds)
            self.assertEqual(r["decision"], "reject", text)
            self.assertIn(alaya.DISPOSITION_NEEDS_FORM_IN_USER_SPAN, r["reason"], text)
            self.assertEqual(self.store.list_seeds(), [], text)


class TestFormInUserEvidenceDecision(unittest.TestCase):
    def test_threshold_evidence_ignores_question_reject(self):
        f = alaya.form_in_user_evidence

        def ev(s):
            return [{"source": "user", "span": s}]
        self.assertFalse(f("決定", ev("A でなく B でいい？")))
        self.assertTrue(f("決定", ev("A でなく B にする。どう？")))
        self.assertFalse(f("決定", ev("Can I use a key instead of a password?")))

    def test_workaround_branch_unchanged(self):
        self.assertTrue(alaya.form_in_user_evidence("回避策", [{"source": "user", "span": "やり直してください？"}]))

    def test_fast_path_order(self):
        with mock.patch.object(alaya, "_sentence_pieces", side_effect=AssertionError("split")):
            self.assertTrue(alaya.form_in_user_evidence("決定", [{"source": "user", "span": "A でなく B にする"}]))
            self.assertFalse(alaya.form_in_user_evidence("決定", [{"source": "user", "span": "B にする？"}]))

    def test_llm_cut_fragment_is_a_known_leak(self):
        # span に ？ が無い (窓を持てない限界・§9 の 1)
        self.assertTrue(alaya.form_in_user_evidence("決定", [{"source": "user", "span": "Flask ではなく FastAPI"}]))


class TestThresholdOfDependentDecision(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.state = os.path.join(self.tmpdir, "state")

    def _threshold(self, user_span):
        seed = _etaki(1, "決定", [{"source": "user", "span": user_span}, {"source": "assistant", "span": "B で進めます"}])
        _write_state(self.state, [seed], life=1)
        store = alaya_store_at(os.path.join(self.state, "alaya.json"), os.path.join(self.state, "imprint_log.json"))
        return store._threshold(store.get_by_ids([1])[0])

    def test_threshold_of_dependent_decision_with_question_reject(self):
        self.assertEqual(self._threshold("A でなく B でいい？"), 2)
        self.assertEqual(self._threshold("A でなく B にする。"), 1)


class TestCrossRight(_StoreCase):
    def _seed(self):
        w = [LedgerTurn(user_text="epoch 秒ではなく ISO 8601 を使う 時刻", assistant_text="", id=0, project=PROJ)]
        r = self.store.imprint(w, _prop("epoch 秒ではなく ISO 8601", ["時刻"],
                                        [("user", "epoch 秒ではなく ISO 8601 を使う")], "決定"))
        self.assertEqual((r["seed"]["sansho"], r["seed"]["kind"]), ("圓成實", "決定"))
        return r["seed"]["id"]

    def _second(self, user_span, action, sid):
        text = "時刻は RFC 3339 に統一。" + user_span
        w = [LedgerTurn(user_text=text, assistant_text="RFC 3339 で統一します 時刻", id=1, project=PROJ)]
        p = _prop("RFC 3339 に統一", ["時刻"],
                  [("user", "時刻は RFC 3339 に統一"), ("user", user_span), ("assistant", "RFC 3339 で統一します")],
                  "決定", action=action, target_id=sid)
        return self.store.imprint(w, p)

    def test_cross_right_needs_reject_outside_question(self):
        for action in ("supersede", "reinforce"):
            with self.subTest(action=action):
                self.setUp()
                sid = self._seed()
                r = self._second("ISO 8601 ではなく RFC 3339 でいい？", action, sid)
                self.assertTrue(r["is_new"])
                self.assertIn("越境", r["reason"])
                seed = self.store.get_by_ids([sid])[0]
                self.assertEqual(seed["content"], "epoch 秒ではなく ISO 8601")
                self.assertEqual(seed["strength"], 1)
                self.assertEqual(len(self.store.list_seeds()), 2)
        # 対照: 問いでない文に印があれば越境は通る
        self.setUp()
        sid = self._seed()
        r = self._second("ISO 8601 ではなく RFC 3339 にする", "supersede", sid)
        self.assertTrue(r.get("superseded"))


if __name__ == "__main__":
    unittest.main()
