# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""gitintel.py 的测试（变更聚类 / 风险 / 提交建议 / 热点）。"""

from __future__ import annotations

import pytest

from tcide.gitintel import GitIntelligence
from tcide.gitops import GitRepo
from tcide.indexer import ProjectIndex
from tcide.workspace import Workspace

pytestmark = pytest.mark.skipif(
    not GitRepo(".").available(), reason="环境没有可用的 git")


def write(repo: GitRepo, relative: str, text: str = "x = 1\n") -> None:
    path = repo.path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestClusters:
    def test_cluster_by_top_dir(self, git_repo):
        write(git_repo, "src/a.py")
        write(git_repo, "src/b.py")
        write(git_repo, "docs/guide.md")
        clusters = GitIntelligence(git_repo).clusters()
        names = [c.name for c in clusters]
        assert names[0] == "src"
        src = clusters[0]
        assert src.files == ["src/a.py", "src/b.py"]

    def test_root_files_grouped(self, git_repo):
        write(git_repo, "notes.md")
        clusters = GitIntelligence(git_repo).clusters()
        assert clusters[0].name == "(root)"

    def test_additions_and_deletions_counted(self, git_repo):
        write(git_repo, "src/app.py", "a = 1\nb = 2\nc = 3\n")
        clusters = GitIntelligence(git_repo).clusters()
        assert clusters[0].additions >= 1

    def test_empty_repo_has_no_clusters(self, git_repo):
        assert GitIntelligence(git_repo).clusters() == []


class TestRisks:
    def test_lock_file_flagged(self, git_repo):
        write(git_repo, "package-lock.json", "{}\n")
        risks = GitIntelligence(git_repo).risks()
        assert any("锁文件" in reason for item in risks for reason in item.reasons)

    def test_generated_dir_flagged(self, git_repo):
        write(git_repo, "dist/bundle.js", "var x=1;\n")
        risks = GitIntelligence(git_repo).risks()
        assert any("生成物" in reason for item in risks for reason in item.reasons)

    def test_ci_change_flagged(self, git_repo):
        write(git_repo, ".github/workflows/ci.yml", "name: ci\n")
        risks = GitIntelligence(git_repo).risks()
        assert any(item.path.startswith(".github/") for item in risks)

    def test_conflict_is_high(self, git_repo):
        from tcide.gitintel import GitIntelligence as GI

        class FakeChange:
            path, x, y, kind, conflicted = "f.py", "U", "U", "conflicted", True
            original = ""

        class FakeStatus:
            changes = [FakeChange()]

        class FakeRepo(GitRepo):
            def status(self):  # type: ignore[override]
                return FakeStatus()

            def diff_stat(self, staged: bool = False):  # type: ignore[override]
                return []

        items = GI(FakeRepo(git_repo.path)).risks()
        assert items and items[0].level == "high"

    def test_missing_tests_noted(self, git_repo):
        (git_repo.path / "tests").mkdir()
        write(git_repo, "src/app.py", "value = 2\n")
        risks = GitIntelligence(git_repo).risks()
        assert any(item.path == "(测试)" for item in risks)

    def test_test_change_suppresses_note(self, git_repo):
        (git_repo.path / "tests").mkdir()
        write(git_repo, "src/app.py", "value = 2\n")
        write(git_repo, "tests/test_app.py", "def test_x():\n    assert True\n")
        risks = GitIntelligence(git_repo).risks()
        assert not any(item.path == "(测试)" for item in risks)

    def test_wide_blast_radius_from_index(self, git_repo):
        """被多个文件依赖的模块改动应升级为高风险（用真实索引算依赖数）。"""
        for index in range(6):
            write(git_repo, f"src/user{index}.py", "import core\n\n\ndef f():\n    return 1\n")
        write(git_repo, "src/core.py", "def g():\n    return 1\n")
        git_repo.stage_all()
        git_repo.commit("feat: 初始结构")
        write(git_repo, "src/core.py", "def g():\n    return 2\n")

        workspace = Workspace(git_repo.path)
        index = ProjectIndex(workspace)
        index.scan()
        risks = GitIntelligence(git_repo, index).risks()
        core = [item for item in risks if item.path == "src/core.py"]
        assert core, f"没识别出 core.py 的风险: {[i.path for i in risks]}"
        assert core[0].level == "high"
        assert any("依赖" in reason for reason in core[0].reasons)

    def test_large_diff_flagged(self, git_repo):
        write(git_repo, "src/app.py", "".join(f"line{i} = {i}\n" for i in range(200)))
        risks = GitIntelligence(git_repo).risks(large_diff=100)
        assert any("拆分提交" in reason for item in risks for reason in item.reasons)

    def test_risks_sorted_by_score(self, git_repo):
        write(git_repo, "package-lock.json", "{}\n")
        write(git_repo, "src/app.py", "value = 3\n")
        risks = GitIntelligence(git_repo).risks()
        scores = [item.score for item in risks]
        assert scores == sorted(scores, reverse=True)

    def test_clean_repo_has_no_risks(self, git_repo):
        assert GitIntelligence(git_repo).risks() == []


class TestCommitSuggestion:
    def test_docs_only(self, git_repo):
        write(git_repo, "docs/a.md")
        suggestion = GitIntelligence(git_repo).suggest_commit()
        assert suggestion.type == "docs"
        assert suggestion.confidence >= 0.9

    def test_tests_only(self, git_repo):
        (git_repo.path / "tests").mkdir()
        write(git_repo, "tests/test_new.py", "def test_a():\n    assert 1\n")
        assert GitIntelligence(git_repo).suggest_commit().type == "test"

    def test_ci_only(self, git_repo):
        write(git_repo, ".github/workflows/x.yml", "name: x\n")
        assert GitIntelligence(git_repo).suggest_commit().type == "ci"

    def test_new_source_is_feat(self, git_repo):
        write(git_repo, "src/new_module.py")
        suggestion = GitIntelligence(git_repo).suggest_commit()
        assert suggestion.type == "feat"
        assert suggestion.scope == "src"
        assert "new_module.py" in suggestion.subject

    def test_modification_is_fix(self, git_repo):
        write(git_repo, "src/app.py", "value = 99\n")
        assert GitIntelligence(git_repo).suggest_commit().type == "fix"

    def test_deletion_of_source_is_chore(self, git_repo):
        """删除源码文件：路径不属文档/测试/CI，按变更类型归为 chore。"""
        (git_repo.path / "src" / "app.py").unlink()
        assert GitIntelligence(git_repo).suggest_commit().type == "chore"

    def test_deletion_of_doc_is_docs(self, git_repo):
        """路径优先：删 .md 属于 docs 变更（类型由路径决定，已文档化）。"""
        (git_repo.path / "README.md").unlink()
        assert GitIntelligence(git_repo).suggest_commit().type == "docs"

    def test_standard_message_format(self, git_repo):
        write(git_repo, "src/new_module.py")
        suggestion = GitIntelligence(git_repo).suggest_commit()
        assert suggestion.message.splitlines()[0].startswith("feat(src): ")

    def test_body_lists_files_and_risks(self, git_repo):
        write(git_repo, "src/app.py", "value = 5\n")
        write(git_repo, "package-lock.json", "{}\n")
        suggestion = GitIntelligence(git_repo).suggest_commit()
        assert "src/app.py" in suggestion.body
        assert "注意" in suggestion.body

    def test_no_changes(self, git_repo):
        suggestion = GitIntelligence(git_repo).suggest_commit()
        assert suggestion.type == "chore"
        assert suggestion.subject == "无变更"

    def test_to_dict_serializable(self, git_repo):
        write(git_repo, "src/a.py")
        data = GitIntelligence(git_repo).suggest_commit().to_dict()
        assert set(data) >= {"type", "scope", "subject", "message", "confidence"}


class TestHotspotsAndReport:
    def test_hotspots_from_history(self, git_repo):
        for index in range(3):
            write(git_repo, "src/app.py", f"value = {index + 10}\n")
            git_repo.commit(f"fix: 第{index}次", add_all=True)
        hotspots = GitIntelligence(git_repo).hotspots()
        assert hotspots and hotspots[0][0] == "src/app.py"
        assert hotspots[0][1] >= 3

    def test_report_has_sections(self, git_repo):
        write(git_repo, "src/a.py")
        report = GitIntelligence(git_repo).report()
        assert "# Git 变更摘要" in report
        assert "## 聚类" in report
        assert "## 建议提交信息" in report
        assert "main" in report
