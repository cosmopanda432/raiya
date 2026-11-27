"""発芽の縁の照合の境 (設計書 2026-11-25-recall-capacity-design §4.1 案 8d・T1) の決定論 pin。

縁が ASCII 英数字で始まるなら語頭の境を要し、2 字以下の縁は語末の境も要する。3 字以上の前方一致
(git → github) は既知の限界として残す。門 (sansho) の接地は部分一致のまま。LLM は使わない。
テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。
"""
import os
import random
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import alaya as alaya_mod  # noqa: E402
from test_layered_circles import _seed, _StateCase  # noqa: E402

def occurs(key, text_n):
    return alaya_mod._occurs_as_word(key, text_n)


class TestOccursAsWord(unittest.TestCase):
    def test_ascii_condition_inside_word_does_not_occur(self):
        self.assertFalse(occurs("art", "let's start now"))
        self.assertFalse(occurs("l", "sqlite版を使う"))
        self.assertFalse(occurs("mit", "commit して"))
        self.assertFalse(occurs("d", "<pasted_content>"))

    def test_short_condition_needs_both_boundaries(self):
        self.assertFalse(occurs("l", "l13 を読む"))
        self.assertFalse(occurs("pi", "pip install"))
        self.assertFalse(occurs("id", "idx を見る"))
        self.assertTrue(occurs("l", "l の形"))
        self.assertTrue(occurs("l", "l' を裁定"))
        self.assertTrue(occurs("pi", "pi拡張"))
        self.assertTrue(occurs("l2", "l2 の件"))

    def test_long_condition_keeps_word_start_prefix(self):
        # 既知の限界: 3 字以上は右を見ない
        self.assertTrue(occurs("hook", "check the hooks"))
        self.assertTrue(occurs("git", "github の issue"))
        self.assertTrue(occurs("python", "python3 -m raiya"))

    def test_boundaries_are_symbols_cjk_and_text_edges(self):
        self.assertTrue(occurs("sansho", "raiya/sansho.py"))
        self.assertTrue(occurs("art", "my_art"))
        self.assertTrue(occurs("art", "art class"))
        self.assertTrue(occurs("raiya", "raiya-hook"))

    def test_non_ascii_or_symbol_first_condition_keeps_substring(self):
        self.assertTrue(occurs("キュー", "メッセージキューは"))
        self.assertTrue(occurs("-con", "raiya-hook-contract"))
        # 設計書は ("-c", "raiya-hook-contract") を真としたが、規則 (2 字以下で ASCII で終わる縁は語末の境) では偽。規則を取る
        self.assertTrue(occurs("-c", "a -c flag"))
        self.assertFalse(occurs("-c", "raiya-hook-contract"))
        self.assertFalse(occurs("", "abc"))

    def test_a_later_position_can_satisfy_the_boundary(self):
        # 最初の出現は語の途中、2 つ目が語頭
        self.assertTrue(occurs("art", "start art"))
        self.assertTrue(occurs("l", "l13 l"))


def _art_seed(seed_id=1, conditions=("art",), kind="決定"):
    return _seed(seed_id, list(conditions), kind=kind, project=None,
                 evidences=[{"source": "user", "span": "I bought art supplies.", "ledger_id": 1, "life": 1}])


class TestGermination(_StateCase):
    def test_mid_word_condition_does_not_fire(self):
        eng = self.write([_art_seed()])
        x = eng.explain_recall("When did I start?", project=None)
        self.assertNotIn(1, [g["id"] for g in x["germinated"]])
        self.assertEqual([r["reason"] for r in x["germination_rejected"] if r["id"] == 1], ["縁の一致が足りない (0/1)"])
        y = eng.explain_recall("my art class", project=None)
        self.assertIn(1, [g["id"] for g in y["germinated"]])

    def test_consolidation_view_uses_same_rule(self):
        eng = self.write([_art_seed()])
        self.assertEqual(eng.alaya.consolidation_view({None}, ["let's start"])["related"], [])
        self.assertEqual([s["id"] for s in eng.alaya.consolidation_view({None}, ["art class"])["related"]], [1])


class TestLatency(_StateCase):
    def test_three_mid_word_conditions_worst_case(self):
        # 縁が語の途中にだけ当たる種子 300 件 × 5 万字 (メモ化の前は 1.0〜1.07 s)
        eng = self.write([_art_seed(i, ("bet", "pha", "lph")) for i in range(1, 301)])
        text = ("alphabet " * 5600)[:50000]
        t = time.perf_counter()
        eng.explain_recall(text, project=None)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_pattern_scans_once_per_distinct_condition(self):
        # 壁時計に頼らない費用の pin: 同じ縁を持つ種子 300 件でも、本文の走査は異なる縁ごとに 1 回
        eng = self.write([_art_seed(i, ("bet", "pha", "lph")) for i in range(1, 301)])
        calls = []
        real = alaya_mod._word_pattern

        def spy(key):
            pat = real(key)
            outer = pat

            class Counted:
                def search(self, text):
                    calls.append(key)
                    return outer.search(text)

            return Counted()

        with mock.patch.object(alaya_mod, "_word_pattern", spy):
            eng.explain_recall("alphabet bet", project=None)
            self.assertEqual(sorted(calls), ["bet", "lph", "pha"])
            calls.clear()
            eng.alaya.consolidation_view({None}, ["alphabet bet"])
            self.assertEqual(sorted(calls), ["bet", "lph", "pha"])


def _rel_seed(*spans):
    return {"evidences": [{"source": "user", "span": sp} for sp in spans]}


def _rand_ascii_cjk(rng, lo, hi, pool="ab_-./ アイ"):
    return "".join(rng.choice(pool) for _ in range(rng.randint(lo, hi)))


class TestIndexRelevanceWordStart(_StateCase):
    """T2 (設計書 §6.2): ③ の関わりでも、入力の ASCII の連なりは span の語頭に当たるときだけ数える。"""

    def test_ascii_run_inside_word_does_not_count(self):
        rel = alaya_mod._index_relevance
        self.assertEqual(rel(_rel_seed("let's start now"), {"art"}), 0)
        self.assertEqual(rel(_rel_seed("my art class"), {"art"}), 3)
        self.assertEqual(rel(_rel_seed("raiya/sansho.py"), {"sansho"}), 6)
        self.assertEqual(rel(_rel_seed("check the hooks"), {"hook"}), 4)   # 3 字以上の前方一致は残る (T1 と同じ限界)
        self.assertEqual(rel(_rel_seed("alphabet soup"), {"pha", "soup"}), 4)
        self.assertEqual(rel(_rel_seed("see ./src/app/x"), {"src/app"}), 7)
        self.assertEqual(rel(_rel_seed("メッセージキューは Kafka"), {"メッセージキュー", "kafka"}), 13)
        # 語頭の位置が多い片 (残りの連なりごとに調べる枝。上の値は語頭から引く枝を通る)
        self.assertEqual(rel(_rel_seed("a_b_c_d_e_f_start"), {"art"}), 0)
        self.assertEqual(rel(_rel_seed("a_b_c_d_e_f_art"), {"art"}), 3)
        self.assertEqual(rel(_rel_seed("a_b_c_d_e_f_hooks"), {"hook"}), 4)

    def test_matches_word_rule_on_random_fixture(self):
        rng = random.Random(8)
        for _ in range(3000):
            inp = _rand_ascii_cjk(rng, 3, 40)
            spans = [_rand_ascii_cjk(rng, 3, 60) for _ in range(2)]
            runs = alaya_mod._input_runs(inp)
            spans_n = [alaya_mod.sansho._n(sp).lower() for sp in spans]
            want = sum(len(r) for r in runs if any(alaya_mod._occurs_as_word(r, sp) for sp in spans_n))
            self.assertEqual(alaya_mod._index_relevance(_rel_seed(*spans), runs), want, (inp, spans))

    def test_run_at_word_start_agrees_with_occurs_as_word(self):
        rng = random.Random(8)
        for _ in range(3000):
            inp = _rand_ascii_cjk(rng, 3, 40)
            span = _rand_ascii_cjk(rng, 3, 60)
            runs = [r for r in alaya_mod._input_runs(inp) if ord(r[0]) < 128]
            pieces = [p for p in alaya_mod._run_groups(span) if ord(p[0]) < 128]
            for r in runs:
                for p in pieces:
                    self.assertEqual(alaya_mod._ascii_run_at_word_start(r, p), alaya_mod._occurs_as_word(r, p), (r, p))

    def test_both_branches_for_separator_runs_match_word_rule(self):
        # 区切りを内に含む連なりの 2 つの枝 (残りごとに片を調べる / 語頭の位置から引く) が、どちらも規則と一致する
        rng = random.Random(21)
        via_helper = via_enumeration = 0
        for _ in range(1500):
            inp = _rand_ascii_cjk(rng, 10, 60, pool="aab_-./ ")
            spans = [_rand_ascii_cjk(rng, 20, rng.choice((40, 400)), pool="aab_-./ ") for _ in range(2)]
            runs = alaya_mod._input_runs(inp)
            spans_n = [alaya_mod.sansho._n(sp).lower() for sp in spans]
            want = sum(len(r) for r in runs if any(alaya_mod._occurs_as_word(r, sp) for sp in spans_n))
            with mock.patch.object(alaya_mod, "_ascii_run_at_word_start",
                                   wraps=alaya_mod._ascii_run_at_word_start) as spy:
                got = alaya_mod._index_relevance(_rel_seed(*spans), runs)
            self.assertEqual(got, want, (inp, spans))
            if any("/" in r or "." in r or "-" in r or "_" in r for r in runs):
                if spy.call_count:
                    via_helper += 1
                else:
                    via_enumeration += 1
        self.assertGreater(via_helper, 30)
        self.assertGreater(via_enumeration, 30)

    def test_both_branches_for_plain_runs_match_word_rule(self):
        # 区切りを含まない連なりの 2 つの枝 (語の先頭部分を引く / 残りの連なりごとに片を調べる) が、どちらも規則と一致する。
        # 入力に区切りを入れない (= 区切りを内に含む連なりが無い) ので、`_ascii_run_at_word_start` を呼ぶのはこの枝だけ
        rng = random.Random(34)
        via_runs = via_tokens = 0
        for i in range(1500):
            few = i % 2 == 0
            letters = "abcdefghijklmnop"
            inp = (_rand_ascii_cjk(rng, 3, 3, pool=letters) + " " + _rand_ascii_cjk(rng, 6, 6, pool=letters) if few
                   else _rand_ascii_cjk(rng, 3, 60, pool="abcd "))
            span = _rand_ascii_cjk(rng, 1500, 2500, pool=letters + "._") if few else _rand_ascii_cjk(rng, 10, 80, pool="abcd._ ")
            runs = alaya_mod._input_runs(inp)
            span_n = alaya_mod.sansho._n(span).lower()
            want = sum(len(r) for r in runs if alaya_mod._occurs_as_word(r, span_n))
            with mock.patch.object(alaya_mod, "_ascii_run_at_word_start",
                                   wraps=alaya_mod._ascii_run_at_word_start) as spy:
                got = alaya_mod._index_relevance(_rel_seed(span), runs)
            self.assertEqual(got, want, (inp, span))
            if runs:
                if spy.call_count:
                    via_runs += 1
                else:
                    via_tokens += 1
        self.assertGreater(via_runs, 30, (via_runs, via_tokens))
        self.assertGreater(via_tokens, 100)

    def test_input_runs_never_reach_the_pattern_cache(self):
        # 守り: 長い入力の連なりで縁の正規表現のキャッシュを追い出さない
        with mock.patch.object(alaya_mod, "_word_pattern", side_effect=AssertionError("used")):
            self.assertEqual(alaya_mod._index_relevance(_rel_seed("my art class alphabet"), {"art", "pha"}), 3)

    def test_index_order_ignores_mid_word_runs(self):
        s1 = _seed(1, ["zzq1"], kind="決定", project=None, strength=2,
                   evidences=[{"source": "user", "span": "let's start now", "ledger_id": 1, "life": 1}])
        s2 = _seed(2, ["zzq2"], kind="決定", project=None, strength=1,
                   evidences=[{"source": "user", "span": "my art class", "ledger_id": 2, "life": 1}])
        eng = self.write([s1, s2])
        self.assertEqual([s["id"] for s in eng.explain_recall("art?", project=None)["index"]], [2, 1])

    def test_relevance_is_computed_once_per_candidate(self):
        seeds = [_seed(i, ["zzq%d" % i], kind="決定", project=None,
                       evidences=[{"source": "user", "span": "my art class", "ledger_id": i, "life": 1}])
                 for i in range(1, 16)]   # 決定 の上限 10 を超える = over の並べ直しが走る
        eng = self.write(seeds)
        with mock.patch.object(alaya_mod, "_index_relevance", wraps=alaya_mod._index_relevance) as spy:
            eng.explain_recall("art?", project=None)
        self.assertLessEqual(spy.call_count, 15)


def _fixture_a():
    return ("pha lph bet hab abe " * 2600)[:50000], "alphabet " * 250


def _fixture_b():
    return " ".join("a" * n for n in range(3, 201)), " ".join(["b" + "a" * 199] * 10)


def _fixture_c(ch):
    return (" ".join(".".join(ch for _ in range(k)) for k in range(2, 100)), ".".join([ch] * 1000))


def _fixture_d():
    rng = random.Random(1)
    inp = " ".join("".join(rng.choice("abc") for _ in range(rng.randint(3, 8))) for _ in range(10000))[:50000]
    span = "".join(rng.choice("abc._") for _ in range(2000))
    return inp, span


class TestIndexRelevanceLatency(_StateCase):
    """T2 (設計書 §6.2): 関わりの計算が sub-second の予算の中に在る。種子 300 件。"""

    def _time_direct(self, inp, span):
        runs = alaya_mod._input_runs(inp)
        lengths = sorted({len(r) for r in runs})
        seeds = [_rel_seed(span) for _ in range(300)]
        t = time.perf_counter()
        for seed in seeds:
            alaya_mod._index_relevance(seed, runs, lengths)
        return time.perf_counter() - t

    def test_mid_word_only_runs(self):
        self.assertLess(self._time_direct(*_fixture_a()), 1.0)

    def test_length_ladder_mid_word(self):
        self.assertLess(self._time_direct(*_fixture_b()), 1.0)

    def test_separator_dense_pieces(self):
        self.assertLess(self._time_direct(*_fixture_c("a")), 1.0)
        self.assertLess(self._time_direct(*_fixture_c("b")), 1.0)

    def test_separator_dense_long_piece_many_runs(self):
        # 区切りの多い 8000 字の片 × 語 4000 個の入力 (区切りを内に含む連なりは無い)。直接計算と explain_recall の両方
        rng = random.Random(3)
        inp = " ".join("".join(rng.choice("bcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(3, 8)))
                       for _ in range(4000))[:20000]
        span = "a." * 4000
        # 2.0: 全体実行の中では 1.0 をほぼ毎回超えた (単独 0.9 前後、2026-11-27 利用者裁定)
        self.assertLess(self._time_direct(inp, span), 2.0)
        eng = self.write([_seed(i, ["zzq%d" % i], kind="決定", project=None,
                                evidences=[{"source": "user", "span": span, "ledger_id": i, "life": 1}])
                          for i in range(1, 301)])
        t = time.perf_counter()
        eng.explain_recall(inp, project=None)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_long_distinct_tokens_many_evidences(self):
        # 長い語 40 個 (8000 字) を持つ user の span 3 本 × 種子 300 件 × 長さの梯子の入力
        rng = random.Random(5)
        inp = " ".join("a" * k for k in range(3, 201))
        runs = alaya_mod._input_runs(inp)
        lengths = sorted({len(r) for r in runs})
        spans = [".".join("b" + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(199)) for _ in range(40))
                 for _ in range(3)]
        seeds = [_rel_seed(*spans) for _ in range(300)]
        t = time.perf_counter()
        for seed in seeds:
            alaya_mod._index_relevance(seed, runs, lengths)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_separator_dense_huge_piece_few_seeds(self):
        # 5 万字の区切りの多い片 × 種子 20 (語の入力と、区切りを内に含む連なりの入力)
        rng = random.Random(3)
        words = " ".join("".join(rng.choice("bcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(3, 8)))
                         for _ in range(4000))[:20000]
        inp_sep, span = _fixture_c("a")[0], "a." * 25000
        for inp in (words, inp_sep):
            runs = alaya_mod._input_runs(inp)
            lengths = sorted({len(r) for r in runs})
            seed = _rel_seed(span)
            t = time.perf_counter()
            for _ in range(20):
                alaya_mod._index_relevance(seed, runs, lengths)
            self.assertLess(time.perf_counter() - t, 1.0)

    def test_random_ascii(self):
        self.assertLess(self._time_direct(*_fixture_d()), 1.0)

    def test_explain_recall_random_ascii(self):
        inp, span = _fixture_d()
        eng = self.write([_seed(i, ["zzq%d" % i], kind="決定", project=None,
                                evidences=[{"source": "user", "span": span, "ledger_id": i, "life": 1}])
                          for i in range(1, 301)])
        t = time.perf_counter()
        eng.explain_recall(inp, project=None)
        self.assertLess(time.perf_counter() - t, 1.0)


if __name__ == "__main__":
    unittest.main()
