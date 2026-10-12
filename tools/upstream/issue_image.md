## 环境

- PawUI **0.1.3.3**（原版，未打补丁）
- Python 3.12.10 · PySide6 6.11.2 · Windows 11 · `QT_QPA_PLATFORM=offscreen`

## 最小复现

```python
import os, pathlib, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel
from pawui.runtime import Runtime

# 1x1 真 PNG（不依赖外部素材）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db40000"
    "000049454e44ae426082")

SOURCE = """
<Window title="repro" width="300" height="200">
  <Column><Image id="pic" src="{$pic}"/></Column>
</Window>
<script>
pic = ""
</script>
"""

with tempfile.TemporaryDirectory() as tmp:
    image = pathlib.Path(tmp) / "one.png"
    image.write_bytes(PNG)

    app = QApplication([])
    runtime = Runtime(SOURCE, "repro.paw", context={})
    runtime.run(block=False)
    app.processEvents()

    runtime.state.set("pic", str(image))     # 状态确实更新了
    app.processEvents()

    widget = runtime.root.findChild(QLabel, "pic")
    pixmap = widget.pixmap()
    print("控件里是否有图像:", pixmap is not None and not pixmap.isNull())
```

**实测输出：**

```
控件里是否有图像: False
```

## 期望

状态更新后控件里出现图像（与 `Text`/`Markdown`/`List`/`Table`/`Select`/`TextArea`
一样支持 `{$state}` 模板刷新）。

## 实际

控件里始终是空的 —— `<Image>` **不响应状态变化**。

## 根因

`pawui/components.py:1234-1255` 的 `Image.build()` 只在**建树时**读一次 `src`，
全文没有 `bind_state` / `watch_state` 调用：

```python
class Image(Component):
    def build(self) -> QLabel:
        label = QLabel()
        src = self.opt_str("src", "")        # ← 只读一次
        path = Path(src)
        if path.is_file():
            pixmap = QPixmap(str(path))
        else:
            pixmap = QPixmap(src)
        if pixmap.isNull() and src:
            raise RenderError(f"image not found: {src}", self.element.pos)
        ...
```

对照：`Text` / `Markdown` / `TextArea` / `List` / `Table` / `Select` 在 `build()` 里
都会为模板属性装上 `bind_state(...)`。

## 影响

图片无法通过 state 动态切换 —— 文件预览、头像、图标等场景都做不到。
我这边只能绕开：在应用侧直接 `QLabel.setPixmap()`（命令式），等于把框架的
"声明式刷新"能力让掉了。

## 建议改法

在 `Image.build()` 末尾按 `Text` 的写法补一个绑定（示意）：

```diff
     def build(self) -> QLabel:
         label = QLabel()
         src = self.opt_str("src", "")
-        path = Path(src)
-        if path.is_file():
-            pixmap = QPixmap(str(path))
-        else:
-            pixmap = QPixmap(src)
-        if pixmap.isNull() and src:
-            raise RenderError(f"image not found: {src}", self.element.pos)
+        def _apply(value: str) -> None:
+            if not value:
+                label.clear()
+                return
+            path = Path(value)
+            pixmap = QPixmap(str(path) if path.is_file() else value)
+            if pixmap.isNull():
+                # 文件存在但内容损坏时，清空并留一句可诊断的提示，
+                # 而不是抛 RenderError 把整个界面建树打断
+                label.clear()
+                label.setText(f"（无法显示图片: {value}）")
+                return
+            label.setPixmap(pixmap)
+
+        _apply(src)
+        if isinstance(self.props.get("src"), str) and is_template(self.props["src"]):
+            self.bind_state(self.props["src"], _apply)
         ...
```

顺带一个小建议：`src` 非空但 `QPixmap.isNull()` 时**抛 `RenderError`** 会让整个界面
建树失败（`components.py:1245-1247`）。对"文件存在但内容损坏/其实是文本"这种情况，
警告 + 清空比抛异常更安全。

## 回归测试建议

```python
def test_image_follows_state():
    runtime = Runtime('<Window><Column><Image id="p" src="{$p}"/></Column></Window>'
                      '<script>p = ""</script>', "t.paw", context={})
    runtime.run(block=False)
    runtime.state.set("p", str(png_path))
    label = runtime.root.findChild(QLabel, "p")
    assert label.pixmap() is not None and not label.pixmap().isNull()
```
