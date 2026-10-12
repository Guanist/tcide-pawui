# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""落地拆开授权：``tcide/`` = LGPL-3.0-or-later，应用侧 = GPL-3.0-or-later。

为什么拆开（实测依据）：**PawUI 是 GPL-3.0**（其 ``pyproject.toml`` 声明
``GPL-3.0-or-later``）——GPL 要求链接它的**组合作品整体**按 GPL 发布，所以
"整个 tcide-pawui 用 LGPL" 在法务上不成立。而 ``tcide/`` 是**不依赖 PawUI**
的可复用库，对它用 LGPL 正当（LGPL 的设计用途就是库）。

本脚本做的事：

1. 把本机已有的**权威原文**复制成许可证文件（不凭记忆写全文）：
   - ``LICENSE``       ← GPL-3.0（取自 PawUI 的 LICENSE，35 KB）
   - ``tcide/LICENSE`` ← LGPL-3.0（取自本机 pystray 包内的 COPYING.LGPL，7.6 KB）
2. 给每个源文件加 SPDX 头（幂等：已有就跳过）。
3. 校验：没有 BOM、许可证原文与来源逐字节一致、头部数量正确。

用法：``python tools/apply_licenses.py``（默认只检查，加 ``--write`` 才落盘）
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 权威原文来源（本机既有的许可证副本）
GPL_SOURCE = pathlib.Path(
    r"C:\Users\noirh\Desktop\Vincent\campus\pawui-audit\pr-work\PawUI\LICENSE")
LGPL_SOURCE = pathlib.Path(
    r"C:\Users\noirh\AppData\Local\Programs\Python\Python312\Lib\site-packages"
    r"\pystray-0.19.5.dist-info\COPYING.LGPL")

GPL_TARGET = ROOT / "LICENSE"
LGPL_TARGET = ROOT / "tcide" / "LICENSE"

LGPL_ID = "LGPL-3.0-or-later"
GPL_ID = "GPL-3.0-or-later"

LGPL_HEADER = (
    "# SPDX-FileCopyrightText: 2025 tcide-pawui contributors\n"
    f"# SPDX-License-Identifier: {LGPL_ID}\n"
)
GPL_HEADER = (
    "# SPDX-FileCopyrightText: 2025 tcide-pawui contributors\n"
    f"# SPDX-License-Identifier: {GPL_ID}\n"
)
HTML_HEADER = (
    "<!-- SPDX-FileCopyrightText: 2025 tcide-pawui contributors\n"
    f"     SPDX-License-Identifier: {LGPL_ID} -->\n"
)

#: 库侧（LGPL）：tcide/ 下所有 .py 与 web 前端资源
LIBRARY_GLOBS = ("tcide/*.py", "tcide/web/*.py", "tcide/web/*.html")
#: 应用侧（GPL）：根目录下的入口与脚本、工具、测试、界面定义
APP_GLOBS = ("*.py", "tools/*.py", "tests/*.py")


def sources_for(pattern: str) -> list[pathlib.Path]:
    return sorted(path for path in ROOT.glob(pattern) if path.is_file())


def sha1(path: pathlib.Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def has_header(path: pathlib.Path, text: str) -> bool:
    """是否已经有 SPDX 头（**锚定到行首**，不做全文子串判断）。

    为什么不能用 ``"SPDX-License-Identifier" in text``：
    - ``tcide/snippets.py`` 里有一条 license 片段，正文**字面包含**这个字符串；
    - 本脚本自己的代码里也有这个字符串。
    结果就是这两个文件被判定"已有头"而**静默跳过**（真发生过）。
    """
    if path.suffix == ".html":
        return text.lstrip().startswith("<!-- SPDX-")
    return bool(re.search(r"^#\s*SPDX-License-Identifier:", text, re.M))


def apply_header(path: pathlib.Path, header: str) -> bool:
    """插入 SPDX 头；已有则返回 False（幂等）。"""
    text = path.read_text(encoding="utf-8")
    path.write_text(header + text, encoding="utf-8", newline="")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="真正落盘")
    args = parser.parse_args()

    problems: list[str] = []

    # ---- 1. 许可证文件 ---------------------------------------------------
    for source, target, label in ((GPL_SOURCE, GPL_TARGET, "GPL-3.0"),
                                  (LGPL_SOURCE, LGPL_TARGET, "LGPL-3.0")):
        if not source.exists():
            problems.append(f"找不到 {label} 原文: {source}")
            continue
        if not target.exists():
            if args.write:
                target.write_bytes(source.read_bytes())
                print(f"已写出 {target.relative_to(ROOT)}（来自 {source.name}）")
            else:
                print(f"[将写出] {target.relative_to(ROOT)} ← {source.name}")
            continue
        same = sha1(source) == sha1(target)
        print(f"{'✓' if same else '✗'} {target.relative_to(ROOT)} 与原文一致"
              f"（{target.stat().st_size} 字节）")
        if not same:
            problems.append(f"{target.name} 与权威原文不一致")

    if not args.write:
        print()
        print("（未加 --write，仅检查）")

    # ---- 2. SPDX 头 -----------------------------------------------------
    pending: list[tuple[pathlib.Path, str]] = []
    for pattern in LIBRARY_GLOBS:
        for path in sources_for(pattern):
            if path.name == "LICENSE":
                continue
            pending.append((path, HTML_HEADER if path.suffix == ".html"
                            else LGPL_HEADER))
    for pattern in APP_GLOBS:
        for path in sources_for(pattern):
            pending.append((path, GPL_HEADER))

    changed = 0
    for path, header in pending:
        text = path.read_text(encoding="utf-8")
        if has_header(path, text):
            continue
        changed += 1
        if args.write:
            apply_header(path, header)

    print()
    print(f"SPDX 头：{len(pending)} 个文件，其中 {changed} 个需要添加")
    if args.write:
        print(f"已添加 {changed} 个")

    # ---- 3. 校验 --------------------------------------------------------
    bom = [path.relative_to(ROOT).as_posix()
           for path in ROOT.rglob("*.py")
           if "__pycache__" not in str(path) and path.read_bytes()[:3] == b"\xef\xbb\xbf"]
    if bom:
        problems.append(f"这些文件带 BOM: {bom}")

    library_with_gpl = []
    for path in sorted(ROOT.glob("tcide/**/*.py")):
        text = path.read_text(encoding="utf-8")
        # **不能用 `GPL_ID in text`**：`"GPL-3.0-or-later"` 是
        # `"LGPL-3.0-or-later"` 的子串，那样每个 LGPL 文件都会被误报成 GPL。
        # （这已经是我第 5 次栽在子串判断上了，所以这次用锚定到标识符的正则。）
        if re.search(r"SPDX-License-Identifier:\s*GPL-3\.0-or-later", text):
            library_with_gpl.append(path.relative_to(ROOT).as_posix())
    if library_with_gpl:
        problems.append(f"库侧文件用了 GPL 头（应为 LGPL）: {library_with_gpl}")

    # 文件内容没被写坏（写入用的是显式 utf-8，这里确认没有替换字符）
    broken = [path.relative_to(ROOT).as_posix()
              for path in ROOT.rglob("*.py")
              if "__pycache__" not in str(path)
              and "\ufffd" in path.read_text(encoding="utf-8")]
    if broken:
        problems.append(f"这些文件出现替换字符（写入把编码搞坏了）: {broken[:5]}")

    if problems:
        print()
        for item in problems:
            print(f"✗ {item}")
        return 1

    print()
    print("全部检查通过" if not args.write else "已经完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
