# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Web 编辑器（独立窗口）的实测测试。

**为什么单独一个文件**：QtWebEngine 必须离屏 + 一套 Chromium 开关才能跑，
而且它的渲染进程会抢占事件循环。放进 ``test_shell.py`` 会把界面自检打坏
（实测从 15/16 掉到 8/16），所以这里**独立**验证：

- 窗口是**懒创建**的（不打开就不存在）；
- 打开之后页面真的加载、真的连上 QWebChannel、文本真的同步过去；
- 页面发回的改动真的进到应用状态（并且不会回环推送）。

依赖不满足（没有 PySide6 / pawui）时整文件跳过。
"""

from __future__ import annotations

import importlib.util
import json

import pytest


def _has(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


MISSING = [name for name in ("PySide6", "pawui") if not _has(name)]

pytestmark = pytest.mark.skipif(bool(MISSING), reason=f"缺少依赖: {MISSING}")


@pytest.fixture(scope="module")
def web_app(tmp_path_factory):
    """建一个真实应用（含 PawUI 运行时），供本文件的测试共用。

    **刻意不依赖 conftest 的 ``qapp`` / ``project``**：那两个是 function 级的，
    而 WebEngine 启动一次要好几秒，本 fixture 必须是 module 级 ——
    混用会直接 ``ScopeMismatch``，表现为**所有用例 setup 阶段 ERROR**（踩过）。
    所以这里自己建 QApplication 与临时项目。
    """
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])

    root = tmp_path_factory.mktemp("webproj")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "class Service:\n    def run(self):\n        return 1\n", encoding="utf-8")

    import main as tcide_main

    runtime, app = tcide_main.create_runtime(root)
    yield app, runtime


def _wait(qapp, predicate, *, rounds: int = 120, step_ms: int = 50) -> bool:
    """把事件循环转起来等条件成立（WebEngine 是异步的，必须真等）。"""
    from PySide6.QtCore import QThread

    for _ in range(rounds):
        qapp.processEvents()
        if predicate():
            return True
        QThread.msleep(step_ms)
        qapp.processEvents()
    return predicate()


class TestLazyCreation:
    def test_view_is_not_created_until_asked(self, web_app):
        """懒创建：不显式打开就不该有 WebEngine 视图（否则又会抢占事件循环）。"""
        app, _runtime = web_app
        assert app._web_view() is None
        assert app._web_bridge is None

    def test_command_creates_and_shows_the_window(self, web_app):
        app, _runtime = web_app
        assert app.cmd_open_web_editor() is True
        view = app._web_view()
        assert view is not None
        assert view.isVisible()
        assert "Web 编辑器" in view.windowTitle()

    def test_second_call_reuses_the_same_view(self, web_app):
        app, _runtime = web_app
        first = app._web_view()
        app.cmd_open_web_editor()
        assert app._web_view() is first


class TestBridgeRoundTrip:
    def test_page_uses_the_monaco_engine(self, web_app, qapp):
        """页面必须真的跑在 Monaco 上，而不是**静默退回**内置编辑器。

        为什么单独钉这条：其它测试用的是 ``window.tcideValue()``，而内置编辑器
        也实现了它 —— 回退了照样全绿。只有断言引擎名才能区分
        「Monaco 可用」与「悄悄退回了后备实现」。

        **等待窗口必须够宽**：离屏冷启动时 Monaco 要从本地读 2.4MB 主文件 +
        分块并初始化整个编辑器，实测需要 20 秒以上。第一版只等 ~15 秒，
        于是误判成"起不来"（tools/diagnose_monaco.py 的自诊断显示
        ``started`` 有值、``done`` 为 0，再等 20 秒就变成 ``monaco``）。
        """
        from PySide6.QtCore import QThread

        app, _runtime = web_app
        app.cmd_open_web_editor()
        view = app._web_view()
        box: dict = {}

        def got(value=None) -> None:
            box["engine"] = value

        for _ in range(180):                     # 最多 ~90 秒
            view.page().runJavaScript("window.tcideEngine && window.tcideEngine()",
                                      got)
            for _ in range(10):
                qapp.processEvents()
                if box.get("engine") == "monaco":
                    break
                QThread.msleep(50)
            if box.get("engine") == "monaco":
                break
        assert box.get("engine") == "monaco", (
            f"编辑器引擎是 {box.get('engine')!r}（应为 monaco）—— "
            f"检查 vendor/monaco 是否随包分发，或用 tools/diagnose_monaco.py 诊断")

    def test_split_opens_a_second_editor(self, web_app, qapp):
        """分屏：把下一个标签推到第二个 Monaco 实例里，并能读到它的内容。

        （Monaco 初始化要 20 秒以上，所以先等引擎就绪 —— 见上面那条测试的说明。）
        """
        from PySide6.QtCore import QThread

        app, _runtime = web_app
        app.cmd_open_web_editor()
        view = app._web_view()

        def ask(expr: str, key: str, rounds: int = 40) -> object:
            box: dict = {}

            def got(value=None) -> None:
                box[key] = value

            for _ in range(rounds):
                view.page().runJavaScript(expr, got)
                for _ in range(10):
                    qapp.processEvents()
                    if key in box:
                        return box[key]
                    QThread.msleep(25)
            return None

        for _ in range(180):
            if ask("window.tcideEngine && window.tcideEngine()", "e") == "monaco":
                break
            qapp.processEvents()
            QThread.msleep(50)

        app.open_file("src/app.py")
        # 再开一个文件，让"下一个标签"不是同一个
        (app.state.root / "second.py").write_text("second = 42\n", encoding="utf-8")
        app.open_file("second.py")
        app.open_file("src/app.py")           # 当前是 app.py，下一个是 second.py
        assert app.cmd_split_open() is True

        assert ask("window.tcideSplitOpen && window.tcideSplitOpen()", "open") is True
        value = ask("window.tcideSplitValue && window.tcideSplitValue()", "v")
        assert isinstance(value, str) and value.strip(), f"分屏里没有内容：{value!r}"
        assert "second" in value, f"分屏内容不是下一个标签：{value[:60]!r}"

        app.cmd_split_close()
        assert ask("window.tcideSplitOpen && window.tcideSplitOpen()", "open2") is False

    def test_split_message_roundtrip_encoding(self):
        """协议层：分屏消息的字段必须齐全（纯逻辑）。"""
        from tcide.web.bridge import OUTBOUND, encode_split, encode_split_close

        message = encode_split("a.py", "x = 1\n", language="python")
        assert message["type"] == "split" and message["path"] == "a.py"
        assert message["language"] == "python"
        assert encode_split_close()["type"] == "splitClose"
        assert {"split", "splitClose"} <= set(OUTBOUND)

    def test_page_connects_and_reports_ready(self, web_app, qapp):
        app, _runtime = web_app
        app.cmd_open_web_editor()
        assert _wait(qapp, lambda: app._web_ready), \
            f"页面没有连上桥：{app._web_editor_line}"

    def test_text_reaches_the_page(self, web_app, qapp):
        """应用状态里的文本必须真的出现在页面的 textarea 里。"""
        from PySide6.QtCore import QThread

        app, _runtime = web_app
        app.open_file("src/app.py")
        app.push()
        view = app._web_view()
        box: dict[str, object] = {}

        def got(value=None) -> None:
            box["value"] = value

        # runJavaScript 是**异步**的：必须给渲染进程真实时间，只 processEvents
        # 连续发问是拿不到回调的（第一版就是这么写的，读回 None）。
        for _ in range(30):
            view.page().runJavaScript("window.tcideValue()", got)
            for _ in range(10):
                qapp.processEvents()
                if isinstance(box.get("value"), str):
                    break
                QThread.msleep(20)
            if isinstance(box.get("value"), str):
                break

        value = box.get("value")
        assert isinstance(value, str), f"没能从页面读回文本（{value!r}）"
        assert "Service" in value, f"页面里的内容和当前文件对不上：{value[:80]!r}"

    def test_page_changes_flow_back_into_state(self, web_app, qapp):
        """页面发 change → 应用状态更新 + 标脏（这是"能编辑"的硬证据）。"""
        app, _runtime = web_app
        before = app.state.editor_text
        app._on_editor_event(
            type("E", (), {"kind": "change", "text": before + "\n# 来自页面\n",
                           "message": "", "line": 0, "column": 0, "name": "",
                           "raw": {}})())
        assert app.state.editor_text.endswith("# 来自页面\n")
        assert app.state.dirty is True

    def test_change_does_not_push_text_back(self, web_app):
        """页面发来的改动不该再推回页面（避免回环）。"""
        app, _runtime = web_app
        sent: list[str] = []
        app._web_bridge.deliver.connect(sent.append)
        app._on_editor_event(
            type("E", (), {"kind": "change", "text": "x = 1\n", "message": "",
                           "line": 0, "column": 0, "name": "", "raw": {}})())
        texts = [json.loads(item) for item in sent]
        assert not [m for m in texts if m.get("type") == "text"], \
            "改动被推回了页面，存在回环风险"

    def test_command_event_dispatches(self, web_app):
        """页面按 Ctrl+S → 应该走保存逻辑（这里只验证分派不炸、状态有反应）。"""
        app, _runtime = web_app
        app.state.dirty = True
        app._on_editor_event(
            type("E", (), {"kind": "command", "name": "save", "text": "",
                           "message": "", "line": 0, "column": 0, "raw": {}})())
        assert app.state.dirty is False

    def test_error_event_is_surfaced(self, web_app):
        """页面报错必须出现在状态栏，不能吞掉。"""
        app, _runtime = web_app
        app._on_editor_event(
            type("E", (), {"kind": "error", "message": "坏消息", "text": "",
                           "line": 0, "column": 0, "name": "", "raw": {}})())
        assert "坏消息" in app.state.status_left


class TestPaletteEntry:
    def test_web_editor_is_in_the_palette(self, web_app):
        app, _runtime = web_app
        app.build_palette()
        assert any("Web 编辑器" in row for row in app.state.palette_rows)

    def test_palette_command_dispatches_to_the_editor(self, web_app):
        app, _runtime = web_app
        app.run_palette_command("打开 Web 编辑器（独立窗口）")
        assert app._web_view() is not None
        assert "已执行" in app.state.status_left
