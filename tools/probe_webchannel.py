# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""探针：file:// 页面 + QWebChannel 到底能不能连上。

复现 app 里的场景，但把 channel **在加载之前**建立（app 里是加载之后才建，
所以还额外测了 reload 的影响）。要回答两个问题：

1. ``file://`` 页面能不能执行 ``qrc:///qtwebchannel/qwebchannel.js``？
2. 页面里的 ``window.qt.webChannelTransport`` 会不会出现？
"""

from __future__ import annotations

import json
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from PySide6.QtCore import QTimer, QUrl                      # noqa: E402
from PySide6.QtWebChannel import QWebChannel                 # noqa: E402
from PySide6.QtWebEngineCore import QWebEngineSettings       # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView        # noqa: E402
from PySide6.QtWidgets import QApplication                   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tcide.web.bridge import EDITOR_HTML, editor_url, make_bridge   # noqa: E402

DIAG = ("JSON.stringify({qwc: typeof QWebChannel,"
        " transport: !!(window.qt && window.qt.webChannelTransport),"
        " bridge: !!window.editorBridge,"
        " value: (typeof window.tcideValue === 'function'"
        "         ? window.tcideValue().length : -1)})")


def main() -> int:
    app = QApplication([])
    view = QWebEngineView()
    view.resize(500, 320)
    page = view.page()

    # 让 file:// 页面能访问 file:// 资源（编辑器页面本身就是本地文件）
    page.settings().setAttribute(
        QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

    received: list[str] = []

    def on_event(event) -> None:
        received.append(event.kind)
        print(f"  <- 页面发来: {event.kind} {event.message!r}")

    bridge = make_bridge(on_event=on_event)
    channel = QWebChannel(page)
    channel.registerObject("editorBridge", bridge)
    page.setWebChannel(channel)          # **加载之前**建立
    print("channel 已在加载前建立")

    state = {"round": 0}

    def step() -> None:
        state["round"] += 1
        round_no = state["round"]

        def show(value=None) -> None:
            print(f"  [第 {round_no} 次] 页面诊断 -> {value}")
            if isinstance(value, str):
                try:
                    data = json.loads(value)
                except ValueError:
                    data = {}
                if data.get("bridge"):
                    print("  [✓] 桥已连上（window.editorBridge 存在）")
                    app.quit()
            if round_no >= 6:
                print("  [✗] 6 次探测后仍未连上")
                app.quit()

        page.runJavaScript(DIAG, show)
        if round_no < 6:
            QTimer.singleShot(700, step)

    def on_load(ok: bool) -> None:
        print(f"loadFinished -> {ok}")
        QTimer.singleShot(400, step)

    page.loadFinished.connect(on_load)
    url = QUrl.fromLocalFile(str(EDITOR_HTML))
    print("加载:", url.toString())
    view.setUrl(url)

    QTimer.singleShot(15000, app.quit)
    app.exec()

    print()
    print("页面发回的事件:", received or "（无）")
    print("editor_url():", editor_url())
    return 0 if "ready" in received else 1


if __name__ == "__main__":
    raise SystemExit(main())
