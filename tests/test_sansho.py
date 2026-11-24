import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import sansho  # noqa: E402
from raiya.sansho import Turn, _n  # noqa: E402


class TestNormalization(unittest.TestCase):
    """T8: `_n` は空白差のみ同一視する (CJK 段落跨ぎは一致、内容差は1文字でも不一致のまま)。"""

    def test_t8_cjk_paragraph_wrap_is_folded_to_equal(self):
        # 日本語は語間空白を持たないため、改行+全角空白を挟んだ段落跨ぎの逐語連結と
        # 空白の無い連結は同一視されなければならない (CJK空白畳み設計.md の規則)。
        wrapped = "お答え申し上げます。\n　今般、子育て政策について"
        flat = "お答え申し上げます。今般、子育て政策について"
        self.assertEqual(_n(wrapped), _n(flat))

    def test_t8_english_word_boundary_is_not_collapsed(self):
        # 全空白除去ではなく CJK 隣接空白のみを畳むため、英単語間の空白は保持され、
        # "the rapist" と "therapist" は畳んでも別語のまま (偽の果を作らない)。
        self.assertNotEqual(_n("the rapist"), _n("therapist"))

    def test_t8_english_line_wrap_across_ascii_words_is_still_folded(self):
        # 既存 (KJV型) の挙動は保存する: 両側が ASCII の折り返しは従来どおり1個の空白に畳まれ、
        # 改行を挟んだ引用と地の文が一致する。
        self.assertEqual(_n("instead\nof Abel"), _n("instead of Abel"))

    def test_t8_one_character_content_difference_stays_distinct(self):
        # 空白差だけを同一視し、1文字でも内容が違えば不一致のまま (嘘を通す方向に働かない)。
        self.assertNotEqual(_n("猫が好き"), _n("猫が嫌い"))

    def test_t8_none_and_empty_input_normalize_to_empty_string(self):
        self.assertEqual(_n(""), "")
        self.assertEqual(_n(None), "")

    def test_t8_nfkc_folds_fullwidth_latin(self):
        # NFKC 段そのものの pin (全角ラテン文字 -> 半角)。LLM 出力は全角/半角の
        # 表記揺れを起こしうるため、この段が抜けると同一内容の全角/半角 span が不一致になる。
        self.assertEqual(_n("ＡＩが好き"), _n("AIが好き"))


class TestCjkRangeLiteral(unittest.TestCase):
    """`_CJK_RANGE` の値そのものを固定するリテラル pin。

    期待値はここでも \\u エスケープでのみ書く (手打ちの CJK グリフは互換漢字/通常
    CJK統合漢字のどちらの符号位置になったか目視で判別できず、無音で入れ替わりうる)。
    範囲の意味は sansho.py 冒頭のコメント参照。
    """

    def test_cjk_range_is_exactly_the_three_u_escaped_blocks(self):
        expected = "\u3001-\u30ff\u3400-\u9fff\uf900-\ufaff"  # \u エスケープのみで書く
        self.assertEqual(sansho._CJK_RANGE, expected)

    def test_cjk_range_codepoints_are_the_expected_block_boundaries(self):
        # 文字単位でも符号位置を明示 pin する (範囲文字列全体の一致だけでなく、
        # 各ブロックの開始/終了符号位置を個別に確認する)。
        expected_codepoints = [
            0x3001, 0x2D, 0x30FF,  # かな+CJK記号/句読点: 開始-ハイフン-終了
            0x3400, 0x2D, 0x9FFF,  # CJK統合漢字
            0xF900, 0x2D, 0xFAFF,  # CJK互換漢字
        ]
        self.assertEqual([ord(c) for c in sansho._CJK_RANGE], expected_codepoints)


class TestClassifySansho(unittest.TestCase):
    """T2: assistant のみ→依他起、user→圓成實、1文字改変→遍計所執。"""

    def test_t2_span_grounded_in_user_text_is_enjoujitsu(self):
        self.assertEqual(
            sansho.classify_sansho("猫が好き", "私は猫が好きです", ""), "圓成實"
        )

    def test_t2_span_grounded_only_in_assistant_text_is_etaki(self):
        self.assertEqual(
            sansho.classify_sansho("猫が好き", "今日はいい天気ですね", "あなたは猫が好きですよね"),
            "依他起",
        )

    def test_t2_span_grounded_in_both_prefers_enjoujitsu(self):
        # ユーザー自身がその往復で逐語言った、という圓成實の意味論は user 側の一致を優先する。
        self.assertEqual(
            sansho.classify_sansho("猫が好き", "私は猫が好きです", "あなたは猫が好きですよね"),
            "圓成實",
        )

    def test_t2_one_character_altered_span_is_hengesho(self):
        # 曖昧一致は使わない: 1文字でも改変されていれば非接地=遍計所執。
        self.assertEqual(
            sansho.classify_sansho("猫が好きでした", "私は猫が好きです", ""), "遍計所執"
        )

    def test_t2_span_absent_from_both_is_hengesho(self):
        self.assertEqual(
            sansho.classify_sansho("宇宙人と話した", "今日は晴れです", "良い天気ですね"),
            "遍計所執",
        )

    def test_t2_empty_span_is_hengesho(self):
        self.assertEqual(sansho.classify_sansho("", "何か言った", ""), "遍計所執")

    def test_t2_whitespace_only_span_is_hengesho(self):
        # 空白のみの span は空文字同然に扱う (何にでも部分一致してしまう縮退を避ける)。
        # fixture は ASCII (CJK でない) 側で組む -- user_text が CJK だと `_n` の
        # CJK隣接空白除去で往復本文の空白が全消滅し、「番人が無くても " " が部分文字列に
        # ならない」別経路で緑になり番人自体は無 pin になる。ASCII の "hello world" は
        # 空白が畳まれず残るため、番人の有無を実際に分ける fixture になる。
        self.assertEqual(sansho.classify_sansho("   ", "hello world", ""), "遍計所執")


class TestGroundConditions(unittest.TestCase):
    """T4 (純粋ロジック側): 未接地の縁は落ちる。全落ちは呼び出し側 (evaluate_proposal) で却下。"""

    def test_t4_keeps_only_grounded_conditions(self):
        grounded = sansho.ground_conditions(["猫", "宇宙人"], "猫が好きです", "")
        self.assertEqual(grounded, ["猫"])

    def test_t4_all_ungrounded_returns_empty_list(self):
        grounded = sansho.ground_conditions(["宇宙人", "UFO"], "猫が好きです", "犬も好きです")
        self.assertEqual(grounded, [])

    def test_t4_condition_grounded_only_in_assistant_text_is_kept(self):
        grounded = sansho.ground_conditions(["散歩"], "今日は天気がいい", "散歩に行きましょう")
        self.assertEqual(grounded, ["散歩"])

    def test_t4_empty_or_falsy_conditions_are_dropped(self):
        grounded = sansho.ground_conditions(["", None, "猫"], "猫が好き", "")
        self.assertEqual(grounded, ["猫"])

    def test_t4_grounded_condition_keeps_original_notation(self):
        # 接地判定には `_n` (NFKC) を使うが、格納する値は元の表記のまま
        # (nc ではなく c を append する)。全角 "ＡＩ" で接地しても、返り値が NFKC 後の
        # "AI" に化けていないことを確認する。
        grounded = sansho.ground_conditions(["ＡＩ"], "ＡＩが好きです", "")
        self.assertEqual(grounded, ["ＡＩ"])

    def test_t4_whitespace_only_condition_is_dropped_and_does_not_ground(self):
        # `_n` 後に空白だけになる縁は空文字列として任意の本文に部分一致してしまうため、落とす。
        for cond in ("  \u3000 ", "\n\t"):
            with self.subTest(cond=cond):
                grounded = sansho.ground_conditions([cond], "I like cats a lot", "let us walk")
                self.assertEqual(grounded, [])


class TestEvaluateProposal(unittest.TestCase):
    """T4 (evaluate_proposal 側): 圓成實/依他起なのに縁が全滅すれば reject。
    T7 (純粋ロジック側): Turn でない呼び出しは TypeError。"""

    def test_t4_grounded_span_with_all_conditions_ungrounded_is_rejected(self):
        turn = Turn(user_text="猫が好きです", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["宇宙人"],
            "polarity": "無記",
            "evidence": {"source": "user", "span": "猫が好きです"},
        }
        result = sansho.evaluate_proposal(proposal, turn)
        self.assertEqual(result["sansho"], "圓成實")
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(result["reason"])

    def test_t4_whitespace_only_sole_condition_is_rejected_not_admitted(self):
        turn = Turn(user_text="I like cats", assistant_text="")
        proposal = {
            "content": "likes cats",
            "conditions": ["  \u3000 "],
            "polarity": "無記",
            "evidence": {"source": "user", "span": "I like cats"},
        }
        result = sansho.evaluate_proposal(proposal, turn)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["grounded_conditions"], [])
        self.assertTrue(result["reason"])

    def test_t4_hengesho_span_is_never_rejected_it_is_quarantined(self):
        # 遍計所執はそもそも quarantine 行きであり、縁の有無を問わず decision は "reject" にならない。
        turn = Turn(user_text="今日は晴れです", assistant_text="")
        proposal = {
            "content": "捏造",
            "conditions": [],
            "polarity": "無記",
            "evidence": {"source": "user", "span": "捏造された文言です"},
        }
        result = sansho.evaluate_proposal(proposal, turn)
        self.assertEqual(result["decision"], "遍計所執")

    def test_t7_evaluate_proposal_without_turn_raises_type_error(self):
        proposal = {
            "content": "x",
            "conditions": ["x"],
            "polarity": "無記",
            "evidence": {"source": "user", "span": "x"},
        }
        # 末那識の注入文はただの str (manas.ManasStore.inject の戻り値の型)。
        with self.assertRaises(TypeError):
            sansho.evaluate_proposal(proposal, "私はこれまでの対話からこう理解している")

    def test_t7_evaluate_proposal_with_introspection_report_string_raises_type_error(self):
        proposal = {
            "content": "x",
            "conditions": ["x"],
            "polarity": "無記",
            "evidence": {"source": "user", "span": "x"},
        }
        # 内観の自己申告文もただの str (backend.call_llm の戻り値の型)。
        with self.assertRaises(TypeError):
            sansho.evaluate_proposal(proposal, "最近の私はこう思い込んでいたかもしれません")

    def test_t7_evaluate_proposal_with_none_raises_type_error(self):
        proposal = {
            "content": "x",
            "conditions": ["x"],
            "polarity": "無記",
            "evidence": {"source": "user", "span": "x"},
        }
        with self.assertRaises(TypeError):
            sansho.evaluate_proposal(proposal, None)

    def test_llm_supplied_sansho_key_in_proposal_is_ignored(self):
        # EXE-15 異熟果=無記: 提案は自分の身分を自称できない。proposal に "sansho" を
        # 混入させても evaluate_proposal は読まない (span の実接地だけで判定する)。
        turn = Turn(user_text="猫が好きです 猫", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "無記",
            "sansho": "圓成實",  # LLM が自称してきたとしても...
            "evidence": {"source": "user", "span": "捏造された猫の話"},  # ...span は非接地
        }
        result = sansho.evaluate_proposal(proposal, turn)
        self.assertEqual(result["decision"], "遍計所執")  # 自称は無視され実接地で判定される


class TestSanshoSortKey(unittest.TestCase):
    """発芽順=クラス→強度降順のソートキー (§3.4)。alaya/manas 双方が共有する。"""

    def test_enjoujitsu_outranks_etaki_regardless_of_strength(self):
        low_enjoujitsu = {"sansho": "圓成實", "strength": 1}
        high_etaki = {"sansho": "依他起", "strength": 99}
        ordered = sorted([high_etaki, low_enjoujitsu], key=sansho.sansho_sort_key)
        self.assertEqual([s["sansho"] for s in ordered], ["圓成實", "依他起"])

    def test_same_class_breaks_tie_by_strength_desc(self):
        weak = {"sansho": "圓成實", "strength": 1}
        strong = {"sansho": "圓成實", "strength": 3}
        ordered = sorted([weak, strong], key=sansho.sansho_sort_key)
        self.assertEqual([s["strength"] for s in ordered], [3, 1])

    def test_unclassified_seed_sorts_after_both_classes(self):
        legacy = {"strength": 100}  # sansho キーの無い旧形式データ
        enjoujitsu = {"sansho": "圓成實", "strength": 1}
        etaki = {"sansho": "依他起", "strength": 1}
        ordered = sorted([legacy, etaki, enjoujitsu], key=sansho.sansho_sort_key)
        self.assertEqual(ordered[-1], legacy)


class TestWindowGate(unittest.TestCase):
    """記憶機能設計書 改修4 §4: 窓 (複数往復) と複数 span の三性判定。"""

    def _turns(self):
        return [
            sansho.Turn(user_text="猫が好きです", assistant_text="いいですね"),
            sansho.Turn(user_text="犬も飼っています", assistant_text="犬は散歩が必要ですね"),
        ]

    def test_as_window_wraps_single_turn_and_rejects_non_turns(self):
        t = sansho.Turn(user_text="a", assistant_text="b")
        self.assertEqual(sansho.as_window(t), (t,))
        self.assertEqual(sansho.as_window([t, t]), (t, t))
        self.assertEqual(sansho.as_window((t, t)), (t, t))
        with self.assertRaises(TypeError):
            sansho.as_window("文字列")
        with self.assertRaises(TypeError):
            sansho.as_window([])
        with self.assertRaises(TypeError):
            sansho.as_window([t, "x"])

    def test_evidence_list_accepts_dict_and_list(self):
        self.assertEqual(
            sansho.evidence_list({"evidence": {"source": "user", "span": "x"}}),
            [{"source": "user", "span": "x"}],
        )
        self.assertEqual(
            sansho.evidence_list({"evidence": [{"source": "user", "span": "x"}, "junk", {"span": "y"}]}),
            [{"source": "user", "span": "x"}, {"source": "", "span": ""}, {"source": "", "span": "y"}],
        )
        self.assertEqual(sansho.evidence_list({}), [])
        self.assertEqual(sansho.evidence_list({"evidence": "x"}), [])

    def test_span_grounded_in_later_turn_is_found_with_index(self):
        self.assertEqual(sansho.classify_span_in_window("犬も飼っています", self._turns()), ("圓成實", 1))
        self.assertEqual(sansho.classify_span_in_window("散歩が必要", self._turns()), ("依他起", 1))
        self.assertEqual(sansho.classify_span_in_window("鳥", self._turns()), ("遍計所執", None))

    def test_conditions_are_grounded_per_turn_not_across_turn_boundary(self):
        window = [
            sansho.Turn(user_text="今日は猫", assistant_text=""),
            sansho.Turn(user_text="犬の話", assistant_text=""),
        ]
        # 「猫犬」は往復1末尾+往復2先頭の連結でしか現れない -> 接地させない
        self.assertEqual(sansho.ground_conditions_in_window(["猫", "猫犬", "犬"], window), ["猫", "犬"])

    def test_multi_span_mixed_user_and_assistant_is_etaki(self):
        # 2026-10-22 裁定: 全 span がユーザー発話に接地して初めて圓成實 (圓滿成就 T1585_008:347)。
        # user 接地 1 件 + assistant のみ接地 1 件の混在は依他起。順序を入れ替えても同じ。
        user_span = {"source": "user", "span": "猫が好きです"}
        assistant_span = {"source": "assistant", "span": "散歩が必要"}
        for evidence, expected, turn_indexes in (
            ([assistant_span, user_span], ["依他起", "圓成實"], [1, 0]),
            ([user_span, assistant_span], ["圓成實", "依他起"], [0, 1]),
        ):
            with self.subTest(order=expected):
                proposal = {"content": "猫も犬も好き", "conditions": ["猫", "犬"], "polarity": "善", "evidence": evidence}
                r = sansho.evaluate_proposal(proposal, self._turns())
                self.assertEqual(r["decision"], "依他起")
                self.assertEqual(r["sansho"], "依他起")
                self.assertEqual(r["grounded_conditions"], ["猫", "犬"])
                self.assertEqual([s["sansho"] for s in r["spans"]], expected)
                self.assertEqual([s["turn_index"] for s in r["spans"]], turn_indexes)

    def test_multi_span_all_user_is_enjoujitsu(self):
        proposal = {
            "content": "猫と犬",
            "conditions": ["猫", "犬"],
            "evidence": [{"span": "猫が好きです"}, {"span": "犬も飼っています"}],
        }
        r = sansho.evaluate_proposal(proposal, self._turns())
        self.assertEqual(r["decision"], "圓成實")
        self.assertEqual([s["sansho"] for s in r["spans"]], ["圓成實", "圓成實"])

    def test_multi_span_only_assistant_is_etaki(self):
        proposal = {
            "content": "犬は散歩",
            "conditions": ["犬"],
            "evidence": [{"source": "assistant", "span": "散歩が必要"}, {"source": "assistant", "span": "いいですね"}],
        }
        self.assertEqual(sansho.evaluate_proposal(proposal, self._turns())["decision"], "依他起")

    def test_one_ungrounded_span_makes_whole_proposal_hengeshoshu(self):
        proposal = {
            "content": "猫と鳥",
            "conditions": ["猫"],
            "evidence": [{"source": "user", "span": "猫が好きです"}, {"source": "user", "span": "鳥が好きです"}],
        }
        r = sansho.evaluate_proposal(proposal, self._turns())
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(r["reason"], "span 1/2 件が往復本文(user/assistant)のいずれにも逐語一致しません(遍計所執=非接地)")

    def test_single_turn_single_span_reason_texts_are_unchanged(self):
        turn = sansho.Turn(user_text="猫が好き", assistant_text="")
        empty = sansho.evaluate_proposal({"content": "x", "conditions": ["猫"], "evidence": {"span": ""}}, turn)
        self.assertEqual(empty["reason"], "span が空です (遍計所執=非接地)")
        miss = sansho.evaluate_proposal({"content": "x", "conditions": ["猫"], "evidence": {"span": "犬"}}, turn)
        self.assertEqual(
            miss["reason"], "span が往復本文(user/assistant)のいずれにも逐語一致しません(遍計所執=非接地)"
        )
        ok = sansho.evaluate_proposal({"content": "x", "conditions": ["猫"], "evidence": {"span": "猫が好き"}}, turn)
        self.assertEqual(ok["decision"], "圓成實")
        self.assertEqual(ok["spans"], [{"source": "", "span": "猫が好き", "sansho": "圓成實", "turn_index": 0}])

    def test_no_grounded_condition_in_any_turn_is_reject(self):
        proposal = {"content": "x", "conditions": ["鳥"], "evidence": {"span": "猫が好きです"}}
        self.assertEqual(sansho.evaluate_proposal(proposal, self._turns())["decision"], "reject")

    def test_self_reported_source_does_not_affect_verdict_in_window(self):
        # LLM の自称 source は判定に使わない (IF-1 見分或時非量)。assistant 発話を
        # "user" と自称してきても判定は依他起のまま。spans[i]["source"] は自称の
        # 素通しであって判定ではない (判定は同じ dict の "sansho" 側)。
        proposal = {
            "content": "x",
            "conditions": ["犬"],
            "evidence": [{"source": "user", "span": "犬は散歩が必要ですね"}],
        }
        r = sansho.evaluate_proposal(proposal, self._turns())
        self.assertEqual(r["decision"], "依他起")
        self.assertEqual(r["spans"][0]["sansho"], "依他起")
        self.assertEqual(r["spans"][0]["turn_index"], 1)
        self.assertEqual(r["spans"][0]["source"], "user")

    def test_non_dict_evidence_item_makes_proposal_hengeshoshu(self):
        # 形の壊れた evidence を捨てると接地済みの span だけが残り、判定が採用の側へ
        # 動いてしまう。空 span へ落として提案全体を遍計所執にする。
        proposal = {
            "content": "x",
            "conditions": ["猫"],
            "evidence": [{"source": "user", "span": "猫が好きです"}, "鳥が好きです"],
        }
        r = sansho.evaluate_proposal(proposal, self._turns())
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(
            r["spans"][1],
            {"source": "", "span": "", "sansho": "遍計所執", "turn_index": None},
        )

    def test_zero_spans_in_window_is_hengeshoshu(self):
        # evidence が空列でも、キーごと無くても、裏付けは 0 件 = 非接地。
        for evidence in ([], None):
            with self.subTest(evidence=evidence):
                proposal = {"content": "x", "conditions": ["猫"]}
                if evidence is not None:
                    proposal["evidence"] = evidence
                r = sansho.evaluate_proposal(proposal, self._turns())
                self.assertEqual(r["decision"], "遍計所執")
                self.assertEqual(r["reason"], "span が空です (遍計所執=非接地)")
                self.assertEqual(r["spans"], [])

    def test_all_spans_ungrounded_reports_full_count(self):
        proposal = {
            "content": "x",
            "conditions": ["猫"],
            "evidence": [{"span": "鳥が好きです"}, {"span": "宇宙人と話した"}],
        }
        r = sansho.evaluate_proposal(proposal, self._turns())
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(r["reason"], "span 2/2 件が往復本文(user/assistant)のいずれにも逐語一致しません(遍計所執=非接地)")

    def test_blank_span_among_grounded_spans_is_hengeshoshu(self):
        # 非接地の span が全て空白なら「空です」と言う (逐語一致しなかったのではない)。
        proposal = {
            "content": "x",
            "conditions": ["猫"],
            "evidence": [{"span": "猫が好きです"}, {"span": "   "}],
        }
        r = sansho.evaluate_proposal(proposal, self._turns())
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(r["spans"][1]["sansho"], "遍計所執")
        self.assertIsNone(r["spans"][1]["turn_index"])
        self.assertEqual(r["reason"], "span が空です (遍計所執=非接地)")


if __name__ == "__main__":
    unittest.main()
