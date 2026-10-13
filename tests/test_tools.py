# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""tools.py 的测试：12 个工具 + 安全约束 + 接线自检。"""

from __future__ import annotations

import shutil

import pytest

from tcide.gitintel import GitIntelligence
from tcide.gitops import GitRepo
from tcide.indexer import ProjectIndex
from tcide.tools import CLAIMED_TOOLS, DANGEROUS_PATTERNS, ToolRegistry
from tcide.workspace import Workspace

pytestmark = pytest.mark.skipif(
    not GitRepo(".").available(), reason="环境没有可用的 git")

HAS_PYTHON = shutil.which("python") is not None


@pytest.fixture
def registry(workspace):
    index = ProjectIndex(workspace)
    index.scan()
    return ToolRegistry(workspace, index=index, command_timeout=30)


class TestRegistryShape:
    def test_all_claimed_tools_present(self, registry):
        """README 里点名的工具必须全都在（原版最大的问题是实现了却没接线）。"""
        missing = set(CLAIMED_TOOLS) - set(registry.names())
        assert not missing, f"缺少工具: {missing}"

    def test_extra_useful_tools(self, registry):
        assert {"edit_file", "get_outline"} <= set(registry.names())

    def test_every_tool_has_schema_and_handler(self, registry):
        for name in registry.names():
            spec = registry.tools[name]
            assert spec.description.strip(), f"{name} 缺少描述（模型靠它选工具）"
            assert spec.parameters["type"] == "object"
            assert callable(spec.handler)

    def test_openai_spec_shape(self, registry):
        for spec in registry.specs():
            assert spec["type"] == "function"
            assert set(spec["function"]) >= {"name", "description", "parameters"}

    def test_anthropic_spec_shape(self, registry):
        for spec in registry.anthropic_specs():
            assert set(spec) >= {"name", "description", "input_schema"}

    def test_dangerous_tool_flagged(self, registry):
        assert registry.tools["run_command"].dangerous is True

    def test_calls_are_recorded(self, registry):
        """接线自检：调用必须被记录，否则无法证明功能真的连上了。"""
        registry.call("list_files", {})
        assert registry.calls[-1]["name"] == "list_files"
        assert registry.calls[-1]["ok"] is True


class TestReadWrite:
    def test_read_file(self, registry):
        assert "def helper" in registry.call("read_file", {"path": "src/app.py"}).output

    def test_read_file_range(self, registry):
        output = registry.call("read_file",
                               {"path": "src/app.py", "start_line": 1,
                                "end_line": 3}).output
        assert len(output.splitlines()) == 3

    def test_read_file_invalid_range(self, registry):
        result = registry.call("read_file",
                               {"path": "src/app.py", "start_line": 500,
                                "end_line": 600})
        assert not result.ok and "行范围非法" in result.error

    def test_read_file_escape_rejected(self, registry):
        result = registry.call("read_file", {"path": "../outside.py"})
        assert not result.ok and "越界" in result.error

    def test_write_then_read(self, registry):
        assert registry.call("write_file",
                             {"path": "src/new.py", "content": "x = 1\n"}).ok
        assert registry.call("read_file", {"path": "src/new.py"}).output == "x = 1\n"

    def test_write_creates_parents(self, registry):
        registry.call("write_file", {"path": "a/b/c.txt", "content": "ok"})
        assert registry.workspace.exists("a/b/c.txt")

    def test_write_escape_rejected(self, registry):
        assert not registry.call("write_file",
                                 {"path": "../evil.txt", "content": "x"}).ok

    def test_edit_file_single(self, registry):
        result = registry.call("edit_file", {"path": "src/app.py",
                                            "search": "CONSTANT = 42",
                                            "replace": "CONSTANT = 7"})
        assert result.ok
        assert "CONSTANT = 7" in registry.call("read_file", {"path": "src/app.py"}).output

    def test_edit_file_all(self, registry):
        registry.call("write_file", {"path": "t.txt", "content": "a a a"})
        result = registry.call("edit_file", {"path": "t.txt", "search": "a",
                                            "replace": "b", "count": 0})
        assert "3 处" in result.output
        assert registry.call("read_file", {"path": "t.txt"}).output == "b b b"

    def test_edit_file_not_found(self, registry):
        result = registry.call("edit_file", {"path": "src/app.py",
                                            "search": "不存在的内容",
                                            "replace": "x"})
        assert not result.ok and "找不到" in result.error

    def test_edit_file_empty_search(self, registry):
        assert not registry.call("edit_file", {"path": "src/app.py",
                                               "search": "", "replace": "x"}).ok


class TestListingAndSearch:
    def test_list_files(self, registry):
        output = registry.call("list_files", {"depth": 2}).output
        assert "src/" in output and "main.py" in output

    def test_list_files_depth(self, registry):
        shallow = registry.call("list_files", {"depth": 1}).output
        assert "src/" in shallow
        assert "app.py" not in shallow

    def test_search_code(self, registry):
        output = registry.call("search_code", {"query": "def helper"}).output
        assert "src/app.py" in output

    def test_search_code_regex(self, registry):
        output = registry.call("search_code", {"query": r"def \w+\(",
                                               "regex": True}).output
        assert output.count(":") >= 2

    def test_search_code_include(self, registry):
        output = registry.call("search_code", {"query": "import",
                                               "include": "*.go"}).output
        assert "app.py" not in output

    def test_search_code_no_hits(self, registry):
        assert registry.call("search_code",
                             {"query": "zzz-not-there"}).output == "（无命中）"


class TestRunCommand:
    def test_success(self, registry):
        result = registry.call("run_command", {"command": "echo hello"})
        assert result.ok and "hello" in result.output

    def test_exit_code_reported(self, registry):
        result = registry.call("run_command", {"command": "exit 3"})
        assert not result.ok and result.meta["exit_code"] == 3

    def test_stderr_captured(self, registry):
        result = registry.call("run_command",
                               {"command": 'python -c "import sys; sys.stderr.write(\'E\')"'})
        assert "E" in result.output

    @pytest.mark.parametrize("command", [
        "rm -rf /", "mkfs.ext4 /dev/sda", "shutdown -h now",
        ":(){ :|:& };:", "git push --force origin main", "git reset --hard",
    ])
    def test_dangerous_commands_blocked(self, registry, command):
        result = registry.call("run_command", {"command": command})
        assert not result.ok and "拒绝执行危险命令" in result.error

    def test_dangerous_allowed_when_opted_in(self, workspace):
        permissive = ToolRegistry(workspace, allow_dangerous=True)
        # 仍然会因为命令本身失败而 ok=False，但**不是**被策略拒绝
        result = permissive.call("run_command", {"command": "git reset --hard"})
        assert "拒绝执行危险命令" not in result.error

    def test_cwd_escape_rejected(self, registry):
        result = registry.call("run_command", {"command": "echo x", "cwd": "../.."})
        assert not result.ok and "越界" in result.error

    def test_empty_command_rejected(self, registry):
        assert not registry.call("run_command", {"command": "   "}).ok

    @pytest.mark.skipif(not HAS_PYTHON, reason="需要 python 可执行文件")
    def test_timeout(self, registry):
        result = registry.call("run_command", {
            "command": 'python -c "import time; time.sleep(5)"', "timeout": 1})
        assert not result.ok and "超时" in result.error

    def test_dangerous_patterns_are_documented(self):
        """每条黑名单都要有中文理由，便于向用户解释为什么被拒。"""
        assert DANGEROUS_PATTERNS
        assert all(reason.strip() for _, reason in DANGEROUS_PATTERNS)


class TestOutputLimit:
    @pytest.mark.skipif(not HAS_PYTHON, reason="需要 python 可执行文件")
    def test_long_output_is_truncated(self, workspace):
        small = ToolRegistry(workspace, max_output=300)
        result = small.call("run_command", {"command": 'python -c "print(\'x\'*5000)"'})
        assert result.truncated
        assert "已截断" in result.output

    def test_read_large_output_truncated(self, workspace):
        workspace.write("big.txt", "y" * 10000)
        small = ToolRegistry(workspace, max_output=200)
        result = small.call("read_file", {"path": "big.txt"})
        assert result.truncated and len(result.output) < 1000


class TestDiagnostics:
    def test_finds_python_syntax_error(self, registry):
        output = registry.call("get_diagnostics", {"path": "src/broken.py"}).output
        assert "src/broken.py" in output and "问题" in output

    def test_reports_clean_file(self, registry):
        output = registry.call("get_diagnostics", {"path": "src/app.py"}).output
        assert "未发现问题" in output

    def test_scan_all_files(self, registry):
        output = registry.call("get_diagnostics", {}).output
        assert "broken.py" in output


class TestOutlineAndOpenFiles:
    def test_get_outline(self, registry):
        output = registry.call("get_outline", {"path": "src/app.py"}).output
        assert "Service" in output and "helper" in output

    def test_get_outline_without_index(self, workspace):
        registry = ToolRegistry(workspace)
        assert not registry.call("get_outline", {"path": "src/app.py"}).ok

    def test_open_files_injected(self, workspace):
        registry = ToolRegistry(workspace, open_files=lambda: ["a.py", "b.py"])
        assert "a.py" in registry.call("get_open_files", {}).output

    def test_open_files_absent(self, registry):
        assert "未提供" in registry.call("get_open_files", {}).output


class TestGitTools:
    def test_git_status_on_repo(self, git_repo):
        registry = ToolRegistry(Workspace(git_repo.path), repo=git_repo)
        output = registry.call("git_status", {}).output
        assert "分支: main" in output

    def test_git_status_on_non_repo(self, plain_dir):
        registry = ToolRegistry(Workspace(plain_dir), repo=GitRepo(plain_dir))
        assert "不可用" in registry.call("git_status", {}).output

    def test_git_diff(self, git_repo):
        (git_repo.path / "src" / "app.py").write_text("value = 42\n", encoding="utf-8")
        registry = ToolRegistry(Workspace(git_repo.path), repo=git_repo)
        output = registry.call("git_diff", {}).output
        assert "value = 42" in output

    def test_git_diff_no_changes(self, git_repo):
        registry = ToolRegistry(Workspace(git_repo.path), repo=git_repo)
        assert registry.call("git_diff", {}).output == "（无差异）"


class TestErrorHandling:
    def test_unknown_tool(self, registry):
        result = registry.call("nope", {})
        assert not result.ok and "未知工具" in result.error

    def test_wrong_argument_type(self, registry):
        result = registry.call("read_file", {"path": "src/app.py",
                                             "start_line": "abc"})
        assert not result.ok

    def test_unexpected_argument(self, registry):
        result = registry.call("read_file", {"path": "src/app.py", "bogus": 1})
        assert not result.ok and "参数不匹配" in result.error

    def test_missing_file(self, registry):
        result = registry.call("read_file", {"path": "nope.py"})
        assert not result.ok and "不是文件" in result.error

    def test_result_as_text(self, registry):
        assert registry.call("list_files", {}).as_text()
        assert registry.call("read_file", {"path": "nope"}).as_text().startswith("ERROR")


class TestAgentWorkflow:
    """把工具串起来跑一遍 Agent 的典型流程（也是接线验证）。"""

    def test_read_edit_verify_commit(self, git_repo):
        workspace = Workspace(git_repo.path)
        index = ProjectIndex(workspace)
        index.scan()
        registry = ToolRegistry(workspace, index=index, repo=git_repo)

        assert "value = 1" in registry.call("read_file", {"path": "src/app.py"}).output
        assert registry.call("edit_file", {"path": "src/app.py",
                                          "search": "value = 1",
                                          "replace": "value = 2"}).ok
        assert "value = 2" in registry.call("git_diff", {}).output
        assert "未发现问题" in registry.call("get_diagnostics",
                                            {"path": "src/app.py"}).output

        # 提交建议应能识别出这是 fix
        suggestion = GitIntelligence(git_repo, index).suggest_commit()
        assert suggestion.type == "fix"
        assert registry.call("run_command", {"command": "git add -A"}).ok
        assert registry.call("run_command",
                             {"command": f'git commit -m "{suggestion.message.splitlines()[0]}"'}).ok
        assert git_repo.is_clean()
