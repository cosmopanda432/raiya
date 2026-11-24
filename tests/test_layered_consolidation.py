"""統合の入力 (層化設計書 B5・plan 2026-10-25-layered-memory-implementation §4・pin 14) の決定論 pin (スライス 3)。

`AlayaStore.consolidation_view`、`engine._build_consolidate_prompt(window, view)`、`consolidate(debug=)` の
`related_ids` / `group_rep_ids`。LLM は呼ばない (`raiya.backend.call_llm` を mock する)。
テストは自分で作った `tempfile.mkdtemp` だけを作り、それだけを消す。
"""
import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from raiya import alaya as alaya_mod  # noqa: E402
from raiya import engine as engine_mod  # noqa: E402
from raiya.engine import (  # noqa: E402
    CONSOLIDATE_GROUPS_MAX,
    CONSOLIDATE_RELATED_MAX,
    CONSOLIDATE_TURN_CHARS,
)
from test_layered_circles import P, Q, _seed, _StateCase  # noqa: E402

R = "/home/dev/r"


def _view(alaya, texts, projects=(P,), **kw):
    return alaya.consolidation_view(set(projects), texts, **kw)


class TestConsolidationView(_StateCase):
    def test_constants(self):
        self.assertEqual((CONSOLIDATE_RELATED_MAX, CONSOLIDATE_GROUPS_MAX), (30, 20))

    def test_related_uses_matched_condition_rule(self):
        eng = self.write([
            _seed(1, ["alpha", "beta"], kind="決定"),                 # 1 語で足りる
            _seed(2, ["gamma", "delta"], kind="性向"),                # 性向は 2 語
            _seed(3, ["gamma", "delta"], kind="性向", project=P),
            _seed(4, ["zzz"], kind="所在"),                           # 触れない
        ])
        v = _view(eng.alaya, ["alpha だけ gamma だけ"])
        self.assertEqual([s["id"] for s in v["related"]], [1])
        v = _view(eng.alaya, ["alpha gamma delta"])
        self.assertEqual([s["id"] for s in v["related"]], [1, 2, 3])
        self.assertEqual(v["related_total"], 3)

    def test_related_has_no_strength_class_twin_or_dormancy_gate(self):
        gone = "/nonexistent-raiya-dir/a/b"
        eng = self.write([
            _seed(1, ["alpha", "beta"], kind="所在", sansho="依他起", strength=0, content="場所 " + gone),
            _seed(2, ["alpha", "beta"], kind="所在", sansho="圓成實", strength=5),   # 1 の双子 E
            _seed(3, ["alpha"], kind="再開点"),                                      # 再開点も出る
            _seed(4, ["alpha"], kind="性向", sansho="依他起"),                         # 1 語のみ -> 出ない
        ])
        v = _view(eng.alaya, ["alpha"])
        self.assertEqual(sorted(s["id"] for s in v["related"]), [1, 2, 3])

    def test_related_excludes_superseded_withdrawn_and_other_project(self):
        eng = self.write([
            _seed(1, ["alpha"], superseded_by=9),
            _seed(2, ["alpha"], withdrawn={"by": "x", "at": 1, "reason": "r"}),
            _seed(3, ["alpha"], project=Q),
            _seed(4, ["alpha"], project=None),
            _seed(5, ["alpha"]),
        ])
        v = _view(eng.alaya, ["alpha"])
        self.assertEqual(sorted(s["id"] for s in v["related"]), [4, 5])

    def test_related_sorted_by_index_sort_key_stable_and_capped(self):
        seeds = [_seed(i, ["alpha"], strength=1) for i in range(1, 8)]
        seeds.append(_seed(8, ["alpha"], strength=9))
        eng = self.write(seeds)
        v = _view(eng.alaya, ["alpha"], related_max=3)
        self.assertEqual(v["related_total"], 8)
        self.assertEqual(len(v["related"]), 3)
        expected = sorted(seeds, key=alaya_mod._index_sort_key)[:3]
        self.assertEqual([s["id"] for s in v["related"]], [s["id"] for s in expected])
        self.assertEqual(v["related"][0]["id"], 8)

    def test_groups_cap_total_and_representative(self):
        seeds = []
        for g in range(3):
            seeds += [_seed(10 * g + 1, ["a%d" % g, "b%d" % g]), _seed(10 * g + 2, ["a%d" % g, "b%d" % g])]
        seeds.append(_seed(91, ["lone"]))
        eng = self.write(seeds)
        v = _view(eng.alaya, ["なし"], groups_max=2)
        self.assertEqual(v["groups_total"], 3)
        self.assertEqual(len(v["groups"]), 2)
        self.assertEqual(v["related"], [])
        for g in v["groups"]:
            self.assertEqual(g["representative"], g["member_ids"][0])
            self.assertEqual(g["rep"]["id"], g["representative"])
        expected = alaya_mod.seed_groups([s for s in eng.alaya.state["seeds"]])
        self.assertEqual([g["representative"] for g in v["groups"]], [g["representative"] for g in expected[:2]])

    def test_groups_exclude_dead_and_other_projects_and_ignore_dormancy(self):
        gone = "/nonexistent-raiya-dir/a/b"
        eng = self.write([
            _seed(1, ["a", "b"], kind="所在", content="場所 " + gone),   # 休眠でも代表になる (stat しない)
            _seed(2, ["a", "b"], kind="所在", content="場所 " + gone),
            _seed(3, ["c", "d"], project=Q),
            _seed(4, ["c", "d"], project=Q),
            _seed(5, ["e", "f"], withdrawn={"by": "x", "at": 1, "reason": "r"}),
            _seed(6, ["e", "f"]),
        ])
        with mock.patch("os.stat") as st:
            v = _view(eng.alaya, ["なし"])
        st.assert_not_called()
        self.assertEqual([g["representative"] for g in v["groups"]], [1])
        self.assertEqual(v["groups_total"], 1)

    def test_view_is_a_deep_copy_and_writes_nothing(self):
        eng = self.write([_seed(1, ["alpha", "beta"]), _seed(2, ["alpha", "beta"])])
        before = copy.deepcopy(eng.alaya.state)
        v = _view(eng.alaya, ["alpha"])
        v["related"][0]["content"] = "改ざん"
        v["groups"][0]["rep"]["content"] = "改ざん"
        self.assertEqual(eng.alaya.state, before)

    def test_texts_are_normalized_like_germination(self):
        eng = self.write([_seed(1, ["Alpha  Beta"])])
        v = _view(eng.alaya, ["xx alpha beta yy"])
        self.assertEqual([s["id"] for s in v["related"]], [1])

    def test_index_consolidate_purpose_still_exists(self):
        eng = self.write([_seed(1, ["alpha"])])
        self.assertEqual([s["id"] for s in eng.alaya.index(project={P}, purpose="consolidate")], [1])


class TestConsolidatePrompt(unittest.TestCase):
    def _prompt(self, view):
        return engine_mod._build_consolidate_prompt([], view)

    def test_empty_sections_and_rule_line(self):
        p = self._prompt({"related": [], "related_total": 0, "groups": [], "groups_total": 0})
        self.assertIn("既に記憶している記録のうちこの台帳の語に触れたもの・記録の群の見出しです。", p)
        self.assertIn("【既存の記録: この台帳の語に触れたもの】\n(無し)\n\n【既存の記録の群 (見出し)】\n(無し)\n\n【台帳】", p)
        self.assertIn("- target_id は上に # で示した記録の番号だけです (群の代表の番号を含む)。群そのものは target にできません。", p)
        self.assertNotIn("他に", p)

    def test_related_lines_and_other_count(self):
        seed = _seed(7, ["alpha", "beta"], kind="所在", content="場所の記録")
        p = self._prompt({"related": [seed], "related_total": 4, "groups": [], "groups_total": 0})
        self.assertIn("#7 [所在] [圓成實] 場所の記録 | 縁: alpha, beta\n(他に 3 件)\n\n【既存の記録の群", p)

    def test_group_lines_start_with_seed_id_and_show_scope(self):
        rep = _seed(3, ["a", "b"], content="代表の内容")
        g1 = {"label": "a", "kind": "決定", "project": P, "member_ids": [3, 4], "representative": 3, "size": 2, "rep": rep}
        g2 = {"label": "x", "kind": "決定", "project": None, "member_ids": [5, 6, 7], "representative": 5, "size": 3,
              "rep": _seed(5, ["x", "y"], project=None, content="全域の代表")}
        p = self._prompt({"related": [], "related_total": 0, "groups": [g1, g2], "groups_total": 5})
        self.assertIn("#3 [決定] [圓成實] 代表の内容 | 群「a」2 件 (project: {})".format(P), p)
        self.assertIn("#5 [決定] [圓成實] 全域の代表 | 群「x」3 件 (project: (全域))\n(他に 3 群)", p)


class TestConsolidateEndToEnd(_StateCase):
    def _consolidate(self, eng, user, assistant="了解", debug=None):
        eng.record_turn(user, assistant, project=P)
        with mock.patch("raiya.backend.call_llm", return_value="[]") as call:
            eng.consolidate(debug=debug)
        return call.call_args.args[0]

    def test_prompt_related_and_groups_and_debug_ids(self):
        eng = self.write([
            _seed(1, ["alpha"], content="アルファの記録"),
            _seed(2, ["zeta", "eta"]),
            _seed(3, ["zeta", "eta"]),
        ])
        debug = {}
        prompt = self._consolidate(eng, "alpha の話", debug=debug)
        self.assertIn("#1 [決定] [圓成實] アルファの記録 | 縁: alpha", prompt)
        self.assertNotIn("#2 [決定] [圓成實] 種子2 | 縁:", prompt)  # 触れていない種子は related に出ない
        self.assertIn("#2 [決定] [圓成實] 種子2 | 群「", prompt)       # 群の見出しの代表は出る
        self.assertEqual(debug["related_ids"], [1])
        self.assertEqual(debug["group_rep_ids"], [2])

    def test_withdrawn_seed_is_not_in_prompt(self):
        eng = self.write([_seed(1, ["alpha"], content="取り下げ済み", withdrawn={"by": "x", "at": 1, "reason": "r"})])
        prompt = self._consolidate(eng, "alpha")
        self.assertNotIn("取り下げ済み", prompt)

    def test_related_cap_message_in_prompt(self):
        seeds = [_seed(i, ["alpha"]) for i in range(1, CONSOLIDATE_RELATED_MAX + 4)]
        eng = self.write(seeds)
        prompt = self._consolidate(eng, "alpha")
        self.assertIn("(他に 3 件)", prompt)
        self.assertEqual(prompt.count("| 縁: alpha"), CONSOLIDATE_RELATED_MAX)

    def test_matching_uses_the_truncated_prompt_text(self):
        # 縁が切り詰めの真ん中にしか無い種子は出ない / 見える先頭・末尾にある種子は出る
        middle = "MIDDLEWORD"
        user = "HEADWORD " + "あ" * (CONSOLIDATE_TURN_CHARS * 2) + " " + middle + " " + "い" * (CONSOLIDATE_TURN_CHARS * 2) + " TAILWORD"
        eng = self.write([
            _seed(1, [middle], content="真ん中だけ"),
            _seed(2, ["HEADWORD"], content="先頭"),
            _seed(3, ["TAILWORD"], content="末尾"),
        ])
        debug = {}
        prompt = self._consolidate(eng, user, debug=debug)
        self.assertNotIn(middle, prompt.split("【台帳】")[1])
        self.assertEqual(debug["related_ids"], [2, 3])
        self.assertNotIn("真ん中だけ", prompt)

    def test_empty_window_sets_no_view_ids_in_debug(self):
        eng = self.write([_seed(1, ["alpha"])])
        debug = {}
        eng.consolidate(debug=debug)
        self.assertNotIn("related_ids", debug)
        self.assertNotIn("group_rep_ids", debug)

    def test_consolidate_leaves_alaya_seeds_unchanged_without_proposals(self):
        eng = self.write([_seed(1, ["a", "b"]), _seed(2, ["a", "b"])])
        before = copy.deepcopy(eng.alaya.state["seeds"])
        self._consolidate(eng, "a b")
        self.assertEqual(json.loads(json.dumps(eng.alaya.state["seeds"])), json.loads(json.dumps(before)))


class TestConsolidationReview(_StateCase):
    """QA 指摘 (スライス 3): 印の語・同一性・複数 project・上限ちょうど・休眠・空窓。"""

    def _consolidate(self, eng, turns, debug=None):
        for user, assistant, project in turns:
            eng.record_turn(user, assistant, project=project)
        with mock.patch("raiya.backend.call_llm", return_value="[]") as call:
            eng.consolidate(debug=debug)
        return call.call_args.args[0]

    def test_a_elision_marker_words_do_not_match(self):
        eng = self.write([_seed(1, ["elided"]), _seed(2, ["chars"]), _seed(3, ["HEADWORD"])])
        user = "HEADWORD " + "あ" * (CONSOLIDATE_TURN_CHARS * 3)
        debug = {}
        prompt = self._consolidate(eng, [(user, "了解", P)], debug=debug)
        self.assertIn("<elided ", prompt)  # プロンプトは印のまま
        self.assertEqual(debug["related_ids"], [3])

    def test_b_group_rep_is_located_by_identity_with_duplicate_ids(self):
        eng = self.write([
            _seed(5, ["zz"], content="単独"),
            _seed(5, ["a", "b"], content="成員"),
            _seed(6, ["a", "b"], content="仲間"),
        ])
        v = _view(eng.alaya, ["なし"])
        self.assertEqual(len(v["groups"]), 1)
        self.assertEqual(v["groups"][0]["rep"]["content"], "成員")

    def test_c_multi_project_window_and_global_only(self):
        eng = self.write([
            _seed(1, ["alpha"], project=P), _seed(2, ["alpha"], project=Q),
            _seed(3, ["alpha"], project=None), _seed(4, ["alpha"], project=R),
        ])
        v = _view(eng.alaya, ["alpha"], projects=(P, Q))
        self.assertEqual(sorted(s["id"] for s in v["related"]), [1, 2, 3])
        v = _view(eng.alaya, ["alpha"], projects=(None,))
        self.assertEqual([s["id"] for s in v["related"]], [3])
        debug = {}
        self._consolidate(eng, [("alpha", "a", P), ("alpha", "a", Q)], debug=debug)
        self.assertEqual(sorted(debug["related_ids"]), [1, 2, 3])

    def test_d_exactly_at_caps_prints_no_other_lines(self):
        seeds = [_seed(i, ["alpha"]) for i in range(1, CONSOLIDATE_RELATED_MAX + 1)]
        for g in range(CONSOLIDATE_GROUPS_MAX):
            seeds += [_seed(1000 + 2 * g, ["g%d" % g, "h%d" % g]), _seed(1001 + 2 * g, ["g%d" % g, "h%d" % g])]
        eng = self.write(seeds)
        v = _view(eng.alaya, ["alpha"])
        self.assertEqual((v["related_total"], v["groups_total"]), (30, 20))
        prompt = engine_mod._build_consolidate_prompt([], v)
        self.assertNotIn("(他に", prompt)

    def test_e_condition_only_in_elided_middle_of_assistant_text(self):
        middle = "MIDDLEWORD"
        asst = "HEADWORD " + "あ" * (CONSOLIDATE_TURN_CHARS * 2) + " " + middle + " " + "い" * (CONSOLIDATE_TURN_CHARS * 2) + " TAILWORD"
        eng = self.write([_seed(1, [middle]), _seed(2, ["HEADWORD"]), _seed(3, ["TAILWORD"])])
        debug = {}
        self._consolidate(eng, [("雑談", asst, P)], debug=debug)
        self.assertEqual(debug["related_ids"], [2, 3])

    def test_f_more_than_cap_groups_through_consolidate(self):
        seeds = []
        for g in range(CONSOLIDATE_GROUPS_MAX + 3):
            seeds += [_seed(10 * g + 1, ["g%d" % g, "h%d" % g]), _seed(10 * g + 2, ["g%d" % g, "h%d" % g])]
        eng = self.write(seeds)
        debug = {}
        prompt = self._consolidate(eng, [("雑談", "雑談", P)], debug=debug)
        self.assertIn("(他に 3 群)", prompt)
        self.assertEqual(len(debug["group_rep_ids"]), CONSOLIDATE_GROUPS_MAX)

    def test_g_dormant_group_matching_window_never_stats(self):
        gone = "/nonexistent-raiya-dir/a/b"
        eng = self.write([
            _seed(1, ["a", "b"], kind="所在", content="場所 " + gone),
            _seed(2, ["a", "b"], kind="所在", content="場所 " + gone),
        ])
        debug = {}
        with mock.patch.object(alaya_mod, "_absent_local_paths", side_effect=AssertionError("stat した")) as absent:
            prompt = self._consolidate(eng, [("a b", "了解", P)], debug=debug)
        absent.assert_not_called()
        self.assertEqual(debug["related_ids"], [1, 2])
        self.assertEqual(debug["group_rep_ids"], [1])
        self.assertIn("#1 [所在]", prompt)

    def test_h_empty_window_does_not_build_the_view(self):
        eng = self.write([_seed(1, ["alpha"])])
        with mock.patch.object(alaya_mod.AlayaStore, "consolidation_view") as cv:
            result = eng.consolidate()
        cv.assert_not_called()
        self.assertEqual(result["skipped"], "empty")


if __name__ == "__main__":
    unittest.main()
