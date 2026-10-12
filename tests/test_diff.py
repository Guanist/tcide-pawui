# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""diff.py 的测试：生成 / 解析 / 按 hunk 应用 / 审核会话。

重点验证 ``acceptDiff`` 的**部分接受**语义，以及"接受之前磁盘不变"这条前提。
"""

from __future__ import annotations

import pytest

from tcide.diff import (
    ChangeProposal,
    FilePatch,
    Hunk,
    ReviewSession,
    ReviewingWorkspace,
    apply_patch,
    apply_selected,
    diff_stats,
    parse_unified,
    unified_diff,
)

OLD = "one\ntwo\nthree\nfour\nfive\nsix\n"
NEW = "one\nTWO\nthree\nfour\nFIVE\nsix\n"


class TestUnifiedDiff:
    def test_basic_shape(self):
        text = unified_diff("a\nb\n", "a\nc\n", path="x.py")
        assert text.startswith("--- a/x.py")
        assert "+++ b/x.py" in text
        assert "@@" in text
        assert "-b" in text and "+c" in text

    def test_identical_inputs_give_empty(self):
        assert unified_diff("same\n", "same\n") == ""

    def test_stats(self):
        text = unified_diff(OLD, NEW, path="f.txt")
        data = diff_stats(text)
        assert data == {"files": 1, "added": 2, "removed": 2}

    def test_context_lines_included(self):
        text = unified_diff(OLD, NEW, path="f.txt")
        assert " three" in text

    def test_path_only_used_in_header(self):
        assert "a/whatever.txt" in unified_diff("a\n", "b\n", path="whatever.txt")


class TestParseUnified:
    def test_roundtrip_single_file(self):
        text = unified_diff(OLD, NEW, path="f.txt")
        patches = parse_unified(text)
        assert len(patches) == 1
        patch = patches[0]
        assert patch.path == "f.txt"
        assert patch.hunks
        assert patch.added == 2 and patch.removed == 2

    def test_multiple_files(self):
        text = (unified_diff("a\n", "b\n", path="one.txt")
                + unified_diff("x\n", "y\n", path="two.txt"))
        patches = parse_unified(text)
        assert [patch.path for patch in patches] == ["one.txt", "two.txt"]

    def test_multiple_hunks_in_one_file(self):
        old = "".join(f"line{i}\n" for i in range(40))
        new_lines = [f"line{i}\n" for i in range(40)]
        new_lines[2] = "CHANGED2\n"
        new_lines[35] = "CHANGED35\n"
        patches = parse_unified(unified_diff(old, "".join(new_lines), path="big.txt"))
        assert len(patches[0].hunks) == 2

    def test_hunk_counts(self):
        text = unified_diff(OLD, NEW, path="f.txt")
        hunk = parse_unified(text)[0].hunks[0]
        assert hunk.old_count == hunk.new_count == 6
        assert hunk.added == 2 and hunk.removed == 2

    def test_new_file(self):
        text = ("diff --git a/new.py b/new.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/new.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+x = 1\n"
                "+y = 2\n")
        patch = parse_unified(text)[0]
        assert patch.is_new is True and patch.path == "new.py"

    def test_deleted_file(self):
        text = ("deleted file mode 100644\n"
                "--- a/gone.py\n"
                "+++ /dev/null\n"
                "@@ -1,2 +0,0 @@\n"
                "-x = 1\n"
                "-y = 2\n")
        patch = parse_unified(text)[0]
        assert patch.is_deleted is True

    def test_no_newline_marker(self):
        text = ("--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
                "\\ No newline at end of file\n")
        hunk = parse_unified(text)[0].hunks[0]
        assert hunk.no_newline_at_end is True

    def test_git_metadata_ignored(self):
        text = ("diff --git a/f.py b/f.py\n"
                "index 1234567..89abcde 100644\n"
                "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n")
        patches = parse_unified(text)
        assert len(patches) == 1 and patches[0].hunks

    def test_empty_and_garbage(self):
        assert parse_unified("") == []
        assert parse_unified("就是一段普通文字\n") == []

    def test_render_roundtrip(self):
        patch = parse_unified(unified_diff(OLD, NEW, path="f.txt"))[0]
        again = parse_unified(patch.render())[0]
        assert again.to_dict() == patch.to_dict()


class TestApplySelected:
    def test_apply_all(self):
        patch = parse_unified(unified_diff(OLD, NEW, path="f.txt"))[0]
        assert apply_patch(OLD, patch) == NEW

    def test_apply_none_keeps_original(self):
        patch = parse_unified(unified_diff(OLD, NEW, path="f.txt"))[0]
        assert apply_selected(OLD, patch, []) == OLD

    def test_partial_accept(self):
        """两个改动块只接受第一个 —— 这是 acceptDiff 的核心语义。"""
        old = "".join(f"line{i}\n" for i in range(40))
        lines = [f"line{i}\n" for i in range(40)]
        lines[2] = "CHANGED2\n"
        lines[35] = "CHANGED35\n"
        new = "".join(lines)
        patch = parse_unified(unified_diff(old, new, path="big.txt"))[0]
        assert len(patch.hunks) == 2

        only_first = apply_selected(old, patch, [0])
        assert "CHANGED2" in only_first
        assert "CHANGED35" not in only_first

        only_second = apply_selected(old, patch, [1])
        assert "CHANGED35" in only_second
        assert "CHANGED2" not in only_second

    def test_partial_result_length_is_sane(self):
        old = "".join(f"line{i}\n" for i in range(40))
        lines = [f"line{i}\n" for i in range(40)]
        lines[2] = "A\n"
        lines[35] = "B\n"
        patch = parse_unified(unified_diff(old, "".join(lines), path="b.txt"))[0]
        text = apply_selected(old, patch, [0])
        assert text.count("\n") == 40

    def test_pure_addition(self):
        old = "a\nb\n"
        patch = parse_unified(unified_diff(old, "a\nnew\nb\n", path="f.txt"))[0]
        assert apply_patch(old, patch) == "a\nnew\nb\n"

    def test_pure_deletion(self):
        old = "a\nb\nc\n"
        patch = parse_unified(unified_diff(old, "a\nc\n", path="f.txt"))[0]
        assert apply_patch(old, patch) == "a\nc\n"

    def test_new_file_from_empty(self):
        patch = parse_unified(unified_diff("", "x = 1\n", path="new.py"))[0]
        assert apply_patch("", patch) == "x = 1\n"

    def test_empty_patch_returns_original(self):
        assert apply_patch("a\nb\n", FilePatch(path="f.txt")) == "a\nb\n"

    def test_crlf_file_keeps_lines(self):
        old = "a\r\nb\r\n"
        patch = parse_unified(unified_diff(old, "a\r\nc\r\n", path="f.txt"))[0]
        assert "c" in apply_patch(old, patch)

    @pytest.mark.parametrize("old,new", [
        ("a\n", "b\n"),
        ("a\nb\nc\n", "a\nc\n"),
        ("a\nb\n", "x\na\nb\n"),
        ("", "hello\n"),
        ("one\ntwo\n", "one\ntwo\n"),
    ])
    def test_roundtrip_various(self, old, new):
        patch = parse_unified(unified_diff(old, new, path="f.txt"))
        if not patch:                       # 无差异
            assert old == new
            return
        assert apply_patch(old, patch[0]) == new


class TestChangeProposal:
    def test_diff_and_stats(self, workspace):
        workspace.write("f.txt", OLD)
        session = ReviewSession(workspace)
        proposal = session.propose("f.txt", NEW, source="测试")
        assert proposal.changed is True
        assert proposal.stats["added"] == 2
        assert "+++ b/f.txt" in proposal.diff
        assert proposal.to_dict()["source"] == "测试"

    def test_unchanged_proposal(self, workspace):
        workspace.write("f.txt", OLD)
        proposal = ReviewSession(workspace).propose("f.txt", OLD)
        assert proposal.changed is False
        assert proposal.diff == ""

    def test_new_file_flag(self, workspace):
        proposal = ReviewSession(workspace).propose("brand-new.py", "x = 1\n")
        assert proposal.is_new_file is True and proposal.original == ""

    def test_preview_truncates(self, workspace):
        workspace.write("f.txt", "".join(f"l{i}\n" for i in range(500)))
        proposal = ReviewSession(workspace).propose(
            "f.txt", "".join(f"L{i}\n" for i in range(500)))
        assert "还有" in proposal.preview(limit=20)


class TestReviewSession:
    def test_nothing_is_written_before_accept(self, workspace):
        """核心前提：接受之前磁盘上什么都不变。"""
        workspace.write("f.txt", OLD)
        session = ReviewSession(workspace)
        session.propose("f.txt", NEW)
        assert workspace.read("f.txt") == OLD

    def test_accept_writes(self, workspace):
        workspace.write("f.txt", OLD)
        session = ReviewSession(workspace)
        session.propose("f.txt", NEW)
        assert session.accept("f.txt") == 1
        assert workspace.read("f.txt") == NEW
        assert session.applied == ["f.txt"]
        assert not session.has_pending()

    def test_reject_discards(self, workspace):
        workspace.write("f.txt", OLD)
        session = ReviewSession(workspace)
        session.propose("f.txt", NEW)
        assert session.reject("f.txt") == 1
        assert workspace.read("f.txt") == OLD
        assert session.discarded == ["f.txt"]

    def test_accept_all(self, workspace):
        workspace.write("a.txt", "1\n")
        workspace.write("b.txt", "2\n")
        session = ReviewSession(workspace)
        session.propose("a.txt", "1x\n")
        session.propose("b.txt", "2x\n")
        assert session.accept() == 2
        assert workspace.read("a.txt") == "1x\n"
        assert workspace.read("b.txt") == "2x\n"

    def test_partial_hunk_accept_writes_merged_text(self, workspace):
        old = "".join(f"line{i}\n" for i in range(40))
        workspace.write("big.txt", old)
        lines = [f"line{i}\n" for i in range(40)]
        lines[2] = "CHANGED2\n"
        lines[35] = "CHANGED35\n"
        session = ReviewSession(workspace)
        session.propose("big.txt", "".join(lines))
        assert session.accept("big.txt", selected_hunks=[0]) == 1
        result = workspace.read("big.txt")
        assert "CHANGED2" in result and "CHANGED35" not in result

    def test_propose_replaces_previous_intent(self, workspace):
        workspace.write("f.txt", "a\n")
        session = ReviewSession(workspace)
        session.propose("f.txt", "b\n")
        session.propose("f.txt", "c\n")
        assert len(session.pending()) == 1
        session.accept()
        assert workspace.read("f.txt") == "c\n"

    def test_accept_twice_is_noop(self, workspace):
        workspace.write("f.txt", "a\n")
        session = ReviewSession(workspace)
        session.propose("f.txt", "b\n")
        assert session.accept("f.txt") == 1
        assert session.accept("f.txt") == 0

    def test_clear(self, workspace):
        workspace.write("a.txt", "1\n")
        session = ReviewSession(workspace)
        session.propose("a.txt", "2\n")
        assert session.clear() == 1
        assert not session.has_pending()

    def test_totals_and_report(self, workspace):
        workspace.write("f.txt", OLD)
        session = ReviewSession(workspace)
        session.propose("f.txt", NEW, source="AI")
        totals = session.totals()
        assert totals["pending"] == 1 and totals["added"] == 2
        report = session.report()
        assert "待审核 1 个文件" in report and "来源：AI" in report

    def test_report_empty(self):
        assert ReviewSession().report() == "没有待审核的改动"

    def test_requires_workspace_to_accept(self):
        session = ReviewSession()
        session.propose("f.txt", "x\n")
        with pytest.raises(ValueError):
            session.accept()

    def test_to_dict(self, workspace):
        workspace.write("f.txt", "a\n")
        session = ReviewSession(workspace)
        session.propose("f.txt", "b\n")
        data = session.to_dict()
        assert data["totals"]["pending"] == 1
        assert data["proposals"][0]["path"] == "f.txt"

    def test_binary_proposal_writes_bytes(self, workspace):
        session = ReviewSession(workspace)
        session.propose_bytes("logo.png", b"\x89PNG\x00\x01")
        session.accept("logo.png")
        assert workspace.read_bytes("logo.png") == b"\x89PNG\x00\x01"


class TestHunkHelpers:
    def test_new_and_old_lines(self):
        hunk = Hunk(1, 3, 1, 3, [" ctx", "-gone", "+added"])
        assert hunk.new_lines == ["ctx", "added"]
        assert hunk.old_lines == ["ctx", "gone"]

    def test_render(self):
        text = Hunk(1, 1, 1, 1, ["-a", "+b"]).render()
        assert text.startswith("@@ -1,1 +1,1 @@")
        assert "-a" in text and "+b" in text

    def test_file_patch_render_has_headers(self):
        patch = FilePatch(path="f.txt", old_path="f.txt",
                          hunks=[Hunk(1, 1, 1, 1, ["-a", "+b"])])
        text = patch.render()
        assert text.startswith("--- f.txt")
        assert "+++ f.txt" in text


class TestReviewingWorkspace:
    """审核代理：Agent 的写操作不落盘，而是进审核队列。"""

    def test_write_is_intercepted(self, workspace):
        workspace.write("f.txt", "old\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.write("f.txt", "new\n")
        assert workspace.read("f.txt") == "old\n"          # 磁盘没变
        assert [p.path for p in session.pending()] == ["f.txt"]
        assert proxy.intercepted == ["f.txt"]

    def test_accept_through_session_writes(self, workspace):
        workspace.write("f.txt", "old\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.write("f.txt", "new\n")
        session.accept("f.txt")
        assert workspace.read("f.txt") == "new\n"

    def test_reject_keeps_disk(self, workspace):
        workspace.write("f.txt", "old\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.write("f.txt", "new\n")
        session.reject("f.txt")
        assert workspace.read("f.txt") == "old\n"

    def test_disabled_proxy_writes_directly(self, workspace):
        workspace.write("f.txt", "old\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session, enabled=False)
        proxy.write("f.txt", "new\n")
        assert workspace.read("f.txt") == "new\n"
        assert not session.pending()

    def test_enabled_can_be_toggled(self, workspace):
        workspace.write("f.txt", "1\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session, enabled=False)
        proxy.write("f.txt", "2\n")
        proxy.enabled = True
        proxy.write("f.txt", "3\n")
        assert workspace.read("f.txt") == "2\n"
        assert len(session.pending()) == 1

    def test_reads_pass_through(self, workspace):
        workspace.write("f.txt", "content\n")
        proxy = ReviewingWorkspace(workspace, ReviewSession(workspace))
        assert proxy.read("f.txt") == "content\n"
        assert proxy.exists("f.txt") is True
        # iter_files 是生成器，必须物化后才能比较
        assert list(proxy.iter_files()) == list(workspace.iter_files())

    def test_new_file_proposal(self, workspace):
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.write("brand-new.py", "x = 1\n")
        proposal = session.get("brand-new.py")
        assert proposal.is_new_file is True

    def test_binary_write_intercepted(self, workspace):
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.write_bytes("blob.bin", b"\x00\x01")
        assert workspace.exists("blob.bin") is False
        assert session.pending()

    def test_delete_is_intercepted_and_needs_confirmation(self, workspace):
        workspace.write("gone.txt", "bye\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.delete("gone.txt")
        assert workspace.exists("gone.txt") is True       # 没有静默删掉
        assert "删除" in session.get("gone.txt").source

    def test_relative_path_normalised(self, workspace):
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        proxy.write(workspace.resolve("sub/dir/f.txt"), "x\n")
        assert [p.path for p in session.pending()] == ["sub/dir/f.txt"]

    def test_path_escape_still_blocked(self, workspace):
        from tcide.workspace import WorkspaceError

        proxy = ReviewingWorkspace(workspace, ReviewSession(workspace))
        with pytest.raises(WorkspaceError):
            proxy.write("../outside.txt", "x\n")

    def test_tools_can_use_proxy_unmodified(self, workspace):
        """工具层只依赖鸭子类型，所以换代理不需要改 tools.py。

        ``ToolRegistry.call(name, arguments_dict)``。
        """
        from tcide.tools import ToolRegistry

        workspace.write("f.txt", "old\n")
        session = ReviewSession(workspace)
        proxy = ReviewingWorkspace(workspace, session)
        tools = ToolRegistry(proxy)
        result = tools.call("write_file", {"path": "f.txt", "content": "new\n"})
        assert result.ok, result.output
        assert workspace.read("f.txt") == "old\n"          # 没落盘
        assert session.get("f.txt") is not None
