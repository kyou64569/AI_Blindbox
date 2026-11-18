"""连接池（connections）解析测试。

覆盖：conn_creds 命中/回退、卡片级与组级 connection 覆盖、
悬空引用降级、老配置（无 connections 字段）行为不变。
"""
from __future__ import annotations

from app.agents import _resolve_models
from app.config import build_connection_map, conn_creds

CONNS = [
    {"id": "whale", "name": "鲸鱼云", "base_url": "https://api.whale.ai/v1", "api_key": "sk-whale"},
    {"id": "step", "name": "阶跃", "base_url": "https://api.stepfun.com/v1", "api_key": "sk-step"},
]


def _section(**kw):
    base = {"models": []}
    base.update(kw)
    return base


# ---------- conn_creds ----------

def test_conn_creds_hit():
    m = build_connection_map(CONNS)
    url, key = conn_creds(m, "whale", "https://own/v1", "sk-own")
    assert url == "https://api.whale.ai/v1"
    assert key == "sk-whale"


def test_conn_creds_miss_falls_back_to_own():
    m = build_connection_map(CONNS)
    url, key = conn_creds(m, "nope", "https://own/v1", "sk-own")
    assert url == "https://own/v1"
    assert key == "sk-own"


def test_conn_creds_empty_ref_keeps_own():
    m = build_connection_map(CONNS)
    assert conn_creds(m, "", "https://own/v1", "sk-own") == ("https://own/v1", "sk-own")
    assert conn_creds(m, None, "u", "k") == ("u", "k")


def test_build_connection_map_skips_dirty_rows():
    m = build_connection_map([{"base_url": "x"}, "bad", None, {"id": ""}, {"id": "ok"}])
    assert set(m) == {"ok"}


# ---------- _resolve_models ----------

def test_card_connection_overrides_card_fields():
    sec = _section(models=[{
        "model": "gpt-4o-mini",
        "connection": "whale",
        "base_url": "https://stale.example/v1",
        "api_key": "sk-stale",
    }])
    out = _resolve_models(sec, {}, CONNS)
    assert len(out) == 1
    assert out[0]["base_url"] == "https://api.whale.ai/v1"
    assert out[0]["api_key"] == "sk-whale"


def test_card_connection_used_when_card_fields_empty():
    sec = _section(models=[{"model": "glm-5.2", "connection": "step"}])
    out = _resolve_models(sec, {}, CONNS)
    assert out[0]["base_url"] == "https://api.stepfun.com/v1"
    assert out[0]["api_key"] == "sk-step"


def test_card_without_connection_keeps_legacy_behaviour():
    sec = _section(models=[{"model": "m", "base_url": "https://own/v1", "api_key": "sk-own"}])
    out = _resolve_models(sec, {}, CONNS)
    assert out[0]["base_url"] == "https://own/v1"
    assert out[0]["api_key"] == "sk-own"


def test_dangling_card_connection_falls_back():
    sec = _section(models=[{"model": "m", "connection": "gone", "base_url": "https://own/v1", "api_key": "k"}])
    out = _resolve_models(sec, {}, CONNS)
    assert out[0]["base_url"] == "https://own/v1"
    assert out[0]["api_key"] == "k"


def test_section_level_connection_applies_to_cards():
    sec = _section(connection="step", models=[{"model": "m"}])
    out = _resolve_models(sec, {}, CONNS)
    assert out[0]["base_url"] == "https://api.stepfun.com/v1"
    assert out[0]["api_key"] == "sk-step"


def test_card_connection_beats_section_connection():
    sec = _section(connection="step", models=[{"model": "m", "connection": "whale"}])
    out = _resolve_models(sec, {}, CONNS)
    assert out[0]["base_url"] == "https://api.whale.ai/v1"


def test_no_connections_list_keeps_old_path():
    sec = _section(models=[{"model": "m", "base_url": "https://own/v1", "api_key": "k"}])
    out = _resolve_models(sec, {}, [])
    assert out[0]["base_url"] == "https://own/v1"
    assert out[0]["api_key"] == "k"


def test_global_fallback_still_works_without_connection():
    sec = _section(models=[{"model": "m"}])
    out = _resolve_models(sec, {"base_url": "https://g/v1", "api_key": "sk-g"}, CONNS)
    assert out[0]["base_url"] == "https://g/v1"
    assert out[0]["api_key"] == "sk-g"


# ---------- 全局默认键：default_* 优先，裸键兼容 ----------
# 前端「全局默认模型」槽位写的是 global.default_*，旧实现读裸键导致这些设置
# 对普通卡片静默失效（只有终极兜底卡片生效）。

def test_global_default_star_keys_apply_to_cards():
    sec = _section(models=[{"model": "m"}])
    out = _resolve_models(
        sec,
        {
            "default_base_url": "https://g/v1",
            "default_api_key": "sk-g",
            "default_max_tokens": 2000,
            "default_timeout": 45,
        },
        [],
    )
    assert out[0]["base_url"] == "https://g/v1"
    assert out[0]["api_key"] == "sk-g"
    assert out[0]["max_tokens"] == 2000
    assert out[0]["timeout"] == 45.0


def test_global_bare_keys_still_work():
    """手改 yaml 写裸键的老配置继续生效（向后兼容）。"""
    sec = _section(models=[{"model": "m"}])
    out = _resolve_models(sec, {"base_url": "https://g/v1", "max_tokens": 1234}, [])
    assert out[0]["base_url"] == "https://g/v1"
    assert out[0]["max_tokens"] == 1234


def test_card_value_beats_global_default():
    sec = _section(models=[{"model": "m", "max_tokens": 999, "timeout": 7}])
    out = _resolve_models(sec, {"default_max_tokens": 2000, "default_timeout": 45}, [])
    assert out[0]["max_tokens"] == 999
    assert out[0]["timeout"] == 7.0


def test_group_level_beats_global_default():
    sec = _section(max_tokens=555, timeout=9, models=[{"model": "m"}])
    out = _resolve_models(sec, {"default_max_tokens": 2000, "default_timeout": 45}, [])
    assert out[0]["max_tokens"] == 555
    assert out[0]["timeout"] == 9.0


# ---------- TTS 音色凭据解析 ----------
# 凭据可能挂在组级 connection，也可能只在后面某张卡片上；只查第一张会漏。

def test_tts_creds_group_fields_win():
    from app.main import _tts_creds

    prov = {"base_url": "https://own/v1", "api_key": "sk-own", "connection": "whale"}
    assert _tts_creds(prov) == ("https://own/v1", "sk-own")


def test_tts_creds_uses_group_connection(monkeypatch):
    from app import main as m

    class _S:
        connections = CONNS
        providers = {"tts": {}}

    monkeypatch.setattr(m.config, "get_settings", lambda: _S())
    prov = {"connection": "step"}
    assert m._tts_creds(prov) == ("https://api.stepfun.com/v1", "sk-step")


def test_tts_creds_falls_through_to_later_card(monkeypatch):
    from app import main as m

    class _S:
        connections = CONNS
        providers = {"tts": {}}

    monkeypatch.setattr(m.config, "get_settings", lambda: _S())
    # 第一张是悬空引用，第二张才有效 —— 旧实现只看 cards[0]，这里会取不到
    prov = {"models": [{"connection": "gone"}, {"connection": "whale"}]}
    assert m._tts_creds(prov) == ("https://api.whale.ai/v1", "sk-whale")


def test_tts_creds_no_connection_returns_empty(monkeypatch):
    from app import main as m

    class _S:
        connections = []
        providers = {"tts": {}}

    monkeypatch.setattr(m.config, "get_settings", lambda: _S())
    assert m._tts_creds({"models": [{"model": "x"}]}) == ("", "")
