# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""统一许可证为 **LGPL-3.0-or-later**（与最新版 PawUI 一致），并给每个源文件加 SPDX 头。

## 为什么现在能统一成 LGPL（历史很重要，别再改回去）

本仓库**曾经**是"拆开授权"：``tcide/`` = LGPL-3.0-or-later，应用侧 = GPL-3.0-or-later。
理由是当时 **PawUI 是 GPL-3.0** —— GPL 要求链接它的**组合作品整体**按 GPL 发布，
所以"整个应用用 LGPL"在法务上不成立。

**PawUI 后来把许可证改成了 LGPL-3.0-or-later**（已核实，2026-10 抓取）：

- ``pyproject.toml``：``license = "LGPL-3.0-or-later"``
- GitHub 仓库元数据：``LGPL-3.0``（GNU Lesser General Public License v3.0）
- ``LICENSE`` 文件：完整的 Lesser 文本（7,652 字节；注意不是 GPL 文本）

于是"整仓统一 LGPL-3.0-or-later"成立 —— 本脚本就把仓库收敛到这一份许可证。

## 本脚本做的事

1. **统一 SPDX 头**：把任何 ``GPL-3.0-or-later`` 头改写成 ``LGPL-3.0-or-later``
   （旧脚本只会"跳过已有头"，改不动历史遗留 ✗）；缺头的补上（幂等）。
2. **许可证文件**：``LICENSE`` 必须是 LGPL-3.0 原文。可以用
   ``--license-source <PawUI 的 LICENSE 路径>`` 安装/校正；不指定则只校验。
   同时把已废弃的 ``tcide/LICENSE``（拆开授权时代的第二份）删掉。
3. **校验**：无 BOM、无替换字符、**全仓不再出现单独的 GPL 标识**、每个源文件都有头。

用法::

    python tools/apply_licenses.py                                   # 只检查
    python tools/apply_licenses.py --write                           # 落盘
    python tools/apply_licenses.py --write --license-source <path>   # 连许可证一起校正

校验用的标识串一律**锚定到标识符**：``GPL-3.0-or-later`` 是
``LGPL-3.0-or-later`` 的子串，用 ``in`` 判断会把每个 LGPL 文件误报成 GPL
（这个坑我踩过多次）。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

LICENSE_ID = "LGPL-3.0-or-later"
OLD_ID = "GPL-3.0-or-later"

#: 许可证文件（单一）
LICENSE_FILE = ROOT / "LICENSE"
#: 拆开授权时代的第二份许可证，现已废弃
OBSOLETE_LICENSE = ROOT / "tcide" / "LICENSE"

#: PawUI 的官方许可证地址（校验失败时提示用）
PAWUI_LICENSE_URL = "https://raw.githubusercontent.com/LK-BLOG/PawUI/main/LICENSE"

PY_HEADER = (
    "# SPDX-FileCopyrightText: 2025 tcide-pawui contributors\n"
    f"# SPDX-License-Identifier: {LICENSE_ID}\n"
)
MARKUP_HEADER = (
    "<!-- SPDX-FileCopyrightText: 2025 tcide-pawui contributors\n"
    f"     SPDX-License-Identifier: {LICENSE_ID} -->\n"
)

#: 需要 SPDX 头的文件（markup 用 HTML 注释形式）
PY_GLOBS = ("*.py", "tools/*.py", "tests/*.py", "tcide/*.py", "tcide/web/*.py")
MARKUP_GLOBS = ("app.paw", "tcide/web/*.html")

#: 许可证原文必须包含的片段（用于确认拿到的是 Lesser 而不是 GPL 全文）
LICENSE_MARKERS = ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007")

#: ``(?<!L)`` 很关键：否则会把 LGPL 头二次改写成 LGLGPL。
#: **只匹配 SPDX 头行**，不做全文替换 —— 文档里关于"历史上曾是 GPL"的说明必须保留，
#: 那是事实记录，改掉就等于篡改历史。
SPDX_OLD = re.compile(
    r"(?m)^(\s*(?:#|<!--|//|\*)?\s*SPDX-License-Identifier:\s*)(?<!L)"
    + re.escape(OLD_ID))
#: 正文里提到 GPL 的地方（只提示、不自动改，交给人判断）
PROSE_GPL = re.compile(r"(?<!L)" + re.escape(OLD_ID))


def sources_for(pattern: str) -> list[pathlib.Path]:
    return sorted(path for path in ROOT.glob(pattern) if path.is_file())


def sha1(path: pathlib.Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def has_header(path: pathlib.Path, text: str) -> bool:
    """是否已有 SPDX 头（**锚定到行首**，不做全文子串判断）。

    为什么不能用 ``"SPDX-License-Identifier" in text``：``tcide/snippets.py`` 里
    有一条 license 片段，正文**字面包含**这个字符串；本脚本自己的代码里也有。
    用子串判断会让这两个文件被判定"已有头"而静默跳过（真发生过）。
    """
    if path.suffix in (".html", ".paw"):
        return text.lstrip().startswith("<!-- SPDX-")
    return bool(re.search(r"^#\s*SPDX-License-Identifier:", text, re.M))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="真正落盘")
    parser.add_argument("--license-source", default="",
                        help="PawUI 的 LICENSE 路径（用于安装/校正本仓库的 LICENSE）")
    args = parser.parse_args()

    problems: list[str] = []
    notes: list[str] = []

    # ---- 1. 许可证文件 ---------------------------------------------------
    if args.license_source:
        source = pathlib.Path(args.license_source)
        if not source.is_file():
            problems.append(f"找不到许可证原文：{source}")
        else:
            text = source.read_text(encoding="utf-8")
            missing = [marker for marker in LICENSE_MARKERS if marker not in text]
            if missing:
                problems.append(f"{source.name} 不是 LGPL-3.0 原文（缺少 {missing}）")
            elif args.write:
                LICENSE_FILE.write_bytes(source.read_bytes())
                print(f"已写出 LICENSE ← {source.name}"
                      f"（{LICENSE_FILE.stat().st_size} 字节）")

    if LICENSE_FILE.is_file():
        text = LICENSE_FILE.read_text(encoding="utf-8")
        ok = all(marker in text for marker in LICENSE_MARKERS)
        print(f"{'✓' if ok else '✗'} LICENSE 是 LGPL-3.0 原文"
              f"（{LICENSE_FILE.stat().st_size} 字节）")
        if not ok:
            problems.append("LICENSE 不是 LGPL-3.0 原文；"
                            f"可从 {PAWUI_LICENSE_URL} 取，或用 --license-source 指定")
    else:
        problems.append("没有 LICENSE 文件")

    if OBSOLETE_LICENSE.is_file():
        if args.write:
            OBSOLETE_LICENSE.unlink()
            print("已删除 tcide/LICENSE（拆开授权时代的第二份，现已统一）")
        else:
            notes.append("[将删除] tcide/LICENSE（拆开授权时代的第二份）")

    if not args.write:
        print()
        print("（未加 --write，仅检查）")

    # ---- 2. SPDX 头：补缺 + 改写旧标识 -----------------------------------
    pending = [(path, MARKUP_HEADER) for pattern in MARKUP_GLOBS
               for path in sources_for(pattern)]
    pending += [(path, PY_HEADER) for pattern in PY_GLOBS
                for path in sources_for(pattern)]

    added = 0
    rewritten = 0
    for path, header in pending:
        if path.name == "LICENSE":
            continue
        text = path.read_text(encoding="utf-8")
        if not has_header(path, text):
            added += 1
            if args.write:
                path.write_text(header + text, encoding="utf-8", newline="")
            continue
        # 已有头但标识是旧的（GPL）→ 只改 SPDX 头行
        if SPDX_OLD.search(text):
            rewritten += 1
            if args.write:
                path.write_text(SPDX_OLD.sub(r"\g<1>" + LICENSE_ID, text),
                                encoding="utf-8", newline="")

    print()
    print(f"SPDX 头：扫描 {len(pending)} 个文件，补 {added} 个，改写旧标识 {rewritten} 个")

    # ---- 3. 校验 --------------------------------------------------------
    bom = [path.relative_to(ROOT).as_posix()
           for path in ROOT.rglob("*.py")
           if "__pycache__" not in str(path) and path.read_bytes()[:3] == b"\xef\xbb\xbf"]
    if bom:
        problems.append(f"这些文件带 BOM: {bom}")

    # **SPDX 头**里不该再出现单独的 GPL 标识（锚定到 SPDX 行，避免子串误报）
    lingering: list[str] = []
    prose: list[str] = []
    for pattern in ("*.py", "*.md", "*.txt", "*.paw", "*.spec", "*.ini",
                    "tools/*.py", "tests/*.py", "tcide/*.py", "tcide/web/*.html"):
        for path in sources_for(pattern):
            if path.name == "LICENSE":
                continue
            text = path.read_text(encoding="utf-8")
            if SPDX_OLD.search(text):
                lingering.append(path.relative_to(ROOT).as_posix())
            elif PROSE_GPL.search(text):
                prose.append(path.relative_to(ROOT).as_posix())
    if lingering:
        problems.append(f"SPDX 头仍是 GPL（应统一为 LGPL）: {lingering[:8]}")
    if prose:
        print()
        print("以下文件的**正文**提到 GPL（不自动改，请人工确认是否属于历史说明）：")
        for name in prose[:12]:
            print(f"  · {name}")

    missing_header = [path.relative_to(ROOT).as_posix() for pattern in PY_GLOBS
                      for path in sources_for(pattern)
                      if not has_header(path, path.read_text(encoding="utf-8"))]
    if missing_header:
        problems.append(f"缺少 SPDX 头: {missing_header[:8]}")

    broken = [path.relative_to(ROOT).as_posix()
              for path in ROOT.rglob("*.py")
              if "__pycache__" not in str(path)
              and "\ufffd" in path.read_text(encoding="utf-8")]
    if broken:
        problems.append(f"这些文件出现替换字符（写入把编码搞坏了）: {broken[:5]}")

    for note in notes:
        print(note)
    if problems:
        print()
        for item in problems:
            print(f"✗ {item}")
        return 1

    print()
    print("全部检查通过（统一为 LGPL-3.0-or-later）" if not args.write else "已经完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
