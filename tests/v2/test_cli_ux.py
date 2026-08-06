"""Tests for the takeout2 CLI *UX layer* — the human-first additions.

Scope (all additive to test_cli.py, which still owns the normative shapes):

    * ``summary_line`` / ``_progress_bar`` — bar + percentage, including 0%,
      100% and the unknown-total case,
    * ``advise`` — the "what should I do now?" advisor, one rule per JobStatus,
    * ``status``/``next`` ``--json`` — machine-readable and parseable,
    * ``doctor --fix`` — repairs a missing parts dir and a stale ACTIVE part,
      and REFUSES anything that would spend a Google attempt or delete bytes,
    * friendly errors — a bogus archive id gets a one-line cause + a real
      command, never a traceback.

Every test is network-free and writes only under ``tmp_path``. Fixtures mirror
``tests/v2/test_cli.py`` (same ARCHIVE key, same seed helper shape).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from takeout2 import cli
from takeout2.cli import (advise, main, summary_line, _progress_bar, _pct_str)
from takeout2.contracts import (AccountIdentity, IdentityRecord, JobStatus,
                                LabelSource, PartPlan, PartStatus, ReasonCode,
                                VerifyState)
from takeout2.ledger import AttemptLedger
from takeout2.state import JobStore

ARCHIVE = "j=test-export-000000000000ab9f2"
BUDGET = 5
RESERVE = 1

PART_000 = "takeout-20260616T040104Z-9-000.zip"
PART_001 = "takeout-20260616T040104Z-9-001.zip"
PART_002 = "takeout-20260616T040104Z-9-002.zip"


def make_identity(label_source=LabelSource.SCRAPED_EMAIL,
                  email="braincreation@gmail.com", label="braincreation",
                  parts=3, archive_id=ARCHIVE) -> IdentityRecord:
    return IdentityRecord(
        archive_id=archive_id,
        export_raw="20260616T040104Z",
        account=AccountIdentity(gaia_user="1005482974000", authuser="0",
                                email=email, label=label,
                                label_source=label_source),
        parts_expected=parts,
    )


def seed_job(tmp_path, parts=3, budget=BUDGET):
    """Create a job with ``parts`` planned parts in a tmp storage root."""
    db = tmp_path / "state.db"
    store = JobStore.open(str(db))
    out_dir = tmp_path / "storage" / "braincreation" / "2026-06-16-04-01-04"
    store.upsert_job(make_identity(), output_dir=str(out_dir),
                     attempt_budget=budget)
    plans = [PartPlan(idx=0, filename=PART_000, size_expected=10 << 30),
             PartPlan(idx=1, filename=PART_001, size_expected=10 << 30),
             PartPlan(idx=2, filename=PART_002, size_expected=10 << 30)]
    store.upsert_parts(ARCHIVE, plans[:parts])
    return db, store, out_dir


def run_cli(capsys, *argv):
    """Invoke the CLI in-process; return (exit_code, stdout, stderr)."""
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def snap(db, store):
    """The list of job_status_data dicts the advisor consumes."""
    ledger = AttemptLedger(sqlite3.connect(str(db)), budget=BUDGET,
                           reserve=RESERVE)
    return [cli.job_status_data(store, ledger, j) for j in store.list_jobs()]


@pytest.fixture(autouse=True)
def _no_cookie_probe(monkeypatch):
    """Never touch CDP: a non-localhost URL makes the source unconstructible."""
    monkeypatch.setenv("TK2_CDP_URL", "http://192.0.2.1:9222")
    monkeypatch.delenv("TK2_RUNNER", raising=False)
    monkeypatch.delenv("TK2_MANAGER_URL", raising=False)


# ==========================================================================
# 1. progress bar + summary line
# ==========================================================================
def test_progress_bar_zero_full_and_unknown():
    assert _progress_bar(0) == "░" * 10
    assert _progress_bar(100) == "▓" * 10
    assert _progress_bar(None) == "─" * 10          # unknown total
    assert len(_progress_bar(37)) == 10
    # Monotonic: more percent is never fewer filled cells.
    filled = [_progress_bar(p).count("▓") for p in range(0, 101, 5)]
    assert filled == sorted(filled)
    assert _progress_bar(50).count("▓") == 5


def test_pct_str_handles_unknown():
    assert _pct_str(None).strip() == "—"
    assert _pct_str(0) == "0%"
    assert _pct_str(19.4) == "19%"
    assert _pct_str(100) == "100%"


def _fake_snap(**over):
    base = {
        "archive_id": ARCHIVE, "account_label": "braincreation",
        "export_ts": "2026-06-16-04-01-04", "status": "DOWNLOADING",
        "parts": {"total": 63, "done": 12, "active": 3, "partial": 0,
                  "pending": 48, "exhausted": 0, "failed": 0},
        "bytes_done": 412 * 1000 ** 3, "bytes_total": 3080 * 1000 ** 3,
        "pct": 19.0, "speed_bps": None, "eta_s": None, "last_error": None,
        "budget": {"parts_at_risk": 0, "at_1": 0, "at_0": 0},
    }
    base.update(over)
    return base


def test_summary_line_shape():
    line = summary_line(_fake_snap())
    assert "braincreation" in line
    assert "▓" in line and "░" in line
    assert "19%" in line
    assert "12/63 parts" in line
    assert "DOWNLOADING" in line
    assert "/" in line                                # bytes done / total


def test_summary_line_flags_budget_danger():
    at_1 = summary_line(_fake_snap(
        budget={"parts_at_risk": 2, "at_1": 2, "at_0": 0}))
    assert "⚠" in at_1
    assert "2 parts at 1 attempt left" in at_1

    at_0 = summary_line(_fake_snap(
        budget={"parts_at_risk": 1, "at_1": 0, "at_0": 1}))
    assert "1 part out of attempts" in at_0

    clean = summary_line(_fake_snap())
    assert "⚠" not in clean


def test_summary_line_zero_and_hundred_and_unknown():
    zero = summary_line(_fake_snap(pct=0.0, bytes_done=0,
                                   parts={"total": 63, "done": 0, "active": 0,
                                          "partial": 0, "pending": 63,
                                          "exhausted": 0, "failed": 0}))
    assert "0%" in zero and "0/63 parts" in zero and "▓" not in zero

    full = summary_line(_fake_snap(pct=100.0, status="COMPLETE",
                                    parts={"total": 63, "done": 63, "active": 0,
                                           "partial": 0, "pending": 0,
                                           "exhausted": 0, "failed": 0}))
    assert "100%" in full and "░" not in full and "COMPLETE" in full

    unknown = summary_line(_fake_snap(pct=None, bytes_total=0))
    assert "─" in unknown
    assert "%" not in unknown


def test_status_renders_summary_line_first(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=10 << 30)
    store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)

    code, out, err = run_cli(capsys, "status", "--db", str(db))

    assert code == 0, out + err
    first = out.splitlines()[0]
    assert "braincreation" in first
    assert "▓" in first or "░" in first
    assert "1/3 parts" in first
    # All pre-existing detail is still there, below the summary.
    assert "SCRAPED_EMAIL" in out
    assert "budget" in out and "cookie" in out and "errors" in out
    # And the advisor hint closes the report.
    assert "next:" in out


def test_status_percent_matches_bytes(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path, parts=2)
    # exactly half the expected bytes on disk -> 50%
    store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=10 << 30)
    code, out, _ = run_cli(capsys, "status", "--db", str(db))
    assert code == 0
    assert "50%" in out.splitlines()[0]
    assert "(50.0%)" in out                    # legacy detail line unchanged


# ==========================================================================
# 2. the advisor — one rule per JobStatus, derived from contracts.py
# ==========================================================================
def test_advise_no_jobs_points_at_the_browser(tmp_path):
    db = tmp_path / "state.db"
    store = JobStore.open(str(db))
    advice = advise(snap(db, store))
    assert advice["kind"] == "no_jobs"
    assert "takeout.google.com" in advice["headline"]
    assert "click Download" in advice["headline"]
    assert "automatically" in advice["headline"]
    assert advice["commands"] == []


def test_advise_needs_cookie(tmp_path):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.NEEDS_COOKIE)
    advice = advise(snap(db, store))
    assert advice["kind"] == "needs_cookie"
    assert "cookie" in advice["headline"].lower()
    assert "Takeout page" in advice["headline"]
    assert "re-captures" in advice["headline"]


def test_advise_budget_exhausted_explains_the_five_attempt_limit(tmp_path):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.BUDGET_EXHAUSTED)
    advice = advise(snap(db, store))
    assert advice["kind"] == "budget_exhausted"
    assert "5 downloads" in advice["headline"]
    assert "human must decide" in advice["headline"]
    assert f"budget {ARCHIVE}" in " ".join(advice["commands"])


def test_advise_downloading_says_nothing_to_do(tmp_path):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)
    advice = advise(snap(db, store))
    assert advice["kind"] == "downloading"
    assert "Nothing to do" in advice["headline"]
    assert "monitor" in advice["headline"]
    assert any(c.endswith("watch") for c in advice["commands"])


def test_advise_partial_parts_with_no_runner_suggests_starting_it(tmp_path):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.READY)
    store.update_part(ARCHIVE, 0, status=PartStatus.PARTIAL,
                      size_on_disk=3 << 30)
    advice = advise(snap(db, store))
    assert advice["kind"] == "stalled_parts"
    assert "no runner" in advice["headline"]
    assert "manager" in advice["headline"]


def test_advise_partial_parts_with_runner_present_is_quiet(tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("TK2_RUNNER", "1")
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.READY)
    store.update_part(ARCHIVE, 0, status=PartStatus.PARTIAL,
                      size_on_disk=3 << 30)
    advice = advise(snap(db, store))
    assert advice["kind"] == "downloading"
    assert "Nothing to do" in advice["headline"]


def test_advise_paused_and_verifying_and_discovering(tmp_path):
    db, store, _out = seed_job(tmp_path)
    for status, kind in ((JobStatus.PAUSED, "paused"),
                         (JobStatus.VERIFYING, "verifying"),
                         (JobStatus.DISCOVERING, "discovering")):
        store.set_job_status(ARCHIVE, status)
        advice = advise(snap(db, store))
        assert advice["kind"] == kind, (status, advice)


def test_advise_complete_offers_free_verification(tmp_path):
    db, store, _out = seed_job(tmp_path)
    for idx in range(3):
        store.update_part(ARCHIVE, idx, status=PartStatus.DONE,
                          size_on_disk=10 << 30)
    store.set_job_status(ARCHIVE, JobStatus.COMPLETE)
    advice = advise(snap(db, store))
    assert advice["kind"] == "complete"
    assert "3/3" in advice["headline"]
    assert "no attempts" in advice["headline"]


def test_advise_failed_surfaces_the_last_error(tmp_path):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.FAILED, error="DISK_ERROR: enospc")
    advice = advise(snap(db, store))
    assert advice["kind"] == "failed"
    assert "DISK_ERROR: enospc" in advice["headline"]


def test_advise_exhausted_part_beats_a_healthy_job_status(tmp_path):
    """A part at the 5-attempt wall outranks 'still downloading'."""
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)
    store.update_part(ARCHIVE, 2, status=PartStatus.BUDGET_EXHAUSTED)
    advice = advise(snap(db, store))
    assert advice["kind"] == "parts_exhausted"
    assert "5-attempt limit" in advice["headline"]


def test_advise_covers_every_job_status():
    """Every JobStatus in contracts.py must map to a rule, not a crash."""
    seen = set()
    for status in JobStatus:
        advice = advise([_fake_snap(status=status.value)])
        assert advice["headline"]
        assert advice["kind"] in cli._ADVICE_RANK, (status, advice)
        seen.add(advice["kind"])
    assert len(seen) >= 6


def test_advise_picks_the_most_urgent_across_jobs():
    quiet = _fake_snap(status="DOWNLOADING", archive_id="j=aaa")
    urgent = _fake_snap(status="BUDGET_EXHAUSTED", archive_id="j=bbb")
    advice = advise([quiet, urgent])
    assert advice["kind"] == "budget_exhausted"
    assert advice["archive_id"] == "j=bbb"
    assert advice["others"] == 1


def test_next_command_prints_one_action(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.NEEDS_COOKIE)
    code, out, err = run_cli(capsys, "next", "--db", str(db))
    assert code == 0, out + err
    assert out.startswith("next:")
    assert "cookie" in out.lower()


def test_next_only_names_real_commands(tmp_path):
    """The advisor must never invent a subcommand that does not exist."""
    real = set(cli.build_parser()._subparsers._group_actions[0].choices)
    db, store, _out = seed_job(tmp_path)
    for status in JobStatus:
        store.set_job_status(ARCHIVE, status)
        for cmd in advise(snap(db, store))["commands"]:
            tokens = cmd.split()
            assert tokens[0] == "takeout2", cmd
            assert tokens[1] in real, f"{cmd} names a non-existent command"


# ==========================================================================
# 3. --json is machine readable
# ==========================================================================
def test_status_json_is_valid_and_carries_advice(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 0, status=PartStatus.DONE, size_on_disk=10 << 30)
    store.set_job_status(ARCHIVE, JobStatus.DOWNLOADING)

    code, out, _ = run_cli(capsys, "status", "--db", str(db), "--json")
    assert code == 0
    data = json.loads(out)                      # must parse
    assert data["jobs"][0]["parts"]["done"] == 1
    assert data["advice"]["kind"] == "downloading"
    assert data["advice"]["archive_id"] == ARCHIVE
    # No human decoration leaked into the machine view.
    assert "next:" not in out
    assert "\u2593" not in out and "\u2591" not in out


def test_status_json_no_jobs_is_valid(tmp_path, capsys):
    db = tmp_path / "state.db"
    code, out, _ = run_cli(capsys, "status", "--db", str(db), "--json")
    assert code == 0
    data = json.loads(out)
    assert data["jobs"] == []
    assert data["advice"]["kind"] == "no_jobs"


def test_next_json_is_valid(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    store.set_job_status(ARCHIVE, JobStatus.BUDGET_EXHAUSTED)
    code, out, _ = run_cli(capsys, "next", "--db", str(db), "--json")
    assert code == 0
    data = json.loads(out)
    assert data["advice"]["kind"] == "budget_exhausted"
    assert isinstance(data["advice"]["commands"], list)


# ==========================================================================
# 4. doctor --fix — repairs the safe, refuses the expensive
# ==========================================================================
def test_doctor_fix_creates_missing_parts_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, out_dir = seed_job(tmp_path)
    parts_dir = out_dir / "parts"
    assert not parts_dir.exists()

    code, out, err = run_cli(capsys, "doctor", "--fix", "--db", str(db))

    assert parts_dir.is_dir(), "doctor --fix must create the parts dir"
    assert "parts dir" in out
    assert "before:" in out and "after:" in out          # before/after per fix
    assert "missing" in out and "created" in out
    assert "Traceback" not in out + err


def test_doctor_fix_resets_stale_active_part_to_partial(tmp_path, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, _out = seed_job(tmp_path)
    # A crashed process left this row ACTIVE with bytes already on disk.
    store.update_part(ARCHIVE, 1, status=PartStatus.ACTIVE,
                      size_on_disk=3 << 30)

    code, out, err = run_cli(capsys, "doctor", "--fix", "--db", str(db))

    fresh = JobStore.open(str(db)).get_part(ARCHIVE, 1)
    assert fresh.status is PartStatus.PARTIAL, "stale ACTIVE must become PARTIAL"
    # Bytes are untouched: resuming is the whole point.
    assert fresh.size_on_disk == 3 << 30
    assert "stale part" in out
    assert "ACTIVE" in out and "PARTIAL" in out
    assert "0 attempts spent" in out


def test_doctor_fix_refuses_budget_exhausted_part(tmp_path, capsys,
                                                  monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 2, status=PartStatus.BUDGET_EXHAUSTED)

    code, out, err = run_cli(capsys, "doctor", "--fix", "--db", str(db))

    # The state is REPORTED and REFUSED, never cleared.
    fresh = JobStore.open(str(db)).get_part(ARCHIVE, 2)
    assert fresh.status is PartStatus.BUDGET_EXHAUSTED
    assert "refused" in out.lower()
    assert "budget exhausted" in out.lower()
    assert "5" in out                                    # the attempt ceiling
    assert "do this:" in out


def test_doctor_fix_refuses_failed_and_corrupt_parts(tmp_path, capsys,
                                                     monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, out_dir = seed_job(tmp_path)
    store.update_part(ARCHIVE, 0, status=PartStatus.FAILED,
                      error="NETWORK_ERROR")
    store.update_part(ARCHIVE, 1, verify_state=VerifyState.CORRUPT,
                      size_on_disk=7 << 30)

    code, out, err = run_cli(capsys, "doctor", "--fix", "--db", str(db))

    store2 = JobStore.open(str(db))
    # Nothing was retried, nothing was reset, no bytes were removed.
    assert store2.get_part(ARCHIVE, 0).status is PartStatus.FAILED
    part1 = store2.get_part(ARCHIVE, 1)
    assert part1.verify_state is VerifyState.CORRUPT
    assert part1.size_on_disk == 7 << 30
    assert "failed parts" in out.lower()
    assert "corrupt parts" in out.lower()
    assert "never automatic" in out


def test_doctor_fix_never_spends_an_attempt(tmp_path, capsys, monkeypatch):
    """The ledger must be byte-identical before and after --fix."""
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, _out = seed_job(tmp_path)
    ledger = AttemptLedger(sqlite3.connect(str(db)), budget=BUDGET,
                           reserve=RESERVE)
    ledger.reserve(ARCHIVE, 0).commit(ReasonCode.OK_PARTIAL, bytes_moved=1 << 20)
    ledger.observe_remote(ARCHIVE, 0, 3)
    store.update_part(ARCHIVE, 1, status=PartStatus.ACTIVE, size_on_disk=1 << 30)
    before = ledger.archive_totals(ARCHIVE)
    before_left = ledger.budget_for(ARCHIVE, 0).remaining

    code, out, err = run_cli(capsys, "doctor", "--fix", "--db", str(db))

    after_ledger = AttemptLedger(sqlite3.connect(str(db)), budget=BUDGET,
                                 reserve=RESERVE)
    assert after_ledger.archive_totals(ARCHIVE) == before
    assert after_ledger.budget_for(ARCHIVE, 0).remaining == before_left
    assert "Traceback" not in out + err


def test_doctor_fix_never_deletes_downloaded_bytes(tmp_path, capsys,
                                                   monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, out_dir = seed_job(tmp_path)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True)
    blob = parts_dir / PART_000
    blob.write_bytes(b"downloaded bytes" * 64)
    size = blob.stat().st_size
    store.update_part(ARCHIVE, 0, status=PartStatus.PARTIAL, size_on_disk=size,
                      verify_state=VerifyState.CORRUPT)

    code, out, err = run_cli(capsys, "doctor", "--fix", "--db", str(db))

    assert blob.exists(), "--fix must never delete a downloaded part"
    assert blob.stat().st_size == size, "--fix must never truncate a part"


def test_doctor_fix_json_reports_fixes_and_refusals(tmp_path, capsys,
                                                    monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 1, status=PartStatus.ACTIVE, size_on_disk=1 << 30)
    store.update_part(ARCHIVE, 2, status=PartStatus.BUDGET_EXHAUSTED)

    code, out, _ = run_cli(capsys, "doctor", "--fix", "--db", str(db), "--json")
    data = json.loads(out)
    assert "checks" in data                       # legacy payload preserved
    assert any("stale part" in f["fix"] for f in data["fixes"])
    assert all({"fix", "before", "after"} <= set(f) for f in data["fixes"])
    assert any("budget exhausted" in r["issue"] for r in data["refusals"])
    assert all({"issue", "why", "do_this"} <= set(r) for r in data["refusals"])


def test_doctor_fix_is_idempotent(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, _out = seed_job(tmp_path)
    store.update_part(ARCHIVE, 1, status=PartStatus.ACTIVE, size_on_disk=1 << 30)
    run_cli(capsys, "doctor", "--fix", "--db", str(db))

    code, out, _ = run_cli(capsys, "doctor", "--fix", "--db", str(db), "--json")
    data = json.loads(out)
    assert not any("stale part" in f["fix"] for f in data["fixes"])
    assert not any("parts dir" in f["fix"] for f in data["fixes"])


def test_doctor_without_fix_changes_nothing(tmp_path, capsys, monkeypatch):
    """Pre-existing behaviour: plain doctor still only diagnoses."""
    monkeypatch.setenv("TK2_STORAGE_ROOT", str(tmp_path))
    db, store, out_dir = seed_job(tmp_path)
    store.update_part(ARCHIVE, 1, status=PartStatus.ACTIVE, size_on_disk=1 << 30)

    code, out, _ = run_cli(capsys, "doctor", "--db", str(db))

    assert JobStore.open(str(db)).get_part(ARCHIVE, 1).status is PartStatus.ACTIVE
    assert not (out_dir / "parts").exists()
    assert "fixed:" not in out
    # And the legacy check table is still what it was.
    assert "storage root writable" in out
    assert "budget health" in out


# ==========================================================================
# 5. friendly errors — a cause and a real command, never a traceback
# ==========================================================================
def test_bogus_archive_id_is_friendly(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    code, out, err = run_cli(capsys, "budget", "j=not-a-real-archive",
                             "--db", str(db))
    text = out + err
    assert code == 1
    assert "Traceback" not in text
    assert "no job for archive" in text
    assert "1 job(s) known" in text
    assert "takeout2 status" in text


def test_abbreviated_archive_id_suggests_the_real_one(tmp_path, capsys):
    db, store, _out = seed_job(tmp_path)
    code, out, err = run_cli(capsys, "budget", "ab9f2", "--db", str(db))
    text = out + err
    assert code == 1
    assert "did you mean" in text
    assert ARCHIVE in text


def test_bogus_archive_id_on_empty_db_points_at_the_browser(tmp_path, capsys):
    db = tmp_path / "state.db"
    code, out, err = run_cli(capsys, "identity", "j=nope", "--db", str(db))
    text = out + err
    assert code == 1
    assert "no jobs" in text
    assert "takeout.google.com" in text


def test_unopenable_db_is_friendly(tmp_path, capsys):
    # A regular file where a directory must be: makedirs cannot fix this, so
    # open_backend really fails and must do so in one friendly line.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    bad = blocker / "state.db"
    code, out, err = run_cli(capsys, "status", "--db", str(bad))
    text = out + err
    assert code == 1
    assert "Traceback" not in text
    assert "state.db" in text
    assert "--db" in text                       # a concrete suggested command


def test_corrupt_db_is_friendly_not_a_traceback(tmp_path, capsys):
    bad = tmp_path / "state.db"
    bad.write_bytes(b"this is definitely not a sqlite database" * 40)
    code, out, err = run_cli(capsys, "status", "--db", str(bad))
    text = out + err
    assert code == 1
    assert "Traceback" not in text
    assert "takeout2:" in text
    # It must never suggest destroying the attempt record.
    assert "rm " not in text and "delete it" not in text.replace("NOT delete", "")


def test_friendly_reason_maps_known_failures():
    locked = cli._friendly_reason(sqlite3.OperationalError("database is locked"))
    assert locked and "locked" in locked[0]
    missing_tbl = cli._friendly_reason(
        sqlite3.OperationalError("no such table: job"))
    assert missing_tbl and "doctor --fix" in missing_tbl[1]
    unopenable = cli._friendly_reason(
        sqlite3.OperationalError("unable to open database file"))
    assert unopenable and "--db" in unopenable[1]
    unreachable = cli._friendly_reason(
        RuntimeError("HTTPConnectionPool: Connection refused"))
    assert unreachable and "unreachable" in unreachable[0]
    assert cli._friendly_reason(ValueError("something exotic")) is None


# ==========================================================================
# 6. --help epilogue + backward compatibility of the command surface
# ==========================================================================
def test_help_epilogue_describes_the_zero_cli_flow(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "TYPICAL USE" in out
    assert "takeout.google.com" in out
    assert "Click Download" in out
    assert "automatically" in out
    assert "INSPECTION and RECOVERY" in out


def test_all_preexisting_commands_and_flags_still_exist():
    parser = build = cli.build_parser()
    choices = build._subparsers._group_actions[0].choices
    for name in ("status", "watch", "run", "budget", "verify", "identity",
                 "doctor", "migrate"):
        assert name in choices, f"{name} disappeared"
        opts = {o for a in choices[name]._actions for o in a.option_strings}
        assert {"--db", "--json", "--debug"} <= opts, name
    assert "next" in choices                              # the new one

    run_opts = {o for a in choices["run"]._actions for o in a.option_strings}
    assert {"--payload", "--dry-run"} <= run_opts
    ver_opts = {o for a in choices["verify"]._actions for o in a.option_strings}
    assert "--deep" in ver_opts
    ident = {o for a in choices["identity"]._actions for o in a.option_strings}
    assert "--set-label" in ident
    mig = {o for a in choices["migrate"]._actions for o in a.option_strings}
    assert {"--output-dir", "--apply"} <= mig
    doc = {o for a in choices["doctor"]._actions for o in a.option_strings}
    assert "--fix" in doc
    assert parser is build
