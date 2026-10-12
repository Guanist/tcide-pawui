# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""环境依赖自检（避免内联引号问题）。"""

from __future__ import annotations

import importlib
import shutil
import sys

print(f"Python {sys.version.split()[0]}")
print()
print("可执行文件:")
for name in ("git", "node", "npm", "npx", "pyright", "python"):
    print(f"  {name:9} {shutil.which(name) or '★ 不可用'}")
print()
print("Python 包:")
for name in ("httpx", "requests", "fastapi", "uvicorn", "pytest", "PySide6",
             "yaml", "tomllib", "watchdog"):
    try:
        module = importlib.import_module(name)
        version = getattr(module, "__version__", "")
        print(f"  ok  {name:10} {version}")
    except Exception as error:  # noqa: BLE001
        print(f"  --  {name:10} {type(error).__name__}")
print()
print("PySide6 子模块:")
for name in ("PySide6.QtWidgets", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
             "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtSvgWidgets"):
    try:
        importlib.import_module(name)
        print(f"  ok  {name}")
    except Exception as error:  # noqa: BLE001
        print(f"  --  {name:32} {type(error).__name__}")
