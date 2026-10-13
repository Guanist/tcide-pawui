# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""标识符补全。

对应 TCIDE 原版 README 宣称的 "semantic completion"（`core/completion/
semantic-completion.ts`）—— 该模块在原版**从未接线，功能静默失效**。

诚实命名：这是**基于项目符号表的前缀补全**，不是机器学习语义补全。
排序规则：同文件符号 > 项目符号 > 语言关键字 > 代码片段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .snippets import SnippetLibrary

#: 光标前的标识符
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")

#: 各语言常用关键字（补全里权重低于真实符号）
KEYWORDS: dict[str, tuple[str, ...]] = {
    "python": ("def", "class", "return", "import", "from", "if", "elif", "else",
               "for", "while", "try", "except", "finally", "with", "as", "yield",
               "async", "await", "lambda", "raise", "assert", "pass", "None",
               "True", "False", "self", "super"),
    "typescript": ("function", "class", "interface", "type", "const", "let", "var",
                   "return", "import", "export", "default", "async", "await", "if",
                   "else", "for", "while", "try", "catch", "throw", "new", "this",
                   "extends", "implements", "public", "private", "readonly"),
    "javascript": ("function", "class", "const", "let", "var", "return", "import",
                   "export", "async", "await", "if", "else", "for", "while", "try",
                   "catch", "throw", "new", "this", "require", "module"),
    "go": ("func", "type", "struct", "interface", "return", "import", "package",
           "var", "const", "if", "else", "for", "range", "go", "defer", "chan",
           "map", "nil", "error"),
    "rust": ("fn", "struct", "enum", "impl", "trait", "let", "mut", "pub", "use",
             "mod", "match", "if", "else", "loop", "while", "for", "return",
             "self", "Self", "Result", "Option"),
    "java": ("class", "interface", "public", "private", "protected", "static",
             "final", "void", "return", "new", "import", "package", "extends",
             "implements", "try", "catch", "throws"),
    "csharp": ("class", "interface", "public", "private", "protected", "static",
               "readonly", "void", "return", "new", "using", "namespace", "async",
               "await", "var"),
    "shell": ("if", "then", "else", "fi", "for", "do", "done", "while", "case",
              "esac", "function", "export", "local", "echo", "exit"),
    "powershell": ("function", "param", "if", "else", "foreach", "while", "return",
                   "Write-Host", "Get-ChildItem", "Set-Location"),
    "paw": ("Window", "Column", "Row", "Text", "Button", "Input", "List", "Table",
            "Tabs", "Tab", "Dialog", "Panel", "Scroll", "SplitPane", "Canvas",
            "Style", "Theme", "Color", "Component", "For", "If", "Shortcut",
            "Markdown", "CodeBlock", "TextArea", "Select", "Checkbox", "Slider",
            "Divider", "Spacer", "Badge", "VirtualList"),
}

#: 权重（可断言）
SCORE_SAME_FILE = 3.0
SCORE_PROJECT = 2.0
SCORE_KEYWORD = 1.5
SCORE_SNIPPET = 1.0


@dataclass
class Completion:
    """一条补全建议。"""

    text: str
    kind: str                 # symbol | keyword | snippet
    detail: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind, "detail": self.detail,
                "score": round(self.score, 2)}


class Completer:
    """基于项目符号表 + 关键字 + 片段的前缀补全。"""

    def __init__(self, index=None, library: SnippetLibrary | None = None,
                 workspace=None) -> None:
        self.index = index
        self.library = library if library is not None else SnippetLibrary()
        self.workspace = workspace

    # -- 工具 -------------------------------------------------------------

    @staticmethod
    def current_token(text_before_cursor: str) -> str:
        """取光标前正在输入的标识符（没有则空串）。"""
        match = _TOKEN_RE.search(text_before_cursor or "")
        return match.group(0) if match else ""

    def language_of(self, path: str) -> str:
        if self.workspace is not None and path:
            return self.workspace.language_of(path)
        if self.index is not None and path:
            entry = self.index.files.get(path)
            if entry is not None:
                return entry.language
        return "plaintext"

    # -- 主流程 -----------------------------------------------------------

    def complete(self, text_before_cursor: str, *, path: str = "",
                 limit: int = 20) -> list[Completion]:
        token = self.current_token(text_before_cursor)
        if not token:
            return []
        needle = token.lower()
        language = self.language_of(path)
        results: dict[str, Completion] = {}

        def add(name: str, kind: str, score: float, detail: str = "") -> None:
            if not name or not name.lower().startswith(needle) or name == token:
                return
            existing = results.get(name)
            if existing is None or existing.score < score:
                results[name] = Completion(name, kind, detail, score)

        if self.index is not None:
            for symbol in self.index.symbols():
                same_file = symbol.path == path
                add(symbol.name,
                    "symbol",
                    SCORE_SAME_FILE if same_file else SCORE_PROJECT,
                    f"{symbol.kind} · {symbol.path}")

        for keyword in KEYWORDS.get(language, ()):
            add(keyword, "keyword", SCORE_KEYWORD, language)

        for snippet in self.library.for_language(language):
            add(snippet.prefix, "snippet", SCORE_SNIPPET, snippet.name)

        ordered = sorted(results.values(), key=lambda c: (-c.score, c.text))
        return ordered[:limit]

    def suggestions_for(self, text_before_cursor: str, **kwargs) -> list[str]:
        """只要文本（界面直接塞进列表用）。"""
        return [item.text for item in self.complete(text_before_cursor, **kwargs)]
