# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Git 智能：变更聚类、风险识别、提交信息建议、热点分析。

对应 TCIDE 原版的 `git/git-intelligence.ts` —— **原版从未接线，功能静默失效**
（见 `src/main/ipc-handlers.ts:43` 的作者注释）。这里把它真正做出来，并且：
- 每个风险判断都写清理由（可解释，不是"AI 说有风险"）；
- 提交信息建议遵循 Conventional Commits，类型由**路径**决定，可预测、可断言；
- "热点"用 git 历史的**真实改动频次**，而不是启发式猜测。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .gitops import GitRepo

#: 不该手改的生成物/锁文件（改了要提醒）
LOCK_FILES: tuple[str, ...] = (
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock",
    "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "Pipfile.lock",
)

#: 生成物目录
GENERATED_DIRS: tuple[str, ...] = ("dist/", "build/", "out/", "release/", "coverage/")

#: 只放文档/测试/配置的路径判定
DOC_PATTERNS = (r"\.md$", r"\.rst$", r"\.txt$", r"^docs/", r"^doc/")
TEST_PATTERNS = (r"(^|/)tests?/", r"(^|/)test_[^/]+\.py$", r"_test\.(py|go)$",
                 r"\.test\.(ts|tsx|js|jsx)$", r"\.spec\.(ts|tsx|js|jsx)$")
CI_PATTERNS = (r"^\.github/", r"^\.gitlab-ci", r"^Jenkinsfile", r"^azure-pipelines")
CONFIG_PATTERNS = (r"^\.editorconfig$", r"^\.gitignore$", r"^Dockerfile",
                   r"\.ya?ml$", r"\.toml$", r"^\.env", r"^Makefile$")
SOURCE_PATTERNS = (r"\.py$", r"\.ts$", r"\.tsx$", r"\.js$", r"\.jsx$", r"\.go$",
                   r"\.rs$", r"\.java$", r"\.kt$", r"\.c$", r"\.cc$", r"\.cpp$",
                   r"\.h$", r"\.hpp$", r"\.cs$", r"\.rb$", r"\.php$", r"\.swift$")


def _matches(path: str, patterns) -> bool:
    return any(re.search(pattern, path, re.IGNORECASE) for pattern in patterns)


@dataclass
class ChangeCluster:
    """按顶层目录/模块聚类的变更。"""

    name: str
    files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.additions + self.deletions

    def to_dict(self) -> dict:
        return {"name": self.name, "files": self.files, "additions": self.additions,
                "deletions": self.deletions, "kinds": self.kinds, "total": self.total}


@dataclass
class RiskItem:
    """一个需要注意的变更点。"""

    path: str
    level: str          # high / medium / low
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": self.path, "level": self.level,
                "score": round(self.score, 2), "reasons": self.reasons}


@dataclass
class CommitSuggestion:
    """提交信息建议。"""

    type: str
    scope: str
    subject: str
    body: str = ""
    confidence: float = 0.5

    @property
    def message(self) -> str:
        head = f"{self.type}({self.scope}): {self.subject}" if self.scope \
            else f"{self.type}: {self.subject}"
        return f"{head}\n\n{self.body}".rstrip() if self.body else head

    def to_dict(self) -> dict:
        return {"type": self.type, "scope": self.scope, "subject": self.subject,
                "body": self.body, "confidence": round(self.confidence, 2),
                "message": self.message}


class GitIntelligence:
    """对一个 :class:`GitRepo` 做变更理解。"""

    def __init__(self, repo: GitRepo, index=None) -> None:
        self.repo = repo
        self.index = index   # 可选：ProjectIndex，用来算"被依赖数"

    # -- 聚类 -------------------------------------------------------------

    @staticmethod
    def cluster_name(path: str) -> str:
        """顶层目录名；根目录文件归到 ``(root)``。"""
        parts = path.split("/")
        return parts[0] if len(parts) > 1 else "(root)"

    def clusters(self, *, staged: bool = False) -> list[ChangeCluster]:
        """把变更按顶层目录聚类，附增删行数。"""
        groups: dict[str, ChangeCluster] = {}
        stats = {item.path: item for item in self.repo.diff_stat(staged=staged)}
        for change in self.repo.status().changes:
            name = self.cluster_name(change.path)
            cluster = groups.setdefault(name, ChangeCluster(name=name))
            cluster.files.append(change.path)
            cluster.kinds[change.kind] = cluster.kinds.get(change.kind, 0) + 1
            item = stats.get(change.path)
            if item:
                cluster.additions += item.additions
                cluster.deletions += item.deletions
        for cluster in groups.values():
            cluster.files.sort()
        return sorted(groups.values(), key=lambda c: (-len(c.files), c.name))

    # -- 风险 -------------------------------------------------------------

    def _dependent_count(self, path: str) -> int:
        if self.index is None:
            return 0
        graph = self.index.dependency_graph()
        return sum(1 for _importer, deps in graph.items() if path in deps)

    def risks(self, *, staged: bool = False, large_diff: int = 300) -> list[RiskItem]:
        """逐条给出风险与理由（可解释）。"""
        changes = self.repo.status().changes
        stats = {item.path: item for item in self.repo.diff_stat(staged=staged)}
        items: list[RiskItem] = []
        for change in changes:
            reasons: list[str] = []
            score = 0.0
            path = change.path

            if change.conflicted:
                reasons.append("存在合并冲突")
                score += 5.0
            if any(path.endswith(name) or path.endswith("/" + name)
                   for name in LOCK_FILES):
                reasons.append("锁文件被改动（通常应由包管理器生成，手改易出错）")
                score += 2.0
            if path.startswith(GENERATED_DIRS):
                reasons.append("位于生成物目录（产物不应手改或提交）")
                score += 2.0
            if _matches(path, CI_PATTERNS):
                reasons.append("CI 配置改动会影响流水线")
                score += 1.5
            elif _matches(path, CONFIG_PATTERNS):
                reasons.append("配置/构建文件改动影响面较广")
                score += 1.0

            dependents = self._dependent_count(path)
            if dependents >= 5:
                reasons.append(f"被 {dependents} 个文件依赖，改动影响面大")
                score += 3.0
            elif dependents >= 1:
                reasons.append(f"被 {dependents} 个文件依赖")
                score += 1.0
            if change.kind == "deleted" and dependents:
                reasons.append("删除的文件仍被其他文件引用")
                score += 2.0

            item = stats.get(path)
            if item and item.total >= large_diff:
                reasons.append(f"改动 {item.total} 行，建议拆分提交")
                score += 1.5

            if not reasons:
                continue
            level = "high" if score >= 3.0 else ("medium" if score >= 1.5 else "low")
            items.append(RiskItem(path=path, level=level, score=score, reasons=reasons))

        if changes and self._has_tests() and not any(
                _matches(c.path, TEST_PATTERNS) for c in changes):
            source_changed = any(_matches(c.path, SOURCE_PATTERNS) for c in changes)
            if source_changed:
                items.append(RiskItem(
                    path="(测试)", level="low", score=0.5,
                    reasons=["改了源码但没改测试，确认是否需要补测试"]))
        return sorted(items, key=lambda i: (-i.score, i.path))

    def _has_tests(self) -> bool:
        try:
            return bool(self.repo.path.glob("tests")) or bool(self.repo.path.glob("test"))
        except OSError:
            return False

    # -- 提交信息建议 -----------------------------------------------------

    def suggest_commit(self, *, staged: bool = False) -> CommitSuggestion:
        """按路径决定 Conventional Commits 类型；类型可预测，便于断言。"""
        changes = self.repo.status().changes
        if not changes:
            return CommitSuggestion(type="chore", scope="", subject="无变更",
                                    confidence=1.0)
        paths = [c.path for c in changes]
        kinds = {c.kind for c in changes}
        clusters = self.clusters(staged=staged)
        scope = clusters[0].name if clusters and clusters[0].name != "(root)" else ""

        if all(_matches(p, DOC_PATTERNS) for p in paths):
            commit_type, confidence = "docs", 0.95
        elif all(_matches(p, TEST_PATTERNS) for p in paths):
            commit_type, confidence = "test", 0.95
        elif all(_matches(p, CI_PATTERNS) for p in paths):
            commit_type, confidence = "ci", 0.9
        elif kinds <= {"untracked"} or kinds <= {"added"}:
            commit_type, confidence = "feat", 0.8
        elif kinds <= {"deleted"}:
            commit_type, confidence = "chore", 0.75
        elif kinds <= {"renamed"}:
            commit_type, confidence = "refactor", 0.75
        elif all(_matches(p, CONFIG_PATTERNS) for p in paths):
            commit_type, confidence = "chore", 0.8
        else:
            commit_type, confidence = "fix", 0.6

        added = sorted(c.path.split("/")[-1] for c in changes
                       if c.kind in ("added", "untracked"))
        if added and len(added) <= 3:
            subject = "新增 " + "、".join(added)
        elif len(changes) == 1:
            subject = f"更新 {changes[0].path.split('/')[-1]}"
        else:
            subject = f"更新 {len(changes)} 个文件"

        lines = [f"- {c.kind}: {c.path}" for c in sorted(changes, key=lambda c: c.path)]
        risky = [item for item in self.risks(staged=staged) if item.level != "low"]
        if risky:
            lines.append("")
            lines.append("注意：")
            lines.extend(f"- {item.path}: {'；'.join(item.reasons)}" for item in risky)
        return CommitSuggestion(type=commit_type, scope=scope, subject=subject,
                                body="\n".join(lines), confidence=confidence)

    # -- 热点与报告 -------------------------------------------------------

    def hotspots(self, *, count: int = 200, top: int = 10) -> list[tuple[str, int]]:
        """改动最频繁的文件（真实 churn）。"""
        churn = self.repo.churn(count=count)
        return list(churn.items())[:top]

    def report(self, *, staged: bool = False) -> str:
        """Markdown 摘要（给 AI 面板显示用）。"""
        status = self.repo.status()
        lines = [f"# Git 变更摘要", ""]
        lines.append(f"- 分支：`{status.branch or '(未知)'}`"
                     + (f" → `{status.upstream}`" if status.upstream else ""))
        if status.ahead or status.behind:
            lines.append(f"- 领先 {status.ahead} / 落后 {status.behind}")
        lines.append(f"- 变更：{len(status.staged)} 已暂存 / "
                     f"{len(status.unstaged)} 未暂存 / {len(status.untracked)} 未跟踪")
        clusters = self.clusters(staged=staged)
        if clusters:
            lines += ["", "## 聚类"]
            for cluster in clusters:
                lines.append(f"- **{cluster.name}**：{len(cluster.files)} 个文件 "
                             f"(+{cluster.additions}/-{cluster.deletions})")
        risks = self.risks(staged=staged)
        if risks:
            lines += ["", "## 风险"]
            for item in risks:
                lines.append(f"- [{item.level}] `{item.path}`：{'；'.join(item.reasons)}")
        suggestion = self.suggest_commit(staged=staged)
        lines += ["", "## 建议提交信息", "", "```", suggestion.message, "```"]
        return "\n".join(lines)
