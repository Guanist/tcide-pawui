# PawUI 问题报告（可直接提交上游）

本仓库在实现 TCIDE 等价应用的过程中发现的 PawUI 问题。

**复现脚本**：[`tools/repro_pawui_findings.py`](tools/repro_pawui_findings.py) —— 对
**原版**与**打过补丁的树**都能跑，逐条打印 `[存在]` / `[已修复]` / `[跳过]`。

```bash
# 原版基线（从原始压缩包解出的未打补丁 0.1.3.3）
PYTHONPATH="./vanilla_pawui/PawUI-main" python tools/repro_pawui_findings.py
# 打过补丁的树
PYTHONPATH="<...>/pr-work/PawUI" python tools/repro_pawui_findings.py
```

版本：PawUI **0.1.3.3**（原版）· Python 3.12.10 · PySide6 6.11.2 · Windows 11 · 离屏 `QT_QPA_PLATFORM=offscreen`

## 实测对照表

| # | 问题 | 原版 0.1.3.3 | 本仓库补丁后 |
|---|---|---|---|
| 1 | 主题令牌经模板传递时不解析 | ⚠️ 见下方说明（脚本跳过） | 已修复 |
| 2 | `resolve_raw` 会把取出的值当模板**再解析** | **存在** | 仍存在（**有意保留**，见说明） |
| 2b | `bind_state` 未退出该链式解析 → **控件内容被吃掉** | **存在** | 已修复 |
| 3 | `<Image src="{$p}">` **不响应状态变化** | **存在** | 仍存在（**未修**，属上游） |

---

## 问题 2b：`bind_state` 把控件内容当模板再解析（**已修复**，建议上游同样处理）

**影响**：任何"用 state 存一段文本、再绑定给控件显示"的场景，只要文本里含
`{$...}`，内容就会被吃掉。对一个**用 `{$x}` 当语法**、又要拿来写代码编辑器的
框架，这等于"不能显示自己的源码"。

**复现**（原版 0.1.3.3）：

```python
# tools/repro_pawui_findings.py --check 2b
from pawui.components import Component
import inspect
"chain=False" in inspect.getsource(Component.bind_state)   # 原版为 False
```

**实测数据**：应用里 `<TextArea value="{$editor_text}">` 打开一个 27,623 字符的
`.paw` 源文件 → **控件里只剩 27,101 字符**（源码中的 `{$not_a_template}` 之类
被当模板插值成空串）。

**根因**：`resolve_raw` / `resolve_template` 为支持"链式引用"
（`<Card value="{$name}"/>` 里 `value` 本身又是 `{$name}`）会**递归解析取出的值**：

```python
# pawui/resolve.py:117-142（原版行号）
def resolve_raw(value, scope, runtime, _seen=None):
    ...
    resolved = _resolve_path(refs[0], scope, runtime)
    if isinstance(resolved, str) and is_template(resolved) and base not in seen:
        return resolve_raw(resolved, scope, runtime, seen)      # ← 二次解析
    return resolved
```

**为什么不能直接删掉链式解析**：`For in="{$items}"` / `Select items="{$rows}"` 依赖它。
所以**建议的修法是给调用方一个开关**（本仓库的做法，已跑通且 PawUI 全量 400 条测试通过）：

```python
def resolve_raw(value, scope, runtime, _seen=None, *, chain: bool = True):
    ...
        if (chain and isinstance(resolved, str) and is_template(resolved)
                and base not in seen):
            return resolve_raw(resolved, scope, runtime, seen, chain=chain)
```

```python
# pawui/components.py:494-507（bind_state）
set_fn(resolve_raw(_template, self.scope, self.runtime, chain=False))
```

**回归测试建议**：`<TextArea value="{$t}">`，state 里 `t = "a = {$x}\n"`，断言控件
内容与 state **完全一致**。

---

## 问题 3：`<Image src="{$p}">` 不响应状态变化（**未修，待上游确认**）

**影响**：图片无法通过 state 动态切换（预览面板的刚需）。同类组件
`Text`/`Markdown`/`List`/`Table`/`Select`/`TextArea` **都支持**模板刷新，`Image` 不支持，
行为不一致。

**复现**（原版 0.1.3.3，脚本第 3 项）：

```python
runtime = Runtime('<Window><Column><Image id="pic" src="{$pic}"/></Column></Window>'
                  '<script>pic = ""</script>', "repro.paw", context={})
runtime.run(block=False)
runtime.state.set("pic", "real.png")      # 状态确实更新了
QLabel.pixmap()                            # 仍然为空
```

**根因**（读源码即可确认）：

```python
# pawui/components.py:1234-1255（原版行号）
class Image(Component):
    def build(self) -> QLabel:
        label = QLabel()
        src = self.opt_str("src", "")      # ← 只在建树时读一次
        ...
        # 全文没有 bind_state / watch_state 调用
```

对照：`Text`/`Markdown`/`TextArea` 等在 `build()` 里会 `self.bind_state("{$...}", setter)`。

**建议修法**：在 `Image.build()` 末尾按 `Text` 的写法补一个绑定，例如

```python
if isinstance(self.props.get("src"), str) and is_template(self.props["src"]):
    self.bind_state(self.props["src"], lambda value: self._apply(label, value))
```

另外建议：`src` 非空但 `QPixmap.isNull()` 时**抛 `RenderError`** 会让整个界面建树失败
（`components.py:1245-1247`）。对"文件存在但内容损坏"这种情况，打个警告并清空更安全 ——
本仓库被迫在应用侧用命令式 `label.setPixmap()` 绕开这两点。

---

## 问题 1：主题令牌经模板传递时不解析（⚠️ 复现脚本在**原版上会跳过**）

**必须先说明**：`resolve_color` **不是原版函数**，它是本仓库重构时引入的助手
（原版的颜色解析路径在 `runtime.py:646 warn_unknown_color` + `theme.py:57 Theme.color`）。
所以脚本第 1 项在原版上跳过是**预期的**，不能声称"在原版上复现了"。

**现象**（本仓库早期实测记录）：`bg="{$x}"` 且 state 里 `x = "border"` 时：

- 打出**自相矛盾**的警告：提示里把 `border` 列为可用主题色，却说这个值不认识；
- QSS 拿到 `background:border`，被 Qt **静默丢弃**（颜色完全不生效，且没有报错）。

**推测的根因**：颜色属性先做模板插值、插值结果**没有再查一次主题令牌**，
于是字符串 `"border"` 直接当颜色用。

**建议**：上游如确认该路径存在，修法与问题 2 类似 —— 在**插值之后**再做一次令牌查表：

```python
def resolve_color(value, scope, runtime):
    resolved = resolve_prop_value(value, scope, runtime)     # 先插值
    if isinstance(resolved, str):
        token = runtime.theme.color(resolved)                # 再查令牌
        if token:
            return token
    return resolved
```

**需要上游确认的点**：原版里等价的解析路径是否会遇到同样问题。本仓库没有声称
已在原版上复现。

---

## 附：本仓库对这些问题的处置

| 问题 | 处置 |
|---|---|
| 1 | 本地已修（`resolve_color` 插值后再查令牌）+ 回归测试；同时应用侧**推十六进制**而非令牌名，因此不依赖补丁 |
| 2b | 本地已修（`chain=False` 仅作用于 `bind_state`，不动 `For`/`Select` 语义）+ 回归测试 |
| 2 | **有意保留**：`resolve_raw` 默认仍链式解析（`For`/`Select` 需要）。若上游同意改成"默认不链式、需要链式的调用方显式开启"，本仓库愿意跟随 |
| 3 | 应用侧用命令式 `label.setPixmap()` 绕开；**建议上游修** |
