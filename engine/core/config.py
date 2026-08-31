"""Configuration loading: config.yaml + .env + environment overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_TRUE = {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency on python-dotenv at import time)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        # Real environment always wins over the file.
        if key and key not in os.environ:
            os.environ[key] = val


class Config:
    """Dot/slash-path accessor over the merged config tree."""

    def __init__(self, data: dict[str, Any], root: Path):
        self._data = data
        self.root = root

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def __getitem__(self, path: str) -> Any:
        return self.get(path)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # ---- convenience -------------------------------------------------
    @property
    def dry_run(self) -> bool:
        return bool(self.get("dry_run", True))

    @property
    def workspace(self) -> Path:
        raw = str(self.get("app.workspace", "./workspace"))
        p = Path(raw)
        if not p.is_absolute():
            p = self.root / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def secret(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default).strip()

    def has_secret(self, name: str) -> bool:
        return bool(self.secret(name))


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> Config:
    root = project_root()
    _load_dotenv(root / ".env")
    cfg_path = Path(path or os.environ.get("AUTOTUBE_CONFIG") or root / "config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config(data, root)

    # Environment overrides for the switches that matter operationally.
    if "DRY_RUN" in os.environ:
        cfg.set("dry_run", os.environ["DRY_RUN"].strip().lower() in _TRUE)
    if "UPLOAD_ENABLED" in os.environ:
        cfg.set("youtube.upload_enabled",
                os.environ["UPLOAD_ENABLED"].strip().lower() in _TRUE)
    if "AUTOTUBE_WORKSPACE" in os.environ:
        cfg.set("app.workspace", os.environ["AUTOTUBE_WORKSPACE"])
    return cfg
