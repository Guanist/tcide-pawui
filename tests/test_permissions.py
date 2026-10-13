# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""权限三档（只读 / 逐次确认 / 自动）的测试。

重点验证**拦截是真的**，不是文案：只读档下 ``ToolRegistry.call("write_file")``
必须既不写盘、又给出可操作的拒绝理由（fail closed + 告诉用户怎么放行）。
"""

from __future__ import annotations

import pytest

from tcide.permissions import (GATED_TOOLS, LABELS, LEVELS, WRITE_TOOLS,
                               Decision, PermissionPolicy, normalize_level)
from tcide.settings import Settings, load, normalize
from tcide.tools import ToolRegistry
from tcide.workspace import Workspace


class TestLevels:
    def test_three_levels_in_rising_autonomy(self):
        assert LEVELS == ("read_only", "ask", "auto")

    def test_every_level_has_a_label(self):
        for level in LEVELS:
            assert LABELS[level], f"{level} 没有显示名"

    def test_normalize_accepts_aliases_and_unknown_falls_back(self):
        assert normalize_level("READ-ONLY") == "read_only"
        assert normalize_level("只读") == "read_only"
        assert normalize_level("确认") == "ask"
        assert normalize_level("full") == "auto"
        assert normalize_level("乱写的东西") == "ask"
        assert normalize_level(None, fallback="read_only") == "read_only"

    def test_next_level_cycles(self):
        policy = PermissionPolicy("read_only")
        seen = []
        for _ in range(4):
            seen.append(policy.level)
            policy = PermissionPolicy(policy.next_level())
        assert seen == ["read_only", "ask", "auto", "read_only"]


class TestDecisionMatrix:
    def test_read_only_denies_writes_and_shell(self):
        policy = PermissionPolicy("read_only")
        for tool in GATED_TOOLS:
            decision = policy.decide(tool)
            assert decision.denied, f"{tool} 在只读档下应当被拒绝"
            assert "只读" in decision.reason

    def test_read_only_still_allows_reading(self):
        policy = PermissionPolicy("read_only")
        for tool in ("read_file", "list_files", "search_code", "git_status"):
            assert policy.decide(tool).allowed, f"{tool} 是只读工具，不该被拦"

    def test_ask_queues_instead_of_denying(self):
        policy = PermissionPolicy("ask")
        for tool in GATED_TOOLS:
            decision = policy.decide(tool)
            assert decision.needs_review and not decision.denied

    def test_auto_allows_everything(self):
        policy = PermissionPolicy("auto")
        for tool in GATED_TOOLS + ("read_file",):
            assert policy.decide(tool).allowed

    def test_denial_message_tells_the_user_how_to_unblock(self):
        """拒绝必须**可操作** —— 只说"被拒绝"是最没用的报错。"""
        reason = PermissionPolicy("read_only").decide("write_file").reason
        assert LABELS["ask"] in reason and LABELS["auto"] in reason

    def test_review_gate_flag_follows_the_level(self):
        assert PermissionPolicy("read_only").review_gate_enabled is True
        assert PermissionPolicy("ask").review_gate_enabled is True
        assert PermissionPolicy("auto").review_gate_enabled is False


class TestToolRegistryEnforcement:
    """**决定性验证**：只读档下写文件必须真的不发生。"""

    def test_read_only_write_is_blocked_and_file_absent(self, tmp_path):
        workspace = Workspace(tmp_path)
        registry = ToolRegistry(workspace)
        registry.policy = PermissionPolicy("read_only")

        result = registry.call("write_file", {"path": "a.txt", "content": "x"})

        assert result.ok is False, "只读档下写文件居然成功了"
        assert not (tmp_path / "a.txt").exists(), "文件被写出来了 —— 拦截是假的"
        assert registry.calls[-1].get("denied") is True, "调用记录里应标记为被拒"

    def test_ask_level_write_goes_through_the_registry(self, tmp_path):
        """逐次确认档：注册表**不**拦（由审核队列负责），文件确实会写。"""
        workspace = Workspace(tmp_path)
        registry = ToolRegistry(workspace)
        registry.policy = PermissionPolicy("ask")

        result = registry.call("write_file", {"path": "b.txt", "content": "y"})

        assert result.ok is True
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "y"

    def test_policy_none_means_no_enforcement(self, tmp_path):
        """库本身不假设一定有权限模型（集成方可以不用）。"""
        workspace = Workspace(tmp_path)
        registry = ToolRegistry(workspace)
        registry.policy = None
        assert registry.call("write_file", {"path": "c.txt", "content": "z"}).ok


class TestSettingsMigration:
    def test_default_level_is_ask(self):
        assert Settings().permission == "ask"

    def test_old_review_gate_false_migrates_to_auto(self):
        """旧配置里 ``review_gate=false`` 表示"不审核" —— 应迁移成 auto，别悄悄收紧。"""
        old = normalize(Settings(permission="", review_gate=False))
        assert old.permission == "auto"

    def test_old_review_gate_true_migrates_to_ask(self):
        assert normalize(Settings(permission="", review_gate=True)).permission == "ask"

    def test_invalid_level_falls_back(self):
        assert normalize(Settings(permission="nonsense")).permission == "ask"

    def test_review_gate_stays_consistent_with_the_level(self):
        assert normalize(Settings(permission="auto")).review_gate is False
        assert normalize(Settings(permission="ask")).review_gate is True

    def test_round_trip_through_the_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TCIDE_CONFIG_DIR", str(tmp_path))
        from tcide.settings import save

        save(Settings(permission="read_only"))
        assert load().permission == "read_only"
