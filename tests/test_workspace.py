# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""workspace.py 的测试。"""

from __future__ import annotations

import pytest

from tcide.workspace import Workspace, WorkspaceError


class TestResolve:
    def test_plain_relative(self, workspace):
        assert workspace.resolve("src/app.py").name == "app.py"

    def test_escape_is_rejected(self, workspace):
        with pytest.raises(WorkspaceError):
            workspace.resolve("../outside.txt")
        with pytest.raises(WorkspaceError):
            workspace.resolve("src/../../outside.txt")

    def test_absolute_inside_is_allowed(self, workspace):
        assert workspace.resolve(workspace.root / "main.py").name == "main.py"

    def test_absolute_outside_is_rejected(self, workspace):
        with pytest.raises(WorkspaceError):
            workspace.resolve(workspace.root.parent / "nope.txt")

    def test_relative_of_root(self, workspace):
        assert workspace.relative(workspace.root / "src" / "app.py") == "src/app.py"


class TestListing:
    def test_skip_dirs_are_hidden(self, workspace):
        names = {node.name for node in workspace.list_dir()}
        assert "src" in names
        assert "node_modules" not in names
        assert ".git" not in names

    def test_tree_depth_limited(self, workspace):
        tree = workspace.tree(max_depth=1)
        assert tree.is_dir
        assert all(not child.children for child in tree.children)
        assert any(child.name == "src" for child in tree.children)

    def test_find_glob(self, workspace):
        assert workspace.find("*.py") == ["main.py", "src/app.py", "src/broken.py",
                                         "src/util.py"]
        assert workspace.find("main.*") == ["main.go", "main.py", "web/main.ts"]

    def test_iter_files_skips_noise(self, workspace):
        files = set(workspace.iter_files())
        assert "src/app.py" in files
        assert not any("node_modules" in f for f in files)
        assert not any(f.startswith(".git/") for f in files)

    def test_stats(self, workspace):
        stats = workspace.stats()
        assert stats["files"] > 5
        assert stats["by_language"]["python"] == 4
        assert stats["bytes"] > 0


class TestReadWrite:
    def test_read_and_write_roundtrip(self, workspace):
        workspace.write("src/new.py", "value = 1\n")
        assert workspace.read("src/new.py") == "value = 1\n"

    def test_write_creates_parents(self, workspace):
        workspace.write("deep/nested/file.txt", "ok")
        assert workspace.exists("deep/nested/file.txt")

    def test_write_is_atomic_no_temp_left(self, workspace):
        workspace.write("src/atomic.txt", "content")
        leftovers = [p for p in workspace.iter_files() if p.endswith(".tcide-tmp")]
        assert leftovers == []

    def test_read_truncates_large_file(self, workspace):
        workspace.write("big.txt", "x" * 5000)
        small = Workspace(workspace.root, max_file_bytes=100)
        text = small.read("big.txt")
        assert "只读了前 100 字节" in text

    def test_read_missing_raises(self, workspace):
        with pytest.raises(WorkspaceError):
            workspace.read("nope.txt")

    def test_delete(self, workspace):
        workspace.write("tmp.txt", "x")
        workspace.delete("tmp.txt")
        assert not workspace.exists("tmp.txt")
        with pytest.raises(WorkspaceError):
            workspace.delete("tmp.txt")

    def test_create_dir(self, workspace):
        workspace.create_dir("a/b/c")
        assert workspace.resolve("a/b/c").is_dir()


class TestLanguageAndBinary:
    @pytest.mark.parametrize("path,language", [
        ("a.py", "python"), ("a.ts", "typescript"), ("a.tsx", "typescript"),
        ("a.js", "javascript"), ("a.go", "go"), ("a.rs", "rust"),
        ("a.java", "java"), ("a.kt", "kotlin"), ("a.cs", "csharp"),
        ("a.rb", "ruby"), ("a.php", "php"), ("a.swift", "swift"),
        ("a.sh", "shell"), ("a.ps1", "powershell"), ("a.yml", "yaml"),
        ("a.toml", "toml"), ("a.sql", "sql"), ("a.html", "html"),
        ("a.css", "css"), ("a.paw", "paw"), ("a.json", "json"),
        ("a.unknown", "plaintext"),
    ])
    def test_language_of(self, path, language):
        assert Workspace.language_of(path) == language

    def test_binary_by_extension(self, workspace):
        assert workspace.is_binary("assets/logo.png")
        assert not workspace.is_binary("src/app.py")

    def test_is_text_file_by_content(self, workspace):
        assert workspace.is_text_file("src/app.py")
        assert not workspace.is_text_file("assets/logo.png")

    def test_twenty_three_languages_covered(self):
        """README 宣称 23 种语言高亮，这里至少有 23 个扩展名能识别。"""
        from tcide.workspace import LANGUAGE_BY_EXT

        assert len(set(LANGUAGE_BY_EXT.values())) >= 23


class TestSearch:
    def test_plain_search(self, workspace):
        hits = workspace.search("def helper")
        assert hits and hits[0].path == "src/app.py"

    def test_case_insensitive_by_default(self, workspace):
        assert workspace.search("DEF HELPER")
        assert not workspace.search("DEF HELPER", case_sensitive=True)

    def test_regex_search(self, workspace):
        hits = workspace.search(r"def\s+\w+", regex=True)
        assert len(hits) >= 3

    def test_invalid_regex_raises(self, workspace):
        with pytest.raises(WorkspaceError):
            workspace.search("([", regex=True)

    def test_include_filter(self, workspace):
        hits = workspace.search("import", include="*.py")
        assert hits and all(hit.path.endswith(".py") for hit in hits)

    def test_skips_binary_content(self, workspace):
        """二进制文件不做内容搜索（但可以按文件名找到）。"""
        assert workspace.search("binary") == []
        assert any(hit.line == 0 for hit in workspace.search("logo"))

    def test_filename_hits_have_line_zero(self, workspace):
        hits = workspace.search("app")
        filename_hits = [h for h in hits if h.line == 0]
        assert filename_hits
        assert all(h.path == "src/app.py" for h in filename_hits)
        assert filename_hits[0].score == pytest.approx(1.5)

    def test_filename_match_scores_higher(self, workspace):
        hits = workspace.search("app")
        assert hits[0].score >= 1.0
        assert any("app.py" in hit.path for hit in hits)

    def test_max_results(self, workspace):
        assert len(workspace.search("def", max_results=2)) <= 2

    def test_empty_query(self, workspace):
        assert workspace.search("") == []

    def test_recent_returns_files(self, workspace):
        recent = workspace.recent(3)
        assert len(recent) <= 3
        assert all(isinstance(name, str) for name, _ in recent)
