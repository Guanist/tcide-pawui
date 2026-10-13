# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""对**真实**模型端点做一次冒烟测试（Key 从环境变量读，绝不写进文件）。

用法::

    $env:TCIDE_API_KEY="sk-..."
    python tools/try_model.py                    # 默认 deepseek

会验证三件事：
1. 普通对话能通（``/chat/completions`` 的请求体与解析）；
2. 工具调用（tool_calls）能被正确识别 —— 这是双 Agent 引擎的关键路径；
3. 用量统计（usage）能读出来。

**为什么值得单独跑**：``tests/test_models.py`` 全用本地假 HTTP 服务，
能证明协议自洽，但证明不了"真端点认这套请求体"。这个脚本补上那一环。
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取工作区里的一个文本文件",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径"}},
            "required": ["path"],
        },
    },
}


def main() -> int:
    key = os.environ.get("TCIDE_API_KEY", "").strip()
    if not key:
        print("★ 没有设置 TCIDE_API_KEY")
        return 2
    provider = os.environ.get("TCIDE_PROVIDER", "deepseek")

    from tcide.models import (ChatMessage, ModelConfig, ModelError, adapter_for,
                              complete)

    config = ModelConfig(provider=provider, api_key=key)
    adapter = adapter_for(config)
    print(f"服务商={config.provider}  适配器={config.adapter_kind}  "
          f"端点={config.base_url}")
    print(f"模型={config.model}  超时={config.timeout}s  Key=***{key[-4:]}"
          f"（长度 {len(key)}）")
    print()

    # ---- 0. 连接测试（拉模型列表）---------------------------------------
    print("=== 0) 连接测试（拉模型列表）===")
    result = adapter.test_connection()
    print(f"结果：{result}")
    print()

    # ---- 1. 普通对话 ---------------------------------------------------
    print("=== 1) 普通对话 ===")
    try:
        response = complete(config, [
            ChatMessage(role="system", content="你是简洁的助手，回答不超过 20 字。"),
            ChatMessage(role="user", content="用一句话说明什么是素数。"),
        ])
    except ModelError as exc:
        print(f"★ 失败：{exc}")
        return 1
    print(f"回复：{response.text!r}")
    print(f"模型={response.model!r} 结束原因={response.finish_reason!r}")
    print(f"用量：{response.usage.to_dict()}")
    print()

    # ---- 2. 工具调用 ---------------------------------------------------
    print("=== 2) 工具调用（双 Agent 的关键路径）===")
    try:
        response = complete(config, [
            ChatMessage(role="system",
                        content="需要读文件时必须调用 read_file 工具，不要凭空回答。"),
            ChatMessage(role="user",
                        content="请读取工作区里的 tcide/diff.py 这个文件。"),
        ], tools=[WEATHER_TOOL])
    except ModelError as exc:
        print(f"★ 失败：{exc}")
        return 1
    print(f"文本：{response.text!r}")
    if response.tool_calls:
        for call in response.tool_calls:
            print(f"工具调用：{call.name}  参数={call.arguments}")
        print("✓ 工具调用解析成功（引擎能据此真正去读文件）")
    else:
        print("✗ 模型没有返回 tool_calls —— 引擎会退化成纯文本回复")
    print()
    print("冒烟测试完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
