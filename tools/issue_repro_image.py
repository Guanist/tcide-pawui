# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""最小复现：``<Image src="{$p}">`` 不响应状态变化。

对**任何**已安装的 PawUI 都能跑（0.1.3.3 上失败）：

    python issue_repro_image.py

原理：``Image.build()`` 只在建树时读一次 ``src``，没有 ``bind_state`` ——
于是状态更新后控件里仍然是空的。同类组件（Text/Markdown/List/Table/Select/
TextArea）都支持模板刷新，Image 不支持，行为不一致。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(line_buffering=True)

#: 1x1 真 PNG（不依赖任何外部素材）
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db40000"
    "000049454e44ae426082")

SOURCE = """
<Window title="repro" width="300" height="200">
  <Column>
    <Image id="pic" src="{$pic}"/>
  </Column>
</Window>
<script>
pic = ""
</script>
"""


def main() -> int:
    from PySide6.QtWidgets import QApplication, QLabel

    from pawui.runtime import Runtime

    with tempfile.TemporaryDirectory() as tmp:
        image = pathlib.Path(tmp) / "one.png"
        image.write_bytes(PNG_1X1)

        app = QApplication.instance() or QApplication([])
        runtime = Runtime(SOURCE, "repro.paw", context={})
        runtime.run(block=False)
        app.processEvents()

        runtime.state.set("pic", str(image))
        app.processEvents()

        widget = runtime.root.findChild(QLabel, "pic")
        if widget is None:
            print("找不到 Image 控件（组件名可能变了）")
            return 2
        pixmap = widget.pixmap()
        shown = pixmap is not None and not pixmap.isNull()

        print(f"状态已设为图片路径: {image}")
        print(f"控件里是否有图像: {shown}")
        if shown:
            print("结果：有图像 —— 已修复")
            return 0
        print("结果：**没有图像** —— <Image> 不响应状态变化")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
