# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
r"""复现本轮发现的 3 个 PawUI 问题（可对原版 / 打过补丁的树分别运行）。

用法::

    # 对已安装的发行版（原版）跑
    python tools/repro_pawui_findings.py

    # 对打过补丁的本地树跑（验证修法有效）
    set PYTHONPATH=...\pr-work\PawUI
    python tools/repro_pawui_findings.py

每个检查打印 ``[存在]`` / ``[已修复]`` / ``[跳过]``，退出码始终 0（除非脚本自身出错）：
这是一份**诊断报告**，不是断言 —— 它要能在两个版本上都跑出可信结论。
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --single-process")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

results: list[tuple[str, str, str]] = []


def record(name: str, state: str, detail: str) -> None:
    results.append((name, state, detail))
    print(f"  [{state}] {name}\n         {detail}")


def banner(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


_SHARED: dict = {}


def shared_runtime():
    """建一个**真** Runtime 供各检查共用。

    第一版用自制 FakeRuntime，结果 ``resolve_raw``/``resolve_color`` 内部还要
    ``runtime.namespace`` → 检查直接"跳过"，等于没验证。真 runtime 才可信。
    """
    if "runtime" in _SHARED:
        return _SHARED["runtime"]
    from PySide6.QtWidgets import QApplication

    from pawui.runtime import Runtime

    QApplication.instance() or QApplication([])
    source = """
<Window title="repro" width="200" height="120">
  <Column>
    <Text id="label" text="x"/>
  </Column>
</Window>
<script>
text = ""
</script>
"""
    runtime = Runtime(source, "repro.paw", context={})
    runtime.run(block=False)
    _SHARED["runtime"] = runtime
    return runtime


# ---------------------------------------------------------------------------
# 问题 1：主题令牌经模板传递时不解析（resolve_color）
# ---------------------------------------------------------------------------

def check_color_token_through_template() -> None:
    banner("问题 1：bg=\"{$x}\"（x=\"border\"）—— 令牌名经模板后不解析")
    try:
        from pawui.resolve import resolve_color

        runtime = shared_runtime()
    except Exception as exc:                      # noqa: BLE001
        record("令牌经模板传递", "跳过", f"无法准备环境: {type(exc).__name__}: {exc}")
        return

    scope = {"x": "border"}
    try:
        value = resolve_color("{$x}", scope, runtime)
    except Exception as exc:                      # noqa: BLE001
        record("令牌经模板传递", "跳过", f"调用失败: {type(exc).__name__}: {exc}")
        return

    expected = getattr(runtime.theme, "border", "#3a3a3c")
    if isinstance(value, str) and value.startswith("#"):
        record("令牌经模板传递", "已修复",
               f"{{$x}} -> {value!r}（正确解析成令牌色）")
    elif value == "border":
        record("令牌经模板传递", "存在",
               "{$x} -> 'border'：插值出令牌名后**没有再查主题** → "
               "QColor('border') 非法、QSS 的 background:border 被 Qt 静默丢弃")
    else:
        record("令牌经模板传递", "存在", f"{{$x}} -> {value!r}（预期 {expected!r}）")


# ---------------------------------------------------------------------------
# 问题 2：state 里的文本含 {$...} 时被当模板二次解析（resolve_raw）
# ---------------------------------------------------------------------------

LITERAL = "a = {$not_a_template}\nb = {$also_literal}\n"


def check_raw_value_not_reparsed() -> None:
    banner("问题 2：state 文本含 {$...} 时被当模板吃掉（resolve_raw / resolve_template）")
    try:
        from pawui.resolve import resolve_raw

        runtime = shared_runtime()
    except Exception as exc:                      # noqa: BLE001
        record("文本被二次解析", "跳过", f"无法准备环境: {type(exc).__name__}: {exc}")
        return

    runtime.state.set("text", LITERAL)
    scope = {"text": LITERAL}
    try:
        plain = resolve_raw("{$text}", scope, runtime)
    except Exception as exc:                      # noqa: BLE001
        record("文本被二次解析", "跳过", f"调用失败: {type(exc).__name__}: {exc}")
        return

    if plain == LITERAL:
        record("文本被二次解析", "已修复",
               f"原样返回 {len(str(plain))} 字符（内容里的 {{$...}} 未被吃掉）")
        return
    record("文本被二次解析", "存在",
           f"{len(LITERAL)} 字符被解析成 {len(str(plain))} 字符：{plain!r} —— "
           "取出的字符串又被当模板解析了一遍")


def check_bind_state_keeps_content() -> None:
    """同一问题的第二个视角：bind_state 用的是 resolve_raw(chain=False) 吗？"""
    banner("问题 2b：控件绑定（bind_state）是否会把内容当模板再解析")
    try:
        import inspect

        from pawui.components import Component

        source = inspect.getsource(Component.bind_state)
    except Exception as exc:                      # noqa: BLE001
        record("bind_state 保原值", "跳过", f"无法读取源码: {exc}")
        return
    if "chain=False" in source:
        record("bind_state 保原值", "已修复", "bind_state 使用 resolve_raw(..., chain=False)")
    else:
        record("bind_state 保原值", "存在",
               "bind_state 未传 chain=False：控件拿到的值会被再解析一次，"
               "含 {$...} 的文本（例如正在编辑的 .paw 源码）会被吃掉")


# ---------------------------------------------------------------------------
# 问题 3：<Image> 不响应状态变化
# ---------------------------------------------------------------------------

def check_image_reactivity() -> None:
    banner("问题 3：<Image src=\"{$p}\"> 是否响应状态变化")
    try:
        from PySide6.QtWidgets import QApplication, QLabel
    except Exception as exc:                      # noqa: BLE001
        record("Image 响应状态", "跳过", f"没有 Qt: {exc}")
        return
    try:
        from pawui.runtime import Runtime
    except Exception as exc:                      # noqa: BLE001
        record("Image 响应状态", "跳过", f"无法导入 Runtime: {exc}")
        return

    import pathlib
    import tempfile

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        png = pathlib.Path(tmp) / "p.png"
        # 写一个 1x1 真 PNG（避免依赖外部素材）
        png.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c63000100000500010d0a2db40000"
            "000049454e44ae426082"))

        source = """
<Window title="t" width="200" height="120">
  <Column>
    <Image id="pic" src="{$pic}"/>
  </Column>
</Window>
<script>
pic = ""
</script>
"""
        runtime = Runtime(source, "repro.paw", context={})
        runtime.run(block=False)
        app.processEvents()

        runtime.state.set("pic", str(png))
        app.processEvents()

        label = runtime.root.findChild(QLabel, "pic")
        if label is None:
            record("Image 响应状态", "跳过", "找不到 Image 控件（组件命名可能不同）")
            return
        has_pixmap = label.pixmap() is not None and not label.pixmap().isNull()
        if has_pixmap:
            record("Image 响应状态", "已修复", "状态更新后控件里出现了图像")
        else:
            record("Image 响应状态", "存在",
                   "状态已 set 成图片路径，但控件里没有图像 —— "
                   "Image.build() 只在建树时读一次 src，未调用 bind_state/watch_state")


def main() -> int:
    print("PawUI 问题复现脚本")
    try:
        import pawui
        print(f"pawui 位置: {pawui.__file__}")
        print(f"pawui 版本: {getattr(pawui, '__version__', '未知')}")
    except Exception as exc:                      # noqa: BLE001
        print(f"无法导入 pawui: {exc}")
        return 2

    check_color_token_through_template()
    check_raw_value_not_reparsed()
    check_bind_state_keeps_content()
    check_image_reactivity()

    print()
    print("=" * 72)
    print("汇总")
    print("=" * 72)
    for name, state, _detail in results:
        print(f"  {state:6} {name}")
    present = sum(1 for _n, s, _d in results if s == "存在")
    print()
    print(f"仍在的问题: {present} / {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
