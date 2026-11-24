import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from raiya.project import resolve_project  # noqa: E402


class TestResolveProject(unittest.TestCase):
    """改修 5 spec §4.2: cwd を git の最上位ディレクトリに正規化する。subprocess を使わない。"""

    def setUp(self):
        self.tmpdir = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_git_dir_found_from_subdir(self):
        repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        sub = os.path.join(repo, "a", "b")
        os.makedirs(sub)
        self.assertEqual(resolve_project(sub), repo)
        self.assertEqual(resolve_project(repo), repo)

    def test_git_file_counts_as_worktree(self):
        wt = os.path.join(self.tmpdir, "wt")
        os.makedirs(os.path.join(wt, "src"))
        with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /elsewhere\n")
        self.assertEqual(resolve_project(os.path.join(wt, "src")), wt)

    def test_nested_repo_uses_the_nearest_git(self):
        outer = os.path.join(self.tmpdir, "outer")
        inner = os.path.join(outer, "inner")
        os.makedirs(os.path.join(outer, ".git"))
        os.makedirs(os.path.join(inner, ".git"))
        self.assertEqual(resolve_project(os.path.join(inner)), inner)

    def test_no_git_returns_cwd_realpath(self):
        plain = os.path.join(self.tmpdir, "plain")
        os.makedirs(plain)
        self.assertEqual(resolve_project(plain), plain)

    def test_symlink_is_resolved(self):
        repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        link = os.path.join(self.tmpdir, "link")
        os.symlink(repo, link)
        self.assertEqual(resolve_project(link), repo)

    def test_non_str_empty_relative_return_none(self):
        self.assertIsNone(resolve_project(None))
        self.assertIsNone(resolve_project(""))
        self.assertIsNone(resolve_project(123))
        self.assertIsNone(resolve_project("relative/path"))


if __name__ == "__main__":
    unittest.main()
