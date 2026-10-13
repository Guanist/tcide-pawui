# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""十六进制视图与文件类型识别。

对应 TCIDE 原版 README 宣称的「文件预览：二进制 → Hex viewer」。

纯逻辑：产出的是**文本行**（偏移 + 十六进制 + ASCII 侧栏），
界面只要把它塞进 ``<CodeBlock>`` 或 ``<TextArea readonly>`` 就行，
所以整块功能都能离线断言。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 常见文件头的魔数 -> 类型名
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG 图片"),
    (b"\xff\xd8\xff", "JPEG 图片"),
    (b"GIF87a", "GIF 图片"),
    (b"GIF89a", "GIF 图片"),
    (b"RIFF", "RIFF 容器（WAV/AVI/WebP）"),
    (b"%PDF", "PDF 文档"),
    (b"PK\x03\x04", "ZIP 容器（含 docx/xlsx/pptx/apk）"),
    (b"7z\xbc\xaf\x27\x1c", "7z 压缩包"),
    (b"\x1f\x8b", "gzip 压缩"),
    (b"Rar!\x1a\x07", "RAR 压缩包"),
    (b"\x7fELF", "ELF 可执行"),
    (b"MZ", "Windows PE 可执行"),
    (b"\xca\xfe\xba\xbe", "Mach-O / Java class（大端）"),
    (b"\xcf\xfa\xed\xfe", "Mach-O（小端 64 位）"),
    (b"SQLite format 3\x00", "SQLite 数据库"),
    (b"OggS", "Ogg 媒体"),
    (b"ID3", "MP3（带 ID3 标签）"),
    (b"\x00asm", "WebAssembly"),
    (b"\xef\xbb\xbf", "UTF-8 BOM 文本"),
    (b"\xff\xfe", "UTF-16 LE 文本"),
    (b"\xfe\xff", "UTF-16 BE 文本"),
)

#: 默认每行字节数
DEFAULT_WIDTH = 16


@dataclass
class HexLine:
    """一行十六进制视图。"""

    offset: int
    data: bytes
    width: int = DEFAULT_WIDTH

    @property
    def hex_parts(self) -> list[str]:
        return [f"{byte:02x}" for byte in self.data]

    @property
    def ascii(self) -> str:
        return "".join(chr(byte) if 32 <= byte < 127 else "." for byte in self.data)

    def render(self, *, show_ascii: bool = True) -> str:
        padded = self.hex_parts + ["  "] * (self.width - len(self.hex_parts))
        # 每 8 字节留一个空隙，方便肉眼对位
        grouped = " ".join(padded[:8]) + "  " + " ".join(padded[8:])
        line = f"{self.offset:08x}  {grouped}"
        return f"{line}  |{self.ascii}|" if show_ascii else line

    def to_dict(self) -> dict:
        return {"offset": self.offset, "hex": " ".join(self.hex_parts),
                "ascii": self.ascii}

    def __str__(self) -> str:
        return self.render()


class HexViewer:
    """按页读取 + 搜索 + 类型识别。"""

    def __init__(self, workspace=None, *, width: int = DEFAULT_WIDTH) -> None:
        if width <= 0:
            raise ValueError("width 必须为正")
        self.workspace = workspace
        self.width = int(width)

    # -- 数据 -------------------------------------------------------------

    def _read(self, path: str, offset: int) -> bytes:
        if self.workspace is None:
            raise ValueError("没有 workspace，无法读取文件")
        data = self.workspace.read_bytes(path, max_bytes=offset + self.width * 256)
        return data

    def page(self, path: str, *, offset: int = 0, rows: int = 16) -> list[HexLine]:
        """读一页（从 ``offset`` 起 ``rows`` 行）。"""
        if offset < 0 or rows <= 0:
            raise ValueError("offset 不能为负，rows 必须为正")
        data = self._read(path, offset)
        chunk = data[offset:offset + rows * self.width]
        return [HexLine(offset=offset + index * self.width,
                        data=chunk[index * self.width:(index + 1) * self.width],
                        width=self.width)
                for index in range((len(chunk) + self.width - 1) // self.width)]

    def render(self, path: str, *, offset: int = 0, rows: int = 16,
               show_ascii: bool = True) -> str:
        lines = self.page(path, offset=offset, rows=rows)
        if not lines:
            return f"（{path} 在偏移 {offset} 处没有数据）"
        header = f"{'offset':8}  {'hex':<{self.width * 3 + 3}}  ascii" if show_ascii \
            else f"{'offset':8}  hex"
        body = "\n".join(line.render(show_ascii=show_ascii) for line in lines)
        return f"{header}\n{body}"

    def find(self, path: str, needle: bytes | str, *, limit: int = 20,
             max_bytes: int = 8 * 1024 * 1024) -> list[int]:
        """在文件里找字节串/字符串，返回所有偏移。"""
        pattern = needle.encode("utf-8") if isinstance(needle, str) else bytes(needle)
        if not pattern:
            return []
        if self.workspace is None:
            raise ValueError("没有 workspace，无法搜索文件")
        data = self.workspace.read_bytes(path, max_bytes=max_bytes)
        offsets: list[int] = []
        start = 0
        while len(offsets) < limit:
            found = data.find(pattern, start)
            if found == -1:
                break
            offsets.append(found)
            start = found + 1
        return offsets

    # -- 元信息 -----------------------------------------------------------

    @staticmethod
    def sniff(head: bytes) -> str:
        """按魔数判断类型；认不出就按「看起来是不是文本」猜。"""
        for magic, label in MAGIC:
            if head.startswith(magic):
                return label
        if not head:
            return "空文件"
        if b"\x00" not in head[:1024]:
            try:
                head[:1024].decode("utf-8")
                return "文本"
            except UnicodeDecodeError:
                return "未知二进制（含非 UTF-8 字节）"
        return "未知二进制"

    def info(self, path: str) -> dict:
        """文件概览：大小、类型、是否文本、前 16 字节。"""
        if self.workspace is None:
            raise ValueError("没有 workspace，无法读取文件")
        size = self.workspace.resolve(path).stat().st_size
        head = self.workspace.read_bytes(path, max_bytes=1024)
        return {
            "path": path,
            "size": size,
            "rows": (size + self.width - 1) // self.width,
            "kind": self.sniff(head),
            "is_text": b"\x00" not in head,
            "head_hex": " ".join(f"{byte:02x}" for byte in head[:self.width]),
        }

    @staticmethod
    def entropy(data: bytes) -> float:
        """香农熵（0-8）：越高越像压缩/加密数据。"""
        if not data:
            return 0.0
        import math

        counts: dict[int, int] = {}
        for byte in data:
            counts[byte] = counts.get(byte, 0) + 1
        total = len(data)
        return -sum((count / total) * math.log2(count / total)
                    for count in counts.values())

    def report(self, path: str) -> str:
        data = self.info(path)
        head = self.workspace.read_bytes(path, max_bytes=4096)
        lines = [f"# {path}", "",
                 f"- 大小：{data['size']} 字节（{data['rows']} 行 × {self.width}）",
                 f"- 类型：{data['kind']}（{'文本' if data['is_text'] else '二进制'}）",
                 f"- 前 {self.width} 字节：`{data['head_hex']}`",
                 f"- 前 4KB 熵：{self.entropy(head):.2f} / 8"]
        lines.append("")
        lines.append("```")
        lines.append(self.render(path, rows=4))
        lines.append("```")
        return "\n".join(lines)
