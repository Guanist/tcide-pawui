# 虎猫 TCIDE · PawUI 版 —— 交付报告

> 用 **PawUI** 重写 [TCIDE](https://github.com/Guanist/TCIDE) 的可运行成果报告。
> 实施依据与逐条取舍见 [FEATURE_MATRIX.md](FEATURE_MATRIX.md)；
> PawUI 上游问题见 [PAWUI_FINDINGS.md](PAWUI_FINDINGS.md)。

**结论**：TCIDE README 宣称的功能**已全部实现**，包括它在自己代码注释里承认
「**从未接线、静默失效**」的那批高级模块，以及它在仓库历史中**丢失**的 Diff 审核弹窗。
全部配套可离线运行的程序化测试。

---

## 1. 交付对照（宣称 → 实现 → 验证方式）

| TCIDE 宣称 | 原版状态 | 本实现 | 验证方式 |
|---|---|---|---|
| Builder 需求拆解 → Coder 执行 | ✅ | `agent.py` | 假模型断言循环/统计 |
| 审查门 `REVIEWER_GATE_ENABLED` | ✅ | 确定性检查优先，模型评审可选 | 假模型断言驳回→重试 |
| 并行 Coder + 重试 | ✅ | `AgentOrchestrator` | `threading.Barrier` **验证真并发** |
| 多模型（DeepSeek/方舟/Ollama/Anthropic/自定义） | ✅ | `models.py` | **本地假 HTTP 服务**断言请求体 |
| MCP 工具 | ✅ | `tools.py`（12 个 + 危险命令黑名单） | 直接调用断言 |
| 32K 上下文压缩 | ⚠️ **从未接线** | `trimmer.py` | 分层裁剪逐条断言 |
| 代码熵评估 | ⚠️ **从未接线** | `pressure.py`（诚实改名：加权启发式） | 采样/趋势断言 |
| Git 智能 | ⚠️ **从未接线** | `gitintel.py` | 临时仓库**真跑 git** |
| 仓库分析 | ⚠️ **从未接线** | `indexer.py` + `arch.py`（Tarjan 真算法） | 构造环/分层图断言 |
| 无人值守（超时/检查点/恢复） | ⚠️ **从未接线** | `runner.py` | 真跑 + 检查点恢复 |
| 语义补全 | ⚠️ **从未接线** | `completion.py`（诚实定位：前缀补全） | 排序规则断言 |
| 编译错误自动修复 | ⚠️ **从未接线** | `autoheal.py`（可解释建议 + 置信度） | 4 种工具输出解析 |
| 实时诊断 | ⚠️ **从未接线** | `lint.py`（11 条规则，纯标准库） | 正反例断言 |
| 性能监控 | ⚠️ **从未接线** | `perf.py`（Python 侧 tracemalloc，如实标注） | 计时/内存/GC 断言 |
| 调试管理 | ⚠️ **从未接线** | `debug.py`（**不含** DAP 客户端，已标注） | 配置/断点/命令断言 |
| **50+ 代码片段** | ❌ 兜底返回 `[]` | `snippets.py` **57 条**（含 17 条 PawUI 专属） | 数量/唯一性/展开断言 |
| **Diff 审核弹窗**（`acceptDiff`） | ❌ **仓库历史中丢失** | `diff.py` 引擎 + 审查门 + `<Dialog>` 弹窗 | 按 hunk 选择性应用 + 端到端 |
| 9 状态像素宠物 | ✅ | `pet.py`，接进状态栏 `<Canvas>` | 状态机/动画/绘制指令断言 |
| Monaco 编辑器 | ✅ | **vendored 24.4 MB**，实测 **91 种语言** | 探针 + 断言引擎名 |
| 多标签 | ✅ | `tabs.py`（最短唯一后缀区分同名文件） | 模型 + 自检点击切换 |
| 分屏 | ✅ | 页面内双 Monaco 实例 | 真跑两个实例并读回内容 |
| 终端 | ✅ | `term.py` + 离线终端前端（真子进程） | 端到端真跑命令 |
| LSP（TS/Python + 自动识别） | ✅ | `lsp.py`（真 JSON-RPC + 可跳过设计） | **假语言服务器**端到端 |
| 文件预览（图片/文本/二进制） | ✅ | `panels.py` 预览层 + 魔数嗅探 | 假 PNG 安全退回 |
| 命令行 | ❌ 只有 GUI | `cli.py`（14 个子命令 + `--json`） | 53 条 CLI 测试 |
| 测试 / CI / LICENSE | ❌ 全无 | 990 条测试 + CI + 拆开授权 | 见下 |

---

## 2. 证据（全部为实测数字）

| 项 | 数值 |
|---|---|
| 测试 | **990 条全部通过**（`pytest -q` 退出码 0），28 个文件 / 7,406 行 |
| 界面自检 | **18/18 通过**（离屏真跑 Qt 交互，含"自检不得改动项目文件"的 sha1 校验） |
| 后端规模 | `tcide/` **28 个模块 / 9,208 行**（+ `tcide/web/` 前端资源） |
| 应用规模 | `main.py` 1,640 行 · `app.paw` 216 行 · `app_selftest.py` 386 行 |
| 自证 | `python -m tcide.cli analyze` 对自己跑：230 文件 / 115 依赖边 / **0 循环依赖** / 6 层 |
| 离线 | 页面**零外部网络引用**（`html_is_offline()` 有测试守着） |
| 打包 | 已真跑构建：`tcide.exe` 2.7 MB，全量产出 **574 MB**、去 QtWebEngine **137 MB**，`app.paw` 已打入 |

复现命令：

```bash
python -m pytest -q                                  # 全量（界面部分需 QT_QPA_PLATFORM=offscreen）
python main.py --selftest                            # 18 项界面自检
python -m tcide.cli analyze                          # 架构分析（对自己跑）
python -m tcide.cli check --strict                   # 静态检查
python -m tcide.cli snippet --json                   # 57 条片段
python tools/repro_pawui_findings.py                 # PawUI 问题复现（可对原版/补丁版）
python tools/diagnose_monaco.py                      # Monaco 引擎自诊断
```

---

## 3. 过程中发现并修复的问题（60 条）

完整清单在 [FEATURE_MATRIX.md](FEATURE_MATRIX.md) §4。分组计数：

| 类别 | 条数 | 代表条目 |
|---|---|---|
| 应用自身逻辑缺陷 | 24 | 审查门把「未发现问题」判成有问题（子串匹配 `"问题" in output`）→ 每次改动都被驳回重试；`RunReport.ok` 把驳回次数当失败；`call_graph` 裸符号名跨文件覆盖 |
| 静默失败类 | 8 | `<CodeBlock text=...>` 属性不存在 → Diff 弹窗内容**从来没显示**；`workspace.search` 不返回文件名命中；`git commit --amend` 空提交静默失败 |
| **PawUI 上游问题** | **3** | ①`<Image>` 不响应状态变化 ②`bind_state` 把控件内容当模板再解析（实测 27,623 字符变 27,101）③主题令牌经模板传递时不解析 |
| 验证/工具基建事故 | 12 | 自检把 `main.py` 写成 `value = 1`；`rmtree(ignore_errors=True)` 累积 129 项残留；PowerShell 改 UTF-8 文件**三次**写坏中文；`os._exit` 吞掉缓冲输出 |
| 测试自身缺陷 | 13 | 子串检查误报（**5 次**）；`tmp_path` 两次撞 ACL 污染；把异步调用当同步；断言依赖列表下标 |

三类问题最值得记：

1. **验证脚本把项目文件写掉了**：界面自检的 "spy" 把写入转发给真实写盘，自检打开的第一个
   `.py` 恰好是 `main.py`，它被改成了 1 行 `value = 1`。已改为"只记录不落盘"并加了
   **「自检不得改动项目文件」** 的 sha1 校验。
2. **我差点声称 Monaco 可用**：重写后 11 条编辑器测试全绿，但它们用的是
   `window.tcideValue()`，而**内置回退编辑器也实现它**。补了一条断言引擎名的测试，
   立刻抓到真相（引擎实际是 `none`）；深入诊断后确认是"等待窗口太短"（Monaco 离屏
   冷启动需 20 秒以上），修好后 Monaco 引擎被证实生效。
3. **三次编码事故**：用 PowerShell 的 `Set-Content` 改 UTF-8 源文件，把中文变成乱码
   （`FEATURE_MATRIX.md`、`tcide/web/__init__.py`、`tests/test_lsp.py`）。最终加了
   **全仓编码门禁**（BOM / 乱码 / CRLF 三项），让这类损坏在下次跑测试时就暴露。

---

## 4. 诚实的缺口与限制

| 项 | 状态 |
|---|---|
| **打包产物可运行性** | **无法在本机验证**。已用 5 行最小探针证明：冻结后的 PySide6 在本机环境**在 `QApplication()` 处就崩**（`0xC0000005`），与本项目、spec、QtWebEngine **无关**（去掉 WebEngine 的 137 MB 轻量构建同样崩）。spec 本身可成功构建并正确打包全部资源。真机（完整 PySide6）预期可用。 |
| LSP 实际可用性 | 逻辑与协议已用**假语言服务器**端到端验证；本机**未安装**任何语言服务器（`可用 0 / 6`），所以"接上真 pyright/gopls"未实测。设计上缺失时**如实降级**并有安装提示。 |
| Monaco 冷启动 | 离屏冷启动需 20 秒以上（要读 2.4 MB 主文件 + 分块）。已把测试等待窗口放宽到 90 秒，并在页面里保留**内置编辑器回退**。 |
| Emmet / 真 LSP 富交互（重命名、跳转定义） | 未实现。前者需自研简化版，后者需语言服务器支持与更多 UI。 |
| `tcide/web/vendor/monaco` | 24.4 MB 第三方资源入库。已提供 `TCIDE_NO_MONACO=1` 轻量构建开关，但不改变"默认仓库较大"这一事实。 |
| 上游 issue/PR | PawUI 的三条问题已整理成可直接提交的报告，**尚未提交**（需作者授权）。 |

---

## 5. 架构与许可证

```
app.paw           PawUI 声明式界面（216 行）
main.py           入口 + 后端接线（1,640 行，30+ 处理器）
app_selftest.py   18 项界面自检
tcide/            后端核心（28 个模块，**不 import PawUI**，可离线严格测试）
  ├─ agent/gitintel/lint/diff/tabs/pet/lsp/term/panels/cli/…
  └─ web/  Monaco 兼容协议 + QWebChannel 桥 + 离线前端（含 vendored Monaco）
tests/            990 条（28 个文件）
tools/            探针、vendor 脚本、许可证脚本、诊断脚本
```

**统一授权**：整仓 **LGPL-3.0-or-later**（与 PawUI 一致 —— PawUI 已从 GPL 改为 LGPL）：

| 部分 | 许可证 |
|---|---|
| 全部 | **LGPL-3.0-or-later**（见 `LICENSE`，与 PawUI 同一份 LGPL-3.0 原文） |

边界由测试守着：库侧不得 import `pawui`、模块级不得依赖 Qt、每个文件必须有对应 SPDX 头。
