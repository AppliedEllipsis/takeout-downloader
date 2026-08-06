"""Autonomy tests: capture -> auto-start -> self-heal, through the real API.

These exercise the wiring that makes the tool self-driving, using the real
FastAPI app mounted exactly as production mounts it (/api/v2) but with a fake
supervisor so no thread, no browser and no Google attempt is involved.

The behaviours pinned here are the ones that would silently cost real money or
wedge a multi-day transfer if they regressed:

* a capture must START the download (that is the entire point of "click
  Download once and walk away")
* a capture for a job parked on a dead cookie must WAKE the existing runner,
  never spawn a second one (R1: two runners = two attempts per part)
* PAUSED and BUDGET_EXHAUSTED must survive automation (R6)
* a capture must still succeed even if autostart blows up — the payload is the
  time-sensitive thing we cannot afford to lose
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from takeout2.api import create_app
from takeout2.contracts import JobStatus
from takeout2.ledger import AttemptLedger
from takeout2.state import JobStore

ARCHIVE = "j-auto-001"


class FakeRunner:
    def __init__(self, archive_id, alive=False):
        self.archive_id = archive_id
        self.started = 0
        self.stopped = 0
        self.woken = 0
        self._alive = alive

    def start(self):
        self.started += 1
        self._alive = True

    def stop(self, **kw):
        self.stopped += 1
        self._alive = False

    def notify_cookie(self):
        self.woken += 1

    def is_alive(self):
        return self._alive

    def snapshot(self):
        return {"archive_id": self.archive_id, "alive": self._alive,
                "bursts": 0, "cookie_waits": 0, "self_heals": self.woken,
                "last_error": None, "started_at": None, "status": None}


class FakeSupervisor:
    def __init__(self, *, explode=False):
        self.runners = {}
        self.explode = explode

    def ensure(self, archive_id):
        if self.explode:
            raise RuntimeError("supervisor is broken")
        return self.runners.setdefault(archive_id, FakeRunner(archive_id))

    def get(self, archive_id):
        return self.runners.get(archive_id)

    def stop(self, archive_id, **kw):
        r = self.get(archive_id)
        if r is None:
            return False
        r.stop()
        return True

    def snapshot_all(self):
        return [r.snapshot() for r in self.runners.values()]


def build(supervisor=None):
    store = JobStore(sqlite3.connect(":memory:", check_same_thread=False))
    ledger = AttemptLedger(sqlite3.connect(":memory:", check_same_thread=False),
                           budget=5, reserve=1)
    app = create_app(store=store, ledger=ledger, api_token="api-secret",
                     capture_token="cap-secret", supervisor=supervisor)
    root = FastAPI()
    root.mount("/api/v2", app)
    return store, ledger, TestClient(root), supervisor


def payload(parts=2):
    names = [f"takeout-20260616T040104Z-9-{i:03d}.zip" for i in range(parts)]
    return {
        "archive_id": ARCHIVE, "user": "100548", "authuser": "0",
        "parts_expected": parts,
        "uris": {f: f"https://dl/{i}" for i, f in enumerate(names)},
        "sizes": {f: 10 << 30 for f in names},
        "filenames": names,
        "dl_counts": {f: 0 for f in names},
        "account": {"email": "a@b.com", "label": "acct",
                    "label_source": "SCRAPED_EMAIL"},
        "export_ts_raw": "20260616T040104Z",
    }


def post_capture(client, body=None, **params):
    return client.post("/api/v2/capture", json=body or payload(),
                       headers={"X-Capture-Token": "cap-secret"},
                       params=params or None)


# ----------------------------------------------------------------- autostart
class TestCaptureAutostart:
    def test_capture_starts_the_runner(self):
        """The whole promise: one click in Takeout and it downloads itself."""
        store, _l, client, sup = build(FakeSupervisor())
        r = post_capture(client)
        assert r.status_code == 200, r.text
        assert r.json().get("autostart") is True
        assert sup.get(ARCHIVE).started == 1

    def test_autostart_can_be_disabled_per_request(self):
        store, _l, client, sup = build(FakeSupervisor())
        r = post_capture(client, autostart=False)
        assert r.status_code == 200
        assert sup.get(ARCHIVE) is None, "must not have created a runner"

    def test_capture_works_with_no_supervisor(self):
        """Import-safe on hosts with no browser: capture still ingests."""
        _s, _l, client, _sup = build(None)
        assert post_capture(client).status_code == 200

    def test_capture_survives_a_broken_supervisor(self):
        """A capture is time-sensitive; never lose it to an autostart bug."""
        _s, _l, client, _sup = build(FakeSupervisor(explode=True))
        r = post_capture(client)
        assert r.status_code == 200, "capture must still be ingested"
        assert r.json().get("autostart") is False
        assert "error" in r.json().get("autostart_reason", "")


# ----------------------------------------------------------------- self-heal
class TestSelfHeal:
    def test_capture_on_a_live_runner_wakes_it_instead_of_restarting(self):
        """R1/R3: the dead-cookie self-heal path."""
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client)                      # creates + starts
        runner = sup.get(ARCHIVE)
        assert runner.started == 1

        post_capture(client)                      # the re-capture
        assert runner.woken == 1, "should wake the parked runner"
        assert runner.started == 1, "must NOT start a second runner"


# ----------------------------------------------------------------- guardrails
class TestOperatorDecisionsWin:
    @pytest.mark.parametrize("status,label", [
        (JobStatus.PAUSED, "PAUSED"),
        (JobStatus.BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED"),
    ])
    def test_automation_never_overrides_a_block(self, status, label):
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client, autostart=False)     # create the job only
        store.set_job_status(ARCHIVE, status)

        r = post_capture(client)
        assert r.json().get("autostart") is False
        assert label in r.json().get("autostart_reason", "")
        assert sup.get(ARCHIVE) is None

    def test_start_route_refuses_budget_exhausted(self):
        """R6: never auto-spend the last of Google's 5 attempts."""
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client, autostart=False)
        store.set_job_status(ARCHIVE, JobStatus.BUDGET_EXHAUSTED)
        r = client.post(f"/api/v2/jobs/{ARCHIVE}/start",
                        headers={"X-Api-Token": "api-secret"})
        assert r.status_code == 409
        assert "BUDGET_EXHAUSTED" in r.text

    def test_clear_budget_block_is_explicit_and_authenticated(self):
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client, autostart=False)
        store.set_job_status(ARCHIVE, JobStatus.BUDGET_EXHAUSTED)

        assert client.post(
            f"/api/v2/jobs/{ARCHIVE}/clear-budget-block").status_code == 401

        r = client.post(f"/api/v2/jobs/{ARCHIVE}/clear-budget-block",
                        headers={"X-Api-Token": "api-secret"})
        assert r.status_code == 200
        assert store.get_job(ARCHIVE).status is JobStatus.READY


# ----------------------------------------------------------------- control
class TestControlRoutes:
    def test_pause_stops_the_runner_then_records_state(self):
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client)
        r = client.post(f"/api/v2/jobs/{ARCHIVE}/pause",
                        headers={"X-Api-Token": "api-secret"})
        assert r.status_code == 200
        assert sup.get(ARCHIVE).stopped == 1
        assert store.get_job(ARCHIVE).status is JobStatus.PAUSED

    def test_resume_restarts_the_runner(self):
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client)
        client.post(f"/api/v2/jobs/{ARCHIVE}/pause",
                    headers={"X-Api-Token": "api-secret"})
        r = client.post(f"/api/v2/jobs/{ARCHIVE}/resume",
                        headers={"X-Api-Token": "api-secret"})
        assert r.status_code == 200
        assert sup.get(ARCHIVE).started >= 2

    def test_both_route_styles_work(self):
        """Legacy /control/{action}/{id} and REST /jobs/{id}/{action}."""
        store, _l, client, sup = build(FakeSupervisor())
        post_capture(client, autostart=False)
        for path in (f"/api/v2/control/pause/{ARCHIVE}",
                     f"/api/v2/jobs/{ARCHIVE}/pause"):
            assert client.post(
                path, headers={"X-Api-Token": "api-secret"}).status_code == 200

    def test_control_requires_the_api_token(self):
        _s, _l, client, _sup = build(FakeSupervisor())
        post_capture(client, autostart=False)
        assert client.post(f"/api/v2/jobs/{ARCHIVE}/pause").status_code == 401

    def test_control_404s_for_unknown_job(self):
        _s, _l, client, _sup = build(FakeSupervisor())
        r = client.post("/api/v2/jobs/nope/pause",
                        headers={"X-Api-Token": "api-secret"})
        assert r.status_code == 404


# ----------------------------------------------------------------- introspect
class TestRunnerIntrospection:
    def test_runners_list_without_supervisor(self):
        _s, _l, client, _sup = build(None)
        body = client.get("/api/v2/runners").json()
        assert body == {"runners": [], "supervisor": False}

    def test_runners_list_with_supervisor(self):
        _s, _l, client, sup = build(FakeSupervisor())
        post_capture(client)
        body = client.get("/api/v2/runners").json()
        assert body["supervisor"] is True
        assert body["runners"][0]["archive_id"] == ARCHIVE

    def test_per_job_runner_state(self):
        _s, _l, client, sup = build(FakeSupervisor())
        post_capture(client)
        body = client.get(f"/api/v2/jobs/{ARCHIVE}/runner").json()
        assert body["alive"] is True
        assert body["archive_id"] == ARCHIVE

    def test_per_job_runner_state_when_absent(self):
        _s, _l, client, sup = build(FakeSupervisor())
        body = client.get("/api/v2/jobs/unknown/runner").json()
        assert body["alive"] is False


# ----------------------------------------------------------------- CORS
class TestOverlayCors:
    """The on-page overlay lives on takeout.google.com and talks to 127.0.0.1.

    A content script's fetch() rides on the extension's host_permissions, but
    EventSource does NOT — SSE obeys ordinary CORS. Without these headers the
    overlay's live stream is blocked and it silently degrades to polling.
    """

    def test_takeout_origin_is_allowed(self):
        _s, _l, client, _sup = build(None)
        r = client.get("/api/v2/jobs",
                       headers={"Origin": "https://takeout.google.com"})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == \
            "https://takeout.google.com"

    def test_unknown_origin_gets_no_cors_header(self):
        """Scoped narrowly: not a blanket allow-all."""
        _s, _l, client, _sup = build(None)
        r = client.get("/api/v2/jobs",
                       headers={"Origin": "https://evil.example"})
        assert r.headers.get("access-control-allow-origin") is None

    def test_sse_endpoint_is_cors_enabled_for_the_overlay(self):
        store, _l, client, _sup = build(FakeSupervisor())
        post_capture(client, autostart=False)
        r = client.get(f"/api/v2/jobs/{ARCHIVE}/events",
                       params={"since": 0, "timeout_s": 0.05},
                       headers={"Origin": "https://takeout.google.com"})
        assert r.headers.get("access-control-allow-origin") == \
            "https://takeout.google.com"

    def test_credentials_are_not_allowed(self):
        """The capture token travels in an explicit header, never as a cookie."""
        _s, _l, client, _sup = build(None)
        r = client.get("/api/v2/jobs",
                       headers={"Origin": "https://takeout.google.com"})
        assert r.headers.get("access-control-allow-credentials") != "true"
