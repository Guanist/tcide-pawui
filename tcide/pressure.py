# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""上下文压力评估。

对应 TCIDE 原版的 ``context/entropy-controller.ts``。原版命名为 "entropy"，
但实现是**加权启发式**（tokenDensity / roleDiversity / messageVolume /
turnPenalty / projectFactor 各自归一化后加权求和），并非信息熵。
这里保留算法思路，但用诚实的名字与文档，并把权重暴露出来便于调参与测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 各因子默认权重（和为 1.0）
DEFAULT_WEIGHTS: dict[str, float] = {
    "token_density": 0.45,
    "message_volume": 0.20,
    "role_diversity": 0.10,
    "turn_penalty": 0.15,
    "project_factor": 0.10,
}

#: 压力等级阈值
LEVELS: tuple[tuple[float, str], ...] = (
    (0.85, "critical"),
    (0.65, "high"),
    (0.35, "moderate"),
    (0.0, "low"),
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class PressureReport:
    """一次压力评估的结果（因子值 + 加权总分 + 建议动作）。"""

    score: float
    level: str
    factors: dict[str, float] = field(default_factory=dict)
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "level": self.level,
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "recommendation": self.recommendation,
        }


class ContextPressure:
    """按加权因子评估上下文压力，给出剪裁建议。

    ``weights`` 可注入，便于测试与按模型调参（例如 128K 模型可以整体调低）。
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        *,
        budget: int = 32768,
        message_scale: int = 50,
        stuck_turn_scale: int = 6,
    ) -> None:
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("weights 之和必须大于 0")
        # 归一化，调用方传任意比例的权重都能用
        self.weights = {k: v / total for k, v in self.weights.items()}
        self.budget = max(1, int(budget))
        self.message_scale = max(1, int(message_scale))
        self.stuck_turn_scale = max(1, int(stuck_turn_scale))

    # -- 因子 -------------------------------------------------------------

    def token_density(self, used_tokens: int) -> float:
        return _clamp(used_tokens / self.budget)

    def message_volume(self, message_count: int) -> float:
        return _clamp(message_count / self.message_scale)

    def role_diversity(self, roles) -> float:
        known = {"system", "user", "assistant", "tool"}
        return _clamp(len({str(r) for r in roles} & known) / len(known))

    def turn_penalty(self, trailing_assistant_turns: int) -> float:
        """连续若干轮助手消息却没有工具调用/用户输入 —— 视为原地打转。"""
        return _clamp(trailing_assistant_turns / self.stuck_turn_scale)

    def project_factor(self, static_tokens: int) -> float:
        return _clamp(static_tokens / self.budget)

    # -- 评估 -------------------------------------------------------------

    def evaluate(
        self,
        *,
        used_tokens: int,
        messages=(),
        static_tokens: int = 0,
        trailing_assistant_turns: int = 0,
    ) -> PressureReport:
        roles = [getattr(m, "role", "") for m in messages]
        factors = {
            "token_density": self.token_density(used_tokens),
            "message_volume": self.message_volume(len(messages)),
            "role_diversity": self.role_diversity(roles),
            "turn_penalty": self.turn_penalty(trailing_assistant_turns),
            "project_factor": self.project_factor(static_tokens),
        }
        score = _clamp(sum(self.weights.get(k, 0.0) * v for k, v in factors.items()))
        level = next(name for threshold, name in LEVELS if score >= threshold)
        return PressureReport(
            score=score,
            level=level,
            factors=factors,
            recommendation=self.recommend(level, factors),
        )

    @staticmethod
    def recommend(level: str, factors: dict[str, float]) -> str:
        if level == "critical":
            return "立即压缩：丢弃历史对话 + 代码块只留骨架 + 依赖文件限 1 个"
        if level == "high":
            return "收紧：保留最近 2 轮对话，依赖文件限 2 个，压缩旧代码块"
        if level == "moderate":
            return "维持：正常裁剪，关注 turn_penalty 是否升高"
        dominant = max(factors, key=factors.get) if factors else ""
        return f"充足（当前最大因子 {dominant}）"
