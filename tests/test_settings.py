# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""``tcide/settings.py`` 的测试（纯离线，不需要 Qt / PawUI）。

设置是"看起来简单、坏起来难查"的典型：一个非法值会让模型请求立刻超时、一个
坏掉的 JSON 会让 IDE 打不开。所以这里把边界都钉住。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tcide.settings import (FONT_RANGE, PROVIDERS, TIMEOUT_RANGE, Settings,
                            config_dir, config_path, load, masked, normalize,
                            save, summary)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把设置目录指到临时目录（默认会写用户目录，测试不能碰真实配置）。"""
    monkeypatch.setenv("TCIDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


class TestDefaults:
    def test_defaults_are_sane(self):
        settings = Settings()
        assert settings.provider in PROVIDERS
        assert TIMEOUT_RANGE[0] <= settings.timeout <= TIMEOUT_RANGE[1]
        assert FONT_RANGE[0] <= settings.font_size <= FONT_RANGE[1]
        assert settings.review_gate is True, "审查门默认开（AI 改动需人工过目）"
        assert settings.telemetry is False, "默认不收集任何数据"

    def test_config_dir_follows_env(self, isolated):
        assert config_dir() == isolated
        assert config_path() == isolated / "settings.json"


class TestRoundTrip:
    def test_save_then_load(self, isolated):
        original = Settings(provider="anthropic", model="claude-x",
                            api_key="sk-abcdefghijk", timeout=90, font_size=15,
                            review_gate=False, compact=True)
        path = save(original)
        assert path.is_file()
        loaded = load()
        assert loaded.provider == "anthropic"
        assert loaded.model == "claude-x"
        assert loaded.api_key == "sk-abcdefghijk"
        assert loaded.timeout == 90
        assert loaded.font_size == 15
        assert loaded.review_gate is False
        assert loaded.compact is True

    def test_save_is_atomic_and_leaves_no_temp(self, isolated):
        save(Settings())
        leftovers = [item.name for item in isolated.iterdir()
                     if item.name.endswith(".tmp")]
        assert not leftovers, f"临时文件没清掉：{leftovers}"

    def test_unknown_keys_are_ignored(self, isolated):
        (isolated / "settings.json").write_text(
            json.dumps({"provider": "deepseek", "未来字段": 1}), encoding="utf-8")
        assert load().provider == "deepseek"


class TestNormalize:
    def test_timeout_is_clamped(self):
        assert normalize(Settings(timeout=0)).timeout == TIMEOUT_RANGE[0]
        assert normalize(Settings(timeout=99999)).timeout == TIMEOUT_RANGE[1]

    def test_font_size_is_clamped(self):
        assert normalize(Settings(font_size=2)).font_size == FONT_RANGE[0]
        assert normalize(Settings(font_size=99)).font_size == FONT_RANGE[1]

    def test_unknown_provider_falls_back(self):
        assert normalize(Settings(provider="不存在的服务商")).provider == "deepseek"
        # 有自定义 base_url 时归到 custom，而不是丢掉端点信息
        assert normalize(Settings(provider="nope",
                                  base_url="https://x/v1")).provider == "custom"

    def test_base_url_is_trimmed(self):
        assert normalize(Settings(base_url=" https://x/v1/ ")).base_url == "https://x/v1"


class TestCorruptFile:
    def test_broken_json_returns_defaults_and_keeps_the_bad_file(self, isolated):
        """坏配置不能让 IDE 起不来；但也不能悄悄删掉，要留证据。"""
        target = isolated / "settings.json"
        target.write_text("{ this is not json", encoding="utf-8")
        settings = load()
        assert settings.provider == "deepseek", "坏文件应退回默认值"
        assert not target.exists(), "坏文件应被移走"
        assert (isolated / "settings.json.bad").exists(), "坏文件应保留下来供排查"

    def test_wrong_type_returns_defaults(self, isolated):
        (isolated / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert load().provider == "deepseek"

    def test_bad_field_type_returns_defaults(self, isolated):
        (isolated / "settings.json").write_text(
            json.dumps({"timeout": "很久"}), encoding="utf-8")
        assert load().timeout == Settings().timeout


class TestMasking:
    def test_keys_are_masked(self):
        assert masked("") == "未配置"
        assert masked("short") == "*****"
        long_key = "sk-1234567890abcdef"
        shown = masked(long_key)
        assert long_key not in shown, "脱敏后不能出现完整 Key"
        assert shown.startswith("sk-1") and shown.endswith("cdef")

    def test_summary_never_leaks_the_key(self):
        text = summary(Settings(api_key="sk-1234567890abcdef"))
        assert "sk-1234567890abcdef" not in text
        assert "deepseek" in text
