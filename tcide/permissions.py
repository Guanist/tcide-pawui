# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""权限三档：**只读 / 逐次确认 / 自动**。

三个参照产品（Codex 的 Read-only/Auto/Full Access、DeepSeek Harness 的
permission-presets、Trae 的手动/自动/完全访问）**都**把"人工智能能自己动多少手"
做成了一个可以随时切换的档位 —— 而不是一个开/关。原因很直白：
"每步都确认"太烦、"完全放行"太险，用户需要的是**按任务风险临时调自主度**。

本模块是**纯策略**（不碰 Qt、不碰 PawUI、不碰文件系统），所以可以离线严格测试。
真正的拦截点在 ``tcide/tools.py`` 的 ``ToolRegistry.call()`` —— 那里是所有工具
调用的唯一咽喉，拦在那儿才不会漏（"fail closed"）。

设计取舍：拦截放在工具注册表而不是 agent 里 —— agent 的代码路径有好几条
（在线/离线/干跑），散着判迟早漏一条；注册表只有一个入口。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 三个档位（顺序即"自主度递增"，界面按此顺序展示）
LEVELS = ("read_only", "ask", "auto")

#: 界面显示名（状态栏与设置面板共用）
LABELS: dict[str, str] = {
    "read_only": "只读",
    "ask": "逐次确认",
    "auto": "自动",
}

#: 会改动磁盘的工具
WRITE_TOOLS = ("write_file", "edit_file")
#: 会执行命令的工具
SHELL_TOOLS = ("run_command",)
#: 需要把关的工具（写 + 执行）
GATED_TOOLS = WRITE_TOOLS + SHELL_TOOLS


def normalize_level(value: object, *, fallback: str = "ask") -> str:
    """把任意输入收敛成合法档位（不抛异常：设置文件可能被手改坏）。"""
    text = str(value or "").strip().lower()
    if text in LEVELS:
        return text
    # 兼容旧写法
    aliases = {"readonly": "read_only", "read-only": "read_only",
               "只读": "read_only", "confirm": "ask", "manual": "ask",
               "确认": "ask", "auto": "auto", "自动": "auto", "full": "auto"}
    return aliases.get(text, fallback)


@dataclass
class Decision:
    """一次工具调用的处置：``allow`` / ``ask`` / ``deny``。"""

    action: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def needs_review(self) -> bool:
        return self.action == "ask"

    @property
    def denied(self) -> bool:
        return self.action == "deny"


class PermissionPolicy:
    """按档位决定每个工具调用该怎么处置。"""

    def __init__(self, level: str = "ask") -> None:
        self.level = normalize_level(level)

    # -- 档位 -------------------------------------------------------------

    @property
    def label(self) -> str:
        return LABELS.get(self.level, self.level)

    @property
    def allows_write(self) -> bool:
        return self.level in ("ask", "auto")

    @property
    def allows_shell(self) -> bool:
        return self.level in ("ask", "auto")

    @property
    def review_gate_enabled(self) -> bool:
        """要交给 agent 的"是否把写操作排队等审核"标志。

        只读档下写操作**在工具层就被拒**了，所以这里返回 True 只是让 agent
        走审核路径（用户会看到一条"已拒绝"的记录，比静默消失清楚）。
        """
        return self.level != "auto"

    def next_level(self) -> str:
        """切到下一档（状态栏按钮用：只读 → 逐次确认 → 自动 → 只读）。"""
        index = LEVELS.index(self.level)
        return LEVELS[(index + 1) % len(LEVELS)]

    # -- 决策 -------------------------------------------------------------

    def decide(self, tool: str) -> Decision:
        """给一个工具名，返回处置决定。"""
        name = str(tool or "")
        if name not in GATED_TOOLS:
            return Decision("allow", "只读类工具，任何档位都放行")
        if self.level == "auto":
            return Decision("allow", "自动档：直接执行")
        if self.level == "read_only":
            kind = "写文件" if name in WRITE_TOOLS else "执行命令"
            return Decision(
                "deny",
                f"当前权限为「只读」，{kind}（{name}）已被拒绝。"
                f"要放行请在状态栏把权限切到「{LABELS['ask']}」"
                f"（写操作会进入审核队列）或「{LABELS['auto']}」。")
        return Decision("ask", f"当前权限为「{LABELS['ask']}」，需要人工确认")

    def describe(self) -> str:
        """一句话说明当前档位的行为（设置面板与状态提示用）。"""
        if self.level == "read_only":
            return "只读：AI 只能读代码，写文件与执行命令会被直接拒绝"
        if self.level == "auto":
            return "自动：AI 的写操作与命令直接执行（有风险，适合可丢弃的改动）"
        return "逐次确认：AI 的写操作先进入审核队列，由你决定接受或驳回"
