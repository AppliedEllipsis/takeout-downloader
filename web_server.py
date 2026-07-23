#!/usr/bin/env python3
"""
Takeout Downloader — Web UI.

A lightweight FastAPI server that exposes the internal downloader through a
browser-based dashboard.  It reuses the existing payload parser and downloader
engine, so the web path gets the same single-stream/rate-limit/resume behaviour
as the CLI/TUI.

Endpoints
---------
  GET  /                  -> static dashboard
  GET  /api/health        -> health check
  GET  /api/jobs          -> list recent jobs
  GET  /api/jobs/{id}     -> single job snapshot
  POST /api/start         -> start a download from a JSON payload
  POST /api/pause/{id}    -> pause a running job
  POST /api/cancel/{id}   -> cancel a job
  POST /api/resume/{id}   -> resume a paused job (fresh payload optional)
  GET  /api/stream/{id}   -> Server-Sent Events progress stream

The download runs in a background thread.  The main thread streams progress
events to the browser via SSE.  Auth challenges pause the job and push an
``auth_required`` event; the user pastes a fresh payload into the web UI and
hits Resume, which updates the cookie and continues from the same part list.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Reuse the existing engine modules.
from takeout import extract_url_parts, validate_output_dir, DEFAULT_OUTPUT_DIR
from takeout_downloader import InternalDownloader
from takeout_payload import parse_multi_payload_meta, TakeoutPayload


try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    raise ImportError("pydantic is required; install with `pip install pydantic`") from None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = os.environ.get("TAKEOUT_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("TAKEOUT_WEB_PORT", "8000")))
MAX_PARTS = int(os.environ.get("MAX_PARTS", "500"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "10"))
MAX_PARALLEL = int(os.environ.get("TAKEOUT_WEB_MAX_PARALLEL", "2"))
DEFAULT_PARALLEL = int(os.environ.get("TAKEOUT_WEB_DEFAULT_PARALLEL", "1"))

# Persistent state directory. Default to a hidden folder inside the default
# output path so it survives container restarts when ./downloads is mounted.
STATE_DIR = Path(os.environ.get("TAKEOUT_WEB_STATE_DIR", "/downloads/.takeout_web_state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
JOBS_INDEX = STATE_DIR / "jobs.json"


# ---------------------------------------------------------------------------
# In-memory job state
# ---------------------------------------------------------------------------
class JobStatus:
    PENDING = "pending"
    DISCOVERING = "discovering"
    RUNNING = "running"
    PAUSED = "paused"           # auth challenge, waiting for fresh cookie
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    payload: TakeoutPayload
    output_dir: Path
    parallel: int
    status: str = JobStatus.PENDING
    parts: list[dict] = field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Event queue used to push SSE updates from the worker thread.
    _queue: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    _cancelled: threading.Event = field(default_factory=threading.Event)
    _auth_event: threading.Event = field(default_factory=threading.Event)
    _new_cookie: Optional[str] = None
    # Reference to the live downloader so pause/cancel can reach it.
    downloader: Optional[InternalDownloader] = None

    def emit(self, event: dict) -> None:
        self._queue.put(event)

    def wait_for_auth(self, timeout: Optional[float] = None) -> bool:
        return self._auth_event.wait(timeout)

    def provide_auth(self, payload: TakeoutPayload) -> None:
        self.payload = payload
        self._new_cookie = payload.cookie
        self._auth_event.set()

    def to_dict(self) -> dict:
        """Serialize the job to a dict for persistent storage."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "output_dir": str(self.output_dir),
            "parallel": self.parallel,
            "created_at": self.created_at,
            "error": self.error,
            "result": self.result,
            "parts": self.parts,
            "payload": self.payload.to_json() if self.payload else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        """Reconstruct a Job from stored dict.  Threading primitives are
        recreated fresh, so a restored job is always inert until resumed."""
        payload = None
        if data.get("payload"):
            payload = TakeoutPayload.from_json(data["payload"])
        output_dir = Path(data.get("output_dir", "/downloads"))
        job = cls(
            job_id=data["job_id"],
            payload=payload,  # type: ignore[arg-type]
            output_dir=output_dir,
            parallel=max(1, data.get("parallel", 1)),
            status=data.get("status", JobStatus.PAUSED),
            parts=list(data.get("parts", []) or []),
            result=data.get("result"),
            error=data.get("error"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )
        return job


_jobs_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_job_order: "deque[str]" = deque(maxlen=MAX_JOBS)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def _persist_jobs() -> None:
    """Write a lightweight index of all known jobs to disk."""
    try:
        with _jobs_lock:
            data = {
                "schema": 1,
                "updated": datetime.now(timezone.utc).isoformat(),
                "order": list(_job_order),
                "jobs": {jid: job.to_dict() for jid, job in _jobs.items()},
            }
        JOBS_INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_jobs() -> None:
    """Restore in-memory job registry from disk, marking any previously
    RUNNING jobs as PAUSED because their worker threads are gone."""
    if not JOBS_INDEX.exists():
        return
    try:
        data = json.loads(JOBS_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    restored: dict[str, Job] = {}
    order = data.get("order", [])
    for jid in order:
        job_data = data.get("jobs", {}).get(jid)
        if not job_data:
            continue
        try:
            job = Job.from_dict(job_data)
            if job.status in (JobStatus.RUNNING, JobStatus.DISCOVERING):
                job.status = JobStatus.PAUSED
                if not job.error:
                    job.error = "Server restarted while running. Click Resume to continue."
            restored[jid] = job
        except Exception:
            continue

    with _jobs_lock:
        _jobs.update(restored)
        _job_order.clear()
        for jid in order:
            if jid in restored:
                _job_order.append(jid)


# Load persisted jobs at module import time.
_load_jobs()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Takeout Downloader", version="7.1.0")

# Static files: the web/ directory contains the SPA assets.
_static_dir = Path(__file__).with_suffix("").parent / "web"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
else:  # pragma: no cover
    print(f"WARN: static directory not found at {_static_dir}", flush=True)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class StartRequest(BaseModel):
    payload: str
    output_dir: Optional[str] = None
    parallel: int = Field(default=DEFAULT_PARALLEL, ge=1, le=MAX_PARALLEL)


class ResumeRequest(BaseModel):
    payload: Optional[str] = None


class PauseCancelRequest(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _human_size(n: int) -> str:
    if n is None or n <= 0:
        return "?"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


def _human_duration(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


def _resolve_output_dir(raw: Optional[str]) -> Path:
    if raw:
        d = validate_output_dir(raw)
    else:
        d = validate_output_dir(DEFAULT_OUTPUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _discover_parts(payload: TakeoutPayload, max_parts: int = MAX_PARTS,
                    on_event: Optional[Callable[[dict], None]] = None) -> list[dict]:
    """Probe the numbered part URLs and build the ``parts`` list."""
    base, file_num, ext, query = extract_url_parts(payload.url)
    if not base or not ext:
        raise ValueError("Could not extract a takeout URL pattern from the payload")

    session = requests.Session()
    headers = dict(payload.headers)
    headers["Cookie"] = payload.cookie

    parts: list[dict] = []
    consecutive_404 = 0
    try:
        for n in range(1, max_parts + 1):
            filename = f"{base.split('/')[-1]}{n:03d}{ext}"
            url = f"{base}{n:03d}{ext}"
            if query:
                url += f"?{query}"

            size = 0
            try:
                resp = session.get(url, headers=headers, stream=True,
                                   timeout=(10, 30), allow_redirects=True)
                # Auth challenge on discovery.
                if resp.url and "accounts.google.com" in resp.url:
                    resp.close()
                    raise RuntimeError("Google redirected to sign-in; cookie may be expired")
                if resp.status_code == 404:
                    consecutive_404 += 1
                    resp.close()
                    if consecutive_404 >= 2:
                        break
                    continue
                # Some parts may not exist; treat 403/410 as end-of-set.
                if resp.status_code in (403, 410):
                    resp.close()
                    break
                if "text/html" in resp.headers.get("content-type", "").lower():
                    resp.close()
                    raise RuntimeError("Server returned HTML instead of the archive")
                if resp.status_code == 206 or resp.status_code == 200:
                    cl = resp.headers.get("content-length")
                    cr = resp.headers.get("content-range", "")
                    if cr and "/" in cr:
                        try:
                            size = int(cr.rsplit("/", 1)[1])
                        except ValueError:
                            pass
                    elif cl and cl.isdigit():
                        size = int(cl)
                resp.close()
            except requests.RequestException as e:
                if on_event:
                    on_event({
                        "type": "log",
                        "level": "warning",
                        "message": f"part {n:03d} probe failed: {e}",
                    })
                break

            consecutive_404 = 0
            parts.append({
                "num": n,
                "url": url,
                "filename": filename,
                "size": size,
                "have": False,
            })
            if on_event:
                on_event({
                    "type": "discovered",
                    "num": n,
                    "filename": filename,
                    "size": size,
                    "size_human": _human_size(size),
                })
    finally:
        session.close()

    return parts


def _build_state_file(output_dir: Path) -> Path:
    return output_dir / "takeout_state.json"


def _part_is_complete(job: Job, part: dict) -> bool:
    """True if the on-disk file already contains the expected bytes."""
    size = part.get("size") or 0
    if size <= 0:
        return False
    dest = job.output_dir / part["filename"]
    return dest.exists() and dest.stat().st_size >= size


def _update_job_state(job: Job) -> None:
    """Persist a tiny state snapshot so the web UI can show resume state."""
    try:
        state = {
            "schema": 1,
            "created": job.created_at,
            "status": job.status,
            "parts": [{"num": p["num"], "size": p.get("size", 0),
                       "complete": p.get("have", False)} for p in job.parts],
        }
        _build_state_file(job.output_dir).write_text(
            json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _job_to_dict(job: Job) -> dict:
    done = sum(1 for p in job.parts if p.get("have"))
    total_size = sum(p.get("size", 0) or 0 for p in job.parts)
    done_size = sum(p.get("size", 0) or 0 for p in job.parts if p.get("have"))
    return {
        "job_id": job.job_id,
        "status": job.status,
        "output_dir": str(job.output_dir),
        "parallel": job.parallel,
        "created_at": job.created_at,
        "parts_total": len(job.parts),
        "parts_done": done,
        "bytes_total": total_size,
        "bytes_done": done_size,
        "error": job.error,
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def _run_download(job: Job) -> None:
    global _active_job
    try:
        job.status = JobStatus.DISCOVERING
        job.emit({"type": "status", "status": job.status,
                  "message": "Discovering parts..."})
        discovered = _discover_parts(job.payload, on_event=job.emit)

        # Preserve already-completed flags from a previous run/resume.
        have_by_num = {p["num"]: p.get("have", False) for p in job.parts}
        for p in discovered:
            if have_by_num.get(p["num"], False) or _part_is_complete(job, p):
                p["have"] = True
        job.parts = discovered

        if not job.parts:
            job.status = JobStatus.FAILED
            job.error = "No downloadable parts were discovered"
            job.emit({"type": "status", "status": job.status,
                      "message": job.error})
            _update_job_state(job)
            _persist_jobs()
            return

        job.status = JobStatus.RUNNING
        job.emit({"type": "status", "status": job.status,
                  "message": f"Starting download of {len(job.parts)} parts",
                  "parts": job.parts})

        # ETA state
        eta_state: dict[str, Any] = {"start_time": time.monotonic(), "last_update": 0.0}

        def on_progress(snapshot: list) -> None:
            now = time.monotonic()
            total_bytes = sum(p.total for p in snapshot)
            done_bytes = sum(p.done for p in snapshot)
            total_speed = sum(p.speed_bps for p in snapshot)

            eta_seconds: Optional[float] = None
            if total_speed > 0 and total_bytes > done_bytes:
                eta_seconds = (total_bytes - done_bytes) / total_speed

            job.emit({
                "type": "progress",
                "timestamp": _now(),
                "eta_seconds": eta_seconds,
                "total_bytes": total_bytes,
                "done_bytes": done_bytes,
                "total_speed": total_speed,
                "parts": [
                    {
                        "num": p.num,
                        "filename": p.filename,
                        "status": p.status,
                        "total": p.total,
                        "done": p.done,
                        "pct": p.pct,
                        "speed": p.speed_bps,
                        "error": p.error,
                    }
                    for p in snapshot
                ],
            })
            # Throttle state persistence (disk writes) to once per second.
            if now - eta_state["last_update"] >= 1.0:
                _update_job_state(job)
                _persist_jobs()
                eta_state["last_update"] = now

        downloader = InternalDownloader(
            cookie=job.payload.cookie,
            headers=job.payload.headers,
            output_dir=job.output_dir,
            parallel=job.parallel,
            logger=None,
        )
        job.downloader = downloader
        result = downloader.download(job.parts, on_progress=on_progress)

        # Persist final sizes so a future resume knows what is complete.
        for p in job.parts:
            if p["num"] in result.completed:
                p["have"] = True

        if result.auth_failed:
            job.status = JobStatus.PAUSED
            job.error = "Cookie expired or auth challenge. Paste a fresh payload and click Resume."
            job.emit({
                "type": "auth_required",
                "message": job.error,
                "completed": result.completed,
                "failed": result.failed,
            })
        else:
            if result.failed:
                job.status = JobStatus.FAILED
                job.error = f"{len(result.failed)} part(s) failed"
                job.emit({"type": "status", "status": job.status,
                          "message": job.error})
            else:
                job.status = JobStatus.COMPLETED
                job.emit({"type": "status", "status": job.status,
                          "message": "Download complete"})

        job.result = {
            "completed": result.completed,
            "failed": result.failed,
            "auth_failed": result.auth_failed,
        }
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.emit({"type": "status", "status": job.status,
                  "message": f"Failed: {exc}"})
    finally:
        _update_job_state(job)
        _persist_jobs()
        _active_job = None
        job.downloader = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    index_path = _static_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Takeout Downloader</h1>")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        return {"jobs": [_job_to_dict(_jobs[jid]) for jid in _job_order]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


@app.post("/api/start")
def start(req: StartRequest):
    try:
        payloads, _ = parse_multi_payload_meta(req.payload)
        if not payloads:
            raise HTTPException(status_code=400, detail="No exports found in payload")
        payload = payloads[0]
        ok, message = payload.validate()
        if not ok:
            raise HTTPException(status_code=400, detail=message or "Payload validation failed")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Enforce safe concurrency defaults for the web path.
    parallel = max(1, min(MAX_PARALLEL, req.parallel or DEFAULT_PARALLEL))

    job = Job(
        job_id=str(uuid.uuid4())[:8],
        payload=payload,
        output_dir=_resolve_output_dir(req.output_dir),
        parallel=parallel,
    )
    if message:
        job.emit({"type": "log", "level": "warning", "message": message})

    with _jobs_lock:
        _jobs[job.job_id] = job
        _job_order.append(job.job_id)
        _active_job = job

    _persist_jobs()
    threading.Thread(target=_run_download, args=(job,), daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/pause/{job_id}")
def pause(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.RUNNING:
        raise HTTPException(status_code=400, detail=f"Job is {job.status}, cannot pause")

    if job.downloader:
        job.downloader.request_stop()
    job.status = JobStatus.PAUSED
    job.error = "Paused by user."
    job.emit({"type": "status", "status": job.status,
                "message": "Paused by user. Click Resume to continue."})
    _update_job_state(job)
    _persist_jobs()
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/cancel/{job_id}")
def cancel(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.CANCELLED:
        return {"job_id": job_id, "status": job.status}

    if job.downloader:
        job.downloader.request_stop()
    job.status = JobStatus.CANCELLED
    job.error = "Cancelled by user."
    job.emit({"type": "status", "status": job.status,
                "message": "Cancelled by user."})
    _update_job_state(job)
    _persist_jobs()
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/resume/{job_id}")
def resume(job_id: str, req: ResumeRequest):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.PAUSED, JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Job is {job.status}, not resumable")

    payload = job.payload
    if req.payload:
        try:
            payloads, _ = parse_multi_payload_meta(req.payload)
            if not payloads:
                raise HTTPException(status_code=400, detail="No exports found in payload")
            payload = payloads[0]
            ok, _ = payload.validate()
            if not ok:
                raise HTTPException(status_code=400, detail="Payload validation failed")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        job.provide_auth(payload)

    if payload is None:
        raise HTTPException(status_code=400, detail="No payload available to resume with")

    job._auth_event.clear()
    job.result = None
    job.error = None
    job.status = JobStatus.PENDING
    _persist_jobs()
    threading.Thread(target=_run_download, args=(job,), daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/stream/{job_id}")
def stream(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    def event_generator():
        while True:
            try:
                event = job._queue.get(timeout=5.0)
            except queue.Empty:
                # Send a keep-alive comment so the browser doesn't drop SSE.
                yield ":\n\n"
                continue
            if event is None:
                break
            data = json.dumps(event)
            yield f"data: {data}\n\n"
            if event.get("type") in ("status", "auth_required"):
                # Terminal-ish event for the current phase; keep the stream open
                # in case the user resumes after an auth challenge.
                if event.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                    break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------
def main() -> int:
    uvicorn.run("web_server:app", host=HOST, port=PORT, log_level="info",
                access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
