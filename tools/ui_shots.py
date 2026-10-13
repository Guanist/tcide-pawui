# SPDX-FileCopyrightText: 2026 Guanist, Inc.
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

    # 主题在**建树前**决定（PawUI 的组件样式是建树时算好并内联的，建完再切没用）
    light = "--light" in sys.argv
    if light:
        os.environ["TCIDE_CONFIG_DIR"] = str(
            pathlib.Path(os.environ.get("TEMP", ".")) / "tcide-shots-light")
        pathlib.Path(os.environ["TCIDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
        from tcide.settings import Settings, save as save_settings

        save_settings(Settings(theme="light"))

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
    suffix = "light" if light else "main"

    def snap(name: str) -> None:
        pixmap = runtime.root.grab()
        ok = pixmap.save(str(OUT / name))
        print(f"{'已保存' if ok else '★失败'} {name} ({pixmap.width()}x{pixmap.height()})")

    def stage1() -> None:
        snap(f"ui-1-{suffix}.png")
        app.cmd_settings_open()
        qapp.processEvents()
        QTimer.singleShot(600, stage2)

    def stage2() -> None:
        snap(f"ui-3-settings-{suffix}.png")
        qapp.quit()

    QTimer.singleShot(2200, stage1)
    return int(qapp.exec())


if __name__ == "__main__":
    raise SystemExit(main())
