"""Runner tests: the autonomy state machine, fully offline.

Every test here drives the real loop with a fake engine and a fake clock, so
the whole thing runs in milliseconds with no network, no threads sleeping, and
no Google attempts spent. The invariants under test are the ones in
docs/v2/08-SELF-DRIVING-UX.md §1.2 — each has a numbered comment (R1..R7)
because violating any of them costs real, unrecoverable download attempts.
"""
from __future__ import annotations

import threading
import time

import pytest

from takeout2.contracts import JobStatus, PartStatus
from takeout2.runner import (BLOCKED_STATUSES, JobRunner, RunnerConfig,
                             RunnerSupervisor, TERMINAL_STATUSES)


# ----------------------------------------------------------------- fakes
class FakeJob:
    def __init__(self, status=JobStatus.READY):
        self.status = status


class FakePart:
    def __init__(self, idx, status=PartStatus.PENDING):
        self.idx = idx
        self.status = status


class FakeStore:
    """In-memory stand-in for state.py with the handful of methods used."""

    def __init__(self, status=JobStatus.READY, parts=None, job=True):
        self.job = FakeJob(status) if job else None
        self.parts = parts if parts is not None else [FakePart(1), FakePart(2)]
        self.events = []
        self.status_history = []

    def get_job(self, archive_id):
        return self.job

    def list_parts(self, archive_id, status=None, **kw):
        return list(self.parts)

    def set_job_status(self, archive_id, status, error=None):
        if self.job is not None:
            self.job.status = status
        self.status_history.append((status, error))

    def emit(self, kind, archive_id=None, **data):
        self.events.append((kind, data))
        return len(self.events)

    def update_part(self, archive_id, idx, **kw):
        pass

    def kinds(self):
        return [k for k, _ in self.events]


class FakeResult:
    def __init__(self, bytes_moved=1000, completed_ok=1, completed_partial=0,
                 attempts_spent=1, budget_exhausted=0):
        self.bytes_moved = bytes_moved
        self.completed_ok = completed_ok
        self.completed_partial = completed_partial
        self.attempts_spent = attempts_spent
        self.budget_exhausted = budget_exhausted


class FakeEngine:
    """Records bursts; can finish the job, fail, or trip an auth failure."""

    def __init__(self, store, *, results=None, raises=None,
                 on_burst=None):
        self.store = store
        self.results = list(results or [])
        self.raises = raises
        self.on_burst = on_burst
        self.calls = 0

    def run_burst(self, archive_id, candidates=None):
        self.calls += 1
        if self.on_burst:
            self.on_burst(self.calls)
        if self.raises:
            raise self.raises
        if self.results:
            return self.results.pop(0)
        return FakeResult()


def make_runner(store, engine, **cfg):
    """Runner with an instant clock so burst gaps cost no wall time."""
    config = RunnerConfig(burst_gap_s=cfg.pop("burst_gap_s", 0.0),
                          cookie_wait_s=cfg.pop("cookie_wait_s", 0.05),
                          max_consecutive_failures=cfg.pop("max_failures", 3))
    return JobRunner("arch-1", store, engine_factory=lambda: engine,
                     config=config, sleep_fn=lambda s: None, **cfg)


def run_to_completion(runner, timeout=5.0):
    """Start and join, failing loudly instead of hanging the suite."""
    runner.start()
    deadline = time.time() + timeout
    while runner.is_alive() and time.time() < deadline:
        time.sleep(0.005)
    if runner.is_alive():
        runner.stop(wait=True, timeout=2.0)
        pytest.fail("runner did not terminate — possible infinite loop")


# ----------------------------------------------------------------- lifecycle
class TestLifecycle:
    def test_completes_when_all_parts_done(self):
        store = FakeStore(parts=[])          # nothing left to do
        runner = make_runner(store, FakeEngine(store))
        run_to_completion(runner)
        assert store.job.status is JobStatus.COMPLETE
        assert "job_complete" in store.kinds()

    def test_runs_bursts_until_parts_are_done(self):
        store = FakeStore()
        engine = FakeEngine(store)

        def finish_after_two(call_n):
            if call_n >= 2:
                store.parts = []             # engine "finished" the work
        engine.on_burst = finish_after_two

        runner = make_runner(store, engine)
        run_to_completion(runner)
        assert engine.calls == 2
        assert store.job.status is JobStatus.COMPLETE

    def test_start_is_idempotent_R1(self):
        """R1: two threads on one part would spend two of the five attempts."""
        store = FakeStore()
        started = threading.Event()
        engine = FakeEngine(store, on_burst=lambda n: started.set())
        runner = make_runner(store, engine, burst_gap_s=0.05)
        runner.start()
        started.wait(timeout=2.0)
        first = runner._thread
        runner.start()                        # must NOT spawn a second thread
        assert runner._thread is first
        runner.stop(wait=True)

    def test_thread_is_daemon_R4(self):
        store = FakeStore()
        runner = make_runner(store, FakeEngine(store), burst_gap_s=0.05)
        runner.start()
        assert runner._thread.daemon is True
        runner.stop(wait=True)

    def test_stop_terminates_the_loop(self):
        store = FakeStore()
        engine = FakeEngine(store, results=[FakeResult() for _ in range(500)])
        runner = make_runner(store, engine, burst_gap_s=0.01)
        runner.start()
        time.sleep(0.05)
        runner.stop(wait=True, timeout=3.0)
        assert not runner.is_alive()

    def test_missing_job_exits_cleanly(self):
        store = FakeStore(job=False)
        runner = make_runner(store, FakeEngine(store))
        run_to_completion(runner)
        assert "vanished" in (runner.last_error or "")


# ----------------------------------------------------------------- cookie
class TestCookieParking:
    def test_engine_build_failure_parks_in_needs_cookie_R3(self):
        """R3: a dead cookie must WAIT, never poll Google."""
        store = FakeStore()

        def boom():
            raise RuntimeError("cannot reach CDP")

        runner = JobRunner("arch-1", store, engine_factory=boom,
                           config=RunnerConfig(burst_gap_s=0.0,
                                               cookie_wait_s=0.05),
                           sleep_fn=lambda s: None)
        runner.start()
        time.sleep(0.1)
        assert store.job.status is JobStatus.NEEDS_COOKIE
        assert runner.cookie_waits >= 1
        runner.stop(wait=True)

    def test_notify_cookie_wakes_a_parked_runner(self):
        """The self-heal path: fresh capture → resume without human action."""
        store = FakeStore()
        engine = FakeEngine(store)
        attempts = {"n": 0}

        def factory():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("no cookie yet")
            store.parts = []                 # second try succeeds and finishes
            return engine

        runner = JobRunner("arch-1", store, engine_factory=factory,
                           config=RunnerConfig(burst_gap_s=0.0,
                                               cookie_wait_s=5.0),
                           sleep_fn=lambda s: None)
        runner.start()
        time.sleep(0.1)
        assert store.job.status is JobStatus.NEEDS_COOKIE

        runner.notify_cookie()               # the extension re-captured
        deadline = time.time() + 3.0
        while runner.is_alive() and time.time() < deadline:
            time.sleep(0.005)
        assert store.job.status is JobStatus.COMPLETE
        assert runner.self_heals >= 1
        assert "self_heal" in store.kinds(), "operator asked to be notified"

    def test_burst_setting_needs_cookie_parks_the_runner(self):
        store = FakeStore()

        def flip_to_needs_cookie(call_n):
            store.job.status = JobStatus.NEEDS_COOKIE

        engine = FakeEngine(store, on_burst=flip_to_needs_cookie)
        runner = make_runner(store, engine, cookie_wait_s=0.05)
        runner.start()
        time.sleep(0.15)
        assert runner.cookie_waits >= 1
        runner.stop(wait=True)

    def test_stop_releases_a_parked_runner(self):
        store = FakeStore()

        def boom():
            raise RuntimeError("no cookie")

        runner = JobRunner("arch-1", store, engine_factory=boom,
                           config=RunnerConfig(cookie_wait_s=60.0),
                           sleep_fn=lambda s: None)
        runner.start()
        time.sleep(0.05)
        runner.stop(wait=True, timeout=3.0)
        assert not runner.is_alive(), "stop must unblock a parked cookie wait"


# ----------------------------------------------------------------- budget
class TestBudgetAndBlocking:
    def test_budget_exhausted_stops_and_needs_a_human_R6(self):
        store = FakeStore()
        engine = FakeEngine(store, results=[
            FakeResult(bytes_moved=0, completed_ok=0, budget_exhausted=2)])
        runner = make_runner(store, engine)
        run_to_completion(runner)
        assert store.job.status is JobStatus.BUDGET_EXHAUSTED
        assert "budget_exhausted" in store.kinds()

    def test_paused_job_is_not_resumed_automatically(self):
        store = FakeStore(status=JobStatus.PAUSED)
        engine = FakeEngine(store)
        runner = make_runner(store, engine)
        run_to_completion(runner)
        assert engine.calls == 0, "a PAUSED job must never auto-start"

    def test_blocked_status_set_is_explicit(self):
        assert JobStatus.BUDGET_EXHAUSTED in BLOCKED_STATUSES
        assert JobStatus.PAUSED in BLOCKED_STATUSES
        assert JobStatus.COMPLETE in TERMINAL_STATUSES
        assert JobStatus.FAILED in TERMINAL_STATUSES

    @pytest.mark.parametrize("status", [JobStatus.COMPLETE, JobStatus.FAILED])
    def test_terminal_jobs_never_start_a_burst(self, status):
        store = FakeStore(status=status)
        engine = FakeEngine(store)
        runner = make_runner(store, engine)
        run_to_completion(runner)
        assert engine.calls == 0


# ----------------------------------------------------------------- failures
class TestFailureHandling:
    def test_gives_up_after_repeated_zero_byte_bursts(self):
        """R2-adjacent: a job that moves nothing must stop, not spin."""
        store = FakeStore()
        engine = FakeEngine(store, results=[
            FakeResult(bytes_moved=0, completed_ok=0) for _ in range(10)])
        runner = make_runner(store, engine, max_failures=3)
        run_to_completion(runner)
        assert store.job.status is JobStatus.FAILED
        assert engine.calls == 3, "should stop at the failure ceiling"

    def test_raising_engine_is_survived_then_gives_up(self):
        store = FakeStore()
        engine = FakeEngine(store, raises=RuntimeError("network gone"))
        runner = make_runner(store, engine, max_failures=2)
        run_to_completion(runner)
        assert store.job.status is JobStatus.FAILED
        assert "burst_failed" in store.kinds()

    def test_progress_resets_the_failure_counter(self):
        store = FakeStore()
        engine = FakeEngine(store, results=[
            FakeResult(bytes_moved=0),      # fail
            FakeResult(bytes_moved=999),    # progress → reset
            FakeResult(bytes_moved=0),      # fail
        ])

        def finish(call_n):
            if call_n >= 3:
                store.parts = []
        engine.on_burst = finish

        runner = make_runner(store, engine, max_failures=2)
        run_to_completion(runner)
        # Without the reset it would have FAILED at burst 2.
        assert store.job.status is JobStatus.COMPLETE

    def test_burst_gap_is_honoured_R2(self):
        """R2: never retry instantly — that would drain 5 attempts in seconds."""
        store = FakeStore()
        slept = []
        engine = FakeEngine(store, results=[FakeResult(bytes_moved=0)] * 3)
        config = RunnerConfig(burst_gap_s=7.5, max_consecutive_failures=2)
        runner = JobRunner("arch-1", store, engine_factory=lambda: engine,
                           config=config, sleep_fn=slept.append)
        run_to_completion(runner)
        assert 7.5 in slept, "a gap must separate bursts"


# ----------------------------------------------------------------- supervisor
class TestSupervisor:
    def test_ensure_returns_one_runner_per_archive_R1(self):
        store = FakeStore()
        sup = RunnerSupervisor(store,
                               engine_factory=lambda aid: FakeEngine(store))
        a = sup.ensure("arch-1")
        b = sup.ensure("arch-1")
        assert a is b, "R1: never two runners for one archive"

    def test_distinct_archives_get_distinct_runners(self):
        store = FakeStore()
        sup = RunnerSupervisor(store,
                               engine_factory=lambda aid: FakeEngine(store))
        assert sup.ensure("arch-1") is not sup.ensure("arch-2")

    def test_get_returns_none_for_unknown(self):
        sup = RunnerSupervisor(FakeStore(), engine_factory=lambda aid: None)
        assert sup.get("nope") is None

    def test_notify_cookie_reports_whether_a_runner_existed(self):
        store = FakeStore()
        sup = RunnerSupervisor(store,
                               engine_factory=lambda aid: FakeEngine(store))
        assert sup.notify_cookie("arch-1") is False
        sup.ensure("arch-1")
        assert sup.notify_cookie("arch-1") is True

    def test_snapshot_all_reports_every_runner(self):
        store = FakeStore()
        sup = RunnerSupervisor(store,
                               engine_factory=lambda aid: FakeEngine(store))
        sup.ensure("arch-1")
        sup.ensure("arch-2")
        snaps = sup.snapshot_all()
        assert {s["archive_id"] for s in snaps} == {"arch-1", "arch-2"}
        assert all("alive" in s and "bursts" in s for s in snaps)

    def test_stop_all_is_safe_with_no_runners(self):
        RunnerSupervisor(FakeStore(), engine_factory=lambda aid: None).stop_all()


# ----------------------------------------------------------------- snapshot
class TestSnapshot:
    def test_snapshot_shape(self):
        store = FakeStore()
        runner = make_runner(store, FakeEngine(store))
        snap = runner.snapshot()
        for key in ("archive_id", "alive", "status", "bursts", "cookie_waits",
                    "self_heals", "last_error", "started_at"):
            assert key in snap, key
        assert snap["archive_id"] == "arch-1"
        assert snap["status"] == JobStatus.READY.value

    def test_snapshot_survives_a_missing_job(self):
        store = FakeStore(job=False)
        runner = make_runner(store, FakeEngine(store))
        assert runner.snapshot()["status"] is None
