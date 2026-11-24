import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.alaya import AlayaStore, QUARANTINE_MAX  # noqa: E402
from raiya import sansho  # noqa: E402
from tests._state_fixture import alaya_store_at, edit_seeds, table_bodies  # noqa: E402


def _imprint(store, content, conditions, polarity="無記", span=None, kind="所在"):
    """`store.imprint(content, conditions, polarity)` 形の置き換えヘルパー。span は
    既定で content 自身とし、conditions も往復本文に連結して全て接地させる -- 常に
    圓成實として熏習が成功する Turn/proposal を組み立てる。

    これらのテストの目的は三性ゲート自体の合否ではなく、strength/発芽閾値・索引/永続化と
    いった既存機構の回帰確認なので、ゲートは常に通す側に振っておく (種子は削除も減衰も
    されない = 枯死は撤去済み)。
    `kind` の既定は 所在 (縁 1 語で発芽する kind): これらのテストは改修5 以前の「縁 1 語で発芽」
    を前提に書かれており、既定の 性向 だと 2 語一致が要る (性向の規則は TestKindAndProject で pin)。
    戻り値: AlayaStore.imprint() の結果 dict。
    """
    cond_text = "".join(c for c in conditions if c)
    # L (question-as-evidence §7): 圓成實 の 所在・性向 は user の span に縁か印が要るため、既定の span は縁を含める。
    span = "{} {} p/".format(content, cond_text) if span is None else span  # #149: 所在 の形は 區切りの語 (`/` を含む語)
    user_text = "{} {}".format(span, cond_text)
    turn = sansho.Turn(user_text=user_text, assistant_text="")
    proposal = {
        "content": content,
        "conditions": list(conditions),
        "polarity": polarity,
        "kind": kind,
        "evidence": {"source": "user", "span": span},
    }
    result = store.imprint(turn, proposal)
    store.commit()  # 動詞途中の暗黙 save は無い: 永続化が要るテストのため、旧来どおり熏習ごとに書く
    return result


class TestAlaya(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "alaya.json")  # ファイル名は alaya_store_at が無視する (raiya.db)
        self.imprint_log_path = os.path.join(self.tmpdir, "imprint_log.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_store(self):
        return alaya_store_at(self.path, self.imprint_log_path)

    # --- 初期状態 ---
    def test_fresh_store_has_no_seeds(self):
        store = self._new_store()
        self.assertEqual(store.list_seeds(), [])
        self.assertEqual(store.current_life, 0)

    def test_begin_new_life_increments(self):
        store = self._new_store()
        self.assertEqual(store.begin_new_life(), 1)
        self.assertEqual(store.begin_new_life(), 2)

    # --- 熏習 / 強化 ---
    def test_imprint_creates_new_seed_with_initial_strength_1(self):
        store = self._new_store()
        store.begin_new_life()
        result = _imprint(store, "猫が好き", ["猫"], "善")
        self.assertTrue(result["is_new"])
        self.assertEqual(result["seed"]["strength"], 1)

    def test_reimprint_same_condition_reinforces_existing_seed(self):
        store = self._new_store()
        store.begin_new_life()
        _imprint(store, "猫が好き", ["猫"], "善")
        result = _imprint(store, "やはり猫が好き", ["猫"], "善")
        self.assertFalse(result["is_new"])
        self.assertEqual(result["seed"]["strength"], 2)
        self.assertEqual(len(store.list_seeds()), 1)

    def test_imprint_different_polarity_still_merges_by_similarity(self):
        # 改修5 §5.1 (E3): polarity は併合条件ではない。縁 Jaccard 1.0 で併合され、polarity は既存を保つ。
        store = self._new_store()
        store.begin_new_life()
        _imprint(store, "猫が好き", ["猫"], "善")
        result = _imprint(store, "猫が嫌い", ["猫"], "不善")
        self.assertFalse(result["is_new"])
        self.assertEqual(len(store.list_seeds()), 1)
        self.assertEqual(store.list_seeds()[0]["polarity"], "善")

    # --- 発芽 (遅延発火) ---
    def test_seed_does_not_germinate_below_threshold(self):
        # 圓成實の発芽閾値は改修4で1になったため、閾値未満で眠る例は依他起
        # (assistant接地) で示す (依他起の閾値は2のまま)。
        store = self._new_store()
        store.begin_new_life()
        turn = sansho.Turn(user_text="猫について", assistant_text="猫が好きです 猫")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "assistant", "span": "猫が好きです"},
        }
        result = store.imprint(turn, proposal)  # strength=1
        self.assertEqual(result["decision"], "依他起")
        fired = store.germinate("猫について教えて")
        self.assertEqual(fired, [])

    def test_germination_after_reinforcement(self):
        store = self._new_store()
        store.begin_new_life()
        _imprint(store, "猫が好き", ["猫"], "善")
        _imprint(store, "やはり猫が好き", ["猫"], "善")  # strength=2
        fired = store.germinate("猫について教えて")
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["conditions"], ["猫"])

    def test_germination_condition_must_match_input(self):
        store = self._new_store()
        store.begin_new_life()
        _imprint(store, "猫が好き", ["猫"], "善")
        _imprint(store, "やはり猫が好き", ["猫"], "善")
        fired = store.germinate("犬について教えて")
        self.assertEqual(fired, [])

    def test_germination_max_5(self):
        store = self._new_store()
        store.begin_new_life()
        for i in range(7):
            cond = "話題{}".format(i)
            _imprint(store, "内容{}".format(i), [cond], "無記")
            _imprint(store, "内容{}再び".format(i), [cond], "無記")  # strength=2
        text = "話題0 話題1 話題2 話題3 話題4 話題5 話題6"
        fired = store.germinate(text)
        self.assertLessEqual(len(fired), 5)

    def test_germination_sorted_by_strength_desc(self):
        store = self._new_store()
        store.begin_new_life()
        _imprint(store, "弱い種", ["トピックA"], "無記")
        _imprint(store, "弱い種2", ["トピックA"], "無記")  # strength=2
        _imprint(store, "強い種", ["トピックB"], "無記")
        _imprint(store, "強い種2", ["トピックB"], "無記")
        _imprint(store, "強い種3", ["トピックB"], "無記")  # strength=3
        fired = store.germinate("トピックAとトピックBについて")
        self.assertEqual(len(fired), 2)
        self.assertEqual(fired[0]["conditions"], ["トピックB"])
        self.assertEqual(fired[0]["strength"], 3)

    def test_germination_updates_fire_count_and_last_fired(self):
        store = self._new_store()
        store.begin_new_life()  # life=1
        _imprint(store, "種", ["犬"], "無記")
        _imprint(store, "種2", ["犬"], "無記")  # strength=2
        store.germinate("犬について")
        seed = store.list_seeds()[0]
        self.assertEqual(seed["fire_count"], 1)
        self.assertEqual(seed["last_fired"], 1)

    # --- 恒隨轉 (KUN-3): 種子は減衰も削除もされない。miss_streak は観測のみ ---
    def test_seed_is_never_decayed_or_deleted_by_missed_lives(self):
        store = self._new_store()
        store.begin_new_life()  # life=1
        _imprint(store, "消えない種", ["幽霊トピック"], "無記")  # strength=1, planted_at=1
        for _ in range(10):
            store.begin_new_life()
        seeds = store.list_seeds()
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["strength"], 1)
        self.assertEqual(seeds[0]["miss_streak"], 9)  # 植えた直後の生は判定対象外 -> 10 生で 9

    def test_firing_resets_miss_streak(self):
        store = self._new_store()
        store.begin_new_life()  # life=1
        _imprint(store, "種", ["トピックX"], "無記")
        store.begin_new_life()  # life=2 (植えたて判定除外)
        store.begin_new_life()  # life=3 miss=1
        store.germinate("トピックXの話")  # 圓成實 strength=1 は閾値1で発芽 -> miss_streak=0
        store.begin_new_life()  # life=4: last_fired(3)==prev_life(3) -> miss=0
        self.assertEqual(store.list_seeds()[0]["miss_streak"], 0)

    # --- 発芽閾値 (改修4 §5): 圓成實 1 / 依他起 2 ---
    def test_enjoujitsu_seed_fires_at_strength_1(self):
        store = self._new_store()
        _imprint(store, "ユーザーが言った", ["言明"], "無記")  # 圓成實 strength=1
        self.assertEqual([s["content"] for s in store.germinate("言明について")], ["ユーザーが言った"])

    def test_etaki_seed_sleeps_at_strength_1_and_fires_at_2(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="質問 推定", assistant_text="アシスタントの推定です 推定")
        proposal = {
            "content": "アシスタントの推定",
            "conditions": ["推定"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "assistant", "span": "アシスタントの推定です"},
        }
        r1 = store.imprint(turn, proposal)
        self.assertEqual(r1["decision"], "依他起")
        self.assertEqual(store.germinate("推定の話"), [])  # strength 1 < 2 は眠る (遅延発火)
        store.imprint(turn, proposal)  # 併合で strength 2
        self.assertEqual([s["content"] for s in store.germinate("推定の話")], ["アシスタントの推定"])

    # --- 文脈窓 (改修4 §5): 縁は入力と各文脈テキストをそれぞれ照合 ---
    def test_germinate_matches_conditions_in_context_texts(self):
        store = self._new_store()
        _imprint(store, "猫が好き", ["猫"], "善")
        self.assertEqual(store.germinate("今日は何をする？"), [])
        fired = store.germinate("今日は何をする？", context_texts=["さっき猫の話をした", "別の話"])
        self.assertEqual([s["content"] for s in fired], ["猫が好き"])

    def test_germinate_does_not_match_across_text_boundary(self):
        store = self._new_store()
        _imprint(store, "境界", ["猫犬"], "無記")
        self.assertEqual(store.germinate("猫", context_texts=["犬"]), [])

    def test_germinate_rejects_str_context_texts(self):
        store = self._new_store()
        with self.assertRaises(TypeError):
            store.germinate("猫について", context_texts="猫犬")

    # --- 索引 (改修4 §5) ---
    def test_index_returns_all_seeds_in_class_then_strength_order_up_to_limit(self):
        store = self._new_store()
        _imprint(store, "弱い圓成實", ["a"], "無記")
        # NOTE (brief差分): 元は "強い圓成實" だったが、"弱い圓成實" との文字2-gram
        # Jaccard が 0.6 (>= MERGE_SIMILARITY_THRESHOLD=0.5) で誤併合し3種子に
        # ならないため、"強力圓成實" (同 0.333) に変更した。既存の
        # test_multi_writer.py TOPICS と同じ「意図的に語彙を大きく違える」規律に合わせる。
        _imprint(store, "強力圓成實", ["b"], "無記")
        _imprint(store, "強力圓成實", ["b"], "無記")  # strength 2
        turn = sansho.Turn(user_text="x 推定", assistant_text="推定の一文 推定")
        store.imprint(turn, {"content": "推定の一文", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                             "evidence": {"source": "assistant", "span": "推定の一文"}})
        index = store.index()
        self.assertEqual([s["content"] for s in index], ["強力圓成實", "弱い圓成實", "推定の一文"])
        self.assertEqual([s["content"] for s in store.index(limit=1)], ["強力圓成實"])
        self.assertEqual(store.seed_count(), 3)
        index[0]["content"] = "改竄"  # deep copy
        self.assertEqual(store.index()[0]["content"], "強力圓成實")

    def test_index_purpose_inject_vs_consolidate_and_unclassified_seed(self):
        # 改修5 §6.3: inject は kind 別の許可クラス (所在 は 圓成實/依他起)、sansho 欄の無い旧種子は
        # どのクラスにも当たらず載らない。consolidate は全クラス・全 kind (性向も) を limit まで。
        store = self._new_store()
        _imprint(store, "弱い圓成實", ["a"], "無記")
        _imprint(store, "強力圓成實", ["b"], "無記")
        _imprint(store, "強力圓成實", ["b"], "無記")  # strength 2
        turn = sansho.Turn(user_text="x 推定", assistant_text="推定の一文 推定")
        store.imprint(turn, {"content": "推定の一文", "conditions": ["推定"], "polarity": "無記", "kind": "所在",
                             "evidence": {"source": "assistant", "span": "推定の一文"}})
        _imprint(store, "旧種子", ["旧"], "無記")
        _imprint(store, "日本語でしてほしい", ["日本語"], "無記", kind="性向")
        def _drop_sansho(seeds):
            for seed in seeds:
                if seed["content"] == "旧種子":
                    del seed["sansho"]
                    break

        edit_seeds(self.tmpdir, _drop_sansho)
        store = self._new_store()

        self.assertEqual([s["content"] for s in store.index()], ["強力圓成實", "弱い圓成實", "推定の一文"])
        self.assertEqual(store.seed_count(), 3)
        self.assertEqual([s["content"] for s in store.index(limit=1)], ["強力圓成實"])

        consolidate = store.index(purpose="consolidate")
        self.assertEqual(
            [s["content"] for s in consolidate],
            ["強力圓成實", "弱い圓成實", "推定の一文", "旧種子", "日本語でしてほしい"],  # kind → クラス → 強度
        )
        self.assertEqual(store.seed_count(purpose="consolidate"), 5)
        self.assertEqual([s["content"] for s in store.index(purpose="consolidate", limit=1)], ["強力圓成實"])

    def test_index_filters_by_project_and_keeps_global_seeds(self):
        # 改修5 §6.2/§6.3: project が第一条件。全域 (project None) の種子は常に載る。
        from raiya.ledger import LedgerTurn

        store = self._new_store()
        _imprint(store, "全域の所在", ["全域"], "無記")
        store.imprint([LedgerTurn(user_text="raiya の所在 p/ raiya", assistant_text="", id=1, project="/home/dev/raiya")],
                      {"content": "raiya の所在", "conditions": ["raiya"], "polarity": "無記", "kind": "所在",
                       "evidence": [{"source": "user", "span": "raiya の所在 p/"}]})
        store.imprint([LedgerTurn(user_text="古事記の所在 p/ 古事記", assistant_text="", id=2, project="/home/dev/cosmic")],
                      {"content": "古事記の所在", "conditions": ["古事記"], "polarity": "無記", "kind": "所在",
                       "evidence": [{"source": "user", "span": "古事記の所在 p/"}]})
        self.assertEqual(sorted(s["content"] for s in store.index(project="/home/dev/raiya")), ["raiya の所在", "全域の所在"])
        self.assertEqual([s["content"] for s in store.index(project=None)], ["全域の所在"])  # None = 全域のみ
        self.assertEqual(
            sorted(s["content"] for s in store.index(project={"/home/dev/raiya", "/home/dev/cosmic"}, purpose="consolidate")),
            ["raiya の所在", "全域の所在", "古事記の所在"],
        )
        self.assertEqual(store.seed_count(project="/home/dev/cosmic"), 2)

    def test_backfill_adds_history_to_legacy_seed(self):
        store = self._new_store()
        _imprint(store, "旧種子", ["旧"], "無記")
        def _strip(seeds):
            del seeds[0]["history"]
            del seeds[0]["miss_streak"]
            del seeds[0]["evidences"]

        edit_seeds(self.tmpdir, _strip)
        reloaded = self._new_store()
        seed = reloaded.list_seeds()[0]
        self.assertEqual(seed["history"], [])
        self.assertEqual(seed["miss_streak"], 0)
        self.assertEqual(seed["evidences"], [])

    def test_legacy_seed_without_sansho_uses_unclassified_threshold(self):
        store = self._new_store()
        _imprint(store, "旧種子", ["旧"], "無記")
        edit_seeds(self.tmpdir, lambda seeds: seeds[0].pop("sansho"))
        reloaded = self._new_store()
        # 未分類 (sansho 欄なし) の閾値は安全側の2。strength=1 では眠る。
        self.assertEqual(reloaded.germinate("旧の話"), [])

        edit_seeds(self.tmpdir, lambda seeds: seeds[0].__setitem__("strength", 2))
        reloaded2 = self._new_store()
        fired = reloaded2.germinate("旧の話")
        self.assertEqual([s["content"] for s in fired], ["旧種子"])

    def test_state_persists_across_instances(self):
        store1 = self._new_store()
        store1.begin_new_life()
        _imprint(store1, "永続する種", ["永続トピック"], "無記")
        store2 = alaya_store_at(self.path, self.imprint_log_path)
        self.assertEqual(store2.current_life, 1)
        self.assertEqual(len(store2.list_seeds()), 1)
        self.assertEqual(store2.list_seeds()[0]["content"], "永続する種")

    # --- imprint_log 分離 (記憶機能設計書§4.3。sqlite 版は別表) ---
    def test_imprint_log_lives_in_its_own_table_not_inside_the_alaya_state(self):
        store = self._new_store()
        store.begin_new_life()
        _imprint(store, "猫が好き", ["猫"], "善")
        self.assertEqual(len(table_bodies(self.tmpdir, "imprint_log")), 1)  # 別表に書かれている
        for body in table_bodies(self.tmpdir, "seeds", order="pos"):
            self.assertNotIn("imprint_log", json.loads(body))  # 種子の本文には無い


class TestSanshoGateInAlaya(unittest.TestCase):
    """記憶機能設計書§3 三性ゲートの alaya.py への結線 pin (T1/T3/T4/T5/T6/T7)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "alaya.json")  # ファイル名は alaya_store_at が無視する (raiya.db)
        self.imprint_log_path = os.path.join(self.tmpdir, "imprint_log.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_store(self):
        store = alaya_store_at(self.path, self.imprint_log_path)
        store.begin_new_life()
        return store

    # --- T1: 遍計所執は seeds に構造的に入らず germinate で返らない ---
    def test_t1_fabricated_span_never_enters_seeds_or_germinates(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="今日は晴れです", assistant_text="良い天気ですね")
        proposal = {
            "content": "ユーザーは月に行ったことがある",
            "conditions": ["晴れ"],
            "polarity": "無記",
            # 往復本文のどちらにも無い捏造 span
            "evidence": {"source": "user", "span": "月に行ったことがある"},
        }
        result = store.imprint(turn, proposal)
        self.assertEqual(result["decision"], "遍計所執")
        self.assertIsNone(result["seed"])
        self.assertEqual(store.list_seeds(), [])
        self.assertEqual(len(store.list_quarantine()), 1)
        self.assertEqual(store.germinate("晴れについて教えて"), [])

    def test_t1_fabricated_span_excluded_while_grounded_seed_still_germinates(self):
        store = self._new_store()
        bad_turn = sansho.Turn(user_text="今日は晴れです", assistant_text="")
        store.imprint(
            bad_turn,
            {
                "content": "捏造された記憶",
                "conditions": ["共通語"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "捏造された記憶です"},
            },
        )
        good_turn = sansho.Turn(user_text="共通語について話した 猫が好きです p/", assistant_text="")
        good_proposal = {
            "content": "猫が好き",
            "conditions": ["共通語"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "共通語について話した 猫が好きです p/"},
        }
        store.imprint(good_turn, good_proposal)
        store.imprint(good_turn, good_proposal)  # strength=2 まで強化 (併合)
        fired = store.germinate("共通語について")
        self.assertEqual([s["content"] for s in fired], ["猫が好き"])

    # --- T3: 併合は sansho ∧ polarity 一致 ∧ 類似度 ≥ 閾値のみ。
    #         content 不変・evidences 追記・strength+1 ---
    def test_t3a_merge_keeps_content_unchanged_and_appends_evidence(self):
        store = self._new_store()
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = store.imprint(
            turn1,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好き p/"},
            },
        )
        self.assertTrue(r1["is_new"])
        seed_id = r1["seed"]["id"]

        turn2 = sansho.Turn(user_text="やっぱり猫が大好きです p/ 猫", assistant_text="")
        r2 = store.imprint(
            turn2,
            {
                "content": "やっぱり猫が大好きです",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "やっぱり猫が大好きです p/"},
            },
        )
        self.assertFalse(r2["is_new"])
        self.assertEqual(r2["seed"]["id"], seed_id)
        self.assertEqual(r2["seed"]["content"], "猫が好き")  # content 不変 (書き換わらない)
        self.assertEqual(r2["seed"]["strength"], 2)
        self.assertEqual(len(r2["seed"]["evidences"]), 2)
        self.assertEqual(r2["seed"]["evidences"][0]["span"], "猫が好き p/")
        self.assertEqual(r2["seed"]["evidences"][1]["span"], "やっぱり猫が大好きです p/")

    def test_t3b_class_mismatch_blocks_merge_even_at_full_similarity(self):
        store = self._new_store()
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = store.imprint(
            turn1,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好き p/"},
            },
        )
        self.assertEqual(r1["sansho"], "圓成實")

        turn2 = sansho.Turn(user_text="今日はいい天気ですね", assistant_text="猫が好き p/ 猫")
        r2 = store.imprint(
            turn2,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "assistant", "span": "猫が好き p/"},
            },
        )
        self.assertEqual(r2["sansho"], "依他起")
        self.assertTrue(r2["is_new"])
        self.assertEqual(len(store.list_seeds()), 2)
        original = store.get_by_ids([r1["seed"]["id"]])[0]
        self.assertEqual(original["strength"], 1)  # 併合されず元の種は強化されない

    def test_t3c_polarity_mismatch_no_longer_blocks_merge_at_full_similarity(self):
        # 改修5 §5.1 (E3): 旧 T3c (polarity 違いは併合しない) の反転。同クラス∧同 kind∧同 project で
        # 完全一致なら polarity が違っても reinforce される (本番の #1/#8 等 3 組の揺れを畳む)。
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好き p/"},
            },
        )
        r2 = store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "不善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好き p/"},
            },
        )
        self.assertFalse(r2["is_new"])
        self.assertEqual(len(store.list_seeds()), 1)
        original = store.get_by_ids([r1["seed"]["id"]])[0]
        self.assertEqual(original["strength"], 2)
        self.assertEqual(original["polarity"], "善")  # 併合時は既存の値を保つ

    def test_t3d_low_similarity_blocks_merge_same_class_and_polarity(self):
        store = self._new_store()
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        store.imprint(
            turn1,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好き p/"},
            },
        )
        turn2 = sansho.Turn(user_text="犬の散歩に行った p/ 散歩", assistant_text="")
        r2 = store.imprint(
            turn2,
            {
                "content": "犬の散歩に行った",
                "conditions": ["散歩"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "犬の散歩に行った p/"},
            },
        )
        self.assertTrue(r2["is_new"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_t3e_similarity_exactly_at_threshold_merges(self):
        # 境界ぴったり: 縁 jaccard = |{猫}∩{猫,犬}| / |{猫}∪{猫,犬}| = 1/2 = 0.5。
        # content 側は文字重複ゼロになるよう構成し、縁の軸だけで境界を作る。
        store = self._new_store()
        turn1 = sansho.Turn(user_text="あいうえお 猫 犬 p/", assistant_text="")
        store.imprint(
            turn1,
            {
                "content": "あいうえお",
                "conditions": ["猫", "犬"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "あいうえお 猫 犬 p/"},
            },
        )
        turn2 = sansho.Turn(user_text="かきくけこ 猫 p/", assistant_text="")
        r2 = store.imprint(
            turn2,
            {
                "content": "かきくけこ",
                "conditions": ["猫"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "かきくけこ 猫 p/"},
            },
        )
        self.assertFalse(r2["is_new"])
        self.assertEqual(r2["seed"]["strength"], 2)
        self.assertEqual(r2["seed"]["content"], "あいうえお")

    def test_t3f_similarity_just_below_threshold_does_not_merge(self):
        # 境界のすぐ下: 縁 jaccard = |{猫,犬,鳥}∩{猫,犬,魚,馬}| / |{猫,犬,鳥}∪{猫,犬,魚,馬}|
        # = 2/5 = 0.4。境界(0.5)により近い側に置く (閾値を 0.5->0.34 まで動かす mutant等
        # を境界の遠い fixture では見逃す)。
        store = self._new_store()
        turn1 = sansho.Turn(user_text="あいうえお 猫 犬 鳥 p/", assistant_text="")
        store.imprint(
            turn1,
            {
                "content": "あいうえお",
                "conditions": ["猫", "犬", "鳥"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "あいうえお 猫 犬 鳥 p/"},
            },
        )
        turn2 = sansho.Turn(user_text="かきくけこ 猫 犬 魚 馬 p/", assistant_text="")
        r2 = store.imprint(
            turn2,
            {
                "content": "かきくけこ",
                "conditions": ["猫", "犬", "魚", "馬"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "かきくけこ 猫 犬 魚 馬 p/"},
            },
        )
        self.assertTrue(r2["is_new"])
        self.assertEqual(len(store.list_seeds()), 2)

    def test_t3g_content_similarity_alone_does_not_merge_without_shared_condition(self):
        # #104 (c): content の文字2-gram類似度 (0.583) が閾値以上でも、縁を必要語数 (min(2, 小さい側の縁数)) 共有しなければ併合しない
        # (旧: 併合していた。max(content軸, 縁軸) の content 側だけで閾値を超える fixture)。
        store = self._new_store()
        turn1 = sansho.Turn(user_text="猫はこたつで丸くなる A p/", assistant_text="")
        store.imprint(
            turn1,
            {
                "content": "猫はこたつで丸くなる",
                "conditions": ["A"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "猫はこたつで丸くなる A p/"},
            },
        )
        turn2 = sansho.Turn(user_text="猫はこたつでまるくなる B p/", assistant_text="")
        r2 = store.imprint(
            turn2,
            {
                "content": "猫はこたつでまるくなる",
                "conditions": ["B"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "猫はこたつでまるくなる B p/"},
            },
        )
        self.assertTrue(r2["is_new"])
        self.assertEqual(r2["seed"]["strength"], 1)
        self.assertEqual(len(store.list_seeds()), 2)

    # --- T4: 未接地の縁は落ち、全落ちは却下+imprint_log に reason ---
    def test_t4_ungrounded_conditions_all_dropped_rejects_with_reason(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["宇宙人", "UFO"],  # どちらも往復本文に無い
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "猫が好きです"},  # span 自体は圓成實接地
        }
        result = store.imprint(turn, proposal)
        self.assertEqual(result["sansho"], "圓成實")  # span 自体は接地している
        self.assertEqual(result["decision"], "reject")
        self.assertIsNone(result["seed"])
        self.assertEqual(store.list_seeds(), [])
        self.assertEqual(store.list_quarantine(), [])  # 却下は quarantine にも入らない
        log = store.list_imprint_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["decision"], "reject")
        self.assertTrue(log[0]["reason"])

    def test_t4_partially_grounded_conditions_keep_only_grounded_ones(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫", "宇宙人"],  # 「猫」のみ本文に実在
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "猫が好きです p/"},
        }
        result = store.imprint(turn, proposal)
        self.assertEqual(result["decision"], "圓成實")
        self.assertEqual(result["seed"]["conditions"], ["猫"])

    # --- T5: imprint_log は追記専用 (件数単調増加・既存行不変) ---
    @staticmethod
    def _snapshot(row):
        # dict の浅いコピー同士を == で比べると、nested な turn/proposal が内部状態と
        # 同一オブジェクトを指したまま(別名化)になり、後から in-place で改竄されても
        # "before" 側のスナップショットまで一緒に化けて検出できない。json.dumps で
        # 文字列に凍結すれば、以後の内部改変から独立した値になる。
        return json.dumps(row, sort_keys=True, ensure_ascii=False)

    def test_t5_imprint_log_is_append_only_and_earlier_rows_unchanged(self):
        store = self._new_store()

        turn1 = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        store.imprint(
            turn1,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )
        log_after_1 = store.list_imprint_log()
        self.assertEqual(len(log_after_1), 1)
        snapshot_1 = self._snapshot(log_after_1[0])

        turn2 = sansho.Turn(user_text="今日は晴れ", assistant_text="")
        store.imprint(
            turn2,
            {
                "content": "捏造",
                "conditions": ["晴れ"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "捏造された文言"},  # 非接地
            },
        )
        log_after_2 = store.list_imprint_log()
        self.assertEqual(len(log_after_2), 2)
        self.assertEqual(self._snapshot(log_after_2[0]), snapshot_1)
        snapshot_2 = self._snapshot(log_after_2[1])

        turn3 = sansho.Turn(user_text="犬も好き p/ 犬", assistant_text="")
        store.imprint(
            turn3,
            {
                "content": "犬が好き",
                "conditions": ["宇宙人"],  # 往復本文に無く全滅 -> 却下
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "犬も好き p/"},
            },
        )
        log_after_3 = store.list_imprint_log()
        self.assertEqual(len(log_after_3), 3)
        self.assertEqual(self._snapshot(log_after_3[0]), snapshot_1)
        self.assertEqual(self._snapshot(log_after_3[1]), snapshot_2)
        self.assertEqual([e["decision"] for e in log_after_3], ["圓成實", "遍計所執", "reject"])

    def test_t5_list_imprint_log_returns_independent_deep_copies(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです 猫", assistant_text="")
        store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです"},
            },
        )
        log1 = store.list_imprint_log()
        log1[0]["proposal"]["content"] = "呼び出し側による改竄"
        log1[0]["turn"]["user_text"] = "呼び出し側による改竄"

        log2 = store.list_imprint_log()
        self.assertEqual(log2[0]["proposal"]["content"], "猫が好き")
        self.assertEqual(log2[0]["turn"]["user_text"], "猫が好きです 猫")

    def test_imprint_log_stored_proposal_is_isolated_from_caller_mutation(self):
        # 書き込み側: dict(proposal) は浅いコピーなので evidence/conditions が共有され、
        # 呼び出し側が imprint 後に proposal を書き換えるとログ(および次回 save)へ漏れる。
        # list_imprint_log の deepcopy (上のテスト) とは逆方向の pin。
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです 猫", assistant_text="")
        proposal = {
            "content": "猫が好き",
            "conditions": ["猫"],
            "polarity": "善","kind": "所在",
            "evidence": {"source": "user", "span": "猫が好きです"},
        }
        store.imprint(turn, proposal)
        proposal["content"] = "呼び出し側による改竄"
        proposal["conditions"].append("改竄条件")
        proposal["evidence"]["span"] = "呼び出し側による改竄"

        log = store.list_imprint_log()
        self.assertEqual(log[0]["proposal"]["content"], "猫が好き")
        self.assertEqual(log[0]["proposal"]["conditions"], ["猫"])
        self.assertEqual(log[0]["proposal"]["evidence"]["span"], "猫が好きです")

    # --- T6: 発芽順=クラス→強度 (クラスが強度に優先する。ラベル描画は test_manas.py 側) ---
    def test_t6_germination_order_class_beats_strength(self):
        store = self._new_store()
        # 依他起 (assistant 接地) を strength=5 まで育てる (新規1 + 併合4)
        assistant_turn = sansho.Turn(user_text="共通語について", assistant_text="推測です 共通語")
        etaki_proposal = {
            "content": "アシスタント由来の推測",
            "conditions": ["共通語"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "assistant", "span": "推測です"},
        }
        r = None
        for _ in range(5):
            r = store.imprint(assistant_turn, etaki_proposal)
        self.assertEqual(r["sansho"], "依他起")
        self.assertEqual(r["seed"]["strength"], 5)

        # 圓成實 (user 接地) は strength=2 のまま (新規1 + 併合1)
        user_turn = sansho.Turn(user_text="共通語について 事実です p/", assistant_text="")
        enjoujitsu_proposal = {
            "content": "ユーザー由来の事実",
            "conditions": ["共通語"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "共通語について 事実です p/"},
        }
        r2 = None
        for _ in range(2):
            r2 = store.imprint(user_turn, enjoujitsu_proposal)
        self.assertEqual(r2["sansho"], "圓成實")
        self.assertEqual(r2["seed"]["strength"], 2)

        fired = store.germinate("共通語")
        self.assertEqual(len(fired), 2)
        # strength は 依他起(5) > 圓成實(2) だが、クラスが強度に優先するため圓成實が先。
        self.assertEqual(fired[0]["sansho"], "圓成實")
        self.assertEqual(fired[0]["strength"], 2)
        self.assertEqual(fired[1]["sansho"], "依他起")
        self.assertEqual(fired[1]["strength"], 5)

    # --- T7: 熏習 API に往復を渡さない呼び出しは型エラー ---
    def test_t7_imprint_without_turn_raises_type_error(self):
        store = self._new_store()
        proposal = {
            "content": "x",
            "conditions": ["x"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "x"},
        }
        # 末那識注入文・内観自己申告文はいずれも単なる str であり、Turn ではない。
        with self.assertRaises(TypeError):
            store.imprint("私はこれまでの対話からこう理解している", proposal)

    def test_t7_imprint_with_dict_masquerading_as_turn_raises_type_error(self):
        store = self._new_store()
        proposal = {
            "content": "x",
            "conditions": ["x"],
            "polarity": "無記","kind": "所在",
            "evidence": {"source": "user", "span": "x"},
        }
        fake_turn = {"user_text": "x", "assistant_text": "x"}  # Turn に見えるだけの plain dict
        with self.assertRaises(TypeError):
            store.imprint(fake_turn, proposal)

    # --- quarantine は上限付き (記憶機能設計書§3.4)。超過は古い順に落とす ---
    def test_quarantine_is_capped(self):
        store = self._new_store()
        for i in range(QUARANTINE_MAX + 1):
            turn = sansho.Turn(user_text="今日は天気がいいですね", assistant_text="")
            store.imprint(
                turn,
                {
                    "content": "捏造{}".format(i),
                    "conditions": [],
                    "polarity": "無記","kind": "所在",
                    "evidence": {"source": "user", "span": "捏造span{}".format(i)},  # 非接地
                },
            )
        quarantine = store.list_quarantine()
        self.assertEqual(len(quarantine), QUARANTINE_MAX)
        # 古い順に落ちる (FIFO): 最初 (id=1) が消え、直近 QUARANTINE_MAX 件が残る。
        self.assertEqual([q["id"] for q in quarantine], list(range(2, QUARANTINE_MAX + 2)))

    # --- 不正な polarity は無記へ正規化される ---
    def test_invalid_polarity_normalizes_to_mukiroku(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです 猫", assistant_text="")
        result = store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "変な値",  # VALID_POLARITIES に無い
                "evidence": {"source": "user", "span": "猫が好きです"},
            },
        )
        self.assertEqual(result["seed"]["polarity"], "無記")

    # --- list_seeds/get_by_ids/list_quarantine/germinate/imprint の result["seed"] を
    #     list_imprint_log と同型の deep copy に揃える (穴は各1本) ---
    def test_med_a_list_seeds_returns_independent_deep_copies(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )
        seeds1 = store.list_seeds()
        seeds1[0]["evidences"][0]["span"] = "呼び出し側による改竄"
        seeds1[0]["conditions"].append("改竄条件")

        seeds2 = store.list_seeds()
        self.assertEqual(seeds2[0]["evidences"][0]["span"], "猫が好きです p/")
        self.assertEqual(seeds2[0]["conditions"], ["猫"])

    def test_med_a_get_by_ids_returns_independent_deep_copies(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        r1 = store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )
        seed_id = r1["seed"]["id"]

        got1 = store.get_by_ids([seed_id])
        got1[0]["evidences"][0]["span"] = "呼び出し側による改竄"
        got1[0]["conditions"].append("改竄条件")

        got2 = store.get_by_ids([seed_id])
        self.assertEqual(got2[0]["evidences"][0]["span"], "猫が好きです p/")
        self.assertEqual(got2[0]["conditions"], ["猫"])

    def test_med_a_list_quarantine_returns_independent_deep_copies(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="今日は晴れです", assistant_text="")
        store.imprint(
            turn,
            {
                "content": "捏造された記憶",
                "conditions": ["晴れ"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "捏造された非接地の文言です"},
            },
        )
        q1 = store.list_quarantine()
        q1[0]["conditions"].append("改竄条件")

        q2 = store.list_quarantine()
        self.assertEqual(q2[0]["conditions"], ["晴れ"])

    def test_med_a_germinate_returns_independent_deep_copies(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )
        store.imprint(
            turn,
            {
                "content": "やはり猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )  # strength=2 (発芽閾値に到達)

        fired1 = store.germinate("猫について")
        fired1[0]["evidences"][0]["span"] = "呼び出し側による改竄"
        fired1[0]["conditions"].append("改竄条件")

        fired2 = store.germinate("猫について")  # 閾値さえ超えていれば何度でも再発火する
        self.assertEqual(fired2[0]["evidences"][0]["span"], "猫が好きです p/")
        self.assertEqual(fired2[0]["conditions"], ["猫"])

    def test_med_a_imprint_result_seed_is_independent_deep_copy(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        r1 = store.imprint(
            turn,
            {
                "content": "猫が好き",
                "conditions": ["猫"],
                "polarity": "善","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )
        r1["seed"]["evidences"][0]["span"] = "呼び出し側による改竄"
        r1["seed"]["conditions"].append("改竄条件")

        seeds_after = store.list_seeds()
        self.assertEqual(seeds_after[0]["evidences"][0]["span"], "猫が好きです p/")
        self.assertEqual(seeds_after[0]["conditions"], ["猫"])

    def test_med_a_index_returns_independent_deep_copies(self):
        store = self._new_store()
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        store.imprint(
            turn,
            {
                "content": "猫が好きです",
                "conditions": ["猫"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "猫が好きです p/"},
            },
        )
        idx = store.index()
        idx[0]["evidences"][0]["span"] = "呼び出し側による改竄"
        idx[0]["conditions"].append("改竄条件")

        self.assertEqual(store.index()[0]["evidences"][0]["span"], "猫が好きです p/")
        self.assertEqual(store.index()[0]["conditions"], ["猫"])

    # --- quarantine_total() = 保持上限による evict と無関係な累計件数 ---
    def test_quarantine_total_counts_beyond_the_retention_cap(self):
        store = self._new_store()
        for i in range(QUARANTINE_MAX + 3):
            turn = sansho.Turn(user_text="今日は天気がいいですね", assistant_text="")
            store.imprint(
                turn,
                {
                    "content": "捏造{}".format(i),
                    "conditions": [],
                    "polarity": "無記","kind": "所在",
                    "evidence": {"source": "user", "span": "捏造span{}".format(i)},  # 非接地
                },
            )
        # 保持 (list_quarantine) は上限で頭打ちだが、累計 (quarantine_total) は
        # evict されても減らない -- 2数字が相異なることを確認する。
        self.assertEqual(len(store.list_quarantine()), QUARANTINE_MAX)
        self.assertEqual(store.quarantine_total(), QUARANTINE_MAX + 3)
        self.assertNotEqual(len(store.list_quarantine()), store.quarantine_total())


class TestImprintWindowAndActions(unittest.TestCase):
    """改修4 §5: 窓に対する熏習、supersede / reinforce、history。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = alaya_store_at(os.path.join(self.tmpdir, "alaya.json"), os.path.join(self.tmpdir, "imprint_log.json"))
        self.store.begin_new_life()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _ledger_window(self):
        from raiya.ledger import LedgerTurn

        return [
            LedgerTurn(user_text="猫が好きです p/", assistant_text="いいですね", id=10, life=1),
            LedgerTurn(user_text="コーヒーより緑茶", assistant_text="緑茶ですね", id=11, life=1),
        ]

    def test_imprint_over_ledger_window_records_ledger_ids_and_strength_is_distinct_turn_count(self):
        r = self.store.imprint(
            self._ledger_window(),
            {
                "content": "猫と緑茶が好き",
                "conditions": ["猫", "緑茶"],
                "polarity": "善","kind": "所在",
                "evidence": [{"source": "user", "span": "猫が好きです p/"}, {"source": "user", "span": "コーヒーより緑茶"}],
            },
        )
        self.assertEqual(r["decision"], "圓成實")
        self.assertTrue(r["is_new"])
        seed = r["seed"]
        self.assertEqual(seed["strength"], 2)
        self.assertEqual([e["ledger_id"] for e in seed["evidences"]], [10, 11])
        self.assertEqual([e["source"] for e in seed["evidences"]], ["user", "user"])
        self.assertNotIn("turn", seed["evidences"][0])  # 台帳にある往復はスナップショットを持たない
        self.assertEqual(seed["history"], [])
        log = self.store.list_imprint_log()[-1]
        self.assertEqual(log["turn"], {"ledger_ids": [10, 11]})

    def test_plain_single_turn_keeps_legacy_evidence_and_log_shape(self):
        turn = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r = self.store.imprint(turn, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": {"source": "user", "span": "猫が好き p/"}})
        ev = r["seed"]["evidences"][0]
        self.assertIsNone(ev["ledger_id"])
        self.assertEqual(ev["turn"], {"user_text": "猫が好き p/ 猫", "assistant_text": ""})
        self.assertEqual(self.store.list_imprint_log()[-1]["turn"], {"user_text": "猫が好き p/ 猫", "assistant_text": ""})
        self.assertEqual(r["seed"]["strength"], 1)

    def test_plain_multi_turn_window_logs_snapshots_list(self):
        window = [sansho.Turn(user_text="猫 猫", assistant_text=""), sansho.Turn(user_text="犬 犬", assistant_text="")]
        self.store.imprint(window, {"content": "猫犬", "conditions": ["猫"], "polarity": "無記","kind": "所在",
                                    "evidence": [{"source": "user", "span": "猫"}, {"source": "user", "span": "犬"}]})
        self.assertEqual(
            self.store.list_imprint_log()[-1]["turn"],
            {"turns": [{"user_text": "猫 猫", "assistant_text": ""}, {"user_text": "犬 犬", "assistant_text": ""}]},
        )

    def test_supersede_replaces_content_and_keeps_history(self):
        turn1 = sansho.Turn(user_text="コーヒーが好き p/ コーヒー", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "コーヒーが好き", "conditions": ["コーヒー"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "コーヒーが好き p/"}})
        seed_id = r1["seed"]["id"]
        turn2 = sansho.Turn(user_text="最近はコーヒーより緑茶 p/ 緑茶", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": seed_id, "content": "緑茶が好き",
                                        "conditions": ["緑茶"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "最近はコーヒーより緑茶 p/"}})
        self.assertFalse(r2["is_new"])
        self.assertTrue(r2.get("superseded"))
        seed = self.store.get_by_ids([seed_id])[0]
        self.assertEqual(seed["content"], "緑茶が好き")
        self.assertEqual(seed["conditions"], ["緑茶"])
        self.assertEqual(seed["strength"], 2)
        self.assertEqual(len(seed["evidences"]), 2)
        self.assertEqual(len(seed["history"]), 1)
        self.assertEqual(seed["history"][0]["content"], "コーヒーが好き")
        self.assertEqual(seed["history"][0]["conditions"], ["コーヒー"])
        self.assertEqual(seed["history"][0]["sansho"], "圓成實")
        self.assertEqual(seed["history"][0]["reason"], "superseded")
        self.assertEqual(self.store.seed_count(), 1)

    def test_etaki_cannot_supersede_enjoujitsu_and_falls_back_to_new_with_note(self):
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き p/"}})
        turn2 = sansho.Turn(user_text="x 猫", assistant_text="犬が好きなのでしょう 猫")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": r1["seed"]["id"], "content": "犬が好き",
                                        "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "assistant", "span": "犬が好きなのでしょう"}})
        self.assertEqual(r2["decision"], "依他起")
        self.assertTrue(r2["is_new"])
        self.assertIn("supersede 不可", r2["reason"])
        self.assertIn("越境", r2["reason"])
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["content"], "猫が好き")
        self.assertIn("supersede 不可", self.store.list_imprint_log()[-1]["reason"])

    def test_enjoujitsu_can_supersede_etaki_and_class_follows_proposal(self):
        turn1 = sansho.Turn(user_text="x 推定", assistant_text="犬が好きなのでしょう 推定")
        r1 = self.store.imprint(turn1, {"content": "犬が好き(推定)", "conditions": ["推定"], "polarity": "無記","kind": "所在",
                                        "evidence": {"source": "assistant", "span": "犬が好きなのでしょう"}})
        turn2 = sansho.Turn(user_text="猫が好きです 推定 p/", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": r1["seed"]["id"], "content": "猫が好き",
                                        "conditions": ["推定"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好きです 推定 p/"}})
        self.assertTrue(r2.get("superseded"))
        seed = self.store.get_by_ids([r1["seed"]["id"]])[0]
        self.assertEqual(seed["sansho"], "圓成實")
        self.assertEqual(seed["polarity"], "善")
        self.assertEqual(seed["history"][0]["sansho"], "依他起")

    def test_supersede_with_unknown_target_falls_back_to_new(self):
        turn = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r = self.store.imprint(turn, {"action": "supersede", "target_id": 999, "content": "猫が好き",
                                      "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": {"source": "user", "span": "猫が好き p/"}})
        self.assertTrue(r["is_new"])
        self.assertIn("supersede 不可", r["reason"])
        self.assertIn("999", r["reason"])

    def test_reinforce_adds_strength_and_keeps_content(self):
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き p/"}})
        turn2 = sansho.Turn(user_text="猫は最高 p/ 猫", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "reinforce", "target_id": r1["seed"]["id"], "content": "全然違う文",
                                        "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫は最高 p/"}})
        self.assertFalse(r2["is_new"])
        self.assertTrue(r2.get("reinforced"))
        seed = self.store.get_by_ids([r1["seed"]["id"]])[0]
        self.assertEqual(seed["content"], "猫が好き")  # content 不変
        self.assertEqual(seed["strength"], 2)
        self.assertEqual(seed["history"], [])

    def test_assistant_only_reinforce_onto_user_span_target_is_recorded_only_even_if_dissimilar(self):
        # #105 D: 越境 (依他起 → 圓成實) でも、内容が似ていなくても、user の逐語を持つ種子には記録のみ。
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き p/"}})
        turn2 = sansho.Turn(user_text="x 犬", assistant_text="犬は静かなのでしょう 犬")
        r2 = self.store.imprint(turn2, {"action": "reinforce", "target_id": r1["seed"]["id"], "content": "犬は静か(推定)",
                                        "conditions": ["犬"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "assistant", "span": "犬は静かなのでしょう"}})
        self.assertIsNone(r2["is_new"])
        self.assertIsNone(r2["seed"])
        self.assertEqual(r2["user_span_match"], r1["seed"]["id"])
        self.assertIn("記録のみ", r2["reason"])
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["strength"], 1)
        self.assertEqual(len(self.store.list_seeds()), 1)

    def test_reinforce_with_class_mismatch_on_target_without_user_span_falls_back_to_new(self):
        turn1 = sansho.Turn(user_text="x 猫", assistant_text="猫が好きなのでしょう 猫")
        r1 = self.store.imprint(turn1, {"content": "猫が好き(推定)", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "assistant", "span": "猫が好きなのでしょう"}})
        self.assertEqual(r1["seed"]["sansho"], "依他起")
        turn2 = sansho.Turn(user_text="犬が好き p/ 犬", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "reinforce", "target_id": r1["seed"]["id"], "content": "犬が好き",
                                        "conditions": ["犬"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "犬が好き p/"}})
        self.assertTrue(r2["is_new"])
        self.assertIn("reinforce 不可", r2["reason"])
        self.assertEqual(self.store.get_by_ids([r1["seed"]["id"]])[0]["strength"], 1)

    def _mixed_window_and_proposal(self):
        window = [
            sansho.Turn(user_text="猫が好きです", assistant_text="いいですね"),
            sansho.Turn(user_text="今日は雨", assistant_text="猫は散歩が不要ですね"),
        ]
        proposal = {
            "content": "猫が好きで散歩は不要",
            "conditions": ["猫"],
            "polarity": "善",
            "kind": "所在",
            "evidence": [
                {"source": "user", "span": "猫が好きです"},
                {"source": "assistant", "span": "散歩が不要ですね"},
            ],
        }
        return window, proposal

    def test_imprint_mixed_user_assistant_spans_plants_etaki_seed(self):
        # 2026-10-22 裁定: user 接地 + assistant のみ接地の混在は依他起 (全 span 圓成實のときだけ圓成實)。
        window, proposal = self._mixed_window_and_proposal()
        r = self.store.imprint(window, proposal)
        self.assertEqual(r["decision"], "依他起")
        self.assertTrue(r["is_new"])
        seed = r["seed"]
        self.assertEqual(seed["sansho"], "依他起")
        self.assertEqual([e["source"] for e in seed["evidences"]], ["user", "assistant"])
        self.assertEqual(seed["strength"], 2)  # 窓内の distinct 往復数 (2 往復に接地)

    def test_imprint_mixed_spans_does_not_merge_into_enjoujitsu_seed(self):
        window, proposal = self._mixed_window_and_proposal()
        base = _imprint(self.store, proposal["content"], ["猫"], polarity="善")
        base_seed = base["seed"]
        self.assertEqual(base_seed["sansho"], "圓成實")
        before = self.store.get_by_ids([base_seed["id"]])[0]
        r = self.store.imprint(window, proposal)
        self.assertEqual(r["decision"], "依他起")
        self.assertTrue(r["is_new"])
        self.assertNotEqual(r["seed"]["id"], base_seed["id"])
        self.assertFalse(r.get("reinforced"))
        after = self.store.get_by_ids([base_seed["id"]])[0]
        self.assertEqual(after["strength"], before["strength"])
        self.assertEqual(after["evidences"], before["evidences"])

    def test_unknown_action_is_treated_as_new(self):
        turn = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r = self.store.imprint(turn, {"action": "delete", "content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": {"source": "user", "span": "猫が好き p/"}})
        self.assertTrue(r["is_new"])
        self.assertNotIn("superseded", r)
        self.assertNotIn("reinforced", r)
        self.assertNotIn("不可", r["reason"])  # 未知の action は黙って new。注記は付かない

    def test_imprint_rejects_non_turn_window(self):
        with self.assertRaises(TypeError):
            self.store.imprint("末那識の注入文", {"content": "x", "evidence": {"span": "x"}})
        with self.assertRaises(TypeError):
            self.store.imprint([], {"content": "x", "evidence": {"span": "x"}})

    def test_evidence_source_is_derived_from_verdict_not_from_self_report(self):
        """EXE-15 身分は自称できない: evidences[].source は三性判定から導出する。

        提案が "source": "user" と自称していても、span がアシスタント発話にしか接地して
        いなければ依他起であり、記録される source は "assistant" になる。spans[] には
        自称値 (span_result["source"]) が隣に並んでいるため、そちらを読んでしまう改変を
        このテストで塞ぐ。
        """
        turn = sansho.Turn(user_text="猫", assistant_text="犬が好きなのでしょう 猫")
        r = self.store.imprint(turn, {"content": "犬が好き", "conditions": ["猫"], "polarity": "無記","kind": "所在",
                                      "evidence": {"source": "user", "span": "犬が好きなのでしょう"}})
        self.assertEqual(r["decision"], "依他起")
        self.assertEqual(r["spans"][0]["source"], "user")  # 自称はそのまま spans に残るが…
        self.assertEqual(r["seed"]["evidences"][0]["source"], "assistant")  # …種子には判定が入る

    def test_result_grounded_conditions_is_not_aliased_to_stored_seed(self):
        """result["grounded_conditions"] は種子の縁と同一オブジェクトであってはならない。

        同一だと、呼び出し側が返り値のリストに append しただけで生きた種子の発芽条件が
        書き換わる (しかも次の save() でディスクにも載る)。
        """
        turn = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r = self.store.imprint(turn, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": {"source": "user", "span": "猫が好き p/"}})
        r["grounded_conditions"].append("呼び出し側による改竄")
        self.assertEqual(self.store.list_seeds()[0]["conditions"], ["猫"])

        turn2 = sansho.Turn(user_text="緑茶が好き p/ 緑茶", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": r["seed"]["id"],
                                        "content": "緑茶が好き", "conditions": ["緑茶"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "緑茶が好き p/"}})
        self.assertTrue(r2.get("superseded"))
        r2["grounded_conditions"].append("呼び出し側による改竄")
        self.assertEqual(self.store.get_by_ids([r["seed"]["id"]])[0]["conditions"], ["緑茶"])

    def test_bool_target_id_does_not_match_seed_id_one(self):
        """bool は int の派生型で True == 1。target_id: true が id=1 の種子を書き換えないこと。"""
        turn1 = sansho.Turn(user_text="猫が好き 猫 p/", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き 猫 p/"}})
        self.assertEqual(r1["seed"]["id"], 1)
        turn2 = sansho.Turn(user_text="犬が好き 猫 p/", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": True, "content": "犬が好き",
                                        "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "犬が好き 猫 p/"}})
        seed = self.store.get_by_ids([1])[0]
        self.assertEqual(seed["content"], "猫が好き")
        self.assertEqual(seed["history"], [])
        self.assertIn("supersede 不可", r2["reason"])

    def test_supersede_across_polarity_replaces_polarity_and_records_old_in_history(self):
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き p/"}})
        turn2 = sansho.Turn(user_text="猫は苦手になった p/ 猫", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": r1["seed"]["id"],
                                        "content": "猫が苦手", "conditions": ["猫"], "polarity": "不善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫は苦手になった p/"}})
        self.assertTrue(r2.get("superseded"))
        seed = self.store.get_by_ids([r1["seed"]["id"]])[0]
        self.assertEqual(seed["polarity"], "不善")
        self.assertEqual(seed["history"][0]["polarity"], "善")
        self.assertEqual(self.store.seed_count(), 1)

    def test_supersede_with_no_grounded_conditions_is_rejected_and_target_untouched(self):
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き p/"}})
        turn2 = sansho.Turn(user_text="緑茶が好き p/", assistant_text="")
        r2 = self.store.imprint(turn2, {"action": "supersede", "target_id": r1["seed"]["id"],
                                        "content": "緑茶が好き", "conditions": ["宇宙"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "緑茶が好き p/"}})
        self.assertEqual(r2["decision"], "reject")
        seed = self.store.get_by_ids([r1["seed"]["id"]])[0]
        self.assertEqual(seed["content"], "猫が好き")
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(seed["history"], [])
        self.assertEqual(len(seed["evidences"]), 1)
        self.assertEqual(self.store.list_imprint_log()[-1]["decision"], "reject")

    def test_mixed_ledger_and_plain_window_records_per_turn_ledger_id(self):
        from raiya.ledger import LedgerTurn

        window = [
            LedgerTurn(user_text="猫が好きです p/", assistant_text="", id=7, life=1),
            sansho.Turn(user_text="犬も好き p/", assistant_text=""),
        ]
        r = self.store.imprint(window, {"content": "動物が好き", "conditions": ["猫", "犬"], "polarity": "善","kind": "所在",
                                        "evidence": [{"source": "user", "span": "猫が好きです p/"},
                                                     {"source": "user", "span": "犬も好き p/"}]})
        self.assertEqual(
            self.store.list_imprint_log()[-1]["turn"],
            {"turns": [{"user_text": "猫が好きです p/", "assistant_text": ""},
                       {"user_text": "犬も好き p/", "assistant_text": ""}]},
        )
        evidences = r["seed"]["evidences"]
        self.assertEqual([e["ledger_id"] for e in evidences], [7, None])
        self.assertNotIn("turn", evidences[0])  # 台帳にある往復は id で指す
        self.assertEqual(evidences[1]["turn"], {"user_text": "犬も好き p/", "assistant_text": ""})

    def test_two_spans_in_one_turn_add_one_strength_but_two_evidences(self):
        # strength は span の本数ではなく接地した往復の数 (改修4)。同じ一往復から
        # 引用を 2 本並べるだけで裏付けの厚みが増える (依他起が閾値 2 に届く) のは、
        # 提案側 (LLM) が発芽を決められるということなので数えない。
        turn = sansho.Turn(user_text="猫が好きです p/ 犬も好き p/ 猫", assistant_text="")
        r = self.store.imprint(turn, {"content": "動物が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": [{"source": "user", "span": "猫が好きです p/"},
                                                   {"source": "user", "span": "犬も好き p/"}]})
        seed = r["seed"]
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(len(seed["evidences"]), 2)  # 別々の引用はどちらも記録する
        snapshot = {"user_text": "猫が好きです p/ 犬も好き p/ 猫", "assistant_text": ""}
        for evidence in seed["evidences"]:
            self.assertIsNone(evidence["ledger_id"])
            self.assertEqual(evidence["turn"], snapshot)

    def test_duplicate_spans_in_one_proposal_count_once(self):
        # 同じ span を 2 回挙げた提案 (LLM がそう書きうる) は、evidences も strength も
        # 1 件ぶんに畳む。比較は三性ゲートと同じ正規化なので空白差だけの重複も畳まれる。
        turn = sansho.Turn(user_text="猫が好きです p/ 猫", assistant_text="")
        r = self.store.imprint(turn, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": [{"source": "user", "span": "猫が好きです p/"},
                                                   {"source": "user", "span": "猫が好きです p/"}]})
        seed = r["seed"]
        self.assertEqual(seed["strength"], 1)
        self.assertEqual(len(seed["evidences"]), 1)
        self.assertEqual(seed["evidences"][0]["span"], "猫が好きです p/")

    def test_etaki_needs_two_different_turns_to_germinate(self):
        # 発芽閾値 2 の依他起が「一往復の中で 2 回引用された」だけで発芽しないこと
        # (F4 の動機そのもの)。別の往復で裏付けが付いて初めて発芽する。
        from raiya.ledger import LedgerTurn

        one_turn = [LedgerTurn(user_text="教えて 散歩", assistant_text="犬には散歩が必要です 散歩", id=21, life=1)]
        r1 = self.store.imprint(one_turn, {"content": "犬には散歩が要る", "conditions": ["散歩"], "polarity": "無記","kind": "所在",
                                           "evidence": [{"source": "assistant", "span": "犬には散歩が必要です"},
                                                        {"source": "assistant", "span": "散歩が必要です"}]})
        self.assertEqual(r1["decision"], "依他起")
        self.assertEqual(r1["seed"]["strength"], 1)
        self.assertEqual(self.store.germinate("散歩の話"), [])  # 1 < 閾値 2 で眠ったまま
        two_turns = one_turn + [
            LedgerTurn(user_text="そうなの 散歩", assistant_text="はい、散歩が必要です 散歩", id=22, life=1)
        ]
        self.store.imprint(two_turns, {"content": "犬には散歩が要る", "conditions": ["散歩"], "polarity": "無記","kind": "所在",
                                       "evidence": [{"source": "assistant", "span": "はい、散歩が必要です"}]})
        self.assertEqual([s["strength"] for s in self.store.germinate("散歩の話")], [2])

    def test_reinforce_record_only_note_reaches_imprint_log(self):
        turn1 = sansho.Turn(user_text="猫が好き p/ 猫", assistant_text="")
        r1 = self.store.imprint(turn1, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "user", "span": "猫が好き p/"}})
        turn2 = sansho.Turn(user_text="x 犬", assistant_text="犬は静かなのでしょう 犬")
        r2 = self.store.imprint(turn2, {"action": "reinforce", "target_id": r1["seed"]["id"],
                                        "content": "犬は静か(推定)", "conditions": ["犬"], "polarity": "善","kind": "所在",
                                        "evidence": {"source": "assistant", "span": "犬は静かなのでしょう"}})
        log_reason = self.store.list_imprint_log()[-1]["reason"]
        self.assertIn("記録のみ", log_reason)
        self.assertEqual(r2["reason"], log_reason)

    def test_ledger_turn_without_a_row_id_keeps_its_snapshot(self):
        """LedgerTurn の既定値 id=0 は「まだ台帳に記録されていない」の意味。

        台帳の id は next_id=1 始まりで 0 を配らないため、0 を行番号として記録すると
        存在し得ない行を指す evidence になる。ledger_id は None に倒し、本文の
        スナップショットを保持する側へ落とす (imprint_log の turn 欄も同じ扱い)。
        """
        from raiya.ledger import LedgerTurn

        turn = LedgerTurn(user_text="猫が好き p/ 猫", assistant_text="", id=0, life=1)
        r = self.store.imprint(turn, {"content": "猫が好き", "conditions": ["猫"], "polarity": "善","kind": "所在",
                                      "evidence": {"source": "user", "span": "猫が好き p/"}})
        evidence = r["seed"]["evidences"][0]
        self.assertIsNone(evidence["ledger_id"])
        self.assertEqual(evidence["turn"], {"user_text": "猫が好き p/ 猫", "assistant_text": ""})
        self.assertEqual(self.store.list_imprint_log()[-1]["turn"],
                         {"user_text": "猫が好き p/ 猫", "assistant_text": ""})


if __name__ == "__main__":
    unittest.main()
