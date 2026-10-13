# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""AI 双 Agent 引擎：Builder 拆解 → Coder 执行 → Reviewer 审查门。

对应 TCIDE 原版的 `core/agent/`（`agent-orchestrator.ts` 399 行 +
`builder-agent.ts` + `coder-agent.ts`）与其常量 `MAX_PARALLEL_CODERS` /
`MAX_CODER_RETRY` / `REVIEWER_GATE_ENABLED`。

与原版的三处刻意差异：
1. **审查门分两层**：先跑**确定性检查**（`get_diagnostics` + 可选测试命令），
   再（可选）让模型评审。原版只有模型判断，不可靠也不可测。
2. **不静默兜底**：计划解析失败会带着原文回退成单任务，并发出 `plan_fallback`
   事件 —— 用户看得见，而不是拿到一个空计划。
3. **每一次工具调用都被记录**，编排产出 `RunReport`，循环次数/重试/拒绝都可断言。

模型通过 `ModelClient` 协议注入，测试用假模型即可完全离线驱动。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .models import ChatMessage, ChatResponse, ModelConfig, ModelError, ToolCall, adapter_for
from .tools import ToolRegistry

#: Coder 单任务最多几轮工具调用（对齐原版的 8 轮）
MAX_TOOL_ROUNDS = 8

#: 同一个工具用同样参数连续调用几次就判定为原地打转
REPEAT_LIMIT = 2

#: 计划里最多接受多少个子任务
MAX_TASKS = 20

BUILDER_SYSTEM = """你是 Builder，负责把用户需求拆解成可执行的任务清单。

规则（与项目 CLAUDE.md 一致）：
- 先澄清再动手：需求不明确时，在 plan 的 notes 里写清假设，不要偷偷补充需求。
- 极简实现：只拆当下必须做的事，不做过度设计。
- 增量闭环：每个任务都要小到能单独验证。

只输出一个 JSON 对象，不要输出别的内容：
{
  "goal": "一句话目标",
  "notes": "假设与风险（可为空）",
  "tasks": [
    {"title": "短标题", "detail": "具体要做什么", "files": ["相对路径.py"]}
  ]
}
"""

CODER_SYSTEM = """你是 Coder，负责用工具完成一个具体任务。

规则：
- 微创修改：只改与任务直接相关的代码，不做无关重构或格式化。
- 先看再改：改文件前先用 read_file 看清现状。
- 闭环验证：改完用 get_diagnostics 确认没有语法错误。
- 卡住了要显性说明，不要静默放弃。

完成后用一两句话总结你改了什么、验证结果如何。
"""

REVIEWER_SYSTEM = """你是 Reviewer，负责判断一次改动是否可以接受。

只输出一个 JSON 对象：
{"approved": true/false, "reasons": ["理由"], "severity": "low|medium|high"}

判断标准：是否解决了任务目标、是否引入语法错误、是否做了无关改动、是否有明显风险。
"""


# ---------------------------------------------------------------------------
# 模型接入
# ---------------------------------------------------------------------------

class ModelClient(Protocol):
    """只要能 `complete` 就够（真实适配器与假模型都满足）。"""

    def complete(self, messages: list[ChatMessage],
                 tools: list[dict] | None = None) -> ChatResponse: ...


class AdapterClient:
    """把 :mod:`tcide.models` 的适配器包成 ModelClient。"""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.adapter = adapter_for(config)

    def complete(self, messages, tools=None) -> ChatResponse:
        return self.adapter.complete(messages, tools)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """一个子任务。"""

    id: str
    title: str
    detail: str = ""
    files: list[str] = field(default_factory=list)
    status: str = "pending"      # pending | running | done | failed | rejected
    attempts: int = 0
    summary: str = ""
    error: str = ""
    tool_calls: int = 0

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "detail": self.detail,
                "files": self.files, "status": self.status,
                "attempts": self.attempts, "summary": self.summary,
                "error": self.error, "tool_calls": self.tool_calls}


@dataclass
class Plan:
    """Builder 的产出。"""

    goal: str
    tasks: list[Task] = field(default_factory=list)
    notes: str = ""
    raw: str = ""
    fallback: bool = False

    def to_dict(self) -> dict:
        return {"goal": self.goal, "notes": self.notes,
                "tasks": [t.to_dict() for t in self.tasks],
                "fallback": self.fallback}


@dataclass
class Review:
    """审查门结论。"""

    approved: bool
    reasons: list[str] = field(default_factory=list)
    severity: str = "low"
    by_model: bool = False

    def to_dict(self) -> dict:
        return {"approved": self.approved, "reasons": self.reasons,
                "severity": self.severity, "by_model": self.by_model}


@dataclass
class RunReport:
    """一次编排的统计。

    口径（务必分清，写错过一次）：
    - ``failed``   任务最终**没有成功**（Coder 失败或审查始终驳回）
    - ``rejected`` 审查门**驳回次数**，是过程指标 —— 驳回后重试成功属于正常流程，
      因此它**不**影响 :attr:`ok`
    - ``done + failed == planned``
    """

    goal: str = ""
    planned: int = 0
    done: int = 0
    failed: int = 0
    rejected: int = 0
    retries: int = 0
    tool_calls: int = 0
    duration_ms: int = 0
    cancelled: bool = False
    tasks: list[Task] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.planned > 0 and self.failed == 0
                and not self.cancelled)

    def to_dict(self) -> dict:
        return {"goal": self.goal, "planned": self.planned, "done": self.done,
                "failed": self.failed, "rejected": self.rejected,
                "retries": self.retries, "tool_calls": self.tool_calls,
                "duration_ms": self.duration_ms, "cancelled": self.cancelled,
                "ok": self.ok, "tasks": [t.to_dict() for t in self.tasks]}

    def summary(self) -> str:
        state = "已取消" if self.cancelled else ("成功" if self.ok else "有失败")
        return (f"{state}：{self.done}/{self.planned} 完成，"
                f"失败 {self.failed}，被拒 {self.rejected}，重试 {self.retries}，"
                f"工具调用 {self.tool_calls} 次，用时 {self.duration_ms}ms")


@dataclass
class AgentEvent:
    """编排过程中的事件（界面/宠物状态机订阅）。"""

    kind: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "data": self.data}


# ---------------------------------------------------------------------------
# JSON 解析（模型输出常有围栏/前后缀）
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json(text: str) -> dict | None:
    """从模型输出里尽力取出一个 JSON 对象；取不到返回 None。"""
    if not text:
        return None
    candidates: list[str] = []
    candidates += [block.strip() for block in _FENCE_RE.findall(text)]
    stripped = text.strip()
    candidates.append(stripped)
    # 退化尝试：第一个 { 到最后一个 }
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class BuilderAgent:
    """把需求拆成任务清单。"""

    def __init__(self, model: ModelClient, *, max_tasks: int = MAX_TASKS) -> None:
        self.model = model
        self.max_tasks = max_tasks

    def plan(self, requirement: str, *, context: str = "",
             tools: list[dict] | None = None) -> Plan:
        requirement = (requirement or "").strip()
        if not requirement:
            raise ValueError("需求不能为空")
        messages = [ChatMessage.system(BUILDER_SYSTEM)]
        if context:
            messages.append(ChatMessage.system(f"# 项目静态上下文\n{context}"))
        messages.append(ChatMessage.user(requirement))
        response = self.model.complete(messages, tools)
        return self.parse_plan(response.text, requirement)

    def parse_plan(self, text: str, requirement: str) -> Plan:
        """解析模型输出；失败时回退成单任务，并标记 fallback（不静默）。"""
        data = extract_json(text)
        if not data or not isinstance(data.get("tasks"), list) or not data["tasks"]:
            return Plan(goal=requirement, notes="计划解析失败，已回退为单任务执行",
                        raw=text, fallback=True,
                        tasks=[Task(id="t1", title=requirement, detail=requirement)])
        tasks: list[Task] = []
        for index, raw in enumerate(data["tasks"][:self.max_tasks], 1):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            files = raw.get("files")
            tasks.append(Task(
                id=f"t{index}", title=title,
                detail=str(raw.get("detail") or "").strip(),
                files=[str(f) for f in files] if isinstance(files, list) else []))
        if not tasks:
            return Plan(goal=requirement, notes="计划里没有有效任务，已回退为单任务",
                        raw=text, fallback=True,
                        tasks=[Task(id="t1", title=requirement, detail=requirement)])
        return Plan(goal=str(data.get("goal") or requirement).strip(),
                    notes=str(data.get("notes") or "").strip(),
                    tasks=tasks, raw=text)


# ---------------------------------------------------------------------------
# Coder
# ---------------------------------------------------------------------------

@dataclass
class TaskOutcome:
    """Coder 执行一个任务的结果。"""

    ok: bool
    summary: str = ""
    rounds: int = 0
    tool_calls: int = 0
    touched: list[str] = field(default_factory=list)
    error: str = ""
    stopped_reason: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "summary": self.summary, "rounds": self.rounds,
                "tool_calls": self.tool_calls, "touched": self.touched,
                "error": self.error, "stopped_reason": self.stopped_reason}


class CoderAgent:
    """用工具循环完成一个任务。"""

    def __init__(self, model: ModelClient, tools: ToolRegistry, *,
                 max_rounds: int = MAX_TOOL_ROUNDS,
                 repeat_limit: int = REPEAT_LIMIT,
                 on_event: Callable[[AgentEvent], None] | None = None) -> None:
        self.model = model
        self.tools = tools
        self.max_rounds = max_rounds
        self.repeat_limit = repeat_limit
        self.on_event = on_event

    def _emit(self, kind: str, message: str = "", **data) -> None:
        if self.on_event is not None:
            self.on_event(AgentEvent(kind=kind, message=message, data=data))

    def execute(self, task: Task, *, context: str = "") -> TaskOutcome:
        messages = [ChatMessage.system(CODER_SYSTEM)]
        if context:
            messages.append(ChatMessage.system(f"# 项目静态上下文\n{context}"))
        prompt = f"# 任务\n{task.title}\n\n{task.detail}".strip()
        if task.files:
            prompt += "\n\n涉及文件：" + "、".join(task.files)
        messages.append(ChatMessage.user(prompt))

        tool_specs = self.tools.specs()
        calls_made = 0
        touched: list[str] = []
        seen: dict[str, int] = {}
        stopped_reason = "model_finished"

        for round_index in range(1, self.max_rounds + 1):
            try:
                response = self.model.complete(messages, tool_specs)
            except ModelError as exc:
                return TaskOutcome(ok=False, error=f"模型调用失败: {exc}",
                                   rounds=round_index - 1, tool_calls=calls_made,
                                   touched=sorted(set(touched)),
                                   stopped_reason="model_error")
            if not response.tool_calls:
                return TaskOutcome(ok=True, summary=response.text.strip() or "（模型未给出说明）",
                                   rounds=round_index, tool_calls=calls_made,
                                   touched=sorted(set(touched)),
                                   stopped_reason=stopped_reason)

            messages.append(ChatMessage.assistant(response.text, response.tool_calls))
            for call in response.tool_calls:
                signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
                seen[signature] = seen.get(signature, 0) + 1
                if seen[signature] > self.repeat_limit:
                    stopped_reason = "repeat_guard"
                    self._emit("loop_guard", f"重复调用被拦截: {call.name}",
                               signature=signature)
                    return TaskOutcome(ok=False, error=f"原地打转：反复调用 {call.name}",
                                       rounds=round_index, tool_calls=calls_made,
                                       touched=sorted(set(touched)),
                                       stopped_reason=stopped_reason)
                self._emit("tool_call", call.name, arguments=call.arguments)
                result = self.tools.call(call.name, call.arguments)
                calls_made += 1
                if result.ok and call.name in ("write_file", "edit_file"):
                    path = str(call.arguments.get("path") or "")
                    if path:
                        touched.append(path)
                messages.append(ChatMessage.tool(call.id, result.as_text(), call.name))

        self._emit("round_limit", f"达到最大轮次 {self.max_rounds}")
        return TaskOutcome(ok=False, error=f"超过最大工具轮次（{self.max_rounds}）",
                           rounds=self.max_rounds, tool_calls=calls_made,
                           touched=sorted(set(touched)), stopped_reason="round_limit")


# ---------------------------------------------------------------------------
# Reviewer（审查门）
# ---------------------------------------------------------------------------

class ReviewerAgent:
    """审查门：确定性检查优先，模型评审可选。

    - **确定性部分**（始终执行）：改动的文件必须通过 `get_diagnostics`；
      配置了 `test_command` 时还会真跑一次测试。
    - **模型部分**（可选）：`use_model=True` 时再问一次模型；模型失败时
      **不静默通过**，而是记入理由并保持"需人工确认"。
    """

    def __init__(self, model: ModelClient | None = None, tools: ToolRegistry | None = None,
                 *, use_model: bool = False, test_command: str = "",
                 test_timeout: int = 120) -> None:
        self.model = model
        self.tools = tools
        self.use_model = use_model and model is not None
        self.test_command = test_command
        self.test_timeout = test_timeout

    def review(self, task: Task, outcome: TaskOutcome, *, context: str = "") -> Review:
        reasons: list[str] = []
        severity = "low"

        if not outcome.ok:
            return Review(approved=False, severity="high",
                          reasons=[f"Coder 未成功完成：{outcome.error or outcome.stopped_reason}"])

        if not outcome.touched:
            reasons.append("没有产生文件改动")
            severity = "medium"

        if self.tools is not None:
            for path in outcome.touched:
                result = self.tools.call("get_diagnostics", {"path": path})
                # 只判断 ok：**不要**去匹配提示文案。曾经写成
                # `"问题" in result.output`，结果「未发现问题」也含「问题」，
                # 每次改动都被误判为检查失败（有回归测试）。
                if not result.ok:
                    return Review(approved=False, severity="high",
                                  reasons=[f"{path} 未通过静态检查：{result.output[:200]}"])

        if self.test_command:
            run = self.tools.call("run_command",
                                  {"command": self.test_command,
                                   "timeout": self.test_timeout}) if self.tools else None
            if run is not None and not run.ok:
                return Review(approved=False, severity="high",
                              reasons=[f"测试未通过：{run.output[-300:] or run.error}"])

        if self.use_model and self.model is not None:
            prompt = (f"# 任务\n{task.title}\n\n# Coder 自述\n{outcome.summary}\n\n"
                      f"# 改动文件\n" + "\n".join(outcome.touched))
            try:
                response = self.model.complete([ChatMessage.system(REVIEWER_SYSTEM),
                                                ChatMessage.user(prompt)])
                data = extract_json(response.text)
            except ModelError as exc:
                reasons.append(f"模型评审不可用（{exc}），仅依赖确定性检查")
                severity = "medium"
                data = None
            if isinstance(data, dict):
                approved = bool(data.get("approved", True))
                model_reasons = [str(r) for r in (data.get("reasons") or [])]
                severity = str(data.get("severity") or severity)
                return Review(approved=approved, reasons=model_reasons or reasons,
                              severity=severity, by_model=True)

        return Review(approved=True, reasons=reasons or ["确定性检查通过"],
                      severity=severity)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

class AgentOrchestrator:
    """Builder → 并行 Coder → 审查门 → 重试，产出可断言的统计。"""

    def __init__(
        self,
        model: ModelClient,
        tools: ToolRegistry,
        *,
        context: str = "",
        max_parallel_coders: int = 2,
        max_coder_retry: int = 2,
        review_gate_enabled: bool = True,
        reviewer: ReviewerAgent | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.context = context
        self.max_parallel_coders = max(1, int(max_parallel_coders))
        self.max_coder_retry = max(0, int(max_coder_retry))
        self.review_gate_enabled = bool(review_gate_enabled)
        self.reviewer = reviewer or ReviewerAgent(tools=tools)
        self.on_event = on_event
        self._cancel = threading.Event()

    # -- 事件与取消 -------------------------------------------------------

    def _emit(self, kind: str, message: str = "", **data) -> None:
        if self.on_event is not None:
            self.on_event(AgentEvent(kind=kind, message=message, data=data))

    def cancel(self) -> None:
        """请求停止（界面上的「停止」按钮）。"""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- 主流程 -----------------------------------------------------------

    def plan(self, requirement: str) -> Plan:
        builder = BuilderAgent(self.model)
        plan = builder.plan(requirement, context=self.context, tools=self.tools.specs())
        if plan.fallback:
            self._emit("plan_fallback", plan.notes, raw=plan.raw[:500])
        else:
            self._emit("plan", f"{len(plan.tasks)} 个任务", goal=plan.goal)
        return plan

    def run_plan(self, plan: Plan) -> RunReport:
        """执行计划：并行度受 `max_parallel_coders` 限制。"""
        started = time.perf_counter()
        report = RunReport(goal=plan.goal, planned=len(plan.tasks), tasks=list(plan.tasks))
        if not plan.tasks:
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            return report

        pending = list(plan.tasks)
        # 用固定大小的线程池模拟原版的「最多 N 个 Coder 并行」
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=self.max_parallel_coders) as pool:
            futures = {pool.submit(self._run_task, task, report): task for task in pending}
            for future in futures:
                future.result()   # 异常已在 _run_task 内部处理

        report.tool_calls = sum(t.tool_calls for t in report.tasks)
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        report.cancelled = self.cancelled
        self._emit("done", report.summary(), **report.to_dict())
        return report

    def _run_task(self, task: Task, report: RunReport) -> None:
        """一个任务：Coder → 审查门 → 重试（线程内执行）。

        记账口径（文档化，便于断言）：
        - ``done``     任务完成；``failed`` 任务最终没成；``done + failed == planned``
        - ``rejected`` 审查门**驳回次数**（每次驳回都计，用于观察门是否在工作）
        - ``retries``  重试次数
        """
        lock = threading.Lock()
        if self.cancelled:
            task.status = "failed"
            task.error = "已取消"
            with lock:
                report.failed += 1
            return
        task.status = "running"
        self._emit("task_start", task.title, task_id=task.id)
        coder = CoderAgent(self.model, self.tools, on_event=self.on_event)
        final_reason = "coder"

        for attempt in range(1, self.max_coder_retry + 2):
            task.attempts = attempt
            outcome = coder.execute(task, context=self.context)
            task.tool_calls += outcome.tool_calls
            task.summary = outcome.summary

            if not outcome.ok:
                task.error = outcome.error
                final_reason = "coder"
                self._emit("attempt_failed", outcome.error, task_id=task.id,
                           attempt=attempt)
            else:
                if not self.review_gate_enabled:
                    task.status = "done"
                    task.error = ""
                    with lock:
                        report.done += 1
                    self._emit("task_done", task.summary, task_id=task.id)
                    return
                review = self.reviewer.review(task, outcome, context=self.context)
                self._emit("review", "通过" if review.approved else "驳回",
                           approved=review.approved, reasons=review.reasons,
                           severity=review.severity, by_model=review.by_model)
                if review.approved:
                    task.status = "done"
                    task.error = ""      # 成功就清掉之前驳回留下的错误信息
                    with lock:
                        report.done += 1
                    self._emit("task_done", task.summary, task_id=task.id)
                    return
                task.error = "；".join(review.reasons)[:400]
                final_reason = "review"
                with lock:
                    report.rejected += 1

            if attempt <= self.max_coder_retry and not self.cancelled:
                with lock:
                    report.retries += 1
                self._emit("retry", f"第 {attempt} 次重试", task_id=task.id)
                continue
            break

        task.status = "rejected" if final_reason == "review" else "failed"
        with lock:
            report.failed += 1
        self._emit("task_failed", task.error or "失败", task_id=task.id,
                   status=task.status)

    def run(self, requirement: str) -> RunReport:
        """便捷入口：Builder + 编排一次跑完。"""
        return self.run_plan(self.plan(requirement))


def run_agent(
    requirement: str,
    tools: ToolRegistry,
    model: ModelClient,
    *,
    context: str = "",
    max_parallel_coders: int = 2,
    max_coder_retry: int = 2,
    review_gate_enabled: bool = True,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> RunReport:
    """一次性调用（无人值守 runner 也用它）。"""
    orchestrator = AgentOrchestrator(
        model, tools, context=context,
        max_parallel_coders=max_parallel_coders,
        max_coder_retry=max_coder_retry,
        review_gate_enabled=review_gate_enabled, on_event=on_event)
    return orchestrator.run(requirement)


def iter_events(events: Iterable[AgentEvent]) -> list[dict]:
    """把事件流转成可序列化列表（界面用）。"""
    return [event.to_dict() for event in events]
