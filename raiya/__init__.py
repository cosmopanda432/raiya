"""raiya -- 頼耶: 捏造を構造的に通さない記憶エンジン

唯識の三能変 (阿頼耶識 -> 末那識 -> 六識) をそのまま処理パイプラインにした記憶ライブラリ。
LLM が提案した記憶 (evidence 付き) を、決定論の三性ゲート (`sansho.py`) だけで検収する:
LLM は身分 (圓成實/依他起/遍計所執) を自称できず、ユーザー発話への逐語接地のみが
「本当にあった」記憶として残る。

配布形態 (記憶機能設計書§4):
    - ライブラリとして `raiya.engine.MemoryEngine` を直接 import する
    - CLI として `python3 -m raiya.cli`
    - MCP サーバとして `python3 -m raiya.mcp_server` (公式 mcp SDK。stdio または
      `--transport http` の Streamable HTTP。サーバ内に LLM 無し。extra `[mcp]` が必要)

出自: cosmicanalysis/src/Eight_Consciousness (波1〜2、2026-08-25) からの独立リポ切り出し
(波3・記憶機能設計書§9 裁定枠a)。「機構は持ち出す、語彙と統治は置いていく」= KojikiOS の
語彙 (柱/神名帳/御魂/誓約 等) は持ち込まない。唯識語彙 (阿頼耶識/末那識/三性) は頼耶自身の
語彙なのでそのまま使う。
"""
from .engine import MemoryEngine
from .sansho import Turn

__all__ = ["MemoryEngine", "Turn", "__version__"]
__version__ = "0.1.0"
