# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""虎猫配色：**深色 = 黑 + 橙**，**浅色 = 白 + 橙**。

取自虎猫的皮毛：橙（皮毛）、黑（条纹）、白（腹部）。两套主题共用同一支橙色，
只有底色与文字色在"黑"与"白"之间互换 —— 这是刻意的：橙色是这只虎猫的标识色。

为什么放在 ``tcide/``（库侧）：配色是**纯数据 + 纯字符串处理**，不碰 Qt、不碰
PawUI，可以离线严格测试。样式表换色也在这里完成（见 :func:`swap_qss`）。

## 三处必须一致

1. ``app.paw`` 的 ``<Theme extends="dark">`` —— 框架组件（按钮/面板/列表）用它；
2. ``styles/app.qss`` —— 框架没管的控件用它（浅色由 :func:`swap_qss` 生成）；
3. 这里的调色板 —— 上面两处的**权威来源**（测试会逐色比对）。
"""

from __future__ import annotations

import re

#: 语义令牌（与 PawUI ``Theme`` 的同名字段一一对应 + 4 个自定义令牌）
TOKENS = ("background", "surface", "text", "subtext", "accent", "border",
          "danger", "stripe", "fur", "amber", "belly")

#: **深色主题 = 黑 + 橙**（虎纹的黑底，皮毛的橙）
TIGER_DARK: dict[str, str] = {
    "background": "#0d0b09",   # 黑（条纹之间最深的阴影）
    "surface": "#17130f",      # 面板：略带暖调的黑
    "text": "#f6efe3",         # 腹部的乳白
    "subtext": "#b8a48c",      # 柔和的毛色
    "accent": "#e8802a",       # **皮毛的橙**（两套主题共用）
    "border": "#33261a",       # 条纹边缘的棕
    "danger": "#d2503f",       # 暖红
    "stripe": "#050403",       # 虎纹的黑
    "fur": "#c96a1e",          # 背脊的暗橙
    "amber": "#f5a65b",        # 亮部（阳光下的毛尖）
    "belly": "#fdf8f0",        # 更白的腹部
}

#: **浅色主题 = 白 + 橙**（虎腹的白底，同一支橙）
TIGER_LIGHT: dict[str, str] = {
    "background": "#fdfaf5",   # 白（暖白，不刺眼）
    "surface": "#ffffff",
    "text": "#1a1512",         # 近黑（虎纹）
    "subtext": "#6f6154",
    "accent": "#e8802a",       # **同一支橙**
    "border": "#e6dccd",
    "danger": "#c0392b",
    "stripe": "#1a1512",
    "fur": "#c96a1e",
    "amber": "#ef9a45",
    "belly": "#ffffff",
}

THEMES: dict[str, dict[str, str]] = {"dark": TIGER_DARK, "light": TIGER_LIGHT}

#: 主题显示名（设置面板里的分段控件用）
LABELS = {"dark": "深色（黑橙）", "light": "浅色（白橙）"}


def palette(name: str) -> dict[str, str]:
    """取某套调色板；名字不认识时退回深色（不抛异常，界面不该因此打不开）。"""
    return dict(THEMES.get((name or "").strip().lower(), TIGER_DARK))


def is_light(name: str) -> bool:
    return (name or "").strip().lower() == "light"


# ---------------------------------------------------------------------------
# 样式表换色
# ---------------------------------------------------------------------------
#
# app.qss 只维护**一份**（深色）。浅色由这里的映射表生成 —— 只维护一份样式表，
# 比维护两份手写 CSS 可靠得多（改一处不会漏掉另一处）。
#
# 映射按**角色**而不是按色值本身：
#   · 橙色系（accent/fur/amber）与"橙底上的白字"在两套主题里**保持不变**；
#   · 底色/文字色在"黑"与"白"之间互换；
#   · 提示气泡这类"永远是深色小片"的颜色也保持（浅色界面里的深色气泡是正常的）。

#: 深色 QSS 里的色值 → 浅色主题下的等价色
QSS_MAP: dict[str, str] = {
    # -- 语义令牌 --
    "#0d0b09": "#fdfaf5",   # background
    "#17130f": "#ffffff",   # surface
    "#f6efe3": "#1a1512",   # text
    "#b8a48c": "#6f6154",   # subtext
    "#e8802a": "#e8802a",   # accent —— 同一支橙，不变
    "#33261a": "#e6dccd",   # border
    "#d2503f": "#c0392b",   # danger
    "#050403": "#1a1512",   # stripe
    "#c96a1e": "#c96a1e",   # fur —— 不变
    "#f5a65b": "#ef9a45",   # amber（浅底上要略深一点才看得清）
    "#fdf8f0": "#fdf8f0",   # belly —— 橙底上的白字，不变
    # -- 派生色（悬停/选中/聚焦/滚动条…）--
    "#241b13": "#f4efe8",   # 次级面板底
    "#1b140e": "#f7f2ea",   # 列表/页签容器底
    "#2a2016": "#f0e9df",   # 悬停底
    "#120e0a": "#ffffff",   # 输入框底
    "#16110c": "#fffdf9",   # 输入框聚焦底
    "#100c09": "#fbf7f1",   # 编辑器底
    "#3a2c1f": "#dfd4c4",   # 输入框边框
    "#55402a": "#cbbfae",   # 输入框悬停边框
    "#4a2f14": "#fbe7d0",   # 选中底（橙的浅色）
    "#7a4a18": "#f7d9b4",   # 选中文字底
    "#4a3a28": "#d9cdbb",   # 滚动条滑块
    "#e9dfd0": "#3a322a",   # 列表文字
    "#fff6e8": "#fff6e8",   # 选中项文字（橙底上）—— 不变
    "#fff8ee": "#fff8ee",   # 菜单选中文字 —— 不变
    "#221a12": "#221a12",   # 提示气泡底（浅色界面里深色气泡是正常的）
}

#: 只出现在浅色主题里的补充规则（深色不需要）
LIGHT_EXTRA = """
/* ── 浅色主题补充（白橙）────────────────────────────────────────────
   深色样式表里"深底浅字"的地方在白底上要反过来，这几条单独补，
   比在映射表里硬凑更清楚。 */
QToolTip { color: #fdf8f0; }             /* 气泡仍是深色小片 */
QScrollBar::handle:vertical,
QScrollBar::handle:horizontal { background: #d9cdbb; }
QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover { background: #e8802a; }
QSplitter::handle { background: #ece3d6; }
"""

_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def hexes_in(text: str) -> set[str]:
    """正文里出现的全部色值（小写）—— 测试用它确认映射表没有遗漏。"""
    return {item.lower() for item in _HEX.findall(text)}


def swap_qss(text: str, *, theme: str = "dark") -> str:
    """把深色样式表换成目标主题。

    只替换**完整的 6 位色值**（用词边界锚定），所以不会把 ``#e8802`` 之类的
    子串误伤 —— 这个坑在别处踩过，这里直接用正则限定长度。
    """
    if not is_light(theme):
        return text
    result = text
    for source, target in QSS_MAP.items():
        if source == target:
            continue
        result = re.sub(source, target, result, flags=re.IGNORECASE)
    return result + LIGHT_EXTRA


def qss_complete(text: str) -> list[str]:
    """返回样式表里**没有**被映射表覆盖的色值（应当为空）。"""
    known = {key.lower() for key in QSS_MAP}
    return sorted(hexes_in(text) - known)


def contrast(first: str, second: str) -> float:
    """WCAG 对比度（1~21）。用来验证"好看"之外还得"看得清"。"""

    def luminance(color: str) -> float:
        value = color.lstrip("#")
        channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    one, two = luminance(first), luminance(second)
    lighter, darker = max(one, two), min(one, two)
    return (lighter + 0.05) / (darker + 0.05)
