# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""agent.py 的测试：用假模型驱动 Builder / Coder / 审查门 / 编排，完全离线。"""

from __future__ import annotations

import threading

import pytest

from tcide.agent import (
    MAX_TASKS,
    AgentEvent,
    AgentOrchestrator,
    BuilderAgent,
    CoderAgent,
    Plan,
    ReviewerAgent,
    Task,
    TaskOutcome,
    extract_json,
    run_agent,
)
from tcide.indexer import ProjectIndex
from tcide.models import ChatMessage, ChatResponse, ModelError, ToolCall
from tcide.tools import ToolRegistry
from tcide.workspace import Workspace


# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------

class FakeModel:
    """按脚本返回响应的假模型；脚本耗尽会立刻报错（避免测试悄悄变空转）。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, messages, tools=None) -> ChatResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.responses:
            raise AssertionError("假模型脚本已耗尽：被测代码调用了超出预期的次数")
        item = self.responses.pop(0)
        return item(messages) if callable(item) else item


class FunctionModel:
    """按**消息内容**决策的无状态假模型。

    共享脚本队列在并行执行下会被乱序消费（我第一版就是这么写的，导致
    「两个任务必须同时在飞」的断言永远失败），所以并发场景必须用无状态模型。
    """

    def __init__(self, function) -> None:
        self.function = function
        self.calls: list[dict] = []

    def complete(self, messages, tools=None) -> ChatResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        return self.function(messages)


def text(value: str) -> ChatResponse:
    return ChatResponse(text=value)


def calls(*pairs, text_value: str = "") -> ChatResponse:
    return ChatResponse(text=text_value, tool_calls=[
        ToolCall(id=f"c{index}", name=name, arguments=arguments)
        for index, (name, arguments) in enumerate(pairs, 1)])


def plan_json(tasks: list[dict], goal: str = "目标", notes: str = "") -> str:
    import json

    return "```json\n" + json.dumps({"goal": goal, "notes": notes, "tasks": tasks},
                                    ensure_ascii=False) + "\n```"


@pytest.fixture
def tools(workspace):
    index = ProjectIndex(workspace)
    index.scan()
    return ToolRegistry(workspace, index=index)


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_fenced(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_with_prose_around(self):
        assert extract_json('好的，结果如下：{"a": 1} 完毕') == {"a": 1}

    def test_invalid_returns_none(self):
        assert extract_json("完全没有 JSON") is None
        assert extract_json("") is None

    def test_non_object_returns_none(self):
        assert extract_json("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class TestBuilder:
    def test_parses_plan(self, tools):
        model = FakeModel([text(plan_json([
            {"title": "加一个函数", "detail": "在 util.py 里加 helper", "files": ["util.py"]},
            {"title": "补测试", "detail": "给 helper 写测试"},
        ], goal="加功能"))])
        plan = BuilderAgent(model).plan("加个功能", tools=tools.specs())
        assert plan.goal == "加功能" and not plan.fallback
        assert [t.id for t in plan.tasks] == ["t1", "t2"]
        assert plan.tasks[0].files == ["util.py"]

    def test_tools_are_passed_to_model(self, tools):
        model = FakeModel([text(plan_json([{"title": "x"}]))])
        BuilderAgent(model).plan("x", tools=tools.specs())
        assert model.calls[0]["tools"]

    def test_context_is_included(self):
        model = FakeModel([text(plan_json([{"title": "x"}]))])
        BuilderAgent(model).plan("x", context="项目用 Python")
        joined = " ".join(message.content for message in model.calls[0]["messages"])
        assert "项目用 Python" in joined

    def test_fallback_on_unparsable(self):
        model = FakeModel([text("我做不到")])
        plan = BuilderAgent(model).plan("做个东西")
        assert plan.fallback is True
        assert len(plan.tasks) == 1
        assert plan.tasks[0].title == "做个东西"
        assert "回退" in plan.notes

    def test_fallback_on_empty_tasks(self):
        assert BuilderAgent(FakeModel([text('{"tasks": []}')])).plan("x").fallback

    def test_tasks_without_title_are_skipped(self):
        plan = BuilderAgent(FakeModel([text(plan_json(
            [{"title": "有效"}, {"detail": "没有标题"}]))])).plan("x")
        assert [t.title for t in plan.tasks] == ["有效"]

    def test_max_tasks_cap(self):
        many = [{"title": f"任务{i}"} for i in range(MAX_TASKS + 5)]
        plan = BuilderAgent(FakeModel([text(plan_json(many))])).plan("x")
        assert len(plan.tasks) == MAX_TASKS

    def test_empty_requirement_rejected(self):
        with pytest.raises(ValueError):
            BuilderAgent(FakeModel([])).plan("   ")

    def test_plan_serializable(self):
        plan = BuilderAgent(FakeModel([text(plan_json([{"title": "a"}]))])).plan("x")
        assert plan.to_dict()["tasks"][0]["title"] == "a"


# ---------------------------------------------------------------------------
# Coder
# ---------------------------------------------------------------------------

class TestCoder:
    def test_plain_answer_finishes(self, tools):
        model = FakeModel([text("不需要改动")])
        outcome = CoderAgent(model, tools).execute(Task("t1", "看看"))
        assert outcome.ok and outcome.summary == "不需要改动"
        assert outcome.rounds == 1 and outcome.tool_calls == 0

    def test_tool_round_then_finish(self, tools):
        model = FakeModel([
            calls(("write_file", {"path": "src/new.py", "content": "x = 1\n"})),
            text("已写入"),
        ])
        outcome = CoderAgent(model, tools).execute(Task("t1", "写文件"))
        assert outcome.ok and outcome.tool_calls == 1
        assert outcome.touched == ["src/new.py"]
        assert tools.workspace.exists("src/new.py")

    def test_tool_result_is_fed_back(self, tools):
        model = FakeModel([
            calls(("read_file", {"path": "nope.py"})),
            text("文件不存在，我换个方式"),
        ])
        CoderAgent(model, tools).execute(Task("t1", "读文件"))
        tool_messages = [m for m in model.calls[1]["messages"] if m.role == "tool"]
        assert tool_messages and tool_messages[0].content.startswith("ERROR")

    def test_round_limit(self, tools):
        script = [calls(("list_files", {"depth": 1 + index})) for index in range(20)]
        outcome = CoderAgent(FakeModel(script), tools, max_rounds=3).execute(
            Task("t1", "转圈"))
        assert not outcome.ok and outcome.stopped_reason == "round_limit"
        assert outcome.rounds == 3

    def test_repeat_guard(self, tools):
        same = calls(("list_files", {"depth": 1}))
        outcome = CoderAgent(FakeModel([same] * 10), tools,
                             repeat_limit=2).execute(Task("t1", "原地打转"))
        assert not outcome.ok and outcome.stopped_reason == "repeat_guard"
        assert "原地打转" in outcome.error

    def test_model_error_is_reported(self, tools):
        def boom(_messages):
            raise ModelError("网络错误: 拒绝连接")

        outcome = CoderAgent(FakeModel([boom]), tools).execute(Task("t1", "x"))
        assert not outcome.ok and outcome.stopped_reason == "model_error"
        assert "网络错误" in outcome.error

    def test_events_emitted(self, tools):
        events: list[AgentEvent] = []
        model = FakeModel([calls(("write_file", {"path": "a.py", "content": "1"})),
                           text("好了")])
        CoderAgent(model, tools, on_event=events.append).execute(Task("t1", "写"))
        assert any(event.kind == "tool_call" and event.data["arguments"] for event in events)

    def test_outcome_serializable(self, tools):
        outcome = CoderAgent(FakeModel([text("ok")]), tools).execute(Task("t1", "x"))
        assert outcome.to_dict()["ok"] is True


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------

class TestReviewer:
    def test_failed_outcome_is_rejected(self, tools):
        review = ReviewerAgent(tools=tools).review(
            Task("t1", "x"), TaskOutcome(ok=False, error="超轮次"))
        assert not review.approved and review.severity == "high"

    def test_no_changes_is_medium(self, tools):
        review = ReviewerAgent(tools=tools).review(
            Task("t1", "x"), TaskOutcome(ok=True, summary="啥也没干"))
        assert review.approved and review.severity == "medium"
        assert any("没有产生文件改动" in r for r in review.reasons)

    def test_syntax_error_blocks_approval(self, tools):
        tools.workspace.write("bad.py", "def broken(:\n")
        review = ReviewerAgent(tools=tools).review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["bad.py"]))
        assert not review.approved and review.severity == "high"

    def test_clean_change_is_approved(self, tools):
        tools.workspace.write("ok.py", "value = 1\n")
        review = ReviewerAgent(tools=tools).review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["ok.py"]))
        assert review.approved and not review.by_model

    def test_failing_tests_block_approval(self, tools):
        tools.workspace.write("ok.py", "value = 1\n")
        review = ReviewerAgent(tools=tools, test_command="exit 1").review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["ok.py"]))
        assert not review.approved and any("测试未通过" in r for r in review.reasons)

    def test_passing_tests_allow_approval(self, tools):
        tools.workspace.write("ok.py", "value = 1\n")
        review = ReviewerAgent(tools=tools, test_command="echo tests ok").review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["ok.py"]))
        assert review.approved

    def test_model_review_can_reject(self, tools):
        tools.workspace.write("ok.py", "value = 1\n")
        model = FakeModel([text('{"approved": false, "reasons": ["无关改动"], "severity": "medium"}')])
        review = ReviewerAgent(model, tools, use_model=True).review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["ok.py"]))
        assert not review.approved and review.by_model
        assert review.reasons == ["无关改动"]

    def test_model_review_can_approve(self, tools):
        tools.workspace.write("ok.py", "value = 1\n")
        model = FakeModel([text('{"approved": true, "reasons": ["没问题"]}')])
        assert ReviewerAgent(model, tools, use_model=True).review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["ok.py"])).approved

    def test_model_failure_does_not_silently_pass(self, tools):
        tools.workspace.write("ok.py", "value = 1\n")

        def boom(_messages):
            raise ModelError("连接失败")

        review = ReviewerAgent(FakeModel([boom]), tools, use_model=True).review(
            Task("t1", "x"), TaskOutcome(ok=True, touched=["ok.py"]))
        assert review.approved is True          # 确定性检查通过
        assert review.by_model is False
        assert any("不可用" in r for r in review.reasons), "模型失败必须留下理由"
        assert review.severity == "medium"

    def test_use_model_requires_model(self, tools):
        assert ReviewerAgent(None, tools, use_model=True).use_model is False

    def test_review_serializable(self, tools):
        review = ReviewerAgent(tools=tools).review(Task("t1", "x"), TaskOutcome(ok=True))
        assert set(review.to_dict()) >= {"approved", "reasons", "severity", "by_model"}


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def two_task_script(tools, *, fail_reviews: int = 0):
    """构造一个「两个任务，各自写一个文件」的模型脚本。"""
    script = [text(plan_json([
        {"title": "写 a.py", "files": ["a.py"]},
        {"title": "写 b.py", "files": ["b.py"]},
    ]))]
    for name in ("a.py", "b.py"):
        script.append(calls(("write_file", {"path": name, "content": "value = 1\n"})))
        script.append(text(f"已写入 {name}"))
    return FakeModel(script)


class TestOrchestrator:
    def test_plan_and_run_two_tasks(self, tools):
        model = two_task_script(tools)
        orchestrator = AgentOrchestrator(model, tools, max_parallel_coders=1)
        report = orchestrator.run("写两个文件")
        assert report.planned == 2 and report.done == 2 and report.failed == 0
        assert report.ok
        assert tools.workspace.exists("a.py") and tools.workspace.exists("b.py")
        assert report.tool_calls == 2

    def test_accounting_invariant(self, tools):
        report = AgentOrchestrator(two_task_script(tools), tools,
                                   max_parallel_coders=1).run("x")
        assert report.done + report.failed == report.planned

    def test_plan_fallback_event(self, tools):
        events: list[AgentEvent] = []
        model = FakeModel([text("我不会拆"), text("做完了")])
        report = AgentOrchestrator(model, tools, max_parallel_coders=1,
                                   on_event=events.append).run("随便做点")
        assert any(event.kind == "plan_fallback" for event in events)
        assert report.planned == 1

    def test_review_gate_rejects_then_retries(self, tools):
        """审查门驳回 → 重试；第 3 次通过。"""
        calls_seen = {"count": 0}

        def write_bad_then_good(_messages):
            calls_seen["count"] += 1
            if calls_seen["count"] <= 2:
                return calls(("write_file", {"path": "bad.py", "content": "def broken(:\n"}))
            return calls(("write_file", {"path": "bad.py", "content": "value = 1\n"}))

        model = FakeModel([
            text(plan_json([{"title": "写 bad.py"}])),
            write_bad_then_good,          # 第 1 次：语法错误 → 驳回
            text("写完了"),
            write_bad_then_good,          # 第 2 次：还是错 → 驳回
            text("又写完了"),
            write_bad_then_good,          # 第 3 次：正常
            text("这次对了"),
        ])
        report = AgentOrchestrator(model, tools, max_parallel_coders=1,
                                   max_coder_retry=2).run("写文件")
        assert report.rejected == 2 and report.retries == 2
        assert report.done == 1 and report.ok

    def test_max_coder_retry_zero(self, tools):
        model = FakeModel([
            text(plan_json([{"title": "x"}])),
            calls(("write_file", {"path": "bad.py", "content": "def broken(:\n"})),
            text("写完了"),
        ])
        report = AgentOrchestrator(model, tools, max_parallel_coders=1,
                                   max_coder_retry=0).run("x")
        assert report.retries == 0 and report.failed == 1
        assert report.tasks[0].status == "rejected"

    def test_review_gate_disabled(self, tools):
        events: list[AgentEvent] = []
        model = two_task_script(tools)
        report = AgentOrchestrator(model, tools, max_parallel_coders=1,
                                   review_gate_enabled=False,
                                   on_event=events.append).run("x")
        assert report.done == 2
        assert not any(event.kind == "review" for event in events)

    def test_coder_failure_marks_task_failed(self, tools):
        def boom(_messages):
            raise ModelError("炸了")

        model = FakeModel([text(plan_json([{"title": "x"}])), boom, boom, boom])
        report = AgentOrchestrator(model, tools, max_parallel_coders=1,
                                   max_coder_retry=1).run("x")
        assert report.failed == 1 and not report.ok
        assert report.tasks[0].status == "failed"
        assert report.retries == 1

    def test_cancel_before_run(self, tools):
        orchestrator = AgentOrchestrator(two_task_script(tools), tools,
                                         max_parallel_coders=1)
        orchestrator.cancel()
        report = orchestrator.run("x")
        assert report.cancelled and report.failed == 2

    def test_parallel_coders_actually_run_concurrently(self, tools):
        """两个 Coder 必须同时在飞：Barrier 超时会直接报 BrokenBarrierError。

        这是对 `max_parallel_coders` 的**真实**并发验证，而不是只看配置值。
        用按内容决策的无状态模型，避免共享脚本队列被乱序消费。
        """
        barrier = threading.Barrier(2)
        threads: list[str] = []
        lock = threading.Lock()

        def decide(messages) -> ChatResponse:
            contents = " ".join(m.content for m in messages)
            if "你是 Builder" in contents:                      # 规划阶段
                return text(plan_json([{"title": "写 a.py", "files": ["a.py"]},
                                       {"title": "写 b.py", "files": ["b.py"]}]))
            if any(m.role == "tool" for m in messages):         # 工具已执行 → 收尾
                return text("完成")
            with lock:
                threads.append(threading.current_thread().name)
                index = len(threads)
            barrier.wait(timeout=5)                             # 两个都到齐才继续
            return calls(("write_file", {"path": f"file{index}.py",
                                         "content": "value = 1\n"}))

        report = AgentOrchestrator(FunctionModel(decide), tools,
                                   max_parallel_coders=2).run("并行写两个文件")
        assert report.done == 2, f"并发没生效: {report.to_dict()}"
        assert len(set(threads)) == 2, f"两个任务应在不同线程: {threads}"

    def test_sequential_when_parallelism_is_one(self, tools):
        threads: list[str] = []

        def decide(messages) -> ChatResponse:
            contents = " ".join(m.content for m in messages)
            if "你是 Builder" in contents:
                return text(plan_json([{"title": "a"}, {"title": "b"}]))
            if any(m.role == "tool" for m in messages):
                return text("完成")
            threads.append(threading.current_thread().name)
            return calls(("list_files", {"depth": 1}))

        report = AgentOrchestrator(FunctionModel(decide), tools,
                                   max_parallel_coders=1).run("x")
        assert report.done == 2
        assert len(set(threads)) == 1, "并行度=1 时应当只用一个线程"

    def test_stats_and_summary(self, tools):
        report = AgentOrchestrator(two_task_script(tools), tools,
                                   max_parallel_coders=1).run("x")
        assert report.duration_ms >= 0
        assert "完成" in report.summary()
        assert report.to_dict()["ok"] is True

    def test_empty_plan(self, tools):
        report = AgentOrchestrator(FakeModel([]), tools).run_plan(Plan(goal="空"))
        assert report.planned == 0 and report.done == 0

    def test_run_agent_helper(self, tools):
        report = run_agent("写文件", tools, two_task_script(tools), max_parallel_coders=1)
        assert report.done == 2


    def test_events_include_review_result(self, tools):
        events: list[AgentEvent] = []
        AgentOrchestrator(two_task_script(tools), tools, max_parallel_coders=1,
                          on_event=events.append).run("x")
        reviews = [event for event in events if event.kind == "review"]
        assert len(reviews) == 2 and all(event.data["approved"] for event in reviews)
