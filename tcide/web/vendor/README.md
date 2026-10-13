# 第三方代码（vendored）

本目录下是**第三方**的前端资源，为满足「完全离线可用」而 vendor 进来。

## Monaco Editor

- 来源：<https://github.com/microsoft/monaco-editor>（版本：0.57.0）
- 授权：**MIT**，原文见 [`monaco/LICENSE.txt`](monaco/LICENSE.txt)
- 获取方式：`python tools/vendor_monaco.py`

## 重要：这里的文件不适用本仓库的 LGPL

本目录下的文件保留它们**各自的**授权（Monaco 为 MIT）。
`tools/apply_licenses.py` 的 glob（`tcide/web/*.py`）刻意**不匹配**本目录，
`tests/test_layering.py` 也断言了本目录里不得出现我们的 SPDX 头 ——
给第三方文件改署名是篡改许可证，不是「统一」。
