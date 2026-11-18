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
from .config import build_connection_map, conn_creds, get_settings
from .gemini import chat as _gemini_native_chat, is_gemini_native
from .models import FallbackExhausted
from .thinking import _DELTA_CTX, _model_scope, accrue_reasoning_text
from .thinking_profiles import thinking_budget, thinking_kwargs
from .tools import _first_choice_text
from .token_usage import _accrue_usage

_log = logging.getLogger(__name__)

# 思考强度合法取值（UI 下拉同款）；"default" 表示不注入任何思考参数
_THINKING_VALUES = {"default", "off", "minimal", "low", "medium", "high"}

# 流式 chunk 里承载思考增量的字段名（与 thinking._REASONING_FIELDS 对应）
_REASONING_DELTA_FIELDS = ("reasoning_content", "reasoning", "thinking", "thinking_content")


def _normalize_thinking(val: Any) -> str:
    """规范化思考强度：非法/空值一律降级为 default（不注入参数）。"""
    t = str(val or "").strip().lower()
    return t if t in _THINKING_VALUES else "default"


def _resolve_models(section: dict, global_: dict, connections: list | None = None) -> list[dict]:
    """把某 section 的 models 列表解析为有序凭据列表。

    每张卡片的 base_url/api_key/temperature/thinking 为空时回退组级，再回退 global。
    跳过model 为空的卡片。返回 [{model, base_url, api_key, temperature, thinking,
    thinking_profile, thinking_capability, ...}, ...]。

    thinking（思考强度）默认值处理：
      - 卡片显式给了非default 值 → 用它
      - 卡片为 default/空 → 看组级，再看 global；都没有则 "default"（不注入）
    thinking_profile / thinking_capability（思考方言与能力声明）：
      卡片 -> 组级 -> global.default_thinking_*，都没有则 None / "graded"。
      未配置方言时走兼容默认，请求体与旧版逐字节一致。
    Gemini 原生端点（is_gemini_native）会由调用方分流到原生 REST，
    非 Gemini 厂商按 thinking_profile 选择请求体字段（见 thinking_profiles.py）。
    """
    if not isinstance(global_, dict):
        global_ = {}
    # 连接池：槽位（组级）或模型卡片可用 connection 引用共享的 base_url + api_key，
    # 免去同一地址在十几个槽位里重复填写。引用命中则优先用连接凭据；
    # 未配置或不命中（悬空引用）时退回槽位自身的值 —— 老配置行为完全不变。
    if connections is None:
        try:
            connections = get_settings().connections
        except Exception:  # 单例未就绪时降级为「无连接池」，不阻断解析
            connections = []
    conn_map = build_connection_map(connections)
    gb = section.get("base_url")
    gk = section.get("api_key")
    gb, gk = conn_creds(conn_map, section.get("connection"), gb or "", gk or "")
    gt = section.get("temperature")
    gth = section.get("thinking")
    # 思考方言 / 能力声明：空值视为「未配置」，继续沿回退链找
    gprof = section.get("thinking_profile")
    gcap = section.get("thinking_capability")
    gmt = section.get("max_tokens")
    gto = section.get("timeout")
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
        # 思考强度回退：卡片 -> 组级 -> global.default_thinking/global.thinking -> default（不注入参数）
        th = _normalize_thinking(card.get("thinking"))
        if th == "default":
            for fallback in (gth, global_.get("default_thinking"), global_.get("thinking")):
                cand = _normalize_thinking(fallback)
                if cand != "default":
                    th = cand
                    break
        # 方言 / 能力声明：与 thinking 同款「卡片 -> 组级 -> global」回退。
        # 非法方言值不做收敛（由 apply_thinking_profile 抛可读错误，指南 §7.5）；
        # 保存配置时 main.py 另有白名单校验挡在入口。
        raw_prof = (card.get("thinking_profile") or gprof
                    or global_.get("default_thinking_profile")
                    or global_.get("thinking_profile"))
        prof = str(raw_prof).strip() if raw_prof else None
        cap = (card.get("thinking_capability") or gcap
               or global_.get("default_thinking_capability")
               or global_.get("thinking_capability") or "graded")
        # 卡片级连接引用优先于卡片自身的 base_url/api_key（连接 → 卡片 → 组级 → global）
        c_url, c_key = conn_creds(
            conn_map, card.get("connection"),
            card.get("base_url") or "", card.get("api_key") or "",
        )
        # 全局回退读 default_* 优先，兼容手改 yaml 写裸键的情况（两种写法都生效）
        base_url = _normalize_base_url(
            c_url or gb or global_.get("default_base_url") or global_.get("base_url") or ""
        )
        # max_tokens 回退：卡片 -> 组级 -> global（default_max_tokens 优先）-> None（不下发）
        mt = card.get("max_tokens")
        if mt is None:
            if gmt is not None:
                mt = gmt
            else:
                mt = global_.get("default_max_tokens")
                if mt is None:
                    mt = global_.get("max_tokens")
        if mt is not None:
            try:
                mt = int(mt)
            except (TypeError, ValueError):
                mt = None
        if isinstance(mt, int) and mt <= 0:
            mt = None
        # timeout 回退：卡片 -> 组级 -> global（default_timeout 优先）-> None（用调用点默认）
        to = card.get("timeout")
        if to is None:
            if gto is not None:
                to = gto
            else:
                to = global_.get("default_timeout")
                if to is None:
                    to = global_.get("timeout")
        if to is not None:
            try:
                to = float(to)
            except (TypeError, ValueError):
                to = None
        if isinstance(to, (int, float)) and to <= 0:
            to = None
        out.append({
            "model": m,
            "base_url": base_url,
            "api_key": c_key or gk or global_.get("default_api_key") or global_.get("api_key"),
            "temperature": temp,
            "thinking": th,
            "thinking_profile": prof,
            "thinking_capability": cap,
            "max_tokens": mt,
            "timeout": to,
            # Gemini 原生端点标记：调用方据此走 :generateContent 而非 /chat/completions
            "gemini_native": is_gemini_native(base_url),
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


def _thinking_kwargs(creds: dict) -> "dict[str, Any]":
    """按思考方言生成 OpenAI 兼容层的请求参数。

    具体实现收敛在 thinking_profiles.py 的 THINKING_PROFILES 注册表：
    - 未配置方言 → 兼容默认 `effort-only`（档位 → reasoning_effort），
      但 **off 不注入任何字段**，存量配置请求体逐字节不变；
    - 显式配置方言 → 按该厂商的字段位置/形状/类型下发（含 NVIDIA NIM 的布尔 thinking，
      以及 effort-only 下 off → reasoning_effort:"none"）；
    - default（未设置）与 Gemini 原生端点一律不发任何思考参数。
    """
    return thinking_kwargs(creds)


def _max_tokens_kwargs(creds: dict) -> "dict[str, Any]":
    """把 creds 里的 max_tokens 转成 OpenAI 兼容层的下发参数。

    - 用户填了有效正整数 → 永远用户优先（不被思考预算覆盖）
    - 未配置 → 仅「显式配置了思考方言」时按档位抬预算，防思考挤掉业务输出；
      存量配置（无方言）不下发，沿用模型默认输出上限。
    """
    mt = creds.get("max_tokens")
    if mt is not None:
        try:
            mt = int(mt)
        except (TypeError, ValueError):
            mt = None
    if isinstance(mt, int) and mt > 0:
        return {"max_tokens": mt}
    budget = thinking_budget(creds)
    return {"max_tokens": budget} if budget else {}


def _openai_stream_text(
    creds: dict,
    messages: "list[dict]",
    temp: "float | None",
    ctx: str,
    timeout: float,
    on_delta: Any,
) -> str:
    """OpenAI 兼容层流式调用：逐 chunk 回调思考增量，最后拼接正文。

    思考增量来源：chunk 里 delta.reasoning_content / reasoning / thinking
    （与 thinking._REASONING_FIELDS 同一批字段名）。
    正文增量（delta.content）不在此回调——上层只关心思考的实时展示，
    正文仍由最终拼接结果给出，避免上层做逐字渲染的复杂度。
    """
    client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=timeout, max_retries=0)
    stream = client.chat.completions.create(
        model=creds["model"],
        temperature=temp,
        messages=messages,
        stream=True,
        **_thinking_kwargs(creds),
        **_max_tokens_kwargs(creds),
    )
    parts: list[str] = []
    usage_obj = None
    # 思考增量同时收集两处：SSE 实时推送 + 归档 bucket。
    # 只推 SSE 不归档会导致结果页「思考过程」只剩 Gemini 原生那一条
    # （原生路径在 gemini.py 里调了 _accrue_reasoning，兼容层没调）。
    thought_parts: list[str] = []
    try:
        for chunk in stream:
            # 部分网关把 usage 放在最后一个 chunk（需显式 stream_options 才给）
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage_obj = chunk
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            piece = getattr(delta, "content", None)
            if piece:
                parts.append(piece)
            for field in _REASONING_DELTA_FIELDS:
                rt = getattr(delta, field, None)
                if rt:
                    s = str(rt)
                    thought_parts.append(s)
                    on_delta(s)
                    break
    except Exception as e:  # noqa: BLE001
        # 流式中途失败：已收到的正文照样返回，让上层走 fallback 而不是全丢
        if not parts:
            raise
        _log.warning("[stream %s] 中断但已有 %d 字正文: %s", ctx, sum(len(p) for p in parts), e)
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    if usage_obj is not None:
        _accrue_usage(usage_obj, creds["model"])
    # 把本次流式收集到的思考写入归档 bucket（结果页「思考过程」数据源）。
    # 用 accrue_reasoning_text 而非 _accrue_reasoning：后者要从响应结构里
    # 自行提取（认 reasoning_content / parts.thought / <think> 包裹），
    # 而流式增量本身已是纯思考文本，没有标签也没有响应结构。
    if thought_parts:
        accrue_reasoning_text("".join(thought_parts), ctx, creds["model"])
    text = "".join(parts)
    if not text.strip():
        raise RuntimeError(f"[{ctx or 'chat'}] model={creds['model']} 流式返回空内容")
    return text


def chat_text(
    creds: dict,
    messages: "list[dict]",
    ctx: str = "",
    timeout: float = 90.0,
    default_temp: float = 0.8,
    force_temp: "float | None" = None,
    on_delta: Any = None,
) -> str:
    """统一的文本对话入口：自动在 Gemini 原生端点与 OpenAI 兼容层之间分流。

    - creds["gemini_native"] 为真 → 走 :generateContent（支持 thinkingLevel + 思考摘要）
    - 否则 → 走 OpenAI 兼容 /chat/completions，按 thinking_profile 注入厂商思考方言

    温度优先级：force_temp（调用点硬编码）> creds 配置温度 > default_temp。
    force_temp 用于保持既有行为——小说/游戏/代码等执行 Agent 原本就是写死温度的，
    不应被用户配置悄悄改掉。

    on_delta：可选的思考增量回调。传入后自动切到流式调用，模型吐出的思考
    片段会实时回调给上层推给前端；不传则读 thinking 的 contextvar（pipeline
    通过 _delta_scope 注入），两者都无才走非流式。

    这样上层调用点只需换这一个函数，不必各自判断厂商。
    """
    # timeout 配置优先：creds["timeout"] 覆盖调用点默认值。
    # 卡片 -> 组级 -> global 回退已在 _resolve_models 解析进 creds["timeout"]；
    # 未配置则为 None，沿用调用点传入的默认值（agent 90 / 文本执行 120 等）。
    cfg_to = creds.get("timeout")
    if isinstance(cfg_to, (int, float)) and cfg_to > 0:
        timeout = float(cfg_to)

    # 未显式传回调时，读 pipeline 注入的 contextvar（这样 8 处调用点无需改签名）
    if on_delta is None:
        on_delta = _DELTA_CTX.get()

    if force_temp is not None:
        temp = force_temp
    else:
        temp = creds.get("temperature")
        temp = default_temp if temp is None else temp

    # 包 _model_scope 让 SSE 的 reasoning_start 能带上真实模型名
    # （fallback 可能换模型，这里才知道实际用的是哪张卡片）。
    # 非流式分支也必须在 scope 内：extractor 类调用方（如图片/音频工具）
    # 会在归档思考时读 current_model()，缺了会记成空模型名。
    with _model_scope(creds.get("model")):
        if creds.get("gemini_native"):
            if on_delta is not None:
                text, _thoughts = _gemini_native_chat(
                    creds, messages, temperature=temp, timeout=timeout, on_thought=on_delta,
                )
                return text.strip()
            text, _thoughts = _gemini_native_chat(creds, messages, temperature=temp, timeout=timeout)
            return text.strip()

        if on_delta is not None:
            return _openai_stream_text(creds, messages, temp, ctx, timeout, on_delta).strip()

        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=timeout, max_retries=0)
        resp = client.chat.completions.create(
            model=creds["model"],
            temperature=temp,
            messages=messages,
            **_thinking_kwargs(creds),
            **_max_tokens_kwargs(creds),
        )
        return _first_choice_text(resp, ctx or "chat", creds["model"]).strip()


def _agent_call(agent_key: str, system: str, user: str, mode: dict | None = None) -> str:
    """调用单个文本类 Agent（创意 / 规划）。返回文本。支持多模型按序 fallback。

    模型列表来自该 Agent 的 models 配置；每张卡片的 base_url/api_key/temperature/thinking
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
        return chat_text(
            creds,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            ctx=f"agent:{agent_key}",
            timeout=90,
            default_temp=0.8,
        )

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
    """创建一个简单的 call 闭包，用于两步式生成。温度读卡片配置（缺失时回落 0.6）。"""
    def call(creds: dict) -> str:
        for attempt in range(2):
            try:
                content = chat_text(
                    creds,
                    [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": usr_prompt},
                    ],
                    ctx="文本执行 Agent",
                    timeout=120,
                    default_temp=0.6,
                )
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
    # 终极兜底卡片：global 默认模型。max_tokens/timeout 读 default_*（卡片级兜底语义）。
    _mt = g.get("default_max_tokens")
    if _mt is not None:
        try:
            _mt = int(_mt)
        except (TypeError, ValueError):
            _mt = None
    if isinstance(_mt, int) and _mt <= 0:
        _mt = None
    _to = g.get("default_timeout")
    if _to is not None:
        try:
            _to = float(_to)
        except (TypeError, ValueError):
            _to = None
    if isinstance(_to, (int, float)) and _to <= 0:
        _to = None
    _fb_url = _normalize_base_url(g.get("default_base_url") or g.get("base_url") or "")
    chain.append({
        "model": g.get("default_model", "agnes-free"),
        "base_url": _fb_url,
        "api_key": g.get("default_api_key") or g.get("api_key"),
        "temperature": None,
        "max_tokens": _mt,
        "timeout": _to,
        # 思考配置同样兜底，否则全局配了方言时最后一跳会静默回到默认行为
        "thinking": _normalize_thinking(g.get("default_thinking") or g.get("thinking")),
        "thinking_profile": (str(g.get("default_thinking_profile") or g.get("thinking_profile") or "").strip() or None),
        "thinking_capability": (str(g.get("default_thinking_capability") or g.get("thinking_capability") or "").strip() or "graded"),
        # 兜底卡片的 base_url 也可能是 Gemini 原生端点，漏了会走错接口路径
        "gemini_native": is_gemini_native(_fb_url),
    })
    return chain
