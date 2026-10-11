"""思考提取与 contextvar 作用域回归。

对应代码审查条目2（thinking.py 的 "__none__" 死代码）与
条目3（非流式路径未纳入 _model_scope）。
"""
from __future__ import annotations

import contextvars

import pytest

from app import thinking
from app.thinking import (
    _DELTA_CTX,
    _MODEL_CTX,
    _model_scope,
    _ReasoningSession,
    _reasoning_stage,
    accrue_reasoning_text,
    current_model,
    emit_delta,
    extract_reasoning,
)


# ---------------------------------------------------------------------------
# 条目2：<think> 包裹的思考必须能提取出来
# ---------------------------------------------------------------------------

def test_extract_think_tag_fallback():
    """思考写在 content 的 <think> 块里时，必须能提取。

    这是审查条目2 指向的代码路径。修复前那行 `__none__` 是死代码，
    但下方 203-205 行有等价兜底，因此功能其实没坏——本用例固化该行为。
    """
    resp = {"choices": [{"message": {"content": "<think>思考内容</think>正文内容"}}]}
    assert "思考内容" in extract_reasoning(resp)


def test_extract_think_tag_multiple_blocks():
    resp = {"choices": [{"message": {
        "content": "<think>第一段</think>中间<think>第二段</think>"
    }}]}
    got = extract_reasoning(resp)
    assert "第一段" in got and "第二段" in got


def test_extract_prefers_reasoning_field_over_think_tag():
    """有标准 reasoning_content 字段时，应优先用它而非 <think> 块。"""
    resp = {"choices": [{"message": {
        "reasoning_content": "标准字段的思考",
        "content": "<think>标签里的思考</think>",
    }}]}
    got = extract_reasoning(resp)
    assert got == "标准字段的思考"


def test_extract_gemini_candidates():
    resp = {"candidates": [{"content": {"parts": [
        {"text": "正文", "thought": False},
        {"text": "原生思考", "thought": True},
    ]}}]}
    assert extract_reasoning(resp) == "原生思考"


@pytest.mark.parametrize("resp", [
    None,
    {},
    {"choices": []},
    {"choices": [{"message": {"content": "只有正文"}}]},
    {"choices": [{"message": {}}]},
])
def test_extract_returns_empty_when_no_reasoning(resp):
    """无思考时返回空串而不是抛异常。"""
    assert extract_reasoning(resp) == ""


def test_extract_never_raises_on_malformed_structure():
    """畸形结构不能抛异常（提取失败绝不能打断开盒）。"""
    class Weird:
        choices = "not-a-list"
        @property
        def candidates(self):
            raise RuntimeError("boom")

    assert extract_reasoning(Weird()) == ""


# ---------------------------------------------------------------------------
# 条目3：_model_scope 必须覆盖非流式路径
# ---------------------------------------------------------------------------

def test_model_scope_sets_and_resets():
    assert current_model() == ""
    with _model_scope("glm-5.2"):
        assert current_model() == "glm-5.2"
        with _model_scope("intern-s2"):
            assert current_model() == "intern-s2"
        assert current_model() == "glm-5.2"
    assert current_model() == ""


def test_model_scope_resets_on_exception():
    """异常时也必须复位 contextvar，否则会污染同线程后续调用。"""
    with pytest.raises(RuntimeError):
        with _model_scope("m"):
            raise RuntimeError("x")
    assert current_model() == ""


def test_model_scope_restores_previous_value():
    token = _MODEL_CTX.set("outer-model")
    try:
        with _model_scope("inner-model"):
            assert current_model() == "inner-model"
        assert current_model() == "outer-model"
    finally:
        _MODEL_CTX.reset(token)


def test_chat_text_non_streaming_sets_model_scope():
    """非流式调用期间 current_model() 必须可读（审查条目3）。

    修复前非流式的 OpenAI 分支落在 with 块之外，模型名为空。
    这里通过桩客户端在 create() 内部读取，直接验证调用期内可见。
    """
    from app import agents as ag

    seen: list[str] = []

    class _Msg:
        content = "回答"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        @staticmethod
        def create(**kw):
            seen.append(current_model())
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, *a, **k):
            self.chat = _Chat()

    creds = {
        "base_url": "https://example.invalid/v1", "api_key": "k",
        "model": "probe-model-xyz", "temperature": 0.8,
    }
    orig = ag.OpenAI
    ag.OpenAI = _Client
    try:
        out = ag.chat_text(creds, [{"role": "user", "content": "hi"}], ctx="probe")
    finally:
        ag.OpenAI = orig

    assert out == "回答"
    assert seen == ["probe-model-xyz"], f"非流式路径内模型名应为 probe-model-xyz，实际 {seen}"
    assert current_model() == "", "退出后 contextvar 应复位"


# ---------------------------------------------------------------------------
# accrue_reasoning_text：流式思考的归档入口
# ---------------------------------------------------------------------------

def test_accrue_reasoning_text_records_stage_and_model():
    with _ReasoningSession() as bucket:
        with _reasoning_stage("创意生成中"), _model_scope("gemini-3.5-flash-lite"):
            accrue_reasoning_text("流式收集到的思考", "creative", "gemini-3.5-flash-lite")
    assert len(bucket) == 1
    assert bucket[0]["stage"] == "创意生成中"
    assert bucket[0]["model"] == "gemini-3.5-flash-lite"
    assert bucket[0]["text"] == "流式收集到的思考"


def test_accrue_reasoning_text_multiple_stages():
    """流式路径下多个阶段应各自成条（对应之前「只有创意有思考」的 bug）。"""
    with _ReasoningSession() as bucket:
        with _reasoning_stage("创意生成中"):
            accrue_reasoning_text("创意思考", "creative", "m1")
        with _reasoning_stage("规划生成中"):
            accrue_reasoning_text("规划思考", "planner", "m2")
        with _reasoning_stage("成品生成中"):
            accrue_reasoning_text("执行思考", "executor", "m3")
    assert len(bucket) == 3
    assert [b["stage"] for b in bucket] == ["创意生成中", "规划生成中", "成品生成中"]


def test_accrue_reasoning_text_ignores_empty():
    with _ReasoningSession() as bucket:
        accrue_reasoning_text("", "ctx", "m")
        accrue_reasoning_text("   ", "ctx", "m")
    assert bucket == []


def test_accrue_reasoning_text_noop_without_session():
    """未开启会话时静默跳过，不能抛异常。"""
    accrue_reasoning_text("没有会话时的思考", "ctx", "m")


def test_accrue_reasoning_text_respects_total_cap():
    """总量上限必须生效，防止超长思考撑爆归档 JSON。"""
    thinking._MAX_TOTAL = 100
    try:
        with _ReasoningSession() as bucket:
            accrue_reasoning_text("x" * 80, "a", "m")
            accrue_reasoning_text("y" * 80, "b", "m2")
        assert sum(len(b["text"]) for b in bucket) <= 100
    finally:
        thinking._MAX_TOTAL = 24000


def test_accrue_reasoning_text_truncates_long_entry():
    thinking._MAX_PER_ENTRY = 50
    try:
        with _ReasoningSession() as bucket:
            accrue_reasoning_text("z" * 200, "a", "m")
        assert len(bucket) == 1
        assert len(bucket[0]["text"]) <= 80  # 50 + 省略提示
        assert "已省略" in bucket[0]["text"]
    finally:
        thinking._MAX_PER_ENTRY = 12000


# ---------------------------------------------------------------------------
# emit_delta / contextvar 回调
# ---------------------------------------------------------------------------

def test_emit_delta_calls_callback():
    got: list[str] = []
    token = _DELTA_CTX.set(got.append)
    try:
        emit_delta("片段1")
        emit_delta("片段2")
    finally:
        _DELTA_CTX.reset(token)
    assert got == ["片段1", "片段2"]


def test_emit_delta_without_callback_is_noop():
    assert _DELTA_CTX.get() is None
    emit_delta("没人监听")  # 不应抛异常


def test_emit_delta_swallows_callback_exception():
    """前端断连不该让模型调用失败。"""
    def boom(_text):
        raise RuntimeError("disconnected")

    token = _DELTA_CTX.set(boom)
    try:
        emit_delta("x")  # 不抛
    finally:
        _DELTA_CTX.reset(token)
