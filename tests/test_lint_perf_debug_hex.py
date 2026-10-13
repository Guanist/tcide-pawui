# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""lint / perf / debug / hexview 四个模块的测试。"""

from __future__ import annotations

import time

import pytest

from tcide.debug import (
    COMMANDS,
    DebugConfigStore,
    DebugSession,
    LaunchConfig,
    load_breakpoints,
)
from tcide.hexview import DEFAULT_WIDTH, HexLine, HexViewer
from tcide.lint import RULES, SEVERITIES, Issue, Linter
from tcide.perf import PerfMonitor, Sample, Timer


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

MESSY_PY = '''import os
import json


def f(a, b=[]):
    if a == None:
        pass
    try:
        return json.dumps(b)
    except:
        return ""


# TODO: 清理这里
'''

LONG_LINE = "x = " + "1" * 200 + "\n"
CONFLICT = "<<<<<<< HEAD\ny = 1\n=======\n"


class TestLinterText:
    def test_long_line(self):
        issues = Linter().lint_text("a.py", LONG_LINE)
        assert any(i.rule == "line-too-long" for i in issues)

    def test_trailing_whitespace(self):
        issues = Linter().lint_text("a.txt", "x = 1   \n")
        assert any(i.rule == "trailing-whitespace" for i in issues)

    def test_conflict_marker_is_error(self):
        issues = Linter().lint_text("a.py", CONFLICT)
        conflict = [i for i in issues if i.rule == "conflict-marker"]
        assert conflict and conflict[0].severity == "error"

    def test_missing_final_newline(self):
        assert any(i.rule == "missing-final-newline"
                   for i in Linter().lint_text("a.txt", "no newline"))

    def test_todo_marker(self):
        issues = Linter().lint_text("a.py", "# TODO: 稍后处理\n")
        todo = [i for i in issues if i.rule == "todo-marker"]
        assert todo and todo[0].column > 0

    def test_mixed_indent(self):
        text = "def f():\n\ta = 1\n    b = 2\n"
        assert any(i.rule == "mixed-indent" for i in Linter().lint_text("a.py", text))

    def test_clean_file_has_no_issues(self):
        text = "value = 1\n\n\ndef f():\n    return value\n"
        assert Linter().lint_text("a.py", text) == []


class TestLinterPython:
    def test_syntax_error(self):
        issues = Linter().lint_text("bad.py", "def broken(:\n")
        assert issues and issues[0].rule == "syntax-error"
        assert issues[0].severity == "error"
        assert issues[0].line == 1

    def test_bare_except(self):
        assert any(i.rule == "bare-except"
                   for i in Linter().lint_text("a.py", MESSY_PY))

    def test_mutable_default(self):
        assert any(i.rule == "mutable-default"
                   for i in Linter().lint_text("a.py", MESSY_PY))

    def test_none_comparison(self):
        assert any(i.rule == "none-comparison"
                   for i in Linter().lint_text("a.py", MESSY_PY))

    def test_unused_import(self):
        issues = Linter().lint_text("a.py", "import os\nimport json\n\njson.dumps(1)\n")
        unused = [i for i in issues if i.rule == "unused-import"]
        assert [i.symbol for i in unused] == ["os"]

    def test_used_import_not_flagged(self):
        issues = Linter().lint_text("a.py", "import os\n\nprint(os.name)\n")
        assert not [i for i in issues if i.rule == "unused-import"]

    def test_all_export_counts_as_used(self):
        text = "from a import b\n\n__all__ = ['b']\n"
        assert not [i for i in Linter().lint_text("a.py", text)
                    if i.rule == "unused-import"]

    def test_no_python_rules_for_other_languages(self):
        issues = Linter().lint_text("a.ts", "import os\n")
        assert not [i for i in issues if i.rule == "unused-import"]

    def test_issues_sorted_by_severity(self):
        issues = Linter().lint_text("a.py", MESSY_PY + LONG_LINE + CONFLICT)
        ranks = [SEVERITIES.index(i.severity) for i in issues]
        assert ranks == sorted(ranks)


class TestLinterWorkspace:
    def test_lint_file_and_all(self, workspace):
        linter = Linter(workspace)
        issues = linter.lint_file("src/broken.py")
        assert issues and issues[0].rule == "syntax-error"
        everything = linter.lint_all()
        assert "src/broken.py" in everything

    def test_binary_skipped(self, workspace):
        assert Linter(workspace).lint_file("assets/logo.png") == []

    def test_requires_workspace(self):
        with pytest.raises(ValueError):
            Linter().lint_file("a.py")
        with pytest.raises(ValueError):
            Linter().lint_all()

    def test_summary_and_report(self, workspace):
        issues = Linter(workspace).lint_all()
        stats = Linter.summary(issues)
        assert stats["total"] > 0 and stats["errors"] >= 1
        assert "共" in Linter.report(issues)

    def test_report_empty(self):
        assert Linter.report([]) == "没有发现问题"


class TestIssueShape:
    def test_to_dict_and_str(self):
        issue = Issue("a.py", 3, "line-too-long", "太长了", column=5,
                      severity="info")
        assert issue.location() == "a.py:3:5"
        assert "[info]" in str(issue)
        assert set(issue.to_dict()) >= {"path", "line", "column", "rule", "severity"}

    def test_every_rule_has_severity_documented(self):
        for rule, (severity, description) in RULES.items():
            assert severity in SEVERITIES
            assert description.strip()


# ---------------------------------------------------------------------------
# perf
# ---------------------------------------------------------------------------

class TestTimer:
    def test_measures_duration(self):
        with Timer("sleepy", track_memory=False) as timer:
            time.sleep(0.05)
        assert timer.result.duration_ms >= 40
        assert timer.result.ok

    def test_records_into_monitor(self):
        monitor = PerfMonitor()
        with monitor.measure("work", track_memory=False):
            pass
        assert len(monitor.timings) == 1

    def test_exception_recorded_not_swallowed(self):
        monitor = PerfMonitor()
        with pytest.raises(RuntimeError):
            with monitor.measure("boom", track_memory=False):
                raise RuntimeError("炸了")
        assert monitor.failures() and not monitor.failures()[0].ok

    def test_memory_tracking(self):
        """分配的内存必须能测到，且**不得出现负数**。

        tracemalloc 的峰值是进程级的：第一次写这个测试时，前面的测试已经把峰值
        顶到更高，于是 `peak - before` 变成负数（"负内存"）。现在 Timer 会
        先 reset_peak 并夹到非负。
        """
        with Timer("alloc", track_memory=True) as timer:
            data = [bytes(1024) for _ in range(200)]
            assert data
        assert timer.result.peak_mb > 0
        assert timer.result.memory_mb >= 0 or timer.result.memory_mb == 0

    def test_memory_deltas_never_negative(self):
        """连着测两段：第二段即使没有新峰值也不能报负数。"""
        with Timer("first", track_memory=True) as first:
            _ = [bytes(4096) for _ in range(500)]
        with Timer("second", track_memory=True) as second:
            pass
        assert first.result.peak_mb >= 0
        assert second.result.peak_mb >= 0 and second.result.memory_mb >= 0


class TestPerfMonitor:
    def test_sample_and_latest(self):
        monitor = PerfMonitor()
        sample = monitor.sample(now=123.0)
        assert sample.at == 123.0
        assert monitor.latest() is sample
        assert sample.python_mb >= 0

    def test_usage_percent_and_critical(self):
        monitor = PerfMonitor(budget_mb=1.0, critical_ratio=0.5)
        monitor.sample()
        assert 0.0 <= monitor.usage_percent() <= 100.0
        assert isinstance(monitor.is_critical(), bool)

    def test_trend_needs_two_samples(self):
        monitor = PerfMonitor()
        assert monitor.trend() == "unknown"
        monitor.samples.append(Sample(at=1.0, python_mb=1.0, blocks=0))
        monitor.samples.append(Sample(at=2.0, python_mb=5.0, blocks=0))
        assert monitor.trend() == "rising"

    def test_trend_steady(self):
        monitor = PerfMonitor()
        monitor.samples.append(Sample(at=1.0, python_mb=1.0, blocks=0))
        monitor.samples.append(Sample(at=2.0, python_mb=1.1, blocks=0))
        assert monitor.trend() == "steady"

    def test_suggest_gc_threshold(self):
        monitor = PerfMonitor(gc_growth_mb=1.0)
        monitor.samples.append(Sample(at=1.0, python_mb=1.0, blocks=0))
        monitor.samples.append(Sample(at=2.0, python_mb=9.0, blocks=0))
        assert monitor.suggest_gc() is True

    def test_collect_returns_stats(self):
        monitor = PerfMonitor()
        monitor.sample()
        result = monitor.collect()
        assert "collected_objects" in result and "freed_mb" in result

    def test_slowest_and_snapshot(self):
        monitor = PerfMonitor()
        with monitor.measure("fast", track_memory=False):
            pass
        with monitor.measure("slow", track_memory=False):
            time.sleep(0.02)
        assert monitor.slowest(1)[0].name == "slow"
        data = monitor.snapshot()
        assert data["timings"] == 2 and data["total_ms"] >= 20
        assert "性能快照" in monitor.report()

    def test_history_bounded(self):
        monitor = PerfMonitor(history=4)
        for index in range(20):
            monitor.sample(now=float(index))
        assert len(monitor.samples) == 4


# ---------------------------------------------------------------------------
# debug
# ---------------------------------------------------------------------------

class TestLaunchConfig:
    def test_defaults_by_type(self):
        config = LaunchConfig(name="py", type="python", program="main.py")
        assert config.port == 0
        assert LaunchConfig(name="n", type="node").port == 9229

    def test_rejects_unknown_type(self):
        with pytest.raises(ValueError):
            LaunchConfig(name="x", type="brainfuck")

    def test_requires_name(self):
        with pytest.raises(ValueError):
            LaunchConfig(name="   ", type="python")

    def test_roundtrip(self):
        config = LaunchConfig(name="t", type="node", args=["a", "b"],
                              env={"K": "V"}, stop_on_entry=True)
        restored = LaunchConfig.from_dict(config.to_dict())
        assert restored.to_dict() == config.to_dict()


class TestDebugStore:
    def test_add_get_remove(self):
        store = DebugConfigStore()
        store.add(LaunchConfig(name="a", type="python"))
        store.add(LaunchConfig(name="b", type="node"))
        assert store.names() == ["a", "b"]
        assert store.get("a").type == "python"
        assert store.remove("a") is True
        assert store.remove("nope") is False

    def test_add_replaces_same_name(self):
        store = DebugConfigStore()
        store.add(LaunchConfig(name="a", type="python"))
        store.add(LaunchConfig(name="a", type="node"))
        assert store.names() == ["a"]
        assert store.get("a").type == "node"

    def test_json_roundtrip(self, workspace):
        store = DebugConfigStore(workspace)
        store.add(LaunchConfig(name="调试", type="python", program="main.py"))
        path = store.save()
        assert path == ".tcide/launch.json"
        fresh = DebugConfigStore(workspace)
        loaded = fresh.load()
        assert [c.name for c in loaded] == ["调试"]

    def test_load_invalid_json_is_empty(self, workspace):
        assert DebugConfigStore(workspace).load_json("{ 不是 json") == []

    def test_load_skips_invalid_entries(self):
        text = '{"configurations": [{"name": "ok", "type": "python"}, {"name": "bad", "type": "nope"}]}'
        loaded = DebugConfigStore().load_json(text)
        assert [c.name for c in loaded] == ["ok"]

    def test_defaults_for_uses_memory(self, memory):
        config = DebugConfigStore.defaults_for(memory)
        assert config.type == "python"
        assert config.program == "main.py"

    def test_save_requires_workspace(self):
        with pytest.raises(ValueError):
            DebugConfigStore().save()


class TestDebugSession:
    def test_toggle(self):
        session = DebugSession()
        assert session.toggle("a.py", 10) is True
        assert session.lines_for("a.py") == [10]
        assert session.toggle("a.py", 10) is False
        assert session.lines_for("a.py") == []

    def test_add_with_condition(self):
        session = DebugSession()
        session.add("a.py", 3, condition="x > 1")
        assert session.all()[0].condition == "x > 1"

    def test_clear(self):
        session = DebugSession()
        session.add("a.py", 1)
        session.add("b.py", 2)
        assert session.clear("a.py") == 1
        assert session.count() == 1
        assert session.clear() == 1
        assert session.count() == 0

    def test_command_python(self):
        session = DebugSession()
        config = LaunchConfig(name="p", type="python", program="main.py")
        assert session.command_for(config) == ["python", "-m", "pdb", "main.py"]

    def test_command_python_module(self):
        session = DebugSession()
        config = LaunchConfig(name="p", type="python")
        assert session.command_for(config, python_module="pkg") == [
            "python", "-m", "pdb", "-m", "pkg"]

    def test_command_node_uses_port(self):
        session = DebugSession()
        config = LaunchConfig(name="n", type="node", program="index.js")
        assert session.command_for(config) == [
            "node", "--inspect-brk=9229", "index.js"]

    def test_command_custom_without_command(self):
        session = DebugSession()
        config = LaunchConfig(name="c", type="custom")
        with pytest.raises(ValueError):
            session.command_for(config)

    def test_command_string_quotes_spaces(self):
        assert DebugSession.command_string(["python", "a b.py"]) == 'python "a b.py"'

    def test_report_and_dict(self):
        session = DebugSession()
        assert session.report() == "没有断点"
        session.add("a.py", 1)
        session.add("a.py", 2)
        assert "共 2 个断点" in session.report()
        assert session.to_dict()["count"] == 2

    def test_parse_breakpoints_text(self):
        points = load_breakpoints("a.py:10\nb.py 20\n坏行\n")
        assert [(p.path, p.line) for p in points] == [("a.py", 10), ("b.py", 20)]

    def test_every_type_has_command(self):
        for kind in COMMANDS:
            assert isinstance(COMMANDS[kind], list)


# ---------------------------------------------------------------------------
# hexview
# ---------------------------------------------------------------------------

class TestHexLine:
    def test_hex_and_ascii(self):
        line = HexLine(offset=0, data=b"AB\x00\xff", width=4)
        assert line.hex_parts == ["41", "42", "00", "ff"]
        assert line.ascii == "AB.."

    def test_render_pads_short_lines(self):
        text = HexLine(offset=16, data=b"A", width=8).render()
        assert text.startswith("00000010")
        assert "|A" in text

    def test_to_dict(self):
        assert HexLine(0, b"A").to_dict()["hex"] == "41"


class TestHexViewer:
    def test_page_and_render(self, workspace):
        workspace.write("bin.dat", "0123456789abcdef" * 4)
        viewer = HexViewer(workspace, width=8)
        page = viewer.page("bin.dat", offset=0, rows=2)
        assert len(page) == 2
        assert page[1].offset == 8
        text = viewer.render("bin.dat", rows=2)
        assert "offset" in text and "00000000" in text

    def test_render_past_end(self, workspace):
        workspace.write("small.dat", "abc")
        assert "没有数据" in HexViewer(workspace).render("small.dat", offset=999)

    def test_invalid_arguments(self, workspace):
        viewer = HexViewer(workspace)
        with pytest.raises(ValueError):
            viewer.page("small.dat", offset=-1)
        with pytest.raises(ValueError):
            viewer.page("small.dat", rows=0)
        with pytest.raises(ValueError):
            HexViewer(workspace, width=0)

    def test_find_offsets(self, workspace):
        workspace.write("data.bin", "abcXYZabcXYZ")
        assert HexViewer(workspace).find("data.bin", "abc") == [0, 6]

    def test_find_str_and_bytes_equivalent(self, workspace):
        workspace.write("data.bin", "hello")
        viewer = HexViewer(workspace)
        assert viewer.find("data.bin", "ell") == viewer.find("data.bin", b"ell")

    def test_find_limit_and_empty(self, workspace):
        workspace.write("data.bin", "aaaa")
        viewer = HexViewer(workspace)
        assert len(viewer.find("data.bin", "a", limit=2)) == 2
        assert viewer.find("data.bin", "") == []

    def test_sniff_known_types(self):
        assert "PNG" in HexViewer.sniff(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
        assert "PDF" in HexViewer.sniff(b"%PDF-1.7")
        assert "ZIP" in HexViewer.sniff(b"PK\x03\x04")
        assert HexViewer.sniff(b"") == "空文件"

    def test_sniff_text_and_binary(self):
        assert HexViewer.sniff("你好".encode()) == "文本"
        assert "未知二进制" in HexViewer.sniff(b"\x00\x01\x02")

    def test_info(self, workspace):
        data = HexViewer(workspace).info("assets/logo.png")
        assert data["size"] > 0
        assert "PNG" in data["kind"]
        assert data["is_text"] is False
        assert data["head_hex"].startswith("89 50 4e 47")

    def test_entropy_range(self):
        viewer = HexViewer()
        assert viewer.entropy(b"") == 0.0
        assert viewer.entropy(b"\x00" * 100) == 0.0
        high = bytes(range(256)) * 8
        assert 7.9 < viewer.entropy(high) <= 8.0

    def test_requires_workspace(self):
        with pytest.raises(ValueError):
            HexViewer().info("a.bin")

    def test_report(self, workspace):
        report = HexViewer(workspace).report("assets/logo.png")
        assert "# assets/logo.png" in report and "熵" in report

    def test_default_width(self):
        assert DEFAULT_WIDTH == 16
