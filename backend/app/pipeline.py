"""盲盒管道核心：随机种子注入 -> 创意 -> 规划 -> 执行（3 Agent 标准管道）。

本文件为拆分后的「编排 + 兼容导出层」：只保留 run_blind_box 主编排逻辑，
其余实现按职责拆分为独立模块（seed / agents / executors / archive / tools /
models / token_usage），并在文件末尾 re-export 全部公开符号，保证
`from . import pipeline; pipeline._slugify` 等旧引用方式继续可用。
"""
from __future__ import annotations

import logging
from typing import Callable

from .config import get_settings
from .models import BlindBoxResult, FallbackExhausted, TaskCancelled
from .token_usage import _TokenUsageSession, _accrue_usage, _usage_accum
from .tools import validate_mode_config

_log = logging.getLogger(__name__)


def run_blind_box(
    mode_id: str,
    seed: str | None = None,
    *,
    on_stage: "Callable[[str, str], None] | None" = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> BlindBoxResult:
    """跑一次完整的开盒管道（创意 -> 规划 -> 执行 -> 归档）。

    参数：
      mode_id       模式 id
      seed          指定种子；缺省随机抽取
      on_stage      阶段回调 (stage, label)，stage 取值 creative/planner/executor，
                    供异步任务层更新进度（同步调用可忽略）
      should_cancel 每阶段切换前调用，返回 True 则抛 TaskCancelled 中断

    返回 BlindBoxResult；被取消时抛 TaskCancelled（不归档）。
    """
    settings = get_settings()
    mode = settings.get_mode(mode_id)
    if mode is None:
        raise ValueError(f"未知模式: {mode_id}")

    # 验证模式配置的结构完整性
    if not isinstance(mode, dict):
        raise ValueError(f"模式配置格式错误: {mode_id} 必须是对象")

    # 验证 executor_type
    executor_type = mode.get("executor_type")
    if not executor_type or not isinstance(executor_type, str):
        raise ValueError(f"模式 {mode_id} 缺少或格式错误的 executor_type")

    # 验证 output_handler
    output_handler = mode.get("output_handler")
    if not output_handler or not isinstance(output_handler, str):
        raise ValueError(f"模式 {mode_id} 缺少或格式错误的 output_handler")

    # 使用集中的验证逻辑（与 main.py 保持一致）
    validate_mode_config(mode, mode_id)

    seed = seed or draw_seed(mode)

    # 兼容旧配置：pipeline_mode 已移除，若用户仍配置则警告
    _pipeline_mode = settings.global_.get("pipeline_mode")
    if _pipeline_mode and _pipeline_mode != "standard":
        _log.warning(
            "配置项 pipeline_mode='%s' 已废弃，当前仅支持标准 3-Agent 管道。请将 settings.yaml 中的 pipeline_mode 删除。",
            _pipeline_mode,
        )

    # token 用量追踪作用域：整个管道（创意/规划/执行/重试）的模型调用都累加到这里
    with _TokenUsageSession() as usage_acc:
        # 1) 创意 Agent
        if should_cancel and should_cancel():
            raise TaskCancelled()
        if on_stage:
            on_stage("creative", "创意生成中")
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

        # 2) 规划 Agent
        if should_cancel and should_cancel():
            raise TaskCancelled()
        if on_stage:
            on_stage("planner", "规划生成中")
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

        if should_cancel and should_cancel():
            raise TaskCancelled()
        if on_stage:
            on_stage("executor", f"成品生成中（{executor_type}）")
        result.output = _run_executor(executor_type, exec_plan, mode, result)
        # 先落 token 用量，再归档 —— 归档记录要包含 usage 字段
        result.usage = dict(usage_acc)
        # 4) 自动归档：落盘元数据 + 把易失的远程产物下载到本地
        try:
            result.archived = archive_result(result, mode_id)
        except Exception:
            # 归档失败不应阻断开盒主流程
            result.archived = None
    return result


# ---------------------------------------------------------------------------
# 兼容导出层：让 `from . import pipeline` 后 `pipeline.<任意旧符号>` 继续可用。
# 各模块导入顺序即依赖方向（tools <- agents <- seed / executors / archive）。
# ---------------------------------------------------------------------------
from .agents import (  # noqa: E402
    _agent_call,
    _call_with_fallback,
    _executor_creds,
    _make_simple_call,
    _mock_agent,
    _normalize_base_url,
    _resolve_models,
)
from .seed import (  # noqa: E402
    _build_seed_prompt,
    _clean_seed_text,
    _core_image,
    _DIM_CN,
    _FALLBACK_POOL,
    _FALLBACK_TOOL_POOL,
    _generate_with_model,
    _get_dim_guide,
    _load_recent_themes,
    _RECENT_THEMES_FILE,
    _RECENT_THEMES_MAX,
    _remember_theme,
    _seems_anchored,
    _TEMPLATE_PREFIXES,
    draw_seed,
    generate_pool_seed,
    refresh_all_seeds,
)
from .tools import (  # noqa: E402
    _check_blocked_env,
    _DEFAULT_BLOCKED_ENV,
    _extract_block,
    _extract_caption,
    _extract_field,
    _extract_html,
    _extract_pages,
    _extract_shots,
    _first_choice_text,
    _first_image_item,
    _IFRAME_HEIGHT_REPORTER,
    _inject_height_reporter,
    _prepend_env_warning,
    _safe_relpath,
    _slugify,
    _strip_env_constraint_section,
    _translate_appearance,
)
from .executors import (  # noqa: E402
    _call_two_pass_game,
    _mock_game_html,
    _mock_quiz_html,
    _run_executor,
    _run_game_executor,
    _run_illustrated_story,
    _run_quiz_executor,
    _run_song,
    _run_tts,
    _run_video_storyboard,
)
from .archive import _download_remote, archive_result  # noqa: E402
# 兼容导出：拆分前 pipeline 模块直接持有 openai 库绑定，外部打桩/引用依赖它
from openai import OpenAI, APITimeoutError  # noqa: E402
