# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""``<Web>`` 编辑器桥的测试（纯协议部分，不需要 Qt / QtWebEngine）。

Qt 侧只做两件薄事：把 JSON 字符串发出去、把收到的字符串交给 ``decode_event``。
所以把协议测透等价于把桥测透 —— 而测试不需要真的起 QtWebEngine。
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from tcide.diff import FilePatch, Hunk, ReviewSession, unified_diff, parse_unified
from tcide.lint import Issue
from tcide.web.bridge import (
    EDITOR_HTML,
    INBOUND,
    MARKER_KINDS,
    OUTBOUND,
    decode_event,
    editor_html,
    editor_url,
    encode_completion,
    encode_goto,
    encode_markers,
    encode_readonly,
    encode_text,
    encode_theme,
    fix_position_markers,
    html_is_offline,
    markers_from_issues,
    markers_from_patch,
)

OLD = "def f():\n    return 1\n\n\nx = 1\n"
NEW = "def f():\n    return 2\n\n\nx = 1\ny = 2\n"


class TestOutboundEncoding:
    def test_text(self):
        message = encode_text("a.py", "x = 1\n", language="python")
        assert message["type"] == "text"
        assert message["path"] == "a.py" and message["text"] == "x = 1\n"
        assert message["language"] == "python"

    def test_text_is_json_serialisable(self):
        json.dumps(encode_text("a.py", "中文 {\"k\": 1}\n"))

    def test_theme_drops_non_hex(self):
        """前端不认识主题令牌名，所以非十六进制值要被丢掉。"""
        message = encode_theme({"bg": "#1d1d1f", "fg": "border", "x": 1})
        assert message["colors"] == {"bg": "#1d1d1f"}

    def test_goto_clamps(self):
        assert encode_goto(0)["line"] == 1
        assert encode_goto(-5, column=-3)["column"] == 0

    def test_completion(self):
        assert encode_completion(["foo", "bar"])["items"] == ["foo", "bar"]

    def test_readonly(self):
        assert encode_readonly(1)["value"] is True

    def test_markers_filters_unknown_kinds(self):
        message = encode_markers([{"line": 1, "kind": "added"},
                                  {"line": 2, "kind": "乱七八糟"}])
        assert [m["kind"] for m in message["markers"]] == ["added"]

    def test_all_outbound_types_declared(self):
        produced = {encode_text("p", "t")["type"], encode_theme({})["type"],
                    encode_goto(1)["type"], encode_completion([])["type"],
                    encode_readonly(True)["type"], encode_markers([])["type"]}
        assert produced <= set(OUTBOUND)


class TestDecodeEvent:
    def test_ready(self):
        event = decode_event(json.dumps({"type": "ready", "message": "qwebchannel"}))
        assert event.kind == "ready" and event.message == "qwebchannel"

    def test_change(self):
        event = decode_event({"type": "change", "text": "x = 1\n", "line": 1})
        assert event.kind == "change" and event.text == "x = 1\n"

    def test_cursor(self):
        event = decode_event(json.dumps({"type": "cursor", "line": 12,
                                        "column": 4}))
        assert (event.line, event.column) == (12, 4)

    def test_command(self):
        assert decode_event({"type": "command", "name": "save"}).name == "save"

    def test_bad_json_does_not_raise(self):
        event = decode_event("{不是 json")
        assert event.kind == "unknown" and event.message

    def test_bad_type_does_not_raise(self):
        assert decode_event(None).kind == "unknown"
        assert decode_event({"type": "恶意"}).kind == "unknown"
        assert decode_event(123).kind == "unknown"

    def test_bad_numbers_become_zero(self):
        event = decode_event({"type": "cursor", "line": "很多", "column": None})
        assert (event.line, event.column) == (0, 0)

    def test_negative_numbers_clamped(self):
        assert decode_event({"type": "cursor", "line": -3}).line == 0

    def test_raw_preserved(self):
        payload = {"type": "debug", "message": "hi", "extra": 1}
        assert decode_event(payload).raw == payload

    def test_to_dict(self):
        data = decode_event({"type": "ready"}).to_dict()
        assert set(data) >= {"kind", "text", "line", "column", "name", "message"}

    def test_every_inbound_type_parses(self):
        for kind in INBOUND:
            assert decode_event({"type": kind}).kind == kind


class TestMarkersFromPatch:
    def _patch(self, old: str, new: str) -> FilePatch:
        return parse_unified(unified_diff(old, new, path="f.py"))[0]

    def test_added_line_marked(self):
        patch = self._patch("a\nb\n", "a\nNEW\nb\n")
        markers = markers_from_patch(patch)
        assert [m["kind"] for m in markers] == ["added"]
        assert markers[0]["line"] == 2

    def test_removed_line_marked_near_its_position(self):
        patch = self._patch("a\nGONE\nb\n", "a\nb\n")
        markers = markers_from_patch(patch)
        assert [m["kind"] for m in markers] == ["removed"]

    def test_modified_line_becomes_changed(self):
        """同一行既删又增 → 合并成 changed，否则前端会画两个重叠标记。"""
        patch = self._patch(OLD, NEW)
        kinds = {m["kind"] for m in markers_from_patch(patch)}
        assert "changed" in kinds
        assert not ({"added", "removed"} <= kinds), "没有合并成 changed"

    def test_line_numbers_within_range(self):
        patch = self._patch(OLD, NEW)
        total = len(NEW.splitlines())
        for marker in markers_from_patch(patch):
            assert 1 <= marker["line"] <= total

    def test_all_kinds_are_declared(self):
        patch = self._patch(OLD, NEW)
        assert {m["kind"] for m in markers_from_patch(patch)} <= set(MARKER_KINDS)

    def test_empty_patch(self):
        assert markers_from_patch(FilePatch(path="x")) == []

    def test_multi_hunk_positions(self):
        old = "".join(f"line{i}\n" for i in range(40))
        lines = [f"line{i}\n" for i in range(40)]
        lines[2] = "CHANGED\n"
        lines[35] = "ALSO\n"
        patch = self._patch(old, "".join(lines))
        markers = markers_from_patch(patch)
        assert len(markers) >= 2
        assert max(m["line"] for m in markers) > 30

    def test_review_session_diff_markers(self, workspace):
        """真实链路：审核会话的 diff → 行标记。"""
        workspace.write("f.py", OLD)
        session = ReviewSession(workspace)
        proposal = session.propose("f.py", NEW)
        markers = markers_from_patch(proposal.patch)
        assert markers and all(m["kind"] in MARKER_KINDS for m in markers)


class TestMarkersFromIssues:
    def test_severity_becomes_kind(self):
        issues = [Issue("a.py", 1, "r", "m", severity="error"),
                  Issue("a.py", 2, "r", "m", severity="warning")]
        kinds = [m["kind"] for m in markers_from_issues(issues)]
        assert kinds == ["error", "warning"]

    def test_accepts_dict_shape(self):
        """lint_all() 返回 dict —— 两处 API 必须都接受（我在这里犯过一次）。"""
        data = {"a.py": [Issue("a.py", 3, "r", "m", severity="error")]}
        assert markers_from_issues(data) == markers_from_issues(data["a.py"])

    def test_line_clamped_to_at_least_one(self):
        assert markers_from_issues([Issue("a.py", 0, "r", "m")])[0]["line"] == 1

    def test_fix_position_markers_only_errors(self):
        issues = [Issue("a.py", 1, "r", "m", severity="error"),
                  Issue("a.py", 2, "r", "m", severity="info")]
        assert len(fix_position_markers(issues)) == 1

    def test_empty(self):
        assert markers_from_issues([]) == []
        assert markers_from_issues(None) == []


class TestEditorAsset:
    def test_html_exists(self):
        assert EDITOR_HTML.exists(), "缺少 editor.html"

    def test_html_is_offline(self):
        """硬要求：不引用任何外部网络资源（Monaco 走 CDN 就是不能接受的原因）。"""
        assert html_is_offline() is True

    def test_offline_check_catches_cdn(self):
        assert html_is_offline('<script src="https://cdn.jsdelivr.net/x.js">') is False
        assert html_is_offline('<script src="//cdn.example.com/x.js">') is False

    def test_html_has_required_elements(self):
        """JS 引用的 DOM 元素必须真的存在。

        页面结构变过一次：加了 Monaco 容器 ``#monaco``，内置后备编辑器的
        ``code``/``overlay`` 改名为 ``fbCode``/``fbOverlay`` —— 这条测试的作用
        就是在改名时抓住"JS 与 HTML 不一致"。
        """
        html = editor_html()
        for element in ('id="monaco"', 'id="fallback"', 'id="fbCode"',
                        'id="fbOverlay"', 'id="gutter"', 'id="path"',
                        'id="engine"', 'id="completion"'):
            assert element in html, f"editor.html 缺少 {element}"

    def test_html_loads_local_monaco(self):
        """Monaco 必须是**本地** vendored 的（离线硬要求）。"""
        html = editor_html()
        assert "vendor/monaco/loader.js" in html
        assert "vendor/monaco/editor/editor.main.css" in html
        assert "vendor/monaco" in html           # require.config 的 vs 路径

    def test_monaco_vendor_is_complete(self):
        """vendor 目录必须含 loader 与编辑器主文件（否则只能退回内置编辑器）。"""
        from tcide.web.bridge import WEB_DIR

        vendor = WEB_DIR / "vendor" / "monaco"
        for probe in ("loader.js", "editor/editor.main.js",
                      "editor/editor.main.css"):
            assert (vendor / probe).exists(), f"vendor 缺少 {probe}"

    def test_monaco_helpers_report_state(self):
        """引擎与体积要能被如实报告（打包说明与状态栏都用它）。"""
        from tcide.web.bridge import (engine_description, monaco_available,
                                      monaco_size_mb)

        assert monaco_available() is True, "本仓库应当已 vendored Monaco"
        assert monaco_size_mb() > 1.0
        assert "Monaco" in engine_description()

    def test_monaco_helpers_when_vendor_missing(self, monkeypatch):
        """vendor 不在场时必须**如实说没有**，而不是假装可用。

        （打包时用 TCIDE_NO_MONACO=1 排除它就是这条路；页面会自动退回内置编辑器。）
        """
        from tcide.web import bridge

        monkeypatch.setattr(bridge, "MONACO_ENTRY",
                            bridge.WEB_DIR / "vendor" / "不存在" / "loader.js")
        monkeypatch.setattr(bridge, "MONACO_DIR",
                            bridge.WEB_DIR / "vendor" / "不存在")
        assert bridge.monaco_available() is False
        assert bridge.monaco_size_mb() == 0.0
        assert "内置编辑器" in bridge.engine_description()

    def test_html_loads_qwebchannel_from_qt_resource(self):
        html = editor_html()
        assert "qrc:///qtwebchannel/qwebchannel.js" in html

    def test_html_uses_the_agreed_protocol(self):
        """前端发的 type 必须都在 INBOUND 里，否则 Python 侧一律判成 unknown。"""
        import re

        html = editor_html()
        sent = set(re.findall(r'post\(\{\s*type:\s*"(\w+)"', html))
        assert sent, "没有解析出前端发送的消息类型"
        assert sent <= set(INBOUND), f"前端发了未约定的类型: {sent - set(INBOUND)}"

    def test_html_handles_all_outbound_types(self):
        html = editor_html()
        for kind in ("text", "theme", "markers", "goto", "completion", "readonly"):
            assert f'"{kind}"' in html, f"前端没有处理 {kind}"

    def test_editor_url_is_a_file_uri(self):
        url = editor_url()
        assert url.startswith("file://")
        assert url.endswith("editor.html")


class TestQtBridgeIsOptional:
    def test_module_imports_without_qt(self):
        """纯协议部分不依赖 Qt：模块能导入（Qt 只在 make_bridge 里惰性导入）。"""
        assert importlib.util.find_spec("tcide.web.bridge") is not None

    def test_make_bridge_is_the_only_qt_entry(self):
        import ast
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent / "tcide" / "web"
                  / "bridge.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_level_qt: list[str] = []
        for node in tree.body:                      # 只看模块级语句
            if isinstance(node, ast.Import):
                module_level_qt += [a.name for a in node.names
                                    if a.name.startswith("PySide6")]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "PySide6"):
                module_level_qt.append(node.module or "")
        assert not module_level_qt, f"PySide6 被提到了模块级: {module_level_qt}"

    @pytest.mark.skipif(importlib.util.find_spec("PySide6") is None,
                        reason="需要 PySide6")
    def test_make_bridge_produces_signals_and_slots(self):
        from tcide.web.bridge import make_bridge

        events = []
        bridge = make_bridge(on_event=events.append)
        received = []
        bridge.deliver.connect(received.append)
        bridge.pushText("a.py", "x = 1\n", False)
        assert received, "没有发出消息"
        assert json.loads(received[0])["type"] == "text"
        bridge.post(json.dumps({"type": "change", "text": "y = 2\n"}))
        assert bridge.received and bridge.received[0].text == "y = 2\n"
        assert events and events[0].kind == "change"
