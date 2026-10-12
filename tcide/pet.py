# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""像素宠物：9 状态机 + 帧动画 + 绘制指令。

对应 TCIDE 原版 README 的「**像素宠物** — 桌面虎猫精灵，实时显示 AI 状态」
（`src/renderer/pet.ts` + `pet-window.html` + `pet_assets/`，9 状态精灵动画）。

与版不同：原版用 PNG 精灵图；这里**用绘制指令程序化画出来**（耳朵/眼睛/尾巴/胡须），
因此不依赖任何素材文件，且每个状态都**可离线断言**：

- :class:`PetStateMachine` —— 状态机（合法迁移、事件映射、空闲自动睡觉）
- :class:`PetAnimator` —— 每个状态的帧序列与按时间取帧
- :class:`PetPainter` —— ``plan()`` 产出**纯数据**绘制指令，``render()`` 再喂给画布
  （PawUI 的 ``<Canvas>`` 提供 clear/rect/line/circle/text/polygon）

前两者是纯逻辑；``plan()`` 也是纯函数，只有 ``render()`` 需要画布对象，
所以整只猫都能在没有 GUI 的情况下测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: 9 个状态（与原版宣称一致）
STATES: tuple[str, ...] = (
    "idle",       # 待机
    "thinking",   # 思考（Builder 规划）
    "working",    # 干活（Coder 执行）
    "reviewing",  # 审查
    "waiting",    # 等待模型/用户
    "happy",      # 开心（任务通过）
    "done",       # 全部完成
    "error",      # 出错
    "sleeping",   # 睡着了（长时间没事做）
)

#: 合法迁移（含自环）；不在表里的迁移会被拒绝
ALLOWED: dict[str, frozenset[str]] = {
    "idle": frozenset({"thinking", "working", "waiting", "happy", "done",
                       "error", "sleeping", "idle"}),
    "thinking": frozenset({"working", "waiting", "reviewing", "happy", "error",
                           "done", "idle", "thinking"}),
    "working": frozenset({"reviewing", "happy", "error", "waiting", "done",
                          "idle", "working"}),
    "reviewing": frozenset({"happy", "error", "waiting", "working", "done",
                            "idle", "reviewing"}),
    "waiting": frozenset({"thinking", "working", "idle", "error", "done",
                          "sleeping", "waiting"}),
    "happy": frozenset({"done", "idle", "thinking", "working", "sleeping", "happy"}),
    "done": frozenset({"idle", "thinking", "working", "sleeping", "done"}),
    "error": frozenset({"idle", "thinking", "working", "waiting", "done", "error"}),
    "sleeping": frozenset({"idle", "thinking", "working", "error", "waiting",
                           "sleeping"}),
}

#: 编排事件 -> 宠物状态
EVENT_TO_STATE: dict[str, str] = {
    "plan": "thinking",
    "plan_fallback": "thinking",
    "task_start": "working",
    "tool_call": "working",
    "review": "reviewing",
    "retry": "waiting",
    "loop_guard": "error",
    "round_limit": "error",
    "attempt_failed": "error",
    "task_failed": "error",
    "task_done": "happy",
    "checkpoint": "working",
    "step_start": "working",
    "step_failed": "error",
    "done": "done",
}

#: 每个状态的中文标签（界面气泡用）
LABELS: dict[str, str] = {
    "idle": "待机中",
    "thinking": "正在拆解任务…",
    "working": "正在写代码…",
    "reviewing": "正在审查改动…",
    "waiting": "等待中…",
    "happy": "这个任务搞定啦",
    "done": "全部完成！",
    "error": "出错了，去看看",
    "sleeping": "Zzz…",
}


@dataclass
class PetFrame:
    """一帧：精灵序号 + 持续时间。"""

    index: int
    duration_ms: int = 200


#: 每个状态的帧序列（程序化绘制，index 决定眼睛/尾巴的姿态）
ANIMATIONS: dict[str, list[PetFrame]] = {
    "idle": [PetFrame(0, 500), PetFrame(1, 500)],
    "thinking": [PetFrame(0, 220), PetFrame(1, 220), PetFrame(2, 220)],
    "working": [PetFrame(0, 120), PetFrame(1, 120), PetFrame(2, 120),
                PetFrame(3, 120)],
    "reviewing": [PetFrame(0, 300), PetFrame(1, 300)],
    "waiting": [PetFrame(0, 400), PetFrame(1, 400), PetFrame(0, 400)],
    "happy": [PetFrame(0, 150), PetFrame(2, 150), PetFrame(0, 150),
              PetFrame(2, 150)],
    "done": [PetFrame(0, 300), PetFrame(3, 300)],
    "error": [PetFrame(0, 180), PetFrame(1, 180)],
    "sleeping": [PetFrame(0, 900), PetFrame(1, 900)],
}

#: 状态主色（画的时候用）
STATE_COLORS: dict[str, str] = {
    "idle": "#f5a623", "thinking": "#4a9eff", "working": "#22d3ee",
    "reviewing": "#a78bfa", "waiting": "#94a3b8", "happy": "#34d399",
    "done": "#34d399", "error": "#ff453a", "sleeping": "#64748b",
}


@dataclass
class PetSnapshot:
    """某一刻的宠物状态。"""

    state: str = "idle"
    previous: str = ""
    since: float = 0.0
    elapsed_ms: int = 0
    frame_index: int = 0
    label: str = ""
    color: str = ""
    history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"state": self.state, "previous": self.previous,
                "elapsed_ms": self.elapsed_ms, "frame_index": self.frame_index,
                "label": self.label, "color": self.color,
                "history": list(self.history[-8:])}


class PetStateMachine:
    """状态机：合法迁移 + 事件映射 + 空闲自动睡觉。"""

    def __init__(self, *, idle_timeout_ms: int = 60_000,
                 clock=time.monotonic, animator: "PetAnimator | None" = None) -> None:
        self.idle_timeout_ms = max(1000, int(idle_timeout_ms))
        self.clock = clock
        self.animator = animator or PetAnimator()
        self.state = "idle"
        self.previous = ""
        self.since = self.clock()
        self.history: list[str] = ["idle"]
        self.rejected: list[tuple[str, str]] = []

    # -- 迁移 -------------------------------------------------------------

    def can_transition(self, target: str) -> bool:
        return target in STATES and target in ALLOWED.get(self.state, frozenset())

    def transition(self, target: str) -> bool:
        """迁移到目标状态；不合法则**拒绝并记录**（返回 False，状态不变）。"""
        if target not in STATES:
            self.rejected.append((self.state, target))
            return False
        if not self.can_transition(target):
            self.rejected.append((self.state, target))
            return False
        if target == self.state:
            self.since = self.clock()      # 自环：刷新计时
            return True
        self.previous = self.state
        self.state = target
        self.since = self.clock()
        self.history.append(target)
        return True

    def feed_event(self, event) -> str:
        """喂一个编排事件（``AgentEvent`` 或 ``"kind"`` 字符串），返回新状态。"""
        kind = getattr(event, "kind", event)
        target = EVENT_TO_STATE.get(str(kind))
        if target is None:
            return self.state
        if self.state == "sleeping":
            self.transition("idle")        # 先醒过来，再按事件迁移
        self.transition(target)
        return self.state

    def tick(self) -> str:
        """时间推进：空闲太久就去睡觉。"""
        if self.state in ("idle", "done", "happy"):
            if self.elapsed_ms() >= self.idle_timeout_ms and self.can_transition("sleeping"):
                self.transition("sleeping")
        return self.state

    # -- 查询 -------------------------------------------------------------

    def elapsed_ms(self) -> int:
        return int((self.clock() - self.since) * 1000)

    def snapshot(self) -> PetSnapshot:
        return PetSnapshot(
            state=self.state, previous=self.previous, since=self.since,
            elapsed_ms=self.elapsed_ms(),
            frame_index=self.animator.frame_at(self.state, self.elapsed_ms()),
            label=LABELS.get(self.state, ""),
            color=STATE_COLORS.get(self.state, "#ffffff"),
            history=list(self.history),
        )

    def reset(self) -> None:
        self.previous = self.state
        self.state = "idle"
        self.since = self.clock()
        self.history.append("idle")


class PetAnimator:
    """按时间取帧。"""

    def __init__(self, animations: dict[str, list[PetFrame]] | None = None) -> None:
        self.animations = animations or ANIMATIONS

    def frames(self, state: str) -> list[PetFrame]:
        return self.animations.get(state) or self.animations.get("idle") or [PetFrame(0)]

    def loop_duration(self, state: str) -> int:
        return sum(frame.duration_ms for frame in self.frames(state))

    def frame_at(self, state: str, elapsed_ms: int) -> int:
        """取该时刻应当显示的精灵序号（循环播放）。"""
        frames = self.frames(state)
        total = sum(frame.duration_ms for frame in frames) or 1
        position = max(0, int(elapsed_ms)) % total
        for frame in frames:
            if position < frame.duration_ms:
                return frame.index
            position -= frame.duration_ms
        return frames[-1].index


class PetPainter:
    """把宠物画成一组**绘制指令**（纯数据，不依赖 Qt）。

    坐标系以宠物左上角为原点，宽 64 高 64（乘 ``scale``）。
    """

    SIZE = 64

    def plan(self, state: str, frame: int = 0, *, x: int = 0, y: int = 0,
             scale: float = 1.0) -> list[dict]:
        """产出绘制指令列表；同样的输入必然得到同样的输出。"""
        def sx(value: float) -> float:
            return x + value * scale

        def sy(value: float) -> float:
            return y + value * scale

        color = STATE_COLORS.get(state, "#f5a623")
        body = "#f5a623" if state != "sleeping" else "#8b95a3"
        ops: list[dict] = []

        # 脸
        ops.append({"op": "circle", "cx": sx(32), "cy": sy(36), "r": 20 * scale,
                    "fill": body})
        # 耳朵（睡觉时耷拉一点）
        ear_lift = 6 if state != "sleeping" else 2
        ops.append({"op": "polygon",
                    "points": [[sx(14), sy(20)], [sx(22), sy(20 - ear_lift)],
                               [sx(20), sy(6)]], "fill": body})
        ops.append({"op": "polygon",
                    "points": [[sx(50), sy(20)], [sx(42), sy(20 - ear_lift)],
                               [sx(44), sy(6)]], "fill": body})
        # 尾巴（帧号决定摆动相位）
        swing = (-1) ** frame * (6 if state == "happy" else 3)
        ops.append({"op": "line", "x1": sx(50), "y1": sy(46),
                    "x2": sx(58 + swing), "y2": sy(30), "color": body,
                    "width": max(2, int(4 * scale))})
        # 眼睛：不同状态不同表情
        if state == "sleeping":
            ops.append({"op": "line", "x1": sx(24), "y1": sy(34),
                        "x2": sx(30), "y2": sy(34), "color": "#1f2937", "width": 2})
            ops.append({"op": "line", "x1": sx(34), "y1": sy(34),
                        "x2": sx(40), "y2": sy(34), "color": "#1f2937", "width": 2})
            ops.append({"op": "text", "x": sx(46), "y": sy(14), "text": "z",
                        "size": max(8, int(10 * scale)), "color": "#cbd5e1"})
        elif state == "error":
            ops.append({"op": "line", "x1": sx(23), "y1": sy(30),
                        "x2": sx(30), "y2": sy(37), "color": "#ff453a", "width": 2})
            ops.append({"op": "line", "x1": sx(30), "y1": sy(30),
                        "x2": sx(23), "y2": sy(37), "color": "#ff453a", "width": 2})
            ops.append({"op": "line", "x1": sx(34), "y1": sy(30),
                        "x2": sx(41), "y2": sy(37), "color": "#ff453a", "width": 2})
            ops.append({"op": "line", "x1": sx(41), "y1": sy(30),
                        "x2": sx(34), "y2": sy(37), "color": "#ff453a", "width": 2})
        elif state in ("happy", "done"):
            # 弯月眼
            for offset in (0, 11):
                ops.append({"op": "line", "x1": sx(23 + offset), "y1": sy(36),
                            "x2": sx(26 + offset), "y2": sy(32),
                            "color": "#1f2937", "width": 2})
                ops.append({"op": "line", "x1": sx(26 + offset), "y1": sy(32),
                            "x2": sx(29 + offset), "y2": sy(36),
                            "color": "#1f2937", "width": 2})
        elif state == "working":
            # 专注：眼睛眯成一条线，帧号控制眨动
            height = 1 if frame % 2 else 3
            for offset in (0, 11):
                ops.append({"op": "rect", "x": sx(23 + offset), "y": sy(33),
                            "w": 6 * scale, "h": max(1, int(height * scale)),
                            "fill": "#1f2937", "radius": 1})
        else:
            r = 3.5 if frame % 2 == 0 else 2.5
            for offset in (0, 11):
                ops.append({"op": "circle", "cx": sx(26 + offset), "cy": sy(34),
                            "r": r * scale, "fill": "#1f2937"})
        # 鼻子 + 胡须
        ops.append({"op": "polygon",
                    "points": [[sx(30), sy(42)], [sx(34), sy(42)], [sx(32), sy(45)]],
                    "fill": "#f472b6"})
        for offset, direction in ((0, -1), (1, 1)):
            ops.append({"op": "line", "x1": sx(32), "y1": sy(44),
                        "x2": sx(32 + direction * 14), "y2": sy(42 + offset * 3),
                        "color": "#d1d5db", "width": 1})
        # 状态色点缀（项圈），让状态一眼可辨
        ops.append({"op": "rect", "x": sx(22), "y": sy(54), "w": 20 * scale,
                    "h": max(2, int(3 * scale)), "fill": color, "radius": 2})
        return ops

    #: 允许的绘制指令名（白名单，防止拼错悄悄什么都不画）
    OPS: frozenset[str] = frozenset({"clear", "rect", "line", "circle", "polygon",
                                     "text"})

    #: 几何参数**按位置传**：PawUI 的 ``<Canvas>`` 画布 API 就是位置参数
    #: （``rect(x, y, w, h, radius=, fill=)``）。用关键字传几何会直接 TypeError。
    GEOMETRY: dict[str, tuple[str, ...]] = {
        "clear": ("color",),
        "rect": ("x", "y", "w", "h"),
        "line": ("x1", "y1", "x2", "y2"),
        "circle": ("cx", "cy", "r"),
        "polygon": ("points",),
        "text": ("x", "y", "text"),
    }

    def render(self, canvas, ops: list[dict] | None = None, *, state: str = "idle",
               frame: int = 0, **kwargs) -> int:
        """把指令喂给画布对象（PawUI ``<Canvas>`` 的 ``on_draw`` 参数）。

        返回实际执行的指令条数。未知指令会抛错 —— 不静默跳过。
        """
        ops = self.plan(state, frame, **kwargs) if ops is None else ops
        for item in ops:
            name = item.get("op")
            if name not in self.OPS:
                raise ValueError(f"未知绘制指令: {name!r}")
            geometry = self.GEOMETRY.get(name, ())
            args = [item[key] for key in geometry]
            style = {key: value for key, value in item.items()
                     if key != "op" and key not in geometry}
            getattr(canvas, name)(*args, **style)
        return len(ops)

    def sprite_sheet(self, *, scale: float = 1.0) -> dict[str, list[dict]]:
        """所有状态的「第 0 帧」指令表，便于一次性生成预览。"""
        return {state: self.plan(state, 0, scale=scale) for state in STATES}
