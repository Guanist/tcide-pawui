# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：虎猫 TCIDE · PawUI 版。

用法：``pyinstaller tcide-pawui.spec``（在项目根目录执行）

**诚实标注**：本仓库的开发环境里没有安装 PyInstaller，因此这个 spec
**没有被实际执行验证过**。已经做的是把能静态验证的部分写成测试
（``tests/test_packaging.py``）：入口文件存在、声明要打包的数据文件存在、
``tcide/`` 的模块都能导入、``hiddenimports`` 覆盖了动态导入的模块。

PySide6 的体积主要来自 QtWebEngine（Monaco/xterm 路线要用）。如果不需要它，
把 ``EXCLUDE_QT`` 里的开关打开即可显著瘦身 —— 但那样 ``<Web>`` 组件会失效。
"""

import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821 - PyInstaller 注入
sys.path.insert(0, str(SPEC_DIR))

from tcide.cli import __version__             # noqa: E402

#: ``TCIDE_CONSOLE=1`` 时构建成带控制台的程序。
#:
#: 为什么需要这个开关：GUI 程序 ``console=False`` 时 stdout 不接终端，
#: **启动阶段崩了什么也看不到**（本项目实测：打包产物 --shot 不出图、也没有任何
#: 输出，无法定位）。排查冻结环境问题时先这样构建一次拿到真实报错。
CONSOLE = os.environ.get("TCIDE_CONSOLE", "") not in ("", "0", "false", "False")

#: ``TCIDE_NO_WEBENGINE=1`` 时排除 QtWebEngine（体积从 ~570MB 降到 ~200MB，
#: 但 ``<Web>`` 组件与 Web 编辑器会失效）
EXCLUDE_QT_WEBENGINE = os.environ.get("TCIDE_NO_WEBENGINE", "") not in (
    "", "0", "false", "False")

#: ``TCIDE_NO_MONACO=1`` 时不打包 vendored 的 Monaco（省 ~24 MB）。
#: 界面会自动退回内置编辑器（editor.html 里有回退实现），功能不丢、只是没有
#: Monaco 的语法高亮与 minimap。
EXCLUDE_MONACO = os.environ.get("TCIDE_NO_MONACO", "") not in (
    "", "0", "false", "False")

#: 必须随程序一起分发的数据文件
DATAS = [
    (str(SPEC_DIR / "app.paw"), "."),                     # 界面定义，运行时读
    (str(SPEC_DIR / "FEATURE_MATRIX.md"), "."),           # 菜单里可打开
    (str(SPEC_DIR / "README.md"), "."),
    # 前端资源（离线硬要求）：编辑器/终端页面 + vendored Monaco
    (str(SPEC_DIR / "tcide" / "web" / "editor.html"), "tcide/web"),
    (str(SPEC_DIR / "tcide" / "web" / "terminal.html"), "tcide/web"),
]
if not EXCLUDE_MONACO:
    DATAS.append((str(SPEC_DIR / "tcide" / "web" / "vendor"), "tcide/web/vendor"))
else:
    print("[spec] TCIDE_NO_MONACO=1：不打包 Monaco（界面退回内置编辑器）")

#: 动态导入 / 运行时才用到的模块，静态分析抓不到
HIDDEN_IMPORTS = [
    "pawui",
    "pawui.runtime",
    "pawui.components",
    "pawui.parser",
    "pawui.resolve",
    "pawui.theme",
    "pawui.widgets",
    "pawui.nodes",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtMultimedia",
    "PySide6.QtPdf",
    "PySide6.QtSvgWidgets",
    "sqlite3",
    "tracemalloc",
]

#: 确认这些模块真的能导入（导入失败会让 spec 直接报错，好过打包出坏包）
for name in HIDDEN_IMPORTS:
    if name.startswith("PySide6") or name in ("pawui",):
        continue          # 由环境决定是否可用，不在这里强制
    __import__(name)

EXCLUDES = [
    "tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest", "_pytest",
    "IPython", "notebook", "setuptools", "pip",
]
if EXCLUDE_QT_WEBENGINE:
    EXCLUDES += ["PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore",
                 "PySide6.QtWebChannel"]

a = Analysis(                                   # noqa: F821
    [str(SPEC_DIR / "main.py")],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)                               # noqa: F821

exe = EXE(                                      # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tcide",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                                  # UPX + QtWebEngine 容易出问题，关掉
    console=CONSOLE,                             # TCIDE_CONSOLE=1 可切到带控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 注意：``EXE(version=...)`` 要的是**版本信息文件的路径**（PyInstaller 的
    # version-info 资源文件），不是版本文本。第一版这里传了
    # f"虎猫 TCIDE {__version__}"，真跑打包时 PyInstaller 直接去打开这个
    # 文件名并报 FileNotFoundError —— 因为 spec 从未实跑，这个错一直没暴露。
    # 版本号通过下面 COLLECT 的目录名体现；要 Windows 文件属性版本号的话，
    # 需要另写一个 version 资源文件并在此给出路径。
)

coll = COLLECT(                                 # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=["vcruntime140.dll", "python3.dll", "Qt6WebEngineCore.dll"],
    name="tcide-pawui",
)
