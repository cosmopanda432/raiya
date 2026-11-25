"""README.md と manual/*.md の相対リンクが、各ファイルの置き場所から見て実在すること。

repo のファイルを読むだけで、何も書かない・消さない。
"""
import glob
import os
import re
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LINK = re.compile(r"\]\(([^)\s]+)\)")


class TestManualLinks(unittest.TestCase):
    def test_manual_relative_links_resolve(self):
        files = [os.path.join(REPO_ROOT, "README.md")]
        files += sorted(glob.glob(os.path.join(REPO_ROOT, "manual", "*.md")))
        self.assertGreaterEqual(len(files), 6)
        checked = 0
        for path in files:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            for target in LINK.findall(text):
                if target.startswith("http") or target.startswith("#"):
                    continue
                rel = target.split("#", 1)[0]
                resolved = os.path.normpath(os.path.join(os.path.dirname(path), rel))
                checked += 1
                self.assertTrue(os.path.exists(resolved),
                                "%s のリンク %r は実在しない (%s)" % (os.path.relpath(path, REPO_ROOT), target, resolved))
        self.assertGreater(checked, 0)

    def test_no_link_syntax_inside_code_fences(self):
        files = [os.path.join(REPO_ROOT, "README.md")]
        files += sorted(glob.glob(os.path.join(REPO_ROOT, "manual", "*.md")))
        fenced_lines = 0
        for path in files:
            in_fence = False
            with open(path, "r", encoding="utf-8") as f:
                for no, line in enumerate(f, 1):
                    if line.lstrip().startswith("```"):
                        in_fence = not in_fence
                        continue
                    if in_fence:
                        fenced_lines += 1
                        self.assertNotIn("](", line,
                                         "%s:%d 囲みの中にリンク記法がある" % (os.path.relpath(path, REPO_ROOT), no))
        self.assertGreater(fenced_lines, 0)


if __name__ == "__main__":
    unittest.main()
