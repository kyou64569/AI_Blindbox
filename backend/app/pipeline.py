"""盲盒管道核心：随机种子注入 -> 创意 -> (规划) -> 执行。

- 支持 standard（3 Agent）与 lite（2 Agent）两种管道模式。
- 支持 mock 模式：无真实 API key 时用本地规则产出，便于跑通骨架。
- 调用统一 OpenAI 兼容接口（openai 库），模型/温度/key 全部来自配置。
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI, APITimeoutError

from .config import SETTINGS, get_settings


class FallbackExhausted(Exception):
    """所有候选模型都调用失败（fallback 链耗尽）。"""

    def __init__(self, ctx: str, last: Exception | None = None):
        self.ctx = ctx
        self.last = last
        super().__init__(f"[fallback 耗尽] {ctx}: {last}")


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
    """确保 base_url 是标准 OpenAI 兼容端点。

    部分供应商要求走子路径（如 https://api.stepfun.com/step_plan/v1），
    OpenAI client 会在其后追加 /chat/completions —— 这是正确的。

    仅当最后一段 /xxx/v1 的 xxx 不含下划线时，才判定为产品名前缀并截断：
    - https://api.xxx.com/step_plan/v1  → 保留（step_plan 含下划线，是路径）
    - https://api.xxx.com/PRODUCT/v1    → 截断为 /v1（纯字母，是产品名）

    若 URL 不含 /v1 后缀，原样返回。
    """
    if not base_url:
        return base_url
    if not re.search(r'/v1/?$', base_url):
        return base_url
    # 仅当最后一段为「纯字母+可选连字符、不含下划线」时才截断
    return re.sub(r'/([a-zA-Z][a-zA-Z-]*)/v1/?$', r'/v1', base_url.rstrip('/'))


def _call_with_fallback(models: list[dict], call_fn, ctx: str = "") -> "Any":
    """遍历 models 调用 call_fn(creds)。

    仅当 call_fn 抛 Exception 才 fallback 到下一个模型；任一成功即返回（成功即停）；
    全部失败抛 FallbackExhausted。
    """
    import logging as _log
    last: Exception | None = None
    for creds in models:
        try:
            _log.info("[fallback] ctx=%s trying model=%s", ctx, creds["model"])
            result = call_fn(creds)
            _log.info("[fallback] ctx=%s SUCCESS with model=%s", ctx, creds["model"])
            return result, creds["model"]
        except Exception as e:  # noqa: BLE001
            _log.warning("[fallback] ctx=%s FAILED model=%s: %s: %s", ctx, creds["model"], type(e).__name__, str(e)[:200])
            last = e
    raise FallbackExhausted(ctx, last)


@dataclass
class BlindBoxResult:
    seed: str
    concept: str           # Agent1 概念卡
    plan: str              # Agent2 / 精简模式的结构化输出
    output: str            # 成品（文本/路径/URL）
    output_kind: str       # text | image_url | video_file | spec_md
    archived: dict | None = None   # 自动归档信息（result json 路径 + 本地成品路径）
    tts_info: str | None = None   # TTS 实际使用的模型标注（仅播客模式）
    caption: str | None = None    # 配文/标题（表情包/漫画等需要文字叠加的模式）
    image_url: str | None = None  # 角色卡等模式的头像/附图 URL
    model_used: str | None = None   # 最终产出模型名；mock 模式为 "Mock 模拟"；无模型调用为 None
    error: str | None = None        # 回退到 mock 的原因（仅 game 等会静默回退的模式）


def draw_seed() -> str:
    """从 twist pool 随机抽取 per_open_draw 个种子，组合成可复现的 seed 串。"""
    rnd = get_settings().randomness
    if not rnd.get("enabled", True):
        return "seed_fixed"
    n = int(rnd.get("per_open_draw", 2))
    pool = rnd.get("pool", {}) or {}
    picked: list[str] = []
    for dim, items in pool.items():
        if items:
            picked.append(f"{dim}:{random.choice(items)}")
    random.shuffle(picked)
    return "+".join(picked[:n])


# 约束/反转类种子常见的模板前缀，剥离后留下的才是「具体意象」，避免把
# 「必须出现」这类通用前缀误判为锚定（否则「必须出现一面碎镜」会被误杀）。
_TEMPLATE_PREFIXES = (
    "必须出现", "画面中央有", "画面中央有一", "仅用", "只用",
    "含一段", "一只", "一扇", "一个", "一段", "一片",
)


def _core_image(entry: str) -> str:
    """去掉模板前缀，提取条目里的核心具象意象（如『必须出现一只橘猫』→『橘猫』）。

    循环剥离：先去『必须出现』得『一只橘猫』，再遇『一只』得『橘猫』，避免
    残留的通用量词（一只/一扇/一段）继续触发锚定误判。
    """
    s = entry.strip()
    changed = True
    while changed:
        changed = False
        for p in _TEMPLATE_PREFIXES:
            if s.startswith(p):
                s = s[len(p):]
                changed = True
                break
    return s


def _seems_anchored(new: str, existing: list[str]) -> bool:
    """新种子是否仍在沿用已有种子的具体意象（锚定）。

    先剥离已有条目的模板前缀，对核心意象做 2 字滑窗检测：若 new 包含某条核心意象
    的 2 字片段（如「橘猫」），判定为沿用同一具象元素、换汤不换药，应重试。
    """
    new = new or ""
    for e in existing:
        e = e.strip()
        if not e:
            continue
        if e == new.strip():
            return True
        core = _core_image(e)
        if not core:
            continue
        for i in range(len(core) - 1):
            tok = core[i:i + 2]
            if tok and tok in new:
                return True
    return False


# 各维度的中文名（用于拼装给大模型的提示词）
_DIM_CN = {"theme": "主题", "style": "风格", "constraint": "约束", "twist": "反转"}
# mock / 无 key / 调用失败时的兜底池（保证「生成种子」按钮始终可用）
_FALLBACK_POOL = {
    "theme": ["量子禅院", "机械敦煌", "酸性冰川", "废土茶道", "霓虹苗寨", "深海图书馆"],
    "style": ["故障艺术", "胶片颗粒", "低多边形", "浮世绘拼贴", "赛博水墨", "暗黑童话"],
    "constraint": ["必须出现一只白鹭", "只用黑白灰三色", "含一段旁白", "画面中央有一扇门"],
    "twist": ["主角早已不在人世", "故事是倒着发生的", "一切都是一场梦", "观众才是真正的主角"],
}


def generate_pool_seed(dim: str, existing: list[str] | None = None) -> str:
    """为指定维度生成一条「当前配置中没有」的新种子词条。

    - 优先调大模型；提示词**强制要求新种子在具象元素上与已有种子完全不同**，
      避免模型换汤不换药（例如已有「橘猫」却反复生成含橘猫的变体）。
    - 若生成结果仍锚定在已有意象（4 字滑窗命中），自动重试；多次仍锚定或调用失败，
      回退内置兜底池（已避开 existing，且不含橘猫等锚定意象），保证按钮始终可用。
    - 凭据复用 creative Agent 配置，留空回退 global。
    """
    dim_cn = _DIM_CN.get(dim, dim)
    existing = [str(x).strip() for x in (existing or []) if str(x).strip()]

    def _pick_fallback() -> str:
        cands = [c for c in _FALLBACK_POOL.get(dim, ["新种子"]) if c not in existing]
        if not cands:  # 兜底池用尽，直接加序号避免空串
            cands = [f"{dim_cn}种子{len(existing) + 1}"]
        return random.choice(cands)

    settings = get_settings()
    cfg = settings.agents.get("creative", {}) or {}
    g = settings.global_
    models = _resolve_models(cfg, g)

    if (settings.mock
            or not any(c["api_key"] for c in models)
            or all(c["api_key"] == "<USER_KEY>" for c in models)):
        return _pick_fallback()

    system = (
        "你是一个为「AI 盲盒」随机池创作新种子词条的助手。"
        f"请根据维度【{dim_cn}】生成【一条】新颖、具体、有画面感的中文种子词条，"
        "适合作为创意盲盒的随机注入项。"
        "只输出这一条词条本身，不要任何解释、不要引号、不要编号、不要句号结尾。"
        "\n\n重要：新种子必须在【具体意象 / 角色 / 动物 / 物体 / 场景元素】上与已有种子"
        "完全不同——只保持同一维度类别即可，不要沿用已有种子里出现的任何具体意象。"
    )
    forbid_hint = ""
    if existing:
        forbid_hint = (
            "\n\n已有种子（仅供参考，请勿沿用其中的具体角色 / 动物 / 物体，"
            "请引入一个全新的、不同的具象元素）：\n"
            + "\n".join(f"- {e}" for e in existing)
        )
    user = f"请为维度【{dim_cn}】创作一条全新的种子。{forbid_hint}"

    def call(creds: dict) -> str:
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=90, max_retries=0)
        for _attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=creds["model"],
                    temperature=creds["temperature"] if creds["temperature"] is not None else 0.95,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                text = (resp.choices[0].message.content or "").strip()
                text = text.split("\n")[0].strip().strip('"').strip("'").strip("。").strip("、").strip()
                if not text:
                    continue
                if _seems_anchored(text, existing):
                    continue
                return text
            except Exception:
                continue
        raise RuntimeError(f"[seed 生成] model={creds['model']} 多次返回空内容或锚定，触发 fallback")

    out, _used = _call_with_fallback(models, call, ctx=f"generate_pool_seed:{dim}")
    return out


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
        return (resp.choices[0].message.content or "").strip()

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


def _extract_block(text: str, tag: str) -> str:
    """从结构化输出中提取 `[TAG] ...` 块内容（到下一个 `[TAG]` 或文本结尾）。

    表情包 / 漫画等模式的 planner_template 会产生 `[IMAGE]`、`[CAPTION]` 之类的标签块。
    对大小写、中英文括号宽容。找不到返回空串。
    """
    if not text:
        return ""
    pat = re.compile(
        rf"\[[（(]?\s*{re.escape(tag)}\s*[）)]?\]\s*(.*?)(?=\n\s*\[[^\]\n]*\]|\Z)",
        re.S | re.I,
    )
    m = pat.search(text)
    if not m:
        return ""
    return m.group(1).strip()


def _extract_caption(plan: str) -> str | None:
    """提取表情包 / 漫画规划输出里的 `[CAPTION]` 配文；没有则返回 None。"""
    block = _extract_block(plan, "CAPTION")
    return block if block else None


# ------------------------------------------------------------------
# 角色卡辅助
# ------------------------------------------------------------------

_APPEARANCE_EN: dict[str, str] = {
    # 发型
    "短发": "short hair", "长发": "long hair", "马尾": "ponytail", "双马尾": "twintails",
    "散发": "loose hair", "盘发": "updo", "刘海": "bangs", "中分": "center-parted hair",
    # 发色
    "黑发": "black hair", "白发": "white hair", "银发": "silver hair", "金发": "blonde hair",
    "红发": "red hair", "蓝发": "blue hair", "紫发": "purple hair", "粉发": "pink hair",
    "绿发": "green hair", "棕发": "brown hair", "灰发": "grey hair",
    # 眼睛
    "蓝眼": "blue eyes", "红眼": "red eyes", "紫眼": "purple eyes", "金眼": "golden eyes",
    "绿眼": "green eyes", "异色瞳": "heterochromia",
    # 表情
    "微笑": "smiling", "冷峻": "stoic expression", "温柔": "gentle expression",
    "严肃": "serious expression", "傲娇": "tsundere expression",
    "笑": "grinning", "哭": "crying", "怒": "angry expression",
    # 体型 / 身高
    "高": "tall", "矮": "short", "瘦": "slim", "胖": "chubby", "肌肉": "muscular",
    # 服装
    "校服": "school uniform", "西装": "suit", "长衫": "changshan robe",
    "盔甲": "armor", "连衣裙": "dress", "卫衣": "hoodie", "风衣": "trench coat",
    "和服": "kimono", "古装": "traditional chinese clothing", "旗袍": "qipao",
    "制服": "uniform", "运动服": "sportswear",
    # 配饰
    "眼镜": "glasses", "围巾": "scarf", "帽子": "hat", "耳环": "earrings",
    "项链": "necklace", "手套": "gloves", "翅膀": "wings", "角": "horns",
    # 种族 / 特征
    "猫耳": "cat ears", "狐耳": "fox ears", "狼耳": "wolf ears",
    "尾巴": "tail", "龙角": "dragon horns", "精灵耳": "elf ears",
}


def _translate_appearance(appearance_cn: str) -> str:
    """把中文外观描述转为英文图片提示词（模板映射 + 原文兜底）。"""
    text = (appearance_cn or "").strip()
    if not text:
        return "anime character portrait, detailed face, upper body, white background"

    translated: list[str] = []
    remaining = text
    for cn, en in _APPEARANCE_EN.items():
        if cn in remaining:
            translated.append(en)
            remaining = remaining.replace(cn, "", 1)

    # 去掉中文标点和残留连接词，保留英文已翻译部分 + 原文核心词
    remaining = re.sub(r"[，、。；：！？\s]+", " ", remaining).strip()
    if remaining:
        # 原文中有未匹配的部分，尝试保留（可能含模型可理解的中文拼音/英文）
        translated.append(remaining)

    base = ", ".join(translated) if translated else text
    return f"anime portrait, {base}, upper body, detailed face, white background, high quality"


def _extract_field(text: str, field_name: str) -> str:
    """从 `Field: Value` 或 `Field | Value` 格式的结构化文本中提取指定字段值。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # "Name: xxx" 或 "Name | xxx" 或 "【Name】xxx"
        m = re.match(
            rf"(?:【|[\[]?\s*){re.escape(field_name)}(?:】|[\]]?\s*)[:\s|]+(.+)",
            line, re.I,
        )
        if m:
            return m.group(1).strip()
    return ""


def run_blind_box(mode_id: str, seed: str | None = None) -> BlindBoxResult:
    settings = get_settings()
    mode = settings.get_mode(mode_id)
    if mode is None:
        raise ValueError(f"未知模式: {mode_id}")

    seed = seed or draw_seed()
    pipeline_mode = settings.global_.get("pipeline_mode", "standard")

    # 1) 创意 Agent
    creative_prompt = mode.get("creative_prompt", "")
    creative_sys = settings.agents.get("creative", {}).get("system_prompt", "")
    concept_in = f"模式要求：{creative_prompt}\n随机种子：{seed}\n请据此生成概念。"
    # 静态图模式：约束概念只描述单帧静态画面，避免承诺「动态/动画/运动」等
    # 成品（图片）根本无法表现的内容，否则会出现「概念卡写动态、成品却是静图」的脱节。
    # 仅对 image 类模式生效，视频模式本就能表现动态，不受影响。
    if mode.get("executor_type") == "image":
        concept_in += (
            "\n\n重要约束：本模式最终产出为【单张静态图片】（不是 GIF / 视频）。"
            "请只描述可被单帧静态画面表现的内容——构图、角色、表情、姿态、色彩、氛围、光影；"
            "不要描述动态效果、运动、动画、眨眼、飘动、弹跳、转场等仅能在视频或 GIF 中表现的内容。"
        )
    concept = _agent_call("creative", creative_sys, concept_in)

    executor_type = mode.get("executor_type", "text")

    # 2) 规划 Agent（standard）或直接出结构化（lite）
    if pipeline_mode == "standard":
        # Text modes (novel, poem, character, naming, puzzle) skip the planner —
        # the creative agent already produces a complete concept. Running it through
        # the low-temperature planner would turn it into a degraded outline.
        if executor_type in ("text",):
            plan = concept  # pass concept directly as plan, skip the planner
        else:
            planner_sys = settings.agents.get("planner", {}).get("system_prompt", "")
            planner_tpl = mode.get("planner_template", "")
            plan_in = f"创意概念：\n{concept}\n\n请按以下格式收敛：\n{planner_tpl}"
            dur = mode.get("duration_seconds")
            if dur:
                plan_in += (
                    f"\n（系统已锁定本视频总时长约为 {dur} 秒，请据此规划分镜与标注，"
                    f"不要自行更改总时长。）"
                )
            plan = _agent_call("planner", planner_sys, plan_in, mode=mode)
    else:
        # lite：让创意 Agent 一段式输出结构化（这里用 mock 时直接复用）
        plan = concept if settings.mock else concept

    # 3) 执行 Agent
    output_kind = mode.get("output_handler", "text")

    result = BlindBoxResult(
        seed=seed,
        concept=concept,
        plan=plan,
        output="",
        output_kind=output_kind,
    )

    # 表情包 / 漫画等结构化模式：规划输出含 [IMAGE] / [CAPTION] 标签块。
    # 1) 提取 [CAPTION] 配文，单独返回供前端叠加到成品图片上（图片本身不画字）；
    # 2) 若含 [IMAGE] 块，把纯净的图片提示词传给 image 执行器，避免 [CAPTION] 等
    #    结构化标签混入提示词、也让图上有内容明确的文字来源。
    result.caption = _extract_caption(plan)
    exec_plan = plan
    if result.caption:
        img_block = _extract_block(plan, "IMAGE")
        if img_block:
            exec_plan = img_block

    result.output = _run_executor(executor_type, exec_plan, mode, result)
    # 4) 自动归档：落盘元数据 + 把易失的远程产物下载到本地
    try:
        result.archived = archive_result(result, mode_id)
    except Exception:
        # 归档失败不应阻断开盒主流程
        result.archived = None
    return result


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
    elif executor_type in ("song",):
        preferred_key = None  # 文本生成走 executor agent，TTS 在函数内单独处理

    # 优先取对应 provider 的模型列表
    if preferred_key:
        pv = settings.providers.get(preferred_key) or {}
        pm = _resolve_models(pv, g)
        if pm:
            return pm

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
        # exec_models 在链首（game 可用的执行器专属模型），然后 creative/planner 兜底
        for creds in (exec_models or []):
            if creds["model"] not in {c["model"] for c in chain}:
                chain.append(creds)
        for ak in ("creative", "planner"):
            ag = settings.agents.get(ak) or {}
            m = (ag.get("model") or "").strip()
            if not m or m in {c["model"] for c in chain}:
                continue
            chain.append({
                "model": m,
                "base_url": _normalize_base_url(ag.get("base_url") or g.get("default_base_url") or ""),
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


def _slugify(text: str, max_len: int = 40) -> str:
    """从提示词/文本中提取一个文件系统友好的短文件名（保留中英文，去除非法字符）。

    用于把本地成品命名为可读形式（而非随机哈希）。保留中文/英文/数字与 _ - ，
    去掉 Windows 非法字符与首尾的 . 空格，并确保非空。
    """
    if not text:
        return "untitled"
    s = re.sub(r"\s+", " ", text.strip())
    # Windows 文件名非法字符：< > : " / \ | ? * 以及控制字符
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = s.strip(" .")              # 去掉首尾 . 与空格（Windows 不允许以这些结尾）
    s = re.sub(r"\s+", "_", s)     # 连续空白压缩为单个下划线
    if len(s) > max_len:
        s = s[:max_len].rstrip("_.")
    s = s.strip("_.")
    return s or "untitled"


def _run_executor(executor_type: str, plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None) -> str:
    """按执行类型产出成品。

    - text：规划输出即正文（真实环境可再调一次文本模型扩写，这里直接返回）。
    - image：调用 providers.image 配置的图像模型（OpenAI 兼容 images.generate）。
    - video：本期 mock 占位，待接入视频生成 API。
    - code-spec：生成 spec.md 落盘 D 盘 output/。
    - code-cli：本期仅预留接口。
    """
    if executor_type == "text":
        models = _executor_creds("text")
        concept = result.concept if result else ""
        settings = get_settings()
        has_key = any(c["api_key"] for c in models) and not all(
            c["api_key"] == "<USER_KEY>" for c in models
        )
        if settings.mock or not has_key:
            mode_name = (mode or {}).get("name", "微小说")
            if result is not None:
                result.model_used = "Mock 模拟"
            return (
                f"【{mode_name}正文成品（Mock）】\n\n"
                "玻璃穹顶外的酸雨落了整整七个世纪。在这个被废弃的温室中央，机械橘猫的胸膛里正发出微弱的齿轮嗡鸣。\n\n"
                "它抬起由钛合金与旧时代电路板构成的爪子，小心翼翼地抚过玻璃罩里最后一株发光的记忆幼苗。"
                "种子在光晕中舒展叶片，映照出人类文明沉睡前的最后景象——那是一片湛蓝的天空与无边无际的麦浪。\n\n"
                "“我们种下的不是花，是时间。”机械橘猫的音频模块发出沙哑的电子音，随后轻轻合上了光学观测仪。"
            )

        system_prompt = (
            "你是一位优秀的主笔小说家。你的任务是根据给定的灵感概念与构思大纲，"
            "独立创作并撰写一篇语言优美、富有叙事张力和感染力的完整文章/小说正文。"
            "\n【严禁提示】：\n"
            "1. 绝对不要在回答中输出“大纲”、“输出大纲”、“正文写作要求”、“标题”等结构标题或编号列表！\n"
            "2. 绝对不要重复用户给出的规划内容！\n"
            "3. 必须直接从正文第一句开始创作，只输出小说/文学作品的正文内容本身。"
        )
        user_prompt = (
            f"请根据以下概念与大纲要求，为我创作一篇完整的文学正文（300-800字）：\n\n"
            f"【故事灵感】：\n{concept}\n\n"
            + (f"【故事构思与情节要点】：\n{plan}\n\n" if plan.strip() != concept.strip() else "")
            + "请直接开始撰写正文（切勿输出任何大纲说明）："
        )

        def call(creds: dict) -> str:
            client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=90, max_retries=0)
            # 单模型内部：最多 2 次尝试（含大纲校验），异常或校验不通过则触发跨模型 fallback
            local_prompt = user_prompt
            for attempt in range(2):
                resp = client.chat.completions.create(
                    model=creds["model"],
                    temperature=0.85,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": local_prompt},
                    ],
                )
                content = (resp.choices[0].message.content or "").strip()
                # 校验：若模型误将大纲原封不动抄回或仍以「输出大纲」开头，自动提示并纠正重试
                if content == plan.strip() or content.startswith("输出大纲") or content.startswith("**输出大纲**"):
                    if attempt == 0:
                        local_prompt += "\n\n【系统提醒】：上一次回答误输出了大纲。请注意，绝对不要输出大纲或要点，请立刻直接输出完整的小说正文内容！"
                        continue
                    break
                return content
            # 同一模型两次都只抄大纲 -> 抛异常触发跨模型 fallback
            raise RuntimeError(
                f"[文本执行 Agent] model={creds['model']} 两次均仅返回大纲，内容校验未通过"
            )

        out, used = _call_with_fallback(models, call, ctx="executor:text")
        if result is not None:
            result.model_used = used
        return out

    # ------------------------------------------------------------------
    # 角色卡：文本模型生成角色设定 → 提取外观 → 图片模型生成动漫头像
    # ------------------------------------------------------------------
    if executor_type == "character_card":
        settings = get_settings()
        text_models = _executor_creds("text")

        # ---- 1) 文本模型：生成角色设定 ----
        char_text = plan  # 兜底：若文本模型调用失败，直接用 plan 作为输出

        has_text_key = any(c["api_key"] for c in text_models) and not all(
            c["api_key"] == "<USER_KEY>" for c in text_models
        )
        if not settings.mock and has_text_key:
            system_prompt = (
                "你是一位出色的角色设计师。请根据给定的角色概念，"
                "输出一份结构化的角色设定卡。严格按以下格式，每行一个字段：\n"
                "Name: 角色名\n"
                "Appearance: 外观描述（中文，具体到发型/发色/眼睛/体型/服装，越详细越好）\n"
                "Personality: 性格特征\n"
                "Backstory: 背景故事\n"
                "Catchphrase: 经典台词\n"
                "Hidden Trait: 隐藏特质\n"
                "\n只输出这六个字段，不要任何解释、标题或多余文字。"
            )
            user_prompt = f"请基于以下角色概念，生成结构化的角色设定卡：\n\n{plan}"

            def _char_call(creds: dict) -> str:
                client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"],
                                timeout=90, max_retries=0)
                resp = client.chat.completions.create(
                    model=creds["model"],
                    temperature=0.85,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                return (resp.choices[0].message.content or "").strip()

            try:
                char_text, txt_used = _call_with_fallback(
                    text_models, _char_call, ctx="executor:character_card:text",
                )
                if result is not None:
                    result.model_used = txt_used
            except FallbackExhausted as e:
                # 文本模型全部失败，用 plan 兜底，但记录错误原因
                if result is not None:
                    result.error = f"角色卡文本生成失败：{e.last if e.last else '所有模型调用失败'}"
                pass

        # ---- 2) 提取 Appearance 字段 → 翻译为英文图片提示词 ----
        appearance_cn = _extract_field(char_text, "Appearance")
        img_prompt = _translate_appearance(appearance_cn)

        # ---- 3) 图片模型：生成动漫头像 ----
        img_models = _executor_creds("image")
        has_img_key = any(c["api_key"] for c in img_models) and not all(
            c["api_key"] == "<USER_KEY>" for c in img_models
        )

        if settings.mock or not has_img_key:
            # mock / 无图片 key → 仅返回文字，不阻断
            if result is not None:
                result.image_url = None
                if result.model_used is None:
                    result.model_used = "Mock 模拟" if settings.mock else None
            return char_text

        def _img_call(creds: dict) -> str:
            client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"],
                            timeout=180, max_retries=0)
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    resp = client.images.generate(
                        model=creds["model"],
                        prompt=img_prompt,
                        size="1024x1024",
                        n=1,
                    )
                    item = resp.data[0]
                    if getattr(item, "url", None):
                        return item.url
                    if getattr(item, "b64_json", None):
                        import base64
                        out_dir = SETTINGS.output_dir
                        img_path = out_dir / f"char_{_slugify(appearance_cn)}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(base64.b64decode(item.b64_json))
                        return str(img_path)
                    raise RuntimeError(
                        f"[角色卡图片生成失败] model={creds['model']} 返回了空的图片数据（无 url / b64_json）"
                    )
                except APITimeoutError as e:
                    last_err = e
                    time.sleep(3)
                    continue
                except Exception as e:
                    last_err = e
                    break
            raise RuntimeError(
                f"[角色卡图片生成失败] model={creds['model']}\n"
                f"错误：{type(last_err).__name__}: {last_err}"
            )

        try:
            img_out, img_used = _call_with_fallback(
                img_models, _img_call, ctx="executor:character_card:image",
            )
            if result is not None:
                result.image_url = img_out if img_out else None
                # 图片模型成功时补充 model_used（文本模型的已设置，这里不覆盖）
        except FallbackExhausted:
            # 图片模型全部失败 → 降级为纯文字，不阻断
            if result is not None:
                result.image_url = None

        return char_text

    if executor_type == "image":
        models = _executor_creds("image")
        settings = get_settings()
        has_key = any(c["api_key"] for c in models) and not all(
            c["api_key"] == "<USER_KEY>" for c in models
        )
        if settings.mock or not has_key:
            if result is not None:
                result.model_used = "Mock 模拟"
            return ("[mock 图片 生成占位] 配置好 providers.image 的真实 base_url/api_key/model "
                    f"并关闭 mock 后，将调用图像模型生成图片。\nplan={plan[:80]}")
        # 注意：该共享图像 API 延迟抖动很大（实测 18~50s 常见，偶发 >150s 超时）。
        # 因此把单次超时放宽到 180s，并对「超时」做最多 2 次重试；非超时错误
        # （鉴权/参数错误/模型不存在）不重试，直接抛出以暴露真实问题。
        # 总耗时上限约 3×180s=540s，仍在代理层(600s)与前端的兜底超时(580s)之内。

        def call(creds: dict) -> str:
            client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=180, max_retries=0)
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    resp = client.images.generate(
                        model=creds["model"],
                        prompt=plan,
                        size="1024x1024",
                        n=1,
                    )
                    # 兼容 url / b64_json 两种返回
                    item = resp.data[0]
                    if getattr(item, "url", None):
                        return item.url
                    if getattr(item, "b64_json", None):
                        # 存为 D 盘文件（体积较大，落 output，不落 C 盘）
                        import base64
                        out_dir = SETTINGS.output_dir
                        img_path = out_dir / f"{_slugify(plan)}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(base64.b64decode(item.b64_json))
                        return str(img_path)
                    raise RuntimeError(
                        f"[图片执行 Agent] model={creds['model']} 返回了空的图片数据（无 url / b64_json）"
                    )
                except APITimeoutError as e:
                    # 图像 API 延迟尖峰，重试（多数情况下重抽会落回正常区间）
                    last_err = e
                    time.sleep(3)
                    continue
                except Exception as e:
                    # 鉴权失败 / 参数错误 / 模型不存在等：不重试，直接抛出
                    last_err = e
                    break
            raise RuntimeError(
                f"[图片执行 Agent 调用失败] model={creds['model']}  base_url={creds['base_url']}\n"
                f"错误：{type(last_err).__name__}: {last_err}\n"
                "注意：图片生成必须使用支持 images/generations 接口的模型（如 DALL·E、Midjourney、Stable Diffusion 等）。"
                "请确认该模型已配置在「图片模型」（providers.image）中，而非仅配置在 Agent3 执行模型。"
            )

        out, used = _call_with_fallback(models, call, ctx="executor:image")
        if result is not None:
            result.model_used = used
        return out

    if executor_type == "tts":
        path, info = _run_tts(plan, mode, result)
        if result is not None:
            result.tts_info = info
        return path

    if executor_type == "video":
        models = _executor_creds("video")
        settings = get_settings()
        has_key = any(c["api_key"] for c in models) and not all(
            c["api_key"] == "<USER_KEY>" for c in models
        )
        if settings.mock or not has_key:
            if result is not None:
                result.model_used = "Mock 模拟"
            return ("[mock 视频 生成占位] 真实环境将调用 providers.video 的模型。\n"
                    f"plan={plan[:60]}...")

        def call(creds: dict) -> str:
            # Agnes 视频为异步：POST /v1/videos 建任务 → 轮询 GET /agnesapi?video_id= 取结果
            import httpx
            from urllib.parse import urlparse
            parsed = urlparse(creds["base_url"])
            root = f"{parsed.scheme}://{parsed.netloc}"
            headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
            create_url = creds["base_url"].rstrip("/") + "/videos"
            # 时长由配置驱动：把 duration_seconds 换算成满足「8n+1」约束的 num_frames，
            # 使规划文案里的时长与成品真实时长一致。缺省回退 5 秒（= 原硬编码 121 帧）。
            frame_rate = 24
            max_frames = 433  # 约 18s@24fps，作为部分视频模型帧数上限的保护
            dur = (mode or {}).get("duration_seconds") or 5
            num_frames = int(round(dur * frame_rate)) + 1
            num_frames = max(9, min(max_frames, num_frames))
            if (num_frames - 1) % 8 != 0:  # 调成 8n+1
                num_frames = ((num_frames - 1) // 8) * 8 + 1
            import logging
            if num_frames >= max_frames:
                logging.warning(
                    "视频时长 %ss 超出帧数上限，已截断到约 %.1fs",
                    dur, (num_frames - 1) / frame_rate,
                )
            body = {
                "model": creds["model"],
                "prompt": plan,
                "height": 768,
                "width": 1152,
                "num_frames": num_frames,   # 需满足 8n+1
                "frame_rate": frame_rate,
            }
            with httpx.Client(timeout=60) as hc:
                cr = hc.post(create_url, json=body, headers=headers)
            if cr.status_code not in (200, 201):
                raise RuntimeError(f"创建视频任务返回 HTTP {cr.status_code}：{cr.text[:200]}")
            cdata = cr.json()
            vid = cdata.get("video_id") or cdata.get("id") or cdata.get("task_id")
            if not vid:
                raise RuntimeError(f"创建视频任务未返回 id：{str(cdata)[:200]}")
            # 轮询结果（最多约 5 分钟；前端代理层已放宽到 10 分钟，避免长连接被掐断导致 "Failed to fetch"）
            poll_url = f"{root}/agnesapi?video_id={vid}"
            for _ in range(60):
                time.sleep(5)
                with httpx.Client(timeout=30) as hc:
                    pr = hc.get(poll_url, headers={"Authorization": f"Bearer {creds['api_key']}"})
                if pr.status_code != 200:
                    continue
                pdata = pr.json()
                status = pdata.get("status")
                if status == "completed":
                    v = pdata.get("url") or pdata.get("video_url")
                    if v:
                        return v
                    return f"[视频已完成但未取到 URL] {str(pdata)[:200]}"
                if status == "failed":
                    raise RuntimeError(f"视频生成失败：{pdata.get('error')}")
            return f"[视频仍在生成中] 任务 id={vid}，可稍后访问查询：{poll_url}"

        out, used = _call_with_fallback(models, call, ctx="executor:video")
        if result is not None:
            result.model_used = used
        return out

    if executor_type == "code-spec":
        # 生成 spec.md，落到 output 目录（D 盘）
        out_dir = SETTINGS.output_dir
        spec_path = out_dir / f"spec_{_slugify(plan)}_{uuid.uuid4().hex[:6]}.md"
        spec_path.write_text(f"# 盲盒代码需求\n\n{plan}\n", encoding="utf-8")
        return str(spec_path)

    if executor_type == "code-cli":
        # 本期仅预留接口，不实现
        return "[预留接口] code-cli 重档本期未实现。"

    if executor_type == "game":
        return _run_game_executor(plan, mode, result)

    if executor_type == "illustrated_story":
        return _run_illustrated_story(plan, mode, result)

    if executor_type == "song":
        return _run_song(plan, mode, result)

    return plan


def _extract_html(content: str) -> str:
    """从模型输出中剥离 ```html ... ``` 围栏，返回纯 HTML 源码；若明显不是 HTML 返回空串。"""
    c = (content or "").strip()
    m = re.search(r"```(?:html)?\s*([\s\S]*?)\s*```", c, flags=re.I)
    if m:
        c = m.group(1).strip()
    c = re.sub(r"^```(?:html)?\s*", "", c, flags=re.I)
    c = re.sub(r"\s*```$", "", c).strip()
    # 必须是 HTML（含 <html / <!DOCTYPE / <canvas / <script 之一）才视为有效
    if re.search(r"<!DOCTYPE|<html|<canvas|<script", c, flags=re.I):
        return c
    return ""


def _mock_game_html(plan: str) -> str:
    """mock / 无 key / 生成失败时的兜底：产出一份确实可玩的单文件 HTML 小游戏，
    避免把「需求提示词」误当成品返回（本项目历史 bug 的根因）。

    不同 plan 会产生颜色主题、难度略有差异的变体，避免每次开盒拿到完全相同的 HTML。
    支持键盘（方向键 / WASD 移动，空格键收集）和鼠标两种操作。
    """
    name = "AI 盲盒小游戏"
    m = re.search(r"【游戏名】\s*([^\n【]+)", plan)
    if m:
        name = m.group(1).strip() or name

    # 基于 plan 内容生成不同的游戏参数，避免每次完全相同的 HTML
    plan_hash = hash(plan) % 100
    themes = [
        { "bg": "#0f172a", "player": "#fde047", "star": "#f472b6", "accent": "#38bdf8" },
        { "bg": "#1a1a2e", "player": "#e94560", "star": "#0f3460", "accent": "#f5a623" },
        { "bg": "#0d1117", "player": "#f78166", "star": "#7ee787", "accent": "#58a6ff" },
        { "bg": "#1e1e2e", "player": "#cba6f7", "star": "#a6e3a1", "accent": "#f38ba8" },
    ]
    t = themes[plan_hash % len(themes)]
    star_life = 70 + (plan_hash % 50)       # 70-119
    spawn_interval = max(20, 35 - plan_hash % 15)  # 20-35
    speed = 3.5 + (plan_hash % 3)           # 3.5-5.5

    template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>__TITLE__</title>
<style>
  html, body {{ width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; background: {t['bg']}; color: #e2e8f0; font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; }}
  #wrap {{ width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: flex-start; box-sizing: border-box; padding: 8px; }}
  h1 {{ margin: 4px 0 2px; font-size: clamp(14px, 3.5vw, 22px); text-align: center; }}
  #hud {{ font-size: clamp(11px, 2.8vw, 15px); margin-bottom: 6px; text-align: center; }}
  #game-box {{ flex: 1 1 auto; min-height: 0; width: 100%; display: flex; align-items: center; justify-content: center; }}
  #game {{ display: block; background: #000; border-radius: 10px; touch-action: none; cursor: pointer; max-width: 100%; max-height: 100%; }}
  button {{ margin-top: 8px; padding: 6px 18px; border: 0; border-radius: 8px; background: {t['accent']}; color: #04263a; font-weight: 700; cursor: pointer; font-size: clamp(12px, 2.5vw, 15px); }}
</style>
</head>
<body>
<div id="wrap">
  <h1>__TITLE__</h1>
  <div id="hud">得分：<span id="score">0</span> · 生命：<span id="lives">3</span> · 键盘：方向键/WASD 移动，空格键收集 · 鼠标：点击星星收集</div>
  <div id="game-box"><canvas id="game"></canvas></div>
  <button id="reset">重新开始</button>
</div>
<script>
  const cv = document.getElementById('game');
  const ctx = cv.getContext('2d');
  const scoreEl = document.getElementById('score');
  const livesEl = document.getElementById('lives');
  let score = 0, lives = 3;
  let W = 360, H = 480;
  const keys = {{}};
  let player = {{ x: 0, y: 0, r: 14, vx: 0, vy: 0 }};
  let star = null;
  let frame = 0;
  const SPEED = {speed};
  const STAR_LIFE = {star_life};
  const SPAWN_INTERVAL = {spawn_interval};

  function resize() {{
    const box = document.getElementById('game-box');
    const maxW = box.clientWidth - 4;
    const maxH = box.clientHeight - 4;
    let w = maxW, h = Math.round(w * 4 / 3);
    if (h > maxH) {{ h = maxH; w = Math.round(h * 3 / 4); }}
    W = w; H = h;
    cv.width = W; cv.height = H;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    if (!star) spawn();
    player.x = Math.min(Math.max(player.r, player.x), W - player.r);
    player.y = Math.min(Math.max(player.r, player.y), H - player.r);
  }}

  function spawn() {{
    const margin = 30;
    star = {{
      x: margin + Math.random() * (W - margin * 2),
      y: margin + Math.random() * (H - margin * 2),
      r: Math.max(10, Math.min(W, H) * 0.045),
      life: STAR_LIFE,
      maxLife: STAR_LIFE,
    }};
  }}

  function update() {{
    frame++;
    // 键盘输入
    player.vx = (player.vx || 0);
    player.vy = (player.vy || 0);
    if (keys['ArrowLeft'] || keys['a'] || keys['A']) player.vx = -SPEED;
    else if (keys['ArrowRight'] || keys['d'] || keys['D']) player.vx = SPEED;
    else player.vx = 0;
    if (keys['ArrowUp'] || keys['w'] || keys['W']) player.vy = -SPEED;
    else if (keys['ArrowDown'] || keys['s'] || keys['S']) player.vy = SPEED;
    else player.vy = 0;
    // 斜向速度归一化
    if (player.vx !== 0 && player.vy !== 0) {{
      const k = 1 / Math.sqrt(2);
      player.vx *= k; player.vy *= k;
    }}
    player.x += player.vx;
    player.y += player.vy;
    player.x = Math.min(Math.max(player.r, player.x), W - player.r);
    player.y = Math.min(Math.max(player.r, player.y), H - player.r);

    // 星星生命周期
    if (star) {{
      star.life--;
      if (star.life <= 0) {{
        star = null;
        lives = Math.max(0, lives - 1);
        livesEl.textContent = lives;
        if (lives <= 0) {{
          alert('游戏结束！得分：' + score);
          score = 0; lives = 3;
          scoreEl.textContent = score;
          livesEl.textContent = lives;
        }}
      }}
    }}
    // 自动刷新星星
    if (!star && frame % SPAWN_INTERVAL === 0) spawn();

    // 碰撞检测（键盘 / 鼠标通用）
    if (star) {{
      const dx = player.x - star.x, dy = player.y - star.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < player.r + star.r - 2) {{
        score++;
        scoreEl.textContent = score;
        star = null;
      }}
    }}
  }}

  function draw() {{
    ctx.clearRect(0, 0, W, H);
    // 星星
    if (star) {{
      const alpha = 0.35 + 0.65 * (star.life / star.maxLife);
      ctx.globalAlpha = Math.max(0.2, alpha);
      ctx.fillStyle = '{t['star']}';
      ctx.beginPath();
      ctx.arc(star.x, star.y, star.r, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }}
    // 玩家
    ctx.fillStyle = '{t['player']}';
    ctx.beginPath();
    ctx.arc(player.x, player.y, player.r, 0, Math.PI * 2);
    ctx.fill();
    // 边框
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.lineWidth = 2;
    ctx.stroke();
  }}

  // 键盘
  window.addEventListener('keydown', e => {{
    keys[e.key] = true;
    if (e.key === ' ' || e.code === 'Space') e.preventDefault();
  }});
  window.addEventListener('keyup', e => {{ keys[e.key] = false; }});

  // 鼠标 / 触摸：点击画布时把玩家瞬移到点击位置（简化操作）
  cv.addEventListener('pointerdown', e => {{
    const r = cv.getBoundingClientRect();
    player.x = (e.clientX - r.left) * (cv.width / r.width);
    player.y = (e.clientY - r.top) * (cv.height / r.height);
    player.x = Math.min(Math.max(player.r, player.x), W - player.r);
    player.y = Math.min(Math.max(player.r, player.y), H - player.r);
  }});

  document.getElementById('reset').onclick = () => {{
    score = 0; lives = 3; star = null; frame = 0;
    scoreEl.textContent = score; livesEl.textContent = lives;
    player.x = W / 2; player.y = H - 40; spawn();
  }};
  window.addEventListener('resize', resize);
  resize();
  player.x = W / 2; player.y = H - 40;
  spawn();
  (function loop() {{ update(); draw(); requestAnimationFrame(loop); }})( );
</script>
</body>
</html>"""
    return template.replace("__TITLE__", name)


def _run_game_executor(plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None) -> str:
    """小游戏模式：基于规划出的游戏需求(plan)，用代码模型生成一份可直接在浏览器运行的
    单文件 HTML5 小游戏（HTML+CSS+JS 内联），落盘 output/ 并返回绝对路径。支持多模型按序 fallback。

    - 真实环境：调用 executor Agent（代码模型）按严格 system 指令产出完整、可玩的 HTML；
      解析时剥离 ```html 围栏，确保落盘的是纯 HTML。单模型内部重试 2 次属于「单模型内部重试」，
      与跨模型 fallback 是两回事——跨模型交给 _call_with_fallback。
    - mock / 无 key / 所有模型均失败：落一份内置的、确实可玩的占位小游戏，保证开盒不中断、
      且不再把「需求提示词」误当成品。
    """
    out_dir = SETTINGS.output_dir
    slug = _slugify(plan, max_len=40)

    settings = get_settings()
    models = _executor_creds("game")

    # Debug: log resolved models for troubleshooting
    import logging as _log
    _log.warning("[game executor] resolved %d models:", len(models))
    for i, m in enumerate(models):
        _log.warning("  [%d] model=%s base_url=%s", i, m["model"], m["base_url"])

    def _save(html: str) -> str:
        dest = out_dir / f"game_{slug}_{uuid.uuid4().hex[:6]}.html"
        dest.write_text(html, encoding="utf-8")
        return str(dest)

    has_key = any(c["api_key"] for c in models) and not all(
        c["api_key"] == "<USER_KEY>" for c in models
    )
    if settings.mock or not has_key:
        # mock / 无 key：直接落占位可玩游戏
        if result is not None:
            result.model_used = "Mock 模拟"
        return _save(_mock_game_html(plan))

    system_prompt = (
        "你是一位资深前端游戏工程师。请基于给定的小游戏需求，产出一份"
        "【完整、可直接在浏览器打开即玩】的单文件 HTML5 小游戏。\n"
        "【硬性要求】：\n"
        "1. 只输出一个 HTML 文件，内含 <style> 与 <script>，不要任何外部依赖（不引用 cdn / 图片 / 字体文件）。\n"
        "2. 游戏必须真实可玩：有清晰目标、可操作的玩法（鼠标点击 / 键盘均可）、计分或胜负判定、重新开始按钮。\n"
        "3. 使用 Canvas 或纯 DOM 均可，画面美观、配色符合需求里的风格描述。\n"
        "4. 代码必须自包含、无语法错误、无未定义变量，打开即可运行。\n"
        "5. 中文界面（标题、提示、得分、按钮均用中文）。\n"
        "6. 绝对不要输出任何解释、不要 Markdown 标题、不要把代码包在 ```html 围栏里——直接输出 HTML 源码本身。\n"
        "7. 游戏页面必须完全自适应 iframe 容器，不得出现滚动条：html/body 必须 100% 宽高、overflow:hidden；"
        "所有 UI 元素必须使用相对单位或根据窗口 resize 动态计算，确保在 360×420 到全屏之间都能完整显示且不溢出。\n"
    )
    user_prompt = (
        f"请基于以下小游戏需求，生成完整可玩的单文件 HTML5 小游戏源码：\n\n{plan}\n\n"
        "（请直接开始输出 <!DOCTYPE html>，不要任何前后缀说明。）"
    )

    def call(creds: dict) -> str:
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=180, max_retries=0)
        # 单模型内部：最多 2 次尝试（内容为空则重试），异常或仍无 HTML 则触发跨模型 fallback
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=creds["model"],
                    temperature=0.7,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = (resp.choices[0].message.content or "").strip()
                html = _extract_html(content)
                if html:
                    return _save(html)
                last_err = RuntimeError("模型未返回可用的 HTML 内容")
                if attempt == 0:
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(2)
                    continue
                break
        # 该模型两次均失败 -> 抛异常触发跨模型 fallback
        raise RuntimeError(f"game 模型 {creds['model']} 生成失败: {last_err}")

    # 所有模型都失败 -> 落一份占位可玩游戏，保证开盒不中断
    try:
        out, used = _call_with_fallback(models, call, ctx="executor:game")
        if result is not None:
            result.model_used = used
        return out
    except FallbackExhausted:
        if result is not None:
            result.model_used = "Mock 模拟"
        return _save(_mock_game_html(plan))


def _run_tts(plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None) -> tuple[str, str]:
    """文本转语音：把规划出的播客台词合成为音频文件，落盘 output/ 并返回 (绝对路径, 使用信息)。

    路由规则（不再有 engine 字段，纯由凭据决定）：
    - 未同时配置 base_url 与 api_key → 免费 Edge TTS（无需 Key，需联网到微软端点）。
    - 同时配置了 base_url 与 api_key → 遍历 providers.tts.models（按序 fallback）走云端
      OpenAI 兼容大模型（models 为空则直接回退 Edge）。每张卡片的 base_url/api_key
      为空时回退组级再到 global；voice 仍为组级共用字段（卡片不单独配 voice）。
      云端容错链：① 用配置的音色（未配置则用通用默认 alloy）② 指定音色出错则去掉 voice
      字段再调一次（依赖厂商默认音色）③ 仍失败才自动回退到免费 Edge TTS，保证开盒不中断。
    「使用信息」会在结果/历史中标注实际使用的模型，便于溯源。
    """
    prov = SETTINGS.providers.get("tts", {}) or {}
    voice = (prov.get("voice") or "").strip()
    raw_text = (plan or "").strip()
    if not raw_text:
        return ("[TTS 输入为空，无法合成]", "（无音频）")

    # Clean TTS text: strip atmosphere markers, convert pauses to actual gaps
    text = raw_text
    # 1) Remove BGM markers — they're sound design cues, not spoken content
    text = re.sub(r'\[BGM\s*[:：][^\]]*\]', '', text, flags=re.I)
    text = re.sub(r'\[背景音[^\]]*\]', '', text, flags=re.I)
    # 2) Remove narrator/主持人 markers
    text = re.sub(r'\[主持人[^\]]*\]', '', text, flags=re.I)
    # 3) Convert [pause Ns] / [停顿 Ns] to SSML breaks where possible,
    #    otherwise insert ellipsis pauses
    # Convert [pause Ns] / [停顿 Ns] to text pauses (comma + ellipsis)
    # SSML <break> support varies across TTS providers, so we use plain text pauses
    text = re.sub(
        r'\[(?:pause|停顿|暂停)\s*(\d+)s?\]',
        lambda m: ', …' * min(5, max(1, int(m.group(1)) // 2)),
        text, flags=re.I,
    )
    # Collapse consecutive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    out_dir = get_settings().output_dir

    base_url = (prov.get("base_url") or "").strip()
    api_key = (prov.get("api_key") or "").strip()
    has_cloud_creds = bool(base_url and api_key)

    # Mock 模式短路：与 text/image/video/game 四个 executor 对齐，离线返回占位音频，
    # 不触发任何真实云端调用，也不回退到需要联网的免费 Edge TTS（避免 mock 模式下仍产生
    # 真实网络/费用依赖）。写入一份本地静音占位音频文件并返回其路径 + 标注。
    if get_settings().mock:
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"podcast_mock_{_slugify(text)}_{uuid.uuid4().hex[:6]}.wav"
        try:
            import wave
            with wave.open(str(dest), "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)          # 16-bit PCM
                w.setframerate(16000)
                n_frames = int(16000 * 0.4)  # 0.4s 静音占位
                w.writeframes(b"\x00\x00" * n_frames)
        except Exception:
            # wave 模块写入失败，尝试手动写入一份合法 WAV 文件
            try:
                sr = 16000
                n_frames = int(sr * 0.4)
                data_size = n_frames * 2
                header = (
                    b'RIFF'
                    + (data_size + 36).to_bytes(4, 'little')
                    + b'WAVE'
                    + b'fmt '
                    + (16).to_bytes(4, 'little')
                    + (1).to_bytes(2, 'little')
                    + (1).to_bytes(2, 'little')
                    + sr.to_bytes(4, 'little')
                    + (sr * 2).to_bytes(4, 'little')
                    + (2).to_bytes(2, 'little')
                    + (2).to_bytes(2, 'little')
                    + b'data'
                    + data_size.to_bytes(4, 'little')
                    + b'\x00\x00' * n_frames
                )
                dest.write_bytes(header)
            except Exception as e2:
                # 两份写入均失败 -> 返回空路径，前端将回退显示文本而非 404 音频
                if result is not None:
                    result.model_used = "Mock 模拟"
                    result.error = f"TTS 静音音频写入失败：{type(e2).__name__}"
                return ("", "（Mock）TTS 占位（静音音频写入失败，未调用真实 TTS）")
        if result is not None:
            result.model_used = "Mock 模拟"
        return (str(dest), "（Mock）静音占位音频 · 未调用真实 TTS / Edge")

    def _edge(voice_name: str) -> Path:
        import asyncio
        import edge_tts
        dest = out_dir / f"podcast_{_slugify(text)}_{_slugify('edge')}_{uuid.uuid4().hex[:6]}.mp3"
        try:
            async def _synth():
                comm = edge_tts.Communicate(text, voice_name)
                await comm.save(str(dest))
            asyncio.run(_synth())
        except Exception:
            # 清理可能产生的空文件，避免留下垃圾
            if dest.exists() and dest.stat().st_size == 0:
                try:
                    dest.unlink()
                except OSError:
                    pass
            raise
        return dest

    def _cloud_request_omit_voice(creds: dict) -> bytes:
        """直接发不含 voice 字段的请求，依赖厂商默认音色（部分 OpenAI 兼容厂商支持）。"""
        import json
        import urllib.request

        url = (creds.get("base_url") or "").rstrip("/") + "/audio/speech"
        payload = {"model": creds.get("model"), "input": text}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {creds['api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def _cloud_one(creds: dict) -> "Path":
        model = (creds.get("model") or "").strip()
        if not model:
            raise RuntimeError(
                "云端 TTS 未配置 model（已填写 base_url/api_key，请在 providers.tts.models[].model "
                "填写模型名，如 gpt-4o-mini-tts）"
            )
        dest = out_dir / f"podcast_{_slugify(text)}_{_slugify(model)}_{uuid.uuid4().hex[:6]}.mp3"
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=120, max_retries=1)
        # ① 第一发：用配置的音色（未配置则用通用默认 alloy）
        try:
            resp = client.audio.speech.create(model=model, voice=voice or "alloy", input=text)
            dest.write_bytes(resp.content)
            return dest
        except Exception as e1:
            # ② 指定音色出错 → 去掉 voice 字段再调一次，依赖厂商默认音色
            print(
                f"[TTS-WARN] 云端 TTS({model}) 指定音色[{voice or '未配置→alloy'}]失败："
                f"{type(e1).__name__}: {e1}；尝试省略 voice 用默认音色重试。",
                flush=True,
            )
            try:
                data = _cloud_request_omit_voice(creds)
                dest.write_bytes(data)
                return dest
            except Exception as e2:
                # ③ 仍失败（多数厂商 voice 为必填，会再次 400）→ 抛出让外层回退 Edge
                print(
                    f"[TTS-WARN] 云端 TTS({model}) 省略 voice 重试亦失败：{type(e2).__name__}: {e2}",
                    flush=True,
                )
                raise RuntimeError(
                    f"云端 TTS 指定音色与默认音色均失败："
                    f"指定音色[{voice or 'alloy'}]→{type(e1).__name__}: {e1}；"
                    f"省略 voice→{type(e2).__name__}: {e2}"
                )

    # 无凭据 → 免费 Edge TTS
    if not has_cloud_creds:
        try:
            dest = _edge(voice or "zh-CN-XiaoxiaoNeural")
        except Exception as e:
            raise RuntimeError(
                f"[TTS 执行失败] engine=Edge TTS（免费）\n错误：{type(e).__name__}: {e}"
            )
        if result is not None:
            result.model_used = "Edge TTS"
        return (str(dest), "Edge TTS（免费·本地无需 Key）")

    # 有凭据 → 遍历 models 按序 fallback；全部失败自动回退 Edge（不中断开盒）
    models = _resolve_models(prov, get_settings().global_)
    if not models:
        try:
            dest = _edge("zh-CN-XiaoxiaoNeural")
        except Exception as e2:
            raise RuntimeError(
                f"[TTS 执行失败] 云端无可用 model 且回退 Edge 失败\n"
                f"Edge 错误：{type(e2).__name__}: {e2}"
            )
        if result is not None:
            result.model_used = "Edge TTS"
        return (str(dest), "Edge TTS（云端无可用 model，已回退·免费）")
    try:
        dest, used = _call_with_fallback(models, _cloud_one, ctx="tts")
        if result is not None:
            result.model_used = used
        return (str(dest), f"{used}（云端大模型）")
    except FallbackExhausted as e:
        # 诊断：把云端真实报错打到后端日志，便于排查（不再只显示“调用失败”）
        print(f"[TTS-WARN] 云端 TTS 全部模型失败，回退 Edge：{e}", flush=True)
        try:
            dest = _edge("zh-CN-XiaoxiaoNeural")
        except Exception as e2:
            raise RuntimeError(
                f"[TTS 执行失败] 云端与回退 Edge 均失败\n"
                f"fallback 错误：{e}\n"
                f"Edge 错误：{type(e2).__name__}: {e2}"
            )
        if result is not None:
            result.model_used = "Edge TTS"
        return (str(dest), "Edge TTS（云端调用失败，已自动回退·免费）")


# ---------------------------------------------------------------------------
# 开盒结果自动归档
# ---------------------------------------------------------------------------
def _download_remote(url: str, out_dir: Path, stem: str, timeout: int = 120) -> str | None:
    """尝试把远程 URL 下载到本地 output 目录。成功返回本地相对路径，失败返回 None。"""
    try:
        import httpx
    except ImportError:
        return None
    # 推断扩展名
    ext = ".bin"
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|$)", url.split("/")[-1])
    if m:
        ext = "." + m.group(1).lower()
    # 视频常用 mp4 兜底
    if ext == ".bin" and "video" in url.lower():
        ext = ".mp4"
    dest = out_dir / f"{stem}{ext}"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as hc:
            r = hc.get(url)
        if r.status_code == 200 and r.content:
            dest.write_bytes(r.content)
            return str(dest.relative_to(out_dir))
    except Exception:
        return None
    return None


def archive_result(result: BlindBoxResult, mode_id: str) -> dict:
    """把一次开盒结果落盘，并返回归档信息。

    - 始终写 output/result_<uuid>.json（含 seed/concept/plan/output/output_kind/时间戳/模式）。
    - 远程 URL 型成品（image_url 返回 url、video_file 返回 URL）尝试下载到本地，
      下载成功则把 output 改写为本地相对路径，避免链接失效导致成果丢失。
    - 本地文件（spec md / base64 图片 png）与纯文本保持原样。
    - 返回 {"json": 相对路径, "local_file": 相对路径或 None}，供前端预览。
    """
    out_dir: Path = SETTINGS.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex[:8]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    local_file: str | None = None
    output_val = result.output
    # 已是本地绝对路径文件（spec md / base64 图片 png）
    if isinstance(output_val, str) and Path(output_val).is_absolute():
        as_path = Path(output_val)
        if as_path.exists() and output_val != str(out_dir):
            try:
                local_file = str(as_path.relative_to(out_dir))
            except ValueError:
                local_file = None

    # 远程 URL -> 尝试下载到本地
    if not local_file and isinstance(output_val, str) and re.match(r"^https?://", output_val.strip()):
        kind_prefix = "image" if result.output_kind == "image_url" else "video"
        dl = _download_remote(
            output_val.strip(), out_dir,
            f"{kind_prefix}_{_slugify(result.plan)}_{uuid.uuid4().hex[:6]}",
        )
        if dl:
            local_file = dl
            result.output = dl  # 改写 output 为本地相对路径（前端优先读本地）
            output_val = dl

    # 纯文本成品（诗歌/角色卡/命名/谜题/小说等）-> 额外落一份可读 md，
    # 方便文件系统直接查看与历史面板浏览；local_file 指向该 md。
    # 排除 URL / 已落盘的绝对路径（前者是远程成品，后者 code-spec/图片已处理）。
    if (not local_file and isinstance(output_val, str) and output_val.strip()
            and not re.match(r"^https?://", output_val.strip())
            and not Path(output_val).is_absolute()):
        mode_meta = SETTINGS.get_mode(mode_id) or {}
        mode_name = mode_meta.get("name", mode_id)
        md_content = (
            f"# {mode_name} · {ts}\n\n"
            f"> seed: {result.seed}\n\n---\n\n{output_val}\n"
        )
        md_path = out_dir / f"text_{_slugify(output_val)}_{uuid.uuid4().hex[:6]}.md"
        md_path.write_text(md_content, encoding="utf-8")
        local_file = str(md_path.relative_to(out_dir))

    record = {
        "id": rid,
        "mode": mode_id,
        "timestamp": ts,
        "seed": result.seed,
        "concept": result.concept,
        "plan": result.plan,
        "output": result.output,
        "output_kind": result.output_kind,
        "tts_info": result.tts_info,
        "model_used": result.model_used,
        "caption": result.caption,
        "image_url": result.image_url,
        "local_file": local_file,
    }
    json_path = out_dir / f"result_{_slugify(result.concept, max_len=30)}_{rid}.json"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "json": str(json_path.relative_to(out_dir)),
        "local_file": local_file,
        "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# 插画故事：一次开盒生成多张连续画面
# ---------------------------------------------------------------------------

def _extract_pages(plan: str, max_pages: int = 6) -> list[str]:
    """从规划输出中提取 [PAGE_1] / [PAGE_2] ... 图片 prompt 块。

    按出现顺序提取，最多 max_pages 页；找不到块则把整段 plan 当作单页。
    """
    blocks: list[str] = []
    for i in range(1, max_pages + 1):
        tag = f"PAGE_{i}"
        block = _extract_block(plan, tag)
        if block:
            blocks.append(block)
    if not blocks:
        # 无结构化标签：整段 plan 作为唯一一页
        p = plan.strip()
        if p:
            blocks.append(p)
    return blocks


def _run_illustrated_story(
    plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None,
) -> str:
    """插画故事模式：根据规划出的多页 prompt，逐一调用图片模型生成，返回 JSON 字符串
    `{"pages": [{"url": "...", "local": "..."}, ...], "count": N}`。

    - 同一 plan 产生 2-6 张图（由 planner 的 [PAGE_N] 块数量决定，最少 2 张）。
    - 对每张图复用 image executor 的凭据与重试逻辑。
    - 遇到个别页面生成失败时跳过该页，不阻断整条故事。
    - mock 模式返回包含占位图信息的 JSON。
    """
    pages_prompt = _extract_pages(plan, max_pages=6)
    if len(pages_prompt) < 2:
        #  Planner 只产出单页：拆成前后两段（概念卡前 1/2 + 后 1/2）作为两页
        half = max(1, len(plan) // 2)
        pages_prompt = [plan[:half].strip(), plan[half:].strip()]

    settings = get_settings()
    img_models = _executor_creds("image")
    has_key = any(c["api_key"] for c in img_models) and not all(
        c["api_key"] == "<USER_KEY>" for c in img_models
    )
    out_dir = SETTINGS.output_dir

    if settings.mock or not has_key:
        if result is not None:
            result.model_used = "Mock 模拟"
        mock_pages = [
            {"prompt": p[:60], "url": None, "local": None} for p in pages_prompt
        ]
        return json.dumps({"pages": mock_pages, "count": len(mock_pages)}, ensure_ascii=False)

    def _gen_one(prompt: str) -> dict | None:
        """生成单张图，返回 {"url", "local"} 或 None（失败跳过）。"""

        def call(creds: dict) -> str:
            client = OpenAI(
                base_url=creds["base_url"], api_key=creds["api_key"],
                timeout=180, max_retries=0,
            )
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    resp = client.images.generate(
                        model=creds["model"], prompt=prompt, size="1024x1024", n=1,
                    )
                    item = resp.data[0]
                    if getattr(item, "url", None):
                        return item.url
                    if getattr(item, "b64_json", None):
                        import base64
                        slug = _slugify(prompt, max_len=30)
                        img_path = out_dir / f"story_{slug}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(base64.b64decode(item.b64_json))
                        return str(img_path)
                    raise RuntimeError("empty image data")
                except APITimeoutError as e:
                    last_err = e
                    time.sleep(3)
                    continue
                except Exception as e:
                    last_err = e
                    break
            raise RuntimeError(
                f"[插画故事 单页失败] model={creds['model']}: {type(last_err).__name__}: {last_err}"
            )

        try:
            out, used = _call_with_fallback(img_models, call, ctx="executor:illustrated_story")
            if result is not None and result.model_used is None:
                result.model_used = used
            if out.startswith("http"):
                return {"url": out, "local": None}
            return {"url": None, "local": str(Path(out).relative_to(out_dir))}
        except FallbackExhausted:
            return None

    pages: list[dict] = []
    failed_count = 0
    for i, prompt in enumerate(pages_prompt):
        info = _gen_one(prompt)
        if info is not None:
            pages.append({"prompt": prompt[:80], **info})
        else:
            failed_count += 1

    payload = {"pages": pages, "count": len(pages)}
    if failed_count > 0:
        payload["failed"] = failed_count
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 歌词 + 哼唱：先写歌词，再用 TTS 朗读合成音频
# ---------------------------------------------------------------------------

def _run_song(
    plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None,
) -> str:
    """歌曲模式：规划输出包含 [LYRICS] 块的歌词文本。

    执行流程：
    1. 提取 [LYRICS] 块作为歌词；若不存在则把整段 plan 当作歌词。
    2. 清理歌词（去掉 BGM 标记、停顿标记等不适合朗读的内容）。
    3. 调用 TTS 将歌词合成为音频文件。
    4. 返回 JSON：{"audio": 本地相对路径, "lyrics": 歌词文本, "tts_info": "..."}。

    mock 模式返回占位 JSON。
    """
    raw_lyrics = _extract_block(plan, "LYRICS") or plan.strip()
    # 清理：去掉 BGM / 停顿标记（TTS 会朗读它们）
    lyrics = re.sub(r'\[BGM\s*[:：][^\]]*\]', '', raw_lyrics, flags=re.I)
    lyrics = re.sub(r'\[(?:pause|停顿|暂停)\s*\d+s?\]', '', lyrics, flags=re.I)
    lyrics = lyrics.strip()
    if not lyrics:
        lyrics = "（歌词为空）"

    settings = get_settings()
    if settings.mock:
        if result is not None:
            result.model_used = "Mock 模拟"
            result.tts_info = "（Mock）静音占位"
        out_dir = SETTINGS.output_dir
        dest = out_dir / f"song_mock_{_slugify(lyrics)}_{uuid.uuid4().hex[:6]}.wav"
        try:
            import wave
            with wave.open(str(dest), "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * int(16000 * 0.4))
        except Exception:
            return json.dumps(
                {"audio": "", "lyrics": lyrics, "tts_info": "（Mock）写入失败"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"audio": str(dest.relative_to(out_dir)), "lyrics": lyrics, "tts_info": "（Mock）静音占位"},
            ensure_ascii=False,
        )

    # 真实环境：调用 TTS 合成音频
    try:
        audio_path, tts_info = _run_tts(lyrics, mode, result)
    except Exception as e:
        return json.dumps(
            {"audio": "", "lyrics": lyrics, "tts_info": f"TTS 失败: {e}"},
            ensure_ascii=False,
        )

    rel = ""
    if audio_path:
        try:
            rel = str(Path(audio_path).relative_to(SETTINGS.output_dir))
        except ValueError:
            rel = audio_path

    return json.dumps(
        {"audio": rel, "lyrics": lyrics, "tts_info": tts_info or ""},
        ensure_ascii=False,
    )
