import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests._state_fixture import manas_store_at  # noqa: E402
from raiya.manas import (  # noqa: E402
    EGO_WEIGHT_FLOOR,
    GERMINATED_HEADER,
    INITIAL_EGO_WEIGHT,
    INTROSPECTION_DECREASE,
    NARRATIVE_HEADER,
    ManasStore,
    _render_index_line,
    _render_seed_line,
)


class TestManas(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "manas.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_initial_ego_weight(self):
        store = manas_store_at(self.path)
        self.assertEqual(store.ego_weight, INITIAL_EGO_WEIGHT)

    def test_build_narrative_empty_without_seeds(self):
        store = manas_store_at(self.path)
        self.assertEqual(store.build_narrative([]), "")

    def test_build_narrative_includes_seed_content(self):
        store = manas_store_at(self.path)
        seeds = [{"id": 1, "content": "猫好き", "strength": 3}]
        text = store.build_narrative(seeds)
        self.assertIn("猫好き", text)

    def test_inject_logs_entry_with_seed_ids(self):
        store = manas_store_at(self.path)
        seeds = [{"id": 1, "content": "猫好き", "strength": 3}]
        text = store.inject(seeds, life=1)
        self.assertEqual(len(store.recent_log()), 1)
        entry = store.recent_log()[0]
        self.assertEqual(entry["life"], 1)
        self.assertEqual(entry["seed_ids"], [1])
        self.assertIn("猫好き", text)

    def test_reduce_ego_weight_decreases_by_configured_amount(self):
        store = manas_store_at(self.path)
        new_weight = store.reduce_ego_weight()
        self.assertAlmostEqual(new_weight, INITIAL_EGO_WEIGHT - INTROSPECTION_DECREASE)

    def test_ego_weight_floors_out(self):
        store = manas_store_at(self.path)
        for _ in range(20):
            store.reduce_ego_weight()
        self.assertEqual(store.ego_weight, EGO_WEIGHT_FLOOR)

    def test_narrative_seed_count_scales_with_ego_weight(self):
        store = manas_store_at(self.path)
        seeds = [{"id": i, "content": "種{}".format(i), "strength": 10 - i} for i in range(5)]
        full_text = store.build_narrative(seeds)
        for _ in range(6):  # ego_weight -> floor (0.1)
            store.reduce_ego_weight()
        low_text = store.build_narrative(seeds)
        self.assertGreater(full_text.count("強度"), low_text.count("強度"))

    def test_record_introspection_appends(self):
        store = manas_store_at(self.path)
        store.record_introspection(1, "自己申告文です")
        self.assertEqual(len(store.state["introspections"]), 1)
        self.assertEqual(store.state["introspections"][0]["report"], "自己申告文です")

    def test_state_persists_across_instances(self):
        store1 = manas_store_at(self.path)
        store1.reduce_ego_weight()
        store1.commit()
        store2 = manas_store_at(self.path)
        self.assertAlmostEqual(store2.ego_weight, INITIAL_EGO_WEIGHT - INTROSPECTION_DECREASE)

    # --- T6 (manas 側): 依他起の注入文に「(推定)」ラベル、圓成實に逐語 span ---
    def test_t6_etaki_seed_is_labeled_with_suitei_tag(self):
        store = manas_store_at(self.path)
        seeds = [
            {
                "id": 1,
                "content": "アシスタント由来の推測",
                "strength": 3,
                "sansho": "依他起",
                "evidences": [{"life": 1, "turn": {}, "source": "assistant", "span": "推測です"}],
            }
        ]
        text = store.build_narrative(seeds)
        self.assertIn("(推定)", text)

    def test_t6_enjoujitsu_seed_shows_verbatim_span_not_only_paraphrase(self):
        store = manas_store_at(self.path)
        seeds = [
            {
                "id": 1,
                "content": "ユーザーは猫を飼っている",  # LLM の要約 (言い換え)
                "strength": 3,
                "sansho": "圓成實",
                "evidences": [
                    {"life": 1, "turn": {}, "source": "user", "span": "うちには猫がいます"}
                ],
            }
        ]
        text = store.build_narrative(seeds)
        self.assertIn("うちには猫がいます", text)  # 逐語 span がそのまま出る
        self.assertNotIn("(推定)", text)  # 圓成實には推定ラベルを付けない

    def test_t6_narrative_order_is_class_then_strength_not_strength_alone(self):
        store = manas_store_at(self.path)
        # strength だけなら依他起(10)が先に来るはずだが、クラスが優先するため圓成實(1)が先。
        seeds = [
            {
                "id": 1,
                "content": "推測種",
                "strength": 10,
                "sansho": "依他起",
                "evidences": [{"life": 1, "turn": {}, "source": "assistant", "span": "推測種"}],
            },
            {
                "id": 2,
                "content": "事実種",
                "strength": 1,
                "sansho": "圓成實",
                "evidences": [{"life": 1, "turn": {}, "source": "user", "span": "事実種です"}],
            },
        ]
        text = store.build_narrative(seeds)
        self.assertLess(text.index("事実種です"), text.index("推測種"))

    def test_t6_legacy_seed_without_sansho_key_still_renders_plain_line(self):
        # sansho 欄の無い旧形式データは従来どおり content のみの行になる (後方互換)。
        store = manas_store_at(self.path)
        seeds = [{"id": 1, "content": "猫好き", "strength": 3}]
        text = store.build_narrative(seeds)
        self.assertIn("- 性向: 猫好き(強度3)", text)  # kind 欄も無いので 性向 のラベル (改修5 §7.2)
        self.assertNotIn("(推定)", text)

    # --- G (spec 2026-10-30 §7): 描画は class でなく evidence (user の span の有無) で決める ---
    def _mixed_seed(self, evidences, sansho_class="依他起", kind="決定"):
        return {"id": 1, "kind": kind, "content": "X ではなく Y を採用", "strength": 2,
                "sansho": sansho_class, "evidences": evidences}

    def test_g_mixed_etaki_renders_latest_user_span_with_partial_mark_and_no_suitei(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([
            {"source": "user", "span": "古い user の発話"},
            {"source": "user", "span": "X ではなく Y にする"},
            {"source": "assistant", "span": "Y で進めます"},  # 最新だが assistant: 主には出ない
        ])
        text = store.build_narrative([seed])
        self.assertIn("- 決定: X ではなく Y にする(強度2・要約: X ではなく Y を採用・一部推定)", text)
        self.assertNotIn("(推定)", text)
        self.assertNotIn("Y で進めます", text)

    def test_whitespace_runs_in_joined_spans_and_content_collapse_per_span_109(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([
            self._ev("user", "前半\n\n\nです", ledger_id=2), self._ev("user", "後半\t\u3000だ\r\n", ledger_id=2),
        ], sansho_class="圓成實")
        seed["content"] = "要約\nの\n\n中"
        for line in (_render_seed_line(seed), _render_index_line(seed)):
            self.assertNotIn("\n", line)
            self.assertIn("前半 です \u2026 後半 だ", line)
            self.assertIn("要約: 要約 の 中", line)
        self.assertEqual(seed["evidences"][0]["span"], "前半\n\n\nです")  # 描画時だけ
        self.assertNotIn("\n\n\n", store.build_narrative([seed]))

    def test_identical_or_whitespace_only_different_spans_render_once_109(self):
        store = manas_store_at(self.path)
        for spans, want in ((["前半\n\nです", "前半\n\nです"], "前半 です"), (["a b", "a  b"], "a b")):
            seed = self._mixed_seed([self._ev("user", sp, ledger_id=2) for sp in spans], sansho_class="圓成實")
            for line in (_render_seed_line(seed), _render_index_line(seed)):
                self.assertNotIn("\u2026", line)
                self.assertEqual(line.count(want), 1)

    def test_g_enjoujitsu_line_has_no_partial_mark(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([{"source": "user", "span": "X ではなく Y にする"}], sansho_class="圓成實")
        text = store.build_narrative([seed])
        self.assertIn("- 決定: X ではなく Y にする(強度2・要約: X ではなく Y を採用)", text)
        self.assertNotIn("一部推定", text)

    def test_g_assistant_only_etaki_keeps_suitei_label(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([{"source": "assistant", "span": "Y で進めます"}])
        text = store.build_narrative([seed])
        self.assertIn("- 決定(推定): X ではなく Y を採用(強度2)", text)
        self.assertNotIn("一部推定", text)

    def _ev(self, source, span, life=2, ledger_id=2):
        e = {"source": source, "span": span}
        if life is not None:
            e["life"] = life
        if ledger_id is not None:
            e["ledger_id"] = ledger_id
        return e

    def test_g_split_spans_same_turn_are_joined_in_line_and_index(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([self._ev("user", "ビルドは make ではなく"),
                                 self._ev("user", "just を使うと決めました。")], sansho_class="圓成實")
        line = "- 決定: ビルドは make ではなく … just を使うと決めました。"
        self.assertIn(line + "(強度2・要約: X ではなく Y を採用・往復 2)", store.build_narrative([seed]))
        from raiya.manas import _render_index_line
        self.assertEqual(_render_index_line(seed),
                         "- 決定: ビルドは make ではなく … just を使うと決めました。 (強度2・要約: X ではなく Y を採用・往復 2)")

    def test_g_only_latest_turn_spans_are_used(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([self._ev("user", "古い往復", ledger_id=1),
                                 self._ev("user", "前半", ledger_id=2), self._ev("user", "後半", ledger_id=2),
                                 self._ev("user", "別 life", life=1, ledger_id=2)][:3])
        text = store.build_narrative([seed])
        self.assertIn("- 決定: 前半 … 後半(", text)
        self.assertNotIn("古い往復", text)
        seed2 = self._mixed_seed([self._ev("user", "life1", life=1, ledger_id=2), self._ev("user", "life2", life=2, ledger_id=2)])
        text2 = store.build_narrative([seed2])
        self.assertIn("- 決定: life2(", text2)
        self.assertNotIn("life1", text2)

    def test_g_old_evidence_without_ledger_id_uses_single_latest_span(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([self._ev("user", "古い", ledger_id=None), self._ev("user", "新しい", ledger_id=None)])
        text = store.build_narrative([seed])
        self.assertIn("- 決定: 新しい(", text)
        self.assertNotIn("古い", text)

    def test_g_duplicate_spans_appear_once_and_assistant_excluded(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([self._ev("user", "A"), self._ev("user", "A"), self._ev("assistant", "助手の発話"),
                                 self._ev("user", "B")])
        text = store.build_narrative([seed])
        self.assertIn("- 決定: A … B(", text)
        self.assertNotIn("助手の発話", text)

    def test_g_evidence_without_source_is_not_a_user_span(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([{"span": "出所不明の span"}, {"source": "user", "span": ""}])
        text = store.build_narrative([seed])
        self.assertIn("- 決定(推定): X ではなく Y を採用(強度2)", text)
        self.assertNotIn("出所不明の span", text)

    def test_g_index_label_follows_user_span_presence(self):
        store = manas_store_at(self.path)
        mixed = self._mixed_seed([{"source": "user", "span": "X ではなく Y にする"},
                                  {"source": "assistant", "span": "Y で進めます"}])
        only_assistant = dict(self._mixed_seed([{"source": "assistant", "span": "Y で進めます"}]), id=2)
        legacy = dict(self._mixed_seed([{"span": "Y で進めます"}]), id=3)
        lines = store.build_index_text([mixed, only_assistant, legacy]).splitlines()[1:]
        self.assertEqual(lines, [
            "- 決定: X ではなく Y にする (強度2・要約: X ではなく Y を採用・一部推定)",
            "- 決定(推定): X ではなく Y を採用 (強度2)",
            "- 決定(推定): X ではなく Y を採用 (強度2)",
        ])

    def test_g2_index_enjojitsu_user_only_has_span_and_summary_without_partial(self):
        store = manas_store_at(self.path)
        seed = self._mixed_seed([{"source": "user", "span": "X ではなく Y にする"}], sansho_class="圓成實")
        text = store.build_index_text([seed])
        self.assertIn("- 決定: X ではなく Y にする (強度2・要約: X ではなく Y を採用)", text)
        self.assertNotIn("一部推定", text)
        self.assertNotIn("(推定)", text)

    def test_g2_index_legacy_without_sansho_keeps_content_line(self):
        store = manas_store_at(self.path)
        seed = {"id": 1, "content": "猫好き", "strength": 3, "evidences": [{"source": "user", "span": "猫"}]}
        self.assertEqual(store.build_index_text([seed]).splitlines()[1:], ["- 性向: 猫好き (強度3)"])

    def test_g_branch_predicate_is_shared_by_both_renderers(self):
        from raiya.manas import _g_branch_applies, _render_index_line, _render_seed_line
        ev = [{"source": "user", "span": "u"}]
        for seed in (
            self._mixed_seed(ev),
            self._mixed_seed(ev, kind="再開点"),
            self._mixed_seed(ev, kind="所在"),
            self._mixed_seed(ev, kind="性向"),
            self._mixed_seed([{"source": "assistant", "span": "a"}]),
            {"id": 1, "content": "c", "strength": 1, "evidences": ev},
        ):
            applies = _g_branch_applies(seed)
            for line in (_render_seed_line(seed), _render_index_line(seed)):
                self.assertEqual(("要約:" in line) or ("逐語:" in line), applies)

    def test_recent_log_returns_independent_deep_copies(self):
        # MED-A と同型: recent_log の entry / seed_ids を書き換えても内部 state は動かない。
        # introspect_material() がこの戻り値をそのまま返すため、漏れは facade 経由でも露呈する。
        store = manas_store_at(self.path)
        store.inject([{"id": 1, "content": "猫好き", "strength": 3}], life=1)
        log1 = store.recent_log()
        log1[0]["injected_text"] = "呼び出し側による改竄"
        log1[0]["seed_ids"].append(999)

        log2 = store.recent_log()
        self.assertIn("猫好き", log2[0]["injected_text"])
        self.assertEqual(log2[0]["seed_ids"], [1])


class TestIndexInjection(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = manas_store_at(os.path.join(self.tmpdir, "manas.json"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, sid, content, sansho_class, strength):
        return {"id": sid, "content": content, "sansho": sansho_class, "strength": strength,
                "conditions": [], "evidences": [{"span": content}]}

    def test_index_text_lists_class_content_strength_with_total(self):
        index = [self._seed(1, "コマンドは表示する", "圓成實", 3), self._seed(2, "推定の性向", "依他起", 2)]
        text = self.store.build_index_text(index, index_total=5)
        # 改修5 §7.2: 行は「kind: content (強度n)」、依他起は kind(推定)。_seed は kind 欄を持たないので 性向。
        self.assertEqual(
            text.splitlines(),
            ["【記録の索引 (上位 2 件 / 全 5 件。載っていない 3 件も記録に在る)】", "- 性向: コマンドは表示する (強度3)", "- 性向(推定): 推定の性向 (強度2)"],
        )
        self.assertEqual(self.store.build_index_text(index).splitlines()[0], "【記録の索引 (全 2 件)】")
        self.assertEqual(self.store.build_index_text([]), "")

    def test_build_index_text_ignores_inconsistent_index_total(self):
        index = [self._seed(1, "コマンドは表示する", "圓成實", 3), self._seed(2, "推定の性向", "依他起", 2)]
        text = self.store.build_index_text(index, index_total=1)
        self.assertEqual(text.splitlines()[0], "【記録の索引 (全 2 件)】")

    def test_inject_combines_narrative_and_index_and_logs_ids(self):
        fired = [self._seed(1, "コマンドは表示する", "圓成實", 3)]
        index = [self._seed(1, "コマンドは表示する", "圓成實", 3), self._seed(2, "推定の性向", "依他起", 2)]
        text = self.store.inject(fired, 4, index=index, index_total=2)
        # 改修5 §7.2 (E6): 先頭行は枠の宣言、我執の強度は見出しに出ない。
        self.assertTrue(text.startswith(NARRATIVE_HEADER + "\n\n"))
        self.assertNotIn("我執", text)
        self.assertNotIn("こう理解している", text)
        self.assertIn(GERMINATED_HEADER, text)
        self.assertIn("【記録の索引 (全 2 件)】", text)
        self.assertLess(text.index(GERMINATED_HEADER), text.index("記録の索引"))
        entry = self.store.recent_log(1)[0]
        self.assertEqual(entry["seed_ids"], [1])
        self.assertEqual(entry["index_ids"], [1, 2])
        self.assertEqual(entry["injected_text"], text)

    def test_inject_with_index_only_returns_index_block(self):
        text = self.store.inject([], 4, index=[self._seed(2, "推定の性向", "依他起", 2)])
        self.assertTrue(text.startswith(NARRATIVE_HEADER + "\n\n【記録の索引"))
        self.assertNotIn(GERMINATED_HEADER, text)

    def test_inject_resume_multiline_content_collapses_109(self):
        text = self.store.inject([], 4, resume={"id": 9, "kind": "再開点", "content": "次\n\n= 待ち"})
        self.assertEqual(text, NARRATIVE_HEADER + "\n\n- 再開点: 次 = 待ち")

    def test_inject_resume_over_budget_multiline_is_cut_on_one_line_109(self):
        from raiya import manas
        content = "あ\n\n" * 3000
        collapsed = " ".join(content.split())
        text = self.store.inject([], 4, resume={"id": 9, "kind": "再開点", "content": content})
        self.assertLessEqual(len(text), manas.INJECTION_MAX_CHARS)
        line = text.split("\n\n")[-1]
        self.assertTrue(line.startswith("- 再開点: "))
        self.assertNotIn("\n", line)
        m = re.match(r"- 再開点: (.*)…\(他 (\d+) 字\)$", line, re.S)
        self.assertIsNotNone(m, line)
        self.assertEqual(int(m.group(2)), len(collapsed) - len(m.group(1)))
        self.assertTrue(collapsed.startswith(m.group(1)))

    def test_inject_appends_resume_point_line_once(self):
        # 改修5 §6.3/§7.2: 再開点はセッション先頭の recall でだけ渡され、1 行で出る。
        text = self.store.inject([], 4, resume={"id": 9, "content": "次 = §11 の裁定待ち", "kind": "再開点"})
        self.assertEqual(text, NARRATIVE_HEADER + "\n\n- 再開点: 次 = §11 の裁定待ち")
        self.assertEqual(self.store.inject([], 4, resume=None), "")

    def test_inject_with_nothing_returns_empty_and_keeps_legacy_signature(self):
        self.assertEqual(self.store.inject([], 4), "")
        self.assertEqual(self.store.recent_log(1)[0]["index_ids"], [])

    def test_inject_index_ids_survives_generator_input(self):
        seed1 = self._seed(1, "コマンドは表示する", "圓成實", 3)
        seed2 = self._seed(2, "推定の性向", "依他起", 2)
        text = self.store.inject([], 4, index=(s for s in [seed1, seed2]), index_total=2)
        self.assertIn("コマンドは表示する", text)
        self.assertIn("推定の性向", text)
        entry = self.store.recent_log(1)[0]
        self.assertEqual(entry["index_ids"], [1, 2])

    def test_log_is_capped_at_manas_log_max(self):
        from raiya.manas import MANAS_LOG_MAX

        for i in range(MANAS_LOG_MAX + 5):
            self.store.inject([], i)
        self.assertEqual(len(self.store.state["log"]), MANAS_LOG_MAX)
        self.assertEqual(self.store.state["log"][0]["life"], 5)
        self.assertEqual(self.store.state["log"][-1]["life"], MANAS_LOG_MAX + 4)


if __name__ == "__main__":
    unittest.main()


class TestPlaceOnlyRendering(unittest.TestCase):
    """spec 2026-10-30 place-and-nonfinding §7 H: user span の無い 所在(依他起) は 處 だけを描く。"""

    CONTENT = "/home/dev/foo/bar.py は見つからなかった"

    def _seed(self, content=None, kind="所在", sansho="依他起", conditions=None, evidences=None, strength=1):
        seed = {"id": 1, "content": self.CONTENT if content is None else content, "kind": kind, "strength": strength,
                "conditions": ["縁A"] if conditions is None else conditions,
                "evidences": [{"source": "assistant", "span": "見つからなかった"}] if evidences is None else evidences}
        if sansho is not None:
            seed["sansho"] = sansho
        return seed

    def test_path_only_in_germinated_and_index_lines(self):
        seed = self._seed()
        self.assertEqual(_render_seed_line(seed), "- 所在(推定): /home/dev/foo/bar.py(強度1)")
        self.assertEqual(_render_index_line(seed), "- 所在(推定): /home/dev/foo/bar.py (強度1)")
        self.assertNotIn("見つからなかった", _render_seed_line(seed) + _render_index_line(seed))

    def test_user_span_mixed_place_takes_P_form(self):
        seed = self._seed(evidences=[{"source": "user", "span": "/home/dev/foo/bar.py にある"}])
        line = _render_seed_line(seed)
        # 混在の 所在 (spec 2026-10-30 mixed-seed-heading §7 P): 見出し = 處、user の逐語は「逐語:」の札
        self.assertEqual(line, "- 所在: /home/dev/foo/bar.py(強度1・逐語: /home/dev/foo/bar.py にある)")
        self.assertEqual(_render_index_line(seed), "- 所在: /home/dev/foo/bar.py (強度1・逐語: /home/dev/foo/bar.py にある)")

    def test_enjosshitsu_place_unchanged(self):
        seed = self._seed(sansho="圓成實", evidences=[{"source": "user", "span": "x"}])
        seed["evidences"] = []
        self.assertEqual(_render_seed_line(seed), "- 所在: {}(強度1)".format(self.CONTENT))
        self.assertEqual(_render_index_line(seed), "- 所在: {} (強度1)".format(self.CONTENT))

    def test_content_when_no_path_or_url_even_with_conditions(self):
        seed = self._seed(content="どこかにあるはず", conditions=["縁A", "縁B"])
        self.assertEqual(_render_seed_line(seed), "- 所在(推定): どこかにあるはず(強度1)")
        self.assertEqual(_render_index_line(seed), "- 所在(推定): どこかにあるはず (強度1)")

    def test_url_and_path_in_order_of_appearance(self):
        seed = self._seed(content="https://example.com/doc を見たが ~/notes/a.md は見つからなかった")
        self.assertEqual(_render_seed_line(seed), "- 所在(推定): https://example.com/doc・~/notes/a.md(強度1)")
        seed = self._seed(content="docs は http://localhost:8080/x。")
        self.assertEqual(_render_index_line(seed), "- 所在(推定): http://localhost:8080/x (強度1)")

    def test_order_of_appearance_with_prefix_paths(self):
        seed = self._seed(content="/a/bc/x.py か https://u.example/z か /a/b")
        self.assertEqual(_render_seed_line(seed), "- 所在(推定): /a/bc/x.py・https://u.example/z・/a/b(強度1)")

    def test_duplicate_path_once(self):
        seed = self._seed(content="/a/b.py と /a/b.py と /c/d.txt")
        self.assertEqual(_render_index_line(seed), "- 所在(推定): /a/b.py・/c/d.txt (強度1)")

    def test_url_inside_fullwidth_parens(self):
        seed = self._seed(content="（https://ex.com/a?b=1）を見た")
        self.assertEqual(_render_seed_line(seed), "- 所在(推定): https://ex.com/a?b=1(強度1)")

    def test_resume_point_keeps_content(self):
        seed = self._seed(kind="再開点", content="/a/b.py の続き")
        self.assertEqual(_render_seed_line(seed), "- 再開点(推定): /a/b.py の続き(強度1)")
        self.assertEqual(_render_index_line(seed), "- 再開点(推定): /a/b.py の続き (強度1)")

    def test_other_kinds_keep_content(self):
        for kind in ("性向", "決定"):
            seed = self._seed(kind=kind)
            self.assertEqual(_render_seed_line(seed), "- {}(推定): {}(強度1)".format(kind, self.CONTENT))
            self.assertEqual(_render_index_line(seed), "- {}(推定): {} (強度1)".format(kind, self.CONTENT))

    def test_legacy_seed_without_sansho_keeps_content(self):
        seed = self._seed(sansho=None)
        self.assertEqual(_render_seed_line(seed), "- 所在: {}(強度1)".format(self.CONTENT))
        self.assertEqual(_render_index_line(seed), "- 所在: {} (強度1)".format(self.CONTENT))


class TestIndexLineResumePointExclusion(unittest.TestCase):
    """spec 2026-10-30 §7 G: 再開点 には G の枝を使わない (索引行も同じ)。"""

    def test_etaki_resume_point_with_user_span_keeps_suitei_label(self):
        from raiya.manas import _render_index_line
        seed = {"id": 1, "kind": "再開点", "sansho": "依他起", "content": "c", "strength": 1,
                "evidences": [{"source": "user", "span": "u"}, {"source": "assistant", "span": "a"}]}
        self.assertEqual(_render_index_line(seed), "- 再開点(推定): c (強度1)")
