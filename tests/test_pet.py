# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""pet.py 的测试（状态机 / 动画 / 绘制指令，全部离线）。"""

from __future__ import annotations

import pytest

from tcide.pet import (
    ALLOWED,
    ANIMATIONS,
    EVENT_TO_STATE,
    LABELS,
    STATES,
    STATE_COLORS,
    PetAnimator,
    PetPainter,
    PetStateMachine,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCanvas:
    """只记录调用，不真的画。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def clear(self, *args, **kwargs): self._record("clear", *args, **kwargs)
    def rect(self, *args, **kwargs): self._record("rect", *args, **kwargs)
    def line(self, *args, **kwargs): self._record("line", *args, **kwargs)
    def circle(self, *args, **kwargs): self._record("circle", *args, **kwargs)
    def polygon(self, *args, **kwargs): self._record("polygon", *args, **kwargs)
    def text(self, *args, **kwargs): self._record("text", *args, **kwargs)


class TestStatesTable:
    def test_nine_states(self):
        """README 宣称 9 状态精灵动画。"""
        assert len(STATES) == 9

    def test_every_state_has_animation(self):
        for state in STATES:
            assert ANIMATIONS.get(state), f"{state} 没有帧序列"
            assert all(f.duration_ms > 0 for f in ANIMATIONS[state])

    def test_every_state_has_label_and_color(self):
        for state in STATES:
            assert LABELS.get(state), f"{state} 没有标签"
            assert STATE_COLORS.get(state, "").startswith("#")

    def test_allowed_table_covers_all_states(self):
        assert set(ALLOWED) == set(STATES)

    def test_allowed_targets_are_known_states(self):
        for source, targets in ALLOWED.items():
            assert targets <= set(STATES), f"{source} 指向了未知状态"

    def test_every_state_can_reach_idle(self):
        assert all("idle" in ALLOWED[state] for state in STATES)

    def test_event_map_targets_are_known_states(self):
        assert set(EVENT_TO_STATE.values()) <= set(STATES)


class TestStateMachine:
    def test_starts_idle(self):
        machine = PetStateMachine(clock=FakeClock())
        assert machine.state == "idle"
        assert machine.history == ["idle"]

    def test_legal_transition(self):
        machine = PetStateMachine(clock=FakeClock())
        assert machine.transition("thinking") is True
        assert machine.state == "thinking"
        assert machine.previous == "idle"

    def test_unknown_state_rejected(self):
        machine = PetStateMachine(clock=FakeClock())
        assert machine.transition("nonsense") is False
        assert machine.state == "idle"
        assert machine.rejected == [("idle", "nonsense")]

    def test_illegal_transition_rejected(self):
        """sleeping → happy 不在允许表里，必须拒绝且状态不变。"""
        machine = PetStateMachine(clock=FakeClock())
        machine.transition("sleeping")
        assert machine.transition("happy") is False
        assert machine.state == "sleeping"

    def test_self_transition_refreshes_timer(self):
        clock = FakeClock()
        machine = PetStateMachine(clock=clock)
        clock.advance(5)
        machine.transition("idle")
        assert machine.elapsed_ms() == 0

    def test_feed_event_maps_kinds(self):
        machine = PetStateMachine(clock=FakeClock())
        assert machine.feed_event("plan") == "thinking"
        assert machine.feed_event("task_start") == "working"
        assert machine.feed_event("review") == "reviewing"
        assert machine.feed_event("task_done") == "happy"
        assert machine.feed_event("done") == "done"

    def test_feed_event_accepts_event_object(self):
        class Event:
            kind = "task_start"

        machine = PetStateMachine(clock=FakeClock())
        assert machine.feed_event(Event()) == "working"

    def test_unknown_event_keeps_state(self):
        machine = PetStateMachine(clock=FakeClock())
        assert machine.feed_event("完全没有的事件") == "idle"

    def test_sleeping_wakes_then_transitions(self):
        machine = PetStateMachine(clock=FakeClock())
        machine.transition("sleeping")
        assert machine.feed_event("task_start") == "working"

    def test_tick_sleeps_after_idle_timeout(self):
        clock = FakeClock()
        machine = PetStateMachine(idle_timeout_ms=2000, clock=clock)
        clock.advance(3)
        assert machine.tick() == "sleeping"

    def test_tick_does_not_sleep_while_working(self):
        clock = FakeClock()
        machine = PetStateMachine(idle_timeout_ms=100, clock=clock)
        machine.transition("working")
        clock.advance(10)
        assert machine.tick() == "working"

    def test_error_never_auto_sleeps(self):
        clock = FakeClock()
        machine = PetStateMachine(idle_timeout_ms=100, clock=clock)
        machine.transition("error")
        clock.advance(10)
        assert machine.tick() == "error"

    def test_snapshot_fields(self):
        clock = FakeClock()
        machine = PetStateMachine(clock=clock)
        machine.transition("working")
        clock.advance(0.25)
        snapshot = machine.snapshot()
        assert snapshot.state == "working"
        assert snapshot.elapsed_ms == 250
        assert snapshot.label == LABELS["working"]
        assert snapshot.color == STATE_COLORS["working"]
        assert "working" in snapshot.history

    def test_snapshot_serializable(self):
        data = PetStateMachine(clock=FakeClock()).snapshot().to_dict()
        assert set(data) >= {"state", "previous", "elapsed_ms", "frame_index",
                             "label", "color", "history"}

    def test_reset(self):
        machine = PetStateMachine(clock=FakeClock())
        machine.transition("working")
        machine.reset()
        assert machine.state == "idle"
        assert machine.previous == "working"

    def test_history_is_bounded_in_snapshot(self):
        machine = PetStateMachine(clock=FakeClock())
        for _ in range(20):
            machine.transition("working")
            machine.transition("idle")
        assert len(machine.snapshot().to_dict()["history"]) <= 8


class TestAnimator:
    def test_frame_at_start(self):
        assert PetAnimator().frame_at("idle", 0) == ANIMATIONS["idle"][0].index

    def test_frame_advances(self):
        animator = PetAnimator()
        assert animator.frame_at("thinking", 600) == ANIMATIONS["thinking"][2].index

    def test_loops_around(self):
        animator = PetAnimator()
        total = animator.loop_duration("idle")
        assert animator.frame_at("idle", total) == animator.frame_at("idle", 0)

    def test_negative_elapsed(self):
        assert PetAnimator().frame_at("idle", -50) == 0

    def test_unknown_state_falls_back(self):
        animator = PetAnimator()
        assert animator.frames("不存在") == animator.frames("idle")

    def test_loop_duration_positive(self):
        animator = PetAnimator()
        for state in STATES:
            assert animator.loop_duration(state) > 0


class TestPainter:
    def test_every_state_produces_ops(self):
        painter = PetPainter()
        for state in STATES:
            ops = painter.plan(state, 0)
            assert ops, f"{state} 没有绘制指令"
            assert all(item["op"] in painter.OPS for item in ops), state

    def test_deterministic(self):
        painter = PetPainter()
        assert painter.plan("working", 1) == painter.plan("working", 1)

    def test_states_look_different(self):
        painter = PetPainter()
        rendered = {state: str(painter.plan(state, 0)) for state in STATES}
        assert len(set(rendered.values())) >= 7, "状态之间画出来几乎一样"

    def test_scale_and_offset_applied(self):
        painter = PetPainter()
        base = painter.plan("idle", 0)
        moved = painter.plan("idle", 0, x=100, y=50, scale=2.0)
        first_base = next(item for item in base if item["op"] == "circle")
        first_moved = next(item for item in moved if item["op"] == "circle")
        assert first_moved["cx"] == 100 + first_base["cx"] * 2
        assert first_moved["r"] == first_base["r"] * 2

    def test_sleeping_has_z(self):
        ops = PetPainter().plan("sleeping", 0)
        assert any(item["op"] == "text" and item["text"] == "z" for item in ops)

    def test_error_has_cross_eyes(self):
        ops = PetPainter().plan("error", 0)
        reds = [item for item in ops if item.get("color") == "#ff453a"]
        assert len(reds) >= 4, "错误状态应当画成叉眼"

    def test_render_calls_canvas(self):
        canvas = FakeCanvas()
        count = PetPainter().render(canvas, state="happy", frame=0)
        assert count == len(canvas.calls)
        assert canvas.calls, "没有调用画布"
        assert {name for name, _args, _kwargs in canvas.calls} <= PetPainter.OPS

    def test_geometry_is_passed_positionally(self):
        """PawUI 的 Canvas API 是位置参数几何；用关键字会直接 TypeError。"""
        canvas = FakeCanvas()
        PetPainter().render(canvas, [
            {"op": "rect", "x": 1, "y": 2, "w": 3, "h": 4,
             "fill": "#fff", "radius": 2},
            {"op": "text", "x": 5, "y": 6, "text": "z", "size": 9},
        ])
        name, args, kwargs = canvas.calls[0]
        assert (name, args) == ("rect", (1, 2, 3, 4))
        assert kwargs == {"fill": "#fff", "radius": 2}
        name, args, kwargs = canvas.calls[1]
        assert (name, args) == ("text", (5, 6, "z"))
        assert kwargs == {"size": 9}

    def test_every_op_has_geometry_mapping(self):
        assert set(PetPainter.GEOMETRY) == set(PetPainter.OPS)

    def test_render_rejects_unknown_op(self):
        canvas = FakeCanvas()
        with pytest.raises(ValueError, match="未知绘制指令"):
            PetPainter().render(canvas, [{"op": "sparkle", "x": 1}])

    def test_render_accepts_precomputed_ops(self):
        canvas = FakeCanvas()
        ops = PetPainter().plan("idle", 0)
        assert PetPainter().render(canvas, ops) == len(ops)

    def test_sprite_sheet_covers_all_states(self):
        sheet = PetPainter().sprite_sheet()
        assert set(sheet) == set(STATES)
        assert all(sheet[state] for state in STATES)

    def test_animation_frame_index_is_used(self):
        """不同帧号应当画出不同姿态（否则动画是假的）。"""
        painter = PetPainter()
        assert painter.plan("happy", 0) != painter.plan("happy", 1)
