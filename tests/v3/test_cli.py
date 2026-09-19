"""CLI-level tests: the wiring, not the functions.

Two bugs escaped the 117-test suite because every test called a *function* and
none called the *CLI*:

  * `cmd_run` called the async `run_once(cfg)` without awaiting it, so the first
    real invocation died with
        AttributeError: 'coroutine' object has no attribute 'report_markdown'
    `run_once_sync` existed and was tested; nothing tested that the CLI used it.
  * the report's attempts table printed zeros because the plumbing from ledger to
    report was missing — again invisible from inside any single function.

So these tests drive `main()` the way an operator does, and assert on exit codes,
which are part of the frozen interface.
"""
from __future__ import annotations

import json
import sys

import pytest

ROOT = r"D:\_projects\takeout_downloader_script\.claude\worktrees\takeout-autopilot"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import autopilot.__main__ as main_mod  # noqa: E402
from autopilot.run import RunOutcome  # noqa: E402


def _outcome(status: str) -> RunOutcome:
    return RunOutcome(archive_id="arch-1", status=status, parts_expected=2,
                      parts_done=(2 if status == "complete" else 0),
                      report_markdown=f"# report\n\n**Verdict: {status}**\n")


def _run_args(tmp_path) -> list:
    return ["run", "--archive-id", "arch-1",
            "--ledger", str(tmp_path / "s.db"),
            "--staging", str(tmp_path / "staging"),
            "--archive", str(tmp_path / "archive")]


def test_cli_run_awaits_and_prints_the_report(tmp_path, monkeypatch, capsys):
    """The exact regression: unawaited run_once produced a coroutine, not a result."""
    monkeypatch.setattr(main_mod, "run_once_sync", lambda cfg, **kw: _outcome("complete"))

    code = main_mod.main(_run_args(tmp_path))

    out = capsys.readouterr().out
    assert code == main_mod.EXIT_OK, f"complete should exit 0, got {code}"
    assert "# report" in out, "the report must be printed"
    assert "Verdict: complete" in out


@pytest.mark.parametrize("status,expected", [
    ("complete", 0),
    ("needs_reauth", 2),
    ("expired_unrecoverable", 3),
    ("incomplete", 1),
    ("failed", 1),
])
def test_cli_exit_codes_match_the_frozen_interface(status, expected, tmp_path,
                                                   monkeypatch, capsys):
    """Exit codes are the interface a cron caller branches on."""
    monkeypatch.setattr(main_mod, "run_once_sync", lambda cfg, **kw: _outcome(status))
    code = main_mod.main(_run_args(tmp_path))
    capsys.readouterr()
    assert code == expected, f"{status} should exit {expected}, got {code}"


def test_cli_run_json_mode_emits_parseable_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "run_once_sync", lambda cfg, **kw: _outcome("needs_reauth"))
    code = main_mod.main(_run_args(tmp_path) + ["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == main_mod.EXIT_NEEDS_REAUTH
    assert payload["status"] == "needs_reauth"


def test_cli_report_reads_the_ledger_and_reflects_real_attempt_counts(tmp_path, capsys):
    """Ledger-only path: no browser, no monkeypatching of the run."""
    from autopilot.ledger import AttemptKind, open_ledger

    led = open_ledger(str(tmp_path / "s.db"))
    led.upsert_job("arch-1")
    led.upsert_parts("arch-1", [(0, "p0.zip", 100), (1, "p1.zip", 100)])
    led.set_part_status("arch-1", 0, "done")
    led.record_mint("arch-1", 1, "https://x/1")
    led.record_transfer("arch-1", 0, AttemptKind.TRANSFER, "complete", bytes_moved=100)
    led.close()

    code = main_mod.main(["report", "--archive-id", "arch-1",
                          "--ledger", str(tmp_path / "s.db")])
    out = capsys.readouterr().out
    assert code == main_mod.EXIT_OTHER          # 1 of 2 parts is not complete
    assert "| mint | 1 |" in out, out           # the counts really reach the report
    assert "| transfer | 1 |" in out, out
    assert "1/2" in out


def test_cli_reports_a_missing_ledger_gracefully(tmp_path, capsys):
    """A bad path must be an error message, not a traceback."""
    code = main_mod.main(["report", "--archive-id", "nope",
                          "--ledger", str(tmp_path / "does-not-exist" / "s.db")])
    captured = capsys.readouterr()
    assert code == main_mod.EXIT_OTHER
    assert "Traceback" not in (captured.out + captured.err)
