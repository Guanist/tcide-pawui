# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""验证跨线程修复：真跑终端与在线 AI，检查 Qt 跨线程警告是否消失。

用法::

    $env:TCIDE_API_KEY="sk-..."
    python tools/verify_thread_fix.py

判定标准（打印在最后）：
1. stderr 里**不再出现** ``Cannot create children for a parent that is in a
   different thread``；
2. 终端输出与 AI 结果确实回到了界面状态里（说明队列被抽干了）。

为什么必须真跑：单元测试只能证明"工作线程里没调 push"，证明不了
"整条链路在真实事件循环下没有跨线程访问"。
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pump(app_qt, app, seconds: float) -> None:
    """转事件循环，并顺带抽干队列（模拟真实主循环）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app_qt.processEvents()
        app._drain_gui_queue()
        time.sleep(0.02)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    import main as tcide_main

    key = os.environ.get("TCIDE_API_KEY", "").strip()
    app_qt = QApplication.instance() or QApplication([])
    runtime, app = tcide_main.create_runtime(ROOT)
    runtime.run(block=False)
    app.bootstrap()
    pump(app_qt, app, 0.3)

    print(f"GUI 线程 ident = {app._gui_thread_id}")

    # ---- 1. 终端：和真实路径一样，在工作线程里跑命令 ----------------------
    print("\n=== 1) 终端命令（工作线程 → 队列 → GUI 线程）===")
    from tcide.term import TerminalSession

    app._term_session = TerminalSession(app._terminal_runner)
    seen: list[tuple[int, str]] = []
    real_send = app._send_to_terminal

    def spy(message: dict) -> None:
        seen.append((threading.get_ident(), str(message.get("type"))))
        return real_send(message)

    app._send_to_terminal = spy
    worker = threading.Thread(target=app._run_terminal_command, args=("echo thread-check",))
    worker.start()
    worker.join(timeout=30)
    print(f"  工作线程是否还活着: {worker.is_alive()}")
    print(f"  命令跑完时页面还没收到消息: {seen == []}  （应当 True）")
    pump(app_qt, app, 0.6)
    print(f"  抽干后收到的消息: {seen}")
    gui_only = all(ident == app._gui_thread_id for ident, _ in seen)
    print(f"  全部来自 GUI 线程: {gui_only}  （应当 True）")

    # ---- 2. 在线 AI（真调模型）------------------------------------------
    ai_ok = "（跳过：没有 TCIDE_API_KEY）"
    if key:
        print("\n=== 2) 在线 AI（工作线程跑编排 → 队列 → GUI 线程）===")
        app.state.api_key = key
        app._append_chat("你", "只回答两个字：收到")
        t0 = time.time()
        ai_worker = threading.Thread(target=app._online_run, args=("只回答两个字：收到",))
        ai_worker.start()
        ai_worker.join(timeout=120)
        print(f"  编排线程是否还活着: {ai_worker.is_alive()}（耗时 {time.time()-t0:.1f}s）")
        pump(app_qt, app, 1.0)
        tail = app.state.chat_log.strip().splitlines()[-1]
        ai_ok = tail[:120]
        print(f"  对话日志最后一行: {ai_ok}")

    print("\n=== 判定 ===")
    print(f"  终端消息全部在 GUI 线程: {'通过' if gui_only else '★ 失败'}")
    print(f"  AI 结果回到界面: {'通过' if key and '完成' in ai_ok or '失败' in ai_ok else '（未测）'}")
    print("  跨线程警告检查请看本进程的 stderr（由外部命令 grep）")
    runtime.root.close()
    return 0 if gui_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
