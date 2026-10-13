# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""无人值守执行器：多步骤、超时、重试、检查点、日志。

对应 TCIDE 原版的 `core/runner/unattended-runner.ts`
（常量 `MAX_RETRIES` / `STEP_TIMEOUT` / `GLOBAL_TIMEOUT` / `MAX_CHECKPOINTS` /
`logDir`）—— 该模块在原版里**从未接线，功能静默失效**。

设计要点：
- **两种步骤**：``command``（子进程，超时能真正杀掉）与 ``action``（Python 可调用，
  在线程里跑；超时只能放弃等待，线程仍可能继续 —— 这一点在文档与事件里说明，
  不假装能杀掉它）；
- **检查点**：每个标记了 ``checkpoint=True`` 的步骤成功后写盘，数量上限可配；
  ``resume=True`` 时跳过已完成步骤；
- **全局超时**：超时后不再开始新步骤，剩余步骤按「未执行」结束；
- **日志**：每一步的结果都落到 ``log_dir`` 的日志文件里（无人值守必须能事后查）。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: 与原版一致的默认值
DEFAULT_STEP_TIMEOUT = 300.0
DEFAULT_GLOBAL_TIMEOUT = 1800.0
DEFAULT_MAX_CHECKPOINTS = 20
DEFAULT_MAX_RETRIES = 1

CHECKPOINT_FILE = "unattended-state.json"


@dataclass
class Step:
    """一个执行步骤。``command`` 与 ``action`` 二选一。

    ``retries=None`` 表示沿用执行器的 ``max_retries``；显式给 ``0`` 表示不重试。
    （写成 ``int | None`` 是为了避免「0 到底是什么意思」的歧义。）
    """

    name: str
    command: str = ""
    action: Callable[[], Any] | None = None
    timeout: float = 0.0            # 0 表示用执行器的 step_timeout
    retries: int | None = None
    checkpoint: bool = False
    expect_exit_zero: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name, "command": self.command,
                "timeout": self.timeout, "retries": self.retries,
                "checkpoint": self.checkpoint}


@dataclass
class StepResult:
    """一步的结果。"""

    name: str
    ok: bool
    output: str = ""
    error: str = ""
    attempts: int = 0
    duration_ms: int = 0
    timed_out: bool = False
    skipped: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "output": self.output[-2000:],
                "error": self.error, "attempts": self.attempts,
                "duration_ms": self.duration_ms, "timed_out": self.timed_out,
                "skipped": self.skipped}


@dataclass
class RunReport:
    """一次无人值守执行的汇总。"""

    steps: list[StepResult] = field(default_factory=list)
    duration_ms: int = 0
    cancelled: bool = False
    global_timeout_hit: bool = False
    checkpoints: int = 0
    log_file: str = ""

    @property
    def done(self) -> int:
        return sum(1 for step in self.steps if step.ok and not step.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for step in self.steps if not step.ok and not step.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for step in self.steps if step.skipped)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.cancelled and not self.global_timeout_hit

    def to_dict(self) -> dict:
        return {"ok": self.ok, "steps": [s.to_dict() for s in self.steps],
                "done": self.done, "failed": self.failed, "skipped": self.skipped,
                "duration_ms": self.duration_ms, "cancelled": self.cancelled,
                "global_timeout_hit": self.global_timeout_hit,
                "checkpoints": self.checkpoints, "log_file": self.log_file}

    def summary(self) -> str:
        if self.global_timeout_hit:
            state = "全局超时"
        elif self.cancelled:
            state = "已取消"
        else:
            state = "成功" if self.ok else "有失败"
        return (f"{state}：完成 {self.done}，失败 {self.failed}，跳过 {self.skipped}，"
                f"检查点 {self.checkpoints}，用时 {self.duration_ms}ms")


class UnattendedRunner:
    """按顺序执行步骤，带超时/重试/检查点/日志。"""

    def __init__(
        self,
        steps: list[Step],
        *,
        log_dir: str | Path,
        cwd: str | Path | None = None,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        global_timeout: float = DEFAULT_GLOBAL_TIMEOUT,
        max_checkpoints: int = DEFAULT_MAX_CHECKPOINTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.steps = list(steps)
        self.log_dir = Path(log_dir)
        self.cwd = Path(cwd) if cwd is not None else self.log_dir
        self.step_timeout = float(step_timeout)
        self.global_timeout = float(global_timeout)
        self.max_checkpoints = max(1, int(max_checkpoints))
        self.max_retries = max(0, int(max_retries))
        self.on_event = on_event
        self._cancel = threading.Event()
        self._log_lines: list[str] = []
        self._checkpoints: list[dict] = []
        # 实例属性而不是类属性：写成类属性会被所有实例共享（这是经典的 Python 坑）
        self._completed: list[str] = []

    # -- 基础设施 ---------------------------------------------------------

    def cancel(self) -> None:
        """请求停止（下一步开始前生效）。"""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _emit(self, kind: str, **data) -> None:
        if self.on_event is not None:
            self.on_event(kind, data)

    def _log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log_lines.append(f"[{stamp}] {line}")

    @property
    def state_file(self) -> Path:
        return self.log_dir / CHECKPOINT_FILE

    def load_state(self) -> dict:
        """读回上次的状态（不存在或损坏时返回空状态，并记一条日志）。"""
        if not self.state_file.exists():
            return {"completed": [], "checkpoints": []}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log(f"检查点文件不可读（{exc}），按全新执行处理")
            return {"completed": [], "checkpoints": []}
        return {"completed": list(data.get("completed") or []),
                "checkpoints": list(data.get("checkpoints") or [])}

    def save_state(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({"completed": self._completed,
                        "checkpoints": self._checkpoints[-self.max_checkpoints:]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

    # -- 单步执行 ---------------------------------------------------------

    def _run_command(self, step: Step, timeout: float) -> StepResult:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                step.command, shell=True, cwd=self.cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return StepResult(name=step.name, ok=False, output=output,
                              error=f"超时（{timeout:.0f}s）", timed_out=True,
                              duration_ms=int((time.perf_counter() - started) * 1000))
        output = (completed.stdout or "") + (completed.stderr or "")
        ok = completed.returncode == 0 if step.expect_exit_zero else True
        return StepResult(
            name=step.name, ok=ok, output=output,
            error="" if ok else f"退出码 {completed.returncode}",
            duration_ms=int((time.perf_counter() - started) * 1000))

    def _run_action(self, step: Step, timeout: float) -> StepResult:
        started = time.perf_counter()
        # 注意：Python 线程无法被强杀，超时只能"不再等待"。
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(step.action)  # type: ignore[misc]
            try:
                value = future.result(timeout=timeout)
            except FutureTimeout:
                return StepResult(
                    name=step.name, ok=False, timed_out=True,
                    error=f"超时（{timeout:.0f}s）；注意线程无法被强杀，可能仍在运行",
                    duration_ms=int((time.perf_counter() - started) * 1000))
            except Exception as exc:  # noqa: BLE001
                return StepResult(name=step.name, ok=False,
                                  error=f"{type(exc).__name__}: {exc}",
                                  duration_ms=int((time.perf_counter() - started) * 1000))
        return StepResult(name=step.name, ok=True,
                          output="" if value is None else str(value),
                          duration_ms=int((time.perf_counter() - started) * 1000))

    def _execute(self, step: Step, timeout: float) -> StepResult:
        if step.command:
            return self._run_command(step, timeout)
        if step.action is not None:
            return self._run_action(step, timeout)
        return StepResult(name=step.name, ok=False, error="步骤既没有 command 也没有 action")

    # -- 主流程 -----------------------------------------------------------

    def run(self, *, resume: bool = True) -> RunReport:
        """执行全部步骤。``resume=True`` 时跳过上次已完成的步骤。"""
        started = time.perf_counter()
        deadline = started + self.global_timeout
        report = RunReport()
        state = self.load_state() if resume else {"completed": [], "checkpoints": []}
        self._completed = list(state["completed"])
        self._checkpoints = list(state["checkpoints"])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"unattended-{stamp}.log"
        report.log_file = str(log_path)
        self._log(f"开始执行 {len(self.steps)} 个步骤（resume={resume}）")

        for step in self.steps:
            if self._cancel.is_set():
                report.cancelled = True
                report.steps.append(StepResult(step.name, ok=False, skipped=True,
                                               error="已取消"))
                continue
            if step.name in self._completed:
                report.steps.append(StepResult(step.name, ok=True, skipped=True,
                                               output="检查点显示已完成，跳过"))
                self._log(f"跳过 {step.name}（检查点）")
                continue
            if time.perf_counter() >= deadline:
                report.global_timeout_hit = True
                report.steps.append(StepResult(step.name, ok=False, skipped=True,
                                               error="全局超时，未执行"))
                self._log(f"全局超时，不再执行 {step.name}")
                continue

            remaining = max(1.0, deadline - time.perf_counter())
            timeout = min(step.timeout or self.step_timeout, remaining)
            result = self._execute_with_retries(step, timeout)
            report.steps.append(result)
            if result.ok:
                self._log(f"完成 {step.name}（{result.duration_ms}ms）")
                if step.checkpoint and step.name not in self._completed:
                    self._completed.append(step.name)
                    self._checkpoints.append({
                        "step": step.name, "at": time.time(),
                        "output": result.output[-500:]})
                    report.checkpoints += 1
                    self.save_state()
                    self._emit("checkpoint", step=step.name)
            else:
                self._log(f"失败 {step.name}: {result.error}")
                self._emit("step_failed", step=step.name, error=result.error)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self._log(f"结束：{report.summary()}")
        log_path.write_text("\n".join(self._log_lines) + "\n", encoding="utf-8")
        self._emit("done", **report.to_dict())
        return report

    def _execute_with_retries(self, step: Step, timeout: float) -> StepResult:
        attempts = 0
        retries = self.max_retries if step.retries is None else max(0, step.retries)
        limit = 1 + retries
        result = StepResult(name=step.name, ok=False, error="未执行")
        while attempts < limit:
            attempts += 1
            self._emit("step_start", step=step.name, attempt=attempts)
            result = self._execute(step, timeout)
            result.attempts = attempts
            if result.ok or self._cancel.is_set():
                return result
            if attempts < limit:
                self._log(f"重试 {step.name}（第 {attempts + 1}/{limit} 次）")
        return result
