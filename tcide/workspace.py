# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""工作区：文件树、读写、搜索、语言识别。

对应 TCIDE 原版的 `file-service.ts` + 文件树/项目搜索。纯标准库，不依赖 Qt，
因此可以完全离线严格测试。

安全要点：**所有对外接口都过 `resolve()`，拒绝路径逃逸**。IDE 的路径来自
界面与 AI 生成的参数，越界读写是必须挡住的一类问题。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

#: 默认跳过的目录（与 TCIDE 的 SKIP_DIRS 对齐并补充）
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", "release", ".next", ".nuxt",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    "coverage", "htmlcov", ".cache", "site-packages",
})

#: 已知二进制扩展名（预览/搜索时跳过）
BINARY_EXTS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns", ".tif", ".tiff",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus",
    ".mp4", ".webm", ".avi", ".mov", ".mkv", ".wmv", ".flv",
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".wasm", ".class", ".pyc", ".o", ".a",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
})

#: 扩展名 -> 语言（覆盖 TCIDE 宣称的 23 种高亮语言）
LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".json": "json", ".jsonc": "json", ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html", ".xml": "xml", ".svg": "xml",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".bat": "bat", ".cmd": "bat",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".sql": "sql", ".txt": "text", ".log": "text",
    # `.paw` 是 PawUI 自己的 DSL（组件标签、{$state} 插值），不是 HTML ——
    # 归到 html 会让关键字/片段补全与语言识别都找错表
    ".paw": "paw",
}

#: 单文件读取上限（避免把二进制/巨文件读进内存）
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024


class WorkspaceError(Exception):
    """工作区操作失败（越界、不可读、过大等）。"""


@dataclass
class FileNode:
    """文件树的一个节点。"""

    path: str          # 相对 root 的 POSIX 风格路径
    name: str
    is_dir: bool
    size: int = 0
    children: list["FileNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {"path": self.path, "name": self.name, "is_dir": self.is_dir,
                "size": self.size}
        if self.is_dir:
            data["children"] = [child.to_dict() for child in self.children]
        return data


@dataclass
class SearchHit:
    """一条搜索命中。"""

    path: str
    line: int
    text: str
    score: float = 1.0

    def to_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "text": self.text,
                "score": round(self.score, 3)}


class Workspace:
    """一个项目根目录上的一组安全文件操作。"""

    def __init__(
        self,
        root: str | Path,
        *,
        skip_dirs: frozenset[str] | set[str] | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        path = Path(root)
        if not path.exists():
            raise WorkspaceError(f"目录不存在: {path}")
        if not path.is_dir():
            raise WorkspaceError(f"不是目录: {path}")
        self.root = path.resolve()
        self.skip_dirs = frozenset(skip_dirs) if skip_dirs is not None else DEFAULT_SKIP_DIRS
        self.max_file_bytes = int(max_file_bytes)

    # -- 路径 -------------------------------------------------------------

    def resolve(self, relative: str | Path) -> Path:
        """把相对路径解析成绝对路径；**越界直接抛错**。"""
        raw = Path(relative)
        candidate = (raw if raw.is_absolute() else self.root / raw)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"路径越界（不在工作区内）: {relative}") from exc
        return resolved

    def relative(self, path: str | Path) -> str:
        """绝对路径 -> 相对 root 的 POSIX 路径。"""
        resolved = self.resolve(path)
        return resolved.relative_to(self.root).as_posix()

    def _skipped(self, name: str) -> bool:
        return name in self.skip_dirs

    # -- 文件树 -----------------------------------------------------------

    def list_dir(self, relative: str = "", *, include_hidden: bool = False) -> list[FileNode]:
        """列出一层目录（目录在前，名称排序）。"""
        target = self.resolve(relative)
        if not target.is_dir():
            raise WorkspaceError(f"不是目录: {relative}")
        nodes: list[FileNode] = []
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.is_dir():
                if self._skipped(entry.name):
                    continue
                if not include_hidden and entry.name.startswith("."):
                    continue
            elif not include_hidden and entry.name.startswith("."):
                continue
            rel = entry.relative_to(self.root).as_posix()
            size = 0
            if entry.is_file():
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
            nodes.append(FileNode(path=rel, name=entry.name, is_dir=entry.is_dir(), size=size))
        return nodes

    def tree(self, relative: str = "", *, max_depth: int = 4,
             include_hidden: bool = False) -> FileNode:
        """递归文件树（深度受限，避免在大仓库上爆栈）。"""
        target = self.resolve(relative)
        node = FileNode(
            path=self.relative(target) if target != self.root else "",
            name=target.name or str(self.root),
            is_dir=True,
        )

        def walk(current: FileNode, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = self.list_dir(current.path, include_hidden=include_hidden)
            except WorkspaceError:
                return
            current.children = entries
            if depth == max_depth:
                return
            for child in entries:
                if child.is_dir:
                    walk(child, depth + 1)

        walk(node, 1)
        return node

    def find(self, pattern: str, *, max_results: int = 500,
             include_hidden: bool = False) -> list[str]:
        """按 glob 查找文件（返回相对路径）。"""
        hits: list[str] = []
        for current, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs
                       if d not in self.skip_dirs and (include_hidden or not d.startswith("."))]
            for name in files:
                if not include_hidden and name.startswith("."):
                    continue
                if fnmatch.fnmatch(name, pattern):
                    hits.append(Path(current, name).relative_to(self.root).as_posix())
                    if len(hits) >= max_results:
                        return sorted(hits)
        return sorted(hits)

    def iter_files(self, *, include_hidden: bool = False):
        """遍历所有非跳过文件，产出相对路径。"""
        for current, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs
                       if d not in self.skip_dirs and (include_hidden or not d.startswith("."))]
            for name in files:
                if not include_hidden and name.startswith("."):
                    continue
                yield Path(current, name).relative_to(self.root).as_posix()

    # -- 读写 -------------------------------------------------------------

    @staticmethod
    def language_of(path: str | Path) -> str:
        """按扩展名判断语言（未知返回 plaintext）。"""
        return LANGUAGE_BY_EXT.get(Path(path).suffix.lower(), "plaintext")

    @staticmethod
    def is_binary(path: str | Path) -> bool:
        """按扩展名判断是否二进制。"""
        return Path(path).suffix.lower() in BINARY_EXTS

    def exists(self, relative: str | Path) -> bool:
        try:
            return self.resolve(relative).exists()
        except WorkspaceError:
            return False

    def is_text_file(self, relative: str | Path) -> bool:
        """内容级判断：前 1KB 内没有 NUL 就算文本。"""
        path = self.resolve(relative)
        if not path.is_file() or self.is_binary(path):
            return False
        try:
            with path.open("rb") as handle:
                return b"\x00" not in handle.read(1024)
        except OSError:
            return False

    def read(self, relative: str | Path, *, max_bytes: int | None = None) -> str:
        """读文本；超限则截断并附提示（不是抛错 —— 界面要能看到部分内容）。"""
        path = self.resolve(relative)
        if not path.is_file():
            raise WorkspaceError(f"不是文件: {relative}")
        limit = self.max_file_bytes if max_bytes is None else int(max_bytes)
        try:
            size = path.stat().st_size
            if size > limit:
                with path.open("rb") as handle:
                    chunk = handle.read(limit)
                text = chunk.decode("utf-8", errors="replace")
                return text + f"\n…  (文件 {size} 字节，只读了前 {limit} 字节)\n"
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise WorkspaceError(f"读取失败: {relative}: {exc}") from exc

    def read_bytes(self, relative: str | Path, *, max_bytes: int = 65536) -> bytes:
        path = self.resolve(relative)
        with path.open("rb") as handle:
            return handle.read(max_bytes)

    def write(self, relative: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
        """写文本（自动建父目录，先写临时文件再替换，避免半截文件）。"""
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tcide-tmp")
        try:
            temp.write_text(content, encoding=encoding, newline="")
            os.replace(temp, path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise WorkspaceError(f"写入失败: {relative}: {exc}") from exc
        return path

    def write_bytes(self, relative: str | Path, data: bytes) -> Path:
        """写二进制（同样的临时文件 + 替换，避免半截文件）。

        与 ``read_bytes`` 成对：Hex 视图 / 二进制改动审核都要用它。
        """
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tcide-tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise WorkspaceError(f"写入失败: {relative}: {exc}") from exc
        return path

    def delete(self, relative: str | Path) -> None:
        path = self.resolve(relative)
        if not path.is_file():
            raise WorkspaceError(f"不是文件: {relative}")
        path.unlink()

    def create_dir(self, relative: str | Path) -> Path:
        path = self.resolve(relative)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- 搜索 -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        include: str | None = None,
        max_results: int = 200,
        max_file_bytes: int | None = None,
    ) -> list[SearchHit]:
        """跨文件搜索（默认子串匹配，可选正则）。

        评分是**关键词命中数**（文件名命中权重更高）——不是向量相似度，如实命名。

        返回两类命中：
        - **文件名命中**：``line=0``、``text="（文件名匹配）…"``，只要文件名含查询词就产出；
        - **内容命中**：``line>=1``，是该行的内容。

        文件名为 ``.png`` 之类的二进制也会作为文件名命中出现（这有用：能按名字找到文件），
        但**不会**被读进来做内容搜索。
        """
        if not query:
            return []
        limit = self.max_file_bytes if max_file_bytes is None else int(max_file_bytes)
        pattern = None
        if regex:
            import re as _re

            flags = 0 if case_sensitive else _re.IGNORECASE
            try:
                pattern = _re.compile(query, flags)
            except _re.error as exc:
                raise WorkspaceError(f"正则无效: {exc}") from exc
        needle = query if case_sensitive else query.lower()

        filename_hits: list[SearchHit] = []
        for relative in self.iter_files():
            if include and not fnmatch.fnmatch(Path(relative).name, include):
                continue
            name = Path(relative).name
            matched = (bool(pattern.search(name)) if pattern
                       else needle in (name if case_sensitive else name.lower()))
            if matched:
                filename_hits.append(SearchHit(relative, 0, f"（文件名匹配）{relative}",
                                               score=1.5))
                if len(filename_hits) >= max_results:
                    return filename_hits

        remaining = max_results - len(filename_hits)
        hits: list[SearchHit] = []
        for relative in self.iter_files():
            if include and not fnmatch.fnmatch(Path(relative).name, include):
                continue
            if not self.is_text_file(relative):
                continue
            path = self.resolve(relative)
            try:
                if path.stat().st_size > limit:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name_bonus = 1.5 if needle in Path(relative).name.lower() else 0.0
            for number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.lower()
                matched = bool(pattern.search(line)) if pattern else needle in haystack
                if not matched:
                    continue
                hits.append(SearchHit(relative, number, line.strip()[:200],
                                      score=1.0 + name_bonus))
                if len(hits) >= remaining:
                    return filename_hits + hits
        return filename_hits + hits

    def recent(self, count: int = 20) -> list[tuple[str, float]]:
        """最近修改的文件（相对路径, mtime）。"""
        items: list[tuple[str, float]] = []
        for relative in self.iter_files():
            try:
                items.append((relative, self.resolve(relative).stat().st_mtime))
            except OSError:
                continue
        items.sort(key=lambda pair: pair[1], reverse=True)
        return items[:count]

    def stats(self) -> dict:
        """工作区概览：文件数、总大小、按语言统计。"""
        files = 0
        total = 0
        by_language: dict[str, int] = {}
        for relative in self.iter_files():
            files += 1
            try:
                total += self.resolve(relative).stat().st_size
            except OSError:
                pass
            language = self.language_of(relative)
            by_language[language] = by_language.get(language, 0) + 1
        return {
            "root": str(self.root),
            "files": files,
            "bytes": total,
            "by_language": dict(sorted(by_language.items(), key=lambda kv: -kv[1])),
        }
