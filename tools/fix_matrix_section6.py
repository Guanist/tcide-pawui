# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""把 FEATURE_MATRIX.md 的损坏尾部截掉，重写正确的「许可证计划」一节。

不依赖复现乱码字节：按 `## 6.` 标记截断，再追加正确文本。
"""

from __future__ import annotations

import pathlib

TARGET = pathlib.Path(__file__).resolve().parent.parent / "FEATURE_MATRIX.md"

SECTION = """## 6. 许可证计划（用户决定：选 LGPL）

**决定**：修许可证时选 **LGPL**。**必须拆开授权**，原因如下（均为实测）：

| 文件 | 是否导入 PawUI | 可以用的许可证 |
|---|---|---|
| `tcide/`（18 个模块） | **0 处** | **LGPL-3.0** ✅ |
| `main.py` | `from pawui.runtime import Runtime` | **必须 GPL-3.0** |

**原因**：PawUI 本身是 **GPL-3.0**。GPL-3.0 要求链接它的**组合作品整体**按 GPL-3.0
发布，所以「整个 tcide-pawui 用 LGPL」在法务上不成立。但 `tcide/` 是一个
**不依赖 PawUI 的可复用库**（只用到标准库 + PySide6，Qt 本身也是 LGPL-3.0），
对它用 LGPL-3.0 完全正当 —— 这恰好是 LGPL 的设计用途（库）。

**执行步骤（放到"完成后"做）**：

1. `tcide/` 下每个模块加 LGPL-3.0 头 + `tcide/LICENSE`；
2. 仓库根加 `LICENSE`（GPL-3.0，覆盖 `main.py` / `app.paw` / `tools/`），
   `README` 里写清「库 = LGPL-3.0，应用 = GPL-3.0」；
3. 把 `tcide/shell_selftest.py` 移到应用侧（如 `app_selftest.py`），
   让 `tcide/` 保持**零界面依赖**，LGPL 边界才干净；
4. 加一条测试：断言 `tcide/` 下没有任何 `import pawui`（守住许可证边界）。

**另注**：TCIDE 原仓库的许可证问题（无 `LICENSE` 文件、`package.json` 写 MIT、
README 徽章写 GPLv3，三者矛盾）属于他们仓库，我们无权变更，只能在报告里指出。
"""

lines = TARGET.read_text(encoding="utf-8").splitlines()
cut = next((index for index, line in enumerate(lines) if line.startswith("## 6.")),
           len(lines))
kept = lines[:cut]
while kept and not kept[-1].strip():
    kept.pop()
damaged = [line for line in kept if "\ufffd" in line]
print(f"截断于第 {cut + 1} 行；保留 {len(kept)} 行；损坏行 {len(damaged)} 条")
for line in damaged[:5]:
    print("   " + line[:80])

TARGET.write_text("\n".join(kept) + "\n\n---\n\n" + SECTION, encoding="utf-8",
                  newline="")

check = TARGET.read_text(encoding="utf-8")
print()
print("校验：")
for probe in ("## 功能矩阵", "## 测试策略", "## 许可证计划", "LGPL-3.0", "pet.py"):
    print(f"  {'✓' if probe in check else '✗'} {probe}")
print(f"  总行数 {len(check.splitlines())}，全角问号占位 {check.count(chr(0xFFFD))} 处")
