"""第三分 (今のコード) と第四分 (受領証の記録) の互証 (七文書による検証層 設計書 §1.3)。

`receipt_harness.main(out_dir=一時ディレクトリ)` で §5 の決定論 fixture を流し直し、
リポジトリの `receipts/deterministic.json` / `receipts/recall.json` と比べる。比べる前に
どの深さの `generated_at` / `judged_at` も取り除き、それ以外は JSON の値として完全一致
(`==`) を求める。推論や閾値は使わない (現量)。

落ちたら、受領証が古いか、コードが受領証の主張から外れたかのどちらか。テストはどちらとも
判断しない (人が受領証を流し直して commit するか、コードを直す)。
`recall_before.json` (改修前の基準) と `smoke*.json` / `judgment_smoke*.json` (実 LLM の
記録=見分の記録。第四分は第三分だけを縁じる = T1585_002:363) は照合の対象にしない。
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import receipt_harness  # noqa: E402
from raiya.engine import INJECT_DISPOSITIONS_ENV  # noqa: E402

REPO_RECEIPTS = os.path.join(receipt_harness.BASE_DIR, "receipts")
TIMESTAMP_KEYS = ("generated_at", "judged_at")
COMPARED = ("deterministic.json", "recall.json")


def _dir_digest(path):
    # tests/test_manual_smoke_consolidate.py の `_dir_digest` と同じ方式。
    digest = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        digest.update(name.encode())
        with open(full, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def _drop_timestamps(value):
    if isinstance(value, dict):
        return {k: _drop_timestamps(v) for k, v in value.items() if k not in TIMESTAMP_KEYS}
    if isinstance(value, list):
        return [_drop_timestamps(v) for v in value]
    return value


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestReceiptReproduces(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="receipt_reproduce_")
        self._repo_digest = _dir_digest(REPO_RECEIPTS)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.assertEqual(_dir_digest(REPO_RECEIPTS), self._repo_digest)

    def test_rerun_matches_committed_receipts_except_timestamps(self):
        # recall は RAIYA_INJECT_DISPOSITIONS を読む。受領証は既定 (注入しない) で採ったので、
        # 他のテストや呼び出し元の環境が漏れても結果が揺れないよう既定に固定する。
        env = {k: v for k, v in os.environ.items() if k != INJECT_DISPOSITIONS_ENV}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            receipt_harness.main(out_dir=self.tmpdir)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "recall_before.json")))
        for name in COMPARED:
            with self.subTest(receipt=name):
                fresh = _load(os.path.join(self.tmpdir, name))
                committed = _load(os.path.join(REPO_RECEIPTS, name))
                # 時刻のキーが実際に在り、取り除かれていること (取り除き損ねの空振り防止)。
                self.assertIn("generated_at", fresh)
                self.assertEqual(_drop_timestamps(fresh), _drop_timestamps(committed))

    def test_drop_timestamps_is_recursive_and_keeps_everything_else(self):
        value = {"generated_at": 1, "a": [{"judged_at": 2, "b": 3}], "c": {"judged_at": 4, "d": [5]}}
        self.assertEqual(_drop_timestamps(value), {"a": [{"b": 3}], "c": {"d": [5]}})


class TestParseOutDir(unittest.TestCase):
    """`--out` の値が無いときは receipts/ へ黙って戻らず、エラーで止まる。"""

    def test_values(self):
        self.assertEqual(receipt_harness._parse_out_dir(["--out", "D"]), "D")
        self.assertEqual(receipt_harness._parse_out_dir(["--out=D"]), "D")
        self.assertEqual(receipt_harness._parse_out_dir(["--recall-before", "--out", "D"]), "D")
        self.assertIsNone(receipt_harness._parse_out_dir([]))
        self.assertIsNone(receipt_harness._parse_out_dir(["--recall-before"]))

    def test_missing_value_exits_with_message_on_stderr(self):
        for argv in (["--out"], ["--out="], ["--out", ""]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    receipt_harness._parse_out_dir(argv)
                # sys.exit(str) は文言を stderr に出して終了コード 1 になる。
                self.assertIsInstance(cm.exception.code, str)
                self.assertIn("--out", cm.exception.code)

    def test_main_with_missing_out_value_never_writes_receipts(self):
        never = mock.Mock(side_effect=AssertionError("引数の誤りは fixture を流す前に止める"))
        with mock.patch.object(sys, "argv", ["receipt_harness.py", "--out"]), \
                mock.patch.object(receipt_harness, "run_deterministic_harness", never), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                receipt_harness.main()
        # 文言は sys.exit(str) がプロセス終了時に stderr へ出す (CLI で実測: exit=1)。
        self.assertIn("--out", cm.exception.code)
        never.assert_not_called()
        # tearDown 相当: receipts/ は変わらない (setUp で採った digest と比べる)
        self.assertEqual(_dir_digest(REPO_RECEIPTS), self._digest)

    def setUp(self):
        self._digest = _dir_digest(REPO_RECEIPTS)


if __name__ == "__main__":
    unittest.main()
