# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""tokens.py 的测试。"""

from __future__ import annotations

from tcide.tokens import (
    code_blocks,
    collapse_whitespace,
    count_cjk,
    estimate_messages,
    estimate_tokens,
    is_cjk,
    shorten_code_blocks,
    strip_code_blocks,
    truncate_middle,
    truncate_to_tokens,
)
from tcide.trimmer import Message


class TestIsCjk:
    def test_chinese_is_cjk(self):
        assert is_cjk("虎")
        assert is_cjk("，")   # 全角标点
        assert is_cjk("あ")   # 假名
        assert is_cjk("한")   # 谚文

    def test_ascii_is_not_cjk(self):
        assert not is_cjk("a")
        assert not is_cjk(",")
        assert not is_cjk(" ")


class TestCounts:
    def test_mixed(self):
        text = "虎猫 TCIDE"
        assert count_cjk(text) == 2
        assert estimate_tokens(text) >= 2

    def test_empty(self):
        assert estimate_tokens("") == 0
        assert count_cjk("") == 0

    def test_one_char_is_at_least_one_token(self):
        assert estimate_tokens("a") == 1


class TestEstimateTokens:
    def test_cjk_roughly_one_token_per_char(self):
        text = "虎" * 100
        assert 100 <= estimate_tokens(text) <= 110

    def test_ascii_roughly_quarter(self):
        text = "a" * 400
        assert 95 <= estimate_tokens(text) <= 105

    def test_monotonic(self):
        base = "虎猫 TCIDE " * 10
        assert estimate_tokens(base) < estimate_tokens(base * 2)

    def test_messages_include_overhead(self):
        messages = [Message("user", "hi"), Message("assistant", "ok")]
        assert estimate_messages(messages) > estimate_tokens("hi") + estimate_tokens("ok")


class TestCodeBlocks:
    def test_extract(self):
        text = "before\n```py\nprint(1)\n```\nafter"
        assert code_blocks(text) == ["```py\nprint(1)\n```"]

    def test_strip(self):
        text = "a\n```py\nprint(1)\n```\nb"
        assert "print(1)" not in strip_code_blocks(text)

    def test_shorten_keeps_head_and_tail(self):
        body = "\n".join(f"line{i}" for i in range(40))
        text = f"```py\n{body}\n```"
        out = shorten_code_blocks(text, head_lines=3, tail_lines=2)
        assert "line0" in out and "line2" in out
        assert "line39" in out and "line38" in out
        assert "line20" not in out
        assert "省略" in out

    def test_shorten_leaves_small_blocks(self):
        text = "```py\nprint(1)\n```"
        assert shorten_code_blocks(text) == text


class TestTextUtils:
    def test_collapse_whitespace_keeps_newlines(self):
        out = collapse_whitespace("a    b\n\nc     d")
        assert out == "a b\n\nc d"

    def test_truncate_middle_keeps_both_ends(self):
        text = "HEAD" + "x" * 500 + "TAIL"
        out = truncate_middle(text, 100)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")
        assert len(out) <= 100 + len("\n…  (截断) …\n")

    def test_truncate_middle_noop_when_short(self):
        assert truncate_middle("short", 100) == "short"

    def test_truncate_middle_zero(self):
        assert truncate_middle("abc", 0) == ""


class TestTruncateToTokens:
    """按 token 预算截断（CJK 内容不能只按字符算）。"""

    def test_respects_budget_for_cjk(self):
        out = truncate_to_tokens("虎" * 5000, 200)
        assert estimate_tokens(out) <= 200

    def test_respects_budget_for_ascii(self):
        out = truncate_to_tokens("a" * 20000, 100)
        assert estimate_tokens(out) <= 100

    def test_noop_when_under_budget(self):
        assert truncate_to_tokens("abc", 100) == "abc"

    def test_zero_budget(self):
        assert truncate_to_tokens("abc", 0) == ""

    def test_keeps_head_and_tail(self):
        text = "HEAD" + "x" * 3000 + "TAIL"
        out = truncate_to_tokens(text, 50)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")
