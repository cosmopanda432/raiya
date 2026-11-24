import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import ledger, sansho  # noqa: E402
from raiya.ledger import LedgerStore, LedgerTurn  # noqa: E402
from tests._state_fixture import (  # noqa: E402
    add_ledger_body, ledger_store_at, meta_value, mutate, ro, set_meta, table_bodies,
)


class TestLedgerStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl = os.path.join(self.tmpdir, "ledger.jsonl")
        self.meta = os.path.join(self.tmpdir, "ledger_meta.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _store(self):
        return ledger_store_at(self.jsonl, self.meta)

    def test_fresh_store_is_empty(self):
        store = self._store()
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.pending_count(), 0)
        self.assertEqual(store.consolidated_upto(), 0)
        self.assertEqual(store.unconsolidated(), [])
        self.assertIsNone(store.warning)

    def test_append_assigns_sequential_ids_and_writes_one_row_per_turn(self):
        store = self._store()
        first = store.append("こんにちは\n二行目", "やあ", life=3, session_id="s1", ts=1.5)
        second = store.append("次", "応答", life=3)
        self.assertEqual((first, second), (1, 2))
        store.commit()  # 動詞途中の暗黙 save は無い: 追記は commit で書かれる
        bodies = table_bodies(self.tmpdir, "ledger")
        self.assertEqual(len(bodies), 2)  # 本文に改行があっても 1 往復 = 1 行
        row = json.loads(bodies[0])
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["life"], 3)
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["ts"], 1.5)
        self.assertEqual(row["user_text"], "こんにちは\n二行目")
        self.assertEqual(row["assistant_text"], "やあ")
        self.assertIsNone(json.loads(bodies[1])["session_id"])
        # body は append が今まで書いてきた行テキストそのもの (既定の区切り・ensure_ascii=False・末尾の改行なし)
        self.assertEqual(bodies[0], json.dumps(row, ensure_ascii=False))
        self.assertEqual(meta_value(self.tmpdir, "ledger.next_id"), 3)

    def test_unconsolidated_returns_ledger_turns_in_order_and_mark_moves_cursor(self):
        store = self._store()
        for i in range(4):
            store.append("u{}".format(i), "a{}".format(i), life=1)
        window = store.unconsolidated()
        self.assertEqual([t.id for t in window], [1, 2, 3, 4])
        self.assertTrue(all(isinstance(t, LedgerTurn) for t in window))
        self.assertTrue(all(isinstance(t, sansho.Turn) for t in window))  # KUN-7 の型検査を通る
        self.assertEqual(window[0].user_text, "u0")
        self.assertEqual(window[0].assistant_text, "a0")
        self.assertEqual(window[0].life, 1)
        self.assertEqual([t.id for t in store.unconsolidated(limit=2)], [1, 2])
        store.mark_consolidated(3)
        self.assertEqual(store.consolidated_upto(), 3)
        self.assertEqual([t.id for t in store.unconsolidated()], [4])
        self.assertEqual(store.pending_count(), 1)
        store.mark_consolidated(2)  # 単調増加のみ: 巻き戻らない
        self.assertEqual(store.consolidated_upto(), 3)

    def test_state_survives_reload_across_instances(self):
        store = self._store()
        store.append("u", "a", life=2)
        store.append("u2", "a2", life=2)
        store.mark_consolidated(1)
        store.commit()
        other = self._store()
        self.assertEqual(other.count(), 2)
        self.assertEqual(other.consolidated_upto(), 1)
        self.assertEqual(other.append("u3", "a3", life=2), 3)
        other.commit()
        store.reload()
        self.assertEqual(store.count(), 3)

    def test_tail_returns_last_n_rows_as_dicts(self):
        store = self._store()
        for i in range(3):
            store.append("u{}".format(i), "a{}".format(i), life=1)
        tail = store.tail(2)
        self.assertEqual([r["id"] for r in tail], [2, 3])
        self.assertEqual(tail[-1]["user_text"], "u2")

    def test_corrupt_body_is_skipped_with_warning_and_ids_continue(self):
        store = self._store()
        store.append("u1", "a1", life=1)
        store.commit()
        add_ledger_body(self.tmpdir, "{not json")
        store.append("u2", "a2", life=1)
        store.commit()
        fresh = self._store()
        self.assertEqual([t.id for t in fresh.unconsolidated()], [1, 2])
        self.assertIn("1 件", fresh.warning or "")
        # 読めない本文は読み飛ばすだけで、DB の行は書き直さない
        self.assertIn("{not json", table_bodies(self.tmpdir, "ledger"))

    def test_corrupt_line_warning_is_settled_on_first_row_scan(self):
        # 行走査は遅延する (区間の入口は meta しか読まない) ので、読めない本文の
        # warning が確定するのは最初に行を読む動詞を呼んだ時点。
        store = self._store()
        store.append("u1", "a1", life=1)
        store.commit()
        add_ledger_body(self.tmpdir, "{not json")
        fresh = self._store()  # 構築 = meta しか読まない
        self.assertIsNone(fresh.warning)
        fresh.unconsolidated()
        self.assertIsNotNone(fresh.warning)

    def test_reload_keeps_the_corrupt_line_warning(self):
        # 行走査で分かった warning は、同じストアの観測では消えない。reload (区間の入口の読み直し) の後は
        # 最初の走査でまた確定する。
        store = self._store()
        store.append("u1", "a1", life=1)
        store.commit()
        add_ledger_body(self.tmpdir, "{not json")
        fresh = self._store()
        fresh.unconsolidated()
        self.assertIsNotNone(fresh.warning)
        fresh.reload()
        fresh.unconsolidated()
        self.assertIsNotNone(fresh.warning)

    def test_construction_reads_meta_only_when_meta_is_valid(self):
        # 台帳は削除されず伸び続ける (実運用で 240MB)。構築のたびに全行を走査すると、
        # 台帳を読まない動詞 (engine.recall 等) までその長さのコストを背負う。
        store = self._store()
        store.append("u1", "a1", life=1)
        store.commit()
        calls = []
        original = LedgerStore._read_rows

        def _counting(self_):
            calls.append(1)
            return original(self_)

        with mock.patch.object(LedgerStore, "_read_rows", _counting):
            fresh = self._store()
            self.assertEqual(len(calls), 0)   # 構築では meta しか読まない
            self.assertEqual(fresh.count(), 1)
            self.assertEqual(len(calls), 1)   # 行を読む動詞のときだけ走査する
            fresh.reload()
            self.assertEqual(len(calls), 1)   # reload も meta のみ

    def test_row_with_non_string_text_is_skipped(self):
        self._store()  # DB を作る
        add_ledger_body(self.tmpdir, json.dumps({"id": 1, "user_text": 123, "assistant_text": None, "life": "x"}), row_id=1)
        fresh = self._store()
        self.assertEqual(fresh.unconsolidated(), [])
        self.assertIn("1 件", fresh.warning or "")

    def test_row_with_non_int_life_is_retained_with_life_zero(self):
        # 本文が正しい行は life が壊れていても捨てない (熏習の材料そのものは無事なので、
        # life という付帯情報のために往復を失うのは行き過ぎ)。0 へ補正して読む。
        self._store()
        add_ledger_body(self.tmpdir, json.dumps({"id": 1, "user_text": "u", "assistant_text": "a", "life": "x"}), row_id=1)
        fresh = self._store()
        window = fresh.unconsolidated()
        self.assertEqual([t.id for t in window], [1])
        self.assertEqual(window[0].life, 0)
        self.assertEqual(window[0].user_text, "u")
        self.assertIsNone(fresh.warning)  # 読み飛ばしではないので警告は出さない

    def test_rejects_table_and_corrupt_body_both_reported(self):
        # ledger_rejects 表 (移行時に読めなかった JSONL の行) が空でなければ警告に出る。読めない本文は走査時にさらに足される。
        self._store()
        mutate(self.tmpdir, lambda c: c.execute("INSERT INTO ledger_rejects(source_line, raw) VALUES (3, '{not json')"))
        add_ledger_body(self.tmpdir, "{not json too")
        fresh = self._store()
        self.assertIn("台帳の読めない行 1 件 (移行時に ledger.jsonl から)", fresh.warning)
        fresh.unconsolidated()
        self.assertIn("移行時に ledger.jsonl から", fresh.warning)
        self.assertIn("読み飛ばしました", fresh.warning)
        self.assertIn("台帳の読めない行 1 件 (移行時に ledger.jsonl から); 台帳の読めない行 1 件を読み飛ばしました", fresh.warning)  # F8: "; " で区切る

    def test_mark_consolidated_cannot_pass_the_last_row(self):
        store = self._store()
        for i in range(3):
            store.append("u{}".format(i), "a{}".format(i), life=1)
        store.mark_consolidated(10 ** 6)
        self.assertEqual(store.consolidated_upto(), 3)
        store.append("u3", "a3", life=1)
        self.assertEqual(store.pending_count(), 1)

    def test_tail_with_non_positive_n_returns_empty(self):
        store = self._store()
        for i in range(3):
            store.append("u{}".format(i), "a{}".format(i), life=1)
        self.assertEqual(store.tail(-1), [])
        self.assertEqual(store.tail(0), [])

    def test_append_coerces_non_string_text_to_empty(self):
        store = self._store()
        new_id = store.append(123, None, life=1)
        store.commit()
        row = json.loads(table_bodies(self.tmpdir, "ledger")[0])
        self.assertEqual(row["user_text"], "")
        self.assertEqual(row["assistant_text"], "")
        turn = store.unconsolidated()[0]
        self.assertIsInstance(turn, LedgerTurn)
        self.assertEqual(turn.id, new_id)
        self.assertEqual(turn.user_text, "")
        self.assertEqual(turn.assistant_text, "")

    def test_load_meta_clamps_consolidated_upto_from_disk_to_last_row(self):
        # mark_consolidated の切り詰めと同じものを、DB から読んだ consolidated_upto 側にも加えてある。
        store = self._store()
        store.append("u1", "a1", life=1)
        store.append("u2", "a2", life=1)
        store.commit()
        set_meta(self.tmpdir, "ledger.consolidated_upto", 999)
        fresh = self._store()
        self.assertEqual(fresh.consolidated_upto(), 2)
        self.assertEqual(fresh.pending_count(), 0)

class TestLedgerProjectField(unittest.TestCase):
    """改修 5 spec §4.2: 台帳の行と LedgerTurn の `project` 欄。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl = os.path.join(self.tmpdir, "ledger.jsonl")
        self.meta = os.path.join(self.tmpdir, "ledger_meta.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_writes_project_and_unconsolidated_carries_it(self):
        store = ledger_store_at(self.jsonl, self.meta)
        store.append("u", "a", life=1, project="/home/dev/raiya")
        store.append("u2", "a2", life=1)
        store.commit()
        rows = [json.loads(b) for b in table_bodies(self.tmpdir, "ledger")]
        self.assertEqual(rows[0]["project"], "/home/dev/raiya")
        self.assertNotIn("project", rows[1])  # None は欄を書かない
        with ro(self.tmpdir) as conn:  # 派生列は本文から同じ読み関数で書く
            self.assertEqual(
                conn.execute("SELECT id, project FROM ledger ORDER BY seq").fetchall(),
                [(1, "/home/dev/raiya"), (2, None)],
            )
        turns = store.unconsolidated()
        self.assertEqual([t.project for t in turns], ["/home/dev/raiya", None])

    def test_non_str_project_in_row_is_read_as_none(self):
        ledger_store_at(self.jsonl, self.meta)
        add_ledger_body(self.tmpdir, json.dumps({"id": 1, "life": 1, "user_text": "u", "assistant_text": "a", "project": 123}), row_id=1)
        store = ledger_store_at(self.jsonl, self.meta)
        self.assertIsNone(store.unconsolidated()[0].project)
        self.assertIsNone(store.warning)

    def test_ledger_turn_default_project_is_none_and_replace_keeps_it(self):
        import dataclasses

        t = LedgerTurn(user_text="u", assistant_text="a", id=3, project="/p")
        self.assertEqual(dataclasses.replace(t, user_text="x").project, "/p")
        self.assertIsNone(LedgerTurn(user_text="u", assistant_text="a").project)


class TestGroundingTextHelpers(unittest.TestCase):
    """設計書 §10.6 処方 5/6/8: 記録/根拠の本文を作る純関数 (決定論)。"""

    PASTE = '<pasted_content id="1d30">\n再開ポイントを保存しました\n</pasted_content id="1d30">'

    def test_strip_pasted_removes_block_with_matching_id_and_keeps_typed_text(self):
        # 実測の形 (打った文が先・貼り付けが後で 1 本の文字列)。
        self.assertEqual(ledger.strip_pasted("再開して\n\n" + self.PASTE), "再開して")
        self.assertEqual(ledger.strip_pasted(self.PASTE + "\n\n\n\n上を読んで"), "上を読んで")

    def test_strip_pasted_removes_each_block_separately(self):
        text = '前文 <pasted_content id="a">A</pasted_content id="a"> 中文 <pasted_content id="b">B</pasted_content id="b"> 後文'
        self.assertEqual(ledger.strip_pasted(text), "前文  中文  後文")

    def test_strip_pasted_leaves_mismatched_or_unclosed_blocks_untouched(self):
        mismatched = '打った文 <pasted_content id="a">貼った</pasted_content id="b">'
        unclosed = '打った文 <pasted_content id="a">貼った'
        plain_close = '打った文 <pasted_content id="a">貼った</pasted_content>'
        for text in (mismatched, unclosed, plain_close):
            self.assertEqual(ledger.strip_pasted(text), text)

    def test_strip_pasted_of_paste_only_text_is_empty(self):
        self.assertEqual(ledger.strip_pasted("\n\n" + self.PASTE + "\n"), "")

    def test_strip_pasted_matches_across_elision_when_both_tags_survive_truncation(self):
        # 実測の形 (打った文 + 末尾に長い貼り付け) を台帳の上限で切っても、開きタグは先頭側・
        # 閉じタグは末尾側に残る -> 印ごと剥がれて打った文だけが根拠になる (id 248 の大きい版)。
        paste = '<pasted_content id="ab12">\n' + "貼" * 10000 + '\n</pasted_content id="ab12">'
        truncated, _ = ledger.elide_middle("再開して\n\n" + paste, 8000)
        self.assertEqual(ledger.strip_pasted(truncated), "再開して")

    def test_strip_pasted_leaves_paste_whose_close_tag_was_elided(self):
        # 既知の穴: 貼り付けの後ろに長い本文が続き閉じタグが省略側に落ちると対にならず、
        # 規則どおり無変更 (残った貼り付けの断片は根拠に混ざる)。推測で剥がさない。
        paste = '<pasted_content id="ab12">\n' + "貼" * 10000 + '\n</pasted_content id="ab12">'
        truncated, _ = ledger.elide_middle("再開して\n\n" + paste + "打" * 5000, 8000)
        self.assertEqual(ledger.strip_pasted(truncated), truncated)

    def test_strip_pasted_accepts_empty_and_regex_special_ids_and_crlf_bodies(self):
        self.assertEqual(ledger.strip_pasted('打った <pasted_content id="">x</pasted_content id="">'), "打った")
        special = '打った <pasted_content id="a.*(">x\r\ny</pasted_content id="a.*(">'
        self.assertEqual(ledger.strip_pasted(special), "打った")
        # 特殊文字の id が別の id に一致してしまわない (正規表現として解釈しない)。
        crossed = '打った <pasted_content id="a.*(">x</pasted_content id="abc(">'
        self.assertEqual(ledger.strip_pasted(crossed), crossed)

    def test_strip_pasted_pairs_each_open_with_the_next_close_of_the_same_id(self):
        # 閉じの無い開き (id=a) は残し、その後ろの対 (id=b) は除く。同じ id が 2 度出れば
        # それぞれ最初の閉じと対になる。
        text = ('前 <pasted_content id="a">残る '
                '<pasted_content id="b">B1</pasted_content id="b"> 中 '
                '<pasted_content id="b">B2</pasted_content id="b"> 後')
        self.assertEqual(ledger.strip_pasted(text), '前 <pasted_content id="a">残る  中  後')

    def test_strip_private_removes_blocks_and_leaves_unclosed_untouched(self):
        self.assertEqual(ledger.strip_private("猫が好き <private>住所は秘密</private>\n\n\n\n以上"), "猫が好き \n\n以上")
        self.assertEqual(ledger.strip_private("猫 <private>閉じ無し"), "猫 <private>閉じ無し")

    def test_strip_private_is_case_insensitive_and_tolerates_whitespace_in_tags(self):
        for text in ("猫 <Private>秘</Private> 犬", "猫 <private >秘</ private> 犬",
                     "猫 <PRIVATE>秘\r\n秘</private > 犬", "猫 < private >秘< / PRIVATE > 犬"):
            self.assertEqual(ledger.strip_private(text), "猫  犬", msg=text)
        self.assertEqual(ledger.strip_private("<private>a</private>中<private>b</private>"), "中")

    def test_strip_private_accepted_limits_nested_and_crossing_a_paste(self):
        # 受け入れる限界 (docstring): 入れ子は最初の閉じで終わり、外側の閉じタグと後ろの
        # 本文は残る。貼り付けの塊を跨ぐ `<private>` は塊の境界を見ずに最初の閉じまでを除く
        # (結果として貼り付けの閉じタグだけが残り、strip_pasted はその塊を対にできない)。
        self.assertEqual(ledger.strip_private("<private>a<private>b</private>c</private>"), "c</private>")
        crossing = '打 <private>秘 <pasted_content id="p">貼</private>る</pasted_content id="p"> 了'
        self.assertEqual(ledger.strip_private(crossing), '打 る</pasted_content id="p"> 了')

    def test_stripping_is_linear_on_many_unclosed_open_tags(self):
        # recall (同期 hook・<1 秒) の経路に乗るため。旧実装 (`<open>.*?<close>` の 1 本の
        # 正規表現) はこの入力で約 7 秒かかった。
        unclosed_private = ("<private>" + "x" * 91) * 4000
        unclosed_pasted = "".join('<pasted_content id="{}">'.format(i) + "y" * 80 for i in range(4000))
        unclosed_pasted += '</pasted_content id="other">'
        for strip, text in ((ledger.strip_private, unclosed_private), (ledger.strip_pasted, unclosed_pasted)):
            started = time.perf_counter()
            self.assertEqual(strip(text), text)
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_strip_functions_do_not_rewrite_text_without_blocks(self):
        text = "  前後の空白と\n\n\n\n空行もそのまま  "
        self.assertEqual(ledger.strip_private(text), text)
        self.assertEqual(ledger.strip_pasted(text), text)

    def test_elide_middle_keeps_head_and_tail_with_marker(self):
        self.assertEqual(ledger.elide_middle("短い", 10), ("短い", False))
        text, truncated = ledger.elide_middle("ABCDEFGHIJKLMNOPQRST", 10)
        self.assertTrue(truncated)
        self.assertEqual(text, "ABCDE\n<elided 10 chars>\nPQRST")
        self.assertRegex(text, ledger.ELISION_RE)

    def test_gate_text_replaces_marker_with_cut_that_survives_normalization(self):
        text, _ = ledger.elide_middle("ABCDEFGHIJKLMNOPQRST", 10)
        gated = ledger.gate_text(text)
        self.assertEqual(gated, "ABCDE" + "\n" + ledger.GATE_CUT + "\n" + "PQRST")
        self.assertNotIn("elided", gated)
        # 区切りは sansho の正規化で消えない (消えると跨ぎの引用が繋がってしまう)。
        self.assertIn("￼", sansho._n(gated))
        self.assertEqual(sansho.classify_sansho("EP", gated, ""), "遍計所執")
        self.assertEqual(sansho.classify_sansho("ABCDE", gated, ""), "圓成實")


class TestLedgerGroundingField(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl = os.path.join(self.tmpdir, "ledger.jsonl")
        self.meta = os.path.join(self.tmpdir, "ledger_meta.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_writes_grounding_text_and_unconsolidated_carries_it(self):
        store = ledger_store_at(self.jsonl, self.meta)
        store.append("記録 + 貼り付け", "応答", life=1, grounding_text="記録")
        store.append("全部貼り付け", "応答", life=1, grounding_text="")
        self.assertEqual(store.tail(2)[0]["grounding_text"], "記録")
        turns = store.unconsolidated()
        self.assertEqual([t.grounding_text for t in turns], ["記録", ""])  # "" は None に潰さない
        self.assertEqual(turns[0].user_text, "記録 + 貼り付け")
        self.assertEqual(sansho.as_window(turns), tuple(turns))  # KUN-7 の型検査を通る

    def test_old_row_without_field_loads_as_none_and_bad_typed_field_as_empty(self):
        # 欄が無い (旧形式) -> None = user_text を根拠にする。欄は在るが型が違う (壊れた) ->
        # "" = 根拠なし (閉じる側に倒す。生の user_text へ戻すと貼り付けが根拠に混ざる)。
        # どちらも行自体は読み飛ばさない (warning も立てない)。
        ledger_store_at(self.jsonl, self.meta)
        add_ledger_body(self.tmpdir, json.dumps({"id": 1, "life": 1, "user_text": "旧", "assistant_text": "a"}, ensure_ascii=False), 1)
        for i, bad in enumerate((123, None, ["x"]), start=2):
            add_ledger_body(self.tmpdir, json.dumps({"id": i, "life": 1, "user_text": "型違い", "assistant_text": "a",
                                                     "grounding_text": bad}, ensure_ascii=False), i)
        store = ledger_store_at(self.jsonl, self.meta)
        turns = store.unconsolidated()
        self.assertEqual(
            [(t.user_text, t.grounding_text) for t in turns],
            [("旧", None), ("型違い", ""), ("型違い", ""), ("型違い", "")],
        )
        self.assertIsNone(store.warning)

    def test_append_without_grounding_writes_no_field(self):
        store = ledger_store_at(self.jsonl, self.meta)
        store.append("u", "a", life=1)
        store.commit()
        self.assertNotIn("grounding_text", json.loads(table_bodies(self.tmpdir, "ledger")[0]))

if __name__ == "__main__":
    unittest.main()
