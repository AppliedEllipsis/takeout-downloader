"""Offline tests for the archive-page scrape.

The two that matter most are the `assert_indexed` cases: they guard the regression
that broke v2's planning. `capturePayload` keyed its maps by the **filename**
parsed out of `data-download-uri`, and when every part's URL reused one basename
the maps collapsed to a single entry — parts then got `url=None` and the engine
booked a NETWORK_ERROR attempt without ever sending a request. The older v1
scraper keyed by `i=` and did not have the bug. v3 keys by `i=` and asserts it.

No pytest plugins required — async cases run via `asyncio.run`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from autopilot.cdp import CdpSession
from autopilot.errors import AutopilotError
from autopilot.scrape import ArchivePage, PartLink, archive_url, read_archive
from fake_cdp import FakeTransport

ARCHIVE_ID = "f470fe32-76d6-43a6-9321-dbc76834919e"
USER = "116943325794238708860"
URL = archive_url(ARCHIVE_ID)


def run(coro):
    return asyncio.run(coro)


def payload(**over) -> str:
    base = {
        "url": URL,
        "title": "Google Takeout",
        "parts": [
            {"raw_uri": f"takeout/download?j={ARCHIVE_ID}&i=0&user={USER}&rapt=R0",
             "index": 0, "archive_id": ARCHIVE_ID, "user": USER, "has_rapt": True,
             "size": "261259", "aria": "", "filename": "x"},
            {"raw_uri": f"takeout/download?j={ARCHIVE_ID}&i=1&user={USER}&rapt=R0",
             "index": 1, "archive_id": ARCHIVE_ID, "user": USER, "has_rapt": True,
             "size": "500000", "aria": "Download again part 2 of 2", "filename": "y"},
        ],
        "dl_counts": {"takeout-20260919T045820Z-1-001.zip": 2},
        "expiry": "September 26, 2026 at 4:58 AM",
        "status": "Completed",
        "challenged": False,
        "part_of_N": [2],
    }
    base.update(over)
    return json.dumps(base)


async def _read(transport, url=URL):
    async with CdpSession(transport) as session:
        return await read_archive(session, url, settle=0.02)


def test_read_archive_parses_parts_and_resolves_relative_uris():
    t = FakeTransport(values=[payload()])
    page = run(_read(t))
    assert len(page.parts) == 2
    assert page.parts[0].index == 0 and page.parts[1].index == 1
    # the relative attribute was resolved against the page origin
    assert page.parts[0].redirector.startswith("https://takeout.google.com/takeout/download?")
    assert page.dl_counts["takeout-20260919T045820Z-1-001.zip"] == 2
    assert page.expiry == "September 26, 2026 at 4:58 AM"
    assert page.status == "Completed"
    assert page.part_of_N == [2]
    assert page.indices == [0, 1]
    page.assert_indexed()  # must not raise


def test_assert_indexed_rejects_a_part_with_no_index():
    page = ArchivePage(parts=[PartLink(index=0, raw_uri="a"),
                              PartLink(index=None, raw_uri="b")])
    with pytest.raises(AutopilotError) as e:
        page.assert_indexed()
    assert "no parseable i= index" in str(e.value)


def test_assert_indexed_rejects_duplicate_indices():
    # This is the v2 filename-collapse shape: several parts, one key.
    page = ArchivePage(parts=[PartLink(index=0, raw_uri="a"),
                              PartLink(index=0, raw_uri="b")])
    with pytest.raises(AutopilotError) as e:
        page.assert_indexed()
    assert "duplicate" in str(e.value)


def test_read_archive_reports_a_challenge_page_instead_of_pretending():
    t = FakeTransport(values=[payload(
        parts=[], url="https://accounts.google.com/v3/signin/challenge/pwd?TL=x",
        challenged=True)])
    page = run(_read(t))
    assert page.challenged is True
    assert page.parts == []


def test_read_archive_raises_when_the_page_returns_nothing():
    t = FakeTransport(values=[None])
    with pytest.raises(AutopilotError):
        run(_read(t))


def test_archive_url_points_at_the_detail_page_not_the_list():
    # Measured trap: download links live on /manage/archive/<id>, NOT on /manage.
    assert URL == f"https://takeout.google.com/manage/archive/{ARCHIVE_ID}"
    assert "/manage/archive/" in URL
    assert not URL.endswith("/manage")
