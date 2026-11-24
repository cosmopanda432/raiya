"""backend.py -- LLM 抽象レイヤー

対話の応答生成・熏習提案 (propose) パス・内観パスは、すべてこの同一の backend 抽象を通す。
HTTP は標準ライブラリの urllib のみ (設定ファイルを読む `raiya.config` だけが PyYAML を使う)。CLI モード専用 -- ライブラリ facade
(`engine.MemoryEngine`) の決定論メソッド (recall/imprint/introspect_material/
record_introspection/audit) はこのモジュールに依存しない。MCP サーバ (`mcp_server.py`) も
このモジュールを import しない (サーバ内に LLM を置かない=記憶機能設計書§4.3)。

設定の優先順位は env (空でない値) > 設定ファイル (`llm:` 節・raiya/config.py) > 既定。
`llm.timeout_sec` / `llm.openai_max_completion_tokens` はファイルだけ (env の対応物なし):
ファイルが無ければ定数 REQUEST_TIMEOUT_SEC / OPENAI_MAX_COMPLETION_TOKENS が既定。
設定ファイルに issue が 1 つでもあれば、LLM を呼ぶ経路 (call_llm と各 `_call_*`) は
BackendError で止まる (fail-closed: `command` 利用者の backend のタイプミスで、台帳の
本文を既定の openai-api へ黙って送らないため。設計書 2026-10-23-config-file-design.md §5.2)。
API キー (RAIYA_OPENAI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY) は env だけ。

環境変数:
    RAIYA_BACKEND        openai-api (既定) | claude-cli | anthropic-api | command
    RAIYA_MODEL          backend にそのまま渡すモデル名
    RAIYA_OPENAI_API_KEY / OPENAI_API_KEY
                         openai-api backend の API キー (前者を優先)
    RAIYA_LLM_CMD        command backend で実行するシェルコマンド文字列
    RAIYA_PROPOSE_MODEL  propose (熏習提案) 呼び出し専用のモデル名。RAIYA_MODEL とは
                         独立=対話本文の応答モデルには影響しない。既定は backend ごと:
                         openai-api なら gpt-6-luna、claude-cli なら haiku
                         (claude-cli の propose 呼び出しは `--no-session-persistence`
                         も付ける)

呼び出し失敗時は 1 回だけ再試行し、それでも失敗すれば BackendError を送出する。
BackendError は reason (有界な事実のみの定型文) と detail (backend の生出力。stderr /
HTTP body 等で会話本文や秘密を含みうるため長命な平文ログには書かない) に分かれる
(記憶機能設計書§10.3d)。
"""
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

from . import config

DEFAULT_ANTHROPIC_API_MODEL = "claude-haiku-5-5"
DEFAULT_OPENAI_API_MODEL = "gpt-6-luna"
REQUEST_TIMEOUT_SEC = 60
# openai-api は呼び出し側の max_tokens に関わらずこの値を max_completion_tokens に送る。
# gpt-6-luna など reasoning 系モデルは思考トークンも上限に数えるため、呼び出し側の小さな
# 上限 (512 / 1024 等) では長い prompt で content が空のまま finish_reason=length になる
# (Issue #9)。
OPENAI_MAX_COMPLETION_TOKENS = 64000
# anthropic-api も同様に、呼び出し側の max_tokens に関わらずこの値を max_tokens に送る。
ANTHROPIC_MAX_TOKENS = 64000
MAX_RETRIES = 1  # 失敗時の再試行回数 (合計で最大 MAX_RETRIES+1 回試行)
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_BACKEND = "openai-api"

# openai-api が finish_reason に返しうる既知の値 (OpenAI API 仕様上の enum)。
# BackendError.reason に埋め込んでよい値をこれに限定する (記憶機能設計書§10.3d:
# 長命ログに書ける有界な事実は許可リストの値だけ。リスト外の生値は "other" に丸める)。
FINISH_REASONS = ("stop", "length", "content_filter", "tool_calls", "function_call")


def _finish_reason_label(value):
    """finish_reason を BackendError.reason へ埋め込める有界なラベルに丸める。

    None は "None"、許可リスト内はその値そのまま、リスト外 (未知の値) は "other"。
    """
    if value is None:
        return "None"
    if value in FINISH_REASONS:
        return value
    return "other"


class BackendError(RuntimeError):
    """LLM 呼び出しに失敗した場合に送出する。

    `reason` = 失敗カテゴリ(このモジュールが書いた定型文。有界な事実=HTTP status・
    backend/model 名(operator の env 設定)・例外クラス名・FINISH_REASONS 許可リスト内の
    finish_reason 値 だけを埋め込んでよい)。
    `detail` = backend が返した本文(stderr / HTTP body / 応答 body / 接続エラー文 /
    許可リスト外の finish_reason 生値)。長命な平文ログ(hooks.log)には reason と
    len(detail) だけを書く(記憶機能設計書§10.3c/10.3d)。
    str(e) の書式("reason: detail"、detail が空なら reason のみ)自体は変わらないが、
    中身は §10.3d 以前と2点だけ異なる: (1) 解析失敗の2サイト (openai-api/
    anthropic-api) は reason に例外クラス名が増えた。(2) finish_reason が
    許可リスト外のとき reason 側は "other" に丸められ、生値は detail 側へ移る。
    """

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail or ""
        message = "{}: {}".format(reason, self.detail) if self.detail else reason
        super().__init__(message)


class Settings(NamedTuple):
    """env > 設定ファイル > 既定 を重ねた、呼び出し 1 回ぶんの設定 (`_settings` が作る)。"""

    backend: str
    model: Optional[str]
    propose_model: Optional[str]
    command: Optional[str]
    timeout_sec: float
    max_completion_tokens: int


def _resolve(cfg):
    values = cfg.values

    def pick(env_name, key):
        return os.environ.get(env_name) or values.get("llm." + key)

    return Settings(
        backend=pick("RAIYA_BACKEND", "backend") or DEFAULT_BACKEND,
        model=pick("RAIYA_MODEL", "model"),
        propose_model=pick("RAIYA_PROPOSE_MODEL", "propose_model"),
        command=pick("RAIYA_LLM_CMD", "command"),
        timeout_sec=values.get("llm.timeout_sec", REQUEST_TIMEOUT_SEC),
        max_completion_tokens=values.get("llm.openai_max_completion_tokens", OPENAI_MAX_COMPLETION_TOKENS),
    )


def _settings():
    """設定の解決。設定ファイルに issue が 1 つでもあれば BackendError (fail-closed・§5.2)。

    reason には summary (category / key パス / 行) だけを入れ、未知のキー名・値・パスなどは
    detail へ (hooks.log には detail_len しか出ない)。
    """
    cfg = config.get()
    if cfg.issues:
        raise BackendError(
            "設定ファイルが不正です ({})".format(config.summary(cfg)),
            detail=" / ".join("{}: {}".format(i.category, i.detail) for i in cfg.issues),
        )
    return _resolve(cfg)


def _model_for(name, purpose, settings=None):
    """backend 名と purpose から実際に渡すモデル名を決める (通信しない純関数)。

    各 `_call_*` と `describe` が同じ解決を共有する (二重の解決で食い違わないように)。
    None = モデルを指定しない (claude-cli の `--model` 無し / command / 未知の backend)。
    settings=None ならその場で解決する (issue があれば BackendError)。
    """
    if settings is None:
        settings = _settings()
    propose = purpose == "propose"
    if name == "claude-cli":
        if propose:
            return settings.propose_model or "haiku"
        return settings.model or None
    if name == "openai-api":
        model = settings.propose_model if propose else settings.model
        return model or DEFAULT_OPENAI_API_MODEL
    if name == "anthropic-api":
        return settings.model or DEFAULT_ANTHROPIC_API_MODEL
    return None


def describe(purpose=None):
    """(backend 名, モデル名) を返す (デバッグログ用・通信しない)。`call_llm` と同じ解決。

    デバッグ記録のために統合を壊さないよう、設定ファイルの issue では送出しない
    (有効なキーと env だけで解決する。止めるのは call_llm の役目)。
    """
    settings = _resolve(config.get())
    return settings.backend, _model_for(settings.backend, purpose, settings)


def call_llm(prompt, max_tokens=1024, purpose=None):
    """設定された backend で LLM を呼び出し、応答テキストを返す。

    失敗時は 1 回再試行し、それでも失敗すれば BackendError を送出する。
    設定は再試行ループの外で 1 回だけ解決する (設定ファイルの不正は再試行しない)。

    `purpose="propose"` を渡すと propose 専用モデル (RAIYA_PROPOSE_MODEL。既定は
    openai-api なら gpt-6-luna、claude-cli なら haiku) を使う。claude-cli では
    さらに `--no-session-persistence` と再帰ガード env が付く
    (`engine.propose()` からのみ渡される。既存呼び出し元 (対話本文の応答生成・内観)
    は purpose を渡さないため挙動は既定値のまま不変)。
    """
    settings = _settings()
    backend_name = settings.backend
    dispatch = {
        "openai-api": lambda p: _call_openai_api(p, max_tokens, purpose=purpose, settings=settings),
        "claude-cli": lambda p: _call_claude_cli(p, purpose=purpose, settings=settings),
        "anthropic-api": lambda p: _call_anthropic_api(p, max_tokens, settings=settings),
        "command": lambda p: _call_command(p, settings=settings),
    }
    fn = dispatch.get(backend_name)
    if fn is None:
        raise BackendError(
            "未知の RAIYA_BACKEND です: {} (openai-api / claude-cli / anthropic-api / command のいずれかを指定してください)".format(
                backend_name
            )
        )

    last_err = None
    for _attempt in range(MAX_RETRIES + 1):
        try:
            return fn(prompt)
        except BackendError as e:
            last_err = e
    raise BackendError(
        "LLM 呼び出しに失敗しました (backend={}): {}".format(backend_name, last_err.reason),
        detail=last_err.detail,
    )


# propose (熏習提案・統合) の子 claude を封じ込める (claude-mem `src/sdk/hardened-options.ts` 参照)。
# 統合プロンプトには台帳の本文 (会話・貼り付け) がそのまま載るので、子が道具・ユーザー設定
# (hooks / plugins / CLAUDE.md / 許可規則)・MCP (raiya 自身を含む)・skills を持たないようにする:
# 本文に紛れた指示で手が出せず、会話以外の文章が提案の材料に混ざらない。2026-10-08 実測
# (Claude Code 2.1.292): この組で init の tools / mcp_servers / slash_commands が空・作業ディレクトリの
# CLAUDE.md とプロジェクト settings の hook が効かない・OAuth (契約) のまま動く。`--bare` は OAuth を
# 読まない (ANTHROPIC_API_KEY 必須) ので使わない。
CLAUDE_CLI_PROPOSE_ISOLATION = [
    "--tools", "",
    "--setting-sources", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--permission-mode", "dontAsk",
]


def _call_claude_cli(prompt, purpose=None, settings=None):
    if settings is None:
        settings = _settings()
    cmd = ["claude", "-p", prompt]
    env = dict(os.environ)
    # claude -p は対話本文・内観でも新しい Claude Code 起動になる。ユーザー設定に登録された raiya の
    # hooks が本番 ~/.raiya に対して走る (二重注入・幽霊 session・本番 consolidate; Issue #26) ので、
    # 全 purpose で再帰ガードを子の env に伝える。--strict-mcp-config も全呼び出しに付け
    # (propose は ISOLATION に含まれる)、~/.claude.json の raiya MCP を子に載せない。
    env["RAIYA_HOOK_ACTIVE"] = "1"
    if purpose == "propose":
        # 熏習提案の呼び出し (hooks の stop からも engine.propose() 経由で再利用される):
        # 軽量モデル+セッション非永続化+封じ込め。
        cmd += ["--model", _model_for("claude-cli", purpose, settings), "--no-session-persistence"] + CLAUDE_CLI_PROPOSE_ISOLATION
    else:
        cmd += ["--strict-mcp-config"]
        model = _model_for("claude-cli", purpose, settings)
        if model:
            cmd += ["--model", model]
    try:
        # stdin を閉じる: 開いたままだと claude -p は stdin の入力を 3 秒待ってから進む。
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=settings.timeout_sec, env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise BackendError("claude コマンドが見つかりません (Claude Code がインストールされていますか)") from e
    except subprocess.TimeoutExpired as e:
        raise BackendError("claude -p の呼び出しがタイムアウトしました") from e
    if result.returncode != 0:
        raise BackendError("claude -p が失敗しました", detail=result.stderr.strip())
    return result.stdout.strip()


def _call_openai_api(prompt, max_tokens, purpose=None, settings=None):
    """OpenAI Chat Completions API を叩く (urllib)。

    propose (熏習提案) 呼び出しは RAIYA_PROPOSE_MODEL、それ以外は RAIYA_MODEL を使い、
    どちらも未設定なら DEFAULT_OPENAI_API_MODEL (gpt-6-luna) に落ちる。propose 経路
    だけは `finish_reason == "length"` (出力が上限で切れた) を BackendError に
    する -- 出力が JSON 契約だから。対話・内観の経路は切れた本文をそのまま返す。
    claude-cli backend と違い子プロセスとして Claude Code を起動しないため、
    hooks の再帰ガード (RAIYA_HOOK_ACTIVE) はここでは不要。
    max_tokens は受け取るが送らない: 上限は常に settings.max_completion_tokens
    (設定ファイルの llm.openai_max_completion_tokens、無ければ OPENAI_MAX_COMPLETION_TOKENS。Issue #9)。
    """
    if settings is None:
        settings = _settings()
    api_key = os.environ.get("RAIYA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise BackendError("OPENAI_API_KEY (または RAIYA_OPENAI_API_KEY) が設定されていません")
    model = _model_for("openai-api", purpose, settings)
    payload = {
        "model": model,
        # 新しめの OpenAI モデルは max_tokens を受け付けず max_completion_tokens を要求する
        # (この上限には reasoning tokens も含まれる)。呼び出し側の max_tokens は使わない。
        "max_completion_tokens": settings.max_completion_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_API_URL,
        data=data,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": "Bearer {}".format(api_key),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise BackendError("openai-api が HTTP {} を返しました".format(e.code), detail=detail) from e
    except urllib.error.URLError as e:
        raise BackendError("openai-api への接続に失敗しました", detail=str(e)) from e
    except (TimeoutError, socket.timeout) as e:
        # 応答待ちの read タイムアウトは URLError に包まれず素の TimeoutError で来る
        # (3.9 では socket.timeout が別クラス)。reason は定型文と operator 設定値だけ。
        raise BackendError(
            "openai-api の応答がタイムアウトしました (model={}, timeout={}秒)".format(model, settings.timeout_sec)
        ) from e
    except (ValueError, KeyError) as e:
        raise BackendError(
            "openai-api の応答の解析に失敗しました ({})".format(type(e).__name__), detail=str(e)
        ) from e

    choices = body.get("choices", []) if isinstance(body, dict) else []
    if not choices:
        raise BackendError("openai-api の応答に choices がありません", detail="{}".format(body))
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    text = (message.get("content") or "").strip()
    if not text:
        # max_completion_tokens を reasoning tokens が食い切った場合などは content が空になる。
        # finish_reason は backend (server) が返す値なので、許可リスト外を生のまま
        # reason に埋め込まない (_finish_reason_label で丸める)。許可リスト外
        # (label=="other") のときだけ生値を detail 側へ退避する: hooks.log には
        # detail_len しか出ないが、cli.py の str(e) 表示 (対話中の失敗表示) では
        # 依然として原因を追える (記憶機能設計書§10.3d)。
        raw_finish_reason = choice.get("finish_reason")
        label = _finish_reason_label(raw_finish_reason)
        detail = "finish_reason={!r}".format(raw_finish_reason) if label == "other" else ""
        raise BackendError(
            "openai-api が空の応答を返しました (model={}, finish_reason={})".format(model, label),
            detail=detail,
        )
    if purpose == "propose" and choice.get("finish_reason") == "length":
        # propose/統合の経路だけ、部分出力があっても失敗として扱う。この経路の出力は
        # JSON 契約なので、途中で切れた本文をそのまま返すと呼び出し元
        # (engine._parse_consolidation) では「LLM が空配列を返した」(健全) と区別できず、
        # 窓を消費して統合が丸ごと消える。
        # 対話本文の生成・内観 (purpose なし) では切れた応答もそのまま返す: 人が読む
        # 文章は途中まででも使えるうえ、ここで失敗にすると再試行で HTTP 呼び出しが
        # 倍になり、部分的な回答まで捨ててしまう (改修4 の再レビュー裁定 2026-08-27)。
        # reason は許可リスト (FINISH_REASONS) の値だけを埋め込み、切れた本文は
        # detail 側へ置く (hooks.log には detail_len しか出ない=記憶機能設計書§10.3d)。
        raise BackendError(
            "openai-api の出力が finish_reason=length で打ち切られました"
            " (上限で切れた, model={})".format(model),
            detail=text,
        )
    return text


def _call_anthropic_api(prompt, max_tokens, settings=None):
    if settings is None:
        settings = _settings()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise BackendError("ANTHROPIC_API_KEY が設定されていません")
    model = _model_for("anthropic-api", None, settings)
    payload = {
        "model": model,
        # 呼び出し側の max_tokens は使わない (受け取るだけ)。
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=data,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise BackendError("anthropic-api が HTTP {} を返しました".format(e.code), detail=detail) from e
    except urllib.error.URLError as e:
        raise BackendError("anthropic-api への接続に失敗しました", detail=str(e)) from e
    except (TimeoutError, socket.timeout) as e:
        # 応答待ちの read タイムアウトは URLError に包まれず素の TimeoutError で来る
        # (3.9 では socket.timeout が別クラス)。reason は定型文と operator 設定値だけ。
        raise BackendError(
            "anthropic-api の応答がタイムアウトしました (model={}, timeout={}秒)".format(model, settings.timeout_sec)
        ) from e
    except (ValueError, KeyError) as e:
        raise BackendError(
            "anthropic-api の応答の解析に失敗しました ({})".format(type(e).__name__), detail=str(e)
        ) from e

    parts = body.get("content", []) if isinstance(body, dict) else []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    return "".join(texts).strip()


def _call_command(prompt, settings=None):
    if settings is None:
        settings = _settings()
    cmd = settings.command
    if not cmd:
        raise BackendError("RAIYA_LLM_CMD が設定されていません")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=settings.timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError("command backend の呼び出しがタイムアウトしました") from e
    if result.returncode != 0:
        raise BackendError("command backend が失敗しました", detail=result.stderr.strip())
    return result.stdout.strip()
