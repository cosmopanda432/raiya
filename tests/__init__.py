import os

# 開発者の ~/.config/raiya/config.yaml をテストに混ぜない (設定ファイル設計書 §8)。
# setdefault ではなく強制する。サブプロセスは env=dict(os.environ, ...) で受け継ぐ。
os.environ["RAIYA_CONFIG"] = ""

# scripts/ のハーネス・ランナー (receipt_harness 等) をテストから import できるようにする。
import sys

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
