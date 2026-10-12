# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""打开文件的标签模型（纯逻辑）。

对应 TCIDE 原版宣称的「编辑器：多标签 + 分屏」里的**多标签**部分。
PawUI 的 ``<Tabs>`` 是**构建期静态**的（子节点在 build 时确定），所以
"动态标签"不能靠它 —— 这里用「打开文件列表 + 一个 List 控件 + 点击切换」
实现，模型放在纯逻辑层，界面只负责显示与转发点击。

两个真实的 IDE 细节：

1. **同名文件要能区分**：``a/main.py`` 与 ``b/main.py`` 不能都显示成 ``main.py``，
   要用**最短唯一后缀**（VS Code 就是这么做的）；
2. **脏标记**：有未保存改动时要在标签上标出来（``●``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 最多同时打开多少个文件（超出就关掉最久没用过的那个）
DEFAULT_LIMIT = 20

#: 脏标记
DIRTY_MARK = "●"
CLEAN_MARK = " "

#: 行的形态：``1: ● a/main.py``
ROW_RE = re.compile(r"^\s*(?P<index>\d+):\s*(?P<mark>[●\s])\s*(?P<name>.*)$")


@dataclass
class OpenFile:
    """一个打开的文件。"""

    path: str
    dirty: bool = False
    cursor_line: int = 1

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]

    def to_dict(self) -> dict:
        return {"path": self.path, "dirty": self.dirty,
                "cursor_line": self.cursor_line, "name": self.name}


def shortest_unique_names(paths: list[str]) -> dict[str, str]:
    """给每个路径算一个**最短且唯一**的显示名。

    ``["a/main.py", "b/main.py"]`` -> ``{"a/main.py": "a/main.py",
    "b/main.py": "b/main.py"}``（因为只留 ``main.py`` 会重名）。
    """
    cleaned = [path.replace("\\", "/") for path in paths]
    parts = {path: path.split("/") for path in cleaned}
    length = {path: 1 for path in cleaned}
    result: dict[str, str] = {}
    for path in cleaned:
        pieces = parts[path]
        while length[path] < len(pieces):
            candidate = "/".join(pieces[-length[path]:])
            clash = any(other != path
                        and "/".join(parts[other][-length[path]:]) == candidate
                        for other in cleaned)
            if not clash:
                break
            length[path] += 1
        result[path] = "/".join(pieces[-length[path]:])
    return result


class OpenFiles:
    """打开的文件的顺序、激活项与脏标记。"""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self.files: list[OpenFile] = []
        self.active_index: int = -1

    # -- 基本操作 ---------------------------------------------------------

    def open(self, path: str) -> OpenFile:
        """打开（已打开则激活它，不重复）。超出上限会关掉最久未用的。"""
        normalized = (path or "").replace("\\", "/")
        for index, item in enumerate(self.files):
            if item.path == normalized:
                self.active_index = index
                return item
        item = OpenFile(path=normalized)
        self.files.append(item)
        self.active_index = len(self.files) - 1
        while len(self.files) > self.limit:
            self._close_index(0)
        return item

    def close(self, path: str | None = None) -> bool:
        """关闭指定文件（不给就关当前）。返回是否真的关掉了。"""
        if not self.files:
            return False
        index = self.active_index if path is None else self._index_of(path)
        return self._close_index(index)

    def _close_index(self, index: int) -> bool:
        if index < 0 or index >= len(self.files):
            return False
        self.files.pop(index)
        if not self.files:
            self.active_index = -1
        elif index < self.active_index:
            self.active_index -= 1
        else:
            self.active_index = min(self.active_index, len(self.files) - 1)
        return True

    def _index_of(self, path: str) -> int:
        normalized = (path or "").replace("\\", "/")
        for index, item in enumerate(self.files):
            if item.path == normalized:
                return index
        return -1

    # -- 切换 -------------------------------------------------------------

    def select(self, path_or_index) -> OpenFile | None:
        """按路径或序号激活。"""
        index = (path_or_index if isinstance(path_or_index, int)
                 else self._index_of(str(path_or_index)))
        if 0 <= index < len(self.files):
            self.active_index = index
            return self.files[index]
        return None

    def next(self) -> OpenFile | None:
        if len(self.files) < 2:
            return self.active()
        self.active_index = (self.active_index + 1) % len(self.files)
        return self.active()

    def previous(self) -> OpenFile | None:
        if len(self.files) < 2:
            return self.active()
        self.active_index = (self.active_index - 1) % len(self.files)
        return self.active()

    def active(self) -> OpenFile | None:
        if 0 <= self.active_index < len(self.files):
            return self.files[self.active_index]
        return None

    # -- 状态 -------------------------------------------------------------

    def mark_dirty(self, path: str, dirty: bool = True) -> None:
        index = self._index_of(path)
        if index >= 0:
            self.files[index].dirty = dirty

    def any_dirty(self) -> bool:
        return any(item.dirty for item in self.files)

    def dirty_paths(self) -> list[str]:
        return [item.path for item in self.files if item.dirty]

    def close_all(self) -> int:
        count = len(self.files)
        self.files = []
        self.active_index = -1
        return count

    def paths(self) -> list[str]:
        return [item.path for item in self.files]

    def count(self) -> int:
        return len(self.files)

    # -- 显示 -------------------------------------------------------------

    def display_names(self) -> dict[str, str]:
        return shortest_unique_names(self.paths())

    def rows(self) -> list[str]:
        """给 ``<List items=...>`` 的行：``1: ● a/main.py``。"""
        names = self.display_names()
        return [f"{index + 1}: {DIRTY_MARK if item.dirty else CLEAN_MARK} "
                f"{names[item.path]}"
                for index, item in enumerate(self.files)]

    def path_for_row(self, label: str) -> str:
        """从行文本反解出路径（点错了就返回空串）。"""
        match = ROW_RE.match(label or "")
        if not match:
            return ""
        index = int(match.group("index")) - 1
        if 0 <= index < len(self.files):
            return self.files[index].path
        return ""

    def to_dict(self) -> dict:
        return {"active": self.active_index, "count": len(self.files),
                "files": [item.to_dict() for item in self.files],
                "rows": self.rows()}


class TabHistory:
    """最近使用的标签顺序（Ctrl+Tab 按最近使用切换，而不是按打开顺序）。"""

    def __init__(self, limit: int = 50) -> None:
        self.limit = max(1, int(limit))
        self.order: list[str] = []

    def touch(self, path: str) -> None:
        normalized = (path or "").replace("\\", "/")
        if not normalized:
            return
        if normalized in self.order:
            self.order.remove(normalized)
        self.order.insert(0, normalized)
        del self.order[self.limit:]

    def forget(self, path: str) -> None:
        normalized = (path or "").replace("\\", "/")
        if normalized in self.order:
            self.order.remove(normalized)

    def recent(self, limit: int = 10) -> list[str]:
        return self.order[:limit]
