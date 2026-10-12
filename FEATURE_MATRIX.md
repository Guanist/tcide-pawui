# TCIDE → PawUI 能力矩阵与实施计划

用 **PawUI**（0.1.3.3 + 本地 PR#9/#10 修复）重写 TCIDE，逐条覆盖其 README 宣称的功能，
**包括原版丢失或静默失效的那些**。本文件是实施依据：每条都写明「怎么实现 / 现状 / 风险」。

配套约束（来自 TCIDE 自己的 `CLAUDE.md`）：先澄清再实现、极简实现、微创修改、增量闭环验证。

---

## 0. 关键前提（已实测）

| 事实 | 证据 |
|---|---|
| `<Web>` = `QWebEngineView`，QtWebEngine 可用 | `components.py:Web.build` + `import PySide6.QtWebEngineWidgets` ✓ |
| QtWebChannel / QtMultimedia / QtPdf / QtSvgWidgets 均可用 | 环境自检全部 ✓ |
| 因此 **Monaco 与 xterm.js 可以真嵌** | TCIDE 前端正是用这两个 |
| PawUI 缺 `Tree` / `Terminal` / `Switch` | 用 `List`+缩进、`Web`+xterm、`Checkbox` 替代 |
| `Text`/`Markdown`/`TextArea`/`List`/`Table`/`Select` 支持模板驱动的动态刷新 | `bind_state`/`watch_state` 调用点已核对 |
| `<Canvas>` 绘制 API 是**位置参数几何 + 关键字样式** | `widgets.py:972-1023`：`rect(x,y,w,h,radius=,fill=)` |
| `<script>` 必须是顶层元素（`<Window>` 的兄弟） | 写进 `<Window>` 会报 `unknown component <script>` |
| git 2.53 / node / npm / httpx / PySide6 6.11.2 可用 | 环境自检 ✓；`pyright` 未安装 |

---

## 1. 原版「宣称但未实现 / 静默失效」清单（考古结论）

来源：`src/main/ipc-handlers.ts:43` 与 `recovered-modules/p2-modules.js` 的**作者注释**。

**P0/P1/P2/P3 高级模块 —— preload/IPC 从未接线，README 宣称的功能静默失效：**

| 模块 | 声称能力 | 本仓库状态 |
|---|---|---|
| `context-trimmer` / `smart-trimmer` | 32K 智能压缩 | ✅ `trimmer.py` |
| `entropy-controller` | 智能上下文 | ✅ `pressure.py`（诚实改名：加权启发式） |
| `entropy-evaluator` | 代码熵评估 | ✅ 度量并入 `arch.py` |
| `git-intelligence` | Git 智能 | ✅ `gitintel.py` |
| `warehouse-analyzer` | 仓库分析 | ✅ `indexer.py` + `arch.py` |
| `unattended-runner` | 无人值守 | ✅ `runner.py` |
| `semantic-completion` | 语义补全 | ✅ `completion.py`（诚实定位：前缀补全） |
| `semantic-chunker` | 大文件语义切块 | ✅ `indexer.chunks()` |
| `autoheal-manager` | 编译错误自动修复 | ✅ `autoheal.py` |
| `arch-analyzer` | 架构分析 | ✅ `arch.py`（Tarjan 真算法） |
| `lint-manager` | 实时诊断 | ✅ `lint.py`（11 条规则，纯标准库） |
| `perf-optimizer` | 内存/性能监控 | ✅ `perf.py`（Python 侧 tracemalloc，如实标注） |
| `debug-manager` | 调试管理 | ✅ `debug.py`（配置 + 断点 + 命令；**不含** DAP 客户端，已标注） |

**P2 前端模块 —— 原始文件从仓库历史丢失：**

| 丢失内容 | 本仓库状态 |
|---|---|
| `acceptDiff` / `closeDiffModal`（**Diff 审核弹窗**） | 📋 计划（`<Dialog>` + `<Web>` Monaco diff） |
| `__tcide_listSnippets` → 兜底返回 `[]`，**50+ snippets 实际为空** | ✅ `snippets.py`（57 条，含 17 条 PawUI 专属） |
| `__tcide_originalSendToAI` 桥接 | 不适用（本实现无此补丁） |

**原版工程缺口**：0 测试、无 CI、无 LICENSE 文件（字段 MIT / 徽章 GPLv3）、`dist/` 提交占仓库 60%、
165 份重复图片、`main.ts` 7,048 行单体、`@ts-nocheck` 11 处、API key 明文存储。

---

## 2. 功能矩阵

图例：✅ 已实现并测试 · 🚧 部分 · 📋 计划 · ⚠️ 需替代方案

### 2.1 AI 双 Agent 引擎

| 功能 | 实现 | 状态 |
|---|---|---|
| Builder：需求 → 任务清单 | `agent.py::BuilderAgent`（解析失败**可见地**回退成单任务） | ✅ |
| Coder：工具循环 + 验证 | `agent.py::CoderAgent`（8 轮上限 + **打转保护**） | ✅ |
| 审查门（原版 `REVIEWER_GATE_ENABLED`） | `agent.py::ReviewerAgent`（确定性检查优先，模型评审可选） | ✅ |
| 并行 Coder + 重试 | `AgentOrchestrator`（**Barrier 验证真并发**） | ✅ |
| 自动读取项目上下文 | `indexer.py` + `memory.py` | ✅ |
| 编译失败自动修复 | 验证环 + `autoheal.py` + `tools.run_command` | ✅ |
| `/task` 一键全自动 | `run_agent()` + `runner.py` | ✅ |
| 项目规则 `CLAUDE.md` 注入 | `memory.py` → pinned 上下文 | ✅ |
| 无人值守（超时/检查点/日志） | `runner.py`（全局+单步超时、重试、检查点、恢复、日志） | ✅ |
| 自动提交 | `cmd_git_commit` 用 `gitintel` 的建议信息 | ✅ |

### 2.2 编辑器与工程能力

| 功能 | 实现 | 状态 |
|---|---|---|
| 编辑器（原生） | `app.paw` 的 `<TextArea>` + 脏标记 + 保存 | ✅ |
| Monaco + minimap + 23 语言高亮 | `<Web>` + QWebChannel | 📋 |
| 多标签 / 分屏 | `Tabs` 是构建期静态；分屏用 `<SplitPane>` | 🚧 |
| Snippets（50+） | `snippets.py`（57 条） | ✅ |
| 补全 | `completion.py` | ✅ |
| 问题面板 | `tools.get_diagnostics` + `<List>`（点击跳转） | ✅ |
| 架构分析（依赖图 + 异味） | `arch.py` | ✅ |
| 命令面板 | `<Dialog>` + 过滤 + 前缀分派（11 条命令） | ✅ |
| 代码大纲 / 项目搜索 / 补全 | `panels.py`（行数据 + 反解成对测试）→ 界面「工程」页 | ✅ |
| 跳转到指定行 | `panels.line_offset()` + `Runtime.query("#editor")` 移动光标（拿不到控件会**明说**） | ✅ |
| Emmet | 需自研简化版 | ⚠️ |
| LSP（TS/Python/自动识别） | 需装 `pyright` / `typescript-language-server` | 📋 |
| Diff 审核弹窗（原版丢失） | `<Dialog>` + `<CodeBlock>` 显示 unified diff，接受/驳回/下一个/只接受第一块 | ✅ |
| Zen 模式 / 代码折叠 | 需状态 + 样式 | 📋 |

### 2.3 终端 / 预览 / Git / AI 对话 / 宠物

| 功能 | 实现 | 状态 |
|---|---|---|
| 终端（原生命令回显） | `<Input on_enter>` + `<Markdown>` + `tools.run_command` | ✅ |
| 终端（xterm.js 多标签） | `<Web>` + xterm + QWebChannel | 📋 |
| 预览：图片 / 文本 / 代码 | `<Image>` / `<CodeBlock>` | 📋（未接） |
| 预览：PDF / 视频 / Hex | QtPdf / QtMultimedia / `hexview.py` | 📋 |
| Git 状态面板 + 一键 stage/commit | `gitops.py` + `<List>` + 按钮 | ✅ |
| Git 智能（聚类/风险/提交建议） | `gitintel.py` | ✅ |
| 分支切换 | `gitops.branches()/checkout()` | ✅（逻辑已测，未接界面） |
| AI 对话（多轮 + 离线演练） | `<Markdown>` + `on_chat_send` + `OfflineModel` | ✅ |
| 多模型（5 家）+ 连接测试 | `models.py`（6 个预设 + 3 个适配器） | ✅ |
| MCP 工具（9 个点名工具） | `tools.py`（12 个 + 危险命令拦截） | ✅ |
| 上下文裁剪 / 压力评估 / token 估算 | `trimmer.py` / `pressure.py` / `tokens.py` | ✅ |
| 像素宠物（9 状态） | `pet.py`，已接进状态栏 `<Canvas>` | ✅ |

---

## 3. 分层与目录（当前状态）

```
tcide-pawui/
  README.md / README_EN.md  ✅ 中英双语（含功能对照表、测试策略、bug 清单、许可证方案）
  FEATURE_MATRIX.md      ✅ 本文件（实施依据 + bug 清单）
  tcide-pawui.spec       ✅ PyInstaller 打包配置（**未实跑**：环境无 PyInstaller，已标注）
  .github/workflows/ci.yml ✅ CI（后端零依赖 → 装 PySide6+PawUI → 全量离屏 → 质量任务 → 许可证边界）
  app.paw                ✅ IDE 外壳（工具栏/文件树/Git/编辑器/底部面板/状态栏+宠物/命令面板/快捷键）
  main.py                ✅ 入口 + 后端接线（AppState + 26 个处理器 + 离线演练 + --selftest/--shot）
  tcide/                 ✅ 后端核心（**不 import PawUI**，可离线严格测试）
    tokens.py            ✅ token 估算（CJK/ASCII 双口径）
    trimmer.py           ✅ 分层上下文裁剪（限附件→压代码块→丢历史→硬截断）
    pressure.py          ✅ 上下文压力（诚实命名：加权启发式）
    workspace.py         ✅ 文件树/读写/搜索（**路径逃逸防护**）
    indexer.py           ✅ 符号表（Python 用 ast；其他正则且标 precise=False）+ 调用图 + 依赖图 + 切块
    memory.py            ✅ 项目元数据 → 静态上下文 + 规则注入
    gitops.py            ✅ Git 封装（porcelain -z 解析、非交互环境变量）
    gitintel.py          ✅ Git 智能（聚类/风险/提交建议/热点）
    models.py            ✅ OpenAI 兼容 / Anthropic / Ollama（标准库 urllib，零第三方依赖）
    tools.py             ✅ 12 个 Agent 工具 + 危险命令黑名单 + 调用留痕
    agent.py             ✅ Builder/Coder/Reviewer + 并行编排/重试/审查门
    runner.py            ✅ 无人值守（超时/重试/检查点/恢复/日志）
    snippets.py          ✅ 57 条片段（原版是空桩）
    completion.py        ✅ 前缀补全（符号 > 关键字 > 片段）
    autoheal.py          ✅ 错误解析 + 可解释建议
    arch.py              ✅ 架构分析（Tarjan 循环依赖/分层/扇入扇出/异味）
    pet.py               ✅ 像素宠物（9 状态机 + 帧动画 + 绘制指令）
    lint.py              ✅ 静态检查（11 条规则：裸 except/不可变默认参数/未用导入/冲突标记…）
    perf.py              ✅ 性能采样（Python 侧 tracemalloc + 计时 + GC 建议）
    debug.py             ✅ 启动配置 + 断点 + 命令构造（**不含** DAP 客户端，已标注）
    hexview.py           ✅ Hex 视图（分页/搜索/魔数识别/香农熵）
    diff.py              ✅ Diff 引擎 + 改动审核（生成/解析/按 hunk 选择应用/审核会话）
                           + `ReviewingWorkspace` 代理：**Agent 的写操作进审核队列而不落盘**，
                             因为工具层只依赖鸭子类型，所以**没有改动 tools.py 一行**
                           —— 补回原版**丢失**的 `acceptDiff` / `closeDiffModal`
    cli.py               ✅ 库侧 CLI（check/lint/analyze/index/search/outline/snippet/
                            complete/hex/git/heal/memory/debug/perf，含 --json）
    shell_selftest.py    ✅ 15 项界面自检（与 --selftest 共用同一套检查）
    lsp.py               📋 LSP 客户端（需外部语言服务器，后置）
    web/                 ✅ Monaco 兼容消息协议 + QWebChannel 桥（Qt 惰性导入）
                           + `editor.html` 离线单文件编辑器（行号/着色/diff gutter/快捷键，
                             **不引用任何外部网络资源**；`html_is_offline()` 有测试守着）
                           + `terminal.html` 离线终端前端（复用同一个桥工厂）
                           + `vendor/monaco/`（**24.4 MB / 151 文件**，vendored Monaco 0.57）
                             · 从 `file://` 走 **AMD**（实测 amd-ok、**91 种语言**；
                               ESM 布局会被 CORS 拦住 → 必须 AMD）
                             · 拿不到时自动退回内置编辑器
                             · `TCIDE_NO_MONACO=1` 可省掉它（页面自动回退）
                           ✅ 已按实测结论接入：**独立顶层窗口 + 懒创建**
                              （命令面板「打开 Web 编辑器」或 `cmd_open_web_editor`）
                              · 不在主窗口里 → 界面自检保持 **15/15**（常驻主窗口时是 8/16）
                              · **先 setWebChannel、后 setUrl**（顺序是硬要求）
                              · 页面 ready 后推文本与 gutter 标记；页面改动回流进状态且不回环
                              · 12 条专项测试（tests/test_web_editor_window.py，离屏真跑 WebEngine）
  tests/                 ✅ 809 条，全部可离线（界面部分离屏）
                        含 test_layering.py：**守住 LGPL 边界**（断言 tcide/ 不 import pawui）
  tools/                 # 截图、环境自检、编码修复
```

`tcide/` 24 模块 7,436 行 / `tests/` 19 文件 4,689 行。CLI 实跑示例：

```
$ python -m tcide.cli snippet pawwin --set title=我的窗口 --set w=800 --set h=600
<Window title="我的窗口" theme="dark" width="800" height="600">
$ python -m tcide.cli analyze          # 对本项目自己跑
52 个文件 / 82 条依赖边；循环依赖 0 组；拓扑 5 层；无主文件 10 个；异味 19 条
```

**分层原则**：`tcide/*` **不 import PawUI**（实测 0 处）；界面只在 `app.paw` + `main.py` +
`tcide/shell_selftest.py`。因此绝大多数功能都能用 pytest 严格验证，UI 层只做
offscreen 建树 + 交互断言 + 截图。这条原则同时决定了许可证怎么切（见 §6）。

---

## 4. 测试策略与 bug 清单

| 层 | 手段 | 需要 Qt |
|---|---|---|
| 纯逻辑（token/裁剪/压力/索引/git/模型协议/工具/编排/宠物/片段/补全/错误解析/架构） | pytest 直接断言 | ❌ |
| 模型协议 | 起**本地假 HTTP 服务**断言请求体与解析 | ❌ |
| Agent 循环 | **假模型**断言并行/重试/审查门/统计 | ❌ |
| Git | 临时仓库**真跑** `git` | ❌ |
| UI 外壳 | `QT_QPA_PLATFORM=offscreen` 建树 + 控件/几何断言 | ✅ |
| 视觉 | `root.grab()` 截图 | ✅ |

### 已发现并修复的真 bug（19 条）

**应用自身**

1. `shorten_code_blocks` 把围栏语言标记当成第一行代码。
2. `keep_recent=0` 时 `drop-old-dialogs` **把整段对话裁空** → 模型收到空上下文。
3. **token 口径不一致**：预算判断只算 `content`、漏算附件 → 巨型附件永远触发不了裁剪。
4. 层次语义缺陷：单条消息既"最新"又受保护 → 内容/附件都无法压缩。
5. **`call_graph` 裸符号名做键跨文件覆盖**（`start`/`main` 这类常见名必然重名）。
6. **`ProjectMemory.name` 被目录名占位** → 清单里的项目名永远读不进来。
7. **`workspace.search` 不返回文件名命中**（按名字找文件是 IDE 基本需求）。
8. **`git commit --amend` 在空提交上静默失败**（git 要求 `--allow-empty`）。
9. 测试清理用 `shutil.rmtree(..., ignore_errors=True)` 吞掉失败 → 累积 **129 项残留**。
10. **未知服务商名被静默当成 OpenAI 兼容端点**（`provider="deepsek"` 不报错）。
11. **审查门把「未发现问题」判成有问题**：`"问题" in output` 命中成功文案 →
    每一次改动都被驳回并重试。已改为结构化 `ToolResult`。
12. **`RunReport.ok` 把「审查驳回次数」当失败条件** → 重试成功后仍报"有失败"。
17. **`.paw` 被识别成 `html` 语言** → 关键字补全与片段表全部找错。
18. `dataclass` 同时登记在关键字表与片段表（同名冲突按分数取高者，实现对但表不该重复）。
20. **`Linter` 在不带 workspace 时完全不跑 Python 规则**：语言退化成 `plaintext`，
    于是 11 条规则里最有价值的 5 条（裸 except/可变默认参数/未用导入…）**一条都不生效**。
    已改为按路径后缀调用静态的 `Workspace.language_of`。
21. **`DebugConfigStore.defaults_for` 把 pyproject 的 console-script 规格当入口**：
    `app:main` 不是可运行的文件路径，拿它当 `program` 会让调试命令直接跑不起来。
    已改为优先选带源码后缀的文件入口。
24. **`Workspace` 只有 `read_bytes` 没有 `write_bytes`**：二进制改动（Hex 视图、资源文件）
    审核通过后**无法落盘**。已补上（同样的临时文件 + 原子替换）。
25. **`parse_unified` 把多个文件的 diff 合并成一个 patch**：看到 `---` 头时只在
    `current.hunks` 非空才收尾，可此时 hunk 还**没装进** `current`，于是两个文件的
    hunk 被并进同一个 `FilePatch`（多文件 diff 只解析出一个文件）。
    已改为 `---` 一律无条件收尾（hunk 正文里不可能出现 `---`）。
26. **`"/dev/null".lstrip("b/")` 得到 `"dev/null"`**：`str.lstrip` 是按**字符集**剥离，
    不是去前缀。同一写法会让 `"a/app.py"` 变成 `"pp.py"` —— **静默的路径损坏**，
    而且只对以 `a`/`b`/`/` 开头的路径发作，极难察觉。已改为显式前缀判断。
27. **`new file mode` / `deleted file mode` 的标志被丢弃**：这两行出现在 `---` 头
    **之前**，那时 `current` 还是 `None`，标志无处可存。已改为先记"待定标志"，
    建 `FilePatch` 时再带上（并额外用 `/dev/null` 推断新增/删除）。
28. **`problem_rows` 按 list 写，而 `lint_all()` 返回 dict** → `KeyError: slice(...)`。
    `Linter.summary`/`report` 都处理了双形态，就这里漏了。已统一，并补上这条易错点的测试。
29. **补全提示用整个缓冲区文本**：等于"永远看文件末尾"，而末尾的词通常没有可补全
    前缀 → 提示莫名为空。已改为优先用**真实光标位置之前的文本**。
30. **我自己又制造了 BOM**：用 PowerShell `Set-Content -Encoding utf8` 建
    `tcide/web/__init__.py`，写出 UTF-8 BOM → `ast.parse` 报
    `invalid non-printable character U+FEFF`，**两个许可证边界测试一起失败**，
    而错误信息完全指不到真正原因。这正是本文件第 19 条教训（不要用 PowerShell
    文本 cmdlet 改 UTF-8 文件）——**我四次后又犯**。已改用专用写文件工具，
    并新增守卫测试：扫描 `tcide/` 下所有 `.py` 的 BOM，让同类问题不可能再悄悄复发。
31. **`Timer` 会报出"负内存"**：`tracemalloc` 的峰值是**进程级**的，前面的测试
    把峰值顶高之后，`peak - before` 变成负数。已改为进入时 `reset_peak()` 并把
    结果夹到非负；测试也补了"连续两段都不得为负"。
32. **`editor.html` 只写了 JS、没有 HTML 骨架**：所有 `getElementById` 都会拿到
    `null`，页面一打开就报错。已重写为完整文档，并加测试断言 JS 引用的 DOM
    元素（`#code`/`#gutter`/`#overlay`/`#path`/`#status`/`#completion`）真实存在。
33. **diff 行标记把删除行归到"上一行"**：于是「改一行」被画成 `added` + `removed`
    两个标记，而不是一个 `changed`。已改为暂存删除行、遇到新增或上下文再定归属。
34. **`<Web>` 常驻主窗口会打坏其它功能**：WebEngine 的渲染进程在建树早期抢占事件循环，
    界面自检从 15/16 掉到 **8/16**。已改为**独立顶层窗口 + 懒创建**（只有用户显式打开
    才创建），自检回到 15/15。这条是"测出来的决定"，不是猜的。
35. **pytest 作用域冲突导致全部用例 setup ERROR**：新建的 module 级 fixture 依赖了
    conftest 里 function 级的 `qapp`/`project` → `ScopeMismatch`，表现为**所有用例
    在 setup 阶段报错**（看不到任何断言失败，很容易误判成实现坏了）。
    已改为 fixture 自给自足（自己建 QApplication 与临时项目）。
36. **`_push_web_state` 引用了不存在的 `AppState.problems`** → `AttributeError`。
    已改为**就地用 `Linter.lint_file()` 取当前文件的诊断**：既不依赖字段名，
    显示的也确实是这个文件的问题。
37. **我又把异步调用当成同步的写测试**（第 16 轮诊断返回 None 也是同一原因）：
    连发 40 次 `runJavaScript` 却不给渲染进程时间，回调根本没机会回来。
    已改为"发一次 → 带 `msleep` 的重试等待"。教训：**跨进程的 QtWebEngine 调用
    必须真等，`processEvents()` 不等于等待**。
43. **【PawUI】`<Image>` 不响应状态变化**：读源码确认 `Image.build()` 只在建树时
    `opt_str("src")` 读一次，**从不调用 `bind_state`/`watch_state`** —— 而
    Text/Markdown/List/Table/Select/TextArea **都支持**模板刷新。实测症状：
    `<Image src="{$preview_image}">` 的状态推对了、控件里却始终没有图像
    （界面自检抓到："判定为图片，但控件里没有图像"）。应用侧改用**命令式
    `setPixmap`** 绕开；顺带也绕开了 `<Image>` 在 src 非空但加载失败时抛
    `RenderError` 会把整个界面打崩的风险。
44. **我自己的自检项把 bug 伪装成了通过**：第一版对"真 PNG 被判成非图片"也返回
    True（写成"已安全退回"），于是掩盖了 #43。已改为**真 PNG 判错就失败**，
    并区分两种情况：「有 PNG 魔数但解码不了（桩文件）→ 应用清空且不崩 = 正确」
    与「能解码但控件里没有 → 必须失败」。
45. **预览路径不能在 `open_file` 里记**：二进制/图片载不进文本编辑器，于是
    `open_file` 根本不会被调用 → 选图片时预览停在**上一个文本文件**的状态。
    已改为在 `on_tree_select` 里独立记录 `_preview_path`（这才是用户真实路径）。
46. **`_preview_image_widget` 直接访问 `self.runtime.root` 会崩**：接线测试会注入
    只带 `state` 的假 runtime（没有 `root`）→ `AttributeError`。已改为防御性取值。
47. **我的复现脚本用了自制 FakeRuntime，结果两条检查直接"跳过"**：`resolve_raw` /
    `resolve_color` 内部还要 `runtime.namespace`，而假对象没有 → 打印"跳过"，
    **等于什么都没验证**（比失败更糟：看起来"跑过了"）。已改用**真 Runtime**。
    教训：验证脚本里的替身必须覆盖被调方真正用到的接口，否则"跳过"会被误读成"通过"。
48. **我把一个自己引入的助手函数当成上游函数写进了复现**：`resolve_color` 不在原版
    `resolve.py` 里（是本次重构引入的），所以那条检查在**原版上必然跳过**。
    已在该条报告里**明确写出"脚本在原版上跳过是预期的、本仓库没有声称已在原版复现"**，
    并指出原版真正的解析路径是 `runtime.py:646 warn_unknown_color` +
    `theme.py:57 Theme.color`。教训：**先确认函数属于哪个版本，再声称"上游有问题"**。

> 三条 PawUI 问题的完整报告（含原版/补丁版实测对照、根因代码、建议修法）见
> 独立文档 **[PAWUI_FINDINGS.md](PAWUI_FINDINGS.md)**，可直接提交上游。
38. **`<CodeBlock text="...">`：CodeBlock 根本没有内容属性** → Diff 审核弹窗里的
    diff **从来没显示出来过**。而窗口照样建树成功、外壳自检照样 15/15 ——
    因为属性名写错只会让控件**永远空着**，不报错。这是本项目最典型的一类"静默失效"，
    最终是 **`pawui check`（本仓库自己给 PawUI 提的那个静态校验）**抓出来的：
    `<CodeBlock>` 只支持 `language`/`height`/`numbers`，内容必须走子元素。
    已改用只读 `<TextArea value="{$diff_text}" readonly="true">`，并把它变成门禁测试
    （`tests/test_paw_validation.py`，含"反向验证校验器确实会报错"）。
39. **test 用 `tmp_path` 撞上被污染的系统临时目录**：升级运行留下的
    `pytest-of-*` 目录让受限运行直接 `PermissionError: 拒绝访问`，
    表现为 **fixture 在 setup 阶段报错**（一条断言失败都看不到，极易误判成实现坏了）。
    `conftest.py` 本来就刻意避开 `tmp_path`，是我新写的测试又用回去了。
    已根治：在 `pytest_configure` 里把 `PYTEST_DEBUG_TEMPROOT` 与 `tempfile.tempdir`
    都重定向到工作区内（`tests/.scratch/tmp`）。
40. **测试猜了不存在的 PawUI 内部 API**（`from pawui.widgets import WIDGETS`）。
    那条测试本来是在重复 `pawui check` 已经做的事（校验组件名），已删除 ——
    **不要重新实现校验器已有的能力**。
41. **spec 的 `EXE(version=...)` 传错了类型**：PyInstaller 要的是**版本信息文件的
    路径**，我传了版本文本 `f"虎猫 TCIDE {__version__}"`，于是它真去打开这个文件名并
    `FileNotFoundError`。**因为 spec 从未实跑，这个错一直没暴露** —— 本轮装上 PyInstaller
    真跑一次就立刻炸了。已修（并注明要 Windows 文件属性版本号需另写 version 资源文件）。
42. **打包成功 ≠ 打包产物能跑**（**未解决，如实记录**）：修完 #41 后构建成功
    （`tcide.exe` 2.7 MB，总产出 574 MB，`app.paw` 已打入 `_internal/`），
    但运行 `tcide.exe --shot dist\frozen_shot.png` **没有生成截图** —— 冻结环境里窗口
    没建起来。而 spec 里 ``console=False`` 让 stdout 不可见（与早先 `os._exit` 吞输出
    同类问题），拿不到报错。
    **下一步**：用 `console=True` 构建一次拿到真实报错，再决定是缺 Qt 平台插件、
    缺 QML 插件（构建时已经警告某个 QML 插件 dll 不存在），还是定位 `app.paw` 的问题。

    **【已定位到环境，非本项目缺陷】后续实测（第 19 轮）**：
    · `TCIDE_CONSOLE=1` 构建后运行 → `exit=-1073741819`（`0xC0000005` 访问冲突），
      **在任何输出之前就崩**
    · `qoffscreen.dll` / `qwindows.dll` 等平台插件**确实打进去了**（与源环境一致），
      去掉 `QT_QPA_PLATFORM` 用默认平台**同样崩** → 不是平台插件问题
    · `TCIDE_NO_WEBENGINE=1` 的轻量构建（137 MB）**同样崩** → **QtWebEngine 被排除**
    · **决定性实验**：把 `tools/min_qt.py`（5 行 PySide6：创建 QApplication + 一个
      QLabel + grab）单独打包 → 同一环境**同样 `0xC0000005`**，且崩在
      **`QApplication([])` 那一行**（前 3 步的 print 都出来了，第 4 步没有）。
      未冻结运行同一个脚本 → exit 0 并成功存图。
    · **结论**：**冻结后的 PySide6 在本机这个环境里根本起不来**，与本项目、spec、
      QtWebEngine 无关（本机 PySide6 安装本身可能不完整 —— 构建时警告过某个 QML
      插件 dll 不存在）。因此**打包产物的可运行性无法在本环境验证**；
      spec 本身已能成功构建并正确打包 `app.paw`。真机（完整 PySide6）预期可用。

**PawUI（反馈上游）**

13. **主题令牌名经 `{$state}` 传递时不解析**：`bg="{$x}"`（`x="border"`）→ 报警（提示里还把
    `border` 列为可用主题色，自相矛盾）+ QSS 拿到 `background:border` 被 Qt 静默丢弃。
    已修 `resolve_color`（模板插值**之后**再查令牌）+ 回归测试。
14. **state 里的文本只要含 `{$...}` 就被当模板吃掉**：`resolve_raw` 为「链式引用」递归解析，
    于是 `<TextArea value="{$editor_text}">` 显示源码时**实测 27623 字符变成 27101**。
    对一个用 `{$x}` 当语法、又要拿来写代码编辑器的框架，等于"不能显示自己的源码"。
    已修（窄修法）：`resolve_raw(..., chain=False)`，只让 `bind_state` 取原值，
    **不动** `For`/`Select` 的链式语义（PawUI 全量 400 条测试通过）。

**验证基建自身（三次事故，都值得记）**

15. **自检把项目文件写掉了**：`edit_and_save` 的 "spy" 把写入转发给真实 `workspace.write`，
    `diagnostics` 往当前文件注入语法错误再用 `value = 1` 覆盖 —— 自检打开的第一个 `.py`
    恰好是 `main.py`，**它被改成了 1 行 `value = 1`**。已修：写入只记录不落盘、静态检查
    改用临时文件，并新增「自检不得改动项目文件」的 sha1 完整性校验。
16. **`os._exit(0)` 吞掉缓冲输出** → 探针零输出，把排查引向完全错误的方向。
19. **PowerShell `Get-Content`/`Set-Content` 往返破坏 UTF-8**：把本文件写成乱码；
    我再用 `errors="replace"` 的"还原"脚本造成 **1,123 处不可逆损坏**，最终只能重写本文件。
    教训：**不要用 PowerShell 文本 cmdlet 改 UTF-8 文档**，用专门的写文件工具。

---

## 5. 与原版的取舍（诚实标注）

| 项 | 决定 | 理由 |
|---|---|---|
| `entropy` 命名 | **改名** `pressure` | 原版是加权启发式，不是信息熵 |
| `vector-indexer` | **不假装向量**，如实叫检索 | 原版无 embedding |
| `dream-engine` | 不实现 | 原版是日志 I/O + 字符串，价值低 |
| `semantic-completion` | 如实叫**前缀补全** | 不冒称"语义" |
| `autoheal` | **确定性解析 + 可解释建议** | 不冒称"AI 自动修好"，每条带依据与置信度 |
| LSP 富交互 | 后置 | 需额外安装语言服务器 |
| 视频/PDF 预览 | QtMultimedia / QtPdf 替代 Electron | 需实测 |
| 安装包 | 后置（QtWebEngine 体积大） | — |
| Electron 外壳 | 替换为 PawUI 原生窗口 | 本项目的目的 |

---

## 6. 许可证（**已完成**，用户决定：选 LGPL）

**结果**：拆开授权，两份全文都是从**本机权威副本**复制而来（不凭记忆写全文）：

| 文件 | 许可证 | 来源（逐字节校验一致） |
|---|---|---|
| `tcide/LICENSE` | **LGPL-3.0-or-later** | 本机 `pystray-0.19.5.dist-info/COPYING.LGPL`（7,656 字节） |
| `LICENSE` | **GPL-3.0-or-later** | PawUI 仓库的 `LICENSE`（35,149 字节） |

**为什么必须拆开**：PawUI 的 `pyproject.toml` 声明 `GPL-3.0-or-later`，GPL 要求链接它的
**组合作品整体**按 GPL 发布，所以"整个 tcide-pawui 用 LGPL"在法务上不成立。
而 `tcide/`（28 个文件）是**不依赖 PawUI** 的可复用库 —— 只用到标准库，
**零模块级界面依赖**（`tcide/web/bridge.py` 里的 PySide6 是函数内惰性导入，
而 PySide6 本身也是 LGPL）。对它用 LGPL 正当：这正是 LGPL 的设计用途（库）。

**落地内容**：

1. `tools/apply_licenses.py` —— 幂等写入 SPDX 头 + 校验原文一致性 + 校验无 BOM /
   无编码损坏（`--write` 才落盘，默认只检查）；
2. 64 个源文件全部带上 SPDX 头（库侧 LGPL / 应用侧 GPL）；
3. **把 `tcide/shell_selftest.py` 移到应用侧 `app_selftest.py`**（`tools/move_selftest.py`）
   —— 这样 `tcide/` 的"零界面依赖"**没有例外**，边界可以硬断言；
4. 边界与许可证门禁（`tests/test_layering.py`，破坏即 CI 失败）：
   - 库侧不得 import `pawui`（GPL）
   - 库侧**模块级**不得 import Qt（惰性导入允许且已注明理由）
   - 库侧每个 `.py` 必须有 LGPL 头；应用侧每个 `.py` 必须有 GPL 头
   - 两份许可证文件必须在场且是全文
   - 库侧不得误用 GPL 头（`GPL-3.0-or-later` 是 `LGPL-3.0-or-later` 的**子串**，
     必须锚定判断 —— 我用子串判断时 27 个文件全被误报过）

**另注**：TCIDE 原仓库的许可证问题（无 `LICENSE` 文件、`package.json` 写 MIT、
README 徽章写 GPLv3，三者矛盾）属于他们仓库，我们无权变更，只能在报告里指出。
