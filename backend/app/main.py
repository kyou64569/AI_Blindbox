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

from .config import PROJECT_ROOT, SETTINGS, load_settings, set_settings
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

SETTINGS_PATH = PROJECT_ROOT / "settings.yaml"
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
    return {"modes": SETTINGS.modes}


@app.get("/api/history")
def list_history(mode: str | None = None, limit: int = 100):
    """列出 output/ 下所有归档记录，按修改时间倒序。

    - 可选 ?mode=<id> 按模式过滤；?limit=<n> 限制条数（默认 100）。
    - 读取异常的单条跳过，不影响整体。
    - 返回 {items: [{id,mode,timestamp,seed,output_kind,concept,output,local_file,json}], total}。
    """
    out_dir = SETTINGS.output_dir
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
            "output": rec.get("output"),
            "local_file": rec.get("local_file"),
            "json": p.name,
        })
        if len(items) >= limit:
            break
    return {"items": items, "total": len(items)}


@app.get("/api/config")
def get_config():
    return {"config": SETTINGS.raw, "paths": {
        "cache_dir": str(SETTINGS.cache_dir),
        "output_dir": str(SETTINGS.output_dir),
        "data_dir": str(SETTINGS.data_dir),
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
        new_settings = load_settings(SETTINGS_PATH)
        set_settings(new_settings)
        pipeline.SETTINGS = new_settings
    return {"ok": True}


@app.post("/api/generate-seed")
def generate_seed(req: GenerateSeedRequest):
    """为指定 twist 维度调用大模型生成一条当前配置中没有的新种子词条。

    优先用请求携带的 existing；若为空则从 settings.yaml 的 pool 读取该维度现有条目，
    避免生成重复种子。返回 {"seed": "..."}。
    """
    dim = req.dimension
    pool = (SETTINGS.randomness or {}).get("pool", {}) or {}
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
            "archived": result.archived,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def health():
    return {"status": "ok", "mock": SETTINGS.mock}


@app.get("/output/{path:path}")
def serve_output(path: str):
    """预览/下载开盒归档产物（限制在 output_dir 内，防目录穿越）。"""
    import urllib.parse
    target = (SETTINGS.output_dir / urllib.parse.unquote(path)).resolve()
    if str(target).startswith(str(SETTINGS.output_dir)) and target.exists():
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail="文件不存在")
