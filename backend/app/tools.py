"""拆分自 pipeline.py（tools.py）：无业务状态依赖的纯工具函数。

- 文件名安全化 / 结构化块提取 / 黑名单扫描 / iframe 高度注入等。
- 唯一的模块间依赖是 token_usage（_first_* 守卫顺带累加 token）。
"""
from __future__ import annotations

import re
import re as _re  # 历史别名：与 pipeline.py 原文件一致，两写法指向同一模块
from pathlib import Path
from typing import Any

from .token_usage import _accrue_usage


# 自定义模式允许的 executor_type / output_handler 枚举（与 pipeline._run_executor 分支一致）
_VALID_EXECUTOR_TYPES = {
    "text", "image", "video", "tts", "game", "quiz", "character_card",
    "code-spec", "code-cli", "illustrated_story", "video_storyboard", "song",
}
_VALID_OUTPUT_HANDLERS = {
    "text", "image_url", "video_file", "audio_file", "spec_md", "game_html",
    "quiz_html", "character_card", "image_strip", "song",
}


def validate_mode_config(mode: dict, mode_id: str) -> None:
    """验证模式配置的 executor_type 和 output_handler 枚举值。
    
    参数:
        mode: 模式配置字典
        mode_id: 模式 ID（用于错误信息）
    
    抛出:
        ValueError: 当 executor_type 或 output_handler 非法时
    """
    etype = mode.get("executor_type")
    if etype and etype not in _VALID_EXECUTOR_TYPES:
        raise ValueError(
            f"模式 {mode_id} 的 executor_type 非法: {etype}；"
            f"允许: {sorted(_VALID_EXECUTOR_TYPES)}"
        )
    
    oh = mode.get("output_handler")
    if oh and oh not in _VALID_OUTPUT_HANDLERS:
        raise ValueError(
            f"模式 {mode_id} 的 output_handler 非法: {oh}；"
            f"允许: {sorted(_VALID_OUTPUT_HANDLERS)}"
        )


def _safe_relpath(path: str, base: "Path") -> str | None:
    """安全地将 path 转为相对于 base 的路径；失败时返回 None。"""
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return None


def _first_image_item(resp: Any, ctx: str, model: str = "") -> Any:
    """取图像接口返回的第一个 item，空结果时抛出可读错误。

    直接写 `resp.data[0]` 在供应商返回 `{"data": []}`（触发内容审核、配额耗尽、
    参数被拒时很常见）或 data 为 null 时会抛 IndexError / TypeError，
    冒泡后只剩一句 "list index out of range"，完全看不出是哪个模型、哪一步失败。
    """
    _accrue_usage(resp, model)  # 顺带累加 token 用量（无 usage 时静默跳过）
    data = getattr(resp, "data", None)
    if not data:
        # 有的供应商把拒绝原因放在这些字段里，尽量带出来便于定位
        hint = ""
        for attr in ("error", "message", "msg", "detail"):
            val = getattr(resp, attr, None)
            if val:
                hint = f"，供应商附加信息: {val}"
                break
        raise RuntimeError(
            f"[{ctx}] model={model or '?'} 返回了空的图片列表（data 为空）"
            f"，常见原因：提示词被内容审核拒绝 / 配额耗尽 / 模型不支持该参数{hint}"
        )
    return data[0]


def _first_choice_text(resp: Any, ctx: str, model: str = "") -> str:
    """取 chat 接口第一个 choice 的文本，空结果时抛出可读错误。

    `resp.choices[0]` 在供应商因内容审核返回空 choices 时会抛 IndexError；
    统一在这里给出带上下文的错误信息。
    """
    _accrue_usage(resp, model)  # 顺带累加 token 用量（无 usage 时静默跳过）
    choices = getattr(resp, "choices", None)
    if not choices:
        finish = getattr(resp, "error", None) or getattr(resp, "message", None) or ""
        raise RuntimeError(
            f"[{ctx}] model={model or '?'} 返回了空的 choices"
            f"，常见原因：内容审核拦截 / 配额耗尽{('，供应商信息: ' + str(finish)) if finish else ''}"
        )
    msg = getattr(choices[0], "message", None)
    return (getattr(msg, "content", None) or "") if msg is not None else ""


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


_DEFAULT_BLOCKED_ENV: tuple[str, ...] = (
    "unreal", "ue5", "unity", "godot", "blender", "substance", "photoshop",
    "fmod", "wwise", "mysql", "sql server", "postgresql", "postgres", "oracle",
    "cuda", "docker", "android studio", "xcode", "maya", "3ds max",
)


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
