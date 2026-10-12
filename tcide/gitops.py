# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Git 操作封装。

对应 TCIDE 原版的 `git-intelligence.ts` 与 Git 面板（状态/一键 stage-commit-push/分支切换）。
纯子进程调用，不依赖 Qt，可用临时仓库真跑测试。

要点：
- 所有命令统一走 :meth:`GitRepo.run`，带超时、非交互环境变量，避免卡住界面；
- 用 ``-c core.quotepath=false`` 保证中文/非 ASCII 文件名不被转义成八进制；
- ``status`` 用 ``--porcelain=v1 -z`` 解析：这是唯一能正确处理重命名与特殊字符的格式。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: 单条 git 命令的默认超时（秒）
DEFAULT_TIMEOUT = 30

#: 让 git 不要尝试交互（凭据/编辑器/分页），否则界面会被挂住
NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}


class GitError(Exception):
    """git 调用失败（不可用、超时、命令返回非零且调用方要求检查）。"""


@dataclass
class GitResult:
    """一次 git 命令的结果。"""

    args: tuple[str, ...]
    code: int
    stdout: str
    stderr: str
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def output(self) -> str:
        return self.stdout if self.stdout.strip() else self.stderr

    def to_dict(self) -> dict:
        return {"args": list(self.args), "code": self.code,
                "stdout": self.stdout[:2000], "stderr": self.stderr[:2000],
                "ok": self.ok, "duration_ms": self.duration_ms}


@dataclass
class FileChange:
    """一个文件的状态位（X=暂存区，Y=工作区）。"""

    path: str
    x: str = " "
    y: str = " "
    original: str = ""   # 重命名/复制时的原路径

    @property
    def staged(self) -> bool:
        return self.x not in (" ", "?")

    @property
    def unstaged(self) -> bool:
        return self.y not in (" ", "?")

    @property
    def untracked(self) -> bool:
        return self.x == "?" and self.y == "?"

    @property
    def conflicted(self) -> bool:
        return self.x == "U" or self.y == "U" or (self.x, self.y) in (
            ("A", "A"), ("D", "D"))

    @property
    def kind(self) -> str:
        """人类可读的变更类型。"""
        code = self.x if self.x not in (" ", "?") else self.y
        return {
            "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
            "C": "copied", "U": "conflicted", "?": "untracked", "T": "typechange",
        }.get(code, "unknown")

    def to_dict(self) -> dict:
        return {"path": self.path, "x": self.x, "y": self.y,
                "original": self.original, "kind": self.kind,
                "staged": self.staged, "unstaged": self.unstaged,
                "untracked": self.untracked, "conflicted": self.conflicted}


@dataclass
class GitStatus:
    """`git status` 的结构化结果。"""

    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    changes: list[FileChange] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.changes

    @property
    def staged(self) -> list[FileChange]:
        return [c for c in self.changes if c.staged]

    @property
    def unstaged(self) -> list[FileChange]:
        return [c for c in self.changes if c.unstaged]

    @property
    def untracked(self) -> list[FileChange]:
        return [c for c in self.changes if c.untracked]

    @property
    def conflicted(self) -> list[FileChange]:
        return [c for c in self.changes if c.conflicted]

    def to_dict(self) -> dict:
        return {"branch": self.branch, "upstream": self.upstream,
                "ahead": self.ahead, "behind": self.behind,
                "clean": self.clean,
                "changes": [c.to_dict() for c in self.changes]}


@dataclass
class Commit:
    """一条提交记录。"""

    sha: str
    short: str
    author: str
    email: str
    date: str
    subject: str

    def to_dict(self) -> dict:
        return {"sha": self.sha, "short": self.short, "author": self.author,
                "date": self.date, "subject": self.subject}


@dataclass
class Branch:
    """一个分支。"""

    name: str
    current: bool = False
    upstream: str = ""
    sha: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "current": self.current,
                "upstream": self.upstream, "sha": self.sha}


@dataclass
class FileStat:
    """`diff --numstat` 的一行。"""

    path: str
    additions: int
    deletions: int

    @property
    def total(self) -> int:
        return self.additions + self.deletions

    def to_dict(self) -> dict:
        return {"path": self.path, "additions": self.additions,
                "deletions": self.deletions}


class GitRepo:
    """一个工作目录上的 git 操作。"""

    def __init__(self, path: str | Path, *, git: str = "git",
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        self.path = Path(path).resolve()
        self.git = git
        self.timeout = int(timeout)
        self._available: bool | None = None

    # -- 基础 -------------------------------------------------------------

    def available(self) -> bool:
        """git 可执行文件是否存在。"""
        if self._available is None:
            try:
                result = subprocess.run([self.git, "--version"], capture_output=True,
                                        text=True, timeout=10)
                self._available = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._available = False
        return self._available

    def run(self, *args: str, check: bool = False, timeout: int | None = None,
            input_text: str | None = None) -> GitResult:
        """执行一条 git 命令。"""
        if not self.available():
            raise GitError(f"找不到可用的 git: {self.git}")
        env = dict(os.environ)
        env.update(NONINTERACTIVE_ENV)
        argv = [self.git, "-c", "core.quotepath=false", *args]
        import time

        started = time.perf_counter()
        try:
            result = subprocess.run(
                argv, cwd=self.path, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout or self.timeout, input=input_text)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git 超时({self.timeout}s): {' '.join(args)}") from exc
        outcome = GitResult(args=tuple(args), code=result.returncode,
                            stdout=result.stdout or "", stderr=result.stderr or "",
                            duration_ms=int((time.perf_counter() - started) * 1000))
        if check and not outcome.ok:
            raise GitError(f"git {' '.join(args)} 失败({outcome.code}): "
                           f"{outcome.stderr.strip()[:300]}")
        return outcome

    def is_repo(self) -> bool:
        """当前目录是否是 git 仓库（工作树内任意位置都算）。"""
        if not self.available():
            return False
        result = self.run("rev-parse", "--is-inside-work-tree")
        return result.ok and result.stdout.strip() == "true"

    def require_repo(self) -> None:
        if not self.is_repo():
            raise GitError(f"不是 git 仓库: {self.path}")

    def root(self) -> Path:
        """仓库根目录。"""
        result = self.run("rev-parse", "--show-toplevel", check=True)
        return Path(result.stdout.strip()).resolve()

    # -- 仓库生命周期 -----------------------------------------------------

    def init(self, *, initial_branch: str = "main") -> GitResult:
        """初始化仓库（重复调用安全）。"""
        if self.is_repo():
            return self.run("rev-parse", "--short", "HEAD")
        return self.run("init", "-b", initial_branch, check=True)

    def config(self, key: str, value: str, *, local: bool = True) -> GitResult:
        args = ["config"]
        if local:
            args.append("--local")
        return self.run(*args, key, value, check=True)

    def ensure_identity(self, name: str = "TCIDE", email: str = "tcide@example.com") -> None:
        """保证有提交身份（测试与首次使用都需要）。"""
        self.config("user.name", name)
        self.config("user.email", email)
        self.config("core.autocrlf", "false")

    # -- 状态 -------------------------------------------------------------

    def status(self) -> GitStatus:
        """解析 ``--porcelain=v1 -z --branch``。"""
        self.require_repo()
        result = self.run("status", "--porcelain=v1", "-z", "--branch", check=True)
        status = GitStatus()
        entries = result.stdout.split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if entry.startswith("## "):
                self._parse_branch_line(entry[3:], status)
                continue
            if len(entry) < 4:
                continue
            x, y, path = entry[0], entry[1], entry[3:]
            change = FileChange(path=path, x=x, y=y)
            if x in ("R", "C") or y in ("R", "C"):
                # -z 格式下原路径跟在后面一个 NUL 项
                if index < len(entries) and entries[index]:
                    change.original = entries[index]
                    index += 1
            status.changes.append(change)
        return status

    @staticmethod
    def _parse_branch_line(text: str, status: GitStatus) -> None:
        # 形态：main...origin/main [ahead 1, behind 2] / main / HEAD (no branch)
        # 空仓库时是：No commits yet on main  /  Initial commit on main
        if text.startswith("No commits yet on "):
            status.branch = text[len("No commits yet on "):].strip()
            return
        if text.startswith("Initial commit on "):
            status.branch = text[len("Initial commit on "):].strip()
            return
        head, _, tail = text.partition(" [")
        if "..." in head:
            status.branch, _, status.upstream = head.partition("...")
        else:
            status.branch = head.strip()
        if tail:
            ahead = re.search(r"ahead (\d+)", tail)
            behind = re.search(r"behind (\d+)", tail)
            status.ahead = int(ahead.group(1)) if ahead else 0
            status.behind = int(behind.group(1)) if behind else 0

    def current_branch(self) -> str:
        result = self.run("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip() if result.ok else ""

    def is_clean(self) -> bool:
        return self.status().clean

    # -- 差异 -------------------------------------------------------------

    def diff(self, *, staged: bool = False, path: str | None = None,
             unified: int = 3, stat_only: bool = False) -> str:
        """工作区/暂存区差异。"""
        args = ["diff", f"-U{unified}"]
        if staged:
            args.append("--cached")
        if stat_only:
            args.append("--stat")
        if path:
            args += ["--", path]
        return self.run(*args).stdout

    def diff_stat(self, *, staged: bool = False) -> list[FileStat]:
        """``--numstat`` 解析（二进制文件会显示为 '-'，按 0 计）。"""
        args = ["diff", "--numstat"]
        if staged:
            args.append("--cached")
        result = self.run(*args)
        stats: list[FileStat] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, deleted, path = parts[0], parts[1], "\t".join(parts[2:])
            stats.append(FileStat(
                path=path,
                additions=int(added) if added.isdigit() else 0,
                deletions=int(deleted) if deleted.isdigit() else 0))
        return stats

    def changed_files(self, *, include_untracked: bool = True) -> list[str]:
        """所有变更文件路径（工作区 + 暂存区，可选含未跟踪）。"""
        changes = self.status().changes
        return sorted({c.path for c in changes
                       if include_untracked or not c.untracked})

    # -- 暂存与提交 -------------------------------------------------------

    def stage(self, *paths: str) -> GitResult:
        if not paths:
            return self.run("add", "-A", check=True)
        return self.run("add", "--", *paths, check=True)

    def stage_all(self) -> GitResult:
        return self.run("add", "-A", check=True)

    def unstage(self, *paths: str) -> GitResult:
        args = ["reset", "HEAD", "--"] if not paths else ["reset", "HEAD", "--", *paths]
        return self.run(*args)

    def commit(self, message: str, *, add_all: bool = False,
               allow_empty: bool = False) -> GitResult:
        """提交。``add_all`` 会先 stage 全部变更。"""
        message = (message or "").strip()
        if not message:
            raise ValueError("提交信息不能为空")
        if add_all:
            self.stage_all()
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        return self.run(*args)

    def amend(self, message: str | None = None, *,
              allow_empty: bool = True) -> GitResult:
        """改写最近一次提交。

        默认带 ``--allow-empty``：若原提交本身是空提交（例如只改了信息），
        git 会以 "doing so would make it empty" 拒绝并返回 1 —— 不带上这个参数
        就会出现「改提交信息静默失败」。这是修出来的 bug，有回归测试。
        """
        args = ["commit", "--amend"]
        if allow_empty:
            args.append("--allow-empty")
        if message:
            args += ["-m", message.strip()]
        else:
            args.append("--no-edit")
        return self.run(*args)

    # -- 远程 -------------------------------------------------------------

    def remotes(self) -> dict[str, str]:
        result = self.run("remote", "-v")
        remotes: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in remotes:
                remotes[parts[0]] = parts[1]
        return remotes

    def fetch(self, remote: str = "origin") -> GitResult:
        return self.run("fetch", remote)

    def pull(self, remote: str = "origin") -> GitResult:
        return self.run("pull", "--ff-only", remote)

    def push(self, *, remote: str = "origin", branch: str | None = None,
             set_upstream: bool = False, force: bool = False) -> GitResult:
        args = ["push"]
        if set_upstream:
            args.append("-u")
        if force:
            args.append("--force-with-lease")
        args.append(remote)
        if branch:
            args.append(branch)
        return self.run(*args)

    # -- 分支与历史 -------------------------------------------------------

    def branches(self, *, include_remote: bool = False) -> list[Branch]:
        args = ["branch", "--format=%(refname:short)\t%(HEAD)\t%(upstream:short)\t%(objectname:short)"]
        if include_remote:
            args.append("-a")
        result = self.run(*args)
        current = self.current_branch()
        items: list[Branch] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if not parts or not parts[0].strip():
                continue
            name = parts[0].strip()
            items.append(Branch(
                name=name,
                current=(len(parts) > 1 and parts[1].strip() == "*") or name == current,
                upstream=parts[2].strip() if len(parts) > 2 else "",
                sha=parts[3].strip() if len(parts) > 3 else ""))
        return items

    def checkout(self, ref: str, *, create: bool = False) -> GitResult:
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(ref)
        return self.run(*args, check=True)

    def create_branch(self, name: str, *, checkout: bool = True) -> GitResult:
        return self.checkout(name, create=True) if checkout else self.run(
            "branch", name, check=True)

    def log(self, count: int = 20, *, ref: str = "HEAD",
            path: str | None = None) -> list[Commit]:
        """最近提交（用 \\x1f 分隔字段，避免提交信息里有特殊字符时解析错）。"""
        separator = "\x1f"
        result = self.run("log", f"-{max(1, count)}",
                          f"--pretty=format:%H{separator}%h{separator}%an{separator}"
                          f"%ae{separator}%ad{separator}%s", "--date=short",
                          ref, *(["--", path] if path else []))
        commits: list[Commit] = []
        for line in result.stdout.splitlines():
            parts = line.split(separator)
            if len(parts) < 6:
                continue
            commits.append(Commit(sha=parts[0], short=parts[1], author=parts[2],
                                  email=parts[3], date=parts[4],
                                  subject=separator.join(parts[5:])))
        return commits

    def show(self, ref: str = "HEAD", *, stat_only: bool = False) -> str:
        args = ["show", ref]
        if stat_only:
            args.append("--stat")
        return self.run(*args).stdout

    def file_at(self, ref: str, path: str) -> str:
        """读取某个版本的文件内容（diff 审核用）。"""
        return self.run("show", f"{ref}:{path}").stdout

    def head_sha(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()

    def blame(self, path: str, *, max_lines: int = 2000) -> list[dict]:
        """逐行 blame（``--line-porcelain``）。"""
        result = self.run("blame", "--line-porcelain", "--", path)
        entries: list[dict] = []
        current: dict = {}
        for line in result.stdout.splitlines():
            if re.match(r"^[0-9a-f]{40} ", line):
                parts = line.split()
                current = {"sha": parts[0][:8], "line": int(parts[2]) if len(parts) > 2 else 0}
                entries.append(current)
            elif line.startswith("author ") and current is not None:
                current["author"] = line[7:]
            elif line.startswith("summary ") and current is not None:
                current["summary"] = line[8:]
            elif line.startswith("\t") and current is not None:
                current["text"] = line[1:]
                if len(entries) >= max_lines:
                    break
        return entries

    def churn(self, *, count: int = 200) -> dict[str, int]:
        """文件改动频次（git 历史的真实 churn，替代原版的启发式猜测）。"""
        result = self.run("log", f"-{max(1, count)}", "--name-only",
                          "--pretty=format:")
        counts: dict[str, int] = {}
        for line in result.stdout.splitlines():
            name = line.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def summary(self) -> dict:
        """面板用的一览：分支/是否干净/变更数/最近提交。"""
        try:
            status = self.status()
        except GitError as exc:
            return {"ok": False, "error": str(exc)}
        commits = self.log(1)
        return {
            "ok": True,
            "branch": status.branch,
            "upstream": status.upstream,
            "ahead": status.ahead,
            "behind": status.behind,
            "clean": status.clean,
            "staged": len(status.staged),
            "unstaged": len(status.unstaged),
            "untracked": len(status.untracked),
            "conflicted": len(status.conflicted),
            "head": commits[0].subject if commits else "",
            "remotes": self.remotes(),
        }
