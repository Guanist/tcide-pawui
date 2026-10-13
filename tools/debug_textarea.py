# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""最小复现：TextArea 内容里的 {$...} 到底在哪一步被吃掉。"""

from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(line_buffering=True)   # os._exit / Qt 崩溃时也不丢输出
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, r"C:\Users\noirh\Desktop\Vincent\campus\pawui-audit\pr-work\PawUI")

from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

import pawui.components as components  # noqa: E402
import pawui.resolve as resolve_mod  # noqa: E402
from pawui.runtime import Runtime  # noqa: E402

print("pawui:", sys.modules["pawui"].__file__)

qapp = QApplication.instance() or QApplication([])

SOURCE = """
<Window width="420" height="240">
  <TextArea id="ed" value="{$text}" height="140"/>
</Window>
"""

literal = "a = {$not_a_template}\nb = {$also_literal}\n"

# 给 _set_text 与 resolve_template / resolve_prop_value 都加上探针
original_set_text = components.TextArea._set_text
original_resolve_template = resolve_mod.resolve_template
original_resolve_prop = resolve_mod.resolve_prop_value


def traced_set_text(self, value):
    print(f"[probe] _set_text 收到: {value!r}")
    return original_set_text(self, value)


def traced_resolve_template(text, scope, runtime):
    result = original_resolve_template(text, scope, runtime)
    if "not_a_template" in str(text) or "not_a_template" in str(result):
        print(f"[probe] resolve_template: input={str(text)[:70]!r} -> "
              f"output={str(result)[:70]!r}")
    return result


def traced_resolve_prop(value, scope, runtime):
    result = original_resolve_prop(value, scope, runtime)
    if "not_a_template" in str(value) or "not_a_template" in str(result):
        print(f"[probe] resolve_prop_value: input={str(value)[:70]!r} -> "
              f"output={str(result)[:70]!r}")
    return result


components.TextArea._set_text = traced_set_text
resolve_mod.resolve_template = traced_resolve_template
resolve_mod.resolve_prop_value = traced_resolve_prop
components.resolve_prop_value = traced_resolve_prop

runtime = Runtime(SOURCE, "probe.paw")
runtime.run(block=False)
edit = runtime.root.findChild(QPlainTextEdit, "ed")
print(f"[probe] 建树后控件内容: {edit.toPlainText()!r}")

print("[probe] --- state.set('text', literal) ---")
runtime.state.set("text", literal)
qapp.processEvents()
print(f"[probe] state 里的值: {runtime.state.get('text')!r}")
print(f"[probe] 控件里的值:   {edit.toPlainText()!r}")
print(f"[probe] 是否一致: {edit.toPlainText() == literal}")
