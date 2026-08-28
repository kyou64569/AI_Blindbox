"""拆分自 pipeline.py（models.py）。"""
from __future__ import annotations

from dataclasses import dataclass


class FallbackExhausted(Exception):
    """所有候选模型都调用失败（fallback 链耗尽）。"""

    def __init__(self, ctx: str, last: Exception | None = None):
        self.ctx = ctx
        self.last = last
        super().__init__(f"[fallback 耗尽] {ctx}: {last}")


class TaskCancelled(Exception):
    """异步开盒任务被用户取消（POST /api/task/{id}/cancel）。

    在 run_blind_box 的阶段切换处抛出，用于中断后续阶段；已产生的中间产物
    （如已完成的创意/规划文本）不落盘归档。
    """


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
    usage: dict | None = None
