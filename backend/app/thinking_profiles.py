"""思考参数「厂商方言」注册表（OpenAI 兼容协议）。

对齐《多厂商 LLM 思考参数统一适配指南》：前端只表达统一意图
（default / off / low / medium / high，另保留历史 minimal 档），本模块把意图
翻译成各网关实际认的请求体字段。新增厂商只需在 THINKING_PROFILES 加一条。

踩坑要点（指南 §1 / §3 / §7）：
- `thinking` 同名三形状：MiniMax 字符串、腾讯混元对象、NVIDIA NIM 布尔。
  给错形状网关照收 200 并继续思考（静默失效），甚至直接 400，必须按方言隔离。
- 「未设置」(default) 一个思考字段都不发；只有显式 off 才发禁用字段。
  纯 instruct 模型收到 enable_thinking:true 会 400。
- 未知方言抛错，不静默回退——回退得到的是「配了关闭仍在思考」的无信号失效。
- binary / none 能力声明，避免 UI「假分级」以及低档预算把业务输出截断。
- 每条方言返回自己占用的字段名，供将来 extra_body 兜底通道做保护。

兼容策略（与 AI_Draughts 默认值的差异，有意为之）：
1. 默认方言取 effort-only（而非指南推荐的 openai-native）。若默认走 openai-native，
   存量 `thinking: off` 卡片会改发 enable_thinking:false，对 Agnes / 多数聚合网关
   是无效字段（静默失效）。
2. **未配置 thinking_profile 时，`off` 一个思考字段都不发**（与改造前逐字节一致）。
   effort-only 本身用 reasoning_effort:"none" 表达关闭，但只认 low/medium/high 的
   严格网关收到 "none" 会 400——存量卡片不能因为升级而从「能跑」变「报错」。
3. 只有**显式选了方言**的卡片才启用新字段、off 的关闭字段与分级预算。
"""
from __future__ import annotations

from typing import Any, Callable

# 与 agents._THINKING_VALUES 同款（保留 minimal 兼容历史配置）
THINKING_LEVELS = frozenset({"default", "off", "minimal", "low", "medium", "high"})
THINKING_CAPABILITIES = frozenset({"graded", "binary", "none"})

# 未配置方言时的兼容默认（见模块 docstring）
THINKING_PROFILE_DEFAULT = "effort-only"

_EFFORT_BY_LEVEL = {
    "minimal": "minimal", "low": "low", "medium": "medium", "high": "high",
}
# 思考 token 计入 max_tokens，低档预算会把业务输出截断（指南 §7.2）
_BUDGET_BY_LEVEL = {"minimal": 4096, "low": 4096, "medium": 8192, "high": 24576}


def normalize_thinking_level(val: Any) -> str:
    """非法/空值一律降级为 default（不注入任何思考参数）。"""
    t = str(val or "").strip().lower()
    return t if t in THINKING_LEVELS else "default"


def normalize_capability(val: Any) -> str:
    """能力声明非法/空值 → graded（缺省行为）。"""
    t = str(val or "").strip().lower()
    return t if t in THINKING_CAPABILITIES else "graded"


def _guard(spec: dict) -> bool:
    """「未设置」守卫：level 为 null 且未显式关闭时不写任何字段。

    不能用纯 level is None 判断——binary 模型显式 off 的 spec 正是
    {level: None, disable_thinking: True}，此时仍必须发禁用字段。
    """
    return spec.get("level") is None and not spec.get("disable_thinking")


# ---------------------------------------------------------------------------
# 七种 OpenAI 兼容方言。签名：(body, spec) -> 该方言占用的字段名集合
# spec = {level: str|None, disable_thinking: bool, effort: str|None, budget: int|None}
# ---------------------------------------------------------------------------

def _openai_native(body: dict, spec: dict) -> set[str]:
    """Qwen3 / vLLM：顶层 enable_thinking + reasoning_effort。"""
    if _guard(spec):
        return set()
    if spec["disable_thinking"]:
        body["enable_thinking"] = False
    else:
        body["enable_thinking"] = True
        if spec.get("effort"):
            body["reasoning_effort"] = spec["effort"]
    return {"enable_thinking", "reasoning_effort"}


def _chat_template(body: dict, spec: dict) -> set[str]:
    """小红书 Dots / Agnes 3.0：必须嵌进 chat_template_kwargs，顶层会被忽略。"""
    if _guard(spec):
        return set()
    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
        body["chat_template_kwargs"] = kwargs
    kwargs["enable_thinking"] = not spec["disable_thinking"]
    if not spec["disable_thinking"] and spec.get("effort"):
        body["reasoning_effort"] = spec["effort"]
    return {"chat_template_kwargs", "reasoning_effort"}


def _thinking_flag(body: dict, spec: dict) -> set[str]:
    """MiniMax-M3 系：字符串 thinking:'enabled'|'disabled'。"""
    if _guard(spec):
        return set()
    body["thinking"] = "disabled" if spec["disable_thinking"] else "enabled"
    if not spec["disable_thinking"] and spec.get("effort"):
        body["reasoning_effort"] = spec["effort"]
    return {"thinking", "reasoning_effort"}


def _thinking_type(body: dict, spec: dict) -> set[str]:
    """腾讯混元 hy3/hy4：对象 thinking:{type:'enabled'|'disabled'}。

    与 _thinking_flag 同名字段不同形状，互不通用，必须作为独立方言并存。
    """
    if _guard(spec):
        return set()
    body["thinking"] = {"type": "disabled" if spec["disable_thinking"] else "enabled"}
    if not spec["disable_thinking"] and spec.get("effort"):
        body["reasoning_effort"] = spec["effort"]
    return {"thinking", "reasoning_effort"}


def _effort_only(body: dict, spec: dict) -> set[str]:
    """无 thinking 开关，仅 reasoning_effort（'none' 表示关闭）。"""
    if _guard(spec):
        return set()
    body["reasoning_effort"] = "none" if spec["disable_thinking"] else spec.get("effort")
    # effort 为 None 且非禁用时不该出现（level 非 null 时 effort 必有值），兜底删掉
    if body.get("reasoning_effort") is None:
        body.pop("reasoning_effort", None)
    return {"reasoning_effort"}


def _thinking_mode(body: dict, spec: dict) -> set[str]:
    """Intern-S1（chat.intern-ai.org.cn）：布尔 thinking_mode，无强度档。"""
    if _guard(spec):
        return set()
    body["thinking_mode"] = not spec["disable_thinking"]
    return {"thinking_mode"}


def _nvidia(body: dict, spec: dict) -> set[str]:
    """NVIDIA NIM / Nemotron：布尔 thinking，字符串写法实测 400，无强度档。"""
    if _guard(spec):
        return set()
    body["thinking"] = not spec["disable_thinking"]
    return {"thinking"}


THINKING_PROFILES: dict[str, Callable[[dict, dict], set[str]]] = {
    "openai-native": _openai_native,
    "chat-template": _chat_template,
    "thinking-flag": _thinking_flag,
    "thinking-type": _thinking_type,
    "effort-only": _effort_only,
    "thinking-mode": _thinking_mode,
    "nvidia": _nvidia,
}
THINKING_PROFILE_KEYS = frozenset(THINKING_PROFILES)


def apply_thinking_profile(profile_key: Any, body: dict, spec: dict) -> set[str]:
    """把统一意图 spec 翻译成请求体字段；返回该方言占用的字段名集合。

    未配置（None/空串）→ 兼容默认方言；未知键 → 抛 ValueError（指南 §7.5）。
    """
    key = str(profile_key or "").strip() or THINKING_PROFILE_DEFAULT
    fn = THINKING_PROFILES.get(key)
    if fn is None:
        raise ValueError(
            f"未知的思考方言 thinking_profile='{profile_key}'"
            f"（可选值：{', '.join(sorted(THINKING_PROFILES))}）"
        )
    return fn(body, spec)


def resolve_thinking_spec(level: Any, capability: Any = "graded") -> dict:
    """第 1 段：把「档位 + 能力声明」归一成厂商无关描述符。

    - none           → level=None，一个思考参数都不发
    - default        → level=None，跟随模型默认（存量配置零变化）
    - off            → disable_thinking=True（level=None，靠守卫放行）
    - binary 的档位  → 一律归一成 high（满血思考 + high 预算，防截断）
    - graded 的档位  → 原样透传，附带 effort 与预算
    """
    lvl = normalize_thinking_level(level)
    cap = normalize_capability(capability)
    empty = {"level": None, "disable_thinking": False, "effort": None, "budget": None}
    if cap == "none" or lvl == "default":
        return dict(empty)
    if lvl == "off":
        return {"level": None, "disable_thinking": True, "effort": None, "budget": None}
    eff_level = "high" if cap == "binary" else lvl
    return {
        "level": eff_level,
        "disable_thinking": False,
        "effort": _EFFORT_BY_LEVEL.get(eff_level),
        "budget": _BUDGET_BY_LEVEL.get(eff_level),
    }


def thinking_kwargs(creds: dict) -> dict:
    """第 3 段：生成本次 OpenAI 兼容请求要附加的思考参数。

    Gemini 原生端点由 gemini.py 用 thinkingConfig 处理，这里直接返回空。
    """
    if creds.get("gemini_native"):
        return {}
    lvl = normalize_thinking_level(creds.get("thinking"))
    if lvl == "default":
        return {}
    # 存量兼容：未显式配置方言时，off 维持旧行为——一个思考字段都不发。
    # 旧实现对 off 一律返回 {}（理由见模块 docstring：OpenAI 兼容层无通用「关闭」
    # 参数，对只认 low/medium/high 的严格网关硬传 reasoning_effort:"none" 会 400，
    # 存量卡片会从「能跑」变「报错」）。只有显式选了方言的用户才吃到新语义。
    if lvl == "off" and not str(creds.get("thinking_profile") or "").strip():
        return {}
    spec = resolve_thinking_spec(lvl, creds.get("thinking_capability"))
    body: dict = {}
    apply_thinking_profile(creds.get("thinking_profile"), body, spec)
    return body


def thinking_budget(creds: dict) -> int | None:
    """第 2 段：按思考档抬预算，仅在「显式配置了方言」且用户未填 max_tokens 时生效。

    存量配置（未配置 thinking_profile）一律返回 None —— 请求体逐字节不变。
    用户显式填了 max_tokens 时由 _max_tokens_kwargs 优先采用，不会被这里覆盖。
    """
    if creds.get("gemini_native"):
        return None
    if not str(creds.get("thinking_profile") or "").strip():
        return None
    # 用户已填 max_tokens → 用户优先
    if creds.get("max_tokens") is not None:
        return None
    spec = resolve_thinking_spec(creds.get("thinking"), creds.get("thinking_capability"))
    return spec["budget"]
