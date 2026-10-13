# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""抓界面实拍图（真实平台，中文正常）：主界面 + 设置面板。

用法：``python tools/ui_shots.py``（产出 shots/ui-1-main.png、shots/ui-2-settings.png）
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(line_buffering=True)
os.environ.pop("QT_QPA_PLATFORM", None)          # 真实平台插件，才有系统字体

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "shots"
sys.path.insert(0, str(ROOT))


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import main as tcide_main

    runtime, app = tcide_main.create_runtime(ROOT)
    qapp = QApplication.instance()
    runtime.run(block=False)
    app.bootstrap()
    app.state.expanded.add("tcide")
    app.refresh_tree()
    app.refresh_git()
    app.open_file("tcide/settings.py")
    app.push()

    OUT.mkdir(parents=True, exist_ok=True)

    def snap(name: str) -> None:
        pixmap = runtime.root.grab()
        ok = pixmap.save(str(OUT / name))
        print(f"{'已保存' if ok else '★失败'} {name} ({pixmap.width()}x{pixmap.height()})")

    def stage1() -> None:
        snap("ui-1-main.png")
        app.cmd_settings_open()
        qapp.processEvents()
        QTimer.singleShot(600, stage2)

    def stage2() -> None:
        snap("ui-2-settings.png")
        qapp.quit()

    QTimer.singleShot(2200, stage1)
    return int(qapp.exec())


if __name__ == "__main__":
    raise SystemExit(main())
