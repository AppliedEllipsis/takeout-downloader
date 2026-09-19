"""The adversarial review's findings, pinned by tests that actually run the path.

Every test here corresponds to a defect found by the four-agent review of
2026-09-19. They are written to FAIL if the guard is present-but-inert — the
failure mode this project has hit eleven times — so the behavioural ones drive
`run_once` through the real entry point rather than asserting on source text.

Why that matters, concretely: the first version of the terminal-status guard was
written *above* its own `_finish` closure, so it would have raised `NameError` on
exactly the path it was meant to protect — and the whole suite still passed,
because nothing exercised it. A guard that cannot run looks identical to a guard
that works until you run it.
"""
from test_integration import (  # noqa: E402  (conftest puts tests/v3 on the path)
    ACCOUNT,
    ARCHIVE_ID,
    go,
    scenario,
)

from autopilot.ledger import AttemptKind, open_ledger
from autopilot.run import (
    MAX_CACHED_URL_FAILURES,
    RUN_OUTCOME_STATUSES,
    TERMINAL_BLOCKED_STATUSES,
)


# ==========================================================================
# F1 — vocabulary drift: three vocabularies must agree
# ==========================================================================
def test_quota_exceeded_is_a_declared_run_outcome():
    """`run_once` returns it, so it must be declared here. It was not — the tuple
    was missed when the status was added to `JOB_STATUSES` and the exit map, which
    is the same drift that once broke `incomplete`."""
    assert "quota_exceeded" in RUN_OUTCOME_STATUSES


def test_every_status_the_orchestrator_can_return_is_declared():
    """Any literal passed to `_finish(...)` must be a declared outcome."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "autopilot" / "run.py").read_text()
    returned = set(re.findall(r'_finish\(\s*"([a-z_]+)"', src))
    undeclared = returned - set(RUN_OUTCOME_STATUSES)
    assert not undeclared, f"run_once can return undeclared status(es): {undeclared}"


def test_terminal_statuses_are_declared_outcomes():
    for status in TERMINAL_BLOCKED_STATUSES:
        assert status in RUN_OUTCOME_STATUSES


# ==========================================================================
# F2 — the scrape's placeholder filename must not overwrite a real one
# ==========================================================================
def test_upsert_parts_does_not_clobber_a_recorded_real_filename(tmp_path):
    """The scrape can only ever produce `download` (the redirector basename); the
    real name arrives only after minting. A plain COALESCE reset every part's name
    on every re-run."""
    led = open_ledger(str(tmp_path / "s.db"))
    led.upsert_parts(ARCHIVE_ID, [(0, "download", 100)])
    led.set_part_filename(ARCHIVE_ID, 0, "takeout-20260919T163231Z-1-001.zip")

    led.upsert_parts(ARCHIVE_ID, [(0, "download", 100)])   # a second scrape
    assert led.part(ARCHIVE_ID, 0)["filename"] == "takeout-20260919T163231Z-1-001.zip"


def test_upsert_parts_still_accepts_a_genuine_new_name(tmp_path):
    led = open_ledger(str(tmp_path / "s.db"))
    led.upsert_parts(ARCHIVE_ID, [(0, "download", 100)])
    led.upsert_parts(ARCHIVE_ID, [(0, "real-name.zip", 100)])
    assert led.part(ARCHIVE_ID, 0)["filename"] == "real-name.zip"


# ==========================================================================
# F5 — a failed transfer must be booked as an attempt
# ==========================================================================
def test_failed_transfer_is_booked_as_an_attempt(tmp_path):
    """It used to book nothing, so the report printed `transfer 0` for runs that
    had made real HTTP requests — and the attempt count is the only budget signal
    this project has."""
    led = open_ledger(str(tmp_path / "s.db"))
    led.upsert_parts(ARCHIVE_ID, [(0, "download", 100)])
    assert led.part(ARCHIVE_ID, 0)["attempts"] == 0

    led.record_failed_transfer(ARCHIVE_ID, 0, RuntimeError("connection reset"))
    assert led.part(ARCHIVE_ID, 0)["attempts"] == 1
    assert led.failed_transfer_count(ARCHIVE_ID, 0) == 1

    led.record_failed_transfer(ARCHIVE_ID, 0, RuntimeError("again"))
    assert led.failed_transfer_count(ARCHIVE_ID, 0) == 2


def test_successful_transfers_are_not_counted_as_failures(tmp_path):
    led = open_ledger(str(tmp_path / "s.db"))
    led.upsert_parts(ARCHIVE_ID, [(0, "download", 100)])
    led.record_transfer(ARCHIVE_ID, 0, AttemptKind.TRANSFER, "complete", bytes_moved=100)
    assert led.failed_transfer_count(ARCHIVE_ID, 0) == 0


def test_failed_transfer_records_its_reason(tmp_path):
    led = open_ledger(str(tmp_path / "s.db"))
    led.upsert_parts(ARCHIVE_ID, [(0, "download", 100)])
    led.record_failed_transfer(ARCHIVE_ID, 0, RuntimeError("boom"))
    row = led.conn.execute(
        "SELECT outcome, note FROM attempts WHERE archive_id=? AND idx=?",
        (ARCHIVE_ID, 0)).fetchone()
    assert row["outcome"] == "failed"
    assert "boom" in (row["note"] or "")


def test_the_cached_url_failure_bound_is_small_and_explicit():
    """It exists to bound mint spend against a 5-attempt allowance, so it must not
    be effectively infinite."""
    assert 2 <= MAX_CACHED_URL_FAILURES <= 10


# ==========================================================================
# F7 — a stored terminal status must stop the run AT THE ENTRY POINT
# ==========================================================================
def _mark_terminal(tmp_path, status: str, error: str = "recorded earlier"):
    """Build a scenario, then stamp a terminal status on its job row."""
    s = scenario(tmp_path, n_parts=1)
    led = open_ledger(s["ledger_path"])
    led.upsert_job(ARCHIVE_ID, account=ACCOUNT)
    led.set_job_status(ARCHIVE_ID, status, error=error)
    led.close()
    return s


def test_run_once_refuses_a_job_already_marked_quota_exceeded(tmp_path):
    s = _mark_terminal(tmp_path, "quota_exceeded", "allowance spent")
    outcome = go(s)

    assert outcome.status == "quota_exceeded"
    assert "allowance spent" in (outcome.error or "")
    assert s["harness"].mint_navigations() == [], "a terminal export must not be minted"
    assert s["client"].requests == [], "a terminal export must not be transferred"


def test_run_once_refuses_a_job_already_marked_expired(tmp_path):
    s = _mark_terminal(tmp_path, "expired_unrecoverable", "window closed")
    outcome = go(s)

    assert outcome.status == "expired_unrecoverable"
    assert "window closed" in (outcome.error or "")
    assert s["harness"].mint_navigations() == []
    assert s["client"].requests == []


def test_the_terminal_refusal_does_not_re_scrape(tmp_path):
    """Rule 4 says *stop*, so it must not even navigate to the archive page — the
    whole point is that an expiry the page no longer renders cannot re-enable it."""
    s = _mark_terminal(tmp_path, "quota_exceeded")
    go(s)
    assert s["harness"].navigated() == [], (
        "a terminal export must not be re-scraped at all")


def test_needs_reauth_is_NOT_terminal_and_a_run_still_tries(tmp_path):
    """A human can clear this one, so blocking on it would be wrong. If this test
    ever fails, the fix has overreached into a different failure mode."""
    s = scenario(tmp_path, n_parts=1)
    led = open_ledger(s["ledger_path"])
    led.upsert_job(ARCHIVE_ID, account=ACCOUNT)
    led.set_job_status(ARCHIVE_ID, "needs_reauth", error="old challenge")
    led.close()

    outcome = go(s)
    assert outcome.status != "needs_reauth", (
        "a stored needs_reauth must not stop a fresh attempt — only an explicit "
        "challenge on the page may do that")
    assert s["harness"].mint_navigations(), "the run should have proceeded to mint"


def test_a_complete_job_is_not_blocked_from_finishing_remaining_work(tmp_path):
    """`complete` is not terminal-with-respect-to-work: a ledger can say complete
    while a part is missing, and blocking would make that unrecoverable."""
    s = scenario(tmp_path, n_parts=1)
    led = open_ledger(s["ledger_path"])
    led.upsert_job(ARCHIVE_ID, account=ACCOUNT)
    led.set_job_status(ARCHIVE_ID, "complete")
    led.close()

    outcome = go(s)
    assert outcome.status == "complete"
    assert s["harness"].mint_navigations(), "it should have re-attempted the part"
