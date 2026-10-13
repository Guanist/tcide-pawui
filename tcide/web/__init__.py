# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""``<Web>`` 编辑器前端资源与 QWebChannel 桥。

- :mod:`tcide.web.bridge` —— 消息协议（纯逻辑）+ Qt 桥（惰性导入 PySide6）
- ``editor.html`` —— 离线单文件编辑器前端（不引用任何外部网络资源）

Qt 只在 ``make_bridge()`` 内部导入，所以本包在没装 PySide6 时也能导入，
纯协议函数照常可测。
"""
