"""Tests for takeout2.state — the JobStore.

The regression tests encode the exact v1 failures documented in
docs/webgui/14-resume-cookies-multiaccount.md.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from takeout2.contracts import (AccountIdentity, IdentityRecord, JobStatus,
                                LabelSource, PartPlan, PartStatus, VerifyState)
from takeout2.state import JobStore

ARCHIVE = "j-abc123"


def identity(label_source=LabelSource.GAIA_FALLBACK, email=None, label=None,
             parts=62, archive_id=ARCHIVE) -> IdentityRecord:
    return IdentityRecord(
        archive_id=archive_id,
        export_raw="20260616T040104Z",
        account=AccountIdentity(gaia_user="1005482974000", authuser="0",
                                email=email, label=label,
                                label_source=label_source),
        parts_expected=parts,
    )


@pytest.fixture
def store():
    return JobStore(sqlite3.connect(":memory:", check_same_thread=False))


class TestJobLifecycle:
    def test_create_and_fetch(self, store):
        store.upsert_job(identity(), output_dir="/opt/a/2026-06-16-04-01-04")
        job = store.get_job(ARCHIVE)
        assert job.status is JobStatus.DISCOVERING
        assert job.parts_expected == 62
        assert job.account_label == "gaia-1005482974000"

    def test_upsert_is_idempotent(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_job(identity(), output_dir="/opt/a")
        assert len(store.list_jobs()) == 1

    def test_status_transition_emits_event(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)
        kinds = [e.kind for e in store.events_since(0)]
        assert "job_status" in kinds
        assert store.get_job(ARCHIVE).status is JobStatus.DOWNLOADING

    def test_terminal_status_sets_finished_at(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.set_job_status(ARCHIVE, JobStatus.COMPLETE)
        assert store.get_job(ARCHIVE).finished_at is not None


class TestIdentityUpgrade:
    """The 2.8 TB bug: a better label must not create a second job."""

    def test_better_label_upgrades_same_job(self, store, tmp_path):
        old_dir = tmp_path / "gaia-1005482974000" / "2026-06-16-04-01-04"
        old_dir.mkdir(parents=True)
        store.upsert_job(identity(), output_dir=str(old_dir))

        upgraded = store.maybe_upgrade_identity(
            identity(LabelSource.SCRAPED_EMAIL, email="braincreation@gmail.com"))

        assert upgraded
        assert len(store.list_jobs()) == 1, "must never create a second job"
        job = store.get_job(ARCHIVE)
        assert job.account_label == "braincreation"
        assert job.label_source is LabelSource.SCRAPED_EMAIL

    def test_upgrade_renames_directory_without_moving_bytes(self, store, tmp_path):
        old_dir = tmp_path / "gaia-1005482974000" / "2026-06-16-04-01-04"
        old_dir.mkdir(parents=True)
        (old_dir / "part-000.zip").write_bytes(b"PK\x03\x04payload")
        store.upsert_job(identity(), output_dir=str(old_dir))

        store.maybe_upgrade_identity(
            identity(LabelSource.SCRAPED_EMAIL, email="braincreation@gmail.com"))

        new_dir = tmp_path / "braincreation" / "2026-06-16-04-01-04"
        assert new_dir.is_dir()
        assert (new_dir / "part-000.zip").read_bytes() == b"PK\x03\x04payload"
        assert not old_dir.exists()
        assert store.get_job(ARCHIVE).output_dir == str(new_dir)

    def test_worse_label_is_ignored(self, store, tmp_path):
        d = tmp_path / "braincreation" / "ts"
        d.mkdir(parents=True)
        store.upsert_job(identity(LabelSource.SCRAPED_EMAIL,
                                  email="braincreation@gmail.com"),
                         output_dir=str(d))
        assert not store.maybe_upgrade_identity(identity(LabelSource.GAIA_FALLBACK))
        assert store.get_job(ARCHIVE).account_label == "braincreation"

    def test_equal_provenance_is_not_an_upgrade(self, store, tmp_path):
        d = tmp_path / "a" / "ts"; d.mkdir(parents=True)
        store.upsert_job(identity(LabelSource.SCRAPED_EMAIL, email="a@b.com"),
                         output_dir=str(d))
        assert not store.maybe_upgrade_identity(
            identity(LabelSource.SCRAPED_EMAIL, email="a@b.com"))

    def test_upsert_with_better_label_upgrades_instead_of_duplicating(self, store, tmp_path):
        d = tmp_path / "gaia-1005482974000" / "ts"; d.mkdir(parents=True)
        store.upsert_job(identity(), output_dir=str(d))
        store.upsert_job(identity(LabelSource.SCRAPED_EMAIL,
                                  email="braincreation@gmail.com"),
                         output_dir=str(d))
        assert len(store.list_jobs()) == 1
        assert store.get_job(ARCHIVE).account_label == "braincreation"

    def test_upgrade_emits_auditable_event(self, store, tmp_path):
        d = tmp_path / "gaia-1005482974000" / "ts"; d.mkdir(parents=True)
        store.upsert_job(identity(), output_dir=str(d))
        store.maybe_upgrade_identity(
            identity(LabelSource.SCRAPED_EMAIL, email="braincreation@gmail.com"))
        ev = [e for e in store.events_since(0) if e.kind == "identity_upgraded"]
        assert ev and ev[0].data["from_label"] == "gaia-1005482974000"
        assert ev[0].data["to_label"] == "braincreation"


class TestParts:
    def test_seed_and_list(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        plans = [PartPlan(idx=i, filename=f"p{i:03d}.zip", size_expected=10 << 30)
                 for i in range(62)]
        assert store.upsert_parts(ARCHIVE, plans) == 62
        assert len(store.list_parts(ARCHIVE)) == 62

    def test_reseeding_preserves_progress(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_parts(ARCHIVE, [PartPlan(idx=0, filename="p0.zip")])
        store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=999)
        store.upsert_parts(ARCHIVE, [PartPlan(idx=0, filename="p0.zip")])
        part = store.get_part(ARCHIVE, 0)
        assert part.status is PartStatus.DONE and part.size_on_disk == 999

    def test_filter_by_status(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_parts(ARCHIVE, [PartPlan(idx=i) for i in range(5)])
        store.update_part(ARCHIVE, 2, status=PartStatus.DONE)
        done = store.list_parts(ARCHIVE, status=PartStatus.DONE)
        assert [p.idx for p in done] == [2]

    def test_remaining_bytes(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_parts(ARCHIVE, [PartPlan(idx=0, size_expected=1000)])
        store.update_part(ARCHIVE, 0, size_on_disk=400)
        assert store.get_part(ARCHIVE, 0).remaining_bytes == 600

    def test_quiet_update_suppresses_event_spam(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_parts(ARCHIVE, [PartPlan(idx=0)])
        before = store.latest_seq()
        for n in range(50):
            store.update_part(ARCHIVE, 0, size_on_disk=n, quiet=True)
        assert store.latest_seq() == before


class TestAggregates:
    def test_totals_are_computed_in_sql(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_parts(ARCHIVE, [PartPlan(idx=i, size_expected=100)
                                     for i in range(10)])
        for i in range(4):
            store.update_part(ARCHIVE, i, status=PartStatus.DONE, size_on_disk=100)
        totals = store.job_totals(ARCHIVE)
        assert totals["parts_total"] == 10
        assert totals["parts_done"] == 4
        assert totals["bytes_done"] == 400
        assert totals["bytes_total"] == 1000


class TestEventCursor:
    def test_events_since_is_monotonic_and_resumable(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        for i in range(10):
            store.emit("tick", ARCHIVE, n=i)
        first = store.events_since(0, limit=5)
        assert [e.data["n"] for e in first[-4:]] == [0, 1, 2, 3]

        # A client that disconnected at `cursor` misses nothing on reconnect.
        cursor = first[-1].seq
        rest = store.events_since(cursor)
        assert all(e.seq > cursor for e in rest)
        assert [e.data["n"] for e in rest if e.kind == "tick"] == [4, 5, 6, 7, 8, 9]

    def test_filter_by_archive(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_job(identity(archive_id="j-other"), output_dir="/opt/b")
        store.emit("tick", ARCHIVE)
        store.emit("tick", "j-other")
        assert all(e.archive_id == ARCHIVE
                   for e in store.events_since(0, archive_id=ARCHIVE))

    def test_sse_serialization_carries_id(self, store):
        seq = store.emit("part_update", ARCHIVE, idx=3)
        sse = store.events_since(seq - 1)[0].to_sse()
        assert sse.startswith(f"id: {seq}")
        assert "event: part_update" in sse


class TestRecovery:
    def test_restart_parks_inflight_jobs_on_cookie(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)
        assert store.recover() == [ARCHIVE]
        assert store.get_job(ARCHIVE).status is JobStatus.NEEDS_COOKIE

    def test_active_parts_become_resumable_not_failed(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.upsert_parts(ARCHIVE, [PartPlan(idx=0)])
        store.update_part(ARCHIVE, 0, status=PartStatus.ACTIVE, size_on_disk=5 << 30)
        store.recover()
        part = store.get_part(ARCHIVE, 0)
        assert part.status is PartStatus.PARTIAL
        assert part.size_on_disk == 5 << 30, "bytes on disk must be preserved"

    def test_complete_jobs_are_untouched(self, store):
        store.upsert_job(identity(), output_dir="/opt/a")
        store.set_job_status(ARCHIVE, JobStatus.COMPLETE)
        assert store.recover() == []
        assert store.get_job(ARCHIVE).status is JobStatus.COMPLETE

    def test_state_survives_reopen(self, tmp_path):
        db = str(tmp_path / "state.db")
        s1 = JobStore.open(db)
        s1.upsert_job(identity(), output_dir="/opt/a")
        s1.upsert_parts(ARCHIVE, [PartPlan(idx=0, size_expected=42)])

        s2 = JobStore.open(db)
        assert s2.get_job(ARCHIVE) is not None
        assert s2.get_part(ARCHIVE, 0).size_expected == 42
