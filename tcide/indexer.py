# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""项目索引：符号表、调用图、依赖图、关键词检索、语义切块。

对应 TCIDE 原版的 `indexer/project-indexer.ts`、`indexer/vector-indexer.ts`、
`indexer/warehouse-analyzer.ts`、`chunker/semantic-chunker.ts`、`arch/arch-analyzer.ts`。

**诚实标注**：原版叫 "vector-indexer"，但实现是关键词/哈希打分，没有 embedding。
这里沿用关键词打分，名字如实叫检索，并在返回结果里保留命中依据。

Python 文件用 `ast` 精确解析（`precise=True`）；其他语言用正则提取（`precise=False`），
调用方可以据此判断置信度。
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field

from .workspace import SearchHit, Workspace

#: 超过这个大小就不建索引（避免拖垮界面）
DEFAULT_MAX_INDEX_BYTES = 1024 * 1024

#: 每个语言的符号提取正则（按扩展名分组）
REGEX_RULES: dict[str, list[tuple[str, str]]] = {
    "typescript": [
        ("function", r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
        ("class", r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"),
        ("interface", r"^\s*(?:export\s+)?interface\s+(\w+)"),
        ("variable", r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\("),
        ("variable", r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[:=]"),
        ("method", r"^\s*(?:public|private|protected|static|async|\s)*(\w+)\s*\([^)]*\)\s*[:{]"),
    ],
    "javascript": [
        ("function", r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
        ("class", r"^\s*(?:export\s+)?class\s+(\w+)"),
        ("variable", r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\("),
    ],
    "go": [
        ("function", r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("),
        ("struct", r"^\s*type\s+(\w+)\s+struct\b"),
        ("interface", r"^\s*type\s+(\w+)\s+interface\b"),
    ],
    "rust": [
        ("function", r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"),
        ("struct", r"^\s*(?:pub\s+)?struct\s+(\w+)"),
        ("interface", r"^\s*(?:pub\s+)?trait\s+(\w+)"),
        ("class", r"^\s*(?:pub\s+)?enum\s+(\w+)"),
    ],
    "java": [
        ("class", r"^\s*(?:public|private|protected)?\s*(?:final\s+|abstract\s+)?class\s+(\w+)"),
        ("interface", r"^\s*(?:public\s+)?interface\s+(\w+)"),
        ("method", r"^\s*(?:public|private|protected)\s+[\w<>\[\],\s]+\s+(\w+)\s*\("),
    ],
    "kotlin": [
        ("class", r"^\s*(?:open\s+|data\s+|sealed\s+)?class\s+(\w+)"),
        ("function", r"^\s*(?:suspend\s+)?fun\s+(\w+)"),
        ("variable", r"^\s*(?:val|var)\s+(\w+)"),
    ],
    "csharp": [
        ("class", r"^\s*(?:public|internal|private)?\s*(?:sealed\s+|abstract\s+|static\s+)?class\s+(\w+)"),
        ("method", r"^\s*(?:public|private|protected|internal)\s+[\w<>\[\],\s]+\s+(\w+)\s*\("),
    ],
    "ruby": [
        ("function", r"^\s*def\s+(\w+)"),
        ("class", r"^\s*class\s+(\w+)"),
        ("module", r"^\s*module\s+(\w+)"),
    ],
    "php": [
        ("function", r"^\s*(?:public|private|protected)?\s*function\s+(\w+)"),
        ("class", r"^\s*class\s+(\w+)"),
    ],
    "c": [
        ("function", r"^[\w\*\s]+?\b(\w+)\s*\([^;]*\)\s*\{"),
        ("struct", r"^\s*(?:typedef\s+)?struct\s+(\w+)"),
    ],
    "cpp": [
        ("class", r"^\s*class\s+(\w+)"),
        ("struct", r"^\s*struct\s+(\w+)"),
        ("function", r"^[\w\*\s:<>]+?\b(\w+)\s*\([^;]*\)\s*\{"),
    ],
    "shell": [("function", r"^\s*(?:function\s+)?(\w+)\s*\(\)\s*\{")],
    "powershell": [("function", r"^\s*function\s+(\w+)")],
}

#: 兜底规则（未知语言）
FALLBACK_RULES: list[tuple[str, str]] = [
    ("function", r"^\s*def\s+(\w+)"),
    ("class", r"^\s*class\s+(\w+)"),
]


@dataclass
class Symbol:
    """一个符号（函数/类/方法/变量/导入…）。"""

    name: str
    kind: str
    line: int
    end_line: int
    path: str
    detail: str = ""
    precise: bool = True   # True=AST 解析，False=正则推断

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "line": self.line,
                "end_line": self.end_line, "path": self.path,
                "detail": self.detail, "precise": self.precise}


@dataclass
class FileIndex:
    """单个文件的索引结果。"""

    path: str
    language: str
    lines: int
    size: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "language": self.language, "lines": self.lines,
                "size": self.size, "symbols": [s.to_dict() for s in self.symbols],
                "imports": self.imports, "error": self.error}


@dataclass
class IndexStats:
    files: int = 0
    skipped: int = 0
    symbols: int = 0
    errors: int = 0
    duration_ms: int = 0
    by_language: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"files": self.files, "skipped": self.skipped, "symbols": self.symbols,
                "errors": self.errors, "duration_ms": self.duration_ms,
                "by_language": self.by_language}


@dataclass
class Chunk:
    """语义切块：给大文件喂给模型用。"""

    path: str
    start: int
    end: int
    text: str
    symbol: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "start": self.start, "end": self.end,
                "symbol": self.symbol, "chars": len(self.text)}


class ProjectIndex:
    """在 :class:`Workspace` 上建立可查询的代码索引。"""

    def __init__(self, workspace: Workspace, *,
                 max_file_bytes: int = DEFAULT_MAX_INDEX_BYTES) -> None:
        self.workspace = workspace
        self.max_file_bytes = int(max_file_bytes)
        self.files: dict[str, FileIndex] = {}
        self.last_stats = IndexStats()

    # -- 构建 -------------------------------------------------------------

    def scan(self, *, max_files: int | None = None) -> IndexStats:
        """遍历工作区建立索引（幂等，可重复调用刷新）。"""
        started = time.perf_counter()
        stats = IndexStats()
        for relative in self.workspace.iter_files():
            if max_files is not None and stats.files >= max_files:
                break
            index = self.index_file(relative)
            if index is None:
                stats.skipped += 1
                continue
            self.files[relative] = index
            stats.files += 1
            stats.symbols += len(index.symbols)
            if index.error:
                stats.errors += 1
            stats.by_language[index.language] = stats.by_language.get(index.language, 0) + 1
        stats.by_language = dict(sorted(stats.by_language.items(), key=lambda kv: -kv[1]))
        stats.duration_ms = int((time.perf_counter() - started) * 1000)
        self.last_stats = stats
        return stats

    def index_file(self, relative: str) -> FileIndex | None:
        """索引单个文件；跳过二进制与过大文件（返回 None）。"""
        if self.workspace.is_binary(relative):
            return None
        try:
            path = self.workspace.resolve(relative)
            size = path.stat().st_size
        except OSError:
            return None
        if size > self.max_file_bytes:
            return None
        if not self.workspace.is_text_file(relative):
            return None
        try:
            text = self.workspace.read(relative)
        except Exception:  # noqa: BLE001
            return None
        language = self.workspace.language_of(relative)
        lines = text.count("\n") + 1
        index = FileIndex(path=relative, language=language, lines=lines, size=size)
        if language == "python":
            self._index_python(relative, text, index)
        else:
            self._index_regex(relative, text, language, index)
        return index

    # -- Python（AST，精确）-----------------------------------------------

    def _index_python(self, relative: str, text: str, index: FileIndex) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            index.error = f"SyntaxError: {exc.msg} (line {exc.lineno})"
            return

        calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    calls.append(func.id)
                elif isinstance(func, ast.Attribute):
                    calls.append(func.attr)

        def visit(node: ast.AST, class_name: str | None = None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "method" if class_name else "function"
                    index.symbols.append(Symbol(
                        name=child.name, kind=kind, line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        path=relative, detail=self._py_signature(child), precise=True))
                    visit(child, None)
                elif isinstance(child, ast.ClassDef):
                    index.symbols.append(Symbol(
                        name=child.name, kind="class", line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        path=relative, detail=", ".join(b.id for b in child.bases if isinstance(b, ast.Name)),
                        precise=True))
                    visit(child, child.name)
                elif isinstance(child, (ast.Import, ast.ImportFrom)):
                    if isinstance(child, ast.Import):
                        for alias in child.names:
                            index.imports.append(alias.name)
                    else:
                        module = child.module or ""
                        if child.level:
                            module = "." * child.level + module
                        index.imports.append(module)
                    visit(child, class_name)
                elif isinstance(child, ast.Assign) and class_name is None and isinstance(node, ast.Module):
                    for target in child.targets:
                        if isinstance(target, ast.Name):
                            index.symbols.append(Symbol(
                                name=target.id, kind="variable", line=child.lineno,
                                end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                                path=relative, precise=True))
                else:
                    visit(child, class_name)

        visit(tree)
        index.symbols.sort(key=lambda s: s.line)
        index.calls = sorted(set(calls))
        index.imports = sorted(set(index.imports))

    @staticmethod
    def _py_signature(node: ast.AST) -> str:
        args = getattr(node, "args", None)
        name = getattr(node, "name", "?")
        if args is None:
            return f"{name}()"
        parts = [arg.arg for arg in list(args.posonlyargs) + list(args.args)]
        if args.vararg:
            parts.append("*" + args.vararg.arg)
        parts += [arg.arg for arg in args.kwonlyargs]
        if args.kwarg:
            parts.append("**" + args.kwarg.arg)
        return f"{name}({', '.join(parts)})"

    # -- 其他语言（正则，标注 precise=False）------------------------------

    def _index_regex(self, relative: str, text: str, language: str,
                     index: FileIndex) -> None:
        rules = REGEX_RULES.get(language, FALLBACK_RULES)
        compiled = [(kind, re.compile(pattern)) for kind, pattern in rules]
        seen: set[tuple[str, int]] = set()
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in compiled:
                match = pattern.search(line)
                if not match:
                    continue
                name = match.group(1)
                if not name or name in ("if", "for", "while", "switch", "catch", "return"):
                    continue
                key = (kind, number)
                if key in seen:
                    break
                seen.add(key)
                index.symbols.append(Symbol(name=name, kind=kind, line=number,
                                            end_line=number, path=relative,
                                            detail=line.strip()[:120], precise=False))
                break
        for number, line in enumerate(text.splitlines(), 1):
            for match in re.finditer(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""", line):
                index.imports.append(match.group(1))
            for match in re.finditer(r"""^\s*import\s+.*?from\s+['"]([^'"]+)['"]""", line):
                index.imports.append(match.group(1))
        index.imports = sorted(set(index.imports))

    # -- 查询 -------------------------------------------------------------

    def _ensure(self) -> None:
        if not self.files:
            self.scan()

    def file(self, relative: str) -> FileIndex | None:
        self._ensure()
        return self.files.get(relative)

    def outline(self, relative: str) -> list[Symbol]:
        """代码大纲：某文件的符号列表（按行号）。"""
        index = self.file(relative)
        return list(index.symbols) if index else []

    def symbols(self, *, kind: str | None = None, path: str | None = None) -> list[Symbol]:
        """全部（或过滤后的）符号。"""
        self._ensure()
        items: list[Symbol] = []
        for index in self.files.values():
            if path and index.path != path:
                continue
            items.extend(s for s in index.symbols if kind is None or s.kind == kind)
        return sorted(items, key=lambda s: (s.path, s.line))

    def find_symbol(self, name: str) -> list[Symbol]:
        """按名字查符号（精确匹配）。"""
        return [s for s in self.symbols() if s.name == name]

    def call_graph(self, *, limit: int = 4000) -> dict[str, list[str]]:
        """调用图：``"<文件>::<符号名>" -> 它调用到的项目内符号名``。

        **键必须带文件路径**：`main` / `start` / `init` / `run` 这类名字在真实
        项目里大量重名，只按裸符号名做键会互相覆盖（这是修出来的 bug，
        有对应的回归测试 `test_call_graph_keys_do_not_collide`）。
        """
        self._ensure()
        defined = {s.name for s in self.symbols()}
        graph: dict[str, list[str]] = {}
        for index in self.files.values():
            for symbol in index.symbols:
                if symbol.kind not in ("function", "method"):
                    continue
                body = self._symbol_text(index.path, symbol)
                called = {
                    name for name in re.findall(r"\b(\w+)\s*\(", body)
                    if name in defined and name != symbol.name
                }
                graph[f"{symbol.path}::{symbol.name}"] = sorted(called)[:limit]
        return graph

    def _symbol_text(self, path: str, symbol: Symbol) -> str:
        try:
            text = self.workspace.read(path)
        except Exception:  # noqa: BLE001
            return ""
        lines = text.splitlines()
        start = max(0, symbol.line - 1)
        end = min(len(lines), symbol.end_line)
        return "\n".join(lines[start:end])

    def dependency_graph(self) -> dict[str, list[str]]:
        """依赖图：``文件 -> 它导入的项目内文件``。"""
        self._ensure()
        known = {path for path in self.files}
        graph: dict[str, list[str]] = {}
        for path, index in self.files.items():
            resolved: set[str] = set()
            for module in index.imports:
                for candidate in self._resolve_module(path, module):
                    if candidate in known and candidate != path:
                        resolved.add(candidate)
            graph[path] = sorted(resolved)
        return graph

    def _resolve_module(self, importer: str, module: str) -> list[str]:
        """把导入名解析成可能的项目内文件路径。"""
        if not module:
            return []
        base = module.lstrip(".")
        parts = [p for p in base.split(".") if p]
        if not parts:
            return []
        text = "/".join(parts)
        candidates: list[str] = []
        suffixes = {
            "python": ["", ".py", "/__init__.py"],
            "typescript": ["", ".ts", ".tsx", "/index.ts"],
            "javascript": ["", ".js", ".jsx", ".mjs", "/index.js"],
            "go": [".go", "/" + parts[-1] + ".go", ""],
            "rust": [".rs", "/mod.rs", ""],
        }
        language = self.workspace.language_of(importer)
        for suffix in suffixes.get(language, ["", ".py", ".ts", ".js"]):
            candidates.append(f"{text}{suffix}")
            # 也尝试相对导入者同级目录
            parent = importer.rsplit("/", 1)[0] if "/" in importer else ""
            if parent:
                candidates.append(f"{parent}/{text}{suffix}")
        return candidates

    def search(self, query: str, *, limit: int = 50) -> list[SearchHit]:
        """符号感知的检索：符号名命中权重高于正文命中。

        这是**关键词/权重打分**，不是向量相似度（原版 `vector-indexer` 的叫法不实）。
        """
        if not query:
            return []
        needle = query.lower()
        scored: list[SearchHit] = []
        for symbol in self.symbols():
            if needle in symbol.name.lower():
                scored.append(SearchHit(symbol.path, symbol.line,
                                        f"{symbol.kind} {symbol.detail or symbol.name}",
                                        score=3.0 if symbol.name.lower() == needle else 2.0))
        for hit in self.workspace.search(query, max_results=limit):
            scored.append(hit)
        scored.sort(key=lambda h: (-h.score, h.path, h.line))
        return scored[:limit]

    # -- 切块 -------------------------------------------------------------

    def chunks(self, relative: str, *, max_lines: int = 80) -> list[Chunk]:
        """把文件切成有语义边界的块：优先按顶层符号切，无符号则按行窗切。

        对应原版 `semantic-chunker`：大文件不能整份塞进上下文。
        """
        index = self.file(relative)
        if index is None:
            return []
        try:
            text = self.workspace.read(relative)
        except Exception:  # noqa: BLE001
            return []
        lines = text.splitlines()
        top_level = [s for s in index.symbols if s.kind in ("function", "class", "struct")
                     and s.end_line > s.line]
        chunks: list[Chunk] = []
        if top_level:
            for position, symbol in enumerate(top_level):
                start = symbol.line
                end = symbol.end_line
                if end - start + 1 > max_lines * 4:
                    end = start + max_lines * 4 - 1
                body = "\n".join(lines[start - 1:end])
                chunks.append(Chunk(relative, start, end, body, symbol.name))
                del position
            return chunks
        for start in range(0, len(lines), max_lines):
            end = min(len(lines), start + max_lines)
            body = "\n".join(lines[start:end])
            if body.strip():
                chunks.append(Chunk(relative, start + 1, end, body))
        return chunks

    def summary(self) -> dict:
        """索引概览：语言分布、最大文件、符号最多的文件。"""
        self._ensure()
        by_kind: dict[str, int] = {}
        for symbol in self.symbols():
            by_kind[symbol.kind] = by_kind.get(symbol.kind, 0) + 1
        biggest = sorted(self.files.values(), key=lambda i: -i.size)[:5]
        richest = sorted(self.files.values(), key=lambda i: -len(i.symbols))[:5]
        return {
            "files": len(self.files),
            "symbols": sum(len(i.symbols) for i in self.files.values()),
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
            "errors": [(i.path, i.error) for i in self.files.values() if i.error],
            "largest_files": [(i.path, i.size) for i in biggest],
            "most_symbols": [(i.path, len(i.symbols)) for i in richest],
        }
