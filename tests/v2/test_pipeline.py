"""End-to-end pipeline integration test.

Proves the whole v2 flow with the extension's EXACT capture payload shape:

    capture (POST /api/v2/capture) -> plan -> engine burst (fake transport)
    -> on-disk verify -> budget view

All network-free: the only "Google" contact is a fake transport that feeds a
real ZIP. This is the test the user's small export will exercise for real.
"""
from __future__ import annotations

import os
import sqlite3
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from takeout2.api import create_app
from takeout2.contracts import PartStatus, ReasonCode
from takeout2.engine import BurstEngine
from takeout2.ledger import AttemptLedger
from takeout2.state import JobStore

ARCHIVE = "abc123"


@pytest.fixture
def real_zip(tmp_path):
    """A real, structurally-valid ZIP to stand in for a downloaded part."""
    path = tmp_path / "part.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data.bin", os.urandom(1 << 20))
    return str(path), os.path.getsize(path)


def extension_payload(filename, size, dl_count=0, label_source="SCRAPED_EMAIL"):
    """The exact shape helpers/content.js capturePayload() emits."""
    return {
        "archive_id": ARCHIVE,
        "user": "1005482974000",
        "authuser": "0",
        "parts_expected": 1,
        "uris": {filename: f"https://takeout-download.usercontent.google.com/download/{filename}?j={ARCHIVE}&i=0"},
        "sizes": {filename: size},
        "filenames": [filename],
        "dl_counts": {filename: dl_count},
        "account": {
            "email": "braincreation@gmail.com",
            "label": "braincreation",
            "label_source": label_source,
        },
        "export_ts_raw": "20260616T040104Z",
        "scrape_report": [{"field": "dl_counts", "source": "english-counter", "ok": True}],
        "locale_warning": False,
        "captured_at": int(time.time() * 1000),
    }


@pytest.fixture
def ctx(tmp_path):
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    store = JobStore(sqlite3.connect(":memory:", check_same_thread=False))
    ledger = AttemptLedger(sqlite3.connect(":memory:", check_same_thread=False),
                           budget=5, reserve=1)
    from fastapi import FastAPI
    app = create_app(store=store, ledger=ledger, capture_token="cap")
    root = FastAPI()
    root.mount("/api/v2", app)      # mirror production mount
    client = TestClient(root)
    return store, ledger, client, str(parts_dir)


class FakeCookie:
    def fresh(self):
        from takeout2.cookie import CookieState
        return CookieState(header="SID=AAA", pulled_at=time.monotonic(),
                           n_cookies=1)


def test_full_pipeline_capture_to_budget(ctx, real_zip):
    store, ledger, client, parts_dir = ctx
    zpath, zsize = real_zip
    filename = "takeout-20260616T040104Z-9-000.zip"

    # 1. Capture: the extension's payload, Google's counter says 0 used.
    r = client.post("/api/v2/capture", json=extension_payload(filename, zsize),
                    headers={"X-Capture-Token": "cap"})
    assert r.json()["ok"] and r.json()["parts"] == 1

    # 2. Burst: fake transport "downloads" the real zip into parts/.
    class FakeFetch:
        def __init__(self):
            self.calls = 0

        def __call__(self, part, have, cookie, write):
            self.calls += 1
            with open(zpath, "rb") as fh:
                write(fh.read(), 10)
            return ReasonCode.OK_COMPLETE, zsize

    fetch = FakeFetch()
    engine = BurstEngine(store, ledger, FakeCookie(), parts_dir, fetch=fetch)
    result = engine.run_burst(ARCHIVE)

    assert result.completed_ok == 1
    assert result.attempts_spent == 1

    # 3. On-disk verify happened; part is DONE + STRUCT_OK.
    part = store.get_part(ARCHIVE, 0)
    assert part.status is PartStatus.DONE
    assert part.verify_state.value == "STRUCT_OK"

    # 4. Budget: 1 local attempt spent, Google still says 0, 4 left.
    b = ledger.budget_for(ARCHIVE, 0)
    assert b.local_used == 1
    assert b.remaining == 4

    # 5. The API's job + budget views reflect it.
    job = client.get(f"/api/v2/jobs/{ARCHIVE}").json()
    assert job["parts_done"] == 1
    budget = client.get(f"/api/v2/jobs/{ARCHIVE}/budget").json()
    assert budget["parts"][0]["local_used"] == 1


def test_dl_count_reaches_budget_ground_truth(ctx, real_zip):
    """Google's counter (scraped) outranks our local estimate."""
    store, ledger, client, _ = ctx
    zpath, zsize = real_zip
    filename = "takeout-20260616T040104Z-9-000.zip"

    # Google says 4 already used (e.g. browser-native downloads we never saw).
    client.post("/api/v2/capture",
                json=extension_payload(filename, zsize, dl_count=4),
                headers={"X-Capture-Token": "cap"})

    b = ledger.budget_for(ARCHIVE, 0)
    assert b.remote_used == 4
    assert b.effective_used == 4
    assert b.remaining == 1
    assert b.exhausted          # only the reserve remains


def test_identity_flows_from_capture(ctx, real_zip):
    """The user's ask: account + export date parsed from the capture."""
    store, _, client, _ = ctx
    zpath, zsize = real_zip
    filename = "takeout-20260616T040104Z-9-000.zip"
    client.post("/api/v2/capture", json=extension_payload(filename, zsize),
                headers={"X-Capture-Token": "cap"})
    job = store.get_job(ARCHIVE)
    assert job.account_label == "braincreation"
    assert job.export_ts == "2026-06-16-04-01-04"   # from the filename
    assert job.export_raw == "20260616T040104Z"
