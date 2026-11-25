"""#106 索引の届き (spec 2026-11-10-index-reach-design.md §7): ③ の並びを今の入力との関わりで決める + 切れた見出し。

LLM なし。テストが消すのは自作 tmp だけ。
"""
import copy
import os
import random
import time
import shutil
import sys
import tempfile
import unittest

os.environ["RAIYA_CONFIG"] = ""
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, manas  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.sansho import Turn  # noqa: E402

PROJ = "/p"
TOPICS = ["データベース", "キャッシュ", "メッセージキュー", "検索エンジン", "認証方式", "ログ基盤", "監視ツール", "コンテナ基盤",
          "フロントエンド", "状態管理", "テスト枠組み", "配信網", "課金基盤", "メール送信", "ジョブ管理", "秘密管理",
          "機能フラグ", "分析基盤", "エラー追跡", "負荷試験", "文書化ツール", "型検査", "言語版", "インフラ定義",
          "証明書", "バックアップ", "通知", "画像保存", "全文索引", "決済", "翻訳", "帳票", "スケジューラ",
          "認可方式", "設定管理", "監査記録", "共有基盤", "開発環境", "公開手順", "移行計画"]
Q = "メッセージキューは何に決めた？"


def _runs(text):
    return alaya._input_runs(text)


class TestInputRuns(unittest.TestCase):
    def test_hiragana_cuts_runs(self):
        self.assertEqual(_runs("メッセージキューは何に決めた？"), {"メッセージキュー"})

    def test_short_ascii_and_digits_dropped(self):
        self.assertEqual(_runs("db 12345 ab kafka"), {"kafka"})

    def test_two_char_non_ascii_kept_single_dropped(self):
        self.assertEqual(_runs("設計と猫"), {"設計"})

    def test_edge_punctuation_stripped(self):
        self.assertEqual(_runs("/src/app/ と ./x/ -_foo_-"), {"src/app", "foo"})

    def test_script_boundary_cuts_without_space(self):
        self.assertEqual(_runs("Kafka メッセージキューは？"), {"kafka", "メッセージキュー"})
        self.assertEqual(_runs("メッセージキューKafkaは"), {"kafka", "メッセージキュー"})

    def test_middle_dot_is_a_separator_long_vowel_mark_stays(self):
        want = {"メッセージキュー", "キャッシュ"}
        self.assertEqual(_runs("メッセージキュー・キャッシュはどうした？"), want)
        self.assertEqual(_runs("ﾒｯｾｰｼﾞｷｭｰ･ｷｬｯｼｭ"), want)
        self.assertEqual(_runs("゠アイ゠ウエ"), {"アイ", "ウエ"})

    def test_repetition_mark_and_extension_a_stay_in_run(self):
        self.assertEqual(_runs("佐々木さん"), {"佐々木"})
        self.assertEqual(_runs("\u3400\u3401は"), {"\u3400\u3401"})

    def test_non_str_is_empty(self):
        self.assertEqual(_runs(None), set())
        self.assertEqual(_runs(123), set())


class TestRelevance(unittest.TestCase):
    def test_reads_user_span_only_and_counts_each_run_once(self):
        seed = {"content": "メッセージキュー", "conditions": ["メッセージキュー"], "evidences": [
            {"source": "assistant", "span": "メッセージキュー"},
            {"source": "user", "span": "メッセージキューは Kafka"},
            {"source": "user", "span": "メッセージキュー再び"},
        ]}
        self.assertEqual(alaya._index_relevance(seed, {"メッセージキュー", "kafka"}), 8 + 5)
        self.assertEqual(alaya._index_relevance(seed, set()), 0)
        only_assistant = {"content": "メッセージキュー", "evidences": [{"source": "assistant", "span": "メッセージキュー"}]}
        self.assertEqual(alaya._index_relevance(only_assistant, {"メッセージキュー"}), 0)


def _naive(seed, runs):
    spans = [alaya.sansho._n(e["span"]).lower() for e in seed.get("evidences") or []
             if alaya._is_user_span_evidence(e)]
    return sum(len(r) for r in runs if any(alaya._occurs_as_word(r, sp) for sp in spans))


def _rand_text(rng, n, pool):
    out = []
    length = 0
    while length < n:
        w = "".join(rng.choice(pool) for _ in range(rng.randint(1, 5))) + rng.choice("はをにがの 、。")
        out.append(w)
        length += len(w)
    return "".join(out)


class TestRelevanceEquivalenceAndOldShapes(unittest.TestCase):
    POOL = [chr(c) for c in range(0x4e00, 0x4e00 + 12)] + list("アイウエKafkaabc01/") + ["々"]

    def test_matches_naive_on_random_fixture(self):
        rng = random.Random(106)
        for _ in range(300):
            runs = alaya._input_runs(_rand_text(rng, rng.randint(5, 60), self.POOL))
            seed = {"evidences": [
                {"source": "user", "span": _rand_text(rng, rng.randint(5, 80), self.POOL)},
                {"source": "assistant", "span": _rand_text(rng, 40, self.POOL)},
                {"source": "user", "span": _rand_text(rng, rng.randint(5, 40), self.POOL)},
            ]}
            self.assertEqual(alaya._index_relevance(seed, runs), _naive(seed, runs))

    def test_matches_word_rule_on_ascii_heavy_fixture(self):
        # 語の途中だけに当たる形が十分出る乱数 (T2)。部分一致の値と語頭の規則の値が違う回が在ることも固定する
        rng = random.Random(8)
        pool = list("aab_ ")
        differing = 0
        for _ in range(500):
            runs = alaya._input_runs("".join(rng.choice(pool) for _ in range(rng.randint(5, 40))))
            seed = {"evidences": [
                {"source": "user", "span": "".join(rng.choice(pool) for _ in range(rng.randint(5, 80)))},
                {"source": "user", "span": "".join(rng.choice(pool) for _ in range(rng.randint(5, 40)))},
            ]}
            spans = [alaya.sansho._n(e["span"]).lower() for e in seed["evidences"]]
            substring = sum(len(r) for r in runs if any(r in sp for sp in spans))
            self.assertEqual(alaya._index_relevance(seed, runs), _naive(seed, runs))
            differing += substring != _naive(seed, runs)
        self.assertGreaterEqual(differing, 50)

    def test_old_shaped_seeds_score_zero_without_error(self):
        runs = {"メッセージキュー"}
        for evidences in (None, [None, 1, "s"], [{"span": "メッセージキュー"}], 5, "メッセージキュー",
                          [{"source": "user", "span": None}], [{"source": "user", "span": 5}],
                          [{"source": "user", "span": ["メッセージキュー"]}], [{"source": "user", "span": ""}]):
            self.assertEqual(alaya._index_relevance({"evidences": evidences}, runs), 0, evidences)
        self.assertEqual(alaya._index_relevance({}, runs), 0)

    def test_worst_case_long_pieces_many_lengths_is_bounded(self):
        # 長さ 2〜200 の語を各 1 つ含む入力 × 区切り無し 1 万字の span を持つ種子 30 件 (直す前は 17s)
        rng = random.Random(7)
        kata = [chr(c) for c in range(0x30a2, 0x30f3)]
        text = "は".join("".join(rng.choice(kata) for _ in range(n)) for n in range(2, 201))
        seeds = [{"evidences": [{"source": "user", "span": "".join(rng.choice(kata) for _ in range(10000))}]}
                 for _ in range(30)]
        runs = alaya._input_runs(text)
        lengths = sorted({len(r) for r in runs})
        t = time.perf_counter()
        for seed in seeds:
            alaya._index_relevance(seed, runs, lengths)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_long_piece_and_many_lengths_match_naive(self):
        rng = random.Random(3)
        kata = [chr(c) for c in range(0x30a2, 0x30a2 + 6)]
        text = "は".join("".join(rng.choice(kata) for _ in range(n)) for n in range(2, 30))
        runs = alaya._input_runs(text)
        long_span = "".join(rng.choice(kata) for _ in range(10000))
        for span in (long_span, long_span[:40], "は".join([long_span[:300], long_span[500:900]])):
            seed = {"evidences": [{"source": "user", "span": span}]}
            self.assertEqual(alaya._index_relevance(seed, runs), _naive(seed, runs))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idxreach_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = MemoryEngine(state_dir=self.tmp)

    def plant(self, user, conds, content=None, kind="決定", evidences=None, assistant="了解しました。"):
        ev = evidences or [{"source": "user", "span": user}]
        r = self.eng.imprint(Turn(user, assistant), [{
            "kind": kind, "content": content or user, "conditions": conds, "action": "new", "evidence": ev[0]}],
            project=PROJ)
        self.assertTrue(r and r[0].get("is_new"), r)
        return r[0]["seed"]["id"]

    def index_ids(self, text):
        r = self.eng.explain_recall(text, project=PROJ)
        return [x["id"] if isinstance(x, dict) else x for x in r["index"]]

    def forty(self, mq_pos, target_conds=None):
        order = list(range(40))
        order.remove(2)
        order.insert(mq_pos, 2)
        target = None
        for i in order:
            t = TOPICS[i]
            a, b = ("Kafka", "RabbitMQ") if i == 2 else ("A%02d採用案" % i, "B%02d対案" % i)
            conds = target_conds if (i == 2 and target_conds is not None) else [a, b]
            sid = self.plant("{}は {} にする。{} は却下。".format(t, a, b), conds, "{}は {} を採用し {} は却下".format(t, a, b))
            if i == 2:
                target = sid
        return target


class TestReach(_Base):
    def test_topic_word_input_puts_target_first(self):
        for mq_pos in (2, 14, 39):
            with self.subTest(mq_pos=mq_pos):
                tmp = tempfile.mkdtemp(prefix="idxreach_")
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                self.eng = MemoryEngine(state_dir=tmp)
                target = self.forty(mq_pos)
                self.assertEqual(self.index_ids(Q)[0], target)

    def test_common_word_plus_topic_still_reaches(self):
        target = self.forty(39)
        ids = self.index_ids("メッセージキューは却下した？")
        self.assertEqual(ids[0], target)

    def test_no_topic_or_no_input_keeps_today_order(self):
        self.forty(39)
        today = self.index_ids("")
        self.assertEqual(self.index_ids("何を決めたっけ？"), today)
        seeds = self.eng.alaya.state["seeds"]
        expected = [s["id"] for _, s in sorted(
            ((alaya._index_sort_key(s), i), s) for i, s in enumerate(seeds))][:alaya.INDEX_KIND_LIMITS["決定"]]
        for kw in ({}, {"input_text": None}, {"input_text": "何を決めたっけ？"}):
            plan = self.eng.alaya._inject_index_plan(PROJ, True, alaya.INDEX_MAX, frozenset(), **kw)
            self.assertEqual([s["id"] for s, _ in plan["entries"]], expected, kw)

    def test_words_only_in_assistant_content_or_conditions_do_not_move(self):
        for i in range(3):
            self.plant("案{:02d}は X{:02d}案 にする。Y{:02d}案 は却下。".format(i, i, i), ["X%02d案" % i, "Y%02d案" % i])
        # 題の語 (ゼータ題) が user の span に無く、content・縁・assistant の発言にだけ在る種子
        self.plant("案99は Z99案 にする。W99案 は却下。", ["Z99案", "W99案", "ゼータ題"], content="ゼータ題を採用",
                   assistant="ゼータ題の件ですね。")
        order = lambda text: [s["id"] for s, _ in self.eng.alaya._inject_index_plan(
            PROJ, True, alaya.INDEX_MAX, frozenset(), input_text=text)["entries"]]
        self.assertEqual(order("ゼータ題は？"), order(None))

    def test_class_above_relevance_inside_kind_and_kind_not_crossed(self):
        # 同 kind 内: 関わる 依他起 (関わり 大) が無関係な 圓成實 より先
        ids = {}
        ids["en"] = self.plant("無関係の設定は Q01案 にする。R01案 は却下。", ["Q01案", "R01案"])
        ids["target"] = self.plant("ゼータ題は Z01案 にする。W01案 は却下。", ["Z01案", "W01案"])
        for seed in self.eng.alaya.state["seeds"]:
            if seed["id"] == ids["target"]:
                seed["sansho"] = "依他起"   # 読む側の in-memory 書き換えだけ (save しない)
        plan = self.eng.alaya._inject_index_plan(PROJ, True, alaya.INDEX_MAX, frozenset(), input_text="ゼータ題は？")
        order = [s["id"] for s, _ in plan["entries"]]
        self.assertEqual(order, [ids["target"], ids["en"]])
        plan0 = self.eng.alaya._inject_index_plan(PROJ, True, alaya.INDEX_MAX, frozenset(), input_text="何だっけ？")
        self.assertEqual([s["id"] for s, _ in plan0["entries"]], [ids["en"], ids["target"]])  # 手がかり無しなら class 順

    def test_kind_not_crossed(self):
        loc = self.plant("ゼータ題の資料は docs/z/ にある。", ["docs/z/"], kind="所在")
        dec = self.plant("無関係は Q02案 にする。R02案 は却下。", ["Q02案", "R02案"])
        order = [s["id"] for s, _ in self.eng.alaya._inject_index_plan(
            PROJ, True, alaya.INDEX_MAX, frozenset(), input_text="ゼータ題は？")["entries"]]
        self.assertEqual(order, [dec, loc])  # 決定 が 所在 より先 (kind_rank) のまま


class TestScriptBoundaryAndContext(_Base):
    def test_mixed_script_inputs_reach_target(self):
        target = self.forty(39, target_conds=["RabbitMQ"])
        for q in ("Kafka メッセージキューは？", "メッセージキュー Kafka は？"):
            self.assertEqual(self.index_ids(q)[0], target, q)

    def test_middle_dot_input_reaches_target(self):
        target = self.forty(39, target_conds=["RabbitMQ"])
        self.assertEqual(self.index_ids("メッセージキュー・キャッシュはどうした？")[0], target)

    def test_context_only_topic_does_not_move_index(self):
        self.forty(30)
        plain = self.eng.explain_recall("何を決めたっけ？", project=PROJ)["index"]
        with_ctx = self.eng.explain_recall("何を決めたっけ？", context=["メッセージキューの話"], project=PROJ)["index"]
        self.assertEqual(plain, with_ctx)

    def test_reached_seed_at_storage_tail_and_rejected_side_order(self):
        unrelated = [self.plant("案{:02d}は X{:02d}案 にする。Y{:02d}案 は却下。".format(i, i, i), ["X%02d案" % i, "Y%02d案" % i])
                     for i in range(2)]
        reached = [self.plant("ゼータ題その{}は V{:02d}案 にする。U{:02d}案 は却下。".format(i, i, i), ["V%02d案" % i, "U%02d案" % i])
                   for i in range(11)]
        q = "ゼータ題は？"
        plan = self.eng.alaya._inject_index_plan(PROJ, True, alaya.INDEX_MAX, frozenset(), input_text=q)
        self.assertEqual([s["id"] for s, _ in plan["entries"]], reached[:10])
        over = [s["id"] for s, inc, r in plan["verdicts"] if not inc and r == alaya.INDEX_REASON_MAX]
        self.assertEqual(over, [reached[10]] + unrelated)   # 落ちた側も 関わり → 倉庫の順
        rej = self.eng.explain_recall(q, project=PROJ)["index_rejected"]
        self.assertEqual([x["id"] for x in rej if x["reason"] == alaya.INDEX_REASON_MAX], over)
        # 落ちた側で 関わる 依他起 が 無関係な 圓成實 より先 (class より関わりが上。従来の鍵では逆になる)。in-memory だけ書き換える
        for seed in self.eng.alaya.state["seeds"]:
            if seed["id"] == reached[10]:
                seed["sansho"] = "依他起"
        plan = self.eng.alaya._inject_index_plan(PROJ, True, alaya.INDEX_MAX, frozenset(), input_text=q)
        over = [s["id"] for s, inc, r in plan["verdicts"] if not inc and r == alaya.INDEX_REASON_MAX]
        self.assertEqual(over, [reached[10]] + unrelated)

    def test_group_measured_by_representative_not_by_other_members(self):
        def seed(i, conds, span):
            return {"id": i, "content": "種子%d" % i, "strength": 1, "conditions": conds, "polarity": "無記",
                    "sansho": "圓成實", "kind": "決定", "project": PROJ, "superseded_by": None, "withdrawn": None,
                    "evidences": [{"life": 1, "source": "user", "span": span, "ledger_id": None}],
                    "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}
        self.plant("案00は X00案 にする。Y00案 は却下。", ["X00案", "Y00案"])
        seeds = [
            seed(1, ["群甲", "群乙"], "無関係な逐語です"),          # 群の代表 (倉庫の先頭・無関係)
            seed(2, ["群甲", "群乙"], "ゼータ題は群甲にする。"),    # 代表でない成員にだけ題の語
            seed(3, ["単独甲", "単独乙"], "ほかの逐語です"),
        ]
        self.eng.alaya.state["seeds"] = seeds
        plan = self.eng.alaya._inject_index_plan(PROJ, True, alaya.INDEX_MAX, frozenset(), input_text="ゼータ題は？")
        self.assertEqual([(s["id"], g is not None) for s, g in plan["entries"]], [(1, True), (3, False)])


class TestPureAndHeader(_Base):
    def test_explain_equals_recall_and_nothing_written(self):
        self.forty(20)
        before = copy.deepcopy(self.eng.inspect_seeds())
        ex = self.eng.explain_recall(Q, project=PROJ)
        self.assertEqual(before, self.eng.inspect_seeds())
        rc = self.eng.recall(Q, project=PROJ)
        self.assertEqual(ex["index"], rc["index"])
        self.assertEqual(ex["narrative"], rc["narrative"])
        after = self.eng.inspect_seeds()
        obs = {"fire_count", "last_fired", "miss_streak"}
        strip = lambda ss: [{k: v for k, v in s.items() if k not in obs} for s in ss]
        self.assertEqual(strip(before), strip(after))

    def test_header_cut_and_uncut(self):
        self.assertEqual(manas._index_header(10, 40), "【記録の索引 (上位 10 件 / 全 40 件。載っていない 30 件も記録に在る)】")
        self.assertEqual(manas._index_header(3, 3), "【記録の索引 (全 3 件)】")
        self.assertEqual(manas._index_header(3, None), "【記録の索引 (全 3 件)】")
        self.forty(5)
        self.assertIn("上位 10 件 / 全 40 件。載っていない 30 件も記録に在る", self.eng.recall(Q, project=PROJ)["narrative"])


if __name__ == "__main__":
    unittest.main()
