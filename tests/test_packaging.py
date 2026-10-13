# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""打包与工程文件的自检（不需要 PyInstaller / Qt）。

**诚实说明**：本环境没有安装 PyInstaller，所以 ``tcide-pawui.spec`` 无法实跑。
这里只断言**能静态验证**的那部分，避免"写了个 spec 就宣称能打包"：

- spec 能被 Python 解析（PyInstaller 的 spec 就是 exec 的 Python）；
- spec 里声明要打包的数据文件**确实存在**；
- spec 里声明要导入的模块**确实能导入**；
- CI 文件存在且包含关键步骤（离屏环境变量、pytest）；
- README / LICENSE 相关文件齐备。
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "tcide-pawui.spec"


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


class TestSpecFile:
    def test_spec_exists(self):
        assert SPEC.exists(), "缺少 PyInstaller spec"

    def test_spec_is_valid_python(self):
        """spec 由 PyInstaller exec，语法必须正确。"""
        ast.parse(_spec_text())

    def test_declares_entry_point(self):
        assert 'main.py' in _spec_text()

    def test_spec_ships_web_assets(self):
        """前端页面必须随包分发，否则运行时的编辑器/终端窗口会是空白的。"""
        text = _spec_text()
        assert '"editor.html"' in text and '"terminal.html"' in text
        assert '"vendor"' in text, "spec 没有打包 vendored Monaco"

    def test_spec_supports_a_monaco_free_build(self):
        """``TCIDE_NO_MONACO=1`` 要能省掉约 24 MB（体积对分发包很敏感）。"""
        text = _spec_text()
        assert "TCIDE_NO_MONACO" in text and "EXCLUDE_MONACO" in text

    def test_spec_supports_console_and_light_builds(self):
        text = _spec_text()
        for flag in ("TCIDE_CONSOLE", "TCIDE_NO_WEBENGINE"):
            assert flag in text, f"spec 缺少 {flag} 开关"

    def test_declared_data_files_exist(self):
        """spec 的 DATAS 里每个路径都必须真实存在（否则打出来的包是坏的）。"""
        text = _spec_text()
        block = text.split("DATAS = [", 1)[1].split("]", 1)[0]
        names = re.findall(r'SPEC_DIR / "([^"]+)"', block)
        assert names, "DATAS 里没解析出任何文件"
        missing = [name for name in names if not (ROOT / name).exists()]
        assert not missing, f"spec 声明要打包但实际不存在的文件: {missing}"

    def test_hidden_imports_cover_dynamic_imports(self):
        """运行时动态用到的模块必须在 hiddenimports 里，否则打包后报 ModuleNotFoundError。"""
        text = _spec_text()
        block = text.split("HIDDEN_IMPORTS = [", 1)[1].split("]", 1)[0]
        declared = set(re.findall(r'"([^"]+)"', block))
        for required in ("pawui.runtime", "PySide6.QtWebEngineWidgets",
                         "PySide6.QtWebChannel", "sqlite3", "tracemalloc"):
            assert required in declared, f"hiddenimports 缺少 {required}"

    def test_stdlib_hidden_imports_are_importable(self):
        text = _spec_text()
        block = text.split("HIDDEN_IMPORTS = [", 1)[1].split("]", 1)[0]
        for name in re.findall(r'"([^"]+)"', block):
            if name.startswith("PySide6") or name.startswith("pawui"):
                continue          # 由环境决定，不强制
            importlib.import_module(name)

    def test_spec_does_not_import_pawui_at_build_time(self):
        """spec 顶部 import 了 tcide.cli 拿版本号；不能顺手把 pawui 也导进来。"""
        tree = ast.parse(_spec_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert "pawui" not in imported
        assert "PySide6" not in imported


class TestCI:
    CI = ROOT / ".github" / "workflows" / "ci.yml"

    def test_ci_exists(self):
        assert self.CI.exists(), "缺少 CI 配置"

    def test_ci_runs_tests_offscreen(self):
        """界面测试必须离屏跑：Qt 在无显示环境下会直接段错误。"""
        text = self.CI.read_text(encoding="utf-8")
        assert "QT_QPA_PLATFORM: offscreen" in text
        assert "pytest" in text

    def test_ci_runs_shell_selftest(self):
        assert "main.py --selftest" in self.CI.read_text(encoding="utf-8")

    def test_ci_guards_license_boundary(self):
        text = self.CI.read_text(encoding="utf-8")
        assert "test_layering" in text, "CI 应当跑许可证边界测试"

    def test_ci_does_not_ignore_conftest(self):
        """写 `--ignore=tests/conftest.py` 会让所有 fixture 失效（我犯过这个错）。"""
        assert "--ignore=tests/conftest.py" not in self.CI.read_text(encoding="utf-8")

    def test_ci_covers_windows(self):
        """本项目主要在 Windows 上开发，CI 必须覆盖它。"""
        assert "windows-latest" in self.CI.read_text(encoding="utf-8")

    def test_ci_quality_job_commands_are_real(self):
        """CI 里跑的命令必须真的存在（命令名写错的话 CI 只会静默跑空）。"""
        text = self.CI.read_text(encoding="utf-8")
        from tcide.cli import build_parser

        parser = build_parser()
        for command in re.findall(r"python -m tcide\.cli -C \. (\w+)", text):
            with pytest.raises(SystemExit) as info:
                parser.parse_args([command, "--help"])
            assert info.value.code == 0, f"CI 里的子命令 {command} 不存在"


class TestProjectFiles:
    def test_readme_both_languages(self):
        for name in ("README.md", "README_EN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            assert len(text) > 2000, f"{name} 太短了"
            assert "LGPL" in text, f"{name} 没写许可证方案"

    def test_readmes_link_each_other(self):
        assert "README_EN.md" in (ROOT / "README.md").read_text(encoding="utf-8")
        assert "README.md" in (ROOT / "README_EN.md").read_text(encoding="utf-8")

    def test_feature_matrix_exists_and_is_not_mojibake(self):
        text = (ROOT / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
        assert "\ufffd" not in text, "FEATURE_MATRIX.md 出现替换字符（编码被破坏过）"
        assert text.count("## ") >= 5

    def test_gitignore_actually_ignores_build_artifacts(self):
        """**语义**检查而不是子串检查。

        第一版这里断言 `"*.pyc" in text`，而 .gitignore 里写的是等价形式
        `*.py[cod]`，于是测试红了 —— 子串检查在 glob 上不可靠（这是我第 4 次
        栽在同一类错误上）。改成用 fnmatch 真的拿产物名去匹配模式。
        """
        import fnmatch

        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        patterns = [line.strip() for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]

        def ignored(name: str) -> bool:
            for pattern in patterns:
                bare = pattern.rstrip("/")
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name, bare):
                    return True
            return False

        for artifact in ("__pycache__", "a.pyc", "a.pyo", "a.pyd", "build", "dist",
                         ".pytest_cache", ".venv", "Thumbs.db"):
            assert ignored(artifact), f".gitignore 没有忽略 {artifact}"

    def test_pytest_ini_disables_cache(self):
        """工作区的 pytest 缓存目录 ACL 有问题，必须关掉缓存插件。"""
        text = (ROOT / "pytest.ini").read_text(encoding="utf-8")
        assert "no:cacheprovider" in text
