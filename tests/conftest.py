# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""共用的临时项目 fixture。

刻意不用 pytest 的 ``tmp_path``（系统临时目录），而是在工作区内建一个受控目录：
本机沙箱对工作区外/系统临时目录的权限不稳定，早前已经踩过一次。
每个 fixture 用完即删。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tcide.indexer import ProjectIndex
from tcide.memory import ProjectMemoryBuilder
from tcide.workspace import Workspace

SCRATCH = Path(__file__).resolve().parent / ".scratch"


def pytest_configure(config) -> None:
    """把 pytest 自己的临时根重定向到工作区内，且**每次运行换一个全新目录**。

    两件踩过的事：

    1. 系统临时目录在本机沙箱下权限不稳定（升级运行留下的 ``pytest-of-*``
       会让受限运行直接 ``PermissionError``，表现为 fixture setup 阶段报错）；
    2. 就算把根移到工作区内，**只要复用同一个根**，照样被上一轮升级运行留下的
       目录卡住 —— 我刚踩了第二次。

    所以用 uuid 生成独一无二的子目录：新目录是空的，pytest 不会去枚举任何
    陈旧目录。
    """
    root = SCRATCH / "tmp" / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)
    tempfile.tempdir = str(root)


def force_rmtree(path: Path) -> None:
    """删除目录树，能处理 Windows 上的只读文件。

    git 会把 ``.git/objects`` 里的对象文件标为只读，``shutil.rmtree`` 会因此失败；
    而 ``ignore_errors=True`` 会把失败**静默吞掉**，导致 ``tests/.scratch`` 越积越多
    （实测残留过 129 项）。这里显式改权限后重试。
    """
    def handle(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=handle)      # Python 3.12+
    except TypeError:                          # pragma: no cover - 旧版本兼容
        shutil.rmtree(path, onerror=handle)

PY_APP = '''"""示例模块：用于验证符号表/调用图/依赖图。"""

import json
from pathlib import Path

CONSTANT = 42


class Service:
    def start(self):
        return self.prepare()

    def prepare(self):
        return helper()


def helper():
    return json.dumps({"a": 1})


async def fetch(path):
    return await load(path)


def unused():
    target = Path("x")
    return target
'''

PY_UTIL = '''import app


def run():
    return app.helper()
'''

TS_MAIN = '''export interface Options { debug: boolean }

export class Runner {
  start(): void {}
}

export function bootstrap(options: Options): void {
  const label = makeLabel();
}

const makeLabel = () => "tcide";
'''

GO_MAIN = '''package main

import "fmt"

type Server struct {
	Port int
}

func main() {
	fmt.Println("hi")
}

func serve(s *Server) error {
	return nil
}
'''

PACKAGE_JSON = """{
  "name": "my-app",
  "version": "1.0.0",
  "scripts": {"dev": "electron .", "build": "vite build", "test": "vitest"},
  "dependencies": {"react": "^18", "electron": "^28"},
  "devDependencies": {"vite": "^5"}
}
"""

PYPROJECT = """[project]
name = "my-app"
dependencies = ["fastapi>=0.115", "pawui>=0.1.1"]

[project.scripts]
my-app = "app:main"
"""

REQUIREMENTS = """fastapi>=0.115.0
pytest>=8
pawui>=0.1.1
# 注释行会被忽略
-r extra.txt
"""

CLAUDE_MD = """# 项目规则

1. 先澄清，再实现。
2. 极简实现，拒绝过度设计。
"""


@pytest.fixture
def project() -> Path:
    """一个覆盖多语言、多清单文件的小项目。"""
    root = SCRATCH / f"proj-{uuid.uuid4().hex[:8]}"
    (root / "src").mkdir(parents=True)
    (root / "web").mkdir(parents=True)
    (root / "node_modules").mkdir(parents=True)
    (root / ".git").mkdir(parents=True)
    (root / "assets").mkdir(parents=True)

    (root / "package.json").write_text(PACKAGE_JSON, encoding="utf-8")
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (root / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    (root / "README.md").write_text("# my-app\n\n示例。\n", encoding="utf-8")
    (root / "main.py").write_text("print('entry')\n", encoding="utf-8")
    (root / "src" / "app.py").write_text(PY_APP, encoding="utf-8")
    (root / "src" / "util.py").write_text(PY_UTIL, encoding="utf-8")
    (root / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "web" / "main.ts").write_text(TS_MAIN, encoding="utf-8")
    (root / "main.go").write_text(GO_MAIN, encoding="utf-8")
    (root / "node_modules" / "junk.js").write_text("var x = 1;\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")

    try:
        yield root
    finally:
        force_rmtree(root)


@pytest.fixture
def workspace(project: Path) -> Workspace:
    return Workspace(project)


@pytest.fixture
def index(workspace: Workspace) -> ProjectIndex:
    instance = ProjectIndex(workspace)
    instance.scan()
    return instance


@pytest.fixture
def memory(workspace: Workspace):
    return ProjectMemoryBuilder(workspace).build()


@pytest.fixture
def git_repo():
    """一个真实的临时 git 仓库（已在 main 分支上有一次提交）。

    用真实 git 而不是打桩：状态解析、重命名、非 ASCII 路径这些坑只有真跑才暴露。
    """
    import uuid as _uuid

    from tcide.gitops import GitRepo

    root = SCRATCH / f"git-{_uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    repo = GitRepo(root)
    repo.init()
    repo.ensure_identity()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    repo.stage_all()
    repo.commit("chore: 初始提交")
    try:
        yield repo
    finally:
        force_rmtree(root)


@pytest.fixture
def plain_dir():
    """一个不是 git 仓库的目录。"""
    import uuid as _uuid

    root = SCRATCH / f"plain-{_uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        force_rmtree(root)


@pytest.fixture(scope="session")
def qapp():
    """离屏 Qt 应用（界面测试用；Qt 在受限环境里会段错误，必须在沙箱外跑）。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def shell(project, qapp):
    """在临时项目上搭起完整的 IDE 外壳（离屏）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import main as tcide_main

    runtime, app = tcide_main.create_runtime(project)
    runtime.run(block=False)
    app.bootstrap()
    for _ in range(6):
        qapp.processEvents()
    try:
        yield runtime, app
    finally:
        runtime.root.close()
