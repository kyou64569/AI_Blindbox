"""思考内容（reasoning / thinking）提取与收集。

与 token_usage 同款contextvar 方案：一次开盒内的多次模型调用（创意 / 规划 / 执行 / 重试）
把各自的思考文本收集到同一份列表，随结果返回给前端「思考过程」面板。

采集方式为「一次性提取 + 前端打字机回放」，不是逐 token 推送：
后端 8 处chat.completions.create() 全部是非流式，真流式需逐个改写成
create(stream=True) 并加 SSE 通道，收益不足以抵消改动面。故此处只做兜底提取 ——
只要厂商在响应里带回思考字段（reasoning_content / reasoning / thinking /
<think> 包裹块 / Gemini 原生 parts.thought），就能显示出来。

未开启会话、响应无思考内容、或思考为空时全部静默跳过，不影响业务路径。
"""
from __future__ import annotations

import contextvars
import re
from typing import Any

# 厂商返回思考内容的常见字段名（OpenAI 兼容层各家命名不统一）
_REASONING_FIELDS = (
    "reasoning_content",   # DeepSeek / Step / 多数国产兼容层
    "reasoning",           # OpenRouter / 部分聚合层
    "thinking",            # Qwen 等
    "thinking_content",    # 另一些Qwen 兼容实现
)

# 单条思考与单次开盒总量上限，防止超长思考把归档 JSON 撑爆
_MAX_PER_ENTRY = 12000
_MAX_TOTAL = 24000

# <think>...</think> / <thinking>...</thinking> 包裹块（部分模型塞在 content 里）
_THINK_BLOCK_RE = re.compile(r"<(think|thinking|reasoning)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)

_REASONING_CTX: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "blindbox_reasoning", default=None
)
_STAGE_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "blindbox_reasoning_stage", default=""
)
# 思考增量的实时回调（流式）。由 pipeline 从 run_blind_box 的 on_delta 参数注入，
# 让 chat_text 不必层层透传回调也能把增量推到 SSE。
_DELTA_CTX: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "blindbox_reasoning_delta", default=None
)
# 当前思考段对应的模型名（用于 reasoning_start 事件的 model 字段）
_MODEL_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "blindbox_reasoning_model", default=""
)


def emit_delta(text: str) -> None:
    """把一段思考增量推给当前会话注册的实时回调（未开启流式时静默跳过）。"""
    if not text:
        return
    cb = _DELTA_CTX.get()
    if cb is None:
        return
    try:
        cb(text)
    except Exception:  # noqa: BLE001 — 前端连接断开不应影响模型调用
        pass


class _model_scope:
    """标记「本次调用实际使用的模型」，供 SSE reasoning_start 事件带上模型名。

    必须在 chat_text 里（而非 pipeline 的 _delta_scope）设置，因为 fallback
    可能换模型——只有真正发起调用的那一层知道用的是哪张卡片。
    """

    def __init__(self, model: str):
        self.model = model or ""

    def __enter__(self) -> str:
        self._t = _MODEL_CTX.set(self.model)
        return self.model

    def __exit__(self, *exc) -> None:
        if getattr(self, "_t", None) is not None:
            _MODEL_CTX.reset(self._t)
            self._t = None


class _delta_scope:
    """在一次模型调用范围内注入实时增量回调。

    用法：
        with _delta_scope(callback):
            concept = _agent_call("creative", ...)
    """

    def __init__(self, callback: Any, model: str = ""):
        self.callback = callback
        self.model = model

    def __enter__(self) -> Any:
        self._t1 = _DELTA_CTX.set(self.callback)
        self._t2 = _MODEL_CTX.set(self.model or "")
        return self.callback

    def __exit__(self, *exc) -> None:
        if getattr(self, "_t1", None) is not None:
            _DELTA_CTX.reset(self._t1)
            self._t1 = None
        if getattr(self, "_t2", None) is not None:
            _MODEL_CTX.reset(self._t2)
            self._t2 = None


def current_model() -> str:
    """当前作用域的模型名（供 reasoning_start 事件使用）。"""
    return _MODEL_CTX.get() or ""


def _read(container: Any, key: str) -> Any:
    """从 pydantic 模型或 dict 里安全取值，取不到返回 None。"""
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get(key)
    return getattr(container, key, None)


def _coerce_text(val: Any) -> str:
    """把思考字段值统一成字符串；list/dict 等结构做浅层拼接。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        # 少数厂商返回 [{type:"text", text:"..."}, ...]
        parts = []
        for item in val:
            t = _read(item, "text") or _read(item, "content")
            if isinstance(t, str):
                parts.append(t)
        return "\n".join(parts).strip()
    if isinstance(val, dict):
        t = _read(val, "text") or _read(val, "content")
        return t.strip() if isinstance(t, str) else ""
    return ""


def _from_message(msg: Any) -> str:
    """从 OpenAI 兼容的 message 对象里取思考文本。"""
    for key in _REASONING_FIELDS:
        text = _coerce_text(_read(msg, key))
        if text:
            return text
    return ""


def _from_choices(resp: Any) -> str:
    """遍历所有 choice 取思考文本（取第一条非空的）。"""
    choices = _read(resp, "choices") or []
    for ch in choices:
        text = _from_message(_read(ch, "message"))
        if text:
            return text
    return ""


def _from_gemini_candidates(resp: Any) -> str:
    """Gemini 原生响应：candidates[].content.parts[] 中 thought=True 的片段才是思考。

    仅在使用 google-genai / 原生 REST 时命中；走 OpenAI 兼容层时本函数直接返回空。
    """
    candidates = _read(resp, "candidates") or []
    chunks: list[str] = []
    for cand in candidates:
        content = _read(cand, "content")
        parts = _read(content, "parts") or []
        for part in parts:
            if _read(part, "thought") is True:
                t = _coerce_text(_read(part, "text"))
                if t:
                    chunks.append(t)
    return "\n".join(chunks).strip()


def extract_reasoning(resp: Any) -> str:
    """从任意一次模型响应中提取思考文本，兼容多厂商字段命名。

    依次尝试：choices[].message.reasoning* → Gemini 原生 parts.thought →
    content 里的 <think> 包裹块。都取不到时返回空字符串。
    """
    if resp is None:
        return ""
    for extractor in (_from_choices, _from_gemini_candidates):
        try:
            text = extractor(resp)
        except Exception:  # noqa: BLE001 — 结构异常不应影响主流程
            text = ""
        if text:
            return _truncate(text)

    # 兜底：部分模型把思考直接写在 content 里，用 <think> 包裹
    choices = _read(resp, "choices") or []
    content = _coerce_text(_read(_read(choices[0], "message") if choices else None, "content"))
    if content:
        blocks = _THINK_BLOCK_RE.findall(content)
        if blocks:
            return _truncate("\n".join(b[1].strip() for b in blocks if b[1].strip()))
    return ""


def _truncate(text: str) -> str:
    """截断超长思考，保留头部并标注省略字数。"""
    text = text.strip()
    if len(text) <= _MAX_PER_ENTRY:
        return text
    dropped = len(text) - _MAX_PER_ENTRY
    return text[:_MAX_PER_ENTRY] + f"\n\n…（思考内容过长，已省略 {dropped} 字）"


def accrue_reasoning_text(text: str, ctx: str = "", model: str = "") -> None:
    """把**已提取好的纯文本思考**写入归档 bucket。

    与 _accrue_reasoning 的区别：后者需要从响应结构里自行提取（认
    reasoning_content / parts.thought / <think> 包裹），而流式场景下
    增量本身已是纯文本，没有标签也没有响应结构，必须走这个入口。
    否则规划/执行阶段的思考会因提取失败被整个丢掉，结果页只剩创意阶段。
    """
    if not text or not text.strip():
        return
    bucket = _REASONING_CTX.get()
    if bucket is None:
        return
    text = _truncate(text)
    used = sum(len(e["text"]) for e in bucket)
    if used >= _MAX_TOTAL:
        return
    room = _MAX_TOTAL - used
    if len(text) > room:
        text = _truncate(text[:room])
    bucket.append({
        "stage": _STAGE_CTX.get() or "",
        "ctx": ctx,
        "model": model or "",
        "text": text,
    })


def _accrue_reasoning(resp: Any, ctx: str = "", model: str = "") -> None:
    """从一次响应里提取思考并追加到当前开盒会话（幂等安全）。

    未开启会话 / 无思考内容 / 超过总量上限时静默跳过。
    """
    bucket = _REASONING_CTX.get()
    if bucket is None:
        return
    try:
        text = extract_reasoning(resp)
    except Exception:  # noqa: BLE001 — 提取失败绝不能打断开盒
        return
    if not text:
        return
    used = sum(len(e["text"]) for e in bucket)
    if used >= _MAX_TOTAL:
        return
    room = _MAX_TOTAL - used
    if len(text) > room:
        text = _truncate(text[:room])
    bucket.append({
        "stage": _STAGE_CTX.get() or "",
        "ctx": ctx,
        "model": model or "",
        "text": text,
    })


class _ReasoningSession:
    """一次开盒的思考内容收集作用域。with 块退出后列表被冻结。

    用法：
        with _ReasoningSession() as bucket:
            ... 模型调用 ...
        # bucket 内是 [{stage, ctx, model, text}, ...]
    """

    def __init__(self):
        self.bucket: list | None = None

    def __enter__(self) -> list:
        self.bucket = []
        self._token = _REASONING_CTX.set(self.bucket)
        return self.bucket

    def __exit__(self, *exc) -> None:
        if getattr(self, "_token", None) is not None:
            _REASONING_CTX.reset(self._token)
            self._token = None


class _reasoning_stage:
    """给思考记录打上阶段标签的可嵌套上下文管理器。

    用法：
        with _reasoning_stage("创意生成中"):
            concept = _agent_call("creative", ...)
    """

    def __init__(self, label: str):
        self.label = label

    def __enter__(self) -> str:
        self._token = _STAGE_CTX.set(self.label or "")
        return self.label

    def __exit__(self, *exc) -> None:
        if getattr(self, "_token", None) is not None:
            _STAGE_CTX.reset(self._token)
            self._token = None


def _reasoning_snapshot(bucket: list | None) -> list | None:
    """把收集结果转成可JSON 序列化的副本；无内容时返回 None。"""
    if not bucket:
        return None
    cleaned = [
        {"stage": e.get("stage") or "", "ctx": e.get("ctx") or "",
         "model": e.get("model") or "", "text": e.get("text") or ""}
        for e in bucket if (e.get("text") or "").strip()
    ]
    return cleaned or None