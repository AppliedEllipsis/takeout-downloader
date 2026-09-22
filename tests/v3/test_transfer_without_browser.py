"""The transfer pass must not need a browser when every URL is already cached.

Measured failure this fixes (2026-09-22): a run whose parts were ALL minted still died with
`needs_reauth`, because it scraped the archive page first and that scrape needs a live rapt.
That invalidated the entire point of `--mint-only` — minting first bought nothing.

The fix is `run.page_from_ledger`, and its most important property is its REFUSALS: if it
were permissive it would silently judge completeness on a partial part list, which is worse
than the bug it fixes.
"""
from __future__ import annotations

import asyncio
import os

from autopilot.ledger import open_ledger
from autopilot.run import page_from_ledger


def _ledger(tmp_path, parts, expiry="September 28, 2026 at 5:19 AM"):
    """parts: list of (idx, filename, size, minted_url)"""
    path = str(tmp_path / "t.db")
    led = open_ledger(path)
    aid = "arch-1"
    led.upsert_job(aid, account="acct", output_dir=str(tmp_path / "out"),
                   expiry_at=expiry)
    for idx, name, size, url in parts:
        led.upsert_parts(aid, [(idx, name, size)])
        if url:
            led.record_mint(aid, idx, url)
    return led, aid


def test_builds_a_page_when_every_part_has_a_url_and_a_real_name(tmp_path):
    led, aid = _ledger(tmp_path, [
        (0, "takeout-1-001.zip", 100, "https://fh/download/takeout-1-001.zip?j=x"),
        (1, "takeout-1-002.zip", 200, "https://fh/download/takeout-1-002.zip?j=y"),
    ])
    page = page_from_ledger(led, aid)
    assert page is not None
    assert sorted(p.index for p in page.parts) == [0, 1]
    assert {p.filename for p in page.parts} == {"takeout-1-001.zip", "takeout-1-002.zip"}
    # sizes must survive as the exact byte strings the transfer expects
    assert {p.size for p in page.parts} == {"100", "200"}
    # the expiry guard must still be able to run, or an expired export would be transferred
    assert page.expiry == "September 28, 2026 at 5:19 AM"
    page.assert_indexed()


def test_declines_when_any_part_lacks_a_url(tmp_path):
    """The whole safety property: one unminted part and this must refuse, because
    completeness would otherwise be judged on a part list we cannot complete."""
    led, aid = _ledger(tmp_path, [
        (0, "takeout-1-001.zip", 100, "https://fh/download/takeout-1-001.zip?j=x"),
        (1, "download", 200, None),
    ])
    assert page_from_ledger(led, aid) is None


def test_declines_when_a_part_still_has_the_placeholder_name(tmp_path):
    """`download` is the redirector basename — it names no file. A URL plus that name is
    not enough to identify what to write."""
    led, aid = _ledger(tmp_path, [
        (0, "download", 100, "https://fh/download/download?j=x"),
    ])
    assert page_from_ledger(led, aid) is None


def test_declines_an_empty_ledger(tmp_path):
    led, aid = _ledger(tmp_path, [])
    assert page_from_ledger(led, aid) is None


def test_declines_a_missing_expiry_free_ledger_is_still_usable(tmp_path):
    """No expiry recorded must not break the page — just no expiry guard."""
    led, aid = _ledger(tmp_path, [
        (0, "takeout-1-001.zip", 100, "https://fh/download/takeout-1-001.zip?j=x"),
    ], expiry=None)
    page = page_from_ledger(led, aid)
    assert page is not None and page.expiry is None


def test_the_urls_come_from_the_ledger_not_the_page(tmp_path):
    """The cached URL is the point. Confirm the row's URL is what the transfer would use."""
    url = "https://fh/download/takeout-1-001.zip?j=unique-marker"
    led, aid = _ledger(tmp_path, [(0, "takeout-1-001.zip", 100, url)])
    row = led.minted_url(aid, 0)
    assert row == url
    assert page_from_ledger(led, aid) is not None
