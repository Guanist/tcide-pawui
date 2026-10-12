# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""上下文裁剪：滑窗 + 分层压缩。

对应 TCIDE 原版的 ``trimmer/context-trimmer.ts``（"32K token 滑动窗口上下文压缩"）
与 ``trimmer/smart-trimmer.ts``。

设计要点（与原版一致，但可测）：
- **分层**：从最不伤信息的动作开始逐层加码，一旦降到预算以内就停。
- **可见**：返回 ``TrimReport``，让界面能显示「剪了多少、用了哪几层」——
  对应 CLAUDE.md 里「严格控制 Token 消耗」的要求，也让行为可断言。
- **不删空对话**：最后一条消息永远保留。否则 ``keep_recent=0`` 时会把整段
  上下文裁空，模型收到空输入（这是实现里必须挡住的情况，已有回归测试）。
- **保护语义**：``keep_recent`` / ``pinned`` 只保护「整轮对话不被丢弃」；
  压缩内容与削减附件对所有消息生效，否则单条巨消息永远压不进预算。
  pinned（项目规则）在硬截断阶段最后才被动，尽量保持原文。
- 不依赖 Qt / 网络，纯逻辑，可离线严格测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import (
    collapse_whitespace,
    estimate_messages,
    estimate_tokens,
    shorten_code_blocks,
    truncate_to_tokens,
)

#: 分层名称（顺序即执行顺序）
LAYERS: tuple[str, ...] = (
    "limit-dependency-files",
    "shorten-code-blocks",
    "drop-old-dialogs",
    "hard-truncate",
)


@dataclass
class Message:
    """一条对话消息。``files`` 是随消息附带的依赖文件（文件名 -> 内容）。"""

    role: str
    content: str
    pinned: bool = False
    files: dict[str, str] = field(default_factory=dict)

    def tokens(self) -> int:
        total = estimate_tokens(self.content) + 4
        total += sum(estimate_tokens(name) + estimate_tokens(text)
                     for name, text in self.files.items())
        return total

    def clone(self) -> Message:
        return Message(self.role, self.content, self.pinned, dict(self.files))


@dataclass
class TrimReport:
    """裁剪报告。"""

    before_tokens: int = 0
    after_tokens: int = 0
    budget: int = 0
    dropped_messages: int = 0
    dropped_files: int = 0
    truncated_messages: int = 0
    layers: list[str] = field(default_factory=list)
    exact: bool = False  # 最终是否真的降到了预算以内

    @property
    def saved(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    def to_dict(self) -> dict:
        return {
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "budget": self.budget,
            "saved": self.saved,
            "dropped_messages": self.dropped_messages,
            "dropped_files": self.dropped_files,
            "truncated_messages": self.truncated_messages,
            "layers": list(self.layers),
            "exact": self.exact,
        }

    def describe(self) -> str:
        if not self.layers:
            return f"上下文 {self.after_tokens}/{self.budget} token，无需裁剪"
        return (f"{self.before_tokens} → {self.after_tokens} token"
                f"（省 {self.saved}），层次: {' → '.join(self.layers)}")


class ContextTrimmer:
    """把一组消息压到预算以内。

    Parameters
    ----------
    budget:
        token 预算（默认 32768，与 TCIDE 的 "32K 滑动窗口" 对齐）。
    keep_recent:
        最近多少条消息在「丢弃历史」这一层必须保留。
    max_depend_files:
        单条消息最多保留几个附带文件。
    head_lines / tail_lines:
        压缩代码块时保留的头尾行数。
    """

    def __init__(
        self,
        budget: int = 32768,
        *,
        keep_recent: int = 4,
        max_depend_files: int = 3,
        head_lines: int = 8,
        tail_lines: int = 4,
    ) -> None:
        if budget <= 0:
            raise ValueError("budget 必须为正")
        if keep_recent < 0:
            raise ValueError("keep_recent 不能为负")
        self.budget = int(budget)
        self.keep_recent = int(keep_recent)
        self.max_depend_files = max(0, int(max_depend_files))
        self.head_lines = int(head_lines)
        self.tail_lines = int(tail_lines)

    # -- 内部 -------------------------------------------------------------

    def _total(self, messages: list[Message], static_context: str) -> int:
        return estimate_messages(messages) + estimate_tokens(static_context)

    @staticmethod
    def _pinned_indices(messages: list[Message]) -> set[int]:
        return {i for i, m in enumerate(messages) if m.pinned}

    def _kept_on_drop(self, messages: list[Message]) -> set[int]:
        """第 3 层允许保留的下标：pinned + 最近 keep_recent 条 + 最后一条。"""
        keep = self._pinned_indices(messages)
        recent = [i for i, m in enumerate(messages) if not m.pinned]
        if self.keep_recent:
            keep.update(recent[-self.keep_recent:])
        if messages:
            keep.add(len(messages) - 1)
        return keep

    def _hard_truncate(
        self,
        messages: list[Message],
        static_context: str,
        report: TrimReport,
        *,
        allow_pinned: bool,
    ) -> bool:
        """把每条消息的内容与附件压到分配的 token 份额内。"""
        pinned = self._pinned_indices(messages)
        targets = [i for i in range(len(messages)) if allow_pinned or i not in pinned]
        if not targets:
            return False
        remaining = max(32, self.budget - estimate_tokens(static_context))
        used = False
        for position, index in enumerate(targets):
            message = messages[index]
            share = max(32, remaining // max(1, len(targets) - position))
            before = message.tokens()

            original = message.content
            message.content = truncate_to_tokens(collapse_whitespace(original), share)
            if message.content != original:
                report.truncated_messages += 1

            # 内容削完仍超份额 → 削附件（先按 token 截断，截不动再丢弃）
            names = sorted(message.files)
            while names and message.tokens() > share:
                name = names.pop()
                others = message.tokens() - estimate_tokens(name) - estimate_tokens(
                    message.files[name])
                allowance = max(32, share - others)
                trimmed = truncate_to_tokens(message.files[name], allowance)
                if trimmed == message.files[name]:
                    message.files.pop(name)
                    report.dropped_files += 1
                else:
                    message.files[name] = trimmed

            after = message.tokens()
            if after < before:
                used = True
            remaining = max(32, remaining - after)
        return used

    # -- 主流程 -----------------------------------------------------------

    def trim(
        self,
        messages,
        *,
        static_context: str = "",
    ) -> tuple[list[Message], TrimReport]:
        """返回 (裁剪后的消息, 报告)。``messages`` 不会被就地修改。"""
        working = [m.clone() if isinstance(m, Message) else Message(**m) for m in messages]
        report = TrimReport(budget=self.budget,
                            before_tokens=self._total(working, static_context))

        def over() -> bool:
            return self._total(working, static_context) > self.budget

        # 第 1 层：限制依赖文件数量（不删消息，最轻）
        if over() and working:
            used = False
            for message in working:
                if len(message.files) <= self.max_depend_files:
                    continue
                keep = sorted(message.files)[:self.max_depend_files]
                report.dropped_files += len(message.files) - len(keep)
                message.files = {name: message.files[name] for name in keep}
                used = True
            if used:
                report.layers.append("limit-dependency-files")

        # 第 2 层：压缩代码块（保留骨架；pinned 原样保留）
        if over():
            used = False
            pinned = self._pinned_indices(working)
            for index, message in enumerate(working):
                if index in pinned:
                    continue
                original = message.content
                message.content = shorten_code_blocks(
                    original, self.head_lines, self.tail_lines)
                if message.content != original:
                    used = True
                    report.truncated_messages += 1
            if used:
                report.layers.append("shorten-code-blocks")

        # 第 3 层：只保留最近的对话（无损，但丢历史）
        if over():
            keep = self._kept_on_drop(working)
            kept = [m for i, m in enumerate(working) if i in keep]
            dropped = len(working) - len(kept)
            if dropped:
                report.dropped_messages += dropped
                report.layers.append("drop-old-dialogs")
                working = kept

        # 第 4 层：硬截断（先避开 pinned，仍然超才动 pinned）
        if over():
            used = self._hard_truncate(working, static_context, report,
                                       allow_pinned=False)
            if over():
                used = self._hard_truncate(working, static_context, report,
                                           allow_pinned=True) or used
            if used:
                report.layers.append("hard-truncate")

        report.after_tokens = self._total(working, static_context)
        report.exact = report.after_tokens <= self.budget
        return working, report
