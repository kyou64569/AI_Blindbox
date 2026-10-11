"""FastAPI 入口：提供模式列表、开盲盒、配置读写接口。"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import config
from . import pipeline
from .gemini import is_gemini_native
from .gemini import list_models as gemini_native_list_models
from .stream_bus import BROADCASTERS
from .tools import validate_mode_config

app = FastAPI(title="AI 盲盒", version="0.1.0")
_logger = logging.getLogger("blindbox.api")

# 简单的内存速率限制器
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()
_rate_limit_key_ttl = 3600  # 每 IP 无请求 1 小时后自动清理
_RATE_LIMIT_WINDOW = 60  # 60秒窗口
_RATE_LIMIT_MAX_REQUESTS = 3  # 每窗口最多3次请求

# 可配置的常量（可通过环境变量覆盖）
_ARCHIVE_LOCKS_MAX = int(os.environ.get("BLINDBOX_ARCHIVE_LOCKS_MAX", "100"))  # 限制锁字典大小
_ARCHIVE_LOCK_IDLE_TIMEOUT = int(os.environ.get("BLINDBOX_ARCHIVE_LOCK_IDLE_TIMEOUT", "300"))  # 锁空闲超时（秒）
_STATS_CACHE_TTL = int(os.environ.get("BLINDBOX_STATS_CACHE_TTL", "60"))  # 缓存有效期（秒）
_TASK_MAX = int(os.environ.get("BLINDBOX_TASK_MAX", "200"))  # 任务表最大容量
_TASK_MAX_CONCURRENT = int(os.environ.get("BLINDBOX_TASK_MAX_CONCURRENT", "2"))  # 并发任务上限


def _check_rate_limit(client_ip: str) -> bool:
    """检查客户端IP是否超过速率限制。返回True表示允许请求，False表示超限。"""
    current_time = time.time()
    with _rate_limit_lock:
        # 清理过期 key（超过 TTL 无请求的 IP）
        stale = [ip for ip, ts in _rate_limit_store.items() if not ts or (current_time - ts[-1] > _rate_limit_key_ttl)]
        for ip in stale:
            del _rate_limit_store[ip]

        # 清理过期的请求记录
        _rate_limit_store[client_ip] = [
            t for t in _rate_limit_store.get(client_ip, [])
            if current_time - t < _RATE_LIMIT_WINDOW
        ]
        # 检查是否超限
        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX_REQUESTS:
            return False
        # 记录本次请求
        _rate_limit_store[client_ip].append(current_time)
        return True


# ---------------------------------------------------------------------------
# 全局异常处理器
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic 参数校验失败 — 返回标准 422 而非 500。"""
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理，防止未捕获异常传播到 uvicorn 层级导致 worker 崩溃。

    当路由内部发生未被 HTTPException 捕获的异常时（如 RuntimeError、连接超时、
    API 调用失败等），返回结构化的 500 错误响应，而不是让异常冒泡到 uvicorn
    进程级别导致整个服务退出。
    """
    # 如果已经是 HTTPException，直接返回其原始状态码与细节
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    # 兜底：完整堆栈只写服务端日志，响应里只给一个可对账的 trace_id。
    # 直接回吐 f"{type(exc).__name__}: {exc}" 会把文件绝对路径、SQL、api_key 片段等
    # 内部细节暴露给调用方，属信息泄露。
    trace_id = uuid.uuid4().hex[:12]
    _logger.exception(
        "unhandled error trace_id=%s method=%s path=%s",
        trace_id, request.method, request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务内部错误，请查看后端日志排查",
            "trace_id": trace_id,
        },
    )

# CORS：本项目是本机运行的工具（uvicorn 绑 127.0.0.1），前端端口会在 5173-5192 / 8000-8019 间漂移，
# 因此用 origin 正则放通本机任意端口，而不是 allow_origins=["*"]。
# 通配符会让任意外部页面在用户浏览器里直接调用 /api/config 改写配置（配置里含 api_key），必须收紧。
# 如需允许其它来源，通过环境变量 BLINDBOX_EXTRA_ORIGINS（逗号分隔）显式追加。
_extra_origins = [
    o.strip() for o in (os.environ.get("BLINDBOX_EXTRA_ORIGINS") or "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_extra_origins,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    # Last-Event-ID 是 SSE 断线续传的标准请求头（EventSource 自动携带）
    allow_headers=["Content-Type", "X-Admin-Token", "Last-Event-ID"],
    expose_headers=["Last-Event-ID"],
)

SETTINGS_PATH = config.PROJECT_ROOT / "settings.yaml"
_config_lock = threading.Lock()
_archive_locks: dict[str, tuple[threading.Lock, float]] = {}  # (lock, last_used_time)
_archive_locks_lock = threading.Lock()

# Stats 缓存：避免每次请求都扫描所有归档文件
_stats_cache: dict[str, Any] = {}
_stats_cache_time: float = 0
_stats_cache_lock = threading.Lock()  # 保护缓存的原子性
_stats_cache_computing = False  # 标记是否正在计算缓存，防止并发重复计算


def _get_archive_lock(file_path: Path) -> threading.Lock:
    """获取指定归档文件的专用锁，避免并发更新冲突。"""
    key = str(file_path)
    current_time = time.time()
    with _archive_locks_lock:
        if key not in _archive_locks:
            _archive_locks[key] = (threading.Lock(), current_time)
        else:
            # 更新最后使用时间
            lock, _ = _archive_locks[key]
            _archive_locks[key] = (lock, current_time)
        
        # 定期清理：超过上限时移除空闲时间最长的锁
        if len(_archive_locks) > _ARCHIVE_LOCKS_MAX:
            # 按最后使用时间排序，移除空闲超过超时时间的锁
            sorted_locks = sorted(_archive_locks.items(), key=lambda kv: kv[1][1])
            to_remove = len(_archive_locks) - _ARCHIVE_LOCKS_MAX
            removed = 0
            for k, (_, last_used) in sorted_locks:
                if removed >= to_remove:
                    break
                # 只移除空闲超过超时时间的锁
                if current_time - last_used > _ARCHIVE_LOCK_IDLE_TIMEOUT:
                    del _archive_locks[k]
                    removed += 1
                # 如果所有锁都在使用中，不强制移除
                elif removed == 0:
                    break
        lock, _ = _archive_locks[key]
        return lock

# 可选管理令牌：设置了 BLINDBOX_ADMIN_TOKEN 后，写配置类接口必须带 X-Admin-Token 头。
# 不设置时保持默认行为（本机单用户场景免鉴权），避免破坏现有使用方式。
_ADMIN_TOKEN = (os.environ.get("BLINDBOX_ADMIN_TOKEN") or "").strip()

# settings.yaml 允许的顶层键；未知键一律拒绝，防止把整份配置写成任意结构后
# 让 load_settings/pipeline 在运行期各处炸掉。
_ALLOWED_TOP_KEYS = {
    "global", "mock", "agents", "randomness", "modes", "providers", "paths",
}


def _require_admin(request: Request) -> None:
    """写配置类接口的鉴权。未配置令牌时放行。"""
    if not _ADMIN_TOKEN:
        return
    token = (request.headers.get("X-Admin-Token") or "").strip()
    # 用 compare_digest 做定长比较，避免早退比较造成的时序侧信道
    import hmac
    if not token or not hmac.compare_digest(token, _ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="缺少或无效的 X-Admin-Token")




def _validate_config_payload(cfg: Any) -> dict[str, Any]:
    """对前端提交的整份配置做结构校验，通过后返回该 dict。

    只做「能挡住把配置写坏」的必要校验，不做业务级强约束，避免过度限制扩展。
    """
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="config 必须是一个对象")
    if not cfg:
        raise HTTPException(status_code=400, detail="config 不能为空对象（会清空 settings.yaml）")
    unknown = set(cfg) - _ALLOWED_TOP_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"config 含未知顶层键: {sorted(unknown)}；允许的键: {sorted(_ALLOWED_TOP_KEYS)}",
        )
    # 关键 section 的类型必须正确，否则 Settings 的各 property 会在运行期返回怪值
    for key, expect, tname in (
        ("global", dict, "对象"), ("agents", dict, "对象"),
        ("randomness", dict, "对象"), ("providers", dict, "对象"),
        ("paths", dict, "对象"), ("modes", list, "数组"),
    ):
        if key in cfg and cfg[key] is not None and not isinstance(cfg[key], expect):
            raise HTTPException(status_code=400, detail=f"config.{key} 必须是{tname}")
    for i, m in enumerate(cfg.get("modes") or []):
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail=f"config.modes[{i}] 必须是对象")
        if not str(m.get("id") or "").strip():
            raise HTTPException(status_code=400, detail=f"config.modes[{i}].id 不能为空")
        if not str(m.get("name") or "").strip():
            raise HTTPException(status_code=400, detail=f"config.modes[{i}].name 不能为空")
        # 使用集中的验证逻辑
        try:
            validate_mode_config(m, str(m.get("id", f"modes[{i}]")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    ids = [str(m.get("id")) for m in (cfg.get("modes") or []) if isinstance(m, dict)]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise HTTPException(status_code=400, detail=f"config.modes 存在重复 id: {sorted(dup)}")
    return cfg


def _write_settings_atomic(cfg: dict[str, Any]) -> None:
    """原子写 settings.yaml 并热更新单例；任一步失败则回滚到写入前内容。

    原实现是「直接覆盖写 → load_settings」，一旦新内容加载失败，
    磁盘上已是坏配置，下次进程启动会直接 FileNotFound/AttributeError 起不来。
    """
    backup = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.exists() else None
    tmp = SETTINGS_PATH.with_suffix(".yaml.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, SETTINGS_PATH)  # 同盘 replace 是原子操作
        config.set_settings(config.load_settings(SETTINGS_PATH))
    except Exception as e:
        # 回滚磁盘内容，内存单例保持原值（set_settings 未成功执行）
        if backup is not None:
            try:
                SETTINGS_PATH.write_bytes(backup)
            except Exception:
                _logger.exception("settings.yaml 回滚失败，磁盘内容可能已损坏")
        raise HTTPException(status_code=400, detail=f"配置保存失败，已回滚: {e}") from e
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


class OpenRequest(BaseModel):
    mode: str
    seed: str | None = None


class SaveConfigRequest(BaseModel):
    config: dict[str, Any]


class ModelsRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class GenerateSeedRequest(BaseModel):
    dimension: str
    existing: list[str] = []
    mode_id: str | None = None   # 指定模式时从该模式的独立池读取/写入


class RefreshAllSeedsRequest(BaseModel):
    # demo.html 存在「POST 不带 body」的调用，故整个 body 与该字段都可选
    mode_id: str | None = None


@app.get("/api/modes")
def list_modes():
    return {"modes": config.get_settings().modes}


@app.get("/api/history")
def list_history(mode: str | None = None, limit: int = 100, q: str = ""):
    """列出 output/ 下所有归档记录，按修改时间倒序。

    - 可选 ?mode=<id> 按模式过滤；?limit=<n> 限制条数（默认 100，范围 1-1000）；
      ?q=<关键词> 对 concept / plan / output 做大小写不敏感子串搜索。
    - 读取异常的单条跳过，不影响整体。
    - 返回 {items: [...], total, returned}：total 为过滤后的总条数（不受 limit 影响），
      returned 为本次实际返回条数，前端据此判断「还有更多」。
    """
    # limit 未做边界限制时，?limit=0 会返回空列表、?limit=-1 会读全量，语义都不清晰
    try:
        limit = max(1, min(int(limit), 1000))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="limit 必须是整数")
    kw = (q or "").strip().lower()
    if len(kw) > 200:
        raise HTTPException(status_code=400, detail="q 过长")
    out_dir = config.get_settings().output_dir
    files = sorted(
        out_dir.glob("result_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    total = 0  # 过滤后命中的总数，与 items 的截断长度解耦
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if mode and rec.get("mode") != mode:
            continue
        if kw:
            # 关键词搜索：concept/plan/output 任一命中即算（也顺带搜 seed，便于复现查找）
            haystack = " ".join(str(rec.get(k) or "") for k in ("concept", "plan", "output", "seed")).lower()
            if kw not in haystack:
                continue
        total += 1
        if len(items) >= limit:
            continue
        items.append({
            "id": rec.get("id"),
            "mode": rec.get("mode"),
            "timestamp": rec.get("timestamp"),
            "seed": rec.get("seed"),
            "output_kind": rec.get("output_kind"),
            "concept": rec.get("concept"),
            "plan": rec.get("plan"),
            "output": rec.get("output"),
            "local_file": rec.get("local_file"),
            "text_file": rec.get("text_file"),
            "json": p.name,
            "tts_info": rec.get("tts_info", ""),
            "model_used": rec.get("model_used", ""),
            "caption": rec.get("caption", ""),
            "image_url": rec.get("image_url", ""),
            "env_warning": rec.get("env_warning", ""),
            "favorite": bool(rec.get("favorite", False)),
            "rating": rec.get("rating"),
            "usage": rec.get("usage"),
            # 思考内容：历史页展开时要用。归档里本来就有（一直落盘），
            # 只是此前没往外返回，导致历史记录看不到思考过程。
            # 体积可控（thinking.py 有 _MAX_TOTAL=24000 上限），直接透传。
            "reasoning": rec.get("reasoning"),
        })
    return {"items": items, "total": total, "returned": len(items)}


def _find_archive_by_id(rid: str) -> "Path | None":
    """按归档 id 定位 result_*.json 文件（id 是归档记录里的 8 位短 id）。

    从 output 目录扫一遍匹配 id 字段；找不到返回 None。
    避免按文件名猜：文件名含 concept 摘要，不保证包含 id。
    """
    out_dir = config.get_settings().output_dir
    if not rid or len(rid) > 50:
        return None
    for p in out_dir.glob("result_*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("id") == rid:
            return p
    return None


def _patch_archive_field(rid: str, field: str, value: Any) -> dict:
    """就地更新归档 JSON 的单个轻量字段（favorite / rating 等），原子写回。

    返回 {"ok": True, field, value}；未找到返回 404。
    使用文件锁避免并发更新冲突。
    """
    p = _find_archive_by_id(rid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"未找到归档记录 id={rid}")
    lock = _get_archive_lock(p)
    with lock:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(status_code=500, detail="归档 JSON 读取失败（可能已损坏）")
        rec[field] = value
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    return {"ok": True, "field": field, "value": value}


class FavoriteRequest(BaseModel):
    favorite: bool = True


class RatingRequest(BaseModel):
    rating: int | None = None


@app.post("/api/history/{rid}/favorite")
def set_favorite(rid: str, req: FavoriteRequest):
    """收藏 / 取消收藏：{favorite: true|false}。就地写回归档 JSON。"""
    return _patch_archive_field(rid, "favorite", bool(req.favorite))


@app.post("/api/history/{rid}/rating")
def set_rating(rid: str, req: RatingRequest):
    """评分 1-5；传 null 清除评分。"""
    r = req.rating
    if r is not None and not (1 <= r <= 5):
        raise HTTPException(status_code=400, detail="rating 必须在 1-5 之间（传 null 清除）")
    return _patch_archive_field(rid, "rating", r)


@app.get("/api/export/{rid}/zip")
def export_zip(rid: str):
    """把一条归档记录打包成 ZIP 下载：归档 JSON + 本地成品文件（图片/音频/视频/md/html）。

    - 按 id 定位 result_*.json；从 record 的 local_file / text_file / image_url /
      output（song 的 audio / image_strip 的 local）等字段收集本地相对路径。
    - 所有文件从 output 目录读取（relative_to 校验归属），跨出目录的路径跳过。
    - 返回 application/zip 下载流；文件名含 concept 摘要（已净化）。
    """
    p = _find_archive_by_id(rid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"未找到归档记录 id={rid}")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="归档 JSON 读取失败（可能已损坏）")

    out_dir = config.get_settings().output_dir
    # 收集要打包的本地文件路径（去重、保留相对路径）
    rel_files: list[str] = [p.name]  # 归档 JSON 本身
    for key in ("local_file", "text_file", "image_url"):
        v = rec.get(key)
        if isinstance(v, str) and v and not re.match(r"^https?://", v) and not Path(v).is_absolute():
            rel_files.append(v.replace("\\", "/"))
    # output 字段可能是 JSON（song / image_strip），内嵌 audio / local 相对路径
    out_val = rec.get("output")
    if isinstance(out_val, str) and out_val.strip().startswith("{"):
        try:
            parsed = json.loads(out_val)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            for k in ("audio",):
                v = parsed.get(k)
                if isinstance(v, str) and v and not re.match(r"^https?://", v):
                    rel_files.append(v.replace("\\", "/"))
            for pg in (parsed.get("pages") or []):
                if isinstance(pg, dict):
                    v = pg.get("local")
                    if isinstance(v, str) and v and not re.match(r"^https?://", v):
                        rel_files.append(v.replace("\\", "/"))

    # 去重并校验目录归属
    seen: set[str] = set()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rel_files:
            if rel in seen:
                continue
            seen.add(rel)
            try:
                # 使用 strict=True 防止通过符号链接逃逸目录
                target = (out_dir / rel).resolve(strict=True)
                target.relative_to(out_dir.resolve())
            except (ValueError, OSError):
                continue
            if not target.is_file():
                continue
            try:
                zf.write(target, arcname=f"blindbox_{rid}/{rel}")
            except Exception:
                continue  # 单文件失败不拖垮整个导出
    buf.seek(0)
    stem = pipeline._slugify(rec.get("concept") or rec.get("mode") or "result", max_len=30) or "result"
    filename = f"blindbox_{stem}_{rid}.zip"
    # 中文文件名需 RFC 5987 编码（latin-1 header 无法直接承载中文）
    from urllib.parse import quote
    ascii_name = f"blindbox_{rid}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        },
    )


@app.get("/api/stats")
def get_stats():
    """开盒历史聚合统计：扫描 output/result_*.json 做轻量汇总。

    返回：
      total          开盒总次数（归档文件数）
      by_mode        {mode_id: 次数} 按模式
      by_model       {model: 次数} 按生成模型（model_used；无模型调用归入 "none"）
      success        产出非空（output 有内容）的次数
      success_rate   成功率（0~1）
      avg_rating     有评分的平均分（无评分时为 None）
      favorites      收藏数
      token_total    {prompt_tokens, completion_tokens, total_tokens} 累计 token（未启用时为 0）
      cost_total     按 global.tokens_price（¥/千 token）估算的累计成本（未配置单价为 0）

    结果缓存 60 秒，避免频繁扫描文件系统。
    """
    global _stats_cache, _stats_cache_time, _stats_cache_computing
    current_time = time.time()
    # 原子性检查缓存：避免并发请求同时绕过缓存
    with _stats_cache_lock:
        if _stats_cache and (current_time - _stats_cache_time) < _STATS_CACHE_TTL:
            return _stats_cache
        # 如果已有请求正在计算，等待其完成（简单轮询）
        if _stats_cache_computing:
            # 等待最多 5 秒，避免死锁
            for _ in range(50):
                time.sleep(0.1)
                if not _stats_cache_computing and _stats_cache:
                    return _stats_cache
            # 超时则继续计算（防御性）
        _stats_cache_computing = True

    try:
        out_dir = config.get_settings().output_dir
        by_mode: dict[str, int] = {}
        by_model: dict[str, int] = {}
        success = 0
        total = 0
        rating_sum = 0
        rating_cnt = 0
        favorites = 0
        t_prompt = t_completion = t_total = 0
        for p in out_dir.glob("result_*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            total += 1
            by_mode[rec.get("mode") or "unknown"] = by_mode.get(rec.get("mode") or "unknown", 0) + 1
            m = rec.get("model_used") or "none"
            by_model[m] = by_model.get(m, 0) + 1
            if (rec.get("output") or "").strip():
                success += 1
            r = rec.get("rating")
            if isinstance(r, (int, float)) and not isinstance(r, bool) and r >= 1:
                rating_sum += r
                rating_cnt += 1
            if rec.get("favorite"):
                favorites += 1
            u = rec.get("usage") or {}
            if isinstance(u, dict):
                try:
                    t_prompt += int(u.get("prompt_tokens") or 0)
                    t_completion += int(u.get("completion_tokens") or 0)
                    t_total += int(u.get("total_tokens") or 0)
                except (ValueError, TypeError):
                    pass  # 跳过格式错误的 usage 字段
        # 单价：settings.yaml global.tokens_price（¥/千 token），缺省 0 = 不估算成本
        price = config.get_settings().global_.get("tokens_price") or 0
        cost_total = round(t_total / 1000.0 * price, 4) if t_total else 0.0
        result = {
            "total": total,
            "by_mode": dict(sorted(by_mode.items(), key=lambda kv: -kv[1])),
            "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
            "success": success,
            "success_rate": round(success / total, 4) if total else 0.0,
            "avg_rating": round(rating_sum / rating_cnt, 2) if rating_cnt else None,
            "favorites": favorites,
            "token_total": {"prompt_tokens": t_prompt, "completion_tokens": t_completion, "total_tokens": t_total},
            "cost_total": cost_total,
            "tokens_price": float(price or 0),
        }
        # 原子性更新缓存
        with _stats_cache_lock:
            _stats_cache = result
            _stats_cache_time = current_time
        return result
    finally:
        with _stats_cache_lock:
            _stats_cache_computing = False


@app.get("/api/config")
def get_config():
    settings = config.get_settings()
    return {"config": settings.raw, "paths": {
        "cache_dir": str(settings.cache_dir),
        "output_dir": str(settings.output_dir),
        "data_dir": str(settings.data_dir),
    }}


def _normalize_base_url(raw: str) -> str:
    """清洗用户输入的 base_url：补全协议头、去尾斜杠、去多余的 /models 后缀。"""
    u = (raw or "").strip()
    if not u:
        return ""
    # 补全协议头：只在「完全没写协议」时补 https。
    # 原实现用 `not re.match(r"^https?://")` 判断，会把 file:///x 变成 https://file:///x —
    # 协议被悄悄改写，后续的协议白名单校验也就白做了。
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        u = "https://" + u
    u = u.rstrip("/")
    # 用户可能直接填到了 /models 端点，回退到上一级
    if u.lower().endswith("/models"):
        u = u[: -len("/models")]
    return u


# 云厂商实例元数据端点：一旦被当作 base_url，服务端会代为读取临时凭据，属典型 SSRF 目标
_METADATA_HOSTS = {
    "169.254.169.254", "metadata.google.internal", "metadata",
    "100.100.200.200",           # 阿里云
    "fd00:ec2::254",             # AWS IMDSv2 IPv6
}


def _assert_outbound_url_allowed(url: str) -> None:
    """对「服务端将要代为请求」的用户输入 URL 做最小必要校验。

    本服务只监听 127.0.0.1（见启动脚本 --host 127.0.0.1），因此不禁止 loopback 与
    内网地址 —— 用户用 Ollama / LM Studio / vLLM 等本地推理服务是正常用法，
    一刀切封 127.0.0.1 会直接砍掉主要场景。这里只挡真正无正当用途的部分：
      1) 非 http/https 协议（file:// gopher:// 等）
      2) URL 内嵌凭据（user:pass@host，易被用来做凭据注入/日志泄露）
      3) 云实例元数据服务地址
    """
    import ipaddress
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"base_url 协议不支持: {parsed.scheme or '(空)'}")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="base_url 不允许内嵌用户名/密码，请用 api_key 字段")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise HTTPException(status_code=400, detail="base_url 缺少主机名")
    if host in _METADATA_HOSTS:
        raise HTTPException(status_code=400, detail="base_url 指向云实例元数据服务，已拒绝")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local:  # 169.254.0.0/16 / fe80::/10，元数据服务常驻网段
            raise HTTPException(status_code=400, detail="base_url 指向链路本地地址，已拒绝")
    except ValueError:
        pass  # 是域名而非 IP，交由 DNS 处理


@app.post("/api/models")
def list_models(req: ModelsRequest):
    """给定 base_url + api_key，返回模型 id 列表，供前端下拉选择。

    自动分流：
      - Gemini 原生端点（generativelanguage.../v1beta，非 /openai）→ 走原生
        GET /models，解析 models[].name（OpenAI 兼容层的 data[].id 结构在这里不适用）
      - 其余 → OpenAI SDK models.list()，失败再退回原生 http GET {base}/models
    """
    import httpx
    from openai import OpenAI

    original = (req.base_url or "").strip()
    api_key = (req.api_key or "").strip()
    if not original:
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key 不能为空")

    base_url = _normalize_base_url(original)
    _assert_outbound_url_allowed(base_url)

    # 0) Gemini 原生端点：结构与鉴权都不同，先单独处理
    if is_gemini_native(base_url):
        try:
            ids = gemini_native_list_models(base_url, api_key)
            return {"models": ids, "provider": "gemini-native"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Gemini 原生模型列表获取失败：{type(e).__name__}: {e}\n"
                    f"已尝试地址：{base_url}/models?pageSize=1000\n"
                    "请确认：1) API Key 正确；2) base_url 填到 /v1beta（不要带 /openai）；"
                    "3) 网络可访问 generativelanguage.googleapis.com。"
                ),
            )

    last_err = ""

    # 1) 优先用 openai 库（大部分 OpenAI 兼容供应商适用）
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=15, max_retries=0)
        models = client.models.list()
        ids = [m.id for m in models.data] if getattr(models, "data", None) else []
        if ids:
            return {"models": ids}
        last_err = "供应商返回了空模型列表（data 为空）"
    except Exception as e:
        last_err = f"{type(e).__name__}: {e}"

    # 2) 退回原生 http GET {base_url}/models
    url = base_url.rstrip("/") + "/models"
    try:
        with httpx.Client(timeout=15, verify=True) as hc:
            r = hc.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                last_err = f"供应商返回 200，但响应不是 JSON：{r.text[:150]}"
                data = {}
            items = data.get("data", [])
            ids = [it.get("id") for it in items if it.get("id")]
            if ids:
                return {"models": ids}
            last_err = "供应商返回 200，但模型列表为空（data 字段无 id）"
        else:
            last_err = f"供应商返回 HTTP {r.status_code}：{r.text[:150]}"
    except Exception as e:
        last_err = f"{type(e).__name__}: {e}"

    # 构造可读诊断信息，帮助用户直接定位填错处
    diag = (
        f"获取模型列表失败：{last_err}\n"
        f"已尝试地址：{base_url}/models\n"
        f"原始输入：{original}\n"
    )
    low = last_err.lower()
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low or "cannot connect" in low or "connection" in low:
        diag += (
            "→ 该域名无法解析/连接。请检查：\n"
            "  1) base_url 是否包含 http(s):// 协议头（本程序会自动补全，但若域名拼写错误仍会失败）；\n"
            "  2) 域名是否拼写正确，是否带多余路径；\n"
            "  3) 当前网络能否访问该域名（是否需代理/公司内网/特殊网络）。\n"
            "  正确示例：https://api.你的供应商.com/v1"
        )
    else:
        diag += (
            "→ 地址可连通但调用未成功。请确认 base_url 形如 https://域名/v1（不要含末尾 /models），"
            "且 api_key 有效、该供应商支持 OpenAI 兼容的 /models 接口。"
        )
    raise HTTPException(status_code=502, detail=diag)


@app.post("/api/config")
def save_config(req: SaveConfigRequest, request: Request):
    """保存配置到 settings.yaml 并热更新内存单例。

    - 需要通过 _require_admin（配置了 BLINDBOX_ADMIN_TOKEN 时生效）
    - 提交内容先做结构校验，再原子写盘，失败自动回滚
    - 全程持锁，保证并发请求不会看到半更新状态
    """
    _require_admin(request)
    cfg = _validate_config_payload(req.config)
    with _config_lock:
        _write_settings_atomic(cfg)
    return {"ok": True}


@app.post("/api/generate-seed")
def generate_seed(req: GenerateSeedRequest):
    """为指定 twist 维度调用大模型生成一条当前配置中没有的新种子词条。

    优先用请求携带的 existing；若为空则从设置中读取该维度现有条目，
    避免生成重复种子。mode_id 指定时读取该模式的独立池。返回 {"seed": "..."}。
    """
    dim = req.dimension
    settings = config.get_settings()
    if req.mode_id:
        mode = settings.get_mode(req.mode_id) or {}
        mode_pool = mode.get("pool") or {}
        pool = mode_pool
    else:
        pool = (settings.randomness or {}).get("pool", {}) or {}
    existing = req.existing or pool.get(dim, []) or []
    try:
        seed = pipeline.generate_pool_seed(dim, existing, req.mode_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"seed": seed}


@app.post("/api/seed/refresh-all")
def refresh_all_seeds(request: Request, req: RefreshAllSeedsRequest | None = None):
    """一键刷新全部种子池——每维度调用 generate_pool_seed 逐个生成新种子。

    保持各维度种子数量不变，将所有旧种子替换为独立随机生成的新种子。
    body 可带 mode_id：指定模式时刷新该模式的独立池（若已配置），否则刷新全局池。
    返回 {pool: {dim: [seed, ...]}}，前端可直接 setConfig 使用。

    注意：mode_id 必须通过 Pydantic body model 取。原实现在同步函数里写
    `body = request.json()`（未 await，返回的是协程），`.get` 必然抛
    AttributeError 并被 except 吞掉，导致 mode_id 恒为 None ——
    前端「刷新 code 模式独立池」实际一直在改全局池。
    """
    _require_admin(request)
    # 速率限制：防止恶意调用消耗API配额
    client_ip = request.client.host if request.client and request.client.host else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请在 {_RATE_LIMIT_WINDOW} 秒后再试"
        )

    mode_id = (req.mode_id or "").strip() if req else None

    # 同步写入 settings.yaml —— 单次持锁，覆盖「读 raw → 生成 → 写盘 → 重载 → 热更新单例」全过程。
    # 将 pool 生成移入锁内，避免并发请求生成相同 pool 后竞争写入
    with _config_lock:
        # 深拷贝后再改：原实现直接改 get_settings().raw，写盘失败时内存单例已被污染
        raw = copy.deepcopy(config.get_settings().raw)
        if mode_id and not any(
            isinstance(m, dict) and m.get("id") == mode_id for m in raw.get("modes", [])
        ):
            # 先校验模式存在，避免白跑一轮 LLM 生成后才发现 404、浪费配额
            raise HTTPException(status_code=404, detail=f"模式 {mode_id} 不存在")

        new_pool = pipeline.refresh_all_seeds(mode_id)

        if mode_id:
            for m in raw.get("modes", []):
                if isinstance(m, dict) and m.get("id") == mode_id:
                    m["pool"] = new_pool
                    break
        else:
            if not isinstance(raw.get("randomness"), dict):
                raw["randomness"] = {}
            raw["randomness"]["pool"] = new_pool
        _write_settings_atomic(raw)
    return {"pool": new_pool}


# ---------------------------------------------------------------------------
# 异步开盒任务：POST /api/open 立即返回 task_id，后台线程跑管道；
# GET /api/task/{id} 轮询进度；POST /api/task/{id}/cancel 取消。
# ---------------------------------------------------------------------------
# 内存任务表：{task_id: {...}}。仅存最近 _TASK_MAX 个，防止长期运行泄漏。
_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()
_STAGE_ORDER = ["creative", "planner", "executor"]
# 同时运行的开盒任务上限：真实模型（尤其多模态）普遍有 TPM/并发配额，
# 批量开盒全部并发会触发 429（实测同批 2 个 image 任务就 TPM 打爆）。
# 用信号量让任务排队执行，超出的任务保持「排队中」状态直到有空位。
_TASK_SEMAPHORE = threading.BoundedSemaphore(_TASK_MAX_CONCURRENT)


def _result_payload(result: Any) -> dict:
    """把 BlindBoxResult 序列化为前端可直接消费的 dict（与同步版一致）。"""
    return {
        "mode": result.mode if hasattr(result, "mode") else "",
        "seed": result.seed,
        "concept": result.concept,
        "plan": result.plan,
        "output": result.output,
        "output_kind": result.output_kind,
        "tts_info": result.tts_info,
        "model_used": result.model_used,
        "caption": result.caption,
        "image_url": result.image_url,
        "env_warning": result.env_warning,
        "reasoning": getattr(result, "reasoning", None),
        "usage": result.usage,
        "archived": result.archived,
    }


def _task_cleanup() -> None:
    """裁剪任务表，只保留最近的 _TASK_MAX 个已完成任务（按完成时间）。
    
    只清理已完成（done/error/cancelled）的任务，避免删除正在运行的任务。
    """
    with _TASKS_LOCK:
        if len(_TASKS) <= _TASK_MAX:
            return
        # 只考虑已完成的任务
        completed_tasks = [
            (tid, task) for tid, task in _TASKS.items()
            if task.get("status") in ("done", "error", "cancelled")
        ]
        if len(completed_tasks) <= _TASK_MAX:
            return
        # 按开始时间升序，淘汰最老的已完成任务
        ordered = sorted(completed_tasks, key=lambda kv: kv[1].get("started_at", 0))
        to_remove = len(completed_tasks) - _TASK_MAX
        for tid, _ in ordered[:to_remove]:
            _TASKS.pop(tid, None)


def _run_task_background(task_id: str, mode_id: str, seed: str | None) -> None:
    """后台线程：跑 run_blind_box，用回调更新任务状态。任何异常都落回任务表。

    受 _TASK_SEMAPHORE 限制：并发超限的任务在 acquire 处阻塞，状态保持
    「排队中」直到拿到执行位，避免批量开盒打爆模型 TPM 配额。
    """
    started = time.time()
    cancel_flag = {"cancelled": False}
    # 排队等待执行位；取消时提前退出（避免无谓排队）
    while not _TASK_SEMAPHORE.acquire(timeout=1.0):
        with _TASKS_LOCK:
            cancelled = bool(_TASKS.get(task_id, {}).get("cancelled"))
        if cancelled:
            with _TASKS_LOCK:
                t = _TASKS.get(task_id)
                if t is not None:
                    t["status"] = "cancelled"
                    t["stage_label"] = "已取消（排队中）"
                    t["elapsed"] = round(time.time() - started, 1)
            return
    try:
        # 拿到执行位，正式开始
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is not None:
                t["stage_label"] = "启动中"
        _run_pipeline_task(task_id, mode_id, seed, started)
    finally:
        _TASK_SEMAPHORE.release()


def _run_pipeline_task(task_id: str, mode_id: str, seed: str | None, started: float) -> None:
    """真正执行管道（已持有执行位）。"""
    bc = BROADCASTERS.get(task_id)

    def on_stage(stage: str, label: str) -> None:
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is None:
                return
            t["stage"] = stage
            t["stage_label"] = label
            try:
                t["progress"] = (_STAGE_ORDER.index(stage) + 1) / len(_STAGE_ORDER)
            except ValueError:
                t["progress"] = 0.0
            t["elapsed"] = round(time.time() - started, 1)
        # 同步推给 SSE 订阅者：切换阶段时先隐式结束上一段思考流
        if bc is not None and not bc.closed:
            bc.stage(stage, label)

    def should_cancel() -> bool:
        with _TASKS_LOCK:
            return bool(_TASKS.get(task_id, {}).get("cancelled"))

    def on_delta(text: str) -> None:
        """思考增量：实时推到 SSE（前端思考面板逐字显示）。"""
        if bc is None or bc.closed or not text:
            return
        from .thinking import current_model
        bc.reasoning_start(current_stage(), current_model())
        bc.reasoning_delta(text)

    def current_stage() -> str:
        with _TASKS_LOCK:
            return str(_TASKS.get(task_id, {}).get("stage_label") or "")

    try:
        result = pipeline.run_blind_box(
            mode_id, seed,
            on_stage=on_stage,
            should_cancel=should_cancel,
            on_delta=on_delta,
        )
        payload = _result_payload(result)
        payload["mode"] = mode_id
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is None:
                return
            t["status"] = "done"
            t["stage"] = "done"
            t["stage_label"] = "已完成"
            t["progress"] = 1.0
            t["elapsed"] = round(time.time() - started, 1)
            t["result"] = payload
        if bc is not None and not bc.closed:
            bc.finish(payload)
    except pipeline.TaskCancelled:
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is not None:
                t["status"] = "cancelled"
                t["stage_label"] = "已取消"
                t["elapsed"] = round(time.time() - started, 1)
        if bc is not None and not bc.closed:
            bc.fail("已取消", status="cancelled")
    except Exception as e:  # noqa: BLE001
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is not None:
                t["status"] = "error"
                t["stage_label"] = "失败"
                t["elapsed"] = round(time.time() - started, 1)
                t["error"] = f"{type(e).__name__}: {e}"
        # 关键：把失败任务的具体异常打到后端日志，下次排查不用抓瞎
        _logger.exception(
            "开盒任务失败: task=%s mode=%s elapsed=%.1fs error=%s",
            task_id, mode_id, time.time() - started, t["error"] if t else "?",
        )
        if bc is not None and not bc.closed:
            bc.fail(f"{type(e).__name__}: {e}", status="error")
    finally:
        # 任务终结即注销广播器（事件已 flush 到订阅者，订阅端会收到 end 后自行关闭）
        BROADCASTERS.drop(task_id)


def _open_task(mode_id: str, seed: str | None) -> dict:
    """创建异步开盒任务，立即返回 task_id。"""
    task_id = uuid.uuid4().hex[:12]
    now = time.time()
    # 先建广播器再起线程：保证任务一开始推的事件就有订阅者能收到
    bc = BROADCASTERS.create(task_id)
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "status": "running",
            "stage": "creative",
            "stage_label": "排队中",
            "progress": 0.0,
            "elapsed": 0.0,
            "started_at": now,
            "mode": mode_id,
            "seed": seed,
            "cancelled": False,
            "error": None,
            "result": None,
        }
    # 注意：必须在 _TASKS_LOCK 块外调用（_task_cleanup 内部自带同一把锁，
    # threading.Lock 不可重入，锁内调用会死锁导致 POST /api/open 永久挂起）。
    _task_cleanup()
    # 先推一条初始 stage，订阅者一连上就能立刻看到状态（不必等 1.2s 轮询）
    bc.stage("creative", "排队中")
    t = threading.Thread(
        target=_run_task_background,
        args=(task_id, mode_id, seed),
        daemon=True,
        name=f"blindbox-task-{task_id}",
    )
    t.start()
    return {"task_id": task_id, "status": "running"}


class CancelRequest(BaseModel):
    pass  # 取消无需 body；POST 空 body 即可


class OpenBatchRequest(BaseModel):
    mode: str
    count: int = 3          # 连开数量，1-6
    seed: str | None = None  # 可选：批量共用同一种子（确定性对比），缺省各自随机


@app.post("/api/open")
def open_box(req: OpenRequest):
    """创建开盒任务并立即返回 task_id（异步）。

    兼容：响应结构与原同步版不同（多了 task_id），前端需改轮询 GET /api/task/{id}。
    """
    return _open_task(req.mode, req.seed)


@app.post("/api/open-batch")
def open_batch(req: OpenBatchRequest):
    """批量开盒：一次创建 N 个异步任务，返回 task_ids 列表。

    - count 限制 1-6（防止一次并发过多导致 API 限流 / 内存压力）。
    - 每个任务独立跑 run_blind_box（各自有进度与结果），前端并排轮询。
    - seed 提供时所有任务共用该种子（适合对比同一种子不同模型）；缺省各自随机。
    - 若传入的 mode 不存在，先 404，避免白跑 N 个任务。
    """
    if not (1 <= req.count <= 6):
        raise HTTPException(status_code=400, detail="count 必须在 1-6 之间")
    settings = config.get_settings()
    if settings.get_mode(req.mode) is None:
        raise HTTPException(status_code=404, detail=f"模式 {req.mode} 不存在")
    tasks: list[dict] = []
    for _ in range(req.count):
        tasks.append(_open_task(req.mode, req.seed))
    return {"mode": req.mode, "count": len(tasks), "tasks": tasks}


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    """轮询任务状态：{status: running|done|error|cancelled, stage, stage_label,
    progress(0~1), elapsed(秒), result?, error?}。"""
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或已过期")
    return {
        "task_id": task_id,
        "status": t["status"],
        "stage": t["stage"],
        "stage_label": t["stage_label"],
        "progress": t["progress"],
        "elapsed": t["elapsed"],
        "mode": t["mode"],
        "result": t.get("result"),
        "error": t.get("error"),
    }


@app.post("/api/task/{task_id}/cancel")
def cancel_task(task_id: str):
    """取消任务：置 cancel 标志，run_blind_box 在阶段切换处抛 TaskCancelled 中断。"""
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或已过期")
        if t["status"] in ("done", "error", "cancelled"):
            return {"task_id": task_id, "status": t["status"], "cancelled": False}
        t["cancelled"] = True
    # 立即让 SSE 订阅者知道（不用等任务线程跑到下一个检查点）
    bc = BROADCASTERS.get(task_id)
    if bc is not None and not bc.closed:
        bc.fail("已取消（用户主动取消）", status="cancelling")
    return {"task_id": task_id, "status": "cancelling", "cancelled": True}


@app.get("/api/task/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    """SSE 实时推送任务事件：阶段切换 + 思考增量。

    事件类型（data 为 JSON）：
      stage           {seq, stage, label}                阶段切换
      reasoning_start {seq, stage, model}                 一段思考开始
      reasoning_delta {seq, text}                         思考增量（逐字）
      reasoning_end   {seq}                               一段思考结束
      done            {seq, result, status:"done"}        成功
      error           {seq, error, status:"error"}        失败
      cancelled       {seq, error, status:"cancelled"}    取消
      end             {}                                   流结束（前端关闭 EventSource）

    支持 Last-Event-ID 断线续传（浏览器 EventSource 自动带），也可用
    ?last_event_id= 手动指定。任务已完成时立即补发 done 并收尾，
    因此前端即使在任务结束后才连上也能拿到最终结果。

    注意：即使没人在订阅，事件也会缓存在广播器里（有上限），
    这样「先开盒、后打开面板」的顺序也能正确补齐思考内容。
    """
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或已过期")

    bc = BROADCASTERS.get(task_id)
    # 任务已结束且广播器已注销 -> 无法再补发，直接把任务表里的终态回给前端
    if bc is None or bc.closed:
        if t["status"] in ("done", "error", "cancelled"):
            async def _replay():
                payload = {
                    "type": t["status"] if t["status"] != "done" else "done",
                    "seq": 0,
                    "status": t["status"],
                }
                if t["status"] == "done":
                    payload["result"] = t.get("result")
                else:
                    payload["error"] = t.get("error") or t.get("stage_label") or ""
                yield f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "event: end\ndata: {}\n\n"
            return StreamingResponse(
                _replay(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # 仍在排队/运行但广播器已丢（极端情况）-> 退化为纯状态轮询提示
        async def _idle():
            yield ": no broadcaster\n\n"
            while not await request.is_disconnected():
                yield ": keepalive\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(_idle(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # Last-Event-ID 由浏览器自动带；也支持 query 手动指定（便于测试）
    last_id = 0
    hdr = request.headers.get("last-event-id")
    if hdr and hdr.isdigit():
        last_id = int(hdr)
    else:
        q = request.query_params.get("last_event_id", "")
        if q.isdigit():
            last_id = int(q)

    async def _gen():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def _pump():
            """在线程池里跑同步的 SSE 生成器，把帧塞进 asyncio 队列。"""
            try:
                for frame in bc.stream(last_id):
                    loop.call_soon_threadsafe(queue.put_nowait, frame)
            except Exception:  # noqa: BLE001
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)

        task = loop.run_in_executor(None, _pump)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # 心跳，防中间代理掐断长连接
                    continue
                if item is SENTINEL:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx/代理的响应缓冲，否则 SSE 会被攒包，实时性全丢
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
def health():
    return {"status": "ok", "mock": config.get_settings().mock}


@app.get("/api/tts/voices")
def list_tts_voices(base_url: str = "", api_key: str = ""):
    """列出可用的 TTS 音色。

    优先用 query 参数；若为空则回退到当前 settings.yaml 的 providers.tts 凭据。
    """
    base_url = base_url.strip()
    api_key = api_key.strip()
    if not base_url or not api_key:
        prov = (config.get_settings().providers.get("tts", {}) or {})
        base_url = base_url or (prov.get("base_url") or "").strip()
        api_key = api_key or (prov.get("api_key") or "").strip()

    edge_voices: list[dict] = []
    try:
        import asyncio, edge_tts
        async def _get():
            return await edge_tts.list_voices()
        voices = asyncio.run(_get())
        if isinstance(voices, dict):
            voices = voices.get("value", voices.get("voices", []))
        for v in voices:
            name = v.get("ShortName", v.get("Name", ""))
            locale = v.get("Locale", "")
            gender = v.get("Gender", "")
            if "zh" in locale.lower():
                edge_voices.append({
                    "id": name,
                    "name": f"{name} ({locale} {gender})",
                    "engine": "edge-tts",
                })
    except Exception as e:
        import logging
        logging.warning(f"Failed to fetch Edge TTS voices: {type(e).__name__}: {e}")
        edge_voices = [
            {"id": "zh-CN-XiaoxiaoNeural", "name": "xiaoxiao (CN 女)", "engine": "edge-tts"},
            {"id": "zh-CN-YunyangNeural", "name": "yunyang (CN 男)", "engine": "edge-tts"},
            {"id": "zh-CN-XiaoyiNeural", "name": "xiaoyi (CN 女)", "engine": "edge-tts"},
        ]

    cloud_voices: list[dict] = []
    if base_url and api_key:
        # 与 /api/models 同样的出站 URL 校验：这里也会由服务端代为请求用户给的地址
        _assert_outbound_url_allowed(_normalize_base_url(base_url))
        # Attempt to fetch voices from the configured TTS provider
        import urllib.request, json as jmod
        candidates = [
            base_url.rstrip("/") + "/audio/voices",
            base_url.rstrip("/") + "/v1/audio/voices",
            base_url.rstrip("/") + "/voices",
        ]
        for url in candidates:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = jmod.loads(resp.read())
                items = data.get("data") or data.get("voices") or data.get("items") or []
                for v in items:
                    vid = v.get("id") or v.get("name") or v.get("voice_id") or ""
                    vname = v.get("name") or v.get("display_name") or vid
                    if vid:
                        cloud_voices.append({"id": vid, "name": vname, "engine": "cloud"})
                if cloud_voices:
                    break
            except Exception:
                continue
        if not cloud_voices:
            cloud_voices.append({
                "id": "__custom__",
                "name": "* 提供商未暴露音色列表，请下方文本框手动输入音色名",
                "engine": "cloud",
            })

    if not cloud_voices:
        cloud_voices = [
            {"id": "alloy", "name": "alloy (中性·OpenAI)", "engine": "cloud"},
            {"id": "echo", "name": "echo (男·OpenAI)", "engine": "cloud"},
            {"id": "fable", "name": "fable (英式男·OpenAI)", "engine": "cloud"},
            {"id": "onyx", "name": "onyx (深沉男·OpenAI)", "engine": "cloud"},
            {"id": "nova", "name": "nova (女·OpenAI)", "engine": "cloud"},
            {"id": "shimmer", "name": "shimmer (女·OpenAI)", "engine": "cloud"},
        ]

    current = (config.get_settings().providers.get("tts", {}) or {}).get("voice", "")
    return {"edge": edge_voices, "cloud": cloud_voices, "current": current}


@app.get("/output/{path:path}")
def serve_output(path: str):
    """预览/下载开盒归档产物（限制在 output_dir 内，防目录穿越）。"""
    import urllib.parse
    try:
        settings = config.get_settings()
        # 基准目录也必须 resolve：output_dir 若含 .. 或本身是符号链接/junction，
        # 只 resolve target 一侧会让 relative_to 误判（拿未规范化的路径去比规范化的路径）。
        base = settings.output_dir.resolve()
        rel = urllib.parse.unquote(path)
        # 绝对路径 / 带盘符的输入会让 `base / rel` 直接跳出 base，先挡掉
        if not rel or Path(rel).is_absolute() or Path(rel).drive or rel.startswith(("/", "\\")):
            raise HTTPException(status_code=404, detail="文件不存在")
        target = (base / rel).resolve()
        target.relative_to(base)  # 落在 base 之外时抛 ValueError
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在")
    # 必须是普通文件：目录会让 FileResponse 抛 500；这里统一按 404 处理
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(target))
