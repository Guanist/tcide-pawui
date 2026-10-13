# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""界面面板的行数据（大纲 / 搜索 / 问题 / 补全）。

做这一层的原因：PawUI 的 ``<List items="{$rows}">`` 只接受**字符串列表**，
选中回调拿到的也是那个字符串。于是「选中第 12 行」这种动作必须能从显示文本
**反解**出真实位置 —— 以前这段解析散落在各个处理器里、也没有测试。

这里把「生成行 + 反解行」成对放在一起，并加测试，避免两边格式漂移：

- :func:`outline_rows` / :func:`search_rows` / :func:`problem_rows` 生成行
- :func:`parse_leading_line` 反解行首行号
- :func:`line_offset` 由行号算字符偏移（编辑器跳转用）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lint import Issue

#: 行首行号的形态：``12: 内容`` / ``12 | 内容`` / ``12  内容``
import re

LEADING_LINE_RE = re.compile(r"^\s*(?P<line>\d+)\s*[:|）)\]]?\s?(?P<rest>.*)$")

#: 显示时行号占位宽度（便于对齐，且反解不受影响）
LINE_WIDTH = 5


@dataclass
class PanelRow:
    """一行面板数据：显示文本 + 反解所需的位置信息。"""

    line: int
    text: str
    path: str = ""
    symbol: str = ""
    kind: str = ""
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def display(self) -> str:
        """给 ``<List>`` 用的显示文本（行号在最前面，便于反解）。"""
        marker = {"symbol": "ƒ"}.get(self.kind, " ")
        return f"{self.line:>{LINE_WIDTH}}: {marker} {self.text}"

    def to_dict(self) -> dict:
        return {"line": self.line, "text": self.text, "path": self.path,
                "symbol": self.symbol, "kind": self.kind, "detail": self.detail}


def parse_leading_line(label: str) -> int:
    """从面板行文本反解出行号；解不出来返回 0（调用方据此忽略这次选择）。"""
    match = LEADING_LINE_RE.match(label or "")
    if not match:
        return 0
    try:
        return int(match.group("line"))
    except ValueError:      # pragma: no cover - 正则已保证是数字
        return 0


def line_offset(text: str, line: int) -> int:
    """第 ``line`` 行（1 基）首字符在 ``text`` 里的偏移。

    越界会被夹到合法范围，方便直接喂给编辑器的光标 API。
    """
    if line <= 1:
        return 0
    offset = 0
    for index, chunk in enumerate((text or "").splitlines(keepends=True), start=1):
        if index >= line:
            break
        offset += len(chunk)
    return min(offset, len(text or ""))


# ---------------------------------------------------------------------------
# 三种面板的行
# ---------------------------------------------------------------------------

def outline_rows(index, path: str, *, limit: int = 300) -> list[PanelRow]:
    """当前文件的符号大纲。"""
    rows: list[PanelRow] = []
    for symbol in index.outline(path):
        detail = symbol.detail or symbol.name
        suffix = "" if symbol.precise else "  (正则推断)"
        rows.append(PanelRow(line=symbol.line, text=f"{detail}{suffix}",
                             path=path, symbol=symbol.name, kind=symbol.kind,
                             detail=symbol.detail))
        if len(rows) >= limit:
            break
    return rows


def search_rows(workspace, query: str, *, regex: bool = False,
                case_sensitive: bool = False, limit: int = 300) -> list[PanelRow]:
    """跨文件搜索结果。"""
    if not (query or "").strip():
        return []
    hits = workspace.search(query, regex=regex, case_sensitive=case_sensitive,
                            max_results=limit)
    return [PanelRow(line=hit.line, text=f"{hit.path}  {hit.text.strip()[:90]}",
                     path=hit.path, detail=hit.text)
            for hit in hits]


def _flatten_issues(issues) -> list[Issue]:
    """把 ``list[Issue]`` 或 ``dict[path, list[Issue]]`` 统一成一维列表。

    ``Linter.lint_all()`` 返回的是 **dict**，直接切片会 ``KeyError: slice(...)``
    —— 我在 ``Linter.summary``/``report`` 里处理了这种双形态，这里也要一致。
    """
    if isinstance(issues, dict):
        return [issue for group in issues.values() for issue in group]
    return list(issues or [])


def problem_rows(issues, *, limit: int = 200) -> list[PanelRow]:
    """静态检查结果（按严重度排好）。接受一维列表或 ``{path: [...]}``。"""
    rows: list[PanelRow] = []
    for issue in _flatten_issues(issues)[:limit]:
        badge = {"error": "✖", "warning": "▲", "info": "•"}.get(issue.severity, "•")
        rows.append(PanelRow(line=issue.line, text=f"{badge} {issue.path}  {issue.message}",
                             path=issue.path, detail=issue.rule,
                             extra={"severity": issue.severity, "rule": issue.rule}))
    return rows


def completion_text(completer, text_before_cursor: str, path: str,
                    limit: int = 8) -> str:
    """补全提示（一行文本，界面直接显示）。"""
    items = completer.complete(text_before_cursor, path=path, limit=limit)
    if not items:
        return ""
    return "建议：" + "、".join(item.text for item in items)


def rows_for_list(rows: list[PanelRow]) -> list[str]:
    """转成 ``<List items=...>`` 需要的字符串列表。"""
    return [row.display() for row in rows]


def row_for_label(rows: list[PanelRow], label: str) -> PanelRow | None:
    """按显示文本找回原始行（处理器里用）。"""
    for row in rows:
        if row.display() == label:
            return row
    return None


# ---------------------------------------------------------------------------
# 文件预览（图片 / 文本 / 二进制 hex）
# ---------------------------------------------------------------------------

#: 可以按图片预览的扩展名（还要过魔数嗅探那一关，见 :func:`preview_kind`）
IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg"})

#: 文本预览默认最多显示多少行（避免把 10 万行文件塞进界面）
PREVIEW_LINES = 200

#: 二进制预览默认显示多少行 hex
PREVIEW_HEX_ROWS = 64


def preview_kind(workspace, path: str, *, sniff=None) -> str:
    """判断该用什么方式预览：``image`` / ``text`` / ``hex`` / ``empty`` / ``none``。

    **图片必须同时过扩展名与魔数两关**：PawUI 的 ``<Image src=...>`` 在 src 非空
    但加载失败时会抛 ``RenderError``（会让整个界面建树失败）。所以宁可退回 hex，
    也不能把一个"名字叫 .png 其实是文本"的文件喂给 QPixmap。
    """
    if not path or workspace is None or not workspace.exists(path):
        return "none"
    try:
        size = workspace.resolve(path).stat().st_size
    except OSError:
        return "none"
    if size == 0:
        return "empty"

    extension = "." + path.lower().rsplit(".", 1)[-1] if "." in path else ""
    head = workspace.read_bytes(path, max_bytes=1024)

    if extension in IMAGE_EXTS:
        if extension == ".svg":
            # SVG 是文本格式的矢量图，魔数嗅探不认识它
            return "image" if (b"<svg" in head or b"<?xml" in head) else "text"
        judge = sniff or _sniff_default
        label = judge(head)
        if "图片" in label or "RIFF" in label:
            return "image"
        return "hex"           # 名字像图片但不是图片 -> 老实按二进制展示
    if b"\x00" in head:
        return "hex"
    return "text"


def _sniff_default(head: bytes) -> str:
    from .hexview import HexViewer

    return HexViewer.sniff(head)


def preview_text(workspace, path: str, *, max_lines: int = PREVIEW_LINES) -> str:
    """文本预览内容（超长会截断并标注）。"""
    if workspace is None or not workspace.exists(path):
        return ""
    try:
        text = workspace.read(path)
    except Exception:      # noqa: BLE001
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = "\n".join(lines[:max_lines])
    return f"{head}\n\n…（还有 {len(lines) - max_lines} 行，已截断）"


def preview_hex(workspace, path: str, *, rows: int = PREVIEW_HEX_ROWS) -> str:
    """二进制预览内容（hex dump）。"""
    from .hexview import HexViewer

    if workspace is None or not workspace.exists(path):
        return ""
    try:
        return HexViewer(workspace).render(path, rows=rows)
    except Exception as exc:      # noqa: BLE001
        return f"（无法读取: {type(exc).__name__}: {exc}）"


def preview_image_path(workspace, path: str, *, sniff=None) -> str:
    """可以安全喂给 ``<Image src=...>`` 的**绝对**路径；不安全时返回空串。

    返回绝对路径是必须的：``QPixmap`` 按**进程工作目录**解析相对路径，
    而应用的项目根目录未必等于工作目录。
    """
    if preview_kind(workspace, path, sniff=sniff) != "image":
        return ""
    try:
        return str(workspace.resolve(path))
    except Exception:      # noqa: BLE001
        return ""


def preview_caption(workspace, path: str, *, kind: str = "") -> str:
    """预览标题行：路径 + 大小 + 预览方式。"""
    if not path or workspace is None or not workspace.exists(path):
        return "预览：在左侧选择一个文件"
    kind = kind or preview_kind(workspace, path)
    labels = {"image": "图片", "text": "文本", "hex": "二进制（hex）",
              "empty": "空文件", "none": "不可预览"}
    try:
        size = workspace.resolve(path).stat().st_size
    except OSError:
        size = 0
    human = f"{size} 字节" if size < 1024 else f"{size / 1024:.1f} KB"
    return f"{path} · {human} · {labels.get(kind, kind)}"
