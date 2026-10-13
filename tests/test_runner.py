# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""runner.py 的测试：超时 / 重试 / 检查点 / 恢复 / 日志 / 取消。"""

from __future__ import annotations

import json
import shutil
import threading
import time

import pytest

from tcide.runner import CHECKPOINT_FILE, Step, StepResult, UnattendedRunner

HAS_PYTHON = shutil.which("python") is not None
SLEEP = 'python -c "import time; time.sleep(10)"'


@pytest.fixture
def log_dir(project):
    path = project / "run-logs"
    path.mkdir(exist_ok=True)
    return path


def runner(steps, log_dir, **kwargs) -> UnattendedRunner:
    return UnattendedRunner(steps, log_dir=log_dir, cwd=log_dir, **kwargs)


class TestStepExecution:
    def test_all_steps_succeed(self, log_dir):
        report = runner([Step("a", command="echo a"), Step("b", command="echo b")],
                        log_dir).run()
        assert report.ok and report.done == 2 and report.failed == 0
        assert [s.name for s in report.steps] == ["a", "b"]

    def test_failing_command(self, log_dir):
        report = runner([Step("bad", command="exit 1")], log_dir).run()
        assert report.failed == 1 and "退出码 1" in report.steps[0].error

    def test_expect_exit_zero_false(self, log_dir):
        report = runner([Step("tolerant", command="exit 3", expect_exit_zero=False)],
                        log_dir).run()
        assert report.ok

    def test_action_step(self, log_dir):
        report = runner([Step("act", action=lambda: "结果")], log_dir).run()
        assert report.ok and report.steps[0].output == "结果"

    def test_action_exception(self, log_dir):
        def boom():
            raise RuntimeError("炸了")

        report = runner([Step("act", action=boom)], log_dir).run()
        assert report.failed == 1 and "RuntimeError" in report.steps[0].error

    def test_step_without_command_or_action(self, log_dir):
        report = runner([Step("empty")], log_dir).run()
        assert report.failed == 1 and "既没有" in report.steps[0].error


class TestRetries:
    def test_default_retry_from_runner(self, log_dir):
        """retries=None 时沿用执行器的 max_retries。"""
        report = runner([Step("bad", command="exit 1")], log_dir,
                        max_retries=1).run()
        assert report.steps[0].attempts == 2

    def test_explicit_zero_means_no_retry(self, log_dir):
        report = runner([Step("bad", command="exit 1", retries=0)], log_dir,
                        max_retries=3).run()
        assert report.steps[0].attempts == 1

    def test_explicit_retries(self, log_dir):
        report = runner([Step("bad", command="exit 1", retries=2)], log_dir).run()
        assert report.steps[0].attempts == 3

    def test_retry_until_success(self, log_dir):
        state = {"count": 0}

        def flaky():
            state["count"] += 1
            if state["count"] < 3:
                raise RuntimeError("再试一次")
            return "终于成功"

        report = runner([Step("flaky", action=flaky, retries=3)], log_dir).run()
        assert report.ok and report.steps[0].attempts == 3


class TestTimeouts:
    @pytest.mark.skipif(not HAS_PYTHON, reason="需要 python 可执行文件")
    def test_command_step_timeout(self, log_dir):
        report = runner([Step("slow", command=SLEEP, timeout=1)], log_dir).run()
        assert report.steps[0].timed_out and not report.ok

    def test_action_step_timeout_is_reported_honestly(self, log_dir):
        report = runner([Step("slow", action=lambda: time.sleep(10), timeout=0.3)],
                        log_dir).run()
        step = report.steps[0]
        assert step.timed_out
        assert "无法被强杀" in step.error, "必须说明线程不能被杀，而不是假装成功"

    def test_global_timeout_stops_remaining_steps(self, log_dir):
        # 步骤必须真的够慢，否则 20 个 echo 一秒内就跑完，全局超时永远不会触发
        # （我第一版就是拿 echo 写的，断言必然失败）
        steps = [Step(f"s{i}", action=lambda: time.sleep(0.3)) for i in range(10)]
        report = runner(steps, log_dir, global_timeout=0.8).run()
        assert report.global_timeout_hit, report.summary()
        assert not report.ok
        assert report.skipped >= 1, "超时后剩余步骤应标记为未执行"

    @pytest.mark.skipif(not HAS_PYTHON, reason="需要 python 可执行文件")
    def test_global_timeout_with_slow_steps(self, log_dir):
        report = runner([Step("slow", command=SLEEP, timeout=30)],
                        log_dir, global_timeout=1.0).run()
        assert report.steps[0].timed_out


class TestCheckpoints:
    def test_checkpoint_written(self, log_dir):
        steps = [Step("a", command="echo a", checkpoint=True),
                 Step("b", command="echo b")]
        report = runner(steps, log_dir).run()
        assert report.checkpoints == 1
        state = json.loads((log_dir / CHECKPOINT_FILE).read_text(encoding="utf-8"))
        assert state["completed"] == ["a"]

    def test_resume_skips_completed(self, log_dir):
        steps = [Step("a", command="echo a", checkpoint=True),
                 Step("b", command="exit 1")]
        runner(steps, log_dir).run()          # 第一次：a 完成，b 失败
        second = runner(steps, log_dir).run(resume=True)
        assert second.steps[0].skipped and second.steps[0].ok
        assert second.failed == 1             # b 仍然失败（没有被跳过）

    def test_resume_false_reruns_everything(self, log_dir):
        steps = [Step("a", command="echo a", checkpoint=True)]
        runner(steps, log_dir).run()
        again = runner(steps, log_dir).run(resume=False)
        assert not again.steps[0].skipped

    def test_checkpoints_capped(self, log_dir):
        steps = [Step(f"s{i}", command="echo x", checkpoint=True) for i in range(6)]
        report = runner(steps, log_dir, max_checkpoints=2).run()
        state = json.loads((log_dir / CHECKPOINT_FILE).read_text(encoding="utf-8"))
        assert len(state["checkpoints"]) <= 2
        assert report.checkpoints == 6, "计数是本次新增的检查点数"

    def test_corrupted_state_is_handled(self, log_dir):
        (log_dir / CHECKPOINT_FILE).write_text("{ 不是 json", encoding="utf-8")
        report = runner([Step("a", command="echo a")], log_dir).run()
        assert report.done == 1

    def test_failed_step_is_not_checkpointed(self, log_dir):
        report = runner([Step("bad", command="exit 1", checkpoint=True)], log_dir).run()
        assert report.checkpoints == 0
        assert not (log_dir / CHECKPOINT_FILE).exists() or json.loads(
            (log_dir / CHECKPOINT_FILE).read_text(encoding="utf-8"))["completed"] == []


class TestLoggingAndEvents:
    def test_log_file_written(self, log_dir):
        report = runner([Step("a", command="echo hello")], log_dir).run()
        content = open(report.log_file, encoding="utf-8").read()
        assert "完成 a" in content
        assert "结束" in content

    def test_failure_is_logged(self, log_dir):
        report = runner([Step("bad", command="exit 1")], log_dir,
                        max_retries=0).run()
        content = open(report.log_file, encoding="utf-8").read()
        assert "失败 bad" in content

    def test_events_emitted(self, log_dir):
        events: list[tuple[str, dict]] = []
        runner([Step("a", command="echo a", checkpoint=True)], log_dir,
               on_event=lambda kind, data: events.append((kind, data))).run()
        kinds = [kind for kind, _ in events]
        assert "step_start" in kinds and "checkpoint" in kinds and "done" in kinds

    def test_failure_event(self, log_dir):
        events: list[tuple[str, dict]] = []
        runner([Step("bad", command="exit 1")], log_dir, max_retries=0,
               on_event=lambda kind, data: events.append((kind, data))).run()
        assert any(kind == "step_failed" for kind, _ in events)


class TestCancel:
    def test_cancel_before_run(self, log_dir):
        instance = runner([Step("a", command="echo a")], log_dir)
        instance.cancel()
        report = instance.run()
        assert report.cancelled and report.steps[0].skipped

    def test_cancel_during_run(self, log_dir):
        instance = runner([Step(f"s{i}", command="echo x") for i in range(50)],
                          log_dir)

        def stopper():
            time.sleep(0.15)
            instance.cancel()

        threading.Thread(target=stopper, daemon=True).start()
        report = instance.run()
        assert report.cancelled
        assert report.done < len(instance.steps)


class TestReportShape:
    def test_report_serializable(self, log_dir):
        report = runner([Step("a", command="echo a")], log_dir).run()
        data = report.to_dict()
        assert set(data) >= {"ok", "steps", "done", "failed", "skipped",
                             "duration_ms", "cancelled", "log_file"}
        assert json.dumps(data, ensure_ascii=False)

    def test_summary_mentions_state(self, log_dir):
        assert "成功" in runner([Step("a", command="echo a")], log_dir).run().summary()
        assert "失败" in runner([Step("b", command="exit 1")], log_dir,
                               max_retries=0).run().summary()

    def test_step_result_serializable(self):
        assert StepResult("x", ok=True).to_dict()["name"] == "x"

    def test_step_serializable(self):
        assert Step("x", command="echo").to_dict()["command"] == "echo"
