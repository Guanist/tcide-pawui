# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""panels.py 的测试：行生成 + 反解必须成对成立（格式漂移就红）。"""

from __future__ import annotations

from tcide.completion import Completer
from tcide.indexer import ProjectIndex
from tcide.lint import Linter
from tcide.panels import (
    IMAGE_EXTS,
    LINE_WIDTH,
    PanelRow,
    completion_text,
    line_offset,
    outline_rows,
    parse_leading_line,
    preview_caption,
    preview_hex,
    preview_image_path,
    preview_kind,
    preview_text,
    problem_rows,
    row_for_label,
    rows_for_list,
    search_rows,
)


class TestParseLeadingLine:
    def test_plain(self):
        assert parse_leading_line("   12: hello") == 12

    def test_with_marker(self):
        assert parse_leading_line("    3: ƒ Service") == 3

    def test_without_colon(self):
        assert parse_leading_line("7 something") == 7

    def test_with_pipe(self):
        assert parse_leading_line("42 | code") == 42

    def test_garbage_is_zero(self):
        for label in ("", "没有行号", "(未打开文件)", None):
            assert parse_leading_line(label) == 0

    def test_roundtrip_with_display(self):
        """生成 <-> 反解必须闭合（这是最容易漂移的地方）。"""
        for line in (1, 9, 10, 9999):
            row = PanelRow(line=line, text="x")
            assert parse_leading_line(row.display()) == line

    def test_line_width_used(self):
        row = PanelRow(line=7, text="x")
        assert row.display().startswith(f"{7:>{LINE_WIDTH}}")


class TestLineOffset:
    def test_first_line(self):
        assert line_offset("abc\ndef\n", 1) == 0

    def test_second_line(self):
        assert line_offset("abc\ndef\n", 2) == 4

    def test_third_line(self):
        assert line_offset("abc\ndef\nghi\n", 3) == 8

    def test_clamps_beyond_end(self):
        assert line_offset("abc\n", 99) == 4

    def test_zero_or_negative(self):
        assert line_offset("abc\n", 0) == 0
        assert line_offset("abc\n", -5) == 0

    def test_empty_text(self):
        assert line_offset("", 3) == 0

    def test_without_trailing_newline(self):
        assert line_offset("abc\ndef", 2) == 4

    def test_crlf_counts_two_chars(self):
        assert line_offset("abc\r\ndef\r\n", 2) == 5


class TestOutlineRows:
    def test_python_outline(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        rows = outline_rows(index, "src/app.py")
        assert rows, "没有大纲行"
        assert any(row.symbol == "Service" for row in rows)
        assert all(row.path == "src/app.py" for row in rows)
        assert all(row.line > 0 for row in rows)

    def test_regex_inferred_is_marked(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        rows = outline_rows(index, "web/main.ts")
        assert any("正则推断" in row.text for row in rows)

    def test_limit_respected(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        assert len(outline_rows(index, "src/app.py", limit=1)) == 1

    def test_missing_file(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        assert outline_rows(index, "没有这个文件.py") == []

    def test_all_rows_reversible(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        for row in outline_rows(index, "src/app.py"):
            assert parse_leading_line(row.display()) == row.line


class TestSearchRows:
    def test_finds_hits(self, workspace):
        rows = search_rows(workspace, "def helper")
        assert rows and rows[0].path == "src/app.py"
        assert parse_leading_line(rows[0].display()) > 0

    def test_empty_query(self, workspace):
        assert search_rows(workspace, "   ") == []

    def test_no_match(self, workspace):
        assert search_rows(workspace, "zzz不存在") == []

    def test_limit(self, workspace):
        assert len(search_rows(workspace, "import", limit=1)) <= 1

    def test_long_line_truncated(self, workspace):
        workspace.write("long.py", "x = '" + "y" * 300 + "'\n")
        rows = search_rows(workspace, "yyy")
        assert all(len(row.text) < 200 for row in rows)


class TestProblemRows:
    def test_badges_by_severity(self, workspace):
        issues = Linter(workspace).lint_all()
        rows = problem_rows(issues)
        assert rows
        assert any("✖" in row.text for row in rows)      # 有语法错误
        assert all(row.extra.get("severity") in ("error", "warning", "info")
                   for row in rows)

    def test_accepts_both_list_and_dict(self, workspace):
        """lint_all() 返回 dict，summary()/report() 也接受两种 —— 这里必须一致。

        第一版按 list 写，拿 dict 切片直接 `KeyError: slice(...)`。
        """
        linter = Linter(workspace)
        as_dict = linter.lint_all()
        as_list = [issue for group in as_dict.values() for issue in group]
        assert len(problem_rows(as_dict)) == len(problem_rows(as_list))

    def test_limit_and_empty(self):
        assert problem_rows([], limit=5) == []
        assert problem_rows({}) == []
        assert problem_rows(None) == []

    def test_reversible(self, workspace):
        rows = problem_rows(Linter(workspace).lint_all())
        for row in rows:
            assert parse_leading_line(row.display()) == row.line


class TestListHelpers:
    def test_rows_for_list(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        rows = outline_rows(index, "src/app.py")
        labels = rows_for_list(rows)
        assert len(labels) == len(rows)
        assert all(isinstance(label, str) for label in labels)

    def test_row_for_label_roundtrip(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        rows = outline_rows(index, "src/app.py")
        for row in rows:
            found = row_for_label(rows, row.display())
            assert found is not None and found.line == row.line

    def test_row_for_label_missing(self):
        assert row_for_label([], "什么都不是") is None

    def test_completion_text(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        completer = Completer(index, workspace=workspace)
        text = completion_text(completer, "hel", "src/util.py")
        assert text.startswith("建议：") and "helper" in text

    def test_completion_text_empty(self, workspace):
        completer = Completer(None, workspace=workspace)
        assert completion_text(completer, "  ", "src/app.py") == ""

    def test_panel_row_to_dict(self):
        data = PanelRow(line=3, text="t", path="p", symbol="s").to_dict()
        assert data["line"] == 3 and data["symbol"] == "s"


class TestPreviewKind:
    """预览方式判定。图片必须**同时**过扩展名与魔数两关。"""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    def test_missing_and_empty(self, workspace):
        assert preview_kind(workspace, "") == "none"
        assert preview_kind(workspace, "不存在.png") == "none"
        workspace.write("empty.txt", "")
        assert preview_kind(workspace, "empty.txt") == "empty"

    def test_real_png_is_image(self, workspace):
        workspace.write_bytes("real.png", self.PNG)
        assert preview_kind(workspace, "real.png") == "image"

    def test_text_file_is_text(self, workspace):
        assert preview_kind(workspace, "src/app.py") == "text"
        assert preview_kind(workspace, "README.md") == "text"

    def test_png_extension_with_text_content_falls_back_to_hex(self, workspace):
        """**关键防线**：`<Image src>` 加载失败会抛 RenderError 把界面打崩，
        所以"名字叫 .png 其实是文本"的文件必须退回 hex 预览。"""
        workspace.write("fake.png", "这其实是一段文本\n")
        assert preview_kind(workspace, "fake.png") == "hex"

    def test_null_bytes_mean_hex(self, workspace):
        workspace.write_bytes("blob.dat", b"\x00\x01\x02abc")
        assert preview_kind(workspace, "blob.dat") == "hex"

    def test_svg_is_image_by_content(self, workspace):
        workspace.write("icon.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')
        assert preview_kind(workspace, "icon.svg") == "image"

    def test_svg_extension_without_svg_content_is_text(self, workspace):
        workspace.write("notsvg.svg", "就是一段文字\n")
        assert preview_kind(workspace, "notsvg.svg") == "text"

    def test_sniff_is_injectable(self, workspace):
        """嗅探函数可注入，方便断言"判定依据是魔数而不是文件名"。"""
        workspace.write_bytes("x.bin", self.PNG)
        assert preview_kind(workspace, "x.bin", sniff=lambda _h: "PNG 图片") == "hex"

    def test_fixture_logo_is_image(self, workspace):
        is_png = workspace.read_bytes("assets/logo.png", max_bytes=8).startswith(
            b"\x89PNG")
        expected = "image" if is_png else "hex"
        assert preview_kind(workspace, "assets/logo.png") == expected

    def test_image_exts_table(self):
        assert {".png", ".jpg", ".svg", ".gif"} <= IMAGE_EXTS


class TestPreviewContent:
    def test_text_content(self, workspace):
        text = preview_text(workspace, "src/app.py")
        assert "Service" in text

    def test_text_truncation(self, workspace):
        workspace.write("long.txt", "\n".join(f"行{i}" for i in range(500)))
        text = preview_text(workspace, "long.txt", max_lines=10)
        assert "已截断" in text and text.count("\n") < 20

    def test_text_missing_file(self, workspace):
        assert preview_text(workspace, "没有.txt") == ""

    def test_hex_content(self, workspace):
        workspace.write_bytes("blob.bin", bytes(range(256)))
        text = preview_hex(workspace, "blob.bin", rows=2)
        assert "00000000" in text and "|" in text

    def test_hex_missing_file(self, workspace):
        assert preview_hex(workspace, "没有.bin") == ""

    def test_image_path_is_absolute_and_only_for_images(self, workspace):
        workspace.write_bytes("real.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        path = preview_image_path(workspace, "real.png")
        assert path and path.endswith("real.png")
        assert ":" in path or path.startswith("/")      # 绝对路径
        assert preview_image_path(workspace, "src/app.py") == ""
        assert preview_image_path(workspace, "fake.png") == ""

    def test_caption(self, workspace):
        workspace.write_bytes("real.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        assert "图片" in preview_caption(workspace, "real.png")
        assert "文本" in preview_caption(workspace, "src/app.py")
        assert "选择一个文件" in preview_caption(workspace, "")

    def test_caption_size_units(self, workspace):
        workspace.write("small.txt", "abc")
        assert "字节" in preview_caption(workspace, "small.txt")
        workspace.write("big.txt", "x" * 3000)
        assert "KB" in preview_caption(workspace, "big.txt")

    def test_caption_accepts_explicit_kind(self, workspace):
        assert "二进制" in preview_caption(workspace, "src/app.py", kind="hex")
