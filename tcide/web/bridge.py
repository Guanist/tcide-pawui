# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""``<Web>`` 编辑器桥：消息协议（纯逻辑）+ QWebChannel 桥（Qt 惰性导入）。

为什么不用 CDN 上的 Monaco：**本项目必须能离线运行**，而 Monaco 默认从 CDN 取
资源。所以这里走两步：

1. 定义一套 **Monaco 兼容的消息协议**（纯 JSON，``encode_*`` / ``decode_event``）；
2. 自带一个**单文件离线编辑器前端**（``editor.html``：行号、语法着色、diff 标记、
   快捷键），它只依赖 QtWebEngine 自带的 ``qrc:///qtwebchannel/qwebchannel.js``。
   想升级成真 Monaco 时，只要让前端产出同样格式的消息即可。

**分层注意**：Qt 只在 :func:`make_bridge` 内部惰性导入，所以本模块
**不装 PySide6 也能导入**（纯协议函数照常可测）。PySide6 是 LGPL-3.0，
不影响 `tcide/` 的 LGPL 边界。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from ..diff import FilePatch
from ..lint import Issue

#: 前端资源目录
WEB_DIR = pathlib.Path(__file__).resolve().parent

#: 编辑器页面
EDITOR_HTML = WEB_DIR / "editor.html"

#: 终端页面（复用同一个桥工厂：deliver 发消息、post 收消息）
TERMINAL_HTML = WEB_DIR / "terminal.html"

#: vendored 的 Monaco（离线自带；见 tools/vendor_monaco.py）
MONACO_DIR = WEB_DIR / "vendor" / "monaco"

#: Monaco 存在与否决定编辑器用哪个引擎
MONACO_ENTRY = MONACO_DIR / "loader.js"

#: Python -> 前端 的消息类型
OUTBOUND: tuple[str, ...] = ("text", "theme", "markers", "goto", "completion",
                             "readonly", "focus",
                             # 分屏（第二个编辑器）
                             "split", "splitClose",
                             # 终端页用这三个
                             "output", "clear", "prompt")

#: 前端 -> Python 的消息类型
INBOUND: tuple[str, ...] = ("ready", "change", "cursor", "command", "debug",
                            "error", "history", "input")

#: 行标记种类（前端按此着色）
MARKER_KINDS: tuple[str, ...] = ("added", "removed", "changed", "error",
                                 "warning", "info")


@dataclass
class EditorEvent:
    """前端发回来的一条事件（解析失败时 ``kind="unknown"``，不抛异常）。"""

    kind: str = "unknown"
    text: str = ""
    line: int = 0
    column: int = 0
    name: str = ""
    message: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "line": self.line,
                "column": self.column, "name": self.name, "message": self.message}


# ---------------------------------------------------------------------------
# 出站消息（Python -> 前端）
# ---------------------------------------------------------------------------

def encode_text(path: str, text: str, *, language: str = "", readonly: bool = False,
                version: int = 0) -> dict:
    """把文件内容推给前端。"""
    return {"type": "text", "path": path, "text": text, "language": language,
            "readonly": bool(readonly), "version": int(version)}


def encode_theme(colors: dict) -> dict:
    """推主题色（十六进制）。界面始终推十六进制，令牌名会被前端当无效色。"""
    safe = {key: value for key, value in (colors or {}).items()
            if isinstance(value, str) and value.startswith("#")}
    return {"type": "theme", "colors": safe}


def encode_goto(line: int, *, column: int = 0) -> dict:
    return {"type": "goto", "line": max(1, int(line)), "column": max(0, int(column))}


def encode_completion(items: list[str], *, prefix: str = "") -> dict:
    return {"type": "completion", "prefix": prefix,
            "items": [str(item) for item in items]}


def encode_readonly(value: bool) -> dict:
    return {"type": "readonly", "value": bool(value)}


def encode_markers(markers: list[dict]) -> dict:
    return {"type": "markers",
            "markers": [item for item in markers
                        if item.get("kind") in MARKER_KINDS]}


def encode_split(path: str, text: str, *, language: str = "") -> dict:
    """在**分屏**里打开一个文件（第二个编辑器，只读参考）。

    分屏放在 Web 编辑器页面内实现：PawUI 的 ``<SplitPane>`` 是构建期静态的，
    而 Monaco 支持多实例，在页面内做分屏既自然又不受那个限制。
    """
    return {"type": "split", "path": path, "text": text,
            "language": language}


def encode_split_close() -> dict:
    return {"type": "splitClose"}


# ---------------------------------------------------------------------------
# 入站消息（前端 -> Python）
# ---------------------------------------------------------------------------

def decode_event(raw) -> EditorEvent:
    """解析前端发来的一条消息。

    容忍三种输入：JSON 字符串、``dict``、以及无法识别的东西（返回 unknown）。
    **不抛异常** —— 网页侧发来的东西不可信。
    """
    payload = raw
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return EditorEvent(kind="unknown", message=str(raw)[:200])
    if not isinstance(payload, dict):
        return EditorEvent(kind="unknown", message=str(payload)[:200])

    kind = str(payload.get("type") or payload.get("kind") or "")
    if kind not in INBOUND:
        return EditorEvent(kind="unknown", message=str(payload)[:200], raw=payload)

    def as_int(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    return EditorEvent(
        kind=kind,
        text=str(payload.get("text") or ""),
        line=as_int(payload.get("line")),
        column=as_int(payload.get("column")),
        name=str(payload.get("name") or ""),
        message=str(payload.get("message") or ""),
        raw=payload,
    )


# ---------------------------------------------------------------------------
# 把 diff / 诊断转成行标记
# ---------------------------------------------------------------------------

def markers_from_patch(patch: FilePatch) -> list[dict]:
    """把改动块转成前端行标记（gutter 用）。

    语义（对着**新版本**的行号）：

    - ``+`` 行 → ``added``
    - 紧跟着 ``+`` 的 ``-`` 行 → 合并成 ``changed``（改一行不该画两个标记）
    - 单独出现的 ``-`` 行 → 标在它**上面那一行**，kind=``removed``
      （新版本里这一行已经不存在了，只能挂在相邻位置）

    第一版把删除一律挂在"上一行"，于是"改一行"被拆成 added+removed 两个标记；
    这里改成先暂存删除行、遇到 ``+`` 或上下文再决定归属。
    """
    markers: list[dict] = []

    def flush_removed(pending: list[str], new_line: int) -> None:
        for content in pending:
            markers.append({"line": max(1, new_line - 1), "kind": "removed",
                            "text": content})
        pending.clear()

    for hunk in patch.hunks:
        new_line = max(1, hunk.new_start)
        pending: list[str] = []
        for raw in hunk.lines:
            prefix, content = (raw[:1], raw[1:]) if raw else (" ", "")
            if prefix == "-":
                pending.append(content)
            elif prefix == "+":
                if pending:
                    markers.append({"line": new_line, "kind": "changed",
                                    "text": content})
                    pending.clear()
                else:
                    markers.append({"line": new_line, "kind": "added",
                                    "text": content})
                new_line += 1
            else:
                flush_removed(pending, new_line)
                new_line += 1
        flush_removed(pending, new_line)      # hunk 末尾的纯删除

    return sorted(markers, key=lambda marker: (marker["line"], marker["kind"]))


def markers_from_issues(issues) -> list[dict]:
    """把静态检查结果转成行标记（error/warning/info）。"""
    if isinstance(issues, dict):
        flat = [issue for group in issues.values() for issue in group]
    else:
        flat = list(issues or [])
    out: list[dict] = []
    for issue in flat:
        kind = issue.severity if issue.severity in MARKER_KINDS else "info"
        out.append({"line": max(1, int(issue.line or 1)), "kind": kind,
                    "text": f"{issue.rule}: {issue.message}"})
    return out


def fix_position_markers(issues: list[Issue]) -> list[dict]:
    """只给 ``error`` 级别做标记（问题面板用全量，编辑器 gutter 只标错误）。"""
    return [marker for marker in markers_from_issues(issues)
            if marker["kind"] == "error"]


# ---------------------------------------------------------------------------
# 页面装载
# ---------------------------------------------------------------------------

def editor_html() -> str:
    """离线编辑器页面的 HTML 内容。"""
    return EDITOR_HTML.read_text(encoding="utf-8")


def editor_url() -> str:
    """给 ``<Web src=...>`` 用的本地文件 URL（前端资源随程序分发）。"""
    return EDITOR_HTML.resolve().as_uri()


def monaco_available() -> bool:
    """vendored Monaco 是否在场。

    决定编辑器用 Monaco 还是内置后备实现 —— 打包时可以用
    ``TCIDE_NO_MONACO=1`` 排除它（省 ~24 MB），页面会自动退回内置编辑器。
    """
    return MONACO_ENTRY.is_file()


def monaco_size_mb() -> float:
    """vendored Monaco 的体积（MB）；不在场返回 0。"""
    if not MONACO_DIR.is_dir():
        return 0.0
    total = sum(path.stat().st_size for path in MONACO_DIR.rglob("*")
                if path.is_file())
    return round(total / 1024 / 1024, 1)


def engine_description() -> str:
    """给状态栏/CLI 用的一句话。"""
    if monaco_available():
        return f"Monaco（vendored {monaco_size_mb()} MB）"
    return "内置编辑器（未 vendored Monaco）"


def html_is_offline(html: str | None = None) -> bool:
    """检查页面没有依赖外部网络（这是本项目"可离线运行"的硬要求）。

    只允许 ``qrc:///qtwebchannel/qwebchannel.js`` 这类**内置资源**。
    """
    text = editor_html() if html is None else html
    lowered = text.lower()
    for bad in ("http://", "https://", "//cdn.", "unpkg.com", "jsdelivr",
                "cdnjs"):
        if bad in lowered:
            return False
    return True


# ---------------------------------------------------------------------------
# QWebChannel 桥（Qt 惰性导入）
# ---------------------------------------------------------------------------

def make_bridge(on_event=None, parent=None):
    """创建 QWebChannel 桥对象；**只有调用它才会导入 PySide6**。

    ``on_event(EditorEvent)`` 会在前端发消息时被调用。
    返回的对象有 Qt 槽：``pushText`` / ``pushTheme`` / ``pushMarkers`` /
    ``gotoLine`` / ``pushCompletion`` / ``setReadonly``，以及信号 ``deliver``
    （Python -> 前端，参数是一个 JSON 字符串）。
    """
    from PySide6.QtCore import QObject, Signal, Slot       # 惰性导入

    class EditorBridge(QObject):
        #: 发给前端的消息（JSON 字符串，前端 JSON.parse）
        deliver = Signal(str)

        def __init__(self) -> None:
            super().__init__(parent)
            self.received: list[EditorEvent] = []

        # -- Python -> 前端 ------------------------------------------

        def _send(self, message: dict) -> None:
            self.deliver.emit(json.dumps(message, ensure_ascii=False))

        @Slot(str, str, bool)
        def pushText(self, path: str, text: str, readonly: bool = False) -> None:
            self._send(encode_text(path, text, readonly=readonly))

        @Slot("QVariantMap")
        def pushTheme(self, colors) -> None:            # pragma: no cover - Qt 侧
            self._send(encode_theme(dict(colors or {})))

        @Slot("QVariantList")
        def pushMarkers(self, markers) -> None:         # pragma: no cover - Qt 侧
            self._send(encode_markers([dict(item) for item in (markers or [])]))

        @Slot(int)
        def gotoLine(self, line: int) -> None:          # pragma: no cover - Qt 侧
            self._send(encode_goto(line))

        @Slot("QVariantList")
        def pushCompletion(self, items) -> None:        # pragma: no cover - Qt 侧
            self._send(encode_completion([str(item) for item in (items or [])]))

        @Slot(bool)
        def setReadonly(self, value: bool) -> None:     # pragma: no cover - Qt 侧
            self._send(encode_readonly(value))

        # -- 前端 -> Python ------------------------------------------

        @Slot(str)
        def post(self, payload: str) -> None:
            """前端调用的入口（名字与 ``editor.html`` 里的约定一致）。"""
            event = decode_event(payload)
            self.received.append(event)
            if on_event is not None:
                on_event(event)

    return EditorBridge()
