# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""memory.py 的测试（项目探测 / 静态上下文档 / 规则注入）。"""

from __future__ import annotations

from tcide.memory import ProjectMemoryBuilder, RULE_FILES


class TestDetection:
    def test_name_from_package_json(self, memory):
        assert memory.name == "my-app"

    def test_scripts_from_package_json(self, memory):
        assert memory.scripts["build"] == "vite build"
        assert memory.scripts["test"] == "vitest"

    def test_commands(self, memory):
        assert memory.build_command == "npm run build"
        assert memory.test_command == "npm test"
        assert memory.run_command == "npm run dev"

    def test_package_manager(self, memory):
        assert memory.package_manager == "npm"

    def test_frameworks_detected(self, memory):
        assert {"React", "Electron", "Vite", "FastAPI", "PawUI", "pytest"} <= set(
            memory.frameworks)

    def test_dependencies_merged_and_deduped(self, memory):
        lowered = [d.lower() for d in memory.dependencies]
        assert "fastapi" in lowered and "pawui" in lowered and "react" in lowered
        assert lowered.count("fastapi") == 1
        assert not any(d.startswith("-r") for d in memory.dependencies)

    def test_primary_language(self, memory):
        assert memory.primary_language == "python"

    def test_entry_point_from_candidates(self, memory):
        assert "main.py" in memory.entry_points

    def test_entry_point_from_pyproject_script(self, memory):
        assert "app:main" in memory.entry_points

    def test_rules_loaded_with_source(self, memory):
        assert "先澄清，再实现" in memory.rules
        assert memory.rules_source == "CLAUDE.md"

    def test_rules_priority_order(self, workspace):
        workspace.write(".tcide/context/static.md", "# 静态上下文\n")
        # CLAUDE.md 仍在，优先级更高
        built = ProjectMemoryBuilder(workspace).build()
        assert built.rules_source == "CLAUDE.md"
        assert RULE_FILES[0] == "CLAUDE.md"


class TestMarkdown:
    def test_markdown_sections(self, memory):
        text = ProjectMemoryBuilder.to_markdown(memory)
        assert "# 项目静态上下文：my-app" in text
        assert "主要语言：python" in text
        assert "包管理：npm" in text
        assert "构建：`npm run build`" in text
        assert "测试：`npm test`" in text
        assert "项目规则（来自 CLAUDE.md）" in text
        assert "先澄清，再实现" in text

    def test_markdown_is_compact(self, memory):
        text = ProjectMemoryBuilder.to_markdown(memory)
        assert len(text) < 6000, "静态上下文必须紧凑，否则白占 token"

    def test_rules_message_is_pinned(self, memory):
        from tcide.trimmer import ContextTrimmer

        message = ProjectMemoryBuilder.rules_message(memory)
        assert message.pinned and message.role == "system"
        # pinned 消息在小预算下也要尽量保持原文
        trimmer = ContextTrimmer(budget=600, keep_recent=1)
        trimmed, _ = trimmer.trim([message])
        assert trimmed and "先澄清，再实现" in trimmed[0].content


class TestStaticCache:
    def test_write_and_load_static(self, workspace):
        builder = ProjectMemoryBuilder(workspace)
        path = builder.write_static()
        assert path.name == "static.md"
        assert workspace.exists(".tcide/context/static.md")
        cached = builder.load_static()
        assert "项目静态上下文" in cached

    def test_load_static_when_absent(self, workspace):
        assert ProjectMemoryBuilder(workspace).load_static() == ""

    def test_static_path_is_inside_workspace(self, workspace):
        path = ProjectMemoryBuilder.static_path(workspace)
        assert str(path).startswith(str(workspace.root))


class TestSerialization:
    def test_to_dict(self, memory):
        data = memory.to_dict()
        assert data["name"] == "my-app"
        assert isinstance(data["frameworks"], list)
        assert "root" in data


class TestEmptyProject:
    def test_bare_directory_does_not_crash(self, project):
        """空目录（无任何清单文件）也必须能构建。"""
        import shutil

        from tcide.workspace import Workspace

        for item in project.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        bare = Workspace(project)
        memory = ProjectMemoryBuilder(bare).build()
        assert memory.name
        assert memory.dependencies == []
        text = ProjectMemoryBuilder.to_markdown(memory)
        assert "项目静态上下文" in text
