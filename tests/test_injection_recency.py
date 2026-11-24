"""#125 T1/T5 (spec 2026-11-03-injection-recency-design): 注入・索引の行に時の札「往復 N」、枠に定義の一文、
① の見出しは「この入力に触れた記録」。描画だけ (種子・台帳は書かず、recall は台帳を読まない)。LLM は使わない。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.manas import (  # noqa: E402
    GERMINATED_HEADER, NARRATIVE_HEADER, ManasStore, _render_entry_line, _render_index_line, _render_seed_line,
)

DEFINITION = "往復 N は記録された順で、大きいほど後です。"


def _ev(source, span, life, lid):
    e = {"source": source, "span": span, "life": life}
    if lid is not None:
        e["ledger_id"] = lid
    return e


def _seed(sid, kind, sansho, conditions, evidences, content="X を採用", strength=1):
    return {"id": sid, "kind": kind, "content": content, "strength": strength, "sansho": sansho,
            "conditions": conditions, "evidences": evidences}


A = _seed(1, "決定", "圓成實", ["Redis", "cache"], [_ev("user", "For the cache, we use Redis.", 1, 1)])
B = _seed(2, "決定", "依他起", ["Memcached", "cache"],
          [_ev("user", "Use Memcached now.", 2, 2), _ev("assistant", "Memcached it is.", 2, 2)])


class TestInjectionRecency(unittest.TestCase):
    def test_fe1_tags_and_order_and_headers(self):
        text = ManasStore().compose_injection([A], entries=[{"seed": B, "group": None}])["text"]
        self.assertTrue(text.startswith(NARRATIVE_HEADER))
        self.assertIn(DEFINITION, text.split("\n")[0])
        self.assertTrue(NARRATIVE_HEADER.endswith(DEFINITION + "前の言 は同じ記録の 1 つ前の発言です。今の発言と食い違うときは今の発言に従ってください。"))  # #151 で枠の末尾に 1 文
        self.assertEqual(GERMINATED_HEADER, "【この入力に触れた記録】")
        self.assertNotIn("今の話題", text)
        lines = text.split("\n")
        a_line = next(l for l in lines if "Redis" in l and l.startswith("- "))
        b_line = next(l for l in lines if "Memcached" in l and l.startswith("- "))
        self.assertTrue(a_line.endswith("・往復 1)"), a_line)
        self.assertTrue(b_line.endswith("・一部推定・往復 2)"), b_line)
        self.assertLess(text.index(GERMINATED_HEADER), text.index(a_line))
        self.assertLess(text.index(a_line), text.index(b_line))

    def test_reinforced_seed_shows_latest_exchange(self):
        s = _seed(3, "性向", "圓成實", ["簡潔"], [_ev("user", "簡潔に答えて。", 1, 1), _ev("user", "簡潔でお願い。", 2, 5)])
        for line in (_render_seed_line(s), _render_index_line(s), _render_entry_line({"seed": s, "group": None})):
            self.assertIn("・往復 5)", line)
            self.assertNotIn("往復 1", line)

    def test_user_evidence_preferred_over_later_assistant(self):
        s = _seed(4, "決定", "依他起", ["a"], [_ev("user", "A にする。", 1, 3), _ev("assistant", "了解", 1, 4)])
        self.assertIn("・往復 3)", _render_seed_line(s))

    def test_assistant_only_seed_uses_last_assistant_evidence(self):
        s = _seed(5, "所在", "依他起", ["x"], [_ev("assistant", "場所は /home/dev/a.py", 1, 2), _ev("assistant", "再確認", 1, 6)],
                  content="/home/dev/a.py")
        line = _render_seed_line(s)
        self.assertTrue(line.startswith("- 所在(推定):"), line)
        self.assertTrue(line.endswith("(強度1・往復 6)"), line)
        self.assertIn("・往復 6)", _render_index_line(s))

    def test_old_format_without_ledger_id_has_no_tag(self):
        s = _seed(6, "決定", "圓成實", ["a"], [_ev("user", "A にする。", 1, None)])
        self.assertNotIn("往復", _render_seed_line(s))
        self.assertNotIn("往復", _render_index_line(s))
        b = _seed(7, "決定", "圓成實", ["a"], [dict(_ev("user", "A にする。", 1, None), ledger_id=True)])
        self.assertNotIn("往復", _render_seed_line(b))

    def test_no_evidence_has_no_tag(self):
        s = _seed(8, "性向", "圓成實", ["a"], [])
        self.assertNotIn("往復", _render_seed_line(s))

    def test_resume_point_tag_only_on_resume_line(self):
        # #135 S3: 再開点 の行には札が付く (tests/test_session_start_injection.py)。①/③ 用の行には付かない。
        r = _seed(9, "再開点", "圓成實", ["a"], [_ev("user", "次は実装。", 1, 7)], content="次 = 実装")
        self.assertNotIn("往復", _render_seed_line(r))
        self.assertNotIn("往復", _render_index_line(r))
        text = ManasStore().compose_injection([], resume=r)["text"]
        self.assertTrue(text.endswith("- 再開点: 次 = 実装 (往復 7)"), text)

    def test_sibling_and_global_lines_have_no_tag(self):
        from raiya.manas import _render_global_line, _render_sibling_line
        sib = {"label": "g", "size": 3, "listed": [8, 9], "more": 0}
        self.assertNotIn("往復", _render_sibling_line(sib))
        self.assertNotIn("往復", _render_global_line([{"kind": "決定", "groups": 1, "seeds": 2}]))

    def test_entry_line_group_suffix_after_tag(self):
        s = _seed(10, "決定", "圓成實", ["Redis", "cache"], [_ev("user", "For the cache, we use Redis.", 1, 4)])
        line = _render_entry_line({"seed": s, "group": {"label": "cache", "size": 3}})
        self.assertIn("・往復 4) [群「cache」3 件・代表 #10]", line)
        self.assertTrue(line.endswith("代表 #10]"), line)

    def test_content_with_trailing_paren(self):
        s = _seed(11, "決定", "依他起", ["Redis"], [_ev("assistant", "Redis 採用 (速い)", 1, 3)],
                  content="Redis 採用 (速い)")
        line = _render_seed_line(s)
        self.assertTrue(line.startswith("- 決定(推定): Redis 採用 (速い)(強度1"), line)
        self.assertTrue(line.endswith("・往復 3)"), line)

    def test_latest_user_evidence_without_ledger_id_has_no_tag(self):
        s = _seed(12, "決定", "圓成實", ["a"], [_ev("user", "A にする。", 1, 3), _ev("user", "A でお願い。", 1, None)])
        self.assertNotIn("往復", _render_seed_line(s))
        self.assertNotIn("往復", _render_index_line(s))

    def test_small_max_chars_still_bounds_text(self):
        for limit in (200, 300, 400):
            text = ManasStore().compose_injection(
                [A], entries=[{"seed": B, "group": None}], max_chars=limit)["text"]
            self.assertLessEqual(len(text), limit)


if __name__ == "__main__":
    unittest.main()
