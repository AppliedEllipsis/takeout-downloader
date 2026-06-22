"""Reason-code diagnosis for a job.

Maps live job state to ONE machine-readable reason code plus a recommended
action, so a weaker model (or the Pi agent) does not have to infer state from
logs. The codes here are the EXACT set used in docs/webgui/04-decision-trees.md:

  cookie_expired | auth_loop | disk_full | network_stall |
  zip_validation_failed | browser_down | manager_down | ok

Each code carries: section (the runbook §), auto_recovers (bool), needs_human
(bool), and a one-line recommended action.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

from . import jobs as J

# reason -> static guidance, mirrors the decision-trees table.
_GUIDE = {
    "ok":                    ("—",  False, False, "Nothing wrong; job is progressing or done."),
    "cookie_expired":        ("§A", True,  False, "Extension auto-recaptures; resumes on fresh cookie."),
    "auth_loop":             ("§B", False, True,  "Google forced logout; log in via the KasmVNC portal."),
    "disk_full":             ("§C", False, True,  "Free space under the storage root, then resume."),
    "network_stall":         ("§D", True,  False, "Engine retries with backoff; check connectivity if persistent."),
    "zip_validation_failed": ("§E", True,  False, "Resume re-downloads the bad part via Range; corrupt only if it repeats."),
    "browser_down":          ("§F", False, True,  "Chromium/CDP unreachable; restart the webtop container."),
    "manager_down":          ("§G", False, True,  "Manager not running; restart the service."),
}

# How long without byte progress before we call it a stall (seconds).
STALL_SECONDS = 180
# Consecutive cookie_expired cycles before we escalate to auth_loop.
AUTH_LOOP_THRESHOLD = 2


def _free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return -1


def diagnose(job: "J.Job", *, recapture_count: int = 0,
             last_progress_ts: Optional[float] = None) -> dict:
    """Return a structured diagnosis for one job.

    recapture_count: how many times this job has gone needs_cookie -> resume.
    last_progress_ts: monotonic ts of the last byte progress (orchestrator owns it).
    """
    snap = job.snapshot()
    status = snap["status"]
    totals = snap["totals"]
    last_error = (snap.get("last_error") or "").lower()

    reason = "ok"

    if status == J.NEEDS_COOKIE:
        reason = "auth_loop" if recapture_count >= AUTH_LOOP_THRESHOLD else "cookie_expired"
    elif status == J.ERROR:
        if "disk" in last_error or "space" in last_error or "no space" in last_error:
            reason = "disk_full"
        elif "valid" in last_error or "zip" in last_error:
            reason = "zip_validation_failed"
        elif "auth" in last_error or "cookie" in last_error:
            reason = "auth_loop"
        else:
            # Generic error: check disk as the most common silent culprit.
            free = _free_bytes(Path(snap["output_dir"]))
            reason = "disk_full" if 0 <= free < (2 * 1024**3) else "network_stall"
    elif status == J.DOWNLOADING:
        # Active but no byte movement for a while => stall.
        if last_progress_ts is not None and totals["speed_bps"] == 0:
            if (time.monotonic() - last_progress_ts) > STALL_SECONDS:
                reason = "network_stall"

    section, auto_recovers, needs_human, action = _GUIDE.get(reason, _GUIDE["ok"])

    free = _free_bytes(Path(snap["output_dir"]))
    return {
        "job_id": snap["job_id"],
        "status": status,
        "reason": reason,
        "section": section,
        "auto_recovers": auto_recovers,
        "needs_human": needs_human,
        "recommended_action": action,
        "recapture_count": recapture_count,
        "free_bytes": free,
        "parts_done": totals["parts_done"],
        "parts_total": totals["parts_total"],
        "bytes_done": totals["bytes_done"],
        "bytes_total": totals["bytes_total"],
        "speed_bps": totals["speed_bps"],
        "last_error": snap.get("last_error"),
    }
