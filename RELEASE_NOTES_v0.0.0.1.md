# 虎猫 TCIDE · PawUI 版 0.0.0.1

用 [PawUI](https://github.com/LK-BLOG/PawUI) 声明式 Python UI 重写的
[TCIDE](https://github.com/Guanist/TCIDE)：覆盖它 README 宣称的全部功能，**包括**
它在自己代码注释里承认「从未接线、静默失效」的十余个模块，以及仓库历史中丢失的
Diff 审核弹窗（`acceptDiff`）。

> 版本号取自仓库的发布线：`0.0.0.1`（首个公开快照）。对应 tag `v0.0.0.1`，
> 应用内可用 `python main.py --version` 查看。

## 亮点

- **994 条离线测试**（`python -m pytest -q` 退出码 0）+ **21 项界面自检**
  （`python main.py --selftest`，含"自检不得改动项目文件与 git 历史"的守卫）
- **双 Agent**：Builder 拆解 → Coder 执行 → **审查门**（AI 的写操作进待审核队列，
  逐块接受/驳回）→ 重试；多模型适配（DeepSeek / 火山方舟 / Ollama / Anthropic / 自定义）
- **工程能力**：上下文分层裁剪、索引与架构分析（Tarjan 真算法，自测 0 循环依赖）、
  Git 智能、无人值守（检查点/恢复）、语义补全、编译错误自愈、11 条诊断规则、
  性能监控、调试管理、**57 条片段**、14 个 CLI 子命令
- **界面**：vendored **Monaco**（离线，91 种语言）、分屏、多标签、独立终端窗口、
  LSP 接线、文件预览、像素宠物、底部「🚀 上手」引导与「工具 ▾」菜单

## 怎么跑（从源码）

```bash
git clone -b v0.0.0.1 https://github.com/Guanist/tcide-pawui
cd tcide-pawui
pip install -r requirements.txt
python main.py .              # 打开当前目录（也可跟任意项目目录）
python main.py --version      # 打印版本号
python main.py --selftest     # 21 项界面自检
python -m pytest -q           # 994 条测试
python -m tcide.cli analyze   # 架构分析（可对任意项目跑）
```

## 诚实的限制

- **本 Release 不含预编译二进制。** 打包脚本能成功构建（`tcide-pawui.spec`，
  含 `TCIDE_NO_MONACO` / `TCIDE_NO_WEBENGINE` / `TCIDE_CONSOLE` 三种构建开关），
  但**冻结后的 PySide6 在作者机器上于 `QApplication()` 处崩溃**——已用 5 行最小探针
  复现，证明与本项目无关（去掉 QtWebEngine 的轻量构建同样崩）。因此无法验证产物
  能否运行，不便附上未经验证的 exe。
- **LSP 未接真语言服务器**：协议与客户端已用**假语言服务器**端到端验证；本机
  `可用 0 / 6`，缺失时界面会如实降级并给出安装提示。
- 依赖 **PawUI ≥ 0.1.3.3**（可直接 `pip install pawui`；PyPI 上现有 0.1.3.3 ~ 0.1.3.5.1）。
  本项目在 **0.1.3.3** 上开发与测试；更新的 PawUI（如 0.1.3.5.1）**未逐一实测**。
  开发期间发现的两个 PawUI 问题已提交上游
  （[#11](https://github.com/LK-BLOG/PawUI/issues/11) /
  [#12](https://github.com/LK-BLOG/PawUI/issues/12)），规避方式见
  [PAWUI_FINDINGS.md](https://github.com/Guanist/tcide-pawui/blob/main/PAWUI_FINDINGS.md)。

## 授权

应用侧（`main.py` / `app.paw` / `app_selftest.py` / `tools/` / `tests/`）为
**LGPL-3.0-or-later**（与 PawUI 一致）。

详见 [REPORT.md](https://github.com/Guanist/tcide-pawui/blob/main/REPORT.md)
与 [FEATURE_MATRIX.md](https://github.com/Guanist/tcide-pawui/blob/main/FEATURE_MATRIX.md)。
