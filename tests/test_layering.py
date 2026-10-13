# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""分层与许可证边界守卫（不需要 Qt，秒级）。

两条不变量：
1. ``tcide/`` **不得** import ``pawui`` —— 这是它能用 LGPL-3.0 的前提
   （PawUI 是 GPL-3.0；一旦 tcide/ 链接了它，整个库就必须跟着 GPL）。
2. ``app.paw`` 里出现的每个 ``on_*`` 处理器都必须在 ``main.py`` 的 context 里，
   每个 ``{$state}`` 都必须被 ``push()`` 推过去（原版最大的坑就是"实现了却没接线"）。
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "tcide"
APP_SIDE = ("main.py", "app.paw", "tools")


def _imports(path: pathlib.Path) -> set[str]:
    """文件里所有 import 的顶层模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class TestLicenseBoundary:
    def test_no_source_file_has_a_bom(self):
        """任何 ``tcide/`` 下的源码都不得有 UTF-8 BOM。

        这不是洁癖：带 BOM 的文件 ``ast.parse`` 会直接报
        ``invalid non-printable character U+FEFF``，于是**两个许可证边界测试
        会一起失败**，而错误信息完全指不到真正的原因（BOM）。
        起因是我用 PowerShell 的 ``Set-Content -Encoding utf8`` 建了一个文件 ——
        这正是本文件作者记下的第 19 条教训（不要用 PowerShell 文本 cmdlet 改
        UTF-8 文件）。这条守卫让同类问题不可能再悄悄复发。
        """
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            raw = path.read_bytes()
            if raw[:3] == b"\xef\xbb\xbf":
                offenders.append(path.relative_to(ROOT).as_posix())
        assert not offenders, f"这些文件带 UTF-8 BOM: {offenders}"

    def test_package_does_not_import_pawui(self):
        """LGPL 边界：tcide/ 一旦 import pawui，就必须整体 GPL。"""
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if "pawui" in _imports(path):
                offenders.append(path.relative_to(ROOT).as_posix())
        assert not offenders, (
            f"tcide/ 里出现了 pawui 依赖（会破坏 LGPL 边界）: {offenders}")

    def test_only_app_side_imports_pawui(self):
        """反过来确认：真正需要 pawui 的只有应用侧，边界是清楚的。"""
        app_imports = [
            path.relative_to(ROOT).as_posix()
            for name in APP_SIDE
            for path in ([ROOT / name] if name.endswith(".py")
                         else sorted((ROOT / name).rglob("*.py")))
            if path.exists() and "pawui" in _imports(path)
        ]
        assert "main.py" in app_imports, "main.py 应当是用 PawUI 的那一层"

    def test_package_does_not_import_third_party_http(self):
        """核心只用标准库 HTTP：不引入 httpx/requests（便于离线分发）。"""
        banned = {"httpx", "requests", "aiohttp"}
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            bad = _imports(path) & banned
            if bad:
                offenders.append(f"{path.name}: {sorted(bad)}")
        assert not offenders, f"tcide/ 引入了第三方 HTTP 库: {offenders}"

    def test_package_modules_are_importable_without_qt(self):
        """``tcide/`` 的**每个**模块在不装 Qt 时都能导入。

        （以前要排除 ``shell_selftest``，因为它在库侧却惰性用 PySide6；
        已经把它移到应用侧 ``app_selftest.py``，所以现在**没有例外**。）
        """
        import importlib

        for path in sorted(PACKAGE.glob("*.py")):
            name = path.stem
            if name == "__init__":
                continue
            module = importlib.import_module(f"tcide.{name}")
            assert module is not None, name

    def test_package_does_not_import_qt_at_module_level(self):
        """边界：库侧**模块级**不得依赖 Qt 界面库 / GPL 的 PawUI。

        只看模块级导入：``tcide/web/bridge.py`` 在 ``make_bridge()`` **函数内部**
        惰性导入 PySide6 —— 那是刻意的（这个库不装 Qt 也能导入、纯协议部分照常可测），
        而 PySide6 本身是 LGPL，不破坏许可证。真正要禁止的是"库一导入就拖进界面栈"。
        """
        banned = {"pawui", "PySide6", "PyQt5", "PyQt6"}
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names: set[str] = set()
            for node in tree.body:                      # 只遍历模块级语句
                if isinstance(node, ast.Import):
                    names |= {alias.name.split(".")[0] for alias in node.names}
                elif (isinstance(node, ast.ImportFrom) and node.module
                      and node.level == 0):
                    names.add(node.module.split(".")[0])
            bad = names & banned
            if bad:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {sorted(bad)}")
        assert not offenders, f"tcide/ 模块级依赖了界面库: {offenders}"


class TestSourceEncoding:
    """全仓编码门禁：中文被写成乱码/BOM/CRLF 时必须立刻红。

    为什么需要：我用 PowerShell 的 ``Get-Content``/``Set-Content`` 改 UTF-8 源文件
    **已经毁了三个文件**（FEATURE_MATRIX、tcide/web/__init__.py、tests/test_lsp.py）——
    症状是中文变成一串互不相干的汉字，而且往往要到很后面才被发现。这条门禁把
    "发现时间"缩到下一次跑测试。

    注意：本类里**不能出现**乱码样例字面量，否则门禁会把自己判成违规（试过两次）。
    所以标记一律用 ``\\uXXXX`` 转义写。
    """

    #: 乱码的典型标记（UTF-8 被按 GBK 解读后的高频字符）。
    #: **用转义写**：否则这个文件自己就"包含乱码"，门禁会把自己判成违规
    #: （第一次跑就发生了这个自指误报）。
    MOJIBAKE = ("\u951b", "\u9428", "\u6d93\u20ac", "\u5a09\u6d93\u6564",
                "\u5a34\u5b2d\u8bd5", "\u9341\u4ecb\u6570")

    @staticmethod
    def _python_files():
        for path in sorted(ROOT.rglob("*.py")):
            relative = path.relative_to(ROOT)
            # 跳过缓存与**测试自己生成的**临时产物（.scratch 里的假服务器故意
            # 输出 CRLF，那是它的协议内容，不是源文件问题）
            if "__pycache__" in relative.parts:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            yield path

    def test_no_python_file_has_a_bom(self):
        offenders = [path.relative_to(ROOT).as_posix() for path in self._python_files()
                     if path.read_bytes()[:3] == b"\xef\xbb\xbf"]
        assert not offenders, f"这些文件带 UTF-8 BOM: {offenders}"

    def test_no_python_file_is_mojibake(self):
        offenders = []
        for path in self._python_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "\ufffd" in text or any(mark in text for mark in self.MOJIBAKE):
                offenders.append(path.relative_to(ROOT).as_posix())
        assert not offenders, f"这些文件的中文已变成乱码: {offenders}"

    def test_docs_are_not_mojibake(self):
        offenders = []
        for name in ("README.md", "README_EN.md", "FEATURE_MATRIX.md",
                     "PAWUI_FINDINGS.md"):
            path = ROOT / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "\ufffd" in text or any(mark in text for mark in self.MOJIBAKE):
                offenders.append(name)
        assert not offenders, f"这些文档已变成乱码: {offenders}"

    def test_source_files_use_lf_not_crlf(self):
        """PS 的 Set-Content 会把换行改成 CRLF —— 这里也一并钉住。

        （不许用 PS 改源文件是硬规矩；这条只是让违规立刻可见。）
        """
        offenders = [path.relative_to(ROOT).as_posix()
                     for path in self._python_files()
                     if b"\r\n" in path.read_bytes()]
        assert not offenders, f"这些文件混入了 CRLF: {offenders}"


class TestLicenseHeaders:
    """SPDX 头门禁：**统一 LGPL-3.0-or-later**，新增文件忘了写头就会红。"""

    LGPL = "SPDX-License-Identifier: LGPL-3.0-or-later"
    #: 单独出现的 GPL 标识。``(?<!L)`` 不能少：``GPL-3.0-or-later`` 是
    #: ``LGPL-3.0-or-later`` 的**子串**，不锚定就会把每个 LGPL 文件误报成 GPL
    #: （这个坑在 tools/apply_licenses.py 里踩过一次，27 个文件被误报）。
    GPL_PATTERN = re.compile(r"SPDX-License-Identifier:\s*(?<!L)GPL-3\.0-or-later")

    def _python_files(self):
        return [*ROOT.glob("*.py"), *sorted(ROOT.glob("tools/*.py")),
                *sorted(ROOT.glob("tests/*.py")), *sorted(PACKAGE.rglob("*.py"))]

    def test_every_python_file_carries_the_lgpl_header(self):
        """全仓统一 LGPL：库侧与应用侧用同一个标识，不再分两套。"""
        missing = [path.relative_to(ROOT).as_posix() for path in self._python_files()
                   if path.is_file() and self.LGPL not in path.read_text(encoding="utf-8")]
        assert not missing, f"缺少 LGPL 头的文件: {missing}"

    def test_license_file_is_the_full_lgpl_text(self):
        """LICENSE 必须是 LGPL-3.0 **全文**（与 PawUI 同一份）。

        历史：这里曾经校验两份文件 —— GPL 的 ``LICENSE``（≥30000 字节）与
        LGPL 的 ``tcide/LICENSE``。PawUI 从 GPL 改为 LGPL-3.0-or-later 之后，
        整仓统一，``tcide/LICENSE`` 已删除，这里同步改成单一校验。
        """
        path = ROOT / "LICENSE"
        assert path.exists(), "缺少 LICENSE"
        text = path.read_text(encoding="utf-8")
        assert path.stat().st_size >= 7000, "LICENSE 太小，可能不是全文"
        assert "GNU LESSER GENERAL PUBLIC LICENSE" in text, "不是 LGPL 原文"
        assert "Version 3, 29 June 2007" in text, "不是 LGPL-3.0 原文"
        assert not (ROOT / "tcide" / "LICENSE").exists(), \
            "tcide/LICENSE 是拆开授权时代的第二份，统一后应已删除"

    def test_no_bare_gpl_identifier_anywhere(self):
        """不能有单独的 GPL 标识（锚定判断，避免子串误报）。"""
        wrong = [path.relative_to(ROOT).as_posix() for path in self._python_files()
                 if path.is_file()
                 and self.GPL_PATTERN.search(path.read_text(encoding="utf-8"))]
        assert not wrong, f"这些文件仍是 GPL 头: {wrong}"


class TestAppWiring:
    """app.paw 与 main.py 的接线守卫（不建树、不需要 Qt）。"""

    def test_every_handler_is_registered(self):
        paw = (ROOT / "app.paw").read_text(encoding="utf-8")
        referenced = set(re.findall(r'on_[a-z_]+\s*=\s*"([a-z_]+)"', paw))
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        registered = set(re.findall(r'"([a-z_]+)"', source.split("names = [")[1]
                                    .split("]")[0])) if "names = [" in source else set()
        missing = referenced - registered
        assert not missing, f"app.paw 引用了未注册的处理器: {sorted(missing)}"

    def test_every_state_key_is_pushed(self):
        """app.paw 用到的每个 ``{$state}`` 都必须有人提供。

        提供方式有两种，**都算**：

        1. ``main.py`` 的 ``push()`` 推过去（动态值）；
        2. ``app.paw`` 的 ``<script>`` 块里给了初值（静态值，不需要每帧推）。

        第一版只认第 1 种，于是"只在 script 里定义"的键被误报成缺失。
        """
        paw = (ROOT / "app.paw").read_text(encoding="utf-8")
        paw = re.sub(r"<!--.*?-->", "", paw, flags=re.S)      # 注释不算
        used = set(re.findall(r"\{\$([a-z_]+)\}", paw))
        assert used, "app.paw 里没有找到任何 {$state} 引用"

        source = (ROOT / "main.py").read_text(encoding="utf-8")
        payload = source.split("payload = {", 1)[1].split("\n        }", 1)[0]
        pushed = set(re.findall(r'"([a-z_]+)":', payload))

        script = paw.split("<script>", 1)[-1].split("</script>", 1)[0]
        declared = set(re.findall(r"^\s*([a-z_]+)\s*=", script, re.M))

        missing = sorted(used - pushed - declared)
        assert not missing, f"app.paw 用了但没人提供的 state: {missing}"

    def test_app_paw_parses_with_project_parser(self):
        """用 PawUI 自己的解析器验证 app.paw 语法（不需要建树）。"""
        import importlib.util

        import pytest

        if importlib.util.find_spec("pawui") is None:
            pytest.skip("需要安装 pawui")
        from pawui.parser import parse

        paw = (ROOT / "app.paw").read_text(encoding="utf-8")
        program = parse(paw, "app.paw")
        assert program.elements, "app.paw 没有解析出任何元素"
        assert any(element.tag == "Window" for element in program.elements)
        assert program.script is not None, "<script> 必须是顶层元素"
