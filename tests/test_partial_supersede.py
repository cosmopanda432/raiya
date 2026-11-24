"""#122 SP1'' (部分交代の門) の決定論 pin: 旧の user の文に新しい言が触れていない記が残るなら supersede は new に倒す。

spec docs/superpowers/specs/2026-11-02-partial-supersede-design.md §5/§7。テストが消すのは自作 tmp だけ。
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import alaya, sansho  # noqa: E402
from tests.test_withdrawal import _TmpCase, _seed  # noqa: E402
from raiya.ledger import LedgerTurn  # noqa: E402
from tests._state_fixture import alaya_store_at  # noqa: E402

PROJ = "/home/dev/raiya"
ASSISTANT = "了解"
OLD_SPAN = "DB は SQLite にしました。キャッシュは Redis で行く。"


def _turn(user_text, assistant_text="", ledger_id=0):
    return LedgerTurn(user_text=user_text, assistant_text=assistant_text, id=ledger_id, project=PROJ)


def _prop(content, conditions, span, source="user", kind="決定", **kw):
    p = {"content": content, "conditions": list(conditions), "polarity": "無記", "kind": kind,
         "evidence": [{"source": source, "span": span}]}
    p.update(kw)
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def plant(self, span, conditions, content="旧の決定", **kw):
        r = self.store.imprint([_turn(span)], _prop(content, conditions, span, **kw))
        self.assertTrue(r["is_new"], r)
        return r["seed"]["id"]

    def supersede(self, sid, span, conditions, content="新の決定", assistant=ASSISTANT, replacement_for=None, **kw):
        p = _prop(content, conditions, span, action="supersede", target_id=sid, **kw)
        return self.store.imprint([_turn(span, assistant, ledger_id=1)], p, replacement_for=replacement_for)

    def seed(self, sid):
        return self.store.get_by_ids([sid])[0]


class TestPartialSupersede(_Base):
    def test_k3_partial_replacement_becomes_new(self):
        sid = self.plant(OLD_SPAN, ["SQLite", "Redis"], content="DB は SQLite、キャッシュは Redis を採用")
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached", "Redis"], content="キャッシュに Memcached を採用")
        self.assertTrue(r["is_new"], r)
        self.assertNotEqual(r["seed"]["id"], sid)
        self.assertIn("部分交代", r["reason"])
        self.assertIn("部分交代", self.store.list_imprint_log()[-1]["reason"])
        old = self.seed(sid)
        self.assertEqual(old["content"], "DB は SQLite、キャッシュは Redis を採用")
        self.assertTrue(alaya.is_live(old))
        self.assertEqual(old["conditions"], ["SQLite", "Redis"])

    def test_total_replacement_goes_through(self):
        sid = self.plant(OLD_SPAN, ["SQLite", "Redis"])
        r = self.supersede(sid, "DB は SQLite のまま、キャッシュは Redis から Memcached にした", ["Memcached", "Redis"])
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(self.seed(sid)["content"], "新の決定")

    def test_single_topic_value_swap_goes_through(self):
        sid = self.plant("キャッシュは Redis で行く。", ["Redis", "キャッシュ"])
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached", "キャッシュ"])
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(self.seed(sid)["content"], "新の決定")

    def test_question_sentence_is_not_a_record(self):
        # 疑の文は記でない: 記は 1 文だけで、それは触れられている → 通る。
        sid = self.plant("キャッシュは Redis で行く。DB は SQLite のままでいい？", ["Redis", "SQLite"])
        r = self.supersede(sid, "キャッシュは Redis から Memcached に決めた", ["Memcached", "Redis"])
        self.assertTrue(r.get("superseded"), r)

    def test_same_without_question_is_blocked(self):
        # 鏡: 疑の文でなければ記は 2 文で SQLite の文が触れられない → new。
        sid = self.plant("キャッシュは Redis で行く。DB は SQLite のままにした。", ["Redis", "SQLite"])
        r = self.supersede(sid, "キャッシュは Redis から Memcached に決めた", ["Memcached", "Redis"])
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])
        self.assertEqual(self.seed(sid)["content"], "旧の決定")

    def test_one_sentence_two_matters_becomes_new(self):
        # 既知の限界 (a) は VC (spec §10) で解ける: 「、」で句に切ると値 (SQLite / Redis) を 1 つずつ持つ 2 句になり、
        # 新しい言が触れない SQLite の句が残るので new に倒れる。
        sid = self.plant("DB は SQLite、キャッシュは Redis。", ["SQLite", "Redis"])
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached"], content="キャッシュに Memcached を採用")
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])
        self.assertEqual(self.seed(sid)["content"], "旧の決定")

    # ---- VC (#122 spec §10): 「、」「,」で句に切り、核 (値) の無い句は隣に戻す ----
    NEW = "キャッシュは Memcached に決めた"

    def _vc(self, old_span, old_conds, new_conds=("Memcached", "キャッシュ"), new_span=None):
        sid = self.plant(old_span, old_conds)
        r = self.supersede(sid, new_span or self.NEW, list(new_conds))
        return sid, r

    def _assert_new(self, old_span, old_conds, **kw):
        sid, r = self._vc(old_span, old_conds, **kw)
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])
        self.assertEqual(self.seed(sid)["content"], "旧の決定")

    def _assert_superseded(self, old_span, old_conds, **kw):
        sid, r = self._vc(old_span, old_conds, **kw)
        self.assertTrue(r.get("superseded"), r)

    def test_vc_two_matters_new_conds_without_topic_word(self):
        self._assert_new("DB は SQLite、キャッシュは Redis にした。", ["SQLite", "Redis"], new_conds=("Memcached",))

    def test_vc_two_matters_second_clause_without_topic_marker(self):
        self._assert_new("DB は SQLite、キャッシュに Redis を使う。", ["SQLite", "Redis"])

    def test_vc_topic_comma_value_is_one_matter(self):
        self._assert_superseded("キャッシュは、Redis にした。", ["Redis", "キャッシュ"])
        self._assert_superseded("キャッシュは、Redis にした。", ["Redis", "キャッシュ"], new_conds=("Memcached",))

    def test_vc_reason_clause_attaches_to_value_clause(self):
        self._assert_superseded("速いので、キャッシュは Redis にした。", ["Redis", "キャッシュ"])

    def test_vc_value_comma_topic_is_one_matter(self):
        self._assert_superseded("Redis は速いので、キャッシュに使う。", ["Redis", "キャッシュ"])
        self._assert_superseded("Redis は速いので、キャッシュに使う。", ["Redis", "キャッシュ"], new_span="Redis から Memcached に変えた")

    def test_vc_inverted_clause_attaches_to_previous(self):
        self._assert_superseded("Redis にした、キャッシュが要るので。", ["Redis", "キャッシュ"])

    def test_vc_one_matter_split_by_comma_with_untouched_rephrase_is_blocked_known_limit(self):
        # 既知の限界 (e)、spec §10: 1 事を読点で分け、新が題にも旧値にも触れない言い換えは止める。
        self._assert_new("Redis は速いので、キャッシュに使う。", ["Redis", "キャッシュ"],
                         new_conds=("Memcached",), new_span="Memcached に変えた")

    def test_vc_english_comma_enumeration_becomes_new(self):
        self._assert_new("We use SQLite for DB, Redis for cache.", ["SQLite", "Redis"],
                         new_conds=("Memcached", "cache"), new_span="cache is now Memcached")

    def test_vc_english_topic_comma_value_is_one_matter(self):
        self._assert_superseded("For cache, we use Redis.", ["Redis", "cache"],
                                new_conds=("Memcached", "cache"), new_span="cache is now Memcached")
        self._assert_superseded("For cache, we use Redis.", ["Redis", "cache"],
                                new_conds=("Memcached",), new_span="cache is now Memcached")

    def test_vc_both_values_in_one_clause_stays_superseded_known_limit(self):
        self._assert_superseded("DB は SQLite と Redis で行く。", ["SQLite", "Redis"])

    def test_vc_z_exact_form(self):
        self._assert_new("DB は SQLite、キャッシュは Redis にした。", ["SQLite", "Redis"])

    def test_vc_full_width_comma_splits(self):
        # 全角「，」は sansho._n (NFKC) で「,」になり同じく句を切る。
        self._assert_new("DB は SQLite，キャッシュは Redis。", ["SQLite", "Redis"], new_conds=("Memcached",))

    def test_vc_thousands_separator_is_not_a_clause_break(self):
        # 数字に挟まれた「,」は切らない: 切るのは「、」だけ (値 60秒 / 1,000 の 2 つ、どちらも新が触れない)。
        self._assert_new("TTL は 60秒、上限は 1,000 件にした。", ["60秒", "1,000"], new_conds=("Memcached",))

    def test_vc_thousands_single_clause_is_one_matter(self):
        # 1,000 の 1 句だけなら 1 事: 値の交代は通る (同じ store に 2 つ植えると併合されるので別 test)。
        self._assert_superseded("上限は 1,000 件にした。", ["1,000"], new_conds=("Memcached",))

    def test_untouched_ignores_comma_in_question_sentence_lazily(self):
        calls = []
        seed = {"conditions": ["SQLite", "Redis"], "evidences": [
            {"source": "user", "span": "DB は SQLite、キャッシュは Redis でいい？"},
            {"source": "user", "span": "キャッシュは Redis。"}]}
        self.assertIsNone(alaya._untouched_user_sentence(seed, ["Memcached"], lambda: calls.append(1) or "x"))
        self.assertEqual(calls, [])

    def test_untouched_does_not_call_touched_text_for_single_comma_free_sentence(self):
        calls = []
        seed = {"conditions": ["Redis", "キャッシュ"], "evidences": [{"source": "user", "span": "キャッシュは Redis で行く。"}]}
        self.assertIsNone(alaya._untouched_user_sentence(seed, ["Memcached"], lambda: calls.append(1) or "x"))
        self.assertEqual(calls, [])

    def test_untouched_calls_touched_text_once_when_single_sentence_has_comma(self):
        calls = []
        seed = {"conditions": ["SQLite", "Redis"], "evidences": [{"source": "user", "span": "DB は SQLite、キャッシュは Redis。"}]}
        got = alaya._untouched_user_sentence(seed, ["Memcached"], lambda: calls.append(1) or "キャッシュは memcached に決めた")
        self.assertEqual(calls, [1])
        self.assertIsNotNone(got)

    def test_single_topic_swap_without_shared_condition_goes_through(self):
        # 門は一括 (記を担う文が 2 文以上) の種子にだけ効く: 1 文の記の値交代は常に 遍 (narrowing の pin)。
        sid = self.plant("猫が好き", ["猫"], kind="性向")
        r = self.supersede(sid, "緑茶が好き", ["緑茶"], kind="性向")
        self.assertTrue(r.get("superseded"), r)


    K3_CONTENT = "DB は SQLite、キャッシュは Redis を採用"

    def _k3(self):
        return self.plant(OLD_SPAN, ["SQLite", "Redis"], content=self.K3_CONTENT)

    def _reinforce(self, sid, span, conditions, content):
        r = self.store.imprint([_turn(span, ledger_id=2)], _prop(content, conditions, span, action="reinforce", target_id=sid))
        self.assertTrue(r.get("reinforced"), r)
        self.assertEqual(len(self.seed(sid)["evidences"]), 2)

    def test_reinforced_single_topic_swap_goes_through(self):
        # 記の単位は事 (縁の組): 同じ文が強化で重なっても 1 群 → 1 つの記の値交代は通る。
        sid = self.plant("DB は SQLite にした", ["SQLite"], content="DB は SQLite にした")
        self._reinforce(sid, "DB は SQLite にした", ["SQLite"], "DB は SQLite にした")
        r = self.supersede(sid, "DB は Postgres にする", ["Postgres"])
        self.assertTrue(r.get("superseded"), r)

    def test_reinforced_restatement_single_topic_swap_goes_through(self):
        sid = self.plant("DB は SQLite にした", ["SQLite"], content="DB は SQLite にした")
        self._reinforce(sid, "やはり SQLite で行く", ["SQLite"], "DB は SQLite にした")
        r = self.supersede(sid, "DB は Postgres にする", ["Postgres"])
        self.assertTrue(r.get("superseded"), r)

    def test_k3_reinforced_once_still_blocks(self):
        sid = self._k3()
        self._reinforce(sid, OLD_SPAN, ["SQLite", "Redis"], self.K3_CONTENT)
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached"])
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])

    def test_partial_fallback_does_not_merge_into_target(self):
        sid = self.plant(
            "DB は SQLite にした。キャッシュは Redis で行く。", ["SQLite", "Redis", "キャッシュ"],
            content="DB は SQLite、キャッシュは Redis を採用",
        )
        before = self.seed(sid)
        r = self.supersede(
            sid, "キャッシュは Redis をやめて Memcached に決めた", ["Redis", "キャッシュ", "Memcached"],
            content="DB は SQLite、キャッシュは Memcached を採用",
        )
        self.assertTrue(r["is_new"], r)
        self.assertNotEqual(r["seed"]["id"], sid)
        after = self.seed(sid)
        for key in ("content", "strength", "evidences", "conditions"):
            self.assertEqual(after[key], before[key], key)

    def test_restatement_naming_only_topic_word_is_blocked_known_limit(self):
        # 既知の限界 (d): 題の語 (キャッシュ) だけで値の交代を言い、旧い値 Redis が窓に無い (縁に接地しない) 言い直しは止まる。
        sid = self._k3()
        r = self.supersede(sid, "DB は SQLite のまま、キャッシュは Memcached にした", ["Memcached", "Redis"])
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])

    def test_old_condition_only_in_new_question_is_untouched(self):
        sid = self._k3()
        r = self.supersede(sid, "DB は SQLite のままでいい？キャッシュは Redis から Memcached に決めた。", ["Memcached", "Redis"])
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])

    def test_case_insensitive_touch(self):
        sid = self._k3()
        r = self.supersede(sid, "DB は sqlite のまま、キャッシュは Redis から Memcached に決めた", ["Memcached", "Redis"])
        self.assertTrue(r.get("superseded"), r)


    def test_reinforced_subset_restatement_single_topic_swap_goes_through(self):
        # 事 = 縁を共有して連結した成分: {Redis, キャッシュ} と {Redis} は同じ成分。
        sid = self.plant("キャッシュは Redis で行く。", ["Redis", "キャッシュ"], content="キャッシュは Redis で行く")
        self._reinforce(sid, "やはり Redis で行く", ["Redis"], "キャッシュは Redis で行く")
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached", "キャッシュ"])
        self.assertTrue(r.get("superseded"), r)

    def test_sentence_naming_both_matters_joins_components_known_limit(self):
        # 既知の限界: 両方の縁を 1 文で言う文が 1 つあると 2 つの事が 1 成分になり、門は効かない。
        sid = self._k3()
        self._reinforce(sid, "SQLite と Redis で行く", ["SQLite", "Redis"], self.K3_CONTENT)
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached"])
        self.assertTrue(r.get("superseded"), r)


class TestPartialSupersedeUserGrounded(_Base):
    """U (#122 §10.1): 門に渡す新の縁は user 本文で接地したものだけ。assistant の復唱 (所覺) は「触れた」に数えない。"""

    OLD = "DB は SQLite、キャッシュは Redis にした。"

    def _plant_old(self):
        return self.plant(self.OLD, ["SQLite", "Redis"], content="DB は SQLite、キャッシュは Redis")

    def test_u_assistant_echo_of_old_value_does_not_touch(self):
        # f11 型 (限界 (f) を閉じる): assistant が「Redis → Memcached」と復唱しても Redis は user が触れていない = 部分交代。
        sid = self._plant_old()
        user, asst = "キャッシュは Memcached に決めた", "Redis → Memcached に変更しました"
        conds = ["Memcached", "キャッシュ", "Redis"]
        # 前提: gate (sansho) は assistant 本文で Redis を接地している = U が無ければ門に渡る。
        window = [_turn(user, asst, ledger_id=1)]
        self.assertIn("Redis", sansho.ground_conditions_in_window(conds, window))
        self.assertEqual(sansho.ground_conditions(["Redis"], user, ""), [])
        r = self.supersede(sid, user, conds, content="キャッシュに Memcached を採用", assistant=asst)
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])
        old = self.seed(sid)
        self.assertTrue(alaya.is_live(old))
        self.assertEqual(old["content"], "DB は SQLite、キャッシュは Redis")
        self.assertEqual(old["conditions"], ["SQLite", "Redis"])

    def test_u_user_mention_of_old_value_still_touches(self):
        # 対照: user 自身が Redis に触れれば従来どおり通る。
        sid = self._plant_old()
        r = self.supersede(sid, "キャッシュは Redis から Memcached に決めた", ["Memcached", "キャッシュ", "Redis"],
                           assistant="Redis → Memcached に変更しました")
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(self.seed(sid)["content"], "新の決定")

    def test_u_one_matter_assistant_only_touch_is_limit_e(self):
        # fc1 型 = 既知の限界 (e): user の言が題 (Redis) にも触れず、assistant の復唱だけが触れる 1 事は、今は止める側 (new)。
        # 以前は assistant の echo が偶然これを通していた。裁定済みの限界で、(f) (記が注入から消える) より軽い誤り。
        sid = self.plant("Redis は速いので、キャッシュに使う。", ["Redis", "キャッシュ"], content="Redis をキャッシュに")
        r = self.supersede(sid, "Memcached に変えた", ["Memcached", "Redis"], content="Memcached に変更",
                           assistant="Redis から Memcached に変更")
        self.assertTrue(r["is_new"], r)
        self.assertIn("部分交代", r["reason"])
        self.assertTrue(alaya.is_live(self.seed(sid)))

    def test_u_user_grounding_in_another_turn_is_kept(self):
        # 窓が 3 往復: 往復 1 は assistant だけが Redis、往復 2 の user (span の外) が Redis に触れる。
        # 往復ごとの user 接地は残る (Redis は新縁として門に渡る) = 通る。span 自体は Redis を含まない。
        sid = self._plant_old()
        span = "キャッシュは Memcached に決めた"
        conds = ["Memcached", "キャッシュ", "Redis"]
        p = _prop("新の決定", conds, span, action="supersede", target_id=sid)
        window = [_turn("次へ", "Redis を確認します", ledger_id=1), _turn("Redis はやめる", ASSISTANT, ledger_id=2),
                  _turn(span, ASSISTANT, ledger_id=3)]
        r = self.store.imprint(window, p)
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(self.seed(sid)["content"], "新の決定")

    def test_u_replacement_for_unaffected_by_assistant_echo(self):
        # 人の置き換え (replacement_for) は門の対象外: f11 の fixture でも通る。
        sid = self._plant_old()
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached", "キャッシュ", "Redis"],
                           assistant="Redis → Memcached に変更しました", replacement_for=sid)
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(self.seed(sid)["content"], "新の決定")


class TestPartialSupersedeHorizontalUserGrounded(_Base):
    def test_u_assistant_only_old_value_does_not_hide_other_in_horizontal(self):
        # 他の種子は読点で 3 事 (SQLite / JSON / Redis)。SQLite・JSON は assistant 本文にだけ現れる = 触れたに数えず、横断では隠さない。
        other = self.plant("DB は SQLite、ログは JSON、キャッシュは Redis にした。", ["SQLite", "JSON", "Redis"],
                           content="別の決定")
        target = self.plant("キャッシュは Redis で行く。", ["Redis", "キャッシュ"], content="target 決定")
        user = "キャッシュは Redis をやめて Memcached に決めた"
        conds = ["Redis", "キャッシュ", "Memcached", "SQLite", "JSON"]
        asst = "SQLite と JSON はそのままです"
        self.assertEqual(sansho.ground_conditions_in_window(conds, [_turn(user, asst, ledger_id=1)]), conds)
        r = self.supersede(target, user, conds, assistant=asst)
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(r["also_superseded"], [])
        self.assertIsNone(self.seed(other).get("superseded_by"))
        self.assertIn("横断 supersede 見送り", r["reason"])


class TestPartialSupersedeExclusions(_Base):
    def test_replacement_for_goes_through(self):
        sid = self.plant(OLD_SPAN, ["SQLite", "Redis"])
        r = self.supersede(sid, "キャッシュは Memcached に決めた", ["Memcached", "Redis"], replacement_for=sid)
        self.assertTrue(r.get("superseded"), r)

    def test_resume_point_goes_through(self):
        span = "DB は SQLite にしました。次は Redis の設定から始める。"
        sid = self.plant(span, ["SQLite", "Redis"], kind="再開点")
        r = self.supersede(sid, "次は Memcached の設定から始める", ["Memcached", "Redis"], kind="再開点")
        self.assertTrue(r.get("superseded"), r)

    def test_target_without_user_evidence_goes_through(self):
        span = "DB は SQLite にした。キャッシュは Redis で行く。"
        r0 = self.store.imprint([_turn("質問", span)], _prop("旧", ["SQLite", "Redis"], span, source="assistant", kind="性向"))
        sid = r0["seed"]["id"]
        self.assertFalse(alaya.has_user_span(self.seed(sid)))
        span2 = "キャッシュは Memcached に決めた"
        p = _prop("新", ["Memcached", "Redis"], span2, source="assistant", kind="性向", action="supersede", target_id=sid)
        r = self.store.imprint([_turn("質問", span2, ledger_id=1)], p)
        self.assertTrue(r.get("superseded"), r)


class TestPartialSupersedeHorizontal(_Base):
    NEW_SPAN = "キャッシュは Redis をやめて Memcached に決めた"
    NEW_CONDS = ["Redis", "キャッシュ", "Memcached"]

    def _run(self, other_span, other_conds):
        target = self.plant("キャッシュは Redis で行く。", ["Redis", "キャッシュ"], content="target 決定")
        other = self.plant(other_span, other_conds, content="別の決定 ログと TTL の扱い")
        self.assertNotEqual(target, other)
        return target, other, self.supersede(target, self.NEW_SPAN, self.NEW_CONDS, assistant="")

    def test_control_without_untouched_sentence_is_hidden(self):
        target, other, r = self._run(
            "キャッシュは Redis で行く。Redis の TTL は 設定 に決めた。Redis のログも出す。", ["Redis", "キャッシュ", "TTL", "設定", "ログ"]
        )
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(r["also_superseded"], [other])
        self.assertEqual(self.seed(other)["superseded_by"], target)

    def test_other_with_untouched_record_is_not_hidden(self):
        target, other, r = self._run(
            "キャッシュは Redis で行く。ログは 設定 に決めた。TTL もある。", ["Redis", "キャッシュ", "ログ", "設定", "TTL"]
        )
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(r["also_superseded"], [])
        self.assertIsNone(self.seed(other).get("superseded_by"))
        self.assertTrue(alaya.is_live(self.seed(other)))
        self.assertIn("横断 supersede 見送り", r["reason"])
        self.assertIn("部分交代", r["reason"])

    def test_other_with_comma_separated_untouched_matter_is_not_hidden(self):
        # VC (spec §10): 読点で並べた 1 文の中に新が触れない事 (SQLite) がある他の種子も 横断 supersede では隠さない。
        # 核 = SQLite / JSON / TTL / 設定 (Redis・キャッシュは新が触れる)。「キャッシュは Redis にした」は核の無い句なので前の句に付く。
        other = self.plant("DB は SQLite、ログは JSON、TTL は 設定、キャッシュは Redis にした。",
                           ["SQLite", "JSON", "TTL", "設定", "Redis", "キャッシュ"],
                           content="別の決定 ログと TTL の扱い")
        target = self.plant("キャッシュは Redis で行く。", ["Redis", "キャッシュ"], content="target 決定")
        r = self.supersede(target, self.NEW_SPAN, self.NEW_CONDS, assistant="")
        self.assertTrue(r.get("superseded"), r)
        self.assertEqual(r["also_superseded"], [])
        self.assertIsNone(self.seed(other).get("superseded_by"))
        self.assertIn("横断 supersede 見送り", r["reason"])


class TestPartialSupersedeTwin(_TmpCase):
    def test_partial_fallback_keeps_twin_match_for_shadowed_target(self):
        # 倒した target が双子として隠された H なら、類似併合から外さず 双子照合 (twin_match) のまま。
        span = "DB は SQLite にした。キャッシュは Redis で行く。"
        content = "DB は SQLite、キャッシュは Redis を採用"
        e = _seed(1, ["SQLite", "Redis"], content=content, project=None)
        h = _seed(2, ["SQLite", "Redis"], content=content, project=None, sansho_class="依他起", strength=2,
                  evidences=[{"life": 1, "source": "user", "span": span, "ledger_id": None}])
        store = self.store([e, h])
        self.assertEqual(alaya._twin_shadows(store.state["seeds"]), {id(store.state["seeds"][1]): 1})
        before = copy.deepcopy(store.get_by_ids([2])[0])
        user_span = "キャッシュは Memcached に決めた"
        turn = sansho.Turn(user_text=user_span, assistant_text="Redis を置き換えます")
        proposal = {"content": content, "conditions": ["Memcached", "Redis"], "polarity": "無記", "kind": "所在",
                    "action": "supersede", "target_id": 2,
                    "evidence": [{"source": "user", "span": user_span}, {"source": "assistant", "span": "Redis を置き換えます"}]}
        r = store.imprint(turn, proposal)
        self.assertIn("部分交代", r["reason"])
        self.assertEqual(r["decision"], "双子照合")
        self.assertEqual(r["twin_match"], 2)
        self.assertIsNone(r["is_new"])
        self.assertEqual(store.get_by_ids([2])[0], before)


if __name__ == "__main__":
    unittest.main()
