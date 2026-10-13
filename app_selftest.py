# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""界面自检：离屏把 IDE 的关键交互跑一遍（**应用侧**，GPL-3.0）。

原在 tcide/shell_selftest.py；为了让 LGPL 库（tcide/）保持零界面依赖，
移到了应用侧。依赖 PySide6（LGPL）与 PawUI（GPL），所以它属于应用层。
第一行原文档字符串：

`python main.py --selftest` 与 `tests/test_shell.py` **共用这一套检查**，
避免"命令行自检"和"测试套件"两处逻辑漂移。

每个检查项返回 ``(名称, 是否通过, 细节)``，不抛异常 —— 一次跑完能看到全部问题。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Callable

Check = tuple[str, bool, str]


def snapshot(workspace) -> dict[str, str]:
    """项目内所有文件的 sha1，用来验证自检没有改动任何东西。"""
    result: dict[str, str] = {}
    for relative in workspace.iter_files():
        try:
            data = workspace.resolve(relative).read_bytes()
        except OSError:
            continue
        result[relative] = hashlib.sha1(data).hexdigest()
    return result


def _run_checks(runtime, app, qapp) -> list[Check]:
    from PySide6.QtWidgets import QLabel, QListWidget, QPlainTextEdit

    results: list[Check] = []
    # 自检前后的项目快照：自检**不允许**改动任何项目文件（有事故前科）
    before_snapshot = snapshot(app.state.workspace)
    # 也**不允许**改动 git 历史：自检曾经真的执行了命令面板里的「提交」，
    # 每次跑都把项目仓库提交一次，而文件内容一个字节没变（哈希检查抓不到）。
    before_head = ""
    try:
        from tcide.gitops import GitRepo

        repo = GitRepo(app.state.root)
        if repo.owns_repo():
            before_head = repo.head_sha()
    except Exception:      # noqa: BLE001 - 没有 git 就跳过这项守卫
        before_head = ""

    def check(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, detail))

    def pump(times: int = 8) -> None:
        for _ in range(times):
            qapp.processEvents()

    def list_widget(name: str):
        return runtime.root.findChild(QListWidget, name)

    # ---- 建树 -----------------------------------------------------------
    check("窗口标题", lambda: (runtime.root.windowTitle().startswith("虎猫 TCIDE"),
                              runtime.root.windowTitle()))
    check("窗口尺寸", lambda: (runtime.root.width() >= 1200 and runtime.root.height() >= 700,
                              f"{runtime.root.width()}x{runtime.root.height()}"))

    def tree_populated() -> tuple[bool, str]:
        widget = list_widget("tree")
        count = widget.count() if widget else -1
        return count > 0, f"{count} 行，示例：{(app.state.tree_rows or ['(空)'])[:2]}"

    check("文件树有内容", tree_populated)

    # ---- 打开文件 -------------------------------------------------------
    file_row = next((row for row in app.state.tree_rows
                     if app.state.tree_paths.get(row, "").endswith(".py")), "")
    if not file_row:
        file_row = next((row for row in app.state.tree_rows if row), "")

    def open_file() -> tuple[bool, str]:
        app.on_tree_select(file_row)
        pump()
        editor = runtime.root.findChild(QPlainTextEdit, "editor")
        text = editor.toPlainText() if editor else ""
        expected = app.state.editor_text
        if text == expected:
            return True, f"{app.state.active_file} · {len(text)} 字符"
        # 失败时给出可定位的差异，而不是只说"不相等"
        first = next((index for index, (a, b) in enumerate(zip(text, expected))
                      if a != b), min(len(text), len(expected)))
        return False, (f"{app.state.active_file}：控件 {len(text)} 字符 / "
                       f"状态 {len(expected)} 字符；首个差异 @{first} "
                       f"({text[first:first + 12]!r} vs {expected[first:first + 12]!r})")

    check("点击文件树打开文件", open_file)

    # ---- 展开目录 -------------------------------------------------------
    dir_row = next((row for row in app.state.tree_rows
                    if "▸" in row and app.state.tree_paths.get(row)), "")

    def expand_dir() -> tuple[bool, str]:
        if not dir_row:
            return True, "（没有可展开的目录，跳过）"
        before = len(app.state.tree_rows)
        path = app.state.tree_paths[dir_row]
        app.on_tree_select(dir_row)
        pump()
        expanded = path in app.state.expanded
        return expanded and len(app.state.tree_rows) >= before, \
            f"{path} 展开后 {len(app.state.tree_rows)} 行"

    check("点击目录可展开", expand_dir)

    # ---- 编辑与保存 -----------------------------------------------------
    def edit_and_save() -> tuple[bool, str]:
        if not app.state.active_path:
            return False, "没有打开文件"
        recorded = {"path": "", "content": ""}
        real_write = app.state.workspace.write

        def fake_write(relative, content, **kwargs):
            # 自检**绝不写真实文件**，只记录。
            # 曾经这里把 spy 转发给真实写盘，结果把项目里的 main.py 覆盖成了
            # 自检的测试内容（1 行 `value = 1`）—— 这是必须挡住的事故。
            recorded["path"] = str(relative)
            recorded["content"] = str(content)
            return app.state.workspace.resolve(relative)

        app.state.workspace.write = fake_write       # type: ignore[assignment]
        try:
            app.on_editor_change("value = 1\n# 由自检写入（不落盘）\n")
            pump()
            dirty_shown = app.state.dirty
            app.cmd_save()
            pump()
        finally:
            app.state.workspace.write = real_write   # type: ignore[assignment]
        ok = (dirty_shown and not app.state.dirty
              and recorded["path"] == app.state.active_path
              and "由自检写入" in recorded["content"])
        return ok, (f"脏标记 {dirty_shown} → {app.state.dirty}；记录写入 "
                    f"{recorded['path']}（{len(recorded['content'])} 字符，未落盘）")

    check("编辑器改动→脏标记→保存", edit_and_save)

    # ---- 终端 -----------------------------------------------------------
    def terminal_run() -> tuple[bool, str]:
        app.on_terminal_run("echo tcide-shell-ok")
        pump()
        return "tcide-shell-ok" in app.state.terminal_log, \
            app.state.terminal_log.strip().splitlines()[-1][:60]

    check("终端执行命令并回显", terminal_run)

    # ---- 静态检查 -------------------------------------------------------
    def diagnostics() -> tuple[bool, str]:
        # 用**临时文件**做检查：不要往用户正在编辑的文件里注入语法错误。
        # （上一版就是这么写的，注入完再拿 `value = 1` 覆盖回去 —— 文件被毁。）
        temp = f".tcide-selftest-{uuid.uuid4().hex[:8]}.py"
        try:
            app.state.workspace.write(temp, "def broken(:\n")
            result = app.state.tools.call("get_diagnostics", {"path": temp})
        finally:
            try:
                app.state.workspace.delete(temp)
            except Exception:  # noqa: BLE001
                pass
        problems = result.meta.get("problems") or []
        return bool(problems), f"临时文件里捕获 {len(problems)} 个问题（已清理）"

    check("静态检查能发现问题", diagnostics)

    # ---- 全项目检查 -----------------------------------------------------
    def check_all() -> tuple[bool, str]:
        app.cmd_check_all()
        pump()
        return "检查" in app.state.status_left, app.state.status_left[:60]

    check("全项目静态检查", check_all)

    # ---- 命令面板 -------------------------------------------------------
    def palette() -> tuple[bool, str]:
        """命令面板：打开 / 过滤 / 执行。

        **必须挑一条无副作用的命令**。第一版这里过滤的是「提交」并真的执行了，
        于是**每次自检都 git 提交一次项目仓库** —— 真实事故：项目历史里混进了
        若干条自检产生的提交（作者被写成 ``TCIDE <tcide@example.com>``，
        消息是 gitintel 的建议格式）。文件哈希检查抓不到它，因为提交不改内容。
        现在改用「刷新文件树」。
        """
        app.cmd_palette()
        pump()
        opened = app.state.palette_open
        app.on_palette_filter("刷新文件树")
        filtered = list(app.state.palette_filtered)
        app.on_palette_pick(filtered[0] if filtered else "")
        app.on_palette_accept()
        pump()
        return (opened and filtered and not app.state.palette_open,
                f"过滤出 {len(filtered)} 条，执行后状态：{app.state.status_left[:40]}")

    check("命令面板：打开/过滤/执行（用无副作用命令）", palette)

    # ---- AI 面板（离线演练）---------------------------------------------
    def offline_agent() -> tuple[bool, str]:
        before = app.state.chat_log
        app.on_chat_send("给这个项目加一个单元测试")
        pump()
        grew = len(app.state.chat_log) > len(before)
        offline = "未调用任何模型 API" in app.state.chat_log
        return grew and offline, f"对话增长 {len(app.state.chat_log) - len(before)} 字符"

    check("AI 面板离线演练（不调 API）", offline_agent)

    # ---- 折叠 -----------------------------------------------------------
    def toggles() -> tuple[bool, str]:
        sidebar = app.state.sidebar_open
        app.cmd_toggle_sidebar()
        pump()
        flipped = app.state.sidebar_open != sidebar
        dock = app.state.dock_open
        app.cmd_toggle_dock()
        pump()
        return flipped and app.state.dock_open != dock, \
            f"侧栏 {sidebar}→{app.state.sidebar_open}，底部 {dock}→{app.state.dock_open}"

    check("面板折叠/展开", toggles)

    # ---- 状态栏与动态文本 ------------------------------------------------
    def status_text() -> tuple[bool, str]:
        app.state.status_left = "自检写入的状态"
        app.push()
        pump()
        labels = [w.text() for w in runtime.root.findChildren(QLabel)]
        found = any("自检写入的状态" in text for text in labels)
        return found, f"匹配到 {sum('自检写入' in t for t in labels)} 个 Label"

    check("状态栏随状态刷新", status_text)

    # ---- 模型切换 -------------------------------------------------------
    def provider() -> tuple[bool, str]:
        app.on_provider_change("ollama · Ollama（本地）")
        pump()
        from PySide6.QtWidgets import QComboBox

        # <Select> 渲染成 QComboBox，不是 QLineEdit（第一版写错，导致检查恒失败）
        widget = runtime.root.findChild(QComboBox, "provider")
        return (app.state.provider == "ollama" and widget is not None,
                f"provider={app.state.provider}, model={app.state.model}, "
                f"控件={'QComboBox' if widget else '没找到'}")

    check("模型服务商切换", provider)

    # ---- 预览面板（图片 / 文本 / 二进制）--------------------------------
    def preview_panel() -> tuple[bool, str]:
        """预览面板必须**真的显示内容**，而不是"接上了但一直是空的"。

        走的是**真实用户路径**：从文件树点选（而不是直接调 open_file）——
        图片/二进制载不进文本编辑器，只有树选中这条路径才能验证预览。
        """
        from PySide6.QtWidgets import QLabel

        # 与**文件真实内容**比对，而不是断言某个魔法字符串：
        # 测试夹具里的 main.py 只有 16 字节的桩，写死 "def " 会误报。
        target = "main.py"
        workspace = app.state.workspace
        if not workspace.exists(target):
            target = app.state.active_file
        app.open_file(target)
        pump()
        expected = workspace.read(target)
        first_line = next((line for line in expected.splitlines() if line.strip()), "")
        if "文本" not in app._preview_caption:
            return False, f"文本预览的说明不对：{app._preview_caption}"
        if first_line and first_line.strip() not in (app._preview_text or ""):
            return False, (f"文本预览内容与文件不符（首行 {first_line[:30]!r} 不在预览里）")

        png = next((path for path in workspace.iter_files()
                    if path.lower().endswith(".png")), "")
        if not png:
            return True, "文本预览 OK（项目里没有 png，跳过图片分支）"

        is_real_png = workspace.read_bytes(png, max_bytes=8).startswith(
            b"\x89PNG\r\n\x1a\n")
        row = next((label for label, target in app.state.tree_paths.items()
                    if target == png), "")
        if not row:
            return False, f"{png} 不在文件树里，无法测试点选路径"
        app.on_tree_select(row)
        pump()

        kind = app._preview_kind
        if is_real_png and kind != "image":
            # **不能把这种情况算成"安全退回"**：真 PNG 被判成别的类型就是 bug
            # （第一版就是这么写的，把 bug 伪装成了通过）
            return False, f"真 PNG {png} 被判成 {kind}（应为 image）"
        if not is_real_png:
            return True, (f"文本预览 OK；{png} 不是真 PNG，已按 {kind} 展示")

        label = runtime.root.findChild(QLabel, "preview_image")
        if label is None:
            return False, f"找不到 preview_image 控件（{png}）"

        # 先自己试着解码一次：夹具里的 assets/logo.png 是**桩文件**（有 PNG 魔数
        # 但 libpng 报 bad header）。这种情况应用会清空控件、不崩 —— 是正确行为，
        # 不能算失败；真正要抓的是"能解码但应用没设置进去"。
        from PySide6.QtGui import QPixmap

        if QPixmap(str(workspace.resolve(png))).isNull():
            return True, (f"文本 OK；{png} 有 PNG 魔数但解码不了（桩文件），"
                          f"应用已安全清空且未崩")

        pixmap = label.pixmap()
        if pixmap is None or pixmap.isNull():
            return False, f"{png} 可解码，但控件里没有图像（预览没生效）"
        return True, f"文本 OK；图片 {png} 已显示 {pixmap.width()}x{pixmap.height()}"

    check("预览面板显示文本与图片", preview_panel)

    # ---- LSP -------------------------------------------------------------
    def lsp_wiring() -> tuple[bool, str]:
        """LSP 接线：状态可查、缺服务器时给出安装提示、启动失败不崩。

        本机**通常没装**语言服务器，所以这里验证的是"可跳过设计"：
        不能抛异常、不能假装成功、必须告诉用户怎么装。
        """
        from tcide.lsp import detect_servers

        app.cmd_lsp_status()
        pump()
        statuses = detect_servers()
        missing = [status for status in statuses if not status.available]
        for status in missing:
            if not status.hint:
                return False, f"{status.language} 缺少服务器却没给安装提示"

        if not any(status.available for status in statuses):
            started = app.cmd_lsp_start()
            if started:
                return False, "没有任何服务器可用，cmd_lsp_start 却报告成功"
            if "不可用" not in app._lsp_note and "启动失败" not in app._lsp_note:
                return False, f"启动失败的原因没说清：{app._lsp_note}"
            return True, f"状态可查（{len(statuses)} 种语言）；无服务器时如实降级"

        started = app.cmd_lsp_start()
        app.cmd_lsp_stop()
        pump()
        return True, f"状态可查；有可用服务器，启动返回 {started}"

    check("LSP 接线与可跳过降级", lsp_wiring)

    # ---- 多标签 ----------------------------------------------------------
    def tabs_wiring() -> tuple[bool, str]:
        """打开两个文件 -> 标签列表能区分它们 -> 点对应行能切回去。

        注意**不要假设行号位置**：前面的检查已经打开过别的文件，标签列表里
        不止这两项（第一版就是这么写错的，点到了别的行）。
        """
        candidates = [path for path in app.state.workspace.iter_files()
                      if path.endswith(".py")][:2]
        if len(candidates) < 2:
            return True, "项目里不足两个 .py，跳过"
        for path in candidates:
            app.open_file(path)
        pump()
        rows = app.open_files.rows()
        if len(rows) < 2:
            return False, f"标签列表只有 {len(rows)} 项"
        picked = [app.open_files.path_for_row(row) for row in rows]
        if candidates[0] not in picked or candidates[1] not in picked:
            return False, f"两个文件没有都出现在标签里：{picked}"

        labels = {}
        for row in rows:
            labels[app.open_files.path_for_row(row)] = row.split(" ", 2)[-1]
        if labels[candidates[0]] == labels[candidates[1]]:
            return False, f"两个文件显示名相同，无法区分：{labels}"

        target_row = next(row for row in rows
                          if app.open_files.path_for_row(row) == candidates[0])
        app.on_tab_select(target_row)
        pump()
        if app.state.active_path != candidates[0]:
            return False, (f"点击标签没有切换：active={app.state.active_path} "
                           f"期望={candidates[0]}")
        app.cmd_tab_close()                  # 关掉当前（未脏）
        pump()
        return True, (f"可区分、可切换、可关闭（{labels[candidates[0]]} / "
                      f"{labels[candidates[1]]}；剩 {app.open_files.count()} 个）")

    check("多标签：打开/区分/切换/关闭", tabs_wiring)

    # ---- P0 体验改进：空状态引导 + 工具菜单 -------------------------------
    def onboarding_wiring() -> tuple[bool, str]:
        """引导：底部「🚀 上手」页签存在、无文件时被自动切到前面。

        为什么断言页签而不是面板：第一版把引导做成编辑器上方的 `<Panel>`，
        结果那一列的空间被 TextArea 与标签列表占满，**面板 body 只有 57px 高、
        内容完全看不见**（tools/diagnose_onboarding.py 实测）。载体换成底部
        面板的第一个页签后才有足够空间。
        """
        from PySide6.QtWidgets import QTabWidget

        dock = runtime.root.findChild(QTabWidget, "dock_tabs")
        if dock is None:
            return False, "找不到底部页签容器 dock_tabs"
        labels = [dock.tabText(index) for index in range(dock.count())]
        if not any("上手" in label for label in labels):
            return False, f"底部面板没有「上手」页签：{labels}"

        app.state.active_path = ""
        app.state.active_file = "(未打开文件)"
        if app._onboarding_visible() is not True:
            return False, "没有打开文件时引导没有被视为该显示"
        app._focus_onboarding_tab()
        pump()
        if "上手" not in dock.tabText(dock.currentIndex()):
            return False, f"无文件时没有切到「上手」：当前是 {dock.tabText(dock.currentIndex())}"

        app.open_file("main.py")
        pump()
        if app._onboarding_visible() is not False:
            return False, "打开文件后引导没有被视为该收起"
        return True, f"{dock.count()} 个页签；无文件时自动切到「上手」"

    check("快速上手：底部页签存在且空状态自动切到它", onboarding_wiring)

    def tools_menu() -> tuple[bool, str]:
        """工具菜单：能打开、条目齐全、选中后能真的分派到处理器。"""
        app.cmd_tools_open()
        pump()
        if app.tools_open is not True or not app.tool_rows:
            return False, "工具菜单没有打开或没有条目"
        rows = list(app.tool_rows)
        must_have = ("Web 编辑器", "终端", "分屏", "LSP", "静态检查")
        missing = [key for key in must_have
                   if not any(key in row for row in rows)]
        if missing:
            return False, f"工具菜单缺少：{missing}"

        # 分派一条**无害**的（不要去触发 WebEngine 窗口，那要 20 秒）
        refresh_row = next((row for row in rows if "刷新文件树" in row), "")
        if not refresh_row:
            return False, "找不到无害的分派目标"
        app.on_tools_pick(refresh_row)
        app.on_tools_accept()
        pump()
        if app.tools_open is not False:
            return False, "执行后工具菜单没有关闭"
        if "已刷新" not in app.state.status_left:
            return False, f"没有分派到处理器：{app.state.status_left}"
        return True, f"{len(rows)} 项；分派到「刷新文件树」成功"

    check("工具菜单：打开/条目/分派", tools_menu)

    def settings_panel() -> tuple[bool, str]:
        """设置面板：打开 → 写值 → 保存 → 从磁盘读回一致。

        用 ``TCIDE_CONFIG_DIR`` 指到临时目录，**不碰真实用户配置**。
        """
        import os
        import tempfile

        from PySide6.QtWidgets import QLineEdit

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("TCIDE_CONFIG_DIR")
            os.environ["TCIDE_CONFIG_DIR"] = tmp
            try:
                app.cmd_settings_open()
                pump()
                if not app._settings_open:
                    return False, "设置面板没有打开"
                field = runtime.root.findChild(QLineEdit, "set_api_key")
                if field is None:
                    return False, '找不到输入框（app.paw 里应有 id="set_api_key"）'
                field.setText("sk-selftest-123456")
                app.on_settings_accept()
                pump()
                from tcide.settings import load as load_settings

                saved = load_settings()
                if saved.api_key != "sk-selftest-123456":
                    return False, f"保存后读回不一致：{saved.api_key!r}"
                if app._settings_open:
                    return False, "保存后面板没有关闭"
                if not app._stylesheet_applied:
                    return False, "样式表没有应用（styles/app.qss）"
                return True, "打开/写入/保存/读回一致；样式表已应用"
            finally:
                if previous is None:
                    os.environ.pop("TCIDE_CONFIG_DIR", None)
                else:
                    os.environ["TCIDE_CONFIG_DIR"] = previous
                app.settings.api_key = ""         # 不留测试值在内存里
                app.settings.model = ""

    check("设置面板：打开/写入/保存/读回 + 样式表", settings_panel)

    def tiger_theme() -> tuple[bool, str]:
        """虎猫配色必须**真的被 PawUI 应用**，而不只是写在 .paw 里。

        只检查文件内容是不够的：主题要经过 `Window theme` → `<Theme extends>` →
        `<Color>` 三层覆盖才落到 `runtime.theme` 上，任何一层没接上，界面就还是
        默认配色而文件看着"写对了"。

        期望值从 `tcide/theme.py` **读**（权威来源）—— 不在自检里再抄一份色值：
        第一版抄了一份，改配色后自检自己先红，说明抄是错的。
        """
        from tcide.theme import TIGER_DARK

        theme = getattr(runtime, "theme", None)
        if theme is None:
            return False, "拿不到 runtime.theme"
        problems = []
        for token in ("background", "surface", "accent", "text"):
            actual = str(getattr(theme, token, "")).lower()
            if actual != TIGER_DARK[token]:
                problems.append(f"{token}={actual!r}（应为 {TIGER_DARK[token]}）")
        custom = getattr(theme, "custom", {}) or {}
        if custom.get("stripe") != TIGER_DARK["stripe"]:
            problems.append(f"自定义令牌 stripe={custom.get('stripe')!r}")
        if problems:
            return False, "；".join(problems)
        return True, (f"accent={TIGER_DARK['accent']} background={TIGER_DARK['background']} "
                      f"stripe={TIGER_DARK['stripe']}（深色黑橙）")

    check("虎猫皮毛主题已应用（深色：黑 + 橙）", tiger_theme)

    def theme_switch() -> tuple[bool, str]:
        """切到浅色（白橙）再切回来 —— 两套主题都要真的生效。"""
        from tcide.theme import TIGER_LIGHT

        app.apply_theme("light")
        pump()
        light = getattr(runtime, "theme", None)
        if light is None or str(light.background).lower() != TIGER_LIGHT["background"]:
            return False, f"浅色没生效：background={getattr(light, 'background', None)!r}"
        if str(light.accent).lower() != TIGER_LIGHT["accent"]:
            return False, f"浅色下强调色不对：{light.accent!r}"
        if not app._stylesheet_applied:
            return False, "样式表没有应用"
        app.apply_theme("dark")
        pump()
        restored = str(getattr(runtime, "theme", None).background).lower()
        if restored != "#0d0b09":
            return False, f"切回深色失败：{restored!r}"
        return True, (f"浅色 background={TIGER_LIGHT['background']} → "
                      f"深色 background={restored}")

    check("主题切换：浅色（白橙）与切回深色", theme_switch)

    def sample_file() -> tuple[bool, str]:
        """「新建示例文件并打开」：真的建出来、真的打开、**用完删掉**。

        必须删掉：自检有一条「不得改动项目文件」的完整性检查（有事故前科），
        这里建的示例文件不能留在项目里。
        """
        before = set(app.state.workspace.iter_files())
        app.cmd_open_sample()
        pump()
        created = sorted(set(app.state.workspace.iter_files()) - before)
        if not created:
            return False, "没有创建示例文件"
        name = created[0]
        if not name.startswith("hello_tcide"):
            app.state.workspace.delete(name)
            return False, f"示例文件名不对：{name}"
        if app.state.active_path != name:
            app.state.workspace.delete(name)
            return False, f"示例文件没有自动打开（当前 {app.state.active_path}）"
        content = app.state.workspace.read(name)
        app.state.workspace.delete(name)          # 还原项目，保住完整性检查
        app.refresh_tree()
        pump()
        if "is_prime" not in content:
            return False, "示例内容不完整"
        return True, f"创建并打开 {name}（{len(content)} 字符，已清理）"

    check("新建示例文件并打开（含清理）", sample_file)

    # ---- 完整性：自检不得留下任何改动 -------------------------------------
    def integrity() -> tuple[bool, str]:
        after = snapshot(app.state.workspace)
        added = sorted(set(after) - set(before_snapshot))
        removed = sorted(set(before_snapshot) - set(after))
        changed = sorted(key for key in set(before_snapshot) & set(after)
                         if before_snapshot[key] != after[key])
        if added or removed or changed:
            return False, f"新增 {added} / 删除 {removed} / 修改 {changed}"

        # 只比文件内容**不够**：自检曾经真的执行了命令面板里的「提交」，
        # 每次跑都把项目仓库提交一次，而文件内容一个字节都没变（哈希检查照样通过）。
        # 所以还要确认 git HEAD 没动。
        head_note = ""
        try:
            from tcide.gitops import GitRepo

            repo = GitRepo(app.state.root)
            if repo.owns_repo():
                if repo.head_sha() != before_head:
                    return False, (f"自检改动了 git HEAD："
                                   f"{before_head[:8]} → {repo.head_sha()[:8]}")
                head_note = f"；git HEAD 未变（{before_head[:8]}）"
        except Exception as exc:      # noqa: BLE001 - 没有 git 就当跳过这项
            head_note = f"；git 检查跳过（{type(exc).__name__}）"
        return True, f"{len(after)} 个文件全部未被改动{head_note}"

    check("自检不得改动项目文件与 git 历史", integrity)

    # 注：``<Web>`` 编辑器（QtWebEngine + QWebChannel）**暂不放进默认窗口**。
    # 实测结论（tools/probe_webchannel.py）：channel **在页面加载之前**建立时
    # 整条链路可用（loadFinished=True、页面发回 ready qwebchannel、
    # window.editorBridge 存在）。但把它挂进主窗口后，WebEngine 的渲染进程会在
    # 建树早期抢占事件循环，把前面 8 项交互检查打挂（自检从 15/16 掉到 8/16）。
    # 所以它需要**独立窗口 / 真正的惰性创建**才适合接入，等下一轮做。

    return results


def run_selftest(runtime, app, qapp, *, verbose: bool = False) -> int:
    """执行自检；返回进程退出码（0 全通过）。"""
    results = _run_checks(runtime, app, qapp)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "✓" if ok else "✗"
        line = f"  [{mark}] {name}"
        if verbose or not ok:
            line += f"  — {detail}"
        print(line)
    print(f"\n自检：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1
