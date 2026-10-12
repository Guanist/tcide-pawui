# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""探针：vendored 的 Monaco 0.57 能不能从 ``file://`` 页面加载起来。

要回答两件事：
1. ``loader.js`` 的 AMD 方式（``require(['vs/editor/editor.main'])``）在 0.57 里还有效吗？
2. 如果它是 ESM 布局，``file://`` 会不会被 CORS 拦住（那就必须改用本地 HTTP 服务）？
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "tcide" / "web" / "vendor" / "monaco"

PROBE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>monaco probe</title>
<style>html,body{margin:0;height:100%}#c{height:100%}</style>
<script src="vendor/monaco/loader.js"></script>
</head><body>
<div id="c"></div>
<script>
window.__probe = {amd: typeof require, monaco: "no"};
try {
  require.config({ paths: { vs: "vendor/monaco" } });
  require(["vs/editor/editor.main"], function () {
    window.__probe.monaco = "amd-ok";
    try {
      const ed = monaco.editor.create(document.getElementById("c"), {
        value: "x = 1\\n", language: "python", theme: "vs-dark",
        automaticLayout: true, minimap: { enabled: false }
      });
      window.__tcideEditor = ed;
      window.__probe.value = ed.getValue();
      window.__probe.langs = (monaco.languages.getLanguages() || []).length;
    } catch (e) { window.__probe.monaco = "create-failed: " + e; }
  }, function (err) { window.__probe.monaco = "amd-error: " + err; });
} catch (e) { window.__probe.monaco = "loader-failed: " + e; }
</script>
</body></html>
"""


def main() -> int:
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    if not (VENDOR / "loader.js").exists():
        print("★ vendor 目录里没有 loader.js，先跑 tools/vendor_monaco.py")
        return 2

    page_file = ROOT / "tcide" / "web" / "monaco_probe.html"
    page_file.write_text(PROBE_HTML, encoding="utf-8")

    app = QApplication([])
    view = QWebEngineView()
    view.resize(700, 420)
    state: dict = {}

    def check(round_no: int = 1) -> None:
        def got(value=None) -> None:
            print(f"  [第 {round_no} 次] __probe = {value}")
            state["probe"] = value
            if isinstance(value, dict) and str(value.get("monaco", "")).startswith(
                    ("amd-ok", "create-failed", "amd-error", "loader-failed")):
                app.quit()
                return
            if round_no >= 8:
                app.quit()
                return
            QTimer.singleShot(700, lambda: check(round_no + 1))

        view.page().runJavaScript("JSON.stringify(window.__probe)", got)

    view.loadFinished.connect(lambda ok: (print(f"loadFinished -> {ok}"),
                                         QTimer.singleShot(500, check)))
    view.setUrl(QUrl.fromLocalFile(str(page_file)))
    QTimer.singleShot(20000, app.quit)
    app.exec()

    probe = state.get("probe") or {}
    verdict = str(probe.get("monaco", "?"))
    print()
    print("=== 结论 ===")
    if verdict == "amd-ok":
        print("[OK] AMD 方式可用：", probe)
        return 0
    print(f"[XX] 从 file:// 直接加载 Monaco 失败：{verdict}")
    print("     -> 需要改用本地 HTTP 服务（ESM + file:// 会被 CORS 拦住），")
    print("        或 vendor 一个仍是纯 AMD 布局的旧版本。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
