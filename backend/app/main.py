"""FastAPI 入口：提供模式列表、开盲盒、配置读写接口。"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import config
from . import pipeline

app = FastAPI(title="AI 盲盒", version="0.1.0")
_logger = logging.getLogger("blindbox.api")

# 简单的内存速率限制器
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()
_rate_limit_key_ttl = 3600  # 每 IP 无请求 1 小时后自动清理
_RATE_LIMIT_WINDOW = 60  # 60秒窗口
_RATE_LIMIT_MAX_REQUESTS = 3  # 每窗口最多3次请求


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
    allow_headers=["Content-Type", "X-Admin-Token"],
)

SETTINGS_PATH = config.PROJECT_ROOT / "settings.yaml"
_config_lock = threading.Lock()

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
def list_history(mode: str | None = None, limit: int = 100):
    """列出 output/ 下所有归档记录，按修改时间倒序。

    - 可选 ?mode=<id> 按模式过滤；?limit=<n> 限制条数（默认 100，范围 1-1000）。
    - 读取异常的单条跳过，不影响整体。
    - 返回 {items: [...], total, returned}：total 为过滤后的总条数（不受 limit 影响），
      returned 为本次实际返回条数，前端据此判断「还有更多」。
    """
    # limit 未做边界限制时，?limit=0 会返回空列表、?limit=-1 会读全量，语义都不清晰
    try:
        limit = max(1, min(int(limit), 1000))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="limit 必须是整数")
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
        })
    return {"items": items, "total": total, "returned": len(items)}


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
    _assert_outbound_url_allowed(base_url)

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


@app.post("/api/open")
def open_box(req: OpenRequest):
    try:
        result = pipeline.run_blind_box(req.mode, req.seed)
        return {
            "mode": req.mode,
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
