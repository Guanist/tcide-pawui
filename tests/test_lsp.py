# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""LSP 的测试：分帧/URI/转换（纯逻辑）+ 用**假语言服务器**做端到端。

假服务器是一个真说 stdio JSON-RPC 的 Python 脚本（本文件生成），
所以它验证的是**协议本身**，而不是我自己的桩函数。

注意：本文件**不得**用 PowerShell 的 Set-Content 改（会把中文写成乱码）——
见 FEATURE_MATRIX 的第 19/30 条，以及 test_layering 里的全仓编码门禁。
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import textwrap
import uuid

import pytest

from tcide.lint import Issue
from tcide.lsp import (
    LspClient,
    LspStatus,
    SEVERITY_MAP,
    available_servers,
    completion_labels,
    detect_servers,
    diagnostics_to_issues,
    encode_message,
    path_to_uri,
    project_language,
    read_message,
    status_report,
    uri_to_path,
)

#: 一个最小但**真的** LSP 服务器：读 Content-Length 分帧、按 id 回响应，
#: 并在 didOpen 时推一条 publishDiagnostics。
FAKE_SERVER = textwrap.dedent('''
    import json, sys

    def read_message(stream):
        length = 0
        while True:
            line = stream.readline()
            if not line:
                return None
            if line in (b"\\r\\n", b"\\n"):
                break
            text = line.decode("ascii", errors="replace").strip().lower()
            if text.startswith("content-length:"):
                length = int(text.split(":", 1)[1].strip())
        body = stream.read(length)
        return json.loads(body.decode("utf-8")) if body else None

    def send(payload):
        body = json.dumps(payload).encode("utf-8")
        sys.stdout.buffer.write(
            f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii") + body)
        sys.stdout.buffer.flush()

    while True:
        message = read_message(sys.stdin.buffer)
        if message is None:
            break
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": message["id"],
                  "result": {"capabilities": {"completionProvider": {}}}})
        elif method == "textDocument/completion":
            send({"jsonrpc": "2.0", "id": message["id"],
                  "result": {"items": [{"label": "alpha"}, {"label": "beta"}]}})
        elif method == "textDocument/hover":
            send({"jsonrpc": "2.0", "id": message["id"],
                  "result": {"contents": {"kind": "markdown", "value": "**hi**"}}})
        elif method == "textDocument/didOpen":
            uri = message["params"]["textDocument"]["uri"]
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "diagnostics": [
                      {"range": {"start": {"line": 4, "character": 2},
                                 "end": {"line": 4, "character": 9}},
                       "severity": 1, "code": "E501", "source": "fake",
                       "message": "示例问题"}]}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": message["id"], "result": None})
        # 刻意**不**给未知方法兜底回包 —— 这样"超时"测的才是真的超时路径
''')


def _scratch(prefix: str) -> pathlib.Path:
    """工作区内的唯一临时目录。

    刻意不用 ``tmp_path`` / ``tmp_path_factory``：本机沙箱下系统临时目录
    （以及复用同一个 pytest 临时根时）会因上一轮升级运行留下的目录而
    ``PermissionError``，表现为 fixture 在 setup 阶段就报错、一条断言都看不到。
    """
    base = (pathlib.Path(__file__).resolve().parent / ".scratch"
            / f"{prefix}-{uuid.uuid4().hex[:8]}")
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture(scope="module")
def fake_server():
    path = _scratch("lsp") / "fake_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


@pytest.fixture
def scratch_dir():
    return _scratch("lspc")


@pytest.fixture
def fake_status(fake_server):
    return LspStatus(language="python", command=sys.executable,
                     args=[str(fake_server)], available=True)


class TestFraming:
    def test_encode_has_content_length(self):
        data = encode_message({"jsonrpc": "2.0", "id": 1, "method": "x"})
        head, _, body = data.partition(b"\r\n\r\n")
        assert head.startswith(b"Content-Length: ")
        assert int(head.split(b":")[1]) == len(body)

    def test_length_counts_bytes_not_chars(self):
        """中文按 UTF-8 是多字节，长度必须按字节算（否则对端读不齐）。"""
        data = encode_message({"text": "中文"})
        head, _, body = data.partition(b"\r\n\r\n")
        assert int(head.split(b":")[1]) == len(body)
        assert json.loads(body.decode("utf-8"))["text"] == "中文"

    def test_roundtrip(self):
        payload = {"jsonrpc": "2.0", "id": 7, "result": {"k": "值"}}
        stream = io.BytesIO(encode_message(payload))
        assert read_message(stream) == payload

    def test_read_respects_extra_headers(self):
        body = b'{"a":1}'
        raw = (b"Content-Length: 7\r\nContent-Type: application/vscode-jsonrpc;"
               b" charset=utf-8\r\n\r\n" + body)
        assert read_message(io.BytesIO(raw)) == {"a": 1}

    def test_read_lf_only_separator(self):
        body = b'{"a":2}'
        assert read_message(io.BytesIO(b"Content-Length: 7\n\n" + body)) == {"a": 2}

    def test_read_eof(self):
        assert read_message(io.BytesIO(b"")) is None

    def test_read_bad_length(self):
        assert read_message(io.BytesIO(b"Content-Length: abc\r\n\r\n")) is None

    def test_read_absurd_length_rejected(self):
        raw = b"Content-Length: 999999999999\r\n\r\nx"
        assert read_message(io.BytesIO(raw)) is None

    def test_read_bad_json(self):
        assert read_message(io.BytesIO(b"Content-Length: 5\r\n\r\n{bad}")) is None


class TestUris:
    def test_posix_path_build(self):
        assert path_to_uri("/home/u/a.py") == "file:///home/u/a.py"

    def test_posix_path_keeps_root_slash(self):
        """POSIX 路径的根斜杠必须保留（第一版把它吃掉了）。"""
        assert uri_to_path("file:///home/u/a.py") == "/home/u/a.py"

    def test_posix_roundtrip(self):
        assert uri_to_path(path_to_uri("/home/u/a.py")) == "/home/u/a.py"

    def test_windows_drive(self):
        uri = path_to_uri(r"C:\work\a.py")
        assert uri == "file:///C:/work/a.py"
        assert uri_to_path(uri) == "C:/work/a.py"

    def test_windows_lowercase_drive(self):
        assert uri_to_path("file:///c:/x/y.py") == "c:/x/y.py"

    def test_percent_escapes(self):
        assert uri_to_path("file:///C:/my%20dir/a.py") == "C:/my dir/a.py"

    def test_roundtrip_windows(self):
        original = "C:/work/proj/a.py"
        assert uri_to_path(path_to_uri(original)) == original

    def test_empty(self):
        assert uri_to_path("") == ""


class TestConversions:
    def test_completion_labels_items(self):
        assert completion_labels({"items": [{"label": "a"}, {"label": "b"}]}) == [
            "a", "b"]

    def test_completion_labels_list(self):
        assert completion_labels([{"label": "a"}, "b"]) == ["a", "b"]

    def test_completion_labels_empty(self):
        assert completion_labels(None) == []
        assert completion_labels({"items": []}) == []
        assert completion_labels([{"nope": 1}]) == []

    def test_diagnostics_to_issues(self):
        diagnostics = [{
            "range": {"start": {"line": 4, "character": 2},
                      "end": {"line": 4, "character": 9}},
            "severity": 1, "code": "E501", "source": "pyright",
            "message": "行太长"}]
        issues = diagnostics_to_issues("file:///C:/proj/a.py", diagnostics,
                                       root="C:/proj")
        assert len(issues) == 1
        issue = issues[0]
        assert isinstance(issue, Issue)
        assert issue.path == "a.py"                   # 相对 root
        assert (issue.line, issue.column) == (5, 3)   # LSP 是 0 基 -> 转 1 基
        assert issue.severity == "error"
        assert issue.rule == "lsp:pyright:E501"
        assert issue.message == "行太长"

    def test_diagnostics_severity_mapping(self):
        for lsp_severity, expected in ((1, "error"), (2, "warning"), (3, "info"),
                                       (4, "info")):
            issues = diagnostics_to_issues("file:///a.py", [{
                "range": {"start": {"line": 0, "character": 0}},
                "severity": lsp_severity, "message": "m"}])
            assert issues[0].severity == expected
        assert set(SEVERITY_MAP) == {1, 2, 3, 4}

    def test_diagnostics_without_source_or_code(self):
        issues = diagnostics_to_issues("file:///a.py", [{
            "range": {"start": {"line": 0, "character": 0}}, "message": "m"}])
        assert issues[0].rule == "lsp:unknown"

    def test_diagnostics_garbage_is_ignored(self):
        assert diagnostics_to_issues("file:///a.py", [None, 3, "x"]) == []

    def test_diagnostics_outside_root_keeps_absolute(self):
        issues = diagnostics_to_issues("file:///D:/other/a.py", [
            {"range": {"start": {"line": 0, "character": 0}}, "message": "m"}],
            root="C:/proj")
        assert issues[0].path == "D:/other/a.py"


class TestDetection:
    def test_detect_reports_missing_with_hint(self):
        statuses = detect_servers(["python"], which=lambda _c: None)
        assert statuses[0].available is False
        assert statuses[0].hint            # 必须告诉用户怎么装
        assert "pyright-langserver" in statuses[0].command

    def test_detect_reports_available(self):
        statuses = detect_servers(["python"], which=lambda cmd: cmd == "pylsp")
        assert statuses[0].available is True
        assert statuses[0].command == "pylsp"

    def test_available_servers_filters(self):
        ready = available_servers(["python", "go"], which=lambda cmd: cmd == "gopls")
        assert [status.language for status in ready] == ["go"]

    def test_unknown_language(self):
        statuses = detect_servers(["brainfuck"], which=lambda _c: "/usr/bin/x")
        assert statuses[0].available is False

    @staticmethod
    def _strip_markers(workspace) -> None:
        """删掉**所有**标记文件。

        夹具项目里同时存在 pyproject.toml / requirements.txt / package.json ——
        只删 python 那几个，下一个命中的是 package.json（javascript），
        于是"应该识别成 go"的断言永远失败（我第一版就是这么写的）。
        """
        for marker in ("pyproject.toml", "setup.py", "requirements.txt",
                       "tsconfig.json", "package.json", "go.mod", "Cargo.toml"):
            if workspace.exists(marker):
                workspace.delete(marker)

    def test_project_language_from_marker(self, workspace):
        """标记文件按 MARKERS 的顺序定优先级（顺序即优先级）。"""
        workspace.write("go.mod", "module x\n")
        # 夹具里已经有 pyproject.toml（python 的标记），而 python 在表里更靠前
        assert project_language(workspace) == "python"

    def test_project_language_go_when_others_removed(self, workspace):
        self._strip_markers(workspace)
        workspace.write("go.mod", "module x\n")
        assert project_language(workspace) == "go"

    def test_project_language_prefers_memory(self, workspace, memory):
        assert project_language(workspace, memory) == memory.primary_language

    def test_project_language_unknown_without_markers(self, workspace):
        self._strip_markers(workspace)
        workspace.write("nothing-here.txt", "x\n")
        assert project_language(workspace) == ""

    def test_status_report(self):
        report = status_report(["python", "go"], which=lambda _c: None)
        assert "✗" in report and "可用 0 / 2" in report

    def test_status_describe_and_dict(self):
        status = LspStatus(language="go", command="gopls", available=True)
        assert "✓" in status.describe()
        assert status.to_dict()["available"] is True


class TestClientEndToEnd:
    def test_initialize_and_completion(self, fake_status, scratch_dir):
        client = LspClient(fake_status, scratch_dir, timeout=15)
        try:
            assert client.start() is True, client.error
            assert client.running is True
            labels = client.completion("C:/proj/a.py", 0, 0)
            assert labels == ["alpha", "beta"]
        finally:
            client.stop()
        assert client.running is False

    def test_hover(self, fake_status, scratch_dir):
        client = LspClient(fake_status, scratch_dir, timeout=15)
        try:
            assert client.start()
            assert client.hover("C:/proj/a.py", 1, 1) == "**hi**"
        finally:
            client.stop()

    def test_diagnostics_are_collected(self, fake_status, scratch_dir):
        import time

        client = LspClient(fake_status, scratch_dir, timeout=15)
        try:
            assert client.start()
            assert client.open_file("C:/proj/a.py", "x = 1\n", language="python")
            for _ in range(50):
                if client.diagnostics:
                    break
                time.sleep(0.05)
            assert client.diagnostics, "没有收到 publishDiagnostics"
            issues = client.issues(root="C:/proj")
            assert issues and issues[0].line == 5
            assert issues[0].rule == "lsp:fake:E501"
        finally:
            client.stop()

    def test_change_file_notifies(self, fake_status, scratch_dir):
        client = LspClient(fake_status, scratch_dir, timeout=15)
        try:
            assert client.start()
            assert client.change_file("C:/proj/a.py", "y = 2\n")
            assert client.change_file("C:/proj/a.py", "y = 3\n")
        finally:
            client.stop()

    def test_start_without_server_returns_false(self, scratch_dir):
        status = LspStatus(language="python", command="不存在的东西",
                           available=False)
        client = LspClient(status, scratch_dir, timeout=2)
        assert client.start() is False
        assert "没有可用" in client.error

    def test_start_failure_is_reported_not_raised(self, scratch_dir):
        status = LspStatus(language="python", command="不存在的命令",
                           available=True)
        client = LspClient(status, scratch_dir, timeout=2)
        assert client.start() is False
        assert client.error          # 有原因，不抛异常

    def test_request_timeout_returns_none(self, fake_server, scratch_dir):
        """服务器不认识的方法不会回包 —— 必须超时返回而不是卡死。"""
        status = LspStatus(language="python", command=sys.executable,
                           args=[str(fake_server)], available=True)
        client = LspClient(status, scratch_dir, timeout=1)
        try:
            assert client.start()
            assert client.request("textDocument/不存在的请求", {}) is None
            assert "超时" in client.error
        finally:
            client.stop()

    def test_to_dict(self, fake_status, scratch_dir):
        client = LspClient(fake_status, scratch_dir, timeout=15)
        try:
            assert client.start()
            data = client.to_dict()
            assert data["language"] == "python" and data["running"] is True
        finally:
            client.stop()

    def test_stop_is_idempotent(self, fake_status, scratch_dir):
        client = LspClient(fake_status, scratch_dir, timeout=15)
        client.start()
        client.stop()
        client.stop()
        assert client.running is False
