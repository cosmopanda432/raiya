import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya import backend, cli, sansho  # noqa: E402
from raiya.alaya import QUARANTINE_MAX  # noqa: E402
from tests._state_fixture import mutate  # noqa: E402


class _CliWiringTestCase(unittest.TestCase):
    """cli.Agent 実配線を直接呼ぶテストの共通土台。

    cli.Agent.__init__ は engine.MemoryEngine() を state_dir=None で呼ぶため、状態の
    隔離は RAIYA_STATE_DIR 環境変数で行う (setUp/tearDown で退避・復元)。backend.call_llm
    もモジュール属性として呼び出し時に解決されるため、同様に素朴な属性差し替えで置き換える
    (REPL の input() だけは標準ライブラリの unittest.mock.patch で差し替える)。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_environ = dict(os.environ)
        self._saved_call_llm = backend.call_llm

    def tearDown(self):
        os.environ.clear()
        os.environ["RAIYA_CONFIG"] = ""  # 設定ファイルを読ませない
        os.environ.update(self._saved_environ)
        backend.call_llm = self._saved_call_llm
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _use_state_subdir(self, name):
        os.environ["RAIYA_STATE_DIR"] = os.path.join(self.tmpdir, name)


class TestImprintFromTurnWiring(_CliWiringTestCase):
    """`cli.Agent.imprint_from_turn` (旧 agent.py:298 相当) が Turn の
    user_text/assistant_text を正しいスロットへ渡していることを、実配線経由で確認する。
    user_text<->assistant_text のスロット入替 mutant は、span が「本当は user 由来なのに
    依他起になる」「本当は assistant 由来なのに圓成實になる」の両方向で必ず露呈するため、
    両方向を1本ずつ assert する。
    """

    @staticmethod
    def _fake_call_llm(evidence_source, evidence_span):
        def fake_call_llm(prompt, max_tokens=512, purpose=None):
            if "JSON配列" in prompt:
                return json.dumps(
                    [
                        {
                            "content": "実配線テスト種",
                            # "文言" は user_text/assistant_text どちらにも共通して
                            # 含む語にしてある (縁の接地は user/assistant いずれかに
                            # あればよいため、スロット入替の影響を受けない)。
                            "conditions": ["文言"],
                            "polarity": "無記","kind": "所在",
                            "evidence": {"source": evidence_source, "span": evidence_span},
                        }
                    ]
                )
            return "応答本文"

        return fake_call_llm

    def test_imprint_from_turn_wires_user_and_assistant_to_the_right_slots(self):
        user_text = "私だけが言うユーザー固有の文言です p/"
        assistant_text = "アシスタントだけが言う別の文言です"

        # 方向1: span は user_text にのみ存在する -> 圓成實 になるはず。
        self._use_state_subdir("case_user")
        backend.call_llm = self._fake_call_llm("user", user_text)
        agent_obj = cli.Agent()
        agent_obj.imprint_from_turn(user_text, assistant_text)
        seeds_user = agent_obj.engine.alaya.list_seeds()
        self.assertEqual(len(seeds_user), 1)
        self.assertEqual(seeds_user[0]["sansho"], "圓成實")

        # 方向2 (逆方向): span は assistant_text にのみ存在する -> 依他起 になるはず。
        self._use_state_subdir("case_assistant")
        backend.call_llm = self._fake_call_llm("assistant", assistant_text)
        agent_obj2 = cli.Agent()
        agent_obj2.imprint_from_turn(user_text, assistant_text)
        seeds_assistant = agent_obj2.engine.alaya.list_seeds()
        self.assertEqual(len(seeds_assistant), 1)
        self.assertEqual(seeds_assistant[0]["sansho"], "依他起")

    def test_cli_seeds_carry_kind_and_project_and_recall_filters_by_project(self):
        # 改修5 §4.2: CLI の propose は kind を出し、imprint/recall は起動 cwd の project で絞る。
        user_text = "沼矛の扱いを決めた文言 p/: 資材として扱う"
        self._use_state_subdir("case_project")
        backend.call_llm = self._fake_call_llm("user", "沼矛の扱いを決めた文言 p/")
        agent_obj = cli.Agent(project="/proj/a")
        agent_obj.imprint_from_turn(user_text, "了解")
        seed = agent_obj.engine.alaya.list_seeds()[0]
        self.assertEqual(seed["kind"], "所在")  # fake LLM の自己申告 kind がそのまま種子に残る
        self.assertEqual(seed["project"], "/proj/a")
        hit = agent_obj.engine.recall("文言", project="/proj/a")
        self.assertEqual([s["id"] for s in hit["seeds"]], [seed["id"]])
        miss = cli.Agent(project="/proj/b").engine.recall("文言", project="/proj/b")
        self.assertEqual(miss["seeds"], [])
        self.assertEqual(miss["index"], [])

    def test_imprint_from_turn_does_not_ground_on_private_text(self):
        # #17: CLI の往復にも <private> は根拠にならず、保存もされない。
        user_text = "猫が好き <private>極秘の暗号</private>"
        self._use_state_subdir("case_private")
        proposal = [{"content": "暗号", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
                     "evidence": {"source": "user", "span": "極秘の暗号"}}]
        backend.call_llm = lambda prompt, max_tokens=512, purpose=None: (
            json.dumps(proposal, ensure_ascii=False) if "JSON配列" in prompt else "応答本文"
        )
        agent_obj = cli.Agent()
        agent_obj.imprint_from_turn(user_text, "了解")
        self.assertEqual(agent_obj.engine.alaya.list_seeds(), [])
        self.assertEqual(len(agent_obj.engine.alaya.state["quarantine"]), 1)

    def test_agent_default_project_is_resolved_from_cwd(self):
        self._use_state_subdir("case_cwd")
        self.assertEqual(cli.Agent().project, cli.resolve_project(os.getcwd()))


class TestCmdConversePrivacy(_CliWiringTestCase):
    """#24: 対話 CLI の記憶経路は hooks と同じく <private>・貼り付けを除く。会話 LLM は生の入力を見る。"""

    SECRET = "極秘の暗号"

    def _agent(self, name, reply="応答本文", proposals=None):
        self._use_state_subdir(name)
        self.prompts = []
        self.propose_prompts = []

        def fake(prompt, max_tokens=512, purpose=None):
            if "JSON配列" in prompt:
                self.propose_prompts.append(prompt)
                return json.dumps(proposals or [], ensure_ascii=False)
            self.prompts.append(prompt)
            return reply

        backend.call_llm = fake
        agent_obj = cli.Agent(project="/proj/a")
        self.recall_args = []
        orig = agent_obj.engine.recall

        def spy(text, *a, **kw):
            self.recall_args.append(text)
            return orig(text, *a, **kw)

        agent_obj.engine.recall = spy
        return agent_obj

    def _converse(self, agent_obj, text):
        with contextlib.redirect_stdout(io.StringIO()):
            agent_obj.cmd_converse(text)

    def test_recall_receives_text_without_private(self):
        a = self._agent("recall")
        self._converse(a, "猫が好き <private>{}</private>".format(self.SECRET))
        self.assertEqual(len(self.recall_args), 1)
        self.assertNotIn(self.SECRET, self.recall_args[0])
        self.assertIn("猫が好き", self.recall_args[0])

    def test_propose_receives_turn_without_private_or_paste(self):
        a = self._agent("propose", reply="了解 <private>{}</private>".format("返答の秘密"))
        captured = []
        orig = a.engine.propose
        a.engine.propose = lambda turn: (captured.append(turn), orig(turn))[1]
        text = '猫が好き <private>{}</private> <pasted_content id="1">貼付本文XYZ</pasted_content id="1">'.format(self.SECRET)
        self._converse(a, text)
        self.assertEqual(len(captured), 1)
        for body in (captured[0].user_text, captured[0].assistant_text):
            for token in (self.SECRET, "返答の秘密", "貼付本文XYZ"):
                self.assertNotIn(token, body)
        self.assertIn("猫が好き", captured[0].user_text)
        for token in (self.SECRET, "返答の秘密", "貼付本文XYZ"):
            self.assertNotIn(token, self.propose_prompts[0])

    def test_private_only_input_skips_propose_but_still_answers(self):
        a = self._agent("only")
        self._converse(a, "<private>秘密</private>")
        self.assertEqual(len(self.prompts), 1)  # 会話は応答される
        self.assertEqual(self.propose_prompts, [])  # 提案の LLM 呼び出しなし
        self.assertEqual(a.engine.alaya.list_seeds(), [])
        self.assertEqual(a.engine.alaya.state["quarantine"], [])
        self.assertEqual(a.engine.audit()["imprint_log_tail"], [])

    def test_assistant_side_empty_after_strip_skips_propose(self):
        a = self._agent("asst_only", reply="<private>返答だけ</private>")
        self._converse(a, "猫が好き")
        self.assertEqual(self.propose_prompts, [])

    def test_conversation_prompt_and_history_keep_raw_input(self):
        a = self._agent("raw")
        raw = "猫が好き <private>{}</private>".format(self.SECRET)
        self._converse(a, raw)
        self.assertIn(raw, self.prompts[0])
        self.assertEqual(a.history[-1]["user"], raw)

    def test_private_token_absent_from_state_files(self):
        proposal = [{"content": "猫", "conditions": ["猫"], "polarity": "無記", "kind": "所在",
                     "evidence": {"source": "user", "span": "猫が好き"}}]
        a = self._agent("bytes", proposals=proposal)
        self._converse(a, "猫が好き <private>{}</private>".format(self.SECRET))
        state_dir = os.environ["RAIYA_STATE_DIR"]
        for name in ("alaya.json", "imprint_log.json", "manas.json"):
            path = os.path.join(state_dir, name)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    self.assertNotIn(self.SECRET.encode("utf-8"), f.read(), name)


class TestCmdInsightView(_CliWiringTestCase):
    """`/透視` (cmd_insight_view) が quarantine の保持/累計件数と imprint_log の
    直近の非採用理由を表示する (記憶機能設計書§3.4)。"""

    def test_cmd_insight_view_shows_distinct_retained_and_total_quarantine_counts(self):
        self._use_state_subdir("insight_case_capped")
        agent_obj = cli.Agent()
        agent_obj.last_turn = {"fired_seeds": [], "manas_text": ""}
        retained_expected = QUARANTINE_MAX
        total_expected = QUARANTINE_MAX + 2
        for i in range(total_expected):
            turn = sansho.Turn(user_text="今日は晴れです", assistant_text="")
            agent_obj.engine.alaya.imprint(
                turn,
                {
                    "content": "捏造{}".format(i),
                    "conditions": [],
                    "polarity": "無記","kind": "所在",
                    "evidence": {"source": "user", "span": "捏造span{}".format(i)},
                },
            )
        agent_obj.engine.alaya.commit()  # imprint は保存しない: cmd_insight_view は再読込するので commit が要る
        self.assertNotEqual(retained_expected, total_expected)  # fixture 自体が相異なる前提

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_insight_view()
        output = buf.getvalue()

        # 行固定 assert (bare 数字禁止): 「保持 N 件」「累計 M 件」という完全な断片で確認する。
        self.assertIn("保持 {} 件".format(retained_expected), output)
        self.assertIn("累計 {} 件".format(total_expected), output)

    def test_cmd_insight_view_prints_imprint_log_warning(self):
        self._use_state_subdir("insight_case_warning")
        agent_obj = cli.Agent()
        agent_obj.last_turn = {"fired_seeds": [], "manas_text": ""}
        mutate(agent_obj.engine.state_dir, lambda c: c.execute("INSERT INTO imprint_log(seed_id, body) VALUES (NULL, '{broken')"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_insight_view()
        self.assertIn("imprint_log の読めない行", buf.getvalue())

    def _insight_output(self, agent_obj):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_insight_view()
        return buf.getvalue()

    def test_cmd_insight_view_prints_alaya_and_manas_warnings_once_each(self):
        self._use_state_subdir("insight_case_store_warnings")
        agent_obj = cli.Agent()
        agent_obj.last_turn = {"fired_seeds": [], "manas_text": ""}
        sd = agent_obj.engine.state_dir
        mutate(sd, lambda c: c.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, '{broken')"))
        mutate(sd, lambda c: c.execute("INSERT INTO manas_log(body) VALUES ('{broken')"))
        out = self._insight_output(agent_obj)
        warn_lines = [ln for ln in out.splitlines() if ln.startswith("頼耶> [透視] 警告:")]
        self.assertEqual(len([ln for ln in warn_lines if "種子" in ln]), 1, msg=out)
        self.assertEqual(len([ln for ln in warn_lines if "manas_log" in ln]), 1, msg=out)

    def test_cmd_introspect_prints_warnings_to_user_but_not_into_llm_prompt(self):
        self._use_state_subdir("introspect_case_warnings")
        agent_obj = cli.Agent()
        sd = agent_obj.engine.state_dir
        mutate(sd, lambda c: c.execute("INSERT INTO seeds(id, kind, project, live, body) VALUES (NULL, NULL, NULL, 0, '{broken')"))
        prompts = []
        buf = io.StringIO()
        with unittest.mock.patch.object(cli.backend, "call_llm", side_effect=lambda p, **kw: prompts.append(p) or "申告"):
            with contextlib.redirect_stdout(buf):
                agent_obj.cmd_introspect()
        warn = [ln for ln in buf.getvalue().splitlines() if ln.startswith("頼耶> [内観] 警告:")]
        self.assertEqual(len(warn), 1, msg=buf.getvalue())
        self.assertIn("読めない行", warn[0])
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("警告", prompts[0])
        self.assertNotIn("読めない行", prompts[0])

    def test_cmd_insight_view_prints_no_warning_when_healthy(self):
        self._use_state_subdir("insight_case_healthy")
        agent_obj = cli.Agent()
        agent_obj.last_turn = {"fired_seeds": [], "manas_text": ""}
        self.assertNotIn("[透視] 警告:", self._insight_output(agent_obj))

    def test_cmd_insight_view_shows_recent_rejection_reason_and_excludes_adopted_entries(self):
        self._use_state_subdir("insight_case_filter")
        agent_obj = cli.Agent()
        agent_obj.last_turn = {"fired_seeds": [], "manas_text": ""}

        rejected_turn = sansho.Turn(user_text="今日は晴れです", assistant_text="")
        agent_obj.engine.alaya.imprint(
            rejected_turn,
            {
                "content": "捏造された記憶",
                "conditions": [],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "user", "span": "捏造された非接地の文言です"},
            },
        )
        adopted_turn = sansho.Turn(
            user_text="今日はいい天気ですね", assistant_text="識別用マーカー依他起本文です 天気"
        )
        agent_obj.engine.alaya.imprint(
            adopted_turn,
            {
                "content": "識別用マーカー依他起content",
                "conditions": ["天気"],
                "polarity": "無記","kind": "所在",
                "evidence": {"source": "assistant", "span": "識別用マーカー依他起本文です"},
            },
        )

        agent_obj.engine.alaya.commit()  # imprint は保存しない: cmd_insight_view は再読込するので commit が要る
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_insight_view()
        output = buf.getvalue()

        marker = "直近の非採用理由:"
        self.assertIn(marker, output)
        rejected_section = output.split(marker, 1)[1]

        self.assertIn("[遍計所執]", rejected_section)
        self.assertIn("非接地", rejected_section)
        self.assertNotIn("[依他起]", rejected_section)
        self.assertNotIn("識別用マーカー依他起", rejected_section)

    def test_cmd_insight_view_without_last_turn_does_not_crash(self):
        self._use_state_subdir("insight_case_empty")
        agent_obj = cli.Agent()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_insight_view()
        self.assertIn("直前の応答がまだありません", buf.getvalue())


class TestCmdIntrospect(_CliWiringTestCase):
    def test_cli_introspect_empty_llm_report_does_not_record(self):
        self._use_state_subdir("introspect_empty")
        agent_obj = cli.Agent()
        backend.call_llm = lambda prompt, max_tokens=512, purpose=None: "   \n"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_introspect()
        self.assertIn("内観失敗", buf.getvalue())
        self.assertNotIn("[内観]\n", buf.getvalue())
        self.assertEqual(agent_obj.engine.manas.ego_weight, 1.0)
        self.assertEqual(agent_obj.engine.manas.state["introspections"], [])


class TestCmdSeeds(_CliWiringTestCase):
    """`/種子` が未発芽種子の last_fired=None を『第None生』と印字しない。"""

    def test_cmd_seeds_unfired_seed_does_not_print_none_life(self):
        self._use_state_subdir("seeds_unfired")
        agent_obj = cli.Agent()
        turn = sansho.Turn(user_text="私は猫が好きです p/ 猫", assistant_text="")
        agent_obj.engine.imprint(
            turn,
            [
                {
                    "content": "猫が好き",
                    "conditions": ["猫"],
                    "polarity": "善","kind": "所在",
                    "evidence": {"source": "user", "span": "私は猫が好きです p/"},
                }
            ],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_seeds()
        output = buf.getvalue()
        self.assertIn("猫が好き", output)
        self.assertNotIn("第None生", output)
        self.assertIn("未発芽", output)


def _raise_backend_error(prompt, max_tokens=512, purpose=None):
    raise backend.BackendError("テスト用の失敗")


class TestBackendErrorPaths(_CliWiringTestCase):
    """Issue #13 項目 8: BackendError は会話を止めず、種子も台帳も汚さない。"""

    def test_propose_backend_error_writes_no_seed(self):
        self._use_state_subdir("propose_err")

        def fake(prompt, max_tokens=512, purpose=None):
            if "JSON配列" in prompt:
                raise backend.BackendError("提案失敗")
            return "応答本文"

        backend.call_llm = fake
        agent_obj = cli.Agent()
        agent_obj.imprint_from_turn("猫が好き", "了解")  # 例外は外へ出ない
        self.assertEqual(agent_obj.engine.alaya.list_seeds(), [])
        self.assertEqual(agent_obj.engine.alaya.state["quarantine"], [])
        self.assertEqual(agent_obj.engine.audit()["imprint_log_tail"], [])

    def test_converse_backend_error_prints_failure_and_records_nothing(self):
        self._use_state_subdir("converse_err")
        prompts = []

        def fake(prompt, max_tokens=512, purpose=None):
            prompts.append(prompt)
            raise backend.BackendError("応答失敗")

        backend.call_llm = fake
        agent_obj = cli.Agent()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_converse("猫が好き")
        self.assertIn("[応答生成失敗]", buf.getvalue())
        self.assertEqual(agent_obj.history, [])
        self.assertIsNone(agent_obj.last_turn)
        self.assertEqual(len(prompts), 1)  # 提案 (propose) の LLM 呼び出しは無い
        self.assertNotIn("JSON配列", prompts[0])
        self.assertEqual(agent_obj.engine.alaya.list_seeds(), [])

    def test_introspect_backend_error_keeps_ego_weight(self):
        self._use_state_subdir("introspect_err")
        agent_obj = cli.Agent()
        backend.call_llm = _raise_backend_error
        before = agent_obj.engine.manas.ego_weight
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_obj.cmd_introspect()
        self.assertIn("[内観失敗]", buf.getvalue())
        self.assertEqual(agent_obj.engine.manas.ego_weight, before)
        self.assertEqual(agent_obj.engine.manas.state["introspections"], [])

    def test_conversation_continues_after_backend_error(self):
        self._use_state_subdir("continue_err")
        calls = []

        def fake(prompt, max_tokens=512, purpose=None):
            if "JSON配列" in prompt:
                return "[]"
            calls.append(prompt)
            if len(calls) == 1:
                raise backend.BackendError("一時的な失敗")
            return "二度目の応答"

        backend.call_llm = fake
        agent_obj = cli.Agent()
        buf = io.StringIO()
        with unittest.mock.patch("builtins.input", side_effect=["一回目", "二回目", "/終"]):
            with contextlib.redirect_stdout(buf):
                agent_obj.run()
        output = buf.getvalue()
        self.assertIn("[応答生成失敗]", output)
        self.assertIn("頼耶> 二度目の応答", output)
        self.assertEqual(agent_obj.history, [{"user": "二回目", "assistant": "二度目の応答"}])


class TestRunRepl(_CliWiringTestCase):
    """`Agent.run` の REPL 分岐。コマンドの実処理は差し替え、振り分けだけを見る。"""

    def _run(self, inputs):
        self._use_state_subdir("repl")
        self.llm_calls = []

        def fake(prompt, max_tokens=512, purpose=None):
            self.llm_calls.append(prompt)
            return "[]" if "JSON配列" in prompt else "応答本文"

        backend.call_llm = fake
        agent_obj = cli.Agent()
        self.dispatched = []
        for name in ("cmd_seeds", "cmd_insight_view", "cmd_introspect"):
            setattr(agent_obj, name, lambda n=name: self.dispatched.append(n))
        agent_obj.cmd_converse = lambda line: self.dispatched.append(("converse", line))
        buf = io.StringIO()
        with unittest.mock.patch("builtins.input", side_effect=inputs):
            with contextlib.redirect_stdout(buf):
                agent_obj.run()
        return buf.getvalue()

    def test_blank_lines_are_skipped(self):
        self._run(["", "   ", "\t", "/終"])
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.llm_calls, [])

    def test_commands_dispatch_to_their_handlers(self):
        self._run(["/透視", "/種子", "/内観", "こんにちは", "/終"])
        self.assertEqual(
            self.dispatched,
            ["cmd_insight_view", "cmd_seeds", "cmd_introspect", ("converse", "こんにちは")],
        )

    def test_quit_prints_farewell_and_stops_reading(self):
        out = self._run(["/終", "読まれない"])  # 2 本目を読むと side_effect が尽きて StopIteration
        self.assertIn("頼耶> 今生の幕を閉じます。阿頼耶識は残ります。", out)
        self.assertEqual(self.dispatched, [])

    def test_eof_breaks_cleanly(self):
        out = self._run([EOFError()])
        self.assertTrue(out.endswith("\n\n"))  # print() の改行が最後に 1 つ増える
        self.assertNotIn("今生の幕", out)
        self.assertEqual(self.dispatched, [])


class TestBuildHistoryText(unittest.TestCase):
    def test_formats_user_and_assistant_lines(self):
        text = cli.build_history_text([{"user": "やあ", "assistant": "どうも"}])
        self.assertEqual(text, "ユーザー: やあ\nアシスタント: どうも")

    def test_keeps_only_last_max_history_turns(self):
        n = cli.MAX_HISTORY_TURNS + 2
        history = [{"user": "u{}".format(i), "assistant": "a{}".format(i)} for i in range(n)]
        lines = cli.build_history_text(history).split("\n")
        self.assertEqual(len(lines), 2 * cli.MAX_HISTORY_TURNS)
        self.assertEqual(lines[0], "ユーザー: u2")
        self.assertEqual(lines[-1], "アシスタント: a{}".format(n - 1))
        self.assertNotIn("ユーザー: u0", lines)
        self.assertNotIn("ユーザー: u1", lines)

    def test_empty_history(self):
        self.assertEqual(cli.build_history_text([]), "(なし)")


if __name__ == "__main__":
    unittest.main()
