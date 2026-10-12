# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""诊断：为什么「快速上手」Panel 的内容不显示。

把每个 Panel 的 header（QToolButton）文本、勾选状态、以及 body 的可见性打出来 ——
`Panel.open` 只在**建树时**读一次（与 `<Image src>` 同类），所以要确认建树时它拿到
的值到底是什么。
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from PySide6.QtWidgets import QApplication, QToolButton, QWidget

    import main as tcide_main

    runtime, app = tcide_main.create_runtime(ROOT)
    qapp = QApplication.instance()
    runtime.run(block=False)
    app.bootstrap()
    qapp.processEvents()

    print("=== 建树后：各 Panel 的 header 状态（isChecked = open）===")
    for button in runtime.root.findChildren(QToolButton):
        parent = button.parentWidget()
        siblings = [child for child in (parent.children() if parent else [])
                    if isinstance(child, QWidget) and child is not button]
        visible = [child for child in siblings if child.isVisible()]
        print(f"  {button.text()!r:30} checked={button.isChecked()} "
              f"| 兄弟容器 {len(siblings)} 个，可见 {len(visible)} 个")

    print()
    print("=== 应用侧状态 ===")
    print(f"  active_path            = {app.state.active_path!r}")
    print(f"  onboarding_open(app)   = {app.onboarding_open!r}")
    print(f"  _onboarding_visible()  = {app._onboarding_visible()!r}")
    try:
        print(f"  runtime.state 里的值   = "
              f"{runtime.state.get('onboarding_open')!r}")
    except Exception as exc:      # noqa: BLE001
        print(f"  runtime.state 读取失败: {exc}")

    print()
    print("=== 快速上手面板的 body 里到底有什么 ===")
    from PySide6.QtWidgets import QLabel
    for button in runtime.root.findChildren(QToolButton):
        if "快速上手" not in button.text():
            continue
        parent = button.parentWidget()
        siblings = [child for child in (parent.children() if parent else [])
                    if isinstance(child, QWidget) and child is not button]
        for body in siblings:
            print(f"  body: 可见={body.isVisible()} 高={body.height()} "
                  f"子控件={len(body.findChildren(QWidget))}")
            for label in body.findChildren(QLabel):
                print(f"    QLabel {label.text()!r}")
            for child in body.findChildren(QToolButton):
                print(f"    QToolButton {child.text()!r}")
            from PySide6.QtWidgets import QPushButton
            for child in body.findChildren(QPushButton):
                print(f"    QPushButton {child.text()!r} 可见={child.isVisible()}")

    print()
    print("=== push() 之后再看一次 ===")
    app.push()
    qapp.processEvents()
    for button in runtime.root.findChildren(QToolButton):
        if "快速上手" in button.text():
            print(f"  {button.text()!r} checked={button.isChecked()}")

    print()
    print("=== <script> 块是否被完整执行（抽查几个后面定义的键）===")
    for key in ("onboarding_open", "onboarding_title", "tools_hint",
                "palette_open", "active_file"):
        try:
            print(f"  {key:20} = {runtime.state.get(key)!r}")
        except Exception as exc:      # noqa: BLE001
            print(f"  {key:20} ! {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
