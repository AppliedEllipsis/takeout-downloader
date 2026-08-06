"""Tests for takeout2.cli — the operator command line.

Runs entirely against tmp_path SQLite files with the REAL JobStore and
AttemptLedger. Every test is network-free:

    * budget uses a tmp db seeded with parts + ledger attempts,
    * status reads the same db,
    * verify checks a real zip built with zipfile,
    * doctor points TK2_CDP_URL at a non-localhost host so the cookie source
      is not constructible and the CDP check is skipped (spec: "only if a
      cookie source is constructible"),
    * run --dry-run must print a plan and leave the ledger untouched,
    * migrate asserts the friendly ImportError message (takeout2.migrate does
      not exist yet).
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile

import pytest

from takeout2.cli import main
from takeout2.contracts import (AccountIdentity, CostClass, IdentityRecord,
                                JobStatus, LabelSource, PartPlan, PartStatus,
                                ReasonCode, VerifyState)
from takeout2.ledger import AttemptLedger
from takeout2.state import JobStore

ARCHIVE = "j=test-export-000000000000ab9f2"
BUDGET = 5
RESERVE = 1

PART_000 = "takeout-20260616T040104Z-9-000.zip"
PART_001 = "takeout-20260616T040104Z-9-001.zip"


def make_identity(label_source=LabelSource.SCRAPED_EMAIL,
                  email="braincreation@gmail.com", label="braincreation",
                  parts=2, archive_id=ARCHIVE) -> IdentityRecord:
    return IdentityRecord(
        archive_id=archive_id,
        export_raw="20260616T040104Z",
        account=AccountIdentity(gaia_user="1005482974000", authuser="0",
                                email=email, label=label,
                                label_source=label_source),
        parts_expected=parts,
    )


def seed_job(tmp_path, parts=2, budget=BUDGET):
    """Create a job with ``parts`` planned parts in a tmp storage root."""
    db = tmp_path / "state.db"
    store = JobStore.open(str(db))
    out_dir = tmp_path / "storage" / "braincreation" / "2026-06-16-04-01-04"
    store.upsert_job(make_identity(), output_dir=str(out_dir),
                     attempt_budget=budget)
    plans = [
        PartPlan(idx=0, filename=PART_000, size_expected=10 << 30),
        PartPlan(idx=1, filename=PART_001, size_expected=10 << 30),
    ]
    store.upsert_parts(ARCHIVE, plans[:parts])
    return db, store, out_dir


def run_cli(capsys, *argv):
    """Invoke the CLI in-process; return (exit_code, stdout, stderr)."""
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------
# budget — the normative shape from docs/v2/01 §9
# --------------------------------------------------------------------------
def test_budget_header_columns_and_part_row(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=10 << 30,
                      verify_state=VerifyState.STRUCT_OK)
    store.update_part(ARCHIVE, 1, status=PartStatus.PARTIAL,
                      size_on_disk=(3 << 30) + (2 << 28))
    ledger = AttemptLedger(sqlite3.connect(str(db)), budget=BUDGET,
                           reserve=RESERVE)
    # part 0: one spent, complete. part 1: four spent, resumable.
    ledger.reserve(ARCHIVE, 0).commit(ReasonCode.OK_COMPLETE,
                                      bytes_moved=10 << 30)
    for _ in range(4):
        ledger.reserve(ARCHIVE, 1).commit(ReasonCode.OK_PARTIAL, bytes_moved=0)
    # Google's own counter for part 1 agrees with our ledger.
    ledger.observe_remote(ARCHIVE, 1, 4)

    code, out, _ = run_cli(capsys, "budget", ARCHIVE, "--db", str(db))

    assert code == 0
    # The 01 §9 header/column tokens are all present.
    for token in ("part", "filename", "size", "on-disk", "local", "google",
                  "left", "state"):
        assert token in out, f"missing header token {token!r} in {out!r}"
    # A part row carries local + google + left as integers, e.g.
    # "     4       4     1  PARTIAL".
    partial_row = next(line for line in out.splitlines() if "PARTIAL" in line)
    assert re.search(r"\s\d+\s+\d+\s+\d+\s", partial_row), partial_row
    # Danger markers from 03 §2.2: part 1 is the "last attempt".
    assert "last attempt" in out
    assert "⚠" in out
    # Completed part reports its verified state.
    assert "STRUCT_OK" in out
    # Footer counts the at-risk parts.
    assert "parts at risk" in out


def test_budget_json_matches_fields(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path, parts=1)
    store.update_part(ARCHIVE, 0, size_on_disk=5 << 30)
    ledger = AttemptLedger(sqlite3.connect(str(db)), budget=BUDGET,
                           reserve=RESERVE)
    ledger.observe_remote(ARCHIVE, 0, 2)

    code, out, _ = run_cli(capsys, "budget", ARCHIVE, "--db", str(db), "--json")
    assert code == 0
    data = json.loads(out)
    assert data["archive_id"] == ARCHIVE
    assert data["parts_total"] == 1
    part = data["parts"][0]
    assert part["local_used"] == 0
    assert part["remote_used"] == 2
    assert part["left"] == BUDGET - 2
    assert "filename" in part and part["filename"] == PART_000


def test_budget_missing_job_is_friendly(tmp_path, capsys):
    db = tmp_path / "state.db"
    code, out, err = run_cli(capsys, "budget", "j=nope", "--db", str(db))
    assert code == 1
    assert "no job for archive" in out + err


# --------------------------------------------------------------------------
# status — one-shot snapshot
# --------------------------------------------------------------------------
def test_status_exits_zero_and_prints_summary(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=10 << 30)
    store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)

    code, out, err = run_cli(capsys, "status", "--db", str(db))

    assert code == 0
    assert "braincreation" in out
    assert "2026-06-16-04-01-04" in out
    assert "parts" in out
    assert "bytes" in out
    assert "SCRAPED_EMAIL" in out
    assert "HIGH" in out


def test_status_json_is_parseable(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=10 << 30)
    store.set_job_status(ARCHIVE, JobStatus.COMPLETE)

    code, out, _ = run_cli(capsys, "status", "--db", str(db), "--json")
    assert code == 0
    data = json.loads(out)
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert job["parts"]["total"] == 2
    assert job["parts"]["done"] == 1
    assert job["bytes_done"] == 10 << 30
    assert job["status"] == "COMPLETE"
    assert job["budget"]["parts_at_risk"] == 0


def test_status_no_jobs_ok(tmp_path, capsys):
    db = tmp_path / "state.db"
    code, out, _ = run_cli(capsys, "status", "--db", str(db))
    assert code == 0
    assert "no jobs" in out.lower()


# --------------------------------------------------------------------------
# verify — a real zip reports STRUCT_OK
# --------------------------------------------------------------------------
def _write_real_zip(path, payload=b"hello takeout " * 4000):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.txt", payload)
    data = buffer.getvalue()
    path.write_bytes(data)
    return len(data)


def test_verify_reports_struct_ok(tmp_path, capsys):
    db, store, out_dir = seed_job(tmp_path, parts=1)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True)
    size = _write_real_zip(parts_dir / PART_000)
    # Match the plan's size_expected to the real file so the size gate passes.
    store.update_part(ARCHIVE, 0, size_expected=size, status=PartStatus.DONE)

    code, out, err = run_cli(capsys, "verify", ARCHIVE, "--db", str(db))

    assert code == 0, out + err
    assert "STRUCT_OK" in out
    assert PART_000 in out
    assert "1 ok" in out


def test_verify_json_and_deep_flag(tmp_path, capsys):
    db, store, out_dir = seed_job(tmp_path, parts=1)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True)
    size = _write_real_zip(parts_dir / PART_000)
    store.update_part(ARCHIVE, 0, size_expected=size, status=PartStatus.DONE)

    code, out, _ = run_cli(capsys, "verify", ARCHIVE, "--db", str(db),
                           "--json", "--deep")
    assert code == 0
    data = json.loads(out)
    assert data["level"] == "HASH_OK"
    assert data["parts"][0]["state"] == "HASH_OK"
    assert data["ok"] == 1


def test_verify_missing_part_fails(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path, parts=1)
    code, out, err = run_cli(capsys, "verify", ARCHIVE, "--db", str(db))
    assert code == 1
    assert "not on disk" in out


# --------------------------------------------------------------------------
# identity — provenance + OPERATOR_OVERRIDE
# --------------------------------------------------------------------------
def test_identity_shows_provenance(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    code, out, _ = run_cli(capsys, "identity", ARCHIVE, "--db", str(db))
    assert code == 0
    assert "SCRAPED_EMAIL" in out
    assert "HIGH" in out
    assert "gaia=1005482974000" in out


def test_identity_set_label_upgrades_to_operator_override(tmp_path, capsys):
    db = tmp_path / "state.db"
    store = JobStore.open(str(db))
    out_dir = tmp_path / "storage" / "gaia-1005482974000" / "2026-06-16-04-01-04"
    store.upsert_job(make_identity(label_source=LabelSource.GAIA_FALLBACK,
                                   label="gaia-1005482974000", email=None),
                     output_dir=str(out_dir))
    assert store.get_job(ARCHIVE).account_label == "gaia-1005482974000"

    code, out, _ = run_cli(capsys, "identity", ARCHIVE, "--set-label",
                           "braincreation", "--db", str(db))
    assert code == 0
    assert "OPERATOR_OVERRIDE" in out
    fresh = JobStore.open(str(db)).get_job(ARCHIVE)
    assert fresh.account_label == "braincreation"
    assert fresh.label_source is LabelSource.OPERATOR_OVERRIDE


# --------------------------------------------------------------------------
# doctor — preflight
# --------------------------------------------------------------------------
def test_doctor_exits_zero_on_healthy_tmp(tmp_path, capsys, monkeypatch):
    # A non-localhost CDP URL makes the cookie source un-constructible, so the
    # CDP/cookie checks are SKIPped rather than failed (spec 03 §2.3: "only if
    # a cookie source is constructible").
    monkeypatch.setenv("TK2_CDP_URL", "http://192.0.2.1:9222")
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db = tmp_path / "state.db"

    code, out, err = run_cli(capsys, "doctor", "--db", str(db))

    assert code == 0, out + err
    assert "storage root writable" in out
    assert "PASS" in out


def test_doctor_json_reports_checks(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TK2_CDP_URL", "http://192.0.2.1:9222")
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db = tmp_path / "state.db"
    code, out, _ = run_cli(capsys, "doctor", "--db", str(db), "--json")
    assert code == 0
    data = json.loads(out)
    names = {c["name"] for c in data["checks"]}
    assert "storage root writable" in names
    assert "state.db opens + WAL" in names
    assert "budget health" in names


# --------------------------------------------------------------------------
# run — dry-run must never touch the ledger
# --------------------------------------------------------------------------
def _payload(tmp_path) -> str:
    p = tmp_path / "payload.json"
    p.write_text(json.dumps({
        "archive_id": ARCHIVE,
        "export_raw": "20260616T040104Z",
        "account": {
            "gaia_user": "1005482974000", "authuser": "0",
            "email": "braincreation@gmail.com", "label": "braincreation",
            "label_source": "SCRAPED_EMAIL",
        },
        "parts_expected": 2,
        "filenames": [PART_000, PART_001],
        "sizes": {PART_000: 10 << 30, PART_001: 10 << 30},
        "dl_counts": {PART_000: 0, PART_001: 0},
        "captured_at": "2026-06-16T15:45:00.000Z",
    }), encoding="utf-8")
    return str(p)


def test_run_dry_run_prints_plan_and_reserves_nothing(tmp_path, capsys,
                                                      monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path / "storage"))
    db = tmp_path / "state.db"
    payload = _payload(tmp_path)

    code, out, err = run_cli(capsys, "run", "--payload", payload,
                             "--db", str(db), "--dry-run")
    assert code == 0, out + err
    assert "dry-run" in out
    assert "plan" in out
    assert "source=PAYLOAD" in out
    assert "braincreation" in out

    # No job row, no part rows, no attempt reservations — nothing was written.
    store = JobStore.open(str(db))
    assert store.list_jobs() == []
    ledger = AttemptLedger(sqlite3.connect(str(db)), budget=BUDGET,
                           reserve=RESERVE)
    assert ledger.archive_totals(ARCHIVE)["attempts_charged"] == 0
    assert store.list_parts(ARCHIVE) == []


def test_run_requires_payload_file(tmp_path, capsys):
    db = tmp_path / "state.db"
    code, out, err = run_cli(capsys, "run", "--payload",
                             str(tmp_path / "missing.json"), "--db", str(db))
    assert code == 1
    assert "payload file not found" in out + err


def test_run_requires_storage_root(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TK2_STORAGE_ROOT", raising=False)
    db = tmp_path / "state.db"
    payload = _payload(tmp_path)
    code, out, err = run_cli(capsys, "run", "--payload", payload,
                             "--db", str(db), "--dry-run")
    assert code == 1
    assert "TK2_STORAGE_ROOT" in out + err


# --------------------------------------------------------------------------
# migrate — lazy; friendly error only when takeout2.migrate is absent
# --------------------------------------------------------------------------
def _has_migrate() -> bool:
    try:
        import takeout2.migrate  # noqa: F401
        return True
    except ImportError:
        return False


def test_migrate_absent_or_dry_run(tmp_path, capsys):
    db = tmp_path / "state.db"
    code, out, _ = run_cli(capsys, "migrate", "--output-dir",
                           str(tmp_path / "v1"), "--db", str(db))
    if not _has_migrate():
        # The module is not built: the friendly error is the contract.
        assert code == 1
        assert "takeout2.migrate not built yet" in out
        return
    # Module present: dry-run over an empty v1 tree reports and writes nothing.
    assert code == 0, out
    assert "dry-run" in out
    assert not db.exists(), "dry-run must not create state.db"


@pytest.mark.skipif(not _has_migrate(),
                    reason="takeout2.migrate not built yet")
def test_migrate_apply_adopts_v1_job(tmp_path, capsys):
    db = tmp_path / "state.db"
    v1 = tmp_path / "v1" / "braincreation" / "2026-06-16-04-01-04"
    v1.mkdir(parents=True)
    part_path = v1 / PART_000
    size = _write_real_zip(part_path)
    v1.joinpath(".manager_state.json").write_text(json.dumps({
        "meta": {"archive_id": ARCHIVE,
                 "email": "braincreation@gmail.com",
                 "user": "1005482974000", "authuser": "0",
                 "export_raw": "20260616T040104Z"},
        "parts": [{"index": 0, "filename": PART_000,
                    "url": f"https://dl.google.com/takeout/download?j={ARCHIVE}",
                    "size": size, "done": size,
                    "status": "done", "zip_valid": True}],
    }), encoding="utf-8")

    code, out, err = run_cli(capsys, "migrate", "--output-dir",
                             str(tmp_path / "v1"), "--db", str(db),
                             "--apply")
    assert code == 0, out + err
    assert "1 jobs created" in out

    store = JobStore.open(str(db))
    job = store.get_job(ARCHIVE)
    assert job is not None
    assert job.account_label == "braincreation"
    part = store.get_part(ARCHIVE, 0)
    assert part.status is PartStatus.DONE
    assert part.verify_state is VerifyState.STRUCT_OK
    assert part.size_on_disk == size


# --------------------------------------------------------------------------
# error handling — no tracebacks without --debug
# --------------------------------------------------------------------------
def test_internal_error_is_friendly_without_debug(tmp_path, capsys):
    # A missing archive + --json on budget is fine; force a real failure by
    # giving budget an unreadable db path.
    code, out, err = run_cli(capsys, "budget", ARCHIVE,
                             "--db", str(tmp_path / "no" / "dir" / "state.db"))
    assert code == 1
    assert "Traceback" not in out + err
