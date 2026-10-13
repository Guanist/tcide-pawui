# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""在**真实平台**（非 offscreen）下给应用截一张图 —— 用于把界面呈现出来。

用法：``python tools/live_shot.py [项目目录] [输出文件]``

和 ``main.py --shot`` 的区别：那个用 ``QT_QPA_PLATFORM=offscreen``（离屏、没有
系统字体，中文会变方块）；这个用本机默认平台插件，**中文与字体都正常**。
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(line_buffering=True)
# 刻意**不**设置 QT_QPA_PLATFORM —— 用真实平台插件，才会用系统字体
os.environ.pop("QT_QPA_PLATFORM", None)

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "shots" / "live.png"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import main as tcide_main

    runtime, app = tcide_main.create_runtime(target)
    qapp = QApplication.instance()
    # **必须先建树**，否则 runtime.root 是 None（第一版漏了这步，截图直接 AttributeError）
    runtime.run(block=False)
    app.bootstrap()

    # 让界面有内容可看：打开一个文件、展开一层目录、刷新各面板
    for candidate in ("tcide/diff.py", "tcide/web/bridge.py", "main.py"):
        if app.state.workspace.exists(candidate):
            app.open_file(candidate)
            break
    app.state.expanded.add("tcide")
    app.refresh_tree()
    app.refresh_git()
    app.build_palette()
    app._append_chat("·", "本机真实平台截图（中文正常显示）")
    app.push()

    def grab() -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        root = runtime.root
        pixmap = root.grab()
        saved = pixmap.save(str(out))
        print(f"{'已保存' if saved else '★ 保存失败'}：{out}"
              f"（{pixmap.width()}x{pixmap.height()}）")
        print(f"窗口标题：{root.windowTitle()}")
        print(f"当前文件：{app.state.active_file}")
        print(f"编辑器字符数：{len(app.state.editor_text)}")
        print(f"标签数：{app.open_files.count()}｜状态栏：{app.state.status_left}")
        qapp.quit()

    QTimer.singleShot(2500, grab)
    return int(qapp.exec())


if __name__ == "__main__":
    raise SystemExit(main())
