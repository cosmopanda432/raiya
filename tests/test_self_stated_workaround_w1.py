"""spec 2026-11-04-self-stated-workaround-design §5.2/§7 (#134, W1): 利用者自身が同じ 1 本の 圓成實 の span で
原因と回避を述べた提案は、訂正の印が無くても 回避策 になる (申告 性向 からは上げ、申告 回避策 は入口の一つ)。
LLM は使わない。テストが消すのは自作 tmp だけ (mkdtemp)。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import markers, sansho  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
NOTE = alaya_mod.SELF_STATED_WORKAROUND_NOTE
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "workaround_avoid_markers_20261104.json")

S1 = "テストで時刻まわりが時々落ちます。原因はタイムゾーンが UTC 固定でないことで、TZ=UTC を付けて実行すると回避できます。"
S2 = "ローカルで docker compose up が失敗するのは、ポート 5432 を手元の Postgres が先に使っているのが原因です。停止してから起動すると回避できます。"
S3 = "フロントのビルドが「heap out of memory」で落ちるのは、Node のメモリ上限が原因です。NODE_OPTIONS=--max-old-space-size=4096 を付けると回避できます。"
S4 = "mypy の誤検知で CI が落ちる件は、原因が古いスタブ (types-requests) です。バージョンを固定すると回避できます。"
FOUR = ((S1, ["時刻", "TZ=UTC"]), (S2, ["docker", "5432"]), (S3, ["ビルド", "NODE_OPTIONS"]), (S4, ["mypy", "types-requests"]))


def _turn(user_text, assistant_text="了解しました", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, spans, kind="性向", **extra):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記",
         "evidence": [{"source": s, "span": t} for s, t in spans]}
    if kind is not None:
        p["kind"] = kind
    p.update(extra)
    return p


def _resolve(turns, content, conds, spans, kind="性向", allow_promotion=True):
    w = sansho.as_window(turns)
    p = _prop(content, conds, spans, kind)
    d = sansho.evaluate_proposal(p, w)
    return alaya_mod._resolve_kind(p, d["spans"], d["grounded_conditions"], w, allow_promotion=allow_promotion)


class TestTable(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            self.fixture = json.load(f)

    def hit(self, t):
        return alaya_mod._has_marker(sansho._n(t), alaya_mod.WORKAROUND_AVOID_MARKERS)

    def test_tables(self):
        self.assertIn("workaround_avoid", markers.ROLES)
        self.assertEqual(alaya_mod.WORKAROUND_AVOID_MARKERS, markers.union("workaround_avoid"))
        self.assertEqual(markers.MARKERS["en"]["workaround_avoid"], ())
        for w in ("回避できる", "解消しました", "解消します", "直りました", "通りました", "動きました"):
            self.assertNotIn(w, markers.MARKERS["ja"]["workaround_avoid"])

    def test_every_hit_matches(self):
        self.assertTrue(self.fixture["hit"])
        for t in self.fixture["hit"]:
            self.assertTrue(self.hit(t), t)

    def test_no_miss_matches(self):
        self.assertTrue(self.fixture["miss"])
        for t in self.fixture["miss"]:
            self.assertFalse(self.hit(t), t)

    def test_known_hits_match(self):
        # ？の無い疑問は表では落とせない既知の当たり (疑の文の規則は ？/? のある文だけ)。表が変われば気づく。
        for t in self.fixture["known_hits"]:
            self.assertTrue(self.hit(t), t)

    def test_question_sentences_are_dropped_by_the_rule_not_the_table(self):
        for t in self.fixture["rule_miss_by_question"]:
            self.assertTrue(self.hit(t), t)   # 表は当たる
            self.assertEqual(_resolve([_turn(t)], "UTC 固定", ["UTC"], [("user", t)]), ("性向", None), t)


class TestPredicate(unittest.TestCase):
    def pred(self, user, spans, source="user"):
        w = sansho.as_window([_turn(user) if source == "user" else _turn("お願いします", user)])
        p = _prop("x", ["x"], spans)
        d = sansho.evaluate_proposal(p, w)
        return alaya_mod._self_stated_workaround(d["spans"], w), d

    def test_four_issue_sentences_pass(self):
        for t, _c in FOUR:
            ok, d = self.pred(t, [("user", t)])
            self.assertTrue(ok, t)

    def test_cause_and_avoid_in_two_sentences_of_one_span_pass(self):
        t = "ポート 5432 を Postgres が使っているのが原因です。停止してから起動すると回避できます。"
        self.assertTrue(self.pred(t, [("user", t)])[0])

    def test_cause_and_avoid_in_two_spans_do_not_pass(self):
        t = "ポート 5432 を Postgres が使っているのが原因です。停止してから起動すると回避できます。"
        spans = [("user", "ポート 5432 を Postgres が使っているのが原因です。"), ("user", "停止してから起動すると回避できます。")]
        self.assertFalse(self.pred(t, spans)[0])   # P1: 同じ 1 本の span

    def test_empty_and_str_spans_do_not_count(self):
        self.assertFalse(alaya_mod._self_stated_workaround([], None))
        self.assertFalse(alaya_mod._self_stated_workaround([S1], None))

    def test_assistant_span_with_both_markers_does_not_pass(self):
        a = "原因は古いスタブです。バージョンを固定すると回避できます。"
        ok, d = self.pred(a, [("assistant", a)], source="assistant")
        self.assertEqual(d["spans"][0]["sansho"], "依他起")
        self.assertFalse(ok)

    def test_one_non_grounded_span_blocks(self):
        user = "原因は古いスタブです。バージョンを固定すると回避できます。"
        w = sansho.as_window([_turn(user, "ところで、それは回避できます。")])
        p = _prop("x", ["スタブ"], [("user", user), ("assistant", "それは回避できます。")])
        d = sansho.evaluate_proposal(p, w)
        self.assertEqual({s["sansho"] for s in d["spans"]}, {"圓成實", "依他起"})
        self.assertFalse(alaya_mod._self_stated_workaround(d["spans"], w))   # §9a 裁定 B

    def test_only_cause_or_only_avoid_does_not_pass(self):
        for t in ("原因はタイムゾーンが UTC 固定でないことです。", "TZ=UTC を付けて実行すると回避できます。"):
            self.assertFalse(self.pred(t, [("user", t)])[0], t)

    def test_question_sentence_removed(self):
        t = "テストが落ちます。原因は UTC 固定ですか？回避できますか？"
        self.assertFalse(self.pred(t, [("user", t)])[0])

    def test_preference_suffix_blocks(self):
        t = "原因は UTC 固定なので、TZ=UTC で回避できるようにしたい。"
        self.assertFalse(self.pred(t, [("user", t)])[0])
        t2 = "原因は UTC 固定なので、TZ=UTC を付けて回避できますが、そうしてほしい。"
        self.assertFalse(self.pred(t2, [("user", t2)])[0])


class TestResolveKind(unittest.TestCase):
    def test_four_sentences_from_either_declaration(self):
        for t, conds in FOUR:
            self.assertEqual(_resolve([_turn(t)], "要約", conds, [("user", t)], "性向"), ("回避策", NOTE), t)
            self.assertEqual(
                _resolve([_turn(t)], "症状 → 原因 → 回避: " + t[:10], conds, [("user", t)], "回避策"), ("回避策", None), t)

    def test_declared_workaround_still_needs_cause_in_content(self):
        kind, note = _resolve([_turn(S1)], "TZ=UTC を付ける", ["TZ=UTC"], [("user", S1)], "回避策")
        self.assertEqual(kind, "性向")
        self.assertIn("原因の印が無い", note)

    def test_declared_workaround_preference_content_stays_disposition(self):
        kind, note = _resolve([_turn(S1)], "原因は UTC 固定。TZ=UTC で回したい", ["TZ=UTC"], [("user", S1)], "回避策")
        self.assertEqual(kind, "性向")
        self.assertIn("好みの形", note)

    def test_promotion_not_done_when_content_is_preference(self):
        self.assertEqual(_resolve([_turn(S1)], "TZ=UTC で回したい", ["TZ=UTC"], [("user", S1)]), ("性向", None))

    def test_promotion_ignores_content_cause_marker(self):
        self.assertEqual(_resolve([_turn(S1)], "TZ=UTC", ["TZ=UTC"], [("user", S1)]), ("回避策", NOTE))

    def test_only_cause_or_only_avoid_stays_disposition(self):
        for t in ("原因はタイムゾーンが UTC 固定でないことです。", "TZ=UTC を付けて実行すると回避できます。"):
            kind, _n = _resolve([_turn(t)], "TZ=UTC", ["TZ=UTC", "UTC"], [("user", t)])
            self.assertEqual(kind, "性向", t)

    def test_question_pair_stays_disposition(self):
        t = "テストが落ちます。原因は UTC 固定ですか？回避できますか？"
        kind, _n = _resolve([_turn(t)], "UTC", ["UTC"], [("user", t)])
        self.assertEqual(kind, "性向")

    def test_preference_in_span_stays_disposition(self):
        t = "原因は UTC 固定なので、TZ=UTC で回避できるようにしたい。"
        kind, _n = _resolve([_turn(t)], "TZ=UTC", ["TZ=UTC"], [("user", t)])
        self.assertEqual(kind, "性向")

    def test_assistant_spans_do_not_pass(self):
        a = "原因は古いスタブです。バージョンを固定すると回避できます。"
        for declared in ("性向", "回避策"):
            kind, _n = _resolve([_turn("どうして落ちる？", a)], "原因はスタブ", ["スタブ"], [("assistant", a)], declared)
            self.assertEqual(kind, "性向", declared)

    def test_not_promoted_without_allow_promotion(self):
        self.assertEqual(_resolve([_turn(S1)], "TZ=UTC", ["TZ=UTC"], [("user", S1)], allow_promotion=False), ("性向", None))

    def test_declared_workaround_passes_without_promotion_flag(self):
        self.assertEqual(
            _resolve([_turn(S1)], "原因は UTC 固定", ["TZ=UTC"], [("user", S1)], "回避策", allow_promotion=False), ("回避策", None))

    def test_ahead_of_decision_promotion(self):
        t = "原因は DB の接続上限なので、プールを増やして回避できます。SQLite にした。"
        kind, note = _resolve([_turn(t)], "SQLite", ["SQLite", "プール"], [("user", t)])
        self.assertEqual((kind, note), ("回避策", NOTE))

    def test_correction_pair_entry_unchanged(self):
        t = "デプロイを手で流した。それは違う、元に戻してください。"
        r = _resolve([_turn(t)], "手で流す → 順序が原因", ["デプロイ"], [("user", "デプロイを手で流した"), ("user", "元に戻してください")], "回避策")
        self.assertEqual(r, ("回避策", None))
        r = _resolve([_turn(t)], "手で流す → 順序が原因", ["デプロイ"], [("user", "元に戻してください")], "回避策")
        self.assertEqual(r[0], "性向")
        self.assertIn("対", r[1])


class TestWindowDecides(unittest.TestCase):
    """台帳の user_text に ？ があり span には無い: 窓を渡すと疑の文として落ち、窓なしなら通る (窓が判定を決める)。"""
    LEDGER = "原因はキャッシュで、消すと回避できますか？"
    SPAN = "原因はキャッシュで、消すと回避できます"

    def setUp(self):
        self.w = sansho.as_window([_turn(self.LEDGER)])
        self.p = _prop("キャッシュ", ["キャッシュ"], [("user", self.SPAN)])
        self.d = sansho.evaluate_proposal(self.p, self.w)

    def test_question_mark_in_ledger_but_not_in_span_blocks_promotion(self):
        self.assertTrue(alaya_mod._self_stated_workaround(self.d["spans"], None))
        self.assertFalse(alaya_mod._self_stated_workaround(self.d["spans"], self.w))
        r = alaya_mod._resolve_kind(self.p, self.d["spans"], self.d["grounded_conditions"], self.w, allow_promotion=True)
        self.assertEqual(r, ("性向", None))

    def test_question_mark_in_ledger_but_not_in_span_blocks_declared_workaround(self):
        p = _prop("原因 キャッシュ", ["キャッシュ"], [("user", self.SPAN)], "回避策")
        d = sansho.evaluate_proposal(p, self.w)
        self.assertTrue(alaya_mod._self_stated_workaround(d["spans"], None))
        kind, note = alaya_mod._resolve_kind(p, d["spans"], d["grounded_conditions"], self.w)
        self.assertEqual(kind, "性向")
        self.assertIn("訂正の印が無い", note)


class TestNarrowing(unittest.TestCase):
    """案 B (spec §7「7 狭め」): 原因は `self_stated_cause` (「原因」だけ)、回避は問いでない文の文末一致。1 本の user span・申告 性向。"""
    def setUp(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            self.fx = json.load(f)

    def kind(self, t):
        return _resolve([_turn(t)], "要約", ["キャッシュ"], [("user", t)])[0]

    def test_roles(self):
        self.assertIn("self_stated_cause", markers.ROLES)
        self.assertEqual(alaya_mod.SELF_STATED_CAUSE_MARKERS, ("原因",))
        self.assertEqual(markers.MARKERS["en"]["self_stated_cause"], ())

    def test_rule_misses_stay_disposition(self):
        for t in self.fx["self_stated_rule_miss"]:
            self.assertEqual(self.kind(t), "性向", t)

    def test_rule_known_hits_are_raised(self):
        # 否定・不明・依頼の「原因」(原因は不明です / 原因を調べてください 等) は受け入れた穴 (spec 2026-11-04-self-stated-workaround §7 行 8)。
        # 上がることを固定し、規則が変わったらここで気づく。
        for t in self.fx["self_stated_rule_known_hits"]:
            self.assertEqual(self.kind(t), "回避策", t)

    def test_pip_case_and_issue_sentences_are_raised(self):
        for t in self.fx["self_stated_rule_hit"] + [s for s, _c in FOUR]:
            self.assertEqual(self.kind(t), "回避策", t)

    def test_question_less_table_hits_do_not_raise(self):
        for t in self.fx["known_hits"]:   # 表には当たるが、文末一致の規則では上がらない (原因の印が無い文でも同様)
            self.assertEqual(self.kind("原因はキャッシュです。" + t), "性向", t)

    def test_tame_and_arrow_are_not_self_stated_cause(self):
        t = "キャッシュのため、消すと回避できます。"
        self.assertEqual(self.kind(t), "性向")
        self.assertEqual(self.kind("キャッシュ → 消すと回避できます。"), "性向")

    def test_preference_elsewhere_in_remainder_blocks(self):
        for t in ("原因はキャッシュです。消すと回避できます。毎回そうしたい。", "原因はキャッシュです。消してほしい。消すと回避できます。"):
            w = sansho.as_window([_turn(t)])
            p = _prop("キャッシュ", ["キャッシュ"], [("user", t)])
            d = sansho.evaluate_proposal(p, w)
            rs = alaya_mod._span_remainders_fn(w)(d["spans"][0])
            self.assertTrue(any(alaya_mod._has_avoid_final(r) and alaya_mod._has_marker(r, alaya_mod.SELF_STATED_CAUSE_MARKERS) for r in rs), t)
            self.assertTrue(any(alaya_mod._has_marker(r, alaya_mod.PREFERENCE_SUFFIXES) for r in rs), t)
            self.assertEqual(alaya_mod._resolve_kind(p, d["spans"], d["grounded_conditions"], w), ("性向", None), t)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()


class TestImprint(_Case):
    def test_four_sentences_plant_workaround_and_inject(self):
        for i, (t, conds) in enumerate(FOUR):
            r = self.store.imprint([_turn(t, ledger_id=i)], _prop("要約 " + t[:8], conds, [("user", t)]))
            self.assertEqual(r["kind"], "回避策", t)
            self.assertIn(NOTE, r["reason"])
            self.assertIn(NOTE, self.store.list_imprint_log()[-1]["reason"])
        seeds = self.store.list_seeds()
        self.assertEqual({s["kind"] for s in seeds}, {"回避策"})
        self.assertEqual({s["sansho"] for s in seeds}, {"圓成實"})
        got = self.store.germinate("テストで時刻まわりが時々落ちます", project=PROJ)
        self.assertTrue(got)
        self.assertIn("回避策", {s["kind"] for s in got})
        indexed = self.store.index(project=PROJ, purpose="inject")
        self.assertEqual(len(indexed), 4)

    def test_declared_workaround_plants_too(self):
        r = self.store.imprint([_turn(S1)], _prop("時刻テストの失敗 → 原因 UTC 固定 → TZ=UTC", ["時刻", "TZ=UTC"], [("user", S1)], "回避策"))
        self.assertEqual(r["kind"], "回避策")
        self.assertEqual(self.store.list_seeds()[0]["sansho"], "圓成實")

    def test_replacement_for_is_not_promoted(self):
        r = self.store.imprint([_turn(S1)], _prop("TZ=UTC", ["時刻", "TZ=UTC"], [("user", S1)]), replacement_for=99)
        self.assertEqual(r["kind"], "性向")
        self.assertNotIn(NOTE, r["reason"])

    def test_existing_disposition_seed_is_not_rewritten(self):
        r0 = self.store.imprint([_turn(S1)], _prop("TZ=UTC", ["時刻", "TZ=UTC"], [("user", S1)]), replacement_for=99)
        self.assertEqual(r0["kind"], "性向")
        before = json.loads(json.dumps(self.store.list_seeds()[0], ensure_ascii=False))
        r1 = self.store.imprint([_turn(S1, ledger_id=1)], _prop("TZ=UTC", ["時刻", "TZ=UTC"], [("user", S1)]))
        self.assertEqual(r1["kind"], "回避策")
        seeds = self.store.list_seeds()
        old = [s for s in seeds if s["id"] == before["id"]][0]
        for k in ("kind", "content", "strength", "sansho"):
            self.assertEqual(old.get(k), before.get(k), k)
        self.assertEqual(len(seeds), 2)   # 併合は同じ kind が条件: 旧 性向 とは併合しない


class TestPlantPrecheck(_Case):
    def test_precheck_agrees_with_imprint_for_declared_workaround(self):
        w = [_turn(S1)]
        p = _prop("症状 → 原因 → 回避", ["時刻", "TZ=UTC"], [("user", S1)], "回避策")
        self.assertIsNone(alaya_mod.plant_precheck(w, p))
        self.assertEqual(self.store.imprint(w, p)["kind"], "回避策")

    def test_precheck_does_not_promote_declared_disposition(self):
        w = [_turn(S1)]
        p = _prop("TZ=UTC", ["時刻", "TZ=UTC"], [("user", S1)])
        seen = []
        real = alaya_mod._resolve_kind

        def spy(*a, **k):
            seen.append(k.get("allow_promotion"))
            r = real(*a, **k)
            seen.append(r)
            return r

        alaya_mod._resolve_kind = spy
        try:
            self.assertIsNone(alaya_mod.plant_precheck(w, p))
        finally:
            alaya_mod._resolve_kind = real
        self.assertEqual(seen, [False, ("性向", None)])


if __name__ == "__main__":
    unittest.main()
