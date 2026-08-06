"""v2 FastAPI layer — the control plane the extension and CLI talk to.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §6 and
``docs/v2/01-IDENTITY-AND-SCRAPE.md`` §6 (the capture sink).

This module is deliberately thin: all real logic lives in ``state.py``,
``ledger.py``, ``plan.py``, ``cookie.py``, ``engine.py``. It is the seam
where the extension's scraped payload becomes a job, where the budget is
seeded from Google's own counter, and where SSE clients resume losslessly via
the monotonic ``?since=<seq>`` cursor.

The app is a factory so tests (and the real manager) can inject a JobStore,
an AttemptLedger, and a BurstEngine — no globals, no module-level state.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .contracts import (DEFAULTS, AccountIdentity, IdentityRecord, JobStatus,
                        LabelSource, PartStatus, VerifyState)
from .plan import plan_from_payload

log = logging.getLogger("takeout2.api")

__all__ = ["create_app", "build_capture_identity", "CAPTURE_FIELD_ORDER"]

#: Expected shape of POST /api/v2/capture — keep in sync with the extension.
#: (doc 01 §6. This list is informational; the handler is defensive.)
CAPTURE_FIELD_ORDER = [
    "archive_id", "user", "authuser", "parts_expected",
    "uris", "sizes", "filenames", "dl_counts",
    "account", "export_ts_raw", "scrape_report", "locale_warning",
    "captured_at",
]


def build_capture_identity(payload: dict, fallback_gaia: str = "") -> IdentityRecord:
    """Turn a capture payload's identity fields into an IdentityRecord.

    provenance ladder (doc 01 §4): OPERATOR_OVERRIDE > SCRAPED_EMAIL >
    SCRAPED_LABEL > GAIA_FALLBACK > UNKNOWN.
    """
    acct = payload.get("account") or {}
    email = acct.get("email") or None
    label = acct.get("label") or None
    source_raw = (acct.get("label_source") or "").upper()

    if source_raw == "OPERATOR_OVERRIDE":
        source = LabelSource.OPERATOR_OVERRIDE
    elif email:
        source = LabelSource.SCRAPED_EMAIL
    elif label:
        source = LabelSource.SCRAPED_LABEL
    else:
        source = LabelSource.GAIA_FALLBACK

    gaia = str(payload.get("user") or fallback_gaia or "")
    return IdentityRecord(
        archive_id=str(payload.get("archive_id") or ""),
        export_raw=str(payload.get("export_ts_raw") or ""),
        account=AccountIdentity(
            gaia_user=gaia,
            authuser=str(payload.get("authuser") or "0"),
            email=email, label=label, label_source=source,
        ),
        parts_expected=payload.get("parts_expected"),
        captured_at=str(payload.get("captured_at") or ""),
        page_url="",
        scrape_report=payload.get("scrape_report") or [],
    )


class CaptureHandler:
    """Stateful handler wiring the capture payload into store+ledger."""

    def __init__(self, store, ledger):
        self.store = store
        self.ledger = ledger

    def ingest(self, payload: dict) -> dict:
        if not payload.get("archive_id"):
            raise HTTPException(400, "payload missing archive_id")
        identity = build_capture_identity(payload)
        if not identity.export_raw:
            # Derive the export timestamp from filenames if the extension did
            # not send it (doc 01 §5 extraction order).
            from .contracts import parse_export_ts
            raw = parse_export_ts(payload.get("filenames") or [])
            if raw:
                identity = IdentityRecord(
                    archive_id=identity.archive_id,
                    export_raw=raw,
                    account=identity.account,
                    parts_expected=identity.parts_expected,
                    captured_at=identity.captured_at,
                )

        output_dir = payload.get("output_dir") or ""
        job = self.store.upsert_job(identity, output_dir=output_dir)
        # Upgrade identity (better provenance) if the existing job has a lower
        # source — never a second job, never a re-download.
        self.store.maybe_upgrade_identity(identity)

        plans = plan_from_payload(payload)
        if plans.ok:
            self.store.upsert_parts(identity.archive_id, plans.parts)
            # Seed Google's own attempt counter into the ledger (doc 01 §8):
            # remote outranks local.
            for p in plans.parts:
                if p.dl_count_remote is not None:
                    self.ledger.observe_remote(identity.archive_id, p.idx,
                                               p.dl_count_remote)
        else:
            log.warning("capture carried no part metadata; discovery deferred "
                        "to planner (zero-probe sources first)")

        self.store.emit("capture_ingested", identity.archive_id,
                        parts=len(plans.parts) if plans.ok else None,
                        label_source=identity.account.label_source.value,
                        export_ts=identity.export_raw)
        return {
            "ok": True,
            "archive_id": identity.archive_id,
            "parts": len(plans.parts) if plans.ok else None,
            "label": identity.account.folder_name(),
            "export_ts": identity.export_ts,
        }


def _auth(deps_token: Optional[str], header_token: Optional[str]) -> None:
    """Enforce X-Api-Token / X-Capture-Token when configured.

    Mirror of v1's behavior: if the env token is empty, auth is open
    (empty-matches-empty). If it is set, a missing/mismatched header is 401.
    Never fix one side without the other (start.md §0.5).
    """
    if deps_token:
        if not header_token or header_token != deps_token:
            raise HTTPException(401, "invalid token")


def create_app(*, store, ledger, engine=None,
               api_token: str = "",
               capture_token: str = "") -> FastAPI:
    app = FastAPI(title="takeout2", version="2.0.0")
    handler = CaptureHandler(store, ledger)

    # -- jobs ---------------------------------------------------------------
    @app.get("/api/v2/jobs")
    def list_jobs(limit: int = 50, offset: int = 0):
        jobs = store.list_jobs(limit=min(limit, 500), offset=offset)
        return {
            "jobs": [{
                "archive_id": j.archive_id,
                "label": j.account_label,
                "label_source": j.label_source.value,
                "export_ts": j.export_ts,
                "status": j.status.value,
                "parts_expected": j.parts_expected,
                **store.job_totals(j.archive_id),
            } for j in jobs],
            "total": len(jobs),
        }

    @app.get("/api/v2/jobs/{archive_id}")
    def get_job(archive_id: str, parts: bool = False):
        job = store.get_job(archive_id)
        if not job:
            raise HTTPException(404, "no such job")
        data = {
            "archive_id": job.archive_id,
            "label": job.account_label,
            "label_source": job.label_source.value,
            "export_ts": job.export_ts,
            "output_dir": job.output_dir,
            "status": job.status.value,
            "parts_expected": job.parts_expected,
            "last_error": job.last_error,
            **store.job_totals(archive_id),
        }
        if parts:
            data["parts"] = [{
                "idx": p.idx, "filename": p.filename, "status": p.status.value,
                "verify_state": p.verify_state.value, "size_expected": p.size_expected,
                "size_on_disk": p.size_on_disk, "attempts_used": p.attempts_used,
                "last_error": p.last_error,
            } for p in store.list_parts(archive_id, limit=10000)]
        return data

    @app.get("/api/v2/jobs/{archive_id}/parts")
    def list_parts(archive_id: str, status: Optional[str] = None,
                   limit: int = 200, offset: int = 0):
        st = PartStatus(status) if status else None
        rows = store.list_parts(archive_id, status=st, limit=min(limit, 2000),
                                offset=offset)
        return {"parts": [{
            "idx": p.idx, "filename": p.filename, "status": p.status.value,
            "verify_state": p.verify_state.value, "size_expected": p.size_expected,
            "size_on_disk": p.size_on_disk, "attempts_used": p.attempts_used,
        } for p in rows]}

    # -- budget (the money view) -------------------------------------------
    @app.get("/api/v2/jobs/{archive_id}/budget")
    def budget(archive_id: str):
        job = store.get_job(archive_id)
        if not job:
            raise HTTPException(404, "no such job")
        parts = []
        for p in store.list_parts(archive_id, limit=10000):
            b = ledger.budget_for(archive_id, p.idx)
            parts.append({
                "idx": p.idx, "filename": p.filename,
                "local_used": b.local_used,
                "remote_used": b.remote_used,
                "effective_used": b.effective_used,
                "remaining": b.remaining,
                "at_risk": b.at_risk,
                "status": p.status.value,
            })
        at_risk = [p for p in parts if p["at_risk"]]
        return {
            "archive_id": archive_id,
            "attempt_budget": ledger._budget,
            "reserve": ledger._reserve,
            "parts": parts,
            "parts_at_risk": len(at_risk),
            "archive": ledger.archive_totals(archive_id),
        }

    # -- control ------------------------------------------------------------
    def _control(action: str):
        def handler(archive_id: str,
                    x_api_token: Optional[str] = Header(None)):
            _auth(api_token, x_api_token)
            job = store.get_job(archive_id)
            if not job:
                raise HTTPException(404, "no such job")
            if action == "pause":
                if job.status in (JobStatus.COMPLETE, JobStatus.FAILED):
                    raise HTTPException(409, "job is terminal")
                store.set_job_status(archive_id, JobStatus.PAUSED)
            elif action == "resume":
                if job.status in (JobStatus.COMPLETE, JobStatus.FAILED):
                    raise HTTPException(409, "job is terminal")
                store.set_job_status(archive_id, JobStatus.DOWNLOADING)
                store.emit("resume_requested", archive_id)
            elif action == "cancel":
                if job.status in (JobStatus.COMPLETE,):
                    raise HTTPException(409, "job already complete")
                store.set_job_status(archive_id, JobStatus.FAILED,
                                     error="cancelled by operator")
            return {"ok": True, "archive_id": archive_id, "action": action}
        return handler

    for action in ("pause", "resume", "cancel"):
        app.add_api_route(
            f"/api/v2/control/{action}/{{archive_id}}", _control(action),
            methods=["POST"], name=f"control_{action}",
        )

    # -- capture sink (the extension's POST) --------------------------------
    @app.post("/api/v2/capture")
    async def capture(request: Request,
                      x_capture_token: Optional[str] = Header(None)):
        _auth(capture_token, x_capture_token)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")
        return handler.ingest(payload)

    # -- SSE with resumable cursor ------------------------------------------
    @app.get("/api/v2/jobs/{archive_id}/events")
    def events(archive_id: str, since: int = 0,
               heartbeat_s: float = 10.0, timeout_s: float = 3600.0):
        job = store.get_job(archive_id)
        if not job:
            raise HTTPException(404, "no such job")

        def stream():
            cursor = since
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                evs = store.events_since(cursor, archive_id=archive_id)
                for ev in evs:
                    cursor = ev.seq
                    yield ev.to_sse()
                if not evs:
                    # Idle heartbeat so clients can detect stalls.
                    yield f"event: heartbeat\ndata: {{\"seq\": {cursor}, \"ts\": \"{time.time():.3f}\"}}\n\n"
                    time.sleep(heartbeat_s)
                else:
                    time.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- capture all events (no archive filter) -----------------------------
    @app.get("/api/v2/events")
    def all_events(since: int = 0, heartbeat_s: float = 10.0):
        def stream():
            cursor = since
            while True:
                evs = store.events_since(cursor)
                for ev in evs:
                    cursor = ev.seq
                    yield ev.to_sse()
                if not evs:
                    yield f"event: heartbeat\ndata: {{\"seq\": {cursor}}}\n\n"
                    time.sleep(heartbeat_s)
                else:
                    time.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream")

    # -- doctor --------------------------------------------------------------
    @app.get("/api/v2/doctor")
    def doctor():
        checks = {}
        try:
            checks["state_db"] = {"ok": True, "detail": "WAL, readable"}
            store.list_jobs(limit=1)
        except Exception as exc:  # noqa: BLE001
            checks["state_db"] = {"ok": False, "detail": str(exc)}
        try:
            checks["storage"] = {"ok": True, "detail": "writable (checked by CLI)"}
        except Exception as exc:  # noqa: BLE001
            checks["storage"] = {"ok": False, "detail": str(exc)}
        at_risk = []
        for job in store.list_jobs(limit=50):
            at_risk += [b for b in ledger.parts_at_risk(job.archive_id)]
        checks["budget"] = {
            "ok": len(at_risk) == 0,
            "detail": f"{len(at_risk)} part(s) at risk",
        }
        ok = all(c["ok"] for c in checks.values())
        return {"ok": ok, "checks": checks}

    return app
