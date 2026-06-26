"""Engine bridge: drive takeout_dl in a worker thread, wire callbacks to Job.

Spec: docs/webgui/02-manager-service.md ("Engine integration", "Process layout").

The bridge owns the lifecycle of one download run. It:
  - parses the payload via the engine's parse_payload,
  - discovers exports (uses the extension's enumerated list when present),
  - runs download_exports in a daemon thread with progress_cb/auth_cb,
  - reflects every part change into the Job + manifest,
  - on auth challenge flips the Job to needs_cookie and waits for a fresh
    payload (POSTed to /api/payload) instead of the CLI's file-poll,
  - resumes the SAME job from partials when a fresh cookie arrives.

No download/integrity logic lives here — that all stays in takeout_dl.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import takeout_dl as engine

from . import jobs as J
from .manifest import Manifest


class JobRunner:
    """Runs one Job's download lifecycle across cookie refreshes."""

    def __init__(self, job: J.Job, on_event=None, on_auth=None):
        self.job = job
        self.on_event = on_event or (lambda kind, job: None)
        self.on_auth = on_auth or (lambda job: None)
        self.manifest = Manifest(job.output_dir)
        self._payload: Optional[engine.Payload] = None
        self._exports: list = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fresh_cookie = threading.Event()
        self._lock = threading.Lock()
        self._auth_fired = False

    # -- payload handling -----------------------------------------------------
    def set_payload(self, payload: "engine.Payload") -> None:
        """Install a (fresh) payload. Wakes a runner waiting in needs_cookie."""
        with self._lock:
            self._payload = payload
            self.job.refresh_cookie(getattr(payload, "captured_at", None))
        self._fresh_cookie.set()

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._fresh_cookie.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- callbacks from the engine -------------------------------------------
    def _progress_cb(self, part) -> None:
        # part is a takeout_dl.PartProgress copy.
        self.job.update_part(
            part.index,
            filename=part.filename,
            url=part.url,
            size=part.size,
            done=part.done,
            status=part.status,
            speed=getattr(part, "speed", 0.0),
        )
        if part.status in ("done", "error", "auth"):
            self.job.persist()
            self.manifest.record_part(self.job.parts.get(part.index, {}))

    def _auth_cb(self, info: dict) -> None:
        self._auth_fired = True
        self.job.set_status(J.NEEDS_COOKIE, error=f"cookie expired: {info.get('url','')[:80]}")
        self.job.persist()
        self.on_auth(self.job)
        self.on_event("needs_cookie", self.job)

    # -- main loop ------------------------------------------------------------
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    payload = self._payload
                if payload is None:
                    # waiting for the very first payload (shouldn't happen:
                    # set_payload is called before start) — bail safe.
                    self.job.set_status(J.NEEDS_COOKIE, error="no payload")
                    self.on_auth(self.job)
                    if not self._fresh_cookie.wait(timeout=300):
                        self.job.set_status(J.ERROR, error="timed out waiting for payload")
                        self.on_event("error", self.job)
                        return
                    self._fresh_cookie.clear()
                    continue

                # Discover exports ONCE. On a cookie-refresh resume the list is
                # already cached -- re-sweeping 60+ parts (each a 1-byte probe)
                # burns the whole short-lived Takeout cookie BEFORE any real
                # download starts, which livelocks the resume. Reuse the cached
                # list so the fresh cookie is spent on bytes, not re-probing.
                try:
                    if not self._exports:
                        self._exports = engine.discover_exports(payload, self.job.max_exports)
                except engine.AuthError:
                    self._enter_needs_cookie("discovery auth failure")
                    if not self._wait_for_cookie():
                        return
                    continue
                except Exception as e:  # noqa: BLE001
                    self.job.set_status(J.ERROR, error=f"discovery failed: {e}")
                    self.job.persist()
                    self.on_event("error", self.job)
                    return

                # seed parts + manifest header
                for ex in self._exports:
                    self.job.update_part(ex.index, filename=ex.filename, url=ex.url,
                                         size=ex.size, status="queued")
                self.manifest.set_header(self.job, self._exports)
                self.job.set_status(J.DOWNLOADING)
                self.job.persist()
                self.on_event("started", self.job)

                self._auth_fired = False
                try:
                    engine.download_exports(
                        self._exports, payload, self.job.output_dir,
                        self.job.parallel,
                        progress_cb=self._progress_cb,
                        auth_cb=self._auth_cb,
                    )
                except engine.AuthError:
                    # auth_cb already flipped status + notified; wait for refresh.
                    if not self._wait_for_cookie():
                        return
                    continue
                except Exception as e:  # noqa: BLE001
                    self.job.set_status(J.ERROR, error=f"download error: {e}")
                    self.job.persist()
                    self.on_event("error", self.job)
                    return

                # download_exports returned without AuthError => all parts done
                # or non-auth-stopped. Verify + finalize.
                complete, incomplete = engine.verify_exports(self._exports, self.job.output_dir)
                for ex in complete:
                    self.job.update_part(ex.index, status="done", zip_valid=True)
                if incomplete and not self._stop.is_set():
                    # Some parts not valid and no auth challenge — treat as error
                    # but keep partials for a manual resume.
                    self.job.set_status(J.ERROR,
                                        error=f"{len(incomplete)} part(s) failed validation")
                    self.job.persist()
                    self.manifest.finalize(self.job, completed=False)
                    self.on_event("error", self.job)
                    return

                self.job.set_status(J.COMPLETE)
                self.job.persist()
                self.manifest.finalize(self.job, completed=True)
                self.on_event("complete", self.job)
                return
        finally:
            pass

    def _enter_needs_cookie(self, reason: str) -> None:
        self.job.set_status(J.NEEDS_COOKIE, error=reason)
        self.job.persist()
        self.on_auth(self.job)
        self.on_event("needs_cookie", self.job)

    def _wait_for_cookie(self, timeout: float = 3600.0) -> bool:
        """Block until a fresh payload arrives (set_payload) or stop/timeout.

        Returns True to continue the loop, False to terminate the runner.
        """
        self._fresh_cookie.clear()
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            if self._fresh_cookie.wait(timeout=5.0):
                self._fresh_cookie.clear()
                if self._stop.is_set():
                    return False
                return True
            if time.monotonic() > deadline:
                self.job.set_status(J.ERROR, error="timed out waiting for fresh cookie")
                self.job.persist()
                self.on_event("error", self.job)
                return False
        return False
