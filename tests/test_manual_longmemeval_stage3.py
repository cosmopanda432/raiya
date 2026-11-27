"""scripts/manual_longmemeval_stage3.py (LongMemEval 段 3・実 LLM は手動 1 本) の決定論のテスト。

実 LLM は呼ばない。`raiya.backend.call_llm` を偽の関数に差し替える (ドライラン)。データ本文は使わず、LongMemEval 形の
欄だけを真似た作文を使う。状態・出力は自作の tempfile.mkdtemp の下だけに作り、自分で作った dir だけを消す。
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import manual_longmemeval_stage3 as m3  # noqa: E402
from longmemeval_stage1 import DataError  # noqa: E402

_ENV_KEYS = ("RAIYA_CONFIG", "RAIYA_INJECT_DISPOSITIONS", "RAIYA_BACKEND", "RAIYA_MODEL", "RAIYA_PROPOSE_MODEL")


def _t(role, content, ha=None):
    d = {"role": role, "content": content}
    if ha is not None:
        d["has_answer"] = ha
    return d


def _q(qid, qtype, question, answer, sessions, dates=None, session_ids=None):
    n = len(sessions)
    return {
        "question_id": qid, "question_type": qtype, "question": question, "answer": answer,
        "question_date": "2023/06/01 (Thu) 09:00",
        "answer_session_ids": ["s_new"],
        "haystack_dates": dates if dates is not None else ["2023/05/20 (Sat) 10:00"] * n,
        "haystack_session_ids": session_ids if session_ids is not None else ["s{}".format(i) for i in range(n)],
        "haystack_sessions": sessions,
    }


_TOPICS = (
    ("Please recommend a pasta recipe with mushrooms.", "Try garlic mushroom pasta."),
    ("How do I repot a fern in spring?", "Use fresh soil and a wider pot."),
    ("Suggest a movie about space travel.", "Try the classic about astronauts."),
    ("What is a good name for a sourdough starter?", "Bubbles is cute."),
    ("Explain how tides work in simple words.", "The moon pulls on the ocean."),
)


def _session(sentence, tag):
    """has_answer でない 5 往復の後に、根拠の 1 往復を置いた 1 セッション (6 往復)。"""
    turns = []
    for i, (u, a) in enumerate(_TOPICS):
        turns += [_t("user", "{} ({}{})".format(u, tag, i), False), _t("assistant", a, False)]
    turns += [_t("user", sentence, True), _t("assistant", "Noted, thanks for telling me.", False)]
    return turns


OLD_SENTENCE = "My 5K personal best is 27:12."
NEW_SENTENCE = "My 5K personal best is now 25:50."


def _ku1(prefix="", qid="ku1"):
    # list の順は s_new -> s_old (日付と逆)
    return _q(qid, "knowledge-update", "What is my current 5K personal best?", "25:50",
              [_session(NEW_SENTENCE, prefix + "n"), _session(OLD_SENTENCE, prefix + "o")],
              dates=["2023/05/20 (Sat) 10:00", "2023/05/01 (Mon) 10:00"], session_ids=["s_new", "s_old"])


def _abs_marked():
    return _q("abs_m_abs", "multi-session", "What is the name of my hamster?", "You did not mention it.",
              [[_t("user", "I adopted a cat named Luna.", True), _t("assistant", "Lovely name.", False)]],
              session_ids=["s_cat"])


def _abs_unmarked():
    return _q("abs_u_abs", "multi-session", "Which pottery wheel did I buy?", "You did not mention it.",
              [[_t("user", "Hello there, how are you?", False), _t("assistant", "Fine, thanks.", False)]],
              session_ids=["s_hello"])


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raiya_lme3_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["RAIYA_CONFIG"] = ""
        for k in ("RAIYA_INJECT_DISPOSITIONS", "RAIYA_BACKEND", "RAIYA_MODEL", "RAIYA_PROPOSE_MODEL"):
            os.environ.pop(k, None)
        self.env_before = {k: os.environ.get(k) for k in _ENV_KEYS}
        # 実ネットワーク・実プロセスの経路は全部塞ぐ (偽の call_llm だけが呼ばれる)
        for name in ("_call_openai_api", "_call_anthropic_api", "_call_claude_cli", "_call_command"):
            p = mock.patch("raiya.backend." + name, side_effect=AssertionError("network"))
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.assertEqual(self.env_before, {k: os.environ.get(k) for k in _ENV_KEYS})


class TestSelection(_Base):
    def test_select_keeps_ku_and_abs_once(self):
        data = [
            _q("a", "knowledge-update", "q", "x", [[_t("user", "hi there")]]),
            _q("b_abs", "multi-session", "q", "x", [[_t("user", "hi there")]]),
            _q("c_abs", "knowledge-update", "q", "x", [[_t("user", "hi there")]]),
            _q("d", "single-session-user", "q", "x", [[_t("user", "hi there")]]),
        ]
        self.assertEqual([q["question_id"] for q in m3.select_questions(data)], ["a", "b_abs", "c_abs"])

    def test_group(self):
        self.assertEqual(m3.question_group(_ku1()), "ku")
        self.assertEqual(m3.question_group(_abs_marked()), "abs_marked")
        self.assertEqual(m3.question_group(_abs_unmarked()), "abs_unmarked")
        self.assertEqual(m3.GROUPS, ("ku", "abs_marked", "abs_unmarked"))

    def test_session_order_sorts_by_date_and_keeps_ties(self):
        self.assertEqual(m3.session_order(_ku1()), [1, 0])
        q = _q("t", "knowledge-update", "q", "x", [[], [], []],
               dates=["2023/05/02 (Tue) 10:00", "2023/05/01 (Mon) 10:00", "2023/05/02 (Tue) 10:00"])
        self.assertEqual(m3.session_order(q), [1, 0, 2])

    def test_missing_dates_fail_closed(self):
        good = _ku1()
        m3.validate_stage3([good])   # 正しい形は通る
        for label, mutate in (
            ("欠け", lambda q: q.pop("haystack_dates")),
            ("長さ違い", lambda q: q.__setitem__("haystack_dates", ["2023/05/01 (Mon) 10:00"])),
            ("str でない", lambda q: q.__setitem__("haystack_dates", ["2023/05/01 (Mon) 10:00", 5])),
            ("question_date が str でない", lambda q: q.__setitem__("question_date", 5)),
        ):
            q = copy.deepcopy(good)
            mutate(q)
            with self.subTest(label), self.assertRaises(DataError):
                m3.validate_stage3([q])


# --- 偽の LLM と、状態つきの土台 (T2 以降) -----------------------------------------------------------


def _prop(kind, content, span, conditions, action="new", target_id=None, source="user"):
    return {"kind": kind, "action": action, "target_id": target_id, "content": content, "conditions": conditions,
            "polarity": "無記", "evidence": [{"source": source, "span": span}]}


def _ledger_part(prompt):
    """統合プロンプトのうち【台帳】(窓の往復) だけ。既存の記録の欄にある文字列に台本が当たらないようにする。"""
    head, _, tail = prompt.partition("【台帳】\n")
    return tail.partition("\n\n次のJSON")[0]


def _fake_llm(script=(), answer="I don't know.", calls=None, hook=None):
    """purpose == "propose": 台帳の部分に needle が在る最初の台本の提案を返す (無ければ [])。purpose is None: answer を返す。
    hook(purpose, prompt) が None 以外を返せばそれを返す (例外を送出させてもよい)。"""
    calls = calls if calls is not None else []

    prompts = []

    def call_llm(prompt, max_tokens=1024, purpose=None):
        calls.append((purpose, prompt[:200]))
        prompts.append(prompt)
        if hook is not None:
            got = hook(purpose, prompt)
            if got is not None:
                return got
        if purpose == "propose":
            ledger = _ledger_part(prompt)
            for needle, props in script:
                if needle in ledger:
                    return json.dumps(props() if callable(props) else props, ensure_ascii=False)
            return "[]"
        return answer

    call_llm.calls = calls
    call_llm.prompts = prompts
    return call_llm


def _fail_on_propose(pred, kind="backend_error"):
    """_fake_llm(hook=) に渡す hook。propose の通算の呼び出し番号 (1 から。回答は数えない) が pred で真なら失敗させる。"""
    state = {"n": 0}

    def hook(purpose, prompt):
        if purpose != "propose":
            return None
        state["n"] += 1
        if not pred(state["n"]):
            return None
        if kind == "unparseable":
            return "not json"
        from raiya.backend import BackendError
        raise BackendError("down")
    return hook


class _EngineBase(_Base):
    def new_engine(self):
        from raiya.engine import MemoryEngine
        return MemoryEngine(state_dir=tempfile.mkdtemp(prefix="raiya_lme3_state_", dir=self.tmp), new_life=False)

    def replay(self, q, fake, engine=None, **kw):
        engine = engine or self.new_engine()
        kw.setdefault("sleep", lambda s: None)
        with m3.patched_llm(fake):
            replay = m3.replay_question(engine, q, **kw)
        return engine, replay


class TestReplay(_EngineBase):
    def test_flow_counts_on_ku1(self):
        fake = _fake_llm()
        _, r = self.replay(_ku1(), fake)
        self.assertEqual([c["trigger"] for c in r["consolidations"]], ["every", "session_end", "every", "session_end"])
        self.assertEqual(r["lives"], 2)
        self.assertEqual(r["recalls"], 12)
        self.assertEqual(r["skipped_turns"], 0)
        self.assertEqual({p for p, _ in fake.calls}, {"propose"})
        self.assertEqual(len(fake.calls), 4)

    def test_ledger_follows_date_order(self):
        _, r = self.replay(_ku1(), _fake_llm())
        ev = [t for t in r["turns"] if t["user_ha"]]
        self.assertEqual([t["session_id"] for t in ev], ["s_old", "s_new"])
        self.assertLess(ev[0]["ledger_id"], ev[1]["ledger_id"])
        self.assertEqual([t["session_rank"] for t in ev], [0, 1])
        for t in ev:
            self.assertIn(t["ledger_id"], r["texts"])
        self.assertIn("27:12", r["texts"][ev[0]["ledger_id"]]["user"])

    def test_recall_gets_session_and_context(self):
        engine = self.new_engine()
        with mock.patch.object(engine, "recall", wraps=engine.recall) as rec:
            self.replay(_ku1(), _fake_llm(), engine=engine)
        calls = rec.call_args_list
        self.assertEqual(len(calls), 12)
        first_old, second_old, first_new = calls[0], calls[1], calls[6]
        self.assertEqual(first_old.kwargs["session_start"], True)
        self.assertEqual(first_old.kwargs["context"], [])
        self.assertEqual(first_old.kwargs["session_id"], "s_old")
        self.assertEqual(first_old.kwargs["project"], None)
        self.assertEqual(second_old.kwargs["session_start"], False)
        self.assertEqual(second_old.kwargs["context"], [first_old.args[0]])
        self.assertEqual(second_old.kwargs["session_id"], "s_old")
        self.assertEqual(first_new.kwargs["session_start"], True)
        self.assertEqual(first_new.kwargs["context"], [])
        self.assertEqual(first_new.kwargs["session_id"], "s_new")

    def test_skippable_first_prompt_gets_no_session_start(self):
        q = _ku1()
        q["haystack_sessions"][1][0]["content"] = "   "   # s_old の最初のプロンプトが skip の対象
        engine = self.new_engine()
        with mock.patch.object(engine, "recall", wraps=engine.recall) as rec:
            _, r = self.replay(q, _fake_llm(), engine=engine)
        self.assertEqual(r["lives"], 2)
        self.assertEqual(r["recalls"], 11)
        self.assertEqual(r["skipped_turns"], 1)
        flags = [c.kwargs["session_start"] for c in rec.call_args_list]
        self.assertEqual(flags[:5], [False] * 5)   # s_old (日付が先): 最初のプロンプトが skip なら真は渡らない (hooks と同じ)
        self.assertEqual(flags[5:], [True, False, False, False, False, False])   # s_new: 1 回目だけ真

    def test_waits_for_next_second_before_recall(self):
        ticks = iter(100.2 + 0.2 * i for i in range(10000))
        slept = []
        self.replay(_ku1(), _fake_llm(), sleep=slept.append, clock=lambda: next(ticks))
        self.assertGreaterEqual(len(slept), 1)
        self.assertTrue(all(s > 0 for s in slept))
        self.assertTrue(m3.needs_wait(100.2, 100.9))
        self.assertFalse(m3.needs_wait(100.9, 101.0))
        self.assertFalse(m3.needs_wait(None, 5.0))

    def test_backend_error_window_is_reread_at_next_trigger(self):
        from raiya.backend import BackendError
        n = []

        def hook(purpose, prompt):
            n.append(1)
            if len(n) == 1:
                raise BackendError("x")
        _, r = self.replay(_ku1(), _fake_llm(hook=hook))
        first, second = r["consolidations"][0], r["consolidations"][1]
        self.assertEqual(first["skipped"], "backend_error")
        self.assertEqual(first["error_reason"], "x")
        self.assertTrue(first["window"])
        self.assertEqual(second["window"][:len(first["window"])], first["window"])
        self.assertGreater(len(second["window"]), len(first["window"]))
        self.assertTrue(set(first["window"]) <= r["consumed"])   # 2 回目が通ったので消費された

    def test_unparseable_window_is_not_consumed(self):
        # 全部が unparseable なら、何も消費されない (カーソルも進まない)
        engine, r = self.replay(_ku1(), _fake_llm(hook=lambda p, prompt: "not json" if p == "propose" else None))
        self.assertEqual(r["consolidations"][0]["skipped"], "unparseable")
        self.assertEqual(r["consolidations"][0]["debug"]["raw"], "not json")
        self.assertEqual(r["consumed"], set())
        self.assertEqual(engine.audit()["ledger"]["consolidated_upto"], 0)
        # 1 回目だけ unparseable なら、2 回目の窓が 1 回目を含み、通った後で消費される
        n = []

        def hook(purpose, prompt):
            n.append(1)
            return "not json" if len(n) == 1 else None
        _, r2 = self.replay(_ku1(), _fake_llm(hook=hook))
        a, b = r2["consolidations"][0], r2["consolidations"][1]
        self.assertEqual(a["skipped"], "unparseable")
        self.assertEqual(b["window"][:len(a["window"])], a["window"])
        self.assertTrue(set(a["window"]) <= r2["consumed"])
        self.assertNotIn("raw", b["debug"])
        self.assertNotIn("prompt", b["debug"])

    def test_failed_last_session_end_leaves_unconsumed(self):
        from raiya.backend import BackendError
        n = []

        def hook(purpose, prompt):
            n.append(1)
            if len(n) == 4:
                raise BackendError("last")
        _, r = self.replay(_ku1(), _fake_llm(hook=hook))
        self.assertEqual(r["consolidations"][-1]["skipped"], "backend_error")
        new_ev = [t["ledger_id"] for t in r["turns"] if t["user_ha"] and t["session_id"] == "s_new"][0]
        old_ev = [t["ledger_id"] for t in r["turns"] if t["user_ha"] and t["session_id"] == "s_old"][0]
        self.assertNotIn(new_ev, r["consumed"])
        self.assertIn(old_ev, r["consumed"])

    def test_decisions_are_trimmed_to_known_keys(self):
        script = [(OLD_SENTENCE, [_prop("決定", "5K best 27:12", OLD_SENTENCE, ["5K", "personal"])])]
        _, r = self.replay(_ku1(), _fake_llm(script))
        step = [c for c in r["consolidations"] if c["decisions"]][0]
        self.assertEqual(set(step["decisions"][0]) - {"seed_id"}, set(m3.DECISION_KEYS) & set(step["decisions"][0]))
        self.assertIsNotNone(step["decisions"][0]["seed_id"])
        self.assertNotIn("seed", step["decisions"][0])
        self.assertEqual(len(step["proposals"]), 1)

    def test_patched_llm_restores(self):
        import raiya.backend as backend
        original = backend.call_llm
        fake = _fake_llm()
        with m3.patched_llm(fake):
            self.assertIsNot(backend.call_llm, original)
            self.assertEqual(backend.call_llm("x", max_tokens=5, purpose="propose"), "[]")
        self.assertIs(backend.call_llm, original)
        with self.assertRaises(RuntimeError):
            with m3.patched_llm(fake):
                raise RuntimeError("boom")
        self.assertIs(backend.call_llm, original)


# --- T3: 書く側の測り -------------------------------------------------------------------------------


def _old_id(engine):
    return engine.inspect_seeds(include_superseded=True, include_withdrawn=True)[0]["id"]


class TestWriteSide(_EngineBase):
    def measure(self, script_fn, q=None, hook=None):
        """script_fn(engine) -> 台本。再生して (engine, replay, seeds, links, KU の結果) を返す。"""
        q = q or _ku1()
        engine = self.new_engine()
        fake = _fake_llm(script_fn(engine), hook=hook)
        engine, replay = self.replay(q, fake, engine=engine)
        seeds = engine.inspect_seeds(include_superseded=True, include_withdrawn=True)
        links = m3.seed_links(seeds)
        upto = engine.audit()["ledger"]["consolidated_upto"]
        return engine, replay, seeds, links, m3.ku_outcome(q, replay, links, upto)

    @staticmethod
    def _old_new(content="5K best 27:12", conds=("5K", "personal")):
        return (OLD_SENTENCE, [_prop("決定", content, OLD_SENTENCE, list(conds))])

    def test_superseded_in_place(self):
        def script(engine):
            return [self._old_new(),
                    (NEW_SENTENCE, lambda: [_prop("決定", "5K best 25:50", NEW_SENTENCE, ["5K", "personal"],
                                                  action="supersede", target_id=_old_id(engine))])]
        _, replay, seeds, links, out = self.measure(script)
        self.assertEqual(len(seeds), 1, [(c["decisions"], c["proposals"]) for c in replay["consolidations"]])
        self.assertEqual(seeds[0]["content"], "5K best 25:50")
        self.assertEqual(out["class"], "superseded_in_place")
        self.assertEqual(out["old_seed_ids"], out["new_seed_ids"])

    def test_merged_keeps_old_content(self):
        def script(engine):
            return [self._old_new(),
                    (NEW_SENTENCE, lambda: [_prop("決定", "5K best 25:50", NEW_SENTENCE, ["5K", "personal"],
                                                  action="reinforce", target_id=_old_id(engine))])]
        _, _, seeds, _, out = self.measure(script)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["content"], "5K best 27:12")
        self.assertEqual(out["class"], "merged")
        self.assertIs(out["content_from_new"], False)

    def test_both_live_split_shown_llm_new(self):
        script = lambda engine: [self._old_new(),
                                 (NEW_SENTENCE, [_prop("決定", "now 25:50", NEW_SENTENCE, ["25:50"])])]
        _, _, seeds, _, out = self.measure(script)
        self.assertEqual(len(seeds), 2)
        self.assertEqual(out["class"], "both_live")
        self.assertIs(out["old_shown"], True)
        self.assertEqual(out["split"], "shown_llm_new")

    def test_both_live_split_not_shown(self):
        script = lambda engine: [self._old_new(conds=("27:12",)),
                                 (NEW_SENTENCE, [_prop("決定", "now 25:50", NEW_SENTENCE, ["25:50"])])]
        _, _, seeds, _, out = self.measure(script)
        self.assertEqual(len(seeds), 2)
        self.assertEqual(out["class"], "both_live")
        self.assertIs(out["old_shown"], False)
        self.assertEqual(out["split"], "not_shown")

    def test_superseded_horizontal(self):
        q = _ku1()
        replay = {"turns": [{"ledger_id": 6, "session_id": "s_old", "session_rank": 0, "user_ha": True, "assistant_ha": False},
                            {"ledger_id": 12, "session_id": "s_new", "session_rank": 1, "user_ha": True, "assistant_ha": False}],
                  "consolidations": [], "consumed": {6, 12}}
        base = {"first_ledger_id": None, "withdrawn": None, "kind": "決定", "sansho": "圓成實", "content": "c",
                "content_has_cjk": False}
        links = {1: dict(base, ledger_ids={6}, superseded_by=2), 2: dict(base, ledger_ids={3, 12}, superseded_by=None)}
        out = m3.ku_outcome(q, replay, links, 12)
        self.assertEqual(out["class"], "superseded_horizontal")
        # 古い種子が隠されていなければ both_live
        links[1]["superseded_by"] = None
        self.assertEqual(m3.ku_outcome(q, replay, links, 12)["class"], "both_live")

    def test_old_only_when_new_not_proposed(self):
        _, _, seeds, _, out = self.measure(lambda engine: [self._old_new()])
        self.assertEqual(len(seeds), 1)
        self.assertEqual(out["class"], "old_only")

    def test_none_and_incomplete(self):
        _, _, _, _, out = self.measure(lambda engine: [])
        self.assertEqual(out["class"], "none")
        from raiya.backend import BackendError
        n = []

        def hook(purpose, prompt):
            n.append(1)
            if len(n) == 4:
                raise BackendError("last")
        _, _, _, _, out = self.measure(lambda engine: [self._old_new()], hook=hook)
        self.assertEqual(out["class"], "incomplete")

    def test_single_evidence_session(self):
        q = _ku1()
        for t in q["haystack_sessions"][0]:
            t.pop("has_answer", None)
        _, _, _, _, out = self.measure(lambda engine: [self._old_new()], q=q)
        self.assertEqual(out["class"], "single_evidence_session")

    def test_order_failed_first(self):
        out = m3.ku_outcome(_ku1(), {"error": "fail:X"}, {}, 0)
        self.assertEqual(out["class"], "failed")
        self.assertEqual(m3.KU_CLASSES[0], "failed")
        self.assertEqual(len(m3.KU_CLASSES), 11)

    def test_extraction_counts_split_evidence_and_others(self):
        def script(engine):
            return [self._old_new(),
                    (NEW_SENTENCE, lambda: [_prop("決定", "5K best 25:50", NEW_SENTENCE, ["5K", "personal"],
                                                  action="supersede", target_id=_old_id(engine))])]
        _, replay, _, links, _ = self.measure(script)
        c = m3.extraction_counts(replay, links)
        self.assertEqual(c["evidence_turns"], 2)
        self.assertEqual(c["evidence_turns_in_seeds"], 2)
        self.assertEqual(c["other_turns"], 10)
        self.assertEqual(c["other_turns_in_seeds"], 0)
        self.assertEqual(c["actions"], {"new": 1, "supersede": 1})
        self.assertEqual(c["proposal_kinds"], {"決定": 2})
        self.assertEqual(sum(c["classes"].values()), 2)

    def test_extraction_counts_reject_reasons_and_record_only(self):
        replay = {"turns": [], "consolidations": [
            {"skipped": None, "window": [1], "proposals": [{"action": "new", "kind": "性向"}] * 3,
             "decisions": [{"decision": "reject", "reason": "x 12 y"}, {"decision": "reject", "reason": "x 3 y"},
                           {"decision": "圓成實", "reason": None, "settled_match": 4}]},
            {"skipped": "unparseable", "window": [2], "proposals": [{"action": "new", "kind": "決定"}], "decisions": []}]}
        c = m3.extraction_counts(replay, {})
        self.assertEqual(c["reject_reasons"], {"x N y": 2})
        self.assertEqual(c["record_only"], {"settled_match": 1})
        self.assertEqual(c["classes"], {"reject": 2, "圓成實": 1})
        self.assertEqual(c["proposal_kinds"], {"性向": 3})   # skipped の窓の提案は数えない

    def test_content_cjk_is_counted(self):
        _, replay, _, links, _ = self.measure(lambda engine: [self._old_new(content="5K の自己ベスト")])
        self.assertEqual(m3.extraction_counts(replay, links)["content_cjk_seeds"], 1)
        self.assertTrue(list(links.values())[0]["content_has_cjk"])
        _, replay, _, links, _ = self.measure(lambda engine: [self._old_new()])
        self.assertEqual(m3.extraction_counts(replay, links)["content_cjk_seeds"], 0)

    def test_seed_links_shape(self):
        _, _, seeds, links, _ = self.measure(lambda engine: [self._old_new()])
        link = links[seeds[0]["id"]]
        self.assertEqual(link["ledger_ids"], {6})
        self.assertEqual(link["first_ledger_id"], 6)
        self.assertEqual(link["kind"], "決定")
        self.assertEqual(link["sansho"], "圓成實")
        self.assertIsNone(link["superseded_by"])

    def test_gate_breach_only_for_admitted_ungrounded(self):
        texts = {3: {"user": "I like  green tea.", "assistant": "ok"}}
        seed = {"id": 9, "sansho": "圓成實", "evidences": [
            {"ledger_id": 3, "span": "I like green tea."},     # 空白の畳みで一致 -> 破れでない
            {"ledger_id": 3, "span": "I hate coffee badly."}]}
        got = m3.gate_breaches([seed], texts)
        self.assertEqual([b["span"] for b in got], ["I hate coffee badly."])
        self.assertEqual(got[0]["seed_id"], 9)
        self.assertEqual(m3.gate_breaches([dict(seed, sansho="遍計所執")], texts), [])
        # ledger_id が texts に無ければ全部の本文と照らす
        other = {"id": 1, "sansho": "依他起", "evidences": [{"ledger_id": 99, "span": "ok"}, {"ledger_id": 99, "span": "zzz"}]}
        self.assertEqual([b["span"] for b in m3.gate_breaches([other], texts)], ["zzz"])
        # span は先頭 200 字
        long = {"id": 2, "sansho": "圓成實", "evidences": [{"ledger_id": 3, "span": "q" * 300}]}
        self.assertEqual(len(m3.gate_breaches([long], texts)[0]["span"]), 200)


# --- T4: 問いの時の想起と回答 -----------------------------------------------------------------------


class TestReadSide(_EngineBase):
    def planted(self, kind="決定", q=None):
        """s_new の根拠の窓に kind の new を 1 件返す台本で再生した state。"""
        q = q or _ku1()
        script = [(NEW_SENTENCE, [_prop(kind, "5K best 25:50", NEW_SENTENCE, ["5K", "personal"])])]
        engine, replay = self.replay(q, _fake_llm(script))
        links = m3.seed_links(engine.inspect_seeds(include_superseded=True, include_withdrawn=True))
        return engine, replay, links

    def test_auto_label(self):
        self.assertEqual(m3.auto_label("25:50", "I don't know."), "abstained")
        self.assertEqual(m3.auto_label("25:50", "  I  DON'T KNOW "), "abstained")
        self.assertEqual(m3.auto_label("25 minutes and 50 seconds (or 25:50)", "It is 25:50."), "key_hit")
        self.assertEqual(m3.auto_label("25 minutes and 50 seconds (or 25:50)", "25 Minutes and 50 seconds."), "key_hit")
        self.assertEqual(m3.auto_label("25:50", "It was 27:12."), "other")
        self.assertEqual(m3.auto_label(3, "You have 3 cats."), "key_hit")
        self.assertEqual(m3.auto_label("", "anything"), "other")   # 空の正答は当たりにしない

    def test_no_injection_skips_answer(self):
        calls = []
        got = m3.answer_arm(_ku1(), {"has_seed_line": False, "narrative": ""}, lambda *a, **k: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(got, {"answer": None, "auto_label": "no_injection", "error": None})

    def test_answer_arm_calls_and_labels(self):
        seen = []

        def call(prompt, purpose=None):
            seen.append((prompt, purpose))
            return "It is 25:50."
        arm = {"has_seed_line": True, "narrative": "NOTE-LINE-XYZ"}
        got = m3.answer_arm(_ku1(), arm, call)
        self.assertEqual(got, {"answer": "It is 25:50.", "auto_label": "key_hit", "error": None})
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0][1])
        self.assertIn("NOTE-LINE-XYZ", seen[0][0])
        from raiya.backend import BackendError

        def boom(prompt, purpose=None):
            raise BackendError("http 500")
        got = m3.answer_arm(_ku1(), arm, boom)
        self.assertEqual(got, {"answer": None, "auto_label": "error", "error": "http 500"})

    def test_answer_prompt_carries_notes_and_date(self):
        q = _ku1()
        text = m3.answer_prompt(q, "NOTES-BODY")
        for needle in ("NOTES-BODY", q["question_date"], q["question"], m3.ABSTAIN_PHRASE):
            self.assertIn(needle, text)
        self.assertEqual(m3.ABSTAIN_PHRASE, "I don't know")

    def test_arms_differ_only_by_env_and_restore(self):
        engine, _, _ = self.planted(kind="性向")
        out = m3.question_recall(engine, _ku1())
        self.assertEqual(set(out), {"default", "dispositions"})
        self.assertFalse(out["default"]["has_seed_line"])
        self.assertTrue(out["dispositions"]["has_seed_line"])
        self.assertEqual(out["default"]["first_circle_ids"], [])
        self.assertEqual(len(out["dispositions"]["first_circle_ids"]), 1)
        self.assertIn("5K", out["dispositions"]["narrative"])
        self.assertEqual(out["dispositions"]["chars"], len(out["dispositions"]["narrative"]))
        self.assertNotIn("RAIYA_INJECT_DISPOSITIONS", os.environ)   # 元 (無し) に戻る

    def test_question_recall_begins_one_life(self):
        engine, _, _ = self.planted()
        before = engine.life
        m3.question_recall(engine, _ku1())
        self.assertEqual(engine.life, before + 1)

    def test_injection_class_maps_to_stage1(self):
        engine, replay, links = self.planted()
        q = _ku1()
        arms = m3.question_recall(engine, q)
        self.assertEqual(m3.injection_class(q, replay, links, arms["default"]), "injected")
        # 性向の種子は既定の腕では注入されない
        engine2, replay2, links2 = self.planted(kind="性向")
        arms2 = m3.question_recall(engine2, q)
        self.assertEqual(m3.injection_class(q, replay2, links2, arms2["default"]), "planted_not_injected")
        self.assertEqual(m3.injection_class(q, replay2, links2, arms2["dispositions"]), "injected")
        # 種子が無い (提案 0 件) KU は all_rejected、abs は abs、印の無い abs は no_marker
        engine3, replay3 = self.replay(q, _fake_llm())
        links3 = m3.seed_links(engine3.inspect_seeds(include_superseded=True, include_withdrawn=True))
        self.assertEqual(m3.injection_class(q, replay3, links3, m3.question_recall(engine3, q)["default"]), "all_rejected")
        for qa, want in ((_abs_marked(), "abs"), (_abs_unmarked(), "no_marker")):
            e, r = self.replay(qa, _fake_llm())
            lk = m3.seed_links(e.inspect_seeds(include_superseded=True, include_withdrawn=True))
            self.assertEqual(m3.injection_class(qa, r, lk, m3.question_recall(e, qa)["default"]), want)

    def test_injection_class_merged_only(self):
        # 根拠の往復が、根拠でない往復で植わった種子へ併合された形 (種子の最初の ledger_id が根拠でない)
        q = _ku1()
        replay = {"turns": [{"ledger_id": 6, "session_id": "s_old", "session_rank": 0, "user_ha": True, "assistant_ha": False}]}
        base = {"withdrawn": None, "superseded_by": None, "kind": "決定", "sansho": "圓成實", "content": "c", "content_has_cjk": False}
        links = {5: dict(base, ledger_ids={3, 6}, first_ledger_id=3)}
        arm = {"first_circle_ids": [5], "index_ids": []}
        self.assertEqual(m3.injection_class(q, replay, links, arm), "merged_into_distractor")
        links = {5: dict(base, ledger_ids={6}, first_ledger_id=6)}
        self.assertEqual(m3.injection_class(q, replay, links, {"first_circle_ids": [], "index_ids": []}), "planted_not_injected")
        self.assertEqual(m3.injection_class(q, replay, links, {"first_circle_ids": [], "index_ids": [5]}), "injected")


# --- T5: ランナー (ガード・鍵・置き場・途中で止める・続き・集計・人の目の表) -------------------------------


def _dir_digest(path):
    import hashlib
    digest = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            digest.update(name.encode())
            with open(full, "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()


def _strip_time(obj):
    """generated_at・timing・llm_ms を再帰的に除く (時間だけが実行ごとに違う)。"""
    if isinstance(obj, dict):
        return {k: _strip_time(v) for k, v in obj.items() if k not in ("generated_at", "timing", "llm_ms")}
    if isinstance(obj, list):
        return [_strip_time(v) for v in obj]
    return obj


def _planting_script():
    """ku1 の古い値・新しい値を 1 本の種子に置き換える台本 (superseded_in_place)。新しい state の最初の種子の id は 1。"""
    return [(OLD_SENTENCE, [_prop("決定", "5K best 27:12", OLD_SENTENCE, ["5K", "personal"])]),
            (NEW_SENTENCE, [_prop("決定", "5K best 25:50", NEW_SENTENCE, ["5K", "personal"], action="supersede", target_id=1)])]


class TestRunner(_Base):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, "out")
        self.repo_receipts = os.path.join(m3.BASE_DIR, "receipts")
        self.receipts_digest = _dir_digest(self.repo_receipts)
        self.addCleanup(lambda: self.assertEqual(_dir_digest(self.repo_receipts), self.receipts_digest))

    def data_path(self, data=None, name="data.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data if data is not None else [_ku1(), _abs_marked(), _abs_unmarked()], f)
        return path

    def run_dry(self, fake=None, data=None, out=None, name="r.json", **kw):
        kw.setdefault("sleep", lambda s: None)
        return m3.run(self.data_path(data), out or self.out, name=name, call_llm=fake or _fake_llm(), **kw)

    def load(self, name="r.json", out=None):
        with open(os.path.join(out or self.out, name), encoding="utf-8") as f:
            return json.load(f)

    def test_dry_run_writes_three_files_outside_repo(self):
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*a, **k):
            path = real_mkdtemp(*a, **k)
            made.append(path)
            return path
        with mock.patch.object(m3.tempfile, "mkdtemp", side_effect=recording):
            result = self.run_dry()
        self.assertEqual(len(made), 3)   # 問いごとに 1 つ
        self.assertEqual([p for p in made if os.path.exists(p)], [])   # 問いごとの state の tmp は消える
        self.assertEqual(result["summary"]["rows_with_failed_consolidations"], {"count": 0, "question_ids": []})
        self.assertEqual(result["summary"]["questions"]["failed"], 0)   # 塞いだ呼び口に届くと failed の行に化けるので、ここで固める

        self.assertEqual(sorted(os.listdir(self.out)), ["r.json", "r.json.partial.jsonl", "r_review.tsv"])
        doc = self.load()
        self.assertEqual(doc["meta"]["schema"], "longmemeval-stage3/1")
        self.assertEqual(doc["meta"]["schema"], m3.SCHEMA)
        self.assertIs(doc["meta"]["dry_run"], True)
        self.assertEqual(set(doc), {"meta", "summary", "rows", "timing"})
        self.assertEqual([r["question_id"] for r in doc["rows"]], ["ku1", "abs_m_abs", "abs_u_abs"])
        self.assertEqual(result, doc)
        self.assertFalse(os.path.realpath(self.out).startswith(os.path.realpath(m3.BASE_DIR) + os.sep))

    def test_existing_result_short_circuits_without_llm(self):
        first = self.run_dry()
        fake = _fake_llm()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            second = self.run_dry(fake)
        self.assertIn("手動1本の規律", err.getvalue())
        self.assertEqual(fake.calls, [])
        self.assertEqual(second, first)

    def test_small_all_unparseable_run_is_blocked_and_rerun_restarts(self):
        bad = _fail_on_propose(lambda n: True, kind="unparseable")
        data = [_abs_marked()]   # consolidate は session_end の 1 回だけ
        first = self.run_dry(_fake_llm(hook=bad), data=data)
        self.assertIs(first["summary"]["blocked"], True)
        with open(os.path.join(self.out, "r.json.partial.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        fake = _fake_llm(hook=_fail_on_propose(lambda n: True, kind="unparseable"))
        self.run_dry(fake, data=data)
        self.assertGreater(len(fake.calls), 0)   # 再走した
        with open(os.path.join(self.out, "r.json.partial.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)   # 積み増さない

    def _two_ku(self):
        return [_ku1(), _ku1(prefix="z", qid="ku2")]

    def _three_ku(self):
        return [_ku1(prefix="a", qid="ku_a"), _ku1(prefix="b", qid="ku_b"), _ku1(prefix="c", qid="ku_c")]

    def _partial_ids(self, out=None):
        with open(os.path.join(out or self.out, "r.json.partial.jsonl"), encoding="utf-8") as f:
            return [json.loads(l)["question_id"] for l in f.read().splitlines()]

    def test_resume_after_abort_mid_streak_matches_straight(self):
        data = self._three_ku()
        first = _fake_llm(script=_planting_script(), hook=_fail_on_propose(lambda n: n >= 7))
        with self.assertRaises(m3.RunAborted):
            self.run_dry(first, data=data)
        self.assertFalse(os.path.exists(os.path.join(self.out, "r.json")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "r_review.tsv")))
        self.assertEqual(self._partial_ids(), ["ku_a"])   # ku_b は落とされ、ku_c は書かれない
        fake = _fake_llm(script=_planting_script())
        resumed = self.run_dry(fake, data=data)
        self.assertEqual(resumed["summary"]["questions"]["failed"], 0)   # 塞いだ呼び口に届くと failed の行に化けるので、ここで固める
        self.assertEqual([c[0] for c in fake.calls].count("propose"), 8)   # ku_b と ku_c の 4 回ずつ (回答は数えない)
        self.assertFalse(any("(an" in p or "(ao" in p for p in fake.prompts), "ku_a の propose が呼ばれた")
        self.assertEqual([r["question_id"] for r in resumed["rows"]], ["ku_a", "ku_b", "ku_c"])
        for r in resumed["rows"]:
            self.assertEqual([c for c in r["consolidations"] if c["skipped"] == "backend_error"], [], r["question_id"])
        self.assertEqual(resumed["summary"]["rows_with_failed_consolidations"], {"count": 0, "question_ids": []})
        straight = self.run_dry(_fake_llm(script=_planting_script()), data=data, out=os.path.join(self.tmp, "out2"))
        self.assertEqual(_strip_time(resumed["rows"]), _strip_time(straight["rows"]))

    def test_isolated_failure_is_kept_not_rerun(self):
        data = self._three_ku()
        doc = self.run_dry(_fake_llm(script=_planting_script(), hook=_fail_on_propose(lambda n: n == 7)), data=data)
        self.assertTrue(os.path.exists(os.path.join(self.out, "r.json")))
        row = [r for r in doc["rows"] if r["question_id"] == "ku_b"][0]
        self.assertEqual([c["skipped"] for c in row["consolidations"]].count("backend_error"), 1)
        self.assertEqual(doc["summary"]["rows_with_failed_consolidations"], {"count": 1, "question_ids": ["ku_b"]})
        fake = _fake_llm()
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_dry(fake, data=data)
        self.assertEqual(fake.calls, [])   # 落としも走らせ直しもしない

    def test_update_streak(self):
        u = m3.update_streak
        self.assertEqual(u((0, []), "q1", [None, None]), (0, []))
        self.assertEqual(u((0, []), "q2", [None, "backend_error", "unparseable"]), (2, ["q2"]))
        self.assertEqual(u((2, ["q2"]), "q3", ["backend_error", None, "backend_error"]), (1, ["q3"]))
        self.assertEqual(u((2, ["q2"]), "q3", ["empty"]), (2, ["q2"]))
        self.assertEqual(u((2, ["q2"]), "q3", ["already_consolidated"]), (0, []))
        self.assertEqual(u((2, ["q2"]), "q3", ["backend_error"]), (3, ["q2", "q3"]))
        self.assertEqual(u((5, ["q2"]), "q3", []), (5, ["q2"]))
        self.assertEqual(u((1, ["q2"]), "q2", ["backend_error"]), (2, ["q2"]))   # 同じ問いは列に 1 回だけ

    def test_name_with_path_is_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        for label, name in (("絶対 path", os.path.join(outside, "x.json")), ("..", "../elsewhere/x.json"),
                            ("下位 dir", "sub/x.json")):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.run_dry(name=name)
                self.assertEqual(self.main(["--data", self.data_path(), "--out", self.out, "--name", name],
                                           call_llm=_fake_llm()), 2)
                self.assertEqual(os.listdir(outside), [])
                self.assertFalse(os.path.exists(self.out) and os.listdir(self.out))

    def test_torn_partial_tail_is_ignored(self):
        data = self._two_ku()
        self.run_dry(data=data, limit_per_group=1)   # ku1 だけ走って結果ができる
        os.remove(os.path.join(self.out, "r.json"))
        with open(os.path.join(self.out, "r.json.partial.jsonl"), "a", encoding="utf-8") as f:
            f.write('{"question_id": "ku2", "gr')   # 書きかけの尾
        fake = _fake_llm()
        doc = self.run_dry(fake, data=data)
        self.assertEqual([r["question_id"] for r in doc["rows"]], ["ku1", "ku2"])
        self.assertFalse(any("(n0)" in p for p in fake.prompts))   # ku1 は走り直さない
        with open(os.path.join(self.out, "r.json.partial.jsonl"), encoding="utf-8") as f:
            self.assertEqual([json.loads(l)["question_id"] for l in f.read().splitlines()], ["ku1", "ku2"])   # 尾は書き直された

    def test_live_path_sets_backend_env_and_records_meta(self):
        """call_llm を渡さない (ライブ) 経路。実ネットワークの代わりに openai-api の呼び口だけを偽にして、env と記録を見る。"""
        seen = []

        def fake_openai(prompt, max_tokens, purpose=None, settings=None):
            seen.append((os.environ.get("RAIYA_BACKEND"), os.environ.get("RAIYA_MODEL"), os.environ.get("RAIYA_PROPOSE_MODEL"),
                         os.environ.get("RAIYA_CONFIG"), purpose))
            return "[]" if purpose == "propose" else "I don't know."
        with mock.patch("raiya.backend._call_openai_api", side_effect=fake_openai):
            doc = m3.run(self.data_path(), self.out, name="live.json", sleep=lambda s: None)
        self.assertIs(doc["meta"]["dry_run"], False)
        self.assertEqual(doc["meta"]["backend"], {"propose": "openai-api", "answer": "openai-api"})
        self.assertEqual(doc["meta"]["model"], {"propose": "gpt-6-luna", "answer": "gpt-6-luna"})
        self.assertTrue(seen)
        self.assertEqual({s[:4] for s in seen}, {("openai-api", "gpt-6-luna", "gpt-6-luna", "")})
        self.assertEqual({s[4] for s in seen}, {"propose"})   # 種子が無いので回答は呼ばれない

    def test_force_rewrites(self):
        first = self.run_dry()
        fake = _fake_llm()
        second = self.run_dry(fake, force=True)
        self.assertGreater(len(fake.calls), 0)
        self.assertNotEqual(second["meta"]["generated_at"], first["meta"]["generated_at"])

    def test_out_inside_repo_is_refused(self):
        target = os.path.join(m3.BASE_DIR, "stage3_refused_dir_for_test")
        self.assertFalse(os.path.exists(target))
        self.assertTrue(m3.out_is_inside_repo(target))
        self.assertTrue(m3.out_is_inside_repo(m3.BASE_DIR))
        self.assertFalse(m3.out_is_inside_repo(self.out))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key-not-real"}):
            code = self.main(["--data", self.data_path(), "--out", target])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(target))

    def main(self, argv, **kw):
        kw.setdefault("sleep", lambda s: None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return m3.main(argv, **kw)

    def test_main_without_key_writes_nothing(self):
        os.makedirs(self.out)
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "RAIYA_OPENAI_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            code = self.main(["--data", self.data_path(), "--out", self.out])
        self.assertEqual(code, 2)
        self.assertEqual(os.listdir(self.out), [])

    def test_main_exit_codes(self):
        # 0: ドライラン (call_llm を渡せば鍵は見ない)
        self.assertEqual(self.main(["--data", self.data_path(), "--out", self.out, "--name", "r.json"],
                                   call_llm=_fake_llm()), 0)
        # 1: データの誤り (haystack_dates の欠け)。何も書かない
        bad = _ku1()
        bad.pop("haystack_dates")
        out2 = os.path.join(self.tmp, "out_bad")
        self.assertEqual(self.main(["--data", self.data_path([bad], "bad.json"), "--out", out2], call_llm=_fake_llm()), 1)
        self.assertFalse(os.path.exists(out2))
        # 3: 連続の失敗で止まる
        from raiya.backend import BackendError

        def fail(purpose, prompt):
            if purpose == "propose":
                raise BackendError("down")
        out3 = os.path.join(self.tmp, "out_abort")
        with mock.patch.object(m3, "ABORT_AFTER_FAILURES", 2):
            self.assertEqual(self.main(["--data", self.data_path(), "--out", out3, "--name", "r.json"],
                                       call_llm=_fake_llm(hook=fail)), 3)
        self.assertFalse(os.path.exists(os.path.join(out3, "r.json")))

    def test_limit_per_group(self):
        data = [_ku1(), _ku1(prefix="z", qid="ku2"), _abs_marked(), dict(_abs_marked(), question_id="abs_m2_abs"), _abs_unmarked()]
        doc = self.run_dry(data=data, limit_per_group=1)
        self.assertEqual([r["question_id"] for r in doc["rows"]], ["ku1", "abs_m_abs", "abs_u_abs"])
        self.assertEqual(doc["summary"]["questions"]["by_group"], {"ku": 1, "abs_marked": 1, "abs_unmarked": 1})
        self.assertEqual(doc["summary"]["questions"]["failed"], 0)

    def test_two_dry_runs_match_except_time(self):
        a = self.run_dry(_fake_llm(script=_planting_script()), out=os.path.join(self.tmp, "a"))
        b = self.run_dry(_fake_llm(script=_planting_script()), out=os.path.join(self.tmp, "b"))
        self.assertEqual(a["summary"]["questions"]["failed"], 0)
        self.assertEqual(b["summary"]["questions"]["failed"], 0)
        self.assertEqual(_strip_time(a), _strip_time(b))

    def test_gate_breach_exit_1(self):
        breach = [{"seed_id": 1, "span": "x"}]
        with mock.patch.object(m3, "gate_breaches", return_value=breach):
            code = self.main(["--data", self.data_path(), "--out", self.out, "--name", "r.json", "--quiet"],
                             call_llm=_fake_llm(script=_planting_script()))
        self.assertEqual(code, 1)
        doc = self.load()
        self.assertIs(doc["summary"]["gate"]["ok"], False)
        self.assertEqual(len(doc["summary"]["gate"]["breaches"]), 3)   # 3 問 × 1 件

    def test_summary_and_review_table_on_fixture(self):
        engine_free = _planting_script()
        doc = self.run_dry(_fake_llm(script=engine_free, answer="It is 25:50."))
        summ = doc["summary"]
        self.assertEqual(summ["questions"]["failed"], 0)
        self.assertEqual(summ["questions"]["by_group"], {"ku": 1, "abs_marked": 1, "abs_unmarked": 1})
        self.assertEqual(summ["ku_classes"]["superseded_in_place"], 1)
        self.assertEqual(sum(summ["ku_classes"].values()), 1)
        self.assertEqual(list(summ["ku_classes"]), list(m3.KU_CLASSES))
        self.assertTrue(summ["gate"]["ok"], summ["gate"])
        self.assertIs(summ["blocked"], False)
        self.assertEqual(summ["arms"]["default"]["injection_class"]["ku"], {"injected": 1})
        self.assertEqual(summ["arms"]["default"]["injection_class"]["abs_marked"], {"abs": 1})
        self.assertEqual(summ["arms"]["default"]["auto_label"]["ku"], {"key_hit": 1})
        self.assertEqual(summ["arms"]["default"]["auto_label"]["abs_unmarked"], {"no_injection": 1})
        self.assertEqual(summ["vacuous_abstain"], {"default": 1, "dispositions": 1})
        self.assertEqual(summ["extraction"]["evidence_turns"], 3)   # ku1 の 2 本 + abs_marked の 1 本
        self.assertEqual(summ["calls"]["answer"], 2)    # ku の 2 腕 (注入が在る) だけ
        self.assertEqual(summ["calls"]["consolidate"], 6)   # ku 4 + abs 1 + abs 1
        self.assertEqual(summ["skipped"], {"consolidated": 6})
        self.assertEqual(summ["llm_ms"]["n"], 6)
        row = doc["rows"][0]
        self.assertEqual(row["ku"]["class"], "superseded_in_place")
        self.assertIn("5K", row["arms"]["default"]["narrative"])
        with open(os.path.join(self.out, "r_review.tsv"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0].split("\t"), ["question_id", "group", "arm", "auto_label", "answer_key", "model_answer", "human_label"])
        body = [l.split("\t") for l in lines[1:]]
        self.assertEqual([(b[0], b[2], b[3], b[6]) for b in body], [("ku1", "default", "key_hit", ""), ("ku1", "dispositions", "key_hit", "")])
        # 「other」の腕も表に出る (答えが 25:50 でない)
        out2 = os.path.join(self.tmp, "o2")
        self.run_dry(_fake_llm(script=engine_free, answer="Something else."), out=out2)
        with open(os.path.join(out2, "r_review.tsv"), encoding="utf-8") as f:
            self.assertEqual([l.split("\t")[3] for l in f.read().splitlines()[1:]], ["other", "other"])

    def test_meta_records_rules_and_no_backend_in_dry_run(self):
        meta = self.run_dry()["meta"]
        self.assertIsNone(meta["backend"])
        self.assertIsNone(meta["model"])
        self.assertEqual(meta["answer_prompt"], m3.ANSWER_PROMPT)
        self.assertEqual(meta["ku_class_order"], list(m3.KU_CLASSES))
        self.assertEqual(meta["abort_after_failures"], m3.ABORT_AFTER_FAILURES)
        self.assertEqual(meta["arms"], [list(a) for a in m3.ARMS])
        self.assertIn("data_sha256", meta)
        self.assertTrue(meta["git_head"] is None or len(meta["git_head"]) == 40)

    def test_failed_question_row_does_not_stop_the_run(self):
        data = [_ku1(), _abs_marked()]
        real = m3.replay_question

        def flaky(engine, q, **kw):
            if q["question_id"] == "ku1":
                raise KeyError("boom")
            return real(engine, q, **kw)
        with mock.patch.object(m3, "replay_question", side_effect=flaky):
            doc = self.run_dry(data=data)
        self.assertEqual(doc["rows"][0]["failed"], "fail:KeyError")
        self.assertEqual(doc["rows"][0]["ku"]["class"], "failed")
        self.assertIsNone(doc["rows"][1]["failed"])
        self.assertEqual(doc["summary"]["questions"]["failed"], 1)

    def test_progress_projection_after_ten_consolidations(self):
        msgs = []
        data = [_ku1(), _ku1(prefix="z", qid="ku2"), _ku1(prefix="y", qid="ku3")]
        with mock.patch.object(m3, "PROJECTION_AFTER", 4):
            self.run_dry(data=data, progress=msgs.append)
        proj = [m for m in msgs if "見込み" in m]
        self.assertEqual(len(proj), 1)
        self.assertIn("p50", proj[0])


if __name__ == "__main__":
    unittest.main()
