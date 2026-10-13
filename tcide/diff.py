# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Diff 引擎与改动审核（原版丢失的 ``acceptDiff`` / ``closeDiffModal``）。

考古结论（`recovered-modules/p2-modules.js` 头部注释）：

> 原始 p2-modules.js 在仓库历史中丢失，其曾定义的全局
> （``acceptDiff`` / ``closeDiffModal`` 等）……

也就是说原版**曾经有**一个 Diff 审核弹窗，后来连文件带功能一起丢了。这里把它做回来，
并且做成可离线严格测试的纯逻辑：

- :func:`unified_diff` —— 生成标准 unified diff（``difflib``）；
- :func:`parse_unified` —— 反过来解析 diff 文本（含多文件、多 hunk、新增/删除文件、
  ``\\ No newline at end of file``）；
- :func:`apply_selected` —— **按 hunk 选择性应用**，这才是 ``acceptDiff`` 的真正语义
  （用户可能只接受一个改动块里的某一处）；
- :class:`ReviewSession` —— 收集 AI 提出的改动，逐条接受/驳回，接受时才真正写盘。

设计要点：**接受之前磁盘上什么都不变**。这是"审查门"能成立的前提。
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field

#: hunk 头：``@@ -1,3 +1,4 @@``（计数可省略，默认 1）
HUNK_RE = re.compile(r"^@@ -(?P<o>\d+)(?:,(?P<oc>\d+))? \+(?P<n>\d+)(?:,(?P<nc>\d+))? @@")

#: 文件头：``--- a/path`` / ``+++ b/path``
FILE_HEADER_RE = re.compile(r"^(?P<marker>---|\+\+\+)\s+(?P<path>.+?)(?:\t.*)?$")

#: 二进制/元信息行，直接忽略
IGNORED_PREFIXES = ("diff ", "index ", "new file mode", "deleted file mode",
                    "old mode", "new mode", "similarity index", "rename ",
                    "Binary files ", "\\ No newline")

NO_NEWLINE = "\\ No newline at end of file"


@dataclass
class Hunk:
    """一个改动块。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)      # 带 ' ' / '+' / '-' 前缀
    no_newline_at_end: bool = False

    def header(self) -> str:
        return (f"@@ -{self.old_start},{self.old_count} "
                f"+{self.new_start},{self.new_count} @@")

    @property
    def added(self) -> int:
        return sum(1 for line in self.lines if line.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for line in self.lines if line.startswith("-"))

    @property
    def new_lines(self) -> list[str]:
        """这个 hunk 在**新文件**里对应的内容（上下文 + 新增）。"""
        return [line[1:] for line in self.lines if line[:1] in (" ", "+")]

    @property
    def old_lines(self) -> list[str]:
        """这个 hunk 在**旧文件**里对应的内容（上下文 + 删除）。"""
        return [line[1:] for line in self.lines if line[:1] in (" ", "-")]

    def render(self) -> str:
        body = "\n".join(self.lines)
        text = f"{self.header()}\n{body}"
        if self.no_newline_at_end:
            text += f"\n{NO_NEWLINE}"
        return text

    def to_dict(self) -> dict:
        return {"old_start": self.old_start, "old_count": self.old_count,
                "new_start": self.new_start, "new_count": self.new_count,
                "added": self.added, "removed": self.removed,
                "header": self.header()}


@dataclass
class FilePatch:
    """一个文件的改动集合。"""

    path: str
    hunks: list[Hunk] = field(default_factory=list)
    old_path: str = ""
    is_new: bool = False
    is_deleted: bool = False

    @property
    def added(self) -> int:
        return sum(hunk.added for hunk in self.hunks)

    @property
    def removed(self) -> int:
        return sum(hunk.removed for hunk in self.hunks)

    def to_dict(self) -> dict:
        return {"path": self.path, "added": self.added, "removed": self.removed,
                "hunks": [hunk.to_dict() for hunk in self.hunks],
                "is_new": self.is_new, "is_deleted": self.is_deleted}

    def render(self) -> str:
        head = [f"--- {self.old_path or self.path}", f"+++ {self.path}"]
        return "\n".join(head + [hunk.render() for hunk in self.hunks])


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def unified_diff(old: str, new: str, *, path: str = "file",
                 context: int = 3) -> str:
    """生成 unified diff 文本（path 只用于文件头，不读盘）。"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines,
                                fromfile=f"a/{path}", tofile=f"b/{path}", n=context)
    return "".join(diff)


def diff_stats(text: str) -> dict:
    """统计 diff 文本的增删行数（忽略文件头）。"""
    added = removed = files = 0
    for line in text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            if line.startswith("+++ "):
                files += 1
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"files": files, "added": added, "removed": removed}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _header_path(value: str, marker: str) -> str:
    """从 ``--- a/x.py`` / ``+++ b/x.py`` 里取路径。

    注意**不能**用 ``str.lstrip("a/")`` —— ``lstrip`` 是按**字符集**剥离的，
    ``"a/app.py".lstrip("a/")`` 会得到 ``"pp.py"``。``/dev/null`` 表示该侧不存在。
    """
    text = value.strip()
    if text in ("/dev/null", "dev/null", ""):
        return ""
    prefix = "a/" if marker == "---" else "b/"
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def parse_unified(text: str) -> list[FilePatch]:
    """解析 unified diff 文本。容忍 git 的额外头（index/mode/rename…）。"""
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    pending_new = False
    pending_deleted = False

    def flush_hunk() -> None:
        nonlocal hunk
        if hunk is not None and current is not None:
            current.hunks.append(hunk)
        hunk = None

    def flush_file() -> None:
        nonlocal current
        flush_hunk()
        if current is not None and (current.hunks or current.is_new
                                    or current.is_deleted):
            patches.append(current)
        current = None

    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            flush_file()
            continue
        if raw.startswith("new file mode"):
            # 这一行出现在 --- / +++ **之前**，此时 current 还不存在，
            # 所以必须先记成"待定标志"，建 FilePatch 时再带上。
            pending_new = True
            continue
        if raw.startswith("deleted file mode"):
            pending_deleted = True
            continue
        if raw.startswith("--- "):
            # 文件段一定以 --- 开头：无条件结束上一个文件（hunk 正文里不会出现 --- ）
            flush_file()
            old_name = _header_path(raw[4:], "---")
            current = FilePatch(path=old_name, old_path=old_name,
                                is_new=pending_new, is_deleted=pending_deleted)
            pending_new = pending_deleted = False
            continue
        if raw.startswith("+++ "):
            new_name = _header_path(raw[4:], "+++")
            if current is not None:
                if not new_name:
                    current.is_deleted = True          # +++ /dev/null => 删除
                else:
                    if not current.path:
                        current.is_new = True          # --- /dev/null => 新增
                    current.path = new_name
            continue
        if raw.startswith("@@"):
            flush_hunk()
            match = HUNK_RE.match(raw)
            if match is None or current is None:
                continue
            hunk = Hunk(old_start=int(match.group("o")),
                        old_count=int(match.group("oc") or 1),
                        new_start=int(match.group("n")),
                        new_count=int(match.group("nc") or 1))
            continue
        if hunk is not None:
            if raw.startswith(NO_NEWLINE):
                hunk.no_newline_at_end = True
                continue
            if raw == "":
                # 合法的空上下文行（'' 表示一个空行，前缀算作空格）
                hunk.lines.append(" ")
                continue
            if raw[0] in " +-":
                hunk.lines.append(raw)
                continue
            # 其他杂项行：结束当前 hunk
            flush_hunk()
            continue
        if any(raw.startswith(prefix) for prefix in IGNORED_PREFIXES):
            continue

    flush_file()
    return patches


# ---------------------------------------------------------------------------
# 应用（含按 hunk 选择）
# ---------------------------------------------------------------------------

def apply_selected(original: str, patch: FilePatch, selected: list[int] | None = None,
                   *, keep_newline: bool = True) -> str:
    """把 patch 里**选中的** hunk 应用到 original 上。

    ``selected=None`` 表示全选。这是 ``acceptDiff`` 的核心语义：用户可以只接受
    一个改动块，其余保持原样。返回新文本。
    """
    if selected is None:
        selected = list(range(len(patch.hunks)))
    chosen = set(selected)
    lines = original.splitlines(keepends=True)
    if not lines and patch.hunks and not original:
        lines = []

    out: list[str] = []
    cursor = 0                      # 已消费的原始行数（0 基）
    for number, hunk in enumerate(patch.hunks):
        start = max(0, hunk.old_start - 1)
        if start < cursor:          # 防御：hunk 顺序异常
            continue
        out.extend(lines[cursor:start])
        if number in chosen:
            for text in hunk.new_lines:
                out.append(text + "\n")
        else:
            out.extend(lines[start:start + hunk.old_count])
        cursor = start + hunk.old_count
    out.extend(lines[cursor:])
    return "".join(out)


def apply_patch(original: str, patch: FilePatch) -> str:
    """全部应用（``apply_selected`` 的简写）。"""
    return apply_selected(original, patch, None)


def newline_style(text: str) -> str:
    """文件用的是 CRLF 还是 LF（写回时保持一致，避免整文件 diff）。"""
    return "\r\n" if "\r\n" in text else "\n"


# ---------------------------------------------------------------------------
# 审核会话
# ---------------------------------------------------------------------------

@dataclass
class ChangeProposal:
    """一条待审核的改动。"""

    path: str
    original: str
    modified: str
    source: str = ""
    created_at: float = field(default_factory=time.time)
    is_new_file: bool = False
    accepted: bool = False
    rejected: bool = False

    @property
    def diff(self) -> str:
        return unified_diff(self.original, self.modified, path=self.path)

    @property
    def patch(self) -> FilePatch:
        patches = parse_unified(self.diff)
        if patches:
            return patches[0]
        return FilePatch(path=self.path)

    @property
    def stats(self) -> dict:
        return diff_stats(self.diff)

    @property
    def changed(self) -> bool:
        return self.original != self.modified

    def to_dict(self) -> dict:
        return {"path": self.path, "source": self.source, "stats": self.stats,
                "is_new_file": self.is_new_file, "changed": self.changed,
                "accepted": self.accepted, "rejected": self.rejected}

    def preview(self, *, limit: int = 200) -> str:
        text = self.diff
        lines = text.splitlines()
        if len(lines) <= limit:
            return text
        return "\n".join(lines[:limit] + [f"…（还有 {len(lines) - limit} 行）"])


class ReviewSession:
    """收集改动 → 展示 diff → 接受/驳回。**接受之前磁盘上什么都不变。**"""

    def __init__(self, workspace=None) -> None:
        self.workspace = workspace
        self.proposals: list[ChangeProposal] = []
        self.applied: list[str] = []
        self.discarded: list[str] = []

    # -- 收集 -------------------------------------------------------------

    def propose(self, path: str, modified: str, *, source: str = "") -> ChangeProposal:
        """登记一条改动；已存在同路径的未决改动会被替换（只保留最新意图）。"""
        original = ""
        is_new = False
        if self.workspace is not None:
            if self.workspace.exists(path):
                original = self.workspace.read(path)
            else:
                is_new = True
        self.proposals = [p for p in self.proposals if p.path != path]
        proposal = ChangeProposal(path=path, original=original, modified=modified,
                                  source=source, is_new_file=is_new)
        self.proposals.append(proposal)
        return proposal

    def propose_bytes(self, path: str, data: bytes, *, source: str = "") -> ChangeProposal:
        """二进制改动（hex 视图/资源文件）：不做逐行 diff，只登记。"""
        proposal = ChangeProposal(path=path, original="", modified="",
                                  source=source or "二进制", is_new_file=True)
        proposal.binary = data                      # type: ignore[attr-defined]
        self.proposals.append(proposal)
        return proposal

    def get(self, path: str) -> ChangeProposal | None:
        return next((p for p in self.proposals if p.path == path), None)

    def pending(self) -> list[ChangeProposal]:
        return [p for p in self.proposals if not p.accepted and not p.rejected]

    def has_pending(self) -> bool:
        return bool(self.pending())

    def clear(self) -> int:
        count = len(self.pending())
        self.proposals = [p for p in self.proposals if p.accepted or p.rejected]
        return count

    # -- 审核 -------------------------------------------------------------

    def accept(self, path: str | None = None, *,
               selected_hunks: list[int] | None = None) -> int:
        """接受改动并**写盘**。

        ``selected_hunks`` 只对单个文件（``path`` 指定）有意义：只应用选中的
        hunk，其余保持原样 —— 这是 ``acceptDiff`` 的部分接受语义。
        """
        targets = [self.get(path)] if path else self.pending()
        written = 0
        for proposal in targets:
            if proposal is None or proposal.accepted or proposal.rejected:
                continue
            if self.workspace is None:
                raise ValueError("没有 workspace，无法写盘")
            data = getattr(proposal, "binary", None)
            if data is not None:
                self.workspace.write_bytes(proposal.path, data)
            elif selected_hunks is None:
                self.workspace.write(proposal.path, proposal.modified)
            else:
                text = apply_selected(proposal.original, proposal.patch,
                                      selected_hunks)
                self.workspace.write(proposal.path, text)
            proposal.accepted = True
            self.applied.append(proposal.path)
            written += 1
        return written

    def reject(self, path: str | None = None) -> int:
        """驳回改动（不写盘）。``path=None`` 表示全部驳回。"""
        targets = [self.get(path)] if path else self.pending()
        count = 0
        for proposal in targets:
            if proposal is None or proposal.accepted or proposal.rejected:
                continue
            proposal.rejected = True
            self.discarded.append(proposal.path)
            count += 1
        return count

    # -- 汇总 -------------------------------------------------------------

    def totals(self) -> dict:
        pending = self.pending()
        return {
            "pending": len(pending),
            "accepted": len(self.applied),
            "rejected": len(self.discarded),
            "added": sum(p.stats["added"] for p in pending),
            "removed": sum(p.stats["removed"] for p in pending),
        }

    def to_dict(self) -> dict:
        return {"totals": self.totals(),
                "proposals": [p.to_dict() for p in self.proposals]}

    def report(self) -> str:
        pending = self.pending()
        if not self.proposals:
            return "没有待审核的改动"
        totals = self.totals()
        lines = [f"待审核 {totals['pending']} 个文件"
                 f"（+{totals['added']} / -{totals['removed']}）", ""]
        for proposal in pending:
            stats = proposal.stats
            flag = "（新文件）" if proposal.is_new_file else ""
            lines.append(f"**{proposal.path}**{flag}  +{stats['added']} -{stats['removed']}")
            if proposal.source:
                lines.append(f"  来源：{proposal.source}")
            for line in proposal.preview(limit=40).splitlines():
                lines.append(f"  {line}")
            lines.append("")
        if self.applied:
            lines.append(f"已接受：{', '.join(self.applied)}")
        if self.discarded:
            lines.append(f"已驳回：{', '.join(self.discarded)}")
        return "\n".join(lines).rstrip()


class ReviewingWorkspace:
    """包住 :class:`~tcide.workspace.Workspace`：**写操作进审核队列，不落盘**。

    Agent 的工具层只认 ``read`` / ``write`` / ``exists`` / ``resolve`` 这些方法
    （鸭子类型），所以套一层代理就能把"直接写盘"改成"先给用户看 diff"，
    而且**完全不用改动 ``tools.py``**。

    ``enabled=False`` 时退化成透明代理（等价于原 workspace）。
    """

    #: 会被拦截的写方法
    WRITES = ("write", "write_bytes", "delete")

    def __init__(self, workspace, session: ReviewSession, *,
                 enabled: bool = True, source: str = "AI") -> None:
        self.workspace = workspace
        self.session = session
        self.enabled = enabled
        self.source = source
        self.intercepted: list[str] = []

    # -- 拦截 -------------------------------------------------------------

    def write(self, relative, content, **kwargs):
        if not self.enabled:
            return self.workspace.write(relative, content, **kwargs)
        path = self._relative(relative)
        self.session.propose(path, content, source=self.source)
        self.intercepted.append(path)
        return self.workspace.resolve(path)

    def write_bytes(self, relative, data):
        if not self.enabled:
            return self.workspace.write_bytes(relative, data)
        path = self._relative(relative)
        self.session.propose_bytes(path, data, source=self.source)
        self.intercepted.append(path)
        return self.workspace.resolve(path)

    def delete(self, relative):
        """删除也走审核：用「改成空内容」表示，接受时写空文件。

        真正的删除动作留给用户在界面/命令行确认（这里不静默删文件）。
        """
        if not self.enabled:
            return self.workspace.delete(relative)
        path = self._relative(relative)
        original = self.workspace.read(path) if self.workspace.exists(path) else ""
        self.session.propose(path, "", source=f"{self.source}（删除）")
        self.intercepted.append(path)
        return self.workspace.resolve(path)

    # -- 透明代理 ---------------------------------------------------------

    def _relative(self, relative) -> str:
        return self.workspace.relative(self.workspace.resolve(relative))

    def __getattr__(self, name):
        # 只对未拦截的属性回落到真 workspace
        return getattr(self.workspace, name)

    def __repr__(self) -> str:      # pragma: no cover - 调试用
        return (f"ReviewingWorkspace(enabled={self.enabled}, "
                f"intercepted={len(self.intercepted)})")
