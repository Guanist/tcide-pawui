# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""gitops.py 的测试（真实临时仓库）。"""

from __future__ import annotations

import pytest

from tcide.gitops import GitError, GitRepo

pytestmark = pytest.mark.skipif(
    not GitRepo(".").available(), reason="环境没有可用的 git")


class TestBasics:
    def test_is_repo_true(self, git_repo):
        assert git_repo.is_repo()

    def test_is_repo_false_for_plain_dir(self, plain_dir):
        assert not GitRepo(plain_dir).is_repo()

    def test_require_repo_raises(self, plain_dir):
        with pytest.raises(GitError):
            GitRepo(plain_dir).require_repo()

    def test_root_is_repo_path(self, git_repo):
        assert git_repo.root() == git_repo.path

    def test_run_captures_output(self, git_repo):
        result = git_repo.run("--version")
        assert result.ok and "git version" in result.stdout

    def test_run_check_raises_on_failure(self, git_repo):
        with pytest.raises(GitError):
            git_repo.run("rev-parse", "--verify", "nope-nope", check=True)

    def test_unavailable_git_raises(self, git_repo):
        """git 不可用时要明确报错，而不是静默返回空结果。"""
        repo = GitRepo(git_repo.path, git="definitely-not-a-git-binary")
        assert repo.available() is False
        assert repo.is_repo() is False
        with pytest.raises(GitError):
            repo.run("status")

    def test_timeout_argument_is_honored(self, git_repo):
        assert GitRepo(git_repo.path, timeout=7).timeout == 7

    def test_no_interactive_prompts(self, git_repo):
        """必须带上非交互环境变量，否则凭据提示会把界面挂住。"""
        from tcide.gitops import NONINTERACTIVE_ENV

        assert NONINTERACTIVE_ENV["GIT_TERMINAL_PROMPT"] == "0"
        assert git_repo.run("config", "--get", "user.name").ok


class TestStatus:
    def test_clean_after_commit(self, git_repo):
        status = git_repo.status()
        assert status.clean
        assert status.branch == "main"

    def test_untracked_detected(self, git_repo):
        (git_repo.path / "new.txt").write_text("x", encoding="utf-8")
        status = git_repo.status()
        assert not status.clean
        assert [c.path for c in status.untracked] == ["new.txt"]
        assert status.untracked[0].kind == "untracked"

    def test_staged_detected(self, git_repo):
        (git_repo.path / "new.txt").write_text("x", encoding="utf-8")
        git_repo.stage("new.txt")
        status = git_repo.status()
        assert [c.path for c in status.staged] == ["new.txt"]
        assert status.staged[0].kind == "added"

    def test_modified_detected(self, git_repo):
        (git_repo.path / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        status = git_repo.status()
        assert status.unstaged and status.unstaged[0].kind == "modified"

    def test_deleted_detected(self, git_repo):
        (git_repo.path / "README.md").unlink()
        status = git_repo.status()
        assert status.unstaged[0].kind == "deleted"

    def test_renamed_detected(self, git_repo):
        git_repo.run("mv", "src/app.py", "src/main.py", check=True)
        status = git_repo.status()
        renamed = [c for c in status.changes if c.kind == "renamed"]
        assert renamed and renamed[0].original == "src/app.py"

    def test_non_ascii_path_not_escaped(self, git_repo):
        (git_repo.path / "中文文件.py").write_text("x = 1\n", encoding="utf-8")
        paths = [c.path for c in git_repo.status().changes]
        assert "中文文件.py" in paths, f"路径被转义了: {paths}"

    def test_empty_repo_branch_parsed(self, plain_dir):
        repo = GitRepo(plain_dir)
        repo.init()
        status = repo.status()
        assert status.branch == "main"
        assert status.clean

    def test_status_to_dict(self, git_repo):
        data = git_repo.status().to_dict()
        assert set(data) >= {"branch", "clean", "changes", "ahead", "behind"}


class TestDiffAndCommit:
    def test_diff_shows_change(self, git_repo):
        (git_repo.path / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        assert "-value = 1" in git_repo.diff()
        assert "+value = 2" in git_repo.diff()

    def test_diff_staged(self, git_repo):
        (git_repo.path / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        assert git_repo.diff(staged=True) == ""
        git_repo.stage_all()
        assert "+value = 2" in git_repo.diff(staged=True)

    def test_diff_stat(self, git_repo):
        (git_repo.path / "src" / "app.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        stats = git_repo.diff_stat()
        assert stats and stats[0].path == "src/app.py"
        assert stats[0].additions >= 1 and stats[0].total >= 1

    def test_commit_cleans_tree(self, git_repo):
        (git_repo.path / "a.txt").write_text("a", encoding="utf-8")
        result = git_repo.commit("feat: 加个文件", add_all=True)
        assert result.ok
        assert git_repo.status().clean

    def test_commit_requires_message(self, git_repo):
        with pytest.raises(ValueError):
            git_repo.commit("   ")

    def test_commit_without_staged_fails(self, git_repo):
        result = git_repo.commit("chore: 空")
        assert not result.ok
        assert "nothing" in (result.stderr + result.stdout).lower()

    def test_allow_empty(self, git_repo):
        assert git_repo.commit("chore: 空提交", allow_empty=True).ok

    def test_unstage(self, git_repo):
        (git_repo.path / "a.txt").write_text("a", encoding="utf-8")
        git_repo.stage_all()
        assert git_repo.status().staged
        git_repo.unstage()
        assert not git_repo.status().staged

    def test_amend(self, git_repo):
        git_repo.commit("chore: 空提交", allow_empty=True)
        git_repo.amend("chore: 改写信息")
        assert git_repo.log(1)[0].subject == "chore: 改写信息"

    def test_amend_empty_commit_does_not_fail_silently(self, git_repo):
        """回归：改写空提交的信息时，git 会拒绝执行，必须带 --allow-empty。"""
        git_repo.commit("chore: 空提交", allow_empty=True)
        assert git_repo.amend("chore: 还是空提交").ok

    def test_amend_without_allow_empty_reports_failure(self, git_repo):
        """不带 --allow-empty 时失败必须能被调用方看见（result.ok=False）。"""
        git_repo.commit("chore: 空提交", allow_empty=True)
        result = git_repo.amend("chore: x", allow_empty=False)
        assert not result.ok
        assert "empty" in (result.stderr + result.stdout).lower()

    def test_amend_keeps_message_when_none(self, git_repo):
        git_repo.commit("chore: 保持不变", allow_empty=True)
        git_repo.amend()
        assert git_repo.log(1)[0].subject == "chore: 保持不变"


class TestHistoryAndBranches:
    def test_log(self, git_repo):
        git_repo.commit("feat: 第二个提交", allow_empty=True)
        commits = git_repo.log(5)
        assert len(commits) == 2
        assert commits[0].subject == "feat: 第二个提交"
        assert len(commits[0].sha) == 40

    def test_log_limit(self, git_repo):
        for index in range(3):
            git_repo.commit(f"chore: {index}", allow_empty=True)
        assert len(git_repo.log(2)) == 2

    def test_branches(self, git_repo):
        git_repo.create_branch("feature/x")
        names = {b.name for b in git_repo.branches()}
        assert {"main", "feature/x"} <= names
        assert git_repo.current_branch() == "feature/x"
        assert [b for b in git_repo.branches() if b.current][0].name == "feature/x"

    def test_checkout(self, git_repo):
        git_repo.create_branch("feature/y")
        git_repo.checkout("main")
        assert git_repo.current_branch() == "main"

    def test_changed_files(self, git_repo):
        (git_repo.path / "a.txt").write_text("a", encoding="utf-8")
        (git_repo.path / "src" / "app.py").write_text("value = 9\n", encoding="utf-8")
        assert git_repo.changed_files() == ["a.txt", "src/app.py"]
        assert git_repo.changed_files(include_untracked=False) == ["src/app.py"]

    def test_show_file_at_rev(self, git_repo):
        head = git_repo.head_sha()
        assert git_repo.file_at(head, "src/app.py") == "value = 1\n"
        (git_repo.path / "src" / "app.py").write_text("changed\n", encoding="utf-8")
        assert git_repo.file_at(head, "src/app.py") == "value = 1\n"

    def test_blame(self, git_repo):
        entries = git_repo.blame("src/app.py")
        assert entries and entries[0]["text"] == "value = 1"
        assert entries[0]["author"] == "TCIDE"

    def test_churn(self, git_repo):
        (git_repo.path / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        git_repo.commit("fix: 改一下", add_all=True)
        churn = git_repo.churn()
        assert churn.get("src/app.py", 0) >= 1

    def test_summary(self, git_repo):
        data = git_repo.summary()
        assert data["ok"] and data["branch"] == "main" and data["clean"]

    def test_summary_on_non_repo(self, plain_dir):
        data = GitRepo(plain_dir).summary()
        assert data["ok"] is False and "error" in data


class TestRemotes:
    def test_remotes_empty(self, git_repo):
        assert git_repo.remotes() == {}

    def test_remotes_parsed(self, git_repo):
        git_repo.run("remote", "add", "origin", "https://example.com/x.git", check=True)
        assert git_repo.remotes() == {"origin": "https://example.com/x.git"}

    def test_push_without_remote_fails(self, git_repo):
        result = git_repo.push()
        assert not result.ok
