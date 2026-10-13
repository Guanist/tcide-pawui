# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""最小复现：``bind_state`` 把控件内容当模板二次解析（内容被吃掉）。

对**任何**已安装的 PawUI 都能跑（原版 0.1.3.3 上失败、修好后通过）：

    python issue_repro_bind_state.py

原理：state 里存一段**文本内容**，其中含 PawUI 自己的模板语法 ``{$...}``。
预期控件显示的内容与 state **逐字符一致**；实际会被再解析一遍，``{$x}`` 被
当成模板插值掉。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(line_buffering=True)

LITERAL = "a = {$not_a_template}\nb = {$also_literal}\n"

SOURCE = """
<Window title="repro" width="420" height="220">
  <Column>
    <TextArea id="editor" value="{$text}" height="180"/>
  </Column>
</Window>
<script>
text = ""
</script>
"""


def main() -> int:
    from PySide6.QtWidgets import QApplication, QPlainTextEdit

    from pawui.runtime import Runtime

    app = QApplication.instance() or QApplication([])
    runtime = Runtime(SOURCE, "repro.paw", context={})
    runtime.run(block=False)
    app.processEvents()

    runtime.state.set("text", LITERAL)
    app.processEvents()

    widget = runtime.root.findChild(QPlainTextEdit, "editor")
    if widget is None:
        print("找不到 TextArea 控件（组件名可能变了）")
        return 2
    shown = widget.toPlainText()

    print(f"state 里是 {len(LITERAL)} 字符：{LITERAL!r}")
    print(f"控件里是   {len(shown)} 字符：{shown!r}")
    if shown == LITERAL:
        print("结果：一致 —— 已修复")
        return 0
    print("结果：**不一致** —— 内容里的 {$...} 被当模板解析掉了")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
