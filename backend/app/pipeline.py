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
    model = cfg.get("model") or g.get("default_model")
    base_url = cfg.get("base_url") or g.get("default_base_url")
    api_key = cfg.get("api_key") or g.get("default_api_key")

    if settings.mock or not api_key or api_key == "<USER_KEY>":
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

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=90, max_retries=0)
    for _ in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.95,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            # 清洗：取首行，去引号 / 编号 / 结尾标点
            text = text.split("\n")[0].strip().strip('"').strip("'").strip("。").strip("、").strip()
            if not text:
                continue
            # 锚定检测：仍沿用已有意象则重试，否则采用
            if _seems_anchored(text, existing):
                continue
            return text
        except Exception:
            continue
    # 多次仍锚定或调用失败 -> 兜底（内置池已避开 existing，且不含橘猫等锚定意象）
    return _pick_fallback()


def _agent_call(agent_key: str, system: str, user: str, mode: dict | None = None) -> str:
    """调用单个文本类 Agent（创意 / 规划）。返回文本。优先真实接口，否则 mock。

    Agent 优先用自己配置的 model/base_url/api_key，留空则回退 global 默认值。
    """
    settings = get_settings()
    cfg = settings.agents.get(agent_key, {}) or {}
    g = settings.global_
    model = cfg.get("model") or g.get("default_model", "agnes-free")
    base_url = cfg.get("base_url") or g.get("default_base_url")
    api_key = cfg.get("api_key") or g.get("default_api_key")
    temperature = float(cfg.get("temperature", 0.8))

    if settings.mock or not api_key or api_key == "<USER_KEY>":
        return _mock_agent(agent_key, system, user, mode)

    import time
    last_err = None
    for attempt in range(2):
        try:
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=90, max_retries=0)
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)
                continue
    raise RuntimeError(
        f"[{agent_key} Agent 调用失败] model={model}  base_url={base_url}\n"
        f"错误：{type(last_err).__name__}: {last_err}"
    )


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
    result.output = _run_executor(executor_type, plan, mode, result)
    # 4) 自动归档：落盘元数据 + 把易失的远程产物下载到本地
    try:
        result.archived = archive_result(result, mode_id)
    except Exception:
        # 归档失败不应阻断开盒主流程
        result.archived = None
    return result


def _executor_creds(executor_type: str) -> tuple[str, str, str]:
    """解析执行 Agent 的凭据：executor 自己的配置优先，否则用 providers.<type>，再否则回退 global。"""
    settings = get_settings()
    g = settings.global_
    exec_cfg = settings.agents.get("executor", {}) or {}
    prov = settings.providers.get(executor_type, {}) or {}

    model = exec_cfg.get("model") or prov.get("model") or g.get("default_model", "agnes-free")
    base_url = exec_cfg.get("base_url") or prov.get("base_url") or g.get("default_base_url")
    api_key = exec_cfg.get("api_key") or prov.get("api_key") or g.get("default_api_key")
    return model, base_url, api_key


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
        model, base_url, api_key = _executor_creds("text")
        concept = result.concept if result else ""
        settings = get_settings()
        if settings.mock or not api_key or api_key == "<USER_KEY>":
            mode_name = (mode or {}).get("name", "微小说")
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

        client = OpenAI(base_url=base_url, api_key=api_key, timeout=90, max_retries=0)
        
        for attempt in range(2):
            resp = client.chat.completions.create(
                model=model,
                temperature=0.85,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            
            # 校验：若模型误将大纲原封不动抄回或仍以「输出大纲」开头，自动提示并纠正重试
            if content == plan.strip() or content.startswith("输出大纲") or content.startswith("**输出大纲**"):
                if attempt == 0:  # Only modify prompt on first attempt
                    user_prompt += "\n\n【系统提醒】：上一次回答误输出了大纲。请注意，绝对不要输出大纲或要点，请立刻直接输出完整的小说正文内容！"
                    continue
                # Second attempt still failed, proceed to cleanup
                break
            return content

        # 若重试后依然带有大纲标记，自动清洗剔除大纲前缀
        clean_text = re.sub(r"^(?:\*\*|\#\#)?\s*输出大纲[\s\S]*?(?=正文|【正文】|\n\n|\Z)", "", content, flags=re.I).strip()
        return clean_text or content

    if executor_type == "image":
        model, base_url, api_key = _executor_creds("image")
        settings = get_settings()
        if settings.mock or not api_key or api_key == "<USER_KEY>":
            return ("[mock 图片 生成占位] 配置好 providers.image 的真实 base_url/api_key/model "
                    f"并关闭 mock 后，将调用图像模型生成图片。\nplan={plan[:80]}")
        # 注意：该共享图像 API 延迟抖动很大（实测 18~50s 常见，偶发 >150s 超时）。
        # 因此把单次超时放宽到 180s，并对「超时」做最多 2 次重试；非超时错误
        # （鉴权/参数错误/模型不存在）不重试，直接抛出以暴露真实问题。
        # 总耗时上限约 3×180s=540s，仍在代理层(600s)与前端的兜底超时(580s)之内。
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=180, max_retries=0)
        last_err: Exception | None = None
        for _attempt in range(3):
            try:
                resp = client.images.generate(
                    model=model,
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
                return "[图片生成返回为空]"
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
            f"[图片执行 Agent 调用失败] model={model}  base_url={base_url}\n"
            f"错误：{type(last_err).__name__}: {last_err}\n"
            "（该图像接口存在明显延迟抖动，属偶发；可直接重试，或临时切 Mock 模式验证链路。）"
        )

    if executor_type == "tts":
        path, info = _run_tts(plan, mode)
        if result is not None:
            result.tts_info = info
        return path

    if executor_type == "video":
        model, base_url, api_key = _executor_creds("video")
        settings = get_settings()
        if settings.mock or not api_key or api_key == "<USER_KEY>":
            return ("[mock 视频 生成占位] 真实环境将调用 providers.video 的模型。\n"
                    f"plan={plan[:60]}...")
        # Agnes 视频为异步：POST /v1/videos 建任务 → 轮询 GET /agnesapi?video_id= 取结果
        try:
            import httpx
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            root = f"{parsed.scheme}://{parsed.netloc}"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            create_url = base_url.rstrip("/") + "/videos"
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
                "model": model,
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
                    pr = hc.get(poll_url, headers={"Authorization": f"Bearer {api_key}"})
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
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"[视频执行 Agent 调用失败] model={model}  base_url={base_url}\n"
                f"错误：{type(e).__name__}: {e}"
            )

    if executor_type == "code-spec":
        # 生成 spec.md，落到 output 目录（D 盘）
        out_dir = SETTINGS.output_dir
        spec_path = out_dir / f"spec_{_slugify(plan)}_{uuid.uuid4().hex[:6]}.md"
        spec_path.write_text(f"# 盲盒代码需求\n\n{plan}\n", encoding="utf-8")
        return str(spec_path)

    if executor_type == "code-cli":
        # 本期仅预留接口，不实现
        return "[预留接口] code-cli 重档本期未实现。"

    return plan


def _run_tts(plan: str, mode: dict[str, Any]) -> tuple[str, str]:
    """文本转语音：把规划出的播客台词合成为音频文件，落盘 output/ 并返回 (绝对路径, 使用信息)。

    路由规则（不再有 engine 字段，纯由凭据决定）：
    - 未同时配置 base_url 与 api_key → 免费 Edge TTS（无需 Key，需联网到微软端点）。
    - 同时配置了 base_url 与 api_key → 走云端 OpenAI 兼容大模型，使用 providers.tts.model
      指定的模型名（任意兼容 /v1/audio/speech 的厂商均可，不限品牌）。
      云端容错链：① 用配置的音色（未配置则用通用默认 alloy）② 指定音色出错则去掉 voice
      字段再调一次（依赖厂商默认音色）③ 仍失败才自动回退到免费 Edge TTS，保证开盒不中断。
    「使用信息」会在结果/历史中标注实际使用的模型，便于溯源。
    """
    prov = SETTINGS.providers.get("tts", {}) or {}
    model = (prov.get("model") or "").strip()
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

    def _cloud_request_omit_voice() -> bytes:
        """直接发不含 voice 字段的请求，依赖厂商默认音色（部分 OpenAI 兼容厂商支持）。"""
        import json
        import urllib.request

        url = base_url.rstrip("/") + "/audio/speech"
        payload = {"model": model, "input": text}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def _cloud() -> Path:
        if not model:
            raise RuntimeError(
                "云端 TTS 未配置 model（已填写 base_url/api_key，请在 providers.tts.model "
                "填写模型名，如 gpt-4o-mini-tts）"
            )
        dest = out_dir / f"podcast_{_slugify(text)}_{_slugify(model)}_{uuid.uuid4().hex[:6]}.mp3"
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=120, max_retries=1)
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
                data = _cloud_request_omit_voice()
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
        return (str(dest), "Edge TTS（免费·本地无需 Key）")

    # 有凭据 → 尝试云端；调用出错自动回退 Edge（不中断开盒）
    try:
        dest = _cloud()
        return (str(dest), f"{model}（云端大模型）")
    except Exception as e:
        # 诊断：把云端真实报错打到后端日志，便于排查（不再只显示“调用失败”）
        print(f"[TTS-WARN] 云端 TTS({model}) 调用失败：{type(e).__name__}: {e}", flush=True)
        try:
            dest = _edge("zh-CN-XiaoxiaoNeural")
        except Exception as e2:
            raise RuntimeError(
                f"[TTS 执行失败] 云端与回退 Edge 均失败\n"
                f"云端错误：{type(e).__name__}: {e}\n"
                f"Edge 错误：{type(e2).__name__}: {e2}"
            )
        return (str(dest), f"Edge TTS（云端 {model} 调用失败，已自动回退·免费）")


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
    if Path(output_val).is_absolute():
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
        "local_file": local_file,
    }
    json_path = out_dir / f"result_{_slugify(result.concept, max_len=30)}_{rid}.json"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "json": str(json_path.relative_to(out_dir)),
        "local_file": local_file,
        "timestamp": ts,
    }
