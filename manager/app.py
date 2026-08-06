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
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_config
from .orchestrator import get_orchestrator
from . import jobs as J
from . import notify as N
from .recipes import RecipeStore, CdpTrigger

log = logging.getLogger("manager.app")
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
# Recipe store (repeat-without-LLM). The CDP trigger drives the hosted Chromium
# to re-open Takeout + re-trigger an export; unreachable CDP just means the
# recipe is marked due and a human/agent can finish it (no crash).
_recipes = RecipeStore(cfg.recipes_dir(), trigger=CdpTrigger())
_poller = N.CommandPoller(_notifier, orch, recipes=_recipes)

# Bridge orchestrator events -> Telegram. Subscriber must never raise.
def _telegram_sink(kind: str, summary: dict) -> None:
    try:
        if kind == "error":
            d = orch.diagnose(summary.get("job_id")) or {}
            summary = {**summary, "reason": d.get("reason")}
        if kind == "complete":
            # Record the completed run as a replayable recipe (no-LLM repeat).
            try:
                job = orch.get_job(summary.get("job_id"))
                if job is not None:
                    _recipes.record_from_job(job.snapshot())
            except Exception:  # noqa: BLE001
                pass
        _notifier.send_event(kind, summary)
    except Exception:  # noqa: BLE001
        pass

orch.subscribe(_telegram_sink)

def _security_self_check() -> list[str]:
    """Return a list of security warnings. Empty list == hardened.

    The dangerous case is binding beyond localhost WITHOUT tokens: that exposes a
    browser logged into the user's Google account to anyone who can reach the
    port. By design (docs/webgui/01-architecture.md) the manager binds 127.0.0.1
    and is reached only via SSH forward; this check is a loud backstop if someone
    overrides MANAGER_HOST.
    """
    warnings = []
    non_local = cfg.host not in ("127.0.0.1", "localhost", "::1")
    if non_local and not cfg.capture_token:
        warnings.append(
            f"manager bound to {cfg.host} with NO MANAGER_CAPTURE_TOKEN "
            "-> anyone reachable can POST payloads. Set a token or bind 127.0.0.1.")
    if non_local and not cfg.api_token:
        warnings.append(
            f"manager bound to {cfg.host} with NO MANAGER_API_TOKEN "
            "-> control plane is open. Set a token or bind 127.0.0.1.")
    return warnings

@app.on_event("startup")
async def _startup():
    for w in _security_self_check():
        log.warning("SECURITY: %s", w)
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


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str,
                     x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    ok = orch.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job not found or still active")
    return {"ok": True}

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

# --- recipes (Phase 9: repeat-without-LLM) ----------------------------------
@app.get("/api/recipes")
async def recipes_list():
    return {"recipes": _recipes.list_recipes()}

@app.post("/api/recipes/{name}/run")
async def recipes_run(name: str,
                      x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    if _recipes.get(name) is None:
        raise HTTPException(status_code=404, detail="no such recipe")
    ok = _recipes.run(name)
    return {"ok": ok, "name": name,
            "note": None if ok else "recipe found but CDP trigger unavailable"}

@app.post("/api/recipes/{name}/schedule")
async def recipes_schedule(name: str, request: Request,
                           x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    body = await _json_body(request)
    ok = _recipes.set_schedule(name, body.get("cron"))
    if not ok:
        raise HTTPException(status_code=404, detail="no such recipe")
    return {"ok": True, "name": name, "cron": body.get("cron")}

@app.delete("/api/recipes/{name}")
async def recipes_delete(name: str,
                         x_api_token: str | None = Header(default=None)):
    _check_api_token(x_api_token)
    ok = _recipes.delete(name)
    if not ok:
        raise HTTPException(status_code=404, detail="no such recipe")
    return {"ok": True}

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


# Asset cache-buster: changes every manager start so a normal browser reload
# always re-fetches app.js/app.css instead of needing a hard refresh.
import time as _time
_ASSET_VERSION = str(int(_time.time()))

@app.get("/", response_class=HTMLResponse)
async def index():
    idx = _web_dir / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8")
        # Inject the capture token so the in-page paste box can POST /api/payload
        # without the operator typing it. Safe: this page is localhost-only.
        token = cfg.capture_token or ""
        api_token = cfg.api_token or ""
        inject = ('<meta name="capture-token" content="%s">\n'
                  '<meta name="api-token" content="%s">') % (token, api_token)
        html = html.replace("</head>", inject + "\n</head>", 1)
        # Cache-bust versioned assets so a plain reload picks up new JS/CSS.
        html = html.replace('href="/ui/app.css"',
                            'href="/ui/app.css?v=%s"' % _ASSET_VERSION)
        html = html.replace('src="/ui/app.js"',
                            'src="/ui/app.js?v=%s"' % _ASSET_VERSION)
        # The HTML itself is dynamic (tokens + version) — never cache it.
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Takeout Manager</h1><p>UI not built yet (Phase 3).</p>")


# --- v2 control plane (Phase 10 cutover) -----------------------------------
# Mount takeout2's attempt-limited routes under /api/v2. v1 keeps working;
# the v2 app shares STORAGE_ROOT and the same API/capture tokens. If v2
# is not importable (pre-deploy), the manager must still boot — v1 only.
try:
    from .v2integration import mount_v2

    mount_v2(app)
    import logging as _logging

    _logging.getLogger("takeout2.integration").info("v2 routes mounted")
except Exception as _exc:  # noqa: BLE001 - v1 must never be bricked by v2
    import logging as _logging

    _logging.getLogger("takeout2.integration").warning(
        "v2 routes NOT mounted (%s); manager running v1 only", _exc)
