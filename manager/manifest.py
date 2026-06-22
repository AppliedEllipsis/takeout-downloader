"""Per-run manifest.json: the durable record of what was downloaded.

Spec: docs/webgui/02-manager-service.md ("Per-run manifest"). Distinct from
.manager_state.json (live job bookkeeping). The manifest is the data source for
the UI's completed-files table, the Telegram /status summary, and recipes.

Atomic writes (temp + os.replace) so a crash mid-write never corrupts it.

The Manifest class wraps one output directory. It is written incrementally as
parts complete and finalized when the job ends, so a manifest on disk always
reflects what has actually landed.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(output_dir: Path) -> Path:
    return Path(output_dir) / MANIFEST_NAME


class Manifest:
    """Incremental per-run manifest writer for one output directory."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self._lock = threading.Lock()
        self._data: dict = self._load()

    # -- io -------------------------------------------------------------------
    def _load(self) -> dict:
        p = manifest_path(self.output_dir)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"files": {}}

    def _save(self) -> None:
        p = manifest_path(self.output_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._serializable(), fh, indent=2, sort_keys=False)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _serializable(self) -> dict:
        """Files are kept as an index->row dict internally; emit a sorted list."""
        out = dict(self._data)
        files = self._data.get("files", {})
        out["files"] = [files[k] for k in sorted(files, key=lambda x: int(x))]
        return out

    # -- header ---------------------------------------------------------------
    def set_header(self, job, exports) -> None:
        """Seed identity + totals from the job and the discovered export list.

        `job` is a manager.jobs.Job; `exports` is a list of takeout_dl.Export.
        Safe to call repeatedly (e.g. after a cookie refresh re-discovers).
        """
        meta = getattr(job, "meta", {}) or {}
        with self._lock:
            self._data.setdefault("files", {})
            self._data.update({
                "account_label": meta.get("account_label") or job.workflow,
                "account_email": meta.get("email"),
                "gaia_user": meta.get("user"),
                "authuser": meta.get("authuser"),
                "export_timestamp": meta.get("export_ts"),
                "export_raw": meta.get("export_raw"),
                "archive_id": meta.get("archive_id"),
                "job_id": job.job_id,
                "output_dir": str(job.output_dir),
                "captured_at": job.cookie_captured_at,
                "started_at": self._data.get("started_at") or job.created_at,
                "completed_at": self._data.get("completed_at"),
                "parts_total": len(exports),
                "bytes_total": sum(getattr(e, "size", 0) or 0 for e in exports),
            })
            self._save()

    # -- per-part -------------------------------------------------------------
    def record_part(self, part: dict) -> None:
        """Record one part's final-ish state. `part` is a Job.parts[i] row."""
        if not part:
            return
        idx = part.get("index")
        if idx is None:
            return
        with self._lock:
            files = self._data.setdefault("files", {})
            files[str(idx)] = {
                "index": idx,
                "filename": part.get("filename"),
                "size": part.get("size"),
                "sha256": part.get("sha256"),
                "started_at": part.get("started_at"),
                "completed_at": part.get("completed_at"),
                "zip_valid": part.get("zip_valid"),
                "status": part.get("status"),
            }
            self._save()

    # -- finalize -------------------------------------------------------------
    def finalize(self, job, completed: bool) -> None:
        with self._lock:
            # Reconcile the files list from the job's AUTHORITATIVE parts dict.
            # record_part() writes incrementally during the run, but under
            # parallel completion a terminal callback can be missed; the job's
            # parts dict is the source of truth, so we rebuild from it here.
            job_parts = getattr(job, "parts", {}) or {}
            files = self._data.setdefault("files", {})
            for idx, part in job_parts.items():
                files[str(idx)] = {
                    "index": part.get("index", idx),
                    "filename": part.get("filename"),
                    "size": part.get("size"),
                    "sha256": part.get("sha256"),
                    "started_at": part.get("started_at"),
                    "completed_at": part.get("completed_at"),
                    "zip_valid": part.get("zip_valid"),
                    "status": part.get("status"),
                }
            self._data["completed_at"] = _now() if completed else None
            self._data["parts_done"] = sum(
                1 for f in files.values() if f.get("status") == "done")
            self._data["bytes_done"] = sum(
                (f.get("size") or 0) for f in files.values()
                if f.get("status") == "done")
            self._data["final_status"] = "complete" if completed else job.status
            self._save()

    # -- read -----------------------------------------------------------------
    def data(self) -> dict:
        with self._lock:
            return self._serializable()
