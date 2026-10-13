# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""轨迹记录（tcide/trace.py）的测试。

原则：**拿不到 token 就显示 "—"，不编数字** —— 看不见的假数据比没有数据更糟。
"""

from __future__ import annotations

import time

import pytest

from tcide.trace import TraceRecorder, TraceStep, human_ms


class TestHumanMs:
    @pytest.mark.parametrize("ms,expected", [
        (0, "0ms"), (120, "120ms"), (999, "999ms"),
        (1000, "1.0s"), (2500, "2.5s"), (59_000, "59.0s"),
        (63_000, "1m03s"), (3600_000, "60m00s"),
    ])
    def test_formats(self, ms, expected):
        assert human_ms(ms) == expected

    def test_negative_is_clamped(self):
        assert human_ms(-5) == "0ms"


class TestRecorder:
    def test_empty_summary_is_honest(self):
        recorder = TraceRecorder()
        assert recorder.is_empty()
        assert "还没有步骤" in recorder.summary()
        assert recorder.labels() == []

    def test_steps_accumulate(self):
        recorder = TraceRecorder()
        recorder.add("tool_call", "read_file", detail="main.py")
        recorder.add("tool_call", "write_file", detail="a.py")
        assert recorder.tool_calls == 2
        assert len(recorder.labels()) == 2

    def test_tool_duration_uses_paired_start(self):
        recorder = TraceRecorder()
        recorder.tool_started("run_command")
        time.sleep(0.02)
        step = recorder.tool_finished("run_command")
        assert step.ms >= 15, f"耗时应该被记下来，实际 {step.ms}"

    def test_unpaired_finish_records_zero_not_a_lie(self):
        """没配对上起点就记 0 —— 绝不编一个看起来合理的耗时。"""
        recorder = TraceRecorder()
        step = recorder.tool_finished("没开始过的工具")
        assert step.ms == 0.0

    def test_failures_are_counted_and_marked(self):
        recorder = TraceRecorder()
        recorder.add("tool_call", "run_command", ok=False)
        recorder.add("tool_call", "read_file")
        assert recorder.failures == 1
        labels = " ".join(recorder.labels())
        assert "✗" in labels and "✓" in labels

    def test_tokens_show_dash_when_unknown(self):
        recorder = TraceRecorder()
        assert recorder.tokens_text() == "token —"
        recorder.usage(120, 30)
        assert "150" in recorder.tokens_text()
        assert "入 120" in recorder.tokens_text()

    def test_begin_round_resets_everything(self):
        recorder = TraceRecorder()
        recorder.add("tool_call", "x")
        recorder.usage(10, 10)
        recorder.begin_round()
        assert recorder.is_empty() and recorder.tokens_text() == "token —"

    def test_summary_mentions_counts_and_duration(self):
        recorder = TraceRecorder()
        recorder.add("tool_call", "read_file")
        text = recorder.summary()
        assert "1 步" in text and "1 次工具调用" in text and "token —" in text

    def test_labels_are_newest_first(self):
        """最新在前：长轨迹不必把滚动条拖到底才能看到刚发生的事。"""
        recorder = TraceRecorder()
        recorder.add("tool_call", "第一步")
        recorder.add("tool_call", "第二步")
        assert "第二步" in recorder.labels()[0]

    def test_labels_limit_keeps_the_tail(self):
        recorder = TraceRecorder()
        for index in range(10):
            recorder.add("tool_call", f"步骤{index}")
        labels = recorder.labels(limit=3)
        assert len(labels) == 3
        assert "步骤9" in labels[0]


class TestStepLabel:
    def test_label_includes_mark_name_detail_and_duration(self):
        step = TraceStep("tool_call", "write_file", detail="a.py", ms=1250, ok=True)
        label = step.label()
        assert "✓" in label and "write_file" in label
        assert "a.py" in label and "1.2s" in label

    def test_zero_duration_is_hidden(self):
        assert "ms" not in TraceStep("tool_call", "x").label()
