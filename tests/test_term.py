# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""term.py 与 terminal.html 的测试（纯逻辑，不需要 Qt / QtWebEngine）。"""

from __future__ import annotations

import json

import pytest

from tcide.term import (
    BUILTINS,
    HISTORY_LIMIT,
    LINE_KINDS,
    OUTPUT_LIMIT,
    TermLine,
    TerminalSession,
    decode_command,
    encode_clear,
    encode_output,
    encode_prompt,
    escape_html,
    format_prompt,
    strip_ansi,
    truncate,
)
from tcide.web.bridge import EDITOR_HTML, INBOUND, OUTBOUND, WEB_DIR, html_is_offline

TERMINAL_HTML = WEB_DIR / "terminal.html"


def fake_runner(result):
    """返回一个记录调用并按给定结果作答的执行器。"""
    calls: list[str] = []

    def runner(command):
        calls.append(command)
        return result

    runner.calls = calls          # type: ignore[attr-defined]
    return runner


class TestHelpers:
    def test_strip_ansi_colors(self):
        assert strip_ansi("\x1b[31m错误\x1b[0m") == "错误"

    def test_strip_ansi_osc(self):
        assert strip_ansi("a\x1b]0;标题\x07b") == "ab"

    def test_strip_ansi_plain_text_untouched(self):
        assert strip_ansi("普通输出 [31m") == "普通输出 [31m"

    def test_escape_html(self):
        assert escape_html("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"

    def test_escape_html_strips_ansi_first(self):
        assert escape_html("\x1b[32m<ok>\x1b[0m") == "&lt;ok&gt;"

    def test_truncate(self):
        assert truncate("abc", 10) == "abc"
        text = truncate("x" * 50, 10)
        assert text.startswith("x" * 10) and "截断" in text

    def test_format_prompt(self):
        assert format_prompt("") == "工作区> "
        assert format_prompt(r"C:\work\demo") == "demo> "
        assert format_prompt("/home/u/proj") == "proj> "


class TestTerminalSession:
    def test_empty_command_does_nothing(self):
        session = TerminalSession()
        assert session.run("") == []
        assert session.run("   ") == []
        assert session.history == []

    def test_runs_through_runner(self):
        runner = fake_runner((0, "hello\n", ""))
        session = TerminalSession(runner)
        lines = session.run("echo hello")
        assert runner.calls == ["echo hello"]
        kinds = [line.kind for line in lines]
        assert kinds[0] == "in" and "out" in kinds
        assert any("hello" in line.text for line in lines)

    def test_nonzero_exit_code_is_shown(self):
        session = TerminalSession(fake_runner((2, "", "出错了\n")))
        lines = session.run("bad")
        assert any(line.kind == "err" for line in lines)
        assert any("退出码 2" in line.text for line in lines)

    def test_no_output_mentions_exit_code(self):
        session = TerminalSession(fake_runner((0, "", "")))
        assert any("无输出" in line.text for line in session.run("true"))

    def test_runner_exception_does_not_escape(self):
        def boom(_command):
            raise RuntimeError("炸了")

        session = TerminalSession(boom)
        lines = session.run("x")
        assert any("RuntimeError" in line.text for line in lines)
        assert any(line.kind == "err" for line in lines)

    def test_without_runner(self):
        session = TerminalSession()
        lines = session.run("ls")
        assert any("没有配置执行器" in line.text for line in lines)

    def test_runner_returning_plain_string(self):
        session = TerminalSession(fake_runner("纯文本"))
        assert any("纯文本" in line.text for line in session.run("x"))

    def test_runner_returning_pair(self):
        session = TerminalSession(fake_runner(("out", "err")))
        lines = session.run("x")
        assert any(line.kind == "out" for line in lines)
        assert any(line.kind == "err" for line in lines)

    def test_ansi_is_stripped_from_output(self):
        session = TerminalSession(fake_runner((0, "\x1b[31m红\x1b[0m", "")))
        assert any(line.text == "红" for line in session.run("x"))

    def test_long_output_is_truncated(self):
        session = TerminalSession(fake_runner((0, "x" * (OUTPUT_LIMIT + 500), "")),
                                  output_limit=1000)
        lines = session.run("big")
        assert any("截断" in line.text for line in lines)
        assert all(len(line.text) < 2000 for line in lines)


class TestBuiltins:
    def test_help(self):
        session = TerminalSession()
        lines = session.run("help")
        assert lines[0].kind == "in"
        assert any("help" in line.text for line in lines[1:])

    def test_version(self):
        assert any("tcide" in line.text.lower()
                   for line in TerminalSession().run("version"))

    def test_history_lists_commands(self):
        session = TerminalSession(fake_runner((0, "", "")))
        session.run("first")
        session.run("second")
        lines = session.run("history")
        text = "\n".join(line.text for line in lines)
        assert "first" in text and "second" in text

    def test_history_includes_itself_like_a_shell(self):
        """shell 惯例：`history` 命令本身也会被记进历史（所以它列得出自己）。"""
        lines = TerminalSession().run("history")
        assert any("history" in line.text for line in lines)

    def test_history_empty_branch(self):
        """空历史分支直接测（否则被"history 自己入历史"挡住测不到）。"""
        lines = TerminalSession()._history_lines()
        assert len(lines) == 1 and "历史为空" in lines[0].text

    def test_builtin_output_is_kept_on_screen(self):
        """内建命令的输出必须进屏缓冲（第一版漏了，help 的内容会凭空消失）。"""
        session = TerminalSession()
        session.run("help")
        assert session.lines, "内建命令的输出没有进屏缓冲"
        assert "help" in session.text()

    def test_clear_empties_screen(self):
        session = TerminalSession(fake_runner((0, "x", "")))
        session.run("x")
        assert session.lines
        returned = session.run("clear")
        assert session.lines == []          # 模型为空
        assert session.text() == ""
        assert any("清屏" in line.text for line in returned)   # 但返回一行提示

    def test_builtin_table(self):
        assert "help" in BUILTINS and "version" in BUILTINS


class TestHistoryNavigation:
    def test_previous_walks_back(self):
        session = TerminalSession(fake_runner((0, "", "")))
        session.run("one")
        session.run("two")
        assert session.previous() == "two"
        assert session.previous() == "one"
        assert session.previous() == "one"       # 到顶就停住

    def test_next_walks_forward_to_empty(self):
        session = TerminalSession(fake_runner((0, "", "")))
        session.run("one")
        session.run("two")
        session.previous()
        session.previous()
        assert session.next() == "two"
        assert session.next() == ""
        assert session.next() == ""

    def test_empty_history(self):
        session = TerminalSession()
        assert session.previous() == ""
        assert session.next() == ""

    def test_duplicate_consecutive_not_repeated(self):
        session = TerminalSession(fake_runner((0, "", "")))
        session.run("same")
        session.run("same")
        assert session.history == ["same"]

    def test_non_consecutive_duplicate_kept(self):
        session = TerminalSession(fake_runner((0, "", "")))
        session.run("a")
        session.run("b")
        session.run("a")
        assert session.history == ["a", "b", "a"]

    def test_history_limit(self):
        session = TerminalSession(fake_runner((0, "", "")), history_limit=3)
        for index in range(5):
            session.run(f"cmd{index}")
        assert session.history == ["cmd2", "cmd3", "cmd4"]
        assert HISTORY_LIMIT >= 10

    def test_cursor_reset_after_run(self):
        session = TerminalSession(fake_runner((0, "", "")))
        session.run("a")
        session.previous()
        session.run("b")
        assert session.previous() == "b"


class TestProtocol:
    def test_encode_output(self):
        message = encode_output([TermLine("hi", "out")], echo="ls")
        assert message["type"] == "output" and message["echo"] == "ls"
        assert message["lines"] == [{"text": "hi", "kind": "out"}]

    def test_encode_output_filters_unknown_kinds(self):
        message = encode_output([TermLine("ok", "out"), TermLine("x", "乱七八糟")])
        assert [line["kind"] for line in message["lines"]] == ["out"]

    def test_encode_clear_and_prompt(self):
        assert encode_clear()["type"] == "clear"
        assert encode_prompt("demo> ")["prompt"] == "demo> "

    def test_decode_command(self):
        assert decode_command(json.dumps({"type": "command", "text": " ls "})) == "ls"
        assert decode_command({"type": "input", "command": "pwd"}) == "pwd"

    def test_decode_command_garbage(self):
        assert decode_command("{不是 json") == ""
        assert decode_command(None) == ""
        assert decode_command({"type": "change", "text": "x"}) == ""

    def test_line_kinds_are_declared_and_renderable(self):
        assert set(LINE_KINDS) == {"in", "out", "err", "info", "meta"}

    def test_to_dict(self):
        session = TerminalSession()
        session.run("help")
        data = session.to_dict()
        assert data["history"] == ["help"] and data["lines"]


class TestTerminalPage:
    def test_page_exists(self):
        assert TERMINAL_HTML.exists(), "缺少 terminal.html"

    def test_page_is_offline(self):
        """硬要求：不引用任何外部网络资源。"""
        assert html_is_offline(TERMINAL_HTML.read_text(encoding="utf-8")) is True

    def test_page_has_required_elements(self):
        text = TERMINAL_HTML.read_text(encoding="utf-8")
        for element in ('id="screen"', 'id="cmd"', 'id="prompt"', 'id="status"'):
            assert element in text, f"terminal.html 缺少 {element}"

    def test_page_loads_qwebchannel_from_qt_resource(self):
        assert "qrc:///qtwebchannel/qwebchannel.js" in TERMINAL_HTML.read_text(
            encoding="utf-8")

    def test_page_uses_the_agreed_protocol(self):
        import re

        text = TERMINAL_HTML.read_text(encoding="utf-8")
        sent = set(re.findall(r'post\(\{\s*type:\s*"(\w+)"', text))
        assert sent, "没有解析出前端发送的消息类型"
        assert sent <= set(INBOUND), f"前端发了未约定的类型: {sent - set(INBOUND)}"

    def test_page_handles_outbound_types(self):
        text = TERMINAL_HTML.read_text(encoding="utf-8")
        for kind in ("output", "clear", "prompt"):
            assert f'"{kind}"' in text, f"前端没有处理 {kind}"

    def test_bridge_object_name_matches(self):
        """前端找的是 terminalBridge —— 应用侧注册的必须是同一个名字。"""
        assert "channel.objects.terminalBridge" in TERMINAL_HTML.read_text(
            encoding="utf-8")

    def test_editor_page_still_present(self):
        assert EDITOR_HTML.exists()

    def test_outbound_protocol_covers_terminal_messages(self):
        """`output`/`clear`/`prompt` 必须在协议表里（否则前端发的类型会被判未知）。"""
        assert {"output", "clear", "prompt"} <= set(OUTBOUND)
