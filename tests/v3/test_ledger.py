"""Offline tests for the ledger.

The two that must never regress are `test_refuses_a_fuse_backed_path` (the live
v1 deployment keeps its SQLite on the rclone mount — a real corruption risk) and
`test_dl_count_is_telemetry_and_gates_nothing` (the counter gave Δ2 then Δ0 for
superficially identical actions, so it cannot be an invariant).
"""
from __future__ import annotations

import sqlite3

import pytest

from autopilot.ledger import (
    AttemptKind,
    LedgerError,
    LedgerOnFuseMount,
    fuse_mount_for,
    open_ledger,
)

MOUNTS = [
    ("/", "ext4"),
    ("/opt/archives", "fuse.rclone"),
    ("/opt/local_cache_crypt", "xfs"),
    ("/config", "xfs"),
]


# ---------------------------------------------------------------------------
# the FUSE guard
# ---------------------------------------------------------------------------
def test_fuse_mount_for_matches_a_fuse_path():
    assert fuse_mount_for("/opt/archives/state.db", MOUNTS) == "/opt/archives"
    assert fuse_mount_for("/opt/archives/google-takeout/state.db", MOUNTS) == "/opt/archives"


def test_fuse_mount_for_allows_local_paths():
    assert fuse_mount_for("/config/state.db", MOUNTS) is None
    assert fuse_mount_for("/opt/local_cache_crypt/state.db", MOUNTS) is None


def test_fuse_mount_for_prefers_the_longest_matching_mount():
    nested = [("/opt", "fuse.weird"), ("/opt/local", "xfs")]
    # the deeper, non-FUSE mount wins — a naive prefix scan would wrongly refuse
    assert fuse_mount_for("/opt/local/x.db", nested) is None
    assert fuse_mount_for("/opt/other/x.db", nested) == "/opt"


def test_refuses_a_fuse_backed_path(tmp_path):
    with pytest.raises(LedgerOnFuseMount) as e:
        open_ledger("/opt/archives/state.db", mounts=MOUNTS)
    assert "FUSE" in str(e.value).upper() or "fuse" in str(e.value)


def test_opens_happily_on_a_local_path(tmp_path):
    led = open_ledger(str(tmp_path / "state.db"), mounts=MOUNTS)
    try:
        led.upsert_job("arch-1", account="acct", output_dir=str(tmp_path))
        assert led.job("arch-1")["status"] == "pending"
    finally:
        led.close()


def test_open_is_idempotent(tmp_path):
    p = str(tmp_path / "state.db")
    led = open_ledger(p, mounts=MOUNTS)
    led.upsert_job("arch-1")
    led.close()
    led2 = open_ledger(p, mounts=MOUNTS)          # re-running the schema is fine
    try:
        assert led2.job("arch-1") is not None
    finally:
        led2.close()


# ---------------------------------------------------------------------------
# parts
# ---------------------------------------------------------------------------
@pytest.fixture()
def led(tmp_path):
    l = open_ledger(str(tmp_path / "state.db"), mounts=MOUNTS)
    yield l
    l.close()


def test_upsert_parts_seeds_and_preserves_progress(led):
    led.upsert_parts("a1", [(0, "p0.zip", 100), (1, "p1.zip", 200)])
    assert len(led.parts("a1")) == 2

    # simulate progress, then re-scrape (a second page read)
    led.record_transfer("a1", 0, AttemptKind.TRANSFER, "ok", bytes_moved=100,
                        size_on_disk=100)
    led.set_part_status("a1", 0, "done")
    led.upsert_parts("a1", [(0, "p0.zip", 100), (1, "p1.zip", 200), (2, "p2.zip", 300)])

    p0 = led.part("a1", 0)
    assert p0["size_on_disk"] == 100 and p0["status"] == "done"   # progress survived
    assert len(led.parts("a1")) == 3                              # new part added


def test_unknown_part_status_is_rejected(led):
    led.upsert_parts("a1", [(0, "p.zip", 1)])
    with pytest.raises(LedgerError):
        led.set_part_status("a1", 0, "not-a-status")


# ---------------------------------------------------------------------------
# the minted-URL cache
# ---------------------------------------------------------------------------
def test_record_mint_caches_the_url_and_books_a_mint_attempt(led):
    led.upsert_parts("a1", [(0, "p.zip", 100)])
    assert led.needs_mint("a1", 0) is True

    led.record_mint("a1", 0, "https://takeout-download.example/x?j=1", etag="E1")

    assert led.needs_mint("a1", 0) is False       # never re-mint while cached
    assert led.minted_url("a1", 0).endswith("j=1")
    assert led.part("a1", 0)["attempts"] == 1
    assert led.attempts_by_kind("a1") == {AttemptKind.MINT: 1}


def test_clear_mint_makes_the_part_mintable_again(led):
    led.upsert_parts("a1", [(0, "p.zip", 1)])
    led.record_mint("a1", 0, "https://x/1")
    led.clear_mint("a1", 0)
    assert led.needs_mint("a1", 0) is True
    assert led.minted_url("a1", 0) is None


def test_transfer_and_resume_are_counted_separately(led):
    led.upsert_parts("a1", [(0, "p.zip", 100)])
    led.record_transfer("a1", 0, AttemptKind.TRANSFER, "ok", bytes_moved=40, size_on_disk=40)
    led.record_transfer("a1", 0, AttemptKind.RESUME, "ok", bytes_moved=60, size_on_disk=100)
    assert led.attempts_by_kind("a1") == {AttemptKind.TRANSFER: 1, AttemptKind.RESUME: 1}
    assert led.part("a1", 0)["size_on_disk"] == 100


def test_record_transfer_rejects_a_bogus_kind(led):
    led.upsert_parts("a1", [(0, "p.zip", 1)])
    with pytest.raises(LedgerError):
        led.record_transfer("a1", 0, "something-else", "ok")


# ---------------------------------------------------------------------------
# rule 3: telemetry, not an invariant
# ---------------------------------------------------------------------------
def test_dl_count_is_telemetry_and_gates_nothing(led):
    """The counter gave Δ2 then Δ0 for the same kind of action.

    So it is stored for the operator and must not change a part's status or
    block any scheduled work.
    """
    led.upsert_parts("a1", [(0, "p.zip", 100)])
    led.observe_dl_count("a1", 0, 5)                    # looks "exhausted"

    row = led.part("a1", 0)
    assert row["dl_count_seen"] == 5
    assert row["status"] == "pending"                   # NOT auto-failed
    assert led.needs_mint("a1", 0) is True              # NOT blocked


# ---------------------------------------------------------------------------
# jobs / summary
# ---------------------------------------------------------------------------
def test_job_status_transitions_including_the_two_that_matter(led):
    led.upsert_job("a1", account="acct", output_dir="/config/jobs/a1")

    # a challenge is a real state, not a generic error
    led.set_job_status("a1", "needs_reauth", error="hit challenge/pwd")
    assert led.job("a1")["status"] == "needs_reauth"

    # and an expired export is terminal, so it stops being re-driven
    led.set_job_status("a1", "expired_unrecoverable", error="export expired")
    assert led.job("a1")["status"] == "expired_unrecoverable"


def test_unknown_job_status_is_rejected(led):
    led.upsert_job("a1")
    with pytest.raises(LedgerError):
        led.set_job_status("a1", "made-up")


def test_summary_counts_parts_and_attempts(led):
    led.upsert_job("a1")
    led.upsert_parts("a1", [(0, "a", 1), (1, "b", 1), (2, "c", 1)])
    led.set_part_status("a1", 0, "done")
    led.record_mint("a1", 1, "https://x/1")

    s = led.summary("a1")
    assert s["parts"] == 3 and s["done"] == 1 and s["remaining"] == 2
    assert s["by_status"]["done"] == 1 and s["by_status"]["pending"] == 2
    assert s["attempts"] == {AttemptKind.MINT: 1}


def test_attempts_table_records_a_trail(led):
    led.upsert_parts("a1", [(0, "p.zip", 10)])
    led.record_mint("a1", 0, "https://x/1")
    led.record_transfer("a1", 0, AttemptKind.RESUME, "ok", bytes_moved=10)

    rows = led.conn.execute(
        "SELECT kind, outcome, bytes_moved FROM attempts WHERE archive_id='a1'"
        " ORDER BY id").fetchall()
    assert [(r["kind"], r["outcome"]) for r in rows] == [
        (AttemptKind.MINT, "ok"), (AttemptKind.RESUME, "ok")]
    assert rows[1]["bytes_moved"] == 10
