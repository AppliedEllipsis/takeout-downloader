"""Exit codes are a contract with cron and shell callers — pin them offline.

Verified live just before this file was written, the run against a spent export
printed:

    **Verdict: BLOCKED — export download allowance exhausted (0/1 held).
       NOT resumable: create a NEW export.**
    - job status: quota_exceeded

but its exit code could not be re-measured on demand, because reaching that point
requires a *live* rapt and the tab's token had gone stale in the meantime — the
no-rapt guard then fires first and correctly reports `needs_reauth`. A contract
that can only be checked when a browser happens to be in the right state is not a
contract, so the mapping is asserted here instead.
"""
from autopilot.__main__ import (
    EXIT_EXPIRED,
    EXIT_NEEDS_REAUTH,
    EXIT_OK,
    EXIT_OTHER,
    EXIT_QUOTA_EXCEEDED,
    _EXIT_FOR_STATUS,
)
from autopilot.ledger import JOB_STATUSES


def test_the_measured_exit_codes_are_what_the_contract_promises():
    assert (EXIT_OK, EXIT_OTHER, EXIT_NEEDS_REAUTH, EXIT_EXPIRED) == (0, 1, 2, 3)


def test_quota_exceeded_has_its_own_exit_code():
    """Not EXIT_OTHER: a caller that files it as generic will retry forever
    against an export that can never succeed — every attempt is refused."""
    assert EXIT_QUOTA_EXCEEDED == 4
    assert _EXIT_FOR_STATUS["quota_exceeded"] == 4
    assert _EXIT_FOR_STATUS["quota_exceeded"] != EXIT_OTHER


def test_every_mapped_status_is_a_real_job_status():
    """The vocabulary drift that broke `incomplete` must not recur: a status
    mapped to an exit code but absent from JOB_STATUSES would be rejected by the
    ledger at exactly the moment it matters."""
    for status in _EXIT_FOR_STATUS:
        assert status in JOB_STATUSES, f"{status!r} is mapped but not a valid job status"


def test_terminal_statuses_do_not_share_the_generic_code():
    """Each of these has a specific remedy; collapsing them loses the remedy."""
    specific = [_EXIT_FOR_STATUS[s] for s in
                ("needs_reauth", "expired_unrecoverable", "quota_exceeded")]
    assert len(set(specific)) == len(specific), "these must be distinguishable by exit code"
    assert EXIT_OTHER not in specific
