"""Wire the v2 control plane into the v1 manager app.

This is the Phase 10 cutover seam (docs/v2/02-BUILD-PLAN-B.md §10):

    v1 manager (manager/app.py)  +  v2 routes (takeout2/api.py)
    -----------------------------------------------
    both share STORAGE_ROOT from the same config; v1 routes keep working;
    v2 is mounted under /api/v2 with the same tokens.

Why a separate module instead of editing manager/app.py directly: the v2
routes live in takeout2/ (its own package, its own SQLite state.db), so the
seam is a small import + mount — reversible by removing the import line.

Mount layout (see takeout2/api.py create_app):
    /api/v2/jobs          GET    paginated job list
    /api/v2/jobs/{id}     GET    job + totals (+ ?parts=1)
    /api/v2/jobs/{id}/parts GET   paginated parts
    /api/v2/jobs/{id}/events GET  SSE with ?since=<seq> resume
    /api/v2/jobs/{id}/budget GET  attempt accounting (the money view)
    /api/v2/control/{pause,resume,cancel}/{id}  POST
    /api/v2/capture       POST   the extension's scrape payload sink
    /api/v2/events        GET    all-events SSE
    /api/v2/doctor        GET    preflight health

The v2 JobStore + AttemptLedger live in <storage_root>/google-takeout/state.db
— one shared SQLite per storage root, not one per job.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .config import get_config

log = logging.getLogger("takeout2.integration")


def _state_db_path() -> Path:
    cfg = get_config()
    return Path(cfg.storage_root) / "google-takeout" / "state.db"


def _connect():
    path = _state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def mount_v2(app) -> None:
    """Mount the v2 router onto the v1 FastAPI app (idempotent)."""
    from takeout2.api import create_app
    from takeout2.ledger import AttemptLedger
    from takeout2.state import JobStore

    cfg = get_config()
    conn = _connect()
    store = JobStore(conn)
    ledger = AttemptLedger(conn,
                           budget=cfg.attempt_budget if hasattr(cfg, "attempt_budget") else 5,
                           reserve=1)
    v2 = create_app(store=store, ledger=ledger,
                    api_token=cfg.api_token or "",
                    capture_token=cfg.capture_token or "")

    # Idempotency: mounting twice would shadow routes.
    if not any(getattr(r, "path", "").startswith("/api/v2/jobs")
               for r in app.routes):
        app.mount("/api/v2", v2)
        log.info("mounted takeout2 v2 routes under /api/v2 (state.db=%s)", _state_db_path())
    else:
        log.info("v2 routes already mounted; skipping")
