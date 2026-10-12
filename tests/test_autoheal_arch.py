# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""autoheal.py 与 arch.py 的测试。"""

from __future__ import annotations

from tcide.arch import ArchAnalyzer
from tcide.autoheal import AutoHeal, ErrorParser
from tcide.indexer import ProjectIndex
from tcide.workspace import Workspace

PY_TRACEBACK = '''
Traceback (most recent call last):
  File "src/app.py", line 12, in <module>
    main()
  File "src/app.py", line 9, in main
    return helperr()
NameError: name 'helperr' is not defined
'''

PY_SYNTAX = '''
  File "src/broken.py", line 1
    def broken(:
               ^
SyntaxError: invalid syntax
'''

PY_IMPORT = '''
Traceback (most recent call last):
  File "main.py", line 3, in <module>
    import numpyx
ModuleNotFoundError: No module named 'numpyx'
'''

PYTEST_OUT = '''
=========================== short test summary info ============================
FAILED tests/test_app.py::test_total - AssertionError: assert 1 == 2
ERROR tests/test_app.py::test_setup
1 failed, 1 error
'''

TSC_OUT = """src/main.ts(12,5): error TS2345: Argument of type 'string' is not assignable to parameter of type 'number'.
"""

GCC_OUT = """src/core.c:42:7: error: 'undeclared_var' undeclared (first use in this function)
src/core.c:50:1: warning: unused variable 'x'
"""


class TestErrorParser:
    def test_python_name_error(self):
        errors = ErrorParser().parse(PY_TRACEBACK)
        assert errors, "没有解析出任何错误"
        error = errors[-1]
        assert error.kind == "name"
        assert error.file == "src/app.py"
        assert error.line == 9            # 最后一个帧（调用点）
        assert error.symbol == "helperr"
        assert "NameError" in error.message

    def test_python_syntax_error_column(self):
        error = ErrorParser().parse(PY_SYNTAX)[-1]
        assert error.kind == "syntax"
        assert error.file == "src/broken.py"
        assert error.column >= 1, "语法错误应当给出列号（来自插入符）"

    def test_python_import_error(self):
        error = ErrorParser().parse(PY_IMPORT)[-1]
        assert error.kind == "import"
        assert error.symbol == "numpyx"

    def test_pytest_failures(self):
        errors = ErrorParser().parse(PYTEST_OUT)
        kinds = {e.kind for e in errors}
        assert kinds == {"test"}
        files = {e.file for e in errors}
        assert files == {"tests/test_app.py"}
        assert any(e.code == "test_total" for e in errors)

    def test_typescript_error(self):
        error = ErrorParser().parse(TSC_OUT)[0]
        assert error.file == "src/main.ts"
        assert (error.line, error.column) == (12, 5)
        assert error.code == "TS2345"
        assert error.kind == "compile"

    def test_gcc_error_and_warning_filtered(self):
        errors = ErrorParser().parse(GCC_OUT)
        assert len(errors) == 1, "warning 不应被当成 error"
        assert errors[0].file == "src/core.c"
        assert errors[0].line == 42
        assert errors[0].column == 7

    def test_dedupe(self):
        assert len(ErrorParser().parse(TSC_OUT + TSC_OUT)) == 1

    def test_empty_and_garbage(self):
        parser = ErrorParser()
        assert parser.parse("") == []
        assert parser.parse("一切都好") == []

    def test_location_string(self):
        error = ErrorParser().parse(GCC_OUT)[0]
        assert error.location() == "src/core.c:42:7"

    def test_to_dict(self):
        assert set(ErrorParser().parse(TSC_OUT)[0].to_dict()) >= {
            "kind", "file", "line", "column", "message", "symbol", "code"}


class TestAutoHeal:
    def test_suggests_close_symbol_from_index(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        heal = AutoHeal(index, workspace)
        diagnosis = heal.analyze(PY_TRACEBACK)[0]
        assert any("helper" in s.action for s in diagnosis.suggestions), \
            f"没有给出相似符号建议: {[s.action for s in diagnosis.suggestions]}"
        assert any(s.source == "index" for s in diagnosis.suggestions)

    def test_missing_module_advice(self, workspace):
        diagnosis = AutoHeal(None, workspace).analyze(PY_IMPORT)[0]
        assert any("安装依赖" in s.action for s in diagnosis.suggestions)

    def test_existing_module_advice(self, workspace):
        workspace.write("mymod.py", "value = 1\n")
        output = ('Traceback (most recent call last):\n  File "x.py", line 1, in <module>\n'
                  "    import mymod\nModuleNotFoundError: No module named 'mymod'\n")
        diagnosis = AutoHeal(None, workspace).analyze(output)[0]
        assert any("项目内存在" in s.action for s in diagnosis.suggestions)

    def test_syntax_advice_mentions_column(self):
        diagnosis = AutoHeal(None, None).analyze(PY_SYNTAX)[0]
        assert any("列" in s.action for s in diagnosis.suggestions)

    def test_test_advice_gives_command(self, workspace):
        diagnosis = AutoHeal(None, workspace).analyze(PYTEST_OUT)[0]
        assert any("pytest -q" in s.action for s in diagnosis.suggestions)

    def test_every_error_gets_at_least_one_suggestion(self):
        heal = AutoHeal(None, None)
        for output in (PY_TRACEBACK, PY_SYNTAX, PY_IMPORT, PYTEST_OUT, TSC_OUT, GCC_OUT):
            for diagnosis in heal.analyze(output):
                assert diagnosis.suggestions, f"{output[:20]} 没有建议"

    def test_confidence_within_range(self):
        for diagnosis in AutoHeal(None, None).analyze(GCC_OUT):
            for suggestion in diagnosis.suggestions:
                assert 0.0 <= suggestion.confidence <= 1.0

    def test_report_markdown(self, workspace):
        report = AutoHeal(None, workspace).report(PYTEST_OUT)
        assert "解析出" in report and "pytest" in report

    def test_report_without_errors(self):
        assert "没有" in AutoHeal(None, None).report("干净")

    def test_dominant_indent(self, workspace):
        workspace.write("ind.py", "def f():\n    a = 1\n    b = 2\n")
        assert AutoHeal(None, workspace).dominant_indent("ind.py") == "    "

    def test_diagnosis_serializable(self):
        diagnosis = AutoHeal(None, None).analyze(PYTEST_OUT)[0]
        data = diagnosis.to_dict()
        assert "error" in data and "suggestions" in data


# ---------------------------------------------------------------------------
# 架构分析
# ---------------------------------------------------------------------------

def make_cycle_project(workspace: Workspace) -> None:
    """构造 a→b→c→a 的环，外加一个自引用与一个孤立文件。"""
    workspace.write("cyc/a.py", "import b\n\n\ndef fa():\n    return 1\n")
    workspace.write("cyc/b.py", "import c\n\n\ndef fb():\n    return 1\n")
    workspace.write("cyc/c.py", "import a\n\n\ndef fc():\n    return 1\n")
    workspace.write("selfref.py", "import selfref\n\n\ndef fs():\n    return 1\n")
    workspace.write("solo.py", "def only():\n    return 1\n")


class TestArchCycles:
    def test_detects_three_file_cycle(self, workspace):
        make_cycle_project(workspace)
        index = ProjectIndex(workspace)
        index.scan()
        cycles = ArchAnalyzer(index).cycles()
        merged = {tuple(group) for group in cycles}
        assert ("cyc/a.py", "cyc/b.py", "cyc/c.py") in merged, \
            f"没检出环: {cycles}"

    def test_no_false_positive_on_acyclic_part(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        cycles = ArchAnalyzer(index).cycles()
        flat = {path for group in cycles for path in group}
        assert "src/app.py" not in flat
        assert "src/util.py" not in flat

    def test_self_loop_branch(self, workspace):
        """自环分支：dependency_graph 会过滤掉自引用，这里直接喂一个自环图来覆盖。"""
        index = ProjectIndex(workspace)
        index.scan()
        index.dependency_graph = lambda: {"x.py": ["x.py"]}    # type: ignore[assignment]
        cycles = ArchAnalyzer(index).cycles()
        assert cycles == [["x.py"]]

    def test_acyclic_flag(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        assert ArchAnalyzer(index).analyze().acyclic is True


class TestArchGraph:
    def test_layers_put_dependency_below(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        layers = ArchAnalyzer(index).layers()
        position = {path: number for number, group in enumerate(layers) for path in group}
        # util 依赖 app，所以 util 的层号更大（第 0 层 = 不依赖任何项目内文件）
        assert position["src/util.py"] > position["src/app.py"]
        assert position["src/app.py"] == 0

    def test_fan_in_and_fan_out(self, workspace):
        workspace.write("solo.py", "def only():\n    return 1\n")
        index = ProjectIndex(workspace)
        index.scan()
        analyzer = ArchAnalyzer(index)
        assert analyzer.fan_in()["src/app.py"] == 1
        assert analyzer.fan_out()["src/util.py"] == 1
        assert analyzer.fan_out()["solo.py"] == 0

    def test_orphans(self, workspace):
        make_cycle_project(workspace)
        index = ProjectIndex(workspace)
        index.scan()
        orphans = ArchAnalyzer(index).orphans()
        assert "solo.py" in orphans
        assert "src/app.py" not in orphans

    def test_report_contains_sections(self, workspace):
        make_cycle_project(workspace)
        index = ProjectIndex(workspace)
        index.scan()
        report = ArchAnalyzer(index).report()
        assert "# 架构分析" in report
        assert "## 循环依赖" in report
        assert "## 分层" in report


class TestArchSmells:
    def test_god_file_detected(self, workspace):
        workspace.write("big.py", "\n".join(f"v{i} = {i}" for i in range(50)) + "\n")
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index, god_lines=10).smells()
        assert any(s.kind == "god-file" and s.path == "big.py" for s in smells)

    def test_long_function_detected(self, workspace):
        body = "\n".join(f"    x{i} = {i}" for i in range(30))
        workspace.write("long.py", f"def big():\n{body}\n")
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index, long_function=10).smells()
        assert any(s.kind == "long-function" for s in smells)

    def test_parse_error_smell(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index).smells()
        assert any(s.kind == "parse-error" and s.path == "src/broken.py" for s in smells)

    def test_cycle_smell(self, workspace):
        make_cycle_project(workspace)
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index).smells()
        assert any(s.kind == "dependency-cycle" and s.severity == "high"
                   for s in smells)

    def test_high_fan_in_smell(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        analyzer = ArchAnalyzer(index)
        analyzer.fan_in = lambda: {"src/app.py": 9}      # type: ignore[assignment]
        assert any(s.kind == "high-fan-in" for s in analyzer.smells())

    def test_deep_path_smell(self, workspace):
        workspace.write("a/b/c/d/e/f.py", "value = 1\n")
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index, deep_depth=3).smells()
        assert any(s.kind == "deep-path" for s in smells)

    def test_smells_sorted_by_severity(self, workspace):
        make_cycle_project(workspace)
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index).smells()
        rank = {"high": 0, "medium": 1, "low": 2}
        values = [rank[s.severity] for s in smells]
        assert values == sorted(values)

    def test_smell_serializable(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        smells = ArchAnalyzer(index).smells()
        assert smells and set(smells[0].to_dict()) >= {"kind", "path", "detail",
                                                      "metric", "severity"}

    def test_report_and_summary(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        report = ArchAnalyzer(index).analyze()
        assert "循环依赖" in report.summary()
        assert "files" in report.to_dict()
