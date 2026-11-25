"""改修 5 (プロジェクト記憶) の決定論 pin。spec: docs/superpowers/specs/2026-10-17-project-memory-design.md

E1/E8 形の検査 (§4.3)・E2 project (§4.2)・E3 polarity 外し (§5.1)・E4 横断 supersede (§5.2)・
再開点の特則 (§5.3)・E5 kind 別発芽 (§6.2)・索引 (§6.3)・E6 注入の枠 (§7)。LLM は使わない。
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, sansho  # noqa: E402
from raiya.alaya import (  # noqa: E402
    DECISION_REJECT_MARKERS,
    DEFAULT_KIND,
    INDEX_KIND_LIMITS,
    INDEX_MAX,
    VALID_KINDS,
    AlayaStore,
)
from raiya import engine  # noqa: E402
from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from raiya.manas import GERMINATED_HEADER, INJECTION_MAX_CHARS, NARRATIVE_HEADER  # noqa: E402
from tests._state_fixture import alaya_store_at, manas_store_at  # noqa: E402

RAIYA = "/home/dev/raiya"
KOJIKI = "/home/dev/cosmicanalysis"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "recall_seeds_20261017.json")


def _fixture_seed(seed_id):
    with open(FIXTURE, "r", encoding="utf-8") as f:
        for seed in json.load(f)["seeds"]:
            if seed["id"] == seed_id:
                return seed
    raise KeyError(seed_id)


def _turn(user_text, assistant_text="", project=None, ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=project)


def _proposal(content, conditions, span, source="user", kind=None, polarity="無記", action=None, target_id=None):
    p = {
        "content": content,
        "conditions": list(conditions),
        "polarity": polarity,
        "evidence": [{"source": source, "span": span}],
    }
    if kind is not None:
        p["kind"] = kind
    if action is not None:
        p["action"] = action
        p["target_id"] = target_id
    return p


ASSISTANT_ACK = "了解しました"
# 決定 の形 (却下側の印 or 断定の形) が無いときの注記 (spec 2026-10-30-decision-without-rejection §7 U)
DECISION_FORM_MISSING = "決定 に 圓成實 の引用の却下側の印も断定の形も無いため 性向 として受理"


def _fake_home(case):
    """所在の `~/.raiya` が休眠で外れないよう、HOME を自作 tmp に向けて `.raiya` を置く (#31 / 休眠設計書 §6)。"""
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".raiya"))
    patcher = mock.patch.dict(os.environ, {"HOME": home})
    patcher.start()
    case.addCleanup(shutil.rmtree, home, ignore_errors=True)  # 自作の tmp だけを消す
    case.addCleanup(patcher.stop)  # 後から登録 = 先に実行 (HOME を戻してから消す)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        _fake_home(self)
        self.tmpdir = tempfile.mkdtemp()
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def plant(self, content, conditions, project=None, kind="所在", source="user", span=None, polarity="無記"):
        if span is None:
            span = content
            if source == "user" and kind in ("所在", "性向"):
                # L (question-as-evidence §7): 圓成實 の 所在・性向 は user の span に縁が要る。
                span = "{} {}".format(content, " ".join(conditions))
                if kind == "所在":
                    span += " p/"  # #149: 所在 の形は 區切りの語 (`/` を含む語)
        text = "{} {}".format(span, " ".join(conditions))
        turn = _turn(text, "", project) if source == "user" else _turn("質問 " + " ".join(conditions), text, project)
        return self.store.imprint([turn], _proposal(content, conditions, span, source=source, kind=kind, polarity=polarity))


class TestWorkaroundEntryAndPair(_StoreCase):
    """spec 2026-10-29-workaround-entry-and-pair-design §1・§2・§6: 回避策 の入口 (ユーザーの訂正) と対 (X → Y)。"""

    CONTENT = "デプロイを手で流す → 本番が壊れた原因は順序 → 順序を守る"
    COND = ["デプロイ"]
    Y = "それは違う、元に戻してください"

    def _imprint(self, turns, spans, content=None, kind="回避策", conditions=None):
        prop = {
            "content": content or self.CONTENT,
            "conditions": list(conditions or self.COND),
            "polarity": "無記",
            "evidence": [{"source": src, "span": sp} for src, sp in spans],
            "kind": kind,
        }
        return self.store.imprint(turns, prop)

    def test_workaround_without_user_correction_demotes(self):
        t = _turn("デプロイを手で流した。順序を間違えた", "", RAIYA)
        r = self._imprint([t], [("user", "デプロイを手で流した"), ("user", "順序を間違えた")])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("回避策 にユーザーの訂正の印が無いため 性向 として受理", r["reason"])

    def test_workaround_correction_only_in_assistant_span_demotes(self):
        t0 = _turn("デプロイして", "デプロイを手で流しました", RAIYA)
        t1 = _turn("続けて", "元に戻してくださいと言われたので戻しました", RAIYA)
        r = self._imprint([t0, t1], [("assistant", "デプロイを手で流しました"), ("assistant", "元に戻してくださいと言われた")])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("ユーザーの訂正の印が無い", r["reason"])

    def test_workaround_correction_only_in_content_demotes(self):
        t0 = _turn("デプロイして", "デプロイを手で流しました", RAIYA)
        t1 = _turn("デプロイの順序が違った", "", RAIYA)
        r = self._imprint(
            [t0, t1], [("assistant", "デプロイを手で流しました"), ("user", "デプロイの順序が違った")],
            content="ユーザーが元に戻してくださいと言った → 順序が原因",
        )
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("ユーザーの訂正の印が無い", r["reason"])

    def test_workaround_evidence_source_self_report_is_ignored(self):
        # 訂正の語を含む span が assistant にだけ接地するなら、source を "user" と自称しても効かない
        t0 = _turn("デプロイして", "デプロイを手で流しました", RAIYA)
        t1 = _turn("続けて", "それは違う、元に戻してください", RAIYA)
        r = self._imprint([t0, t1], [("user_correction", "デプロイを手で流しました"), ("user", "それは違う、元に戻してください")])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertEqual(r["seed"]["sansho"], "依他起")

    def test_workaround_single_span_with_correction_demotes(self):
        t = _turn("デプロイを手で流したので元に戻してください", "", RAIYA)
        r = self._imprint([t], [("user", "デプロイを手で流したので元に戻してください")])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("対 (別の span 2 本) を持たないため 性向 として受理", r["reason"])

    def test_workaround_pair_user_x_same_turn_is_kept(self):
        t = _turn("デプロイを手で流した。{}".format(self.Y), "", RAIYA)
        r = self._imprint([t], [("user", "デプロイを手で流した"), ("user", self.Y)])
        self.assertEqual(r["seed"]["kind"], "回避策")
        self.assertEqual(r["seed"]["sansho"], "圓成實")

    def test_workaround_pair_assistant_x_previous_turn_is_kept(self):
        t0 = _turn("デプロイして", "デプロイを手で流しました", RAIYA)
        t1 = _turn(self.Y + " デプロイ", "", RAIYA)
        r = self._imprint([t0, t1], [("assistant", "デプロイを手で流しました"), ("user", self.Y)])
        self.assertEqual(r["seed"]["kind"], "回避策")
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.assertEqual(r["seed"]["strength"], 2)

    def test_workaround_pair_user_x_earlier_turn_is_kept(self):
        t0 = _turn("デプロイを手で流した", "了解", RAIYA)
        t1 = _turn(self.Y + " デプロイ", "", RAIYA)
        r = self._imprint([t0, t1], [("user", "デプロイを手で流した"), ("user", self.Y)])
        self.assertEqual(r["seed"]["kind"], "回避策")
        self.assertEqual(r["seed"]["sansho"], "圓成實")

    def test_workaround_three_spans_nested_plus_valid_x_is_kept(self):
        t = _turn("デプロイを手で流した。{}".format(self.Y), "", RAIYA)
        r = self._imprint([t], [("user", self.Y), ("user", "元に戻してください"), ("user", "デプロイを手で流した")])
        self.assertEqual(r["seed"]["kind"], "回避策")

    def test_workaround_pair_with_preference_suffix_demotes(self):
        t = _turn("デプロイを手で流した。{}".format(self.Y), "", RAIYA)
        r = self._imprint(
            [t], [("user", "デプロイを手で流した"), ("user", self.Y)],
            content="デプロイを手で流す → 順序が原因 → 順序を守ってほしい",
        )
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("文末が好みの形", r["reason"])
        self.assertNotIn("対 (別の span 2 本)", r["reason"])

    def test_workaround_pair_assistant_x_same_turn_demotes(self):
        t = _turn(self.Y + " デプロイ", "デプロイを手で流しました", RAIYA)
        r = self._imprint([t], [("assistant", "デプロイを手で流しました"), ("user", self.Y)])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("対 (別の span 2 本) を持たない", r["reason"])

    def test_workaround_pair_x_after_y_demotes(self):
        t0 = _turn(self.Y + " デプロイ", "", RAIYA)
        t1 = _turn("デプロイを手で流した", "", RAIYA)
        r = self._imprint([t0, t1], [("user", self.Y), ("user", "デプロイを手で流した")])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("対 (別の span 2 本) を持たない", r["reason"])

    def test_workaround_pair_nested_spans_demote(self):
        t = _turn("デプロイ。{}".format(self.Y), "", RAIYA)
        r = self._imprint([t], [("user", self.Y), ("user", "元に戻してください")])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("対 (別の span 2 本) を持たない", r["reason"])

    def test_workaround_pair_same_text_two_turns_demotes(self):
        t0 = _turn(self.Y + " デプロイ", "", RAIYA)
        t1 = _turn(self.Y, "", RAIYA)
        r = self._imprint([t0, t1], [("user", self.Y), ("user", self.Y)])
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("対 (別の span 2 本) を持たない", r["reason"])

    def test_workaround_pair_cross_project_rejected(self):
        t0 = _turn("デプロイして", "デプロイを手で流しました", RAIYA)
        t1 = _turn(self.Y + " デプロイ", "", KOJIKI)
        r = self._imprint([t0, t1], [("assistant", "デプロイを手で流しました"), ("user", self.Y)])
        self.assertEqual(r["decision"], "reject")
        self.assertIn("project 跨ぎ", r["reason"])
        self.assertEqual(self.store.list_seeds(), [])

    def test_correction_inside_paste_is_not_grounded(self):
        from raiya.engine import _gate_window
        paste = '<pasted_content id="p1">\n{}\n</pasted_content id="p1">'.format(self.Y)
        t = LedgerTurn(
            user_text="デプロイを手で流した " + paste, assistant_text="", id=1, project=RAIYA,
            grounding_text="デプロイを手で流した",
        )
        gate = _gate_window([t])
        prop = {
            "content": self.CONTENT, "conditions": self.COND, "polarity": "無記", "kind": "回避策",
            "evidence": [{"source": "user", "span": "デプロイを手で流した"}, {"source": "user", "span": self.Y}],
        }
        r = self.store.imprint(gate, prop)
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(self.store.list_seeds(), [])

    def test_old_single_span_workaround_seed_unchanged(self):
        # 既存の種子 (訂正なし単一 span の 回避策) は書き換えない・読み時に再計算しない。
        # 併合は同 kind が条件なので、訂正の無い新提案 (性向 に落ちる) は古い種子を reinforce しない。
        old = self.plant("デプロイを手で流す → 順序が原因 → 順序を守る", ["デプロイ"], kind="所在")
        old_id = old["seed"]["id"]
        seed = [x for x in self.store.state["seeds"] if x["id"] == old_id][0]
        seed["kind"] = "回避策"   # 旧版が植えた単一 span の 回避策 を直接置く
        before = copy.deepcopy(seed)
        second = self.plant("デプロイを手で流す → 順序が原因 → 順序を守る", ["デプロイ"], kind="回避策")
        self.assertEqual(second["seed"]["kind"], "性向")
        self.assertNotEqual(second["seed"]["id"], old_id)
        after = [x for x in self.store.list_seeds() if x["id"] == old_id][0]
        self.assertEqual(after["kind"], "回避策")
        self.assertEqual(after["strength"], before["strength"])
        self.assertEqual(after["evidences"], before["evidences"])
        self.assertIn(old_id, [x["id"] for x in self.store.index(project=None) if x["kind"] == "回避策"])

    def test_plant_precheck_applies_workaround_rule(self):
        from raiya.alaya import plant_precheck
        t0 = _turn("デプロイして", "デプロイを手で流しました", RAIYA)
        t1 = _turn(self.Y + " デプロイ", "", RAIYA)
        prop = {
            "content": self.CONTENT, "conditions": self.COND, "kind": "回避策",
            "evidence": [{"source": "", "span": "デプロイを手で流しました"}, {"source": "", "span": self.Y}],
        }
        self.assertIsNone(plant_precheck([t0, t1], prop))
        from raiya import alaya as _alaya
        from raiya import sansho as _sansho
        win = _sansho.as_window([t0, t1])
        decision = _sansho.evaluate_proposal(prop, win)
        self.assertEqual(_alaya._resolve_kind(prop, decision["spans"])[0], "回避策")
        prop["evidence"] = prop["evidence"][:1]
        decision = _sansho.evaluate_proposal(prop, win)
        self.assertEqual(_alaya._resolve_kind(prop, decision["spans"])[0], "性向")

    def test_resolve_kind_with_string_spans_cannot_prove_a_correction(self):
        from raiya import alaya as _alaya
        prop = {"kind": "回避策", "content": self.CONTENT}
        kind, note = _alaya._resolve_kind(prop, ["デプロイを手で流した", self.Y])
        self.assertEqual(kind, "性向")


class TestKindFormCheck(_StoreCase):
    """E1 / E8 / §4.3: kind は自己申告だが形はゲートが検査する。"""

    def test_kind_missing_is_disposition(self):
        r = self.plant("猫が好き", ["猫"], kind=None)
        self.assertEqual(r["kind"], DEFAULT_KIND)
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIsNone(r["seed"]["project"])
        self.assertIsNone(r["seed"]["superseded_by"])

    def test_kind_invalid_is_rejected_and_logged(self):
        r = self.plant("趣味の話", ["趣味"], kind="趣味")
        self.assertEqual(r["decision"], "reject")
        self.assertIn("kind", r["reason"])
        self.assertEqual(self.store.list_seeds(), [])
        self.assertEqual(self.store.list_imprint_log(1)[0]["decision"], "reject")

    def test_decision_without_reject_marker_demotes_to_disposition(self):
        # #114 U: 縁を含む user の span があれば 決定 のまま (TestDecisionWithoutRejection)。
        # 縁も却下側の印も 圓成實 の span に無ければ従来どおり 性向 (span を縁なしに変更: 旧 span=content は縁を含む)。
        # (圓成實 の 性向 は L で縁/好みの印を要するため植わらない = seed None。kind の読み替えは r["kind"] で見る)
        r = self.plant("claude-cli では --setting-sources を使う", ["setting-sources"], kind="決定", span="claude-cli では")
        self.assertEqual(r["kind"], "性向")
        self.assertIn("却下側の印", r["reason"])
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    def test_decision_with_reject_marker_is_kept(self):
        content = "claude-cli の統合呼び出しは --bare でなく --setting-sources を採用 (--bare は OAuth を落とすため却下)"
        r = self.plant(content, ["setting-sources"], kind="決定")
        self.assertEqual(r["seed"]["kind"], "決定")
        self.assertTrue(any(m in content for m in DECISION_REJECT_MARKERS))

    # --- Issue #14 原因 2 (spec 2026-10-23-twin-seed-index §3): 決定 の印は接地した引用に求める ---
    def test_decision_marker_only_in_content_demotes(self):
        # #114 U: span に縁 (採用) があるなら content の印だけでも 決定 (旧: 性向)。縁の無い span は 性向 のまま。
        content = "B ではなく A を採用"
        r = self.plant(content, ["採用"], kind="決定", span="A にする")
        self.assertEqual(r["kind"], "性向")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    def test_decision_without_reject_marker_with_condition_in_span_is_kept(self):
        # #114 U: 旧 test_decision_without_reject_marker_demotes_to_disposition の入力そのまま (span = content)。性向 → 決定 に反転。
        r = self.plant("claude-cli では --setting-sources を使う", ["setting-sources"], kind="決定")
        self.assertEqual(r["seed"]["kind"], "決定")
        self.assertNotIn("性向 として受理", r["reason"])

    def test_decision_marker_only_in_content_with_condition_in_span_is_kept(self):
        # 旧 test_decision_marker_only_in_content_demotes の入力そのまま。#114 U で 性向 → 決定 に反転。
        r = self.plant("B ではなく A を採用", ["採用"], kind="決定", span="A を採用")
        self.assertEqual(r["seed"]["kind"], "決定")
        self.assertNotIn("性向 として受理", r["reason"])

    def test_decision_marker_only_in_assistant_span_demotes(self):
        # spec 2026-10-30 §7 H: 印は 圓成實 (user 接地) の span に要る。assistant だけの span では 性向。
        r = self.plant("B ではなく A を採用", ["採用"], kind="決定", source="assistant", span="B でなく A を採用")
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    def test_decision_evidence_source_self_report_is_ignored(self):
        # 提案の自称 (evidence.source == "user" / sansho == "圓成實") は読まない: 判定は逐語照合だけ。
        turn = _turn("質問 採用", "B でなく A を採用", RAIYA)
        proposal = _proposal("B ではなく A を採用", ["採用"], "B でなく A を採用", source="user", kind="決定")
        proposal["sansho"] = "圓成實"
        r = self.store.imprint([turn], proposal)
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.assertEqual(r["seed"]["evidences"][0]["source"], "assistant")
        self.assertIn("断定の形も無いため", r["reason"])

    def test_precheck_and_imprint_agree_assistant_only_marker_is_disposition(self):
        turn = _turn("質問 採用", "B でなく A を採用", RAIYA)
        proposal = _proposal("B ではなく A を採用", ["採用"], "B でなく A を採用", source="assistant", kind="決定")
        from raiya import alaya as _alaya
        # plant_precheck は kind を返さず (植わるなら None)、_resolve_kind を同じ判定 span で呼ぶ。
        decision = sansho.evaluate_proposal(proposal, sansho.as_window([turn]))
        self.assertEqual(_alaya._resolve_kind(proposal, decision["spans"])[0], "性向")
        self.assertIsNone(_alaya.plant_precheck([turn], proposal))
        r = self.store.imprint([turn], proposal)
        self.assertEqual(r["seed"]["kind"], "性向")

    def test_decision_marker_in_one_of_several_spans_is_kept(self):
        turn = _turn("A を採用する。B は不採用にする。", "", RAIYA)
        proposal = _proposal("B ではなく A を採用", ["採用"], "A を採用する", kind="決定")
        proposal["evidence"].append({"source": "user", "span": "B は不採用にする"})
        r = self.store.imprint([turn], proposal)
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_decision_marker_matched_after_normalization(self):
        turn = _turn("B でなく A にする", "", RAIYA)
        r = self.store.imprint([turn], _proposal("B ではなく A", ["A"], "B で なく A", kind="決定"))
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_decision_markers_are_normalization_invariant(self):
        for m in DECISION_REJECT_MARKERS:
            self.assertEqual(sansho._n(m), m)

    def test_workaround_content_marker_alone_is_not_enough(self):
        # 原因の印は content で足りるが、回避策 にはさらにユーザーの訂正と対が要る (2026-10-29 spec)
        r = self.plant("症状 → 原因は X → 回避", ["症状"], kind="回避策", span="症状")
        self.assertEqual(r["seed"]["kind"], "性向")

    def test_demoted_decision_does_not_merge_into_existing_decision(self):
        first = self.plant("A を採用 (B は却下)", ["採用", "却下"], kind="決定", span="B は却下 採用")
        self.assertEqual(first["seed"]["kind"], "決定")
        before = copy.deepcopy(self.store.list_seeds()[0])
        # span を「A を採用したい」に変更: 旧 span「A を採用」は縁を含み好みの印も無く #114 U で 決定 になる。
        # 好みの印 (したい) を含む span: 縁を含むが好みの印があるので (ii) を満たさず 性向 (L は好みの印で通る)。
        second = self.plant("A を採用 (B ではなく)", ["採用", "却下"], kind="決定", span="A を採用したい")
        self.assertEqual(second["seed"]["kind"], "性向")
        self.assertNotEqual(second["seed"]["id"], before["id"])
        after = [s for s in self.store.list_seeds() if s["id"] == before["id"]][0]
        self.assertEqual(after["strength"], before["strength"])

    def test_one_sided_decision_merges_into_two_sided_decision(self):
        # #114 U: 片側の 決定 (縁を含む user span・却下側の印なし) は、同クラス ∧ 同 kind ∧ 同 project ∧ 類似 ∧ 縁 2 語共有
        # (#104: min(2, 小さい側の縁の数)) なので既存の両側の 決定 に併合され reinforce になる (別種子は立たない)。
        first = self.plant("A を採用 (B は却下)", ["採用", "却下"], kind="決定", span="B は却下 採用")
        self.assertEqual(first["seed"]["kind"], "決定")
        second = self.plant("A を採用 (B ではなく)", ["採用", "却下"], kind="決定", span="A を採用")
        self.assertEqual(second["kind"], "決定")
        self.assertTrue(second.get("reinforced"))
        self.assertFalse(second["is_new"])
        self.assertEqual(second["seed"]["id"], first["seed"]["id"])
        self.assertEqual(second["seed"]["strength"], first["seed"]["strength"] + 1)
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_workaround_without_cause_marker_demotes(self):
        r = self.plant("正規表現を線形走査に替えた", ["正規表現"], kind="回避策")
        self.assertEqual(r["seed"]["kind"], "性向")

    def test_workaround_with_cause_marker_without_correction_demotes(self):
        r = self.plant("private の除去を正規表現でやると 400 KB で 7 秒かかる → 線形走査に替えた", ["正規表現"], kind="回避策")
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("ユーザーの訂正の印が無い", r["reason"])

    def test_preference_suffix_demotes_non_disposition_kind(self):
        r = self.plant("本番 state は ~/.raiya に置いてほしい。", ["state"], kind="所在")
        self.assertEqual(r["seed"]["kind"], "性向")
        r2 = self.plant("本番 state は ~/.raiya にある", ["state"], kind="所在")
        self.assertEqual(r2["seed"]["kind"], "所在")

    # --- marker-table spec (2026-10-24) §6.2: 英語の表・casefold・和集合 ---
    def test_en_decision_marker_in_grounded_span_is_kept(self):
        r = self.plant("TOML instead of YAML", ["TOML"], kind="決定", span="Use TOML instead of YAML")
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_en_decision_marker_only_in_content_demotes(self):
        # span を縁 (TOML) の無い「Use it」に変更: 旧 span「Use TOML」は縁を含み #114 U で 決定 になる (下の _is_kept)。
        r = self.plant("TOML instead of YAML", ["TOML"], kind="決定", span="Use it")
        self.assertEqual(r["kind"], "性向")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    def test_en_decision_marker_only_in_content_with_condition_in_span_is_kept(self):
        r = self.plant("TOML instead of YAML", ["TOML"], kind="決定", span="Use TOML")
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_en_decision_marker_is_case_insensitive(self):
        r = self.plant("Instead Of YAML: TOML", ["TOML"], kind="決定", span="Instead of YAML, use TOML")
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_bare_not_is_not_a_reject_marker(self):
        # #114 U: 縁を渡さない (conditions=()) と 却下側の印だけが頼り。素の not は印でないので 性向 (断定の形に頼らず印の表を直接見る)。
        from raiya import alaya as _alaya
        span = {"span": "Do not use YAML", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(_alaya._resolve_kind({"kind": "決定", "content": "YAML rejected"}, [span])[0], "性向")
        self.assertFalse(any(m in "Do not use YAML".casefold() for m in DECISION_REJECT_MARKERS))

    def test_bare_not_does_not_promote_decision(self):
        # span を縁 (YAML) の無い「Do not use it」に変更: 素の not は印でない。縁を含む span は #114 U で別経路 (断定の形) で 決定。
        r = self.plant("YAML rejected", ["YAML"], kind="決定", span="Do not use it")
        self.assertEqual(r["kind"], "性向")

    def test_mixed_ja_span_en_content_is_kept(self):
        r = self.plant("TOML instead of YAML", ["YAML"], kind="決定", span="YAML ではなく")
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_ja_decision_still_works_with_union(self):
        r = self.plant("A ではなく B", ["B"], kind="決定", span="A ではなく B")
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_en_workaround_cause_in_content_without_correction_demotes(self):
        r = self.plant("pip fails because the proxy blocks it → set HTTPS_PROXY", ["pip"], kind="回避策", span="pip fails")
        self.assertEqual(r["seed"]["kind"], "性向")

    def test_en_workaround_pair_with_correction_is_kept(self):
        turn = _turn("pip install fails behind the proxy. That's wrong, set HTTPS_PROXY", "", RAIYA)
        prop = _proposal("pip fails because the proxy blocks it → set HTTPS_PROXY", ["pip"], "pip install fails behind the proxy", kind="回避策")
        prop["evidence"].append({"source": "user", "span": "That's wrong, set HTTPS_PROXY"})
        r = self.store.imprint([turn], prop)
        self.assertEqual(r["seed"]["kind"], "回避策")

    def test_en_workaround_without_cause_demotes(self):
        r = self.plant("set HTTPS_PROXY for pip", ["pip"], kind="回避策", span="set HTTPS_PROXY for pip")
        self.assertEqual(r["seed"]["kind"], "性向")

    def test_preference_suffix_unchanged_for_ja(self):
        r = self.plant("本番 state は ~/.raiya に置いてほしい。", ["state"], kind="所在")
        self.assertEqual(r["seed"]["kind"], "性向")
        r2 = self.plant("put the production state in ~/.raiya please", ["state"], kind="所在")
        self.assertEqual(r2["seed"]["kind"], "所在")

    def test_resolve_kind_skips_non_str_and_bare_str_spans(self):
        # 手順 4 の isinstance(sp, str) ガード: 非 str の span は印の照合から外す (落ちない)。
        from raiya import alaya as _alaya
        prop = {"kind": "決定", "content": "A ではなく B"}
        ok = {"span": "A ではなく B", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(_alaya._resolve_kind(prop, [None, 123, ok]), ("決定", None))
        # 判定の無い str・判定が 圓成實 でない dict は 圓成實 と見なさない (H)
        for spans in ([None, 123], ["A ではなく B"], [dict(ok, sansho="依他起")]):
            kind, note = _alaya._resolve_kind(prop, spans)
            self.assertEqual(kind, "性向")
            self.assertIn("断定の形も無いため", note)
            # 縁を渡しても、圓成實 の dict でない項目は断定の形の根拠にならない (#114 U)
            self.assertEqual(_alaya._resolve_kind(prop, spans, ["A"])[0], "性向")

    def test_ungrounded_proposal_with_invalid_kind_is_quarantined_not_rejected(self):
        # §4.3: 遍計所執は形の検査より先に quarantine (非接地の観測記録を kind の形で失わない)。
        r = self.store.imprint([_turn("猫の話", "", RAIYA)], _proposal("捏造", ["猫"], "ここには無い引用", kind="趣味"))
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(r["quarantine_entry"]["kind"], DEFAULT_KIND)
        self.assertEqual(self.store.quarantine_total(), 1)
        r2 = self.store.imprint([_turn("猫の話 猫", "", RAIYA)], _proposal("猫の話", ["猫"], "猫の話", kind="趣味"))
        self.assertEqual(r2["decision"], "reject")  # 接地した提案の不正 kind は却下

    def test_quarantine_entry_carries_kind_and_project(self):
        r = self.store.imprint(
            [_turn("猫の話", "", RAIYA)],
            _proposal("捏造", ["猫"], "ここには無い引用", kind="所在"),
        )
        self.assertEqual(r["decision"], "遍計所執")
        self.assertEqual(r["quarantine_entry"]["kind"], "所在")
        self.assertIsNone(r["quarantine_entry"]["project"])

    def test_valid_kinds_enumeration_is_the_spec_list(self):
        self.assertEqual(VALID_KINDS, ("決定", "回避策", "所在", "再開点", "性向"))


class TestDecisionWithoutRejection(_StoreCase):
    """#114 U (spec 2026-10-30-decision-without-rejection §7): 決定 の形は 圓成實 の span の
    (i) 却下側の印 または (ii) 縁を含み好みの印を含まない span。content の却下側の印は要らない。"""

    def test_1_enjojitsu_condition_in_user_span_without_reject_is_decision_and_indexed(self):
        r = self.plant("DB は SQLite にした", ["SQLite"], kind="決定")
        self.assertEqual(r["seed"]["kind"], "決定")
        self.assertEqual(r["seed"]["sansho"], "圓成實")
        self.assertNotIn("性向 として受理", r["reason"])
        self.assertFalse(any(m in "DB は SQLite にした" for m in DECISION_REJECT_MARKERS))
        index = self.store.index(project=None)
        self.assertEqual([(s["kind"], s["content"]) for s in index], [("決定", "DB は SQLite にした")])

    def test_2_no_condition_in_user_span_is_disposition(self):
        # 縁 (SQLite) は往復本文にあるが、user の span (DB にした) には無い。
        r = self.plant("DB は SQLite にした", ["SQLite"], kind="決定", span="DB にした")
        self.assertEqual(r["sansho"], "圓成實")
        self.assertEqual(r["kind"], "性向")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])
        # 性向 に読み替わった 圓成實 は L (縁か好みの印を担う user の span) で植わらない (従来の挙動)。決定 としては載らない。
        self.assertIsNone(r["seed"])
        self.assertEqual(self.store.index(project=None), [])

    def test_3_preference_marker_in_the_same_span_is_disposition(self):
        r = self.plant("DB は SQLite にした", ["SQLite"], kind="決定", span="SQLite を使ってほしい")
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    def test_3b_preference_marker_is_judged_per_span(self):
        # 好みの印を含む span とは別の、縁を含み好みの印の無い span が 1 本あれば 決定。
        turn = _turn("SQLite を使ってほしい。DB は SQLite にした", "", RAIYA)
        prop = _proposal("DB は SQLite にした", ["SQLite"], "SQLite を使ってほしい", kind="決定")
        prop["evidence"].append({"source": "user", "span": "DB は SQLite にした"})
        r = self.store.imprint([turn], prop)
        self.assertEqual(r["seed"]["sansho"], "圓成實")
        self.assertEqual(r["seed"]["kind"], "決定")

    def test_4_preference_marker_in_content_is_disposition(self):
        r = self.plant("DB は SQLite を使ってほしい", ["SQLite"], kind="決定", span="DB は SQLite にした")
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("文末が好みの形", r["reason"])

    def test_5_reject_marker_in_user_span_keeps_decision_without_content_marker_or_condition(self):
        # (i): 却下側の印は 圓成實 の span にあれば足りる。content にも縁にも要らない。
        r = self.plant("DB は SQLite", ["PostgreSQL"], kind="決定", span="MySQL ではなく SQLite にした")
        self.assertEqual(r["seed"]["kind"], "決定")
        self.assertNotIn("性向 として受理", r["reason"])
        # 従来経路 (content にも印): 不変
        r2 = self.plant("A を採用 (B は却下)", ["採用"], kind="決定", span="B は却下 採用")
        self.assertEqual(r2["seed"]["kind"], "決定")

    def test_6_assistant_only_decision_is_disposition(self):
        r = self.plant("DB は SQLite にした", ["SQLite"], kind="決定", source="assistant", span="DB は SQLite にした")
        self.assertEqual(r["seed"]["sansho"], "依他起")
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    def test_7_rejection_only_assertion_with_condition_is_decision(self):
        # r5: 却下だけの断定 (「B はやめた」)。語表には足さない: 縁を含むので (ii) で 決定。
        self.assertFalse(any(m in "MySQL はやめた" for m in DECISION_REJECT_MARKERS))
        r = self.plant("MySQL はやめた", ["MySQL"], kind="決定")
        self.assertEqual(r["seed"]["kind"], "決定")
        # 縁を含まなければ 性向
        r2 = self.plant("PostgreSQL はやめた", ["PostgreSQL"], kind="決定", span="それはやめた")
        self.assertEqual(r2["kind"], "性向")

    def test_one_sided_decision_threshold_unchanged_for_enjojitsu(self):
        r = self.plant("DB は SQLite にした", ["SQLite"], kind="決定")
        self.assertEqual(self.store._threshold(r["seed"]), 1)
        self.assertEqual(len(self.store.germinate("SQLite の話")), 1)

    def test_10_other_kinds_checks_unchanged(self):
        # 回避策: 縁を含む user の span があっても、訂正の印と対が無ければ 性向 (断定の形は 決定 だけの規則)。
        r = self.plant("デプロイを手で流す → 原因は順序 → 順序を守る", ["デプロイ"], kind="回避策", span="デプロイを手で流した")
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIn("ユーザーの訂正の印が無い", r["reason"])
        # 所在: 好みの形の content は 性向 / path を担う content は 所在
        r = self.plant("本番 state は ~/.raiya に置いてほしい。", ["state"], kind="所在")
        self.assertEqual(r["seed"]["kind"], "性向")
        r = self.plant("本番 state は ~/.raiya にある", ["state"], kind="所在")
        self.assertEqual(r["seed"]["kind"], "所在")
        # 性向: kind を持たない提案は 性向 のまま
        self.assertEqual(self.plant("猫が好き", ["猫"], kind=None)["seed"]["kind"], "性向")
        # 再開点: 依他起 の新規は従来どおり却下 (B)
        r = self.plant("次は X から", ["X"], kind="再開点", source="assistant", span="次は X から")
        self.assertEqual(r["decision"], "reject")

    def test_resolve_kind_pure_function(self):
        from raiya import alaya as _alaya
        prop = {"kind": "決定", "content": "DB は SQLite にした"}
        span = {"span": "DB は  SQLite にした", "sansho": "圓成實", "turn_index": 0}   # 空白の揺れは sansho._n が畳む
        # 縁の照合は casefold・正規化済み
        self.assertEqual(_alaya._resolve_kind(prop, [span], ["sqlite"]), ("決定", None))
        self.assertEqual(_alaya._resolve_kind(prop, [span], ["ＳＱＬｉｔｅ"])[0], "決定")   # NFKC
        # 後方互換: 縁を渡さない既定 () は 却下側の印だけが頼り
        self.assertEqual(_alaya._resolve_kind(prop, [span])[0], "性向")
        # 空白だけの縁はどの span にも一致しない (parity: form_in_user_spans_for_kind も同じ)
        self.assertEqual(_alaya._resolve_kind({"kind": "決定", "content": "use TOML"},
                                              [{"span": "use TOML", "sansho": "圓成實", "turn_index": 0}], [" "])[0], "性向")
        self.assertFalse(_alaya.form_in_user_spans_for_kind("性向", ["use TOML"], [" "]))
        # 縁が非 str / 空・span が 依他起 / 縁を含まない
        self.assertEqual(_alaya._resolve_kind(prop, [span], [None, "", 3])[0], "性向")
        self.assertEqual(_alaya._resolve_kind(prop, [dict(span, sansho="依他起")], ["SQLite"])[0], "性向")
        self.assertEqual(_alaya._resolve_kind(prop, [span], ["MySQL"])[0], "性向")
        # 縁に好みの印が無くても、縁を含む span の好みの印で 性向
        pref = dict(span, span="SQLite を使ってほしい")
        self.assertEqual(_alaya._resolve_kind(prop, [pref], ["SQLite"])[0], "性向")

    def test_plant_precheck_and_imprint_agree(self):
        # plant_precheck は kind を返さず植わるなら None。imprint と同じ縁 (grounded_conditions) を _resolve_kind に渡す。
        from raiya import alaya as _alaya
        turn = _turn("DB は SQLite にした", "", RAIYA)
        prop = _proposal("DB は SQLite にした", ["SQLite"], "DB は SQLite にした", kind="決定")
        decision = sansho.evaluate_proposal(prop, sansho.as_window([turn]))
        self.assertEqual(_alaya._resolve_kind(prop, decision["spans"], decision["grounded_conditions"])[0], "決定")
        self.assertIsNone(_alaya.plant_precheck([turn], prop))
        self.assertEqual(self.store.imprint([turn], prop)["seed"]["kind"], "決定")


class TestDecisionQuestionSentenceExcluded(_StoreCase):
    """#114 U 追補 (spec 2026-10-30-decision-without-rejection §7「疑の文の除外」): (ii) は span から 疑の文
    (窓の user の本文を文に分けたとき、文末の記号列に ？/? を含む文。語の途中の半角 ? は文末でない) を除いた
    残りで判定する。読む往復は span が接地した往復と同じ project のもの。(i) 却下側の印は span 全体。"""

    THREE = "We use PostgreSQL for the database. I'd like FastAPI for the API layer. Maybe try Celery for jobs?"
    DB_Q = "前に決めた DB と設定の置き場所はどれだった？"

    def _imp(self, user_text, spans, conditions, assistant_text="", content="決めたこと", grounding_text=None):
        """spans = [(source, span)]。往復 1 件の窓で imprint する。"""
        turn = (
            LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=0, project=RAIYA, grounding_text=grounding_text)
            if grounding_text is not None else _turn(user_text, assistant_text, RAIYA)
        )
        prop = {
            "content": content, "conditions": list(conditions), "polarity": "無記", "kind": "決定",
            "evidence": [{"source": src, "span": sp} for src, sp in spans],
        }
        return turn, prop, self.store.imprint([turn], prop)

    def _resolve(self, spans, conditions, window=None):
        _alaya = alaya
        items = [{"span": sp, "sansho": "圓成實", "turn_index": 0} for sp in spans]
        return _alaya._resolve_kind({"kind": "決定", "content": "x"}, items, conditions, window)

    def _kind(self, user_text, span, conditions, **kw):
        return self._imp(user_text, [("user", span)], conditions, **kw)[2]["kind"]

    # 1. 疑の文だけの span
    def test_question_only_span_is_disposition(self):
        text = "Maybe try Celery for jobs?"
        self.assertEqual(self._kind(text, text, ["Celery"]), "性向")
        # 窓なし (span 自身を文に分ける) でも同じ。2 引数・3 引数の呼び出しは従来どおり使える
        self.assertEqual(self._resolve([text], ["Celery"])[0], "性向")
        window = sansho.as_window([_turn(text)])
        self.assertEqual(self._resolve([text], ["Celery"], window)[0], "性向")
        self.assertIn(DECISION_FORM_MISSING, self._resolve([text], ["Celery"], window)[1])

    # 2. 短い span は囲む文の ？ を受ける
    def test_short_span_inside_question_sentence_is_disposition(self):
        window = sansho.as_window([_turn(self.DB_Q)])
        self.assertEqual(self._resolve(["前に決めた DB"], ["DB"], window)[0], "性向")
        self.assertEqual(self._kind(self.DB_Q, "前に決めた DB", ["DB"]), "性向")
        # 窓が無ければ span 自身には ？ が無い: 従来どおり 決定 (後方互換を固定する)
        self.assertEqual(self._resolve(["前に決めた DB"], ["DB"])[0], "決定")
        # turn_index は同じ project の往復を選ぶのに読む。使えない (無い・窓の外・bool) ときは窓の全往復
        # (ここでは全往復が project なし = 同じ project なので、どれも同じ結果)
        for idx in (None, 5, -1, True):
            item = {"span": "前に決めた DB", "sansho": "圓成實", "turn_index": idx}
            self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["DB"], window)[0], "性向", idx)

    def test_mixed_form_with_assistant_span_is_disposition(self):
        # 依他起 (assistant の span が混ざる): 圓成實 の user span 「前に決めた DB」は疑の文の一部
        _turn_, _prop, r = self._imp(
            self.DB_Q, [("user", "前に決めた DB"), ("assistant", "前に決めた DB は SQLite です")], ["DB"],
            assistant_text="前に決めた DB は SQLite です",
        )
        self.assertEqual(r["sansho"], "依他起")
        self.assertEqual(r["kind"], "性向")
        self.assertIn(DECISION_FORM_MISSING, r["reason"])

    # 3. 複数文の span は断定の文を残す
    def test_multi_sentence_span_keeps_assertion_sentences(self):
        self.assertEqual(self._kind(self.THREE, self.THREE, ["PostgreSQL", "FastAPI", "Celery"]), "決定")
        self.assertEqual(self._resolve([self.THREE], ["PostgreSQL", "FastAPI", "Celery"])[0], "決定")
        # 縁が疑の文にしか無ければ 性向
        self.assertEqual(self._kind(self.THREE, self.THREE, ["Celery"]), "性向")
        # 縁が文を跨いで一致しない (残りの文は区切って判定する)
        text = "We use foo\nMaybe q?\nbar ok"
        self.assertEqual(self._kind(text, text, ["foo"], assistant_text="foo bar"), "決定")
        self.assertEqual(self._kind(text, text, ["foo bar"], assistant_text="foo bar"), "性向")

    # 4. 断定の文は従来どおり
    def test_assertions_unchanged(self):
        for text, cond in (
            ("DB は SQLite にした", "SQLite"),
            ("設定ファイルは config/ に置く", "config"),
            ("キャッシュは Redis がいい", "Redis"),
            ("ログは structlog を使いたい", "structlog"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text, text, [cond]), "決定")
                self.assertEqual(self._resolve([text], [cond])[0], "決定")

    # 5. 好みの印は残りの部分だけを見る (断定の文の好みの印は従来どおり効く)
    def test_preference_marker_in_remaining_part_still_applies(self):
        text = "テストは pytest で書くのが好み。unittest は使わないで。"
        self.assertEqual(self._kind(text, text, ["pytest"]), "決定")   # 従来どおり (「を好む」ではない)
        self.assertEqual(self._kind("SQLite を使ってほしい。次は?", "SQLite を使ってほしい。次は?", ["SQLite"]), "性向")
        # 好みの印が疑の文の側にあるときは、残りの断定で 決定
        self.assertEqual(
            self._kind("DB は SQLite にした。Redis を使ってほしい？", "DB は SQLite にした。Redis を使ってほしい？", ["SQLite", "Redis"]),
            "決定",
        )

    # 6. 却下側の印 (i) は疑の文でも変わらない
    def test_rejection_marker_inside_question_keeps_decision(self):
        text = "A でなく B でいい？"
        self.assertTrue(any(m in text for m in DECISION_REJECT_MARKERS))
        self.assertEqual(self._kind(text, text, ["B"]), "決定")
        self.assertEqual(self._resolve([text], ["B"], sansho.as_window([_turn(text)]))[0], "決定")
        # 縁が無くても (i) だけで 決定
        self.assertEqual(self._resolve([text], ["zzz"])[0], "決定")

    # 7. `.` は語の中では切らない
    def test_dot_inside_a_word_does_not_split(self):
        for text, cond in (("config.yaml にした", "config.yaml"), ("v1.2 にした", "v1.2")):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text, text, [cond]), "決定")
        text = "DB は SQLite にした。これでいい？"
        self.assertEqual(self._kind(text, "DB は SQLite にした", ["SQLite"]), "決定")
        self.assertEqual(self._kind(text, "これでいい", ["いい"]), "性向")
        # 後ろが空白の . は文末
        self.assertEqual(self._kind("Use SQLite. Is it ok?", "Is it ok", ["ok"]), "性向")
        self.assertEqual(self._kind("Use SQLite. Is it ok?", "Use SQLite", ["SQLite"]), "決定")

    # 8. 改行は文の切れ目 (_n は改行を畳むので _n の前に切る)
    def test_line_break_splits_sentences(self):
        text = "SQLite にした\nどう思う？"
        self.assertEqual(self._kind(text, "SQLite にした", ["SQLite"]), "決定")
        self.assertEqual(self._kind(text, "どう思う？", ["どう"]), "性向")
        self.assertEqual(self._kind("SQLite にした\r\n\r\n  どう思う？", "SQLite にした", ["SQLite"]), "決定")
        # 改行のない同じ文は一続きの問い
        self.assertEqual(self._kind("SQLite にしたよね？", "SQLite にした", ["SQLite"]), "性向")

    # 9. 全角・半角の ？/?、閉じ括弧・引用符
    def test_full_and_half_width_and_closers(self):
        for q in ("？", "?"):
            with self.subTest(q=q):
                self.assertEqual(self._kind("DB は SQLite にした" + q, "DB は SQLite にした", ["SQLite"]), "性向")
        for tail in ("か？」", "か？\u300f", "か?)", "か？！", "か?!", "か？\u201d"):
            with self.subTest(tail=tail):
                text = "SQLite にした" + tail
                self.assertEqual(self._kind(text, "SQLite にした", ["SQLite"]), "性向", text)
        # 感嘆符・句点で終わる文は疑ではない
        for tail in ("！", "!", "。", "."):
            with self.subTest(tail=tail):
                self.assertEqual(self._kind("SQLite にした" + tail, "SQLite にした", ["SQLite"]), "決定")

    # 10. plant_precheck と imprint は同じ判定
    def test_plant_precheck_and_imprint_agree(self):
        from raiya import alaya as _alaya
        cases = (
            ("Maybe try Celery for jobs?", [("user", "Maybe try Celery for jobs?")], ["Celery"], "性向"),
            (self.DB_Q, [("user", "前に決めた DB")], ["DB"], "性向"),
            (self.THREE, [("user", self.THREE)], ["PostgreSQL", "FastAPI", "Celery"], "決定"),
        )
        for text, spans, conds, expected in cases:
            with self.subTest(text=text):
                turn = _turn(text, "", RAIYA)
                prop = {
                    "content": "決めたこと", "conditions": conds, "polarity": "無記", "kind": "決定",
                    "evidence": [{"source": src, "span": sp} for src, sp in spans],
                }
                decision = sansho.evaluate_proposal(prop, sansho.as_window([turn]))
                kind = _alaya._resolve_kind(prop, decision["spans"], decision["grounded_conditions"], sansho.as_window([turn]))[0]
                self.assertEqual(kind, expected)
                # plant_precheck も imprint も、ゲートが評価した窓をそのまま _resolve_kind に渡し、同じ kind を得る
                seen = []
                real = _alaya._resolve_kind

                def spy(*args, **kwargs):
                    out = real(*args, **kwargs)
                    seen.append((args[3] if len(args) > 3 else kwargs.get("window"), out[0]))
                    return out

                with mock.patch.object(_alaya, "_resolve_kind", spy):
                    self.assertIsNone(_alaya.plant_precheck([turn], prop))
                    self.store.imprint([turn], prop)
                self.assertEqual(len(seen), 2)
                for window, kind_seen in seen:
                    self.assertEqual(window, sansho.as_window([turn]))
                    self.assertEqual(kind_seen, expected)
                self.assertEqual(self.store.imprint([turn], prop)["kind"], expected)

    # 11. 文は ゲートが接地した根拠本文 (貼り付けを除いたもの) で切る
    def test_sentences_come_from_the_evidence_text_the_gate_used(self):
        from raiya.engine import _gate_window
        # user_text の末尾は ? だが、根拠本文 (grounding_text) に ? は無い: 根拠本文で判定 = 決定
        t = LedgerTurn(
            user_text="DB は SQLite にした?", assistant_text="", id=1, project=RAIYA,
            grounding_text="DB は SQLite にした",
        )
        prop = _proposal("DB は SQLite にした", ["SQLite"], "DB は SQLite にした", kind="決定")
        self.assertEqual(self.store.imprint(_gate_window([t]), prop)["kind"], "決定")
        # 逆: user_text に ? は無いが根拠本文は問い
        t2 = LedgerTurn(
            user_text="DB は SQLite にした", assistant_text="", id=2, project=RAIYA,
            grounding_text="DB は SQLite にした？",
        )
        self.assertEqual(self.store.imprint(_gate_window([t2]), prop)["kind"], "性向")
        # 貼り付けの中の ？ は根拠本文に無い (strip_pasted 済み)
        paste = '<pasted_content id="p1">これで合ってる？</pasted_content id="p1">'
        t3 = LedgerTurn(
            user_text="DB は SQLite にした" + paste, assistant_text="", id=3, project=RAIYA,
            grounding_text="DB は SQLite にした",
        )
        self.assertEqual(self.store.imprint(_gate_window([t3]), prop)["kind"], "決定")
        # 根拠なし ("") は span が接地しない = 遍計所執 (user_text に戻さない)
        t4 = LedgerTurn(user_text="DB は SQLite にした", assistant_text="", id=4, project=RAIYA, grounding_text="")
        self.assertEqual(self.store.imprint(_gate_window([t4]), prop)["decision"], "遍計所執")

    # span が窓の user の本文のどこかで疑の文の外に出ていれば、その出現で判定する (統合の窓は複数往復が普通)
    def test_any_occurrence_outside_a_question_counts(self):
        win = lambda *texts: sansho.as_window([_turn(t) for t in texts])   # noqa: E731
        span = "SQLite で行く"
        # 問いの後の往復で確定
        self.assertEqual(self._resolve([span], ["SQLite"], win("SQLite で行く？", "やはり SQLite で行く。"))[0], "決定")
        # 同じ往復の中で問いの後に確定
        one = "SQLite で行く？やはり SQLite で行く。"
        self.assertEqual(self._kind(one, span, ["SQLite"]), "決定")
        # 確定が先、問いが後
        self.assertEqual(self._kind("SQLite にした。SQLite にした？", "SQLite にした", ["SQLite"]), "決定")
        self.assertEqual(self._kind("SQLite にした？やはり SQLite にした。", "SQLite にした", ["SQLite"]), "決定")
        # どの往復でも疑の文の中だけなら 性向
        self.assertEqual(self._resolve([span], ["SQLite"], win("SQLite で行く？", "SQLite で行く？"))[0], "性向")
        self.assertEqual(self._kind("SQLite で行く？SQLite で行く？", span, ["SQLite"]), "性向")
        # 疑の外の出現が好みの印で外れ、縁の無い出現は数えない: 出現ごとでなく 残りごとに判定
        self.assertEqual(self._resolve(["SQLite を使ってほしい"], ["SQLite"], win("SQLite を使ってほしい。", "SQLite を使ってほしい？"))[0], "性向")
        # assistant の本文の出現は数えない
        window = sansho.as_window([_turn("SQLite で行く？", "SQLite で行く。")])
        self.assertEqual(self._resolve([span], ["SQLite"], window)[0], "性向")
        # 実際の imprint (assistant の断定は依他起の span になり、user の疑の文の外には出ない)
        _t, _p, r = self._imp("SQLite で行く？", [("user", span)], ["SQLite"], assistant_text="SQLite で行く。")
        self.assertEqual(r["kind"], "性向")

    def test_bool_turn_index_in_multi_turn_window(self):
        # 同じ project の往復の中ではどの user の本文でも判定するので、turn_index が何番目を指しても 決定
        # (bool は使えない index として窓の全往復を読む)
        window = sansho.as_window([_turn("SQLite にした"), _turn("SQLite にした？")])
        for idx in (True, False, 1, 0, None):
            item = {"span": "SQLite にした", "sansho": "圓成實", "turn_index": idx}
            self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["SQLite"], window)[0], "決定", idx)
        # 逆順 (疑の往復が先) でも同じ
        window = sansho.as_window([_turn("SQLite にした？"), _turn("SQLite にした")])
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [{"span": "SQLite にした", "sansho": "圓成實", "turn_index": True}], ["SQLite"], window)[0], "決定")
        # 2 往復の imprint でも同じ
        turns = [_turn("SQLite にした？", "", RAIYA, 1), _turn("SQLite にした", "", RAIYA, 2)]
        prop = _proposal("決めた", ["SQLite"], "SQLite にした", kind="決定")
        self.assertEqual(self.store.imprint(turns, prop)["kind"], "決定")

    # 語の途中の . / ? は文末でない (疑の文の中でも)
    def test_dot_in_word_inside_question_sentence(self):
        self.assertEqual(self._kind("config.yaml にする？", "config.yaml にする", ["config"]), "性向")
        self.assertEqual(self._kind("config.yaml にする。v1.2 は？", "config.yaml にする", ["config"]), "決定")

    def test_mid_token_question_mark_is_not_a_sentence_end(self):
        for text, cond in (
            ("DB は https://x.io/a?b=1 を使う", "x.io"),
            ("foo?.bar を採用した", "foo"),
            ("DB は https://x.io/?b=1 を使う", "x.io"),
            ("API は /search/?q=1 にした", "search"),
            ("foo()?.bar を採用した", "foo"),
            ("a[0]?.b を採用した", "採用"),
            ("a?b を採用した", "採用"),
            ("x?_y?-z?/w?&v?=u を採用した", "採用"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text, text, [cond]), "決定")
                _n, pieces = alaya._sentence_pieces(text)
                self.assertFalse(any(q for _lo, _hi, q in pieces))
        # 後ろが CJK・空白・文の終わり・閉じは文末の ?
        for text in ("これでいい？次へ", "ok? next", "ok?", "ok?)", "ok?」次へ"):
            with self.subTest(text=text):
                _n, pieces = alaya._sentence_pieces(text)
                self.assertTrue(pieces[0][2], text)
        self.assertEqual(self._kind("SQLite にした？次へ", "SQLite にした", ["SQLite"]), "性向")
        # 語の途中の ? の後ろの疑の文は疑のまま
        self.assertEqual(self._kind("see a?b=1 now. SQLite にした？", "SQLite にした", ["SQLite"]), "性向")

    def test_question_with_trailing_dots(self):
        # 「?」の後の「.」が英数字に続かなければ語の途中でない: 文末の ? = 疑の文
        for text in ("SQLite にする？…", "SQLite にする?.", "Is SQLite ok?...", "SQLite にする?..."):
            with self.subTest(text=text):
                span = "Is SQLite ok" if text.startswith("Is") else "SQLite にする"
                self.assertEqual(self._kind(text, span, ["SQLite"]), "性向", text)
        # 英数字が続く「.」は語の途中 (`foo?.bar`)
        self.assertEqual(self._kind("foo?.bar を採用した", "foo?.bar を採用した", ["foo"]), "決定")

    def test_fullwidth_question_before_ascii(self):
        # 元が全角の ？ は常に文末 (NFKC 後に半角になっても後ろが ASCII でも語の途中にしない)
        for text, span in (
            ("SQLite でいい？Redis は入れる。", "SQLite でいい"),
            ("SQLite はどう？OK", "SQLite はどう"),
            ("DB はどうする？SQLite でいい。", "DB はどうする"),
            ("SQLite にした？x=1", "SQLite にした"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text, span, ["SQLite", "DB"]), "性向", text)
        # 断定の側は残る
        self.assertEqual(self._kind("DB はどうする？SQLite でいい。", "SQLite でいい", ["SQLite"]), "決定")
        # 半角でも左が ASCII 英数字で右も語が続けば語の途中。左が CJK なら文末
        self.assertEqual(self._kind("SQLite?x=1 にした", "SQLite?x=1 にした", ["SQLite"]), "決定")
        self.assertEqual(self._kind("SQLite にした?x=1", "SQLite にした", ["SQLite"]), "性向")

    def test_cross_project_statement_does_not_rescue_a_question(self):
        # 別 project の発話では救わない (同依)。接地した往復は /p/a の問い
        a, b = "/p/a", "/p/b"
        window = sansho.as_window([_turn("Redis にする？", "", a, 1), _turn("Redis にする。", "", b, 2)])
        for idx, expected in ((0, "性向"), (1, "決定")):
            item = {"span": "Redis にする", "sansho": "圓成實", "turn_index": idx}
            self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["Redis"], window)[0], expected, idx)
        # turn_index が使えなければ全往復を読む
        item = {"span": "Redis にする", "sansho": "圓成實", "turn_index": None}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["Redis"], window)[0], "決定")
        # imprint: 問いの往復 (/p/a) だけに接地した提案は 性向。project 跨ぎの窓は別の理由で却下される
        prop = _proposal("Redis にする", ["Redis"], "Redis にする？", kind="決定")
        r = self.store.imprint([_turn("Redis にする？", "", a, 1)], prop)
        self.assertEqual(r["kind"], "性向")
        # project を文字列として比べる: str でない project は None 扱い (_window_project と同じ)
        window = sansho.as_window([_turn("Redis にする？", "", 7, 1), _turn("Redis にする。", "", None, 2)])
        item = {"span": "Redis にする", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["Redis"], window)[0], "決定")

    def test_same_project_multi_turn_window_rescues_a_question(self):
        p = "/p/a"
        window = sansho.as_window([_turn("Redis にする？", "", p, 1), _turn("Redis にする。", "", p, 2)])
        item = {"span": "Redis にする", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["Redis"], window)[0], "決定")
        # imprint でも (どちらの往復に接地したかによらず 決定)
        prop = _proposal("Redis にする", ["Redis"], "Redis にする", kind="決定")
        self.assertEqual(self.store.imprint(window, prop)["kind"], "決定")

    def test_span_cut_by_questions_leaves_no_remainder(self):
        # 往復ごとの最初の出現で残りを取る: `foo bar` は問い `foo bar?` の中で、`foo` `bar?` に割れて残らない
        text = "foo bar?\nfoo\nbar?"
        self.assertEqual(self._kind(text, "foo bar", ["foo"]), "性向")
        # 残った文はつないで 1 つの出現にならない (つなぎ目は改行)
        text = "foo bar?\nfoo\nbaz?\nbar"
        self.assertEqual(self._kind(text, "foo bar", ["foo"]), "性向")

    def test_kept_sentences_do_not_join_into_a_match(self):
        text = "foo bar?\nfoo\nbaz?\nbar"
        n, pieces = alaya._sentence_pieces(text)
        kept = "\n".join(n[lo:hi] for lo, hi, q in pieces if not q)
        self.assertNotIn("foo bar", kept)
        self.assertEqual(self._kind(text, "foo bar", ["foo"]), "性向")

    def test_no_window_span_keeps_line_breaks(self):
        # 窓なし: span 自身を文に分ける。改行が文の切れ目として残る (_n の後では畳まれる)
        item = {"span": "SQLite にした\nどう？", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["SQLite"])[0], "決定")
        item = {"span": "SQLite にしたどう？", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["SQLite"])[0], "性向")

    def test_bare_turn_window_falls_back(self):
        # 窓が Turn のまま (list / tuple でない): 往復を読まず span 自身を分ける。例外にしない
        item = {"span": "SQLite", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["SQLite"], sansho.Turn("SQLite?", ""))[0], "決定")
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["SQLite"], "SQLite?")[0], "決定")

    def test_non_str_user_text_is_skipped(self):
        item = {"span": "SQLite", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["SQLite"], (sansho.Turn(123, ""),))[0], "決定")

    def test_clean_occurrence_decides_over_remainders(self):
        # 窓に span 全体が疑の文の外に出る往復があれば、その span 全体で判定する (好みの印で 性向)。
        # 別の往復の最初の出現の残り (`foo` だけ) が (ii) を満たしても、それでは救わない
        window = sansho.as_window([_turn("foo\nを使ってほしい？"), _turn("foo を使ってほしい。")])
        item = {"span": "foo を使ってほしい", "sansho": "圓成實", "turn_index": 1}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["foo"], window)[0], "性向")

    def test_remainder_found_only_in_a_later_turn(self):
        # 往復ごとに最初の出現で残りを取り、どれかで (ii) が立てばよい
        window = sansho.as_window([_turn("foo bar?"), _turn("foo\nbar?")])
        item = {"span": "foo bar", "sansho": "圓成實", "turn_index": 0}
        self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["foo"], window)[0], "決定")

    def test_halfwidth_question_before_cjk(self):
        self.assertEqual(self._kind("SQLite にした?次へ", "SQLite にした", ["SQLite"]), "性向")

    def test_halfwidth_question_after_cjk_before_ascii(self):
        # 左が CJK の半角 ? は語の途中でない
        self.assertEqual(self._kind("SQLite はどう?OK", "SQLite はどう", ["SQLite"]), "性向")
        self.assertEqual(self._kind("SQLite でいい?Redis は入れる。", "SQLite でいい", ["SQLite"]), "性向")
        # 左が ASCII 英数字でも右が語でなければ文末
        self.assertEqual(self._kind("Is it SQLite? Redis も", "Is it SQLite", ["SQLite"]), "性向")

    def test_unusable_turn_index_reads_all_turns(self):
        a, b = "/p/a", "/p/b"
        window = sansho.as_window([_turn("Redis にする？", "", a, 1), _turn("Redis にする。", "", b, 2)])
        for idx in (False, -2, 9, None, "0"):
            item = {"span": "Redis にする", "sansho": "圓成實", "turn_index": idx}
            self.assertEqual(alaya._resolve_kind({"kind": "決定"}, [item], ["Redis"], window)[0], "決定", idx)

    def test_exclaim_then_question_terminal_run(self):
        for text in ("本当に SQLite にした!?", "SQLite にした⁉", "SQLite にした?!", "SQLite にした！？", "SQLite にした？」"):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text, "SQLite にした", ["SQLite"]), "性向", text)

    def test_span_absent_from_grounded_text_falls_back(self):
        # 窓の user の本文に span が無い (手で組んだ span): span 自身を文に分ける
        window = sansho.as_window([_turn("next? something else")])
        self.assertEqual(self._resolve(["foo SQLite"], ["SQLite"], window)[0], "決定")
        self.assertEqual(self._resolve(["foo SQLite?"], ["SQLite"], window)[0], "性向")

    def test_empty_user_text_falls_back(self):
        window = sansho.as_window([sansho.Turn("", "")])
        self.assertEqual(self._resolve(["DB は SQLite にした"], ["SQLite"], window)[0], "決定")
        self.assertEqual(self._resolve(["DB は SQLite にした？"], ["SQLite"], window)[0], "性向")
        # 空白だけの span・非 str の本文でも落ちない
        self.assertEqual(self._resolve([" "], ["SQLite"], window)[0], "性向")
        self.assertEqual(self._resolve(["SQLite"], ["SQLite"], sansho.as_window([_turn(None)]))[0], "決定")

    def test_question_cannot_supersede_or_reinforce_decision(self):
        def seeds():
            return {x["id"]: (x["content"], x["strength"], x.get("superseded_by")) for x in self.store.state["seeds"]}

        _t, _p, r0 = self._imp("DB は SQLite にした", [("user", "DB は SQLite にした")], ["SQLite"], content="DB は SQLite にした")
        self.assertEqual(r0["seed"]["kind"], "決定")
        base = seeds()[r0["seed"]["id"]]
        sid = r0["seed"]["id"]
        for action, text, cond, target in (
            ("supersede", "DB は Postgres にする？", "Postgres", sid),
            ("reinforce", "DB は SQLite にした？", "SQLite", sid),
            (None, "DB は SQLite にした？", "SQLite", None),   # 暗黙の併合
        ):
            with self.subTest(action=action):
                turn = _turn(text, "", RAIYA)
                prop = _proposal(text.rstrip("？"), [cond], text.rstrip("？"), kind="決定", action=action, target_id=target)
                r = self.store.imprint([turn], prop)
                # 問い文 spec §9 L': 性向 に落ちた問いは縁が疑の文にしか無く、L で記録のみ (別の種子も植わらない)
                self.assertEqual(r["decision"], "reject")
                self.assertEqual(r["kind"], "性向")
                self.assertIn(DECISION_FORM_MISSING, r["reason"])
                self.assertIn(alaya.DISPOSITION_NEEDS_FORM_IN_USER_SPAN, r["reason"])
                self.assertIsNone(r["seed"])
                self.assertFalse(r.get("superseded"))
                self.assertEqual(seeds()[sid], base)   # 決定 の内容・強度・supersede は不変
        # 疑の文でなければ従来どおり supersede できる
        turn = _turn("DB は Postgres にする", "", RAIYA)
        r = self.store.imprint([turn], _proposal("DB は Postgres にする", ["Postgres"], "DB は Postgres にする", kind="決定", action="supersede", target_id=sid))
        self.assertEqual(r["kind"], "決定")
        self.assertTrue(r["superseded"])
        self.assertEqual(r["seed"]["id"], sid)   # 圓成實 同士の supersede は対象の種子を置き換える
        self.assertEqual(seeds()[sid][0], "DB は Postgres にする")

    # 文の切り口が `_n` と食い違わない (改行・CJK 隣接空白・先頭末尾の空白)
    def _assert_pieces_tile(self, n, pieces):
        """文は `n` の中で順に並び、文と文の間は空白 1 個か無し、文は空白で始まらず終わらない。"""
        prev = 0
        for lo, hi, _q in pieces:
            self.assertIn(n[prev:lo], ("", " "))
            self.assertTrue(prev <= lo < hi <= len(n))
            self.assertEqual(n[lo:hi], n[lo:hi].strip(" "))
            prev = hi
        self.assertIn(n[prev:], ("", " "))

    def test_sentence_pieces_fuzz_tiles_the_gate_normalized_text(self):
        import random
        rnd = random.Random(20261031)
        alphabet = list("ab 1\n\r\t\u3000。.!?？！．」)\"'日本語かなＡＢ=_-/&\u00a0\u2028\u0301\ufffc\u200b⁉…") + ["e\u0301", "\u1100", "\u1161"]
        for _ in range(3000):
            text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 14)))
            n, pieces = alaya._sentence_pieces(text)
            self.assertEqual(n, sansho._n(text))
            self._assert_pieces_tile(n, pieces)

    def test_sentence_pieces_parity_with_gate_normalization(self):
        _alaya = alaya
        for text in ("", "  日本 語? 」 foo", "x\n\n", "日本語 \n x. y", "a.b. c", "?", "ＳＱＬｉｔｅ にした？ 次は\nＯＫ。", "a ？ b"):
            with self.subTest(text=text):
                n, pieces = _alaya._sentence_pieces(text)
                self.assertEqual(n, sansho._n(text))
                self._assert_pieces_tile(n, pieces)


class TestSeedProject(_StoreCase):
    """E2 / §4.2: 種子の project は接地した往復の project 集合から決まる。"""

    def test_project_from_grounded_turn(self):
        r = self.plant("raiya の所在", ["raiya"], project=RAIYA)
        self.assertEqual(r["project"], RAIYA)
        self.assertEqual(r["seed"]["project"], RAIYA)

    def test_plain_turn_gives_global_seed(self):
        r = self.store.imprint(sansho.Turn(user_text="全域の話 p/ 全域", assistant_text=""), _proposal("全域の話", ["全域"], "全域の話 p/", kind="所在"))
        self.assertIsNone(r["seed"]["project"])

    def test_project_straddle_is_rejected(self):
        window = [_turn("raiya で決めた 共通", "", RAIYA, 1), _turn("古事記で決めた 共通", "", KOJIKI, 2)]
        p = {
            "content": "両方で決めた",
            "conditions": ["共通"],
            "polarity": "無記",
            "kind": "所在",
            "evidence": [{"source": "user", "span": "raiya で決めた"}, {"source": "user", "span": "古事記で決めた"}],
        }
        r = self.store.imprint(window, p)
        self.assertEqual(r["decision"], "reject")
        self.assertIn("project 跨ぎ", r["reason"])
        self.assertEqual(self.store.list_seeds(), [])

    def test_none_and_project_mixed_is_also_a_straddle(self):
        window = [_turn("ここで決めた 共通", "", None, 1), _turn("あちらで決めた 共通", "", RAIYA, 2)]
        p = {
            "content": "決めた",
            "conditions": ["共通"],
            "polarity": "無記",
            "kind": "所在",
            "evidence": [{"source": "user", "span": "ここで決めた"}, {"source": "user", "span": "あちらで決めた"}],
        }
        self.assertEqual(self.store.imprint(window, p)["decision"], "reject")

    def test_only_grounded_turns_count_for_project(self):
        # 2 往復の窓でも接地したのが 1 往復だけなら、その往復の project になる。
        window = [_turn("無関係 共通", "", KOJIKI, 1), _turn("raiya で決めた p/ 共通", "", RAIYA, 2)]
        r = self.store.imprint(window, _proposal("決めた", ["共通"], "raiya で決めた p/ 共通", kind="所在"))
        self.assertEqual(r["seed"]["project"], RAIYA)


class TestMergeWithoutPolarity(_StoreCase):
    """E3 / §5.1: 本番 fixture の polarity 揺れペア (#1/#8, #24/#47, #27/#29) が畳まれ、#4/#5 (クラス違い) は 2 件のまま。"""

    def _plant_fixture(self, seed_id, project=None, polarity=None, source="user"):
        seed = _fixture_seed(seed_id)
        return self.plant(seed["content"], seed["conditions"], project=project, kind="所在",
                          polarity=polarity or seed["polarity"], source=source)

    def test_e3_same_content_different_polarity_merges_when_conditions_overlap(self):
        # (24,47) / (27,29) は同文でも縁が 1 語も重ならない: #104 (c) 以後は併合しない (下のテストで pin)。
        for a, b in ((1, 8),):
            with self.subTest(pair=(a, b)):
                store_dir = tempfile.mkdtemp()
                self.store = alaya_store_at(os.path.join(store_dir, "alaya.json"), os.path.join(store_dir, "imprint_log.json"))
                r1 = self._plant_fixture(a)
                r2 = self._plant_fixture(b)
                self.assertNotEqual(_fixture_seed(a)["polarity"], _fixture_seed(b)["polarity"])
                self.assertFalse(r2["is_new"])
                self.assertEqual(r2["seed"]["id"], r1["seed"]["id"])
                self.assertEqual(r2["seed"]["polarity"], _fixture_seed(a)["polarity"])  # 既存の値を保つ
                self.assertEqual(len(self.store.list_seeds()), 1)
                shutil.rmtree(store_dir, ignore_errors=True)

    def test_e3_same_content_with_disjoint_conditions_no_longer_merges_after_104(self):
        for a, b in ((24, 47), (27, 29)):
            with self.subTest(pair=(a, b)):
                store_dir = tempfile.mkdtemp()
                self.store = alaya_store_at(os.path.join(store_dir, "alaya.json"), os.path.join(store_dir, "imprint_log.json"))
                self._plant_fixture(a)
                r2 = self._plant_fixture(b)
                self.assertTrue(r2["is_new"])
                self.assertEqual(len(self.store.list_seeds()), 2)
                shutil.rmtree(store_dir, ignore_errors=True)

    def test_e3_reinforce_target_with_other_polarity_is_not_demoted_to_new(self):
        r1 = self._plant_fixture(1)
        seed = _fixture_seed(8)
        turn = _turn("{} {}".format(seed["content"], " ".join(seed["conditions"])))
        r2 = self.store.imprint([turn], _proposal(seed["content"], seed["conditions"], seed["content"], kind="所在",
                                                   polarity="善", action="reinforce", target_id=r1["seed"]["id"]))
        self.assertTrue(r2.get("reinforced"))
        self.assertNotIn("reinforce 不可", r2["reason"])
        self.assertEqual(r2["seed"]["strength"], 2)

    def test_e3_cross_class_same_content_stays_two_seeds(self):
        r4 = self._plant_fixture(4)
        r5 = self._plant_fixture(5, source="assistant")
        self.assertEqual((r4["sansho"], r5["sansho"]), ("圓成實", "依他起"))
        self.assertTrue(r5["is_new"])
        self.assertEqual(len(self.store.list_seeds()), 2)

    def test_e3_different_kind_is_not_merged(self):
        self.plant("本番 state は ~/.raiya にある", ["state"], kind="所在")
        r = self.plant("本番 state は ~/.raiya にある", ["state"], kind=None)  # 性向
        self.assertTrue(r["is_new"])

    def test_e3_different_project_is_not_merged(self):
        self.plant("本番 state は ~/.raiya にある", ["state"], project=RAIYA)
        r = self.plant("本番 state は ~/.raiya にある", ["state"], project=KOJIKI)
        self.assertTrue(r["is_new"])
        self.assertEqual(len(self.store.list_seeds()), 2)

    def test_e3_reinforce_target_in_other_project_falls_back_to_new(self):
        r1 = self.plant("本番 state は ~/.raiya にある", ["state"], project=RAIYA)
        turn = _turn("本番 state は ~/.raiya にある state", "", KOJIKI)
        r2 = self.store.imprint([turn], _proposal("本番 state は ~/.raiya にある", ["state"], "本番 state は ~/.raiya にある",
                                                   kind="所在", action="reinforce", target_id=r1["seed"]["id"]))
        self.assertTrue(r2["is_new"])
        self.assertIn("kind/project", r2["reason"])
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["strength"], 1)


class TestHorizontalSupersede(_StoreCase):
    """E4 / §5.2: supersede は target に加え、同 project ∧ 同 kind ∧ 縁 2 語共有の種子に superseded_by を立てる。"""

    # #26 の縁は spec §5.2 どおり主題語 (沼矛, 神器) に置き換えるが、#20 と縁が完全に同じだと縁 Jaccard
    # 1.0 で #20 に併合されてしまう (polarity 違いが併合を止めていた改修4 までとの差)。横断 supersede
    # を試すために #26 には固有の縁を足して類似度を閾値未満 (2/5) にする。
    COND26 = ("沼矛", "神器", "資材", "矛", "扱い")

    def _plant_20_26(self, cond26=COND26):
        s20 = _fixture_seed(20)
        s26 = _fixture_seed(26)
        r20 = self.plant(s20["content"], s20["conditions"], project=KOJIKI, kind="決定")
        r26 = self.plant(s26["content"], list(cond26), project=KOJIKI, kind="決定")
        self.assertEqual((r20["seed"]["kind"], r26["seed"]["kind"]), ("決定", "決定"))
        self.assertTrue(r26["is_new"])
        return r20["seed"]["id"], r26["seed"]["id"]

    def _supersede_20(self, id20, project=KOJIKI, content="沼矛は神器ではなく資材として扱う", source="user"):
        text = "{} 沼矛 神器".format(content)
        if source == "mixed":  # 依他起 だが 却下側の印を持つ user の span もある (H 後に 決定 が依他起で植わる形)
            turn = _turn(text, ASSISTANT_ACK, project)
            prop = _proposal(content, ["沼矛", "神器"], content, source="user", kind="決定",
                             action="supersede", target_id=id20)
            prop["evidence"].append({"source": "assistant", "span": ASSISTANT_ACK})
            return self.store.imprint([turn], prop)
        turn = _turn(text, "", project) if source == "user" else _turn("沼矛 神器", text, project)
        return self.store.imprint([turn], _proposal(content, ["沼矛", "神器"], content, source=source, kind="決定",
                                                     action="supersede", target_id=id20))

    def test_e4_two_shared_conditions_hide_the_other_seed(self):
        id20, id26 = self._plant_20_26()
        r = self._supersede_20(id20)
        self.assertTrue(r.get("superseded"))
        self.assertEqual(r["also_superseded"], [id26])
        s26 = self.store.get_by_ids([id26])[0]
        self.assertEqual(s26["superseded_by"], id20)
        self.assertEqual(s26["history"][-1]["reason"], "superseded_by")
        self.assertEqual(s26["history"][-1]["superseded_by"], id20)
        self.assertNotIn(id26, [s["id"] for s in self.store.index(project=KOJIKI)])
        self.assertNotIn(id26, [s["id"] for s in self.store.index(project=KOJIKI, purpose="consolidate")])

    def test_e4_superseded_seed_is_neither_deleted_nor_weakened(self):
        id20, id26 = self._plant_20_26()
        before = self.store.get_by_ids([id26])[0]
        self._supersede_20(id20)
        after = self.store.get_by_ids([id26])[0]
        self.assertEqual((after["content"], after["strength"], after["conditions"]),
                         (before["content"], before["strength"], before["conditions"]))
        self.assertEqual(len(self.store.list_seeds()), 2)

    def test_e4_one_shared_condition_is_not_enough(self):
        id20, id26 = self._plant_20_26(cond26=("資材", "矛"))  # 本番どおりの縁: 共有 0 語
        r = self._supersede_20(id20)
        self.assertEqual(r["also_superseded"], [])
        self.assertIsNone(self.store.get_by_ids([id26])[0]["superseded_by"])
        id_one = self.plant("沼矛は矛", ["沼矛", "資材"], project=KOJIKI, kind="決定")["seed"]["id"]  # 共有 1 語
        self.store.imprint([_turn("沼矛は神器ではなく資材として扱う 沼矛 神器", "", KOJIKI)],
                           _proposal("沼矛は神器ではなく資材として扱う", ["沼矛", "神器"], "沼矛は神器ではなく資材として扱う",
                                     kind="決定", action="supersede", target_id=id20))
        self.assertIsNone(self.store.get_by_ids([id_one])[0]["superseded_by"])

    def test_e4_shared_match_is_exact_not_substring_and_case_sensitive(self):
        id20, _ = self._plant_20_26(cond26=("矛", "神器", "資材", "扱い"))  # 「矛」は「沼矛」の部分文字列 → 共有に数えない
        r = self._supersede_20(id20)
        self.assertEqual(r["also_superseded"], [])
        id_case = self.plant("Hooks の決定ではなく台帳の決定", ["Hooks", "Ledger"], project=RAIYA, kind="決定")["seed"]["id"]
        id_tgt = self.plant("hooks は台帳に書くのではなく読む", ["hooks", "ledger"], project=RAIYA, kind="決定")["seed"]["id"]
        self.store.imprint([_turn("hooks は台帳を読むのではなく書く hooks ledger", "", RAIYA)],
                           _proposal("hooks は台帳を読むのではなく書く", ["hooks", "ledger"], "hooks は台帳を読むのではなく書く",
                                     kind="決定", action="supersede", target_id=id_tgt))
        self.assertIsNone(self.store.get_by_ids([id_case])[0]["superseded_by"])

    def test_e4_etaki_cannot_hide_enjoujitsu(self):
        id20, id26 = self._plant_20_26()
        # 依他起の提案: target #20 (圓成實) も置換できず new になる = 横断も起きない
        # H 後: assistant だけの 決定 は 性向 に読み替わり、kind が違うので #20 を置換せず #26 も隠さない
        r = self._supersede_20(id20, source="assistant")
        self.assertFalse(r.get("superseded"))
        self.assertEqual(r["seed"]["kind"], "性向")
        self.assertIsNone(self.store.get_by_ids([id26])[0]["superseded_by"])
        # 印が user の span にある 依他起 (混在) の 決定 は new で植わる (E は supersede/reinforce の越境だけ)
        turn0 = _turn("沼矛は神器ではなく資材 沼矛 神器", ASSISTANT_ACK, KOJIKI)
        prop0 = _proposal("沼矛は神器ではなく資材", ["沼矛", "神器"], "沼矛は神器ではなく資材", kind="決定")
        prop0["evidence"].append({"source": "assistant", "span": ASSISTANT_ACK})
        r = self.store.imprint([turn0], prop0)
        self.assertEqual((r["seed"]["kind"], r["seed"]["sansho"]), ("決定", "依他起"))
        self.assertIsNone(self.store.get_by_ids([id26])[0]["superseded_by"])
        # 依他起の target に対する supersede は成立するが、圓成實の #26 は隠せない
        # (#78 P5: 上で植わった依他起 H は #26 に隠されるので、同じ依他起の再提案は双子照合になり積まれない。
        #  その twin_match = H を supersede の target にする)
        self.assertTrue(r["is_new"])
        turn = _turn("沼矛は武器ではなく神器 沼矛 神器 武器", ASSISTANT_ACK, KOJIKI)
        prop = _proposal("沼矛は武器ではなく神器", ["沼矛", "神器", "武器"], "沼矛は武器ではなく神器", kind="決定")
        prop["evidence"].append({"source": "assistant", "span": ASSISTANT_ACK})
        again = self.store.imprint([turn], prop)
        self.assertEqual(again["decision"], "双子照合")
        id_e = again["twin_match"]
        self.assertEqual(id_e, r["seed"]["id"])
        r2 = self._supersede_20(id_e, source="mixed")
        self.assertTrue(r2.get("superseded"))
        self.assertNotIn(id26, r2["also_superseded"])

    def test_e4_other_project_and_other_kind_are_untouched(self):
        id20, _ = self._plant_20_26()
        other_project = self.plant("沼矛と神器の所在", ["沼矛", "神器"], project=RAIYA, kind="決定", span="沼矛と神器の所在ではなく")["seed"]["id"]
        other_kind = self.plant("沼矛と神器の所在", ["沼矛", "神器"], project=KOJIKI, kind="所在")["seed"]["id"]
        self._supersede_20(id20)
        self.assertIsNone(self.store.get_by_ids([other_project])[0]["superseded_by"])
        self.assertIsNone(self.store.get_by_ids([other_kind])[0]["superseded_by"])

    NOTE_UNFIT = "supersede 不可: target の kind/project が違うか superseded のため new として扱う"

    def _assert_supersede_falls_back_to_new(self, target_id, project):
        """target を触らずに新種子を 1 つ植え、判断ログに不可の理由が残ること。"""
        before = copy.deepcopy(self.store.list_seeds())
        content = "祭具は封印ではなく保管として扱う"  # 既存種子と縁が重ならない (類似併合に吸われない)
        r = self.store.imprint([_turn("{} 祭具 封印".format(content), "", project)],
                               _proposal(content, ["祭具", "封印"], content, kind="決定",
                                         action="supersede", target_id=target_id))
        self.assertTrue(r["is_new"])
        self.assertFalse(r.get("superseded"))
        self.assertNotEqual(r["seed"]["id"], target_id)
        after = self.store.list_seeds()
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual([s for s in after if s["id"] != r["seed"]["id"]], before)  # 既存種子は変わらない
        self.assertIn(self.NOTE_UNFIT, self.store.list_imprint_log(1)[0]["reason"])
        return r

    def test_e4_supersede_target_of_other_kind_becomes_new(self):
        target = self.plant("沼矛の所在は倉庫", ["沼矛", "倉庫"], project=KOJIKI, kind="所在")["seed"]["id"]
        self._assert_supersede_falls_back_to_new(target, KOJIKI)

    def test_e4_supersede_target_of_other_project_becomes_new(self):
        id20, _ = self._plant_20_26()  # KOJIKI の決定
        self._assert_supersede_falls_back_to_new(id20, RAIYA)

    def test_e4_supersede_target_already_superseded_becomes_new(self):
        id20, id26 = self._plant_20_26()
        self._supersede_20(id20)  # #26 が superseded になる
        self.assertEqual(self.store.get_by_ids([id26])[0]["superseded_by"], id20)
        self._assert_supersede_falls_back_to_new(id26, KOJIKI)

    def test_reinforce_with_missing_target_id_becomes_new(self):
        self._plant_20_26()
        for target_id in (None, 9999):
            with self.subTest(target_id=target_id):
                before = copy.deepcopy(self.store.list_seeds())
                tag, tag2, content = (("縁甲", "縁乙", "縁甲は縁乙ではなく保管として扱う") if target_id is None
                                      else ("縁丙", "縁丁", "縁丙案を採用し縁丁案は却下する"))  # 反復ごとに固有の縁 (前回の新種子へ併合されない)
                r = self.store.imprint([_turn("{} {} {}".format(content, tag, tag2), "", KOJIKI)],
                                       _proposal(content, [tag, tag2], content, kind="決定",
                                                 action="reinforce", target_id=target_id))
                self.assertTrue(r["is_new"])
                self.assertFalse(r.get("reinforced"))
                after = self.store.list_seeds()
                self.assertEqual(len(after), len(before) + 1)
                self.assertEqual([s for s in after if s["id"] != r["seed"]["id"]], before)
                self.assertIn("reinforce 不可: target_id", self.store.list_imprint_log(1)[0]["reason"])

    def test_superseded_seed_is_not_a_merge_or_reinforce_target(self):
        id20, id26 = self._plant_20_26()
        self._supersede_20(id20)
        s26 = _fixture_seed(26)
        r = self.plant(s26["content"], list(self.COND26), project=KOJIKI, kind="決定")  # #26 と完全一致の new
        self.assertTrue(r["is_new"])  # 隠れた種子へは併合されない (戻し = 新種子)
        self.assertNotEqual(r["seed"]["id"], id26)
        r2 = self.store.imprint([_turn("{} {}".format(s26["content"], " ".join(self.COND26)), "", KOJIKI)],
                                _proposal(s26["content"], list(self.COND26), s26["content"], kind="決定",
                                          action="reinforce", target_id=id26))
        self.assertIn("superseded", r2["reason"])


class TestResumePoint(_StoreCase):
    """§5.3 / §12.2: 再開点 は同 project に 1 件。新しい再開点は前を supersede (クラス不問、2026-10-22 改訂)。"""

    def test_second_resume_point_in_same_project_supersedes_first(self):
        r1 = self.plant("次 = §11 の裁定待ち", ["裁定"], project=RAIYA, kind="再開点")
        r2 = self.plant("次 = E7 の receipt", ["receipt"], project=RAIYA, kind="再開点")
        self.assertTrue(r2.get("superseded"))
        self.assertEqual(r2["seed"]["id"], r1["seed"]["id"])
        self.assertEqual(r2["seed"]["content"], "次 = E7 の receipt")
        self.assertEqual(r2["seed"]["history"][0]["content"], "次 = §11 の裁定待ち")
        self.assertIn("再開点", r2["reason"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.store.resume_point(RAIYA)["content"], "次 = E7 の receipt")

    def test_resume_points_in_different_projects_coexist(self):
        self.plant("次 = raiya", ["raiya"], project=RAIYA, kind="再開点")
        r = self.plant("次 = 古事記", ["古事記"], project=KOJIKI, kind="再開点")
        self.assertTrue(r["is_new"])
        self.assertEqual(self.store.resume_point(KOJIKI)["content"], "次 = 古事記")
        self.assertIsNone(self.store.resume_point("/elsewhere"))

    def test_etaki_resume_point_supersedes_enjoujitsu(self):
        """2026-10-22 に §12.2 を覆した: 再開点に限りクラスを問わず最新が既存を置換する (WM-3)。"""
        r1 = self.plant("次 = §11 の裁定待ち", ["裁定"], project=RAIYA, kind="再開点")
        r2 = self.plant("次 = 推定の続き", ["続き"], project=RAIYA, kind="再開点", source="assistant")
        self.assertTrue(r2.get("superseded"))
        self.assertEqual(r2["seed"]["id"], r1["seed"]["id"])
        live = [s for s in self.store.list_seeds() if s["kind"] == "再開点" and not s.get("superseded_by")]
        self.assertEqual(len(live), 1)
        seed = self.store.resume_point(RAIYA)
        self.assertEqual(seed["id"], r1["seed"]["id"])
        self.assertEqual(seed["content"], "次 = 推定の続き")
        self.assertEqual(seed["sansho"], "依他起")
        self.assertEqual(seed["history"][0]["content"], "次 = §11 の裁定待ち")
        self.assertEqual(seed["history"][0]["sansho"], "圓成實")

    def test_etaki_resume_point_supersedes_even_with_action_new_or_no_target(self):
        for kwargs in ({}, {"action": "new", "target_id": None}, {"action": "supersede", "target_id": 999}):
            with self.subTest(kwargs=kwargs):
                store = alaya_store_at(os.path.join(self.tmpdir, "a{}.json".format(len(str(kwargs)))),
                                   os.path.join(self.tmpdir, "l{}.json".format(len(str(kwargs)))))
                store.begin_new_life()
                r1 = store.imprint([_turn("次 = 裁定待ち 裁定", "", RAIYA)],
                                   _proposal("次 = 裁定待ち", ["裁定"], "次 = 裁定待ち", kind="再開点"))
                r2 = store.imprint([_turn("質問 続き", "次 = 推定の続き 続き", RAIYA)],
                                   _proposal("次 = 推定の続き", ["続き"], "次 = 推定の続き", source="assistant",
                                             kind="再開点", **kwargs))
                self.assertTrue(r2.get("superseded"))
                self.assertEqual(r2["seed"]["id"], r1["seed"]["id"])
                self.assertEqual(store.resume_point(RAIYA)["sansho"], "依他起")
                self.assertEqual(len(store.list_seeds()), 1)
                self.assertEqual(store.resume_point(RAIYA)["history"][0]["sansho"], "圓成實")

    def test_etaki_resume_point_does_not_touch_other_projects_resume_point(self):
        other = self.plant("次 = 古事記", ["古事記"], project=KOJIKI, kind="再開点")
        self.plant("次 = raiya", ["raiya"], project=RAIYA, kind="再開点")
        r = self.plant("次 = 推定の続き", ["続き"], project=RAIYA, kind="再開点", source="assistant")
        self.assertTrue(r.get("superseded"))
        kojiki = self.store.resume_point(KOJIKI)
        self.assertEqual(kojiki["id"], other["seed"]["id"])
        self.assertEqual(kojiki["content"], "次 = 古事記")
        self.assertEqual(kojiki["sansho"], "圓成實")
        self.assertEqual(kojiki["history"], [])

    def test_etaki_still_cannot_supersede_enjoujitsu_for_other_kinds(self):
        r1 = self.plant("A を採用し B は却下", ["採用"], project=RAIYA, kind="決定")
        r2 = self.store.imprint(
            [_turn("質問 採用", "C を採用し D は却下 採用", RAIYA)],
            _proposal("C を採用し D は却下", ["採用"], "C を採用し D は却下", source="assistant", kind="決定",
                      action="supersede", target_id=r1["seed"]["id"]))
        self.assertTrue(r2["is_new"])
        self.assertIn("越境", r2["reason"])
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["sansho"], "圓成實")
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["content"], "A を採用し B は却下")

    def test_cross_class_assistant_only_reinforce_onto_user_span_is_recorded_only(self):
        r1 = self.plant("A を採用し B は却下", ["採用"], project=RAIYA, kind="決定")
        r2 = self.store.imprint(
            [_turn("質問 採用", "C を採用し D は却下 採用", RAIYA)],
            _proposal("C を採用し D は却下", ["採用"], "C を採用し D は却下", source="assistant", kind="決定",
                      action="reinforce", target_id=r1["seed"]["id"]))
        self.assertIsNone(r2["is_new"])
        self.assertEqual(r2["user_span_match"], r1["seed"]["id"])
        self.assertEqual(len(self.store.list_seeds()), 1)
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["strength"], 1)

    def test_resume_point_is_not_germinated_and_not_in_index(self):
        self.plant("次 = §11 の裁定待ち", ["裁定"], project=RAIYA, kind="再開点")
        self.assertEqual(self.store.germinate("裁定の話", project=RAIYA), [])
        self.assertEqual(self.store.index(project=RAIYA), [])
        self.assertEqual(INDEX_KIND_LIMITS["再開点"], 0)


class TestGerminationByKind(_StoreCase):
    """E5 / §6.2: project が第一条件、性向は縁 2 語、superseded と再開点は発芽しない。"""

    def test_project_is_the_first_condition(self):
        self.plant("raiya の所在", ["raiya"], project=RAIYA)
        self.plant("全域の所在", ["raiya"])
        self.assertEqual([s["content"] for s in self.store.germinate("raiya について", project=KOJIKI)], ["全域の所在"])
        self.assertEqual(sorted(s["content"] for s in self.store.germinate("raiya について", project=RAIYA)),
                         ["raiya の所在", "全域の所在"])
        self.assertEqual([s["content"] for s in self.store.germinate("raiya について", project=None)], ["全域の所在"])

    def test_disposition_needs_two_conditions(self):
        self.plant("回答は日本語で簡潔にしてほしい", ["日本語", "簡潔"], kind="性向")
        self.assertEqual(self.store.germinate("日本語で"), [])
        self.assertEqual(len(self.store.germinate("日本語で簡潔に")), 1)
        self.assertEqual(len(self.store.germinate("日本語で", context_texts=["簡潔に頼む"])), 1)

    def test_disposition_excluded_when_include_false(self):
        self.plant("回答は日本語で簡潔にしてほしい", ["日本語", "簡潔"], kind="性向")
        self.plant("state は ~/.raiya にある", ["日本語", "簡潔"], kind="所在")
        fired = self.store.germinate("日本語で簡潔に", include_dispositions=False)
        self.assertEqual([s["kind"] for s in fired], ["所在"])

    def test_superseded_seed_does_not_germinate(self):
        id20 = self.plant("沼矛は神器ではなく資材", ["沼矛", "神器"], project=KOJIKI, kind="決定")["seed"]["id"]
        id26 = self.plant("沼矛は資材ではなく神器とする", ["沼矛", "神器", "資材", "矛", "扱い"], project=KOJIKI, kind="決定")["seed"]["id"]
        self.store.imprint([_turn("沼矛は資材であって神器ではなく 沼矛 神器", "", KOJIKI)],
                           _proposal("沼矛は資材であって神器ではなく", ["沼矛", "神器"], "沼矛は資材であって神器ではなく",
                                     kind="決定", action="supersede", target_id=id20))
        self.assertEqual([s["id"] for s in self.store.germinate("沼矛", project=KOJIKI)], [id20])
        self.assertEqual(self.store.get_by_ids([id26])[0]["superseded_by"], id20)


class TestIndexByKind(_StoreCase):
    """§6.3 / §12.5: 注入索引は kind 別上限・kind 別クラス、性向と再開点は 0。"""

    def test_inject_index_excludes_dispositions_and_includes_etaki_for_decision_kinds(self):
        self.plant("回答は日本語でしてほしい", ["日本語"], kind="性向")
        self.plant("state は ~/.raiya にある", ["state"], kind="所在")
        self.plant("統合は claude-cli で封じ込める", ["封じ込め"], kind="所在", source="assistant")
        index = self.store.index(project=None)
        self.assertEqual([(s["kind"], s["sansho"]) for s in index], [("所在", "圓成實"), ("所在", "依他起")])
        self.assertEqual(self.store.seed_count(), 2)
        consolidate = self.store.index(project=None, purpose="consolidate")
        self.assertEqual([s["kind"] for s in consolidate], ["所在", "所在", "性向"])

    def test_inject_index_per_kind_limit_and_order(self):
        # content は互いに 2-gram を共有しない 2 字の語 (似た文だと類似度 0.5 以上で併合されてしまう)。
        words = ["台帳", "設計", "受領", "隔離", "索引", "発芽", "統合", "封印", "翻訳", "監査", "配線", "星図", "暦法"]
        for i in range(INDEX_KIND_LIMITS["所在"] + 3):
            self.plant(words[i], ["L{:02d}".format(i)], kind="所在")
        for w in ("甲は乙のため", "丙は丁のため", "戊は己のため"):
            # 回避策 は訂正の印と対 (別の span 2 本) が要る (2026-10-29 spec)
            turn = _turn("{} 元に戻してください {}".format(w, w[0]), "", None)
            prop = _proposal(w, [w[0]], w, kind="回避策")
            prop["evidence"].append({"source": "user", "span": "元に戻してください"})
            self.assertEqual(self.store.imprint([turn], prop)["seed"]["kind"], "回避策")
        self.plant("A を採用し B は却下", ["decision"], kind="決定")
        index = self.store.index()
        kinds = [s["kind"] for s in index]
        self.assertEqual(kinds, ["決定"] + ["回避策"] * 3 + ["所在"] * INDEX_KIND_LIMITS["所在"])  # kind 順
        self.assertEqual(sum(INDEX_KIND_LIMITS.values()), INDEX_MAX)
        self.assertEqual(len(self.store.index(limit=2)), 2)


class TestInjectionFrame(unittest.TestCase):
    """E6 / §7: 注入は記録の提示。先頭行・性向の非注入・RAIYA_INJECT_DISPOSITIONS・再開点・project=None。"""

    def setUp(self):
        _fake_home(self)
        self.tmpdir = tempfile.mkdtemp()
        self.eng = MemoryEngine(state_dir=self.tmpdir)
        os.environ.pop(INJECT_DISPOSITIONS_ENV, None)

    def tearDown(self):
        os.environ.pop(INJECT_DISPOSITIONS_ENV, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _plant(self, content, conditions, kind, project=None, source="user"):
        text = "{} {}".format(content, " ".join(conditions))
        turn = _turn(text, "", project) if source == "user" else _turn("質問 " + " ".join(conditions), text, project)
        span = content
        if source == "user" and kind in ("所在", "性向"):
            span = text  # L (question-as-evidence §7): user の span に縁を含める (_StoreCase.plant と同じ)
            if kind == "所在":
                span = text = text + " p/"  # #149: 所在 の形は 區切りの語 (`/` を含む語)
                turn = _turn(text, "", project)
        return self.eng.imprint([turn], [_proposal(content, conditions, span, source=source, kind=kind)])[0]

    def test_narrative_starts_with_record_frame_and_has_no_ego_strength(self):
        self._plant("state は ~/.raiya にある", ["state"], "所在", project=RAIYA)
        r = self.eng.recall("state の話", project=RAIYA)
        self.assertTrue(r["narrative"].startswith(NARRATIVE_HEADER + "\n\n"))
        self.assertNotIn("我執", r["narrative"])
        self.assertNotIn("こう理解している", r["narrative"])
        self.assertIn("- 所在: state は ~/.raiya にある", r["narrative"])

    def test_dispositions_are_not_injected_by_default_but_with_env(self):
        self._plant("回答は日本語で簡潔にしてほしい", ["日本語", "簡潔"], "性向", project=RAIYA)
        r = self.eng.recall("日本語で簡潔に", project=RAIYA)
        self.assertEqual((r["seeds"], r["index"], r["narrative"]), ([], [], ""))
        os.environ[INJECT_DISPOSITIONS_ENV] = "1"
        r2 = self.eng.recall("日本語で簡潔に", project=RAIYA)
        self.assertEqual([s["kind"] for s in r2["seeds"]], ["性向"])
        self.assertIn("- 性向: 回答は日本語で簡潔にしてほしい", r2["narrative"])
        # 従来動作 = 発芽した性向の narrative と、性向の索引 (圓成實のみ) の両方 (§7.2 / §11.2)。
        # 発芽した性向は ① に出るので ③ では繰り返さない (layered-memory 計画 §2.3 の 発芽 の門)。
        self.assertEqual(r2["index"], [])
        self.assertNotIn("【記録の索引", r2["narrative"])
        r3 = self.eng.recall("別の話", project=RAIYA)
        self.assertEqual([s["kind"] for s in r3["index"]], ["性向"])  # 発芽しなくても索引には載る

    def test_resume_point_only_on_session_start(self):
        self._plant("次 = E7 の receipt", ["receipt"], "再開点", project=RAIYA)
        r = self.eng.recall("続き", project=RAIYA)
        self.assertIsNone(r["resume"])
        self.assertEqual(r["narrative"], "")
        r2 = self.eng.recall("続き", project=RAIYA, session_start=True)
        self.assertEqual(r2["resume"]["content"], "次 = E7 の receipt")
        self.assertEqual(r2["narrative"], NARRATIVE_HEADER + "\n\n- 再開点: 次 = E7 の receipt")
        self.assertIsNone(self.eng.recall("続き", project=KOJIKI, session_start=True)["resume"])

    def test_project_none_returns_global_seeds_only(self):
        self._plant("raiya の所在", ["raiya"], "所在", project=RAIYA)
        self._plant("全域の所在", ["raiya"], "所在")
        r = self.eng.recall("raiya について")
        self.assertEqual([s["content"] for s in r["seeds"]], ["全域の所在"])
        self.assertEqual(r["index"], [])  # 発芽した種子は ① に出るので ③ では繰り返さない
        self.assertEqual([s["content"] for s in self.eng.recall("別の話")["index"]], ["全域の所在"])
        r2 = self.eng.recall("raiya について", project=RAIYA)
        self.assertEqual(sorted(s["content"] for s in r2["seeds"]), ["raiya の所在", "全域の所在"])
        r3 = self.eng.recall("別の話", project=RAIYA)
        self.assertEqual(sorted(s["content"] for s in r3["index"]), ["raiya の所在", "全域の所在"])

    def test_germinated_seed_line_carries_kind_label_and_suitei(self):
        self._plant("A を採用し B は却下", ["decision"], "決定", project=RAIYA)
        self._plant("統合は claude-cli で封じ込める", ["封じ込め"], "所在", project=RAIYA, source="assistant")
        r = self.eng.recall("decision 封じ込め", project=RAIYA)
        self.assertIn(GERMINATED_HEADER, r["narrative"])
        self.assertIn("- 決定: A を採用し B は却下(強度1・要約: A を採用し B は却下)", r["narrative"])
        self.assertIn("- 所在(推定): 統合は claude-cli で封じ込める (強度1)", r["narrative"])  # 索引側 (発芽は閾値 2)


class TestConsolidationPromptParseAuditAndImprintProject(unittest.TestCase):
    """E1 後半 / §3.3 / §9: 統合プロンプトの文言 pin、kind の allowlist、audit の集計、imprint(project=)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prompt_asks_for_five_kinds_and_drops_one_off_exclusion(self):
        window = [_turn("raiya で決めた", "はい", RAIYA, 7), _turn("古事記の話", "ええ", None, 8)]
        index = [{"id": 3, "kind": "決定", "sansho": "圓成實", "content": "A を採用 (B は却下)", "conditions": ["A"]}]
        prompt = engine._build_consolidate_prompt(window, {"related": index, "related_total": len(index), "groups": [], "groups_total": 0})
        for kind in VALID_KINDS:
            self.assertIn(kind, prompt)
        self.assertIn('"kind"', prompt)
        self.assertNotIn("採用側と却下側の**両方**", prompt)   # #116: 片側の決定で却下側を作文させない
        self.assertIn("作文しない", prompt)
        self.assertIn("再取得できる事実", prompt)
        self.assertNotIn("一回限りの出来事。", prompt)  # 旧除外の撤去 (§3.3)
        self.assertIn("再取得できないものは抽出してください", prompt)
        self.assertIn("--- 往復 7 (project: {}) ---".format(RAIYA), prompt)
        self.assertIn("--- 往復 8 (project: (無し)) ---", prompt)
        self.assertIn("#3 [決定] [圓成實] A を採用 (B は却下) | 縁: A", prompt)
        self.assertIn("主題語", prompt)

    def test_propose_prompt_shares_kind_guide_with_consolidate(self):
        # 2026-10-17 改訂 (operator): CLI の propose も同じ 5 kind で出す (spec §3.3 の「変えない」を撤回)。
        prompt = engine._build_propose_prompt("u", "a")
        self.assertIn(engine.KIND_GUIDE, prompt)
        self.assertIn(engine.KIND_JSON_FIELD, prompt)

    def test_propose_prompt_shares_scope_guide_with_consolidate(self):
        # Issue #10: 「再取得できる事実は抽出しない」(§3.1) の範囲説明も両プロンプトで同じ文面を共有する。
        self.assertIn(engine.SCOPE_GUIDE, engine._build_propose_prompt("u", "a"))
        self.assertIn(engine.SCOPE_GUIDE, engine._build_consolidate_prompt([], {"related": [], "related_total": 0, "groups": [], "groups_total": 0}))

    def test_parse_consolidation_passes_kind_through_and_omits_when_absent(self):
        raw = json.dumps([
            {"content": "a", "kind": "決定", "evidence": {"source": "user", "span": "x"}},
            {"content": "b", "kind": 5, "evidence": {"source": "user", "span": "y"}},
            {"content": "c", "evidence": {"source": "user", "span": "z"}},
        ], ensure_ascii=False)
        proposals = engine._parse_consolidation(raw)
        self.assertEqual(proposals[0]["kind"], "決定")
        self.assertNotIn("kind", proposals[1])
        self.assertNotIn("kind", proposals[2])

    def test_consolidate_end_to_end_kind_and_project_reach_the_seed(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.record_turn("統合は --bare ではなく --setting-sources を採用した", "了解です", project=RAIYA)
        raw = json.dumps([{"kind": "決定", "action": "new", "target_id": None,
                           "content": "統合は --bare ではなく --setting-sources を採用",
                           "conditions": ["setting-sources"], "polarity": "無記",
                           "evidence": [{"source": "user", "span": "--bare ではなく --setting-sources を採用した"}]}],
                         ensure_ascii=False)
        with mock.patch("raiya.backend.call_llm", return_value=raw) as call:
            result = eng.consolidate()
        self.assertIn("(project: {})".format(RAIYA), call.call_args.args[0])
        seed = result["decisions"][0]["seed"]
        self.assertEqual((seed["kind"], seed["project"], seed["sansho"]), ("決定", RAIYA, "圓成實"))
        audit = eng.audit()
        self.assertEqual(audit["kind_counts"]["決定"], 1)
        self.assertEqual(audit["project_counts"], {RAIYA: 1})
        self.assertEqual(audit["superseded_count"], 0)

    def test_consolidate_index_covers_window_projects_and_global(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint([_turn("raiya の所在 p/ raiya", "", RAIYA)], [_proposal("raiya の所在", ["raiya"], "raiya の所在 p/", kind="所在")])
        eng.imprint([_turn("古事記の所在 p/ 古事記", "", KOJIKI)], [_proposal("古事記の所在", ["古事記"], "古事記の所在 p/", kind="所在")])
        eng.imprint(sansho.Turn(user_text="全域の所在 p/ 全域", assistant_text=""), [_proposal("全域の所在", ["全域"], "全域の所在 p/", kind="所在")])
        eng.imprint(sansho.Turn(user_text="日本語で 日本語 丁寧", assistant_text=""), [_proposal("日本語でしてほしい", ["日本語", "丁寧"], "日本語で")])
        eng.record_turn("雑談 raiya 古事記 全域 日本語 丁寧", "雑談", project=RAIYA)
        with mock.patch("raiya.backend.call_llm", return_value="[]") as call:
            eng.consolidate()
        prompt = call.call_args.args[0]
        self.assertIn("raiya の所在", prompt)
        self.assertIn("全域の所在", prompt)
        self.assertIn("日本語でしてほしい", prompt)  # 統合の索引には性向も載る (§11.2)
        self.assertNotIn("古事記の所在", prompt)   # 窓に無い project の種子は見せない

    def test_engine_imprint_with_project_wraps_plain_turns(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        r = eng.imprint(sansho.Turn(user_text="state は ~/.raiya にある state", assistant_text=""),
                        [_proposal("state は ~/.raiya にある", ["state"], "state は ~/.raiya にある", kind="所在")],
                        project=RAIYA)[0]
        self.assertEqual(r["seed"]["project"], RAIYA)
        self.assertIsNone(r["seed"]["evidences"][0]["ledger_id"])  # 台帳外のスナップショットのまま
        r2 = eng.imprint([_turn("x p/ state", "", KOJIKI)], [_proposal("x", ["state"], "x p/ state", kind="所在")], project=RAIYA)[0]
        self.assertEqual(r2["seed"]["project"], KOJIKI)  # 既に project を持つ往復は変えない

    def test_audit_counts_superseded_and_dispositions(self):
        eng = MemoryEngine(state_dir=self.tmpdir)
        eng.imprint(sansho.Turn(user_text="日本語で 日本語 丁寧", assistant_text=""), [_proposal("日本語でしてほしい", ["日本語", "丁寧"], "日本語で")])
        id20 = eng.imprint([_turn("沼矛は神器ではなく資材 沼矛 神器", "", KOJIKI)],
                           [_proposal("沼矛は神器ではなく資材", ["沼矛", "神器"], "沼矛は神器ではなく資材", kind="決定")])[0]["seed"]["id"]
        other = "矛として扱い神器とする (資材でなく)"  # #20 と content 2-gram 類似度 < 0.5 にする
        eng.imprint([_turn("{} 沼矛 神器 資材 矛 扱い".format(other), "", KOJIKI)],
                    [_proposal(other, ["沼矛", "神器", "資材", "矛", "扱い"], other, kind="決定")])
        eng.imprint([_turn("沼矛は資材であって神器ではなく 沼矛 神器", "", KOJIKI)],
                    [_proposal("沼矛は資材であって神器ではなく", ["沼矛", "神器"], "沼矛は資材であって神器ではなく",
                               kind="決定", action="supersede", target_id=id20)])
        audit = eng.audit()
        self.assertEqual(audit["kind_counts"], {"決定": 2, "回避策": 0, "所在": 0, "再開点": 0, "性向": 1})
        self.assertEqual(audit["project_counts"], {"(全域)": 1, KOJIKI: 2})
        self.assertEqual(audit["superseded_count"], 1)


class TestHooksAndMcpProject(unittest.TestCase):
    """§4.2 / §12.3: hooks は stdin の cwd から、MCP は任意引数 project (stdio は cwd 既定) から project を決める。"""

    def setUp(self):
        _fake_home(self)
        self.tmpdir = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(os.path.join(self.repo, "sub"))
        self.state = os.path.join(self.tmpdir, "state")
        self._saved_environ = dict(os.environ)
        os.environ["RAIYA_BACKEND"] = "command"
        os.environ["RAIYA_LLM_CMD"] = "/bin/false"  # 統合は起きない件数に抑える

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stop_records_project_from_cwd_and_recall_filters_by_it(self):
        import contextlib
        import io

        from raiya.hooks import claude_code as hooks

        repo = os.path.realpath(self.repo)
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.cmd_recall({"prompt": "state の置き場所を決めた", "session_id": "s1", "cwd": os.path.join(self.repo, "sub")}, self.state)
        hooks.cmd_stop({"session_id": "s1", "last_assistant_message": "了解", "cwd": os.path.join(self.repo, "sub")}, self.state)
        hooks.cmd_stop({"session_id": "s1", "last_assistant_message": "了解"}, self.state)  # cwd 無し
        eng = MemoryEngine(state_dir=self.state, new_life=False)
        rows = eng.ledger.tail(2)
        self.assertEqual(rows[0]["project"], repo)
        self.assertIsNone(rows[1]["project"])

        eng.imprint([_turn("state は ~/.raiya にある state", "", repo)],
                    [_proposal("state は ~/.raiya にある", ["state"], "state は ~/.raiya にある", kind="所在")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "state の話", "session_id": "s1", "cwd": self.repo}, self.state)
        self.assertIn("state は ~/.raiya にある", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.cmd_recall({"prompt": "state の話", "session_id": "s1", "cwd": self.tmpdir}, self.state)  # 別 project
        self.assertEqual(out.getvalue(), "")

    def test_recall_passes_session_start_only_on_first_prompt(self):
        import contextlib
        import io

        from raiya.hooks import claude_code as hooks

        calls = []
        empty = {"seeds": [], "narrative": "", "index": [], "resume": None}

        def _fake(self_, text, context=None, project=None, session_start=False, session_id=None):
            calls.append((project, session_start))
            sessions.append(session_id)
            return empty

        sessions = []

        with mock.patch.object(MemoryEngine, "recall", _fake):
            with contextlib.redirect_stdout(io.StringIO()):
                hooks.cmd_recall({"prompt": "最初の発言", "session_id": "s2", "cwd": self.repo}, self.state)
                hooks.cmd_recall({"prompt": "次の発言", "session_id": "s2", "cwd": self.repo}, self.state)
                hooks.cmd_recall({"prompt": "cwd 無し", "session_id": "s2"}, self.state)
        repo = os.path.realpath(self.repo)
        self.assertEqual(calls, [(repo, True), (repo, False), (None, False)])
        self.assertEqual(sessions, ["s2", "s2", "s2"])  # inspect spec §2.1: stdin の session_id を渡す

    def test_mcp_project_argument_is_coerced_and_default_applies_only_when_absent(self):
        try:
            from raiya import mcp_server
        except ImportError:  # pragma: no cover - mcp 未インストール環境
            self.skipTest("mcp SDK が無い")
        eng = MemoryEngine(state_dir=self.state)
        schema = next(t for t in mcp_server.TOOLS if t["name"] == "memory_recall")["inputSchema"]
        self.assertEqual(schema["properties"]["project"], {"type": "string"})
        self.assertEqual(schema["required"], ["text"])
        imprint_schema = next(t for t in mcp_server.TOOLS if t["name"] == "memory_imprint")["inputSchema"]
        self.assertEqual(imprint_schema["properties"]["project"], {"type": "string"})
        self.assertEqual(imprint_schema["required"], ["turn", "proposals"])

        turn = {"user_text": "state は ~/.raiya にある state", "assistant_text": ""}
        proposals = [_proposal("state は ~/.raiya にある", ["state"], "state は ~/.raiya にある", kind="所在")]
        r = mcp_server._handle_tool_call(eng, "memory_imprint", {"turn": turn, "proposals": proposals, "project": RAIYA})
        self.assertEqual(r[0]["seed"]["project"], RAIYA)
        r2 = mcp_server._handle_tool_call(eng, "memory_imprint", {"turn": turn, "proposals": proposals, "project": 5},
                                          default_project=KOJIKI)
        self.assertIsNone(r2[0]["seed"]["project"])  # 非 str は None (既定には戻さない)
        r3 = mcp_server._handle_tool_call(eng, "memory_imprint", {"turn": turn, "proposals": proposals}, default_project=KOJIKI)
        self.assertEqual(r3[0]["seed"]["project"], KOJIKI)

        # 発芽した種子は ③ の index に出ないので、発芽しない問い (縁に触れない) で project の絞り込みを見る。
        got = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "別の話", "project": RAIYA})
        self.assertEqual(sorted(s["project"] or "" for s in got["index"]), ["", RAIYA])
        got_global = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "別の話"})
        self.assertEqual([s["project"] for s in got_global["index"]], [None])
        got_default = mcp_server._handle_tool_call(eng, "memory_recall", {"text": "別の話"}, default_project=KOJIKI)
        self.assertEqual(sorted(s["project"] or "" for s in got_default["index"]), ["", KOJIKI])

    def test_mcp_stdio_default_project_is_resolved_from_cwd(self):
        try:
            from raiya import mcp_server
        except ImportError:  # pragma: no cover
            self.skipTest("mcp SDK が無い")
        import inspect

        src = inspect.getsource(mcp_server.serve_stdio)
        self.assertIn("default_project=resolve_project(os.getcwd())", src)
        self.assertNotIn("resolve_project", inspect.getsource(mcp_server.build_http_app))


class TestRenderG(_StoreCase):
    """spec 2026-10-30 §7 G: imprint した種子を注入文まで通す (混在 = user の span + assistant の evidence)。"""

    def setUp(self):
        super().setUp()
        self.manas = manas_store_at(os.path.join(self.tmpdir, "manas.json"))

    def _mixed_decision(self):
        user = "B ではなく A を採用 採用"
        turn = _turn(user, ASSISTANT_ACK, RAIYA)
        prop = _proposal("B ではなく A を採用", ["採用"], "B ではなく A を採用", source="user", kind="決定")
        prop["evidence"].append({"source": "assistant", "span": ASSISTANT_ACK})
        return self.store.imprint([turn], prop)

    def test_spoofed_proposal_renders_suitei_without_partial_mark(self):
        turn = _turn("質問 採用", "B でなく A を採用", RAIYA)
        prop = _proposal("B ではなく A を採用", ["採用"], "B でなく A を採用", source="user", kind="性向")
        prop["sansho"] = "圓成實"
        r = self.store.imprint([turn], prop)
        seed = r["seed"]
        self.assertEqual(seed["sansho"], "依他起")
        self.assertIn("(推定)", self.manas.build_narrative([seed]))
        self.assertNotIn("一部推定", self.manas.build_narrative([seed]))
        self.assertIn("(推定)", self.manas.build_index_text([seed]))

    def test_real_mixed_decision_germinates_with_partial_mark(self):
        r = self._mixed_decision()
        r = self._mixed_decision()  # 依他起は強度 2 で発芽 (遅延発火)
        self.assertEqual(r["seed"]["kind"], "決定")
        self.assertEqual(r["seed"]["sansho"], "依他起")
        fired = self.store.germinate("採用について", project=RAIYA)
        self.assertEqual(len(fired), 1)
        text = self.manas.build_narrative(fired)
        self.assertRegex(text, r"- 決定: B ではなく A を採用\(強度\d+・要約: B ではなく A を採用・一部推定\)")
        self.assertIn("・一部推定)", text)
        self.assertNotIn("(推定)", text)

    def test_mixed_enjoujitsu_shows_partial_mark_but_pure_user_does_not(self):
        pure = {"id": 1, "kind": "決定", "content": "c", "strength": 1, "sansho": "圓成實",
                "evidences": [{"source": "user", "span": "u1"}]}
        self.assertNotIn("一部推定", self.manas.build_narrative([pure]))
        mixed = dict(pure, evidences=[{"source": "user", "span": "u1"}, {"source": "assistant", "span": "a1"}])
        self.assertIn("- 決定: u1(強度1・要約: c・一部推定)", self.manas.build_narrative([mixed]))

    def test_legacy_sansho_less_and_resume_point_are_content_only(self):
        ev = [{"source": "user", "span": "u1"}]
        legacy = {"id": 1, "kind": "決定", "content": "c", "strength": 1, "evidences": ev}
        self.assertEqual(self.manas.build_narrative([legacy]).splitlines()[1], "- 決定: c(強度1)")
        resume = {"id": 2, "kind": "再開点", "content": "c", "strength": 1, "sansho": "依他起", "evidences": ev}
        self.assertEqual(self.manas.build_narrative([resume]).splitlines()[1], "- 再開点(推定): c(強度1)")

    def test_long_mixed_line_becomes_name_only(self):
        # 6b (T3): 長い混在の行は、入らなければ名だけの形で描かれ (切れた数には数えない)、逐語は切らない
        seed = {"id": 1, "kind": "決定", "content": "要" * 200, "strength": 2, "sansho": "依他起",
                "evidences": [{"source": "user", "span": "長" * (INJECTION_MAX_CHARS + 100)},
                              {"source": "assistant", "span": "a"}]}
        composed = self.manas.compose_injection([seed])
        self.assertLessEqual(len(composed["text"]), INJECTION_MAX_CHARS)
        self.assertEqual(composed["shown_seed_ids"], [1])
        self.assertEqual(composed["cut"]["germinated"], 0)
        self.assertIn("- 決定: #1 (逐語 3300 字・容量で省略・強度2・一部推定)", composed["text"])


if __name__ == "__main__":
    unittest.main()
