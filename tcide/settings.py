# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""应用设置：读写、校验、默认值。

为什么放在 ``tcide/``（库侧）而不是 ``main.py``：设置是**纯数据**逻辑，不碰 Qt、
不碰 PawUI，放在这里可以离线严格测试（界面上点几下就写坏配置文件是很难查的问题）。

存放位置（按顺序取第一个可用的）：

1. 环境变量 ``TCIDE_CONFIG_DIR`` 指定的目录 —— 测试与自检用它隔离
2. ``%APPDATA%/tcide``（Windows）或 ``~/.config/tcide``（其它平台）

刻意**不**写进项目目录：设置属于"用户 + 这台机器"，不该跟着仓库走。
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass, field, replace

#: 支持的服务商（与 tcide/models.py 的预设保持一致，另加"自定义"）
PROVIDERS = ("deepseek", "ark", "ollama", "anthropic", "openai", "custom")

#: 字号可选范围（界面用 Segmented 给几档，这里做校验兜底）
FONT_RANGE = (11, 20)
#: 请求超时可选范围（秒）
TIMEOUT_RANGE = (10, 600)


@dataclass
class Settings:
    """一份可序列化的应用设置。字段名直接对应设置面板里的控件。"""

    # -- 模型 --
    provider: str = "deepseek"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout: int = 60

    # -- 界面 --
    font_size: int = 13
    compact: bool = False
    ui_scale: str = "100%"          # 预留：高 DPI 缩放
    editor_font: str = "Cascadia Mono"
    monaco_enabled: bool = True     # 关掉则退回内置编辑器（省内存）

    # -- 行为 --
    review_gate: bool = True        # AI 写操作是否需要人工审核
    restore_last_project: bool = True
    run_tests_on_save: bool = False
    telemetry: bool = False         # 默认关：本项目不收集任何数据

    # -- 其它 --
    extra: dict = field(default_factory=dict)


def config_dir() -> pathlib.Path:
    """设置目录（``TCIDE_CONFIG_DIR`` 优先，便于测试隔离）。"""
    override = os.environ.get("TCIDE_CONFIG_DIR", "").strip()
    if override:
        return pathlib.Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return pathlib.Path(base) / "tcide"
    return pathlib.Path(os.path.expanduser("~")) / ".config" / "tcide"


def config_path() -> pathlib.Path:
    return config_dir() / "settings.json"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def normalize(settings: Settings) -> Settings:
    """把越界/非法的值收拢到合法范围。

    界面上的输入框可以填任何东西，配置文件也可能被手改坏 —— 这里统一兜底，
    免得非法值一路传到模型请求里（比如 timeout=0 会立刻超时）。
    """
    provider = (settings.provider or "deepseek").strip().lower()
    if provider not in PROVIDERS:
        provider = "custom" if settings.base_url else "deepseek"
    return replace(
        settings,
        provider=provider,
        model=(settings.model or "").strip(),
        api_key=(settings.api_key or "").strip(),
        base_url=(settings.base_url or "").strip().rstrip("/"),
        timeout=_clamp(settings.timeout, *TIMEOUT_RANGE),
        font_size=_clamp(settings.font_size, *FONT_RANGE),
        editor_font=(settings.editor_font or "Cascadia Mono").strip(),
    )


def load(path: pathlib.Path | None = None) -> Settings:
    """读取设置；文件不存在/坏掉都返回默认值（**不抛异常**）。

    为什么要吞掉错误：一个坏掉的配置文件不该让 IDE 起不来 —— 用户会遇到
    "打开就崩"却不知道原因。坏文件会被保留（改名为 .bad）以便排查。
    """
    target = path or config_path()
    if not target.is_file():
        return Settings()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001
        try:
            target.replace(target.with_suffix(".json.bad"))
        except Exception:      # noqa: BLE001
            pass
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    known = {item.name for item in Settings.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    payload = {key: value for key, value in data.items() if key in known}
    try:
        return normalize(Settings(**payload))
    except Exception:      # noqa: BLE001 - 类型不对也别崩
        return Settings()


def save(settings: Settings, path: pathlib.Path | None = None) -> pathlib.Path:
    """写回设置（原子写：先写临时文件再替换，避免写一半断电变坏文件）。"""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(normalize(settings))
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                         encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def masked(key: str) -> str:
    """给界面显示的脱敏 Key（日志/截图里不该出现完整 Key）。"""
    if not key:
        return "未配置"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def summary(settings: Settings) -> str:
    """一行摘要，给状态下拉/状态栏用。"""
    model = settings.model or "(默认)"
    return f"{settings.provider}/{model} · Key {masked(settings.api_key)}"
