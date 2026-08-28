"""多模型配置改造的回退逻辑单元测试（全部 mock，不真实调用 API）。

运行：
  <解释器> -m pytest backend/tests/ -q

覆盖：
  T1/T2 旧单 model -> models 迁移（load_settings 规范化）
  T3    _resolve_models 空字段回退 + _call_with_fallback 顺序/异常触发/成功即停
  T4    _executor_creds 空回退链（仅取 image/video 置顶项 + global）
  T5    _run_tts 云端全失败 -> 自动回退 Edge
"""
import contextlib
import json
import os
import sys

# 把 backend 加入路径，确保 `from app import ...` 可导入（pipeline 内部用相对导入）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from app import pipeline, config
from app import executors  # _run_tts 拆分后位于 executors 模块，OpenAI 哨兵需打桩此处


@contextlib.contextmanager
def _patch_raw(agents=None, providers=None, global_=None):
    """临时替换内存单例 SETTINGS.raw，测试结束后还原，避免污染其他用例。"""
    raw = config.SETTINGS.raw
    saved = json.loads(json.dumps(raw))  # 深拷贝
    if global_ is not None:
        raw["global"] = global_
    if agents is not None:
        raw.setdefault("agents", {}).update(agents)
    if providers is not None:
        raw.setdefault("providers", {}).update(providers)
    try:
        yield
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 1：旧单 model -> models 迁移（load_settings 规范化）
# ---------------------------------------------------------------------------
def test_load_settings_migration(tmp_path):
    raw = {
        "paths": {"cache_dir": "x", "output_dir": "y", "data_dir": "z"},
        "global": {"default_model": "dm", "default_base_url": "db", "default_api_key": "dk"},
        "agents": {
            "creative": {"model": "m1", "base_url": "b", "api_key": "k", "temperature": 0.9},
        },
        "providers": {
            "tts": {"model": "tm", "base_url": "tb", "api_key": "tk", "voice": "v"},
        },
    }
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    s = config.load_settings(p)
    assert s.agents["creative"]["models"][0]["model"] == "m1"
    assert s.providers["tts"]["models"][0]["model"] == "tm"
    # 幂等：已有非空 models 不再包
    sec = {"model": "x", "models": [{"model": "y"}]}
    config._normalize_section(sec)
    assert sec["models"] == [{"model": "y"}]
    # 空 model -> models=[]
    sec2 = {"model": "", "base_url": "", "api_key": ""}
    config._normalize_section(sec2)
    assert sec2["models"] == []


# ---------------------------------------------------------------------------
# 用例 2：_resolve_models 空字段回退（组级 / global）
# ---------------------------------------------------------------------------
def test_resolve_models_fallback():
    g = {"base_url": "GB", "api_key": "GK"}
    # 卡片空 -> 组级（pipeline._normalize_base_url 只补 /v1，不补协议头）
    sec = {"base_url": "B", "api_key": "K", "models": [{"model": "m"}]}
    out = pipeline._resolve_models(sec, g)
    assert out[0]["base_url"] == "B/v1" and out[0]["api_key"] == "K"
    # 卡片显式空 -> 仍回退组级
    sec2 = {"base_url": "B", "api_key": "K", "models": [{"model": "m2", "base_url": "", "api_key": ""}]}
    out2 = pipeline._resolve_models(sec2, g)
    assert out2[0]["base_url"] == "B/v1" and out2[0]["api_key"] == "K"
    # 组级也无 -> 回退 global
    sec3 = {"models": [{"model": "mx", "base_url": "", "api_key": ""}]}
    out3 = pipeline._resolve_models(sec3, g)
    assert out3[0]["base_url"] == "GB/v1" and out3[0]["api_key"] == "GK"
    # model 为空 -> 跳过该卡片
    sec4 = {"base_url": "B", "api_key": "K", "models": [{"model": ""}]}
    assert pipeline._resolve_models(sec4, g) == []


# ---------------------------------------------------------------------------
# 用例 3：_call_with_fallback 顺序 + 仅异常触发 + 成功即停 + 全失败抛 FallbackExhausted
# ---------------------------------------------------------------------------
def test_call_with_fallback():
    models = [{"model": "a"}, {"model": "b"}, {"model": "c"}]

    # 第一次成功 -> 仅调用 1 次，返回 (结果, 实际使用的模型名)
    calls = []
    res, used = pipeline._call_with_fallback(
        [{"model": "a"}],
        lambda c: (calls.append(c["model"]) or f"ok:{c['model']}"),
    )
    assert res == "ok:a" and used == "a" and calls == ["a"]

    # 第 N 次成功 -> 调用到 N+1 个，used 为最终成功模型名
    calls = []
    def call_fn(c):
        calls.append(c["model"])
        if len(calls) < 2:
            raise RuntimeError("fail")
        return f"ok:{c['model']}"
    res, used = pipeline._call_with_fallback(models, call_fn)
    assert res == "ok:b" and used == "b" and calls == ["a", "b"]

    # 全部失败 -> 抛 FallbackExhausted
    calls = []
    def always_fail(c):
        calls.append(c["model"])
        raise RuntimeError("x")
    try:
        pipeline._call_with_fallback(models, always_fail)
        assert False, "should raise FallbackExhausted"
    except pipeline.FallbackExhausted:
        pass
    assert calls == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 用例 4：_executor_creds 空回退链（仅取 image/video 置顶项 + global）
# ---------------------------------------------------------------------------
def test_executor_creds_empty_chain():
    g = {"default_model": "gdm", "default_base_url": "gdb", "default_api_key": "gdk"}
    exec_cfg = {"model": "", "base_url": "", "api_key": "", "temperature": 0.8, "models": []}
    empty_agent = {"model": "", "base_url": "", "api_key": "", "models": []}
    prov = {
        "image": {"base_url": "ib", "api_key": "ik", "model": "im",
                  "models": [{"model": "im", "base_url": "ib", "api_key": "ik", "temperature": None}]},
        "video": {"base_url": "vb", "api_key": "vk", "model": "vm",
                  "models": [{"model": "vm", "base_url": "vb", "api_key": "vk", "temperature": None}]},
    }
    # text 类型：executor/creative/planner 全空 -> global 兜底
    with _patch_raw(agents={"executor": exec_cfg, "creative": empty_agent, "planner": empty_agent},
                    providers=prov, global_=g):
        chain = pipeline._executor_creds("text")
    assert [c["model"] for c in chain] == ["gdm"]
    assert chain[0]["base_url"] == "gdb/v1"
    # image 类型：优先 providers.image 置顶；image 空时回退 video 置顶；全空时 global 兜底
    with _patch_raw(agents={"executor": exec_cfg, "creative": empty_agent, "planner": empty_agent},
                    providers=prov, global_=g):
        chain_img = pipeline._executor_creds("image")
    assert [c["model"] for c in chain_img] == ["im"]
    assert chain_img[0]["base_url"] == "ib/v1"
    prov_no_image = {"image": {"model": "", "base_url": "", "api_key": "", "models": []},
                     "video": {"base_url": "vb", "api_key": "vk", "model": "vm",
                               "models": [{"model": "vm", "base_url": "vb", "api_key": "vk", "temperature": None}]}}
    with _patch_raw(agents={"executor": exec_cfg, "creative": empty_agent, "planner": empty_agent},
                    providers=prov_no_image, global_=g):
        chain_img2 = pipeline._executor_creds("image")
    # image 空 -> 回退链：video 置顶 + global 兜底
    assert [c["model"] for c in chain_img2] == ["vm", "gdm"]


# ---------------------------------------------------------------------------
# 用例 5：_run_tts 云端全失败 -> 回退 Edge（mock 云端与 Edge）
# ---------------------------------------------------------------------------
def test_run_tts_cloud_fail_fallback_edge(monkeypatch):
    raw = config.SETTINGS.raw
    saved = json.loads(json.dumps(raw))
    raw["providers"]["tts"] = {
        "voice": "v",
        "base_url": "tb",
        "api_key": "tk",
        "model": "tm",
        "models": [{"model": "tm", "base_url": "tb", "api_key": "tk", "temperature": None}],
    }
    try:
        # 1) 云端 OpenAI 调用必失败
        class _FakeAudio:
            class speech:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("cloud down")
        class _FakeClient:
            audio = _FakeAudio
        monkeypatch.setattr(executors, "OpenAI", lambda *a, **k: _FakeClient())
        # 2) 省略 voice 重试（直接 urllib.request.urlopen 发请求）也失败
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("omit fail")))
        # 3) Edge TTS 落一份假 mp3（注入伪 edge_tts 模块，避免真实依赖/联网）
        import sys, types
        _fake_edge = types.ModuleType("edge_tts")
        class _FakeComm:
            def __init__(self, text, voice):
                self.text, self.voice = text, voice
            async def save(self, path):
                with open(path, "wb") as f:
                    f.write(b"FAKEMP3")
        _fake_edge.Communicate = _FakeComm
        sys.modules["edge_tts"] = _fake_edge

        path, info = pipeline._run_tts("你好世界，这是播客台词。", {})
        assert path.endswith(".mp3"), f"返回路径异常: {path}"
        assert "Edge" in info, f"应回退 Edge，实际 info={info}"
        with open(path, "rb") as f:
            assert f.read() == b"FAKEMP3"
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 6：_run_tts 在 mock 模式下离线短路（QA 回归：此前 mock 仍触发真实云端 TTS）
# ---------------------------------------------------------------------------
def test_run_tts_mock_shortcut(monkeypatch, tmp_path):
    raw = config.SETTINGS.raw
    saved = json.loads(json.dumps(raw))
    raw["mock"] = True
    raw.setdefault("providers", {})["tts"] = {
        "voice": "v", "base_url": "tb", "api_key": "tk",
        "model": "tm", "models": [{"model": "tm", "base_url": "tb", "api_key": "tk", "temperature": None}],
    }
    # 即便云端凭据齐全，mock 也应短路：重定向产物目录到临时目录
    monkeypatch.setattr(config.SETTINGS, "output_dir", tmp_path)
    calls = {"n": 0}

    class _FakeClient:
        # 任何对 OpenAI 客户端的访问都视为失败（证明 mock 未触发真实调用）
        def __getattr__(self, name):
            calls["n"] += 1
            raise AssertionError("mock 模式下不应调用 OpenAI")

    monkeypatch.setattr(executors, "OpenAI", lambda *a, **k: _FakeClient())
    try:
        path, info = pipeline._run_tts("你好世界，这是播客台词。", {})
        assert path.endswith(".wav"), f"mock 应返回本地占位音频路径，实际: {path}"
        assert "Mock" in info, f"info 应标注 Mock，实际: {info}"
        assert os.path.exists(path), f"占位音频文件未生成: {path}"
        with open(path, "rb") as f:
            assert f.read(4) == b"RIFF", "占位文件非 WAV 格式"
        assert calls["n"] == 0, "mock 模式不应触发任何 OpenAI 调用"
    finally:
        config.SETTINGS.raw = saved
