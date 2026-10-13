# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""静态检查（lint）。

对应 TCIDE 原版的 `core/lint/lint-manager.ts` —— 该模块在原版**从未接线，功能静默失效**
（作者注释原话：「preload/IPC 从未接线，导致……实时诊断等 README 宣称功能静默失效」）。

本实现刻意**只用标准库**（`ast` + 文本规则），因此：
- 不需要装 flake8/ruff/pyright 就能工作；
- 每一条规则都有稳定 id 与明确依据，可离线逐条断言。

Python 用 `ast`，其他语言走**文本规则**（长行、行尾空白、Tab/空格混用、冲突标记、
TODO 等）。规则集刻意保持小而准 —— 宁少报，不误报。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .workspace import Workspace

#: 严重度（按严重排序用）
SEVERITIES: tuple[str, ...] = ("error", "warning", "info")
SEVERITY_RANK: dict[str, int] = {name: index for index, name in enumerate(SEVERITIES)}

#: 规则表：id -> (严重度, 说明)
RULES: dict[str, tuple[str, str]] = {
    "syntax-error": ("error", "语法错误，文件无法解析"),
    "conflict-marker": ("error", "残留了 git 冲突标记"),
    "bare-except": ("warning", "裸 except 会吞掉所有异常（含 KeyboardInterrupt）"),
    "unused-import": ("warning", "导入了但没用到"),
    "mutable-default": ("warning", "可变默认参数会在调用之间共享"),
    "none-comparison": ("info", "与 None 比较建议用 is / is not"),
    "line-too-long": ("info", "行过长"),
    "trailing-whitespace": ("info", "行尾有多余空白"),
    "mixed-indent": ("warning", "同一文件混用 Tab 与空格缩进"),
    "missing-final-newline": ("info", "文件末尾没有换行"),
    "todo-marker": ("info", "遗留的 TODO / FIXME / XXX"),
}

#: 文本规则默认阈值
MAX_LINE_LENGTH = 120
CONFLICT_MARKERS: tuple[str, ...] = ("<<<<<<<", ">>>>>>>", "=======")
TODO_RE = None  # 在下面用 re 编译（避免 import 顺序问题）


@dataclass
class Issue:
    """一条检查结果。"""

    path: str
    line: int
    rule: str
    message: str = ""
    column: int = 0
    severity: str = "info"
    symbol: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "column": self.column,
                "rule": self.rule, "severity": self.severity,
                "message": self.message, "symbol": self.symbol}

    def location(self) -> str:
        base = f"{self.path}:{self.line}"
        return f"{base}:{self.column}" if self.column else base

    def __str__(self) -> str:
        return f"{self.location()}: [{self.severity}] {self.rule}: {self.message}"


class Linter:
    """对一个文件/一段文本做检查。"""

    def __init__(self, workspace=None, *, max_line_length: int = MAX_LINE_LENGTH) -> None:
        import re

        self.workspace = workspace
        self.max_line_length = int(max_line_length)
        self.todo_re = re.compile(r"\b(TODO|FIXME|XXX)\b[:\s]*(.*)")

    # -- 语言无关的文本规则 -----------------------------------------------

    def _text_issues(self, path: str, text: str) -> list[Issue]:
        issues: list[Issue] = []
        lines = text.splitlines()
        tabs = spaces = 0
        for number, line in enumerate(lines, 1):
            if len(line) > self.max_line_length:
                issues.append(Issue(path, number, "line-too-long",
                                    f"{len(line)} 字符（上限 {self.max_line_length}）",
                                    severity=RULES["line-too-long"][0]))
            if line != line.rstrip():
                issues.append(Issue(path, number, "trailing-whitespace",
                                    "行尾有空白",
                                    severity=RULES["trailing-whitespace"][0]))
            stripped = line.lstrip()
            indent = line[:len(line) - len(stripped)]
            if stripped:
                if "\t" in indent:
                    tabs += 1
                elif indent:
                    spaces += 1
            if line.startswith(CONFLICT_MARKERS):
                issues.append(Issue(path, number, "conflict-marker",
                                    f"发现冲突标记 {line[:12]!r}",
                                    severity=RULES["conflict-marker"][0]))
            found = self.todo_re.search(line)
            if found:
                issues.append(Issue(path, number, "todo-marker",
                                    found.group(0).strip()[:60],
                                    column=found.start() + 1,
                                    severity=RULES["todo-marker"][0]))
        if tabs and spaces:
            issues.append(Issue(path, 1, "mixed-indent",
                                f"Tab 缩进 {tabs} 行 / 空格缩进 {spaces} 行",
                                severity=RULES["mixed-indent"][0]))
        if text and not text.endswith("\n"):
            issues.append(Issue(path, len(lines) or 1, "missing-final-newline",
                                "文件末尾缺少换行",
                                severity=RULES["missing-final-newline"][0]))
        return issues

    # -- Python 专用（AST）------------------------------------------------

    def _python_issues(self, path: str, text: str) -> list[Issue]:
        issues: list[Issue] = []
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            issues.append(Issue(path, exc.lineno or 1, "syntax-error",
                                exc.msg or "语法错误", column=exc.offset or 0,
                                severity=RULES["syntax-error"][0]))
            return issues

        # 裸 except / 可变默认参数 / == None
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append(Issue(path, node.lineno, "bare-except",
                                    "裸 except", severity=RULES["bare-except"][0]))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in list(node.args.defaults) + [
                        item for item in node.args.kw_defaults if item is not None]:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set,
                                            ast.ListComp, ast.DictComp, ast.SetComp)):
                        issues.append(Issue(
                            path, getattr(default, "lineno", node.lineno),
                            "mutable-default",
                            f"{node.name} 的可变默认参数",
                            severity=RULES["mutable-default"][0]))
            if isinstance(node, ast.Compare):
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq)) and (
                            isinstance(comparator, ast.Constant)
                            and comparator.value is None):
                        issues.append(Issue(
                            path, node.lineno, "none-comparison",
                            "与 None 用 == / != 比较",
                            severity=RULES["none-comparison"][0]))

        # 未使用的 import：只在 import 语句之后出现的名字里找
        imported: list[tuple[str, int]] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imported.append((name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported.append((alias.asname or alias.name, node.lineno))
        if imported:
            used: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    used.add(node.value.id)
            # __all__ 里列出的也算用到
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__all__":
                            for item in ast.walk(node.value):
                                if isinstance(item, ast.Constant) and isinstance(
                                        item.value, str):
                                    used.add(item.value)
            for name, lineno in imported:
                if name not in used:
                    issues.append(Issue(path, lineno, "unused-import",
                                        f"{name} 导入了但没用到", symbol=name,
                                        severity=RULES["unused-import"][0]))
        return issues

    # -- 对外接口 ---------------------------------------------------------

    def lint_text(self, path: str, text: str, *, language: str = "") -> list[Issue]:
        issues = self._text_issues(path, text)
        if not language:
            # 没有 workspace 时也要能按**后缀**判断语言（`Workspace.language_of`
            # 是静态方法）。以前这里退化成 plaintext，导致不带 workspace 时
            # Python 规则**一条都不会跑**。
            language = (self.workspace.language_of(path) if self.workspace
                        else Workspace.language_of(path))
        if language == "python":
            issues += self._python_issues(path, text)
        return self._sorted(issues)

    def lint_file(self, path: str) -> list[Issue]:
        if self.workspace is None:
            raise ValueError("没有 workspace，无法按路径检查")
        if not self.workspace.is_text_file(path):
            return []
        try:
            text = self.workspace.read(path)
        except Exception:  # noqa: BLE001
            return []
        return self.lint_text(path, text)

    def lint_all(self, *, limit: int = 500) -> dict[str, list[Issue]]:
        """检查整个工作区（只返回有问题的文件）。"""
        if self.workspace is None:
            raise ValueError("没有 workspace，无法全量检查")
        result: dict[str, list[Issue]] = {}
        for index, path in enumerate(self.workspace.iter_files()):
            if index >= limit:
                break
            issues = self.lint_file(path)
            if issues:
                result[path] = issues
        return result

    @staticmethod
    def _sorted(issues: list[Issue]) -> list[Issue]:
        return sorted(issues, key=lambda i: (SEVERITY_RANK.get(i.severity, 9),
                                            i.path, i.line, i.column))

    # -- 汇总 -------------------------------------------------------------

    @staticmethod
    def summary(issues) -> dict:
        """按严重度/规则统计（界面用）。"""
        if isinstance(issues, dict):
            flat = [issue for group in issues.values() for issue in group]
        else:
            flat = list(issues)
        by_severity: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        for issue in flat:
            by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
            by_rule[issue.rule] = by_rule.get(issue.rule, 0) + 1
        return {
            "total": len(flat),
            "by_severity": {name: by_severity.get(name, 0) for name in SEVERITIES},
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "errors": by_severity.get("error", 0),
        }

    @staticmethod
    def report(issues, *, limit: int = 30) -> str:
        """Markdown 报告。"""
        if isinstance(issues, dict):
            flat = [issue for group in issues.values() for issue in group]
        else:
            flat = list(issues)
        if not flat:
            return "没有发现问题"
        stats = Linter.summary(flat)
        lines = [f"共 {stats['total']} 条（错误 {stats['errors']}）", ""]
        lines += [f"- {issue}" for issue in flat[:limit]]
        if len(flat) > limit:
            lines.append(f"- …（还有 {len(flat) - limit} 条）")
        return "\n".join(lines)
