# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""「变更」清单（tcide/changes.py）的测试。

依据：Codex 的 review pane / DeepSeek Harness 的 changes-review / Trae 的代码变更
面板 —— 共同要解决的是"AI 一次改一堆文件，人无从复核"。这里只测数据聚合层。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tcide.changes import ChangeRow, ChangeSummary


@dataclass
class FakeProposal:
    """最小替身：只要 path / source / stats() 就够（真实的是 diff.ChangeProposal）。"""

    path: str
    added: int = 0
    removed: int = 0
    source: str = ""

    def stats(self) -> dict:
        return {"added": self.added, "removed": self.removed}


@dataclass
class FakeChange:
    path: str


@dataclass
class FakeStatus:
    staged: list = field(default_factory=list)
    unstaged: list = field(default_factory=list)
    untracked: list = field(default_factory=list)
    conflicted: list = field(default_factory=list)


class TestFromProposals:
    def test_rows_are_sorted_by_path(self):
        summary = ChangeSummary.from_proposals([
            FakeProposal("z.py", 1, 1), FakeProposal("a.py", 2, 2)])
        assert summary.paths() == ["a.py", "z.py"], "清单要按路径排序，便于查找"

    def test_totals_add_up(self):
        summary = ChangeSummary.from_proposals([
            FakeProposal("a.py", 10, 2), FakeProposal("b.py", 5, 3)])
        assert summary.total_added == 15
        assert summary.total_removed == 5

    def test_summary_line_mentions_counts(self):
        summary = ChangeSummary.from_proposals([FakeProposal("a.py", 10, 2)])
        text = summary.summary()
        assert "1 个文件" in text and "+10" in text and "2" in text

    def test_label_shows_path_and_counts(self):
        row = ChangeSummary.from_proposals([FakeProposal("a.py", 10, 2)]).rows[0]
        label = row.label()
        assert "a.py" in label and "+10" in label

    def test_broken_stats_does_not_explode(self):
        """统计失败（补丁畸形等）不该让整个界面炸 —— 退化成 0/0 即可。"""

        class Broken:
            path = "x.py"

            def stats(self):
                raise RuntimeError("补丁坏了")

        summary = ChangeSummary.from_proposals([Broken()])
        assert summary.paths() == ["x.py"]
        assert summary.total_added == 0


class TestFromStatus:
    def test_groups_map_to_kinds(self):
        summary = ChangeSummary.from_status(FakeStatus(
            staged=[FakeChange("s.py")], unstaged=[FakeChange("u.py")],
            untracked=[FakeChange("n.py")], conflicted=[FakeChange("c.py")]))
        kinds = {row.path: row.kind for row in summary.rows}
        assert kinds == {"s.py": "M", "u.py": "M", "n.py": "A", "c.py": "!"}

    def test_missing_groups_are_tolerated(self):
        class Partial:
            unstaged = [FakeChange("only.py")]

        assert ChangeSummary.from_status(Partial()).paths() == ["only.py"]


class TestEmpty:
    def test_empty_summary_says_so(self):
        summary = ChangeSummary()
        assert summary.is_empty
        assert "没有" in summary.summary()
        assert summary.labels() == []

    def test_empty_hint_tells_the_user_what_to_do(self):
        """空状态不能只说"没有" —— 要告诉用户这里会出现什么。"""
        hint = ChangeSummary().hint()
        assert "AI" in hint, f"空状态提示太干：{hint!r}"

    def test_non_empty_hint_mentions_the_actions(self):
        summary = ChangeSummary.from_proposals([FakeProposal("a.py", 1, 0)])
        hint = summary.hint(reviewed=True)
        assert "接受" in hint and "驳回" in hint
