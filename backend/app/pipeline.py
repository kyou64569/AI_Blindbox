"""盲盒管道核心：随机种子注入 -> 创意 -> 规划 -> 执行（3 Agent 标准管道）。

- 支持 mock 模式：无真实 API key 时用本地规则产出，便于跑通骨架。
- 调用统一 OpenAI 兼容接口（openai 库），模型/温度/key 全部来自配置。
"""
from __future__ import annotations

import json
import logging
import random
import re
import re as _re  # 历史别名：本文件同时存在 re. 与 _re. 两种写法，二者指向同一模块
import time
import time as _time_mod  # 历史别名，同上
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI, APITimeoutError

# 注意：只导入 get_settings()，不要 `from .config import SETTINGS`。
# 后者是 import 时的值绑定快照，config.set_settings() 热更新后本模块仍持有旧对象，
# 曾导致改配置后 output_dir / providers / modes 不生效（需重启进程）。
from .config import get_settings

# 模块级 logger：供未在函数内 `import logging as _log` 的位置使用，
# 避免在闭包/异常分支里因未定义 _log 触发 NameError。
_log = logging.getLogger(__name__)


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


@dataclass
class BlindBoxResult:
    seed: str
    concept: str           # Agent1 概念卡
    plan: str              # Agent2 规划输出
    output: str            # 成品（文本/路径/URL）
    output_kind: str       # text | image_url | video_file | spec_md | quiz_html | video_storyboard | image_strip
    archived: dict | None = None   # 自动归档信息（result json 路径 + 本地成品路径）
    tts_info: str | None = None   # TTS 实际使用的模型标注（仅播客模式）
    caption: str | None = None    # 配文/标题（表情包/漫画等需要文字叠加的模式）
    image_url: str | None = None  # 角色卡等模式的头像/附图 URL
    model_used: str | None = None   # 最终产出模型名；mock 模式为 "Mock 模拟"；无模型调用为 None
    error: str | None = None        # 回退到 mock 的原因（仅 game 等会静默回退的模式）
    env_warning: str | None = None  # code 模式技术栈黑名单校验命中的环境体积警告（重试后仍命中时透传前端）


_RECENT_THEMES_FILE = "recent_themes.json"   # 最近用过的主题记录（放 output 目录）
_RECENT_THEMES_MAX = 3                        # 排除最近 N 次开盒的主题，避免近期连续撞同主题


def _load_recent_themes() -> list[str]:
    """读取最近用过的主题列表（无文件/损坏时返回空）。"""
    try:
        p = get_settings().output_dir / _RECENT_THEMES_FILE
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _remember_theme(theme: str) -> None:
    """记录本次抽到的主题，只保留最近 N 个。"""
    try:
        p = get_settings().output_dir / _RECENT_THEMES_FILE
        recent = [t for t in _load_recent_themes() if t != theme]
        recent.append(theme)
        recent = recent[-_RECENT_THEMES_MAX:]
        p.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def draw_seed(mode: "dict[str, Any] | None" = None) -> str:
    """从 twist pool 随机抽取 per_open_draw 个种子，组合成可复现的 seed 串。

    - 优先使用 mode 级独立池（mode.pool，如 code 模式的「工具池」），
      未配置时回退全局 randomness.pool。
    - theme 维度带「近期去重」：排除最近 _RECENT_THEMES_MAX 次用过的主题，
      避免连续抽到同一主题（其余维度保持纯随机）；池被排空时回退全池。
    """
    rnd = get_settings().randomness
    if not rnd.get("enabled", True):
        return "seed_fixed"
    n = int(rnd.get("per_open_draw", 2))
    pool = (mode or {}).get("pool") or rnd.get("pool", {}) or {}
    picked: list[str] = []
    for dim, items in pool.items():
        if not items:
            continue
        if dim == "theme":
            recent = _load_recent_themes()
            cands = [it for it in items if it not in recent]
            if not cands:
                cands = list(items)
            theme = random.choice(cands)
            _remember_theme(theme)
            picked.append(f"{dim}:{theme}")
        else:
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
# 模式独立池（如 code 工具池）的兜底池：必须是能开发成实用工具的功能方向，而非世界观/意象
_FALLBACK_TOOL_POOL = {
    "theme": ["文件批处理", "Markdown 笔记", "数据格式转换", "待办清单", "番茄计时", "本地搜索", "图片压缩", "剪贴板历史"],
    "style": ["极简清爽", "复古终端", "温暖手账", "冷峻数据", "毛玻璃拟态", "报纸排版"],
    "constraint": ["数据必须本地持久化，刷新不丢失", "必须支持一键导出文件", "必须完全离线可用，无网络请求", "必须提供快捷键操作", "支持批量处理", "界面使用中文"],
    "twist": ["支持拖拽文件直接导入", "附带一个让用户眼前一亮的彩蛋", "所有操作都可撤销", "提供模板快速开始", "支持暗色亮色主题切换"],
}


def _clean_seed_text(raw: str) -> str:
    """清洗大模型返回的种子词条：只取首行，剥除维度标签 / 编号前缀 / 引号 / 结尾标点。

    generate_pool_seed 与 _generate_with_model 共用，避免两处清洗规则漂移。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.split("\n")[0].strip()
    # 前缀可能以任意顺序叠加（「【约束】1. x」/「1. 【约束】x」），循环剥离直到稳定
    for _ in range(4):
        before = text
        # 维度标签，如「【约束】必须出现...」
        text = _re.sub(r"^【[^】]*】\s*", "", text)
        # 编号前缀，如「1. 」「2、」
        text = _re.sub(r"^\d+[\.\、\s]+", "", text)
        text = text.lstrip('"').lstrip("'").strip()
        if text == before:
            break
    return text.strip('"').strip("'").strip("。").strip("、").strip()


def _build_seed_prompt(dim: str, existing: list[str], tool: bool = False) -> tuple[str, str, str]:
    """构建种子生成的 system prompt、user prompt 和 forbid_hint。

    - tool=False（默认）：全局创意池，产出「有画面感」的世界观/意象种子。
    - tool=True：模式独立池（当前为 code 工具池），产出「能开发成实用软件工具」的功能方向种子，
      并明确禁止世界观/意象类词条（如 '苔藓时钟' '糖霜浮雕'）。
    """
    dim_cn = _DIM_CN.get(dim, dim)
    dim_guide = _get_dim_guide(dim, tool)
    if tool:
        system = (
            "你是一个为「AI 盲盒·代码工具池」创作新种子词条的助手。"
            f"请根据维度【{dim_cn}】生成【一条】能用于开发【实用软件工具】的词条，"
            "适合作为工具开发盲盒的随机注入项。\n"
            f"【维度定义】{dim_guide}\n"
            "只输出这一条词条本身，不要任何解释、不要引号、不要编号、不要句号结尾。"
            "\n\n重要：必须是【具体可开发的功能方向】，禁止世界观/意象/氛围类词条"
            "（如 '苔藓时钟' '潮汐碑林' '糖霜浮雕' 这类艺术意象一律不要）。"
            "新种子必须在具体功能点上与已有种子完全不同——引入一个全新的功能方向。"
        )
        forbid_hint = (
            "\n\n已有种子（仅供参考，请勿沿用其中的具体功能点，请引入一个全新的、不同的功能方向）：\n"
            + "\n".join(f"- {e}" for e in existing)
        ) if existing else ""
    else:
        system = (
            "你是一个为「AI 盲盒」随机池创作新种子词条的助手。"
            f"请根据维度【{dim_cn}】生成【一条】新颖、有画面感的中文种子词条，"
            "适合作为创意盲盒的随机注入项。\n"
            f"【维度定义】{dim_guide}\n"
            "只输出这一条词条本身，不要任何解释、不要引号、不要编号、不要句号结尾。"
            "\n\n重要：新种子必须在【具体意象 / 角色 / 动物 / 物体 / 场景元素】上与已有种子"
            "完全不同——只保持同一维度类别即可，不要沿用已有种子里出现的任何具体意象。"
        )
        forbid_hint = (
            "\n\n已有种子（仅供参考，请勿沿用其中的具体角色 / 动物 / 物体，"
            "请引入一个全新的、不同的具象元素）：\n"
            + "\n".join(f"- {e}" for e in existing)
        ) if existing else ""
    user = f"请为维度【{dim_cn}】创作一条全新的种子。{forbid_hint}"
    return system, user, forbid_hint


def generate_pool_seed(dim: str, existing: list[str] | None = None, mode_id: str | None = None) -> str:
    """为指定维度生成一条「当前配置中没有」的新种子词条。

    - mode_id 指定时按「模式独立池」（当前为 code 工具池）生成：产出能开发成实用工具的
      功能方向词条（tool 指导语 + 工具兜底池），避免刷出世界观/意象类种子。
    - 优先调大模型；提示词**强制要求新种子在具象元素上与已有种子完全不同**，
      避免模型换汤不换药（例如已有「橘猫」却反复生成含橘猫的变体）。
    - 若生成结果仍锚定在已有意象（4 字滑窗命中），自动重试；多次仍锚定或调用失败，
      回退内置兜底池（已避开 existing，且不含橘猫等锚定意象），保证按钮始终可用。
    - 凭据复用 creative Agent 配置，留空回退 global。
    """
    tool = bool(mode_id)
    dim_cn = _DIM_CN.get(dim, dim)
    existing = [str(x).strip() for x in (existing or []) if str(x).strip()]

    def _pick_fallback() -> str:
        pool = _FALLBACK_TOOL_POOL if tool else _FALLBACK_POOL
        cands = [c for c in pool.get(dim, ["新种子"]) if c not in existing]
        if not cands:  # 兜底池用尽，直接加序号避免空串
            cands = [f"{dim_cn}工具{len(existing) + 1}" if tool else f"{dim_cn}种子{len(existing) + 1}"]
        return random.choice(cands)

    settings = get_settings()
    cfg = settings.agents.get("creative", {}) or {}
    g = settings.global_
    models = _resolve_models(cfg, g)

    if (settings.mock
            or not any(c["api_key"] for c in models)
            or all(c["api_key"] == "<USER_KEY>" for c in models)):
        return _pick_fallback()

    # 按维度定制指导语
    system, user, forbid_hint = _build_seed_prompt(dim, existing, tool)

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
                text = _clean_seed_text(resp.choices[0].message.content or "")
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


def refresh_all_seeds(mode_id: str | None = None) -> dict[str, list[str]]:
    """一键刷新所有维度的种子池——并行调用多模型生成，大幅提速且分散 API 压力。

    保持各维度种子数量不变，全部替换为新种子。
    从 executor chain（deepseek/step/gemini/agnes 四家 API）轮转模型，每维度平均分配。
    - mode_id 指定且该模式配置了独立 pool → 刷新该模式的池；
    - 否则刷新全局 randomness.pool。
    """
    import logging as _log
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    settings = get_settings()
    mode = settings.get_mode(mode_id) if mode_id else None
    if mode is not None and mode.get("pool"):
        pool = mode["pool"] or {}
    else:
        pool = (settings.randomness or {}).get("pool", {}) or {}
    new_pool: dict[str, list[str]] = {}

    # 收集所有可用模型（去重），分散到不同 API 提供商避免限频
    all_models = _executor_creds("game")
    unique_models: list[dict] = []
    seen = set()
    for m in all_models:
        if m["model"] not in seen and m.get("api_key"):
            unique_models.append(m)
            seen.add(m["model"])

    # 验证是否有可用模型
    if not unique_models:
        _log.error("[seed refresh] No valid models available for seed generation")
        raise RuntimeError("No valid API keys configured for seed generation")

    _log.warning("[seed refresh] using %d models: %s",
                 len(unique_models), [m["model"][:25] for m in unique_models])

    # 收集所有需要生成的 (dim, index) 任务
    tasks: list[tuple[str, int]] = []
    for dim in ("theme", "style", "constraint", "twist"):
        target = len(pool.get(dim, []) or [])
        for i in range(target):
            tasks.append((dim, i))

    _log.warning("[seed refresh] total tasks: %d", len(tasks))

    # 维度 → 已生成种子列表（线程安全预分配）
    results: dict[str, list[str | None]] = {}
    for dim in ("theme", "style", "constraint", "twist"):
        results[dim] = [None] * len(pool.get(dim, []))

    # 线程锁，保护 results 字典的并发读写和 model_idx 的原子递增
    results_lock = threading.Lock()
    model_idx = 0

    def _gen_one(dim: str, task_i: int, creds: dict) -> tuple[str, int, str | None]:
        # 使用锁读取已生成的种子，避免竞态条件
        with results_lock:
            existing = [s for s in results[dim][:task_i] if s is not None]
        for retry in range(3):
            try:
                seed = _generate_with_model(dim, existing, creds, tool=bool(mode_id))
                seed = seed.strip()
                # 对 theme/style 维度进行长度限制（Python字符串是Unicode，按字符截断是安全的）
                if dim in ("theme", "style") and len(seed) > 12:
                    seed = seed[:12]
                if seed:
                    return dim, task_i, seed
            except Exception as e:
                _log.warning("[seed refresh] dim=%s [%d/%d] model=%s retry=%d: %s",
                             dim, task_i + 1, len(results[dim]), creds["model"], retry, str(e)[:80])
                _time_mod.sleep(1)
        return dim, task_i, None  # 彻底失败

    # 线程池并行生成，每个任务轮转分配不同模型
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        for dim, i in tasks:
            # 使用锁确保 model_idx 的原子递增，避免竞态条件导致模型分配不均
            with results_lock:
                creds = unique_models[model_idx % len(unique_models)]
                model_idx += 1
            futures[executor.submit(_gen_one, dim, i, creds)] = (dim, i)

        for future in as_completed(futures):
            dim, i, seed = future.result()
            # 使用锁写入结果，避免竞态条件
            with results_lock:
                if seed:
                    results[dim][i] = seed
                    _log.info("[seed refresh] dim=%s [%d/%d] %s", dim, i + 1, len(results[dim]), seed)
                else:
                    results[dim][i] = f"{dim}_{i + 1}"
                    _log.warning("[seed refresh] dim=%s [%d/%d] → fallback", dim, i + 1, len(results[dim]))

    # 收拢结果，确保即使维度为空也保留该键
    for dim in ("theme", "style", "constraint", "twist"):
        new_pool[dim] = [s for s in results[dim] if s is not None]

    _log.warning("[seed refresh] complete: %s", {d: len(v) for d, v in new_pool.items()})
    return new_pool


def _generate_with_model(dim: str, existing: list[str], creds: dict, tool: bool = False) -> str:
    """用指定的单个模型凭据生成一条种子（不 fallback）。tool=True 时按工具池指导语生成。"""
    system, user, _ = _build_seed_prompt(dim, existing, tool)

    client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=180, max_retries=0)
    for _attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=creds["model"],
                temperature=creds.get("temperature") or 0.95,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = _clean_seed_text(resp.choices[0].message.content or "")
            if not text:
                continue
            # constraint/twist 是描述性短语，2 字滑窗锚定检查过于激进（中文 80 出现"必须""一个"就误判），
            # 仅对 theme/style（具象标签）做锚定检查。
            if dim in ("theme", "style") and _seems_anchored(text, existing):
                continue
            return text
        except Exception:
            continue
    raise RuntimeError(f"模型 {creds['model']} 多次返回空内容或锚定")


def _get_dim_guide(dim: str, tool: bool = False) -> str:
    if tool:
        if dim == "theme":
            return "种子是一个【实用工具/应用的功能主题】（如 '本地文件处理' '数据转换' '个人记账'）。必须精炼：2-8 个汉字，指向一个能开发成软件工具的具体功能场景，不要世界观/意象。"
        elif dim == "style":
            return "种子是一个【软件 UI/界面风格】（如 '极简清爽' '复古终端' '毛玻璃拟态'）。必须精炼：2-6 个汉字，是前端可实现的具体视觉风格，不要艺术意象风格。"
        elif dim == "constraint":
            return "种子是一条【软件功能约束】（如 '数据必须本地持久化' '必须支持一键导出' '必须离线可用'）。是一条开发中可落地的功能要求，不是创作意象。"
        elif dim == "twist":
            return "种子是一个【软件功能亮点/彩蛋特性】（如 '支持拖拽导入' '所有操作可撤销' '附带彩蛋功能'）。是一个可实现的具体功能特性，不是剧情反转。"
        return "必须精炼，且是可开发的具体功能方向。"
    if dim == "theme":
        return "种子是一个【主题场景/世界观标签】（如 '赛博禅院' '深海火炉'）。必须精炼：2-6 个汉字，一个核心意象组合。"
    elif dim == "style":
        return "种子是一个【艺术风格/视觉技法标签】（如 '故障艺术' '胶片颗粒'）。必须精炼：2-6 个汉字。"
    elif dim == "constraint":
        return "种子是一条【创作约束/限制条件】（如 '必须出现一只橘猫' '只用黑白灰三色' '含一段独白'）。是一条规则，不是主题意象。"
    elif dim == "twist":
        return "种子是一个【叙事反转/情节转折】（如 '主角其实是 AI' '结局即开场' '无声胜有声'）。是一个意外情节，不是主题意象。"
    return "必须精炼。"
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

    seed = seed or draw_seed(mode)

    # 兼容旧配置：pipeline_mode 已移除，若用户仍配置则警告
    _pipeline_mode = settings.global_.get("pipeline_mode")
    if _pipeline_mode and _pipeline_mode != "standard":
        import logging as _log
        _log.warning(
            "配置项 pipeline_mode='%s' 已废弃，当前仅支持标准 3-Agent 管道。请将 settings.yaml 中的 pipeline_mode 删除。",
            _pipeline_mode,
        )

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

    # 2) 规划 Agent
    # Text modes (novel, poem, character, naming, puzzle) skip the planner —
    # the creative agent already produces a complete concept. Running it through
    # the low-temperature planner would turn it into a degraded outline.
    # 例外：text 模式可通过 use_planner: true 启用结构化规划（如算命/朋友圈等需要固定格式的场景）
    if executor_type in ("text",) and not mode.get("use_planner"):
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

    # 3) 执行 Agent
    output_kind = mode.get("output_handler", "text")

    result = BlindBoxResult(
        seed=seed,
        concept=concept,
        plan=plan,
        output="",
        output_kind=output_kind,
    )

    # 代码模式：技术栈环境黑名单校验 —— 防止生成的 spec 引导安装重型环境
    # （引擎/美术工具链/数据库/CUDA 等）导致磁盘空间不足。
    # 命中黑名单 → 带警告重试 planner 一次；重试后仍命中 → spec 顶部插入
    # 警告头，并把警告透传前端展示（result.env_warning）。
    if executor_type in ("code-spec", "code-cli") and plan:
        hits, warns = _check_blocked_env(plan, mode)
        if hits:
            retry_in = (
                f"创意概念：\n{concept}\n\n"
                f"上一版规划使用了禁止环境（{'、'.join(hits)}），其安装会占用大量磁盘空间。\n"
                "请保留创意概念与世界观，改用轻量实现载体重写规划：纯 Web 单文件（HTML/CSS/JS）、"
                "Python 标准库+少量 pip 包、Node+少量 npm 包。不要在正文中复述禁止项清单。\n\n"
                f"请按以下格式收敛：\n{planner_tpl}"
            )
            retry = _agent_call("planner", planner_sys, retry_in, mode=mode)
            hits2, warns2 = _check_blocked_env(retry, mode)
            if not hits2:
                plan = retry
            else:
                plan = _prepend_env_warning(retry, hits2)
                result.env_warning = "\n".join(warns2)
            result.plan = plan

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


def _safe_relpath(path: str, base: "Path") -> str | None:
    """安全地将 path 转为相对于 base 的路径；失败时返回 None。"""
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return None


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


# ------------------------------------------------------------------
# code 模式：技术栈环境黑名单校验
# ------------------------------------------------------------------
# 目的：spec 蓝图允许宏大创意，但避免引导出安装会占用大量磁盘空间的重型环境
# （引擎/美术工具链/数据库/CUDA 等），防止用户多抽几个代码盲盒后硬盘告急。
# 命中黑名单 → 带警告重试 planner 一次；重试后仍命中 → spec 顶部插入警告头并透传前端。
_DEFAULT_BLOCKED_ENV: tuple[str, ...] = (
    "unreal", "ue5", "unity", "godot", "blender", "substance", "photoshop",
    "fmod", "wwise", "mysql", "sql server", "postgresql", "postgres", "oracle",
    "cuda", "docker", "android studio", "xcode", "maya", "3ds max",
)


def _strip_env_constraint_section(text: str) -> str:
    """剔除 spec 中复述【技术栈环境约束】的章节，防止「禁令清单里的关键词」被误判为真实选型。

    模板要求 planner 输出约束章节（含 CUDA/Unity/MySQL 等禁止项名称），模型常会原样复述，
    若整篇扫描会把「复述禁令」当成「技术选型」误报。实际选型集中在【技术栈】等章节，
    故扫描前删掉约束章节。兼容 【技术栈环境约束】/### 技术栈环境约束 等标题形式。
    """
    if not text:
        return text
    idx = text.lower().find("技术栈环境约束")
    if idx == -1:
        return text
    line_start = text.rfind("\n", 0, idx) + 1          # 标题所在行行首
    line_end = text.find("\n", idx)                    # 标题行行尾
    if line_end == -1:
        return text[:line_start]                       # 约束章节是最后一段，整体删除
    after = text[line_end + 1:]                        # 标题行之后的正文
    m = re.search(r"(?m)^[\s]*[【#]", after)           # 下一个章节标记行
    end = line_end + 1 + (m.start() if m else len(after))
    return text[:line_start] + text[end:]


def _check_blocked_env(text: str, mode: "dict[str, Any] | None" = None) -> "tuple[list[str], list[str]]":
    """扫描 spec 文本是否命中重型环境关键词。

    - 黑名单优先取 mode.blocked_env（settings.yaml 可配），为空回退内置默认。
    - 扫描前先剔除【技术栈环境约束】章节，避免「复述禁令清单」造成误报
      （真实技术选型在【技术栈】等章节，不受影响）。
    - 命中即返回；重试后仍命中才插入警告头（主流程处理）。
    - 返回 (命中关键词列表, 警告文案列表)。
    """
    mode = mode or {}
    blocked = [
        str(x).strip().lower()
        for x in mode.get("blocked_env", _DEFAULT_BLOCKED_ENV)
        if str(x).strip()
    ]
    if not blocked:
        blocked = list(_DEFAULT_BLOCKED_ENV)
    text_l = _strip_env_constraint_section(text or "").lower()
    hits = [b for b in blocked if b in text_l]
    if not hits:
        return [], []
    warns = [
        f"检测到重型环境/工具关键词：{'、'.join(hits)}，其安装可能占用大量磁盘空间。",
        "建议优先改用轻量实现载体：纯 Web 单文件（HTML/CSS/JS）、Python 标准库+少量 pip 包、Node+少量 npm 包。",
    ]
    return hits, warns


def _prepend_env_warning(spec_text: str, hits: list[str]) -> str:
    """在 spec 顶部插入环境体积警告头（重试后仍命中的兜底）。"""
    head = (
        "> ⚠️ **环境体积警告**：本规格出现重型环境/工具关键词（"
        + "、".join(hits)
        + "），安装可能占用大量磁盘空间，请谨慎评估后再开发；"
        "建议优先考虑轻量实现载体（Web 单文件 / Python / Node 少量依赖）。\n\n---\n\n"
    )
    return head + (spec_text or "")


def _run_executor(executor_type: str, plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None) -> str:
    """按执行类型产出成品。

    - text：规划输出即正文（真实环境可再调一次文本模型扩写，这里直接返回）。
    - image：调用 providers.image 配置的图像模型（OpenAI 兼容 images.generate）。
    - video：本期 mock 占位，待接入视频生成 API。
    - code-spec：生成 spec.md 落盘 output/。
    - code-cli：本期仅预留接口。
    """
    if executor_type == "text":
        models = _executor_creds("text")
        concept = result.concept if result else ""
        settings = get_settings()
        # 透传模式：planner 输出已是最终成品，直接返回不经过执行 Agent 改写
        if mode.get("passthrough") and plan.strip():
            if result is not None:
                result.model_used = "Planner 直出"
            return plan
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

        # 使用 planner 的模式：planner 输出包含格式约束，执行 Agent 需严格遵守
        if mode.get("use_planner") and plan.strip() != concept.strip():
            system_prompt = (
                "你是一位严格遵循格式的写手。你的任务是严格按照【构思大纲】里定义的格式、"
                "字数、结构要求来创作正文。\n"
                "【严禁提示】：\n"
                "1. 绝对不要输出与要求不符的字数或格式！\n"
                "2. 绝对不要重复大纲原文！\n"
                "3. 严格遵守大纲中的每条规则（字数上限、禁止词汇等）。"
            )
            user_prompt = (
                f"【故事灵感】：\n{concept}\n\n"
                f"【构思大纲与格式要求】：\n{plan}\n\n"
                "请严格按照上述大纲的格式和字数要求创作正文。直接输出正文内容："
            )
        else:
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
                        size=mode.get("size", "1024x1024"),
                        n=1,
                    )
                    item = resp.data[0]
                    if getattr(item, "url", None):
                        return item.url
                    if getattr(item, "b64_json", None):
                        import base64
                        out_dir = get_settings().output_dir
                        img_path = out_dir / f"char_{_slugify(appearance_cn)}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(base64.b64decode(item.b64_json))
                        return str(img_path)
                    raise RuntimeError(
                        f"[角色卡图片生成失败] model={creds['model']} 返回了空的图片数据（无 url / b64_json）"
                    )
                except APITimeoutError as e:
                    last_err = e
                    _time_mod.sleep(3)
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
                        # 存为本地文件（体积较大，落 output 产物目录）
                        import base64
                        out_dir = get_settings().output_dir
                        img_path = out_dir / f"{_slugify(plan)}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(base64.b64decode(item.b64_json))
                        return str(img_path)
                    raise RuntimeError(
                        f"[图片执行 Agent] model={creds['model']} 返回了空的图片数据（无 url / b64_json）"
                    )
                except APITimeoutError as e:
                    # 图像 API 延迟尖峰，重试（多数情况下重抽会落回正常区间）
                    last_err = e
                    _time_mod.sleep(3)
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
                _time_mod.sleep(5)
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
        # 生成 spec.md，落到 output 产物目录
        out_dir = get_settings().output_dir
        spec_path = out_dir / f"spec_{_slugify(plan)}_{uuid.uuid4().hex[:6]}.md"
        spec_path.write_text(f"# 盲盒代码需求\n\n{plan}\n", encoding="utf-8")
        return str(spec_path)

    if executor_type == "code-cli":
        # 本期仅预留接口，不实现
        return "[预留接口] code-cli 重档本期未实现。"

    if executor_type == "game":
        return _run_game_executor(plan, mode, result)

    if executor_type == "quiz":
        return _run_quiz_executor(plan, mode, result)

    if executor_type == "illustrated_story":
        return _run_illustrated_story(plan, mode, result)

    if executor_type == "video_storyboard":
        return _run_video_storyboard(plan, mode, result)

    if executor_type == "song":
        return _run_song(plan, mode, result)

    return plan


# 注入到生成的 quiz/game HTML 中的高度上报脚本。
# iframe 使用 sandbox="allow-scripts"（不含 allow-same-origin）以保持 origin 隔离，
# 父页面因此无法读取 contentDocument 来自适应高度；改由子页面主动 postMessage 上报。
# 目标 origin 用 "*" 是安全的：仅上报一个高度数字，不含任何敏感信息。
_IFRAME_HEIGHT_REPORTER = """
<script>
(function(){
  function report(){
    try{
      var d=document.documentElement,b=document.body;
      var h=Math.max(b.scrollHeight,b.offsetHeight,d.scrollHeight,d.offsetHeight);
      if(h>0) parent.postMessage({__blindbox_height:h},"*");
    }catch(e){}
  }
  window.addEventListener("load",report);
  document.addEventListener("click",function(){setTimeout(report,350);});
  if(window.ResizeObserver){try{new ResizeObserver(report).observe(document.body);}catch(e){}}
  setTimeout(report,120);
})();
</script>
"""


def _inject_height_reporter(html: str) -> str:
    """向生成的 HTML 注入高度上报脚本，使沙箱 iframe 仍能自适应高度。"""
    if not html or "__blindbox_height" in html:
        return html
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx != -1:
        return html[:idx] + _IFRAME_HEIGHT_REPORTER + html[idx:]
    idx = lower.rfind("</html>")
    if idx != -1:
        return html[:idx] + _IFRAME_HEIGHT_REPORTER + html[idx:]
    return html + _IFRAME_HEIGHT_REPORTER


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


def _call_two_pass_game(
    models: list[dict],
    system_prompt: str,
    user_prompt: str,
    save_fn,
) -> "tuple[str, str]":
    """两步式游戏生成：骨架 → 脚本，每段精短，避免截断。

    Step 1: 生成 HTML 骨架（head + CSS + body + canvas + 占位script标签）
    Step 2: 给定骨架，生成游戏逻辑（<script>...</script>）
    拼接后保存。
    """
    import logging as _log

    # ---- Step 1: 骨架 ----
    skel_sys = (
        "你是前端工程师。只输出游戏的 HTML 骨架——不写任何 JavaScript 游戏逻辑。\n"
        "输出要求：\n"
        "1. <!DOCTYPE html><html><head><meta charset=UTF-8><style>CSS样式</style></head>\n"
        "2. <body> 内只放 <canvas id=\"gc\"></canvas> 和 <div id=\"hud\">得分:0</div>\n"
        "3. CSS 控制全屏自适应、颜色主题、字体，不超过 40 行。\n"
        "4. 在 </style> 之后放一个空的 <script></script> 占位（内容只写 // GAME_LOGIC）。\n"
        "5. 以 </html> 结尾。总输出不超过 1500 字符。"
        "不要任何解释，直接输出源码。"
    )
    skel_user = f"游戏需求：{user_prompt}\n\n请输出 HTML 骨架（不含游戏逻辑的JS）："

    skel_call = _make_simple_call(models, skel_sys, skel_user)
    skel, skel_model = _call_with_fallback(models, skel_call, ctx="game:skel")
    skeleton = _extract_html(skel).strip()
    # 验证必需元素：CSS 闭合、HTML 闭合、canvas 元素、hud 元素
    if not skeleton:
        raise RuntimeError("骨架生成失败（返回空内容）")
    if "</style>" not in skeleton.lower():
        raise RuntimeError("骨架生成不完整（缺 CSS 闭合标签 </style>）")
    if "</html>" not in skeleton.lower():
        raise RuntimeError("骨架生成不完整（缺 HTML 闭合标签 </html>）")
    if 'id="gc"' not in skeleton and "id='gc'" not in skeleton:
        raise RuntimeError("骨架生成不完整（缺 canvas 元素 id=\"gc\"）")
    if 'id="hud"' not in skeleton and "id='hud'" not in skeleton:
        raise RuntimeError("骨架生成不完整（缺 div 元素 id=\"hud\"）")

    _log.warning("[game two-pass] skeleton ready (%d chars) via %s", len(skeleton), skel_model)

    # ---- Step 2: 脚本 ----
    script_sys = (
        "你是游戏脚本工程师。只输出 <script>...</script> 块内的 JS 游戏逻辑。\n"
        "要求：\n"
        "1. 状态初始化、update/draw 循环、鼠标/键盘输入、计分、重来按钮。\n"
        "2. Canvas 元素 id 为 \"gc\"，得分显示在 id=\"hud\" 的 div 中。\n"
        "3. 变量名用简短英文字母，总 JS 不超过 120 行，总输出不超过 2500 字符。\n"
        "4. 输出格式：第一行 <script>，最后一行 </script>，中间是 JS 代码。\n"
        "5. requestAnimationFrame 驱动主循环，最后一帧调用它形成循环。\n"
        "不要任何解释，直接输出 <script>...JS代码...</script>。"
    )
    script_user = f"HTML 骨架如下，请生成游戏逻辑脚本：\n\n{skeleton}"

    script_call = _make_simple_call(models, script_sys, script_user)
    script, script_model = _call_with_fallback(models, script_call, ctx="game:script")
    # 提取 <script>...</script> 块
    m = _re.search(r"<script[^>]*>([\s\S]*?)</script>", script, flags=_re.I)
    if m:
        js_code = m.group(1).strip()
    else:
        js_code = script.strip()
    if not js_code or "// GAME_LOGIC" in js_code:
        raise RuntimeError("脚本生成失败（空或仅有占位符）")

    _log.warning("[game two-pass] script ready (%d chars) via %s", len(js_code), script_model)

    # ---- 拼接 ----
    # 更健壮的替换策略：先尝试精确替换占位符，失败则替换整个 script 标签内容
    if "// GAME_LOGIC" in skeleton:
        full_html = skeleton.replace("// GAME_LOGIC", js_code)
    else:
        # 回退策略：查找空的 <script> 标签并替换其内容
        # 匹配 <script>...</script> 或 <script /> 等形式
        pattern = r'(<script[^>]*>)(\s*//?\s*GAME_LOGIC\s*|)(</script>)'
        match = _re.search(pattern, skeleton, flags=_re.I | _re.DOTALL)
        if match:
            full_html = skeleton[:match.start()] + match.group(1) + js_code + match.group(3) + skeleton[match.end():]
        else:
            # 最后回退：在 </body> 前插入 script
            if "</body>" in skeleton:
                full_html = skeleton.replace("</body>", f"<script>{js_code}</script></body>")
            else:
                # 极端回退：直接追加
                full_html = skeleton + f"<script>{js_code}</script></html>"
                _log.warning("[game two-pass] skeleton 缺少 </body>，采用追加方式插入脚本")

    path = save_fn(full_html)
    _log.warning("[game two-pass] stitched %d chars", len(full_html))
    return path, f"{skel_model}+{script_model}" if skel_model != script_model else script_model


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
                content = (resp.choices[0].message.content or "").strip()
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


def _run_game_executor(plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None) -> str:
    """小游戏模式：基于规划出的游戏需求(plan)，用代码模型生成一份可直接在浏览器运行的
    单文件 HTML5 小游戏（HTML+CSS+JS 内联），落盘 output/ 并返回绝对路径。支持多模型按序 fallback。

    - 真实环境：调用 executor Agent（代码模型）按严格 system 指令产出完整、可玩的 HTML；
      解析时剥离 ```html 围栏，确保落盘的是纯 HTML。单模型内部重试 2 次属于「单模型内部重试」，
      与跨模型 fallback 是两回事——跨模型交给 _call_with_fallback。
    - mock / 无 key / 所有模型均失败：落一份内置的、确实可玩的占位小游戏，保证开盒不中断、
      且不再把「需求提示词」误当成品。
    """
    out_dir = get_settings().output_dir
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
        dest.write_text(_inject_height_reporter(html), encoding="utf-8")
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
        "你是一位擅长快速写代码的前端工程师。请基于小游戏需求，产出"
        "【完整可玩的单文件 HTML5 小游戏】。\n"
        "【第一优先级——长度硬约束】CSS≤60 行、JS≤150 行、整体≤250 行、总字符≤5000。"
        "只实现核心玩法（移动/点击/计分/重来），不要加冗余的动画/粒子/特效/音效/菜单。"
        "控制台日志全部删掉。变量名用单字母或无意义短名（a,b,c...）节省长度。\n\n"
        "【其余要求】：\n"
        "1. 无外部依赖、Canvas 或纯 DOM 均可。\n"
        "2. 可操作：鼠标/键盘、计分、重新开始按钮。\n"
        "3. 中文界面。\n"
        "4. 自适应 iframe：html/body 100%宽高 overflow:hidden。\n"
        "5. 绝对禁止 pointer-events:none。\n"
        "6. 直接输出 <!DOCTYPE html> 开头，不要 ```html 围栏，不要任何解释。\n"
        "7. <script> 开头写：var keys={};window.addEventListener('keydown',function(e){keys[e.key]=true;e.preventDefault();});window.addEventListener('keyup',function(e){keys[e.key]=false;});var cv=document.querySelector('canvas');if(cv){cv.style.cursor='crosshair';cv.addEventListener('click',function(e){var r=cv.getBoundingClientRect();cv._mx=(e.clientX-r.left)*(cv.width/r.width);cv._my=(e.clientY-r.top)*(cv.height/r.height);cv._mdown=true;});cv.addEventListener('mousemove',function(e){var r=cv.getBoundingClientRect();cv._mx=(e.clientX-r.left)*(cv.width/r.width);cv._my=(e.clientY-r.top)*(cv.height/r.height);});}\n"
        "   游戏逻辑用 keys['ArrowUp']/keys['w'] 检测键盘、cv._mdown/cv._mx/cv._my 检测鼠标。\n"
    )
    user_prompt = (
        f"请基于以下小游戏需求，生成完整可玩的单文件 HTML5 小游戏源码：\n\n{plan}\n\n"
        "（请直接开始输出 <!DOCTYPE html>，不要任何前后缀说明。）"
    )

    def _patch_game_html(html: str) -> str:
        """修复 AI 生成游戏中常见的交互缺失问题。"""

        # 1) 移除 pointer-events:none
        html = _re.sub(
            r'pointer-events\s*:\s*none\b',
            'pointer-events: auto',
            html, flags=_re.IGNORECASE,
        )

        # 2) 检测是否缺少 addEventListener（AI 生成时常忘绑定交互事件）
        has_listener = bool(_re.search(r'addEventListener', html, flags=_re.IGNORECASE))
        if not has_listener:
            # 注入基础输入桥接层，暴露给游戏逻辑调用
            bridge = (
                '\n<script>\n'
                '(function(){\n'
                '  var keys={};\n'
                '  window.addEventListener("keydown",function(e){keys[e.key]=true;e.preventDefault();});\n'
                '  window.addEventListener("keyup",function(e){keys[e.key]=false;});\n'
                '  window._getKeys=function(){return keys;};\n'
                '  window._isKey=function(k){return !!keys[k];};\n'
                '  var cv=document.querySelector("canvas");\n'
                '  if(cv){cv.style.pointerEvents="auto";'
                '  cv.addEventListener("click",function(e){var r=cv.getBoundingClientRect();'
                '  cv._mx=(e.clientX-r.left)*(cv.width/r.width);'
                '  cv._my=(e.clientY-r.top)*(cv.height/r.height);'
                '  cv._mclick=true;});'
                '  cv.addEventListener("pointermove",function(e){var r=cv.getBoundingClientRect();'
                '  cv._mx=(e.clientX-r.left)*(cv.width/r.width);'
                '  cv._my=(e.clientY-r.top)*(cv.height/r.height);});}'
                '  window._canvasClick=function(){var c=document.querySelector("canvas");return c?c._mclick:false;};'
                '  window._clearCanvasClick=function(){var c=document.querySelector("canvas");if(c)c._mclick=false;};'
                '  window._getCanvasPos=function(){var c=document.querySelector("canvas");'
                '  return c?{x:c._mx||0,y:c._my||0}:{x:0,y:0};};\n'
                '})();\n'
                '</script>\n'
            )
            html = html.replace('</body>', bridge + '</body>', 1)
            if '</body>' not in html:
                html += bridge

        # 3) 确保 canvas 有合适的 cursor（仅在 canvas 元素本身没有 cursor 样式时添加）
        if '<canvas' in html and not _re.search(r'<canvas[^>]*cursor\s*:', html, flags=_re.I):
            html = html.replace('<canvas', '<canvas style="cursor: crosshair"', 1)

        return html

    def call(creds: dict) -> str:
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=180, max_retries=0)
        # 单模型内部：最多 2 次尝试（截断/不完整则重试），异常或仍不完整则触发跨模型 fallback
        last_err: Exception | None = None
        local_user_prompt = user_prompt
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=creds["model"],
                    temperature=0.7,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": local_user_prompt},
                    ],
                )
                content = (resp.choices[0].message.content or "").strip()
                # 诊断日志：记录每个模型的原始输出（前 300 字 + 最后一行的截断标记）
                tail = content[-120:] if len(content) > 120 else content
                has_html_start = bool(re.search(r'<!DOCTYPE|<html|<canvas|<script', content, flags=re.I))
                has_html_end = "</script>" in content.lower() and content.rstrip().endswith(("</html>", "</script>"))
                _log.warning(
                    "[game diag] model=%s attempt=%d len=%d has_html_start=%s has_html_end=%s head=%r tail=%r",
                    creds["model"], attempt, len(content),
                    has_html_start, has_html_end,
                    content[:120].replace("\n", "\\n"),
                    tail.replace("\n", "\\n"),
                )
                html = _extract_html(content)
                # 完整性校验：必须有 </script> 闭合（保证 JS 完整），
                # 不强求 </html>（有些模型生成格式不严格但游戏可玩）
                if html and "</script>" in html.lower():
                    return _save(_patch_game_html(html))
                if html:
                    last_err = RuntimeError("HTML JS 未闭合（缺 </script>），可能被截断")
                    local_user_prompt = user_prompt + "\n\n【严重警告】上次输出又长又没写完。这次必须极简：去掉所有特效/音效/动画/多余样式，只保留核心玩法（移动+得分+重来）。JS 控制在 100 行以内，总代码控制在 200 行以内。必须输出完整闭合的 <script>...</script>。"
                else:
                    last_err = RuntimeError("模型未返回可用的 HTML 内容")
                if attempt == 0:
                    _time_mod.sleep(2)
                    continue
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    _time_mod.sleep(2)
                    continue
                break
        # 该模型两次均失败 -> 抛异常触发跨模型 fallback
        raise RuntimeError(f"game 模型 {creds['model']} 生成失败: {last_err}")

    # === 主流程：先单步，失败则两步式，再失败才降级到 Mock ===
    try:
        out, used = _call_with_fallback(models, call, ctx="executor:game")
        if result is not None:
            result.model_used = used
        return out
    except FallbackExhausted:
        pass  # 单步失败 → 尝试两步式

    # --- 两步式生成：骨架 + 脚本，每段精短避免截断 ---
    try:
        _log.warning("[game two-pass] single-pass failed, trying two-pass approach")
        out, used = _call_two_pass_game(models, system_prompt, user_prompt, _save)
        if result is not None:
            result.model_used = used
        return out
    except FallbackExhausted:
        pass  # 两步式也失败 → Mock

    if result is not None:
        result.model_used = "Mock 模拟"
    return _save(_mock_game_html(plan))


def _run_quiz_executor(plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None) -> str:
    """趣味测试模式：基于规划出的测试内容，生成交互式 HTML 测试页。
    每次显示一道题，用户选择后显示下一道，完成后点击查看结果。
    """
    import logging as _log
    out_dir = get_settings().output_dir
    slug = _slugify(plan, max_len=40)
    settings = get_settings()
    models = _executor_creds("text")

    def _save(html: str) -> str:
        dest = out_dir / f"quiz_{slug}_{uuid.uuid4().hex[:6]}.html"
        dest.write_text(_inject_height_reporter(html), encoding="utf-8")
        return str(dest)

    has_key = any(c["api_key"] for c in models) and not all(
        c["api_key"] == "<USER_KEY>" for c in models
    )
    if settings.mock or not has_key:
        if result is not None:
            result.model_used = "Mock 模拟"
        return _save(_mock_quiz_html(plan))

    system_prompt = (
        "你是一位前端交互工程师。请基于【测试内容】，生成一个【完整可玩的单文件 HTML5 趣味测试页】。\n"
        "【交互逻辑——必须严格遵守】：\n"
        "1. 每次只显示一道题，用户选择选项后自动显示下一道题\n"
        "2. 最后一题答完后，显示一个「查看结果」按钮\n"
        "3. 点击「查看结果」按钮后，才显示测试结果（根据用户选项分布给出结果）\n"
        "4. 记录每道题的选项，结果页统计 A/B/C/D 选择最多的类型\n\n"
        "【设计要求】：\n"
        "1. 只输出一个 HTML 文件，内含 <style> 与 <script>，不要任何外部依赖\n"
        "2. 页面美观、现代感、配色舒适，有适当的过渡动画\n"
        "3. 顶部显示进度（如「第 1/3 题」）\n"
        "4. 选项用卡片形式，hover 效果，选中后高亮\n"
        "5. 结果页要有揭晓动画感（如淡入、缩放）\n"
        "6. 底部有「再测一次」按钮（刷新页面）\n"
        "7. 直接输出 <!DOCTYPE html> 开头，不要 ```html 围栏，不要任何解释\n"
        "8. 总字符数控制在 5000 以内"
    )
    user_prompt = f"请基于以下测试内容，生成完整可玩的单文件 HTML5 趣味测试页：\n\n{plan}"

    def call(creds: dict) -> str:
        client = OpenAI(base_url=creds["base_url"], api_key=creds["api_key"], timeout=180, max_retries=0)
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
                if html and "<script" in html.lower():
                    return _save(html)
                if attempt == 0:
                    _time_mod.sleep(2)
                    continue
                raise RuntimeError("模型未返回可用的 HTML 内容")
            except Exception as e:
                if attempt == 0:
                    _time_mod.sleep(2)
                    continue
                raise e

    try:
        out, used = _call_with_fallback(models, call, ctx="executor:quiz")
        if result is not None:
            result.model_used = used
        return out
    except FallbackExhausted:
        if result is not None:
            result.model_used = "Mock 模拟"
        return _save(_mock_quiz_html(plan))


def _mock_quiz_html(plan: str) -> str:
    """生成占位的趣味测试 HTML 页，尝试从 plan 中解析测试内容。"""

    # 尝试从 plan 中解析测试内容
    questions = []
    results = {}

    # 尝试解析题目
    lines = plan.split('\n')
    current_q = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配题目行
        q_match = _re.match(r'题目(\d+)[:：]\s*(.+)', line)
        if q_match:
            if current_q:
                questions.append(current_q)
            current_q = {"q": q_match.group(2), "opts": []}
        # 匹配选项
        elif current_q and line.startswith(('A.', 'B.', 'C.', 'D.')):
            current_q["opts"].append(line)
        # 匹配结果解析
        elif line.startswith('选A最多'):
            results['A'] = line.split('：')[-1].strip() if '：' in line else line.split(':')[-1].strip()
        elif line.startswith('选B最多'):
            results['B'] = line.split('：')[-1].strip() if '：' in line else line.split(':')[-1].strip()
        elif line.startswith('选C最多'):
            results['C'] = line.split('：')[-1].strip() if '：' in line else line.split(':')[-1].strip()
        elif line.startswith('选D最多'):
            results['D'] = line.split('：')[-1].strip() if '：' in line else line.split(':')[-1].strip()

    if current_q:
        questions.append(current_q)

    # 如果解析失败，使用默认题目
    if not questions:
        questions = [
            {"q": "你更倾向于哪种冒险？", "opts": ["A.探索未知城市", "B.深入荒野森林", "C.潜入深海世界", "D.穿越星际宇宙"]},
            {"q": "面对困境时你的第一反应？", "opts": ["A.冷静分析局势", "B.寻求朋友帮助", "C.凭直觉行动", "D.制定详细计划"]},
            {"q": "你最看重哪种品质？", "opts": ["A.勇气与魄力", "B.智慧与谋略", "C.善良与同理心", "D.自由与独立"]}
        ]
    if not results:
        results = {
            "A": "你是「开拓先锋」——永远走在探索未知的路上，勇气是你的旗帜。",
            "B": "你是「智慧谋士」——深思熟虑后用智慧化解一切，谋略是你的武器。",
            "C": "你是「直觉行者」——相信内心的声音，总能在关键时刻做出正确选择。",
            "D": "你是「独立思想家」——不随波逐流，用自己的方式定义世界的规则。"
        }

    # 转换为JavaScript格式
    questions_js = json.dumps(questions, ensure_ascii=False)
    results_js = json.dumps(results, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>趣味测试</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'PingFang SC',sans-serif;background:linear-gradient(135deg,#1a1a2e,#16213e);min-height:100vh;display:flex;align-items:center;justify-content:center;color:#fff}}
.container{{background:rgba(255,255,255,.1);backdrop-filter:blur(10px);border-radius:20px;padding:40px;max-width:500px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.3)}}
.progress{{text-align:center;margin-bottom:20px;color:#aaa;font-size:14px}}
.question{{font-size:20px;font-weight:bold;margin-bottom:24px;text-align:center;min-height:60px}}
.options{{display:flex;flex-direction:column;gap:12px}}
.option{{background:rgba(255,255,255,.08);border:2px solid rgba(255,255,255,.15);border-radius:12px;padding:16px 20px;cursor:pointer;transition:all .3s;text-align:center}}
.option:hover{{background:rgba(255,255,255,.15);border-color:#7f77dd;transform:translateY(-2px)}}
.option.selected{{background:rgba(127,119,221,.3);border-color:#7f77dd}}
.result-title{{font-size:24px;text-align:center;margin-bottom:16px}}
.result-text{{font-size:16px;line-height:1.8;text-align:center;margin-bottom:24px}}
.btn{{display:block;width:100%;padding:14px;background:linear-gradient(135deg,#7f77dd,#5b54c0);border:none;border-radius:12px;color:#fff;font-size:16px;cursor:pointer;transition:transform .2s}}
.btn:hover{{transform:scale(1.02)}}
.hidden{{display:none}}
.fade-in{{animation:fadeIn .5s ease}}
@keyframes fadeIn{{from{{opacity:0;transform:scale(.95)}}to{{opacity:1;transform:scale(1)}}}}
</style>
</head>
<body>
<div class="container">
  <div class="progress" id="progress">第 1/{{len(questions)}} 题</div>
  <div class="question" id="question">加载中...</div>
  <div class="options" id="options"></div>
  <div class="result hidden" id="result">
    <div class="result-title">🎉 测试结果</div>
    <div class="result-text" id="resultText"></div>
    <button class="btn" onclick="location.reload()">再测一次</button>
  </div>
</div>
<script>
const questions={questions_js};
const results={results_js};
let current=0,answers=[];
function showQuestion(){{
  document.getElementById("progress").textContent=`第 ${{current+1}}/${{questions.length}} 题`;
  document.getElementById("question").textContent=questions[current].q;
  const opts=document.getElementById("options");
  opts.innerHTML="";
  questions[current].opts.forEach((opt,i)=>{{
    const d=document.createElement("div");
    d.className="option";
    d.textContent=opt;
    d.onclick=()=>selectOpt(i,d);
    opts.appendChild(d);
  }});
}}
function selectOpt(i,d){{
  document.querySelectorAll(".option").forEach(o=>o.classList.remove("selected"));
  d.classList.add("selected");
  answers[current]=String.fromCharCode(65+i);
  setTimeout(()=>{{
    current++;
    if(current<questions.length) showQuestion();
    else showResultBtn();
  }},300);
}}
function showResultBtn(){{
  document.getElementById("options").innerHTML="";
  document.getElementById("question").textContent="所有题目已答完";
  document.getElementById("progress").textContent="";
  const btn=document.createElement("button");
  btn.className="btn";
  btn.textContent="查看结果";
  btn.onclick=showResult;
  document.getElementById("options").appendChild(btn);
}}
function showResult(){{
  const count={{A:0,B:0,C:0,D:0}};
  answers.forEach(a=>count[a]++);
  const max=Object.keys(count).reduce((a,b)=>count[a]>count[b]?a:b);
  document.getElementById("question").classList.add("hidden");
  document.getElementById("options").classList.add("hidden");
  document.getElementById("progress").classList.add("hidden");
  const r=document.getElementById("result");
  r.classList.remove("hidden");
  r.classList.add("fade-in");
  document.getElementById("resultText").textContent=results[max];
}}
showQuestion();
</script>
</body>
</html>"""


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
    prov = get_settings().providers.get("tts", {}) or {}
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
    out_dir: Path = get_settings().output_dir
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

    # song（歌词+哼唱）：output 是 JSON {"audio": "相对路径", "lyrics": "...", "tts_info": "..."}
    if not local_file and isinstance(output_val, str) and result.output_kind == "song":
        try:
            song = json.loads(output_val)
            rel_audio = (song.get("audio") or "").strip()
            if rel_audio:
                # audio 是 output 目录内的相对路径，直接标记为归档产物
                audio_path = out_dir / rel_audio
                if audio_path.exists():
                    local_file = rel_audio
                    result.output = rel_audio
                    output_val = rel_audio
        except Exception as e:
            import logging
            logging.warning("[archive] song 归档失败: %s", e)

    # image_strip（插画故事）：output 是 JSON，内含多页远程 URL，逐页下载到本地
    if not local_file and isinstance(output_val, str) and result.output_kind == "image_strip":
        try:
            strip = json.loads(output_val)
            pages = strip.get("pages", []) if isinstance(strip, dict) else []
            downloaded = 0
            for i, pg in enumerate(pages):
                url = pg.get("url", "")
                if not url or not re.match(r"^https?://", url.strip()):
                    continue
                dl = _download_remote(
                    url.strip(), out_dir,
                    f"story_{rid}_p{i + 1}_{uuid.uuid4().hex[:6]}",
                )
                if dl:
                    pg["local"] = dl
                    downloaded += 1
            if downloaded > 0:
                # 把带本地路径的 JSON 写回 output，方便前端持久化预览
                result.output = json.dumps(strip, ensure_ascii=False)
                output_val = result.output
                # 整个 story 视作一个归档产物，用第一张本地图的路径做标记
                local_file = next(
                    (pg["local"] for pg in pages if pg.get("local")), None
                )
        except Exception as e:
            import logging
            logging.warning("[archive] image_strip 归档失败: %s", e)

    # 角色卡等模式的附图（image_url 字段）：远程 URL 也下载到本地，
    # 避免链接失效导致头像/附图丢失（与 output 的 URL 下载兜底同一策略）。
    if (isinstance(result.image_url, str) and result.image_url.strip()
            and re.match(r"^https?://", result.image_url.strip())):
        dl = _download_remote(
            result.image_url.strip(), out_dir,
            f"img_{_slugify(result.seed or 'img', max_len=20)}_{uuid.uuid4().hex[:6]}",
        )
        if dl:
            result.image_url = dl

    # 纯文本成品（诗歌/角色卡/命名/谜题/小说等）-> 额外落一份可读 md，
    # 方便文件系统直接查看与历史面板浏览；local_file 指向该 md。
    # 排除 URL / 已落盘的绝对路径（前者是远程成品，后者 code-spec/图片已处理）。
    if (not local_file and isinstance(output_val, str) and output_val.strip()
            and not re.match(r"^https?://", output_val.strip())
            and not Path(output_val).is_absolute()):
        mode_meta = get_settings().get_mode(mode_id) or {}
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
        "env_warning": result.env_warning,
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
    out_dir = get_settings().output_dir

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
                    _time_mod.sleep(3)
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
            local = _safe_relpath(out, out_dir)
            if local is None:
                _log.warning("[illustrated_story] 输出路径不在 output_dir 下，跳过: %s", out)
                return None
            return {"url": None, "local": local}
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


def _extract_shots(plan: str, max_shots: int = 6) -> list[str]:
    """从规划输出中提取 [SHOT 1] / [SHOT 2] ... 分镜 prompt 块。

    按出现顺序提取，最多 max_shots 镜；找不到块则把整段 plan 当作单镜。
    """
    blocks: list[str] = []
    # 匹配 [SHOT 1]、[SHOT_1]、[SHOT1] 等变体，使用贪婪匹配捕获多行内容
    # 分隔符为换行后跟下一个标签，或行末；也兼容标签紧贴（无换行）的情况
    pattern = r'\[SHOT[_\s]?(\d+)\]\s*[:：]?\s*(.*?)(?=\n?\s*\[SHOT[_\s]?\d+\]|\Z)'
    matches = _re.findall(pattern, plan, flags=_re.IGNORECASE | _re.DOTALL)
    for num, content in matches:
        if int(num) <= max_shots:
            blocks.append(content.strip())
    if not blocks:
        # 也尝试匹配 [SHOT N] prompt: 格式（prompt 在标签同行）
        pattern2 = r'\[SHOT[_\s]?(\d+)\]\s*[:：]?\s*(.+?)(?=\n?\s*\[SHOT|\Z)'
        matches2 = _re.findall(pattern2, plan, flags=_re.IGNORECASE | _re.DOTALL)
        for num, content in matches2:
            if int(num) <= max_shots:
                blocks.append(content.strip())
    if not blocks:
        # 无结构化标签：整段 plan 作为唯一一镜
        p = plan.strip()
        if p:
            blocks.append(p)
    return blocks[:max_shots]


def _run_video_storyboard(
    plan: str, mode: dict[str, Any], result: "BlindBoxResult | None" = None,
) -> str:
    """视频分镜模式：根据规划出的多镜头 prompt，逐一调用图片模型生成故事板，
    返回 JSON 字符串 `{"pages": [{"url": "...", "local": "..."}, ...], "count": N}`。

    - 同一 plan 产生 4-6 张分镜图（由 planner 的 [SHOT_N] 块数量决定）。
    - 对每张图复用 image executor 的凭据与重试逻辑。
    - 遇到个别分镜生成失败时跳过该镜，不阻断整条故事板。
    - mock 模式返回包含占位信息的 JSON。
    """
    shots = _extract_shots(plan, max_shots=6)
    if len(shots) < 2:
        # Planner 只产出单镜：拆成前后两段作为两镜
        half = max(1, len(plan) // 2)
        shots = [plan[:half].strip(), plan[half:].strip()]

    settings = get_settings()
    img_models = _executor_creds("image")
    has_key = any(c["api_key"] for c in img_models) and not all(
        c["api_key"] == "<USER_KEY>" for c in img_models
    )
    out_dir = get_settings().output_dir

    if settings.mock or not has_key:
        if result is not None:
            result.model_used = "Mock 模拟"
        mock_pages = [
            {"prompt": s[:60], "url": None, "local": None} for s in shots
        ]
        return json.dumps({"pages": mock_pages, "count": len(mock_pages)}, ensure_ascii=False)

    def _gen_one(prompt: str) -> dict | None:
        """生成单张分镜图，返回 {"url", "local"} 或 None（失败跳过）。"""

        def call(creds: dict) -> str:
            client = OpenAI(
                base_url=creds["base_url"], api_key=creds["api_key"],
                timeout=180, max_retries=0,
            )
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    resp = client.images.generate(
                        model=creds["model"], prompt=prompt, size="1792x1024", n=1,
                    )
                    item = resp.data[0]
                    if getattr(item, "url", None):
                        return item.url
                    if getattr(item, "b64_json", None):
                        import base64 as _b64
                        slug = _slugify(prompt, max_len=30)
                        img_path = out_dir / f"storyboard_{slug}_{uuid.uuid4().hex[:6]}.png"
                        img_path.write_bytes(_b64.b64decode(item.b64_json))
                        return str(img_path)
                    raise RuntimeError("empty image data")
                except APITimeoutError as e:
                    last_err = e
                    _time_mod.sleep(3)
                    continue
                except Exception as e:
                    last_err = e
                    break
            raise RuntimeError(
                f"[视频分镜 单镜失败] model={creds['model']}: {type(last_err).__name__}: {last_err}"
            )

        try:
            out, used = _call_with_fallback(img_models, call, ctx="executor:video_storyboard")
            if result is not None and result.model_used is None:
                result.model_used = used
            if out.startswith("http"):
                return {"url": out, "local": None}
            local = _safe_relpath(out, out_dir)
            if local is None:
                _log.warning("[video_storyboard] 输出路径不在 output_dir 下，跳过: %s", out)
                return None
            return {"url": None, "local": local}
        except FallbackExhausted:
            return None

    pages: list[dict] = []
    failed_count = 0
    for i, prompt in enumerate(shots):
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
        out_dir = get_settings().output_dir
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
            rel = str(Path(audio_path).relative_to(get_settings().output_dir))
        except ValueError:
            rel = audio_path

    return json.dumps(
        {"audio": rel, "lyrics": lyrics, "tts_info": tts_info or ""},
        ensure_ascii=False,
    )
