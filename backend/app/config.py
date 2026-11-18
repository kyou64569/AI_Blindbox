"""配置加载与路径管理。

所有体积较大文件（缓存/产物/上传）默认放在项目目录内，避免写入系统盘。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

# 项目根：本文件位于 backend/app/config.py，上溯一级到 backend。
# 桌面版（PyInstaller）里 __file__ 会落到临时解压目录 _MEIPASS —— 只读且每次启动都变，
# 若沿用会把 settings.yaml / output / data 写进去，配置里的 API Key 关窗即丢。
# 因此允许桌面启动器用 BLINDBOX_HOME 显式指定可写的数据根目录。
# 网页版不设置该环境变量 → 解析结果与改动前逐字节一致。
_home_override = os.environ.get("BLINDBOX_HOME", "").strip()
PROJECT_ROOT = (
    Path(_home_override).expanduser().resolve()
    if _home_override
    else Path(__file__).resolve().parents[1]
)

# 大文件路径默认绑定到项目目录内，避免写入系统盘用户敏感目录
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def _as_path(value: str | None, default: Path) -> Path:
    """把配置中的路径字符串解析为绝对路径；若为空或不安全（落在系统盘用户敏感目录），回退默认。"""
    if not value:
        return default
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    # 安全兜底：不允许落进系统盘（Windows 的 C:）用户敏感目录
    dangerous = ("AppData", "Desktop", "Documents", "Downloads", "Program Files")
    parts = set(p.parts)
    if p.drive.upper() == "C:" and any(d in parts for d in dangerous):
        return default
    return p


class Settings:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        paths = raw.get("paths", {}) or {}
        self.cache_dir = _as_path(paths.get("cache_dir"), DEFAULT_CACHE_DIR)
        self.output_dir = _as_path(paths.get("output_dir"), DEFAULT_OUTPUT_DIR)
        self.data_dir = _as_path(paths.get("data_dir"), DEFAULT_DATA_DIR)
        for d in (self.cache_dir, self.output_dir, self.data_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def global_(self) -> dict[str, Any]:
        return self.raw.get("global", {}) or {}

    @property
    def mock(self) -> bool:
        return bool(self.raw.get("mock", False))

    @property
    def agents(self) -> dict[str, Any]:
        return self.raw.get("agents", {}) or {}

    @property
    def randomness(self) -> dict[str, Any]:
        return self.raw.get("randomness", {}) or {}

    @property
    def modes(self) -> list[dict[str, Any]]:
        return self.raw.get("modes", []) or []

    @property
    def providers(self) -> dict[str, Any]:
        return self.raw.get("providers", {}) or {}

    @property
    def connections(self) -> list[dict[str, Any]]:
        """可复用的连接池（base_url + api_key），供各模型槽位按 id 引用。

        结构：[{id, name, base_url, api_key}]。老配置没有该字段时返回空列表，
        所有槽位继续走自身 base_url/api_key 的老路径，行为不变。
        """
        conns = self.raw.get("connections")
        return conns if isinstance(conns, list) else []

    def get_mode(self, mode_id: str) -> dict[str, Any] | None:
        for m in self.modes:
            if m.get("id") == mode_id:
                return m
        return None


def _normalize_section(section: "dict | None") -> "dict | None":
    """把旧的扁平 {model,base_url,api_key,temperature} 规范成多模型 {models:[...]}。

    - 若 section 有真值 model 且（models 不存在或为空列表）→ 包成 models=[{...}]。
    - 若 model 为空（如 executor 当前 model: ''）→ 设 models=[]（不包空卡片）。
    - 幂等：已有非空 models 不再包。
    - 同时保留原有扁平 model/base_url/api_key/temperature 作为组级默认值。
    """
    if not isinstance(section, dict):
        return section
    models = section.get("models")
    if isinstance(models, list) and len(models) > 0:
        return section  # 已有非空 models，幂等不重复包
    model = (section.get("model") or "").strip()
    if model:
        section["models"] = [{
            "model": model,
            "base_url": section.get("base_url") or "",
            "api_key": section.get("api_key") or "",
            "temperature": section.get("temperature", None),
        }]
    else:
        section["models"] = []
    return section


def build_connection_map(connections: Any) -> dict[str, dict[str, Any]]:
    """把 connections 列表转成 {id: conn}，忽略没有 id 的脏数据。"""
    out: dict[str, dict[str, Any]] = {}
    for c in (connections or []):
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if cid:
            out[cid] = c
    return out


def conn_creds(
    conn_map: dict[str, dict[str, Any]] | None,
    ref: Any,
    base_url: str = "",
    api_key: str = "",
) -> tuple[str, str]:
    """按连接引用取凭据：ref 命中则用连接的 base_url/api_key，否则回退传入值。

    悬空引用（引用了一个已删除的连接 id）不抛错、静默回退到槽位自身的值 ——
    即使 settings.yaml 被手工改坏，也不会让整条 Agent 链起不来。
    """
    conn = (conn_map or {}).get(str(ref or "").strip())
    if isinstance(conn, dict):
        base_url = conn.get("base_url") or base_url
        api_key = conn.get("api_key") or api_key
    return base_url, api_key


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path) if path else (PROJECT_ROOT / "settings.yaml")
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    # 空文件 / 只有注释时 safe_load 返回 None；顶层写成 list/str 时类型也不对。
    # 这里统一兜底，避免后续 raw.get(...) 抛 AttributeError 导致进程起不来。
    if raw is None:
        raw = {}
    elif not isinstance(raw, dict):
        raise ValueError(
            f"配置文件格式错误: {path} 顶层应为映射(dict)，实际为 {type(raw).__name__}"
        )
    # 多模型规范化：把 agents / providers 各 section 的扁平 model 升级为 models 列表
    agents = raw.get("agents", {}) or {}
    for ak in ("creative", "planner", "executor"):
        _normalize_section(agents.get(ak))
    providers = raw.get("providers", {}) or {}
    for pk in ("image", "video", "tts", "game"):
        _normalize_section(providers.get(pk))
    return Settings(raw)


# 模块级单例（进程内）
SETTINGS = load_settings()
_settings_lock = threading.RLock()


def get_settings() -> Settings:
    """Thread-safe getter for the global SETTINGS singleton."""
    with _settings_lock:
        return SETTINGS


def set_settings(s: "Settings") -> None:
    """热更新全局单例，确保所有引用（pipeline/main）同步。"""
    with _settings_lock:
        global SETTINGS
        SETTINGS = s
