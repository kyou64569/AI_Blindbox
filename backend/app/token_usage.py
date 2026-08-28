"""拆分自 pipeline.py（token_usage.py）：一次开盒的 token 用量追踪。

用 contextvar 而不是全局变量：一次开盒的多次模型调用（创意/规划/执行/重试）都累加
到同一次运行的计数里；并发开盒时 contextvar 按调用栈隔离，不会互相污染。
未启用（模块默认 None）或模型不返回 usage 时各字段为 0 / None。
"""
from __future__ import annotations

import contextvars
from typing import Any

_TOKEN_USAGE_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "blindbox_token_usage", default=None
)


def _usage_accum() -> "dict | None":
    """返回当前开盒会话的 token 累加器（contextvar）；无会话时返回 None。"""
    return _TOKEN_USAGE_CTX.get()


def _accrue_usage(resp: Any, model: str = "") -> None:
    """从一次 OpenAI 兼容响应的 usage 字段累加 token（contextvar 会话内）。

    幂等安全：resp 无 usage / 非 dict / 未开启追踪时静默跳过，不影响业务路径。
    """
    acc = _usage_accum()
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if not isinstance(usage, dict) and not hasattr(usage, "prompt_tokens"):
        return
    pt = getattr(usage, "prompt_tokens", None) if not isinstance(usage, dict) else usage.get("prompt_tokens")
    ct = getattr(usage, "completion_tokens", None) if not isinstance(usage, dict) else usage.get("completion_tokens")
    tt = getattr(usage, "total_tokens", None) if not isinstance(usage, dict) else usage.get("total_tokens")
    if isinstance(pt, int):
        acc["prompt_tokens"] += pt
    if isinstance(ct, int):
        acc["completion_tokens"] += ct
    if isinstance(tt, int):
        acc["total_tokens"] += tt
    # 防御性检查：确保 acc 不为 None（理论上不应发生，但保持健壮性）
    if acc is not None:
        acc["model"] = model or acc.get("model") or ""


class _TokenUsageSession:
    """一次开盒的 token 计数作用域。with 块进入时创建累加器，退出时取走结果。

    用法：
        with _TokenUsageSession() as acc:
            ... 模型调用 ...
        # acc 已冻结为 dict（含 prompt_tokens / completion_tokens / total_tokens）
    """

    def __init__(self):
        self.acc: dict | None = None

    def __enter__(self) -> dict:
        self.acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": ""}
        self._token = _TOKEN_USAGE_CTX.set(self.acc)
        return self.acc

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            _TOKEN_USAGE_CTX.reset(self._token)
            self._token = None
