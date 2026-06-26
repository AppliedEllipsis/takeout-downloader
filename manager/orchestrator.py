"""Orchestrator: the single owner of all jobs and their runners.

Sits between the HTTP layer (app.py) and the engine bridge (engine_bridge.py).
Responsibilities:
  - turn an incoming payload into the right Job (new, or resume an existing
    job that is sitting in needs_cookie for the SAME export),
  - derive the dated output directory (account/export-ts) and enforce the
    allowlist,
  - keep an in-memory registry of jobs + runners, recover persisted jobs,
  - fan job lifecycle events out to notifiers (Telegram) and SSE subscribers.

Spec: docs/webgui/02-manager-service.md.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

import takeout_dl as engine

from . import jobs as J
from . import derive as D
from .config import Config, get_config
from .engine_bridge import JobRunner


def _payload_filenames(payload: "engine.Payload") -> list[str]:
    names = []
    for e in getattr(payload, "exports", []) or []:
        fn = getattr(e, "filename", "") or ""
        names.append(fn)
        url = getattr(e, "url", "") or ""
        if url:
            names.append(url)
    if getattr(payload, "url", ""):
        names.append(payload.url)
    return names


def _payload_meta(payload: "engine.Payload", raw: dict | None) -> dict:
    """Pull account identity hints out of the payload + its raw JSON.

    The extension puts user/authuser/email under a top-level "meta" object (see
    docs/webgui/03-extension-v4.md). We also accept them at the top level.
    """
    meta = {}
    raw = raw or {}
    # The v4 extension nests identity under "_meta"; older popup payloads use
    # "_meta" too, and we also accept a bare "meta" or top-level keys.
    rmeta = {}
    for container_key in ("_meta", "meta"):
        c = raw.get(container_key)
        if isinstance(c, dict):
            rmeta = {**c, **rmeta}
    def _pick(*keys):
        for k in keys:
            v = rmeta.get(k) or raw.get(k)
            if v:
                return v
        return None
    # URL fallback: Google bakes ?user=<gaia>&authuser=<n> into every part URL.
    # The _meta/meta blocks often omit these (see unknown-account fallback), so
    # parse them off the first export URL when the meta blocks don't carry them.
    def _from_urls(param: str):
        from urllib.parse import urlparse, parse_qs
        for e in getattr(payload, "exports", []) or []:
            url = getattr(e, "url", "") or ""
            if not url:
                continue
            vals = parse_qs(urlparse(url).query).get(param)
            if vals and vals[0]:
                return vals[0]
        url = getattr(payload, "url", "") or ""
        if url:
            vals = parse_qs(urlparse(url).query).get(param)
            if vals and vals[0]:
                return vals[0]
        return None
    email = _pick("email")
    if email:
        meta["email"] = email
    label = _pick("label")
    if label:
        meta["label"] = label
    user = _pick("user") or _from_urls("user")
    if user:
        meta["user"] = user
    authuser = _pick("authuser") or _from_urls("authuser")
    if authuser:
        meta["authuser"] = authuser
    archive_id = _pick("archive_id", "archiveId")
    if archive_id:
        meta["archive_id"] = archive_id
    if getattr(payload, "captured_at", ""):
        meta["captured_at"] = payload.captured_at
    return meta


class Orchestrator:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()
        self._jobs: dict[str, J.Job] = {}
        self._runners: dict[str, JobRunner] = {}
        self._lock = threading.Lock()
        self._event_subs: list[Callable[[str, dict], None]] = []
        # job_id -> count of needs_cookie->resume cycles (feeds auth_loop detection)
        self._recapture_count: dict[str, int] = {}
        # job_id -> monotonic ts of last byte progress (feeds network_stall detection)
        self._last_progress: dict[str, float] = {}

    # -- event fan-out --------------------------------------------------------
    def subscribe(self, cb: Callable[[str, dict], None]) -> Callable[[], None]:
        """Register an event callback (kind, job_summary). Returns an unsub fn."""
        with self._lock:
            self._event_subs.append(cb)

        def _unsub():
            with self._lock:
                if cb in self._event_subs:
                    self._event_subs.remove(cb)

        return _unsub

    def _emit(self, kind: str, job: J.Job) -> None:
        summary = job.summary()
        # Track byte-progress freshness for stall detection.
        if summary.get("totals", {}).get("bytes_done"):
            import time as _t
            self._last_progress[job.job_id] = _t.monotonic()
        for cb in list(self._event_subs):
            try:
                cb(kind, summary)
            except Exception:  # noqa: BLE001 — a bad subscriber must not break a job
                pass

    # -- output dir derivation ------------------------------------------------
    def _derive_output_dir(self, meta: dict, payload: "engine.Payload",
                           label_override: Optional[str]) -> tuple[Path, D.Derivation]:
        d = D.derive(meta, _payload_filenames(payload),
                     getattr(payload, "captured_at", None), label_override)
        out = self.cfg.takeout_root / d.account_label / d.export_ts
        # Enforce the engine's allowlist (raises ValueError if outside it).
        validated = engine.validate_output_dir(out)
        return validated, d

    # -- payload intake -------------------------------------------------------
    def accept_payload(self, raw_text: str, label_override: Optional[str] = None) -> dict:
        """Parse + route a captured payload. Returns {job_id, status, resumed}."""
        payload = engine.parse_payload(raw_text)
        ok, msg = engine.validate_cookie(payload.cookie)
        if not ok:
            raise ValueError(msg)

        import json
        try:
            raw = json.loads(raw_text)
        except Exception:  # noqa: BLE001
            raw = {}
        meta = _payload_meta(payload, raw)

        out_dir, d = self._derive_output_dir(meta, payload, label_override)
        # Fold the derived identity into meta so the manifest header can read
        # account_label / export_ts / export_raw from a single place.
        meta["account_label"] = d.account_label
        meta["export_ts"] = d.export_ts
        meta["export_raw"] = d.export_raw

        resumed_job = None
        with self._lock:
            # Resume path: prefer the STABLE archive_id (the j= param is identical
            # across captures of the same export, even if the account label — and
            # thus the output dir — changed). Fall back to the output dir for
            # legacy jobs that have no archive_id recorded.
            existing = self._find_job_for_archive(meta.get("archive_id"))
            if existing is None:
                existing = self._find_job_for_dir(out_dir)
            if existing is not None:
                # A recovered job may have no runner yet — build one so resume
                # actually works (and we don't fall through to a duplicate job).
                runner = self._runners.get(existing.job_id)
                if runner is None:
                    runner = self._make_runner(existing)
                existing.meta.update(meta)
                runner.set_payload(payload)
                self._recapture_count[existing.job_id] = \
                    self._recapture_count.get(existing.job_id, 0) + 1
                resumed_job = existing
            else:
                # New job.
                job_id = f"{_compact_now()}-{d.account_label}"
                job = J.Job(job_id=job_id, workflow=d.account_label, output_dir=out_dir,
                            parallel=self.cfg.parallel, max_exports=self.cfg.max_exports,
                            meta=meta)
                self._jobs[job_id] = job
                runner = self._make_runner(job)

        # Emit + start OUTSIDE the lock: _emit -> Telegram can do a blocking
        # network POST, which must never be held under the global lock.
        if resumed_job is not None:
            r = self._runners[resumed_job.job_id]
            if not r.is_alive():
                r._stop.clear()
                r.start()
            self._emit("resumed", resumed_job)
            return {"job_id": resumed_job.job_id, "status": resumed_job.status,
                    "resumed": True}

        runner.set_payload(payload)
        runner.start()
        return {"job_id": job.job_id, "status": job.status, "resumed": False}

    def _make_runner(self, job: J.Job) -> JobRunner:
        """Create + register a JobRunner for a job. Single source of the event
        wiring so recover() and accept_payload() stay consistent."""
        runner = JobRunner(job, on_event=lambda kind, j: self._emit(kind, j),
                           on_auth=lambda j: self._emit("needs_cookie", j))
        self._runners[job.job_id] = runner
        return runner

    def _find_job_for_archive(self, archive_id) -> Optional[J.Job]:
        """Find a resumable job for THIS export by its stable archive_id."""
        if not archive_id:
            return None
        for job in self._jobs.values():
            if (job.meta or {}).get("archive_id") == archive_id and job.status in (
                    J.NEEDS_COOKIE, J.PAUSED, J.DOWNLOADING, J.QUEUED):
                return job
        return None

    def _find_job_for_dir(self, out_dir: Path) -> Optional[J.Job]:
        for job in self._jobs.values():
            if Path(job.output_dir) == Path(out_dir) and job.status in (
                    J.NEEDS_COOKIE, J.PAUSED, J.DOWNLOADING, J.QUEUED):
                return job
        return None

    # -- queries --------------------------------------------------------------
    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [j.summary() for j in sorted(
                self._jobs.values(), key=lambda x: x.created_at, reverse=True)]

    def get_job(self, job_id: str) -> Optional[J.Job]:
        return self._jobs.get(job_id)

    # -- control --------------------------------------------------------------
    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        runner = self._runners.get(job_id)
        if not job or not runner:
            return False
        runner.stop()
        job.set_status(J.PAUSED)
        job.persist()
        self._emit("paused", job)
        return True

    def cancel(self, job_id: str) -> bool:
        runner = self._runners.get(job_id)
        job = self._jobs.get(job_id)
        if runner:
            runner.stop()
        if job:
            job.set_status(J.ERROR, error="cancelled by user")
            job.persist()
            self._emit("error", job)
            return True
        return False


    def delete_job(self, job_id: str) -> bool:
        """Remove a finished/failed job from the registry and forget its
        persisted state so a restart won't resurrect it. Refuses while the job
        is actively downloading/queued (cancel it first). Downloaded files are
        kept on disk; only the .manager_state.json bookkeeping file is removed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in (J.DOWNLOADING, J.QUEUED):
                return False
            runner = self._runners.get(job_id)
            if runner:
                runner.stop()
            try:
                sp = J.state_path(job.output_dir)
                if sp.exists():
                    sp.unlink()
            except OSError:
                pass
            self._jobs.pop(job_id, None)
            self._runners.pop(job_id, None)
            self._recapture_count.pop(job_id, None)
            self._last_progress.pop(job_id, None)
        self._emit("deleted", job)
        return True

    def resume(self, job_id: str) -> bool:
        """Resume a paused job. The runner thread re-discovers + continues from
        partials with the last payload it holds. A truly expired cookie will
        re-enter needs_cookie immediately, which is correct."""
        job = self._jobs.get(job_id)
        runner = self._runners.get(job_id)
        if not job or not runner:
            return False
        if job.status not in (J.PAUSED, J.NEEDS_COOKIE, J.ERROR):
            return False
        job.set_status(J.QUEUED)
        job.persist()
        if not runner.is_alive():
            runner._stop.clear()
            runner.start()
        runner._fresh_cookie.set()
        self._emit("resumed", job)
        return True

    def request_recapture(self, job_id: str) -> bool:
        """Flip a job into needs_cookie so the extension/UI re-captures."""
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.set_status(J.NEEDS_COOKIE, error="manual recapture requested")
        job.persist()
        self._emit("needs_cookie", job)
        return True

    def diagnose(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(job_id)
        if not job:
            return None
        from . import diagnose as Dg
        return Dg.diagnose(job,
                           recapture_count=self._recapture_count.get(job_id, 0),
                           last_progress_ts=self._last_progress.get(job_id))

    # -- recovery -------------------------------------------------------------
    def recover(self) -> int:
        """Load persisted jobs from disk under the takeout root. Does not auto
        resume downloads (they need a fresh cookie); just makes them visible."""
        root = self.cfg.takeout_root
        if not root.exists():
            return 0
        found = 0
        for state in root.glob("*/*/.manager_state.json"):
            job = J.Job.load_from(state.parent)
            if job is None:
                continue
            with self._lock:
                if job.job_id not in self._jobs:
                    # Persisted jobs come back as needs_cookie unless complete.
                    if job.status not in (J.COMPLETE, J.ERROR):
                        job.status = J.NEEDS_COOKIE
                    self._jobs[job.job_id] = job
                    # Build a runner so resume/pause/recapture work and a fresh
                    # payload resumes THIS job instead of spawning a duplicate.
                    # (Completed/errored jobs need no runner.)
                    if job.status not in (J.COMPLETE, J.ERROR):
                        self._make_runner(job)
                    found += 1
        return found


def _compact_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


_orch: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch
