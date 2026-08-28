"""拆分自 pipeline.py（agents.py）：模型调用层。

- _resolve_models / _normalize_base_url / _call_with_fallback / _executor_creds：凭据解析与按序回退
- _agent_call / _mock_agent / _make_simple_call：文本类 Agent 调用（真实 + mock）
"""
from __future__ import annotations

import logging
import re
import re as _re  # 历史别名：与 pipeline.py 原文件一致
import time as _time_mod
from typing import Any

from openai import OpenAI

# 注意：只导入 get_settings()，不要 `from .config import SETTINGS`。
from .config import get_settings
from .models import FallbackExhausted
from .tools import _first_choice_text

_log = logging.getLogger(__name__)


def _resolve_models(section: dict, global_: dict) -> list[dict]:
    """把某 section 的 models 列表解析为有序凭据列表。

    每张卡片的 base_url/api_key/temperature 为空时回退组级，再回退 global。
    跳过 model 为空的卡片。返回 [{model, base_url, api_key, temperature}, ...]。
    """
    if not isinstance(global_, dict):
        global_ = {}
    gb = section.get("base_url")
    gk = section.get("api_key")
    gt = section.get("temperature")
    out: list[dict] = []
    for card in (section.get("models") or []):
        if not isinstance(card, dict):
            continue
        m = (card.get("model") or "").strip()
        if not m:
            continue
        # 验证 model 名称只包含安全字符（允许 /，Google Gemini 等模型名格式）
        if not re.match(r'^[a-zA-Z0-9._/\-]+$', m):
            continue
        # 温度回退：卡片 -> 组级 -> global -> 默认 0.8
        temp = card.get("temperature")
        if temp is None:
            temp = gt if gt is not None else 0.8
        out.append({
            "model": m,
            "base_url": _normalize_base_url(card.get("base_url") or gb or global_.get("base_url") or ""),
            "api_key": card.get("api_key") or gk or global_.get("api_key"),
            "temperature": temp,
        })
    return out


def _normalize_base_url(base_url: str) -> str:
    """规范化 base_url，尽量不修改用户配置的自定义路径。

    - 去除末尾斜杠（避免拼出双斜杠）
    - 剥离末尾的 /models：用户常直接粘贴「模型列表」接口地址，
      而 OpenAI 客户端会在 base_url 后追加 /chat/completions，
      留着会拼成 /v1/models/chat/completions → HTTP 404
    - 若剥离后是纯域名（无任何路径），追加 /v1

    保留 step_plan/v1、api.foo.com/custom/path 等完整路径不变。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""

    # 剥离末尾 /models（可能重复出现，如 .../v1/models/）；/models 不可能是有效的
    # chat/completions 前缀，因此剥离是安全的。
    while True:
        stripped = _re.sub(r"/models$", "", url, flags=_re.IGNORECASE)
        if stripped == url:
            break
        url = stripped.rstrip("/")
    if not url:
        return ""

    # 已包含路径（含斜杠）则原样保留，如 https://host/step_plan/v1
    if "/" in url.split("://", 1)[-1]:
        return url
    # 纯域名，追加 /v1
    return url + "/v1"


def _call_with_fallback(models: list[dict], call_fn, ctx: str = "") -> "Any":
    """遍历 models 调用 call_fn(creds)。

    仅当 call_fn 抛 Exception 才 fallback 到下一个模型；任一成功即返回（成功即停）；
    全部失败抛 FallbackExhausted。
    """
    import logging as _log
    last: Exception | None = None
    for creds in models:
        _log.warning("[fallback %s] trying model=%s base_url=%s", ctx, creds["model"], creds.get("base_url", "?"))
        try:
            _log.info("[fallback] ctx=%s trying model=%s", ctx, creds["model"])
            result = call_fn(creds)
            _log.info("[fallback] ctx=%s SUCCESS with model=%s", ctx, creds["model"])
            return result, creds["model"]
        except Exception as e:  # noqa: BLE001
            _log.warning("[fallback] ctx=%s FAILED model=%s: %s: %s", ctx, creds["model"], type(e).__name__, str(e)[:200])
            last = e
    raise FallbackExhausted(ctx, last)


def _agent_call(agent_key: str, system: str, user: str, mode: dict | None = None) -> str:
    """调用单个文本类 Agent（创意 / 规划）。返回文本。支持多模型按序 fallback。

    模型列表来自该 Agent 的 models 配置；每张卡片的 base_url/api_key/temperature
    为空时回退组级再到 global。优先真实接口，否则 mock。
    """
    settings = get_settings()
    cfg = settings.agents.get(agent_key) or {}
    g = settings.global_
    models = _resolve_models(cfg, g)

    if (settings.mock
            or not any(c["api_key"] for c in models)
            or all(c["api_key"] == "<USER_KEY>" for c in models)):
        return _mock_agent(agent_key, system, user, mode)

    def call(creds: dict) -> str:
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=90, max_retries=0)
        resp = client.chat.completions.create(
            model=creds["model"],
            temperature=creds["temperature"] if creds["temperature"] is not None else 0.8,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return _first_choice_text(resp, f"agent:{agent_key}", creds["model"]).strip()

    out, _used = _call_with_fallback(models, call, ctx=f"agent:{agent_key}")
    return out


def _mock_agent(agent_key: str, system: str, user: str, mode: dict | None = None) -> str:
    """无 key 时的本地规则产出，保证骨架可演示。"""
    if agent_key == "creative":
        etype = (mode or {}).get("executor_type", "text")
        if etype == "image":
            # 静态图模式：只描述单帧画面，不写动态/声音，与真实链路约束保持一致
            return (
                "【概念卡】在「末日温室」的玻璃穹顶下，一只机械橘猫静静守护着最后的人类记忆种子；"
                "画面以水墨写真风格呈现，柔光穿过藤蔓，冷色调中透出一丝暖意，构图居中、主体突出。"
            )
        return (
            "【概念卡】在「末日温室」的玻璃穹顶下，一只机械橘猫守护着最后的人类记忆种子；"
            "画面以水墨写真风格呈现，柔光穿过藤蔓。一段独白缓缓响起：'我们种下的不是花，是时间。'"
        )
    if agent_key == "planner":
        etype = (mode or {}).get("executor_type", "text")
        if etype == "image":
            return ("IMAGE_PROMPT: a mechanical orange cat guarding the last seed of human memory "
                    "inside a post-apocalyptic glass greenhouse, ink-wash realistic style, soft light "
                    "through vines --no humans --no text --ar 16:9")
        if etype == "video":
            return ("STORYBOARD: scene1 establish the glass greenhouse; scene2 the orange cat reveals "
                    "the seed; scene3 memory bloom. style=cinematic, duration=6s")
        if etype in ("text",):
            return ("【创作规划与大纲】\n"
                    "【标题】《末日温室的守护者》\n"
                    "【视角与基调】第三人称限制视角，语言凝练、冷峻中带着一丝温情\n"
                    "【核心情节】废弃温室中机械橘猫守护最后一株记忆种子，随着幼苗发光回溯人类曾经的繁荣，最后平静守护。\n"
                    "【写作指导】注重描写画面中的光影与意象，字数控制在 400-600 字。")
        if etype in ("code-spec", "code-cli"):
            return ("SPEC: 技术栈 Python+FastAPI；目录 src/ tests/；模块：config 加载、pipeline 编排、"
                    "executor 分发；验收：能跑通一次盲盒并返回结构化结果。")
    # executor mock 占位
    return f"[mock output for {agent_key}]"


def _make_simple_call(models: list[dict], sys_prompt: str, usr_prompt: str):
    """创建一个简单的 call 闭包，用于两步式生成。"""
    def call(creds: dict) -> str:
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=120, max_retries=0)
        # 使用配置的温度，默认 0.6
        temp = creds.get("temperature", 0.6)
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=creds["model"],
                    temperature=temp,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": usr_prompt},
                    ],
                )
                content = _first_choice_text(resp, "文本执行 Agent", creds["model"]).strip()
                if content:
                    return content
                if attempt == 0:
                    _time_mod.sleep(2)
                    continue
                raise RuntimeError("模型返回空内容")
            except Exception as e:
                if attempt == 0:
                    _time_mod.sleep(2)
                    continue
                raise e
    return call


def _executor_creds(executor_type: str) -> list[dict]:
    """解析执行 Agent 的凭据列表（按序 fallback）。

    根据 executor_type 选择正确的模型来源：
    - image  → providers.image（图片生成模型）
    - video  → providers.video（视频生成模型）
    - 其余  → agents.executor（文本 / 代码模型）

    若对应来源为空，则回退到其余来源，最终 global 兜底。
    """
    settings = get_settings()
    g = settings.global_

    preferred_key = None
    if executor_type == "image":
        preferred_key = "image"
    elif executor_type == "video":
        preferred_key = "video"
    elif executor_type == "game":
        preferred_key = "game"
    elif executor_type == "tts":
        preferred_key = "tts"
    elif executor_type == "illustrated_story":
        preferred_key = "image"  # 复用图片模型，逐页生成
    elif executor_type == "video_storyboard":
        preferred_key = "image"  # 复用图片模型，逐镜生成
    elif executor_type in ("song",):
        preferred_key = None  # 文本生成走 executor agent，TTS 在函数内单独处理

    # 优先取对应 provider 的模型列表
    pm: list[dict] = []
    if preferred_key:
        pv = settings.providers.get(preferred_key) or {}
        pm = _resolve_models(pv, g)
        if pm:
            # image/video/tts：直接返回 provider 模型
            if preferred_key in ("image", "video", "tts"):
                return pm
            # game 继续往下构建完整 fallback 链

    # 回退到 executor Agent 配置（仅 image/video 直接返回，game/text 加入 fallback 链）
    exec_cfg = settings.agents.get("executor") or {}
    exec_models = _resolve_models(exec_cfg, g)
    if preferred_key in ("image", "video") and exec_models:
        return exec_models

    # 构建多源 fallback 链
    # 对于 image/video 类：优先取自另一 provider 的模型
    # 对于 game/text 类：优先取自 Agent 的模型（executor 在链首，creative/planner 兜底）
    chain: list[dict] = []
    if preferred_key in ("image", "video"):
        for pk in ("image", "video"):
            if pk == preferred_key:
                continue
            pv = settings.providers.get(pk) or {}
            pm = _resolve_models(pv, g)
            if pm:
                chain.append(pm[0])
    else:
        # game 模式：provider 模型 + executor + creative/planner + global 兜底
        if pm:
            for creds in pm:
                norm_url = _normalize_base_url(creds["base_url"])
                if (creds["model"], norm_url) not in {(c["model"], _normalize_base_url(c["base_url"])) for c in chain}:
                    chain.append(creds)
        for creds in (exec_models or []):
            norm_url = _normalize_base_url(creds["base_url"])
            if (creds["model"], norm_url) not in {(c["model"], _normalize_base_url(c["base_url"])) for c in chain}:
                chain.append(creds)
        for ak in ("creative", "planner"):
            ag = settings.agents.get(ak) or {}
            m = (ag.get("model") or "").strip()
            norm_url = _normalize_base_url(ag.get("base_url") or g.get("default_base_url") or "")
            if not m or (m, norm_url) in {(c["model"], _normalize_base_url(c["base_url"])) for c in chain}:
                continue
            chain.append({
                "model": m,
                "base_url": norm_url,
                "api_key": ag.get("api_key") or g.get("default_api_key"),
                "temperature": ag.get("temperature") or 0.7,
            })
    chain.append({
        "model": g.get("default_model", "agnes-free"),
        "base_url": _normalize_base_url(g.get("default_base_url") or ""),
        "api_key": g.get("default_api_key"),
        "temperature": None,
    })
    return chain
