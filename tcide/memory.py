# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""项目记忆：把项目元数据压成「静态上下文」，并注入项目规则。

对应 TCIDE 原版的 `memory/project-memory.ts`、`config/config-manager.ts`，
以及 `CLAUDE.md` 里规定的 `.tcide/context/static.md` 静态缓存机制：

> 项目架构、技术栈、全局配置、接口说明统一存放于 `.tcide/context/static.md`。
> 该部分作为静态缓存内容，单次加载后复用，不重复全量传输、不产生冗余 Token。

这里做两件事：
1. **探测**常用构建系统与清单文件，推导语言/框架/依赖/构建与测试命令/入口文件；
2. 产出**紧凑的 Markdown**，作为 pinned 上下文（配合 `trimmer` 不被裁剪）。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .trimmer import Message
from .workspace import Workspace

#: 会被读作「项目规则」的文件（按优先级）
RULE_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    ".tcide/context/static.md",
    "AGENTS.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
)

#: 依赖名 -> 框架标签
FRAMEWORK_HINTS: dict[str, str] = {
    "react": "React", "vue": "Vue", "svelte": "Svelte", "next": "Next.js",
    "electron": "Electron", "vite": "Vite", "webpack": "Webpack",
    "express": "Express", "fastapi": "FastAPI", "flask": "Flask",
    "django": "Django", "uvicorn": "Uvicorn", "pydantic": "Pydantic",
    "pytest": "pytest", "pyside6": "PySide6", "pawui": "PawUI",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "numpy": "NumPy",
    "pandas": "pandas", "sqlalchemy": "SQLAlchemy", "gin": "Gin",
    "actix": "Actix", "tokio": "Tokio", "spring": "Spring",
}

#: 依赖名 -> 用途标签（用 `--` 统一成小写后匹配）
PACKAGE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"), ("bun.lockb", "bun"),
    ("poetry.lock", "poetry"), ("uv.lock", "uv"), ("Pipfile.lock", "pipenv"),
    ("requirements.txt", "pip"), ("pyproject.toml", "pip"),
    ("go.sum", "go modules"), ("Cargo.lock", "cargo"),
)

#: 可能的入口文件（按优先级）
ENTRY_CANDIDATES: tuple[str, ...] = (
    "main.py", "app.py", "__main__.py", "src/main.py", "manage.py",
    "main.ts", "main.js", "index.js", "index.ts",
    "src/main.ts", "src/index.ts", "src/index.js", "src/main/index.ts",
    "server.js", "server.ts", "main.go", "cmd/main.go", "src/main.rs",
    "Main.java", "Program.cs",
)


@dataclass
class ProjectMemory:
    """项目的静态画像。"""

    name: str = ""
    root: str = ""
    primary_language: str = ""
    languages: dict[str, int] = field(default_factory=dict)
    package_manager: str = ""
    frameworks: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)
    entry_points: list[str] = field(default_factory=list)
    build_command: str = ""
    test_command: str = ""
    run_command: str = ""
    rules: str = ""
    rules_source: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "root": self.root,
            "primary_language": self.primary_language, "languages": self.languages,
            "package_manager": self.package_manager, "frameworks": self.frameworks,
            "dependencies": self.dependencies, "scripts": self.scripts,
            "entry_points": self.entry_points, "build_command": self.build_command,
            "test_command": self.test_command, "run_command": self.run_command,
            "rules_source": self.rules_source, "notes": self.notes,
        }


class ProjectMemoryBuilder:
    """从工作区推导 :class:`ProjectMemory`。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    # -- 工具 -------------------------------------------------------------

    def _maybe_text(self, relative: str, limit: int = 200_000) -> str:
        try:
            if not self.workspace.exists(relative):
                return ""
            return self.workspace.read(relative, max_bytes=limit)
        except Exception:  # noqa: BLE001
            return ""

    def _maybe_json(self, relative: str) -> dict:
        text = self._maybe_text(relative)
        if not text:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _maybe_toml(self, relative: str) -> dict:
        text = self._maybe_text(relative)
        if not text:
            return {}
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}

    # -- 各语言清单 -------------------------------------------------------

    def _read_node(self, memory: ProjectMemory) -> None:
        package = self._maybe_json("package.json")
        if not package:
            return
        memory.name = memory.name or str(package.get("name") or "")
        scripts = package.get("scripts") or {}
        if isinstance(scripts, dict):
            memory.scripts.update({str(k): str(v) for k, v in scripts.items()})
        deps: list[str] = []
        for key in ("dependencies", "devDependencies"):
            value = package.get(key)
            if isinstance(value, dict):
                deps.extend(str(name) for name in value)
        memory.dependencies.extend(deps)
        if memory.scripts.get("build"):
            memory.build_command = memory.build_command or "npm run build"
        if memory.scripts.get("test"):
            memory.test_command = memory.test_command or "npm test"
        if memory.scripts.get("dev"):
            memory.run_command = memory.run_command or "npm run dev"
        elif memory.scripts.get("start"):
            memory.run_command = memory.run_command or "npm start"

    def _read_python(self, memory: ProjectMemory) -> None:
        requirements = self._maybe_text("requirements.txt")
        if requirements:
            for line in requirements.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip()
                if name:
                    memory.dependencies.append(name)
        pyproject = self._maybe_toml("pyproject.toml")
        if pyproject:
            project = pyproject.get("project") or {}
            if isinstance(project, dict):
                memory.name = memory.name or str(project.get("name") or "")
                deps = project.get("dependencies") or []
                if isinstance(deps, list):
                    for item in deps:
                        name = re.split(r"[<>=!~\[; ]", str(item), 1)[0].strip()
                        if name:
                            memory.dependencies.append(name)
                scripts = project.get("scripts") or {}
                if isinstance(scripts, dict) and scripts:
                    memory.entry_points.append(next(iter(scripts.values())))
            poetry = (pyproject.get("tool") or {}).get("poetry") or {}
            if isinstance(poetry, dict):
                memory.name = memory.name or str(poetry.get("name") or "")
                deps = poetry.get("dependencies") or {}
                if isinstance(deps, dict):
                    memory.dependencies.extend(
                        str(name) for name in deps if str(name).lower() != "python")

    def _read_go(self, memory: ProjectMemory) -> None:
        text = self._maybe_text("go.mod")
        if not text:
            return
        match = re.search(r"^module\s+(\S+)", text, re.M)
        if match:
            memory.name = memory.name or match.group(1).split("/")[-1]
        for match in re.finditer(r"^\s*([\w\.\-/]+)\s+v[\d\.]", text, re.M):
            memory.dependencies.append(match.group(1))
        memory.build_command = memory.build_command or "go build ./..."
        memory.test_command = memory.test_command or "go test ./..."

    def _read_rust(self, memory: ProjectMemory) -> None:
        cargo = self._maybe_toml("Cargo.toml")
        if not cargo:
            return
        package = cargo.get("package") or {}
        if isinstance(package, dict):
            memory.name = memory.name or str(package.get("name") or "")
        deps = cargo.get("dependencies") or {}
        if isinstance(deps, dict):
            memory.dependencies.extend(str(name) for name in deps)
        memory.build_command = memory.build_command or "cargo build"
        memory.test_command = memory.test_command or "cargo test"

    def _read_other_manifests(self, memory: ProjectMemory) -> None:
        if self.workspace.exists("pom.xml"):
            memory.notes.append("检测到 Maven (pom.xml)")
            memory.build_command = memory.build_command or "mvn package"
            memory.test_command = memory.test_command or "mvn test"
        if self.workspace.exists("build.gradle") or self.workspace.exists("build.gradle.kts"):
            memory.notes.append("检测到 Gradle")
            memory.build_command = memory.build_command or "gradle build"
            memory.test_command = memory.test_command or "gradle test"
        gemfile = self._maybe_text("Gemfile")
        if gemfile:
            memory.notes.append("检测到 Bundler (Gemfile)")
            memory.test_command = memory.test_command or "bundle exec rspec"
        composer = self._maybe_json("composer.json")
        if composer:
            memory.name = memory.name or str(composer.get("name") or "")
            require = composer.get("require") or {}
            if isinstance(require, dict):
                memory.dependencies.extend(str(name) for name in require)

    # -- 主流程 -----------------------------------------------------------

    def build(self) -> ProjectMemory:
        # 注意：name 先留空。之前先用目录名兜底会导致清单里的项目名永远读不进来
        # （`memory.name or package["name"]` 永远是目录名）—— 已修，并有回归测试。
        memory = ProjectMemory(root=str(self.workspace.root))
        self._read_node(memory)
        self._read_python(memory)
        self._read_go(memory)
        self._read_rust(memory)
        self._read_other_manifests(memory)

        stats = self.workspace.stats()
        memory.languages = stats.get("by_language", {})
        for language, _count in memory.languages.items():
            if language not in ("plaintext", "json", "markdown", "yaml", "text"):
                memory.primary_language = language
                break

        for name, manager in PACKAGE_MANAGERS:
            if self.workspace.exists(name):
                memory.package_manager = manager
                break

        # 依赖去重并保序
        seen: set[str] = set()
        unique: list[str] = []
        for dep in memory.dependencies:
            key = dep.lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(dep)
        memory.dependencies = unique

        for dep in unique:
            label = FRAMEWORK_HINTS.get(dep.lower())
            if label and label not in memory.frameworks:
                memory.frameworks.append(label)

        if not memory.test_command:
            if self.workspace.exists("tests") or self.workspace.exists("test"):
                memory.test_command = "python -m pytest"
            elif "pytest" in {d.lower() for d in unique}:
                memory.test_command = "python -m pytest"
        if not memory.build_command and memory.primary_language == "python":
            memory.build_command = "python -m build"

        for candidate in ENTRY_CANDIDATES:
            if self.workspace.exists(candidate):
                memory.entry_points.append(candidate)
                if not memory.run_command:
                    memory.run_command = (f"python {candidate}" if candidate.endswith(".py")
                                          else f"node {candidate}")
                break

        if not memory.name:
            memory.name = self.workspace.root.name

        for rule_file in RULE_FILES:
            text = self._maybe_text(rule_file, limit=100_000)
            if text.strip():
                memory.rules = text
                memory.rules_source = rule_file
                break

        return memory

    # -- 输出 -------------------------------------------------------------

    @staticmethod
    def to_markdown(memory: ProjectMemory) -> str:
        """产出紧凑的静态上下文（对应 `.tcide/context/static.md`）。"""
        lines: list[str] = [f"# 项目静态上下文：{memory.name or '(未命名)'}", ""]
        if memory.primary_language:
            lines.append(f"- 主要语言：{memory.primary_language}")
        if memory.languages:
            top = ", ".join(f"{k}({v})" for k, v in list(memory.languages.items())[:6])
            lines.append(f"- 语言分布：{top}")
        if memory.package_manager:
            lines.append(f"- 包管理：{memory.package_manager}")
        if memory.frameworks:
            lines.append(f"- 框架/库：{', '.join(memory.frameworks)}")
        if memory.entry_points:
            lines.append(f"- 入口：{', '.join(memory.entry_points)}")
        if memory.run_command:
            lines.append(f"- 运行：`{memory.run_command}`")
        if memory.build_command:
            lines.append(f"- 构建：`{memory.build_command}`")
        if memory.test_command:
            lines.append(f"- 测试：`{memory.test_command}`")
        if memory.dependencies:
            deps = ", ".join(memory.dependencies[:30])
            more = f" …（共 {len(memory.dependencies)} 个）" if len(memory.dependencies) > 30 else ""
            lines.append(f"- 依赖：{deps}{more}")
        if memory.scripts:
            lines.append("- 脚本：" + ", ".join(f"`{k}`" for k in list(memory.scripts)[:12]))
        if memory.notes:
            lines.append("- 备注：" + "；".join(memory.notes))
        if memory.rules_source:
            lines.append("")
            lines.append(f"## 项目规则（来自 {memory.rules_source}）")
            lines.append("")
            lines.append(memory.rules.strip()[:4000])
        return "\n".join(lines)

    @staticmethod
    def rules_message(memory: ProjectMemory, *, markdown: str = "") -> Message:
        """把静态上下文包成 **pinned** 消息（裁剪时会尽量保持原文）。"""
        body = markdown or ProjectMemoryBuilder.to_markdown(memory)
        return Message(role="system", content=body, pinned=True)

    @staticmethod
    def static_path(workspace: Workspace) -> Path:
        """`.tcide/context/static.md` 的绝对路径（存在则复用，不重复全量传输）。"""
        return workspace.resolve(".tcide/context/static.md")

    def write_static(self, memory: ProjectMemory | None = None) -> Path:
        """把静态上下文落盘到 `.tcide/context/static.md`。"""
        memory = memory or self.build()
        return self.workspace.write(".tcide/context/static.md",
                                    ProjectMemoryBuilder.to_markdown(memory))

    def load_static(self) -> str:
        """读回已缓存的静态上下文（没有则空串）。"""
        return self._maybe_text(".tcide/context/static.md")
