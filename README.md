# 虎猫 TCIDE · PawUI 版

> 用 **PawUI**（声明式 Python UI 层）重写的 [TCIDE](https://github.com/Guanist/TCIDE) ——
> 个人专属 AI 编程 IDE：**Builder + Coder 双 Agent 协作**，内置上下文压缩、Git 智能、
> 架构分析、像素宠物。

[English](README_EN.md)

---

## 这是什么

原版 TCIDE 是 30,211 行 TypeScript + Electron 的桌面 IDE。本项目用 PawUI 把它的能力
**逐条重新实现**，并且**把它 README 里宣称、实际却静默失效的功能真正做出来**。

原版作者在自己代码里写下了这份清单（`src/main/ipc-handlers.ts:43`）：

> 这些模块历史上只在 renderer 侧被调用，但 preload/IPC 从未接线，
> 导致「代码熵评估/智能上下文/Git智能/仓库分析/无人值守/语义补全」等
> README 宣称功能**静默失效**。

还有 `recovered-modules/p2-modules.js` 的头部：

> 原始 p2-modules.js 在仓库历史中丢失，其曾定义的全局（`acceptDiff` /
> `closeDiffModal` 等）……`__tcide_listSnippets` 兜底返回 `[]`

也就是说：**README 宣称的 50+ 代码片段，在原版里是空的**。本仓库里它是 57 条。

---

## 快速开始

```bash
# 1) 装依赖（PawUI 与 PySide6 均为 LGPL-3.0）
pip install PySide6 "pawui>=0.1.1"

# 2) 跑 IDE（用当前目录作为项目）
python main.py

# 3) 指定项目目录
python main.py D:\my-project

# 4) 离屏自检（15 项交互检查，不需要显示器）
python main.py --selftest

# 5) 截图（无头环境下验证布局）
python main.py --shot shots/ide.png
```

没有配置 API Key 时，AI 面板走**离线演练**：会真实跑通 Builder/Coder 的编排流程，
但**不联网、不修改任何文件**，并在对话里明确标注。

---

## 命令行

后端是一个**不依赖 PawUI、不需要 Qt** 的库，也能单独当 CLI 用：

```bash
python -m tcide.cli check               # 静态检查（11 条规则），有 error 退出码 1
python -m tcide.cli analyze             # 架构分析（循环依赖/分层/扇入扇出/异味）
python -m tcide.cli index               # 索引概览（语言分布、解析失败）
python -m tcide.cli search "def main"   # 跨文件搜索
python -m tcide.cli outline tcide/cli.py
python -m tcide.cli snippet pawwin --set title=我的窗口 --set w=800 --set h=600
python -m tcide.cli hex assets/logo.png --info
python -m tcide.cli git suggest         # 用 Git 智能给出 Conventional Commits 建议
python -m tcide.cli heal err.log        # 解析报错并给出可解释的修复建议
```

所有分析类命令都支持 `--json`，方便脚本消费。

---

## 功能对照

图例：✅ 已实现并有测试 · 🚧 部分 · 📋 计划

| 能力 | 原版 | 本实现 |
|---|---|---|
| Builder 需求拆解 → Coder 执行 | ✅ | ✅ `agent.py`（解析失败**可见地**回退） |
| 审查门（`REVIEWER_GATE_ENABLED`） | ✅ | ✅ **确定性检查优先**（静态检查+测试命令），模型评审可选 |
| 并行 Coder + 重试 | ✅ | ✅ 测试用 `Barrier` **验证真并发** |
| 多模型（DeepSeek/火山方舟/Ollama/Anthropic/自定义） | ✅ | ✅ `models.py`，**零第三方 HTTP 依赖** |
| MCP 工具（read/write/search/run/git…） | ✅ | ✅ 12 个工具 + 危险命令拦截 |
| 32K 上下文压缩 | ⚠️ 从未接线 | ✅ `trimmer.py`（分层裁剪 + 可断言报告） |
| 代码熵评估 | ⚠️ 从未接线 | ✅ `pressure.py`（**诚实改名**：加权启发式） |
| Git 智能 | ⚠️ 从未接线 | ✅ `gitintel.py`（聚类/风险/提交建议/热点） |
| 无人值守（超时/检查点） | ⚠️ 从未接线 | ✅ `runner.py` |
| 语义补全 | ⚠️ 从未接线 | ✅ `completion.py`（**诚实定位**：前缀补全） |
| 自动修复 | ⚠️ 从未接线 | ✅ `autoheal.py`（**可解释建议**，不冒称 AI 修好） |
| 架构分析 | ⚠️ 从未接线 | ✅ `arch.py`（**Tarjan 真算法**） |
| 实时诊断 | ⚠️ 从未接线 | ✅ `lint.py`（11 条规则，纯标准库） |
| 性能监控 | ⚠️ 从未接线 | ✅ `perf.py`（Python 侧 tracemalloc，如实标注） |
| 调试管理 | ⚠️ 从未接线 | ✅ `debug.py`（配置+断点+命令；**不含** DAP 客户端） |
| 50+ 代码片段 | ❌ 兜底返回 `[]` | ✅ `snippets.py`（57 条，含 17 条 PawUI 专属） |
| 像素宠物 9 状态 | ✅ | ✅ `pet.py`（程序化绘制，不依赖素材；已接进状态栏） |
| Monaco / xterm / LSP | ✅ | 📋 见「下一步」 |
| Diff 审核弹窗（`acceptDiff`） | ❌ 丢失 | 📋 计划 |
| 测试 / CI / LICENSE | ❌ 全无 | ✅ 669 条测试 + CI；许可证见下 |

完整的实施依据与 bug 清单见 **[FEATURE_MATRIX.md](FEATURE_MATRIX.md)**。

---

## 架构

```
app.paw          PawUI 声明式界面（工具栏/文件树/Git/编辑器/底部面板/状态栏+宠物/命令面板）
main.py          入口 + 后端接线（AppState + 26 个处理器 + 离线演练 + --selftest/--shot）
tcide/           后端核心（24 个模块，**不 import PawUI**，可离线严格测试）
tests/           669 条测试（19 个文件）
tools/           截图、环境自检、编码修复脚本
```

**分层原则**：`tcide/*` 不依赖 PawUI、也不接管界面。所以绝大多数功能都能用 pytest
严格验证，界面只做离屏建树 + 交互断言 + 截图。这条原则同时决定了许可证怎么切。

---

## 测试

```bash
# 后端核心（零第三方依赖，秒级）
python -m pytest -q tests/ --ignore=tests/test_shell.py

# 全量（界面部分需要 Qt，必须离屏）
QT_QPA_PLATFORM=offscreen python -m pytest -q

# 界面交互自检（15 项）
python main.py --selftest
```

测试策略：

| 层 | 手段 | 需要 Qt |
|---|---|---|
| 纯逻辑（裁剪/索引/Git/编排/宠物/片段/补全/错误解析/架构/CLI） | pytest 直接断言 | ❌ |
| 模型协议 | **本地假 HTTP 服务**断言请求体与解析 | ❌ |
| Agent 循环 | **假模型**断言并行/重试/审查门 | ❌ |
| Git | 临时仓库**真跑** `git` | ❌ |
| UI 外壳 | offscreen 建树 + 控件/几何断言 + 截图 | ✅ |

---

## 开发过程中发现并修复的 bug

共 22 条，完整清单在 [FEATURE_MATRIX.md](FEATURE_MATRIX.md) §4。其中值得单独一提的：

- **`<TextArea>` 无法显示含 `{$...}` 的文本**（PawUI 缺陷）：state 里的字符串只要含
  `{$x}` 就被当模板递归解析，实测 **27,623 字符变 27,101**。对一个用 `{$x}` 当语法、
  又要拿来写代码编辑器的框架，等于"不能显示自己的源码"。已用窄修法修复
  （`resolve_raw(chain=False)` 只作用于控件绑定，**不动** `For`/`Select` 的链式语义，
  PawUI 全量 400 条测试通过）。
- **主题令牌经 `{$state}` 传递时不解析**（PawUI 缺陷）：`bg="{$x}"`（`x="border"`）
  会打出一条**自相矛盾**的警告（提示里还把 `border` 列为可用主题色），且颜色被静默丢弃。
- **审查门把「未发现问题」判成有问题**：子串匹配 `"问题" in output` 命中了成功文案，
  导致每一次改动都被驳回并重试。已改为结构化 `ToolResult`。
- **验证脚本把 `main.py` 写掉了**：界面自检的 "spy" 把写入转发给真实写盘，
  自检打开的第一个 `.py` 恰好是 `main.py`，它被改成了 1 行 `value = 1`。
  已改为"写入只记录不落盘"，并加了「自检不得改动项目文件」的 sha1 校验。

---

## 打包

```bash
pip install pyinstaller
pyinstaller tcide-pawui.spec                    # 默认：含 QtWebEngine + vendored Monaco
TCIDE_NO_MONACO=1 pyinstaller tcide-pawui.spec  # 省 ~24 MB（界面退回内置编辑器）
TCIDE_NO_WEBENGINE=1 pyinstaller tcide-pawui.spec  # 再省 ~430 MB（<Web> 组件失效）
TCIDE_CONSOLE=1 pyinstaller tcide-pawui.spec    # 带控制台，排查启动崩溃用
```

体积参考：全量 **574 MB**、去 Monaco 约 550 MB、去 QtWebEngine **137 MB**。

**关于 Monaco**：Monaco 默认从 CDN 取资源，而本项目要求完全离线，所以把 npm 包里的
`min/vs` 树 **vendored** 进 `tcide/web/vendor/monaco/`（24.4 MB，151 个文件；
用 `tools/vendor_monaco.py` 可重新生成）。实测它可以从 `file://` 用 **AMD** 方式加载
（ESM 布局会被 CORS 拦住），开箱 **91 种语言**。拿不到时页面自动退回内置编辑器。

> **已知环境限制**：本机冻结后的 PySide6 在 `QApplication()` 处就会崩
> （用 5 行最小探针验证过，与本项目无关），所以打包产物的**可运行性无法在本机验证**；
> spec 本身可以成功构建并正确打包全部资源。详见 [FEATURE_MATRIX.md](FEATURE_MATRIX.md)。

## 许可证

本项目**整仓统一 LGPL-3.0-or-later**。历史上曾是「拆开授权」（库 LGPL + 应用 GPL），
原因是当时依赖的 PawUI 是 GPL-3.0；**PawUI 后来改为 LGPL-3.0-or-later**
（其 `pyproject.toml` 声明 `license = "LGPL-3.0-or-later"`，LICENSE 为 Lesser 全文），
于是不再需要拆开：

| 部分 | 许可证 | 说明 |
|---|---|---|
| 全部（`tcide/`、`main.py`、`app_selftest.py`、`app.paw`、`tools/`、`tests/`） | **LGPL-3.0-or-later** | 见 [`LICENSE`](LICENSE)（PawUI 那份 LGPL-3.0 原文） |

每个源文件头部都有 **SPDX 标识**（`tools/apply_licenses.py` 幂等地写入，
并校验 LICENSE 确实是 LGPL-3.0 全文）。

`tcide/` 的边界由测试守着，破坏即 CI 失败：

- `test_package_does_not_import_pawui` —— 库侧不得依赖 PawUI
  （许可证上已无强制，但这保证 `tcide/` 能独立复用、能离线严格测试）
- `test_package_does_not_import_qt_at_module_level` —— 库侧模块级不得拖进界面栈
  （`tcide/web/bridge.py` 里的 PySide6 是**函数内惰性导入**，属刻意设计：
  不装 Qt 也能导入这个库；PySide6 本身是 LGPL）
- `test_every_library_file_is_lgpl` / `test_every_app_file_is_gpl` —— SPDX 头门禁

> 界面自检原本在库侧（`tcide/shell_selftest.py`），为了让 LGPL 边界**没有例外**，
> 已移到应用侧 `app_selftest.py`。

---

## 下一步

1. `<Web>` + QWebChannel 接入 **Monaco** 编辑器与 **xterm.js** 终端（QtWebEngine 已确认可用）
2. **Diff 审核弹窗**（补上原版丢失的 `acceptDiff`）
3. 把已测过的后端能力接进界面（大纲 / 搜索 / 补全面板）
4. 打包（PyInstaller）与安装包
5. 统一许可证为 LGPL-3.0-or-later（`tools/apply_licenses.py`，见 LICENSE）
