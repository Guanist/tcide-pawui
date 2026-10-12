# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""TCIDE-PawUI：用 PawUI 实现的虎猫 TCIDE 等价应用。

后端核心（本包）刻意**不依赖 Qt**，可以完全离线、无界面地测试；
PawUI 只负责界面层（``app.paw`` + ``main.py``）。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "agent",
    "gitops",
    "indexer",
    "memory",
    "models",
    "pet",
    "pressure",
    "tokens",
    "tools",
    "trimmer",
    "workspace",
]
