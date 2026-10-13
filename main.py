# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""虎猫 TCIDE · PawUI 版：入口与后端接线。

把 `tcide/` 里的后端核心（工作区 / 索引 / Git / 工具 / 编排）接到 `app.paw` 上。

用法::

    python main.py                    # 用当前目录作为项目打开
    python main.py D:\\proj            # 指定项目目录
    python main.py --shot shot.png    # 离屏渲染并截图（无头验证用）
    python main.py --selftest         # 离屏跑一遍交互自检并打印结果

设计约束：
- **所有状态都在 ``AppState`` 里**，界面只通过 ``{$state}`` 模板读它；
  处理器改状态后调 :meth:`TCIDEApp.push` 把值推给 ``runtime.state``。
- 没有配置 API Key 时，AI 面板走**离线演练**路径并明确说明 —— 不假装调用了模型。
- 所有文件访问都过 `Workspace`，越界会被拒。
"""

from __future__ import annotations

#: 版本号（与发布 tag 对应：v0.0.0.1）
__version__ = "0.0.0.1"

import argparse
import json
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# QtWebEngine 的环境开关：**必须在导入任何 Qt 模块之前设置**。
#
# 这几项是实测出来的：offscreen + 无特权环境下，不设置它们 QtWebEngine 要么
# 起不来、要么白屏。探针见 tools/probe_webengine.py（实测 loadFinished=True，
# runJavaScript 能读回页面里的值）。
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

# 允许直接 python main.py 运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tcide.agent import AgentEvent, AgentOrchestrator
from tcide.completion import Completer
from tcide.gitintel import GitIntelligence
from tcide.gitops import GitError, GitRepo
from tcide.indexer import ProjectIndex
from tcide.diff import ReviewSession, ReviewingWorkspace
from tcide.memory import ProjectMemoryBuilder
from tcide.models import PRESETS, ChatResponse, ModelConfig, ModelError
from tcide.panels import (completion_text, line_offset, outline_rows,
                          parse_leading_line, preview_caption, preview_hex,
                          preview_image_path, preview_kind, preview_text,
                          row_for_label, rows_for_list, search_rows)
from tcide.pet import PetPainter, PetStateMachine
from tcide.tabs import OpenFiles, TabHistory
from tcide.tools import ToolRegistry
from tcide.workspace import Workspace, WorkspaceError

PAW_FILE = Path(__file__).resolve().parent / "app.paw"


# ---------------------------------------------------------------------------
# 离线假模型（没有 API Key 时用，明确告知而不是假装）
# ---------------------------------------------------------------------------

class OfflineModel:
    """离线"演练"模型：不联网，返回可预期的响应。

    用途：没有 API Key 时仍能把 Builder/Coder 的**编排流程**跑通并展示出来，
    同时明确标注这是演练结果。
    """

    def __init__(self, plan_tasks: list[dict] | None = None) -> None:
        self.plan_tasks = plan_tasks or []
        self.calls = 0

    def complete(self, messages, tools=None) -> ChatResponse:
        import json

        self.calls += 1
        joined = " ".join(message.content for message in messages)
        if "你是 Builder" in joined:
            tasks = self.plan_tasks or [
                {"title": "（离线演练）阅读相关文件",
                 "detail": "用 read_file/get_outline 了解现状"},
                {"title": "（离线演练）执行改动",
                 "detail": "配置 API Key 后可真正执行"},
            ]
            return ChatResponse(text="```json\n" + json.dumps(
                {"goal": joined.strip().splitlines()[-1][:60],
                 "notes": "离线演练：未调用任何模型 API", "tasks": tasks},
                ensure_ascii=False) + "\n```")
        if any(message.role == "tool" for message in messages):
            return ChatResponse(text="（离线演练）已按计划查看完毕；配置 API Key 后可执行真实改动。")
        return ChatResponse(text="（离线演练）")


# ---------------------------------------------------------------------------
# 应用状态
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    """界面读写的全部状态。"""

    root: Path
    workspace: Workspace
    index: ProjectIndex
    repo: GitRepo
    tools: ToolRegistry
    git_intel: GitIntelligence

    tree_rows: list[str] = field(default_factory=list)
    tree_paths: dict[str, str] = field(default_factory=dict)
    expanded: set[str] = field(default_factory=set)

    active_file: str = "(未打开文件)"
    active_path: str = ""
    editor_text: str = ""
    dirty: bool = False

    git_rows: list[str] = field(default_factory=list)
    git_paths: dict[str, str] = field(default_factory=dict)
    git_branch: str = ""
    git_hint: str = ""

    problem_rows: list[str] = field(default_factory=list)
    problem_paths: dict[str, str] = field(default_factory=dict)

    terminal_log: str = "_（输出会显示在这里）_\n"
    chat_log: str = "_（描述需求后回车；没有 API Key 时会走离线演练）_\n"
    palette_open: bool = False
    palette_rows: list[str] = field(default_factory=list)
    palette_filtered: list[str] = field(default_factory=list)
    palette_picked: str = ""

    sidebar_open: bool = True
    git_open: bool = True
    dock_open: bool = True

    provider: str = "deepseek"
    model: str = ""
    api_key: str = ""
    status_left: str = "就绪"
    status_right: str = ""
    project_line: str = ""
    mode_label: str = "离线"
    history: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

class TCIDEApp:
    """把后端接到 PawUI 运行时上。"""

    MAX_TREE_ROWS = 400
    MAX_PROBLEM_ROWS = 200
    MAX_DIFF_LINES = 200

    #: 引导里「新建示例文件并打开」写进去的内容（有意留了可改/可测的东西）
    SAMPLE_FILE = '''"""TCIDE 示例文件。

试试：改一行 -> Ctrl+S 保存（标签栏会出现 ● 脏标记）；
然后到「AI 对话」里说：“给 is_prime 写几个单测并运行”。
"""


def add(a: int, b: int) -> int:
    """两个数相加。"""
    return a + b


def is_prime(n: int) -> bool:
    """判断素数（朴素试除法）。"""
    if n < 2:
        return False
    divisor = 2
    while divisor * divisor <= n:
        if n % divisor == 0:
            return False
        divisor += 1
    return True


if __name__ == "__main__":
    print("add(2, 3) =", add(2, 3))
    print("is_prime(97) =", is_prime(97))
'''

    def _set_status(self, message: str) -> None:
        """更新状态栏左侧文字（界面用 ``{$status_left}`` 显示）。"""
        self.state.status_left = message

    def refresh_file(self, path: str) -> None:
        """把磁盘上的内容重新读进编辑器（审核接受/驳回后同步显示）。"""
        if not self.state.workspace.exists(path):
            return
        self.state.editor_text = self.state.workspace.read(path)
        self.state.dirty = False

    def __init__(self, root: str | Path, *, provider: str = "deepseek",
                 api_key: str = "") -> None:
        # 一个共享的 Workspace 就够（以前建了 3 个独立实例，索引和写入各看各的）
        workspace = Workspace(root)
        self.review = ReviewSession(workspace)
        #: 审查门开启时，Agent 的写操作进审核队列而不落盘
        self.review_enabled = True
        self.tool_workspace = ReviewingWorkspace(workspace, self.review,
                                                 enabled=self.review_enabled)
        self.diff_open = False
        self.diff_index = 0
        #: 多标签：打开的文件模型（纯逻辑在 tcide/tabs.py）
        self.open_files = OpenFiles()
        self._tab_history = TabHistory()
        #: Web 编辑器（<Web> + QWebChannel）；只有窗口里存在 <Web> 时才建立
        self._web_bridge = None
        self._web_channel = None
        self._web_view_obj = None
        self._web_ready = False
        self._web_syncing = False
        self.state = AppState(
            root=Path(root).resolve(),
            workspace=workspace,
            index=ProjectIndex(workspace),
            repo=GitRepo(root),
            tools=ToolRegistry(self.tool_workspace),
            git_intel=GitIntelligence(GitRepo(root)),
        )
        self.state.index.scan()
        self.state.tools.index = self.state.index
        self.state.git_intel.index = self.state.index
        self.state.provider = provider if provider in PRESETS else "custom"
        self.state.api_key = api_key
        self.state.model = PRESETS[self.state.provider].model
        self.pet = PetStateMachine()
        self.pet_painter = PetPainter()
        self.runtime = None            # 由 create_runtime 注入
        self._lock = threading.Lock()
        #: 跨线程投递（见 _post_to_gui）：GUI 线程认领自己的 ident，工作线程排队
        self._gui_thread_id = threading.get_ident()
        self._gui_lock = threading.Lock()
        self._gui_queue: deque = deque()
        self._gui_timer = None

    # -- 跨线程投递：工作线程绝不直接碰界面 --------------------------------
    #
    # 事故记录（真实运行时报的警告，不是推测）：
    #   QObject: Cannot create children for a parent that is in a different thread.
    #   (Parent is QTextDocument(...), parent's thread is QThread(A),
    #    current thread is QThread(B))
    # 原因：终端命令与在线 AI 跑在**工作线程**里，跑完却直接调 ``self.push()``
    # / ``page().runJavaScript()``，于是 PawUI 在 B 线程里更新 Markdown 控件
    # （QTextDocument 在 A 线程创建）—— Qt 只允许在对象所属线程里访问 GUI 对象，
    # 轻则警告、重则随机崩溃。
    #
    # 做法：工作线程把回调塞进队列，GUI 线程用 QTimer 定期抽干。
    # 不用 Qt 信号是因为 TCIDEApp 不是 QObject；队列方案不依赖 Qt 版本。
    # **已经在 GUI 线程时直接执行**，这样离线演练/自检仍是同步语义。

    def _post_to_gui(self, func, *args) -> None:
        """从**任意线程**安排一段代码在 GUI 线程里执行。"""
        if threading.get_ident() == self._gui_thread_id:
            func(*args)
            return
        with self._gui_lock:
            self._gui_queue.append((func, args))

    def _drain_gui_queue(self) -> int:
        """只在 GUI 线程里被 QTimer 调用：执行排队中的回调。返回执行条数。"""
        done = 0
        while True:
            with self._gui_lock:
                if not self._gui_queue:
                    return done
                func, args = self._gui_queue.popleft()
            try:
                func(*args)
            except Exception as exc:      # noqa: BLE001 - 一个回调失败不该拖垮界面
                self._set_status(f"回调失败：{type(exc).__name__}: {exc}")
            done += 1

    def _start_gui_pump(self) -> None:
        if self._gui_timer is not None:
            return
        from PySide6.QtCore import QTimer

        timer = QTimer()
        timer.setInterval(80)
        timer.timeout.connect(self._drain_gui_queue)
        timer.start()
        self._gui_timer = timer

    # -- 与运行时对接 -----------------------------------------------------

    def context(self) -> dict:
        """传给 ``Runtime(context=...)`` 的处理器表。"""
        names = [
            "cmd_open_project", "cmd_save", "cmd_palette", "cmd_run_tests",
            "cmd_git_stage", "cmd_git_commit", "cmd_toggle_sidebar",
            "cmd_toggle_dock", "cmd_toggle_git", "cmd_agent_dry_run",
            "cmd_clear_chat",
            "cmd_review_toggle", "cmd_review_open", "cmd_diff_next",
            "cmd_diff_accept_first", "cmd_diff_reject_all",
            "cmd_open_web_editor", "cmd_open_terminal",
            "cmd_open_sample", "cmd_onboarding_ai",
            "cmd_tools_open", "cmd_tools_close", "on_tools_pick", "on_tools_accept",
            "on_onboarding_toggle",
            "cmd_split_open", "cmd_split_close",
            "cmd_lsp_status", "cmd_lsp_start", "cmd_lsp_stop",
            "cmd_tab_close", "cmd_tab_next", "cmd_tab_previous",
            "on_tab_select",
            "on_tree_select", "on_git_select", "on_problem_select",
            "on_terminal_run", "on_chat_send", "on_editor_change",
            "on_provider_change", "on_palette_filter", "on_palette_pick",
            "on_palette_accept", "on_palette_close",
            "on_sidebar_toggle", "on_git_toggle", "on_dock_toggle",
            "on_pet_draw", "on_diff_accept", "on_diff_reject",
            "on_search_run", "on_search_select", "on_outline_select",
        ]
        return {name: getattr(self, name) for name in names}

    def push(self, *, _skip_web: bool = False, **overrides) -> None:
        """把状态推给运行时（界面上的 ``{$...}`` 会立即刷新）。

        ``_skip_web=True`` 用于**来自 Web 编辑器自身的改动**：不再把文本推回去，
        避免"改动 → 推送 → 改动"的回环（页面侧也有去重，这里是双保险）。
        """
        if self.runtime is None:
            return
        # 大纲跟随当前文件刷新（放在 push 里，避免每个打开文件的入口都要记得调）
        self.refresh_outline()
        self._refresh_preview()
        if not _skip_web:
            self._push_web_state()
        state = self.state
        payload = {
            "tree_rows": state.tree_rows,
            "project_line": state.project_line,
            "active_file": state.active_file,
            "tab_rows": self.open_files.rows(),
            "editor_text": state.editor_text,
            "dirty_label": "未保存" if state.dirty else "已保存",
            "dirty_color": self._dirty_color(),
            "editor_stats": self._editor_stats(),
            "git_rows": state.git_rows,
            "git_branch_line": state.git_branch,
            "git_hint": state.git_hint,
            "problem_rows": state.problem_rows,
            "problems_line": self._problems_line(),
            "terminal_log": state.terminal_log,
            "chat_log": state.chat_log,
            "palette_open": state.palette_open,
            "palette_rows": state.palette_filtered or state.palette_rows,
            "palette_hint": f"共 {len(state.palette_rows)} 条命令，回车执行选中的一条",
            "sidebar_open": state.sidebar_open,
            "git_open": state.git_open,
            "dock_open": state.dock_open,
            "provider": state.provider,
            "provider_items": [f"{key} · {preset.label}"
                               for key, preset in PRESETS.items()],
            "model_line": f"{state.model}"
                          f"（{'已配置 Key' if state.api_key else '未配置 Key → 离线演练'}）",
            "mode_label": "在线" if state.api_key else "离线",
            "status_left": state.status_left,
            "status_right": state.status_right,
            "pet_label": self.pet.snapshot().label,
            # ---- Diff 审核（补回原版丢失的 acceptDiff）----
            "review_label": "审查门：开" if self.review_enabled else "审查门：关",
            "review_badge": f"待审核 {len(self.review.pending())}",
            "review_badge_color": (self._hex("theme.danger") if self.review.pending()
                                   else self._hex("theme.border")),
            "diff_open": self.diff_open,
            "diff_title": self._diff_title(),
            "diff_path": self._diff_path(),
            "diff_text": self._diff_text(),
            "diff_stats": self._diff_stats(),
            "diff_hint": self._diff_hint(),
            # ---- 工程面板（大纲 / 搜索 / 补全）----
            "search_rows": self._search_rows,
            "search_line": self._search_line,
            "outline_rows": self._outline_row_texts,
            "outline_line": self._outline_line,
            "completion_line": self._completion_line(),
            # ---- 快速上手（空状态引导）与工具菜单 ----
            "onboarding_open": self._onboarding_visible(),
            "tools_open": self.tools_open,
            "tools_rows": list(self.tool_rows),
            "tools_hint": self.tools_hint(),
            # ---- 预览面板 ----
            "preview_caption": self._preview_caption,
            "preview_image": self._preview_image,
            "preview_text": self._preview_text,
            # ---- Web 编辑器 ----
            "web_editor_url": self._web_editor_url(),
            "web_editor_line": self._web_editor_line,
        }
        payload.update(overrides)
        for key, value in payload.items():
            self.runtime.state.set(key, value)

    def _editor_stats(self) -> str:
        text = self.state.editor_text
        lines = text.count("\n") + 1 if text else 0
        return f"{len(text)} 字符 · {lines} 行"

    def _dirty_color(self) -> str:
        """脏标记的颜色：**推十六进制色值**，而不是主题令牌名。

        原因：PawUI 0.1.3.3 里经由 ``{$state}`` 传令牌名不会被解析成颜色
        （``bg="{$x}"``、``x="border"`` → 报警 + QSS 里 `background:border` 被丢弃）。
        这个缺陷已在本地的 PawUI 树上修好并加了回归测试，但应用**不应该依赖补丁**，
        所以这里直接给出最终色值。详见 FEATURE_MATRIX 的 bug 清单。
        """
        if self.runtime is None:
            return "#ff453a" if self.state.dirty else "#3a3a3c"
        theme = self.runtime.theme
        return str(theme.danger if self.state.dirty else theme.border)

    def _problems_line(self) -> str:
        if not self.state.problem_rows:
            return "未发现问题"
        return f"{len(self.state.problem_rows)} 个问题（点击跳转）"

    # -- 启动刷新 ---------------------------------------------------------

    def bootstrap(self) -> None:
        """首次填充：文件树、Git、问题列表、状态栏。"""
        self.refresh_tree()
        self.refresh_git()
        self.refresh_problems()
        self.state.project_line = (f"{self.state.root.name} · "
                                   f"{self.state.index.last_stats.files} 个文件")
        memory = ProjectMemoryBuilder(self.state.workspace).build()
        self.state.status_left = (
            f"{memory.name or self.state.root.name} · "
            f"{memory.primary_language or '未知语言'} · {len(memory.dependencies)} 个依赖")
        self.state.status_right = f"PawUI · {self.state.provider}/{self.state.model}"
        self.build_palette()
        self.push()
        # 启动跨线程投递的抽水泵（工作线程的回调靠它回到 GUI 线程执行）
        self._start_gui_pump()
        # 没打开文件时把「🚀 上手」页签切到前面（只做一次，之后不抢用户的页签）
        self._focus_onboarding_tab()

    # -- 文件树 -----------------------------------------------------------

    def refresh_tree(self) -> None:
        """按展开状态生成文件树行；行的显示文本唯一（重名会加序号）。"""
        rows: list[str] = []
        paths: dict[str, str] = {}
        counters: dict[str, int] = {}

        def walk(relative: str, depth: int) -> None:
            if len(rows) >= self.MAX_TREE_ROWS:
                return
            try:
                nodes = self.state.workspace.list_dir(relative)
            except WorkspaceError:
                return
            for node in nodes:
                if node.is_dir:
                    if depth > 2:          # 默认只展开到第 2 层
                        continue
                    expanded = node.path in self.state.expanded
                    glyph = "▾" if expanded else "▸"
                    label = f"{'  ' * depth}{glyph} {node.name}/"
                else:
                    label = f"{'  ' * depth}· {node.name}"
                count = counters.get(label, 0) + 1
                counters[label] = count
                unique = label if count == 1 else f"{label} ({count})"
                rows.append(unique)
                paths[unique] = node.path
                if node.is_dir and node.path in self.state.expanded:
                    walk(node.path, depth + 1)

        walk("", 0)
        self.state.tree_rows = rows
        self.state.tree_paths = paths

    def on_tree_select(self, value: str = "", *_args) -> None:
        """点击文件树：目录则展开/折叠，文件则打开。

        **预览路径在这里就记下来**（不管这个文件能不能载入文本编辑器）：
        预览面板的意义正是能看图片/二进制，而它们本来就载不进 TextArea。
        实测踩过：只在 ``open_file`` 里记路径的话，选图片时预览会停在**上一个**
        文本文件的状态（因为二进制文件被跳过，``open_file`` 根本没被调用）。
        """
        path = self.state.tree_paths.get(str(value))
        if not path:
            return
        target = self.state.workspace.resolve(path)
        if target.is_dir():
            if path in self.state.expanded:
                self.state.expanded.discard(path)
                self.state.status_left = f"折叠 {path}"
            else:
                self.state.expanded.add(path)
                self.state.status_left = f"展开 {path}"
            self.refresh_tree()
        else:
            self._preview_path = path
            self.open_file(path)
        self.push()

    def open_file(self, path: str) -> None:
        try:
            text = self.state.workspace.read(path)
        except WorkspaceError as exc:
            self.log(f"打开失败：{exc}")
            return
        self._preview_path = path
        self.open_files.open(path)          # 多标签：登记并激活
        self.state.active_path = path
        self.state.active_file = path
        self.state.editor_text = text
        self.state.dirty = False
        self.open_files.mark_dirty(path, False)
        self.state.status_left = f"已打开 {path}（{len(text)} 字符）"
        self.refresh_problems()
        self._lsp_sync_file(path, text)

    # -- 多标签（打开的文件）----------------------------------------------

    open_files = None                        # 由 __init__ 注入 OpenFiles()
    _tab_history = None

    def on_tab_select(self, value: str = "", *_args) -> None:
        """点击「打开的文件」列表：切换到那个文件。"""
        path = self.open_files.path_for_row(str(value))
        if not path:
            return
        if path == self.state.active_path:
            self.state.status_left = f"已经是 {path}"
            self.push()
            return
        if self.state.dirty and self.state.active_path:
            # 切走前把当前编辑内容留在模型里（不落盘，但脏标记要跟着走）
            self.open_files.mark_dirty(self.state.active_path, True)
        self.open_file(path)
        self.state.status_left = f"已切换到 {path}"
        self.push()

    def cmd_tab_close(self, *_args) -> None:
        """关闭当前标签（有未保存改动时只提醒，不静默丢弃）。"""
        path = self.state.active_path
        if not path:
            self._set_status("没有打开的标签")
            return self.push()
        if self.state.dirty:
            self._set_status(f"{path} 有未保存改动 —— 先保存（Ctrl+S）再关闭")
            return self.push()
        self.open_files.close(path)
        self._tab_history.forget(path)
        remaining = self.open_files.active()
        if remaining is None:
            self.state.active_path = ""
            self.state.active_file = "(未打开文件)"
            self.state.editor_text = ""
            self._set_status(f"已关闭 {path}")
        else:
            self.open_file(remaining.path)
            self._set_status(f"已关闭 {path}，切到 {remaining.path}")
        self.push()

    def cmd_tab_next(self, *_args) -> None:
        item = self.open_files.next()
        if item is not None:
            self.open_file(item.path)
            self._set_status(f"切到 {item.path}")
        self.push()

    def cmd_tab_previous(self, *_args) -> None:
        item = self.open_files.previous()
        if item is not None:
            self.open_file(item.path)
            self._set_status(f"切到 {item.path}")
        self.push()

    # -- 编辑器 -----------------------------------------------------------

    def on_editor_change(self, text: str = "", *_args) -> None:
        self.state.editor_text = str(text)
        self.state.dirty = True
        self.push()

    def cmd_save(self, *_args) -> None:
        if not self.state.active_path:
            self.state.status_left = "没有打开的文件"
            self.push()
            return
        try:
            self.state.workspace.write(self.state.active_path, self.state.editor_text)
        except WorkspaceError as exc:
            self.state.status_left = f"保存失败：{exc}"
            self.push()
            return
        self.state.dirty = False
        self.state.status_left = f"已保存 {self.state.active_path}"
        self.refresh_problems()
        self.refresh_git()
        self.push()

    # -- Git --------------------------------------------------------------

    def refresh_git(self) -> None:
        state = self.state
        if not state.repo.is_repo():
            state.git_rows = ["（当前目录不是 git 仓库）"]
            state.git_paths = {}
            state.git_branch = "Git：不可用"
            state.git_hint = "在项目目录里 git init 后可用"
            return
        try:
            status = state.repo.status()
        except GitError as exc:
            state.git_rows = [f"（git 出错：{exc}）"]
            state.git_paths = {}
            return
        rows: list[str] = []
        paths: dict[str, str] = {}
        for change in status.changes[:120]:
            label = f"[{change.kind[:4]:<4}] {change.path}"
            if label in paths:
                continue
            rows.append(label)
            paths[label] = change.path
        state.git_rows = rows or ["（工作区干净）"]
        state.git_paths = paths
        state.git_branch = (f"分支 {status.branch or '?'}"
                            + (f" → {status.upstream}" if status.upstream else "")
                            + (f"  领先 {status.ahead}/落后 {status.behind}"
                               if status.ahead or status.behind else ""))
        try:
            suggestion = state.git_intel.suggest_commit()
            state.git_hint = f"建议提交信息：{suggestion.message.splitlines()[0]}"
        except Exception:  # noqa: BLE001
            state.git_hint = ""

    def on_git_select(self, value: str = "", *_args) -> None:
        path = self.state.git_paths.get(str(value))
        if not path:
            return
        diff = (self.state.repo.diff(path=path)
                or self.state.repo.diff(staged=True, path=path))
        self.set_terminal(f"$ git diff -- {path}\n\n{diff or '（无差异）'}")
        self.push()

    def cmd_git_stage(self, *_args) -> None:
        try:
            result = self.state.repo.stage_all()
        except GitError as exc:
            self.log(f"暂存失败：{exc}")
            return
        self.set_terminal("$ git add -A\n\n"
                          + (result.stdout + result.stderr or "（无输出）"))
        self.refresh_git()
        self.push()

    def cmd_git_commit(self, *_args) -> None:
        if not self.state.repo.is_repo():
            self.state.status_left = "不是 git 仓库，无法提交"
            self.push()
            return
        message = self.state.git_intel.suggest_commit().message.splitlines()[0]
        try:
            self.state.repo.stage_all()
            result = self.state.repo.commit(message)
        except GitError as exc:
            self.log(f"提交失败：{exc}")
            return
        output = (result.stdout + result.stderr).strip() or "（无输出）"
        self.set_terminal(f'$ git commit -m "{message}"\n\n{output}')
        self.refresh_git()
        self.push()

    # -- 问题面板 ---------------------------------------------------------

    def refresh_problems(self) -> None:
        """只检查当前打开的文件（全项目检查放在命令面板里）。"""
        state = self.state
        rows: list[str] = []
        paths: dict[str, str] = {}
        if state.active_path:
            result = state.tools.call("get_diagnostics", {"path": state.active_path})
            for item in (result.meta.get("problems") or [])[:self.MAX_PROBLEM_ROWS]:
                label = str(item)
                if label in paths:
                    continue
                rows.append(label)
                paths[label] = state.active_path
        # 并入 LSP 诊断（如果语言服务器在跑）—— 两类问题同处显示，
        # 因为 tcide.lsp 已经把 LSP 诊断转成了同一种 Issue 结构
        for issue in self._lsp_issues():
            if issue.path and issue.path != state.active_path:
                continue
            label = str(issue)
            if label in paths:
                continue
            rows.append(label)
            paths[label] = issue.path or state.active_path
        state.problem_rows = rows
        state.problem_paths = paths

    def on_problem_select(self, value: str = "", *_args) -> None:
        path = self.state.problem_paths.get(str(value))
        if path:
            self.open_file(path)
            self.state.status_left = f"跳转到 {value}"
            self.push()

    # -- LSP（语言服务器）--------------------------------------------------

    _lsp_client = None
    _lsp_note: str = "LSP：未启动（命令面板 →LSP 状态/LSP 启动）"

    def cmd_lsp_status(self, *_args) -> None:
        """报告每个语言的服务器可用性（缺什么、怎么装）。"""
        import shutil

        from tcide.lsp import detect_servers, project_language, status_report

        language = project_language(self.state.workspace, self._memory_for_lsp())
        report = status_report(which=shutil.which)
        self._lsp_note = f"LSP：项目语言 {language or '未识别'}"
        self._append_chat("·", "```\n" + report + "\n```")
        self._set_status(f"LSP 状态已输出到 AI 面板（项目语言 {language or '未识别'}）")
        # 顺带把"缺哪个"直接说在状态栏，免得用户还要翻对话
        missing = [status.language for status in detect_servers(which=shutil.which)
                   if not status.available]
        if missing:
            self._lsp_note += f"（未安装：{'、'.join(missing)}）"
        self.push()

    def _memory_for_lsp(self):
        try:
            return ProjectMemoryBuilder(self.state.workspace).build()
        except Exception:      # noqa: BLE001
            return None

    def cmd_lsp_start(self, *_args) -> bool:
        """为当前文件启动语言服务器并接上诊断。

        可跳过设计：没有可用服务器就**如实说明**（含安装提示），界面照常能用。
        """
        from tcide.lsp import LspClient, detect_servers, project_language

        import shutil

        if not self.state.active_path:
            self._set_status("LSP：先打开一个文件")
            return False
        memory = self._memory_for_lsp()
        language = (project_language(self.state.workspace, memory)
                    or self.state.workspace.language_of(self.state.active_path))
        statuses = {status.language: status
                    for status in detect_servers(which=shutil.which)}
        status = statuses.get(language)
        if status is None or not status.available:
            hint = status.hint if status else "这门语言没有配置服务器"
            self._lsp_note = f"LSP：{language} 不可用（{hint}）"
            self._set_status(self._lsp_note)
            self.push()
            return False

        if self._lsp_client is not None:
            self._lsp_client.stop()
        client = LspClient(status, self.state.root, timeout=8)
        if not client.start():
            self._lsp_note = f"LSP：启动失败（{client.error}）"
            self._set_status(self._lsp_note)
            self.push()
            return False
        self._lsp_client = client
        self._lsp_note = f"LSP：{language} 已连接（{status.command}）"
        self._lsp_sync_file(self.state.active_path, self.state.editor_text)
        self._set_status(self._lsp_note)
        self.push()
        return True

    def cmd_lsp_stop(self, *_args) -> None:
        if self._lsp_client is not None:
            self._lsp_client.stop()
            self._lsp_client = None
        self._lsp_note = "LSP：已停止"
        self._set_status(self._lsp_note)
        self.push()

    def _lsp_sync_file(self, path: str, text: str) -> None:
        """把文件内容告诉语言服务器（打开/改动）。"""
        if self._lsp_client is None:
            return
        import time

        from tcide.lsp import path_to_uri

        try:
            absolute = str(self.state.workspace.resolve(path))
            if self._lsp_client._opened.get(path_to_uri(absolute)):
                self._lsp_client.change_file(absolute, text)
            else:
                self._lsp_client.open_file(
                    absolute, text,
                    language=self.state.workspace.language_of(path))
            # 诊断是异步推来的：给读线程一小段时间（不阻塞太久）
            for _ in range(20):
                if self._lsp_client.diagnostics:
                    break
                time.sleep(0.05)
        except Exception:      # noqa: BLE001 - 语言服务器不可靠是常态
            pass

    def _lsp_issues(self) -> list:
        """当前文件的 LSP 诊断。"""
        if self._lsp_client is None or not self.state.active_path:
            return []
        try:
            return self._lsp_client.issues(root=self.state.root)
        except Exception:      # noqa: BLE001
            return []

    # -- 快速上手（空状态引导）与工具菜单 ---------------------------------
    #
    # 设计取舍：PawUI 没有动态显隐，但 <Panel open=...> 会**响应状态变化**，
    # 所以"没打开文件时展开引导、打开后自动收起"用 Panel 的 open 就够，
    # 不需要引入新的显隐机制。

    onboarding_open = True          # 用户的手动开关（引导面板是否展开）
    tools_open = False
    tool_rows: tuple = ()
    tools_pick: str = ""

    #: 工具菜单条目：显示文本 -> 处理键
    TOOL_ENTRIES: tuple[tuple[str, str], ...] = (
        ("Web 编辑器（Monaco · 91 种语言 · 可分屏）", "web_editor"),
        ("终端（真子进程执行，支持历史与清屏）", "terminal"),
        ("分屏打开下一个标签（只读参考）", "split"),
        ("关闭分屏", "split_close"),
        ("关闭当前标签", "tab_close"),
        ("下一个标签", "tab_next"),
        ("上一个标签", "tab_previous"),
        ("LSP 状态（哪些语言服务器可用）", "lsp_status"),
        ("LSP 启动（为当前文件接上诊断）", "lsp_start"),
        ("LSP 停止", "lsp_stop"),
        ("全项目静态检查（11 条规则）", "check_all"),
        ("刷新文件树 / Git 状态", "refresh"),
    )

    def _onboarding_visible(self) -> bool:
        """没打开任何文件、且用户没手动收起时，引导处于"该被看到"的状态。

        注意：引导的**载体**是底部面板的第一个页签「🚀 上手」（不是编辑器上方
        的 Panel —— 那里空间被 TextArea 挤满，实测面板 body 只有 57px 高、
        内容看不见）。所以这里只表达"该不该自动把那个页签切到前面"。
        """
        return bool(self.onboarding_open and not self.state.active_path)

    def _focus_onboarding_tab(self) -> bool:
        """没打开文件时，把底部面板切到「🚀 上手」页签（只在启动时做一次）。"""
        if not self._onboarding_visible():
            return False
        return self._select_dock_tab(0)

    def on_onboarding_toggle(self, *_args) -> None:
        self.onboarding_open = not self.onboarding_open
        self.push()

    def cmd_open_sample(self, *_args) -> None:
        """引导里的「新建示例文件并打开」—— 把"从哪开始"变成具体动作。

        注意：这里直接写 `state.workspace`（不是审核代理）——这是**用户显式**
        新建文件，不该进 AI 改动审核队列。也不会覆盖已有文件（自动换名）。
        """
        base = "hello_tcide"
        name = f"{base}.py"
        counter = 2
        while self.state.workspace.exists(name):
            name = f"{base}_{counter}.py"
            counter += 1
        try:
            self.state.workspace.write(name, self.SAMPLE_FILE)
        except WorkspaceError as exc:
            self._set_status(f"创建示例文件失败：{exc}")
            return self.push()
        self.refresh_tree()
        self.open_file(name)
        self.state.status_left = f"已创建 {name} —— 改一行再按 Ctrl+S，标签栏会出现 ● 脏标记"
        self._append_chat("·", f"已创建示例文件 `{name}`。可以到「AI 对话」里说："
                               f"“给 is_prime 写几个单测并运行”")
        self.push()

    def cmd_onboarding_ai(self, *_args) -> None:
        """引导里的「看看 AI 怎么干活」：切到 AI 页签 + 跑离线演练（不调 API）。"""
        self._select_dock_tab(4)
        self._append_chat("你", "给当前文件里的 is_prime 写几个单测，并说明怎么运行")
        self.cmd_agent_dry_run()
        self.push()

    def tools_hint(self) -> str:
        if not self.tool_rows:
            self.refresh_tools()
        return f"共 {len(self.tool_rows)} 项 —— 选中后按「执行」（或点两下）"

    def refresh_tools(self) -> None:
        self.tool_rows = tuple(label for label, _key in self.TOOL_ENTRIES)

    def cmd_tools_open(self, *_args) -> None:
        """打开工具菜单：把原先藏在命令面板里的能力提到界面。"""
        self.refresh_tools()
        self.tools_open = True
        self._set_status("工具菜单：选一条执行")
        self.push()

    def cmd_tools_close(self, *_args) -> None:
        self.tools_open = False
        self.push()

    def on_tools_pick(self, value: str = "", *_args) -> None:
        self.tools_pick = str(value)
        self._set_status(f"已选：{value}") if value else None

    def on_tools_accept(self, *_args) -> None:
        label = self.tools_pick or (self.tool_rows[0] if self.tool_rows else "")
        self.tools_open = False
        if label:
            self._run_tool(label)
        self.push()

    def _run_tool(self, label: str) -> bool:
        """按工具菜单条目分派（精确匹配，避免误触发）。"""
        table = {
            "web_editor": self.cmd_open_web_editor,
            "terminal": self.cmd_open_terminal,
            "split": self.cmd_split_open,
            "split_close": self.cmd_split_close,
            "tab_close": self.cmd_tab_close,
            "tab_next": self.cmd_tab_next,
            "tab_previous": self.cmd_tab_previous,
            "lsp_status": self.cmd_lsp_status,
            "lsp_start": self.cmd_lsp_start,
            "lsp_stop": self.cmd_lsp_stop,
            "check_all": self.cmd_check_all,
            "refresh": self._refresh_all,
        }
        for text, key in self.TOOL_ENTRIES:
            if text == label:
                handler = table.get(key)
                if handler is not None:
                    handler()
                    return True
        self._set_status(f"未识别的工具：{label}")
        return False

    def _refresh_all(self, *_args) -> None:
        self.refresh_tree()
        self.refresh_git()
        self._set_status("已刷新文件树与 Git 状态")
        self.push()

    def _select_dock_tab(self, index: int) -> bool:
        """切换底部面板的页签（引导用：把 AI 对话切到前面）。

        PawUI 的 `<Tabs>` 就是 QTabWidget；拿不到控件时**静默降级**（只提示），
        不因为一个锦上添花的动作把界面搞崩。
        """
        if self.runtime is None:
            return False
        root = getattr(self.runtime, "root", None)
        if root is None or not hasattr(root, "findChild"):
            return False
        try:
            from PySide6.QtWidgets import QTabWidget

            widget = root.findChild(QTabWidget, "dock_tabs")
            if widget is None:
                return False
            widget.setCurrentIndex(max(0, min(int(index), widget.count() - 1)))
            return True
        except Exception:      # noqa: BLE001
            return False

    def cmd_run_tests(self, *_args) -> None:
        """跑项目测试命令（从项目记忆里取）。"""
        memory = ProjectMemoryBuilder(self.state.workspace).build()
        command = memory.test_command
        if not command:
            self.log("没有检测到测试命令（可在 CLAUDE.md/清单文件里声明）")
            return
        result = self.state.tools.call("run_command",
                                       {"command": command, "timeout": 300})
        self.set_terminal(f"$ {command}\n\n{result.output or result.error}")
        self.state.status_left = f"测试{'通过' if result.ok else '失败'}：{command}"
        self.push()

    # -- 终端 -------------------------------------------------------------

    def set_terminal(self, text: str) -> None:
        self.state.terminal_log = "```\n" + text.strip()[:8000] + "\n```\n"

    def log(self, message: str) -> None:
        self.set_terminal(message)
        self.state.status_left = message
        self.push()

    def on_terminal_run(self, text: str = "", *_args) -> None:
        command = str(text).strip()
        if not command:
            return
        self.state.history.append(command)
        result = self.state.tools.call("run_command",
                                       {"command": command, "timeout": 60})
        body = result.output or result.error or "（无输出）"
        self.set_terminal(f"$ {command}\n\n{body}")
        self.state.status_left = f"命令{'成功' if result.ok else '失败'}：{command}"
        self.refresh_git()
        self.push()

    # -- AI 面板 ----------------------------------------------------------

    def _append_chat(self, role: str, text: str) -> None:
        if self.state.chat_log.startswith("_"):
            self.state.chat_log = ""
        self.state.chat_log += f"\n**{role}**：{text}\n"

    def make_orchestrator(self, model) -> AgentOrchestrator:
        return AgentOrchestrator(
            model, self.state.tools,
            context=ProjectMemoryBuilder.to_markdown(
                ProjectMemoryBuilder(self.state.workspace).build()),
            max_parallel_coders=2, max_coder_retry=1,
            review_gate_enabled=True, on_event=self._on_agent_event)

    def _on_agent_event(self, event: AgentEvent) -> None:
        # 像素宠物跟着编排状态走（9 状态机在 tcide/pet.py 里，可离线测试）。
        # pet.feed_event 只动纯 Python 状态，不碰 Qt，所以在工作线程里也安全。
        self.pet.feed_event(event)
        if event.kind in ("task_start", "task_done", "review", "retry", "plan"):
            # 写对话日志要回 GUI 线程：在线模式下这个回调来自工作线程，
            # 而 chat_log 会被 Markdown 控件渲染（QTextDocument 在主线程）。
            self._post_to_gui(self._append_chat, "·",
                              f"{event.kind} {event.message}".strip())

    def on_pet_draw(self, painter=None, *_args) -> None:
        """PawUI ``<Canvas on_draw>`` 回调：把当前状态画出来。

        画布是 28x28，宠物按 64x64 设计，所以缩放 0.42。
        """
        if painter is None:
            return
        snapshot = self.pet.snapshot()
        self.pet_painter.render(painter, state=snapshot.state,
                                frame=snapshot.frame_index,
                                x=1, y=1, scale=0.42)

    # -- Diff 审核（补回原版丢失的 acceptDiff / closeDiffModal）-----------

    #: 主题色的十六进制回退值。界面**始终推十六进制**而不是令牌名，
    #: 所以在未打补丁的 PawUI 上也能正常显示（令牌名会被 Qt 静默丢弃）。
    FALLBACK_COLORS = {"danger": "#ff453a", "border": "#3a3a3c",
                       "accent": "#4a9eff", "subtext": "#9aa4b2"}

    def _hex(self, token: str) -> str:
        """把 ``theme.xxx`` 解析成十六进制；拿不到就用回退值。"""
        name = token.split(".")[-1]
        theme = getattr(self.runtime, "theme", None) if self.runtime else None
        if theme is not None:
            for attr in ("color", "resolve", "get"):
                method = getattr(theme, attr, None)
                if callable(method):
                    try:
                        value = method(name)
                    except Exception:      # noqa: BLE001 - 主题 API 不保证
                        continue
                    if isinstance(value, str) and value.startswith("#"):
                        return value
        return self.FALLBACK_COLORS.get(name, "#3a3a3c")

    def _current_proposal(self):
        pending = self.review.pending()
        if not pending:
            return None
        self.diff_index = max(0, min(self.diff_index, len(pending) - 1))
        return pending[self.diff_index]

    def _diff_title(self) -> str:
        totals = self.review.totals()
        if not totals["pending"]:
            return "改动审核（无待审核）"
        return f"改动审核 · {self.diff_index + 1}/{totals['pending']}"

    def _diff_path(self) -> str:
        proposal = self._current_proposal()
        if proposal is None:
            return "AI 的写操作会先出现在这里，接受后才落盘"
        return ("新文件 " if proposal.is_new_file else "") + proposal.path

    def _diff_text(self) -> str:
        proposal = self._current_proposal()
        if proposal is None:
            return ""
        if not proposal.changed:
            return "（内容没有变化）"
        return proposal.preview(limit=self.MAX_DIFF_LINES)

    def _diff_stats(self) -> str:
        proposal = self._current_proposal()
        if proposal is None:
            return "待审核 0"
        stats = proposal.stats
        return f"+{stats['added']} / -{stats['removed']}"

    def _diff_hint(self) -> str:
        proposal = self._current_proposal()
        if proposal is None:
            return "没有待审核的改动"
        source = f"来源：{proposal.source} · " if proposal.source else ""
        return (f"{source}接受=写入磁盘；驳回=丢弃。"
                f"也可以用「只接受第一个改动块」做部分接受")

    def cmd_review_toggle(self, *_args) -> None:
        """开关审查门：关掉之后 Agent 的写操作直接落盘。"""
        self.review_enabled = not self.review_enabled
        self.tool_workspace.enabled = self.review_enabled
        self._append_chat("·", f"审查门 {'开启' if self.review_enabled else '关闭'}"
                              f"（Agent 写操作将{'先进入审核' if self.review_enabled else '直接落盘'}）")
        self.push()

    def cmd_review_open(self, *_args) -> None:
        """打开审核弹窗（没有待审核内容时也提示一下，避免看起来像没反应）。"""
        self.diff_index = 0
        self.diff_open = True
        if not self.review.pending():
            self._set_status("没有待审核的改动")
        self.push()

    def cmd_diff_next(self, *_args) -> None:
        count = len(self.review.pending())
        if count:
            self.diff_index = (self.diff_index + 1) % count
        self.push()

    def cmd_diff_accept_first(self, *_args) -> None:
        """只接受当前文件的**第一个改动块**（部分接受）。"""
        proposal = self._current_proposal()
        if proposal is None:
            self.diff_open = False
            return self.push()
        path = proposal.path
        if not proposal.patch.hunks:
            self.review.accept(path)
        else:
            self.review.accept(path, selected_hunks=[0])
        self._after_review(path, "已接受第一个改动块")
        self.diff_index = 0

    def cmd_diff_reject_all(self, *_args) -> None:
        count = self.review.reject()
        self.diff_index = 0
        self._after_review(None, f"已驳回 {count} 处改动")

    def on_diff_accept(self, *_args) -> None:
        """对话框「接受」：写入磁盘。"""
        proposal = self._current_proposal()
        path = proposal.path if proposal else None
        self.review.accept(path)
        self._after_review(path, f"已接受 {path}" if path else "已接受全部改动")

    def on_diff_reject(self, *_args) -> None:
        """对话框「驳回」（也是关闭按钮）：丢弃改动。"""
        proposal = self._current_proposal()
        path = proposal.path if proposal else None
        self.review.reject(path)
        self._after_review(path, f"已驳回 {path}" if path else "已驳回全部改动")

    # -- 工程面板（大纲 / 搜索 / 补全）------------------------------------
    #
    # 类级默认值用**元组**而不是列表：不可变，就不会出现"多个实例共享一个
    # 可变默认值"的经典事故。处理器里一律整体重新赋值。

    _search_panel: tuple = ()          # list[PanelRow]，反解用
    _search_rows: tuple = ()           # list[str]，给 <List items=...>
    _search_line: str = "输入关键词后回车搜索整个项目"
    _outline_panel: tuple = ()
    _outline_row_texts: tuple = ()
    _outline_line: str = "大纲：打开一个文件后显示符号"

    def refresh_outline(self) -> None:
        path = self.state.active_file
        if not path or path.startswith("("):
            self._outline_panel = ()
            self._outline_row_texts = ()
            self._outline_line = "大纲：打开一个文件后显示符号"
            return
        rows = outline_rows(self.state.index, path)
        self._outline_panel = tuple(rows)
        self._outline_row_texts = tuple(rows_for_list(rows))
        if not rows:
            self._outline_line = f"大纲：{path} 没有提取到符号"
        else:
            precise = sum(1 for row in rows if "正则推断" not in row.text)
            self._outline_line = (f"大纲：{path} · {len(rows)} 个符号"
                                  f"（精确解析 {precise}）")

    def on_search_run(self, value: str = "", *_args) -> None:
        """在「工程」页回车 → 跨文件搜索。``/`` 开头按正则处理。"""
        query = (value or "").strip()
        if not query:
            self._search_panel = ()
            self._search_rows = ()
            self._search_line = "输入关键词后回车搜索整个项目"
            return self.push()
        regex = query.startswith("/")
        needle = query[1:] if regex else query
        rows = search_rows(self.state.workspace, needle, regex=regex, limit=200)
        self._search_panel = tuple(rows)
        self._search_rows = tuple(rows_for_list(rows))
        mode = "正则" if regex else "文本"
        self._search_line = (f"搜索「{needle}」（{mode}）：{len(rows)} 个命中"
                             if rows else f"搜索「{needle}」（{mode}）：没有命中")
        self._set_status(self._search_line)
        self.push()

    def on_search_select(self, value: str = "", *_args) -> None:
        """点击搜索结果 → 打开那个文件并跳到对应行。"""
        row = row_for_label(list(self._search_panel), value)
        if row is None or not row.path:
            return
        if row.path != self.state.active_file:
            self.open_file(row.path)
        self.goto_line(row.line)
        self.push()

    def on_outline_select(self, value: str = "", *_args) -> None:
        """点击大纲条目 → 跳到该符号所在行。"""
        row = row_for_label(list(self._outline_panel), value)
        if row is None:
            return
        self.goto_line(row.line)
        self.push()

    def _completion_line(self) -> str:
        """编辑器当前内容的补全提示（离线、纯前缀匹配）。

        有控件时用**真实光标位置之前的文本**；拿不到控件才退化成整个缓冲区。
        只用整个缓冲区是错的：那样永远在看文件末尾，而末尾的词通常没有可补全的
        前缀，提示会莫名其妙为空。
        """
        text = self.state.editor_text or ""
        if not text.strip():
            return ""
        before = text
        widget = self._editor_widget()
        if widget is not None:
            try:
                before = text[:widget.textCursor().position()]
            except Exception:      # noqa: BLE001 - 不同 Qt 版本 API 有差异
                before = text
        completer = Completer(self.state.index, workspace=self.state.workspace)
        return completion_text(completer, before, self.state.active_file or "",
                               limit=8)

    def goto_line(self, line: int) -> bool:
        """把编辑器光标移到第 ``line`` 行。

        拿不到编辑器控件时**不静默失败**：状态栏会说明光标没能移动。
        """
        if line <= 0:
            return False
        offset = line_offset(self.state.editor_text, line)
        widget = self._editor_widget()
        if widget is None:
            self._set_status(f"已定位到第 {line} 行（拿不到编辑器控件，光标未移动）")
            return False
        try:
            from PySide6.QtGui import QTextCursor

            cursor = widget.textCursor()
            cursor.setPosition(min(offset, len(widget.toPlainText())))
            widget.setTextCursor(cursor)
            widget.centerCursor()
            self._set_status(f"已跳到第 {line} 行")
            return True
        except Exception as exc:      # noqa: BLE001 - 不同 Qt 版本 API 有差异
            self._set_status(f"跳转失败：{type(exc).__name__}: {exc}")
            return False

    def _editor_widget(self):
        """取编辑器底层控件（``Runtime.query`` 的选择器语法可能变化，逐个试）。"""
        if self.runtime is None:
            return None
        for selector in ("#editor", "editor", "TextArea#editor"):
            try:
                element = self.runtime.query(selector)
            except Exception:      # noqa: BLE001
                continue
            component = getattr(element, "component", None) or element
            widget = getattr(component, "widget", None)
            if widget is not None:
                return widget
        return None

    # -- 预览面板（图片 / 文本 / 二进制 hex）-----------------------------

    _preview_caption: str = "预览：在左侧选择一个文件"
    _preview_image: str = ""
    _preview_text: str = ""
    _preview_kind: str = "none"
    #: 树里选中的文件（可能是二进制/图片，载不进编辑器，但预览要用它）
    _preview_path: str = ""

    def _refresh_preview(self) -> None:
        """按当前文件类型准备预览内容。

        图片只有在**魔数确认**之后才会填进 ``<Image src>`` —— 因为 PawUI 的
        ``<Image>`` 在 src 非空但加载失败时会抛 ``RenderError``，直接把界面打崩。
        预览路径优先用 ``_preview_path``（树里点的那个文件，可能是二进制），
        其次才是编辑器里打开的文件。
        """
        path = self._preview_path or self.state.active_file
        if not path or path.startswith("("):
            self._preview_kind = "none"
            self._preview_image = ""
            self._preview_text = ""
            self._preview_caption = "预览：在左侧选择一个文件"
            return
        kind = preview_kind(self.state.workspace, path)
        self._preview_kind = kind
        self._preview_image = preview_image_path(self.state.workspace, path)
        if kind == "text":
            self._preview_text = preview_text(self.state.workspace, path)
        elif kind == "hex":
            self._preview_text = preview_hex(self.state.workspace, path)
        elif kind == "image":
            self._preview_text = "（图片已在上方按图像显示）"
        elif kind == "empty":
            self._preview_text = "（空文件）"
        else:
            self._preview_text = ""
        self._preview_caption = preview_caption(self.state.workspace, path, kind=kind)
        self._apply_preview_image()

    def _preview_image_widget(self):
        """取预览用的 QLabel（``<Image>`` 就是 QLabel）。

        防御性取值：接线测试会注入只带 ``state`` 的**假 runtime**，
        直接访问 ``self.runtime.root`` 会 AttributeError。
        """
        if self.runtime is None:
            return None
        root = getattr(self.runtime, "root", None)
        if root is None or not hasattr(root, "findChild"):
            return None
        from PySide6.QtWidgets import QLabel

        return root.findChild(QLabel, "preview_image")

    def _apply_preview_image(self) -> bool:
        """**命令式**把图片设进控件。

        为什么不用 ``<Image src="{$preview_image}">`` 的模板绑定：实测 + 读源码确认
        **PawUI 的 `<Image>` 不响应状态变化** —— ``Image.build()`` 只在建树时
        ``opt_str("src")`` 读一次，没有 ``bind_state``/``watch_state``；而
        Text/Markdown/List/Table/Select/TextArea 都支持模板刷新。
        症状是"状态推对了、控件里始终没有图像"，界面自检抓到了它。

        命令式设置还有个附带好处：完全绕开了 ``<Image>`` 在 src 非空但加载失败时
        抛 ``RenderError`` 把界面打崩的风险。
        """
        label = self._preview_image_widget()
        if label is None:
            return False
        path = self._preview_image
        if not path:
            label.clear()
            return False
        try:
            from PySide6.QtGui import QPixmap

            pixmap = QPixmap(path)
            if pixmap.isNull():
                label.clear()
                return False
            label.setPixmap(pixmap)
            return True
        except Exception:      # noqa: BLE001 - Qt 版本差异
            label.clear()
            return False

    # -- xterm 风格终端（独立窗口）----------------------------------------

    _term_session = None
    _term_bridge = None
    _term_channel = None
    _term_view_obj = None
    _term_ready: bool = False

    # -- Web 编辑器（<Web> + QWebChannel）---------------------------------

    _web_editor_line: str = "Web 编辑器：加载 editor.html（离线，无外部依赖）"

    def _web_view(self):
        """返回应用自己持有的 Web 编辑器视图（**不在主窗口里**）。

        为什么不在 ``app.paw`` 里放 ``<Web>``：实测（第 16 轮）把 `<Web>` 常驻主窗口
        会让 WebEngine 的渲染进程在建树早期抢占事件循环，界面自检从 15/16 掉到
        **8/16**。所以改成**独立顶层窗口 + 懒创建**：只有用户显式打开时才创建。
        """
        return self._web_view_obj

    def cmd_open_web_editor(self, *_args) -> bool:
        """打开（或再次激活）独立的 Web 编辑器窗口。"""
        fresh = self._web_bridge is None
        if not self._ensure_web_bridge():
            self.push()
            return False
        view = self._web_view_obj
        if view is not None:
            view.show()
            view.raise_()
        if fresh:
            # **只有新建的桥**才需要等页面 ready。页面是"连上过一次就只发一次
            # ready"，所以重开窗口时把它重置成 False 会让同步**永久停摆**
            # （实测：重开之后 _web_ready 永远为 False，文本再也推不过去）。
            self._web_ready = False
        self._push_web_state()
        self.push()
        return True

    def _ensure_web_bridge(self) -> bool:
        """创建独立 Web 编辑器窗口：**先 setWebChannel，后 setUrl**。

        顺序是硬要求（``tools/probe_webchannel.py`` 实测）：反过来页面的 ``boot()``
        会先跑完并判定"没有 Qt"而走 standalone 分支，于是**永远不 ready**，
        而且它发的 ready 也到不了 Python（standalone 只打日志）。
        """
        if self._web_bridge is not None:
            return True
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtWebChannel import QWebChannel
            from PySide6.QtWebEngineCore import QWebEngineSettings
            from PySide6.QtWebEngineWidgets import QWebEngineView

            from tcide.web.bridge import EDITOR_HTML, engine_description, make_bridge

            view = QWebEngineView()
            view.setWindowTitle("TCIDE · Web 编辑器")
            view.resize(960, 640)
            page = view.page()
            page.settings().setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

            channel = QWebChannel(page)
            bridge = make_bridge(on_event=self._on_editor_event)
            channel.registerObject("editorBridge", bridge)
            page.setWebChannel(channel)                 # ← 必须在 setUrl 之前

            self._web_view_obj = view
            self._web_channel = channel
            self._web_bridge = bridge
            self._web_editor_line = f"Web 编辑器：{engine_description()}，正在加载页面…"
            page.setUrl(QUrl.fromLocalFile(str(EDITOR_HTML)))   # ← 之后才加载
            view.show()
            return True
        except Exception as exc:      # noqa: BLE001 - WebEngine 版本/环境差异
            self._web_editor_line = f"Web 编辑器不可用：{type(exc).__name__}: {exc}"
            self._set_status(self._web_editor_line)
            return False

    def _push_web_state(self) -> None:
        """把当前文件内容与行标记推给页面（页面 ready 之后才有效）。

        **注意：默认窗口里没有 `<Web>`，所以这个方法通常直接返回。**
        实测（``tools/probe_webchannel.py``）：QtWebEngine + QWebChannel 整条链路
        在 offscreen 下可用，但把 `<Web>` 常驻主窗口会在建树早期抢占事件循环，
        把界面自检从 15/16 打到 8/16。所以要接入必须先解决"惰性创建/独立窗口"，
        见 FEATURE_MATRIX §3 的说明。
        """
        if self._web_bridge is None and not self._ensure_web_bridge():
            return
        if not self._web_ready or self._web_syncing:
            return
        from tcide.lint import Linter
        from tcide.web.bridge import (encode_markers, encode_text,
                                      markers_from_issues, markers_from_patch)

        messages = [encode_text(self.state.active_file, self.state.editor_text)]
        proposal = self._current_proposal()
        if proposal is not None:
            messages.append(encode_markers(markers_from_patch(proposal.patch)))
        else:
            # 就地取当前文件的诊断（不依赖 AppState 的字段名，显示的也正是
            # 这个文件的真实问题）
            issues = (Linter(self.state.workspace).lint_file(self.state.active_file)
                      if self.state.active_file else [])
            messages.append(encode_markers(markers_from_issues(issues)))
        for message in messages:
            self._web_bridge.deliver.emit(json.dumps(message, ensure_ascii=False))

    def _on_editor_event(self, event) -> None:
        """Web 编辑器发回的事件 → 同步进应用状态。"""
        if event.kind == "ready":
            self._web_ready = True
            self._web_editor_line = f"Web 编辑器：已连接（{event.message or 'ok'}）"
            self._push_web_state()
        elif event.kind == "change":
            self._web_syncing = True
            try:
                self.state.editor_text = event.text
                self.state.dirty = True
                self.push(_skip_web=True)
            finally:
                self._web_syncing = False
        elif event.kind == "command":
            handler = {"save": self.cmd_save, "palette": self.cmd_palette,
                       "toggle_sidebar": self.cmd_toggle_sidebar,
                       "toggle_dock": self.cmd_toggle_dock}.get(event.name)
            if handler is not None:
                handler()
        elif event.kind == "error":
            self._set_status(f"Web 编辑器报错：{event.message}")

    def cmd_split_open(self, *_args) -> bool:
        """把**下一个标签**推到分屏里（只读参考视图）。

        为什么分屏在 Web 页面里做：PawUI 的 ``<SplitPane>`` 是构建期静态的，
        而 Monaco 支持多实例 —— 在页面内分屏既自然又不受那个限制。
        """
        from tcide.web.bridge import encode_split

        if self._web_bridge is None:
            if not self.cmd_open_web_editor():
                return False
        item = self.open_files.next()
        if item is None:
            self._set_status("分屏：先打开至少一个文件")
            self.push()
            return False
        try:
            text = self.state.workspace.read(item.path)
        except WorkspaceError as exc:
            self._set_status(f"分屏读取失败：{exc}")
            self.push()
            return False
        self._web_split_path = item.path
        self._web_bridge.deliver.emit(json.dumps(
            encode_split(item.path, text,
                         language=self.state.workspace.language_of(item.path)),
            ensure_ascii=False))
        self._set_status(f"分屏：{item.path}")
        self.push()
        return True

    def cmd_split_close(self, *_args) -> None:
        from tcide.web.bridge import encode_split_close

        self._web_split_path = ""
        if self._web_bridge is not None:
            self._web_bridge.deliver.emit(json.dumps(encode_split_close()))
        self._set_status("分屏已关闭")
        self.push()

    @staticmethod
    def _web_editor_url() -> str:
        """离线编辑器页面的 file:// URL（<Web src=...> 用它）。"""
        try:
            from tcide.web.bridge import editor_url

            return editor_url()
        except Exception:      # noqa: BLE001
            return ""

    # -- xterm 风格终端（独立窗口，复用同一个桥工厂）----------------------

    def cmd_open_terminal(self, *_args) -> bool:
        """打开独立的终端窗口（懒创建；不影响主窗口事件循环）。"""
        fresh = self._term_bridge is None
        if not self._ensure_terminal_bridge():
            self.push()
            return False
        view = self._term_view_obj
        if view is not None:
            view.show()
            view.raise_()
        if fresh:
            # 同 Web 编辑器：重开窗口**不能**重置 ready —— 页面只发一次 ready
            self._term_ready = False
        self.push()
        return True

    def _ensure_terminal_bridge(self) -> bool:
        """与 Web 编辑器同样的顺序要求：**先 setWebChannel、后 setUrl**。"""
        if self._term_bridge is not None:
            return True
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtWebChannel import QWebChannel
            from PySide6.QtWebEngineCore import QWebEngineSettings
            from PySide6.QtWebEngineWidgets import QWebEngineView

            from tcide.term import TerminalSession
            from tcide.web.bridge import TERMINAL_HTML, make_bridge

            view = QWebEngineView()
            view.setWindowTitle("TCIDE · 终端")
            view.resize(860, 520)
            page = view.page()
            page.settings().setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

            channel = QWebChannel(page)
            bridge = make_bridge(on_event=self._on_terminal_event)
            channel.registerObject("terminalBridge", bridge)   # 名字与页面一致
            page.setWebChannel(channel)

            self._term_view_obj = view
            self._term_bridge = bridge
            self._term_channel = channel
            if self._term_session is None:
                self._term_session = TerminalSession(self._terminal_runner)
            page.setUrl(QUrl.fromLocalFile(str(TERMINAL_HTML)))
            view.show()
            return True
        except Exception as exc:      # noqa: BLE001
            self._set_status(f"终端不可用：{type(exc).__name__}: {exc}")
            return False

    def _terminal_runner(self, command: str) -> tuple[int, str, str]:
        """在工作区里真跑一条命令（由工作线程调用）。"""
        import subprocess

        completed = subprocess.run(          # noqa: S602 - 这是终端，本来就要 shell
            command, shell=True, cwd=str(self.state.root), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120)
        return completed.returncode, completed.stdout or "", completed.stderr or ""

    def _send_to_terminal(self, message: dict) -> None:
        if self._term_bridge is None:
            return
        self._term_bridge.deliver.emit(json.dumps(message, ensure_ascii=False))

    def _on_terminal_event(self, event) -> None:
        """终端页发回的事件。"""
        from tcide.term import (TermLine, encode_clear, encode_output,
                                encode_prompt)

        if event.kind == "ready":
            self._term_ready = True
            self._send_to_terminal(encode_prompt(self._term_prompt()))
            self._send_to_terminal(encode_output([
                TermLine(f"工作区: {self.state.root}（命令在这里真实执行）",
                         "info")]))
            return
        if event.kind == "history":
            session = self._term_session
            if session is None:
                return
            direction = str(event.raw.get("direction") or "previous")
            text = session.previous() if direction == "previous" else session.next()
            view = self._term_view_obj
            if view is not None:
                view.page().runJavaScript(
                    f"window.tcideSetInput({json.dumps(text, ensure_ascii=False)})")
            return
        if event.kind == "command":
            text = (event.text or "").strip()
            if not text:
                return
            if text.split()[0].lower() == "clear":
                self._term_session.run(text) if self._term_session else None
                self._send_to_terminal(encode_clear())
                return
            # 在**工作线程**里跑：命令可能很久，不能卡住界面。
            # Qt 信号跨线程是队列投递，所以直接 emit 是安全的。
            threading.Thread(target=self._run_terminal_command, args=(text,),
                             daemon=True).start()
            return
        if event.kind == "error":
            self._set_status(f"终端报错：{event.message}")

    def _run_terminal_command(self, text: str) -> None:
        """**工作线程**：只做慢的子进程执行，界面更新全部投递回 GUI 线程。"""
        session = self._term_session
        if session is None:
            return
        lines = session.run(text)
        self._post_to_gui(self._finish_terminal_command, lines, text)

    def _finish_terminal_command(self, lines: list, text: str) -> None:
        """**GUI 线程**：把终端输出写回页面（runJavaScript 必须在本线程）。"""
        from tcide.term import encode_output

        self._send_to_terminal(encode_output(lines, echo=text))
        self._send_to_terminal({"type": "prompt", "prompt": self._term_prompt()})

    def _term_prompt(self) -> str:
        from tcide.term import format_prompt

        return format_prompt(self.state.root.name if self.state.root else "")

    def _after_review(self, path: str | None, message: str) -> None:
        """审核动作之后的公共收尾：关窗、刷新、必要时重载编辑器内容。"""
        self._append_chat("·", message)
        if path and self.state.active_file == path:
            self.refresh_file(path)
        if not self.review.pending():
            self.diff_open = False
        self.refresh_tree()
        self.refresh_git()
        self._set_status(message)
        self.push()

    def _offline_run(self, requirement: str) -> None:
        """离线演练：跑通 Builder/Coder 编排但不联网、**不写任何文件**。"""
        model = OfflineModel()
        tools = ToolRegistry(self.tool_workspace, index=self.state.index,
                             repo=self.state.repo)
        # 演练只读：摘掉所有写操作，避免"演练"真的改了用户的文件
        for name in ("write_file", "edit_file", "run_command"):
            tools.tools.pop(name, None)
        orchestrator = AgentOrchestrator(
            model, tools, max_parallel_coders=1, max_coder_retry=0,
            review_gate_enabled=False, on_event=self._on_agent_event)
        report = orchestrator.run(requirement)
        self._append_chat("TCIDE", f"离线演练完成：{report.summary()}"
                                   "（未调用任何模型 API，也未修改任何文件）")

    def cmd_agent_dry_run(self, *_args) -> None:
        self._offline_run("（离线演练）")
        self.state.status_left = "离线演练完成"
        self.push()

    def on_chat_send(self, text: str = "", *_args) -> None:
        requirement = str(text).strip()
        if not requirement:
            return
        self._append_chat("你", requirement)
        if not self.state.api_key:
            self._offline_run(requirement)
            self.push()
            return
        self._append_chat("TCIDE", "正在调用模型（在线模式）…")
        self.push()
        threading.Thread(target=self._online_run, args=(requirement,),
                         daemon=True).start()

    def _online_run(self, requirement: str) -> None:
        """**工作线程**：只跑模型编排，界面更新投递回 GUI 线程。

        （第一版在这里直接 ``self.push()``，于是 PawUI 在**工作线程**里更新
        Markdown 控件 —— Qt 报 "Cannot create children for a parent that is in
        a different thread. (Parent is QTextDocument...)"。）
        """
        try:
            config = ModelConfig(provider=self.state.provider, model=self.state.model,
                                 api_key=self.state.api_key)
            from tcide.agent import AdapterClient

            report = self.make_orchestrator(AdapterClient(config)).run(requirement)
            outcome = f"完成：{report.summary()}"
        except (ModelError, GitError, OSError) as exc:
            outcome = f"失败：{exc}"
        self._post_to_gui(self._finish_online_run, outcome)

    def _finish_online_run(self, outcome: str) -> None:
        """**GUI 线程**：写入对话并刷新界面。"""
        self._append_chat("TCIDE", outcome)
        self.push()

    def cmd_clear_chat(self, *_args) -> None:
        self.state.chat_log = "_（已清空）_\n"
        self.push()

    # -- 命令面板 ---------------------------------------------------------

    def build_palette(self) -> None:
        self.state.palette_rows = [
            "保存当前文件   Ctrl+S",
            "打开项目…",
            "全部暂存 (git add -A)",
            "提交（用建议的提交信息）",
            "跑项目测试",
            "全项目静态检查",
            "刷新文件树",
            "刷新 Git 状态",
            "折叠/展开资源管理器   Ctrl+B",
            "折叠/展开底部面板     Ctrl+`",
            "打开 Web 编辑器（独立窗口）",
            "打开终端（独立窗口）",
            "工具菜单（Web 编辑器/终端/分屏/LSP/检查）",
            "新建示例文件并打开",
            "分屏打开下一个标签（只读参考）",
            "关闭分屏",
            "LSP 状态（哪些语言服务器可用）",
            "LSP 启动（为当前文件接上诊断）",
            "LSP 停止",
            "关闭当前标签",
            "下一个标签",
            "上一个标签",
            "清空 AI 对话",
        ]
        self.state.palette_filtered = list(self.state.palette_rows)

    def cmd_palette(self, *_args) -> None:
        self.state.palette_open = True
        self.state.palette_filtered = list(self.state.palette_rows)
        self.push()

    def on_palette_filter(self, text: str = "", *_args) -> None:
        needle = str(text).strip().lower()
        self.state.palette_filtered = [row for row in self.state.palette_rows
                                       if needle in row.lower()]
        self.push()

    def on_palette_pick(self, value: str = "", *_args) -> None:
        self.state.palette_picked = str(value)
        self.state.status_left = f"已选择：{value}"

    def on_palette_accept(self, *_args) -> None:
        picked = self.state.palette_picked or (
            self.state.palette_filtered[0] if self.state.palette_filtered else "")
        self.state.palette_open = False
        if picked:
            self.run_palette_command(picked)
        self.push()

    def on_palette_close(self, *_args) -> None:
        self.state.palette_open = False
        self.push()

    def run_palette_command(self, label: str) -> None:
        """按标签前缀分派命令（可预测、可测）。"""
        table = [
            ("保存当前文件", self.cmd_save),
            ("打开项目", self.cmd_open_project),
            ("全部暂存", self.cmd_git_stage),
            ("提交", self.cmd_git_commit),
            ("跑项目测试", self.cmd_run_tests),
            ("全项目静态检查", self.cmd_check_all),
            ("刷新文件树", self.cmd_refresh_tree),
            ("刷新 Git", self.cmd_refresh_git),
            ("折叠/展开资源管理器", self.cmd_toggle_sidebar),
            ("折叠/展开底部面板", self.cmd_toggle_dock),
            ("打开 Web 编辑器", self.cmd_open_web_editor),
            ("打开终端", self.cmd_open_terminal),
            ("工具菜单", self.cmd_tools_open),
            ("新建示例文件", self.cmd_open_sample),
            ("分屏打开下一个标签", self.cmd_split_open),
            ("关闭分屏", self.cmd_split_close),
            ("LSP 状态", self.cmd_lsp_status),
            ("LSP 启动", self.cmd_lsp_start),
            ("LSP 停止", self.cmd_lsp_stop),
            ("关闭当前标签", self.cmd_tab_close),
            ("下一个标签", self.cmd_tab_next),
            ("上一个标签", self.cmd_tab_previous),
            ("清空 AI 对话", self.cmd_clear_chat),
        ]
        for prefix, handler in table:
            if label.startswith(prefix):
                handler()
                self.state.status_left = f"已执行：{prefix}"
                return
        self.state.status_left = f"未识别的命令：{label}"

    # -- 其它命令 ---------------------------------------------------------

    def cmd_open_project(self, *_args) -> None:
        """无对话框时的退化行为：重新扫描当前项目（原生目录选择留给后续）。"""
        self.state.index.scan()
        self.state.expanded.clear()
        self.refresh_tree()
        self.refresh_git()
        self.state.status_left = f"已重新扫描 {self.state.root}"
        self.push()

    def cmd_refresh_tree(self, *_args) -> None:
        self.refresh_tree()
        self.state.status_left = "文件树已刷新"
        self.push()

    def cmd_refresh_git(self, *_args) -> None:
        self.refresh_git()
        self.state.status_left = "Git 状态已刷新"
        self.push()

    def cmd_check_all(self, *_args) -> None:
        result = self.state.tools.call("get_diagnostics", {})
        self.set_terminal(result.output or result.error)
        problems = result.meta.get("problems") or []
        self.state.status_left = ("全项目检查完成：" +
                                  ("无问题" if result.ok else f"{len(problems)} 个问题"))
        self.push()

    def cmd_toggle_sidebar(self, *_args) -> None:
        self.state.sidebar_open = not self.state.sidebar_open
        self.push()

    def cmd_toggle_dock(self, *_args) -> None:
        self.state.dock_open = not self.state.dock_open
        self.push()

    def cmd_toggle_git(self, *_args) -> None:
        self.state.git_open = not self.state.git_open
        self.push()

    def on_sidebar_toggle(self, *_args) -> None:
        self.state.sidebar_open = not self.state.sidebar_open
        self.push()

    def on_git_toggle(self, *_args) -> None:
        self.state.git_open = not self.state.git_open
        self.push()

    def on_dock_toggle(self, *_args) -> None:
        self.state.dock_open = not self.state.dock_open
        self.push()

    def on_provider_change(self, value: str = "", *_args) -> None:
        key = str(value).split(" · ")[0].strip()
        if key in PRESETS:
            self.state.provider = key
            self.state.model = PRESETS[key].model
        self.state.status_left = f"模型切换为 {self.state.provider}/{self.state.model}"
        self.push()


# ---------------------------------------------------------------------------
# 运行时构造
# ---------------------------------------------------------------------------

def create_runtime(root: str | Path = ".", *, provider: str = "deepseek",
                   api_key: str = "", paw_file: Path | None = None):
    """构造 (Runtime, TCIDEApp)；调用方负责 run()。"""
    from pawui.runtime import Runtime

    app = TCIDEApp(root, provider=provider, api_key=api_key)
    source = (paw_file or PAW_FILE).read_text(encoding="utf-8")
    runtime = Runtime(source, str(paw_file or PAW_FILE), context=app.context())
    app.runtime = runtime
    return runtime, app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tcide-pawui",
                                     description="虎猫 TCIDE · PawUI 版")
    parser.add_argument("project", nargs="?", default=".", help="项目目录")
    parser.add_argument("--shot", help="离屏渲染并截图到指定 png")
    parser.add_argument("--selftest", action="store_true", help="离屏跑交互自检")
    parser.add_argument("--version", action="version",
                        version=f"虎猫 TCIDE · PawUI 版 {__version__}")
    parser.add_argument("--provider", default="deepseek", help="模型服务商")
    parser.add_argument("--api-key", default="", help="API Key（留空则离线演练）")
    args = parser.parse_args(argv)

    qapp = None
    if args.shot or args.selftest:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        qapp = QApplication.instance() or QApplication([])

    runtime, app = create_runtime(args.project, provider=args.provider,
                                 api_key=args.api_key)

    if args.selftest:
        from app_selftest import run_selftest

        runtime.run(block=False)          # 必须先建树，否则 runtime.root 是 None
        app.bootstrap()
        for _ in range(6):
            qapp.processEvents()
        return run_selftest(runtime, app, qapp, verbose=True)

    runtime.run(block=False)
    app.bootstrap()
    if args.shot:
        from PySide6.QtCore import QTimer

        def grab() -> None:
            target = Path(args.shot)
            target.parent.mkdir(parents=True, exist_ok=True)   # 目录不存在时
            pixmap = runtime.root.grab()
            saved = pixmap.save(str(target))                    # save() 会静默失败
            print(f"{'已保存截图' if saved else '★ 截图保存失败'}：{target}"
                  f"（{pixmap.width()}x{pixmap.height()}）")
            qapp.quit()

        QTimer.singleShot(400, grab)
        return qapp.exec()
    runtime.run(block=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
