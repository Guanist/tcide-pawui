# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""诊断：Monaco 在**应用页面**里为什么起不来（独立探针里是好的）。

读页面里的 ``window.__monacoDiag``（自诊断对象），把"走到哪一步"打出来：
require 是否可见、config 是否成功、AMD 回调是否触发、脚本加载了哪些、有无脚本错误。

用法：``python tools/diagnose_monaco.py``
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    import main as tcide_main

    app_qt = QApplication.instance() or QApplication([])
    app = tcide_main.TCIDEApp(ROOT)

    opened = app.cmd_open_web_editor()
    print(f"打开 Web 编辑器: {opened}")
    view = app._web_view()
    if view is None:
        print("★ 拿不到视图")
        return 2

    def read(expr: str, key: str, box: dict, rounds: int = 40) -> object:
        for _ in range(rounds):
            view.page().runJavaScript(expr, lambda value, k=key: box.__setitem__(k, value))
            for _ in range(10):
                app_qt.processEvents()
                if key in box:
                    return box[key]
                time.sleep(0.02)
        return None

    # 等在应用里连上桥（这步之前一直是好的）
    for _ in range(100):
        app_qt.processEvents()
        if app._web_ready:
            break
        time.sleep(0.05)
    print(f"页面 ready: {app._web_ready}（{app._web_editor_line}）")

    box: dict = {}
    raw = read("JSON.stringify(window.__monacoDiag || {missing:true})", "diag", box)
    print()
    print("=== 页面自诊断 ===")
    try:
        diag = json.loads(str(raw))
    except Exception:
        print(f"  (读不到: {raw!r})")
        diag = {}
    for key in ("engine", "requireSeen", "configOk", "qrc", "started", "done",
                "error"):
        print(f"  {key:12} = {diag.get(key)!r}")
    print(f"  scripts      = {diag.get('scripts')}")
    print(f"  errors       = {diag.get('errors')}")

    # 再单独问几个关键量（避免只有自诊断对象的字段）
    for expr, label in (("typeof require", "typeof require"),
                        ("typeof monaco", "typeof monaco"),
                        ("window.tcideEngine ? window.tcideEngine() : 'n/a'",
                         "tcideEngine()")):
        print(f"  {label:16} -> {read(expr, label, {})!r}")

    # 给足时间：Monaco 首次要从本地读 2.4MB 主文件 + 分块
    print()
    print("=== 再等 20 秒看回调会不会补上 ===")
    for _ in range(200):
        app_qt.processEvents()
        time.sleep(0.1)
    box2: dict = {}
    raw2 = read("JSON.stringify(window.__monacoDiag || {})", "diag", box2)
    try:
        diag2 = json.loads(str(raw2))
    except Exception:
        diag2 = {}
    print(f"  20 秒后 engine = {diag2.get('engine')!r}  error = {diag2.get('error')!r}")
    if diag2.get("started") and not diag2.get("done"):
        print("  -> AMD 回调**从未触发**（请求发出去了但没回来）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(3)
