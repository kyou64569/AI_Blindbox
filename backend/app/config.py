"""配置加载与路径管理。

所有体积较大文件（缓存/产物/上传）默认落在 D 盘项目目录，不写 C 盘。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

# 项目根：本文件位于 backend/app/config.py，上溯一级到 backend
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 大文件路径默认绑定到 D 盘项目目录，避免写入 C 盘
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def _as_path(value: str | None, default: Path) -> Path:
    """把配置中的路径字符串解析为绝对路径；若为空或不安全（落在 C 盘系统目录），回退默认。"""
    if not value:
        return default
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    # 安全兜底：不允许落进 C 盘用户敏感目录
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


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path) if path else (PROJECT_ROOT / "settings.yaml")
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
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
