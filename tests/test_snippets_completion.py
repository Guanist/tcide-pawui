# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""snippets.py 与 completion.py 的测试。"""

from __future__ import annotations

from tcide.completion import KEYWORDS, SCORE_KEYWORD, SCORE_PROJECT, SCORE_SAME_FILE
from tcide.completion import Completer
from tcide.indexer import ProjectIndex
from tcide.snippets import SNIPPETS, Snippet, SnippetLibrary
from tcide.workspace import Workspace


class TestSnippetLibrary:
    def test_has_at_least_fifty(self):
        """README 宣称 50+ 片段，原版实际是空的（p2 丢失后兜底返回 []）。"""
        assert len(SNIPPETS) >= 50, f"只有 {len(SNIPPETS)} 条"

    def test_prefix_unique_per_language(self):
        seen: set[tuple[str, str]] = set()
        for snippet in SNIPPETS:
            key = (snippet.language, snippet.prefix)
            assert key not in seen, f"同语言前缀重复: {key}"
            seen.add(key)

    def test_all_bodies_and_names_non_empty(self):
        for snippet in SNIPPETS:
            assert snippet.body.strip(), f"{snippet.prefix} 正文为空"
            assert snippet.name.strip(), f"{snippet.prefix} 没有名字"
            assert snippet.language, f"{snippet.prefix} 没有语言"

    def test_pawui_snippets_present(self):
        paw = [s for s in SNIPPETS if s.language == "paw"]
        assert len(paw) >= 10, f"PawUI 片段只有 {len(paw)} 条"

    def test_languages_cover_claimed_set(self):
        languages = {s.language for s in SNIPPETS}
        assert {"python", "typescript", "javascript", "go", "rust", "shell",
                "yaml", "json", "toml", "paw"} <= languages

    def test_counts_sorted(self):
        counts = SnippetLibrary().counts()
        values = list(counts.values())
        assert values == sorted(values, reverse=True)
        assert sum(values) == len(SNIPPETS)


class TestSnippetBehaviour:
    def test_placeholders_extracted(self):
        snippet = Snippet("x", "x", "def ${name}(${args}):\n    ${name}\n")
        assert snippet.placeholders == ["name", "args"]

    def test_render_replaces(self):
        snippet = Snippet("x", "x", "def ${name}(${a}): pass\n")
        assert snippet.render(name="f", a="x") == "def f(x): pass\n"

    def test_render_keeps_unknown(self):
        snippet = Snippet("x", "x", "def ${name}(): pass\n")
        assert snippet.render() == "def ${name}(): pass\n"

    def test_numbered_placeholder_form(self):
        snippet = Snippet("x", "x", "class ${1:Name}:\n    pass\n")
        assert snippet.placeholders == ["Name"]
        assert snippet.render(Name="Card") == "class Card:\n    pass\n"

    def test_library_complete_exact_prefix_first(self):
        library = SnippetLibrary()
        results = library.complete("paw", language="paw")
        assert results and results[0].prefix.startswith("paw")

    def test_library_complete_filters_language(self):
        library = SnippetLibrary()
        for snippet in library.complete("d", language="python"):
            assert snippet.language == "python"

    def test_library_complete_empty_fragment_returns_language_default(self):
        library = SnippetLibrary()
        assert library.complete("", language="go")
        assert library.complete("", language=None) == []

    def test_library_render_by_prefix(self):
        library = SnippetLibrary()
        rendered = library.render("pawwin", title="我的窗口", w="800", h="600")
        assert "我的窗口" in rendered and "800" in rendered

    def test_library_render_unknown_prefix(self):
        import pytest

        with pytest.raises(KeyError):
            SnippetLibrary().render("不存在的片段")

    def test_to_dict(self):
        data = SnippetLibrary().by_prefix("def", language="python").to_dict()
        assert set(data) >= {"prefix", "name", "language", "placeholders"}


class TestCurrentToken:
    def test_simple(self):
        assert Completer.current_token("foo.ba") == "ba"
        assert Completer.current_token("x = hel") == "hel"
        assert Completer.current_token("") == ""
        assert Completer.current_token("  ") == ""
        assert Completer.current_token("a + ") == ""
        assert Completer.current_token("中文") == ""


class TestCompleter:
    def test_same_file_symbols_rank_first(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        completer = Completer(index, workspace=workspace)
        results = completer.complete("Ser", path="src/app.py")
        assert results, "没有给出任何补全"
        assert results[0].text == "Service"
        assert results[0].score == SCORE_SAME_FILE

    def test_project_symbol_ranked_below_same_file(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        completer = Completer(index, workspace=workspace)
        # 必须输入**前缀**：实现会排除与当前词完全相同的结果
        results = completer.complete("hel", path="src/util.py")
        helper = [r for r in results if r.text == "helper"]
        assert helper and helper[0].score == SCORE_PROJECT

    def test_keywords_come_from_language(self, workspace):
        completer = Completer(None, workspace=workspace)
        results = completer.complete("ret", path="src/app.py")
        assert any(r.kind == "keyword" and r.text == "return" for r in results)

    def test_snippets_are_suggested(self, workspace):
        completer = Completer(None, workspace=workspace)
        # 注意输入的是**前缀**而不是完整前缀名：实现会排除与当前词完全相同的结果
        results = completer.complete("trye", path="src/app.py")
        assert any(r.kind == "snippet" and r.text == "tryexc" for r in results), \
            f"没有片段建议: {[(r.text, r.kind) for r in results]}"

    def test_keyword_wins_over_snippet_on_name_collision(self, workspace):
        """同名时按分数取高者 —— 关键字 > 片段（显式断言语义）。"""
        completer = Completer(None, workspace=workspace)
        results = completer.complete("datacl", path="src/app.py")
        assert results and results[0].text == "dataclass"
        assert results[0].kind == "snippet"      # 已从 KEYWORDS 移除，只剩片段

    def test_paw_keywords(self, workspace):
        workspace.write("ui.paw", "<Window/>")
        completer = Completer(None, workspace=workspace)
        results = completer.complete("Spl", path="ui.paw")
        assert any(r.text == "SplitPane" for r in results), \
            f".paw 的语言识别或关键字表不对: {[r.text for r in results]}"

    def test_paw_snippets_reachable(self, workspace):
        workspace.write("ui.paw", "<Window/>")
        completer = Completer(None, workspace=workspace)
        results = completer.complete("paww", path="ui.paw")
        assert any(r.kind == "snippet" and r.text == "pawwin" for r in results)

    def test_empty_token_gives_nothing(self, workspace):
        assert Completer(None, workspace=workspace).complete("x = ") == []

    def test_limit_respected(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        completer = Completer(index, workspace=workspace)
        assert len(completer.complete("c", path="src/app.py", limit=3)) <= 3

    def test_no_duplicate_texts(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        results = Completer(index, workspace=workspace).complete("c", path="src/app.py")
        texts = [r.text for r in results]
        assert len(texts) == len(set(texts))

    def test_exact_current_token_excluded(self, workspace):
        index = ProjectIndex(workspace)
        index.scan()
        results = Completer(index, workspace=workspace).complete(
            "Service", path="src/app.py")
        assert all(r.text != "Service" for r in results)

    def test_suggestions_for_returns_strings(self, workspace):
        completer = Completer(None, workspace=workspace)
        assert all(isinstance(x, str) for x in completer.suggestions_for(
            "ret", path="src/app.py"))

    def test_score_constants_ordered(self):
        assert SCORE_SAME_FILE > SCORE_PROJECT > SCORE_KEYWORD

    def test_keywords_defined_for_core_languages(self):
        for language in ("python", "typescript", "go", "rust", "paw"):
            assert KEYWORDS[language]
