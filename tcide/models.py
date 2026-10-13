# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""多模型适配层：OpenAI 兼容 / Anthropic / Ollama。

对应 TCIDE 原版的 `core/model/adapter.ts`（564 行）与 README 宣称的五家服务商
（DeepSeek / 火山方舟 / Ollama / Anthropic / 自定义 OpenAI 兼容端点）。

设计要点：
- **只用标准库 urllib**：后端核心不引入第三方 HTTP 依赖，分发更省事；
- 请求构造与响应解析**拆成纯函数**（`build_request` / `parse_response`），
  这样可以用本地假 HTTP 服务把三家的协议逐字段断言，完全离线；
- 错误一律抛 :class:`ModelError`，并带上响应体片段（否则界面只能显示「失败了」）；
- 不假设各家字段一致：Anthropic 的 `system` 要提到顶层、工具结果用 content block，
  Ollama 用 `options`、模型列表在 `/api/tags`。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

#: 默认超时（秒）
DEFAULT_TIMEOUT = 60.0

#: Anthropic 要求的版本头
ANTHROPIC_VERSION = "2023-06-01"


class ModelError(Exception):
    """调用模型失败（网络、协议、鉴权、解析）。"""


# ---------------------------------------------------------------------------
# 服务商预设
# ---------------------------------------------------------------------------

@dataclass
class ProviderPreset:
    """一个服务商的默认端点与常用模型。"""

    key: str
    label: str
    adapter: str          # openai | anthropic | ollama
    base_url: str
    model: str
    needs_key: bool = True

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "adapter": self.adapter,
                "base_url": self.base_url, "model": self.model,
                "needs_key": self.needs_key}


#: README 宣称的五家 + 常见自定义
PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset("deepseek", "DeepSeek", "openai",
                               "https://api.deepseek.com/v1", "deepseek-chat"),
    "volcengine": ProviderPreset("volcengine", "火山方舟", "openai",
                                 "https://ark.cn-beijing.volces.com/api/v3",
                                 "doubao-pro-32k"),
    "openai": ProviderPreset("openai", "OpenAI", "openai",
                             "https://api.openai.com/v1", "gpt-4o-mini"),
    "anthropic": ProviderPreset("anthropic", "Anthropic", "anthropic",
                                "https://api.anthropic.com/v1", "claude-3-5-sonnet-latest"),
    "ollama": ProviderPreset("ollama", "Ollama（本地）", "ollama",
                             "http://localhost:11434", "llama3", needs_key=False),
    "custom": ProviderPreset("custom", "自定义 OpenAI 兼容", "openai",
                             "http://localhost:8000/v1", "local-model",
                             needs_key=False),
}


@dataclass
class ModelConfig:
    """一次会话的模型配置。"""

    provider: str = "deepseek"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = DEFAULT_TIMEOUT
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: 显式指定协议适配器（openai / anthropic / ollama）。留空则按 provider 推断。
    adapter: str = ""

    def __post_init__(self) -> None:
        preset = PRESETS.get(self.provider)
        if preset:
            if not self.base_url:
                self.base_url = preset.base_url
            if not self.model:
                self.model = preset.model
        self.base_url = (self.base_url or "").rstrip("/")

    @property
    def adapter_kind(self) -> str:
        """协议适配器类别。

        **未知服务商名会报错，而不是悄悄退化**：`provider="deepsek"` 这种拼错
        以前会被当成 OpenAI 兼容端点继续跑，用户只会看到莫名的 404。
        自定义端点请用 ``provider="custom"``，或用 ``adapter=`` 显式指定协议。
        """
        if self.adapter:
            if self.adapter not in ADAPTERS:
                raise ModelError(f"未知适配器 {self.adapter!r}（可用: {sorted(ADAPTERS)}）")
            return self.adapter
        preset = PRESETS.get(self.provider)
        if preset is None:
            raise ModelError(
                f"未知服务商 {self.provider!r}；可用: {sorted(PRESETS)}。"
                f"自定义端点请用 provider='custom' 并设置 base_url，"
                f"或用 adapter='openai'|'anthropic'|'ollama' 显式指定协议。")
        return preset.adapter

    def validate(self) -> None:
        """调用前检查：适配器/端点/模型必须明确；需要鉴权的服务商必须有 key。"""
        self.adapter_kind  # 未知服务商/适配器在这里就报错
        if not self.base_url:
            raise ModelError(f"服务商 {self.provider!r} 缺少 base_url")
        if not self.model:
            raise ModelError(f"服务商 {self.provider!r} 缺少 model")
        preset = PRESETS.get(self.provider)
        if preset and preset.needs_key and not self.api_key:
            raise ModelError(f"服务商 {preset.label} 需要 API Key")

    def to_dict(self, *, hide_key: bool = True) -> dict:
        try:
            kind = self.adapter_kind
        except ModelError:
            kind = ""
        return {
            "provider": self.provider, "model": self.model,
            "base_url": self.base_url, "adapter": kind,
            "api_key": ("***" if self.api_key else "") if hide_key else self.api_key,
            "temperature": self.temperature, "max_tokens": self.max_tokens,
            "timeout": self.timeout,
        }


# ---------------------------------------------------------------------------
# 消息与工具调用
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """模型请求调用一个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class ChatMessage:
    """线上格式的一条消息（与 `trimmer.Message` 区分：那个负责预算，这个负责协议）。"""

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "",
                  tool_calls: list[ToolCall] | None = None) -> ChatMessage:
        return cls(role="assistant", content=content, tool_calls=list(tool_calls or []))

    @classmethod
    def tool(cls, tool_call_id: str, content: str,
             name: str = "") -> ChatMessage:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    def to_openai(self) -> dict:
        data: dict[str, Any] = {"role": self.role}
        if self.role == "tool":
            data["tool_call_id"] = self.tool_call_id
            data["content"] = self.content
            return data
        data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": call.id, "type": "function",
                 "function": {"name": call.name,
                              "arguments": call.raw_arguments
                              or json.dumps(call.arguments, ensure_ascii=False)}}
                for call in self.tool_calls
            ]
        return data

    def to_anthropic(self) -> dict:
        if self.role == "tool":
            return {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": self.tool_call_id,
                 "content": self.content}]}
        if self.role == "assistant" and self.tool_calls:
            blocks: list[dict] = []
            if self.content:
                blocks.append({"type": "text", "text": self.content})
            blocks += [{"type": "tool_use", "id": call.id, "name": call.name,
                        "input": call.arguments} for call in self.tool_calls]
            return {"role": "assistant", "content": blocks}
        return {"role": self.role, "content": self.content}

    def to_ollama(self) -> dict:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [{"function": {"name": call.name,
                                                "arguments": call.arguments}}
                                  for call in self.tool_calls]
        return data


@dataclass
class Usage:
    """token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens}


@dataclass
class ChatResponse:
    """一次补全的结果（三家统一）。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict:
        return {"text": self.text, "tool_calls": [c.to_dict() for c in self.tool_calls],
                "usage": self.usage.to_dict(), "model": self.model,
                "finish_reason": self.finish_reason}


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

class ModelAdapter:
    """协议适配基类。子类实现 `build_request` / `parse_response` / `list_models`。"""

    kind = "base"

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    # -- 纯函数部分（可离线断言）------------------------------------------

    def build_request(self, messages: list[ChatMessage],
                      tools: list[dict] | None = None) -> tuple[str, dict, dict]:
        """返回 (url, headers, payload)。"""
        raise NotImplementedError

    def parse_response(self, data: dict) -> ChatResponse:
        raise NotImplementedError

    def list_models_path(self) -> str:
        raise NotImplementedError

    def parse_models(self, data: dict) -> list[str]:
        raise NotImplementedError

    # -- 网络部分 ---------------------------------------------------------

    def complete(self, messages: list[ChatMessage],
                 tools: list[dict] | None = None) -> ChatResponse:
        self.config.validate()
        url, headers, payload = self.build_request(messages, tools)
        data = post_json(url, payload, headers, self.config.timeout)
        return self.parse_response(data)

    def test_connection(self) -> dict:
        """连接测试：先拉模型列表，**失败则退化为一次最小补全**。

        为什么必须退化（实测教训）：用户的 Key 在 DeepSeek 的 ``/v1/models`` 上
        返回 ``401 Authentication Fails (governor)``，但 ``/chat/completions``
        完全可用。第一版在这里直接返回失败，于是"连接测试"报鉴权失败、而实际
        一切正常 —— 把可用的配置误判成坏的。
        """
        try:
            self.config.validate()
        except ModelError as exc:
            return {"ok": False, "error": str(exc)}

        models_error = ""
        url = f"{self.config.base_url}{self.list_models_path()}"
        try:
            data = get_json(url, self.auth_headers(), self.config.timeout)
            models = self.parse_models(data)
        except ModelError as exc:
            models_error = str(exc)
        else:
            return {"ok": True, "models": models, "count": len(models),
                    "via": "models"}

        # 退化路径：发一次最小补全。能回话就说明 Key 与端点都是好的。
        try:
            response = self.complete([ChatMessage(role="user", content="ping")])
        except ModelError as exc:
            return {"ok": False, "error": models_error, "chat_error": str(exc)}
        return {"ok": True, "via": "chat", "models_error": models_error,
                "model": response.model or self.config.model,
                "reply_chars": len(response.text)}

    def auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        headers.update(self.config.extra_headers)
        return headers


class OpenAICompatibleAdapter(ModelAdapter):
    """OpenAI `/chat/completions` 协议（DeepSeek / 火山方舟 / 自定义端点同用）。"""

    kind = "openai"

    def build_request(self, messages, tools=None):
        url = f"{self.config.base_url}/chat/completions"
        headers = self.auth_headers()
        headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return url, headers, payload

    def parse_response(self, data: dict) -> ChatResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError(f"响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:200]}")
        choice = choices[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            arguments_text = function.get("arguments") or ""
            arguments: dict[str, Any] = {}
            if arguments_text:
                try:
                    parsed = json.loads(arguments_text)
                    if isinstance(parsed, dict):
                        arguments = parsed
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(ToolCall(id=str(raw.get("id") or f"call_{index}"),
                                  name=str(function.get("name") or ""),
                                  arguments=arguments,
                                  raw_arguments=arguments_text))
        usage = data.get("usage") or {}
        return ChatResponse(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            usage=Usage(int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0)),
            model=str(data.get("model") or self.config.model),
            finish_reason=str(choice.get("finish_reason") or ""),
            raw=data,
        )

    def list_models_path(self) -> str:
        return "/models"

    def parse_models(self, data: dict) -> list[str]:
        items = data.get("data")
        if not isinstance(items, list):
            raise ModelError("响应缺少 data 列表（不是 OpenAI 兼容端点？）")
        return sorted(str(item.get("id")) for item in items if item.get("id"))


class AnthropicAdapter(ModelAdapter):
    """Anthropic `/messages` 协议。"""

    kind = "anthropic"

    def build_request(self, messages, tools=None):
        url = f"{self.config.base_url}/messages"
        headers = self.auth_headers()
        headers["x-api-key"] = self.config.api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION

        # Anthropic 不接受 system 角色：必须提到顶层 system 字段
        system_parts = [m.content for m in messages if m.role == "system"]
        conversation = [m.to_anthropic() for m in messages if m.role != "system"]
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": conversation,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [{"name": spec["function"]["name"],
                                 "description": spec["function"].get("description", ""),
                                 "input_schema": spec["function"].get("parameters", {})}
                                for spec in tools]
        return url, headers, payload

    def parse_response(self, data: dict) -> ChatResponse:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ModelError(f"响应缺少 content 块: {json.dumps(data, ensure_ascii=False)[:200]}")
        texts: list[str] = []
        calls: list[ToolCall] = []
        for index, block in enumerate(blocks):
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text") or ""))
            elif kind == "tool_use":
                arguments = block.get("input")
                calls.append(ToolCall(id=str(block.get("id") or f"toolu_{index}"),
                                      name=str(block.get("name") or ""),
                                      arguments=arguments if isinstance(arguments, dict) else {}))
        usage = data.get("usage") or {}
        return ChatResponse(
            text="".join(texts),
            tool_calls=calls,
            usage=Usage(int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0)),
            model=str(data.get("model") or self.config.model),
            finish_reason=str(data.get("stop_reason") or ""),
            raw=data,
        )

    def list_models_path(self) -> str:
        return "/models"

    def parse_models(self, data: dict) -> list[str]:
        items = data.get("data")
        if not isinstance(items, list):
            raise ModelError("响应缺少 data 列表")
        return sorted(str(item.get("id")) for item in items if item.get("id"))


class OllamaAdapter(ModelAdapter):
    """Ollama 本地协议（`/api/chat`、`/api/tags`）。"""

    kind = "ollama"

    def build_request(self, messages, tools=None):
        url = f"{self.config.base_url}/api/chat"
        headers = self.auth_headers()
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_ollama() for m in messages],
            "stream": False,
            "options": {"temperature": self.config.temperature,
                        "num_predict": self.config.max_tokens},
        }
        if tools:
            payload["tools"] = tools
        return url, headers, payload

    def parse_response(self, data: dict) -> ChatResponse:
        message = data.get("message")
        if not isinstance(message, dict):
            raise ModelError(f"响应缺少 message: {json.dumps(data, ensure_ascii=False)[:200]}")
        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            calls.append(ToolCall(id=str(raw.get("id") or f"call_{index}"),
                                  name=str(function.get("name") or ""),
                                  arguments=arguments if isinstance(arguments, dict) else {}))
        return ChatResponse(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            usage=Usage(int(data.get("prompt_eval_count") or 0),
                        int(data.get("eval_count") or 0)),
            model=str(data.get("model") or self.config.model),
            finish_reason=str(data.get("done_reason") or ""),
            raw=data,
        )

    def list_models_path(self) -> str:
        return "/api/tags"

    def parse_models(self, data: dict) -> list[str]:
        items = data.get("models")
        if not isinstance(items, list):
            raise ModelError("响应缺少 models 列表（不是 Ollama？）")
        return sorted(str(item.get("name")) for item in items if item.get("name"))


#: provider 的 adapter 类别 -> 实现
ADAPTERS: dict[str, type[ModelAdapter]] = {
    "openai": OpenAICompatibleAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
}


def adapter_for(config: ModelConfig) -> ModelAdapter:
    """按配置取适配器。"""
    kind = config.adapter_kind
    if kind not in ADAPTERS:
        raise ModelError(f"未知适配器: {kind!r}（可用: {sorted(ADAPTERS)}）")
    return ADAPTERS[kind](config)


def complete(config: ModelConfig, messages: list[ChatMessage],
             tools: list[dict] | None = None) -> ChatResponse:
    """便捷入口。"""
    return adapter_for(config).complete(messages, tools)


# ---------------------------------------------------------------------------
# HTTP（标准库，便于离线测试与零依赖分发）
# ---------------------------------------------------------------------------

def post_json(url: str, payload: dict, headers: dict[str, str],
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST JSON 并解析响应；非 2xx 或非法 JSON 抛 :class:`ModelError`。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={**headers, "Content-Type": "application/json; charset=utf-8"})
    return _read_json(request, timeout, method="POST")


def get_json(url: str, headers: dict[str, str],
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    """GET JSON。"""
    request = urllib.request.Request(url, method="GET", headers=headers)
    return _read_json(request, timeout, method="GET")


def _read_json(request: urllib.request.Request, timeout: float, *, method: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ModelError(f"{method} {request.full_url} 返回 {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ModelError(f"{method} {request.full_url} 网络错误: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ModelError(f"{method} {request.full_url} 超时（{timeout}s）") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError(f"响应不是合法 JSON: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise ModelError(f"响应不是 JSON 对象: {text[:200]}")
    return data
