# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""计划模式：**先方案、人批了再动手**。

三个参照产品都有对应物：
- Codex 的 ``/plan``（先看计划，再在线批准或拒绝步骤）
- DeepSeek Harness 的 ``plan-mode`` + ``exit_plan_mode``（方案落盘、轮次尾部保留卡片）
- Trae 的 Plan / Spec / Goal 三种粒度（落盘到 ``.trae/`` 目录）

共同点：**"让 AI 先想清楚"必须是一个可执行的机制，而不是一句提示词**。
本项目的做法更硬一点 —— 计划模式会把权限档位**强制成只读**
（见 ``main.py`` 的 ``_set_permission``）：这样即使模型不听话，它也**物理上写不了文件**。

本模块只做方案的**数据与落盘**（不碰 Qt），所以能离线严格测试。
"""

from __future__ import annotations

import datetime as _datetime
import pathlib
import re
from dataclasses import dataclass, field

#: 方案落盘目录（相对项目根）
PLANS_DIR = ".tcide/plans"

_SLUG_SAFE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
#: Markdown 待办项：``- [ ] 步骤`` / ``- [x] 步骤``
_STEP = re.compile(r"^\s*[-*]\s*\[(?P<done>[ xX])\]\s*(?P<text>.+?)\s*$")


def slugify(title: str, *, fallback: str = "plan") -> str:
    """把标题变成安全的文件名（保留中文，去掉路径分隔符等）。"""
    text = (title or "").strip().lower()
    text = _SLUG_SAFE.sub("-", text).strip("-")
    return (text or fallback)[:60]


@dataclass
class PlanStep:
    """方案里的一步（对应 Markdown 的 ``- [ ]`` 待办项）。"""

    text: str
    done: bool = False

    def line(self) -> str:
        return f"- [{'x' if self.done else ' '}] {self.text}"


@dataclass
class Plan:
    """一份方案：标题 + 正文 + 可勾选的步骤。"""

    title: str
    body: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    slug: str = ""
    created: str = ""

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.title)
        if not self.created:
            self.created = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_markdown(cls, text: str, *, slug: str = "",
                      created: str = "") -> "Plan":
        """从 Markdown 解析方案（第一行 ``# 标题``，``- [ ]`` 行为步骤）。"""
        lines = (text or "").splitlines()
        title = ""
        body_lines: list[str] = []
        steps: list[PlanStep] = []
        for line in lines:
            stripped = line.strip()
            if not title and stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                continue
            match = _STEP.match(line)
            if match:
                steps.append(PlanStep(match.group("text"),
                                      done=match.group("done").lower() == "x"))
                continue
            body_lines.append(line)
        return cls(title=title or "未命名方案",
                   body="\n".join(body_lines).strip("\n"),
                   steps=steps, slug=slug, created=created)

    # -- 展示 -------------------------------------------------------------

    def to_markdown(self) -> str:
        parts = [f"# {self.title}", "",
                 f"> 生成于 {self.created}（tcide 计划模式）", ""]
        if self.body:
            parts += [self.body, ""]
        if self.steps:
            parts += ["## 步骤", ""]
            parts += [step.line() for step in self.steps]
        return "\n".join(parts).rstrip() + "\n"

    @property
    def progress(self) -> tuple[int, int]:
        return (sum(1 for step in self.steps if step.done), len(self.steps))

    def summary(self) -> str:
        done, total = self.progress
        if not total:
            return f"方案：{self.title}"
        return f"方案：{self.title}（{done}/{total} 步）"

    def is_empty(self) -> bool:
        return not (self.title.strip() and (self.body.strip() or self.steps))


class PlanStore:
    """方案的落盘与读取（``<项目根>/.tcide/plans/*.md``）。"""

    def __init__(self, root) -> None:
        self.root = pathlib.Path(root)
        self.dir = self.root / PLANS_DIR

    def path_for(self, plan: Plan) -> pathlib.Path:
        return self.dir / f"{plan.slug or slugify(plan.title)}.md"

    def save(self, plan: Plan) -> pathlib.Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.path_for(plan)
        target.write_text(plan.to_markdown(), encoding="utf-8", newline="\n")
        return target

    def load(self, slug: str) -> Plan | None:
        target = self.dir / f"{slug}.md"
        if not target.is_file():
            return None
        return Plan.from_markdown(target.read_text(encoding="utf-8"), slug=slug)

    def list(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(item.stem for item in self.dir.glob("*.md"))

    def latest(self) -> Plan | None:
        if not self.dir.is_dir():
            return None
        files = sorted(self.dir.glob("*.md"),
                       key=lambda item: item.stat().st_mtime)
        if not files:
            return None
        newest = files[-1]
        return Plan.from_markdown(newest.read_text(encoding="utf-8"),
                                  slug=newest.stem)


def plan_prompt(requirement: str) -> str:
    """交给模型的计划提示词（离线模型也会照这个骨架产出）。"""
    return (
        "你正在**计划模式**：只允许阅读与思考，不要修改任何文件。\n"
        "请针对下面的需求给出一份可执行方案，格式要求：\n"
        "1. 第一行 `# 标题`；\n"
        "2. 一段说明（改哪些文件、为什么这样改、有什么风险）；\n"
        "3. `## 步骤` 下面用 `- [ ] 步骤` 列出可勾选的步骤，粒度要能单独验证。\n\n"
        f"需求：{requirement}"
    )


def draft_plan(requirement: str, *, files: list[str] | None = None) -> Plan:
    """离线可用的方案草稿（不联网也能演示计划模式）。

    这不是"假实现"：它产出的确实是一份可读、可勾选、可落盘的方案骨架，
    只是内容来自本地线索（涉及的文件、步骤模板），而不是模型推理。
    界面上会明确标注这是草稿。
    """
    touched = [name for name in (files or []) if name][:6]
    listed = "、".join(f"`{name}`" for name in touched) if touched else "（待确定）"
    body = (
        f"**需求**：{requirement}\n\n"
        f"**涉及文件（本地线索）**：{listed}\n\n"
        "**做法**：先读相关代码确认现状，再最小改动；每改一处就补一条能离线跑的测试，"
        "最后跑全量测试确认没有回归。**不引入新依赖**。"
    )
    steps = [
        PlanStep(f"阅读与「{requirement[:20]}」相关的代码，确认现状与边界"),
        PlanStep("列出具体改动点（文件 + 行为），标出风险"),
        PlanStep("实现改动（最小化，不动无关代码）"),
        PlanStep("补离线测试（含一条「证明真的生效」的断言）"),
        PlanStep("跑全量测试与界面自检，确认无回归"),
        PlanStep("更新文档，说明限制与遗留"),
    ]
    return Plan(title=f"计划：{requirement[:40]}", body=body, steps=steps)
