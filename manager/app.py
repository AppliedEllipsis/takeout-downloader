"""FastAPI app: HTTP surface for the manager.

Phase 2 endpoints (this file grows in Phase 3/4):
  POST /api/payload            capture sink (extension POSTs here)
  GET  /api/jobs               list jobs (summaries)
  GET  /api/jobs/{id}          full job snapshot
  GET  /api/jobs/{id}/events   SSE stream of job events
  GET  /api/jobs/{id}/log      tail of the engine log
  GET  /api/control/health     basic health
  GET  /                       static progress UI (Phase 3)

Localhost-only. Tokens (when configured) gate the capture + control surfaces.
Spec: docs/webgui/02-manager-service.md.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_config
from .orchestrator import get_orchestrator
from . import jobs as J
from . import notify as N

cfg = get_config()
orch = get_orchestrator()
app = FastAPI(title="Takeout Manager", version="0.1.0")

# --- Telegram notifier + command poller (no-op if unconfigured) -------------
_notifier = N.TelegramNotifier(
    token=cfg.telegram_token,
    chat_id=cfg.telegram_chat_id,
    enabled=cfg.telegram_enabled,
    progress_interval=cfg.telegram_progress_interval,
)
_poller = N.CommandPoller(_notifier, orch)

# Bridge orchestrator events -> Telegram. Subscriber must never raise.
def _telegram_sink(kind: str, summary: dict) -> None:
    try:
        if kind == "error":
            d = orch.diagnose(summary.get("job_id")) or {}
            summary = {**summary, "reason": d.get("reason")}
        _notifier.send_event(kind, summary)
    except Exception:  # noqa: BLE001
        pass

orch.subscribe(_telegram_sink)

@app.on_event("startup")
async def _startup():
    # Recover persisted jobs so they're visible after a restart.
    try:
        orch.recover()
    except Exception:  # noqa: BLE001
        pass
    _poller.start()
    if _notifier.enabled:
        _notifier.send("\U0001F7E2 Takeout manager online.")

@app.on_event("shutdown")
async def _shutdown():
    _poller.stop()


# --- auth helpers ------------------------------------------------------------
def _check_capture_token(token: str | None) -> None:
    if cfg.capture_token and token != cfg.capture_token:
        raise HTTPException(status_code=401, detail="bad capture token")


def _check_api_token(token: str | None) -> None:
    if cfg.api_token and token != cfg.api_token:
        raise HTTPException(status_code=401, detail="bad api token")


# --- capture sink ------------------------------------------------------------
@app.post("/api/payload")
async def post_payload(request: Request,
                       x_capture_token: str | None = Header(default=None)):
    _check_capture_token(x_capture_token)
    raw = (await request.body()).decode("utf-8", errors="replace")
    label = request.query_params.get("label")
    try:
        result = orch.accept_payload(raw, label_override=label)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return JSONResponse(result)


# --- job queries -------------------------------------------------------------
@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": orch.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = orch.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no such job")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/log")
async def get_job_log(job_id: str, lines: int = 200):
    job = orch.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no such job")
    log_path = Path(job.output_dir) / "takeout_dl.log"
    if not log_path.exists():
        return PlainTextResponse("(no log yet)")
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return PlainTextResponse("\n".join(content[-lines:]))


# --- SSE event stream --------------------------------------------------------
@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    job = orch.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no such job")

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_event_loop()

    def _on_event(kind: str, summary: dict):
        if summary.get("job_id") != job_id:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, summary))
        except Exception:  # noqa: BLE001
            pass

    unsub = orch.subscribe(_on_event)

    async def _gen():
        try:
            # Prime with the current snapshot so a late subscriber is in sync.
            yield _sse("snapshot", job.snapshot())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, summary = await asyncio.wait_for(queue.get(), timeout=15.0)
                    # Send the full snapshot so the UI always has parts detail.
                    cur = orch.get_job(job_id)
                    payload = cur.snapshot() if cur else summary
                    yield _sse(kind, payload)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsub()

    return StreamingResponse(_gen(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# --- health ------------------------------------------------------------------
@app.get("/api/control/health")
async def health():
    import shutil
    jobs = orch.list_jobs()
    active = [j for j in jobs if j["status"] in (J.DOWNLOADING, J.NEEDS_COOKIE)]
    try:
        du = shutil.disk_usage(str(cfg.storage_root))
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = {"free": -1, "total": -1}
    return {
        "ok": True,
        "jobs_total": len(jobs),
        "jobs_active": len(active),
        "storage_root": str(cfg.takeout_root),
        "disk": disk,
        "capture_token_set": bool(cfg.capture_token),
        "api_token_set": bool(cfg.api_token),
    }


# --- recapture signal (extension polls this) ---------------------------------
# Gated by the CAPTURE token (the extension holds it), not the API token: it is
# read-only and only reveals whether a fresh cookie is wanted. Lets the v4
# extension auto-recapture without the broader control token.
@app.get("/api/control/recapture-pending")
async def recapture_pending(x_capture_token: str | None = Header(default=None)):
    _check_capture_token(x_capture_token)
    pending = [j["job_id"] for j in orch.list_jobs() if j["status"] == J.NEEDS_COOKIE]
    return {"pending": bool(pending), "job_ids": pending}

# --- control plane (Phase 4) -------------------------------------------------
# All gated by MANAGER_API_TOKEN (when set). Separate from the capture token so
# a leaked capture token can only POST payloads, never drive jobs.
@app.post("/api/control/pause")
async def control_pause(request: Request,
                        x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    body = await _json_body(request)
    ok = orch.pause(body.get("job_id", ""))
    if not ok:
        raise HTTPException(status_code=404, detail="no such job / cannot pause")
    return {"ok": True}

@app.post("/api/control/resume")
async def control_resume(request: Request,
                         x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    body = await _json_body(request)
    ok = orch.resume(body.get("job_id", ""))
    if not ok:
        raise HTTPException(status_code=404, detail="no such job / cannot resume")
    return {"ok": True}

@app.post("/api/control/cancel")
async def control_cancel(request: Request,
                         x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    body = await _json_body(request)
    ok = orch.cancel(body.get("job_id", ""))
    if not ok:
        raise HTTPException(status_code=404, detail="no such job")
    return {"ok": True}

@app.post("/api/control/recapture")
async def control_recapture(request: Request,
                            x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    body = await _json_body(request)
    ok = orch.request_recapture(body.get("job_id", ""))
    if not ok:
        raise HTTPException(status_code=404, detail="no such job")
    return {"ok": True}

@app.get("/api/control/diagnose")
async def control_diagnose(job_id: str,
                           x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    d = orch.diagnose(job_id)
    if d is None:
        raise HTTPException(status_code=404, detail="no such job")
    return d

async def _json_body(request: Request) -> dict:
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return {}

# --- static UI (Phase 3 fills web/) -----------------------------------------
_web_dir = Path(__file__).parent / "web"
if _web_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_web_dir), html=True), name="ui")


@app.get("/", response_class=HTMLResponse)
async def index():
    idx = _web_dir / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Takeout Manager</h1><p>UI not built yet (Phase 3).</p>")
