# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""打开文件标签模型的测试（纯逻辑，不需要 Qt）。"""

from __future__ import annotations

from tcide.tabs import (
    CLEAN_MARK,
    DEFAULT_LIMIT,
    DIRTY_MARK,
    OpenFile,
    OpenFiles,
    TabHistory,
    shortest_unique_names,
)


class TestShortestUniqueNames:
    def test_unique_basenames(self):
        names = shortest_unique_names(["src/app.py", "tests/test_app.py"])
        assert names == {"src/app.py": "app.py", "tests/test_app.py": "test_app.py"}

    def test_same_basename_uses_longer_suffix(self):
        """同名文件必须能区分（真实的 IDE 需求）。"""
        names = shortest_unique_names(["a/main.py", "b/main.py"])
        assert names["a/main.py"] != names["b/main.py"]
        assert names["a/main.py"].endswith("main.py")

    def test_deep_clash(self):
        names = shortest_unique_names(["x/a/util.py", "y/a/util.py", "z/b/util.py"])
        assert len(set(names.values())) == 3

    def test_backslashes_normalised(self):
        names = shortest_unique_names([r"a\b\c.py", r"d\e\c.py"])
        assert len(set(names.values())) == 2
        assert "\\" not in "".join(names.values())

    def test_empty(self):
        assert shortest_unique_names([]) == {}

    def test_single(self):
        assert shortest_unique_names(["only.py"]) == {"only.py": "only.py"}


class TestOpenFiles:
    def test_open_activates(self):
        files = OpenFiles()
        files.open("a.py")
        files.open("b.py")
        assert files.count() == 2
        assert files.active().path == "b.py"

    def test_open_same_file_twice_activates_existing(self):
        files = OpenFiles()
        files.open("a.py")
        files.open("b.py")
        files.open("a.py")
        assert files.count() == 2
        assert files.active().path == "a.py"

    def test_backslashes_are_normalised(self):
        files = OpenFiles()
        files.open(r"src\app.py")
        assert files.paths() == ["src/app.py"]

    def test_close_current(self):
        files = OpenFiles()
        files.open("a.py")
        files.open("b.py")
        files.open("c.py")
        assert files.close() is True
        assert files.paths() == ["a.py", "b.py"]
        assert files.active().path == "b.py"        # 关掉后激活前一个

    def test_close_middle_keeps_active_on_same_file(self):
        files = OpenFiles()
        for name in ("a.py", "b.py", "c.py"):
            files.open(name)
        files.select(0)
        assert files.close("c.py") is True
        assert files.active().path == "a.py"

    def test_close_unknown(self):
        files = OpenFiles()
        files.open("a.py")
        assert files.close("nope.py") is False
        assert files.count() == 1

    def test_close_last_resets_active(self):
        files = OpenFiles()
        files.open("a.py")
        assert files.close() is True
        assert files.active() is None and files.count() == 0
        assert files.close() is False

    def test_close_all(self):
        files = OpenFiles()
        for name in ("a.py", "b.py"):
            files.open(name)
        assert files.close_all() == 2
        assert files.active() is None

    def test_select_by_path_and_index(self):
        files = OpenFiles()
        files.open("a.py")
        files.open("b.py")
        assert files.select("a.py").path == "a.py"
        assert files.select(1).path == "b.py"
        assert files.select("nope.py") is None
        assert files.select(99) is None

    def test_next_and_previous_wrap(self):
        files = OpenFiles()
        for name in ("a.py", "b.py", "c.py"):
            files.open(name)
        assert files.next().path == "a.py"          # c -> a
        assert files.next().path == "b.py"
        assert files.previous().path == "a.py"
        assert files.previous().path == "c.py"

    def test_next_with_single_file(self):
        files = OpenFiles()
        files.open("a.py")
        assert files.next().path == "a.py"
        assert files.previous().path == "a.py"

    def test_next_on_empty(self):
        files = OpenFiles()
        assert files.next() is None and files.previous() is None
        assert files.active() is None

    def test_limit_closes_oldest(self):
        files = OpenFiles(limit=3)
        for name in ("a.py", "b.py", "c.py", "d.py"):
            files.open(name)
        assert files.paths() == ["b.py", "c.py", "d.py"]
        assert files.active().path == "d.py"

    def test_dirty_tracking(self):
        files = OpenFiles()
        files.open("a.py")
        files.open("b.py")
        files.mark_dirty("a.py")
        assert files.any_dirty() is True
        assert files.dirty_paths() == ["a.py"]
        files.mark_dirty("a.py", False)
        assert files.any_dirty() is False

    def test_mark_dirty_unknown_path(self):
        files = OpenFiles()
        files.mark_dirty("nope.py")
        assert files.any_dirty() is False

    def test_rows_show_dirty_mark(self):
        files = OpenFiles()
        files.open("a.py")
        files.open("b.py")
        files.mark_dirty("b.py")
        rows = files.rows()
        assert len(rows) == 2
        assert DIRTY_MARK in rows[1] and CLEAN_MARK in rows[0]

    def test_rows_reverse_to_paths(self):
        """显示 <-> 反解必须闭合（点错行不能跳到别的文件）。"""
        files = OpenFiles()
        for name in ("a.py", "b.py", "c.py"):
            files.open(name)
        for label, path in zip(files.rows(), files.paths()):
            assert files.path_for_row(label) == path

    def test_path_for_row_garbage(self):
        files = OpenFiles()
        files.open("a.py")
        assert files.path_for_row("") == ""
        assert files.path_for_row("没有行号") == ""
        assert files.path_for_row("99:   a.py") == ""

    def test_rows_distinguish_same_basename(self):
        files = OpenFiles()
        files.open("a/main.py")
        files.open("b/main.py")
        names = [row.split(" ", 2)[-1] for row in files.rows()]
        assert names[0] != names[1]

    def test_to_dict(self):
        files = OpenFiles()
        files.open("a.py")
        data = files.to_dict()
        assert data["count"] == 1 and data["active"] == 0
        assert data["files"][0]["name"] == "a.py"

    def test_default_limit_is_sane(self):
        assert DEFAULT_LIMIT >= 5

    def test_open_file_dataclass(self):
        item = OpenFile(path="a/b/c.py")
        assert item.name == "c.py"
        assert item.to_dict()["name"] == "c.py"


class TestTabHistory:
    def test_most_recent_first(self):
        history = TabHistory()
        history.touch("a.py")
        history.touch("b.py")
        assert history.recent() == ["b.py", "a.py"]

    def test_touch_again_moves_to_front(self):
        history = TabHistory()
        for name in ("a.py", "b.py", "c.py"):
            history.touch(name)
        history.touch("a.py")
        assert history.recent() == ["a.py", "c.py", "b.py"]

    def test_forget(self):
        history = TabHistory()
        history.touch("a.py")
        history.touch("b.py")
        history.forget("b.py")
        assert history.recent() == ["a.py"]

    def test_limit(self):
        history = TabHistory(limit=2)
        for name in ("a.py", "b.py", "c.py"):
            history.touch(name)
        assert history.recent() == ["c.py", "b.py"]

    def test_empty_and_blank(self):
        history = TabHistory()
        assert history.recent() == []
        history.touch("")
        assert history.recent() == []

    def test_normalises_backslashes(self):
        history = TabHistory()
        history.touch(r"a\b.py")
        history.touch("a/b.py")
        assert history.recent() == ["a/b.py"]

    def test_recent_limit_argument(self):
        history = TabHistory()
        for index in range(5):
            history.touch(f"f{index}.py")
        assert len(history.recent(2)) == 2
