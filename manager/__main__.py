"""Run the manager: python -m manager

Binds to MANAGER_HOST:MANAGER_PORT (default 127.0.0.1:8080 — localhost only,
never exposed by the Cloudflare tunnel; see docs/webgui/05-deployment.md).
"""
from __future__ import annotations

import uvicorn

from .config import get_config


def main() -> None:
    cfg = get_config()
    uvicorn.run(
        "manager.app:app",
        host=cfg.host,
        port=cfg.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
