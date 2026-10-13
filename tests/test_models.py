# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""models.py 的测试：用本地假 HTTP 服务断言三家的协议细节（完全离线）。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tcide.models import (
    ADAPTERS,
    PRESETS,
    AnthropicAdapter,
    ChatMessage,
    ChatResponse,
    ModelConfig,
    ModelError,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    ToolCall,
    adapter_for,
    complete,
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _run(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = {"__raw__": raw}
        self.server.records.append({
            "method": method, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "json": body,
        })
        route = self.server.routes.get((method, self.path))
        if route == "sleep":
            time.sleep(self.server.sleep_seconds)
            route = (200, {})
        if route is None:
            self._send(404, {"error": "no route"})
            return
        if route == "badjson":
            payload = b"this is not json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        status, data = route
        self._send(status, data)

    def _send(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        self._run("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._run("GET")

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def fake_server():
    """可编程的假服务端：routes[(method, path)] = (status, payload) | 'sleep' | 'badjson'。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.records = []
    server.routes = {}
    server.sleep_seconds = 2.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def openai_payload(text: str = "你好", tool_calls=None, usage=None) -> dict:
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "x", "model": "test-model",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


class TestPresets:
    def test_five_claimed_providers_exist(self):
        """README 宣称五家：DeepSeek / 火山方舟 / Ollama / Anthropic / 自定义。"""
        assert {"deepseek", "volcengine", "ollama", "anthropic", "custom"} <= set(PRESETS)

    def test_presets_map_to_known_adapters(self):
        for preset in PRESETS.values():
            assert preset.adapter in ADAPTERS

    def test_preset_fills_defaults(self):
        config = ModelConfig(provider="deepseek")
        assert config.base_url == "https://api.deepseek.com/v1"
        assert config.model == "deepseek-chat"
        assert config.adapter_kind == "openai"

    def test_trailing_slash_removed(self):
        assert ModelConfig(provider="custom",
                           base_url="http://x/v1/").base_url == "http://x/v1"

    def test_to_dict_hides_key(self):
        config = ModelConfig(provider="openai", api_key="secret-value")
        assert config.to_dict()["api_key"] == "***"
        assert config.to_dict(hide_key=False)["api_key"] == "secret-value"


class TestValidation:
    def test_missing_key_rejected(self):
        with pytest.raises(ModelError, match="API Key"):
            ModelConfig(provider="deepseek").validate()

    def test_ollama_needs_no_key(self):
        ModelConfig(provider="ollama").validate()

    def test_missing_model_rejected(self):
        config = ModelConfig(provider="custom", model="x", base_url="http://x")
        config.model = ""
        with pytest.raises(ModelError):
            config.validate()

    def test_unknown_provider_is_rejected_not_silently_openai(self):
        """回归：拼错服务商名以前会被悄悄当成 OpenAI 兼容端点，报错要明确。"""
        config = ModelConfig(provider="deepsek", api_key="k",
                             base_url="http://x/v1", model="m")
        with pytest.raises(ModelError, match="未知服务商"):
            config.validate()

    def test_unknown_provider_message_is_actionable(self):
        config = ModelConfig(provider="deepsek", api_key="k",
                             base_url="http://x/v1", model="m")
        with pytest.raises(ModelError) as info:
            config.validate()
        message = str(info.value)
        assert "custom" in message and "adapter" in message

    def test_explicit_adapter_overrides_provider(self):
        config = ModelConfig(provider="anything", model="claude", api_key="k",
                             base_url="http://x/v1", adapter="anthropic")
        assert config.adapter_kind == "anthropic"

    def test_unknown_adapter_is_rejected(self):
        config = ModelConfig(provider="custom", model="m", base_url="http://x",
                             adapter="nope")
        with pytest.raises(ModelError, match="未知适配器"):
            config.validate()

    def test_unknown_adapter(self):
        config = ModelConfig(provider="nope", model="m", base_url="http://x")
        with pytest.raises(ModelError, match="未知服务商"):
            adapter_for(config)


class TestMessageConversion:
    def test_openai_tool_result(self):
        message = ChatMessage.tool("call_1", "结果")
        data = message.to_openai()
        assert data["role"] == "tool" and data["tool_call_id"] == "call_1"

    def test_openai_assistant_with_tool_calls(self):
        message = ChatMessage.assistant("", [ToolCall("c1", "read_file", {"path": "a.py"})])
        data = message.to_openai()
        assert data["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(data["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}

    def test_anthropic_tool_result_is_user_block(self):
        data = ChatMessage.tool("toolu_1", "ok").to_anthropic()
        assert data["role"] == "user"
        assert data["content"][0]["type"] == "tool_result"
        assert data["content"][0]["tool_use_id"] == "toolu_1"

    def test_anthropic_assistant_tool_use_blocks(self):
        message = ChatMessage.assistant("想一下", [ToolCall("toolu_1", "git_status", {})])
        blocks = message.to_anthropic()["content"]
        assert [b["type"] for b in blocks] == ["text", "tool_use"]
        assert blocks[1]["input"] == {}

    def test_ollama_arguments_are_objects(self):
        message = ChatMessage.assistant("", [ToolCall("c1", "f", {"a": 1})])
        assert message.to_ollama()["tool_calls"][0]["function"]["arguments"] == {"a": 1}


class TestOpenAIProtocol:
    def test_build_request(self):
        config = ModelConfig(provider="deepseek", api_key="k", temperature=0.5,
                             max_tokens=123)
        url, headers, payload = OpenAICompatibleAdapter(config).build_request(
            [ChatMessage.user("hi")])
        assert url == "https://api.deepseek.com/v1/chat/completions"
        assert headers["Authorization"] == "Bearer k"
        assert payload["model"] == "deepseek-chat"
        assert payload["temperature"] == 0.5 and payload["max_tokens"] == 123
        assert payload["stream"] is False
        assert payload["messages"] == [{"role": "user", "content": "hi"}]

    def test_build_request_with_tools(self):
        config = ModelConfig(provider="openai", api_key="k")
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        _, _, payload = OpenAICompatibleAdapter(config).build_request(
            [ChatMessage.user("x")], tools)
        assert payload["tools"] == tools and payload["tool_choice"] == "auto"

    def test_parse_response_with_tool_calls(self):
        adapter = OpenAICompatibleAdapter(ModelConfig(provider="openai", api_key="k"))
        data = openai_payload("", tool_calls=[{
            "id": "call_7", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}])
        response = adapter.parse_response(data)
        assert response.wants_tools
        assert response.tool_calls[0].name == "read_file"
        assert response.tool_calls[0].arguments == {"path": "a.py"}
        assert response.usage.prompt_tokens == 10
        assert response.finish_reason == "tool_calls"

    def test_parse_response_bad_tool_arguments_keeps_raw(self):
        adapter = OpenAICompatibleAdapter(ModelConfig(provider="openai", api_key="k"))
        data = openai_payload("", tool_calls=[{
            "id": "c", "function": {"name": "f", "arguments": "{not json"}}])
        call = adapter.parse_response(data).tool_calls[0]
        assert call.arguments == {} and call.raw_arguments == "{not json"

    def test_parse_response_without_choices_raises(self):
        adapter = OpenAICompatibleAdapter(ModelConfig(provider="openai", api_key="k"))
        with pytest.raises(ModelError, match="choices"):
            adapter.parse_response({"error": "boom"})

    def test_complete_end_to_end(self, fake_server):
        fake_server.routes[("POST", "/v1/chat/completions")] = (200, openai_payload("答案"))
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url=f"{fake_server.base_url}/v1")
        response = complete(config, [ChatMessage.user("问题")])
        assert response.text == "答案"
        record = fake_server.records[-1]
        assert record["json"]["messages"][0]["content"] == "问题"
        assert record["headers"]["authorization"] == "Bearer k"
        assert record["headers"]["content-type"].startswith("application/json")

    def test_http_error_includes_body(self, fake_server):
        fake_server.routes[("POST", "/v1/chat/completions")] = (
            401, {"error": {"message": "invalid api key"}})
        config = ModelConfig(provider="custom", model="m", api_key="bad",
                             base_url=f"{fake_server.base_url}/v1")
        with pytest.raises(ModelError) as info:
            complete(config, [ChatMessage.user("x")])
        assert "401" in str(info.value) and "invalid api key" in str(info.value)

    def test_test_connection_lists_models(self, fake_server):
        fake_server.routes[("GET", "/v1/models")] = (
            200, {"data": [{"id": "b"}, {"id": "a"}]})
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url=f"{fake_server.base_url}/v1")
        result = OpenAICompatibleAdapter(config).test_connection()
        assert result["ok"] and result["models"] == ["a", "b"]

    def test_test_connection_reports_failure(self, fake_server):
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url=f"{fake_server.base_url}/v1")
        result = OpenAICompatibleAdapter(config).test_connection()
        assert result["ok"] is False and "404" in result["error"]

    def test_test_connection_falls_back_to_chat(self, fake_server):
        """``/models`` 被拒但对话可用时，必须**报成功**。

        这是实测踩到的真事：用户的 Key 在 DeepSeek 的 ``/v1/models`` 上返回
        ``401 Authentication Fails (governor)``，而 ``/chat/completions``
        完全可用。第一版在这里直接返回失败，于是"连接测试"把可用的配置
        误判成坏的。
        """
        fake_server.routes[("GET", "/v1/models")] = (401, {"error": "nope"})
        fake_server.routes[("POST", "/v1/chat/completions")] = (
            200, {"choices": [{"message": {"content": "pong"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url=f"{fake_server.base_url}/v1")
        result = OpenAICompatibleAdapter(config).test_connection()
        assert result["ok"] is True, result
        assert result["via"] == "chat"
        assert "401" in result["models_error"]
        paths = [record["path"] for record in fake_server.records]
        assert "/v1/chat/completions" in paths, "没有真的走退化补全"


class TestAnthropicProtocol:
    def test_system_is_hoisted(self):
        config = ModelConfig(provider="anthropic", api_key="k")
        url, headers, payload = AnthropicAdapter(config).build_request([
            ChatMessage.system("规则"), ChatMessage.user("问题")])
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "k"
        assert headers["anthropic-version"] == "2023-06-01"
        assert payload["system"] == "规则"
        assert all(m["role"] != "system" for m in payload["messages"])
        assert payload["max_tokens"] > 0, "Anthropic 必须带 max_tokens"

    def test_tools_use_input_schema(self):
        config = ModelConfig(provider="anthropic", api_key="k")
        tools = [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}}}]
        _, _, payload = AnthropicAdapter(config).build_request(
            [ChatMessage.user("x")], tools)
        assert payload["tools"][0]["input_schema"] == {"type": "object"}
        assert "function" not in payload["tools"][0]

    def test_parse_content_blocks(self):
        adapter = AnthropicAdapter(ModelConfig(provider="anthropic", api_key="k"))
        data = {"model": "claude", "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "看一下"},
                            {"type": "tool_use", "id": "toolu_1",
                             "name": "git_status", "input": {}}],
                "usage": {"input_tokens": 7, "output_tokens": 3}}
        response = adapter.parse_response(data)
        assert response.text == "看一下"
        assert response.tool_calls[0].id == "toolu_1"
        assert response.tool_calls[0].arguments == {}
        assert response.usage.total_tokens == 10

    def test_parse_without_content_raises(self):
        adapter = AnthropicAdapter(ModelConfig(provider="anthropic", api_key="k"))
        with pytest.raises(ModelError, match="content"):
            adapter.parse_response({"id": "x"})

    def test_complete_end_to_end(self, fake_server):
        fake_server.routes[("POST", "/v1/messages")] = (
            200, {"model": "claude", "content": [{"type": "text", "text": "好"}],
                  "usage": {"input_tokens": 1, "output_tokens": 1}})
        config = ModelConfig(provider="custom", model="claude", api_key="k",
                             base_url=f"{fake_server.base_url}/v1")
        config.provider = "anthropic"
        response = AnthropicAdapter(config).complete([ChatMessage.user("hi")])
        assert response.text == "好"


class TestOllamaProtocol:
    def test_build_request_uses_options(self):
        config = ModelConfig(provider="ollama", temperature=0.7, max_tokens=99)
        url, _headers, payload = OllamaAdapter(config).build_request(
            [ChatMessage.user("hi")])
        assert url == "http://localhost:11434/api/chat"
        assert payload["stream"] is False
        assert payload["options"] == {"temperature": 0.7, "num_predict": 99}

    def test_parse_response(self):
        adapter = OllamaAdapter(ModelConfig(provider="ollama"))
        data = {"model": "llama3", "done_reason": "stop",
                "message": {"role": "assistant", "content": "本地回答"},
                "prompt_eval_count": 12, "eval_count": 8}
        response = adapter.parse_response(data)
        assert response.text == "本地回答"
        assert response.usage.total_tokens == 20
        assert response.finish_reason == "stop"

    def test_parse_tool_calls(self):
        adapter = OllamaAdapter(ModelConfig(provider="ollama"))
        data = {"message": {"content": "", "tool_calls": [
            {"function": {"name": "list_files", "arguments": {"depth": 1}}}]}}
        call = adapter.parse_response(data).tool_calls[0]
        assert call.name == "list_files" and call.arguments == {"depth": 1}

    def test_list_models_from_tags(self, fake_server):
        fake_server.routes[("GET", "/api/tags")] = (
            200, {"models": [{"name": "llama3:latest"}, {"name": "qwen2:7b"}]})
        config = ModelConfig(provider="custom", model="m",
                             base_url=fake_server.base_url)
        config.provider = "ollama"
        result = OllamaAdapter(config).test_connection()
        assert result["ok"] and result["models"] == ["llama3:latest", "qwen2:7b"]

    def test_tags_without_models_raises(self):
        adapter = OllamaAdapter(ModelConfig(provider="ollama"))
        with pytest.raises(ModelError, match="models"):
            adapter.parse_models({"ok": True})


class TestTransportErrors:
    def test_timeout(self, fake_server):
        fake_server.routes[("POST", "/v1/chat/completions")] = "sleep"
        fake_server.sleep_seconds = 3.0
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url=f"{fake_server.base_url}/v1", timeout=0.3)
        with pytest.raises(ModelError, match="超时|timed out|网络错误"):
            complete(config, [ChatMessage.user("x")])

    def test_invalid_json(self, fake_server):
        fake_server.routes[("POST", "/v1/chat/completions")] = "badjson"
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url=f"{fake_server.base_url}/v1")
        with pytest.raises(ModelError, match="JSON"):
            complete(config, [ChatMessage.user("x")])

    def test_connection_refused(self):
        config = ModelConfig(provider="custom", model="m", api_key="k",
                             base_url="http://127.0.0.1:1/v1", timeout=2)
        with pytest.raises(ModelError, match="网络错误"):
            complete(config, [ChatMessage.user("x")])

    def test_response_object_roundtrip(self):
        response = ChatResponse(text="a", tool_calls=[ToolCall("1", "f", {"x": 1})],
                                model="m", finish_reason="stop")
        data = response.to_dict()
        assert data["tool_calls"][0]["arguments"] == {"x": 1}
        assert data["finish_reason"] == "stop"
