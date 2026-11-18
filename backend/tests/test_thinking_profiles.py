"""思考参数厂商方言注册表回归（对齐《多厂商 LLM 思考参数统一适配指南》）。

覆盖：7 种 OpenAI 兼容方言的形状/位置/类型、未设置守卫、binary/none 能力、
未知方言报错、存量配置零变化、预算抬升与用户优先、配置入口白名单、
Gemini 原生 default 落 medium。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import main
from app.agents import _max_tokens_kwargs, _resolve_models, _thinking_kwargs
from app.gemini import _build_thinking_config
from app.thinking_profiles import (
    THINKING_PROFILE_DEFAULT,
    THINKING_PROFILE_KEYS,
    apply_thinking_profile,
    resolve_thinking_spec,
    thinking_kwargs,
)

# (方言, off 请求体, high 请求体)
_DIALECTS = [
    ("openai-native",
     {"enable_thinking": False},
     {"enable_thinking": True, "reasoning_effort": "high"}),
    ("chat-template",
     {"chat_template_kwargs": {"enable_thinking": False}},
     {"chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "high"}),
    ("thinking-flag",
     {"thinking": "disabled"},
     {"thinking": "enabled", "reasoning_effort": "high"}),
    ("thinking-type",
     {"thinking": {"type": "disabled"}},
     {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("effort-only",
     {"reasoning_effort": "none"},
     {"reasoning_effort": "high"}),
    ("thinking-mode",
     {"thinking_mode": False},
     {"thinking_mode": True}),
    ("nvidia",
     {"thinking": False},
     {"thinking": True}),
]


def _body(profile: str, level: str, capability: str = "graded") -> dict:
    body: dict = {}
    apply_thinking_profile(profile, body, resolve_thinking_spec(level, capability))
    return body


def test_registry_covers_all_seven_dialects():
    assert THINKING_PROFILE_KEYS == {row[0] for row in _DIALECTS}
    assert "nvidia" in THINKING_PROFILE_KEYS
    assert THINKING_PROFILE_DEFAULT == "effort-only"  # 存量兼容，勿改成 openai-native


@pytest.mark.parametrize("profile,off,high", _DIALECTS)
def test_dialect_off_and_high(profile, off, high):
    """指南 §3：关闭/开启的字段位置、形状、类型必须逐字正确。"""
    assert _body(profile, "off") == off
    assert _body(profile, "high") == high


@pytest.mark.parametrize("profile,off,high", _DIALECTS)
def test_dialect_default_writes_nothing(profile, off, high):
    """指南 §7.6：未设置一个思考字段都不发（存量配置逐字节不变）。"""
    assert _body(profile, "default") == {}


def test_thinking_field_has_three_distinct_shapes():
    """同名字段三种形状：字符串 / 对象 / 布尔，接错即静默失效或 400。"""
    assert _body("thinking-flag", "high")["thinking"] == "enabled"
    assert _body("thinking-type", "high")["thinking"] == {"type": "enabled"}
    assert _body("nvidia", "high")["thinking"] is True
    assert _body("nvidia", "off")["thinking"] is False


def test_binary_dialects_never_send_effort():
    """指南 §3.6/§3.7：thinking-mode 与 nvidia 没有强度档。"""
    for profile in ("thinking-mode", "nvidia"):
        for level in ("low", "medium", "high"):
            body = _body(profile, level, "binary")
            assert "reasoning_effort" not in body
            assert body[list(body)[0]] is True


def test_binary_off_still_writes_disable_field():
    """binary 显式 off 的 spec 是 {level:None, disable:True}，守卫必须放行。"""
    spec = resolve_thinking_spec("off", "binary")
    assert spec["level"] is None and spec["disable_thinking"] is True
    assert _body("nvidia", "off", "binary") == {"thinking": False}
    assert _body("thinking-mode", "off", "binary") == {"thinking_mode": False}


def test_capability_normalization():
    assert resolve_thinking_spec("low", "binary")["level"] == "high"
    assert resolve_thinking_spec("low", "binary")["budget"] == 24576
    assert resolve_thinking_spec("high", "none")["level"] is None
    assert _body("nvidia", "high", "none") == {}
    assert resolve_thinking_spec("medium")["effort"] == "medium"


def test_unknown_profile_raises_instead_of_fallback():
    with pytest.raises(ValueError, match="未知的思考方言"):
        apply_thinking_profile("bogus", {}, resolve_thinking_spec("off"))


def test_profile_returns_occupied_keys():
    assert apply_thinking_profile("nvidia", {}, resolve_thinking_spec("default")) == set()
    assert apply_thinking_profile("nvidia", {}, resolve_thinking_spec("high")) == {"thinking"}
    assert apply_thinking_profile("chat-template", {}, resolve_thinking_spec("high")) == {
        "chat_template_kwargs", "reasoning_effort",
    }


def test_chat_template_merges_existing_kwargs():
    """嵌套对象按子键合并，不能整块替换掉用户已有的 kwargs。"""
    body = {"chat_template_kwargs": {"foo": "bar"}}
    apply_thinking_profile("chat-template", body, resolve_thinking_spec("off"))
    assert body["chat_template_kwargs"] == {"foo": "bar", "enable_thinking": False}


# ---------------------------------------------------------------------------
# 接入层：agents.py / gemini.py / main.py
# ---------------------------------------------------------------------------

def _creds(card: dict, section: dict | None = None, glob: dict | None = None) -> dict:
    s = dict(section or {})
    s["models"] = [card]
    return _resolve_models(s, glob or {})[0]


def test_legacy_config_request_body_unchanged():
    """未配置方言时：off 完全不注入（与改造前逐字节一致），档位只发 reasoning_effort，
    且不下发任何 max_tokens。"""
    off = _creds({"model": "m", "thinking": "off"})
    assert _thinking_kwargs(off) == {}
    assert _max_tokens_kwargs(off) == {}
    high = _creds({"model": "m", "thinking": "high"})
    assert _thinking_kwargs(high) == {"reasoning_effort": "high"}
    assert _max_tokens_kwargs(high) == {}
    default = _creds({"model": "m", "thinking": "default"})
    assert _thinking_kwargs(default) == {} and _max_tokens_kwargs(default) == {}


def test_explicit_effort_only_off_sends_none():
    """显式选 effort-only 时才用 reasoning_effort:"none" 表达关闭思考。"""
    c = _creds({"model": "m", "thinking": "off", "thinking_profile": "effort-only"})
    assert _thinking_kwargs(c) == {"reasoning_effort": "none"}


def test_explicit_nvidia_profile_and_budget():
    c = _creds({"model": "m", "thinking": "high", "thinking_profile": "nvidia",
                "thinking_capability": "binary"})
    assert _thinking_kwargs(c) == {"thinking": True}
    assert _max_tokens_kwargs(c) == {"max_tokens": 24576}
    # 两个 ** 展开不得撞键（thinking_kwargs 永远不含 max_tokens）
    merged = {**_thinking_kwargs(c), **_max_tokens_kwargs(c)}
    assert merged == {"thinking": True, "max_tokens": 24576}


def test_user_max_tokens_always_wins():
    c = _creds({"model": "m", "thinking": "high", "thinking_profile": "nvidia",
                "max_tokens": 4096})
    assert _max_tokens_kwargs(c) == {"max_tokens": 4096}
    assert "max_tokens" not in _thinking_kwargs(c)


def test_profile_fallback_chain():
    assert _creds({"model": "m"})["thinking_profile"] is None
    assert _creds({"model": "m"}, {"thinking_profile": "chat-template"})["thinking_profile"] == "chat-template"
    assert _creds({"model": "m"}, glob={"default_thinking_profile": "thinking-type"})["thinking_profile"] == "thinking-type"
    assert _creds({"model": "m", "thinking_profile": "nvidia"},
                  {"thinking_profile": "chat-template"})["thinking_profile"] == "nvidia"
    assert _creds({"model": "m"})["thinking_capability"] == "graded"


def test_gemini_native_skips_openai_dialect():
    c = _creds({"model": "gemini-3.5-flash-lite", "thinking": "high",
                "base_url": "https://generativelanguage.googleapis.com/v1beta"})
    assert c["gemini_native"] is True
    assert thinking_kwargs(c) == {}


def test_gemini_default_falls_to_medium():
    """指南 §4：留空 thinkingConfig 时 flash-lite 完全不思考，default 必须落 medium。"""
    assert _build_thinking_config("default") == {"thinkingLevel": "medium", "includeThoughts": True}
    assert _build_thinking_config(None) == {"thinkingLevel": "medium", "includeThoughts": True}
    assert _build_thinking_config("off")["thinkingLevel"] == "minimal"


def _valid_cfg() -> dict:
    return {
        "global": {}, "mock": False, "agents": {}, "randomness": {}, "modes": [],
        "providers": {}, "paths": {},
    }


def test_config_validation_rejects_unknown_profile():
    cfg = _valid_cfg()
    cfg["agents"] = {"creative": {"models": [{"model": "m", "thinking_profile": "bogus"}]}}
    with pytest.raises(HTTPException) as e:
        main._validate_config_payload(cfg)
    assert e.value.status_code == 400 and "thinking_profile" in e.value.detail


def test_config_validation_accepts_valid_profiles():
    cfg = _valid_cfg()
    cfg["agents"] = {"creative": {
        "thinking_profile": "chat-template",
        "models": [{"model": "m", "thinking_profile": "nvidia", "thinking_capability": "binary"}],
    }}
    cfg["global"] = {"default_thinking_profile": "thinking-type"}
    assert main._validate_config_payload(cfg) is cfg


@pytest.mark.parametrize("key", ["default_thinking_profile", "thinking_profile"])
def test_config_validation_rejects_unknown_global_profile(key):
    """global 的两套键运行时都参与回退，入口必须都校验（裸键曾漏检）。"""
    cfg = _valid_cfg()
    cfg["global"] = {key: "bogus"}
    with pytest.raises(HTTPException) as e:
        main._validate_config_payload(cfg)
    assert e.value.status_code == 400 and key in e.value.detail


@pytest.mark.parametrize("key", ["default_thinking_capability", "thinking_capability"])
def test_config_validation_rejects_unknown_global_capability(key):
    cfg = _valid_cfg()
    cfg["global"] = {key: "bogus"}
    with pytest.raises(HTTPException) as e:
        main._validate_config_payload(cfg)
    assert e.value.status_code == 400 and key in e.value.detail


def test_config_validation_accepts_global_bare_keys():
    cfg = _valid_cfg()
    cfg["global"] = {"thinking_profile": "nvidia", "thinking_capability": "binary"}
    assert main._validate_config_payload(cfg) is cfg
