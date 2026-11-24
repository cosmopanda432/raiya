"""project.py -- `project` 欄の決定 (改修 5 spec §4.2)

hook stdin の `cwd` を git の最上位ディレクトリに正規化する。所熏四義の「共和合」(書く者と
書き先が同所) を「同じリポジトリ」で読み替えるための鍵。粒度は git の最上位 (§11.3)、
値は絶対パス (§12.4: 移動・再 clone で孤立する代償は受け入れる)。

決定論・標準ライブラリのみ・subprocess を使わない (同期 hook の recall は <1 秒の予算)・
`backend` を import しない (mcp_server が読み込んでも LLM が載らない)。
"""
import os


def resolve_project(cwd):
    """`cwd` から上へ `.git` (ディレクトリ**または**ファイル = worktree / submodule) を探し、
    見つかったディレクトリの realpath を返す。git 外なら `cwd` の realpath。

    非 str・空・相対パスは None (= project 無し。種子は全域になる)。
    """
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        return None
    path = os.path.realpath(cwd)
    current = path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return path
        current = parent
