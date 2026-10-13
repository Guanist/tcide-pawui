# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""工具集：给 Agent 用的、可直接操作项目的函数。

对应 TCIDE 原版 `main/mcp-tools.ts` 宣称的 10 个工具
（read_file / write_file / list_files / search_code / run_command /
get_diagnostics / get_open_files / git_status / git_diff），
这里做成 12 个（多出 `edit_file` 与 `get_outline`，都是 Agent 真正需要的）。

安全约束（AI 生成参数，必须挡住越界与破坏性操作）：
- 所有路径过 ``Workspace.resolve``，越界直接失败；
- ``run_command`` 有**危险模式黑名单**、超时上限、工作目录限制、输出截断；
- ``write_file`` / ``edit_file`` 走原子写；
- 每次调用都记入 ``calls``，便于断言"工具真的被接线了"（原版教训）。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .gitops import GitError, GitRepo
from .workspace import BINARY_EXTS, Workspace, WorkspaceError

#: 单次工具调用的默认输出上限（字符）
DEFAULT_MAX_OUTPUT = 20000

#: 命令默认/最大超时（秒）
DEFAULT_COMMAND_TIMEOUT = 60
MAX_COMMAND_TIMEOUT = 600

#: 危险命令模式：命中即拒绝（除非显式开启 allow_dangerous）
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"rm\s+(-[a-z]*\s+)*(/|~|\$HOME)\s*$", "递归删除根/家目录"),
    (r"rm\s+-[a-z]*r[a-z]*f?\s+\.\s*$", "递归删除当前目录"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "关机/重启"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;:", "fork 炸弹"),
    (r">\s*/dev/(sd|hd|nvme)", "直接写裸设备"),
    (r"\bdel\s+/[fsq]\b.*[a-z]:\\?\s*$", "递归删除整盘"),
    (r"\bgit\s+push\s+.*--force(?!-with-lease)", "强推（可能覆盖远端历史）"),
    (r"\bgit\s+reset\s+--hard\b", "硬重置（会丢弃改动）"),
    (r"\bgit\s+clean\s+-[a-z]*f", "清理未跟踪文件"),
)


class ToolError(Exception):
    """工具调用失败（参数非法、越界、被策略拒绝）。"""


@dataclass
class ToolResult:
    """一次工具调用的结果。"""

    ok: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output, "error": self.error,
                "duration_ms": self.duration_ms, "truncated": self.truncated,
                "meta": self.meta}

    def as_text(self) -> str:
        """给模型看的文本形式。"""
        return self.output if self.ok else f"ERROR: {self.error}"


@dataclass
class ToolSpec:
    """一个工具的定义。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    dangerous: bool = False

    def to_openai(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def to_anthropic(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.parameters}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """输出过长时保留头尾（错误信息往往在尾部）。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    return (text[:head] + f"\n…  （输出共 {len(text)} 字符，已截断）…\n"
            + text[-tail:]), True


class ToolRegistry:
    """注册/发现/调用工具。"""

    def __init__(
        self,
        workspace: Workspace,
        *,
        index=None,
        repo: GitRepo | None = None,
        open_files: Callable[[], list[str]] | None = None,
        max_output: int = DEFAULT_MAX_OUTPUT,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        allow_dangerous: bool = False,
    ) -> None:
        self.workspace = workspace
        self.index = index
        self.repo = repo if repo is not None else GitRepo(workspace.root)
        self.open_files = open_files
        self.max_output = int(max_output)
        self.command_timeout = min(int(command_timeout), MAX_COMMAND_TIMEOUT)
        self.allow_dangerous = bool(allow_dangerous)
        self.tools: dict[str, ToolSpec] = {}
        self.calls: list[dict[str, Any]] = []
        self._register_defaults()

    # -- 注册与发现 -------------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self.tools)

    def specs(self) -> list[dict]:
        """OpenAI function-calling 格式。"""
        return [self.tools[name].to_openai() for name in self.names()]

    def anthropic_specs(self) -> list[dict]:
        return [self.tools[name].to_anthropic() for name in self.names()]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """调用一个工具；任何异常都转成 ``ok=False`` 的结果，不向调用方抛。"""
        arguments = dict(arguments or {})
        started = time.perf_counter()
        record: dict[str, Any] = {"name": name, "arguments": arguments}
        spec = self.tools.get(name)
        if spec is None:
            record["ok"] = False
            self.calls.append(record)
            return ToolResult(ok=False, error=f"未知工具: {name}（可用: {self.names()}）")
        try:
            result = spec.handler(**arguments)
        except TypeError as exc:
            result = ToolResult(ok=False, error=f"参数不匹配: {exc}")
        except (ToolError, WorkspaceError) as exc:
            result = ToolResult(ok=False, error=str(exc))
        except GitError as exc:
            result = ToolResult(ok=False, error=f"git 失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if isinstance(result, str):
            result = ToolResult(ok=True, output=result)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        output, truncated = truncate_output(result.output, self.max_output)
        result.output = output
        result.truncated = result.truncated or truncated
        record.update({"ok": result.ok, "duration_ms": result.duration_ms,
                       "truncated": result.truncated})
        self.calls.append(record)
        return result

    # -- 默认工具 ---------------------------------------------------------

    def _register_defaults(self) -> None:
        self.register(ToolSpec(
            name="read_file",
            description="读取工作区内一个文本文件的内容（可指定行范围）。",
            parameters=_schema({
                "path": {"type": "string", "description": "相对工作区的路径"},
                "start_line": {"type": "integer", "description": "起始行（1 起，默认 1）"},
                "end_line": {"type": "integer", "description": "结束行（含；0 表示到文件末尾）"},
            }, ["path"]),
            handler=self.read_file,
        ))
        self.register(ToolSpec(
            name="write_file",
            description="写入（覆盖）工作区内的文本文件，必要时创建父目录。",
            parameters=_schema({
                "path": {"type": "string"},
                "content": {"type": "string"},
            }, ["path", "content"]),
            handler=self.write_file,
        ))
        self.register(ToolSpec(
            name="edit_file",
            description="在文件里把 search 精确替换为 replace（默认只替换第一处）。",
            parameters=_schema({
                "path": {"type": "string"},
                "search": {"type": "string", "description": "要查找的原文（需精确匹配）"},
                "replace": {"type": "string"},
                "count": {"type": "integer", "description": "替换次数，0 表示全部"},
            }, ["path", "search", "replace"]),
            handler=self.edit_file,
        ))
        self.register(ToolSpec(
            name="list_files",
            description="列出工作区内的文件与目录（可控制深度）。",
            parameters=_schema({
                "path": {"type": "string", "description": "相对目录，默认根目录"},
                "depth": {"type": "integer", "description": "递归深度，默认 2"},
            }),
            handler=self.list_files,
        ))
        self.register(ToolSpec(
            name="search_code",
            description="跨文件搜索代码（默认子串，可选正则）。",
            parameters=_schema({
                "query": {"type": "string"},
                "regex": {"type": "boolean"},
                "include": {"type": "string", "description": "文件名 glob，如 *.py"},
                "max_results": {"type": "integer"},
            }, ["query"]),
            handler=self.search_code,
        ))
        self.register(ToolSpec(
            name="run_command",
            description="在工作区内执行命令并返回输出与退出码（有超时与危险命令拦截）。",
            parameters=_schema({
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "工作目录（相对工作区）"},
                "timeout": {"type": "integer", "description": "秒，默认 60"},
            }, ["command"]),
            handler=self.run_command,
            dangerous=True,
        ))
        self.register(ToolSpec(
            name="get_diagnostics",
            description="对文件做静态检查：Python 语法错误、JSON/TOML 解析错误。",
            parameters=_schema({
                "path": {"type": "string", "description": "留空表示检查全部可解析文件"},
            }),
            handler=self.get_diagnostics,
        ))
        self.register(ToolSpec(
            name="get_open_files",
            description="返回编辑器当前打开的文件列表。",
            parameters=_schema({}),
            handler=self.get_open_files,
        ))
        self.register(ToolSpec(
            name="get_outline",
            description="返回某文件的符号大纲（函数/类/方法及其行号）。",
            parameters=_schema({"path": {"type": "string"}}, ["path"]),
            handler=self.get_outline,
        ))
        self.register(ToolSpec(
            name="git_status",
            description="返回 git 状态：分支、领先/落后、暂存/未暂存/未跟踪文件。",
            parameters=_schema({}),
            handler=self.git_status,
        ))
        self.register(ToolSpec(
            name="git_diff",
            description="返回 git 差异（可选只看暂存区）。",
            parameters=_schema({
                "staged": {"type": "boolean"},
                "path": {"type": "string"},
            }),
            handler=self.git_diff,
        ))

    # -- 各工具实现 -------------------------------------------------------

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        text = self.workspace.read(path)
        if start_line <= 1 and end_line <= 0:
            return text
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = len(lines) if int(end_line) <= 0 else min(len(lines), int(end_line))
        if start > end:
            raise ToolError(f"行范围非法: {start_line}..{end_line}（文件共 {len(lines)} 行）")
        return "\n".join(lines[start - 1:end])

    def write_file(self, path: str, content: str) -> str:
        self.workspace.write(path, content)
        return f"已写入 {path}（{len(content)} 字符）"

    def edit_file(self, path: str, search: str, replace: str,
                  count: int = 1) -> str:
        if not search:
            raise ToolError("search 不能为空")
        text = self.workspace.read(path)
        occurrences = text.count(search)
        if occurrences == 0:
            raise ToolError(f"在 {path} 里找不到要替换的内容（{len(search)} 字符）")
        limit = occurrences if int(count) == 0 else max(1, int(count))
        updated = text.replace(search, replace, limit)
        self.workspace.write(path, updated)
        return f"已在 {path} 替换 {min(limit, occurrences)} 处（原有 {occurrences} 处）"

    def list_files(self, path: str = "", depth: int = 2) -> str:
        tree = self.workspace.tree(path, max_depth=max(1, min(int(depth), 8)))
        lines: list[str] = []

        def walk(node, prefix: str = "") -> None:
            for child in node.children:
                marker = "/" if child.is_dir else ""
                lines.append(f"{prefix}{child.name}{marker}")
                if child.is_dir:
                    walk(child, prefix + "  ")

        walk(tree)
        return "\n".join(lines) if lines else "（目录为空）"

    def search_code(self, query: str, regex: bool = False,
                    include: str = "", max_results: int = 40) -> str:
        hits = self.workspace.search(query, regex=bool(regex),
                                    include=include or None,
                                    max_results=max(1, min(int(max_results), 200)))
        if not hits:
            return "（无命中）"
        return "\n".join(f"{hit.path}:{hit.line}: {hit.text}" for hit in hits)

    def run_command(self, command: str, cwd: str = "", timeout: int = 0) -> ToolResult:
        command = (command or "").strip()
        if not command:
            raise ToolError("命令不能为空")
        if not self.allow_dangerous:
            for pattern, why in DANGEROUS_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    raise ToolError(f"拒绝执行危险命令（{why}）: {command[:120]}")
        working_dir = self.workspace.resolve(cwd) if cwd else self.workspace.root
        if not working_dir.is_dir():
            raise ToolError(f"工作目录不存在: {cwd}")
        limit = min(int(timeout) or self.command_timeout, MAX_COMMAND_TIMEOUT)
        try:
            completed = subprocess.run(
                command, cwd=working_dir, shell=True, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=limit)
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"命令超时（{limit}s）: {command[:120]}",
                              meta={"timeout": limit})
        output = (completed.stdout or "") + (completed.stderr or "")
        return ToolResult(ok=completed.returncode == 0, output=output,
                          error="" if completed.returncode == 0
                          else f"退出码 {completed.returncode}",
                          meta={"exit_code": completed.returncode, "cwd": str(working_dir)})

    def get_diagnostics(self, path: str = "") -> ToolResult:
        """真实静态检查：Python 用 ast，JSON/TOML 用标准库解析。

        返回 :class:`ToolResult`，**发现问题时 ``ok=False``**，问题明细放在
        ``meta["problems"]`` 里。这样调用方只判断 ``ok`` 就够了 ——
        之前调用方用 ``"问题" in output`` 判断，结果连**「未发现问题」**都会被
        当成有问题（这是一个真实踩过的坑，有回归测试）。
        """
        targets = [path] if path else list(self.workspace.iter_files())
        problems: list[str] = []
        checked = 0
        for relative in targets:
            language = self.workspace.language_of(relative)
            if language not in ("python", "json", "toml"):
                continue
            if not self.workspace.is_text_file(relative):
                continue
            try:
                text = self.workspace.read(relative)
            except WorkspaceError:
                continue
            checked += 1
            try:
                if language == "python":
                    ast.parse(text)
                elif language == "json":
                    json.loads(text)
                else:
                    import tomllib

                    tomllib.loads(text)
            except SyntaxError as exc:
                problems.append(f"{relative}:{exc.lineno}: {exc.msg}")
            except (json.JSONDecodeError, ValueError) as exc:
                problems.append(f"{relative}: {exc}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{relative}: {type(exc).__name__}: {exc}")
        if not problems:
            return ToolResult(ok=True, output=f"未发现问题（检查了 {checked} 个文件）",
                              meta={"checked": checked, "problems": []})
        return ToolResult(
            ok=False,
            output=(f"发现 {len(problems)} 个问题（检查了 {checked} 个文件）:\n"
                    + "\n".join(problems)),
            error=f"{len(problems)} 个静态检查问题",
            meta={"checked": checked, "problems": problems})

    def get_open_files(self) -> str:
        if self.open_files is None:
            return "（编辑器未提供打开文件列表）"
        files = list(self.open_files())
        return "\n".join(files) if files else "（没有打开的文件）"

    def get_outline(self, path: str) -> str:
        if self.index is None:
            raise ToolError("索引未初始化")
        symbols = self.index.outline(path)
        if not symbols:
            return f"（{path} 未提取到符号）"
        return "\n".join(f"{s.line:>5}  {s.kind:<9} {s.detail or s.name}" for s in symbols)

    def git_status(self) -> str:
        data = self.repo.summary()
        if not data.get("ok"):
            return f"（不可用: {data.get('error')}）"
        lines = [f"分支: {data['branch']}"
                 + (f" → {data['upstream']}" if data["upstream"] else ""),
                 f"领先 {data['ahead']} / 落后 {data['behind']}",
                 f"暂存 {data['staged']} / 未暂存 {data['unstaged']} / "
                 f"未跟踪 {data['untracked']} / 冲突 {data['conflicted']}"]
        status = self.repo.status()
        for change in status.changes[:50]:
            lines.append(f"  {change.kind:<10} {change.path}")
        return "\n".join(lines)

    def git_diff(self, staged: bool = False, path: str = "") -> str:
        diff = self.repo.diff(staged=bool(staged), path=path or None)
        return diff or "（无差异）"


#: README 声称的工具名 -> 本实现的映射（用于「接线自检」）
CLAIMED_TOOLS: tuple[str, ...] = (
    "read_file", "write_file", "list_files", "search_code", "run_command",
    "get_diagnostics", "get_open_files", "git_status", "git_diff",
)
