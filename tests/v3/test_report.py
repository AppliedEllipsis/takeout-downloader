"""Offline tests for the completeness report.

The report is where this project kept lying to itself. v2's verification only
checked a zip header and its end-of-archive marker, and completeness was never
tied to the export's full part list — so "all downloaded parts are valid" could be
printed while three parts were missing and the export had expired.

Two tests enforce the honest behaviour:

* `test_expired_and_incomplete_says_unrecoverable` — must say the loss is
  unrecoverable and that a NEW export is the only remedy. Never imply the old one
  is resumable.
* `test_dl_counts_are_labelled_telemetry` — the counter is not an invariant and
  the report must say so where it prints it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from autopilot.report import build_report, parse_expiry

NOW = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)
EXPIRY = "September 26, 2026 at 4:58 AM"      # the exact format the page renders
EXPIRED = "September 1, 2026 at 4:58 AM"


@dataclass
class P:
    idx: int
    status: str = "pending"
    filename: Optional[str] = None
    size_expected: Optional[int] = 100
    size_on_disk: int = 0
    attempts: int = 0
    dl_count_seen: Optional[int] = None


def parts(*specs) -> list:
    return [P(**s) for s in specs]


# ---------------------------------------------------------------------------
# expiry parsing
# ---------------------------------------------------------------------------
def test_parses_the_format_the_page_actually_renders():
    dt = parse_expiry(EXPIRY)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 9, 26, 4)
    assert dt.tzinfo is not None


def test_parses_iso_and_rejects_junk():
    assert parse_expiry("2026-09-26T04:58:00Z") is not None
    assert parse_expiry("2026-09-26") is not None
    assert parse_expiry("whenever") is None
    assert parse_expiry(None) is None
    assert parse_expiry("") is None


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------
def test_a_complete_export_is_reported_complete():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100},
                             {"idx": 1, "status": "done", "size_on_disk": 100}),
                       archive_id="a1", expiry_at=EXPIRY, now=NOW)
    assert rep.complete is True
    assert rep.verdict.startswith("COMPLETE")
    assert rep.missing_indices == []


def test_missing_parts_are_named_not_summarised_away():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100},
                             {"idx": 1, "status": "partial", "size_on_disk": 40},
                             {"idx": 2}),
                       archive_id="a1", expiry_at=EXPIRY, now=NOW)
    assert rep.parts_expected == 3 and rep.parts_done == 1
    assert rep.partial_indices == [1]
    assert rep.missing_indices == [2]
    assert rep.verdict.startswith("INCOMPLETE")


def test_expired_and_incomplete_says_unrecoverable():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100},
                             {"idx": 1}),
                       archive_id="a1", expiry_at=EXPIRED, now=NOW)
    assert rep.expired is True
    v = rep.verdict.upper()
    assert "EXPIRED" in v
    assert "UNRECOVERABLE" in v
    # and it must point at the only real remedy
    assert "NEW EXPORT" in v
    assert "resumable" not in v.lower()


def test_the_expired_unrecoverable_job_status_wins_even_without_a_date():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100},
                             {"idx": 1}),
                       archive_id="a1", status="expired_unrecoverable", now=NOW)
    assert "UNRECOVERABLE" in rep.verdict.upper()


def test_needs_reauth_is_reported_as_blocked_not_as_incomplete():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100},
                             {"idx": 1}),
                       archive_id="a1", status="needs_reauth", expiry_at=EXPIRY, now=NOW)
    assert rep.verdict.startswith("BLOCKED")


def test_no_parts_is_unknown_not_complete():
    rep = build_report([], archive_id="a1", now=NOW)
    assert rep.verdict.startswith("UNKNOWN")
    assert rep.warnings, "an empty ledger must warn rather than read as success"
    assert rep.complete is False


# ---------------------------------------------------------------------------
# the honesty requirements
# ---------------------------------------------------------------------------
def test_dl_counts_are_labelled_telemetry():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100,
                              "filename": "takeout-x.zip", "dl_count_seen": 3}),
                       archive_id="a1", expiry_at=EXPIRY, now=NOW)
    md = rep.as_markdown()
    assert "Telemetry" in md
    assert "NOT an invariant" in md
    assert "takeout-x.zip" in md and "3" in md


def test_the_attempts_table_marks_transfer_as_unmeasured():
    """Every measured Δ0 was a Range request; a fresh full GET was never measured."""
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100}),
                       archive_id="a1", expiry_at=EXPIRY, now=NOW)
    md = rep.as_markdown()
    assert "never" in md and "measured" in md
    assert "Δ1" in md and "Δ0" in md


def test_flags_the_v2_signature_of_attempts_spent_with_zero_bytes():
    """v2 booked NETWORK_ERROR attempts for parts that had no URL and never sent
    a request. Attempts > 0 with 0 bytes on disk is that signature."""
    rep = build_report(parts({"idx": 0, "status": "failed", "attempts": 2,
                              "size_on_disk": 0}),
                       archive_id="a1", now=NOW)
    assert any("zero bytes" in w for w in rep.warnings)


def test_unparseable_expiry_warns_instead_of_assuming():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 1}),
                       archive_id="a1", expiry_at="some time next week", now=NOW)
    assert any("could not parse" in w for w in rep.warnings)
    assert rep.expired is False


def test_partial_parts_are_flagged_as_free_to_finish():
    rep = build_report(parts({"idx": 0, "status": "partial", "size_on_disk": 10}),
                       archive_id="a1", expiry_at=EXPIRY, now=NOW)
    assert any("Δ0" in w for w in rep.warnings)
    assert "Partial" in rep.as_markdown()


def test_markdown_renders_the_headline_numbers():
    rep = build_report(parts({"idx": 0, "status": "done", "size_on_disk": 100},
                             {"idx": 1}),
                       archive_id="arch-xyz", account="acct", expiry_at=EXPIRY, now=NOW)
    md = rep.as_markdown()
    assert "arch-xyz" in md and "1/2" in md and "acct" in md
