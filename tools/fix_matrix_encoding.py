# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""修复被 PowerShell Get-Content/Set-Content 往返破坏的 UTF-8 编码。

现象：原文 UTF-8 字节被按 GBK 解码后再存成 UTF-8，于是 `关键` 变成 `鍏抽敭`。
这是可逆变换：把当前文本按 GBK 编码回去，就得到原始 UTF-8 字节。
"""

from __future__ import annotations

import pathlib

TARGET = pathlib.Path(__file__).resolve().parent.parent / "FEATURE_MATRIX.md"
MARKERS = ("鍏抽", "鍔熻兘", "璁稿彲")

raw = TARGET.read_bytes()
text = raw.decode("utf-8", errors="replace")
print(f"文件 {len(raw)} 字节，当前是否含乱码标记: "
      f"{[m for m in MARKERS if m in text]}")

if any(marker in text for marker in MARKERS):
    try:
        fixed = text.encode("gbk", errors="strict").decode("utf-8", errors="strict")
    except UnicodeError as exc:
        print(f"★ 直接还原失败（{exc}），改用逐行容错还原")
        fixed = text.encode("gbk", errors="replace").decode("utf-8", errors="replace")
    TARGET.write_text(fixed, encoding="utf-8", newline="")
    print("已还原并重写")
else:
    print("没有检测到乱码，无需修改")
    fixed = text

check = TARGET.read_text(encoding="utf-8")
print()
print("校验：")
for probe in ("## 功能矩阵", "## 许可证计划", "## 测试策略", "tcide/", "pet.py"):
    print(f"  {'✓' if probe in check else '✗'} {probe}")
print(f"  行数: {len(check.splitlines())}")
print()
print("标题一览：")
for line in check.splitlines():
    if line.startswith("## "):
        print("  " + line)
