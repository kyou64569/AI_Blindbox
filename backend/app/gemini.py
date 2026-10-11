"""Gemini 原生 API 支持（Google AI / generativelanguage.googleapis.com）。

与项目其他部分统一走 OpenAI 兼容层的做法不同，这里直接对Google 原生 REST
端点发请求，原因是原生端点才有 thinkingLevel（Gemini 3+的思考强度）与
`/v1beta/models` 模型列表——这两项在 OpenAI 兼容层上要么不生效、要么结构不同。

覆盖两件事：
1. chat 调用：POST {base}/models/{model}:generateContent，
   通过 generationConfig.thinkingConfig.thinkingLevel 控制思考强度，
   includeThoughts=true 让响应里带回思考摘要（复用 thinking.py 提取）。
2. 模型列表：GET {base}/models?key=...，解析 models[].name并剥掉 "models/" 前缀
   （原生返回 models[].supportedGenerationMethods，与 OpenAI 的 data[].id 完全不同）。

鉴权同时支持两种：base_url 里已含 ?key= 时直接用；否则用 x-goog-api-key 头。

设计约束：只依赖 httpx（项目已有），不引入 google-genai SDK——保持轻量，
避免为一个厂商增加重型依赖。
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx

from .thinking import _accrue_reasoning
from .token_usage import _accrue_usage

_log = logging.getLogger(__name__)

# Gemini 原生 host 特征（用于自动识别已填的 base_url 是否属于原生端点）
_GEMINI_HOSTS = ("generativelanguage.googleapis.com",)

# 思考强度 -> Gemini 3+ 的 thinkingLevel 取值
#   off  → Gemini 3 不支持真正关闭思考，用 minimal 表达"最接近关闭"
#   注意：thinking_budget 是 Gemini 2.5 及更早的参数，传给 Gemini 3 会间歇性 400，
#   故这里一律用 thinking_level，不用 budget。
_THINKING_LEVEL_MAP = {
    "off": "minimal",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
}

# 思考摘要最长保留字数（防止超长思考撑爆归档 JSON）
_MAX_THOUGHTS = 24000


def is_gemini_native(base_url: str) -> bool:
    """判断 base_url 是否为 Gemini 原生端点（含 /v1beta 且非 /openai 子路径）。"""
    url = (base_url or "").strip().lower()
    if not url:
        return False
    if not any(h in url for h in _GEMINI_HOSTS):
        return False
    # OpenAI 兼容层走已有路径，不要重复接管
    if "/openai" in url:
        return False
    return True


def _split_key_query(base_url: str, api_key: str) -> "tuple[str, str | None]":
    """把可能内嵌在 URL 里的 ?key=xxx 拆出来。

    返回 (清洗后的 base_url, url 内嵌的 key 或 None)。
    """
    parsed = urlparse(base_url)
    if not parsed.query:
        return base_url, None
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    key = q.get("key") or None
    q.pop("key", None)
    rebuilt = parsed._replace(query=urlencode(q)).geturl()
    return rebuilt.rstrip("?&"), key


def _model_path(model: str) -> str:
    """把模型名规范成原生端点要的路径片段（不含 models/ 前缀）。"""
    m = (model or "").strip()
    if m.lower().startswith("models/"):
        m = m[7:]
    # 兼容用户误填 /v1beta/models/xxx 这类完整 URL
    if "models/" in m:
        m = m.split("models/", 1)[1]
    return m.split("/")[0].strip()


def _build_thinking_config(thinking: str | None) -> "dict | None":
    """按思考强度构造 generationConfig.thinkingConfig。

    - default / 空 → 不传该字段，用模型默认思考水平
    - off→ minimal（Gemini 3 无法真正关闭思考，minimal 最接近）
    - low / medium / high →对应thinkingLevel
    """
    key = (thinking or "").strip().lower()
    if not key or key == "default":
        return None
    level = _THINKING_LEVEL_MAP.get(key)
    if not level:
        return None
    return {"thinkingLevel": level, "includeThoughts": True}


def _messages_to_contents(messages: "list[dict]") -> "tuple[list[dict], str | None]":
    """把 OpenAI 风格 messages 转成 Gemini 的 contents + systemInstruction。

    - system 消息抽为 systemInstruction（Gemini 原生不认 system role）
    - user/assistant 合并进contents[].parts[].text
    """
    system_parts: list[str] = []
    contents: list[dict] = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        content = m.get("content")
        if isinstance(content, list):
            # 多模态：只取 text 片段拼接（本项目仅文本调用走原生）
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        text = content or ""
        if role == "system":
            if text.strip():
                system_parts.append(text)
            continue
        gem_role = "model" if role == "assistant" else "user"
        contents.append({"role": gem_role, "parts": [{"text": text}]})
    sys_instr = "\n\n".join(system_parts) if system_parts else None
    return contents, sys_instr


def _extract_text_and_thoughts(payload: "dict | None") -> "tuple[str, str]":
    """从 generateContent 响应里分别取出正文与思考摘要。"""
    if not isinstance(payload, dict):
        return "", ""
    texts: list[str] = []
    thoughts: list[str] = []
    for cand in payload.get("candidates") or []:
        content = (cand or {}).get("content") or {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            t = part.get("text") or ""
            if not t:
                continue
            if part.get("thought") is True:
                thoughts.append(t)
            else:
                texts.append(t)
    return "\n".join(texts).strip(), "\n".join(thoughts).strip()


def _raise_for_error(payload: Any, status: int, model: str) -> None:
    """把 Gemini 错误响应转成可读异常（含 400 常见原因提示）。"""
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        msg = (err.get("message") if isinstance(err, dict) else None) or str(payload)[:300]
        status = (err.get("code") if isinstance(err, dict) else None) or status
    else:
        msg = str(payload)[:300]
    hint = ""
    low = str(msg).lower()
    if status == 400 or "invalid_argument" in low:
        hint = (
            "；常见原因：thinkingLevel 与 thinkingBudget 不可同时传（Gemini 3 请只用 thinkingLevel）、"
            "该模型不支持所选思考强度（如 3.1 Pro 不支持 minimal）、或模型名不含 'models/' 前缀时被拒"
        )
    if status == 401 or status == 403:
        hint = "；请检查 API Key 是否正确、是否已启用 Generative Language API"
    raise RuntimeError(f"[gemini-native] model={model} 调用失败 HTTP {status}: {msg}{hint}")


def _stream_generate_content(
    base_url: str,
    api_key: str,
    model: str,
    body: "dict",
    timeout: float,
    on_thought: Any,
) -> "tuple[str, str]":
    """流式调用 Gemini 原生 :streamGenerateContent，返回 (正文, 思考摘要)。

    用 SSE（?alt=sse）逐行读 JSON，每个 chunk 里 thought=true 的 part 是思考增量，
    立即回调 on_thought；正文 part 拼起来最后返回。
    """
    url = f"{base_url.rstrip('/')}/models/{model}:streamGenerateContent?alt=sse"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    texts: list[str] = []
    thoughts: list[str] = []
    usage: dict = {}

    with httpx.Client(timeout=timeout, verify=True) as hc:
        with hc.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code >= 400:
                # 错误在流开始前就要读出来（此时 body 还没被消费）
                try:
                    parsed = json.loads(r.read().decode("utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    parsed = None
                _raise_for_error(parsed, r.status_code, model)

            for line in r.iter_lines():
                if not line:
                    continue
                s = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                s = s.strip()
                if not s.startswith("data:"):
                    continue  # 忽略 event: / :heartbeat / id: 等非数据行
                raw = s[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    chunk = json.loads(raw)
                except Exception:  # noqa: BLE001 — 单行解析失败跳过，不中断整流
                    continue
                t, th = _extract_text_and_thoughts(chunk)
                if t:
                    texts.append(t)
                if th:
                    thoughts.append(th)
                    on_thought(th)
                um = chunk.get("usageMetadata")
                if isinstance(um, dict):
                    usage = um

    return "".join(texts).strip(), "".join(thoughts).strip()


def chat(
    creds: "dict",
    messages: "list[dict]",
    temperature: "float | None" = None,
    timeout: float = 90.0,
    on_thought: Any = None,
) -> "tuple[str, str]":
    """调用 Gemini 原生 generateContent，返回 (正文, 思考摘要)。

    creds 需含 base_url / api_key / model，可选 thinking（思考强度）。
    思考摘要为空时第二项为 ""，调用方无需特判。

    on_thought 传入时切到流式（:streamGenerateContent），思考增量实时回调；
    不传则一次性返回。
    """
    base_url, url_key = _split_key_query(creds.get("base_url") or "", creds.get("api_key") or "")
    if not base_url:
        raise RuntimeError("[gemini-native] base_url 不能为空")
    api_key = url_key or (creds.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("[gemini-native] api_key 不能为空")
    model = _model_path(creds.get("model") or "")
    if not model:
        raise RuntimeError("[gemini-native] model 不能为空")

    contents, sys_instr = _messages_to_contents(messages)
    body: dict[str, Any] = {"contents": contents}
    if sys_instr:
        body["systemInstruction"] = {"parts": [{"text": sys_instr}]}
    # temperature 优先用显式入参；未传时回退 creds 里的值（_resolve_models 已解析好温度回退链），
    # 两者皆无才不注入 —— 避免调用方漏传导致静默丢掉温度设置。
    temp = temperature if temperature is not None else creds.get("temperature")
    gen_cfg: dict[str, Any] = {}
    if isinstance(temp, (int, float)):
        gen_cfg["temperature"] = float(temp)
    thinking_cfg = _build_thinking_config(creds.get("thinking"))
    if thinking_cfg:
        gen_cfg["thinkingConfig"] = thinking_cfg
    if gen_cfg:
        body["generationConfig"] = gen_cfg

    if on_thought is not None:
        text, thoughts = _stream_generate_content(
            base_url, api_key, model, body, timeout, on_thought,
        )
        if thoughts:
            # 流式已实时回调过，这里只补齐收集链路里的完整文本
            _accrue_reasoning(
                {"candidates": [{"content": {"parts": [{"thought": True, "text": thoughts[:_MAX_THOUGHTS]}]}}]},
                "gemini-native",
                model,
            )
        if not text:
            raise RuntimeError(
                f"[gemini-native] model={model} 流式返回空正文"
                f"，常见原因：内容审核拦截 / 配额耗尽 / 模型名写错"
            )
        return text, thoughts

    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    with httpx.Client(timeout=timeout, verify=True) as hc:
        r = hc.post(url, headers=headers, json=body)

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        payload = None

    if r.status_code >= 400:
        _raise_for_error(payload, r.status_code, model)

    text, thoughts = _extract_text_and_thoughts(payload)

    # 思考内容接入统一收集链路（与 OpenAI 兼容层的 reasoning_content 汇到同一处）
    if thoughts:
        _accrue_reasoning(
            {"candidates": [{"content": {"parts": [{"thought": True, "text": thoughts[:_MAX_THOUGHTS]}]}}]},
            "gemini-native",
            model,
        )
    # token 用量接入统一统计（原生字段名与 OpenAI 一致）
    _accrue_usage(payload if isinstance(payload, dict) else {}, model)

    if not text:
        block = ""
        if isinstance(payload, dict):
            block = str((payload.get("promptFeedback") or {}).get("blockReason") or "")
        raise RuntimeError(
            f"[gemini-native] model={model} 返回空正文"
            f"{('，原因: ' + block) if block else ''}"
            f"，常见原因：内容审核拦截 / 配额耗尽 / 模型名写错"
        )
    return text, thoughts


def list_models(base_url: str, api_key: str, timeout: float = 15.0) -> "list[str]":
    """调用 Gemini 原生 GET {base}/models，返回可用的 generateContent 模型 id 列表。

    只保留 supportedGenerationMethods 含 generateContent 的模型（过滤掉
    embedContent / 图像专属等），并剥掉 "models/" 前缀——前端下拉与配置里
    存的模型名不带前缀更简洁。
    """
    base_url, url_key = _split_key_query(base_url or "", "")
    key = url_key or (api_key or "").strip()
    if not base_url:
        raise RuntimeError("base_url 不能为空")
    if not key:
        raise RuntimeError("api_key 不能为空")

    url = f"{base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json", "x-goog-api-key": key}
    ids: list[str] = []
    with httpx.Client(timeout=timeout, verify=True) as hc:
        # 原生分页默认 50 条，显式拉大避免漏模型
        r = hc.get(url, headers=headers, params={"pageSize": 1000})
        if r.status_code >= 400:
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = None
            _raise_for_error(payload, r.status_code, "models.list")
        payload = r.json()
        for m in payload.get("models") or []:
            if not isinstance(m, dict):
                continue
            methods = m.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue  # 只支持 embed / 图像专属的模型不列
            name = (m.get("name") or "").strip()
            if not name:
                continue
            ids.append(name[7:] if name.lower().startswith("models/") else name)

    if not ids:
        raise RuntimeError("Gemini 返回了空的模型列表")
    return sorted(set(ids))