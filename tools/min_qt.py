# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""最小探针：冻结后的 PySide6 到底能不能跑。

用来把两类问题分开：
- 如果**这个**打包产物也段错误 → 是"本机 PySide6 安装 / 冻结环境"的问题，
  与我的应用无关；
- 如果它能跑 → 问题在我的应用或它的打包配置里。

运行：``python -m PyInstaller --console --noconfirm min_qt.py`` 然后执行 dist 里的 exe。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(line_buffering=True)

print("1) 解释器启动 OK")
print("   frozen =", getattr(sys, "frozen", False), "| _MEIPASS =",
      getattr(sys, "_MEIPASS", None))

try:
    import PySide6
    print("2) PySide6 导入 OK，版本 =", PySide6.__version__)
except Exception as exc:                      # noqa: BLE001
    print("2) PySide6 导入失败:", exc)
    raise SystemExit(2)

from PySide6.QtWidgets import QApplication, QLabel   # noqa: E402

print("3) QtWidgets 导入 OK")

app = QApplication([])
print("4) QApplication 创建 OK，平台 =", app.platformName())

label = QLabel("hello")
label.resize(120, 40)
label.show()
app.processEvents()
print("5) 控件创建 OK，尺寸 =", label.width(), label.height())

pixmap = label.grab()
out = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "min_qt_shot.png")
saved = pixmap.save(out)
print(f"6) grab+save = {saved} -> {out}")
print("最小探针全部通过")
