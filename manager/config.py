"""Manager configuration.

All settings come from environment variables with safe defaults so the manager
runs locally with zero config. Secrets (tokens) are read from the environment
only; nothing is hardcoded. See docs/webgui/05-deployment.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class Config:
    # Where downloads land: <storage_root>/google-takeout/<account>/<export-ts>/
    storage_root: str = field(default_factory=lambda: _env("STORAGE_ROOT", "/opt"))
    takeout_subdir: str = field(default_factory=lambda: _env("TAKEOUT_SUBDIR", "google-takeout"))

    # Server bind (localhost only — never exposed by the tunnel).
    host: str = field(default_factory=lambda: _env("MANAGER_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MANAGER_PORT", 8080))

    # Download defaults.
    parallel: int = field(default_factory=lambda: _env_int("PARALLEL_DOWNLOADS", 4))
    max_exports: int = field(default_factory=lambda: _env_int("MAX_EXPORTS", 500))

    # Auth tokens. Empty => that surface is OPEN (dev only). In deployment both
    # are set; the capture token is narrow (POST /api/payload only), the API
    # token gates the control plane.
    capture_token: str = field(default_factory=lambda: _env("MANAGER_CAPTURE_TOKEN"))
    api_token: str = field(default_factory=lambda: _env("MANAGER_API_TOKEN"))

    # Telegram (see manager/notify.py + docs/webgui/06-telegram.md).
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_enabled: bool = field(
        default_factory=lambda: _env("TELEGRAM_ENABLED", "true").lower() not in ("0", "false", "no")
    )
    telegram_progress_interval: int = field(
        default_factory=lambda: _env_int("TELEGRAM_PROGRESS_INTERVAL", 300)
    )

    # Optional sha256 of each finished part in the manifest (costs a full reread).
    manifest_sha256: bool = field(
        default_factory=lambda: _env("MANIFEST_SHA256", "false").lower() in ("1", "true", "yes")
    )

    @property
    def takeout_root(self) -> Path:
        return Path(self.storage_root) / self.takeout_subdir

    def recipes_dir(self) -> Path:
        return self.takeout_root / ".recipes"


# Singleton-style accessor; tests can construct their own Config().
_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
