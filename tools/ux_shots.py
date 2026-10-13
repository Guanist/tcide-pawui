# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""给新界面（P0 体验改进）抓一组实拍图，便于对比"改之前/改之后"。

用法：``python tools/ux_shots.py``（真实平台，中文正常）
产出：``shots/ux-1-empty.png`` / ``ux-2-sample.png`` / ``ux-3-tools.png``

注意：会临时创建示例文件 `hello_tcide*.py`，**截完就删掉**（保持项目干净）。
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(line_buffering=True)
os.environ.pop("QT_QPA_PLATFORM", None)          # 用真实平台插件，才有系统字体

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
    app.push()

    OUT.mkdir(parents=True, exist_ok=True)
    steps: list[str] = []
    created: list[str] = []

    def snap(name: str) -> None:
        target = OUT / name
        pixmap = runtime.root.grab()
        ok = pixmap.save(str(target))
        line = f"{'已保存' if ok else '★失败'} {target.name} ({pixmap.width()}x{pixmap.height()})"
        print(line)
        steps.append(line)

    def stage1() -> None:
        # ① 空状态：引导卡应该自动展开
        snap("ux-1-empty.png")
        QTimer.singleShot(400, stage2)

    def stage2() -> None:
        # ② 点「新建示例文件并打开」：文件出现、引导自动收起、标签栏有内容
        before = set(app.state.workspace.iter_files())
        app.cmd_open_sample()
        created.extend(sorted(set(app.state.workspace.iter_files()) - before))
        qapp.processEvents()
        snap("ux-2-sample.png")
        QTimer.singleShot(400, stage3)

    def stage3() -> None:
        # ③ 工具菜单：原先藏在命令面板里的能力
        app.cmd_tools_open()
        qapp.processEvents()
        snap("ux-3-tools.png")
        QTimer.singleShot(300, finish)

    def finish() -> None:
        for name in created:                    # 还原项目
            try:
                app.state.workspace.delete(name)
                print(f"已清理临时示例文件 {name}")
            except Exception as exc:            # noqa: BLE001
                print(f"清理失败 {name}: {exc}")
        qapp.quit()

    QTimer.singleShot(2000, stage1)
    code = int(qapp.exec())
    print("---")
    print(f"共 {len(steps)} 张")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
