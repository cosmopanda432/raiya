import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import backend, engine, ledger, manas, sansho  # noqa: E402
from raiya.alaya import INDEX_KIND_LIMITS, INDEX_MAX  # noqa: E402
from raiya.engine import MemoryEngine  # noqa: E402
from raiya.snapshot import write_state  # noqa: E402
from tests._state_fixture import add_ledger_body, set_meta  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def imports_backend_at_top_level(source):
    """モジュールソース文字列の直下 (関数内は除く) が "backend" という名前を
    import しているか。

    `import backend` / `import raiya.backend` / `from . import backend` /
    `from raiya import backend` / `from .backend import X` / `from raiya.backend
    import X` のいずれの書き方も検出する。`from . import backend` は
    ImportFrom.module が None になり実際に読み込む名前は names[].name 側に載る
    ため、module 属性だけを見ると見逃す (最初の実装がこれで backend 混入 mutant を
    見逃していた -- names[] も必ず見る、という pin の教訓そのもの)。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name == "backend" or alias.name.endswith(".backend") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "backend" or (node.module or "").endswith(".backend"):
                return True
            if any(alias.name == "backend" for alias in node.names):
                return True
    return False


class TestMemoryEngineDeterministicFacade(unittest.TestCase):
    """記憶機能設計書§4.2 facade の決定論動詞 (recall/imprint/introspect_material/
    record_introspection/audit) が alaya/manas/sansho を正しく束ねていることの pin。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = MemoryEngine(state_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_imprint_returns_a_decision_list_matching_proposals_length(self):
        turn = sansho.Turn(user_text="私は猫が好きです p/ 猫", assistant_text="")
        proposals = [
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "私は猫が好きです p/"},
            },
        ]
        decisions = self.engine.imprint(turn, proposals)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "圓成實")

    def test_recall_returns_fired_seeds_and_narrative(self):
        turn = sansho.Turn(user_text="私は猫が好きです p/ 猫", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "私は猫が好きです p/"},
        }
        self.engine.imprint(turn, [proposal])
        self.engine.imprint(turn, [proposal])  # strength=2 (発芽閾値)

        recalled = self.engine.recall("猫について教えて")
        self.assertEqual(len(recalled["seeds"]), 1)
        self.assertIn("猫が好き", recalled["narrative"])

    def test_imprint_of_fabricated_span_routes_to_quarantine_not_seeds(self):
        turn = sansho.Turn(user_text="今日は晴れです", assistant_text="")
        proposal = {
            "content": "捏造された記憶",
            "conditions": ["晴れ"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "完全に捏造されたspanです"},
        }
        decisions = self.engine.imprint(turn, [proposal])
        self.assertEqual(decisions[0]["decision"], "遍計所執")
        self.assertEqual(self.engine.alaya.list_seeds(), [])

    def test_audit_reports_class_counts_and_quarantine_and_log_tail(self):
        turn = sansho.Turn(user_text="今日は晴れです", assistant_text="")
        proposal = {
            "content": "捏造",
            "conditions": [],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "完全に捏造されたspan"},
        }
        self.engine.imprint(turn, [proposal])
        audit = self.engine.audit()
        self.assertEqual(audit["quarantine_retained"], 1)
        self.assertEqual(audit["quarantine_total"], 1)
        self.assertEqual(len(audit["imprint_log_tail"]), 1)

    def test_audit_reports_current_life_and_follows_begin_new_life(self):
        before = self.engine.audit()["life"]
        self.assertEqual(before, self.engine.alaya.current_life)
        self.engine.begin_new_life()
        self.assertEqual(self.engine.audit()["life"], before + 1)

    def test_audit_life_reflects_other_process_begin_new_life(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        a = engine.MemoryEngine(state_dir=d, new_life=False)
        b = engine.MemoryEngine(state_dir=d, new_life=False)
        new_life = b.begin_new_life()
        self.assertEqual(a.audit()["life"], new_life)

    def test_record_introspection_rejects_blank_or_non_str_without_changing_ego(self):
        for bad in ("", "  \n", None, 1, ["x"]):
            with self.assertRaises(ValueError):
                self.engine.record_introspection(bad)
        self.assertEqual(self.engine.manas.ego_weight, 1.0)
        self.assertEqual(self.engine.manas.state["introspections"], [])

    def test_introspect_material_and_record_introspection_roundtrip(self):
        turn = sansho.Turn(user_text="私は猫が好きです 猫", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "私は猫が好きです"},
        }
        self.engine.imprint(turn, [proposal])
        self.engine.imprint(turn, [proposal])
        self.engine.recall("猫について")  # germinate + manas.inject でログに1件残す

        material = self.engine.introspect_material()
        self.assertEqual(len(material["manas_log"]), 1)

        before_weight = self.engine.manas.ego_weight
        result = self.engine.record_introspection("最近は猫の話ばかりしていたかもしれません")
        self.assertLess(result["ego_weight"], before_weight)

    def test_introspect_material_manas_log_is_isolated_from_caller(self):
        turn = sansho.Turn(user_text="私は猫が好きです p/ 猫", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "私は猫が好きです p/"},
        }
        self.engine.imprint(turn, [proposal])
        self.engine.imprint(turn, [proposal])
        self.engine.recall("猫について")
        material = self.engine.introspect_material()
        material["manas_log"][0]["injected_text"] = "呼び出し側による改竄"
        material["manas_log"][0]["seed_ids"].append(999)
        again = self.engine.introspect_material()
        self.assertIn("猫が好き", again["manas_log"][0]["injected_text"])
        self.assertNotEqual(again["manas_log"][0]["injected_text"], "呼び出し側による改竄")
        self.assertNotIn(999, again["manas_log"][0]["seed_ids"])

    def test_state_dir_defaults_to_raiya_state_dir_env_var(self):
        saved = os.environ.get("RAIYA_STATE_DIR")
        try:
            os.environ["RAIYA_STATE_DIR"] = os.path.join(self.tmpdir, "via_env")
            eng = MemoryEngine()
            self.assertEqual(eng.state_dir, os.path.join(self.tmpdir, "via_env"))
        finally:
            if saved is None:
                os.environ.pop("RAIYA_STATE_DIR", None)
            else:
                os.environ["RAIYA_STATE_DIR"] = saved

    def test_new_life_default_true_advances_life_on_each_construction(self):
        # 既存挙動 pin (c): 引数省略時 (CLI/MCP の従来どおり) は構築のたびに +1。
        # setUp が既に self.tmpdir へ1回構築済み (self.engine.life が起点)。
        baseline = self.engine.life
        first = MemoryEngine(state_dir=self.tmpdir)
        self.assertEqual(first.life, baseline + 1)
        second = MemoryEngine(state_dir=self.tmpdir, new_life=True)
        self.assertEqual(second.life, baseline + 2)

    def test_new_life_false_does_not_advance_life(self):
        # (c): new_life=False は構築時に生を進めない (hooks 専用)。
        baseline = self.engine.life
        first = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(first.life, baseline)
        second = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(second.life, baseline)

    def test_begin_new_life_verb_advances_and_updates_cached_life(self):
        baseline = self.engine.life
        eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(eng.life, baseline)
        new_life = eng.begin_new_life()
        self.assertEqual(new_life, baseline + 1)
        self.assertEqual(eng.life, baseline + 1)
        # 別インスタンスからも見える (lock 経由で save されている)。
        other = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(other.life, baseline + 1)


class TestMemoryEnginePropose(unittest.TestCase):
    """propose() は backend を遅延 import する。呼ばれない限り raiya.backend は
    sys.modules に載らない (mcp_server.py が LLM を一切読み込まない根拠)。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_call_llm = backend.call_llm

    def tearDown(self):
        backend.call_llm = self._saved_call_llm
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_propose_calls_backend_and_returns_parsed_proposals(self):
        def fake_call_llm(prompt, max_tokens=512, purpose=None):
            return json.dumps(
                [
                    {
                        "content": "猫が好き",
                        "conditions": ["猫"],
                        "polarity": "善","kind": "所在",
                        "evidence": {"source": "user", "span": "私は猫が好きです"},
                    }
                ]
            )

        backend.call_llm = fake_call_llm
        eng = MemoryEngine(state_dir=self.tmpdir)
        turn = sansho.Turn(user_text="私は猫が好きです", assistant_text="")
        proposals = eng.propose(turn)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["content"], "猫が好き")

    def test_propose_tags_the_backend_call_with_purpose_propose(self):
        # hooks の stop / backend._call_claude_cli の軽量モデル切替は
        # purpose="propose" を頼りにする。engine.propose() が必ずこれを渡すことの pin。
        calls = []

        def fake_call_llm(prompt, max_tokens=512, purpose=None):
            calls.append(purpose)
            return "[]"

        backend.call_llm = fake_call_llm
        eng = MemoryEngine(state_dir=self.tmpdir)
        turn = sansho.Turn(user_text="私は猫が好きです", assistant_text="")
        eng.propose(turn)
        self.assertEqual(calls, ["propose"])

    def test_engine_module_does_not_import_backend_at_top_level(self):
        # engine.py 自体のトップレベルは backend を import しない (propose() の
        # 関数本体の中だけで遅延 import する)。モジュール直下の文だけを ast で見る
        # (関数内の import は tree.body に現れないため、これだけで十分弁別できる)。
        self.assertFalse(imports_backend_at_top_level(inspect.getsource(engine)))


class TestParseProposals(unittest.TestCase):
    """旧 agent.parse_seed_json 相当 (engine._parse_proposals)。
    既知キーだけを allowlist で読み取る。"""

    def test_extracts_evidence_source_and_span(self):
        raw = (
            '[{"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", '
            '"evidence": {"source": "user", "span": "私は猫が好きです"}}]'
        )
        proposals = engine._parse_proposals(raw)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(
            proposals[0]["evidence"], {"source": "user", "span": "私は猫が好きです"}
        )

    def test_llm_supplied_sansho_key_is_not_carried_into_the_proposal(self):
        # EXE-15 異熟果=無記: LLM が sansho を出力しても proposal に混入させない。
        raw = (
            '[{"content": "猫が好き", "conditions": ["猫"], "polarity": "無記", '
            '"sansho": "圓成實", '
            '"evidence": {"source": "user", "span": "私は猫が好きです"}}]'
        )
        proposals = engine._parse_proposals(raw)
        self.assertEqual(len(proposals), 1)
        self.assertNotIn("sansho", proposals[0])

    def test_kind_is_passed_through_only_when_str(self):
        # 改修5 §3.3: kind は allowlist に入れて通す (列挙・形の検査は alaya.imprint)。非 str は落とす。
        raw = (
            '[{"kind": "決定", "content": "A を採用 (B は却下)", "conditions": ["A"], '
            '"evidence": {"source": "user", "span": "A"}}, '
            '{"kind": 3, "content": "x", "conditions": ["x"], "evidence": {"source": "user", "span": "x"}}]'
        )
        proposals = engine._parse_proposals(raw)
        self.assertEqual(proposals[0]["kind"], "決定")
        self.assertNotIn("kind", proposals[1])

    def test_propose_prompt_asks_for_the_five_kinds(self):
        prompt = engine._build_propose_prompt("u", "a")
        for kind in ("決定", "回避策", "所在", "再開点", "性向"):
            self.assertIn(kind, prompt)
        self.assertIn('"kind":', prompt)
        self.assertIn(engine.KIND_GUIDE, engine._build_consolidate_prompt([], {"related": [], "related_total": 0, "groups": [], "groups_total": 0}))

    def test_kind_guide_asks_for_correction_and_pair_spans(self):
        guide = engine.KIND_GUIDE
        self.assertIn("訂正した発話", guide)
        self.assertIn("別々の span", guide)
        self.assertIn("逐語", guide)
        self.assertIn(guide, engine._build_propose_prompt("u", "a"))

    def test_kind_guide_decision_line_does_not_claim_demotion_to_disposition(self):
        # #114 U (spec 2026-10-30-decision-without-rejection §7 プロンプト追補): 片側の 決定 も 決定 のまま植わるので、
        # 決定 の行は「性向 として扱われる」と言わない。回避策 の行は今も真なので残る。
        lines = {line.split(":", 1)[0].lstrip("- "): line for line in engine.KIND_GUIDE.splitlines() if line.startswith("- ")}
        self.assertNotIn("性向 として扱われる", lines["決定"])
        # #116: 片側の決定で却下側を作文させないため「両方を書く」指示は外し、作文の禁止を書く
        self.assertNotIn("両方", lines["決定"])
        self.assertIn("作文しない", lines["決定"])
        self.assertIn("逐語で含める", lines["決定"])
        self.assertIn("性向 として扱われる", lines["回避策"])

    def test_consolidate_prompt_shows_invalid_kind_as_disposition(self):
        # #102: 索引・群の行の kind 表示は alaya.seed_kind に揃える (VALID_KINDS 外の kind は 性向 として LLM に届く)
        bad = {"id": 7, "kind": "謎", "sansho": "圓成實", "content": "x", "conditions": ["x"]}
        view = {"related": [bad], "related_total": 1,
                "groups": [{"representative": 7, "rep": bad, "kind": "謎", "label": "x", "size": 2, "project": None}],
                "groups_total": 1}
        prompt = engine._build_consolidate_prompt([], view)
        self.assertNotIn("謎", prompt)
        self.assertIn("#7 [性向] [圓成實] x", prompt)
        # rep に kind が無ければ群の kind を読む (不正なら同じく 性向)
        view["groups"][0]["rep"] = {"id": 7, "sansho": "圓成實", "content": "x"}
        view["groups"][0]["kind"] = "所在"
        self.assertIn("#7 [所在] [圓成實] x | 群", engine._build_consolidate_prompt([], view))
        # rep の kind が不正で群の kind が正しいときは rep を読んで 性向 (今の選択をピン)
        view["groups"][0]["rep"] = dict(bad)
        self.assertIn("#7 [性向] [圓成實] x | 群", engine._build_consolidate_prompt([], view))

    def test_propose_prompt_states_the_refetchable_facts_exclusion(self):
        self.assertIn("再取得できる事実", engine._build_propose_prompt("u", "a"))

    def test_missing_evidence_key_defaults_to_empty_source_and_span(self):
        raw = '[{"content": "猫が好き", "conditions": ["猫"], "polarity": "無記"}]'
        proposals = engine._parse_proposals(raw)
        self.assertEqual(proposals[0]["evidence"], {"source": "", "span": ""})

    def test_empty_array_returns_empty_list(self):
        self.assertEqual(engine._parse_proposals("[]"), [])

    def test_strips_markdown_fence_and_caps_at_two_items(self):
        items = [
            {
                "content": "種{}".format(i),
                "conditions": ["猫"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "猫"},
            }
            for i in range(4)
        ]
        raw = "```json\n{}\n```".format(json.dumps(items, ensure_ascii=False))
        proposals = engine._parse_proposals(raw)
        self.assertEqual([p["content"] for p in proposals], ["種0", "種1"])

    def test_non_list_json_and_garbage_return_empty_list(self):
        self.assertEqual(engine._parse_proposals('{"content": "x"}'), [])
        self.assertEqual(engine._parse_proposals("not json at all"), [])

    def test_non_list_conditions_become_empty_list(self):
        for bad in ("猫", 3, {"a": "猫"}):
            raw = json.dumps([{"content": "猫が好き", "conditions": bad,
                               "evidence": {"source": "user", "span": "猫"}}], ensure_ascii=False)
            self.assertEqual(engine._parse_proposals(raw)[0]["conditions"], [], bad)

    def test_opening_fence_without_closing_fence_still_parses(self):
        raw = '```json\n[{"content": "猫が好き", "conditions": ["猫"]}]'
        proposals = engine._parse_proposals(raw)
        self.assertEqual([p["content"] for p in proposals], ["猫が好き"])
        self.assertEqual(engine._extract_json_array(raw), [{"content": "猫が好き", "conditions": ["猫"]}])


class TestParseProposalsNotes(unittest.TestCase):
    """`_parse_proposals(raw, notes=[])`: 寛容パースが働いた理由を notes に積む。

    「LLM が [] を正常に返した」(健全・note 無し)と「出力を読み取れなかった」
    (異常・note 有り)を区別するための診断専用引数。返り値(proposals)は notes の
    有無に関わらず TestParseProposals の既存挙動と同一でなければならない
    (各テストで unchanged 側もあわせて assert する)。
    """

    def test_healthy_empty_array_produces_no_note(self):
        # 健全な空提案: [] が正しく JSON として読める場合は notes に何も積まない。
        notes = []
        proposals = engine._parse_proposals("[]", notes=notes)
        self.assertEqual(proposals, [])
        self.assertEqual(notes, [])

    def test_no_brackets_found_records_note_with_bounded_length_fact(self):
        # prose 拒否/空応答など、[ ] が全く見つからない場合。生の文面は積まず、
        # 長さという有界な事実のみを記す。
        notes = []
        raw = "申し訳ありませんが、その依頼にはお応えできません。"
        proposals = engine._parse_proposals(raw, notes=notes)
        self.assertEqual(proposals, engine._parse_proposals(raw))  # 返り値は不変
        self.assertEqual(len(notes), 1)
        self.assertIn("角括弧", notes[0])
        self.assertIn("len={}".format(len(raw)), notes[0])
        self.assertNotIn(raw, notes[0])  # 生の本文そのものは書かない

    def test_no_brackets_found_on_genuinely_empty_response(self):
        # 空応答 (raw_text="" や空白のみ) も同じ分岐: len=0 の note。
        notes = []
        proposals = engine._parse_proposals("   ", notes=notes)
        self.assertEqual(proposals, [])
        self.assertEqual(notes, ["角括弧([ ])が出力中に見つからない(len=0)"])

    def test_json_decode_error_records_exception_class_name(self):
        # 角括弧自体は存在する(start/end が見つかる)が、中身が壊れた JSON。
        notes = []
        raw = "[this is not valid json]"
        proposals = engine._parse_proposals(raw, notes=notes)
        self.assertEqual(proposals, engine._parse_proposals(raw))
        self.assertEqual(len(notes), 1)
        self.assertIn("JSON解析失敗", notes[0])
        self.assertIn("JSONDecodeError", notes[0])

    def test_more_than_two_items_records_truncation_note(self):
        items = [
            {
                "content": "種{}".format(i),
                "conditions": ["猫"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "猫"},
            }
            for i in range(4)
        ]
        raw = json.dumps(items, ensure_ascii=False)
        notes = []
        proposals = engine._parse_proposals(raw, notes=notes)
        self.assertEqual([p["content"] for p in proposals], ["種0", "種1"])  # 返り値は不変
        self.assertEqual(notes, ["4件中2件のみ採用(上限2)"])

    def test_exactly_two_items_produces_no_truncation_note(self):
        # 境界の反対側: 2件はいつもの健全な最大件数。ここで note が出ると、
        # 健全な2提案セッションの hooks.log が毎回「切り捨てました」と誤報する
        # (`len(data) > 2` が `>= 2` に化けた場合の回帰 pin。3件側の
        # test_more_than_two_items_records_truncation_note と対で境界の両側を張る)。
        items = [
            {
                "content": "種{}".format(i),
                "conditions": ["猫"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "猫"},
            }
            for i in range(2)
        ]
        raw = json.dumps(items, ensure_ascii=False)
        notes = []
        proposals = engine._parse_proposals(raw, notes=notes)
        self.assertEqual(len(proposals), 2)
        self.assertEqual(notes, [])

    def test_item_validation_rejection_records_count(self):
        # 1件目は content 欠落で却下、2件目は dict ですらない、3件目は正常。
        # data[:2] で 3 件目は最初から候補外になるため、却下されるのは 1,2 件目の 2 件。
        raw = json.dumps(
            [
                {"conditions": ["猫"], "polarity": "無記"},  # content 欠落
                "not-a-dict",
                {
                    "content": "猫が好き",
                    "conditions": ["猫"],
                    "polarity": "無記","kind": "所在",
                    "evidence": {"source": "user", "span": "猫"},
                },
            ],
            ensure_ascii=False,
        )
        notes = []
        proposals = engine._parse_proposals(raw, notes=notes)
        self.assertEqual(proposals, engine._parse_proposals(raw))
        self.assertEqual(proposals, [])  # 3件目は data[:2] の外で候補にすら入らない
        # 3件あるため上限2件の切り捨てnoteも先に積まれる (2つのnoteは互いに異なる文言)。
        self.assertEqual(
            notes,
            [
                "3件中2件のみ採用(上限2)",
                "項目検証で2件を却下(dict でない/content 欠落)",
            ],
        )

    def test_notes_default_none_does_not_raise(self):
        # notes を渡さない既存の呼び出し方は今までどおり動く。
        self.assertEqual(engine._parse_proposals("not json at all"), [])

    def test_non_list_notes_argument_is_ignored_not_raised(self):
        # notes に list 以外 (例: tuple) を渡しても、寛容パースは例外を出さず
        # 黙って診断を諦める (`_note` の isinstance 番人)。cmd_stop はここで例外に
        # なると有料の LLM 呼び出し後に往復ごと熏習を落としてしまうため、
        # 診断専用引数の型崩れが fail-soft を破らないことの pin。
        proposals = engine._parse_proposals("no brackets here", notes=())
        self.assertEqual(proposals, [])


class TestBuildProposePrompt(unittest.TestCase):
    def test_prompt_requires_evidence_and_keeps_json_array_marker(self):
        prompt = engine._build_propose_prompt("ユーザー発言", "アシスタント応答")
        self.assertIn("evidence", prompt)
        self.assertIn("JSON配列", prompt)
        self.assertIn("身分", prompt)  # 「身分は出力しないでください」の明示

    def test_prompt_requires_conditions_verbatim_in_turn_text(self):
        # ゲートは縁を逐語接地する。契約も span と同じく逐語を要求する。
        # schema 例の "conditions" や span の「逐語引用」だけでは 縁 が conditions の後に無い。
        # 順序だけでは足りない: 否定文・個数削除・「出さない」節の削除をそれぞれ殺す。
        prompt = engine._build_propose_prompt("ユーザー発言", "アシスタント応答")
        cond_at = prompt.find("conditions")
        self.assertGreaterEqual(cond_at, 0)
        en_at = prompt.find("縁", cond_at)
        self.assertGreaterEqual(en_at, 0)
        seg = prompt[en_at:]
        self.assertIn("逐語で現れる語", seg)
        self.assertIn("1〜3", seg)
        self.assertIn("その記録は出さない", seg)


class TestBuildConsolidatePrompt(unittest.TestCase):
    def test_prompt_requires_conditions_verbatim_in_window_text(self):
        # 統合も conditions を出させる。窓の往復本文への逐語接地を契約に書く。
        # 順序だけでは足りない: 否定文・個数削除・「出さない」節の削除をそれぞれ殺す。
        prompt = engine._build_consolidate_prompt([], {"related": [], "related_total": 0, "groups": [], "groups_total": 0})
        cond_at = prompt.find("conditions")
        self.assertGreaterEqual(cond_at, 0)
        en_at = prompt.find("縁", cond_at)
        self.assertGreaterEqual(en_at, 0)
        seg = prompt[en_at:]
        self.assertIn("逐語で現れる語", seg)
        self.assertIn("1〜3", seg)
        self.assertIn("その記録は出さない", seg)


class TestConsolidatedMemoryFacade(unittest.TestCase):
    """改修4 §7: record_turn / consolidate / recall(context) / audit。LLM は backend.call_llm を patch。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_turn_appends_to_ledger_and_reports_pending(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        r1 = eng.record_turn("猫が好きです", "いいですね", session_id="s1")
        r2 = eng.record_turn("犬も", "そうですか")
        self.assertEqual((r1["ledger_id"], r1["pending"]), (1, 1))
        self.assertEqual((r2["ledger_id"], r2["pending"]), (2, 2))
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "raiya.db")))
        other = MemoryEngine(state_dir=self.tmpdir, new_life=False)  # 別インスタンスから見える
        self.assertEqual(other.audit()["ledger"], {"turns": 2, "consolidated_upto": 0, "pending": 2})

    def test_record_turn_writes_project_to_ledger_row(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好きです", "いいですね", project="/home/dev/raiya")
        eng.record_turn("犬も", "そうですか", project=7)  # 非 str は欄を書かない
        rows = eng.ledger.tail(2)
        self.assertEqual(rows[0]["project"], "/home/dev/raiya")
        self.assertIsNone(rows[1]["project"])
        self.assertEqual([t.project for t in eng.ledger.unconsolidated()], ["/home/dev/raiya", None])

    def test_audit_reports_ledger_warning(self):
        """壊れた台帳の行を観測できる唯一の窓口が audit であること。

        壊れた行の warning は最初の行走査で確定する = `MemoryEngine.__init__` が集める
        `warnings` には載らない。audit は `count()`/`pending_count()` で行を読むので、
        `ledger_warning` を読む時点では確定している。
        """
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好きです", "いいですね")
        self.assertIsNone(eng.audit()["ledger_warning"])  # 健全な台帳では None
        add_ledger_body(self.tmpdir, "{not json")
        fresh = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(fresh.warnings, [])  # 構築時点ではまだ行を読んでいない
        audit = fresh.audit()
        self.assertIsNotNone(audit["ledger_warning"])
        self.assertIn("台帳の読めない行", audit["ledger_warning"])
        self.assertEqual(audit["ledger"]["turns"], 1)  # 壊れた行だけ読み飛ばす

    def test_record_turn_from_stale_engine_does_not_rewind_cursor(self):
        # 多重書き手 pin: 台帳の meta を in-memory に持ったままの engine_a が、
        # engine_b の統合で進んだ consolidated_upto を古い値で上書きしないこと
        # (= _locked が台帳も reload してから append する)。巻き戻ると統合済みの
        # 往復が再統合され、有料の LLM 呼び出しと重複熏習が起きる。
        engine_a = MemoryEngine(state_dir=self.tmpdir)
        engine_a.record_turn("猫が好き", "いいですね")
        engine_b = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            engine_b.consolidate()
        engine_a.record_turn("犬も好き", "そうですか")  # engine_a の meta は stale
        fresh = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.assertEqual(fresh.audit()["ledger"], {"turns": 2, "consolidated_upto": 1, "pending": 1})

    def test_consolidate_with_empty_window_does_not_call_backend(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        with mock.patch("raiya.backend.call_llm", side_effect=AssertionError("呼ばれてはいけない")):
            result = eng.consolidate()
        self.assertEqual(result, {"window": [], "proposals": [], "decisions": [], "skipped": "empty", "window_sessions": 0})

    def test_consolidate_imprints_grounded_dispositions_and_advances_cursor(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("コマンドを表示して p/", "了解、表示します")
        eng.record_turn("stdio専用は変な仕様", "HTTPにも対応できます")
        raw = json.dumps([
            {"action": "new", "target_id": None, "content": "コマンドは実行前に表示する",
             "conditions": ["コマンド", "表示"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "コマンドを表示して p/"}]},
            {"action": "new", "content": "捏造", "conditions": ["コマンド"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "存在しない引用"}]},
        ], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw) as call:
            notes = []
            result = eng.consolidate(notes=notes)
        self.assertEqual(call.call_args.kwargs.get("purpose"), "propose")
        prompt = call.call_args.args[0]
        self.assertIn("--- 往復 1 (project: (無し)) ---", prompt)
        self.assertIn("コマンドを表示して", prompt)
        self.assertIn("(無し)", prompt)  # 既存性向の索引が空
        self.assertEqual(result["window"], [1, 2])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["圓成實", "遍計所執"])
        self.assertIsNone(result["skipped"])
        self.assertEqual(notes, [])
        audit = eng.audit()
        self.assertEqual(audit["seed_count"], 1)
        self.assertEqual(audit["ledger"], {"turns": 2, "consolidated_upto": 2, "pending": 0})
        self.assertEqual(audit["quarantine_retained"], 1)
        seed = eng.alaya.list_seeds()[0]
        self.assertEqual(seed["evidences"][0]["ledger_id"], 1)

    def test_consolidate_prompt_lists_existing_index_and_supersede_works_end_to_end(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("コーヒーが好き p/", "")
        first = json.dumps([{"content": "コーヒーが好き", "conditions": ["コーヒー"], "polarity": "善","kind": "所在",
                             "evidence": [{"source": "user", "span": "コーヒーが好き p/"}]}], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=first):
            eng.consolidate()
        seed_id = eng.alaya.list_seeds()[0]["id"]
        eng.record_turn("最近はコーヒーより緑茶 p/", "")
        second = json.dumps([{"action": "supersede", "target_id": seed_id, "content": "緑茶が好き",
                              "conditions": ["緑茶"], "polarity": "善","kind": "所在",
                              "evidence": [{"source": "user", "span": "最近はコーヒーより緑茶 p/"}]}], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=second) as call:
            result = eng.consolidate()
        self.assertIn("#{} [所在] [圓成實] コーヒーが好き".format(seed_id), call.call_args.args[0])
        self.assertTrue(result["decisions"][0].get("superseded"))
        seed = eng.alaya.list_seeds()[0]
        self.assertEqual(seed["content"], "緑茶が好き")
        self.assertEqual(len(seed["history"]), 1)
        self.assertEqual(eng.audit()["history_total"], 1)

    def test_consolidate_marks_window_consumed_even_with_zero_proposals(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("雑談", "雑談")
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            result = eng.consolidate()
        self.assertEqual(result["decisions"], [])
        self.assertEqual(eng.audit()["ledger"]["pending"], 0)

    def test_consolidate_unparseable_output_leaves_window_pending(self):
        """出力を配列として読み取れなかった統合は窓を消費しない (改修4 F1)。

        途中で切れた JSON を「提案 0 件」と同一視すると、窓は統合済みとして mark され、
        後には parse_notes の 1 行しか残らない -- 出力が長くなる=内容の濃い統合ほど
        失われる、という最悪の壊れ方をする。読み取れなかった窓は未統合のまま残し、
        次の統合が同じ窓を読み直す。
        """
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好きです", "いいですね")
        cut_off = '[{"content": "x", "evidence": [{"span": "'  # max_tokens で切れた出力
        notes = []
        with mock.patch("raiya.backend.call_llm", return_value=cut_off):
            result = eng.consolidate(notes=notes)
        self.assertEqual(result["skipped"], "unparseable")
        self.assertEqual(result["window"], [1])  # 窓は報告するが消費はしない
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["decisions"], [])
        self.assertTrue(notes)  # 読み取れなかった理由は有界に残る
        audit = eng.audit()
        self.assertEqual(audit["ledger"], {"turns": 1, "consolidated_upto": 0, "pending": 1})
        self.assertEqual(audit["seed_count"], 0)
        self.assertEqual(audit["quarantine_retained"], 0)
        # 次の統合が同じ窓を読み直して熏習できる (試行回数は数えない: 統合は Stop の
        # たびに高々 1 回しか走らないので、再試行はその周期で自然に有界)。
        raw = json.dumps([{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                           "evidence": [{"source": "user", "span": "猫が好きです"}]}], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            second = eng.consolidate()
        self.assertEqual(second["window"], [1])
        self.assertIsNone(second["skipped"])
        self.assertEqual(eng.audit()["ledger"]["pending"], 0)

    def test_consolidate_skips_partial_overlap_without_imprinting(self):
        """#103: LLM 応答待ちの間に別の書き手が窓の前半だけ統合した -> 重なった往復を二度
        熏習しないよう、書込を丸ごと skip する (cursor も進めない)。残りは次の統合が拾う。"""
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好き", "")
        eng.record_turn("犬も好き", "")
        raw = json.dumps([{"content": "猫が好き", "conditions": ["猫"], "polarity": "善", "kind": "所在",
                           "evidence": [{"source": "user", "span": "猫が好き"}]}], ensure_ascii=False)

        def _race(prompt, **kwargs):
            other = MemoryEngine(state_dir=self.tmpdir, new_life=False)
            other.ledger.reload()
            other.ledger.mark_consolidated(1)
            other.ledger.commit()
            return raw

        bak = os.path.join(self.tmpdir, "raiya.db.bak")
        self.assertFalse(os.path.exists(bak))
        with mock.patch("raiya.backend.call_llm", side_effect=_race):
            result = eng.consolidate()
        self.assertFalse(os.path.exists(bak))  # 何も書かない run は backup も作らない
        self.assertEqual(result["skipped"], "partial_overlap")
        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["window"], [1, 2])
        self.assertEqual(eng.audit()["seed_count"], 0)
        eng.ledger.reload()
        self.assertEqual(eng.ledger.consolidated_upto(), 1)
        # 次の統合は未統合の尾 (turn 2) だけを窓にする。
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            again = eng.consolidate()
        self.assertEqual(again["window"], [2])
        self.assertIsNone(again["skipped"])

    def test_consolidate_skips_when_another_writer_consolidated_first(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好き", "")
        raw = json.dumps([{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                           "evidence": [{"source": "user", "span": "猫が好き"}]}], ensure_ascii=False)

        def _race(prompt, **kwargs):
            # LLM 応答待ちの間に別プロセスが同じ窓を統合した状況を再現する。
            # 注記: StateLock は再入不可 (別 fd の flock) なので、LLM 呼び出しが
            # lock の内側へ移ったらこの pin は fail ではなくハングして落ちる。
            other = MemoryEngine(state_dir=self.tmpdir, new_life=False)
            other.ledger.reload()
            other.ledger.mark_consolidated(1)
            other.ledger.commit()
            return raw

        with mock.patch("raiya.backend.call_llm", side_effect=_race):
            result = eng.consolidate()
        self.assertEqual(result["skipped"], "already_consolidated")
        self.assertEqual(result["decisions"], [])
        self.assertEqual(eng.audit()["seed_count"], 0)

    def test_consolidate_parse_notes_on_broken_output(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好き", "")
        with mock.patch("raiya.backend.call_llm", return_value="no json here"):
            notes = []
            result = eng.consolidate(notes=notes)
        self.assertEqual(result["proposals"], [])
        self.assertEqual(len(notes), 1)
        self.assertNotIn("no json here", notes[0])  # 生の LLM 出力は記録しない

    def test_parse_consolidation_normalizes_action_target_and_evidence(self):
        raw = json.dumps([
            {"content": "a", "action": "supersede", "target_id": 3, "evidence": {"source": "user", "span": "x"}},
            {"content": "b", "action": "delete", "target_id": "3", "evidence": [{"span": "y"}, "junk"]},
            {"content": "c", "target_id": True, "sansho": "圓成實"},
            {"content": "d"}, {"content": "e"}, {"content": "f"},
            "junk", {"conditions": ["no content"]},
        ], ensure_ascii=False)
        notes = []
        proposals = engine._parse_consolidation(raw, notes=notes)
        self.assertEqual(len(proposals), engine.CONSOLIDATE_MAX)
        self.assertEqual(proposals[0], {"content": "a", "conditions": [], "polarity": "無記", "action": "supersede",
                                        "target_id": 3, "evidence": [{"source": "user", "span": "x"}]})
        self.assertEqual(proposals[1]["action"], "new")
        self.assertIsNone(proposals[1]["target_id"])
        # 形の壊れた要素 ("junk") は捨てずに空 span へ落とす (fail-closed): 捨てると
        # 接地済みの span だけが残り、この提案が圓成實として採用されうる。
        self.assertEqual(proposals[1]["evidence"], [{"source": "", "span": "y"}, {"source": "", "span": ""}])
        self.assertIsNone(proposals[2]["target_id"])
        self.assertNotIn("sansho", proposals[2])
        self.assertTrue(any("上限" in n for n in notes))

    def test_parse_consolidation_non_list_conditions_become_empty_list(self):
        raw = json.dumps([{"content": "a", "conditions": "猫"}, {"content": "b", "conditions": ["犬", 2]}],
                         ensure_ascii=False)
        proposals = engine._parse_consolidation(raw)
        self.assertEqual(proposals[0]["conditions"], [])
        self.assertEqual(proposals[1]["conditions"], ["犬", "2"])

    def test_consolidate_quarantines_proposal_with_malformed_evidence_item(self):
        """evidence に壊れた要素が1つでも混ざれば提案は遍計所執 (改修4 §13 裁定1)。

        パーサが非 dict 要素を捨てていた頃は、`consolidate` 経由だと接地済みの span
        だけが残って圓成實として採用され、同じ提案を `imprint`/MCP 経由で通したとき
        (sansho.evidence_list が空 span へ落とす) と結論が食い違っていた。
        """
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好きです", "いいですね")
        raw = json.dumps([{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                           "evidence": [{"source": "user", "span": "猫が好きです"}, "junk"]}],
                         ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            result = eng.consolidate()
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執"])
        audit = eng.audit()
        self.assertEqual(audit["seed_count"], 0)
        self.assertEqual(audit["quarantine_retained"], 1)

    def test_recall_does_not_scan_ledger_rows(self):
        """同期 hook の `recall` は台帳の行を1行も読まない (改修4 §13 裁定3)。

        台帳は削除されず伸び続ける (実運用で 240MB = 全行走査 0.4 秒)。構築や recall が
        行を走査する形だと、<1 秒の予算で回る UserPromptSubmit hook が台帳の長さに
        比例して遅くなる (README/CLAUDE.md の「recall は台帳に触らない」とも食い違う)。
        """
        from raiya.ledger import LedgerStore

        seed_engine = MemoryEngine(state_dir=self.tmpdir)
        seed_engine.record_turn("猫が好きです", "いいですね")  # 台帳に 1 行ある状態にする
        calls = []
        original = LedgerStore._read_rows

        def _counting(self_):
            calls.append(1)
            return original(self_)

        with mock.patch.object(LedgerStore, "_read_rows", _counting):
            eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
            eng.recall("x")
        self.assertEqual(calls, [])

    def test_recall_passes_context_and_returns_index(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        eng.imprint(turn, [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                            "evidence": {"source": "user", "span": "猫が好きです p/"}}])
        plain = eng.recall("今日は何をする？")
        self.assertEqual(plain["seeds"], [])
        self.assertEqual([s["content"] for s in plain["index"]], ["猫が好き"])
        self.assertIn("【記録の索引 (全 1 件)】", plain["narrative"])
        with_ctx = eng.recall("今日は何をする？", context=["さっき猫の話をした"])
        self.assertEqual([s["content"] for s in with_ctx["seeds"]], ["猫が好き"])
        self.assertIn("【この入力に触れた記録】", with_ctx["narrative"])

    def test_recall_survives_non_dict_evidences_and_falls_back_to_content(self):
        # 壊れた evidence (非 dict) を持つ生きた圓成實の所在種子でも recall は落ちず、
        # 逐語 span が取れないので「要約:」無しの content 表示になる (manas._latest_span のガード)。
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        state = os.path.join(self.tmpdir, "broken_evidence_state")
        seed = {"id": 1, "content": "設定ファイルの場所", "strength": 2, "conditions": ["設定"],
                "polarity": "無記", "sansho": "圓成實", "kind": "所在", "evidences": ["x", 3],
                "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}
        write_state(state, {"seeds": [seed], "alaya_meta": {"life": 1}})
        eng = MemoryEngine(state_dir=state, new_life=False)
        recalled = eng.recall("設定について")
        self.assertIn("- 所在: 設定ファイルの場所(強度2)", recalled["narrative"])
        self.assertNotIn("要約:", recalled["narrative"])

    def test_recall_skips_non_dict_evidences_and_uses_older_dict_span(self):
        # 末尾が非 dict でも、手前の dict の span を使う (「要約:」行が出る)。
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        state = os.path.join(self.tmpdir, "mixed_evidence_state")
        seed = {"id": 1, "content": "設定ファイルの場所", "strength": 2, "conditions": ["設定"],
                "polarity": "無記", "sansho": "圓成實", "kind": "所在",
                "evidences": [{"span": "設定は ~/.x"}, 3],
                "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}
        write_state(state, {"seeds": [seed], "alaya_meta": {"life": 1}})
        eng = MemoryEngine(state_dir=state, new_life=False)
        recalled = eng.recall("設定について")
        self.assertIn("設定は ~/.x", recalled["narrative"])
        self.assertIn("要約:", recalled["narrative"])

    def test_recall_ungerminated_disposition_etaki_is_absent_from_index_and_narrative(self):
        # 性向の依他起は従来どおり索引に載らず、発芽もしない (強度 1 < 閾値 2)。
        eng = MemoryEngine(state_dir=self.tmpdir)
        turn = sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定")
        eng.imprint(turn, [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記", "kind": "性向",
                            "evidence": {"source": "assistant", "span": "アシスタントの推定です"}}])
        self.assertEqual(eng.alaya.list_seeds()[0]["sansho"], "依他起")
        recalled = eng.recall("推定について")
        self.assertEqual(recalled["index"], [])
        self.assertEqual(recalled["seeds"], [])
        self.assertEqual(recalled["narrative"], "")
        self.assertEqual(eng.audit()["class_counts"]["依他起"], 1)
        eng.recall("推定について")
        eng.recall("別の話題")
        self.assertEqual(eng.audit()["class_counts"]["依他起"], 1)

    def test_recall_ungerminated_decision_kind_etaki_is_in_index_with_suitei_label(self):
        # 改修5 §12.5: 決定系 kind (所在) の依他起は発芽前 (強度 1) でも索引に「(推定)」付きで載る。
        eng = MemoryEngine(state_dir=self.tmpdir)
        turn = sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定")
        eng.imprint(turn, [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記", "kind": "所在",
                            "evidence": {"source": "assistant", "span": "アシスタントの推定です"}}])
        recalled = eng.recall("推定について")
        self.assertEqual(recalled["seeds"], [])  # 強度 1 < 閾値 2 は発芽しない
        self.assertEqual([s["sansho"] for s in recalled["index"]], ["依他起"])
        self.assertIn("- 所在(推定): アシスタントの推定 (強度1)", recalled["narrative"])
        self.assertNotIn("【この入力に触れた記録】", recalled["narrative"])

    def test_recall_germinated_etaki_appears_in_narrative_not_index(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        window = [
            sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定"),
            sansho.Turn(user_text="再確認 推定", assistant_text="同じ推定の再確認です 推定"),
        ]
        eng.imprint(window, [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                              "evidence": [{"source": "assistant", "span": "アシスタントの推定です"},
                                           {"source": "assistant", "span": "同じ推定の再確認です"}]}])
        seed = eng.alaya.list_seeds()[0]
        self.assertEqual(seed["sansho"], "依他起")
        self.assertEqual(seed["strength"], 2)
        recalled = eng.recall("推定について")
        self.assertEqual(len(recalled["seeds"]), 1)
        # 改修5 §12.5: 決定系 kind の依他起は索引にも載りうるが、発芽した種子は ① に出るので外側の ③ では
        # 繰り返さない (layered-memory 計画 §2.3 の 発芽 の門)。
        self.assertEqual(recalled["index"], [])
        self.assertIn("- 所在(推定): アシスタントの推定(強度2)", recalled["narrative"])
        self.assertNotIn("- 所在(推定): アシスタントの推定 (強度2)", recalled["narrative"])
        self.assertNotIn("【記録の索引", recalled["narrative"])

    def test_recall_germinated_etaki_is_in_narrative_while_index_lists_only_enjojitsu(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text=""),
                    [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                      "evidence": {"source": "user", "span": "猫が好きです p/"}}])
        window = [
            sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定"),
            sansho.Turn(user_text="再確認 推定", assistant_text="同じ推定の再確認です 推定"),
        ]
        eng.imprint(window, [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                              "evidence": [{"source": "assistant", "span": "アシスタントの推定です"},
                                           {"source": "assistant", "span": "同じ推定の再確認です"}]}])
        recalled = eng.recall("推定について")
        self.assertIn("(推定)", recalled["narrative"])
        # 改修5 §12.5: 所在 の索引は 圓成實 → 依他起 の順に載りうるが、発芽した 依他起 は ① に出るので ③ は 圓成實 だけ。
        self.assertIn("【記録の索引 (全 1 件)】", recalled["narrative"])
        self.assertEqual([s["sansho"] for s in recalled["index"]], ["圓成實"])
        self.assertIn("- 所在: 猫が好きです p/ (強度1・要約: 猫が好き)", recalled["narrative"])

    def _imprint_mixed_decision(self, eng):
        window = [sansho.Turn(user_text="方針は A ではなく B にする", assistant_text="B で進めます")]
        eng.imprint(window, [{"content": "A ではなく B を採用", "conditions": ["方針"], "polarity": "善", "kind": "決定",
                              "evidence": [{"source": "user", "span": "A ではなく B にする"},
                                           {"source": "assistant", "span": "B で進めます"}]}])

    def test_recall_index_line_of_mixed_decision_renders_user_span_g2(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        self._imprint_mixed_decision(eng)
        recalled = eng.recall("全く別の話題です")  # 縁を共有しない: 発芽せず ③ に載る
        self.assertEqual(recalled["seeds"], [])
        self.assertEqual(len(recalled["index"]), 1)
        self.assertIn("- 決定: A ではなく B にする (強度", recalled["narrative"])
        self.assertIn("・要約: A ではなく B を採用・一部推定)", recalled["narrative"])
        self.assertNotIn("(推定)", recalled["narrative"])
        self.assertEqual(eng.explain_recall("全く別の話題です")["narrative"], recalled["narrative"])

    def test_recall_place_only_rendering_matches_explain_recall_h(self):
        with open(os.path.join(self.tmpdir, "bar.py"), "w") as f:  # 実在する path (無いと休眠して描かれない)
            f.write("")
        seed = {"id": 1, "content": self.tmpdir + "/bar.py は見つからなかった", "strength": 1, "conditions": ["配置"],
                "polarity": "善", "sansho": "依他起", "kind": "所在",
                "evidences": [{"source": "assistant", "span": "見つからなかった"}],
                "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}
        state = os.path.join(self.tmpdir, "place_only")
        write_state(state, {"seeds": [seed], "alaya_meta": {"life": 1}})
        eng = MemoryEngine(state_dir=state, new_life=False)
        for prompt in ("全く別の話題です", "配置"):
            recalled = eng.recall(prompt)
            self.assertIn("- 所在(推定): " + self.tmpdir + "/bar.py", recalled["narrative"])
            self.assertNotIn("見つからなかった", recalled["narrative"])
            self.assertEqual(eng.explain_recall(prompt)["narrative"], recalled["narrative"])

    def test_recall_dormant_place_only_seed_renders_nothing_and_matches_explain_h(self):
        seed = {"id": 1, "content": self.tmpdir + "/absent_zz.py は見つからなかった", "strength": 1, "conditions": ["配置"],
                "polarity": "善", "sansho": "依他起", "kind": "所在",
                "evidences": [{"source": "assistant", "span": "見つからなかった"}],
                "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0}
        state = os.path.join(self.tmpdir, "place_dormant")
        write_state(state, {"seeds": [seed], "alaya_meta": {"life": 1}})
        eng = MemoryEngine(state_dir=state, new_life=False)
        for prompt in ("全く別の話題です", "配置"):
            recalled = eng.recall(prompt)
            self.assertNotIn("absent_zz", recalled["narrative"])
            self.assertNotIn("見つからなかった", recalled["narrative"])
            self.assertEqual(eng.explain_recall(prompt)["narrative"], recalled["narrative"])

    def _imprint_multiline_decision(self, eng):
        text = "ビルドは make ではなく\n\n\njust を使うと決めました。"
        eng.imprint([sansho.Turn(user_text=text, assistant_text="")],
                    [{"content": "make ではなく\njust を採用", "conditions": ["ビルド"], "polarity": "善", "kind": "決定",
                      "evidence": [{"source": "user", "span": text}]}])
        return text

    def test_recall_multiline_user_span_renders_on_one_line_in_index_and_germinated_109(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        text = self._imprint_multiline_decision(eng)
        before = eng.inspect_seeds()
        self.assertEqual(before[0]["evidences"][0]["span"], text)
        for prompt, zone in (("全く別の話題です", "index"), ("ビルド", "seeds")):
            recalled = eng.recall(prompt)
            self.assertTrue(recalled[zone], zone)
            lines = [ln for ln in recalled["narrative"].split("\n") if ln.startswith("- 決定")]
            self.assertEqual(len(lines), 1, recalled["narrative"])
            self.assertIn("ビルドは make ではなく just を使うと決めました。", lines[0])
            self.assertIn("要約: make ではなく just を採用", lines[0])
            self.assertEqual(eng.explain_recall(prompt)["narrative"], recalled["narrative"])
        after = eng.inspect_seeds()
        self.assertEqual([s["evidences"] for s in after], [s["evidences"] for s in before])
        self.assertEqual(after[0]["evidences"][0]["span"], text)  # 保存は逐語のまま

    def test_recall_index_lines_with_long_user_spans_are_cut_within_budget(self):
        seeds = []
        for i in range(1, 11):
            span = "長い逐語%d" % i + "あ" * 400
            seeds.append({"id": i, "content": "要約%d" % i, "strength": 2, "conditions": ["縁%d" % i], "polarity": "善",
                          "sansho": "圓成實", "kind": "決定", "evidences": [{"source": "user", "span": span}],
                          "history": [], "planted_at": 1, "last_fired": None, "fire_count": 0, "miss_streak": 0})
        state = os.path.join(self.tmpdir, "long_spans")
        write_state(state, {"seeds": seeds, "alaya_meta": {"life": 1}})
        eng = MemoryEngine(state_dir=state, new_life=False)
        recalled = eng.recall("全く別の話題です")
        self.assertLessEqual(len(recalled["narrative"]), manas.INJECTION_MAX_CHARS)
        self.assertGreater(recalled["circles"]["cut"]["index"], 0)
        self.assertEqual(eng.explain_recall("全く別の話題です")["narrative"], recalled["narrative"])

    def test_recall_index_header_counts_injected_classes_only(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text=""),
                    [{"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                      "evidence": {"source": "user", "span": "猫が好きです p/"}}])
        eng.imprint(sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定"),
                    [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                      "evidence": {"source": "assistant", "span": "アシスタントの推定です"}}])
        eng.imprint(sansho.Turn(user_text="教えて 散歩", assistant_text="犬には散歩が必要です 散歩"),
                    [{"content": "犬には散歩が要る", "conditions": ["散歩"], "polarity": "無記","kind": "所在",
                      "evidence": {"source": "assistant", "span": "犬には散歩が必要です"}}])
        # 性向 (圓成實) は既定で索引に載らない (改修5 §7・§11.2)。
        eng.imprint(sansho.Turn(user_text="日本語でお願いします p/ 日本語", assistant_text=""),
                    [{"content": "回答は日本語でしてほしい", "conditions": ["日本語"], "polarity": "無記", "kind": "性向",
                      "evidence": {"source": "user", "span": "日本語でお願いします p/"}}])
        audit = eng.audit()
        self.assertEqual(audit["class_counts"]["圓成實"], 2)
        self.assertEqual(audit["class_counts"]["依他起"], 2)
        recalled = eng.recall("今日は何をする？")
        self.assertEqual([s["sansho"] for s in recalled["index"]], ["圓成實", "依他起", "依他起"])
        self.assertIn("【記録の索引 (全 3 件)】", recalled["narrative"])  # 性向は数えない
        self.assertNotIn("日本語", recalled["narrative"])
        # 見出しの文面には 性向 が語として出る (#100) ので、見出しを除いた本文に 性向 が無いことを見る
        self.assertNotIn("性向", recalled["narrative"].replace(manas.NARRATIVE_HEADER, "", 1))

    def test_recall_index_header_truncates_with_injected_class_total(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        for i in range(INDEX_MAX + 2):
            span = "U{:02d} C{:02d} p/".format(i, i)  # #149: 所在 の形
            cond = "C{:02d}".format(i)
            eng.imprint(sansho.Turn(user_text="{} {}".format(span, cond), assistant_text=""),
                        [{"content": "S{:02d}".format(i), "conditions": [cond], "polarity": "無記","kind": "所在",
                          "evidence": {"source": "user", "span": span}}])
        eng.imprint(sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定"),
                    [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                      "evidence": {"source": "assistant", "span": "アシスタントの推定です"}}])
        eng.imprint(sansho.Turn(user_text="教えて 散歩", assistant_text="犬には散歩が必要です 散歩"),
                    [{"content": "犬には散歩が要る", "conditions": ["散歩"], "polarity": "無記","kind": "所在",
                      "evidence": {"source": "assistant", "span": "犬には散歩が必要です"}}])
        self.assertEqual(eng.audit()["class_counts"]["圓成實"], INDEX_MAX + 2)
        self.assertEqual(eng.audit()["class_counts"]["依他起"], 2)
        recalled = eng.recall("今日は何をする？")
        # 改修5 §6.3: 索引は kind ごとの上限 (所在 10)。全 M 件は kind 別の許可クラス込みの該当件数。
        limit = INDEX_KIND_LIMITS["所在"]
        self.assertEqual(len(recalled["index"]), limit)
        self.assertTrue(all(s.get("sansho") == "圓成實" for s in recalled["index"]))  # 圓成實 が先に埋める
        self.assertIn("【記録の索引 (上位 {} 件 / 全 {} 件。載っていない 24 件も記録に在る)】".format(limit, INDEX_MAX + 4), recalled["narrative"])

    def test_consolidate_prompt_lists_etaki_in_existing_index(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        turn = sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定")
        eng.imprint(turn, [{"content": "アシスタントの推定", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                            "evidence": {"source": "assistant", "span": "アシスタントの推定です"}}])
        seed_id = eng.alaya.list_seeds()[0]["id"]
        eng.record_turn("雑談 推定", "雑談")
        with mock.patch("raiya.backend.call_llm", return_value="[]") as call:
            eng.consolidate()
        self.assertIn("#{} [所在] [依他起] アシスタントの推定".format(seed_id), call.call_args.args[0])

    def test_consolidate_reads_at_most_window_max_turns_and_leaves_the_rest_pending(self):
        # 窓に上限が無いと、backend が落ちている間 mark されずに溜まった未統合分が
        # 毎回まるごと再送され、プロンプトが膨らみ続けて二度と成功しなくなる。
        eng = MemoryEngine(state_dir=self.tmpdir)
        for i in range(engine.CONSOLIDATE_WINDOW_MAX + 2):
            eng.record_turn("発言{}".format(i), "応答{}".format(i))
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            result = eng.consolidate()
        self.assertEqual(result["window"], list(range(1, engine.CONSOLIDATE_WINDOW_MAX + 1)))
        self.assertEqual(eng.audit()["ledger"]["pending"], 2)  # 残りは消えずに次回へ
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            second = eng.consolidate()
        self.assertEqual(
            second["window"],
            [engine.CONSOLIDATE_WINDOW_MAX + 1, engine.CONSOLIDATE_WINDOW_MAX + 2],
        )
        self.assertEqual(eng.audit()["ledger"]["pending"], 0)

    def test_consolidate_backend_failure_leaves_window_pending_and_imprints_nothing(self):
        # 失敗経路: backend が落ちたら例外はそのまま呼び出し元 (hooks の fail-soft) へ
        # 抜け、窓は未統合のまま残り、種子も quarantine も1件も書かれない。
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("猫が好き", "")
        with mock.patch("raiya.backend.call_llm", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                eng.consolidate()
        audit = eng.audit()
        self.assertEqual(audit["ledger"], {"turns": 1, "consolidated_upto": 0, "pending": 1})
        self.assertEqual(audit["seed_count"], 0)

    def test_consolidate_prompt_truncates_long_turn_bodies_and_marker_cannot_ground(self):
        # 長い往復はプロンプト上でだけ切り詰める (先頭+末尾+`<elided N chars>`・処方 8)。三性
        # ゲートは台帳の**全文**を見るので、LLM が切り詰めマーカーごと引用してきた span も、
        # 切れ目を跨いで先頭の末尾と末尾の先頭を繋いだ span も逐語一致せず遍計所執に落ちる
        # (= 切り詰めが捏造の抜け道にならない)。先頭・中間・末尾は別の字にする: 一様な
        # 文字列だと「繋いだ span」が全文の部分文字列になってしまい、跨ぎを検出できない。
        eng = MemoryEngine(state_dir=self.tmpdir)
        limit = engine.CONSOLIDATE_TURN_CHARS
        # #149: 所在 の形に 區切りの語 (`/` を含む語) を要るので、先頭の保持部 (最初の limit//2 字) と中間 (プロンプトでは隠れる部分) に p/ を置く
        long_text = "前" * 10 + " p/ " + "前" * limit + "閑" * 25 + " p/ " + "閑" * 25 + "後" * limit
        eng.record_turn(long_text, "")
        marker = "<elided {} chars>".format(len(long_text) - limit)
        raw = json.dumps([
            {"content": "印ごと", "conditions": ["前"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前" * 10 + "\n" + marker + "\n" + "後" * 10}]},
            {"content": "跨ぎ", "conditions": ["前"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前" * 10 + "後" * 10}]},
            {"content": "先頭だけ", "conditions": ["前"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前" * 10 + " p/"}]},
            # プロンプトの切り詰めで隠れた中間は台帳の全文に残る。ゲートは台帳を見るので圓成實。
            {"content": "中間", "conditions": ["閑"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "閑" * 10 + " p/"}]},
        ], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw) as call:
            result = eng.consolidate()
        prompt = call.call_args.args[0]
        self.assertIn(long_text[: limit // 2] + "\n" + marker + "\n" + "後" * (limit - limit // 2), prompt)
        self.assertNotIn("閑", prompt)
        self.assertEqual(
            [d["decision"] for d in result["decisions"]],
            ["遍計所執", "遍計所執", "圓成實", "圓成實"],
        )

    def test_recall_rejects_str_context(self):
        # 文脈窓は「文字列の列」。str をそのまま渡すと 1 文字ずつの縁照合に化けるため、
        # alaya.germinate の型ガードに TypeError を出させる (engine が list() で
        # 包んでしまうとガードをすり抜ける)。
        eng = MemoryEngine(state_dir=self.tmpdir)
        with self.assertRaises(TypeError):
            eng.recall("x", context="猫犬")

    def test_consolidate_with_empty_window_loads_no_backend_in_a_fresh_process(self):
        # 「空窓なら backend を読み込まない」の唯一の pin。このプロセス内では張れない:
        # このテストモジュールは冒頭で `from raiya import backend` しているため、
        # sys.modules から外しても raiya パッケージの属性 `backend` は残り、遅延
        # `from . import backend` が sys.modules を経由せずに解決してしまう
        # (= 空窓で import されても検出できない)。よって別プロセスで確かめる
        # (test_mcp_server の同型 pin と同じ手口)。
        code = (
            "import sys\n"
            "from raiya.engine import MemoryEngine\n"
            "eng = MemoryEngine(state_dir={!r}, new_life=False)\n"
            "assert eng.consolidate()['skipped'] == 'empty'\n"
            "print('raiya.backend' in sys.modules)\n"
        ).format(self.tmpdir)
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class TestAuditSumInvariants(unittest.TestCase):
    """#94 (K2): audit() の内訳の和が総数と合う。seed_count == Σ class == Σ kind == Σ project、
    台帳は turns == (id <= consolidated_upto の行数) + pending。audit に鍵は足さない。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        patcher = mock.patch.dict(os.environ, {"RAIYA_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_sums(self, eng):
        audit = eng.audit()
        n = audit["seed_count"]
        self.assertEqual(sum(audit["class_counts"].values()), n)
        self.assertEqual(sum(audit["kind_counts"].values()), n)
        self.assertEqual(sum(audit["project_counts"].values()), n)
        led = audit["ledger"]
        rows = eng.ledger.tail(led["turns"] + 10)
        self.assertEqual(len(rows), led["turns"])
        consolidated = sum(1 for r in rows if r["id"] <= led["consolidated_upto"])
        self.assertEqual(led["turns"], consolidated + led["pending"])
        return audit

    def test_empty_state_sums_to_zero(self):
        audit = self._assert_sums(MemoryEngine(state_dir=self.tmpdir))
        self.assertEqual(audit["seed_count"], 0)

    def test_sums_hold_with_mixed_class_kind_project_and_partial_consolidation(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("コマンドを表示して", "了解", project="/p/a")
        eng.record_turn("stdio専用は変な仕様", "HTTPにも対応できます", project="/p/b")
        eng.record_turn("三つ目の話", "はい")
        raw = json.dumps([
            {"content": "コマンドは表示する", "conditions": ["コマンド", "表示"], "polarity": "無記",
             "kind": "所在", "evidence": [{"source": "user", "span": "コマンドを表示して"}]},
            {"content": "HTTP対応", "conditions": ["HTTP"], "polarity": "無記", "kind": "決定",
             "evidence": [{"source": "assistant", "span": "HTTPにも対応できます"}]},
        ], ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw):
            eng.consolidate()
        turn = sansho.Turn(user_text="私は猫が好きです 猫", assistant_text="")
        eng.imprint(turn, [{"content": "猫好き", "conditions": ["猫"], "polarity": "善", "kind": "性向",
                            "evidence": [{"source": "user", "span": "私は猫が好きです"}]}], project="/p/a")
        eng.record_turn("四つ目", "はい")  # consolidate 後に pending を足す
        audit = self._assert_sums(eng)
        self.assertGreaterEqual(audit["seed_count"], 2)
        self.assertGreaterEqual(audit["ledger"]["pending"], 1)
        self.assertGreaterEqual(audit["ledger"]["consolidated_upto"], 1)

    def test_sums_hold_with_old_form_superseded_and_withdrawn_seeds(self):
        def seed(i, **extra):
            d = {"id": i, "content": "種子{}".format(i), "strength": 1, "conditions": ["c{}".format(i)],
                 "polarity": "無記", "evidences": [], "history": [], "planted_at": 1,
                 "last_fired": None, "fire_count": 0, "miss_streak": 0}
            d.update(extra)
            return d
        old = seed(1)  # 旧形式: kind / project / sansho 無し
        sup = seed(2, sansho="依他起", kind="決定", project="/p/a", superseded_by=1)
        wd = seed(3, sansho="圓成實", kind="所在", project="/p/b",
                  withdrawn={"by": "h", "at": 1, "reason": "r", "life": 1})
        state = os.path.join(self.tmpdir, "old_form_state")
        write_state(state, {"seeds": [old, sup, wd], "alaya_meta": {"life": 1}})
        audit = self._assert_sums(MemoryEngine(state_dir=state, new_life=False))
        self.assertEqual(audit["seed_count"], 3)
        self.assertEqual(audit["superseded_count"], 1)
        self.assertEqual(audit["withdrawn_count"], 1)
        self.assertEqual(audit["kind_counts"]["性向"], 1)
        self.assertEqual(audit["project_counts"]["(全域)"], 1)

    def test_audit_keys_are_unchanged(self):
        self.assertEqual(
            set(MemoryEngine(state_dir=self.tmpdir).audit()),
            {"life", "seed_count", "class_counts", "kind_counts", "project_counts", "superseded_count",
             "withdrawn_count", "quarantine_retained", "quarantine_total", "ledger", "basis_counts",
             "log_check", "ledger_warning", "history_total", "imprint_log_tail", "imprint_log_warning",
             "alaya_warning", "manas_warning"},
        )


class TestGroundingAndElisionAtTheGate(unittest.TestCase):
    """設計書 §10.6 処方 5/6/8: 台帳は記録 (貼り付け込み) と根拠 (貼り付け抜き) を分け、
    統合の三性ゲートとプロンプトは根拠だけを user 発話として読む。classify_sansho は不変。"""

    PASTE = '<pasted_content id="ff48">\n中断前に再開ポイントを保存しました\n</pasted_content id="ff48">'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _consolidate(self, proposals):
        raw = json.dumps(proposals, ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw) as call:
            result = self.eng.consolidate()
        return result, call.call_args.args[0]

    @staticmethod
    def _proposal(span, condition, source="user"):
        return {"content": "性向", "conditions": [condition], "polarity": "無記","kind": "所在",
                "evidence": [{"source": source, "span": span}]}

    def test_record_turn_keeps_paste_in_record_and_strips_it_from_grounding(self):
        self.eng.record_turn("再開して\n\n" + self.PASTE, "再開しました")
        row = self.eng.ledger.tail(1)[0]
        self.assertEqual(row["user_text"], "再開して\n\n" + self.PASTE)
        self.assertEqual(row["grounding_text"], "再開して")

    def test_record_turn_strips_private_from_record_and_grounding_on_both_sides(self):
        self.eng.record_turn("猫が好き <private>住所は東京</private>", "東京ですね <private>東京</private>了解")
        row = self.eng.ledger.tail(1)[0]
        self.assertNotIn("東京", row["user_text"])
        self.assertNotIn("東京", row["grounding_text"])
        self.assertEqual(row["assistant_text"], "東京ですね 了解")  # 閉じた塊だけを除く

    def test_span_only_in_paste_is_quarantined_and_typed_span_is_grounded(self):
        self.eng.record_turn("再開して p/\n\n" + self.PASTE, "了解")
        result, prompt = self._consolidate([
            self._proposal("中断前に再開ポイントを保存しました", "再開"),
            self._proposal("再開して p/", "再開"),
        ])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執", "圓成實"])
        self.assertNotIn("中断前に再開ポイント", prompt)  # 貼り付けは LLM にも見せない
        self.assertEqual(self.eng.audit()["quarantine_total"], 1)
        # evidence は台帳の行を指す (差し替えた本文は保存しない)。
        seed = self.eng.alaya.list_seeds()[0]
        self.assertEqual(seed["evidences"][0]["ledger_id"], 1)
        self.assertNotIn("turn", seed["evidences"][0])

    def test_paste_only_turn_grounds_nothing_instead_of_falling_back_to_user_text(self):
        # 根拠 "" は「全部が貼り付け」= 根拠なし。`grounding_text or user_text` と書くと
        # ここで user_text (貼り付け込み) に戻ってしまい、貼り付けが圓成實になる。
        self.eng.record_turn(self.PASTE, "了解")
        result, _ = self._consolidate([self._proposal("中断前に再開ポイントを保存しました", "再開")])
        self.assertEqual(result["decisions"][0]["decision"], "遍計所執")

    def test_old_format_row_without_grounding_field_consolidates_on_user_text(self):
        add_ledger_body(self.tmpdir, json.dumps({"id": 1, "life": 1, "session_id": None, "ts": 0,
                                                 "user_text": "私は猫が好きです p/", "assistant_text": ""}, ensure_ascii=False), 1)
        set_meta(self.tmpdir, "ledger.next_id", 2)
        result, prompt = self._consolidate([self._proposal("私は猫が好きです p/", "猫")])
        self.assertEqual(result["decisions"][0]["decision"], "圓成實")
        self.assertIn("私は猫が好きです", prompt)

    def test_corrupt_grounding_field_grounds_nothing_instead_of_raw_user_text(self):
        # 欄は在るが型が違う (壊れた) 行は "" = 根拠なし (閉じる側)。生の user_text へ
        # 戻すと、そこに残っている貼り付けが圓成實になる。
        add_ledger_body(self.tmpdir, json.dumps({"id": 1, "life": 1, "session_id": None, "ts": 0,
                                                 "user_text": "再開して\n\n" + self.PASTE, "grounding_text": 0,
                                                 "assistant_text": ""}, ensure_ascii=False), 1)
        set_meta(self.tmpdir, "ledger.next_id", 2)
        result, _ = self._consolidate([self._proposal("中断前に再開ポイントを保存しました", "再開")])
        self.assertEqual(result["decisions"][0]["decision"], "遍計所執")

    def test_ledger_elision_marker_and_straddling_span_are_not_grounded(self):
        # hook は台帳に先頭+印+末尾を記録する (TRUNCATE_LIMIT)。その印はプロンプトに見えるが、
        # ゲートでは GATE_CUT に置き換わる = 印ごとの引用・印を除いて先頭と末尾を繋いだ
        # 引用・印だけの引用はどれも接地しない (user 側も assistant 側も)。
        self._record_elided_turn()
        marker = "<elided 10 chars>"
        result, prompt = self._consolidate([
            self._proposal("前前\n" + marker + "\n後後", "前"),
            self._proposal("前前後後", "前"),
            self._proposal(marker, "前"),
            self._proposal("甲甲\n" + marker + "\n乙乙", "甲", source="assistant"),
            self._proposal("甲甲乙乙", "甲", source="assistant"),
        ])
        self.assertIn(marker, prompt)
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執"] * 5)

    def test_ledger_elided_turn_still_grounds_spans_within_head_or_tail(self):
        self._record_elided_turn()
        result, _ = self._consolidate([
            self._proposal("前前前 p/", "前"),
            self._proposal("後後後 p/", "後"),
            self._proposal("乙乙乙", "乙", source="assistant"),
        ])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["圓成實", "圓成實", "依他起"])

    def test_span_containing_the_gate_cut_char_is_quarantined_not_grounded(self):
        # 区切り (U+FFFC) は LLM に見せないが、span が自分でその字を含めば区切りを跨げて
        # しまう。その提案は U+FFFC を除いた窓で判定され、通常の遍計所執 (quarantine +
        # imprint_log に元の提案のまま記帳) に落ちる = 黙って捨てない・通さない。
        self._record_elided_turn()
        result, _ = self._consolidate([
            self._proposal("前前\ufffc後後", "前"),
            self._proposal("前前\n\ufffc\n後後", "前"),
            self._proposal("甲甲\n\ufffc\n乙乙", "甲", source="assistant"),
        ])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執"] * 3)
        audit = self.eng.audit()
        self.assertEqual(audit["quarantine_total"], 3)
        self.assertEqual(audit["seed_count"], 0)
        self.assertEqual(
            [e["proposal"]["evidence"][0]["span"] for e in audit["imprint_log_tail"]],
            ["前前\ufffc後後", "前前\n\ufffc\n後後", "甲甲\n\ufffc\n乙乙"],
        )
        # 本文を空にした窓で判定するので縁も接地しない (quarantine の縁は空)。
        self.assertEqual(self.eng.alaya.list_quarantine()[0]["conditions"], [])

    def test_gate_cut_char_in_span_or_any_condition_makes_the_whole_proposal_henge(self):
        # 区切りの字 (U+FFFC) が span か縁のどれかに在れば、提案全体を本文を空にした窓で
        # 判定する = 他の span・縁が正当でも必ず遍計所執 (reject にも圓成實にもならない)。
        self._record_elided_turn()
        result, _ = self._consolidate([
            {"content": "縁だけ区切り", "conditions": ["前\ufffc後"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前前前"}]},
            {"content": "縁の 1 つが区切り", "conditions": ["前", "\ufffc"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前前前"}]},
            {"content": "span の 1 つが区切り", "conditions": ["前"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前前前"}, {"source": "user", "span": "前\ufffc後"}]},
            {"content": "区切り無し", "conditions": ["前"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "前前前 p/"}]},
        ])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執"] * 3 + ["圓成實"])
        self.assertEqual(self.eng.audit()["quarantine_total"], 3)

    def test_dummy_gate_cut_condition_cannot_steer_grounding_across_users_own_cut_char(self):
        # QA 実測の穴: 本人の本文 `私は猫<U+FFFC>が好き` に対し span `私は猫が好き` は遍計所執。
        # 旧実装は U+FFFC の縁を 1 つ足すと本文の U+FFFC を消した窓へ切り替わり、同じ span が
        # 圓成實になった (LLM が判定の窓を選べた)。今はどちらも遍計所執。
        self.eng.record_turn("私は猫\ufffcが好き", "はい")
        result, _ = self._consolidate([
            self._proposal("私は猫が好き", "猫"),
            {"content": "性向", "conditions": ["猫", "\ufffc"], "polarity": "無記","kind": "所在",
             "evidence": [{"source": "user", "span": "私は猫が好き"}]},
        ])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執", "遍計所執"])

    def test_gate_cut_char_in_users_own_text_still_cannot_be_quoted(self):
        # 本人の本文に U+FFFC が元から在っても、それを含む span は閉じる側に倒す (既知の
        # 取りこぼし: 本当に逐語の引用でも圓成實にはならない)。含まない span は通常どおり。
        self.eng.record_turn("画像\ufffcの説明です p/", "了解")
        result, _ = self._consolidate([self._proposal("画像\ufffcの説明", "画像"),
                                       self._proposal("の説明です p/", "説明")])
        self.assertEqual([d["decision"] for d in result["decisions"]], ["遍計所執", "圓成實"])

    def test_record_turn_uses_caller_grounding_text_after_stripping_private(self):
        self.eng.record_turn("記録 <private>秘</private>", "応答", grounding_text="根拠 <private>秘</private>")
        row = self.eng.ledger.tail(1)[0]
        self.assertEqual((row["user_text"], row["grounding_text"]), ("記録", "根拠"))

    def _record_elided_turn(self):
        # #149: 所在 の形 (`/` を含む語) を先頭と末尾の保持部に置く。全長 70・限度 60 は不変。
        user, _ = ledger.elide_middle("前前前 p/ " + "前" * 23 + "閑" * 10 + "後" * 23 + " 後後後 p/", 60)
        assistant, _ = ledger.elide_middle("甲" * 30 + "閑" * 10 + "乙" * 30, 60)
        self.eng.record_turn(user, assistant)


TS = b"2026-10-20T00:57:59.319190+00:00 "  # hooks._log が行頭に書く ISO 時刻


class TestSevenTextsObservation(unittest.TestCase):
    """七文書による検証層 設計書 §2.3 (同依: basis_counts / window_sessions) と
    §3.3 (四大教法: log_check) の pin。どれも audit / consolidate の _locked の中で読む。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir, new_life=False)
        self.log_path = os.path.join(self.tmpdir, "hooks.log")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _record(self, n, session_id, project):
        for i in range(n):
            self.eng.record_turn("発話 {}".format(i), "応答", session_id=session_id, project=project)

    def _write_log(self, data):
        with open(self.log_path, "wb") as f:
            f.write(data)

    # --- basis_counts ---
    def test_basis_counts_two_sessions_by_two_projects(self):
        # 統合済みの行は数えない (未統合の行だけ)。
        self._record(1, "s1", "/p/a")
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            self.eng.consolidate()
        # 件数を全て違えて並び順を pin する (同数 1/1 は project の文字列順)。
        self._record(1, "s1", "/p/b")
        self._record(2, "s2", "/p/b")
        self._record(3, "s1", "/p/a")
        self._record(1, "s2", "/p/a")
        audit = self.eng.audit()
        self.assertEqual(audit["basis_counts"], [
            {"project": "/p/a", "session_id": "s1", "pending": 3},
            {"project": "/p/b", "session_id": "s2", "pending": 2},
            {"project": "/p/a", "session_id": "s2", "pending": 1},
            {"project": "/p/b", "session_id": "s1", "pending": 1},
        ])
        self.assertEqual(audit["ledger"]["pending"], 7)
        self.assertEqual(sum(b["pending"] for b in audit["basis_counts"]), audit["ledger"]["pending"])

    def test_basis_counts_displays_none_and_breaks_ties_by_session(self):
        self._record(1, "s1", None)
        self._record(1, None, None)
        self._record(1, 7, 8)  # 非 str はどちらも None 扱い
        self.assertEqual(self.eng.audit()["basis_counts"], [
            {"project": "(全域)", "session_id": "(不明)", "pending": 2},
            {"project": "(全域)", "session_id": "s1", "pending": 1},
        ])

    def test_basis_counts_empty_ledger(self):
        self.assertEqual(self.eng.audit()["basis_counts"], [])

    def test_unconsolidated_bases_maps_non_str_to_none_at_ledger_level(self):
        # record_turn を通さず台帳に非 str の session_id / project を持つ行がある state を snapshot で作る
        # (record_turn の project 補正に頼らない。store の meta や jsonl を外から書き換えない)。
        state = os.path.join(self.tmpdir, "ledger_level_state")
        write_state(state, {
            "ledger_rows": [
                {"id": 1, "life": 0, "session_id": 7, "user_text": "u", "assistant_text": "a"},
                {"id": 2, "life": 0, "session_id": 8, "project": 9, "user_text": "u", "assistant_text": "a"},
            ],
        })
        self.eng = MemoryEngine(state_dir=state, new_life=False)
        self.assertEqual(self.eng.ledger.unconsolidated_bases(), [(None, None), (None, None)])
        turns, bases = self.eng.ledger.unconsolidated_with_bases()
        self.assertEqual([t.id for t in turns], [1, 2])
        self.assertEqual(bases, [(None, None), (None, None)])

    def test_basis_counts_empty_session_id_is_shown_as_is(self):
        # "" は str なので None ではない: "(不明)" に丸めない。
        self._record(1, "", "/p/a")
        self._record(1, None, "/p/a")
        self.assertEqual(self.eng.audit()["basis_counts"], [
            {"project": "/p/a", "session_id": "", "pending": 1},
            {"project": "/p/a", "session_id": "(不明)", "pending": 1},
        ])

    def test_unconsolidated_bases_matches_unconsolidated_rows_and_limit(self):
        self._record(2, "s1", "/p/a")
        self._record(1, None, None)
        ledger_store = self.eng.ledger
        self.assertEqual(ledger_store.unconsolidated_bases(), [("/p/a", "s1"), ("/p/a", "s1"), (None, None)])
        self.assertEqual(ledger_store.unconsolidated_bases(limit=2), [("/p/a", "s1"), ("/p/a", "s1")])
        self.assertEqual(
            [t.project for t in ledger_store.unconsolidated(limit=2)],
            [p for p, _ in ledger_store.unconsolidated_bases(limit=2)],
        )

    # --- log_check ---
    def test_log_check_match(self):
        self._record(2, "s1", None)
        # 最後の ledger_id= を読む (先頭の 5 を読む mutant は 不一致 になる)。
        self._write_log(TS + b"session=s1 ledger_id=5 pending=5\n" + TS + b"session=s1 ledger_id=2 pending=2\n")
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "一致", "log_ledger_id": 2, "ledger_last_id": 2})

    def test_log_check_ledger_ahead(self):
        self._record(3, "s1", None)
        self._write_log(TS + b"session=s1 ledger_id=2 pending=2\n")
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "台帳が先行", "log_ledger_id": 2, "ledger_last_id": 3})

    def test_log_check_log_claims_a_row_that_does_not_exist(self):
        self._record(1, "s1", None)
        self._write_log(TS + b"session=s1 ledger_id=4 pending=4\n")
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "不一致", "log_ledger_id": 4, "ledger_last_id": 1})

    def test_log_check_no_record(self):
        self._record(1, "s1", None)
        # hooks.log が無い
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "記録なし", "log_ledger_id": None, "ledger_last_id": 1})
        # ledger_id= の行が無い
        self._write_log(TS + b"session=s1 skip: assistant_text \xe3\x81\x8c\xe7\xa9\xba\n")
        self.assertEqual(self.eng.audit()["log_check"]["status"], "記録なし")
        # 読めない (hooks.log がディレクトリ): 例外にしない
        os.remove(self.log_path)
        os.mkdir(self.log_path)
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "記録なし", "log_ledger_id": None, "ledger_last_id": 1})

    def test_log_check_ignores_ledger_id_outside_the_stop_line_shape(self):
        """stop の記録行 (`<時刻> session=X ledger_id=N pending=M` で行末) だけを証言として読む。
        session id に ` ledger_id=999` を仕込んだ consolidate 行や skip 行は拾わない。"""
        self._record(2, "s1", None)
        self._write_log(
            TS + b"session=s1 ledger_id=2 pending=2\n"
            + TS + b"session=x ledger_id=999 consolidate window=5 sessions=1 proposals=0\n"
            + TS + b"session=x ledger_id=999 pending=1 skip: assistant_text\n"
            + b"ledger_id=999 pending=1\n"
        )
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "一致", "log_ledger_id": 2, "ledger_last_id": 2})

    def test_log_check_reads_a_real_stop_line_written_by_the_hook_logger(self):
        from raiya.hooks import claude_code as hooks
        self._record(1, "s1", None)
        hooks._log(self.tmpdir, "session={} ledger_id={} pending={}".format("s1", 1, 1))
        self.assertEqual(self.eng.audit()["log_check"]["status"], "一致")

    def test_log_check_reads_only_the_tail(self):
        self._record(1, "s1", None)
        filler = b"x" * 99 + b"\n"
        tail = engine.LOG_CHECK_TAIL_BYTES
        # (a) 末尾 64 KiB より前にしか ledger_id= が無い -> 読まない
        self._write_log(TS + b"session=s1 ledger_id=1 pending=1\n" + filler * (tail // len(filler) + 2))
        self.assertEqual(self.eng.audit()["log_check"]["status"], "記録なし")
        # (b) 行頭が欠けた最初の行は捨てる: 64 KiB を超える 1 行 (形は記録行そのもの) は、
        # 切り口からは行末だけが見えるので読まない
        self._write_log(TS + b"session=" + b"y" * (tail + 10) + b" ledger_id=1 pending=1\n")
        self.assertEqual(self.eng.audit()["log_check"]["status"], "記録なし")
        # (c) 切り詰めても、末尾の完全な行は読む
        self._write_log(filler * (tail // len(filler) + 2) + TS + b"session=s1 ledger_id=1 pending=1\n")
        self.assertEqual(self.eng.audit()["log_check"],
                         {"status": "一致", "log_ledger_id": 1, "ledger_last_id": 1})
        # (d) 切り口がちょうど行頭に当たった行は欠けていないので読む
        line = TS + b"session=s1 ledger_id=1 pending=1\n"
        self._write_log(b"z" * 50 + b"\n" + line + b"w" * (tail - len(line) - 1) + b"\n")
        self.assertEqual(self.eng.audit()["log_check"]["status"], "一致")

    # --- window_sessions ---
    def test_consolidate_reports_window_sessions(self):
        self._record(2, "s1", None)
        self._record(1, "s2", None)
        self._record(1, None, None)  # None も 1 種
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            result = self.eng.consolidate()
        self.assertIsNone(result["skipped"])
        self.assertEqual(result["window_sessions"], 3)

    def test_window_sessions_non_str_session_id_counts_as_none(self):
        self._record(1, 7, None)
        self._record(1, None, None)
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            result = self.eng.consolidate()
        self.assertEqual(result["window_sessions"], 1)

    def test_window_sessions_counts_only_the_window(self):
        self._record(engine.CONSOLIDATE_WINDOW_MAX, "s1", None)
        self._record(1, "s2", None)  # 窓の外 (次回の統合が拾う)
        with mock.patch("raiya.backend.call_llm", return_value="[]"):
            result = self.eng.consolidate()
        self.assertEqual(len(result["window"]), engine.CONSOLIDATE_WINDOW_MAX)
        self.assertEqual(result["window_sessions"], 1)

    def test_window_sessions_on_unparseable_and_already_consolidated(self):
        self._record(1, "s1", None)
        self._record(1, "s2", None)
        with mock.patch("raiya.backend.call_llm", return_value='[{"content": "x'):
            result = self.eng.consolidate()
        self.assertEqual((result["skipped"], result["window_sessions"]), ("unparseable", 2))

        def _race(prompt, **kwargs):
            other = MemoryEngine(state_dir=self.tmpdir, new_life=False)
            other.ledger.reload()
            other.ledger.mark_consolidated(2)
            other.ledger.commit()
            return "[]"

        with mock.patch("raiya.backend.call_llm", side_effect=_race):
            result = self.eng.consolidate()
        self.assertEqual((result["skipped"], result["window_sessions"]), ("already_consolidated", 2))


class TestImprintAppliesLedgerRulesToCallerTurns(unittest.TestCase):
    """#17 #19: engine.imprint (MCP/CLI) も台帳の往復と同じ規則 (private・貼り付け・切り詰めの印・
    U+FFFC) で三性ゲートに渡す。classify_sansho は不変。"""

    PASTE = '<pasted_content id="a1">\n中断前に再開ポイントを保存しました\n</pasted_content id="a1">'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _p(span, cond, source="user"):
        return {"content": "記録", "conditions": [cond], "polarity": "無記", "kind": "所在",
                "evidence": [{"source": source, "span": span}]}

    def _decisions(self, turn, proposals):
        return [d["decision"] for d in self.eng.imprint(turn, proposals)]

    def _raw(self, name):
        # 保存層は raiya.db 一つ。名前は無視して DB のバイト列全体 (種子・隔離・imprint_log を含む) を読む。
        with open(os.path.join(self.tmpdir, "raiya.db"), "rb") as f:
            return f.read().decode("utf-8", "replace")

    def test_span_only_in_paste_is_quarantined_and_typed_span_is_grounded(self):
        turn = sansho.Turn(user_text="再開して p/\n\n" + self.PASTE, assistant_text="")
        got = self._decisions(turn, [self._p("中断前に再開ポイントを保存しました", "再開"), self._p("再開して p/", "再開")])
        self.assertEqual(got, ["遍計所執", "圓成實"])
        self.assertEqual(len(self.eng.alaya.state["quarantine"]), 1)

    def test_mismatched_paste_ids_are_not_stripped(self):
        text = '再開 p/ <pasted_content id="a1">保存しました p/</pasted_content id="b2">'
        got = self._decisions(sansho.Turn(user_text=text, assistant_text=""), [self._p("保存しました p/", "保存")])
        self.assertEqual(got, ["圓成實"])

    def test_span_only_in_private_is_quarantined_on_both_sides(self):
        turn = sansho.Turn(user_text="猫が好き p/ <private>住所は東京です</private>",
                           assistant_text="了解 <private>合言葉は青空です</private>")
        got = self._decisions(turn, [
            self._p("住所は東京です", "猫"),
            self._p("合言葉は青空です", "了解", source="assistant"),
            self._p("猫が好き p/", "猫"),
        ])
        self.assertEqual(got, ["遍計所執", "遍計所執", "圓成實"])

    def test_condition_only_inside_a_block_is_not_grounded(self):
        turn = sansho.Turn(user_text="再開して\n\n" + self.PASTE + " <private>秘密語</private>", assistant_text="")
        got = self._decisions(turn, [self._p("再開して", "保存しました"), self._p("再開して", "秘密語")])
        self.assertEqual(got, ["reject", "reject"])

    def test_private_text_is_not_written_to_disk(self):
        turn = sansho.Turn(user_text="猫が好き <private>極秘の暗号</private>",
                           assistant_text="了解 <private>機密の応答</private>")
        self.eng.imprint(turn, [self._p("猫が好き", "猫")])
        self.eng.imprint(turn, [self._p("猫が好き", "猫"), self._p("存在しない文", "猫")])
        for name in ("raiya.db",):
            raw = self._raw(name)
            self.assertNotIn("極秘の暗号".encode("unicode_escape").decode(), raw)
            self.assertNotIn("極秘の暗号", raw)
            self.assertNotIn("機密の応答", raw)

    def test_elision_marker_cannot_be_quoted_or_bridged(self):
        turn = sansho.Turn(user_text="先頭の文 p/ <elided 12 chars> 末尾の文 p/", assistant_text="")
        got = self._decisions(turn, [
            self._p("<elided 12 chars>", "先頭"),
            self._p("先頭の文 p/ <elided 12 chars> 末尾の文 p/", "先頭"),
            self._p("先頭の文 p/ 末尾の文 p/", "先頭"),
            self._p("先頭の文 p/", "先頭"),
            self._p("末尾の文 p/", "末尾"),
        ])
        self.assertEqual(got, ["遍計所執", "遍計所執", "遍計所執", "圓成實", "圓成實"])

    def test_fffc_in_span_or_condition_quarantines_whole_proposal_only(self):
        for turn_text in ("猫が好き p/", "猫が好き p/\ufffc"):
            with self.subTest(turn_text=turn_text):
                turn = sansho.Turn(user_text=turn_text, assistant_text="")
                in_span = self._p("猫が好き p/\ufffc", "猫")
                in_cond = self._p("猫が好き p/", "猫\ufffc")
                before = len(self.eng.alaya.state["quarantine"])
                got = self._decisions(turn, [in_span, in_cond, self._p("猫が好き p/", "猫")])
                self.assertEqual(got, ["遍計所執", "遍計所執", "圓成實"])
                self.assertEqual(len(self.eng.alaya.state["quarantine"]), before + 2)
                logged = self.eng.alaya.imprint_log[-3:] if hasattr(self.eng.alaya, "imprint_log") else None
                if logged is not None:
                    self.assertEqual(logged[0]["proposal"], in_span)
                    self.assertEqual(logged[1]["proposal"], in_cond)

    def test_ledger_turn_grounding_text_is_the_evidence(self):
        empty = ledger.LedgerTurn(user_text="猫が好き", assistant_text="", grounding_text="")
        self.assertEqual(self._decisions(empty, [self._p("猫が好き", "猫")]), ["遍計所執"])
        other = ledger.LedgerTurn(user_text="犬が好き", assistant_text="", grounding_text="猫が好き p/ <private>x</private>")
        self.assertEqual(self._decisions(other, [self._p("猫が好き p/", "猫"), self._p("犬が好き", "犬")]),
                         ["圓成實", "遍計所執"])
        bad = ledger.LedgerTurn(user_text="猫が好き", assistant_text="", grounding_text=5)
        self.assertEqual(self._decisions(bad, [self._p("猫が好き", "猫")]), ["遍計所執"])


if __name__ == "__main__":
    unittest.main()
