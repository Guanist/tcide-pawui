# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""轨迹记录：这一轮 AI 到底做了哪些步骤、花了多久、用了多少 token。

为什么需要（三个参照产品都指向同一件事）：DeepSeek Harness 有专门的"轨迹视图"
（Turn/Step 分组 + token 用量 + 时长 + TTFT），Codex 有 ``/status`` 与状态栏预算
可见性，Trae 有任务状态面板。它们的共同判断是：**"AI 在黑盒里干活"是焦虑的根源**，
把过程摊开就能消解。

本模块只做记录与汇总（不碰 Qt），因此可以离线严格测试。
**诚实原则**：拿不到 token 用量时显示"—"，不编数字。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: 耗时可读化阈值
_MS = 1000.0


def human_ms(ms: float) -> str:
    """把毫秒变成人看的字符串（120ms / 1.2s / 1m03s）。"""
    value = max(0.0, float(ms))
    if value < _MS:
        return f"{value:.0f}ms"
    seconds = value / _MS
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


@dataclass
class TraceStep:
    """一步：一次工具调用，或一个任务节点。"""

    kind: str                    # tool_call | task_start | task_done | plan | ...
    name: str                    # 工具名 / 任务标题
    detail: str = ""
    ms: float = 0.0
    ok: bool = True

    def label(self) -> str:
        mark = "✓" if self.ok else "✗"
        duration = f"  {human_ms(self.ms)}" if self.ms else ""
        detail = f"  {self.detail}" if self.detail else ""
        return f"{mark} {self.name}{detail}{duration}"


@dataclass
class TraceRecorder:
    """按轮次记录步骤；一轮 = 一次 agent 运行。"""

    steps: list[TraceStep] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 正在计时的工具起点（工具成对事件用）
    _open: dict[str, float] = field(default_factory=dict)

    # -- 记录 -------------------------------------------------------------

    def begin_round(self) -> None:
        self.steps.clear()
        self._open.clear()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.started = time.perf_counter()

    def add(self, kind: str, name: str, *, detail: str = "", ms: float = 0.0,
            ok: bool = True) -> TraceStep:
        step = TraceStep(kind=kind, name=name, detail=detail, ms=ms, ok=ok)
        self.steps.append(step)
        return step

    def tool_started(self, name: str) -> None:
        self._open[name] = time.perf_counter()

    def tool_finished(self, name: str, *, detail: str = "", ok: bool = True) -> TraceStep:
        """工具结束：用配对的起点算耗时（没配对上就记 0，不编）。"""
        started = self._open.pop(name, None)
        ms = (time.perf_counter() - started) * _MS if started else 0.0
        return self.add("tool_call", name, detail=detail, ms=ms, ok=ok)

    def usage(self, prompt: int = 0, completion: int = 0) -> None:
        self.prompt_tokens += max(0, int(prompt or 0))
        self.completion_tokens += max(0, int(completion or 0))

    # -- 查询 -------------------------------------------------------------

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * _MS

    @property
    def tool_calls(self) -> int:
        return sum(1 for step in self.steps if step.kind == "tool_call")

    @property
    def failures(self) -> int:
        return sum(1 for step in self.steps if not step.ok)

    def tokens_text(self) -> str:
        """token 用量：拿不到就显示 "—"（**不编数字**）。"""
        if not (self.prompt_tokens or self.completion_tokens):
            return "token —"
        return (f"token {self.prompt_tokens + self.completion_tokens}"
                f"（入 {self.prompt_tokens} / 出 {self.completion_tokens}）")

    def summary(self) -> str:
        if not self.steps:
            return "本轮还没有步骤（让 AI 干点活就会出现在这里）"
        parts = [f"本轮 {len(self.steps)} 步",
                 f"{self.tool_calls} 次工具调用",
                 human_ms(self.elapsed_ms)]
        if self.failures:
            parts.append(f"{self.failures} 次失败")
        parts.append(self.tokens_text())
        return " · ".join(parts)

    def labels(self, *, limit: int = 200) -> list[str]:
        """给界面用的行（最新在前，避免长轨迹要把滚动条拖到底）。"""
        return [step.label() for step in reversed(self.steps[-limit:])]

    def is_empty(self) -> bool:
        return not self.steps
