# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""把 ``tcide/shell_selftest.py`` 移到应用侧 ``app_selftest.py``。

**为什么移**：它虽然只在函数内部惰性导入 PySide6，但它在 **LGPL 库**里 ——
"库不依赖界面"这条边界因此需要一个例外（`test_layering.py` 里要专门排除它）。
移出去之后：

- ``tcide/`` 真正零界面依赖 → LGPL 边界干净、可断言；
- 界面自检属于**应用**（当时应用侧是 GPL；**现在整仓统一 LGPL-3.0-or-later**）。

顺带把头部换成目标层当时的标识（现在两边都是 LGPL-3.0-or-later，见
``tools/apply_licenses.py``）。

用法：``python tools/move_selftest.py``（幂等：已经移过就什么都不做）
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tcide" / "shell_selftest.py"
TARGET = ROOT / "app_selftest.py"
IMPORTERS = (ROOT / "main.py", ROOT / "tests" / "test_shell.py")

OLD_IMPORT = "from tcide.shell_selftest import"
NEW_IMPORT = "from app_selftest import"


def main() -> int:
    if not SOURCE.exists():
        print("已经移动过了（tcide/shell_selftest.py 不存在）")
    else:
        text = SOURCE.read_text(encoding="utf-8")
        text = text.replace("tcide/shell_selftest.py", "app_selftest.py")
        text = text.replace(
            '"""界面自检：离屏把 IDE 的关键交互跑一遍。',
            '"""界面自检：离屏把 IDE 的关键交互跑一遍（**应用侧**）。\n\n'
            '原在 tcide/shell_selftest.py；为了让库（tcide/）保持零界面依赖，\n'
            '移到了应用侧。依赖 PySide6 与 PawUI（两者都是 LGPL-3.0-or-later）。\n'
            '第一行原文档字符串：', 1)
        TARGET.write_text(text, encoding="utf-8", newline="")
        SOURCE.unlink()
        print(f"已移动 -> {TARGET.relative_to(ROOT)}")

    for path in IMPORTERS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if OLD_IMPORT not in text:
            print(f"  {path.relative_to(ROOT)}：无需改导入")
            continue
        path.write_text(text.replace(OLD_IMPORT, NEW_IMPORT),
                        encoding="utf-8", newline="")
        print(f"  {path.relative_to(ROOT)}：导入已更新")

    # 校验
    left = [p.relative_to(ROOT).as_posix()
            for p in ROOT.glob("tcide/**/*.py")
            if "shell_selftest" in p.read_text(encoding="utf-8")]
    print("库侧残留引用:", left or "无")
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
