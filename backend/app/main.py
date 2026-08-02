"""FastAPI 入口：提供模式列表、开盲盒、配置读写接口。"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import config
from . import pipeline

app = FastAPI(title="AI 盲盒", version="0.1.0")


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
    # 兜底：记录异常信息，返回 500
    detail = f"{type(exc).__name__}: {exc}"
    return JSONResponse(
        status_code=500,
        content={"detail": detail},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SETTINGS_PATH = config.PROJECT_ROOT / "settings.yaml"
_config_lock = threading.Lock()


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


@app.get("/api/modes")
def list_modes():
    return {"modes": config.get_settings().modes}


@app.get("/api/history")
def list_history(mode: str | None = None, limit: int = 100):
    """列出 output/ 下所有归档记录，按修改时间倒序。

    - 可选 ?mode=<id> 按模式过滤；?limit=<n> 限制条数（默认 100）。
    - 读取异常的单条跳过，不影响整体。
    - 返回 {items: [{id,mode,timestamp,seed,output_kind,concept,plan,output,local_file,json,tts_info,caption}], total}。
    """
    out_dir = config.get_settings().output_dir
    files = sorted(
        out_dir.glob("result_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if mode and rec.get("mode") != mode:
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
            "json": p.name,
            "tts_info": rec.get("tts_info", ""),
            "model_used": rec.get("model_used", ""),
            "caption": rec.get("caption", ""),
            "image_url": rec.get("image_url", ""),
        })
        if len(items) >= limit:
            break
    return {"items": items, "total": len(items)}


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
    # 补全协议头
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    u = u.rstrip("/")
    # 用户可能直接填到了 /models 端点，回退到上一级
    if u.lower().endswith("/models"):
        u = u[: -len("/models")]
    return u


@app.post("/api/models")
def list_models(req: ModelsRequest):
    """给定 base_url + api_key，调用 OpenAI 兼容 /models 返回模型 id 列表，供前端下拉选择。"""
    import httpx
    from openai import OpenAI

    original = (req.base_url or "").strip()
    api_key = (req.api_key or "").strip()
    if not original:
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key 不能为空")

    base_url = _normalize_base_url(original)

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
def save_config(req: SaveConfigRequest):
    """Save configuration to settings.yaml and hot-reload in-memory.

    Uses a lock to ensure read/write consistency: concurrent requests
    won't see half-updated state.
    """
    with _config_lock:
        import yaml
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(req.config, f, allow_unicode=True, sort_keys=False)
        new_settings = config.load_settings(SETTINGS_PATH)
        config.set_settings(new_settings)
    return {"ok": True}


@app.post("/api/generate-seed")
def generate_seed(req: GenerateSeedRequest):
    """为指定 twist 维度调用大模型生成一条当前配置中没有的新种子词条。

    优先用请求携带的 existing；若为空则从 settings.yaml 的 pool 读取该维度现有条目，
    避免生成重复种子。返回 {"seed": "..."}。
    """
    dim = req.dimension
    pool = (config.get_settings().randomness or {}).get("pool", {}) or {}
    existing = req.existing or pool.get(dim, []) or []
    try:
        seed = pipeline.generate_pool_seed(dim, existing)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"seed": seed}


@app.post("/api/open")
def open_box(req: OpenRequest):
    try:
        result = pipeline.run_blind_box(req.mode, req.seed)
        return {
            "seed": result.seed,
            "concept": result.concept,
            "plan": result.plan,
            "output": result.output,
            "output_kind": result.output_kind,
            "tts_info": result.tts_info,
            "model_used": result.model_used,
            "caption": result.caption,
            "image_url": result.image_url,
            "archived": result.archived,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        target = (settings.output_dir / urllib.parse.unquote(path)).resolve()
        target.relative_to(settings.output_dir)  # Raises ValueError if outside
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在")
    if target.exists():
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail="文件不存在")
