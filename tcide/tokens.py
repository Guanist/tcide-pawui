# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Token 估算与文本工具。

TCIDE 原版按「中文字符数 + 英文字符数/4」粗估（`contextManager.ts` 里的
`cnChars` / `enChars`）。这里保持同样的口径，但把它做成可测试的纯函数，
并额外提供与真实分词器对齐的校准系数。

注意：这是**估算**，不是精确 tokenizer。所有对外接口都叫 estimate_*，
避免调用方误以为是精确值。
"""

from __future__ import annotations

import re

#: 中日韩统一表意文字 + 扩展 A + 兼容表意 + 全角标点 + 假名 + 谚文
CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF), (0x2E80, 0x2EFF), (0x3000, 0x303F), (0x3040, 0x30FF),
    (0x3130, 0x318F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F),
    (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF),
    (0x20000, 0x2FA1F),
)

#: 1 个 ASCII 字符约等于多少 token 的倒数（经验值：英文约 4 字符 1 token）
_ASCII_CHARS_PER_TOKEN = 4.0

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_WHITESPACE_RUN_RE = re.compile(r"[ \t]{2,}")


def is_cjk(char: str) -> bool:
    """单个字符是否属于 CJK 区。"""
    code = ord(char)
    return any(low <= code <= high for low, high in CJK_RANGES)


def count_cjk(text: str) -> int:
    """CJK 字符个数。"""
    return sum(1 for char in text if is_cjk(char))


def count_other(text: str) -> int:
    """非 CJK 字符个数。"""
    return len(text) - count_cjk(text)


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（CJK 1 字符 ≈ 1 token，ASCII 4 字符 ≈ 1 token）。

    空文本返回 0；至少 1 个字符返回至少 1。
    """
    if not text:
        return 0
    cjk = count_cjk(text)
    other = len(text) - cjk
    return int(cjk + other / _ASCII_CHARS_PER_TOKEN + 0.999)


def estimate_messages(messages) -> int:
    """估算一组消息的总 token。

    如果消息自带 ``tokens()``（``trimmer.Message`` 就是），就用它 —— 它会把
    附带的依赖文件也算进去。**必须走同一条口径**：否则附件再大也测不出超预算。
    没有 ``tokens()`` 的普通对象退化为「content + 4」。
    """
    total = 0
    for message in messages:
        counter = getattr(message, "tokens", None)
        if callable(counter):
            total += counter()
            continue
        total += estimate_tokens(getattr(message, "content", "") or "") + 4
    return total


def code_blocks(text: str) -> list[str]:
    """取出文本里的 ``` 围栏代码块。"""
    return _CODE_FENCE_RE.findall(text)


def strip_code_blocks(text: str) -> str:
    """把围栏代码块替换成占位符（用于估算「去掉代码后的骨架」大小）。"""
    return _CODE_FENCE_RE.sub("```…```", text)


def shorten_code_blocks(text: str, head_lines: int = 8, tail_lines: int = 4) -> str:
    """把每个代码块压缩成「头 N 行 + … + 尾 M 行」。

    用于上下文紧张时保留代码骨架、丢掉中间实现细节。
    正确保留围栏后的语言标记（```python 里的 python 不是代码行）。
    """
    def shrink(match: re.Match[str]) -> str:
        block = match.group(0)
        inner = block[3:-3]
        if inner.startswith("\n"):
            info, body = "", inner[1:]
        else:
            info, _, body = inner.partition("\n")
        if body.endswith("\n"):
            body = body[:-1]
        lines = body.split("\n")
        if len(lines) <= head_lines + tail_lines + 1:
            return block
        kept = lines[:head_lines] + ["…  (中间省略)"] + lines[-tail_lines:]
        return "```" + info + "\n" + "\n".join(kept) + "\n```"

    return _CODE_FENCE_RE.sub(shrink, text)


def truncate_to_tokens(text: str, max_tokens: int,
                       marker: str = "\n…  (截断) …\n") -> str:
    """按 **token** 预算截断（按字符等比例迭代逼近）。

    `truncate_middle` 只能按字符数截断，而 CJK 1 字符 ≈ 1 token、ASCII 4 字符 ≈
    1 token，只按字符算会让中文内容远超预算。这里迭代几次把字符预算调到位。
    """
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    floor = len(marker) + 8
    chars = max(floor, len(text))
    candidate = text
    for _ in range(5):
        candidate = truncate_middle(text, chars, marker)
        current = estimate_tokens(candidate)
        if current <= max_tokens:
            return candidate
        ratio = max_tokens / max(1, current)
        chars = max(floor, int(len(candidate) * ratio * 0.96))
    return truncate_middle(text, chars, marker)


def collapse_whitespace(text: str) -> str:
    """折叠多余空白（保留换行）。"""
    lines = [_WHITESPACE_RUN_RE.sub(" ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines)


def truncate_middle(text: str, max_chars: int, marker: str = "\n…  (截断) …\n") -> str:
    """按字符数在**中间**截断，保留头尾（尾部通常含结论/报错）。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return text[:max_chars]
    keep = max_chars - len(marker)
    head = keep * 2 // 3
    tail = keep - head
    return text[:head] + marker + text[len(text) - tail:]
