"""CLI: `python -m autopilot <run|report>`.

Exit codes are part of the interface (see `docs/v3/02-RUN-INTERFACE.md`) so a
shell or cron caller can branch **without parsing prose**:

    0  complete
    2  needs_reauth            — a human must satisfy the ReAuth challenge
    3  expired_unrecoverable   — the export is gone; request a NEW export
    1  anything else (incomplete, failed, bad usage)

`report` reads the ledger only: no browser, no network, no CDP. That matters
operationally — it is the command you can always run, including on a box where
the browser is wedged or the session is dead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from .ledger import LedgerOnFuseMount, open_ledger
from .report import build_report
from .run import RunConfig, run_once_sync

# NOTE: `run_once` is async. The CLI must call `run_once_sync`, which wraps it in
# asyncio.run. Calling `run_once` directly returns a coroutine and every later
# attribute access fails — measured live 2026-09-19:
#     AttributeError: 'coroutine' object has no attribute 'report_markdown'
#     RuntimeWarning: coroutine 'run_once' was never awaited
# The sync wrapper existed and was tested; nothing tested that the CLI *used* it.
# tests/v3/test_cli.py now exercises the CLI path itself.

EXIT_OK = 0
EXIT_OTHER = 1
EXIT_NEEDS_REAUTH = 2
EXIT_EXPIRED = 3

_EXIT_FOR_STATUS = {
    "complete": EXIT_OK,
    "needs_reauth": EXIT_NEEDS_REAUTH,
    "expired_unrecoverable": EXIT_EXPIRED,
}


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--archive-id", required=True,
                   help="the Takeout export id (the j= parameter)")
    p.add_argument("--ledger", required=True,
                   help="path to the ledger; MUST be local disk (a FUSE path is refused)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m autopilot",
        description="Browser-mints, transporter-moves Takeout downloader (v3).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run one pass over an export")
    _add_common(run)
    run.add_argument("--staging", required=True, help="local staging dir, e.g. /config/jobs/<id>")
    run.add_argument("--archive", required=True, help="final destination dir")
    run.add_argument("--account", default=None)
    run.add_argument("--cdp-http", default="http://127.0.0.1:9222")
    run.add_argument("--max-parts", type=int, default=None,
                     help="bound the work (smoke runs)")
    run.add_argument("--verify-hash", action="store_true",
                     help="hash-verify instead of the cheap header+EOCD check")
    run.add_argument("--allow-unmounted-archive", action="store_true",
                     help="skip the destination-is-a-mount assertion (DEV ONLY)")

    rep = sub.add_parser("report", help="print the completeness report from the ledger")
    _add_common(rep)
    rep.add_argument("--account", default=None)
    return ap


def cmd_run(args) -> int:
    cfg = RunConfig(
        archive_id=args.archive_id,
        ledger_path=args.ledger,
        staging_dir=args.staging,
        archive_dir=args.archive,
        account=args.account,
        cdp_http=args.cdp_http,
        max_parts=args.max_parts,
        verify_hash=args.verify_hash,
        require_mount=not args.allow_unmounted_archive,
    )
    outcome = run_once_sync(cfg)

    if args.json:
        print(json.dumps({
            "archive_id": outcome.archive_id, "status": outcome.status,
            "parts_expected": outcome.parts_expected, "parts_done": outcome.parts_done,
            "bytes_moved": outcome.bytes_moved, "error": outcome.error,
            "notes": outcome.notes,
        }, indent=2))
    else:
        print(outcome.report_markdown)
        if outcome.error:
            print(f"\n**error:** {outcome.error}\n")
    return _EXIT_FOR_STATUS.get(outcome.status, EXIT_OTHER)


def cmd_report(args) -> int:
    """Ledger-only. Works with no browser and no network."""
    ledger = open_ledger(args.ledger)
    try:
        job = ledger.job(args.archive_id)
        out = build_report(
            ledger.parts(args.archive_id),
            archive_id=args.archive_id,
            account=(job["account"] if job else args.account),
            status=(job["status"] if job else "pending"),
            expiry_at=(job["expiry_at"] if job else None),
            attempts=ledger.attempts_by_kind(args.archive_id),
        )
    finally:
        ledger.close()

    if args.json:
        print(json.dumps({
            "archive_id": out.archive_id, "verdict": out.verdict,
            "parts_expected": out.parts_expected, "parts_done": out.parts_done,
            "missing_indices": out.missing_indices,
            "partial_indices": out.partial_indices,
            "failed_indices": out.failed_indices,
            "expired": out.expired, "warnings": out.warnings,
        }, indent=2))
    else:
        print(out.as_markdown())
    # A missing/expired export is not a usage error, but a caller may want to know.
    return EXIT_OK if out.complete else EXIT_OTHER


def _make_output_unicode_safe() -> None:
    """Stop the CLI crashing on a non-UTF-8 console.

    Measured 2026-09-19: `python -m autopilot report` raised
    `UnicodeEncodeError: 'charmap' codec can't encode character '\u0394'` on a
    Windows cp1252 console, because the report markdown deliberately uses `Δ1` /
    `Δ0` for measured attempt costs. The primary output path of the CLI failed on
    the machine it was written on.

    Reconfiguring to UTF-8 with `errors="replace"` fixes it everywhere: a UTF-8
    consumer gets the real characters, and a legacy console gets a substitute
    rather than a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Optional[list] = None) -> int:
    _make_output_unicode_safe()
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "run":
            return cmd_run(args)
        if args.cmd == "report":
            return cmd_report(args)
    except LedgerOnFuseMount as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_OTHER
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_OTHER
    return EXIT_OTHER


if __name__ == "__main__":
    sys.exit(main())
