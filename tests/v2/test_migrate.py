"""Tests for takeout2.migrate — the v1 -> state.db adoption path.

The fixtures are written by the REAL v1 classes (manager.jobs.Job,
manager.manifest.Manifest), so the field names on disk are exactly what v1
produced — nothing here guesses at the v1 shape.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import manager.jobs as J
from manager.manifest import Manifest
from takeout2.contracts import LabelSource, PartStatus, VerifyState
from takeout2.ledger import AttemptLedger
from takeout2.migrate import inspect_v1, migrate_from_v1
from takeout2.state import JobStore

EXPORT_TS = "20260616T040104Z"
EXPORT_FOLDER = "2026-06-16-04-01-04"
ARCHIVE = "abc12345-def0-1234-abcd-1234567890ab"  # hex id — matches [a-f0-9-]
GAIA = "1005482974000"
EMAIL = "braincreation@gmail.com"

FN = {
    0: "takeout-20260616T040104Z-1-001.zip",
    1: "takeout-20260616T040104Z-1-002.zip",
    2: "takeout-20260616T040104Z-1-003.zip",
    3: "takeout-20260616T040104Z-1-004.zip",
}

PARTIAL_SIZE, PARTIAL_DONE = 100_000, 40_960


def _url(i: int, j_param: bool) -> str:
    j = f"j={ARCHIVE}&" if j_param else ""
    return (f"https://takeout-download.usercontent.google.com/download/{FN[i]}"
            f"?{j}i={i}&user={GAIA}&authuser=0")


def _build_v1(tmp_path: Path, *, with_missing: bool = True,
              archive_id: str | None = ARCHIVE, j_param: bool = True,
              attempts_part: int | None = None,
              dl_count_part: int | None = None):
    """Write a realistic v1 job dir; returns (job_root, takeout_root, zip_size).

    Layout mirrors v1's derivation: takeout_root/<label>/<export_ts>/.
    """
    takeout_root = tmp_path / "google-takeout"
    job_root = takeout_root / "braincreation" / EXPORT_FOLDER
    job_root.mkdir(parents=True)

    meta = {
        "email": EMAIL, "label": None, "user": GAIA, "authuser": "0",
        "account_label": "braincreation",
        "export_ts": EXPORT_FOLDER, "export_raw": EXPORT_TS,
        "captured_at": "2026-06-16T15:45:00.000Z",
    }
    if archive_id:
        meta["archive_id"] = archive_id

    job = J.Job(job_id="20260616T040104Z-braincreation", workflow="braincreation",
                output_dir=job_root, parallel=4, max_exports=500, meta=meta)

    # Part 0: DONE + zip_valid, real zip on disk.
    z0 = job_root / FN[0]
    with zipfile.ZipFile(z0, "w") as zf:
        zf.writestr("README.txt", "hello takeout")
    zip_size = z0.stat().st_size
    job.update_part(0, filename=FN[0], url=_url(0, j_param), size=zip_size,
                    done=zip_size, status="active")
    job.update_part(0, status="done", zip_valid=True)

    # Part 1: DONE + zip_valid; its file is absent unless with_missing=False.
    job.update_part(1, filename=FN[1], url=_url(1, j_param), size=zip_size,
                    done=zip_size, status="active")
    job.update_part(1, status="done", zip_valid=True)
    if not with_missing:
        with zipfile.ZipFile(job_root / FN[1], "w") as zf:
            zf.writestr("README.txt", "hello takeout part 2")

    # Part 2: PARTIAL — interrupted mid-stream, bytes on disk.
    (job_root / FN[2]).write_bytes(b"\x00" * PARTIAL_DONE)
    job.update_part(2, filename=FN[2], url=_url(2, j_param), size=PARTIAL_SIZE,
                    done=PARTIAL_DONE, status="active")
    if attempts_part == 2:
        job.update_part(2, attempts=2)
    if dl_count_part == 2:
        job.update_part(2, dl_count=3)

    # Part 3: queued, nothing on disk yet.
    job.update_part(3, filename=FN[3], url=_url(3, j_param), size=PARTIAL_SIZE,
                    done=0, status="queued")

    job.set_status(J.DOWNLOADING)
    job.persist()

    mf = Manifest(job_root)
    exports = [SimpleNamespace(filename=FN[i], size=PARTIAL_SIZE) for i in range(4)]
    exports[0].size = exports[1].size = zip_size
    mf.set_header(job, exports)
    for i in range(4):
        mf.record_part(job.parts[i])
    mf.finalize(job, completed=False)

    return job_root, takeout_root, zip_size


def _v1_bytes(job_root: Path) -> dict:
    return {name: (job_root / name).read_bytes()
            for name in (".manager_state.json", "manifest.json")}


def _file_store(tmp_path: Path) -> JobStore:
    return JobStore.open(str(tmp_path / "state.db"))


def _memory_store() -> JobStore:
    return JobStore(sqlite3.connect(":memory:", check_same_thread=False))


# --------------------------------------------------------------------------
# inspect_v1
# --------------------------------------------------------------------------
class TestInspect:
    def test_valid_fixture_clean_report(self, tmp_path):
        job_root, base, zip_size = _build_v1(tmp_path, with_missing=False)
        reports = inspect_v1(str(base))
        assert len(reports) == 1
        r = reports[0]
        assert r["archive_id"] == ARCHIVE
        assert r["account_label"] == "braincreation"
        assert r["export_ts"] == EXPORT_FOLDER
        assert r["status"] == "downloading"
        assert r["parts_total"] == 4
        assert r["parts_done"] == 2
        zip1_size = (job_root / FN[1]).stat().st_size
        assert r["bytes_done"] == zip_size + zip1_size
        assert r["errors"] == []

    def test_missing_done_file_flagged(self, tmp_path):
        job_root, base, zip_size = _build_v1(tmp_path, with_missing=True)
        reports = inspect_v1(str(base))
        assert len(reports) == 1
        r = reports[0]
        assert r["parts_total"] == 4
        assert r["parts_done"] == 1           # part 1 refused, stays PENDING
        assert r["bytes_done"] == zip_size
        assert len(r["errors"]) == 1
        assert "DONE" in r["errors"][0] and FN[1] in r["errors"][0]

    def test_empty_or_missing_dir_yields_no_jobs(self, tmp_path):
        assert inspect_v1(str(tmp_path / "nope")) == []
        (tmp_path / "empty").mkdir()
        assert inspect_v1(str(tmp_path / "empty")) == []

    def test_unresolvable_archive_id_reported(self, tmp_path):
        _, base, _ = _build_v1(tmp_path, archive_id=None, j_param=False)
        reports = inspect_v1(str(base))
        assert len(reports) == 1
        r = reports[0]
        assert r["archive_id"] is None
        assert any("archive_id" in e for e in r["errors"])


# --------------------------------------------------------------------------
# migrate_from_v1 -- apply
# --------------------------------------------------------------------------
class TestApply:
    def test_seeds_state_db(self, tmp_path):
        job_root, base, zip_size = _build_v1(tmp_path)
        v1_before = _v1_bytes(job_root)
        store = _file_store(tmp_path)

        res = migrate_from_v1(str(base), store, apply=True)
        assert res["jobs_found"] == 1
        assert res["jobs_created"] == 1
        assert res["parts_seeded"] == 4
        assert res["parts_done"] == 1
        assert res["attempts_seeded"] == 0
        assert len(res["errors"]) == 1          # the missing DONE file

        job = store.get_job(ARCHIVE)
        assert job is not None
        assert job.account_label == "braincreation"
        assert job.label_source is LabelSource.SCRAPED_EMAIL
        assert job.account_email == EMAIL
        assert job.gaia_user == GAIA
        assert job.authuser == "0"
        assert job.export_ts == EXPORT_FOLDER
        assert job.export_raw == EXPORT_TS
        assert job.output_dir == str(job_root)
        assert job.parts_expected == 4

        p0 = store.get_part(ARCHIVE, 0)
        assert p0.status is PartStatus.DONE
        assert p0.verify_state is VerifyState.STRUCT_OK
        assert p0.size_on_disk == zip_size
        assert p0.size_expected == zip_size
        assert p0.filename == FN[0]

        p1 = store.get_part(ARCHIVE, 1)         # done in v1 but file missing
        assert p1.status is PartStatus.PENDING
        assert p1.verify_state is VerifyState.UNVERIFIED
        assert p1.size_on_disk == 0

        p2 = store.get_part(ARCHIVE, 2)         # partial bytes on disk
        assert p2.status is PartStatus.PARTIAL
        assert p2.verify_state is VerifyState.UNVERIFIED
        assert p2.size_on_disk == PARTIAL_DONE
        assert p2.size_expected == PARTIAL_SIZE

        p3 = store.get_part(ARCHIVE, 3)         # nothing on disk
        assert p3.status is PartStatus.PENDING

        kinds = [e.kind for e in store.events_since(0)]
        assert "migration_applied" in kinds

        for name, before in v1_before.items():  # v1 files untouched
            assert (job_root / name).read_bytes() == before

    def test_apply_twice_is_idempotent(self, tmp_path):
        _, base, _ = _build_v1(tmp_path)
        store = _file_store(tmp_path)

        first = migrate_from_v1(str(base), store, apply=True)
        parts_before = [(p.idx, p.status, p.verify_state, p.size_on_disk,
                         p.attempts_used) for p in store.list_parts(ARCHIVE)]
        second = migrate_from_v1(str(base), store, apply=True)

        assert second["jobs_found"] == first["jobs_found"] == 1
        assert second["jobs_created"] == 0          # not recreated
        assert second["parts_seeded"] == 4
        assert len(store.list_jobs()) == 1          # no duplicate job
        assert len(store.list_parts(ARCHIVE)) == 4  # no duplicate parts
        parts_after = [(p.idx, p.status, p.verify_state, p.size_on_disk,
                        p.attempts_used) for p in store.list_parts(ARCHIVE)]
        assert parts_after == parts_before

    def test_archive_id_from_j_param(self, tmp_path):
        _, base, _ = _build_v1(tmp_path, archive_id=None, j_param=True)
        store = _file_store(tmp_path)
        res = migrate_from_v1(str(base), store, apply=True)
        assert res["jobs_found"] == 1
        assert res["jobs_created"] == 1
        assert store.get_job(ARCHIVE) is not None

    def test_unresolvable_job_skipped(self, tmp_path):
        _, base, _ = _build_v1(tmp_path, archive_id=None, j_param=False)
        store = _file_store(tmp_path)
        res = migrate_from_v1(str(base), store, apply=True)
        assert res["jobs_found"] == 1
        assert res["jobs_created"] == 0
        assert res["parts_seeded"] == 0
        assert store.list_jobs() == []
        assert any("archive_id" in e for e in res["errors"])

    def test_attempts_and_remote_count_seeded(self, tmp_path):
        _, base, _ = _build_v1(tmp_path, attempts_part=2, dl_count_part=2)
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        store = JobStore(conn)
        ledger = AttemptLedger(conn)

        res = migrate_from_v1(str(base), store, ledger=ledger, apply=True)
        assert res["attempts_seeded"] == 2
        assert store.get_part(ARCHIVE, 2).attempts_used == 2
        assert ledger.remote_used(ARCHIVE, 2) == 3  # Google's counter seeded


# --------------------------------------------------------------------------
# migrate_from_v1 -- dry run
# --------------------------------------------------------------------------
class TestDryRun:
    def test_zero_writes(self, tmp_path):
        job_root, base, _ = _build_v1(tmp_path)
        state_path, manifest_path = job_root / ".manager_state.json", job_root / "manifest.json"
        before_mtime = {p: p.stat().st_mtime_ns for p in (state_path, manifest_path)}
        before_bytes = _v1_bytes(job_root)
        store = _memory_store()

        res = migrate_from_v1(str(base), store, apply=False)
        assert res["jobs_found"] == 1
        assert res["jobs_created"] == 0
        assert res["parts_seeded"] == 0
        assert res["attempts_seeded"] == 0
        assert res["parts_done"] == 1
        assert len(res["errors"]) == 1
        assert store.latest_seq() == 0              # no events
        assert store.list_jobs() == []              # no jobs
        assert list(base.rglob("state.db")) == []   # no db file created

        for name, before in before_bytes.items():
            assert (job_root / name).read_bytes() == before
        for p, mtime in before_mtime.items():
            assert p.stat().st_mtime_ns == mtime   # files untouched on disk
