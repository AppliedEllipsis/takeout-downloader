"""Autonomous job runner — the piece that makes v2 self-driving.

WHY this module exists
======================
Everything else in v2 is a library: you capture, then *you* run the CLI, and if
the cookie dies the transfer stops until *you* notice. A 3 TB / 63-part export
takes days while the download cookie idles out in ~1-2 minutes. No human can
babysit that, so something in-process has to own the loop.

This is a direct port of the pattern proven in v1's ``manager/engine_bridge.py``:
a daemon thread that parks on a ``threading.Event`` when the cookie dies and
wakes the instant a fresh capture arrives.

The expensive lesson inherited from v1 (see its comment at engine_bridge.py:110)
is encoded here as `R-DISCOVER`: **do not re-discover parts on a cookie refresh.**
Re-sweeping 60+ parts spends the freshly captured, short-lived cookie on probes
before a single byte moves, which livelocks the resume. The plan is resolved from
local state and reused.

Invariants (docs/v2/08-SELF-DRIVING-UX.md §1.2) — all enforced here:

R1  One runner per archive_id. ``start()`` on a live runner is a no-op, never a
    second thread: two threads on one part would spend two of the five attempts
    for a single file.
R2  Never retry instantly. ``burst_gap_s`` separates bursts, so a failing job
    cannot drain its whole attempt budget in a tight loop.
R3  On auth failure park in NEEDS_COOKIE and WAIT on an Event — never poll
    Google with a cookie already known to be dead (that spends attempts for
    zero bytes).
R4  Daemon thread: process exit never blocks on a runner.
R5  Adds ZERO new request paths. Every attempt still flows through
    ``ledger.reserve()`` inside the engine; the ledger stays the only truth.
R6  BUDGET_EXHAUSTED stops the runner and needs an explicit human unblock.
R7  Runner state is in memory only; ``state.db`` remains the source of truth so
    a manager restart re-derives everything.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from .contracts import JobStatus, PartStatus

log = logging.getLogger("takeout2.runner")

__all__ = ["JobRunner", "RunnerSupervisor", "RunnerConfig",
           "TERMINAL_STATUSES", "BLOCKED_STATUSES"]

#: Job states from which a runner must not proceed on its own.
TERMINAL_STATUSES = frozenset({JobStatus.COMPLETE, JobStatus.FAILED})
#: States needing a human decision (R6) — never auto-cleared.
BLOCKED_STATUSES = frozenset({JobStatus.BUDGET_EXHAUSTED, JobStatus.PAUSED})


class RunnerConfig:
    """Tunables. Plain class (not a dataclass) to stay importable anywhere."""

    def __init__(self, burst_gap_s: float = 5.0,
                 cookie_wait_s: float = 300.0,
                 max_consecutive_failures: int = 5,
                 idle_exit_s: Optional[float] = None):
        #: R2 — minimum gap between bursts.
        self.burst_gap_s = burst_gap_s
        #: How long to park in NEEDS_COOKIE before re-checking (the Event wakes
        #: us sooner; this is only the ceiling so a missed signal self-heals).
        self.cookie_wait_s = cookie_wait_s
        #: Consecutive burst failures that move zero bytes before giving up.
        self.max_consecutive_failures = max_consecutive_failures
        #: Optional: stop the thread after this long with nothing to do.
        self.idle_exit_s = idle_exit_s


class JobRunner:
    """Owns one archive's download loop in a daemon thread.

    The engine is injected as a factory so tests drive the whole state machine
    with no network, no sleeping, and no real cookie.
    """

    def __init__(self, archive_id: str, store, *,
                 engine_factory: Callable[[], Any],
                 config: Optional[RunnerConfig] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 sleep_fn: Optional[Callable[[float], None]] = None,
                 now_fn: Optional[Callable[[], float]] = None):
        self.archive_id = archive_id
        self.store = store
        self.engine_factory = engine_factory
        self.config = config or RunnerConfig()
        self.on_event = on_event or (lambda kind, data: None)
        self._sleep = sleep_fn or time.sleep
        self._now = now_fn or time.monotonic

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fresh_cookie = threading.Event()
        self._lock = threading.RLock()

        # Observability (surfaced via snapshot(); not authoritative state).
        self.bursts = 0
        self.cookie_waits = 0
        self.self_heals = 0
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self._consecutive_failures = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Idempotent (R1). Starting a live runner does nothing."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.debug("runner %s already alive; start() is a no-op",
                          self.archive_id)
                return
            self._stop.clear()
            self.started_at = self._now()
            self._thread = threading.Thread(
                target=self._run, name=f"tk2-runner-{self.archive_id[:12]}",
                daemon=True)                                            # R4
            self._thread.start()
            log.info("runner started for %s", self.archive_id)
            self._emit("runner_started", {})

    def stop(self, *, wait: bool = False, timeout: float = 10.0) -> None:
        self._stop.set()
        self._fresh_cookie.set()          # unblock a parked cookie wait
        thread = self._thread
        if wait and thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        log.info("runner stop requested for %s", self.archive_id)

    def notify_cookie(self) -> None:
        """Wake a runner parked in NEEDS_COOKIE (R3). This is the self-heal."""
        self._fresh_cookie.set()
        log.info("fresh cookie signalled for %s", self.archive_id)

    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def snapshot(self) -> dict:
        job = self.store.get_job(self.archive_id)
        return {
            "archive_id": self.archive_id,
            "alive": self.is_alive(),
            "status": getattr(job, "status", None).value
                      if job is not None and getattr(job, "status", None) is not None
                      else None,
            "bursts": self.bursts,
            "cookie_waits": self.cookie_waits,
            "self_heals": self.self_heals,
            "last_error": self.last_error,
            "started_at": self.started_at,
        }

    # -- internals ---------------------------------------------------------
    def _emit(self, kind: str, data: dict) -> None:
        try:
            self.store.emit(kind, archive_id=self.archive_id, **data)
        except Exception as exc:                                   # noqa: BLE001
            log.debug("emit %s failed: %s", kind, exc)
        try:
            self.on_event(kind, {"archive_id": self.archive_id, **data})
        except Exception as exc:                                   # noqa: BLE001
            log.debug("on_event %s failed: %s", kind, exc)

    def _set_status(self, status: JobStatus, error: Optional[str] = None) -> None:
        try:
            self.store.set_job_status(self.archive_id, status, error=error)
        except TypeError:                       # store without an error kwarg
            self.store.set_job_status(self.archive_id, status)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("could not set status %s: %s", status, exc)

    def _remaining_parts(self) -> list:
        """Parts still worth working on. Local state only — no requests."""
        try:
            parts = self.store.list_parts(self.archive_id)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("cannot list parts for %s: %s", self.archive_id, exc)
            return []
        return [p for p in parts
                if p.status not in (PartStatus.DONE, PartStatus.SKIPPED,
                                    PartStatus.BUDGET_EXHAUSTED)]

    def _wait_for_cookie(self) -> bool:
        """Park until a fresh capture arrives (R3). False if stopping."""
        self.cookie_waits += 1
        self._fresh_cookie.clear()
        self._emit("needs_cookie", {"waits": self.cookie_waits})
        log.info("%s parked in NEEDS_COOKIE, waiting for a fresh capture",
                 self.archive_id)
        deadline_gap = self.config.cookie_wait_s
        while not self._stop.is_set():
            if self._fresh_cookie.wait(timeout=deadline_gap):
                self._fresh_cookie.clear()
                if self._stop.is_set():
                    return False
                self.self_heals += 1
                # Operator asked to be notified on every self-heal.
                self._emit("self_heal", {"self_heals": self.self_heals})
                log.info("%s got a fresh cookie, resuming", self.archive_id)
                return True
            # Timed out: loop and keep waiting. A job with no cookie is not a
            # failure, it is simply blocked on a human/extension action.
        return False

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                job = self.store.get_job(self.archive_id)
                if job is None:
                    self.last_error = "job vanished from state.db"
                    log.error("runner %s: %s", self.archive_id, self.last_error)
                    return

                status = getattr(job, "status", None)
                if status in TERMINAL_STATUSES:
                    log.info("runner %s: job is %s, exiting", self.archive_id,
                             status)
                    return
                if status in BLOCKED_STATUSES:                            # R6
                    log.info("runner %s: job is %s and needs a human decision",
                             self.archive_id, status)
                    self._emit("runner_blocked", {"status": status.value})
                    return

                remaining = self._remaining_parts()
                if not remaining:
                    self._set_status(JobStatus.COMPLETE)
                    self._emit("job_complete", {})
                    log.info("runner %s: all parts done", self.archive_id)
                    return

                # --- one burst -------------------------------------------
                try:
                    engine = self.engine_factory()
                except Exception as exc:                           # noqa: BLE001
                    # Almost always "cannot reach Chrome/CDP for a cookie".
                    self.last_error = f"cannot build engine: {exc}"
                    self._set_status(JobStatus.NEEDS_COOKIE,
                                     error=self.last_error)
                    if not self._wait_for_cookie():
                        return
                    continue

                self._set_status(JobStatus.DOWNLOADING)
                self.bursts += 1
                try:
                    # R-DISCOVER: run_burst plans from local state only; we
                    # never re-discover here, so a fresh cookie is spent on
                    # bytes rather than on re-probing 60+ parts.
                    result = engine.run_burst(self.archive_id)
                except Exception as exc:                           # noqa: BLE001
                    self.last_error = f"burst raised: {exc}"
                    self._consecutive_failures += 1
                    log.warning("runner %s burst failed: %s",
                                self.archive_id, exc)
                    self._emit("burst_failed", {"error": str(exc)})
                    if self._consecutive_failures >= self.config.max_consecutive_failures:
                        self._set_status(JobStatus.FAILED, error=self.last_error)
                        self._emit("runner_gave_up",
                                   {"failures": self._consecutive_failures})
                        return
                    self._sleep(self.config.burst_gap_s)                  # R2
                    continue

                moved = getattr(result, "bytes_moved", 0) or 0
                if moved > 0:
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1

                self._emit("burst_done", {
                    "ok": getattr(result, "completed_ok", 0),
                    "partial": getattr(result, "completed_partial", 0),
                    "bytes": moved,
                    "attempts": getattr(result, "attempts_spent", 0),
                })

                # Auth failure inside the burst → park, do not hammer (R3).
                job_after = self.store.get_job(self.archive_id)
                status_after = getattr(job_after, "status", None)
                if status_after is JobStatus.NEEDS_COOKIE:
                    if not self._wait_for_cookie():
                        return
                    continue
                if status_after in BLOCKED_STATUSES:                      # R6
                    self._emit("runner_blocked",
                               {"status": getattr(status_after, "value", "?")})
                    return

                if getattr(result, "budget_exhausted", 0) and moved == 0:
                    # Every remaining part is out of attempts: a human must
                    # decide (re-request the export, or accept partial).
                    self._set_status(JobStatus.BUDGET_EXHAUSTED)
                    self._emit("budget_exhausted", {})
                    return

                if self._consecutive_failures >= self.config.max_consecutive_failures:
                    self.last_error = (f"{self._consecutive_failures} bursts "
                                       f"moved no bytes")
                    self._set_status(JobStatus.FAILED, error=self.last_error)
                    self._emit("runner_gave_up",
                               {"failures": self._consecutive_failures})
                    return

                self._sleep(self.config.burst_gap_s)                      # R2
        except Exception as exc:                                   # noqa: BLE001
            self.last_error = f"runner crashed: {exc}"
            log.exception("runner %s crashed", self.archive_id)
            self._emit("runner_crashed", {"error": str(exc)})
        finally:
            self._emit("runner_stopped", {})
            log.info("runner exited for %s", self.archive_id)


class RunnerSupervisor:
    """Process-wide registry: at most one live runner per archive (R1)."""

    def __init__(self, store, *, engine_factory: Callable[[str], Any],
                 config: Optional[RunnerConfig] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        self.store = store
        self.engine_factory = engine_factory
        self.config = config or RunnerConfig()
        self.on_event = on_event
        self._runners: dict[str, JobRunner] = {}
        self._lock = threading.RLock()

    def ensure(self, archive_id: str) -> JobRunner:
        """Create-or-get. Never returns a second runner for one archive."""
        with self._lock:
            runner = self._runners.get(archive_id)
            if runner is None or (not runner.is_alive() and runner._stop.is_set()):
                runner = JobRunner(
                    archive_id, self.store,
                    engine_factory=lambda aid=archive_id: self.engine_factory(aid),
                    config=self.config, on_event=self.on_event)
                self._runners[archive_id] = runner
            return runner

    def get(self, archive_id: str) -> Optional[JobRunner]:
        with self._lock:
            return self._runners.get(archive_id)

    def notify_cookie(self, archive_id: str) -> bool:
        """Wake a parked runner. True if one existed to wake."""
        runner = self.get(archive_id)
        if runner is None:
            return False
        runner.notify_cookie()
        return True

    def stop(self, archive_id: str, *, wait: bool = False) -> bool:
        runner = self.get(archive_id)
        if runner is None:
            return False
        runner.stop(wait=wait)
        return True

    def stop_all(self, *, wait: bool = False) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop(wait=wait)

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            runners = list(self._runners.values())
        return [r.snapshot() for r in runners]
