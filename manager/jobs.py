"""Job model + state machine + persistence.

Spec: docs/webgui/02-manager-service.md ("State model", "Job status enum").

A Job is one download run for one workflow (one export instance). State is
persisted to <outdir>/.manager_state.json so a manager restart recovers it.
This module is pure data + persistence; the engine bridge (engine_bridge.py)
drives transitions and the FastAPI app (app.py) reads snapshots.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATE_NAME = ".manager_state.json"

# Status enum (see spec). The needs_cookie <-> downloading transition is the
# auto-relogin heartbeat.
IDLE = "idle"
QUEUED = "queued"
DOWNLOADING = "downloading"
NEEDS_COOKIE = "needs_cookie"
PAUSED = "paused"
COMPLETE = "complete"
ERROR = "error"

ALL_STATUSES = {IDLE, QUEUED, DOWNLOADING, NEEDS_COOKIE, PAUSED, COMPLETE, ERROR}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path(output_dir: Path) -> Path:
    return Path(output_dir) / STATE_NAME


class Job:
    """In-memory job state with thread-safe mutation and disk persistence."""

    def __init__(self, job_id: str, workflow: str, output_dir: Path,
                 parallel: int, max_exports: int, meta: Optional[dict] = None):
        self.lock = threading.Lock()
        self.job_id = job_id
        self.workflow = workflow
        self.output_dir = Path(output_dir)
        self.parallel = parallel
        self.max_exports = max_exports
        self.meta = meta or {}
        self.status = QUEUED
        self.created_at = _now()
        self.updated_at = self.created_at
        self.cookie_captured_at = self.meta.get("captured_at") or self.created_at
        self.last_error: Optional[str] = None
        self.recipe_ref: Optional[str] = workflow
        # part_index -> dict(index, filename, url, size, done, status, started_at, completed_at, zip_valid, sha256)
        self.parts: dict[int, dict] = {}

    # -- mutation -------------------------------------------------------------
    def set_status(self, status: str, error: Optional[str] = None) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"bad status {status}")
        with self.lock:
            self.status = status
            if error is not None:
                self.last_error = error
            if status != ERROR:
                # keep last_error visible only while in error; clear on recover
                if status in (DOWNLOADING, COMPLETE):
                    self.last_error = None
            self.updated_at = _now()

    def update_part(self, index: int, **kw) -> None:
        with self.lock:
            p = self.parts.get(index)
            if p is None:
                p = {"index": index, "filename": "", "url": "", "size": 0,
                      "done": 0, "status": "queued", "started_at": None,
                      "completed_at": None, "zip_valid": None, "sha256": None}
                self.parts[index] = p
            prev_status = p["status"]
            for k, v in kw.items():
                p[k] = v
            new_status = p["status"]
            if new_status == "active" and not p["started_at"]:
                p["started_at"] = _now()
            if new_status == "done" and prev_status != "done":
                p["completed_at"] = _now()
            self.updated_at = _now()

    def refresh_cookie(self, captured_at: Optional[str] = None) -> None:
        with self.lock:
            self.cookie_captured_at = captured_at or _now()
            self.updated_at = _now()

    # -- snapshots ------------------------------------------------------------
    def totals(self) -> dict:
        with self.lock:
            parts = list(self.parts.values())
        parts_total = len(parts)
        parts_done = sum(1 for p in parts if p["status"] == "done")
        bytes_total = sum(p["size"] or 0 for p in parts)
        bytes_done = sum(p["done"] or 0 for p in parts)
        speed = sum(p.get("speed", 0) or 0 for p in parts if p["status"] == "active")
        return {
            "parts_total": parts_total,
            "parts_done": parts_done,
            "bytes_total": bytes_total,
            "bytes_done": bytes_done,
            "speed_bps": speed,
        }

    def snapshot(self) -> dict:
        with self.lock:
            parts = [dict(p) for p in sorted(self.parts.values(), key=lambda x: x["index"])]
            base = {
                "job_id": self.job_id,
                "workflow": self.workflow,
                "output_dir": str(self.output_dir),
                "status": self.status,
                "parallel": self.parallel,
                "max_exports": self.max_exports,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "cookie_captured_at": self.cookie_captured_at,
                "last_error": self.last_error,
                "recipe_ref": self.recipe_ref,
                "meta": dict(self.meta),
            }
        base["totals"] = self.totals()
        base["parts"] = parts
        return base

    def summary(self) -> dict:
        s = self.snapshot()
        return {
            "job_id": s["job_id"],
            "workflow": s["workflow"],
            "status": s["status"],
            "output_dir": s["output_dir"],
            "totals": s["totals"],
            "updated_at": s["updated_at"],
            "last_error": s["last_error"],
        }

    # -- persistence ----------------------------------------------------------
    def persist(self) -> None:
        snap = self.snapshot()
        p = state_path(self.output_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=2)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @classmethod
    def from_state(cls, snap: dict) -> "Job":
        job = cls(
            job_id=snap["job_id"],
            workflow=snap["workflow"],
            output_dir=Path(snap["output_dir"]),
            parallel=snap.get("parallel", 4),
            max_exports=snap.get("max_exports", 500),
            meta=snap.get("meta", {}),
        )
        job.status = snap.get("status", QUEUED)
        job.created_at = snap.get("created_at", job.created_at)
        job.updated_at = snap.get("updated_at", job.updated_at)
        job.cookie_captured_at = snap.get("cookie_captured_at", job.cookie_captured_at)
        job.last_error = snap.get("last_error")
        job.recipe_ref = snap.get("recipe_ref", job.workflow)
        for p in snap.get("parts", []):
            job.parts[p["index"]] = dict(p)
        # A job that was mid-download when the manager died is resumable.
        if job.status == DOWNLOADING:
            job.status = QUEUED
        return job

    @classmethod
    def load_from(cls, output_dir: Path) -> Optional["Job"]:
        p = state_path(output_dir)
        if not p.exists():
            return None
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
            return cls.from_state(snap)
        except (json.JSONDecodeError, OSError, KeyError):
            return None
