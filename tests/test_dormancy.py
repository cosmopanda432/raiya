"""休眠 (所在のローカル絶対 path の不在) と同点の並び。LLM は使わない。
spec: docs/superpowers/specs/2026-10-24-dormancy-and-accumulation-design.md §2.2 / §4 / §7。

stat を踏むテストは自作の tmp dir の path だけを使う (実在の path を前提にしない)。
削除するのは自作の tmp だけ (runtime 由来の path・cwd・BASE_DIR は消さない)。
"""
import copy
import errno
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.alaya import (  # noqa: E402
    GERMINATION_REASON_DORMANT,
    INDEX_REASON_DORMANT,
    _absent_local_paths,
    extract_local_paths,
)
from raiya.engine import INJECT_DISPOSITIONS_ENV, MemoryEngine  # noqa: E402
from tests.test_inspect import P, _ids, _seed, _tree_digest, _write_state  # noqa: E402

E, H = "圓成實", "依他起"


class TestExtractLocalPaths(unittest.TestCase):
    def check(self, text, expected):
        got = extract_local_paths(text)
        self.assertEqual(got, tuple(expected), text)
        self.assertTrue(all(p in text for p in got), text)  # 返り値は常に原文の部分文字列

    def test_plain_absolute_path(self):
        self.check("資料は /tmp/raiya-x/doc.md にある。", ["/tmp/raiya-x/doc.md"])

    def test_returns_tuple_and_non_str_is_empty(self):
        self.assertIsInstance(extract_local_paths("/a/b"), tuple)
        self.assertEqual(extract_local_paths(None), ())

    def test_tilde_not_expanded(self):
        self.check("~/x/y", ["~/x/y"])
        self.check("設定は ~/.config/raiya/config.yaml にある", ["~/.config/raiya/config.yaml"])

    def test_u2026_is_three_dots(self):
        self.check("/a/\u2026/b", [])  # 途中の省略
        self.check("/d/e/\u2026", ["/d/e"])

    def test_urls_and_host_paths_are_not_anchors(self):
        for text in ("https://example.com/a/b", "file:///a/b", "C:/a/b", "host:/a/b", "user@host:/a/b"):
            self.check(text, [])

    def test_relative_variable_and_elided_are_not_anchors(self):
        for text in ("docs/x/y", "./a/b", "../a/b", "$HOME/x/y", "<elided 3 chars>/x/y", "\ufffc/x/y"):
            self.check(text, [])

    def test_equals_is_not_a_path_char(self):
        self.check("--out=/x/y", ["/x/y"])

    def test_path_ends_at_non_path_chars(self):
        self.check("`/a/b` と「/c/d」、(/e/f)", ["/a/b", "/c/d", "/e/f"])
        self.check("/a/b,/c/d", ["/a/b", "/c/d"])  # `,` は path 文字でなく、次の `/` は `,` の後なので始点
        self.check("/tmp/xにある", ["/tmp/x"])

    def test_colon_and_hash_cut(self):
        self.check("/tmp/a.py:12", ["/tmp/a.py"])
        self.check("/tmp/a.md#L3", ["/tmp/a.md"])

    def test_reject_char_adjacent_excluded(self):
        for text in ("/a/b/*.py", "/a/b?", "/a/[x]", "/a/{b}", "/a/b$X", "/a/b\\c", "/a/b<c", "/a/b>", "/a/b\ufffc"):
            self.check(text, [])

    def test_trailing_rules(self):
        self.check("/d/e/...", ["/d/e"])  # 末尾の . だけの成分(3 個以上)は落とす
        self.check("raiya.../x/y", [])  # `..` で終わる名前の省略
        self.check("/a/b..", [])
        self.check("置き場所は /srv/data/file.", ["/srv/data/file"])  # 文末の句点
        self.check("/a/.../b", [])  # 途中の省略

    def test_shape_rule(self):
        for text in ("/mcp", "/compact", "/tmp", "~", "/", "~/"):
            self.check(text, [])
        self.check("/tmp/x", ["/tmp/x"])

    def test_double_slash_and_dotdot(self):
        self.check("//x/y", [])
        self.check("/a/b/..", [])
        self.check("/a/./b", [])
        self.check("/a\u2026/b", [])
        self.check("/a.../b", [])
        self.check("/a/b/c.", ["/a/b/c"])
        self.check("/d/e/...", ["/d/e"])
        self.check("/d/e/\u2026", ["/d/e"])

    def test_u2026_mid_component_returns_as_written(self):
        self.check("/a/x\u2026y/b", [])
        self.check("/a/x...y/b", [])
        self.check("/d/e/\u2026", ["/d/e"])
        for text in ("/a/x\u2026y/b", "/a/x...y/b", "/d/e/\u2026", "/d/e/...", "/a\u2026/b", "/a/b/c.",
                     "資料は /tmp/x\u2026 と /tmp/y/z\u2026 と /q/r にある"):
            for path in extract_local_paths(text):
                self.assertIn(path, text)

    def test_trailing_period_before_slash_returns_substring(self):
        self.check("/a/b./", ["/a/b./"])
        self.check("/a/b.//", ["/a/b.//"])
        self.check("/srv/data./ にある", ["/srv/data./"])
        self.check("~/-./", ["~/-./"])
        self.check("/a/b/c.", ["/a/b/c"])
        self.check("/d/e/...//", ["/d/e"])

    def test_fuzz_returns_substrings_without_u2026(self):
        rng = random.Random(20261024)
        alphabet = "/~.a\u2026: #xあ$<>\ufffc-"
        for _ in range(20000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
            for path in extract_local_paths(text):
                self.assertIn(path, text)
                self.assertNotIn("\u2026", path)

    def test_multiple_and_dedup_in_order(self):
        self.check("(/a/b) と `~/c/d` と /a/b", ["/a/b", "~/c/d"])

    def test_no_path(self):
        self.check("資料は共有の場所にある。", [])


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix="raiya_dormancy_"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)  # 自作の tmp だけ
        self.state = os.path.join(self.tmpdir, "state")
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.loc = os.path.join(self.tmpdir, "loc")
        os.makedirs(self.loc)

    def path(self, name):
        return os.path.join(self.loc, name)

    def touch(self, name):
        p = self.path(name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        return p

    def loc_seed(self, seed_id, *paths, conds=("資料",), sansho=E, **kw):
        content = "資料は {} にある。".format(" と ".join(paths)) if paths else "資料は共有の場所にある。"
        return _seed(seed_id, list(conds), kind="所在", sansho=sansho, content=content, **kw)

    def engine(self, seeds):
        _write_state(self.state, seeds)
        return MemoryEngine(state_dir=self.state, new_life=False)


class TestAbsentLocalPaths(_TmpCase):
    def test_missing_path_is_dormant_and_reports_paths_as_written(self):
        a, b = self.path("a.md"), self.path("b.md")
        seed = self.loc_seed(1, a, b)
        self.assertEqual(_absent_local_paths(seed), (a, b))

    def test_existing_path_is_not_dormant(self):
        seed = self.loc_seed(1, self.touch("a.md"))
        self.assertEqual(_absent_local_paths(seed), ())

    def test_one_existing_among_several_is_not_dormant(self):
        seed = self.loc_seed(1, self.path("gone.md"), self.touch("here.md"))
        self.assertEqual(_absent_local_paths(seed), ())

    def test_directory_counts_as_present(self):
        seed = self.loc_seed(1, self.loc)
        self.assertEqual(_absent_local_paths(seed), ())

    def test_not_a_directory_is_absent(self):
        f = self.touch("file")
        seed = self.loc_seed(1, f + "/child")
        self.assertEqual(_absent_local_paths(seed), (f + "/child",))

    def test_broken_symlink_is_dormant(self):
        link = self.path("link.md")
        os.symlink(self.path("nowhere.md"), link)
        self.assertEqual(_absent_local_paths(self.loc_seed(1, link)), (link,))

    def test_permission_error_is_not_dormant(self):
        seed = self.loc_seed(1, self.path("a.md"))
        with mock.patch("raiya.alaya.os.stat", side_effect=PermissionError("denied")):
            self.assertEqual(_absent_local_paths(seed), ())

    def test_eloop_and_enametoolong_explicit(self):
        seed = self.loc_seed(1, self.path("a.md"))
        for code in (errno.ELOOP, errno.ENAMETOOLONG):
            with mock.patch("raiya.alaya.os.stat", side_effect=OSError(code, os.strerror(code))):
                self.assertEqual(_absent_local_paths(seed), ())

    def test_absent_path_adjacent_to_fffc_or_elided_is_not_dormant(self):
        gone = self.path("gone.md")
        for content in ("資料は {}\ufffc にある".format(gone), "資料は {}<elided 3 chars> にある".format(gone),
                        "資料は \ufffc{} にある".format(gone), "資料は <elided 3 chars>{} にある".format(gone)):
            seed = _seed(1, ["資料"], kind="所在", content=content)
            self.assertEqual(_absent_local_paths(seed), (), content)

    def test_other_oserror_and_valueerror_are_not_dormant(self):
        seed = self.loc_seed(1, self.path("a.md"))
        for exc in (OSError(40, "loop"), ValueError("embedded null")):
            with mock.patch("raiya.alaya.os.stat", side_effect=exc):
                self.assertEqual(_absent_local_paths(seed), ())

    def test_only_location_kind_and_str_content(self):
        p = self.path("a.md")
        self.assertEqual(_absent_local_paths(_seed(1, ["x"], kind="決定", content="捨てた: " + p)), ())
        no_kind = _seed(1, ["x"], content="x " + p)
        del no_kind["kind"]  # 欄が無ければ 性向 = 対象外
        self.assertEqual(_absent_local_paths(no_kind), ())
        non_str = _seed(1, ["x"], kind="所在")
        non_str["content"] = 5
        self.assertEqual(_absent_local_paths(non_str), ())

    def test_no_path_location_is_not_dormant(self):
        self.assertEqual(_absent_local_paths(self.loc_seed(1)), ())

    def test_home_tilde_expands_with_home(self):
        home = os.path.join(self.tmpdir, "home")
        os.makedirs(home)
        seed = self.loc_seed(1, "~/notes/a.md")
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(_absent_local_paths(seed), ("~/notes/a.md",))
            os.makedirs(os.path.join(home, "notes"))
            with open(os.path.join(home, "notes", "a.md"), "w") as f:
                f.write("x")
            self.assertEqual(_absent_local_paths(seed), ())

    def test_unexpandable_tilde_is_not_dormant(self):
        seed = self.loc_seed(1, "~/notes/a.md")
        with mock.patch("raiya.alaya.os.path.expanduser", return_value="~/notes/a.md"):
            self.assertEqual(_absent_local_paths(seed), ())
        with mock.patch("raiya.alaya.os.path.expanduser", return_value="relative/a.md"):
            self.assertEqual(_absent_local_paths(seed), ())

    def test_one_stat_per_path(self):
        seed = self.loc_seed(1, self.path("a.md"), self.path("b.md"))
        real = os.stat
        calls = []

        def spy(p, *a, **k):
            if isinstance(p, str) and p.startswith(self.loc):
                calls.append(p)
            return real(p, *a, **k)

        with mock.patch("raiya.alaya.os.stat", side_effect=spy):
            _absent_local_paths(seed)
        self.assertEqual(sorted(calls), sorted([self.path("a.md"), self.path("b.md")]))


class TestDormancyInRecall(_TmpCase):
    def test_dormant_seed_leaves_germination_index_and_count(self):
        gone = self.path("gone.md")
        here = self.touch("here.md")
        eng = self.engine([self.loc_seed(1, gone), self.loc_seed(2, here)])
        r = eng.recall("資料", project=P)
        self.assertEqual(_ids(r["seeds"]), [2])
        # 発芽した 2 は ① に出るので ③ の索引には繰り返さない (layered-memory 計画 §2.3 の 発芽 の門)。
        self.assertEqual(_ids(r["index"]), [])
        self.assertEqual(eng.alaya.seed_count(project=P, purpose="inject"), 1)
        r = eng.recall("無関係", project=P)  # 発芽しない問いなら 2 は索引に出て、休眠の 1 は出ない
        self.assertEqual((_ids(r["seeds"]), _ids(r["index"])), ([], [2]))
        self.assertIn("全 1 件", r["narrative"])

    def test_etaki_also_goes_dormant(self):
        eng = self.engine([self.loc_seed(1, self.path("gone.md"), sansho=H, conds=("資料", "資料2"))])
        r = eng.recall("資料 資料2", project=P)
        self.assertEqual((_ids(r["seeds"]), _ids(r["index"])), ([], []))

    def test_explain_shows_reason_and_paths(self):
        gone = self.path("gone.md")
        x = self.engine([self.loc_seed(1, gone)]).explain_recall("資料", project=P)
        self.assertEqual(x["germination_rejected"][0]["reason"], GERMINATION_REASON_DORMANT.format(gone))
        self.assertEqual(x["index_rejected"][0]["reason"], INDEX_REASON_DORMANT.format(gone))

    def test_recall_equals_explain_recall(self):
        seeds = [
            self.loc_seed(1, self.path("gone.md")),
            self.loc_seed(2, self.touch("here.md")),
            self.loc_seed(3),
        ]
        eng = self.engine(seeds)
        x = eng.explain_recall("資料", project=P)
        r = eng.recall("資料", project=P)
        self.assertEqual(_ids(x["seeds"]), _ids(r["seeds"]))
        self.assertEqual(_ids(x["index"]), _ids(r["index"]))
        self.assertEqual(x["narrative"], r["narrative"])

    def test_returns_when_path_comes_back(self):
        p = self.path("later.md")
        eng = self.engine([self.loc_seed(1, p)])
        self.assertEqual(_ids(eng.recall("資料", project=P)["seeds"]), [])
        self.touch("later.md")
        r = eng.recall("資料", project=P)
        self.assertEqual((_ids(r["seeds"]), _ids(r["index"])), ([1], []))  # 発芽したので ③ には繰り返さない
        self.assertEqual(_ids(eng.recall("無関係", project=P)["index"]), [1])

    def test_dormant_seeds_do_not_use_index_cap(self):
        gone = [self.loc_seed(i, self.path("g{}.md".format(i))) for i in range(1, 11)]
        alive = [self.loc_seed(i, self.touch("a{}.md".format(i))) for i in range(11, 13)]
        r = self.engine(gone + alive).recall("無関係", project=P)
        self.assertEqual(_ids(r["index"]), [11, 12])

    def test_unfiltered_views(self):
        gone = self.path("gone.md")
        seed = self.loc_seed(1, gone)
        eng = self.engine([seed])
        self.assertEqual(_ids(eng.alaya.index(project=P, purpose="consolidate")), [1])
        self.assertEqual(_ids(eng.inspect_seeds(project=P)), [1])
        self.assertEqual(eng.audit()["kind_counts"].get("所在"), 1)
        self.assertEqual(eng.alaya.resume_point(P), None)
        resume = self.engine([_seed(1, ["x"], kind="再開点", content="続き " + gone)])
        self.assertEqual(resume.alaya.resume_point(P)["id"], 1)

    def test_other_project_location_is_not_stat(self):
        other = self.loc_seed(1, self.path("gone.md"), project="/home/dev/other")
        eng = self.engine([other])
        real = os.stat
        touched = []

        def spy(p, *a, **k):
            if isinstance(p, str) and p.startswith(self.loc):
                touched.append(p)
            return real(p, *a, **k)

        with mock.patch("raiya.alaya.os.stat", side_effect=spy):
            eng.recall("資料", project=P)
        self.assertEqual(touched, [])

    def _stat_spy(self):
        real = os.stat
        touched = []

        def spy(p, *a, **k):
            if isinstance(p, str) and p.startswith(self.loc):
                touched.append(p)
            return real(p, *a, **k)

        return touched, mock.patch("raiya.alaya.os.stat", side_effect=spy)

    def test_non_location_kinds_are_not_stat(self):
        gone = lambda n: self.path(n)  # noqa: E731
        e_conds = ("資料", "資料2")
        seeds = [
            _seed(1, ["資料", "資料2"], kind="性向", content="いつも " + gone("d.md")),
            _seed(2, ["x"], kind="再開点", content="続きは " + gone("r.md")),
            self.loc_seed(3, gone("s.md"), superseded_by=9),
            self.loc_seed(4, conds=e_conds),  # 圓成實 (path 無し = stat しない)
            self.loc_seed(5, gone("h.md"), sansho=H, conds=e_conds),  # 双子に隠れる 依他起
        ]
        for inject in ("0", "1"):
            with mock.patch.dict(os.environ, {INJECT_DISPOSITIONS_ENV: inject}):
                eng = self.engine(seeds)
                touched, patcher = self._stat_spy()
                with patcher:
                    r = eng.recall("資料 資料2", project=P, session_start=True)
                self.assertEqual(touched, [], inject)
                self.assertEqual(r["resume"]["id"], 2)

    def test_two_stats_per_recall(self):
        # 判定は `_circle_verdicts` の 1 回にまとまったので、path ごとに高々 2 回 (発芽で 1 回・③ で 1 回)。
        # 層化の前は germinate / index / seed_count の 3 回だった。
        eng = self.engine([self.loc_seed(1, self.path("gone.md"))])
        touched, patcher = self._stat_spy()
        with patcher:
            eng.recall("資料", project=P)
        self.assertEqual(len(touched), 2)
        touched, patcher = self._stat_spy()
        with patcher:
            eng.explain_recall("資料", project=P)
        self.assertEqual(len(touched), 2)

    def test_dormancy_writes_nothing_to_seeds(self):
        eng = self.engine([self.loc_seed(1, self.path("gone.md"))])
        before = _tree_digest(self.state)
        eng.explain_recall("資料", project=P)
        self.assertEqual(_tree_digest(self.state), before)
        eng.recall("資料", project=P)
        s = eng.inspect_seeds(project=P)[0]
        self.assertIsNone(s["superseded_by"])
        self.assertEqual((s["history"], s["strength"], s["miss_streak"], s["fire_count"]), ([], 1, 0, 0))


class TestVerdictsArePure(_TmpCase):
    def test_verdicts_do_not_change_seeds(self):
        seeds = [
            self.loc_seed(1, self.path("gone.md")),
            self.loc_seed(2, self.touch("here.md")),
            self.loc_seed(3, sansho=H, conds=("資料", "資料2")),
        ]
        eng = self.engine(seeds)
        before = copy.deepcopy(eng.alaya.state["seeds"])
        eng.alaya._germination_verdicts("資料 資料2", (), project=P)
        eng.alaya._index_verdicts(P, "inject")
        eng.alaya._index_verdicts(P, "consolidate")
        _absent_local_paths(eng.alaya.state["seeds"][0])
        self.assertEqual(eng.alaya.state["seeds"], before)


class TestTieOrder(_TmpCase):
    def test_equal_seeds_come_out_in_id_order(self):
        seeds = [self.loc_seed(i, self.touch("t{}.md".format(i))) for i in (1, 2)]
        eng = self.engine(seeds)
        self.assertEqual(_ids(eng.explain_recall("資料", project=P)["seeds"]), [1, 2])
        # 発芽した種子は ③ に出ないので、索引の並びは発芽しない問いで見る。
        self.assertEqual(_ids(eng.recall("無関係", project=P)["index"]), [1, 2])

    def test_supersede_style_content_change_does_not_move_rank(self):
        seeds = [self.loc_seed(i, self.touch("t{}.md".format(i))) for i in (1, 2)]
        seeds[0]["content"] = "資料は {} に移った。".format(self.touch("new.md"))
        r = self.engine(seeds).recall("無関係", project=P)  # 発芽した種子は ③ に出ないので発芽しない問いで見る
        self.assertEqual(_ids(r["index"]), [1, 2])


class TestFixtureContentsHaveNoPaths(unittest.TestCase):
    """決定論の受領書の fixture の content は extract_local_paths が () を返すものに限る (spec §6)。"""

    def test_receipt_harness_proposals(self):
        import receipt_harness

        contents = []

        def walk(obj, depth=0):
            if depth > 4:
                return
            if isinstance(obj, receipt_harness.ScriptedItem):
                contents.append(obj.proposal.get("content"))
            elif isinstance(obj, (tuple, list)):
                for x in obj:
                    walk(x, depth + 1)

        for value in vars(receipt_harness).values():
            walk(value)
        self.assertGreater(len(contents), 10)
        for content in contents:
            self.assertEqual(extract_local_paths(content), (), content)

    def test_recall_fixture_seeds(self):
        import receipt_harness

        seeds, _ = receipt_harness._load_recall_fixture()
        self.assertGreater(len(seeds), 10)
        for seed in seeds:
            self.assertEqual(extract_local_paths(seed.get("content")), (), seed["id"])


if __name__ == "__main__":
    unittest.main()
