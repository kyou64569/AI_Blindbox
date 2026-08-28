"""拆分自 pipeline.py（executors.py）：全部执行器。

- _run_executor：按 executor_type 派发
- _run_game_executor / _run_quiz_executor / _run_tts / _run_illustrated_story /
  _run_video_storyboard / _run_song：各模式成品生成（真实 + mock）
"""
from __future__ import annotations

import json
import logging
import re
import re as _re  # 历史别名：与 pipeline.py 原文件一致
import time as _time_mod
import uuid
from pathlib import Path
from typing import Any

from openai import OpenAI, APITimeoutError

from .agents import _call_with_fallback, _executor_creds, _make_simple_call, _resolve_models
from .config import get_settings
from .models import BlindBoxResult, FallbackExhausted
from .tools import (
    _extract_block,
    _extract_field,
    _extract_html,
    _extract_pages,
    _extract_shots,
    _first_choice_text,
    _first_image_item,
    _inject_height_reporter,
    _safe_relpath,
    _slugify,
    _translate_appearance,
)

_log = logging.getLogger(__name__)


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
                content = _first_choice_text(resp, "小说执行 Agent", creds["model"]).strip()
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
                return _first_choice_text(resp, "角色卡文本", creds["model"]).strip()

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
                    item = _first_image_item(resp, "角色卡图片生成", creds["model"])
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
                    item = _first_image_item(resp, "图片执行 Agent", creds["model"])
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
            # vid 来自上游响应，必须 URL 编码后再拼进 query：
            # 若其中含 & # ? 等字符，直接拼接会截断/污染查询串，导致轮询永远拿不到结果。
            from urllib.parse import quote as _urlquote
            poll_url = f"{root}/agnesapi?video_id={_urlquote(str(vid), safe='-_.~')}"
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
                content = _first_choice_text(resp, "游戏执行 Agent", creds["model"]).strip()
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
                content = _first_choice_text(resp, "代码执行 Agent", creds["model"]).strip()
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

            # asyncio.run 在「已有运行中的事件循环」里会直接抛
            # RuntimeError: asyncio.run() cannot be called from a running event loop。
            # 当前调用链是同步路由（FastAPI 放到线程池执行，线程内无 loop）所以能跑通，
            # 但一旦有人把 /api/open 改成 async def，这里就会炸。做兼容处理：
            # 检测到已有 loop 时，把协程丢到独立线程的新 loop 里跑并等待结果。
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_synth())          # 无运行中的 loop，正常路径
            else:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    ex.submit(lambda: asyncio.run(_synth())).result()
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
                    item = _first_image_item(resp, "插画故事 单页", creds["model"])
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
                    item = _first_image_item(resp, "视频分镜 单镜", creds["model"])
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
