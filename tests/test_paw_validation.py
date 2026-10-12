# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""用 PawUI 自己的校验器静态检查 app.paw（parse-only，不需要 Qt）。

**为什么需要这条测试**：属性名写错**不会**让窗口建树失败 —— 那个控件只是永远
空着。界面自检也发现不了（它不检查控件内容）。本轮就是靠 `pawui check` 抓到
``<CodeBlock text="...">``（CodeBlock 根本没有内容属性），也就是 Diff 审核弹窗里
的 diff **从来没显示出来过**。

所以把校验器变成门禁：本地跑测试就拦得住，不必等 CI。
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

PAWUI_MISSING = importlib.util.find_spec("pawui") is None

pytestmark = pytest.mark.skipif(PAWUI_MISSING, reason="需要安装 pawui")

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_PAW = ROOT / "app.paw"

#: 工作区内的临时目录（**不用 tmp_path**：系统临时目录在本机沙箱下权限不稳定，
#: 会让 fixture 在 setup 阶段就 `PermissionError`，看不到任何断言失败）
SCRATCH = pathlib.Path(__file__).resolve().parent / ".scratch" / "pawcheck"


def _scratch_file(name: str, content: str) -> pathlib.Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / name
    path.write_text(content, encoding="utf-8")
    return path


class TestAppPawValidation:
    def test_app_paw_passes_pawui_check(self, capsys):
        from pawui.cli import check

        code = check(str(APP_PAW))
        captured = capsys.readouterr()
        assert code == 0, f"app.paw 没通过校验：{captured.out}{captured.err}"

    def test_validator_rejects_unknown_props(self):
        """反向验证：校验器**不是**永远返回 0，否则上一条测试毫无意义。"""
        from pawui.cli import check

        bad = _scratch_file("bad.paw",
                            '<Window title="x"><Column>'
                            '<CodeBlock text="这不被支持"/>'
                            "</Column></Window>")
        try:
            assert check(str(bad)) == 1
        finally:
            bad.unlink(missing_ok=True)

    def test_validator_rejects_syntax_errors(self):
        from pawui.cli import check

        bad = _scratch_file("broken.paw", "<Window title='没闭合>")
        try:
            assert check(str(bad)) == 1
        finally:
            bad.unlink(missing_ok=True)

    def test_app_paw_has_no_codeblock_with_text_prop(self):
        """把具体那个坑单独钉住（即使校验器被换掉也不会悄悄回归）。"""
        text = APP_PAW.read_text(encoding="utf-8")
        assert "<CodeBlock" not in text or 'text="' not in text.split("<CodeBlock")[1][:200]

    def test_every_handler_name_exists_in_main(self):
        """校验器只检查名字形式；这里确认 main.py 里真有这些方法。

        （app.paw 引用的 on_* / cmd_* 都必须能在 TCIDEApp 上找到。）
        """
        import ast
        import re

        text = APP_PAW.read_text(encoding="utf-8")
        referenced = set(re.findall(r'(?:on_click|on_change|on_enter|on_select|'
                                    r'on_press|on_toggle|on_accept|on_reject|'
                                    r'on_draw)="([a-z_]+)"', text))
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        defined = {node.name for node in ast.walk(ast.parse(source))
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = sorted(name for name in referenced if name not in defined)
        assert not missing, f"app.paw 引用了 main.py 里不存在的方法: {missing}"
