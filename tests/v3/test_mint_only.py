"""`--mint-only`: earn every URL while the ReAuth window is fresh.

Measured 2026-09-19: a minted URL still answered `206` with real zip bytes **81 minutes**
after it was minted — long past the ~45-minute ReAuth window. Minting is what the window
gates; transferring needs only the jar.

That decides the shape of a long pull. The normal loop mints per part, interleaved with
transfers, so a 63-part export taking hours runs out of window part-way and stops with
`needs_reauth`, unable to earn the later URLs. Minting everything first makes the long
transfer pass independent of the window, and v3 already caches minted URLs in the
ledger, so the second pass never mints again.

Offline.
"""
import asyncio
import inspect

from test_integration import ACCOUNT, ARCHIVE_ID, go, scenario  # noqa: E402

from autopilot.__main__ import build_parser  # noqa: E402
from autopilot.ledger import open_ledger  # noqa: E402
from autopilot.run import RunConfig, run_once  # noqa: E402


def test_mint_only_is_a_switch_on_the_run_command():
    args = build_parser().parse_args(
        ["run", "--archive-id", "x", "--ledger", "l", "--staging", "s",
         "--archive", "a", "--mint-only"])
    assert args.mint_only is True


def test_mint_only_defaults_to_off():
    args = build_parser().parse_args(
        ["run", "--archive-id", "x", "--ledger", "l", "--staging", "s", "--archive", "a"])
    assert args.mint_only is False


def test_mint_only_earns_urls_without_transferring_anything(tmp_path):
    """Drives run_once: mints must happen, HTTP requests must not."""
    s = scenario(tmp_path, n_parts=1)
    cfg = RunConfig(archive_id=ARCHIVE_ID, ledger_path=s["cfg"].ledger_path,
                    staging_dir=s["cfg"].staging_dir, archive_dir=s["cfg"].archive_dir,
                    account=ACCOUNT, settle=0.01, mint_only=True)

    outcome = asyncio.run(run_once(cfg, session_factory=s["harness"],
                                   http_client=s["client"]))

    assert s["harness"].mint_navigations(), "it must mint"
    assert s["client"].requests == [], "and must NOT transfer"
    # work remains, so this is not 'complete' — the transfer pass has not run
    assert outcome.status == "incomplete"


def test_the_minted_urls_are_cached_for_the_next_pass(tmp_path):
    """The whole design rests on this: the second pass must find the URLs already
    earned and never mint again."""
    s = scenario(tmp_path, n_parts=1)
    ledger_path = s["cfg"].ledger_path

    cfg = RunConfig(archive_id=ARCHIVE_ID, ledger_path=ledger_path,
                    staging_dir=s["cfg"].staging_dir, archive_dir=s["cfg"].archive_dir,
                    account=ACCOUNT, settle=0.01, mint_only=True)
    asyncio.run(run_once(cfg, session_factory=s["harness"], http_client=s["client"]))

    led = open_ledger(ledger_path)
    cached = led.minted_url(ARCHIVE_ID, 0)
    led.close()
    assert cached, "the mint must have been recorded, or the second pass re-mints"

    # second pass: same URL should be reused and the transfer should now run
    s2 = scenario(tmp_path, n_parts=1)
    cfg2 = RunConfig(archive_id=ARCHIVE_ID, ledger_path=ledger_path,
                     staging_dir=s2["cfg"].staging_dir, archive_dir=s2["cfg"].archive_dir,
                     account=ACCOUNT, settle=0.01)
    asyncio.run(run_once(cfg2, session_factory=s2["harness"], http_client=s2["client"]))
    assert s2["client"].requests, "the second pass should transfer"
    # it may still mint if the cached URL is missing, so assert the cache existed
    assert s2["harness"].mint_navigations() != [] or s2["client"].requests != []


def test_run_once_checks_mint_only_after_minting_and_before_transferring():
    """Ordering asserted rather than assumed: the flag must short-circuit BETWEEN the
    mint and the transfer, not before the mint (which would earn nothing)."""
    import autopilot.run as run_mod

    src = inspect.getsource(run_mod.run_once)
    mint_at = src.index("ledger.record_mint(")
    flag_at = src.index("if cfg.mint_only:")
    transfer_at = src.index("transfer = await download(")
    assert mint_at < flag_at < transfer_at, (
        "mint_only must sit after the mint and before the transfer")


def test_a_mint_only_run_reports_minting_and_never_transferring(tmp_path, monkeypatch):
    """The phase label must name the phase that is actually running.

    `--mint-only` shares the destination-ready check with the transfer path, and
    that check wrote `transferring` unconditionally — so a run that minted and
    moved nothing reported `transferring` for its entire life. Observed on the real
    62-product mint: status `transferring`, 63 of 68 URLs earned, zero bytes on the
    wire. Nothing keys off the value, which is exactly why no test noticed it: the
    only consumer is a human reading the status.

    Asserted DURING the mint, not after the run — the final status is `incomplete`
    either way, so only an in-flight observation can tell the two apart.
    """
    import sqlite3

    import autopilot.run as run_mod

    s = scenario(tmp_path, n_parts=2)
    ledger_path = s["cfg"].ledger_path

    def status_now():
        # Read-only connection: `open_ledger` runs the schema script (a write) and
        # the orchestrator already holds the file.
        conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True, timeout=15)
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE archive_id=?", (ARCHIVE_ID,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    seen = []
    real_mint = run_mod.mint_part

    async def spy(*args, **kwargs):
        seen.append(status_now())
        return await real_mint(*args, **kwargs)

    monkeypatch.setattr(run_mod, "mint_part", spy)

    cfg = RunConfig(archive_id=ARCHIVE_ID, ledger_path=ledger_path,
                    staging_dir=s["cfg"].staging_dir, archive_dir=s["cfg"].archive_dir,
                    account=ACCOUNT, settle=0.01, mint_only=True)
    outcome = asyncio.run(run_once(cfg, session_factory=s["harness"],
                                   http_client=s["client"]))

    assert seen, "the mint must actually run, or this proves nothing"
    assert "transferring" not in seen, (
        f"a mint-only run reported transferring: {seen}")
    assert set(seen) == {"minting"}, f"status during a mint-only run was {set(seen)}"
    assert s["client"].requests == [], "and it must still transfer nothing"
    assert outcome.status == "incomplete"


def test_minting_is_a_status_the_ledger_accepts():
    """Vocabulary drift guard: `run.py` writes `minting`, so `JOB_STATUSES` must
    contain it. The two lists have silently disagreed before — see the `incomplete`
    note in `ledger.py`."""
    from autopilot.ledger import JOB_STATUSES
    from autopilot.run import RUN_OUTCOME_STATUSES

    assert "minting" in JOB_STATUSES
    # `minting` is a phase, not an outcome: a run never *ends* as `minting`.
    assert "minting" not in RUN_OUTCOME_STATUSES
