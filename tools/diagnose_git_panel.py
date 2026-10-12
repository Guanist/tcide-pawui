# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""诊断：Git 面板为什么没列出被修改的文件（tests/test_shell.py 里那条用例）。

复刻 fixture 的步骤，然后把**原始 git 输出**与**面板拿到的行**都打出来。
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from tcide.gitops import GitRepo

    scratch = ROOT / "tests" / ".scratch" / f"diag-git-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True)
    repo = GitRepo(scratch)
    print(f"临时仓库: {scratch}")
    print(f"init: {repo.init()}")
    print(f"ensure_identity: {repo.ensure_identity()}")
    (scratch / "src").mkdir()
    (scratch / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (scratch / "README.md").write_text("# demo\n", encoding="utf-8")
    print(f"stage_all: {repo.stage_all()}")
    print(f"commit: {repo.commit('chore: 初始提交')}")
    print(f"提交后 is_clean: {repo.is_clean()}")

    # 关键一步：改一个文件，面板应当列出它
    (scratch / "src" / "app.py").write_text("value = 42\n", encoding="utf-8")
    print()
    print("=== 改动后 GitRepo 的原始视图 ===")
    try:
        status = repo.status()
        print(f"  status() 类型={type(status).__name__}")
        for item in status:
            print(f"    {item}")
    except Exception as exc:      # noqa: BLE001
        print(f"  status() 抛错: {type(exc).__name__}: {exc}")
    print(f"  is_clean(): {repo.is_clean()}")

    print()
    print("=== 界面侧：application bootstrap 之后 ===")
    import main as tcide_main

    app_qt = QApplication.instance() or QApplication([])
    runtime, app = tcide_main.create_runtime(scratch)
    runtime.run(block=False)
    app.bootstrap()
    for _ in range(6):
        app_qt.processEvents()
    print(f"  git_rows        = {list(app.state.git_rows)}")
    print(f"  git_branch_line = {app.state.git_branch_line!r}")
    print(f"  git_hint        = {app.state.git_hint!r}")
    print(f"  有没有 'app.py' = {any('app.py' in row for row in app.state.git_rows)}")

    print()
    print("=== 直接问底层 git status --porcelain ===")
    try:
        out = repo.run(["status", "--porcelain"])
        print(f"  {out!r}")
    except Exception as exc:      # noqa: BLE001
        print(f"  抛错: {type(exc).__name__}: {exc}")

    print()
    print("=== 这个临时仓库是不是落在某个 git 仓库的工作区里 ===")
    probe = scratch
    for _ in range(6):
        if (probe / ".git").exists():
            print(f"  最近的 .git 在 {probe}")
            break
        probe = probe.parent
    else:
        print("  没有找到 .git")

    runtime.root.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
