# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""性能采样（Python 侧）。

对应 TCIDE 原版的 `core/perf/perf-optimizer.ts`（`getMetrics` / `getTrend` /
`getUsagePercent` / `isCritical` / `suggestGC`）—— 该模块在原版**从未接线，静默失效**。

**诚实标注**：这里测的是 **Python 侧分配**（``tracemalloc``）与**代码块耗时**，
不是整进程 RSS（那需要 ``psutil`` 或平台 API，会让核心多一个依赖）。
界面上会如实写成「Python 侧」。
"""

from __future__ import annotations

import gc
import time
import tracemalloc
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Sample:
    """一次采样。"""

    at: float
    python_mb: float
    blocks: int


@dataclass
class Timing:
    """一次计时的结果。"""

    name: str
    duration_ms: int
    memory_mb: float = 0.0
    peak_mb: float = 0.0
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "duration_ms": self.duration_ms,
                "memory_mb": round(self.memory_mb, 3),
                "peak_mb": round(self.peak_mb, 3), "ok": self.ok,
                "error": self.error}


class Timer:
    """上下文管理器：测一段代码的耗时与 Python 侧内存增量。"""

    def __init__(self, name: str, monitor: "PerfMonitor | None" = None,
                 *, track_memory: bool = True) -> None:
        self.name = name
        self.monitor = monitor
        self.track_memory = track_memory
        self.result = Timing(name=name, duration_ms=0)
        self._started = 0.0
        self._own_tracemalloc = False
        self._before = (0, 0)

    def __enter__(self) -> "Timer":
        if self.track_memory and not tracemalloc.is_tracing():
            tracemalloc.start()
            self._own_tracemalloc = True
        if tracemalloc.is_tracing():
            # 峰值是**进程级**的：如果之前已经有人跑到更高的峰值，
            # `peak - before_peak` 会是负数（无意义的"负内存"）。
            # 所以先重置峰值，让本段的增量真实可信。
            tracemalloc.reset_peak()
            self._before = tracemalloc.get_traced_memory()
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        self.result.duration_ms = int((time.perf_counter() - self._started) * 1000)
        if tracemalloc.is_tracing():
            current, peak = tracemalloc.get_traced_memory()
            # 夹到非负：测量值只能是"涨了多少"，不可能是负数
            self.result.memory_mb = max(0.0, (current - self._before[0]) / 1024 / 1024)
            self.result.peak_mb = max(0.0, (peak - self._before[1]) / 1024 / 1024)
        if exc is not None:
            self.result.ok = False
            self.result.error = f"{type(exc).__name__}: {exc}"
        if self._own_tracemalloc:
            tracemalloc.stop()
        if self.monitor is not None:
            self.monitor.record(self.result)
        return False        # 不吞异常


class PerfMonitor:
    """采样历史 + 趋势 + 用量百分比 + 是否危险 + 何时建议 GC。"""

    def __init__(self, *, history: int = 120, budget_mb: float = 512.0,
                 critical_ratio: float = 0.85, gc_growth_mb: float = 32.0) -> None:
        self.budget_mb = float(budget_mb)
        self.critical_ratio = float(critical_ratio)
        self.gc_growth_mb = float(gc_growth_mb)
        self.samples: deque[Sample] = deque(maxlen=max(4, int(history)))
        self.timings: deque[Timing] = deque(maxlen=max(4, int(history)))
        self.started = time.time()

    # -- 采样 -------------------------------------------------------------

    def sample(self, *, now: float | None = None) -> Sample:
        """采一次样（Python 侧当前/峰值内存）。"""
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        current, _peak = tracemalloc.get_traced_memory()
        item = Sample(at=now if now is not None else time.time(),
                      python_mb=current / 1024 / 1024,
                      blocks=gc.get_count()[0])
        self.samples.append(item)
        return item

    def latest(self) -> Sample | None:
        return self.samples[-1] if self.samples else None

    def record(self, timing: Timing) -> None:
        self.timings.append(timing)

    def measure(self, name: str, *, track_memory: bool = True) -> Timer:
        return Timer(name, self, track_memory=track_memory)

    # -- 指标 -------------------------------------------------------------

    def usage_percent(self) -> float:
        latest = self.latest()
        if latest is None or self.budget_mb <= 0:
            return 0.0
        return max(0.0, min(100.0, latest.python_mb / self.budget_mb * 100))

    def is_critical(self) -> bool:
        return self.usage_percent() >= self.critical_ratio * 100

    def trend(self, window: int = 10) -> str:
        """近 ``window`` 次采样的走势：rising / falling / steady / unknown。"""
        if len(self.samples) < 2:
            return "unknown"
        recent = list(self.samples)[-window:]
        first, last = recent[0].python_mb, recent[-1].python_mb
        delta = last - first
        if abs(delta) < 0.5:
            return "steady"
        return "rising" if delta > 0 else "falling"

    def suggest_gc(self) -> bool:
        """内存相对首次采样涨得够多才建议 GC（避免无谓抖动）。"""
        if not self.samples:
            return False
        growth = self.samples[-1].python_mb - self.samples[0].python_mb
        return growth >= self.gc_growth_mb

    def collect(self) -> dict:
        """真的执行一次 GC，并报告回收量。"""
        before = self.latest().python_mb if self.samples else 0.0
        collected = gc.collect()
        after = self.sample().python_mb
        return {"collected_objects": collected,
                "freed_mb": round(max(0.0, before - after), 3)}

    # -- 汇总 -------------------------------------------------------------

    def slowest(self, limit: int = 5) -> list[Timing]:
        return sorted(self.timings, key=lambda t: -t.duration_ms)[:limit]

    def failures(self) -> list[Timing]:
        return [item for item in self.timings if not item.ok]

    def snapshot(self) -> dict:
        latest = self.latest()
        timings = list(self.timings)
        total_ms = sum(item.duration_ms for item in timings)
        return {
            "python_mb": round(latest.python_mb, 3) if latest else 0.0,
            "budget_mb": self.budget_mb,
            "usage_percent": round(self.usage_percent(), 2),
            "is_critical": self.is_critical(),
            "trend": self.trend(),
            "suggest_gc": self.suggest_gc(),
            "samples": len(self.samples),
            "timings": len(timings),
            "total_ms": total_ms,
            "failures": len(self.failures()),
            "slowest": [item.to_dict() for item in self.slowest()],
        }

    def report(self) -> str:
        data = self.snapshot()
        lines = ["# 性能快照（Python 侧）", ""]
        lines.append(f"- 已用 {data['python_mb']} MB / 预算 {data['budget_mb']} MB"
                     f"（{data['usage_percent']}%）")
        lines.append(f"- 走势 {data['trend']}；采样 {data['samples']} 次；"
                     f"{'建议 GC' if data['suggest_gc'] else '暂不需要 GC'}")
        lines.append(f"- 计时 {data['timings']} 段，合计 {data['total_ms']} ms，"
                     f"失败 {data['failures']} 段")
        if data["slowest"]:
            lines.append("")
            lines.append("最慢的几段：")
            for item in data["slowest"]:
                lines.append(f"- {item['name']}：{item['duration_ms']} ms"
                             f"（内存 {item['memory_mb']} MB）")
        return "\n".join(lines)
