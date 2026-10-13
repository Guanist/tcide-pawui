# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""把"这一轮到底改了什么"整理成一张可审阅的清单。

为什么单独成模块（三个参照产品都有对应物）：Codex 的 review pane（未提交／整分支／
仅上一轮，可按 hunk 接受）、DeepSeek Harness 的 ``workspace-changes`` +
``changes-review`` 右栏、Trae 的"代码变更面板 + 逐文件 diff" —— 都在解决同一个
问题：**AI 一次改几十个文件时，人无从复核**。

这里只做**数据聚合**（不碰 Qt、不碰界面），所以能离线严格测试：
输入是"待审核的提案"或"git 状态"，输出是给界面用的行与汇总。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChangeRow:
    """一行变更：一个文件。"""

    path: str
    added: int = 0
    removed: int = 0
    #: 来源说明（例如 agent 的名字、或 "工作区"）
    source: str = ""
    #: 状态字母：M 修改 / A 新增 / D 删除 / ? 未跟踪
    kind: str = "M"

    def label(self) -> str:
        """界面里显示的一行（等宽对齐，行数用 +/- 直观表示）。"""
        counts = f"+{self.added} −{self.removed}"
        return f"{self.kind}  {self.path}    {counts}"

    def to_dict(self) -> dict:
        return {"path": self.path, "added": self.added, "removed": self.removed,
                "kind": self.kind, "source": self.source}


@dataclass
class ChangeSummary:
    """一次汇总：若干文件 + 总增删。"""

    rows: list[ChangeRow] = field(default_factory=list)

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_proposals(cls, proposals) -> "ChangeSummary":
        """从审核会话里的待审提案构造（AI 这一轮改的东西）。"""
        rows: list[ChangeRow] = []
        for proposal in proposals:
            try:
                stats = proposal.stats()
            except Exception:      # noqa: BLE001 - 统计失败也不能让界面炸
                stats = {}
            rows.append(ChangeRow(
                path=str(getattr(proposal, "path", "")),
                added=int(stats.get("added", 0) or 0),
                removed=int(stats.get("removed", 0) or 0),
                source=str(getattr(proposal, "source", "") or ""),
                kind="M",
            ))
        return cls(sorted(rows, key=lambda row: row.path))

    @classmethod
    def from_status(cls, status) -> "ChangeSummary":
        """从 git 状态构造（包含人自己改的，用来做"工作区总览"）。"""
        rows: list[ChangeRow] = []
        for group, kind in (("staged", "M"), ("unstaged", "M"), ("untracked", "A"),
                            ("conflicted", "!")):
            try:
                changes = list(getattr(status, group, []) or [])
            except Exception:      # noqa: BLE001
                changes = []
            for change in changes:
                rows.append(ChangeRow(path=str(getattr(change, "path", "")),
                                      kind=kind,
                                      source="工作区"))
        return cls(sorted(rows, key=lambda row: row.path))

    # -- 查询 -------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def total_added(self) -> int:
        return sum(row.added for row in self.rows)

    @property
    def total_removed(self) -> int:
        return sum(row.removed for row in self.rows)

    def summary(self) -> str:
        """一句话汇总（对话里那张"变更卡"的标题）。"""
        if self.is_empty:
            return "本轮没有文件变更"
        return (f"本轮 {len(self.rows)} 个文件  "
                f"+{self.total_added} −{self.total_removed}")

    def labels(self) -> list[str]:
        return [row.label() for row in self.rows]

    def paths(self) -> list[str]:
        return [row.path for row in self.rows]

    def hint(self, *, reviewed: bool = False) -> str:
        """空状态/提示语：告诉用户下一步能做什么，而不是只说"没有"。"""
        if self.is_empty:
            return "还没有待审阅的改动。让 AI 改点东西，这里会出现逐文件清单。"
        if reviewed:
            return (f"待审核 {len(self.rows)} 个文件 —— 选中一个可按文件接受或驳回；"
                    f"也可以用「全部接受」/「全部驳回」")
        return f"工作区有 {len(self.rows)} 个文件被改动（含你手改的）"
