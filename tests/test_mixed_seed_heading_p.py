"""spec 2026-10-30-mixed-seed-heading §7 P: 混在 (依他起 + user 接地の span) の 所在・性向 は見出しを content (所在 は 處) に立て、
user の逐語は「逐語:」の札。描画 (manas) だけの変更で、保存・sansho・imprint は触らない。
LLM は使わない。テストが消すのは自作 tmp だけ (mkdtemp)。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.manas import _render_index_line, _render_seed_line  # noqa: E402

PROJ = "/home/dev/raiya"
Q = "前に決めた DB はどれだった？"


class _Case(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".raiya"))
        patcher = mock.patch.dict(os.environ, {"HOME": home})
        patcher.start()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.addCleanup(patcher.stop)
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.environ.pop(INJECT_DISPOSITIONS_ENV, None)
        self.eng = MemoryEngine(state_dir=self.tmpdir)
        self.dbpath = os.path.join(self.tmpdir, "target.db")  # 休眠で外れないよう実在させる
        open(self.dbpath, "w").close()

    def plant(self, content, conditions, kind, user_span, assistant_span, twice=True):
        """user の span + assistant の evidence = 依他起 の混在を real engine で植える (依他起は強度 2 で発芽)。"""
        seed = None
        for _ in range(2 if twice else 1):
            turn = LedgerTurn(user_text=user_span, assistant_text=assistant_span, id=0, project=PROJ)
            prop = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
                    "evidence": [{"source": "user", "span": user_span}, {"source": "assistant", "span": assistant_span}]}
            seed = self.eng.imprint([turn], [prop])[0]["seed"]
            self.assertEqual(seed["sansho"], "依他起", seed)
        return seed

    def recall(self, text, **kw):
        return self.eng.recall(text, project=PROJ, **kw)

    def body(self, r):
        """narrative から見出しの枠を除いた「- 」で始まる行だけ。"""
        return [ln for ln in r["narrative"].split("\n") if ln.startswith("- ")]


Q2 = "前に決めた DB は SQLite だった？"


class TestMixedHeadingP(_Case):
    def test_1_mixed_place_heading_is_place_with_verbatim_tag(self):
        self.plant("DB は " + self.dbpath, ["DB", "SQLite"], "所在", Q2, self.dbpath)
        r = self.recall("DB SQLite の話")
        self.assertEqual([s["kind"] for s in r["seeds"]], ["所在"])
        self.assertEqual(self.body(r), ["- 所在: {}(強度2・逐語: {}・一部推定)".format(self.dbpath, Q2)])
        body = "\n".join(self.body(r))      # 前書きの「要約は…照合されていません」(#130 案 A) は本文でない
        self.assertNotIn("要約", body)
        self.assertNotIn("(推定)", body)

    def test_2_mixed_place_without_path_or_url_uses_content(self):
        self.plant("DB の置き場は設定に書いてある", ["DB", "SQLite"], "所在", Q2, "設定に書いてある")
        r = self.recall("DB SQLite の話")
        self.assertEqual(self.body(r), ["- 所在: DB の置き場は設定に書いてある(強度2・逐語: {}・一部推定)".format(Q2)])

    def test_2b_mixed_place_path_and_url_in_order(self):
        self.plant("見る先は {} と https://example.com/db".format(self.dbpath), ["DB", "SQLite"], "所在", Q2,
                   self.dbpath)
        r = self.recall("DB SQLite の話")
        self.assertEqual(self.body(r), ["- 所在: {}・https://example.com/db(強度2・逐語: {}・一部推定)".format(self.dbpath, Q2)])

    def test_3_mixed_disposition_heading_is_content(self):
        os.environ[INJECT_DISPOSITIONS_ENV] = "1"
        self.addCleanup(os.environ.pop, INJECT_DISPOSITIONS_ENV, None)
        q = "簡潔に日本語で答えてほしい？"
        self.plant("回答は簡潔な日本語で", ["簡潔", "日本語"], "性向", q, "回答は簡潔な日本語で")
        r = self.recall("簡潔 日本語")
        self.assertEqual(self.body(r), ["- 性向: 回答は簡潔な日本語で(強度2・逐語: {}・一部推定)".format(q)])

    def test_4_mixed_decision_keeps_user_verbatim_as_main(self):
        user = "B ではなく A を採用 採用"
        self.plant("B ではなく A を採用", ["採用", "A"], "決定", user, "了解しました、A でいきます")
        r = self.recall("A 採用 について")
        self.assertEqual(self.body(r), ["- 決定: {}(強度2・要約: B ではなく A を採用・一部推定)".format(user)])
        self.assertNotIn("逐語:", r["narrative"])

    def test_7_index_line_has_the_same_form_as_germinated(self):
        self.plant("DB は " + self.dbpath, ["DB", "SQLite"], "所在", Q2, self.dbpath)
        germinated = self.body(self.recall("DB SQLite の話"))[0]
        r = self.recall("別の話")
        self.assertEqual(r["seeds"], [])
        indexed = self.body(r)
        self.assertEqual(indexed, ["- 所在: {} (強度2・逐語: {}・一部推定)".format(self.dbpath, Q2)])
        # ① と ③ の違いは括弧の前の空白 1 つだけ
        self.assertEqual(germinated.replace("(強度", " (強度"), indexed[0])

    def test_9_explain_recall_equals_recall(self):
        self.plant("DB は " + self.dbpath, ["DB", "SQLite"], "所在", Q2, self.dbpath)
        for text in ("DB SQLite の話", "別の話"):
            r = self.recall(text)
            e = self.eng.explain_recall(text, project=PROJ)
            self.assertEqual(e["narrative"], r["narrative"])
            self.assertIn("逐語:", r["narrative"])

    def test_render_does_not_change_stored_seed(self):
        seed = self.plant("DB は " + self.dbpath, ["DB", "SQLite"], "所在", Q2, self.dbpath)
        self.recall("DB SQLite の話")
        again = self.eng.imprint(
            [LedgerTurn(user_text=Q2, assistant_text=self.dbpath, id=0, project=PROJ)],
            [{"content": "DB は " + self.dbpath, "conditions": ["DB", "SQLite"], "polarity": "無記", "kind": "所在",
              "evidence": [{"source": "user", "span": Q2}, {"source": "assistant", "span": self.dbpath}]}])[0]["seed"]
        self.assertEqual(again["id"], seed["id"])  # 描画に依らず同じ種子へ併合される
        self.assertEqual(again["content"], seed["content"])


class TestPureRender(unittest.TestCase):
    """sansho 欄・evidence で描画が決まる純関数側 (manas の 2 関数)。"""

    @staticmethod
    def seed(kind, sansho, evidences, content="/home/dev/a/b.py にある"):
        s = {"id": 1, "kind": kind, "content": content, "strength": 1, "conditions": ["縁A"], "evidences": evidences}
        if sansho is not None:
            s["sansho"] = sansho
        return s

    U = {"source": "user", "span": "どこ？", "life": 1, "ledger_id": 3}
    A = {"source": "assistant", "span": "/home/dev/a/b.py", "life": 1, "ledger_id": 3}

    def lines(self, seed):
        return _render_seed_line(seed), _render_index_line(seed)

    def test_5_enjosshitsu_place_unchanged(self):
        s = self.seed("所在", "圓成實", [{"source": "user", "span": "/home/dev/a/b.py にある"}])
        self.assertEqual(self.lines(s), ("- 所在: /home/dev/a/b.py にある(強度1・要約: /home/dev/a/b.py にある)",
                                         "- 所在: /home/dev/a/b.py にある (強度1・要約: /home/dev/a/b.py にある)"))

    def test_6_user_spanless_place_keeps_H(self):
        s = self.seed("所在", "依他起", [self.A])
        self.assertEqual(self.lines(s), ("- 所在(推定): /home/dev/a/b.py(強度1・往復 3)", "- 所在(推定): /home/dev/a/b.py (強度1・往復 3)"))

    def test_8_resume_point_unchanged(self):
        s = self.seed("再開点", "依他起", [self.U, self.A], content="次 = 実装")
        self.assertEqual(self.lines(s), ("- 再開点(推定): 次 = 実装(強度1)", "- 再開点(推定): 次 = 実装 (強度1)"))

    def test_mixed_place_pure(self):
        s = self.seed("所在", "依他起", [self.U, self.A])
        self.assertEqual(self.lines(s), ("- 所在: /home/dev/a/b.py(強度1・逐語: どこ？・一部推定・往復 3)",
                                         "- 所在: /home/dev/a/b.py (強度1・逐語: どこ？・一部推定・往復 3)"))

    def test_mixed_without_kind_renders_as_disposition_P_form(self):
        for bad in ("missing", "不正な種類"):
            s = self.seed("性向", "依他起", [self.U, self.A], content="簡潔に")
            if bad == "missing":
                del s["kind"]
            else:
                s["kind"] = bad
            self.assertEqual(self.lines(s), ("- 性向: 簡潔に(強度1・逐語: どこ？・一部推定・往復 3)",
                                             "- 性向: 簡潔に (強度1・逐語: どこ？・一部推定・往復 3)"))

    def test_mixed_without_assistant_evidence_has_no_partial_mark(self):
        s = self.seed("性向", "依他起", [self.U], content="簡潔に")
        self.assertEqual(self.lines(s), ("- 性向: 簡潔に(強度1・逐語: どこ？・往復 3)", "- 性向: 簡潔に (強度1・逐語: どこ？・往復 3)"))

    def test_legacy_without_sansho_unchanged(self):
        s = self.seed("所在", None, [self.U, self.A])
        self.assertEqual(self.lines(s), ("- 所在: /home/dev/a/b.py にある(強度1・往復 3)", "- 所在: /home/dev/a/b.py にある (強度1・往復 3)"))

    def test_10_whitespace_collapse_and_joiner_inside_tag(self):
        u1 = {"source": "user", "span": "どこ  に\n  あった？", "life": 1, "ledger_id": 3}
        u2 = {"source": "user", "span": "\u3000あれ\tだよ", "life": 1, "ledger_id": 3}
        s = self.seed("性向", "依他起", [u1, u2, self.A], content="簡潔  に\n答える")
        self.assertEqual(self.lines(s), ("- 性向: 簡潔 に 答える(強度1・逐語: どこ に あった？ … あれ だよ・一部推定・往復 3)",
                                         "- 性向: 簡潔 に 答える (強度1・逐語: どこ に あった？ … あれ だよ・一部推定・往復 3)"))


if __name__ == "__main__":
    unittest.main()
