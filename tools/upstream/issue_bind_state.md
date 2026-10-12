## 环境

- PawUI **0.1.3.3**（原版，未打补丁）
- Python 3.12.10 · PySide6 6.11.2 · Windows 11 · `QT_QPA_PLATFORM=offscreen`

## 最小复现

把一段**文本内容**放进 state、再绑定给控件显示，内容里含 PawUI 自己的模板语法：

```python
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from pawui.runtime import Runtime

LITERAL = "a = {$not_a_template}\nb = {$also_literal}\n"
SOURCE = """
<Window title="repro" width="420" height="220">
  <Column><TextArea id="editor" value="{$text}" height="180"/></Column>
</Window>
<script>
text = ""
</script>
"""

app = QApplication([])
runtime = Runtime(SOURCE, "repro.paw", context={})
runtime.run(block=False)
app.processEvents()

runtime.state.set("text", LITERAL)          # 状态里就是这段**内容**
app.processEvents()

widget = runtime.root.findChild(QPlainTextEdit, "editor")
print("state 里: ", len(LITERAL), repr(LITERAL))
print("控件里:   ", len(widget.toPlainText()), repr(widget.toPlainText()))
```

**实测输出：**

```
state 里:  42 'a = {$not_a_template}\nb = {$also_literal}\n'
控件里:    10 'a = \nb = \n'
```

## 期望

控件内容与 state **逐字符一致**。

## 实际

`{$not_a_template}` / `{$also_literal}` 被当成模板插值成了空串。

## 根因

1. `pawui/components.py:494-507` 的 `bind_state` 用 `resolve_raw` 拿"原始值"
   （注释也写明必须走 `resolve_raw`，否则 `bool("False")` 之类会出错）：

   ```python
   def bind_state(self, template, set_fn):
       ...
       set_fn(resolve_raw(_template, self.scope, self.runtime))
   ```

2. 而 `pawui/resolve.py:117-142` 的 `resolve_raw` 为了支持**链式引用**
   （`<Card value="{$name}"/>` 里 `value` 本身又是 `{$name}`）会**递归解析取出的值**：

   ```python
   resolved = _resolve_path(refs[0], scope, runtime)
   if isinstance(resolved, str) and is_template(resolved) and base not in seen:
       return resolve_raw(resolved, scope, runtime, seen)      # ← 二次解析
   return resolved
   ```

于是"原始值"被再解析一遍，内容里的 `{$...}` 遭殃。

## 影响

任何"state 存一段文本 → 绑定给控件显示"的场景都会中招。对**以 `{$x}` 为语法**
的框架来说，这等于"控件无法显示自己的源码"：我在用 PawUI 写一个 IDE，编辑器打开一个
27,623 字符的 `.paw` 源文件时，控件里只剩 **27,101** 字符（所有 `{$...}` 被吃掉）。

## 建议改法

链式解析是**有意设计**（`<For in="{$items}">`、`<Select items="{$rows}">` 依赖它），
所以建议**给调用方一个开关**，而不是直接删掉：

```diff
-def resolve_raw(value, scope, runtime, _seen=None):
+def resolve_raw(value, scope, runtime, _seen=None, *, chain=True):
     ...
-            if isinstance(resolved, str) and is_template(resolved) and base not in seen:
+            if (chain and isinstance(resolved, str) and is_template(resolved)
+                    and base not in seen):
                 seen.add(base)
-                return resolve_raw(resolved, scope, runtime, seen)
+                return resolve_raw(resolved, scope, runtime, seen, chain=chain)
```

```diff
 # pawui/components.py  bind_state
-                    set_fn(resolve_raw(_template, self.scope, self.runtime))
+                    set_fn(resolve_raw(_template, self.scope, self.runtime,
+                                       chain=False))
```

这样：控件绑定拿到的是**原值**（内容不再被当模板），`For`/`Select` 的链式语义**不受影响**。

## 回归测试建议

```python
def test_bind_state_keeps_template_syntax():
    runtime = Runtime('<Window><Column><TextArea id="e" value="{$t}"/></Column></Window>'
                      '<script>t = ""</script>', "t.paw", context={})
    runtime.run(block=False)
    runtime.state.set("t", "a = {$x}\n")
    widget = runtime.root.findChild(QPlainTextEdit, "e")
    assert widget.toPlainText() == "a = {$x}\n"
```

## 附

我在本地只改了上面 `bind_state` 一处（加 `chain=False`），PawUI **全量 400 条测试通过**
（含 `For`/`Select` 的链式用例），说明这个改法不会破坏既有语义。
