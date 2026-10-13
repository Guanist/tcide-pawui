# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""终端会话（纯逻辑）：历史、内建命令、输出规整与转义。

配合 ``tcide/web/terminal.html``（离线前端，走**同一个** QWebChannel 桥工厂）
构成"xterm 风格终端"：原版 TCIDE 用的是 Electron + node-pty + xterm.js；
这里在 PawUI 上跑的是**真实子进程**（经由注入的 runner），前端只负责显示与输入。

刻意把语义都放在这一层（纯逻辑、可离线严格测试），Qt 侧只做两件事：
把 JSON 字符串发出去、把收到的字符串交给 :func:`decode_command`。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

#: 保留多少条历史（上下键回溯用）
HISTORY_LIMIT = 200

#: 单个命令输出最多保留多少字符（超出截断，避免把界面塞爆）
OUTPUT_LIMIT = 20_000

#: 行类型（前端按此着色）
LINE_KINDS: tuple[str, ...] = ("in", "out", "err", "info", "meta")

#: 内建命令（不经过 runner）
BUILTINS: dict[str, str] = {
    "help": "内建：help / clear / history / version —— 其它命令交给工作区执行",
    "version": "tcide-pawui 终端",
}

#: 需要从输出里剥掉的 ANSI 颜色/控制序列（前端是纯文本渲染）
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


@dataclass
class TermLine:
    """一行终端输出。"""

    text: str
    kind: str = "out"

    def to_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind}


def strip_ansi(text: str) -> str:
    """去掉 ANSI 转义序列（命令输出里很常见）。"""
    return ANSI_RE.sub("", text or "")


def escape_html(text: str) -> str:
    """转义成可安全塞进 HTML 的文本（前端是 innerHTML 渲染）。"""
    return html.escape(strip_ansi(text or ""), quote=False)


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """按字符数截断并标注。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（输出被截断，共 {len(text)} 字符）"


def format_prompt(cwd: str = "") -> str:
    """提示符：``工作区> ``（有 cwd 就用 cwd 的末段）。"""
    tail = (cwd or "工作区").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return f"{tail or '工作区'}> "


def encode_output(lines: list[TermLine], *, echo: str = "") -> dict:
    """Python -> 前端：一段输出。"""
    return {"type": "output", "echo": echo,
            "lines": [line.to_dict() for line in lines
                      if line.kind in LINE_KINDS]}


def encode_clear() -> dict:
    return {"type": "clear"}


def encode_prompt(prompt: str) -> dict:
    return {"type": "prompt", "prompt": prompt}


def decode_command(raw) -> str:
    """前端 -> Python：取出要执行的命令行（解析失败返回空串，不抛异常）。"""
    import json

    payload = raw
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return ""
    if not isinstance(payload, dict):
        return ""
    kind = str(payload.get("type") or payload.get("kind") or "")
    if kind not in ("command", "input"):
        return ""
    return str(payload.get("text") or payload.get("command") or "").strip()


class TerminalSession:
    """终端会话：内建命令 + 注入的执行器 + 历史回溯。"""

    def __init__(self, runner=None, *, history_limit: int = HISTORY_LIMIT,
                 output_limit: int = OUTPUT_LIMIT, cwd: str = "") -> None:
        self.runner = runner
        self.history: list[str] = []
        self.history_limit = max(1, int(history_limit))
        self.output_limit = max(100, int(output_limit))
        self.cwd = cwd
        self.lines: list[TermLine] = []
        self._cursor = 0              # 上下键回溯位置（0 = 最后一条之后）

    # -- 执行 -------------------------------------------------------------

    def run(self, command: str) -> list[TermLine]:
        """执行一条命令，返回新增的输出行。

        所有分支的输出都会进 ``self.lines``（屏缓冲）—— 第一版只有 runner 分支
        这么做了，于是 ``help``/``history`` 的内容**凭空消失**，``text()`` 与
        ``to_dict()`` 也看不到它们。现在统一在出口处追加一次。
        """
        text = (command or "").strip()
        if not text:
            return []
        self._remember(text)
        name = text.split()[0].lower()

        if name == "clear":
            self.clear()
            # **特例**：返回值里给一行提示，但不留在屏缓冲里 —— 清屏后模型就是空的，
            # 提示要不要显示由调用方决定（应用侧通常同时发前端的 clear 消息）。
            return [TermLine("（已清屏）", "meta")]
        if name == "history":
            output = self._history_lines()
        elif name in BUILTINS:
            output = [TermLine(f"$ {text}", "in"), TermLine(BUILTINS[name], "info")]
        else:
            output = self._run_external(text)

        self.lines += output
        return output

    def _run_external(self, text: str) -> list[TermLine]:
        """交给注入的执行器跑（没有执行器就如实说明）。"""
        output = [TermLine(f"$ {text}", "in")]
        if self.runner is None:
            output.append(TermLine("（没有配置执行器：这个会话只做演示）", "err"))
            return output
        try:
            result = self.runner(text)
        except Exception as exc:      # noqa: BLE001 - 执行器是外部注入的
            output.append(TermLine(f"执行失败: {type(exc).__name__}: {exc}", "err"))
            return output

        code, stdout, stderr = _unpack(result)
        if stdout:
            output.append(TermLine(truncate(strip_ansi(stdout), self.output_limit),
                                   "out"))
        if stderr:
            output.append(TermLine(truncate(strip_ansi(stderr), self.output_limit),
                                   "err"))
        if not stdout and not stderr:
            output.append(TermLine(f"（退出码 {code}，无输出）", "meta"))
        elif code:
            output.append(TermLine(f"（退出码 {code}）", "meta"))
        return output

    # -- 历史 -------------------------------------------------------------

    def _remember(self, command: str) -> None:
        # 连续重复的命令不重复入历史（和 shell 的习惯一致）
        if self.history and self.history[-1] == command:
            self._cursor = 0
            return
        self.history.append(command)
        if len(self.history) > self.history_limit:
            del self.history[:len(self.history) - self.history_limit]
        self._cursor = 0

    def _history_lines(self) -> list[TermLine]:
        if not self.history:
            return [TermLine("（历史为空）", "meta")]
        return [TermLine(f"  {index + 1:>3}  {item}", "meta")
                for index, item in enumerate(self.history)]

    def previous(self) -> str:
        """上键：往历史里退一条（到顶就停在最早一条）。"""
        if not self.history:
            return ""
        self._cursor = min(self._cursor + 1, len(self.history))
        return self.history[-self._cursor]

    def next(self) -> str:
        """下键：往历史外走一条（到底返回空串，表示新命令）。"""
        if not self.history or self._cursor <= 0:
            self._cursor = 0
            return ""
        self._cursor -= 1
        return self.history[-self._cursor] if self._cursor else ""

    def reset_cursor(self) -> None:
        self._cursor = 0

    # -- 视图 -------------------------------------------------------------

    def clear(self) -> None:
        self.lines = []

    def prompt(self) -> str:
        return format_prompt(self.cwd)

    def text(self, *, limit: int = 400) -> str:
        """把当前屏内容拼成纯文本（测试/日志用）。"""
        return "\n".join(line.text for line in self.lines[-limit:])

    def to_dict(self) -> dict:
        return {"cwd": self.cwd, "history": list(self.history),
                "lines": [line.to_dict() for line in self.lines]}


def _unpack(result) -> tuple[int, str, str]:
    """把执行器的返回值规整成 ``(退出码, stdout, stderr)``。

    允许执行器返回：字符串（当作 stdout）、``(code, out, err)`` 三元组。
    """
    if result is None:
        return 0, "", ""
    if isinstance(result, str):
        return 0, result, ""
    if isinstance(result, tuple):
        if len(result) == 3:
            code, out, err = result
            try:
                return int(code), str(out or ""), str(err or "")
            except (TypeError, ValueError):
                return 0, str(out or ""), str(err or "")
        if len(result) == 2:
            out, err = result
            return 0, str(out or ""), str(err or "")
        if len(result) == 1:
            return 0, str(result[0] or ""), ""
    return 0, str(result), ""
