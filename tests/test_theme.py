# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""虎猫配色的测试：**深色 = 黑 + 橙**，**浅色 = 白 + 橙**。

调色板的**权威来源是 `tcide/theme.py`** —— 这里不复制一份色值表（第一版复制了，
改了主题之后测试自己先红，说明复制是错的）。

三层必须一致（否则界面会花）：
1. ``app.paw`` 的 ``<Theme extends="dark">`` —— 框架组件（按钮/面板/列表）用它；
2. ``styles/app.qss`` —— 框架没管的控件用它（浅色由 swap_qss 生成）；
3. ``main.py`` 的 ``FALLBACK_COLORS`` —— 推给模板的十六进制回退值。

运行时的验证在 ``app_selftest.py``（确认 PawUI 真的应用了主题，只写文件不算数）。
"""

from __future__ import annotations

import pathlib
import re

from tcide.theme import (LABELS, LIGHT_EXTRA, QSS_MAP, TIGER_DARK, TIGER_LIGHT,
                         contrast, hexes_in, palette, qss_complete, swap_qss)

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAW = (ROOT / "app.paw").read_text(encoding="utf-8")
QSS = (ROOT / "styles" / "app.qss").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

CORE = ("background", "surface", "text", "subtext", "accent", "border", "danger")


class TestTwoThemes:
    def test_dark_is_black_plus_orange(self):
        dark = TIGER_DARK
        assert contrast(dark["background"], "#000000") < 1.6, "深色底要接近纯黑"
        assert dark["accent"] == "#e8802a", "强调色是虎橙"
        assert contrast(dark["text"], dark["background"]) >= 7.0, "深色下正文要够亮"

    def test_light_is_white_plus_orange(self):
        light = TIGER_LIGHT
        assert contrast(light["background"], "#ffffff") < 1.1, "浅色底要接近纯白"
        assert light["accent"] == TIGER_DARK["accent"], "两套主题用**同一支橙**"
        assert contrast(light["text"], light["background"]) >= 7.0, "浅色下正文要够黑"

    def test_both_themes_share_the_orange_identity(self):
        """橙色是这只虎猫的标识色：换主题不该把它换掉。"""
        for name in ("dark", "light"):
            values = palette(name)
            assert values["accent"] == "#e8802a"
            assert values["fur"] == "#c96a1e"

    def test_palette_falls_back_to_dark(self):
        assert palette("不存在") == TIGER_DARK

    def test_labels_describe_both(self):
        assert "黑" in LABELS["dark"] and "白" in LABELS["light"]


class TestAccessibility:
    """好看之外还得看得清：两套主题都要过对比度。"""

    def test_body_text_contrast(self):
        for name, values in (("dark", TIGER_DARK), ("light", TIGER_LIGHT)):
            ratio = contrast(values["text"], values["background"])
            assert ratio >= 7.0, f"{name} 主题正文对比度只有 {ratio:.1f}:1（要 ≥7）"

    def test_secondary_text_contrast(self):
        for name, values in (("dark", TIGER_DARK), ("light", TIGER_LIGHT)):
            ratio = contrast(values["subtext"], values["surface"])
            assert ratio >= 4.5, f"{name} 主题次要文字只有 {ratio:.1f}:1（要 ≥4.5）"

    def test_orange_is_visible_on_both_backgrounds(self):
        for name, values in (("dark", TIGER_DARK), ("light", TIGER_LIGHT)):
            ratio = contrast(values["accent"], values["background"])
            assert ratio >= 2.5, f"{name} 主题里橙色在底色上会糊（{ratio:.1f}:1）"


class TestThemeIsDefinedInOnePlace:
    """主题必须**只在一处定义**（``tcide/theme.py``）。

    曾经把调色板也写进 ``app.paw`` 的 ``<Theme extends="dark">``，结果实测发现
    ``runtime.run()`` 建树时会按文件里的主题重新解析、把 Python 侧设好的覆盖回去
    —— 浅色主题只在窗口与工具栏生效、PawUI 组件仍是深色 ✗。所以现在：

    - 文件里**不得**出现 ``<Theme>`` 或 ``Window theme=``；
    - 调色板只在 ``tcide/theme.py``（本文件直接从那里 import，不再抄一份）。
    """

    def test_paw_does_not_hardcode_a_theme(self):
        # 注意：注释里**提到** `<Theme ...>` 是允许的（解释为什么不能写），
        # 所以要检测行首的元素，而不是全文子串。
        assert not re.search(r"^\s*<Theme\s", PAW, re.M), \
            "app.paw 里不该再有 <Theme> 元素（会覆盖 Python 侧主题）"
        assert not re.search(r'<Window[^>]*\btheme="', PAW), \
            "app.paw 的 <Window> 不该写 theme=（会覆盖 Python 侧主题）"

    def test_paw_points_to_the_authoritative_source(self):
        assert "tcide/theme.py" in PAW, "文件里要写明主题的权威来源在哪"

    def test_palette_lives_in_the_library(self):
        assert TIGER_DARK["accent"] == "#e8802a"
        assert TIGER_LIGHT["background"] != TIGER_DARK["background"]


class TestStylesheet:
    def test_every_color_in_the_qss_is_mapped(self):
        """映射表必须覆盖样式表里的**每一个**色值，否则浅色主题会花。"""
        missing = qss_complete(QSS)
        assert not missing, f"这些色值没有浅色映射：{missing}"

    def test_qss_uses_the_dark_palette(self):
        assert TIGER_DARK["accent"] in QSS
        assert QSS.count(TIGER_DARK["accent"]) >= 5, "强调色应贯穿选中/聚焦/滚动条"

    def test_qss_documents_the_palette(self):
        assert "虎猫皮毛" in QSS or "虎猫" in QSS
        assert "黑橙" in QSS or "浅色" in QSS, "样式表要写明两套主题的关系"

    def test_swap_produces_a_light_sheet(self):
        light = swap_qss(QSS, theme="light")
        assert TIGER_LIGHT["background"] in light, "浅色样式表应有白底"
        assert TIGER_DARK["background"] not in light, "深色底不该残留"
        assert TIGER_DARK["surface"] not in light, "深色面板色不该残留"
        assert TIGER_LIGHT["accent"] in light, "橙色保留"
        # 换色后出现的色值必须来自：浅色调色板、深色原文、或浅色补充块
        allowed = set(QSS_MAP.values()) | hexes_in(QSS) | hexes_in(LIGHT_EXTRA)
        unexpected = hexes_in(light) - allowed
        assert not unexpected, f"换色后出现了预期外的颜色：{sorted(unexpected)}"

    def test_dark_sheet_is_returned_unchanged(self):
        assert swap_qss(QSS, theme="dark") == QSS


class TestPythonFallbacksMatch:
    def test_fallback_colors_use_the_dark_palette(self):
        for name in ("accent", "border", "danger", "subtext"):
            assert f'"{name}": "{TIGER_DARK[name]}"' in MAIN, \
                f"main.py 的 FALLBACK_COLORS 里 {name} 应为 {TIGER_DARK[name]}"

    def test_no_leftover_cold_colors(self):
        offenders = [color for color in ("#4a9eff", "#ff453a", "#3a3a3c", "#14100b")
                     if color in MAIN]
        assert not offenders, f"main.py 里还留着旧配色：{offenders}"
