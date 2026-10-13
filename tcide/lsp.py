# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""LSP 客户端（语言服务器协议）。

TCIDE 原版宣称「LSP 支持 TS/Python、自动识别」。这里实现**真的 LSP**：
标准 ``Content-Length`` 分帧的 JSON-RPC over stdio，官方 ``initialize`` 握手、
``textDocument/didOpen``、补全、悬停、``publishDiagnostics`` 通知。

**可跳过设计**：语言服务器是外部程序，本机不一定装。所以

- :func:`detect_servers` 只**检测**，把「装了什么 / 缺什么 / 怎么装」如实报出来；
- :class:`LspClient` 启动失败时返回 ``False`` 并给出原因，**不抛异常**；
- 诊断/补全都退化成"没有"而不是报错 —— 界面照常能用。

纯逻辑部分（分帧、URI 互转、结果转换）都不依赖子进程，可离线严格测试；
客户端本身用一个**假语言服务器**（同样说 stdio JSON-RPC）端到端测试。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

from .lint import Issue

#: LSP 严重度 -> 我们的严重度
SEVERITY_MAP: dict[int, str] = {1: "error", 2: "warning", 3: "info", 4: "info"}

#: 反向映射（发给服务器时用）
SEVERITY_BACK: dict[str, int] = {"error": 1, "warning": 2, "info": 3}

#: 支持的语言 -> 服务器候选（按优先级）
SERVERS: dict[str, tuple[dict, ...]] = {
    "python": (
        {"command": "pyright-langserver", "args": ["--stdio"],
         "hint": "pip install pyright"},
        {"command": "pylsp", "args": [],
         "hint": "pip install python-lsp-server"},
    ),
    "typescript": (
        {"command": "typescript-language-server", "args": ["--stdio"],
         "hint": "npm i -g typescript-language-server typescript"},
    ),
    "javascript": (
        {"command": "typescript-language-server", "args": ["--stdio"],
         "hint": "npm i -g typescript-language-server typescript"},
    ),
    "go": (
        {"command": "gopls", "args": [],
         "hint": "go install golang.org/x/tools/gopls@latest"},
    ),
    "rust": (
        {"command": "rust-analyzer", "args": [],
         "hint": "rustup component add rust-analyzer"},
    ),
    "paw": (),
}

#: 没有已知服务器的语言 -> 说明。
#: 不能只留个空的"缺少"（用户会一头雾水）——界面自检就是这么抓到 paw 的。
NO_SERVER_HINTS: dict[str, str] = {
    "paw": ("PawUI 是声明式框架，没有 LSP 服务器；"
            "用内置的 `python -m pawui check` 做静态校验"),
}

#: 项目文件 -> 语言（"自动识别"用）
MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"), ("setup.py", "python"),
    ("requirements.txt", "python"), ("tsconfig.json", "typescript"),
    ("package.json", "javascript"), ("go.mod", "go"), ("Cargo.toml", "rust"),
    ("app.paw", "paw"),
)

#: 单条消息上限（防止坏服务器把内存吃光）
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


@dataclass
class LspStatus:
    """某个语言的服务器可用性。"""

    language: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    available: bool = False
    hint: str = ""

    def to_dict(self) -> dict:
        return {"language": self.language, "command": self.command,
                "args": list(self.args), "available": self.available,
                "hint": self.hint}

    def describe(self) -> str:
        if self.available:
            return f"{self.language}: {self.command} ✓"
        if not self.command:
            # 这门语言本来就没有服务器 —— 别写成"缺少 （无候选）"那种读不通的话
            return f"{self.language}: 无语言服务器（{self.hint}）"
        return f"{self.language}: 缺少 {self.command}（{self.hint}）"


# ---------------------------------------------------------------------------
# 分帧（纯逻辑）
# ---------------------------------------------------------------------------

def encode_message(payload: dict) -> bytes:
    """按 LSP 的 ``Content-Length`` 分帧编码一条消息。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_message(stream) -> dict | None:
    """从二进制流读一条消息；流结束返回 ``None``。

    只认 ``Content-Length``（忽略 ``Content-Type`` 等其它头）。
    """
    length = 0
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        try:
            text = line.decode("ascii", errors="replace").strip()
        except Exception:      # noqa: BLE001
            continue
        if text.lower().startswith("content-length:"):
            try:
                length = int(text.split(":", 1)[1].strip())
            except ValueError:
                return None
    if length <= 0 or length > MAX_MESSAGE_BYTES:
        return None
    body = stream.read(length)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# URI 与结果转换（纯逻辑）
# ---------------------------------------------------------------------------

def path_to_uri(path: str | pathlib.Path) -> str:
    """本地路径 -> ``file://`` URI（Windows 盘符要写成 ``/C:/...``）。"""
    text = str(path).replace("\\", "/")
    if text.startswith("/"):
        return f"file://{text}"
    return f"file:///{text}"


def uri_to_path(uri: str) -> str:
    """``file://`` URI -> 本地路径。

    要同时照顾两种形态（第一版只顾了 Windows，把 POSIX 的根斜杠吃掉了）：

    - ``file:///C:/work/a.py`` -> ``C:/work/a.py``（Windows 盘符）
    - ``file:///home/u/a.py``  -> ``/home/u/a.py``（POSIX，**根斜杠必须保留**）
    """
    text = uri or ""
    if text.startswith("file://"):
        text = text[len("file://"):]
    text = (text.replace("%20", " ").replace("%3A", ":")
            .replace("%3a", ":").replace("%2F", "/"))
    if not text:
        return ""
    # Windows：/C:/... -> C:/...
    if len(text) >= 3 and text[0] == "/" and text[1].isalpha() and text[2] == ":":
        return text[1:]
    return text


def completion_labels(result) -> list[str]:
    """``textDocument/completion`` 的返回 -> 标签列表。

    兼容三种形态：``CompletionList``、``CompletionItem[]``、``None``。
    """
    if not result:
        return []
    if isinstance(result, dict):
        items = result.get("items") or []
    else:
        items = result
    labels: list[str] = []
    for item in items:
        if isinstance(item, dict):
            label = item.get("label")
            if isinstance(label, str) and label:
                labels.append(label)
        elif isinstance(item, str):
            labels.append(item)
    return labels


def diagnostics_to_issues(uri: str, diagnostics, *, root: str | pathlib.Path = ""
                         ) -> list[Issue]:
    """``publishDiagnostics`` -> :class:`tcide.lint.Issue` 列表。

    这样 LSP 诊断能直接进"问题"面板，和本地静态检查同一种数据结构。
    """
    path = uri_to_path(uri or "")
    if root:
        try:
            path = str(pathlib.Path(path).relative_to(pathlib.Path(root)))
        except ValueError:
            pass
    path = path.replace("\\", "/")
    issues: list[Issue] = []
    for item in diagnostics or []:
        if not isinstance(item, dict):
            continue
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        code = item.get("code")
        issues.append(Issue(
            path=path,
            line=int(start.get("line", 0)) + 1,          # LSP 是 0 基
            column=int(start.get("character", 0)) + 1,
            rule=f"lsp:{item.get('source') or 'unknown'}"
                 + (f":{code}" if code not in (None, "") else ""),
            message=str(item.get("message") or "").strip(),
            severity=SEVERITY_MAP.get(int(item.get("severity") or 1), "error"),
        ))
    return issues


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------

def project_language(workspace, memory=None) -> str:
    """自动识别项目主语言：先看记忆，再看标记文件，最后看文件后缀统计。"""
    if memory is not None and getattr(memory, "primary_language", ""):
        return memory.primary_language
    if workspace is not None:
        for name, language in MARKERS:
            if workspace.exists(name):
                return language
    return ""


def detect_servers(languages=None, *, which=shutil.which) -> list[LspStatus]:
    """检测每个语言的服务器是否可用（``which`` 可注入，便于测试）。"""
    targets = list(languages) if languages else list(SERVERS)
    statuses: list[LspStatus] = []
    for language in targets:
        candidates = SERVERS.get(language, ())
        status = LspStatus(language=language)
        if not candidates:
            # 这门语言本来就没有服务器 —— 也要说清楚，而不是留空
            status.hint = NO_SERVER_HINTS.get(language, "这门语言没有已知的语言服务器")
            statuses.append(status)
            continue
        for candidate in candidates:
            command = candidate["command"]
            if which(command):
                status.command = command
                status.args = list(candidate.get("args") or [])
                status.available = True
                status.hint = ""
                break
            if not status.command:
                status.command = command
                status.hint = candidate.get("hint", "")
        statuses.append(status)
    return statuses


def available_servers(languages=None, *, which=shutil.which) -> list[LspStatus]:
    return [status for status in detect_servers(languages, which=which)
            if status.available]


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class LspClient:
    """一个语言服务器的会话（stdio JSON-RPC）。

    线程模型：起一个读线程把响应按 ``id`` 派发、把通知收进 :attr:`notifications`。
    所有对外方法都在**超时后放弃等待**并返回空结果，不阻塞界面。
    """

    def __init__(self, status: LspStatus, root: str | pathlib.Path, *,
                 timeout: float = 10.0) -> None:
        self.status = status
        self.root = pathlib.Path(root)
        self.timeout = float(timeout)
        self.process: subprocess.Popen | None = None
        self.error = ""
        self.notifications: list[dict] = []
        self.diagnostics: dict[str, list[dict]] = {}
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._opened: dict[str, int] = {}

    # -- 生命周期 ---------------------------------------------------------

    def start(self, *, popen=None) -> bool:
        """启动服务器并完成 ``initialize``；失败返回 False（不抛异常）。"""
        if self.process is not None:
            return True
        if not self.status.available or not self.status.command:
            self.error = f"没有可用的 {self.status.language} 语言服务器"
            return False
        launcher = popen or subprocess.Popen
        try:
            self.process = launcher(                     # type: ignore[assignment]
                [self.status.command, *self.status.args], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=str(self.root))
        except Exception as exc:      # noqa: BLE001 - 外部程序
            self.error = f"启动失败: {type(exc).__name__}: {exc}"
            return False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        result = self.request("initialize", {
            "processId": None,
            "rootUri": path_to_uri(self.root),
            "capabilities": {"textDocument": {
                "completion": {"completionItem": {"snippetSupport": False}},
                "hover": {}, "publishDiagnostics": {}}},
        })
        if result is None:
            self.error = self.error or "initialize 没有响应"
            self.stop()
            return False
        self.notify("initialized", {})
        return True

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:      # noqa: BLE001
            try:
                process.kill()
            except Exception:      # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # -- 收发 -------------------------------------------------------------

    def _write(self, payload: dict) -> bool:
        if self.process is None or self.process.stdin is None:
            return False
        try:
            self.process.stdin.write(encode_message(payload))
            self.process.stdin.flush()
            return True
        except (OSError, ValueError) as exc:
            self.error = f"写入失败: {exc}"
            return False

    def notify(self, method: str, params: dict) -> bool:
        return self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict):
        """发一个请求并等响应；超时或出错返回 ``None``。"""
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            slot: dict = {}
            self._pending[request_id] = slot
        if not self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                            "params": params}):
            with self._lock:
                self._pending.pop(request_id, None)
            return None
        event = threading.Event()
        slot["event"] = event
        if not event.wait(self.timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            self.error = self.error or f"{method} 超时（{self.timeout}s）"
            return None
        with self._lock:
            self._pending.pop(request_id, None)
        return slot.get("result")

    def _read_loop(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:
            return
        while True:
            message = read_message(stream)
            if message is None:
                return
            if "id" in message and ("result" in message or "error" in message):
                with self._lock:
                    slot = self._pending.get(int(message["id"]))
                if slot is not None:
                    slot["result"] = message.get("result")
                    event = slot.get("event")
                    if isinstance(event, threading.Event):
                        event.set()
                continue
            self.notifications.append(message)
            if message.get("method") == "textDocument/publishDiagnostics":
                params = message.get("params") or {}
                self.diagnostics[str(params.get("uri") or "")] = list(
                    params.get("diagnostics") or [])

    # -- 常用请求 ---------------------------------------------------------

    def open_file(self, path: str, text: str, *, language: str = "") -> bool:
        uri = path_to_uri(path)
        self._opened[uri] = self._opened.get(uri, 0) + 1
        return self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language or self.status.language,
            "version": self._opened[uri], "text": text}})

    def change_file(self, path: str, text: str) -> bool:
        uri = path_to_uri(path)
        self._opened[uri] = self._opened.get(uri, 0) + 1
        return self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": self._opened[uri]},
            "contentChanges": [{"text": text}]})

    def completion(self, path: str, line: int, character: int) -> list[str]:
        result = self.request("textDocument/completion", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": max(0, line), "character": max(0, character)}})
        return completion_labels(result)

    def hover(self, path: str, line: int, character: int) -> str:
        result = self.request("textDocument/hover", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": max(0, line), "character": max(0, character)}})
        if not isinstance(result, dict):
            return ""
        contents = result.get("contents")
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return str(contents.get("value") or "")
        if isinstance(contents, list):
            parts = []
            for item in contents:
                parts.append(item if isinstance(item, str)
                             else str((item or {}).get("value") or ""))
            return "\n".join(part for part in parts if part)
        return ""

    def issues(self, *, root: str | pathlib.Path = "") -> list[Issue]:
        """把已收到的诊断转成 Issue 列表。"""
        out: list[Issue] = []
        for uri, diagnostics in self.diagnostics.items():
            out += diagnostics_to_issues(uri, diagnostics,
                                         root=root or self.root)
        return out

    def to_dict(self) -> dict:
        return {"language": self.status.language, "command": self.status.command,
                "running": self.running, "error": self.error,
                "diagnostics": len(self.diagnostics),
                "notifications": len(self.notifications)}


def status_report(languages=None, *, which=shutil.which) -> str:
    """给 CLI / 界面用的一行行状态说明。"""
    statuses = detect_servers(languages, which=which)
    if not statuses:
        return "没有可检测的语言"
    lines = []
    for status in statuses:
        lines.append(("✓ " if status.available else "✗ ") + status.describe())
    ready = sum(1 for status in statuses if status.available)
    lines.append(f"可用 {ready} / {len(statuses)}")
    return "\n".join(lines)
