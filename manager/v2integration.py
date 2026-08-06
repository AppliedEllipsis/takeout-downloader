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


def _build_supervisor(store, ledger, cfg):
    """Create the RunnerSupervisor that makes downloads self-driving.

    The engine factory is built lazily PER BURST on purpose: a fresh
    ``LiveCookieJar`` is constructed each time so every burst uses the newest
    cookie in the browser rather than a stale handle captured at boot. If the
    browser/CDP is unreachable the factory raises, and the runner parks the job
    in NEEDS_COOKIE and waits for the extension to re-capture (R3) instead of
    spending attempts against a cookie we know is dead.

    Returns None if anything is missing, which degrades the API to read-only
    rather than breaking the manager.
    """
    import os

    try:
        from takeout2.contracts import DEFAULTS, VerifyState
        from takeout2.cookie import LiveCookieJar
        from takeout2.engine import WRITE_CHUNK, BurstEngine, EngineConfig
        from takeout2.progress import make_progress_emitter
        from takeout2.runner import RunnerConfig, RunnerSupervisor
    except Exception as exc:                                    # noqa: BLE001
        log.warning("v2 runner unavailable (%s); API stays read-only", exc)
        return None

    def _env(name, default):
        raw = os.environ.get(name)
        return raw if raw not in (None, "") else default

    def _num(name, default, cast=float):
        try:
            return cast(_env(name, default))
        except (TypeError, ValueError):
            return default

    def engine_factory(archive_id: str):
        job = store.get_job(archive_id)
        if job is None:
            raise RuntimeError(f"no such job: {archive_id}")
        output_dir = getattr(job, "output_dir", None)
        if not output_dir:
            raise RuntimeError(f"job {archive_id} has no output_dir")
        parts_dir = os.path.join(output_dir, "parts")

        # Fresh cookie handle per burst — raises when Chrome/CDP is down,
        # which the runner turns into a NEEDS_COOKIE park.
        cookie = LiveCookieJar(
            _env("TK2_CDP_URL", DEFAULTS["CDP_URL"]),
            timeout=_num("TK2_CDP_TIMEOUT_S", 10.0, float))

        engine_config = EngineConfig(
            parallel=_num("TK2_PARALLEL", DEFAULTS["PARALLEL"], int),
            attempt_budget=getattr(ledger, "_budget", 5),
            budget_reserve=getattr(ledger, "_reserve", 1),
            verify_level=VerifyState(_env("TK2_VERIFY_LEVEL",
                                          DEFAULTS["VERIFY_LEVEL"])),
            # Reliability guards (docs/v2/07-RELIABILITY-HARDENING.md).
            stall_abort_s=_num("TK2_STALL_ABORT_S", 180.0, float),
            stall_resume_attempts=_num("TK2_STALL_RESUME", 1, int),
            resume_rewind=_num("TK2_RESUME_REWIND", WRITE_CHUNK, int),
            fsync_interval_s=_num("TK2_FSYNC_INTERVAL_S", 30.0, float),
            require_mount=str(_env("TK2_REQUIRE_MOUNT", "0")).lower()
                          not in ("0", "", "false"),
            cache_dir=_env("TK2_CACHE_DIR", "") or None,
            cache_max_bytes=_num("TK2_CACHE_MAX_BYTES", 100 * 1024 ** 3, int),
            cache_wait_max_s=_num("TK2_CACHE_WAIT_MAX_S", 1800.0, float),
            rate_limit_backoff=str(_env("TK2_RATE_BACKOFF", "1")).lower()
                               not in ("0", "false"),
        )
        return BurstEngine(
            store, ledger, cookie, parts_dir, config=engine_config,
            on_chunk=make_progress_emitter(store, archive_id))

    def on_runner_event(kind: str, data: dict) -> None:
        """Notify on self-heal / genuine blockage (operator's choice)."""
        if kind not in ("self_heal", "budget_exhausted", "runner_gave_up"):
            return
        try:
            from . import notify
        except Exception:                                       # noqa: BLE001
            return
        archive_id = data.get("archive_id", "?")
        if kind == "self_heal":
            text = (f"\U0001f504 {archive_id}: cookie expired, re-captured "
                    f"automatically, download resumed.")
        elif kind == "budget_exhausted":
            text = (f"\u26d4 {archive_id}: attempt budget exhausted — Google's "
                    f"5-download limit reached. Needs your decision.")
        else:
            text = (f"\u26a0 {archive_id}: runner gave up after repeated "
                    f"failures ({data.get('failures', '?')}).")
        for fname in ("send", "notify", "send_message", "push"):
            fn = getattr(notify, fname, None)
            if callable(fn):
                try:
                    fn(text)
                except Exception as exc:                        # noqa: BLE001
                    log.debug("notify failed: %s", exc)
                return

    return RunnerSupervisor(
        store, engine_factory=engine_factory,
        config=RunnerConfig(burst_gap_s=_num("TK2_BURST_GAP_S", 5.0, float)),
        on_event=on_runner_event)


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
    supervisor = _build_supervisor(store, ledger, cfg)
    v2 = create_app(store=store, ledger=ledger,
                    api_token=cfg.api_token or "",
                    capture_token=cfg.capture_token or "",
                    supervisor=supervisor)

    # Idempotency: mounting twice would shadow routes.
    if not any(getattr(r, "path", "").startswith("/api/v2/jobs")
               for r in app.routes):
        app.mount("/api/v2", v2)
        log.info("mounted takeout2 v2 routes under /api/v2 (state.db=%s, "
                 "runner=%s)", _state_db_path(),
                 "on" if supervisor is not None else "off")
    else:
        log.info("v2 routes already mounted; skipping")
