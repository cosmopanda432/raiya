"""MCP テスト用の会話ヘルパ (テスト器具。出荷物ではない)。

公式 `mcp` SDK の stdio サーバは `initialize` だけを読み取りループ内で同期処理し、
それ以外のリクエストは別タスクで処理する。stdin が EOF になると処理中のタスクは
打ち切られるため、「全メッセージを一括で流し込んで stdin を閉じ、応答をまとめて回収する」
旧来の書き方では末尾の応答が失われる。実クライアント (Claude Code 等) と同じく
「1 リクエストを送り、その id の応答を待ってから次を送る」会話をここで提供する。
"""
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "raiya-tests", "version": "0.0.1"},
}
INITIALIZED_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def initialize_request(request_id="init", protocol_version=None):
    params = dict(INITIALIZE_PARAMS)
    if protocol_version is not None:
        params["protocolVersion"] = protocol_version
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": params}


def make_env(state_dir):
    env = dict(os.environ)
    env["RAIYA_STATE_DIR"] = state_dir
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StdioServer:
    """`python -m raiya.mcp_server` (stdio) を子プロセスで起動し、1件ずつ応答を待つ。"""

    def __init__(self, env, extra_args=None, timeout=30):
        self.timeout = timeout
        self._stderr_file = tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", prefix="raiya_mcp_stderr_", suffix=".log", delete=False
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "raiya.mcp_server"] + list(extra_args or []),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=REPO_ROOT,
        )
        self.stray = []  # 待っていた id 以外の応答行 (通知に応答が無いことの検証に使う)
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            if line.strip():
                self._lines.put(line)
        self._lines.put(None)  # EOF

    # --- 送信 ---
    def send_raw(self, text):
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def notify(self, message):
        self.send_raw(json.dumps(message, ensure_ascii=False) + "\n")

    def request(self, message):
        self.notify(message)
        return self.read_response(message["id"])

    def initialize(self, protocol_version=None):
        """initialize -> notifications/initialized の標準ハンドシェイク。initialize 応答を返す。"""
        response = self.request(initialize_request(protocol_version=protocol_version))
        self.notify(INITIALIZED_NOTIFICATION)
        return response

    def call_tool(self, request_id, name, arguments=None):
        return self.request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )

    # --- 受信 ---
    def read_response(self, request_id):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "id={!r} の応答が {}s 以内に来ませんでした (stderr: {!r})".format(
                        request_id, self.timeout, self.stderr_tail()
                    )
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(
                    "id={!r} の応答を待つ間にサーバの stdout が閉じました (stderr: {!r})".format(
                        request_id, self.stderr_tail()
                    )
                )
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("id") == request_id:
                return obj
            self.stray.append(obj)

    def stderr_tail(self, n=2000):
        try:
            with open(self._stderr_file.name, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except OSError:
            return ""

    # --- 終了 ---
    def close(self):
        """stdin を閉じてサーバの自然終了を待つ。(returncode, stderr) を返す。"""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            returncode = self.proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
            raise AssertionError("stdin を閉じてもサーバが終了しません (stderr: {!r})".format(self.stderr_tail()))
        self._reader.join(timeout=10)
        stderr = self.stderr_tail(n=10 ** 6)
        self._release()
        return returncode, stderr

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._release()

    def _release(self):
        """パイプ (stdin/stdout の TextIOWrapper) と stderr ログを閉じる (ResourceWarning 防止)。
        close()/kill() のどちらから何度呼ばれても安全。"""
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        if not self._stderr_file.closed:
            self._stderr_file.close()
            try:
                os.unlink(self._stderr_file.name)
            except OSError:
                pass


class HttpServer:
    """`python -m raiya.mcp_server --transport http --port N` を子プロセスで起動し、
    TCP 接続できるまで待つ。`url` は Streamable HTTP のエンドポイント。"""

    def __init__(self, env, port=None, extra_args=None, timeout=30):
        self.port = port or free_port()
        self.url = "http://127.0.0.1:{}/mcp".format(self.port)
        self._log_file = tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", prefix="raiya_mcp_http_", suffix=".log", delete=False
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "raiya.mcp_server", "--transport", "http", "--port", str(self.port)]
            + list(extra_args or []),
            stdin=subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=REPO_ROOT,
        )
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(
                    "HTTP サーバが起動前に終了しました (returncode={}, log: {!r})".format(
                        self.proc.returncode, self.log_tail()
                    )
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    self.close()
                    raise AssertionError("HTTP サーバが {}s 以内に listen しません (log: {!r})".format(timeout, self.log_tail()))
                time.sleep(0.1)

    def log_tail(self, n=2000):
        try:
            with open(self._log_file.name, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except OSError:
            return ""

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        log = self.log_tail(n=10 ** 6)
        self._log_file.close()
        try:
            os.unlink(self._log_file.name)
        except OSError:
            pass
        return log
