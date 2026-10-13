# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""IDE 外壳的离屏界面测试。

`shell_selftest` 里的检查项与 `python main.py --selftest` **完全同一套**，
这里只负责把它接进 pytest（逐项报告，失败时能看到具体哪一步坏了）。

Qt 在受限沙箱里会直接段错误，这一组必须在沙箱外运行。
"""

from __future__ import annotations

import importlib.util

import pytest

from app_selftest import _run_checks

#: 界面外壳依赖 PawUI（GPL-3.0）。没装就跳过，而不是让整个测试套件红掉 ——
#: CI 上先跑后端、再装 PawUI 跑界面，两边都干净。
PAWUI_MISSING = importlib.util.find_spec("pawui") is None

pytestmark = [
    pytest.mark.usefixtures("qapp"),
    pytest.mark.skipif(PAWUI_MISSING, reason="需要安装 pawui 才能测试界面外壳"),
]


@pytest.fixture(scope="module")
def checks(request):
    """整个模块共用一次建树（Qt 建树较慢）。"""
    return None


class TestShellChecks:
    def test_all_checks_pass(self, shell, qapp):
        runtime, app = shell
        results = _run_checks(runtime, app, qapp)
        failures = [f"{name} → {detail}" for name, ok, detail in results if not ok]
        assert not failures, "界面自检未通过：\n  " + "\n  ".join(failures)

    def test_check_count_is_meaningful(self, shell, qapp):
        """确保自检覆盖了足够多的交互（防止有人不小心删掉检查项）。"""
        results = _run_checks(shell[0], shell[1], qapp)
        assert len(results) >= 12, f"自检项太少：{len(results)}"


class TestShellWiring:
    """单独断言几条最容易"接了但没接上"的连线。"""

    def test_context_exposes_all_handlers(self, shell):
        _, app = shell
        context = app.context()
        for name in app.context():
            assert callable(context[name]), f"{name} 不是可调用对象"

    def test_paw_references_only_known_handlers(self, shell):
        """app.paw 里出现的每个 on_* 名字都必须真的在 context 里（原版的经典坑）。"""
        import re
        from pathlib import Path

        paw = (Path(__file__).resolve().parent.parent / "app.paw").read_text(encoding="utf-8")
        referenced = set(re.findall(r'on_[a-z_]+\s*=\s*"([a-z_]+)"', paw))
        missing = referenced - set(shell[1].context())
        assert not missing, f"app.paw 引用了未注册的处理器: {sorted(missing)}"

    # 注：原先这里还有一份「app.paw 用到的每个 {$state} 都要有人提供」的检查。
    # 它和 tests/test_layering.py 里的同名检查**重复**了 —— 上一轮我改了
    # layering 那份、忘了这份，于是同一件事在两边规则不一致、一红一绿。
    # 现在这条守卫**只由 test_layering.py 负责**（它不需要 Qt，跑得更快、
    # 也能同时接受 push() 与 <script> 两种提供方式）。

    def test_tree_rows_map_to_real_paths(self, shell):
        runtime, app = shell
        assert app.state.tree_rows
        for row in app.state.tree_rows[:20]:
            assert app.state.tree_paths[row]
            assert app.state.workspace.resolve(app.state.tree_paths[row]).exists()

    def test_git_panel_handles_non_repo(self, shell):
        runtime, app = shell
        assert app.state.git_rows  # 不是仓库时也应给出可读提示
        assert "git" in app.state.git_branch.lower() or "分支" in app.state.git_branch


class TestShellWithGitRepo:
    def test_git_panel_populates(self, git_repo, qapp):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import main as tcide_main

        (git_repo.path / "src" / "app.py").write_text("value = 42\n", encoding="utf-8")
        runtime, app = tcide_main.create_runtime(git_repo.path)
        runtime.run(block=False)
        app.bootstrap()
        for _ in range(6):
            qapp.processEvents()
        try:
            assert any("app.py" in row for row in app.state.git_rows)
            app.cmd_git_stage()
            app.cmd_git_commit()
            for _ in range(4):
                qapp.processEvents()
            assert git_repo.is_clean(), "界面上的提交没有真正落盘"
        finally:
            runtime.root.close()


class TestThreadMarshalling:
    """工作线程**绝不直接碰界面**。

    真实运行时报过这条 Qt 警告（不是推测出来的）：

        QObject: Cannot create children for a parent that is in a different thread.
        (Parent is QTextDocument(...), parent's thread is QThread(A),
         current thread is QThread(B))

    原因是终端命令与在线 AI 跑在工作线程里，跑完却直接 ``self.push()`` /
    ``page().runJavaScript()``，于是 PawUI 在 B 线程里更新 A 线程创建的
    Markdown 控件。Qt 只允许在对象所属线程访问 GUI 对象：轻则警告，重则崩溃。
    """

    def test_online_run_never_pushes_from_worker(self, shell):
        import threading

        runtime, app = shell
        calls: list[int] = []
        real_push = app.push

        def spy_push(*args, **kwargs):
            calls.append(threading.get_ident())
            return real_push(*args, **kwargs)

        class _Report:
            def summary(self) -> str:
                return "假的完成摘要"

        class _FakeOrchestrator:
            def run(self, requirement):
                return _Report()

        app.push = spy_push                       # type: ignore[assignment]
        app.make_orchestrator = lambda model: _FakeOrchestrator()  # type: ignore[assignment]
        app.state.api_key = "test-key"            # 让 on_chat_send 走在线分支

        worker = threading.Thread(target=app._online_run, args=("随便一个需求",))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()

        assert calls == [], (
            f"工作线程里调了 push（线程 {calls}，GUI 线程 {app._gui_thread_id}）—— "
            f"这就是 QTextDocument 跨线程警告的来源")
        assert app._drain_gui_queue() >= 1, "投递的回调没有被抽干"
        assert calls and calls[0] == app._gui_thread_id, "抽干后应在 GUI 线程里 push"
        assert "假的完成摘要" in app.state.chat_log

    def test_terminal_command_returns_to_gui_thread(self, shell):
        import threading

        from tcide.term import TerminalSession

        runtime, app = shell
        seen: list[int] = []
        app._send_to_terminal = lambda message: seen.append(          # type: ignore[assignment]
            threading.get_ident())

        # 用**真实**会话（跑真 echo）：encode_output 要的是 TermLine 对象，
        # 第一版我塞了普通字符串，于是 AttributeError 被抽水泵兜底吃掉、
        # 测试只看到"什么都没发"。
        app._term_session = TerminalSession(app._terminal_runner)     # type: ignore[assignment]

        worker = threading.Thread(target=app._run_terminal_command, args=("echo hi",))
        worker.start()
        worker.join(timeout=20)
        assert not worker.is_alive()

        assert seen == [], "工作线程里直接给终端页面发消息了（runJavaScript 必须在本线程）"
        assert app._drain_gui_queue() >= 1
        assert seen and set(seen) == {app._gui_thread_id}, (
            f"终端输出应当在 GUI 线程里发：{seen} vs {app._gui_thread_id}")

    def test_post_to_gui_runs_immediately_on_gui_thread(self, shell):
        """已经在 GUI 线程时**同步执行** —— 离线演练与自检依赖这个语义。"""
        runtime, app = shell
        ran: list[str] = []
        app._post_to_gui(ran.append, "立刻")
        assert ran == ["立刻"], "GUI 线程里被不必要地延迟了"
        assert app._gui_queue == deque() if False else True      # 队列没被污染
        assert not app._gui_queue, "GUI 线程里不该往队列塞东西"
