# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""cli.py 的测试：每个子命令都要真的能跑，并且退出码语义正确。"""

from __future__ import annotations

import json

import pytest

from tcide.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main


def run(capsys, *argv: str) -> tuple[int, str, str]:
    """执行 CLI，返回 (退出码, stdout, stderr)。"""
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestUsage:
    def test_no_command_is_usage_error(self, capsys):
        code, _out, err = run(capsys)
        assert code == EXIT_USAGE
        assert "usage" in err.lower()

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as info:
            main(["--version"])
        assert info.value.code == 0
        assert "tcide" in capsys.readouterr().out

    def test_help(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "check" in capsys.readouterr().out

    def test_unknown_command_is_usage_error(self, capsys):
        with pytest.raises(SystemExit) as info:
            main(["完全没有的命令"])
        assert info.value.code == EXIT_USAGE

    def test_unknown_project_dir(self, capsys):
        code, _out, err = run(capsys, "-C", "不存在的目录", "index")
        assert code == EXIT_FAILURE and "不存在" in err


class TestCheck:
    def test_finds_syntax_error_and_fails(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "check")
        assert code == EXIT_FAILURE
        assert "syntax-error" in out

    def test_clean_file_passes(self, capsys, project):
        code, _out, _err = run(capsys, "-C", str(project), "check", "src/app.py")
        assert code == EXIT_OK

    def test_strict_treats_warning_as_failure(self, capsys, project):
        (project / "warn.py").write_text("import os\n", encoding="utf-8")
        assert run(capsys, "-C", str(project), "check", "warn.py")[0] == EXIT_OK
        assert run(capsys, "-C", str(project), "check", "--strict", "warn.py")[0] \
            == EXIT_FAILURE

    def test_json_output_is_valid(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "check", "--json")
        data = json.loads(out)
        assert code == EXIT_FAILURE
        assert data["summary"]["total"] > 0
        assert any(i["rule"] == "syntax-error" for i in data["issues"])

    def test_directory_argument(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "check", "src")
        assert code == EXIT_FAILURE and "broken.py" in out

    def test_path_escape_rejected(self, capsys, project):
        code, _out, err = run(capsys, "-C", str(project), "check", "../外面.py")
        assert code == EXIT_FAILURE and "越界" in err


class TestLint:
    def test_rule_statistics(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "lint")
        assert code == EXIT_FAILURE
        assert "按规则" in out and "syntax-error" in out

    def test_json(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "lint", "--json")
        data = json.loads(out)
        assert set(data) == {"total", "by_severity", "by_rule", "errors"}
        assert data["errors"] >= 1


class TestAnalyze:
    def test_report_without_cycles_passes(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "analyze")
        assert code == EXIT_OK and "# 架构分析" in out

    def test_cycles_fail_the_command(self, capsys, project):
        for name, target in (("a", "b"), ("b", "c"), ("c", "a")):
            (project / "cyc").mkdir(exist_ok=True)
            (project / "cyc" / f"{name}.py").write_text(
                f"import {target}\n", encoding="utf-8")
        code, out, _err = run(capsys, "-C", str(project), "analyze")
        assert code == EXIT_FAILURE
        assert "循环依赖" in out

    def test_json_shape(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "analyze", "--json")
        data = json.loads(out)
        assert {"files", "edges", "cycles", "layers", "smells"} <= set(data)


class TestIndexAndSearch:
    def test_index(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "index")
        assert code == EXIT_OK and "语言分布" in out and "解析失败" in out

    def test_index_json(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "index", "--json")
        assert json.loads(out)["stats"]["files"] > 0

    def test_search_hit(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "search", "def helper")
        assert code == EXIT_OK and "src/app.py" in out

    def test_search_miss_returns_failure(self, capsys, project):
        assert run(capsys, "-C", str(project), "search", "zzz没有")[0] == EXIT_FAILURE

    def test_search_json(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "search", "helper", "--json")
        hits = json.loads(out)
        assert hits and {"path", "line", "text"} <= set(hits[0])

    def test_search_include_filter(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "search", "import",
                               "--include", "*.go")
        assert "app.py" not in out


class TestOutlineAndSnippets:
    def test_outline(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "outline", "src/app.py")
        assert code == EXIT_OK
        assert "Service" in out and "helper" in out

    def test_outline_marks_regex_inferred(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "outline", "web/main.ts")
        assert "正则推断" in out

    def test_outline_missing_symbols(self, capsys, project):
        (project / "empty.py").write_text("", encoding="utf-8")
        assert run(capsys, "-C", str(project), "outline", "empty.py")[0] == EXIT_FAILURE

    def test_snippet_list_counts(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "snippet")
        assert code == EXIT_OK and "共" in out and "python" in out

    def test_snippet_json_has_at_least_fifty(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "snippet", "--json")
        assert len(json.loads(out)) >= 50

    def test_snippet_render_with_placeholders(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "snippet", "pawwin",
                              "--set", "title=我的窗口", "--set", "w=800",
                              "--set", "h=600")
        assert code == EXIT_OK
        assert 'title="我的窗口"' in out and "800" in out

    def test_unknown_snippet(self, capsys, project):
        code, _out, err = run(capsys, "-C", str(project), "snippet", "没有这个")
        assert code == EXIT_FAILURE and "没有这个片段" in err


class TestCompleteAndHex:
    def test_complete(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "complete", "hel",
                              "--file", "src/app.py")
        assert code == EXIT_OK and "helper" in out

    def test_complete_nothing(self, capsys, project):
        assert run(capsys, "-C", str(project), "complete", "zzzz")[0] == EXIT_FAILURE

    def test_hex_dump(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "hex", "main.py",
                              "--rows", "2")
        assert code == EXIT_OK and "00000000" in out and "|" in out

    def test_hex_info(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "hex", "assets/logo.png",
                              "--info")
        assert code == EXIT_OK
        data = json.loads(out)
        assert "PNG" in data["kind"] and data["is_text"] is False

    def test_hex_find(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "hex", "main.py",
                              "--find", "print")
        assert code == EXIT_OK and out.strip()

    def test_hex_find_miss(self, capsys, project):
        assert run(capsys, "-C", str(project), "hex", "main.py",
                   "--find", "绝不存在")[0] == EXIT_FAILURE


class TestGitCommands:
    def test_non_repo(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "git", "status")
        assert code == EXIT_FAILURE and "不是 git 仓库" in out

    def test_status_on_repo(self, capsys, git_repo):
        (git_repo.path / "new.txt").write_text("x", encoding="utf-8")
        code, out, _err = run(capsys, "-C", str(git_repo.path), "git", "status")
        assert code == EXIT_OK and "分支 main" in out and "new.txt" in out

    def test_suggest(self, capsys, git_repo):
        (git_repo.path / "new.py").write_text("x = 1\n", encoding="utf-8")
        code, out, _err = run(capsys, "-C", str(git_repo.path), "git", "suggest")
        assert code == EXIT_OK and out.strip().startswith("feat")

    def test_suggest_json(self, capsys, git_repo):
        (git_repo.path / "new.py").write_text("x = 1\n", encoding="utf-8")
        _code, out, _err = run(capsys, "-C", str(git_repo.path), "git", "suggest",
                               "--json")
        assert json.loads(out)["type"] == "feat"

    def test_diff(self, capsys, git_repo):
        (git_repo.path / "src" / "app.py").write_text("value = 9\n", encoding="utf-8")
        code, out, _err = run(capsys, "-C", str(git_repo.path), "git", "diff")
        assert code == EXIT_OK and "value = 9" in out

    def test_report(self, capsys, git_repo):
        code, out, _err = run(capsys, "-C", str(git_repo.path), "git", "report")
        assert code == EXIT_OK and "建议提交信息" in out


class TestHeal:
    TRACEBACK = ('Traceback (most recent call last):\n'
                 '  File "src/app.py", line 9, in main\n'
                 "    return helperr()\n"
                 "NameError: name 'helperr' is not defined\n")

    def test_reads_file(self, capsys, project):
        (project / "err.log").write_text(self.TRACEBACK, encoding="utf-8")
        code, out, _err = run(capsys, "-C", str(project), "heal", "err.log")
        assert code == EXIT_FAILURE
        assert "helperr" in out and "helper" in out

    def test_json(self, capsys, project):
        (project / "err.log").write_text(self.TRACEBACK, encoding="utf-8")
        _code, out, _err = run(capsys, "-C", str(project), "heal", "err.log", "--json")
        data = json.loads(out)
        assert data[0]["error"]["kind"] == "name"
        assert data[0]["suggestions"]

    def test_clean_output_exits_ok(self, capsys, project):
        (project / "ok.log").write_text("一切都好\n", encoding="utf-8")
        assert run(capsys, "-C", str(project), "heal", "ok.log")[0] == EXIT_OK


class TestOtherCommands:
    def test_memory(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "memory")
        assert code == EXIT_OK and "项目静态上下文" in out

    def test_memory_json(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "memory", "--json")
        assert json.loads(out)["name"] == "my-app"

    def test_debug_defaults(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "debug", "defaults")
        assert code == EXIT_OK
        assert json.loads(out)["program"].endswith(".py")

    def test_debug_breakpoints(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "debug", "breakpoints",
                              "--set", "src/app.py:10")
        assert code == EXIT_OK and "第 10 行" in out

    def test_debug_list_empty(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "debug", "list")
        assert code == EXIT_OK and "没有启动配置" in out

    def test_perf(self, capsys, project):
        code, out, _err = run(capsys, "-C", str(project), "perf", "--measure",
                              "src/app.py")
        assert code == EXIT_OK and "性能快照" in out

    def test_perf_json(self, capsys, project):
        _code, out, _err = run(capsys, "-C", str(project), "perf", "--json")
        data = json.loads(out)
        assert {"python_mb", "usage_percent", "is_critical", "trend"} <= set(data)


class TestNoPawuiDependency:
    def test_cli_does_not_import_pawui(self):
        """CLI 属于库侧，必须守住 LGPL 边界。"""
        import ast
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "tcide" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                banned |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                banned.add(node.module.split(".")[0])
        assert "pawui" not in banned
        assert "PySide6" not in banned
