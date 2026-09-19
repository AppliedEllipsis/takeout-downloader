"""Regression test: the report's attempts table must show REAL counts.

It printed zeros for everything until `build_report` gained an `attempts` parameter
and both callers started passing `ledger.attempts_by_kind(...)`. A table of zeros
reads as "no attempts spent", which is a claim, not a placeholder — and this
project's signature failure was printing numbers that are not measurements.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

PY_ROOT = r"D:\_projects\takeout_downloader_script\.claude\worktrees\takeout-autopilot"
if PY_ROOT not in sys.path:
    sys.path.insert(0, PY_ROOT)

from autopilot.ledger import AttemptKind, open_ledger
from autopilot.report import build_report


def run(coro):
    return asyncio.run(coro)


def test_attempts_table_reflects_real_counts(tmp_path):
    led = open_ledger(str(tmp_path / "s.db"))
    try:
        led.upsert_job("a1")
        led.upsert_parts("a1", [(0, "p0.zip", 100), (1, "p1.zip", 100)])
        # one mint, two transfers, one resume
        led.record_mint("a1", 0, "https://x/0")
        led.record_transfer("a1", 0, AttemptKind.TRANSFER, "complete", bytes_moved=100)
        led.record_transfer("a1", 1, AttemptKind.TRANSFER, "partial", bytes_moved=10)
        led.record_transfer("a1", 1, AttemptKind.RESUME, "complete", bytes_moved=90)

        counts = led.attempts_by_kind("a1")
        assert counts == {AttemptKind.MINT: 1, AttemptKind.TRANSFER: 2,
                          AttemptKind.RESUME: 1}

        md = build_report(led.parts("a1"), archive_id="a1",
                          attempts=counts).as_markdown()

        assert "| mint | 1 |" in md, md
        assert "| transfer | 2 |" in md, md
        assert "| resume | 1 |" in md, md
    finally:
        led.close()


def test_omitting_attempts_still_renders_but_says_zero(tmp_path):
    """Backwards compatible: no counts supplied renders zeros, not a crash."""
    led = open_ledger(str(tmp_path / "s.db"))
    try:
        led.upsert_job("a1")
        led.upsert_parts("a1", [(0, "p0.zip", 1)])
        md = build_report(led.parts("a1"), archive_id="a1").as_markdown()
        assert "| mint | 0 |" in md
    finally:
        led.close()


def test_dl_counts_can_be_supplied_directly(tmp_path):
    led = open_ledger(str(tmp_path / "s.db"))
    try:
        led.upsert_job("a1")
        led.upsert_parts("a1", [(0, "p0.zip", 1)])
        md = build_report(led.parts("a1"), archive_id="a1",
                          dl_counts={"takeout-x.zip": 4}).as_markdown()
        assert "takeout-x.zip" in md and "4" in md
        assert "NOT an invariant" in md
    finally:
        led.close()
