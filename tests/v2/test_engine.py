"""Tests for takeout2.engine — the cookie-window burst scheduler.

Network-free: the engine is given a fake transport via ``fetch`` and a fake
cookie source, so the burst logic is exercised without touching Google.
"""
from __future__ import annotations

import os
import sqlite3
import zipfile

import pytest

from takeout2.classify import ReasonCode
from takeout2.contracts import CostClass, PartStatus, JobStatus
from takeout2.engine import BurstEngine, EngineConfig
from takeout2.ledger import AttemptLedger
from takeout2.state import JobStore

ARCHIVE = "j-abc123"


class FakeCookieSource:
    def __init__(self, header="SID=AAA; HSID=BBB"):
        self.header = header
        self.calls = 0

    def fresh(self):
        self.calls += 1
        from takeout2.cookie import CookieState
        import time
        return CookieState(header=self.header, pulled_at=time.monotonic(),
                           n_cookies=2)


class EmptyStore:
    def list_parts(self, archive_id):
        return []


class FakeFetch:
    """Configurable fake transport recording what the engine asked for."""

    def __init__(self, outcome=ReasonCode.OK_COMPLETE, first_bytes=b"PK\x03\x04\x14\x00\x00\x00"):
        self.outcome = outcome
        self.calls = []
        self.write_fn = None
        self._first_bytes = first_bytes
        self.zip_size = None
        # Build a real zip once so its size can drive size_expected.
        if outcome is ReasonCode.OK_COMPLETE:
            import tempfile
            payload = os.urandom(1 << 20) * 4   # ~4 MB incompressible
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
                with zipfile.ZipFile(f, "w") as zf:
                    zf.writestr("data.bin", payload)
                self._zip_path = f.name
            self.zip_size = os.path.getsize(self._zip_path)

    def __call__(self, part, have, cookie, write):
        self.calls.append((part.idx, have, cookie))
        self.write_fn = write
        if self.outcome is ReasonCode.OK_PARTIAL:
            for _ in range(3):
                write(b"\x00" * (1 << 20), 10)
            return ReasonCode.OK_PARTIAL, 3 << 20
        if self.outcome is ReasonCode.OK_COMPLETE:
            with open(self._zip_path, "rb") as fh:
                chunks = fh.read()
            write(chunks, 20)
            return ReasonCode.OK_COMPLETE, len(chunks)
        # auth failure etc.
        return self.outcome, 0


@pytest.fixture
def env(tmp_path):
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    store = JobStore(sqlite3.connect(":memory:", check_same_thread=False))
    ledger = AttemptLedger(sqlite3.connect(":memory:", check_same_thread=False),
                           budget=5, reserve=1)
    from takeout2.contracts import (AccountIdentity, IdentityRecord,
                                    LabelSource)
    store.upsert_job(
        IdentityRecord(archive_id=ARCHIVE, export_raw="20260616T040104Z",
                       account=AccountIdentity(gaia_user="1", authuser="0",
                                               label_source=LabelSource.GAIA_FALLBACK)),
        output_dir=str(tmp_path))
    return store, ledger, str(parts_dir)


def seed_parts(store, n=4, size=None):
    from takeout2.contracts import PartPlan
    if size is None:
        size = 10_000_000
    store.upsert_parts(ARCHIVE, [
        PartPlan(idx=i, filename=f"part-{i:03d}.zip", url=f"https://dl/{i}",
                 size_expected=size)
        for i in range(n)
    ])
    return store.list_parts(ARCHIVE)


class TestCanary:
    def test_dead_cookie_spends_one_attempt_not_n(self, env, tmp_path):
        store, ledger, parts_dir = env
        parts = seed_parts(store, n=4)

        class DeadFetch(FakeFetch):
            def __call__(self, part, have, cookie, write):
                self.calls.append((part.idx, have, cookie))
                return ReasonCode.AUTH_REDIRECT, 0

        engine = BurstEngine(store, ledger, FakeCookieSource(),
                             parts_dir, fetch=DeadFetch())
        result = engine.run_burst(ARCHIVE)

        # Only ONE stream opened (the canary), everything else untouched.
        assert result.canary_passed is False
        assert len(engine.fetch.calls) == 1 if hasattr(engine.fetch, "calls") else True
        # The job parked on cookie.
        assert store.get_job(ARCHIVE).status is not JobStatus.DOWNLOADING

    def test_good_cookie_opens_all_streams(self, env):
        store, ledger, parts_dir = env
        fetch = FakeFetch(outcome=ReasonCode.OK_COMPLETE)
        seed_parts(store, n=3, size=fetch.zip_size)
        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=fetch)
        result = engine.run_burst(ARCHIVE)
        assert result.canary_passed is True
        assert result.completed_ok == 3
        assert len(fetch.calls) == 3


class TestReservationDiscipline:
    def test_every_stream_is_ledger_reserved(self, env):
        store, ledger, parts_dir = env
        fetch = FakeFetch(outcome=ReasonCode.OK_COMPLETE)
        seed_parts(store, n=2, size=fetch.zip_size)
        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=fetch)
        engine.run_burst(ARCHIVE)
        # 2 PAYLOAD reservations charged.
        totals = ledger.archive_totals(ARCHIVE)
        assert totals["attempts_charged"] == 2

    def test_budget_exhausted_part_is_parked_not_fetched(self, env):
        store, ledger, parts_dir = env
        seed_parts(store, n=2)
        # Burn part 0's budget via Google's counter.
        ledger.observe_remote(ARCHIVE, 0, 5)
        fetch = FakeFetch(outcome=ReasonCode.OK_COMPLETE)
        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=fetch)
        result = engine.run_burst(ARCHIVE)
        assert result.budget_exhausted == 1
        assert store.get_part(ARCHIVE, 0).status is PartStatus.BUDGET_EXHAUSTED
        # Only part 1 actually fetched.
        assert [c[0] for c in fetch.calls] == [1]


class TestAuthFailureHandling:
    def test_auth_failure_parks_job_on_cookie(self, env):
        store, ledger, parts_dir = env
        seed_parts(store, n=1)

        class AuthFetch(FakeFetch):
            def __call__(self, part, have, cookie, write):
                return ReasonCode.AUTH_REDIRECT, 0

        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=AuthFetch())
        engine.run_burst(ARCHIVE)
        assert store.get_job(ARCHIVE).status is JobStatus.NEEDS_COOKIE

    def test_partial_keeps_bytes_resumable(self, env):
        store, ledger, parts_dir = env
        seed_parts(store, n=1)
        fetch = FakeFetch(outcome=ReasonCode.OK_PARTIAL)
        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=fetch)
        result = engine.run_burst(ARCHIVE)
        assert result.completed_partial == 1
        assert store.get_part(ARCHIVE, 0).status is PartStatus.PARTIAL


class TestCompletePath:
    def test_complete_part_verified_and_done(self, env):
        store, ledger, parts_dir = env
        fetch = FakeFetch(outcome=ReasonCode.OK_COMPLETE)
        seed_parts(store, n=1, size=fetch.zip_size)
        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=fetch)
        result = engine.run_burst(ARCHIVE)
        assert result.completed_ok == 1
        part = store.get_part(ARCHIVE, 0)
        assert part.status is PartStatus.DONE
        assert part.verify_state.value in ("STRUCT_OK", "HASH_OK")

    def test_existing_done_part_does_not_spend_attempt(self, env):
        store, ledger, parts_dir = env
        parts = seed_parts(store, n=1, size=100)
        # Put a real valid zip on disk at the exact expected size.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            with zipfile.ZipFile(f, "w") as zf:
                zf.writestr("a", b"hello")
            path = f.name
        target = os.path.join(parts_dir, "part-000.zip")
        os.replace(path, target)
        # Match the expected size.
        expected = os.path.getsize(target)
        from takeout2.contracts import PartPlan
        store.upsert_parts(ARCHIVE, [PartPlan(idx=0, filename="part-000.zip",
                                              url="https://dl/0",
                                              size_expected=expected)])
        fetch = FakeFetch(outcome=ReasonCode.OK_COMPLETE)
        engine = BurstEngine(store, ledger, FakeCookieSource(), parts_dir,
                             fetch=fetch)
        result = engine.run_burst(ARCHIVE)
        assert result.completed_ok == 1
        assert len(fetch.calls) == 0          # no network at all
        assert ledger.archive_totals(ARCHIVE)["attempts_charged"] == 0
