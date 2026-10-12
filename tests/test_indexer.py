# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""indexer.py 的测试（符号表 / 调用图 / 依赖图 / 检索 / 切块）。"""

from __future__ import annotations


class TestScan:
    def test_scan_counts(self, index):
        stats = index.last_stats
        assert stats.files >= 6
        assert stats.symbols > 0
        assert stats.duration_ms >= 0

    def test_binary_and_oversize_skipped(self, index):
        assert index.file("assets/logo.png") is None

    def test_syntax_error_recorded_not_raised(self, index):
        broken = index.file("src/broken.py")
        assert broken is not None
        assert "SyntaxError" in broken.error
        assert index.last_stats.errors == 1

    def test_scan_is_repeatable(self, index):
        first = index.scan()
        second = index.scan()
        assert first.files == second.files


class TestPythonSymbols:
    def test_symbols_from_ast(self, index):
        symbols = {s.name: s for s in index.outline("src/app.py")}
        assert symbols["CONSTANT"].kind == "variable"
        assert symbols["Service"].kind == "class"
        assert symbols["start"].kind == "method"
        assert symbols["prepare"].kind == "method"
        assert symbols["helper"].kind == "function"
        assert symbols["fetch"].kind == "function"

    def test_symbols_are_precise_for_python(self, index):
        assert all(s.precise for s in index.outline("src/app.py"))

    def test_signature_extracted(self, index):
        symbols = {s.name: s for s in index.outline("src/app.py")}
        assert symbols["helper"].detail == "helper()"
        assert symbols["fetch"].detail == "fetch(path)"

    def test_end_line_present(self, index):
        symbols = {s.name: s for s in index.outline("src/app.py")}
        assert symbols["Service"].end_line > symbols["Service"].line

    def test_imports(self, index):
        assert index.file("src/app.py").imports == ["json", "pathlib"]

    def test_calls_collected(self, index):
        calls = set(index.file("src/app.py").calls)
        assert {"helper", "prepare", "dumps", "load"} <= calls

    def test_symbols_sorted_by_line(self, index):
        lines = [s.line for s in index.outline("src/app.py")]
        assert lines == sorted(lines)

    def test_find_symbol(self, index):
        found = index.find_symbol("helper")
        assert len(found) == 1 and found[0].path == "src/app.py"

    def test_symbols_filtered_by_kind(self, index):
        classes = index.symbols(kind="class")
        assert any(s.name == "Service" for s in classes)
        assert all(s.kind == "class" for s in classes)


class TestOtherLanguages:
    def test_typescript_symbols(self, index):
        symbols = {s.name: s for s in index.outline("web/main.ts")}
        assert symbols["Runner"].kind == "class"
        assert symbols["bootstrap"].kind == "function"
        assert symbols["Options"].kind == "interface"

    def test_typescript_is_marked_imprecise(self, index):
        assert all(not s.precise for s in index.outline("web/main.ts"))

    def test_go_symbols(self, index):
        symbols = {s.name: s for s in index.outline("main.go")}
        assert symbols["main"].kind == "function"
        assert symbols["Server"].kind == "struct"
        assert symbols["serve"].kind == "function"

    def test_typescript_imports_strings(self, workspace):
        workspace.write("web/util.ts", "import { a } from './other';\n")
        from tcide.indexer import ProjectIndex

        fresh = ProjectIndex(workspace)
        index = fresh.index_file("web/util.ts")
        assert "./other" in index.imports


class TestGraphs:
    def test_call_graph(self, index):
        graph = index.call_graph()
        assert "prepare" in graph["src/app.py::start"]
        assert graph["src/app.py::helper"] == []

    def test_call_graph_keys_do_not_collide(self, index):
        """回归：`start` 在 app.py 与 web/main.ts 都出现，不能互相覆盖。"""
        graph = index.call_graph()
        assert "src/app.py::start" in graph
        assert "web/main.ts::start" in graph
        assert graph["src/app.py::start"] != graph["web/main.ts::start"]

    def test_call_graph_keys_are_qualified(self, index):
        graph = index.call_graph()
        assert all("::" in key for key in graph)

    def test_call_graph_excludes_unknown_names(self, index):
        defined = {s.name for s in index.symbols()}
        for called in index.call_graph().values():
            assert all(name in defined for name in called)

    def test_dependency_graph_resolves_local_module(self, index):
        graph = index.dependency_graph()
        assert graph["src/util.py"] == ["src/app.py"]

    def test_dependency_graph_ignores_stdlib(self, index):
        graph = index.dependency_graph()
        assert graph["src/app.py"] == []

    def test_dependency_graph_covers_all_files(self, index):
        assert set(index.dependency_graph()) == set(index.files)


class TestSearchAndChunks:
    def test_symbol_search_scores_higher(self, index):
        hits = index.search("helper")
        assert hits
        assert hits[0].score >= 2.0
        assert hits[0].path == "src/app.py"

    def test_search_empty(self, index):
        assert index.search("") == []

    def test_search_limit(self, index):
        assert len(index.search("def", limit=3)) <= 3

    def test_chunks_by_symbol(self, index):
        chunks = index.chunks("src/app.py")
        names = {chunk.symbol for chunk in chunks}
        assert {"Service", "helper", "fetch"} <= names
        assert all(chunk.text for chunk in chunks)

    def test_chunks_fallback_window(self, index):
        chunks = index.chunks("web/main.ts", max_lines=3)
        assert chunks
        assert chunks[0].start == 1

    def test_chunks_unknown_file(self, index):
        assert index.chunks("assets/logo.png") == []

    def test_summary(self, index):
        summary = index.summary()
        assert summary["files"] >= 6
        assert summary["by_kind"]
        assert any(path == "src/broken.py" for path, _ in summary["errors"])
        assert summary["largest_files"]
