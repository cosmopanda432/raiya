"""長い入力での recall の費用 (設計書 2026-11-25-long-input-recall-cost-design §6 T1〜T3) の pin。

③ の関わり (`alaya._index_relevance`・`_RunIndex`) と ① の札の判定の近道。値は変えない (乱数で素朴な定義と一致)。
LLM は使わない。テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。計時は 1 本ずつ直列に走らせる。
"""
import os
import random
import sys
import time
import unicodedata
import unittest
from unittest import mock

os.environ["RAIYA_CONFIG"] = ""
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import alaya  # noqa: E402
from raiya import manas as manas_mod  # noqa: E402
from test_index_reach import _naive  # noqa: E402
from test_layered_circles import _seed, _StateCase  # noqa: E402

LOWER = "abcdefghijklmnopqrstuvwxyz"


def _rel_seed(*spans):
    return {"evidences": [{"source": "user", "span": sp} for sp in spans]}


def _time_direct(inp, spans, n_seeds):
    runs = alaya._input_runs(inp)
    index = alaya._run_index(runs)   # 想起では 1 回だけ作って全候補に渡す (`_inject_index_plan` と同じ)
    seeds = [_rel_seed(*spans) for _ in range(n_seeds)]
    t = time.perf_counter()
    for seed in seeds:
        alaya._index_relevance(seed, runs, index=index)
    return time.perf_counter() - t


def _fixture_h():
    """形 H: 区切り入りの連なり × 4000 (全部区切り入り・長さ 6 種)、span `"a." * 4000`。"""
    rng = random.Random(11)
    words = lambda: "".join(rng.choice(LOWER) for _ in range(rng.randint(2, 4)))  # noqa: E731
    inp = " ".join(words() + "." + words() for _ in range(4000))[:20000]
    return inp, "a." * 4000


def _fixture_gb3():
    """形 Gb3: 入力 = b が 3〜200 個の語の梯子、span = "b" + 小文字 199 字の語 40 個を "." で継いだもの × 3 本。"""
    rng = random.Random(5)
    inp = " ".join("b" * k for k in range(3, 201))
    spans = [".".join("b" + "".join(rng.choice(LOWER) for _ in range(199)) for _ in range(40)) for _ in range(3)]
    return inp, spans


def _fixture_path_list():
    """path の一覧の貼り付け: 最初の語が全部 home の区切り入りの連なり 2000 個。"""
    rng = random.Random(13)

    def seg():
        return "".join(rng.choice(LOWER) for _ in range(rng.randint(3, 10)))

    paths = ["/home/dev/%s/%s/%s.py" % (seg(), seg(), seg()) for _ in range(2000)]
    inp = "\n".join(paths)
    other = ["/home/dev/%s/%s/%s.py" % (seg(), seg(), seg()) for _ in range(5)]
    pool = paths[:20] + other + ["を見る", "fix", "the"]
    span = " ".join(rng.choice(pool) for _ in range(40))[:3000]
    return inp, span


KATAKANA = "".join(chr(c) for c in range(0x30A2, 0x30F3))


def _fixture_hn():
    """形 Hn: 漢字の連なり 4989 個 (長さ 3 種) の入力 × カタカナ 8000 字の片。"""
    rng = random.Random(12)
    kanji = "".join(chr(0x4E00 + i) for i in range(2000))
    words = ["".join(rng.choice(kanji) for _ in range(rng.randint(2, 4))) for _ in range(5000)]
    inp = "は".join(words)[:20000]
    span = "".join(rng.choice(KATAKANA) for _ in range(8000))
    return inp, span


def _fixture_r3():
    """index-reach R3 (語 2〜20 字): カタカナの語を「は」で継いだ 20 万字の入力 × カタカナ 2000 字の片。"""
    rng = random.Random(7)
    words, total = [], 0
    while total < 200000:
        w = "".join(rng.choice(KATAKANA) for _ in range(rng.randint(2, 20)))
        words.append(w)
        total += len(w) + 1
    inp = "は".join(words)[:200000]
    span = "".join(rng.choice(KATAKANA) for _ in range(2000))
    return inp, span


class TestRunIndexAscii(_StateCase):
    def test_run_index_shape(self):
        ix = alaya._run_index({"kafka", "art", "src/app", "a.b.c", "メッセージキュー"})
        self.assertEqual(ix.plain, frozenset({"kafka", "art"}))
        self.assertEqual(ix.plain_lengths, {"kaf": (5,), "art": (3,)})
        self.assertEqual(ix.sep, frozenset({"src/app", "a.b.c"}))
        self.assertEqual(ix.sep_lengths, {"src": (7,), "a": (5,)})
        self.assertEqual(ix.lengths, (3, 5, 7, 8))

    def test_index_is_built_once_per_recall(self):
        seeds = [_seed(i, ["zzq%d" % i], kind="決定", project=None,
                       evidences=[{"source": "user", "span": "my art class", "ledger_id": i, "life": 1}])
                 for i in range(1, 16)]
        eng = self.write(seeds)
        with mock.patch.object(alaya, "_run_index", wraps=alaya._run_index) as spy:
            eng.explain_recall("art class kafka?", project=None)
        self.assertEqual(spy.call_count, 1)

    def test_long_mixed_fixture_matches_naive(self):
        rng = random.Random(25)
        for pool in ("ab_-./ アイ", "aab_ ", "ab./ アイウ漢字は", "ab_-./ "):
            for _ in range(2000):
                inp = "".join(rng.choice(pool) for _ in range(rng.randint(3, 60)))
                spans = ["".join(rng.choice(pool) for _ in range(rng.randint(3, rng.choice((60, 600)))))
                         for _ in range(2)]
                seed = _rel_seed(*spans)
                runs = alaya._input_runs(inp)
                self.assertEqual(alaya._index_relevance(seed, runs), _naive(seed, runs), (inp, spans))


class TestRelevanceLatencyAscii(_StateCase):
    def test_separator_runs_long_piece_600_seeds(self):
        inp, span = _fixture_h()
        self.assertLess(_time_direct(inp, [span], 600), 1.0)

    def test_word_ladder_long_tokens_600_seeds(self):
        inp, spans = _fixture_gb3()
        self.assertLess(_time_direct(inp, spans, 600), 1.0)

    def test_path_list_paste_against_path_spans(self):
        inp, span = _fixture_path_list()
        seeds = [_seed(i, ["zzq%d" % i], kind="決定", project=None,
                       evidences=[{"source": "user", "span": span, "ledger_id": i, "life": 1}])
                 for i in range(1, 301)]
        eng = self.write(seeds)
        t = time.perf_counter()
        eng.explain_recall(inp, project=None)
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRunIndexWide(unittest.TestCase):
    def test_run_index_wide_fields(self):
        ix = alaya._run_index({"kafka", "メッセージキュー", "設計"})
        self.assertEqual(ix.wide, frozenset({"メッセージキュー", "設計"}))
        self.assertEqual(ix.wide_lengths, {"メッ": (8,), "設計": (2,)})

    def test_small_alphabet_matches_naive(self):
        rng = random.Random(3)
        pool = [chr(c) for c in range(0x30A2, 0x30A8)]

        def word(lo, hi):
            return "".join(rng.choice(pool) for _ in range(rng.randint(lo, hi)))

        for _ in range(2000):
            inp = "は".join(word(1, 8) for _ in range(rng.randint(1, 10)))
            spans = ["は".join(word(1, 30) for _ in range(3)) for _ in range(2)]
            seed = _rel_seed(*spans)
            runs = alaya._input_runs(inp)
            self.assertEqual(alaya._index_relevance(seed, runs), _naive(seed, runs), (inp, spans))


class TestRelevanceLatencyWide(_StateCase):
    def test_many_wide_runs_long_piece(self):
        inp, span = _fixture_hn()
        self.assertLess(_time_direct(inp, [span], 300), 1.0)

    def test_short_wide_words_200k_input(self):
        inp, span = _fixture_r3()
        self.assertLess(_time_direct(inp, [span], 300), 1.0)

    def test_explain_recall_many_wide_runs(self):
        inp, span = _fixture_hn()
        seeds = [_seed(i, ["zzq%d" % i], kind="決定", project=None,
                       evidences=[{"source": "user", "span": span, "ledger_id": i, "life": 1}])
                 for i in range(1, 301)]
        eng = self.write(seeds)
        t = time.perf_counter()
        eng.explain_recall(inp, project=None)
        self.assertLess(time.perf_counter() - t, 1.0)


def _megachar_state_seeds(span, conditions, n=5):
    return [_seed(i, list(conditions), kind="決定", project=None, content="キュー",
                  evidences=[{"source": "user", "span": span, "ledger_id": i, "life": 1}])
            for i in range(1, n + 1)]


class TestJudgementShortcuts(_StateCase):
    def test_question_source_chars_are_complete(self):
        # `?` か `_FULLWIDTH_QUESTION` を NFKC の後に生む字の全部 (Unicode の版が上がって増えたらここで落ちる)
        want = {"?", alaya._FULLWIDTH_QUESTION}
        found = set()
        for c in range(0x110000):
            if 0xD800 <= c <= 0xDFFF:
                continue
            ch = chr(c)
            n = unicodedata.normalize("NFKC", ch.translate(alaya._FULLWIDTH_TABLE))
            produces = any(q in n for q in want)
            self.assertEqual(produces, ch in alaya._QUESTION_SOURCE_CHARS, hex(c))
            if produces:
                found.add(ch)
        self.assertEqual(found, set(alaya._QUESTION_SOURCE_CHARS))
        self.assertEqual(len(alaya._QUESTION_SOURCE_CHARS), 8)

    def test_shortcuts_match_full_path_on_random_fixture(self):
        rng = random.Random(4)
        chars = list("ab ?？﹖。.\n") + ["かも", "かもね", "Kafka", "kafka", "？"]
        cond_pool = ["kafka", "かも", "a", "b ab"]
        for _ in range(3000):
            evidences = []
            for _ in range(rng.randint(1, 3)):
                span = "".join(rng.choice(chars) for _ in range(rng.randint(1, 25)))
                source = "assistant" if rng.random() < 1 / 3 else "user"
                evidences.append({"source": source, "span": span, "life": 1, "ledger_id": 1})
            seed = {"kind": "決定", "conditions": rng.sample(cond_pool, 2), "evidences": evidences}
            users = [e for e in evidences if e["source"] == "user"]
            shown = users[-1:]
            got_d = alaya.doubt_judgement(seed, shown)
            got_c = alaya.conjecture_only(seed, shown)
            with mock.patch.object(alaya, "_may_have_question", return_value=True):
                want_d = alaya.doubt_judgement(seed, shown)
            with mock.patch.object(alaya, "_may_hold_conjecture", return_value=True):
                want_c = alaya.conjecture_only(seed, shown)
            self.assertEqual(got_d, want_d, seed)
            self.assertEqual(got_c, want_c, seed)

    def test_no_sentence_split_without_question_or_conjecture(self):
        seed = {"id": 1, "kind": "決定", "sansho": "圓成實", "strength": 1, "conditions": ["kafka"], "content": "キュー",
                "evidences": [{"source": "user", "span": "We use Kafka for the queue. It is fast.", "life": 1,
                               "ledger_id": 7}]}
        with mock.patch.object(alaya, "_sentence_pieces", wraps=alaya._sentence_pieces) as spy:
            forms = manas_mod._seed_line_forms(seed)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(forms[0], "- 決定: We use Kafka for the queue. It is fast.(強度1・要約: キュー・往復 7)")

    def test_split_still_runs_with_question(self):
        seed = {"id": 1, "kind": "決定", "sansho": "圓成實", "strength": 1, "conditions": ["kafka"], "content": "キュー",
                "evidences": [{"source": "user", "span": "Kafka を使う？", "life": 1, "ledger_id": 7}]}
        with mock.patch.object(alaya, "_sentence_pieces", wraps=alaya._sentence_pieces) as spy:
            forms = manas_mod._seed_line_forms(seed)
        self.assertGreaterEqual(spy.call_count, 1)
        self.assertIn("一部問い: kafka", forms[0])

    def test_five_megachar_spans_without_question(self):
        span = ("We use Kafka for the queue. It is fast and stable. " * 20000)[:1_000_000]
        eng = self.write(_megachar_state_seeds(span, ["kafka"]))
        t = time.perf_counter()
        result = eng.explain_recall("kafka", project=None)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(result["circles"]["shortened"]["name_only"], [1, 2, 3, 4, 5])

    def test_question_spans_still_judged_in_full(self):
        # 59 字の単位を 3389 回 (約 20 万字)。単位の途中で切ると最後の「Celery」が疑の文の外に出て札が立たない
        span = "We keep Kafka for storage here. Maybe try Celery for jobs? " * 3389
        eng = self.write(_megachar_state_seeds(span, ["kafka", "celery"]))
        t = time.perf_counter()
        result = eng.explain_recall("kafka", project=None)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertIn("一部問い: celery", result["narrative"])


if __name__ == "__main__":
    unittest.main()
