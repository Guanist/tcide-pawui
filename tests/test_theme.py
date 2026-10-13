# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""虎猫皮毛主题（橙 · 黑 · 白）的测试。

三层必须一致，否则界面会花：
1. ``app.paw`` 的 ``<Theme extends="dark">`` —— 框架组件（按钮/面板/列表）用它；
2. ``styles/app.qss`` —— 框架没管的那些控件用它；
3. ``main.py`` 的 ``FALLBACK_COLORS`` —— 推给模板的十六进制回退值。

（另外还有一条**运行时**检查在 ``app_selftest.py`` 里：确认 PawUI 真的把主题
应用到了 ``runtime.theme`` 上 —— 只写在文件里不算数。）
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAW = (ROOT / "app.paw").read_text(encoding="utf-8")
QSS = (ROOT / "styles" / "app.qss").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

#: 虎猫皮毛调色板（权威定义；三处必须一致）
PALETTE = {
    "accent": "#e8802a",       # 皮毛的橙
    "amber": "#f5a65b",        # 亮部
    "fur": "#c96a1e",          # 背脊的暗橙
    "background": "#14100b",   # 条纹之间的阴影
    "surface": "#1f1811",      # 暖黑面板
    "stripe": "#0d0a07",       # 虎纹的黑
    "text": "#f6efe3",         # 腹部乳白
    "belly": "#fdf8f0",        # 更白的腹部
    "subtext": "#b8a48c",      # 柔和的毛色
    "border": "#3b2c1e",       # 条纹边缘的棕
    "danger": "#d2503f",       # 暖红
}

#: 改造前的冷色（蓝色系）—— 不该再出现
OLD_BLUES = ("#4a9eff", "#2f4f7f", "#1d1d1f", "#3a3a3c", "#9aa4b2", "#ff453a")


def _luminance(color: str) -> float:
    value = color.lstrip("#")
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    """WCAG 对比度（1~21）。"""
    one, two = _luminance(foreground), _luminance(background)
    lighter, darker = max(one, two), min(one, two)
    return (lighter + 0.05) / (darker + 0.05)


class TestPaletteIsDeclared:
    def test_paw_declares_a_theme_block_extending_dark(self):
        assert re.search(r'<Theme\s+extends="dark"\s*>', PAW), \
            "app.paw 里应有 <Theme extends=\"dark\">（框架组件靠它变色）"

    def test_every_core_color_is_overridden_in_paw(self):
        declared = dict(re.findall(r'<Color\s+name="([a-z_]+)"\s+value="(#[0-9a-f]{6})"',
                                   PAW))
        for name in ("background", "surface", "text", "subtext", "accent",
                     "border", "danger"):
            assert name in declared, f"主题里缺少核心色 {name}"
            assert declared[name] == PALETTE[name], \
                f"{name} 应为 {PALETTE[name]}，实际 {declared[name]}"

    def test_custom_tiger_tokens_exist(self):
        """custom 令牌可以在任意 bg=/color= 上用（PawUI 会从 theme.custom 解析）。"""
        declared = dict(re.findall(r'<Color\s+name="([a-z_]+)"\s+value="(#[0-9a-f]{6})"',
                                   PAW))
        for name in ("stripe", "fur", "amber", "belly"):
            assert declared.get(name) == PALETTE[name], f"缺少自定义令牌 {name}"


class TestStylesheetMatches:
    def test_qss_uses_the_tiger_accent(self):
        assert PALETTE["accent"] in QSS
        assert QSS.count(PALETTE["accent"]) >= 5, "强调色应贯穿选中/聚焦/滚动条等处"

    def test_qss_has_no_leftover_cold_colors(self):
        leftovers = [color for color in OLD_BLUES if color in QSS]
        assert not leftovers, f"样式表里还留着旧的冷色：{leftovers}"

    def test_qss_documents_the_palette(self):
        """配色表要写在样式表顶部 —— 后来的人才知道每个色值的来历。"""
        assert "虎猫皮毛" in QSS
        assert PALETTE["background"] in QSS and PALETTE["text"] in QSS


class TestPythonFallbacksMatch:
    def test_fallback_colors_use_tiger_values(self):
        for name, value in (("accent", PALETTE["accent"]),
                            ("border", PALETTE["border"]),
                            ("danger", PALETTE["danger"]),
                            ("subtext", PALETTE["subtext"])):
            assert f'"{name}": "{value}"' in MAIN, \
                f"main.py 的 FALLBACK_COLORS 里 {name} 应为 {value}"

    def test_no_leftover_cold_colors_in_main(self):
        offenders = [color for color in ("#4a9eff", "#ff453a", "#3a3a3c")
                     if color in MAIN]
        assert not offenders, f"main.py 里还留着旧配色：{offenders}"


class TestReadability:
    """虎猫配色要好用，不只是好看：文字必须读得清。"""

    def test_body_text_contrast(self):
        ratio = contrast(PALETTE["text"], PALETTE["background"])
        assert ratio >= 7.0, f"正文对比度只有 {ratio:.1f}:1，低于 7:1（AAA）"

    def test_secondary_text_contrast(self):
        ratio = contrast(PALETTE["subtext"], PALETTE["surface"])
        assert ratio >= 4.5, f"次要文字对比度只有 {ratio:.1f}:1，低于 4.5:1（AA）"

    def test_accent_on_dark_is_visible(self):
        ratio = contrast(PALETTE["accent"], PALETTE["background"])
        assert ratio >= 4.5, f"强调色对比度只有 {ratio:.1f}:1，橙色在暗底上会糊"

    def test_on_accent_text_is_dark_enough(self):
        """橙色底上的文字用乳白还是黑？算一下，别拍脑袋。"""
        assert contrast(PALETTE["belly"], PALETTE["accent"]) >= 2.5, \
            "橙底配乳白文字对比度过低"
