# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""trimmer.py 与 pressure.py 的测试（不依赖 Qt / 网络）。"""

from __future__ import annotations

import pytest

from tcide.pressure import DEFAULT_WEIGHTS, ContextPressure
from tcide.tokens import estimate_messages
from tcide.trimmer import ContextTrimmer, Message, TrimReport


def long_code(lines: int = 60) -> str:
    body = "\n".join(f"    value_{i} = {i}" for i in range(lines))
    return f"```python\ndef f():\n{body}\n    return 0\n```"


class TestPressure:
    def test_factors_are_normalized(self):
        pressure = ContextPressure(budget=1000)
        assert pressure.token_density(0) == 0.0
        assert pressure.token_density(1000) == 1.0
        assert pressure.token_density(500) == pytest.approx(0.5)
        assert pressure.token_density(99999) == 1.0  # 夹紧

    def test_weights_are_normalized(self):
        pressure = ContextPressure({"token_density": 2, "message_volume": 2})
        assert sum(pressure.weights.values()) == pytest.approx(1.0)
        assert pressure.weights["token_density"] == pytest.approx(0.5)

    def test_zero_weights_rejected(self):
        with pytest.raises(ValueError):
            ContextPressure({"a": 0})

    def test_score_monotonic_in_tokens(self):
        pressure = ContextPressure(budget=1000)
        low = pressure.evaluate(used_tokens=100).score
        high = pressure.evaluate(used_tokens=900).score
        assert low < high

    def test_levels(self):
        pressure = ContextPressure(budget=1000)
        assert pressure.evaluate(used_tokens=0).level == "low"
        assert pressure.evaluate(used_tokens=1000, messages=[Message("user", "x")] * 60,
                                 trailing_assistant_turns=9).level in ("high", "critical")

    def test_recommendation_is_non_empty(self):
        pressure = ContextPressure(budget=1000)
        for used in (0, 400, 700, 1000):
            assert pressure.evaluate(used_tokens=used).recommendation

    def test_report_serializable(self):
        report = ContextPressure(budget=1000).evaluate(used_tokens=500)
        data = report.to_dict()
        assert set(data) == {"score", "level", "factors", "recommendation"}
        assert all(isinstance(v, float) for v in data["factors"].values())

    def test_default_weights_sum_to_one(self):
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_role_diversity(self):
        pressure = ContextPressure(budget=1000)
        assert pressure.role_diversity([]) == 0.0
        assert pressure.role_diversity(["user", "assistant", "system", "tool"]) == 1.0
        assert pressure.role_diversity(["user", "user"]) == pytest.approx(0.25)


class TestTrimmerBasics:
    def test_under_budget_is_untouched(self):
        trimmer = ContextTrimmer(budget=10000)
        messages = [Message("user", "你好"), Message("assistant", "好的")]
        out, report = trimmer.trim(messages)
        assert [m.content for m in out] == ["你好", "好的"]
        assert report.layers == []
        assert report.saved == 0
        assert report.exact

    def test_does_not_mutate_input(self):
        trimmer = ContextTrimmer(budget=50)
        message = Message("user", long_code(80))
        original = message.content
        trimmer.trim([message])
        assert message.content == original

    def test_invalid_budget(self):
        with pytest.raises(ValueError):
            ContextTrimmer(budget=0)
        with pytest.raises(ValueError):
            ContextTrimmer(budget=100, keep_recent=-1)


class TestTrimmerLayers:
    def test_dependency_files_are_limited_first(self):
        # 6 个文件各 240 字符 ≈ 60 token → 合计 ~380 token，预算 150 必然超
        trimmer = ContextTrimmer(budget=150, keep_recent=0, max_depend_files=1)
        message = Message("user", "看这几个文件",
                          files={f"file{i}.py": "x = 1\n" * 40 for i in range(6)})
        out, report = trimmer.trim([message])
        assert report.layers[0] == "limit-dependency-files"
        assert len(out[0].files) == 1
        assert report.dropped_files == 5

    def test_code_blocks_are_shortened(self):
        # 120 行代码 ≈ 500 token，预算 250 必然超
        trimmer = ContextTrimmer(budget=250, keep_recent=0)
        out, report = trimmer.trim([Message("user", long_code(120))])
        assert "shorten-code-blocks" in report.layers
        assert "省略" in out[0].content

    def test_old_dialogs_are_dropped(self):
        trimmer = ContextTrimmer(budget=300, keep_recent=1)
        messages = [Message("user", f"第{i}条消息 " + "细节" * 20) for i in range(8)]
        out, report = trimmer.trim(messages)
        assert "drop-old-dialogs" in report.layers
        assert len(out) < len(messages)
        assert out[-1].content.endswith("细节" * 20)  # 最近一条保留

    def test_pinned_messages_survive(self):
        trimmer = ContextTrimmer(budget=200, keep_recent=1)
        messages = [Message("system", "PROJECT RULES " + "规" * 50, pinned=True)]
        messages += [Message("user", f"msg{i} " + "内容" * 30) for i in range(6)]
        out, _ = trimmer.trim(messages)
        assert any("PROJECT RULES" in m.content for m in out)

    def test_hard_truncate_as_last_resort(self):
        # 4000 个中文字符 ≈ 4000 token，预算 200 必须硬截断
        trimmer = ContextTrimmer(budget=200, keep_recent=0)
        out, report = trimmer.trim([Message("user", "填充" * 2000)])
        assert "hard-truncate" in report.layers
        assert report.after_tokens < report.before_tokens

    def test_conversation_is_never_emptied(self):
        """回归：keep_recent=0 时旧实现会把整段对话裁空，模型收到空上下文。"""
        trimmer = ContextTrimmer(budget=50, keep_recent=0)
        out, _ = trimmer.trim([Message("user", "只有这一条" * 500)])
        assert out, "裁剪不能把对话清空"
        assert out[-1].content.strip()

    def test_last_message_always_survives(self):
        trimmer = ContextTrimmer(budget=50, keep_recent=0)
        messages = [Message("user", "旧消息" * 300) for _ in range(5)]
        out, _ = trimmer.trim(messages)
        assert out, "最后一条必须保留"
        assert "旧消息" in out[-1].content

    def test_attachments_are_shrunk_when_content_is_short(self):
        """回归：内容很短但附件巨大时，必须能削附件，否则预算永远压不下来。"""
        trimmer = ContextTrimmer(budget=120, keep_recent=0, max_depend_files=99)
        message = Message("user", "看", files={"big.py": "value = 1\n" * 400})
        out, report = trimmer.trim([message])
        assert report.after_tokens <= report.budget, report.describe()
        assert report.exact
        assert report.dropped_files or out[0].files["big.py"] != "value = 1\n" * 400

    def test_pinned_rules_survive_when_others_can_be_cut(self):
        trimmer = ContextTrimmer(budget=400, keep_recent=1)
        messages = [
            Message("system", "PROJECT RULES " + "规" * 100, pinned=True),
            Message("user", "旧" * 400),
            Message("user", "新问题"),
        ]
        out, _ = trimmer.trim(messages)
        rules = [m for m in out if m.pinned]
        assert rules and "PROJECT RULES" in rules[0].content
        assert rules[0].content.count("规") == 100, "pinned 内容不应被压缩"

    def test_pinned_is_touched_only_as_last_resort(self):
        trimmer = ContextTrimmer(budget=150, keep_recent=0)
        messages = [Message("system", "规则" * 2000, pinned=True)]
        out, report = trimmer.trim(messages)
        assert out, "对话不能清空"
        assert report.after_tokens <= report.budget, report.describe()
        assert report.truncated_messages >= 1

    def test_newest_message_code_block_is_shortened(self):
        """最新一条消息里的代码块也应该先被压缩，而不是直接硬截断。"""
        trimmer = ContextTrimmer(budget=260, keep_recent=0)
        out, report = trimmer.trim([Message("user", long_code(140))])
        assert "shorten-code-blocks" in report.layers
        assert report.exact

    def test_layers_are_in_declared_order(self):
        trimmer = ContextTrimmer(budget=200, keep_recent=0, max_depend_files=0)
        messages = [Message("user", long_code(200), files={"a.py": "x" * 400})]
        messages += [Message("user", "旧消息" * 100) for _ in range(6)]
        _, report = trimmer.trim(messages)
        declared = ["limit-dependency-files", "shorten-code-blocks",
                    "drop-old-dialogs", "hard-truncate"]
        indices = [declared.index(name) for name in report.layers]
        assert indices == sorted(indices), f"层次顺序不对: {report.layers}"

    def test_static_context_counts_toward_budget(self):
        trimmer = ContextTrimmer(budget=300, keep_recent=0)
        messages = [Message("user", "短")]
        _, without = trimmer.trim(messages)
        _, with_static = trimmer.trim(messages, static_context="规则" * 500)
        assert with_static.after_tokens > without.after_tokens

    def test_report_serializable_and_describable(self):
        trimmer = ContextTrimmer(budget=300, keep_recent=0)
        _, report = trimmer.trim([Message("user", long_code(100))])
        assert isinstance(report, TrimReport)
        assert set(report.to_dict()) >= {"before_tokens", "after_tokens", "layers", "saved"}
        assert isinstance(report.describe(), str)

    def test_budget_is_respected_when_possible(self):
        trimmer = ContextTrimmer(budget=500, keep_recent=0)
        messages = [Message("user", "内容" * 300) for _ in range(10)]
        out, report = trimmer.trim(messages)
        assert report.after_tokens < report.before_tokens
        assert out  # 至少留一条，不能裁成空

    def test_estimate_matches_report(self):
        trimmer = ContextTrimmer(budget=10000)
        messages = [Message("user", "abc"), Message("assistant", "def")]
        out, report = trimmer.trim(messages)
        assert report.after_tokens == estimate_messages(out)
