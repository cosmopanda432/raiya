"""config.py -- 利用者単位の設定ファイル config.yaml (設計書 2026-10-23-config-file-design.md)

探索 (最初に当たったものだけ。ファイルは高々 1 つ):
    1. `RAIYA_CONFIG` が空文字 → 設定ファイルを使わない (テスト分離)。
    2. `RAIYA_CONFIG` が空でない → expanduser した値だけを読む (絶対パス必須・無ければ not_found)。
    3. それ以外 → `$XDG_CONFIG_HOME/raiya/config.yaml` (空・相対なら無視して
       `~/.config/raiya/config.yaml`)。無ければ黙って「ファイル無し」。
プロジェクト内 (cwd / git 最上位) と `<state_dir>/` は探さない: clone したリポジトリが
`command` backend のシェルコマンドを選べてしまうため (設計書 §1)。

不正なファイル (§5.1): ファイル全体が読めない/壊れている (not_absolute / not_found /
unreadable / yaml_missing / yaml_syntax / not_mapping / bad_version) ときは全体を無効に
(`whole_file=True`・`values={}`)、unknown_key / bad_type / bad_value はそのキーだけを落とす。
`load()` は例外を出さない。報告用の `summary()` は category / key パス / 行だけを返し、
値・未知のキー名・パス・YAML のエラー文 (= `Issue.detail`) は含めない。

import してよいのは標準ライブラリだけ。`yaml` は `load()` の中で、ファイルがあることを
確かめてから遅延 import する。`backend` / `engine` / `hooks` は import しない (AST pin)。
env は読む側 (engine / backend) がその場で重ねる。ここでメモ化するのはファイルの内容だけ。
"""
import math
import os
import stat
import sys
from typing import NamedTuple, Optional, Tuple

CONFIG_ENV = "RAIYA_CONFIG"
SCHEMA_VERSION = 1
VALID_BACKENDS = ("openai-api", "claude-cli", "anthropic-api", "command")

# config_src (hook_end のレベル 1 キー)
SRC_DISABLED = "disabled"
SRC_ENV = "env"
SRC_XDG = "xdg"
SRC_NONE = "none"

_TOP_KEYS = ("version", "state_dir", "inject_dispositions", "llm")
_LLM_STR_KEYS = ("backend", "model", "propose_model", "command")
_LLM_KEYS = _LLM_STR_KEYS + ("timeout_sec", "openai_max_completion_tokens")

SUMMARY_MAX_ISSUES = 8
# socket.settimeout(1e12) は OverflowError になる。巨大な値が下流 (socket / subprocess) で溢れないように上限を置く。
MAX_CONFIG_BYTES = 1024 * 1024  # 通常ファイルで 1 MiB まで (FIFO / /dev/zero で hook が固まらないように)
BRIEF_MAX = 80
TIMEOUT_SEC_MAX = 10 ** 9  # 巨大な int / float が下流 (socket / subprocess) で溢れないように


class Issue(NamedTuple):
    category: str
    key: str  # 許可リスト上のキーパス ("llm.timeout_sec") か節の名前 ("" / "llm")
    line: Optional[int]
    detail: str  # パス・未知のキー名・YAML のエラー文 (hooks.log には書かない)


class Config(NamedTuple):
    path: Optional[str]
    values: dict
    issues: Tuple[Issue, ...]
    whole_file: bool
    source: str = SRC_NONE


def _resolve(environ):
    """(path | None, source)。純関数 (expanduser のみ)。path は絶対とは限らない (explicit のとき)。"""
    explicit = environ.get(CONFIG_ENV)
    if explicit is not None:
        if explicit == "":
            return None, SRC_DISABLED
        return os.path.expanduser(explicit), SRC_ENV
    xdg = environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        base = xdg
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "raiya", "config.yaml")
    if not os.path.isabs(path):
        return None, SRC_NONE
    return path, SRC_XDG


def config_path(environ=os.environ):
    return _resolve(environ)[0]


def _brief(v):
    """detail に入れてよい有界な記述。repr()/str() を無制限の値に使わない
    (PyYAML の 16 進・60 進の巨大 int は repr が ValueError になる)。"""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int(bit_length={})".format(v.bit_length())
    if isinstance(v, str):
        return v if len(v) <= BRIEF_MAX else "{}...(len={})".format(v[:BRIEF_MAX], len(v))
    return type(v).__name__


def _cap(text):
    text = str(text)
    return text if len(text) <= 400 else text[:400] + "...(len={})".format(len(text))


def _whole(path, category, detail, source, line=None):
    return Config(path, {}, (Issue(category, "", line, detail),), True, source)


def load(path, optional=False, source=SRC_ENV):
    """path の設定ファイルを読んで検証する。例外を出さない。

    optional=True (XDG の既定位置) のときだけ、ファイルが無いことは issue ではなく「ファイル無し」。
    """
    if path is None:
        return Config(None, {}, (), False, source)
    try:
        return _load(path, optional, source)
    except Exception as e:  # noqa: BLE001 -- load() は全関数。個々の分岐に依存させない
        return _whole(path, "bad_value", "load failed: {}".format(type(e).__name__), source)


def _load(path, optional, source):
    if not os.path.isabs(path):
        return _whole(path, "not_absolute", _cap(path), source)
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return _whole(path, "unreadable", "not a regular file", source)
        if st.st_size > MAX_CONFIG_BYTES:
            return _whole(path, "unreadable", "too large (size={})".format(st.st_size), source)
        with open(path, "rb") as f:
            raw = f.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return _whole(path, "unreadable", "too large", source)
    except FileNotFoundError:
        if optional:
            return Config(None, {}, (), False, SRC_NONE)
        return _whole(path, "not_found", _cap(path), source)
    except OSError as e:
        return _whole(path, "unreadable", "{}: {}".format(type(e).__name__, _cap(e)), source)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return _whole(path, "unreadable", "UnicodeDecodeError: {}".format(_cap(e)), source)
    try:
        import yaml
    except ImportError as e:
        return _whole(path, "yaml_missing", _cap(e), source)
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 -- YAMLError に限らず (再帰・メモリ等も) 全体無効に倒す
        line = None
        mark = getattr(e, "problem_mark", None)
        if mark is not None and isinstance(getattr(mark, "line", None), int):
            line = mark.line + 1
        return _whole(path, "yaml_syntax", "{}: {}".format(type(e).__name__, _cap(e)), source, line=line)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return _whole(path, "not_mapping", type(data).__name__, source)
    if data.get("version") is not None:
        ver = data["version"]
        if isinstance(ver, bool) or not isinstance(ver, int) or ver != SCHEMA_VERSION:
            return _whole(path, "bad_version", _brief(ver), source)
    values, issues = _validate(data)
    return Config(path, values, tuple(issues), False, source)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate(data):
    values = {}
    issues = []

    def bad(category, key, detail=""):
        issues.append(Issue(category, key, None, detail))

    def put_str(key, v):
        if not isinstance(v, str):
            bad("bad_type", key, type(v).__name__)
        elif not v.strip() or "\0" in v:
            bad("bad_value", key, "empty or NUL")
        else:
            return True
        return False

    for k in data:
        if k not in _TOP_KEYS:
            bad("unknown_key", "", _brief(k))

    v = data.get("state_dir")
    if v is not None and put_str("state_dir", v):
        expanded = os.path.expanduser(v)
        if os.path.isabs(expanded):
            values["state_dir"] = expanded
        else:
            bad("bad_value", "state_dir", "not absolute")

    v = data.get("inject_dispositions")
    if v is not None:
        if isinstance(v, bool):
            values["inject_dispositions"] = v
        else:
            bad("bad_type", "inject_dispositions", type(v).__name__)

    llm = data.get("llm")
    if llm is not None:
        if not isinstance(llm, dict):
            bad("bad_type", "llm", type(llm).__name__)
        else:
            for k in llm:
                if k not in _LLM_KEYS:
                    bad("unknown_key", "llm", _brief(k))
            for k in _LLM_STR_KEYS:
                key = "llm." + k
                v = llm.get(k)
                if v is None or not put_str(key, v):
                    continue
                if k == "backend" and v not in VALID_BACKENDS:
                    bad("bad_value", key, _brief(v))
                    continue
                values[key] = v
            v = llm.get("timeout_sec")
            if v is not None:
                if not _is_num(v):
                    bad("bad_type", "llm.timeout_sec", type(v).__name__)
                elif not (0 < v <= TIMEOUT_SEC_MAX and (isinstance(v, int) or math.isfinite(v))):
                    bad("bad_value", "llm.timeout_sec", _brief(v))
                else:
                    values["llm.timeout_sec"] = v
            v = llm.get("openai_max_completion_tokens")
            if v is not None:
                if isinstance(v, bool) or not isinstance(v, int):
                    bad("bad_type", "llm.openai_max_completion_tokens", type(v).__name__)
                elif v < 1:
                    bad("bad_value", "llm.openai_max_completion_tokens", _brief(v))
                else:
                    values["llm.openai_max_completion_tokens"] = v
    return values, issues


_memo = {}


def get():
    """1 プロセス 1 回のメモ化。キーは config_path(os.environ) の結果 (変わったときだけ読み直す)。"""
    path, source = _resolve(os.environ)
    key = (path, source)
    cfg = _memo.get(key)
    if cfg is None:
        _memo.clear()
        try:
            cfg = load(path, optional=(source == SRC_XDG), source=source)
        except Exception as e:  # noqa: BLE001 -- 多重防御: get() は決して送出しない
            cfg = _whole(path, "bad_value", "load failed: {}".format(type(e).__name__), source)
        _memo[key] = cfg
    return cfg


def _reset():
    _memo.clear()


def summary(cfg):
    """有界な 1 行: "category=bad_type key=llm.model line=3; …" (detail を含めない)。"""
    parts = []
    for issue in cfg.issues[:SUMMARY_MAX_ISSUES]:
        part = "category={} key={}".format(issue.category, issue.key or "-")
        if issue.line is not None:
            part += " line={}".format(issue.line)
        parts.append(part)
    if len(cfg.issues) > SUMMARY_MAX_ISSUES:
        parts.append("他 {} 件".format(len(cfg.issues) - SUMMARY_MAX_ISSUES))
    return "; ".join(parts)


def state_dir_lost(cfg):
    """設定ファイルが state_dir を決めていたはずなのに失われたか (§5.3)。"""
    return bool(cfg.whole_file or any(i.key == "state_dir" for i in cfg.issues))


def describe_issues(cfg):
    """対話の場 (stderr) 用の 1 行。summary に加えてパスと detail を付けてよい。"""
    details = " / ".join("{}: {}".format(i.category, i.detail) for i in cfg.issues[:SUMMARY_MAX_ISSUES])
    return "{} [{}] {}".format(summary(cfg), cfg.path or "-", details).replace("\n", " ")


def startup_check(prog, explicit_state_dir=False, environ=os.environ, stream=None):
    """raiya-cli / raiya-mcp-server / raiya-inspect の起動時に 1 回呼ぶ (§5.2/§5.3)。

    設定を 1 回読み、issue があれば stderr に 1 行書く (state dir の中には書かない)。
    state_dir が失われ (`state_dir_lost`)、`RAIYA_STATE_DIR` も明示の引数も無いときは、
    `./state` へ落とさず止める: False を返す (呼び出し側が exit 2)。それ以外は True。
    """
    stream = sys.stderr if stream is None else stream
    cfg = get()
    if not cfg.issues:
        return True
    lost = state_dir_lost(cfg) and not explicit_state_dir and not environ.get("RAIYA_STATE_DIR")
    if lost:
        stream.write("{}: 設定ファイルが不正で state_dir を決められないため止めました: {}\n".format(
            prog, describe_issues(cfg)))
        return False
    stream.write("{}: 設定ファイルが不正です (有効なキーと env で続行): {}\n".format(prog, describe_issues(cfg)))
    return True
