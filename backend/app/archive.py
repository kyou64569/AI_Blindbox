"""拆分自 pipeline.py（archive.py）：开盒结果归档与远程产物下载。"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from .config import get_settings
from .models import BlindBoxResult
from .tools import _slugify

_log = logging.getLogger(__name__)


def _download_remote(url: str, out_dir: Path, stem: str, timeout: int = 120) -> str | None:
    """尝试把远程 URL 下载到本地 output 目录。成功返回本地相对路径，失败返回 None。"""
    try:
        import httpx
    except ImportError:
        _log.warning("[archive] httpx 未安装，无法下载远程文件: %s", url)
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
        else:
            _log.warning("[archive] 下载失败: HTTP %s, URL: %s", r.status_code, url)
    except Exception as e:
        _log.warning("[archive] 下载异常: %s, URL: %s", e, url)
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

    # 角色卡模式的附图可能是本地文件（b64_json 落盘 / 相对路径）：
    # 图片应是本模式的 local_file 主角，不能让下方的文本 md 抢占该位。
    if result.output_kind == "character_card" and isinstance(result.image_url, str) and result.image_url.strip():
        img_candidate = result.image_url.strip()
        img_path = out_dir / img_candidate if not Path(img_candidate).is_absolute() else Path(img_candidate)
        if img_path.exists():
            try:
                local_file = str(img_path.relative_to(out_dir))
            except ValueError:
                # 保留已有的 local_file 值，不覆盖为 None
                pass

    # 纯文本成品（诗歌/角色卡/命名/谜题/小说等）-> 额外落一份可读 md，
    # 方便文件系统直接查看与历史面板浏览；local_file 指向该 md。
    # 排除 URL / 已落盘的绝对路径（前者是远程成品，后者 code-spec/图片已处理）。
    # 角色卡（character_card）除外：其文本已有专属渲染（字段卡），local_file 留给附图。
    if (not local_file and isinstance(output_val, str) and output_val.strip()
            and not re.match(r"^https?://", output_val.strip())
            and not Path(output_val).is_absolute()
            and result.output_kind != "character_card"):
        mode_meta = get_settings().get_mode(mode_id) or {}
        mode_name = mode_meta.get("name", mode_id)
        md_content = (
            f"# {mode_name} · {ts}\n\n"
            f"> seed: {result.seed}\n\n---\n\n{output_val}\n"
        )
        md_path = out_dir / f"text_{_slugify(output_val)}_{uuid.uuid4().hex[:6]}.md"
        md_path.write_text(md_content, encoding="utf-8")
        local_file = str(md_path.relative_to(out_dir))

    # 文字成品文件（text_file）：让「图 + 文」类成品可分别访问文字与图片。
    # - 纯文本模式：local_file 已是 md，text_file 复用同一文件；
    # - 图片模式（有 caption / image_url）：把 caption 文字单独落一份可读 md。
    text_file: str | None = None
    if local_file and str(local_file).endswith(".md"):
        text_file = str(local_file)
    elif local_file and (result.image_url or result.caption):
        caption_text = (result.caption or "").strip()
        if caption_text:
            md_path = out_dir / f"text_{_slugify(caption_text, max_len=30)}_{uuid.uuid4().hex[:6]}.md"
            md_path.write_text(caption_text, encoding="utf-8")
            text_file = str(md_path.relative_to(out_dir))

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
        "text_file": text_file,
        "env_warning": result.env_warning,
        "favorite": False,      # 收藏标记（历史面板星标），轻量字段默认不收藏
        "rating": None,         # 评分 1-5，未评分为 None
        "usage": result.usage,  # token 用量统计（未启用/失败时为 None）
    }
    json_path = out_dir / f"result_{_slugify(result.concept, max_len=30)}_{rid}.json"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "json": str(json_path.relative_to(out_dir)),
        "local_file": local_file,
        "text_file": text_file,
        "timestamp": ts,
    }
