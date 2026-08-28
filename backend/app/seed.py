"""拆分自 pipeline.py（seed.py）：随机种子池生成与锚定检测。

- draw_seed：从 twist pool 随机抽取并组合种子（含近期主题去重）
- generate_pool_seed / refresh_all_seeds / _generate_with_model：调大模型补种
- _core_image / _seems_anchored / _clean_seed_text：种子清洗与「换汤不换药」锚定检测
"""
from __future__ import annotations

import json
import logging
import random
import re as _re
import threading
import time as _time_mod
from typing import Any

from openai import OpenAI

from .agents import _call_with_fallback, _executor_creds, _resolve_models
from .config import get_settings
from .tools import _first_choice_text

_log = logging.getLogger(__name__)


_RECENT_THEMES_FILE = "recent_themes.json"


_RECENT_THEMES_MAX = 3


# 保护 recent_themes.json 的并发读写，避免文件损坏
_recent_themes_lock = threading.Lock()


def _load_recent_themes() -> list[str]:
    """读取最近用过的主题列表（无文件/损坏时返回空）。"""
    with _recent_themes_lock:
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
    with _recent_themes_lock:
        try:
            p = get_settings().output_dir / _RECENT_THEMES_FILE
            # 直接读取文件，避免调用 _load_recent_themes 导致死锁
            recent = []
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        recent = [str(x) for x in data if str(x).strip()]
                except Exception:
                    recent = []
            # 去重并添加新主题
            recent = [t for t in recent if t != theme]
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

    先剥离已有条目的模板前缀，对核心意象做 3 字滑窗检测：若 new 包含某条核心意象
    的 3 字片段（如「机械猫」），判定为沿用同一具象元素、换汤不换药，应重试。

    改进：从 2 字改为 3 字，减少对常见中文组合的误判（如「机械」「橘猫」等常见词）；
    但核心意象本身 ≤2 字（如「橘猫」「白鹭」）时滑窗为空，需回退为完整匹配，
    否则 2 字核心词的锚定会漏判。
    """
    new = new or ""
    for e in existing:
        if not isinstance(e, str):
            continue  # 脏数据（None / 数字）直接跳过，避免 .strip() 崩 AttributeError
        e = e.strip()
        if not e:
            continue
        if e == new.strip():
            return True
        core = _core_image(e)
        if not core:
            continue
        # 3 字滑窗检测，减少误判
        for i in range(len(core) - 2):
            tok = core[i:i + 3]
            if tok and tok in new:
                return True
        # 核心意象 ≤2 字：3 字滑窗覆盖不到，回退为整词包含检测
        if len(core) <= 2 and core in new:
            return True
    return False


_DIM_CN = {"theme": "主题", "style": "风格", "constraint": "约束", "twist": "反转"}


_FALLBACK_POOL = {
    "theme": ["量子禅院", "机械敦煌", "酸性冰川", "废土茶道", "霓虹苗寨", "深海图书馆"],
    "style": ["故障艺术", "胶片颗粒", "低多边形", "浮世绘拼贴", "赛博水墨", "暗黑童话"],
    "constraint": ["必须出现一只白鹭", "只用黑白灰三色", "含一段旁白", "画面中央有一扇门"],
    "twist": ["主角早已不在人世", "故事是倒着发生的", "一切都是一场梦", "观众才是真正的主角"],
}


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
        # 维度标签：中文【】或英文 [ ] 都兼容
        text = _re.sub(r"^[【\[]\s*[^】\]]*\s*[】\]]\s*", "", text)
        # 编号前缀，如「1. 」「2、」
        text = _re.sub(r"^\d+[\.\、\s]+", "", text)
        text = text.lstrip('"').lstrip("'").strip()
        if text == before:
            break
    # 结尾标点/引号可能交错（如「"深海图书馆"。」），循环剥到稳定
    for _ in range(3):
        before = text
        text = text.strip('"').strip("'").strip("「」").strip("『』").strip("。").strip("、").strip()
        if text == before:
            break
    return text


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
        last_err: Exception | None = None
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
                text = _clean_seed_text(_first_choice_text(resp, "种子生成", creds["model"]))
                if not text:
                    _log.warning("[seed 生成] model=%s 第 %d 次返回空内容，重试",
                                 creds["model"], _attempt + 1)
                    continue
                if _seems_anchored(text, existing):
                    _log.warning("[seed 生成] model=%s 第 %d 次输出锚定已有种子(%r)，重试",
                                 creds["model"], _attempt + 1, text[:30])
                    continue
                return text
            except Exception as e:
                # 原实现是裸 `except Exception: continue`，鉴权失败/额度耗尽/模型名错
                # 这类不可能自愈的错误会被静默重试 3 次后统一报「返回空内容」，
                # 真实原因完全丢失。这里至少留下可排查的日志。
                last_err = e
                _log.warning("[seed 生成] model=%s 第 %d 次调用异常 %s: %s",
                             creds["model"], _attempt + 1, type(e).__name__, e)
                continue
        raise RuntimeError(
            f"[seed 生成] model={creds['model']} 3 次尝试均失败"
            f"{f'，最后一次错误 {type(last_err).__name__}: {last_err}' if last_err else '（返回空内容或锚定已有种子）'}"
        )

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
                    # 使用 Unicode 字符截断，避免多字节字符被截断导致编码问题
                    seed = seed[:12]
                if seed:
                    return dim, task_i, seed
            except Exception as e:
                _log.warning("[seed refresh] dim=%s [%d/%d] model=%s retry=%d: %s",
                             dim, task_i + 1, len(results[dim]), creds["model"], retry, str(e)[:80])
                _time_mod.sleep(1)
        # 彻底失败：从兜底池挑一条未用过的中文词条（而不是拼 `constraint_1` 占位符）。
        # 与单条生成 generate_pool_seed 的 _pick_fallback 保持同一套兜底池，避免两处漂移。
        _pool = _FALLBACK_TOOL_POOL if mode_id else _FALLBACK_POOL
        cands = [c for c in _pool.get(dim, ["新种子"]) if c not in existing]
        if not cands:
            cands = [f"{_DIM_CN.get(dim, dim)}新种子{task_i + 1}"]
        return dim, task_i, random.choice(cands)

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
                    # 兜底（_gen_one 已保证返回非 None，这里仅作最终防线；不用英文占位符）
                    results[dim][i] = f"{_DIM_CN.get(dim, dim)}新种子{i + 1}"
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
            text = _clean_seed_text(_first_choice_text(resp, "种子池刷新", creds["model"]))
            if not text:
                _log.warning("[seed refresh] model=%s 第 %d 次返回空内容", creds["model"], _attempt + 1)
                continue
            # constraint/twist 是描述性短语，2 字滑窗锚定检查过于激进（中文 80 出现"必须""一个"就误判），
            # 仅对 theme/style（具象标签）做锚定检查。
            if dim in ("theme", "style") and _seems_anchored(text, existing):
                _log.warning("[seed refresh] model=%s 第 %d 次输出锚定已有种子(%r)", creds["model"], _attempt + 1, text[:30])
                continue
            return text
        except Exception as e:
            _log.warning("[seed refresh] model=%s 第 %d 次调用异常: %s: %s", creds["model"], _attempt + 1, type(e).__name__, e)
            continue
    raise RuntimeError(f"模型 {creds['model']} 多次返回空内容或锚定")
