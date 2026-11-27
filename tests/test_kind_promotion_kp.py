"""spec 2026-10-31-resume-point-and-kind-name-design §5.2/§7 (#117): 申告が 性向 で全 span が 圓成實 のとき、
却下の印か (縁 ∧ 文末の断定の印) があれば 決定 へ上げる (KP2a+b)。縁だけなら注記のみ (KP6)。
LLM は使わない。テストが消すのは自作 tmp だけ (mkdtemp)。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import sansho  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.manas import _render_seed_line  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
PROMOTED = alaya_mod.PROMOTION_NOTE
KP6 = "性向 の申告に 決定 の形あり (読み替えない)"


def _turn(user_text, assistant_text="了解しました", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, spans, kind="性向", **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記",
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    if kind is not None:
        p["kind"] = kind
    p.update(extra)
    return p


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def resolve(self, user_text, content, conds, kind="性向", span=None, source="user", allow_promotion=True):
        w = sansho.as_window(
            [_turn("お願いします", user_text) if source == "assistant" else _turn(user_text, "了解しました")])
        p = _prop(content, conds, [(source, span if span is not None else user_text)], kind)
        d = sansho.evaluate_proposal(p, w)
        return alaya_mod._resolve_kind(p, d["spans"], d["grounded_conditions"], w, allow_promotion=allow_promotion)


class TestResolveKindPromotion(_Case):
    def test_d2_1_assert_form_promotes(self):
        self.assertEqual(self.resolve("DB は SQLite にした。", "DB は SQLite", ["SQLite"]), ("決定", PROMOTED))

    def test_m1_1_question_sentences_removed_then_promotes(self):
        t = "DB は PostgreSQL にした。キャッシュは Redis にする？ ジョブは Celery でいいかな？"
        self.assertEqual(self.resolve(t, "DB は PostgreSQL", ["PostgreSQL"]), ("決定", PROMOTED))

    def test_jwt_decided(self):
        self.assertEqual(self.resolve("認証は JWT に決めた。", "認証は JWT", ["JWT"]), ("決定", PROMOTED))

    def test_reject_marker_promotes_without_assert_ending(self):
        self.assertEqual(self.resolve("Flask ではなく FastAPI を使う", "FastAPI を使う", ["FastAPI"]), ("決定", PROMOTED))

    def test_e1_5_reject_marker_with_preference_is_not_promoted(self):
        t = "Redis は採らない。Memcached を利用したい。"
        kind, note = self.resolve(t, "Memcached", ["Memcached"])
        self.assertEqual(kind, "性向")
        self.assertNotEqual(note, PROMOTED)

    def test_content_preference_suffix_blocks_promotion(self):
        kind, note = self.resolve("DB は SQLite にした。", "SQLite を採用したい", ["SQLite"])
        self.assertEqual(kind, "性向")
        self.assertNotEqual(note, PROMOTED)

    def test_misses_do_not_promote(self):
        for t in ("DB は SQLite にしたほうがいい。", "DB は SQLite にしたら？", "SQLite にしたい。", "SQLite にしよう。"):
            kind, note = self.resolve(t, "DB は SQLite", ["SQLite"])
            self.assertEqual(kind, "性向", t)
            self.assertNotEqual(note, PROMOTED, t)

    def test_marker_without_condition_does_not_promote(self):
        # 「それにした。」: 縁が span に無い (縁 SQLite は assistant の言) → 上げない・注記も無い
        self.assertEqual(self.resolve("それにした。", "SQLite", []), ("性向", None))
        self.assertEqual(self.resolve("それにした。", "SQLite", ["SQLite"]), ("性向", None))

    def test_kp6_condition_only_keeps_disposition_with_note(self):
        self.assertEqual(self.resolve("DB は SQLite で。", "DB は SQLite", ["SQLite"]), ("性向", KP6))

    def test_no_condition_no_marker_is_plain(self):
        self.assertEqual(self.resolve("今日は晴れ。", "天気", ["SQLite"], span="今日は晴れ。"), ("性向", None))

    def test_assistant_span_never_promotes(self):
        t = "DB は SQLite にした。"
        self.assertEqual(self.resolve(t, "DB は SQLite", ["SQLite"], source="assistant"), ("性向", None))

    def test_mixed_user_and_assistant_spans_do_not_promote(self):
        w = sansho.as_window([_turn("DB は SQLite にした。", "ORM は Peewee にした。")])
        p = _prop("DB", ["SQLite"], [("user", "DB は SQLite にした。"), ("assistant", "ORM は Peewee にした。")])
        d = sansho.evaluate_proposal(p, w)
        self.assertEqual(alaya_mod._resolve_kind(p, d["spans"], d["grounded_conditions"], w), ("性向", None))

    def test_str_items_and_empty_spans_do_not_promote(self):
        p = _prop("DB は SQLite", ["SQLite"], [])
        self.assertEqual(alaya_mod._resolve_kind(p, ["DB は SQLite にした。"], ["SQLite"]), ("性向", None))
        self.assertEqual(alaya_mod._resolve_kind(p, [], ["SQLite"]), ("性向", None))

    def test_raw_none_is_never_promoted(self):
        self.assertEqual(self.resolve("DB は SQLite にした。", "DB は SQLite", ["SQLite"], kind=None), ("性向", None))

    def test_demoted_decision_does_not_reach_promotion(self):
        # 決定 と申告 → 好みの文末で落ちた 性向 は昇格しない (早期 return)
        kind, note = self.resolve("DB は SQLite にした。", "SQLite を採用したい", ["SQLite"], kind="決定")
        self.assertEqual(kind, "性向")
        self.assertIn("好みの形", note)

    def test_user_correction_marker_in_span_blocks_both_paths(self):
        kind, note = self.resolve("やり直してください。DB は SQLite にした。", "DB は SQLite", ["SQLite"])
        self.assertEqual(kind, "性向")
        self.assertNotEqual(note, PROMOTED)
        kind, note = self.resolve("やり直してください。Flask ではなく FastAPI を使う", "FastAPI", ["FastAPI"])
        self.assertEqual(kind, "性向")

    def test_pure_question_with_reject_marker_is_not_promoted(self):
        # (a) は 決定 の (i) と同じ、疑の文を除いた残りの却下の印。LongMemEval 段 2 の観測で見直した (2026-11-27 H8)
        self.assertEqual(self.resolve("Flask ではなく FastAPI にする？", "FastAPI", ["FastAPI"]), ("性向", None))
        self.assertEqual(self.resolve("Flask ではなく FastAPI にする？", "FastAPI", ["FastAPI"], kind="決定")[0], "性向")

    def test_fixed_phrases_do_not_promote(self):
        for t in ("SQLite の性能を気にした。", "SQLite のエラーを目にした。", "SQLite の性能を気にしました。", "SQLite のエラーを目にしました。"):
            self.assertEqual(self.resolve(t, "SQLite", ["SQLite"]), ("性向", KP6), t)

    def test_allow_promotion_false(self):
        self.assertEqual(
            self.resolve("DB は SQLite にした。", "DB は SQLite", ["SQLite"], allow_promotion=False), ("性向", None))

    def test_promotion_is_subset_of_decision_survival(self):
        cases = [
            ("DB は SQLite にした。", "DB は SQLite", ["SQLite"]),
            ("DB は PostgreSQL にした。キャッシュは Redis にする？", "DB は PostgreSQL", ["PostgreSQL"]),
            ("Flask ではなく FastAPI を使う", "FastAPI を使う", ["FastAPI"]),
            ("Redis は採らない。Memcached を利用したい。", "Memcached", ["Memcached"]),
            ("SQLite にしたい。", "SQLite", ["SQLite"]),
            ("Flask ではなく FastAPI にする？", "FastAPI", ["FastAPI"]),
            ("Can I use a private key instead of a password?", "x", ["key"]),
        ]
        for t, content, conds in cases:
            promoted = self.resolve(t, content, conds)[0] == "決定"
            survives = self.resolve(t, content, conds, kind="決定")[0] == "決定"
            self.assertTrue(survives or not promoted, t)


class TestImprintPromotion(_Case):
    def test_imprint_plants_a_decision_with_note_in_log(self):
        w = [_turn("DB は SQLite にした。")]
        r = self.store.imprint(w, _prop("DB は SQLite", ["SQLite"], [("user", "DB は SQLite にした。")]))
        self.assertEqual(r["kind"], "決定")
        self.assertEqual(self.store.list_seeds()[0]["kind"], "決定")
        self.assertIn(PROMOTED, r["reason"])
        self.assertIn(PROMOTED, self.store.list_imprint_log()[-1]["reason"])

    def test_replacement_for_is_not_promoted(self):
        w = [_turn("DB は SQLite にした。")]
        r = self.store.imprint(w, _prop("DB は SQLite", ["SQLite"], [("user", "DB は SQLite にした。")]), replacement_for=99)
        self.assertEqual(r["kind"], "性向")
        self.assertNotIn(PROMOTED, r["reason"])

    def test_kp6_note_reaches_the_log_and_kind_is_unchanged(self):
        w = [_turn("DB は SQLite で。")]
        r = self.store.imprint(w, _prop("DB は SQLite", ["SQLite"], [("user", "DB は SQLite で。")]))
        self.assertEqual(r["kind"], "性向")
        self.assertIn(KP6, r["reason"])

    def test_m1_1_promoted_seed_renders_the_doubt_label(self):
        t = "DB は PostgreSQL にした。キャッシュは Redis にする？ ジョブは Celery でいいかな？"
        w = [_turn(t)]
        r = self.store.imprint(w, _prop("DB は PostgreSQL", ["PostgreSQL", "Redis", "Celery"], [("user", t)]))
        self.assertEqual(r["kind"], "決定")
        line = _render_seed_line(self.store.list_seeds()[0])
        self.assertIn("一部問い", line)
        self.assertIn("Redis", line)
        self.assertIn("Celery", line)


class TestPlantPrecheckDoesNotPromote(_Case):
    def test_precheck_passes_allow_promotion_false(self):
        # plant_precheck は kind を返さないので、植わる (None) ことと 昇格しないことを _resolve_kind 呼び出しで確かめる
        w = [_turn("DB は SQLite にした。")]
        p = _prop("DB は SQLite", ["SQLite"], [("user", "DB は SQLite にした。")])
        seen = []
        real = alaya_mod._resolve_kind

        def spy(*a, **k):
            seen.append(k.get("allow_promotion"))
            return real(*a, **k)

        alaya_mod._resolve_kind = spy
        try:
            self.assertIsNone(alaya_mod.plant_precheck(w, p))
        finally:
            alaya_mod._resolve_kind = real
        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
