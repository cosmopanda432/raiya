"""#151 P1 (spec 2026-11-10-supersede-addition-design §7 規則 b): supersede 歴のある種子の ①/③ の行に札「前の言」。
描画のみ (種子・history・evidences は変えず、recall は台帳を読まない)。テストが消すのは自作 tmp だけ。"""
import contextlib
import copy
import io
import os
import shutil
import sys
import tempfile
import unittest

os.environ["RAIYA_CONFIG"] = ""
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import config, inspect_cli  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.manas import (  # noqa: E402
    INJECTION_MAX_CHARS, NARRATIVE_HEADER, ManasStore, _previous_saying, _render_entry_line, _render_index_line, _render_seed_line,
)

S1 = "このリポジトリでは今後、検査用の変更は src/ に入れず docs/x38-note.md に書くことにする。理由は、先週 src に残った検査用コメントで diff レビューが混乱したから。"
S2 = "検査用の行には必ず日付を付ける。docs/x38-note.md に「x38 S2 2026-11-10」を足して"
S3 = "検査用の行は日付つきで docs/x38-note.md にまとめる。"
HEADER_TAIL = "前の言 は同じ記録の 1 つ前の発言です。今の発言と食い違うときは今の発言に従ってください。"


def setUpModule():
    config._reset()


def _ev(source, span, life, lid):
    e = {"source": source, "span": span, "life": life}
    if lid is not None:
        e["ledger_id"] = lid
    return e


def _sup(life, reason="superseded"):
    return {"life": life, "content": "旧", "conditions": [], "sansho": "圓成實", "polarity": "無記", "reason": reason}


def _seed(evidences, history, kind="決定", sansho="圓成實", content="検査用の行には日付"):
    return {"id": 1, "kind": kind, "content": content, "strength": 2, "sansho": sansho,
            "conditions": ["検査用", "docs/x38-note.md"], "evidences": evidences, "history": history}


def _x38():
    return _seed([_ev("user", S1, 1, 1), _ev("user", S2, 2, 2)], [_sup(2)])


class TestPreviousSaying(unittest.TestCase):
    def test_x38_shape_seed_line_and_index_line(self):
        s = _x38()
        for line in (_render_seed_line(s), _render_index_line(s), _render_entry_line({"seed": s, "group": None})):
            self.assertTrue(line.endswith("・往復 2・前の言 (往復 1): {})".format(S1)), line)
            self.assertIn("理由は、先週 src に残った検査用コメントで", line)
        self.assertIn("・要約: 検査用の行には日付・往復 2・前の言", _render_seed_line(s))

    def test_injection_text_contains_it(self):
        text = ManasStore().compose_injection([_x38()])["text"]
        self.assertIn("・往復 2・前の言 (往復 1): " + S1 + ")", text)

    def test_reinforce_after_supersede_keeps_s1(self):
        s = _x38()
        s["evidences"].append(_ev("user", S3, 3, 3))
        self.assertTrue(_render_seed_line(s).endswith("・往復 3・前の言 (往復 1): {})".format(S1)))

    def test_same_life_has_no_tag_known_limit_g(self):
        s = _seed([_ev("user", S1, 2, 1), _ev("user", S2, 2, 2)], [_sup(2)])
        self.assertEqual(_previous_saying(s), "")
        self.assertNotIn("前の言", _render_seed_line(s))
        self.assertTrue(_render_seed_line(s).endswith("・往復 2)"))

    def test_no_superseded_history_is_unchanged(self):
        s = _seed([_ev("user", S1, 1, 1), _ev("user", S2, 2, 2)], [])
        line = _render_seed_line(s)
        self.assertNotIn("前の言", line)
        self.assertEqual(line, _render_seed_line(_seed(s["evidences"], [])))
        self.assertTrue(line.endswith("・往復 2)"))

    def test_other_reasons_have_no_tag(self):
        for reason in ("superseded_by", "withdrawn", "restored"):
            s = _seed([_ev("user", S1, 1, 1), _ev("user", S2, 2, 2)], [_sup(2, reason)])
            self.assertNotIn("前の言", _render_seed_line(s), reason)

    def test_two_supersedes_use_last_life_and_s2(self):
        s = _seed([_ev("user", S1, 1, 1), _ev("user", S2, 2, 2), _ev("user", S3, 3, 3)], [_sup(2), _sup(3)])
        line = _render_seed_line(s)
        self.assertTrue(line.endswith("・往復 3・前の言 (往復 2): {})".format(S2)), line)

    def test_multiple_spans_same_round_joined_deduped_collapsed(self):
        s = _seed([_ev("user", "前の  言 A\n続き", 1, 4), _ev("user", "別の　言 B", 1, 4),
                   _ev("user", "前の 言 A 続き", 1, 4), _ev("user", S2, 2, 5)], [_sup(2)])
        self.assertEqual(_previous_saying(s), "・前の言 (往復 4): 前の 言 A 続き … 別の 言 B")

    def test_assistant_only_and_bad_ledger_id_are_not_previous(self):
        s = _seed([_ev("assistant", "了解しました", 1, 1), _ev("user", S2, 2, 2)], [_sup(2)])
        self.assertEqual(_previous_saying(s), "")
        old = _seed([_ev("user", S1, 1, None), dict(_ev("user", S1, 1, True)), _ev("user", S2, 2, 2)], [_sup(2)])
        self.assertEqual(_previous_saying(old), "")
        self.assertNotIn("前の言", _render_seed_line(old))

    def test_resume_point_and_no_recency_tag_lines_have_none(self):
        r = _seed([_ev("user", S1, 1, 1), _ev("user", S2, 2, 2)], [_sup(2)], kind="再開点", content="次 = 実装")
        self.assertNotIn("前の言", _render_seed_line(r))
        self.assertNotIn("前の言", _render_index_line(r))
        self.assertNotIn("前の言 (", ManasStore().compose_injection([], resume=r)["text"])
        old = _seed([_ev("user", S1, 1, 1), _ev("user", S2, 2, None)], [_sup(2)])
        self.assertNotIn("往復", _render_seed_line(old))   # S2 に ledger_id が無く、札自体が出ない行
        self.assertNotIn("前の言", _render_seed_line(old))

    def test_malformed_shapes_do_not_raise(self):
        evs = [_ev("user", S1, 1, 1), _ev("user", S2, 2, 2)]
        bad = [
            _seed(evs, {"life": 2, "reason": "superseded"}), _seed(evs, 5), _seed(evs, ["x", 3, None]),
            _seed(evs, [{"life": "2", "reason": "superseded"}]), _seed(evs, [{"life": True, "reason": "superseded"}]),
            _seed(evs, [{"life": None, "reason": "superseded"}]), _seed(evs, [{"reason": "superseded"}]),
            _seed("zzz", [_sup(2)]), _seed(5, [_sup(2)]), _seed({"a": 1}, [_sup(2)]), _seed(None, [_sup(2)]),
            _seed([None, "x", 3, _ev("user", S2, 2, 2)], [_sup(2)]),
        ]
        for s in bad:
            self.assertEqual(_previous_saying(s), "", s)
            if isinstance(s["evidences"], int):   # evidences が int の描画は既存の別規則 (本件の範囲外)
                continue
            self.assertNotIn("前の言", _render_seed_line(s), s)
            self.assertNotIn("前の言", _render_index_line(s), s)
        s = _seed([dict(_ev("user", S1, "1", 1)), dict(_ev("user", S1, True, 1)), _ev("user", S2, 2, 2)], [_sup(2)])
        self.assertEqual(_previous_saying(s), "")

    def test_does_not_mutate_seed(self):
        s = _x38()
        before = copy.deepcopy(s)
        _render_seed_line(s)
        _render_index_line(s)
        ManasStore().compose_injection([s])
        self.assertEqual(s, before)

    def test_doubt_keep_conjecture_unchanged_by_previous_saying(self):
        # 前の世代にだけ断定がある 決定: 最新の往復の逐語だけを読む判定は変わらず、前の言が付くだけ。
        prev = "Redis で決めた。"
        now = "Redis かもしれない。"
        with_hist = _seed([_ev("user", prev, 1, 1), _ev("user", now, 2, 2)], [_sup(2)], content="Redis 採用")
        with_hist["conditions"] = ["Redis"]
        without = copy.deepcopy(with_hist)
        without["history"] = []
        a, b = _render_seed_line(with_hist), _render_seed_line(without)
        suffix = "・前の言 (往復 1): {}".format(prev)
        self.assertTrue(a.endswith(suffix + ")"), a)
        self.assertEqual(a.replace(suffix, ""), b)
        ai, bi = _render_index_line(with_hist), _render_index_line(without)
        self.assertEqual(ai.replace(suffix, ""), bi)
        self.assertTrue(b.startswith("- 決定(推量):"), b)

    def test_header_ends_with_new_sentence(self):
        self.assertTrue(NARRATIVE_HEADER.endswith("往復 N は記録された順で、大きいほど後です。" + HEADER_TAIL))
        self.assertTrue(ManasStore().compose_injection([_x38()])["text"].startswith(NARRATIVE_HEADER))

    def test_doubt_only_line_with_previous_saying(self):
        s = _seed([_ev("user", "Redis で決めた？", 1, 1), _ev("user", "Redis にする？", 2, 2)], [_sup(2)], content="Redis 採用")
        s["conditions"] = ["Redis"]
        line = _render_seed_line(s)
        self.assertTrue(line.endswith("(強度2・一部問い: Redis・往復 2・前の言 (往復 1): Redis で決めた？)"), line)
        self.assertNotIn("要約", line)

    def test_keep_judgement_ignores_previous_saying(self):
        with_hist = _seed([_ev("user", "Memcached は使わない。", 1, 1), _ev("user", "Redis にする？", 2, 2)], [_sup(2)],
                          content="Redis 採用")
        with_hist["conditions"] = ["Redis", "Memcached"]
        without = copy.deepcopy(with_hist)
        without["history"] = []
        suffix = "・前の言 (往復 1): Memcached は使わない。"
        a, b = _render_seed_line(with_hist), _render_seed_line(without)
        self.assertIn("要約: Redis 採用", a)
        self.assertEqual(a.replace(suffix, ""), b)
        self.assertNotEqual(a, b)

    def test_mixed_place_line_with_previous_saying(self):
        s = _seed([_ev("user", "/srv/a/b にある", 1, 1), _ev("user", "/srv/a/c にある", 2, 2),
                   _ev("assistant", "/srv/a/c ですね", 2, 2)], [_sup(2)], kind="所在", sansho="依他起", content="/srv/a/c")
        line = _render_seed_line(s)
        self.assertEqual(line, "- 所在: /srv/a/c(強度2・逐語: /srv/a/c にある・一部推定・往復 2・前の言 (往復 1): /srv/a/b にある)")

    def test_group_suffix_follows_paren(self):
        s = _x38()
        group = {"label": "検査用", "size": 2}
        line = _render_entry_line({"seed": s, "group": group})
        self.assertTrue(line.endswith("・前の言 (往復 1): {}) [群「検査用」2 件・代表 #1]".format(S1)), line)

    def test_previous_equal_to_current_round_has_no_tag(self):
        evs = [_ev("user", S1, 1, 1), _ev("assistant", "了解", 2, 2)]
        with_hist = _seed(evs, [_sup(2)])
        without = _seed(evs, [])
        self.assertEqual(_previous_saying(with_hist), "")
        self.assertEqual(_render_seed_line(with_hist), _render_seed_line(without))
        self.assertEqual(_render_index_line(with_hist), _render_index_line(without))

    def test_evidence_order_not_tuple_max(self):
        s = _seed([_ev("user", "五番目", 1, 5), _ev("user", "三番目", 1, 3), _ev("user", S2, 2, 9)], [_sup(2)])
        self.assertEqual(_previous_saying(s), "・前の言 (往復 3): 三番目")

    def test_bool_float_life_not_joined(self):
        s = _seed([_ev("user", "本物", 1, 1), dict(_ev("user", "bool", True, 1)), dict(_ev("user", "float", 1.0, 1)),
                   _ev("user", S2, 2, 2)], [_sup(2)])
        self.assertEqual(_previous_saying(s), "・前の言 (往復 1): 本物")
        # 末尾の evidence が float の life でも、(1, 1) の選択を奪わず連結にも混ざらない
        s2 = _seed([_ev("user", "本物", 1, 1), dict(_ev("user", "float", 1.0, 2)), _ev("user", S2, 2, 3)], [_sup(2)])
        self.assertEqual(_previous_saying(s2), "・前の言 (往復 1): 本物")

    def test_long_previous_saying_becomes_name_only(self):
        # 6b (T3): 長い 前の言 を持つ行は、入らなければ名だけの形 (前の言 と要約は付けない) で描かれ、以後の ①③④ も残る
        long_prev = "あ" * 3100
        long_seed = _seed([_ev("user", long_prev, 1, 1), _ev("user", S2, 2, 2)], [_sup(2)])
        out = ManasStore().compose_injection([long_seed])
        self.assertLessEqual(len(out["text"]), INJECTION_MAX_CHARS)
        self.assertEqual(out["cut"]["germinated"], 0)
        self.assertEqual(out["shown_seed_ids"], [long_seed["id"]])
        self.assertIn("容量で省略", out["text"])
        self.assertNotIn("前の言 (", out["text"])
        # 対照: history が無ければ同じ種子の行は描かれる
        plain = dict(long_seed, history=[])
        out_plain = ManasStore().compose_injection([plain])
        self.assertEqual(out_plain["shown_seed_ids"], [1])
        self.assertIn(S2, out_plain["text"])
        # 順: 長い種子は名だけで描かれるので、短い種子が前でも後ろでも両方残る (6b。前は後ろの短い種子も落ちた)
        short = dict(_seed([_ev("user", "短い決定の逐語", 1, 5)], []), id=2)
        first = ManasStore().compose_injection([short, long_seed])
        self.assertEqual(first["shown_seed_ids"], [2, 1])
        self.assertIn("短い決定の逐語", first["text"])
        last = ManasStore().compose_injection([long_seed, short])
        self.assertEqual(last["shown_seed_ids"], [1, 2])
        self.assertIn("短い決定の逐語", last["text"])
        self.assertEqual(last["shortened"], {"no_summary": [], "name_only": [1]})


class TestPreviousSayingThroughEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _imprint(self, user, lid, extra=None, new_life=True):
        e = MemoryEngine(state_dir=self.tmp, new_life=new_life)
        p = {"content": "検査用の行には日付", "conditions": ["検査用", "docs/x38-note.md"], "polarity": "無記",
             "kind": "決定", "evidence": [{"source": "user", "span": user}]}
        p.update(extra or {})
        return e.imprint([LedgerTurn(user_text=user, assistant_text="了解", id=lid)], [p])[0]

    def test_recall_shows_previous_saying_and_explain_matches(self):
        r1 = self._imprint(S1, 1, {"content": "検査用の変更は docs/x38-note.md", "conditions": ["src", "docs/x38-note.md", "検査用"]})
        r2 = self._imprint(S2, 2, {"action": "supersede", "target_id": r1["seed"]["id"]})
        self.assertTrue(r2.get("superseded"), r2)
        e = MemoryEngine(state_dir=self.tmp, new_life=False)
        out = e.recall("検査用 docs/x38-note.md")
        self.assertIn("・往復 2・前の言 (往復 1): " + S1, out["narrative"])
        self.assertEqual(out["narrative"], e.explain_recall("検査用 docs/x38-note.md")["narrative"])
        self.assertTrue(out["narrative"].startswith(NARRATIVE_HEADER))
        # --json 相当 (inspect_seeds) の形に新しいキーは無い
        seeds = e.inspect_seeds()
        for s in seeds:
            self.assertFalse([k for k in s if "前の言" in k or "previous" in k], s)
        self.assertNotIn("前の言", str(seeds))

    def test_inspect_json_shape_unchanged_and_cli_show_text_has_tag(self):
        r1 = self._imprint(S1, 1, {"content": "検査用の変更は docs/x38-note.md", "conditions": ["src", "docs/x38-note.md", "検査用"]})
        e0 = MemoryEngine(state_dir=self.tmp, new_life=False)
        keys_before = [set(s) for s in e0.inspect_seeds()]
        self._imprint(S2, 2, {"action": "supersede", "target_id": r1["seed"]["id"]})
        e = MemoryEngine(state_dir=self.tmp, new_life=False)
        seeds = e.inspect_seeds()
        self.assertTrue(seeds)
        known = set.union(*keys_before) | {"history"}
        for s in seeds:   # supersede 後も、supersede 前に在ったキーと history 以外のキーは増えない (前の言 は描画だけ)
            self.assertLessEqual(set(s), known, sorted(set(s) - known))
        self.assertNotIn("前の言", str(seeds))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inspect_cli.main(["--state-dir", self.tmp, "show", "--all"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("・前の言 (往復 1): " + S1, out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inspect_cli.main(["--state-dir", self.tmp, "--json", "show", "--all"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotIn("前の言", out.getvalue())
        self.assertNotIn("previous", out.getvalue())


if __name__ == "__main__":
    unittest.main()
