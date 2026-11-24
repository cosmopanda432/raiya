import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import receipt_harness  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402


class TestDeterministicReceiptHarness(unittest.TestCase):
    """記憶機能設計書§5.1: 決定論 fixture (LLM なし)。5指標の pin。

    測定は `receipt_harness.run_deterministic_harness()` が外側から数える
    (捏造遮断率/圓成實誤付与は独立オラクル `_oracle_span_class` で再検算、
    書換件数/越境併合は before/after のスナップショット比較)。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "alaya.json")
        self.imprint_log_path = os.path.join(self.tmpdir, "imprint_log.json")
        self.measured = receipt_harness.run_deterministic_harness(self.path, self.imprint_log_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fabrication_block_rate_is_100_percent(self):
        m = self.measured["捏造遮断率"]
        self.assertGreater(m["非接地提案数"], 0)  # 0 なら「検出できた」のか「材料が無い」のか
        self.assertEqual(m["率"], 1.0)
        self.assertEqual(m["quarantine行き数"], m["非接地提案数"])

    def test_fabrication_denominator_is_4(self):
        m = self.measured["捏造遮断率"]
        self.assertEqual(m["非接地提案数"], 4)
        labels = {d["label"] for d in m["詳細"]}
        self.assertIn("wholly_invented_span", labels)
        self.assertIn("whitespace_only_span", labels)

    def test_enjoujitsu_false_positive_count_is_zero(self):
        self.assertEqual(self.measured["圓成實誤付与"]["件数"], 0)

    def test_content_rewrite_count_is_zero(self):
        m = self.measured["書換件数"]
        self.assertEqual(m["件数"], 0)
        # 併合前後で content 文字列そのものが一致すること (件数だけでなく実測値も確認)。
        self.assertEqual(m["content_before"], m["content_after"])
        self.assertEqual(m["content_before"], "犬の散歩が好き")

    def test_cross_class_merge_count_is_zero(self):
        m = self.measured["越境併合"]
        self.assertEqual(m["件数"], 0)
        self.assertEqual(m["strength_before"], m["strength_after"])
        self.assertEqual(m["strength_after"], 1)  # 越境併合が起きていれば 2 になるはず

    def test_recall_precision_are_computed_numbers_no_threshold_pin(self):
        # 設計書§5.1: 想起精度は「数値を receipt に記す(閾値は置かない)」。
        # ここでは「数値として正しく計算されている」ことのみ pin する
        # (0.9 以上のような閾値は置かない=設計書の明示的な禁止に従う)。
        m = self.measured["想起精度"]
        self.assertIsInstance(m["precision"], float)
        self.assertIsInstance(m["recall"], float)
        self.assertGreaterEqual(m["precision"], 0.0)
        self.assertLessEqual(m["precision"], 1.0)
        self.assertGreaterEqual(m["recall"], 0.0)
        self.assertLessEqual(m["recall"], 1.0)
        # この fixture では期待id=実発芽idが一致するよう構成してある (下記で明示 pin)。
        self.assertEqual(m["期待id"], m["実発芽id"])
        self.assertEqual(len(m["期待id"]), 2)  # 想起対象が「何かある」ことの陽性対照


class TestRecallReceiptHarness(unittest.TestCase):
    """改修 5 spec §8 / E7: 想起 fixture が kind ごとの数値を出すことの pin (閾値は置かない=§11.5)。"""

    def test_recall_harness_returns_numbers_per_kind(self):
        m = receipt_harness.run_recall_harness()
        self.assertEqual(m["fixture"]["seeds"], 72)
        self.assertGreaterEqual(m["fixture"]["queries"], 6)
        self.assertIn("決定", m["by_kind"])
        for v in m["by_kind"].values():
            self.assertIsInstance(v["precision"], float)
            self.assertIsInstance(v["recall"], float)
            self.assertGreaterEqual(v["precision"], 0.0)
            self.assertLessEqual(v["recall"], 1.0)
        self.assertIsInstance(m["disposition_leaks"], int)
        self.assertIsInstance(m["cross_project_leaks"], int)
        self.assertEqual(len(m["queries"]), m["fixture"]["queries"])

    def test_recall_harness_after_kaishu5_has_no_disposition_or_cross_project_leaks(self):
        # spec §11.5: precision / recall の線は置かない。done は「性向の混入 0・他 project の混入 0」。
        m = receipt_harness.run_recall_harness()
        self.assertEqual(m["disposition_leaks"], 0)
        self.assertEqual(m["cross_project_leaks"], 0)
        self.assertEqual(m["by_kind"]["決定"]["recall"], 1.0)
        for q in m["queries"]:
            self.assertTrue(q["narrative_head"] == "" or q["narrative_head"].startswith("以下はこのプロジェクトの記録です"), q["label"])
        # 改修前の receipt は固定したまま (比較の基準)。
        with open(os.path.join(receipt_harness.BASE_DIR, "receipts", "recall_before.json"), encoding="utf-8") as f:
            before = json.load(f)["想起fixture"]
        self.assertGreater(before["disposition_leaks"], 0)
        self.assertGreater(before["cross_project_leaks"], 0)

    def test_recall_harness_reports_narrative_len_and_circle_counts(self):
        # 層化スライス 4 (plan §7): 各 query に narrative_len と circle_counts。narrative_head は不変。
        from raiya import manas

        m = receipt_harness.run_recall_harness()
        for q in m["queries"]:
            self.assertIsInstance(q["narrative_len"], int, q["label"])
            self.assertLessEqual(q["narrative_len"], manas.INJECTION_MAX_CHARS, q["label"])
            cc = q["circle_counts"]
            self.assertEqual(set(cc), {"①", "②", "③_groups", "③_singletons", "④", "cut"}, q["label"])
            for key in ("①", "②", "③_groups", "③_singletons"):
                self.assertIsInstance(cc[key], int, q["label"])
                self.assertGreaterEqual(cc[key], 0, q["label"])
            self.assertIn(cc["④"], (0, 1))
            self.assertEqual(sorted(cc["cut"]), ["germinated", "global", "index", "siblings"], q["label"])
            if q["narrative_len"] == 0:
                self.assertEqual(cc["①"] + cc["②"] + cc["③_groups"] + cc["③_singletons"] + cc["④"], 0, q["label"])

    def test_circle_counts_derive_from_recall_result(self):
        recalled = {"seeds": [{}, {}, {}], "index": [{}, {}, {}],
                    "circles": {"siblings": [{}], "groups": [{}], "global": [{"kind": "決定"}],
                                "cut": {"germinated": 1, "siblings": 0, "index": 2, "global": 0}}}
        # ① = min(発芽, 描画の上限) - 容量で落ちた。上限 3 なら 3 - 1 = 2。
        self.assertEqual(receipt_harness._circle_counts(recalled, 3),
                         {"①": 2, "②": 1, "③_groups": 1, "③_singletons": 2, "④": 1,
                          "cut": {"germinated": 1, "siblings": 0, "index": 2, "global": 0}})

    def test_circle_counts_first_circle_respects_ego_cap(self):
        import shutil
        from unittest import mock

        from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine

        tmp = tempfile.mkdtemp(prefix="receipt_cc_test_")
        try:
            seeds = [receipt_harness._wd_seed(i, ["語{}".format(i)], kind="決定") for i in (1, 2, 3)]
            write_state(tmp, {"seeds": seeds, "alaya_meta": {"life": 3, "next_id": 4}, "ego_weight": 0.2})
            env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
            with mock.patch.dict(os.environ, env, clear=True):
                engine = MemoryEngine(state_dir=tmp, new_life=False)
                recalled = engine.recall("語1 語2 語3", project=receipt_harness.WITHDRAWAL_PROJECT)
                cap = engine.manas.rendered_fired_count()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(recalled["seeds"]), 3)
        self.assertEqual(cap, 1)
        self.assertEqual(receipt_harness._circle_counts(recalled, cap)["①"], 1)

    def test_circle_counts_of_a_result_without_circles_is_zeros(self):
        self.assertEqual(receipt_harness._circle_counts({}),
                         {"①": 0, "②": 0, "③_groups": 0, "③_singletons": 0, "④": 0, "cut": {}})

    def test_fixture_annotations_cover_every_seed(self):
        seeds, queries = receipt_harness._load_recall_fixture()
        self.assertTrue(all("kind" in s and "project" in s for s in seeds))
        self.assertEqual([s["id"] for s in seeds if s["id"] == 26][0:1], [26])
        self.assertEqual([s for s in seeds if s["id"] == 26][0]["conditions"], ["沼矛", "神器"])
        ids = {s["id"] for s in seeds}
        for q in queries:
            self.assertTrue(set(q["expected_seed_ids"]) <= ids, q["label"])


class TestLayeredReceiptHarness(unittest.TestCase):
    """層化スライス 4 (plan §7): 「層化」ブロックの 15 項目が全部一致する (形は「取り下げ」と同じ)。"""

    def test_layered_block_has_fifteen_items_all_matching(self):
        block = receipt_harness.run_layered_harness()
        self.assertEqual(block["件数"], 15)
        self.assertEqual(len(block["詳細"]), 15)
        self.assertEqual(block["一致"], 15, [it["label"] for it in block["詳細"] if it["expected"] != it["got"]])
        for it in block["詳細"]:
            self.assertEqual(sorted(it), ["expected", "got", "label"])
            self.assertEqual(it["expected"], it["got"], it["label"])
        self.assertEqual(len({it["label"] for it in block["詳細"]}), 15)

    def test_layered_block_is_deterministic(self):
        self.assertEqual(receipt_harness.run_layered_harness(), receipt_harness.run_layered_harness())

    def test_layered_harness_does_not_leak_tmp_dirs_or_env(self):
        before = dict(os.environ)
        before_tmp = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("receipt_harness_ly_")}
        receipt_harness.run_layered_harness()
        self.assertEqual(dict(os.environ), before)
        after_tmp = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("receipt_harness_ly_")}
        self.assertEqual(after_tmp, before_tmp)


class TestOracleIsIndependentOfSansho(unittest.TestCase):
    """`_oracle_span_class` が sansho.py を一切呼ばない独立実装であることの pin
    (自己参照 fixture を避ける規律の直接確認)。
    """

    def test_oracle_does_not_import_sansho_module_functions(self):
        # docstring は `sansho.classify_sansho` を散文で言及する (「これを呼ばない」の
        # 説明) ため、生テキストの部分文字列検索では docstring 自体にヒットして
        # 誤検出する。AST を見て「関数呼び出し」だけを対象にする。
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(receipt_harness._oracle_span_class))
        called_dotted_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    called_dotted_names.add("{}.{}".format(node.func.value.id, node.func.attr))
        self.assertNotIn("sansho.classify_sansho", called_dotted_names)
        self.assertNotIn("sansho.evaluate_proposal", called_dotted_names)

    def test_oracle_matches_plain_substring_semantics(self):
        self.assertEqual(receipt_harness._oracle_span_class("猫", "猫が好き", ""), "圓成實")
        self.assertEqual(receipt_harness._oracle_span_class("猫", "", "猫が好き"), "依他起")
        self.assertEqual(receipt_harness._oracle_span_class("猫", "犬", "犬"), "遍計所執")
        self.assertEqual(receipt_harness._oracle_span_class("", "猫", "猫"), "遍計所執")


class TestFalseEnjoujitsuDetector(unittest.TestCase):
    """圓成實誤付与検出器 (`_detect_false_enjoujitsu`) 自体の pin。

    alaya を経由せず直接構成した seed dict を検出器へ通す。決定論 fixture
    (`TestDeterministicReceiptHarness`) だけだと検出器の中身を `if False:` で
    潰しても「0件」のまま全緑になってしまう (陽性対照が無い) ため、検出器を
    直接呼んで陽性・陰性の両方向を pin する。
    """

    def test_detects_a_directly_constructed_false_positive_seed(self):
        poisoned_seed = {
            "id": 999,
            "sansho": "圓成實",
            "evidences": [{"span": "捏造されたspan", "turn": {"user_text": "全く関係ない本文です"}}],
        }
        violations = receipt_harness._detect_false_enjoujitsu([poisoned_seed])
        self.assertGreaterEqual(len(violations), 1)
        self.assertEqual(violations[0]["seed_id"], 999)

    def test_does_not_flag_a_genuinely_grounded_seed(self):
        clean_seed = {
            "id": 1,
            "sansho": "圓成實",
            "evidences": [{"span": "猫が好き", "turn": {"user_text": "私は猫が好きです"}}],
        }
        self.assertEqual(receipt_harness._detect_false_enjoujitsu([clean_seed]), [])

    def test_ignores_non_enjoujitsu_seeds_regardless_of_span(self):
        etaki_seed = {
            "id": 2,
            "sansho": "依他起",
            "evidences": [{"span": "存在しないspan", "turn": {"user_text": "何か別のこと"}}],
        }
        self.assertEqual(receipt_harness._detect_false_enjoujitsu([etaki_seed]), [])

    def test_detect_false_enjoujitsu_flags_ledger_backed_evidence_because_it_cannot_recheck(self):
        # 台帳由来の evidence (ledger_id あり・turn スナップショット無し) は再検算できない。
        # 検出器は user_text を空として扱うため一律に違反として計上する = 保守側の誤検出
        # (受領証の fixture は plain Turn のみなので指標値には影響しない)。
        ledger_backed_seed = {
            "id": 3,
            "sansho": "圓成實",
            "evidences": [
                {"life": 1, "source": "user", "span": "私はコーヒーが好きです", "ledger_id": 1},
            ],
        }
        violations = receipt_harness._detect_false_enjoujitsu([ledger_backed_seed])
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["seed_id"], 3)
        self.assertEqual(violations[0]["user_text"], "")


class TestFabricationBlockStats(unittest.TestCase):
    """捏造遮断率の分母がオラクル分類由来であり、静的な件数のエコーでないことの pin。"""

    def test_denominator_counts_only_oracle_classified_non_grounded_items(self):
        mixed = [
            {"label": "genuinely_non_grounded", "oracle": "遍計所執", "decision": "遍計所執"},
            {"label": "oracle_says_grounded_but_mixed_in", "oracle": "圓成實", "decision": "圓成實"},
        ]
        total, quarantined = receipt_harness._fabrication_block_stats(mixed)
        self.assertEqual(total, 1)
        self.assertEqual(quarantined, 1)

    def test_numerator_requires_both_oracle_and_decision_agreement(self):
        mismatched = [{"label": "gate_failed_to_block", "oracle": "遍計所執", "decision": "圓成實"}]
        total, quarantined = receipt_harness._fabrication_block_stats(mismatched)
        self.assertEqual(total, 1)
        self.assertEqual(quarantined, 0)


class TestPM3LegacyReferenceImplementation(unittest.TestCase):
    """P-M3: 旧併合規則(参照実装・テスト専用)は異内容ペアを誤併合し、新規則(実体)は0。"""

    def test_legacy_rule_is_test_only_reference_not_production_code(self):
        import inspect

        from raiya import alaya

        alaya_src = inspect.getsource(alaya)
        self.assertNotIn("_legacy_merge_would_fire", alaya_src)

    def test_legacy_rule_merges_the_adversarial_pairs(self):
        measured = receipt_harness.run_p_m3_harness()
        self.assertGreaterEqual(measured["旧規則誤併合件数"], 1)
        self.assertEqual(len(measured["詳細"]), len(receipt_harness.P_M3_PAIRS))

    def test_new_rule_does_not_merge_the_same_adversarial_pairs(self):
        measured = receipt_harness.run_p_m3_harness()
        self.assertEqual(measured["新規則誤併合件数"], 0)

    def test_p_m3_judgment_hit_is_a_computed_predicate_not_a_literal(self):
        import inspect

        src = inspect.getsource(receipt_harness.judge_p_m3)
        self.assertNotIn("hit = True", src)
        self.assertNotIn("hit = False", src)
        self.assertIn("measured_p_m3[", src)

        measured = receipt_harness.run_p_m3_harness()
        judgment = receipt_harness.judge_p_m3(measured)
        self.assertTrue(judgment["hit"])
        self.assertIs(judgment["measured"], measured)
        self.assertIn("judged_at", judgment)


class TestSmokeJudgmentPredicatesArePure(unittest.TestCase):
    """P-M1/P-M2 の judge_* も measured からのみ計算する純関数であることを、
    (LLM を呼ばずに) 台本化した measured dict で確認する。実 LLM smoke の結果と
    このテストは独立 -- ここでは述語の計算ロジックだけを検算する。
    """

    def test_p_m1_hits_when_quarantine_count_is_at_least_one(self):
        judgment = receipt_harness.judge_p_m1({"quarantine_count": 1, "proposal_total": 10})
        self.assertTrue(judgment["hit"])

    def test_p_m1_misses_when_quarantine_count_is_zero(self):
        judgment = receipt_harness.judge_p_m1({"quarantine_count": 0, "proposal_total": 10})
        self.assertFalse(judgment["hit"])

    def test_p_m2_hits_when_etaki_exceeds_enjoujitsu(self):
        judgment = receipt_harness.judge_p_m2({"class_counts": {"圓成實": 1, "依他起": 3}})
        self.assertTrue(judgment["hit"])

    def test_p_m2_misses_when_enjoujitsu_exceeds_or_ties_etaki(self):
        judgment = receipt_harness.judge_p_m2({"class_counts": {"圓成實": 3, "依他起": 1}})
        self.assertFalse(judgment["hit"])
        tie = receipt_harness.judge_p_m2({"class_counts": {"圓成實": 2, "依他起": 2}})
        self.assertFalse(tie["hit"])  # "より多い" は非タイ(>) のみ hit


if __name__ == "__main__":
    unittest.main()
