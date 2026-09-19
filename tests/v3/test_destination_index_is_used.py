"""Prove the destination index is actually CONSULTED, not merely built.

`index_destination` and `plan_moves` were imported by `run.py`, the index was
built and written to, and it was never read — so a part whose bytes were already
in the archive could still be re-minted (Δ1 against a 5-attempt allowance) and
re-transferred. `test_moving_is_idempotent_by_planning` proved the planner correct
in isolation while nothing in the run used it, which is a test that reads as
coverage and provides none.

Wiring it up without a test that FAILS when the wiring is removed would just be
another guard that looks right. These tests drive `run_once`.
"""
from test_integration import (  # noqa: E402
    ACCOUNT,
    ARCHIVE_ID,
    go,
    part_filename,
    scenario,
)

from autopilot.ledger import open_ledger  # noqa: E402

#: The test fixture's own part name. It must be the SAME name the scrape supplies,
#: because in reality one run records the name and writes the file — a test that
#: uses two different names is testing a situation that cannot occur.
REAL_NAME = part_filename(0)


def _seed_ledger_with_real_name(s, name=REAL_NAME):
    """Record the part under its REAL name, as a previous run would have."""
    led = open_ledger(s["ledger_path"])
    led.upsert_job(ARCHIVE_ID, account=ACCOUNT)
    led.upsert_parts(ARCHIVE_ID, [(0, "download", s["sizes"][0])])
    led.set_part_filename(ARCHIVE_ID, 0, name)
    led.close()


def _place_in_destination(s, name=REAL_NAME, size=None):
    """Put the bytes in the archive, as a previous successful move would have."""
    (s["archive"] / name).write_bytes(b"x" * (size if size is not None else s["sizes"][0]))


def test_a_part_already_in_the_destination_is_not_minted_or_transferred(tmp_path):
    """The whole point of the index. Without the wiring this test fails on both
    counts: a mint navigation happens and an HTTP request is made."""
    s = scenario(tmp_path, n_parts=1)
    _seed_ledger_with_real_name(s)
    _place_in_destination(s)

    outcome = go(s)

    assert outcome.status == "complete"
    assert outcome.parts_done == 1
    assert s["harness"].mint_navigations() == [], (
        "a part already archived must not be minted again — that is Δ1 against a "
        "5-attempt allowance")
    assert s["client"].requests == [], (
        "a part already archived must not be re-transferred")


def test_a_present_file_of_the_WRONG_size_is_not_treated_as_done(tmp_path):
    """Size must be compared, not merely presence. A truncated file in the
    destination is not a completed part."""
    s = scenario(tmp_path, n_parts=1)
    _seed_ledger_with_real_name(s)
    _place_in_destination(s, size=s["sizes"][0] - 10)   # short

    outcome = go(s)

    assert s["harness"].mint_navigations(), (
        "a wrong-sized destination file must not satisfy the part")
    assert outcome.status == "complete", "and the run should then fetch it properly"


def test_a_placeholder_filename_does_not_enable_the_skip(tmp_path):
    """A destination file named `download` must NOT satisfy a part: that is the
    redirector basename, not a part name, and matching on it would let an
    unrelated file silence a part that was never fetched."""
    s = scenario(tmp_path, n_parts=1)
    led = open_ledger(s["ledger_path"])
    led.upsert_job(ARCHIVE_ID, account=ACCOUNT)
    led.upsert_parts(ARCHIVE_ID, [(0, "download", s["sizes"][0])])
    led.close()
    # a file literally called `download` is present, of exactly the right size
    (s["archive"] / "download").write_bytes(b"x" * s["sizes"][0])

    outcome = go(s)

    assert s["harness"].mint_navigations(), (
        "a destination file named `download` is the redirector basename, not a "
        "part name, and must not be mistaken for a completed part")
    assert outcome.status == "complete"


def test_the_wiring_exists_in_source_as_a_backstop(tmp_path):
    """Behaviour is asserted above; this pins the call so a refactor cannot quietly
    drop it. Cheap, and this project has lost three guards to silent removal."""
    import inspect

    import autopilot.run as run_mod

    src = inspect.getsource(run_mod.run_once)
    assert "plan_moves(" in src, "run_once must consult the destination index"
    assert 'skip-present' in src, "the skip-present decision must be acted on"
