# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""调试辅助：启动配置 + 断点 + 命令构造。

对应 TCIDE 原版的 `core/debug/debug-manager.ts` —— 该模块在原版**从未接线，静默失效**。

**诚实标注**：这里做的是「配置管理 + 断点记录 + 生成启动命令」，
**不是** DAP 调试客户端（那需要实现完整的 Debug Adapter Protocol 并与界面双向通信，
是独立的大工程）。界面上会如实写成「启动调试（外部终端）」。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

#: 支持的调试器类型
DEBUG_TYPES: tuple[str, ...] = ("python", "node", "go", "rust", "custom")

#: 各类型默认的调试入口命令（外部终端里跑）
COMMANDS: dict[str, list[str]] = {
    "python": ["python", "-m", "pdb"],
    "node": ["node", "--inspect-brk"],
    "go": ["dlv", "debug", "--"],
    "rust": ["rust-gdb", "--args"],
    "custom": [],
}

#: 默认监听端口（node/dlv 之类）
DEFAULT_PORTS: dict[str, int] = {"node": 9229, "go": 2345, "rust": 1234}

#: 断点文件里一行的形态：``path:line`` 或 ``path line``
BREAKPOINT_RE = re.compile(r"^\s*(?P<path>[^\s:]+)(?::|\s+)(?P<line>\d+)\s*$")


@dataclass
class LaunchConfig:
    """一个启动配置（对齐 VS Code 的 launch.json 结构）。"""

    name: str
    type: str = "python"
    request: str = "launch"
    program: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    stop_on_entry: bool = False
    port: int = 0
    console: str = "integratedTerminal"

    def __post_init__(self) -> None:
        if self.type not in DEBUG_TYPES:
            raise ValueError(f"未知调试类型 {self.type!r}（可用: {list(DEBUG_TYPES)}）")
        if not self.name.strip():
            raise ValueError("启动配置必须有名字")
        if not self.port:
            self.port = DEFAULT_PORTS.get(self.type, 0)

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "request": self.request,
                "program": self.program, "args": list(self.args), "cwd": self.cwd,
                "env": dict(self.env), "stopOnEntry": self.stop_on_entry,
                "port": self.port, "console": self.console}

    @classmethod
    def from_dict(cls, data: dict) -> "LaunchConfig":
        return cls(
            name=str(data.get("name") or "未命名"),
            type=str(data.get("type") or "python"),
            request=str(data.get("request") or "launch"),
            program=str(data.get("program") or ""),
            args=[str(a) for a in (data.get("args") or [])],
            cwd=str(data.get("cwd") or ""),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            stop_on_entry=bool(data.get("stopOnEntry", False)),
            port=int(data.get("port") or 0),
            console=str(data.get("console") or "integratedTerminal"),
        )


class DebugConfigStore:
    """启动配置的增删查改（JSON 落盘，路径固定为 ``.tcide/launch.json``）。"""

    FILE_NAME = ".tcide/launch.json"

    def __init__(self, workspace=None, configs: list[LaunchConfig] | None = None) -> None:
        self.workspace = workspace
        self.configs: list[LaunchConfig] = list(configs or [])

    # -- 增删查 -----------------------------------------------------------

    def add(self, config: LaunchConfig) -> LaunchConfig:
        self.configs = [c for c in self.configs if c.name != config.name]
        self.configs.append(config)
        return config

    def remove(self, name: str) -> bool:
        before = len(self.configs)
        self.configs = [c for c in self.configs if c.name != name]
        return len(self.configs) != before

    def get(self, name: str) -> LaunchConfig | None:
        return next((c for c in self.configs if c.name == name), None)

    def names(self) -> list[str]:
        return [c.name for c in self.configs]

    # -- 持久化 -----------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({"version": "0.2.0",
                           "configurations": [c.to_dict() for c in self.configs]},
                          ensure_ascii=False, indent=2)

    def load_json(self, text: str) -> list[LaunchConfig]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        raw = data.get("configurations") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        loaded: list[LaunchConfig] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                loaded.append(LaunchConfig.from_dict(item))
            except ValueError:
                continue
        self.configs = loaded
        return loaded

    def save(self) -> str:
        if self.workspace is None:
            raise ValueError("没有 workspace，无法保存")
        self.workspace.write(self.FILE_NAME, self.to_json() + "\n")
        return self.FILE_NAME

    def load(self) -> list[LaunchConfig]:
        if self.workspace is None or not self.workspace.exists(self.FILE_NAME):
            return self.configs
        return self.load_json(self.workspace.read(self.FILE_NAME))

    # -- 默认配置 ---------------------------------------------------------

    @staticmethod
    def defaults_for(memory) -> LaunchConfig:
        """按项目记忆推一个可用的默认启动配置。

        入口优先取**文件**：pyproject 的 console-script 规格（``app:main``）不是
        可直接运行的路径，拿它当 ``program`` 会让调试命令跑不起来。
        """
        entries = list(memory.entry_points or [""])
        source_exts = (".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".rs")
        entry = next((item for item in entries if item.endswith(source_exts)),
                     entries[0])
        language = memory.primary_language or "python"
        kind = {"python": "python", "typescript": "node", "javascript": "node",
                "go": "go", "rust": "rust"}.get(language, "custom")
        return LaunchConfig(name=f"调试 {entry or language}", type=kind,
                            program=entry, cwd=".")


@dataclass
class Breakpoint:
    """一个断点。"""

    path: str
    line: int
    enabled: bool = True
    condition: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "enabled": self.enabled,
                "condition": self.condition}

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"

    @staticmethod
    def parse(text: str) -> "Breakpoint | None":
        match = BREAKPOINT_RE.match(text or "")
        if not match:
            return None
        return Breakpoint(path=match.group("path"), line=int(match.group("line")))


class DebugSession:
    """断点集合 + 命令构造（**不含** DAP 客户端）。"""

    def __init__(self) -> None:
        self.breakpoints: dict[str, dict[int, Breakpoint]] = {}
        self.disabled: set[str] = set()

    # -- 断点 -------------------------------------------------------------

    def toggle(self, path: str, line: int) -> bool:
        """切换断点；返回切换后**是否处于启用**状态。"""
        bucket = self.breakpoints.setdefault(path, {})
        if line in bucket:
            bucket.pop(line)
            if not bucket:
                self.breakpoints.pop(path, None)
            return False
        bucket[line] = Breakpoint(path=path, line=line)
        return True

    def add(self, path: str, line: int, *, condition: str = "") -> Breakpoint:
        point = Breakpoint(path=path, line=line, condition=condition)
        self.breakpoints.setdefault(path, {})[line] = point
        return point

    def remove(self, path: str, line: int) -> bool:
        bucket = self.breakpoints.get(path)
        if not bucket or line not in bucket:
            return False
        bucket.pop(line)
        if not bucket:
            self.breakpoints.pop(path, None)
        return True

    def clear(self, path: str | None = None) -> int:
        if path is None:
            count = self.count()
            self.breakpoints.clear()
            return count
        bucket = self.breakpoints.pop(path, {})
        return len(bucket)

    def all(self) -> list[Breakpoint]:
        return [point for bucket in self.breakpoints.values()
                for point in sorted(bucket.values(), key=lambda p: p.line)]

    def for_file(self, path: str) -> list[Breakpoint]:
        return sorted(self.breakpoints.get(path, {}).values(), key=lambda p: p.line)

    def count(self) -> int:
        return sum(len(bucket) for bucket in self.breakpoints.values())

    def lines_for(self, path: str) -> list[int]:
        """某文件上所有断点行号（界面画标记用）。"""
        return sorted(self.breakpoints.get(path, {}))

    # -- 命令 -------------------------------------------------------------

    def command_for(self, config: LaunchConfig, *, python_module: str = "") -> list[str]:
        """生成在**外部终端**里跑的调试命令。"""
        base = list(COMMANDS.get(config.type, []))
        if not base:
            raise ValueError(f"{config.type} 没有默认命令，请在配置里自己指定")
        program = config.program
        if config.type == "python":
            if python_module:
                return [*base, "-m", python_module, *config.args]
            return [*base, program, *config.args] if program else [*base]
        if config.type == "node":
            if config.port:
                return [base[0], f"--inspect-brk={config.port}", program,
                        *config.args]
            return [*base, program, *config.args]
        if config.type == "go":
            return [*base, *config.args]
        return [*base, program, *config.args]

    @staticmethod
    def command_string(command: list[str]) -> str:
        """可直接粘进终端的字符串。

        用**双引号**而不是 ``shlex.quote`` 的单引号：这个命令主要是给 Windows
        上的终端（cmd/PowerShell）粘贴用的，单引号在那里不是引号。
        """
        parts: list[str] = []
        for part in command:
            if " " in part or "\t" in part:
                parts.append('"' + part.replace('"', '\\"') + '"')
            else:
                parts.append(part)
        return " ".join(parts)

    # -- 汇总 -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"count": self.count(),
                "files": sorted(self.breakpoints),
                "breakpoints": [p.to_dict() for p in self.all()]}

    def report(self) -> str:
        points = self.all()
        if not points:
            return "没有断点"
        lines = [f"共 {len(points)} 个断点：", ""]
        current = ""
        for point in points:
            if point.path != current:
                current = point.path
                lines.append(f"**{current}**")
            suffix = f"（条件：{point.condition}）" if point.condition else ""
            lines.append(f"- 第 {point.line} 行{suffix}")
        return "\n".join(lines)


def load_breakpoints(text: str) -> list[Breakpoint]:
    """从文本里解析断点（每行 ``path:line``），用于导入。"""
    out: list[Breakpoint] = []
    for line in (text or "").splitlines():
        point = Breakpoint.parse(line)
        if point is not None:
            out.append(point)
    return out


def config_path(workspace) -> Path:
    """启动配置的绝对路径。"""
    return workspace.resolve(DebugConfigStore.FILE_NAME)
