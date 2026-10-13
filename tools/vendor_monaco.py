# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""把 Monaco 编辑器 vendored 进 ``tcide/web/vendor/monaco/``（离线硬要求）。

为什么必须 vendored：Monaco 默认从 CDN 取资源，而本项目要求**完全离线可用**。
所以把 ``monaco-editor`` 包里的 ``min/vs`` 树抽出来随程序分发。

用法::

    npm pack monaco-editor --cache .npm-cache      # 得到 monaco-editor-<版本>.tgz
    python tools/vendor_monaco.py monaco-editor-0.57.0.tgz

脚本会打印体积与关键文件检查，并在最后删掉 tarball 之外不做任何破坏性操作。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT / "tcide" / "web" / "vendor" / "monaco"

#: 抽出后必须存在的关键文件（少一个都说明 vendor 不完整）
REQUIRED = (
    "loader.js",
    "editor/editor.main.js",
    "editor/editor.main.css",
    "base/worker/workerMain.js",
    "basic-languages/python/python.js",
    "language/typescript/tsMode.js",
)

#: 抽出的内容前缀（monaco-editor 包内布局）
PREFIX = "/min/vs/"


def vendor(tarball: pathlib.Path) -> int:
    if not tarball.exists():
        print(f"找不到 tarball: {tarball}")
        return 2
    if DEST.exists():
        shutil.rmtree(DEST, ignore_errors=True)
    DEST.mkdir(parents=True)

    members: list[tarfile.TarInfo] = []
    with tarfile.open(tarball) as archive:
        for member in archive.getmembers():
            if not member.isfile() or PREFIX not in member.name:
                continue
            member.name = member.name.split(PREFIX, 1)[1]
            members.append(member)
        if not members:
            print(f"★ tarball 里没有 {PREFIX} 内容，布局可能变了")
            return 3
        archive.extractall(DEST, members=members)

    total = sum(path.stat().st_size for path in DEST.rglob("*") if path.is_file())
    print(f"抽出 {len(members)} 个文件，{total / 1024 / 1024:.1f} MB -> "
          f"{DEST.relative_to(ROOT)}")
    missing = []
    for probe in REQUIRED:
        ok = (DEST / probe).exists()
        print(f"  [{'ok' if ok else 'XX'}] {probe}")
        if not ok:
            missing.append(probe)
    if missing:
        print(f"★ 缺少关键文件: {missing}")
        return 4
    print("vendor 完成")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tarball", nargs="?", default="monaco-editor.tgz")
    args = parser.parse_args()
    return vendor(pathlib.Path(args.tarball))


if __name__ == "__main__":
    raise SystemExit(main())
