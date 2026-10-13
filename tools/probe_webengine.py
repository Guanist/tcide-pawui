# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""探针：QtWebEngine 在这个环境（offscreen + 沙箱）里到底能不能起来。

在把 ``<Web>`` 挂进窗口之前必须先测出来 —— 否则一挂上去就是"要么跑不起来、
要么静默白屏"，两种都很难查。
"""

from __future__ import annotations

import os
import sys
import traceback

sys.stdout.reconfigure(line_buffering=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen + 无特权环境下的常见必需开关（不加经常直接崩）
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

print("QT_QPA_PLATFORM =", os.environ["QT_QPA_PLATFORM"])
print("CHROMIUM_FLAGS  =", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])

HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>探针</title>
<style>body{background:#1d1d1f;color:#e8e8ea;font-family:monospace}</style></head>
<body><h1 id="t">WebEngine 探针</h1><p>中文也要能显示</p>
<textarea id="code">x = 1</textarea></body></html>"""


def main() -> int:
    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
    except Exception:
        print("★ 导入失败：")
        traceback.print_exc()
        return 2

    app = QApplication([])
    view = QWebEngineView()
    view.resize(600, 400)

    results: dict[str, object] = {}

    def on_load(ok: bool) -> None:
        results["load_finished"] = ok
        print(f"loadFinished -> {ok}")

        def got_title(title: str) -> None:
            results["title"] = title
            print(f"页面标题 -> {title!r}")

        def got_value(value) -> None:
            results["value"] = value
            print(f"runJavaScript 读回 textarea -> {value!r}")
            results["grab_size"] = (view.grab().width(), view.grab().height())
            print(f"grab() 尺寸 -> {results['grab_size']}")
            view.grab().save("shots/probe_webengine.png")
            app.quit()

        def got_scroll(_value=None) -> None:
            view.page().runJavaScript("document.getElementById('code').value",
                                      got_value)

        view.page().titleChanged.connect(got_title)
        view.page().runJavaScript(
            "document.getElementById('t').textContent", got_scroll)

    view.loadFinished.connect(on_load)
    view.setHtml(HTML, QUrl("file:///"))
    QTimer.singleShot(8000, app.quit)
    app.exec()

    print()
    print("=== 结论 ===")
    if results.get("load_finished"):
        print("[✓] QtWebEngine 在本环境可用（loadFinished=True）")
        print(f"     读回的 JS 值: {results.get('value')!r}")
        return 0
    print("[✗] QtWebEngine 未能完成加载 —— 记录为环境限制，")
    print("    界面继续用原生 <TextArea>，Web 编辑器标记为「需可用环境」")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
