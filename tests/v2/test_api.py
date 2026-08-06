"""Tests for takeout2.api — the v2 control plane.

The capture-sink tests encode the extension <-> backend contract: a payload
carrying Google's own dl_counts must seed the ledger with remote truth.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from takeout2.api import create_app
from takeout2.contracts import LabelSource, PartStatus
from takeout2.ledger import AttemptLedger
from takeout2.state import JobStore

ARCHIVE = "j-capture-001"


@pytest.fixture
def ctx():
    store = JobStore(sqlite3.connect(":memory:", check_same_thread=False))
    ledger = AttemptLedger(sqlite3.connect(":memory:", check_same_thread=False),
                           budget=5, reserve=1)
    app = create_app(store=store, ledger=ledger, api_token="api-secret",
                     capture_token="cap-secret")
    client = TestClient(app)
    return store, ledger, client


def make_payload(parts=3, dl_count=0, label_source="SCRAPED_EMAIL"):
    filenames = [f"takeout-20260616T040104Z-9-{i:03d}.zip" for i in range(parts)]
    return {
        "archive_id": ARCHIVE,
        "user": "1005482974000",
        "authuser": "0",
        "parts_expected": parts,
        "uris": {f: f"https://dl/{i}" for i, f in enumerate(filenames)},
        "sizes": {f: 10 << 30 for f in filenames},
        "filenames": filenames,
        "dl_counts": {f: dl_count for f in filenames},
        "account": {
            "email": "braincreation@gmail.com",
            "label": "braincreation",
            "label_source": label_source,
        },
        "export_ts_raw": "20260616T040104Z",
        "scrape_report": [{"field": "dl_counts", "source": "regex", "ok": True}],
        "locale_warning": False,
        "captured_at": "2026-06-16T15:45:00.000Z",
    }


class TestCaptureSink:
    def test_capture_creates_job_and_seeds_ledger(self, ctx):
        store, ledger, client = ctx
        r = client.post("/api/v2/capture", json=make_payload(parts=3, dl_count=4),
                        headers={"X-Capture-Token": "cap-secret"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["archive_id"] == ARCHIVE
        assert body["parts"] == 3

        job = store.get_job(ARCHIVE)
        assert job.account_label == "braincreation"
        assert job.label_source is LabelSource.SCRAPED_EMAIL
        assert job.parts_expected == 3

        # Google's counter seeded the ledger (remote truth).
        assert ledger.remote_used(ARCHIVE, 0) == 4
        b = ledger.budget_for(ARCHIVE, 0)
        assert b.effective_used == 4
        assert b.remaining == 1
        assert b.exhausted          # only the reserve remains

    def test_capture_rejects_missing_archive_id(self, ctx):
        _, _, client = ctx
        r = client.post("/api/v2/capture", json={"parts_expected": 3},
                        headers={"X-Capture-Token": "cap-secret"})
        assert r.status_code == 400

    def test_capture_requires_token_when_set(self, ctx):
        _, _, client = ctx
        r = client.post("/api/v2/capture", json=make_payload())
        assert r.status_code == 401
        r = client.post("/api/v2/capture", json=make_payload(),
                        headers={"X-Capture-Token": "wrong"})
        assert r.status_code == 401

    def test_capture_without_part_metadata_degrades(self, ctx):
        store, ledger, client = ctx
        payload = make_payload(parts=3, dl_count=0)
        del payload["parts_expected"]
        del payload["uris"]
        del payload["sizes"]
        payload["filenames"] = []
        r = client.post("/api/v2/capture", json=payload,
                        headers={"X-Capture-Token": "cap-secret"})
        assert r.status_code == 200
        # Job still created; planner will resolve the part list later.
        assert store.get_job(ARCHIVE) is not None

    def test_recapture_upgrades_identity_not_duplicates(self, ctx):
        store, ledger, client = ctx
        headers = {"X-Capture-Token": "cap-secret"}
        # First capture: only gaia fallback (label_source=GAIA_FALLBACK).
        payload = make_payload()
        payload["account"] = {"email": None, "label": None,
                              "label_source": "GAIA_FALLBACK"}
        client.post("/api/v2/capture", json=payload, headers=headers)
        assert len(store.list_jobs()) == 1
        assert store.get_job(ARCHIVE).label_source is LabelSource.GAIA_FALLBACK

        # Recapture with a better (scraped email) identity.
        client.post("/api/v2/capture", json=make_payload(), headers=headers)
        jobs = store.list_jobs()
        assert len(jobs) == 1, "recapture must never create a second job"
        assert jobs[0].label_source is LabelSource.SCRAPED_EMAIL


class TestJobsRoutes:
    def test_list_and_get(self, ctx):
        store, _, client = ctx
        client.post("/api/v2/capture", json=make_payload(parts=3),
                    headers={"X-Capture-Token": "cap-secret"})
        r = client.get("/api/v2/jobs")
        assert r.status_code == 200
        assert len(r.json()["jobs"]) == 1

        r = client.get(f"/api/v2/jobs/{ARCHIVE}")
        assert r.json()["status"] == "DISCOVERING"
        assert r.json()["parts_total"] == 3

        r = client.get(f"/api/v2/jobs/{ARCHIVE}/parts")
        assert len(r.json()["parts"]) == 3

    def test_get_missing_job_404(self, ctx):
        _, _, client = ctx
        assert client.get("/api/v2/jobs/nope").status_code == 404

    def test_budget_route_mirrors_ledger(self, ctx):
        store, ledger, client = ctx
        client.post("/api/v2/capture", json=make_payload(parts=2, dl_count=3),
                    headers={"X-Capture-Token": "cap-secret"})
        r = client.get(f"/api/v2/jobs/{ARCHIVE}/budget")
        body = r.json()
        assert body["attempt_budget"] == 5
        assert body["parts_at_risk"] == 2      # 5-3=2 <= reserve+1
        assert all(p["remote_used"] == 3 for p in body["parts"])


class TestControlRoutes:
    def test_pause_resume_cancel(self, ctx):
        store, _, client = ctx
        client.post("/api/v2/capture", json=make_payload(),
                    headers={"X-Capture-Token": "cap-secret"})
        h = {"X-Api-Token": "api-secret"}

        r = client.post(f"/api/v2/control/pause/{ARCHIVE}", headers=h)
        assert r.json()["ok"] and store.get_job(ARCHIVE).status.value == "PAUSED"

        r = client.post(f"/api/v2/control/resume/{ARCHIVE}", headers=h)
        assert store.get_job(ARCHIVE).status.value == "DOWNLOADING"

        r = client.post(f"/api/v2/control/cancel/{ARCHIVE}", headers=h)
        assert store.get_job(ARCHIVE).status.value == "FAILED"

    def test_control_requires_token(self, ctx):
        _, _, client = ctx
        client.post("/api/v2/capture", json=make_payload(),
                    headers={"X-Capture-Token": "cap-secret"})
        r = client.post(f"/api/v2/control/pause/{ARCHIVE}")
        assert r.status_code == 401

    def test_cancel_complete_rejected(self, ctx):
        store, _, client = ctx
        client.post("/api/v2/capture", json=make_payload(),
                    headers={"X-Capture-Token": "cap-secret"})
        from takeout2.contracts import JobStatus
        store.set_job_status(ARCHIVE, JobStatus.COMPLETE)
        r = client.post(f"/api/v2/control/cancel/{ARCHIVE}",
                        headers={"X-Api-Token": "api-secret"})
        assert r.status_code == 409


class TestSSE:
    def test_events_resume_losslessly(self, ctx):
        store, _, client = ctx
        client.post("/api/v2/capture", json=make_payload(),
                    headers={"X-Capture-Token": "cap-secret"})
        # Emit a few events after capture.
        for i in range(5):
            store.emit("tick", ARCHIVE, n=i)

        # First read: since=0, small timeout so the stream closes.
        r = client.get(f"/api/v2/jobs/{ARCHIVE}/events?since=0&timeout_s=0.2&heartbeat_s=0.1")
        first = r.text
        assert "id: " in first

        # Parse seqs from the first read.
        seqs = [int(l.split(":")[1]) for l in first.splitlines()
                if l.startswith("id: ")]
        assert len(seqs) >= 1
        last_seen = max(seqs)

        # Second read from the cursor must not duplicate.
        r2 = client.get(f"/api/v2/jobs/{ARCHIVE}/events?since={last_seen}"
                        "&timeout_s=0.2&heartbeat_s=0.1")
        seqs2 = [int(l.split(":")[1]) for l in r2.text.splitlines()
                 if l.startswith("id: ")]
        overlap = set(seqs) & set(seqs2)
        assert not overlap, "SSE resume duplicated an event"
        assert all(s > last_seen for s in seqs2)


class TestDoctor:
    def test_healthy_doctor(self, ctx):
        _, _, client = ctx
        r = client.get("/api/v2/doctor")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_at_risk_parts_flag_doctor(self, ctx):
        _, ledger, client = ctx
        client.post("/api/v2/capture", json=make_payload(dl_count=4),
                    headers={"X-Capture-Token": "cap-secret"})
        r = client.get("/api/v2/doctor")
        assert r.json()["ok"] is False
        assert r.json()["checks"]["budget"]["ok"] is False
