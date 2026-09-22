"""A mint must not leave a real browser download running.

Failure mode **1.22**, measured 2026-09-22. A mint navigate ends at a
`Content-Disposition: attachment` response, so Chrome starts a REAL download of the
part. The extension's `autoCancelDownloads` used to dispose of it; that switch is now
OFF by owner directive (the workflow depends on real browser downloads), so nothing
does. A 68-part mint left **15.4 GB** in `/config/Downloads`, and this project is now
aimed at 500 GB-2 TB exports against ~198 GB free on the staging volume.

The design is **cancel-after-capture**, and the alternative is why:

* Denying downloads up front (`Browser.setDownloadBehavior{behavior:"deny"}`) would be
  simpler, but aborting a request is *measured* to PREVENT the mint, and a denied
  download is a form of abort. A change that fixes disk hygiene by breaking the mint
  is not a fix.
* Cancelling after the URL is captured cannot affect the mint at all, because the value
  we came for is already in hand.

Measured end to end on a 9.88 GB part: 581,397,203 bytes into its download,
`Browser.cancelDownload(guid)` took the progress state to `canceled`, the partial file
disappeared, and the byte count and free space returned exactly to their pre-download
values.

These tests pin the parts that are easy to get subtly wrong: that the cancel happens on
**every** exit path (not just success), that asking for the events does not move a
human's downloads, that the request is never aborted, and that a browser which refuses
the whole `Browser` domain still mints.

Offline. No pytest plugins: async cases run through `asyncio.run`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from autopilot.cdp import CdpSession
from autopilot.errors import NeedsReauth
from autopilot.mint import (
    DOWNLOAD_BEGIN_EVENT,
    FILE_HOST,
    ORIGIN,
    _stray_download_guids,
    build_redirector,
    mint,
)
from fake_cdp import FakeTransport, redirect_event, response_event

ARCHIVE_ID = "29462f3d-4d75-4196-b35c-8a1dbecf617b"
USER = "116943325794238708860"
RAPT = "AEjHL4MkL0nNJQx5Ugb0UsKyyCKdJS"

REDIRECTOR = build_redirector(ARCHIVE_ID, 0, USER, RAPT)
FILE_URL = (
    f"https://{FILE_HOST}/download/takeout-20260919T043451Z-3-006.zip"
    f"?j={ARCHIVE_ID}&i=67&user=1005482974000&authuser=0"
)
CHALLENGE_URL = (
    "https://accounts.google.com/v3/signin/challenge/pwd"
    "?TL=ACv9tzEyMERAmxQTT5X-GlF72fkOYy-6-aWG2NAzW708vKurQVt8kHCKqcZnlr5e&authuser=0"
)
GUID = "0d435f54-4515-497f-beff-7d035893eb3c"

FAST = dict(settle=0.05, navigate_timeout=2)


def download_began(guid=GUID, filename="takeout-20260919T043451Z-3-006.zip"):
    """`Browser.downloadWillBegin` — what the browser emits when it starts downloading."""
    return {
        "method": DOWNLOAD_BEGIN_EVENT,
        "params": {"frameId": "F1", "guid": guid, "url": FILE_URL,
                   "suggestedFilename": filename},
    }


def plan_with_download(extra=None):
    return {REDIRECTOR: [redirect_event(REDIRECTOR, FILE_URL), download_began()] + (extra or [])}


def run(coro):
    return asyncio.run(coro)


async def _mint_once(transport, url=REDIRECTOR, **kw):
    params = {**FAST, **kw}
    async with CdpSession(transport) as session:
        return await mint(session, url, **params)


def sent_calls(transport, method):
    return [json.loads(s)["params"] for s in transport.sent
            if json.loads(s).get("method") == method]


def cancelled_guids(transport):
    return [p.get("guid") for p in sent_calls(transport, "Browser.cancelDownload")]


class RefusingBrowser(FakeTransport):
    """A browser (or session) that refuses the whole `Browser` domain."""

    def __init__(self, refuse_prefix="Browser.", **kw):
        super().__init__(**kw)
        self.refuse_prefix = refuse_prefix

    async def send(self, payload):
        method = json.loads(payload).get("method", "")
        if method.startswith(self.refuse_prefix):
            self.sent.append(payload)
            await self._queue().put(json.dumps(
                {"id": json.loads(payload).get("id"),
                 "error": {"code": -32601, "message": "method not found"}}))
            return
        await super().send(payload)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_mint_cancels_the_download_it_started():
    t = FakeTransport(plan=plan_with_download())
    result = run(_mint_once(t))
    assert result.url == FILE_URL
    assert cancelled_guids(t) == [GUID], (
        "the download this mint started must be cancelled, or it runs to completion "
        "into /config/Downloads")


def test_the_download_is_cancelled_exactly_once():
    """`_leave` clears the list, so a later exit path cannot cancel a stale guid."""
    t = FakeTransport(plan=plan_with_download())
    run(_mint_once(t))
    assert len(cancelled_guids(t)) == 1


def test_a_file_host_response_without_a_location_also_cancels():
    """The second success path (`file_host_hits`) is a different `return` statement,
    so it needs its own coverage — that is exactly how one of six exits gets missed."""
    t = FakeTransport(plan={REDIRECTOR: [response_event(FILE_URL, 200), download_began()]})
    assert run(_mint_once(t)).url == FILE_URL
    assert cancelled_guids(t) == [GUID]


# ---------------------------------------------------------------------------
# Every exit path, not just success
# ---------------------------------------------------------------------------
def test_a_failed_mint_still_cancels_its_download():
    """The download does not care why the mint stopped."""
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, CHALLENGE_URL),
                                         download_began()]})
    with pytest.raises(NeedsReauth):
        run(_mint_once(t))
    assert cancelled_guids(t) == [GUID]


def test_a_hop_limited_mint_still_cancels_its_download():
    """The chain that never converges: a download started on hop 1 would otherwise
    survive a mint that failed on the last hop.

    The bounce must carry BOTH a live `rapt` and `j`, because that is what makes
    branch 3 rebuild the redirector and try again — without them the chain falls
    through to `MintError` instead of exhausting the hop limit.
    """
    from autopilot.errors import HopLimitExceeded

    archive_bounce = (
        f"{ORIGIN}/manage/archive/{ARCHIVE_ID}?user={USER}&rapt={RAPT}&j={ARCHIVE_ID}")
    # The rebuilt redirector is byte-identical to the one that bounced, so every hop
    # loops back to the same place and the hop limit is genuinely reached.
    assert build_redirector(ARCHIVE_ID, 0, USER, RAPT) == REDIRECTOR
    t = FakeTransport(plan={
        REDIRECTOR: [redirect_event(REDIRECTOR, archive_bounce), download_began()],
    })
    with pytest.raises(HopLimitExceeded):
        run(_mint_once(t, max_hops=2))
    # `set(...)`, not a list: the fake replays the same guid for every navigate to the
    # same URL, while a real browser issues a fresh guid per download. What matters is
    # that our download was cancelled while leaving a mint that failed.
    assert set(cancelled_guids(t)) == {GUID}


def test_every_hop_that_starts_a_download_gets_its_own_cancel():
    """A multi-hop mint can start MORE than one download — the redirector navigate and
    the rebuilt-redirector navigate are two separate navigations. Cancelling only the
    last guid would leave the first download running."""
    guid2 = "82bfb227-0f90-4369-be5a-303dfafde231"
    fresh = "AEjHL4MFRESHtokenB0UsKyyCKdJS999"
    archive_bounce = (
        f"{ORIGIN}/manage/archive/{ARCHIVE_ID}?user={USER}&rapt={fresh}&j={ARCHIVE_ID}")
    rebuilt = build_redirector(ARCHIVE_ID, 0, USER, fresh)
    t = FakeTransport(plan={
        REDIRECTOR: [redirect_event(REDIRECTOR, archive_bounce), download_began(GUID)],
        rebuilt: [redirect_event(rebuilt, FILE_URL), download_began(guid2)],
    })
    assert run(_mint_once(t)).url == FILE_URL
    assert sorted(cancelled_guids(t)) == sorted([GUID, guid2])


# ---------------------------------------------------------------------------
# The event request itself
# ---------------------------------------------------------------------------
def test_download_events_are_requested_without_moving_a_humans_downloads():
    """`behavior: "default"` and no `downloadPath`. Setting a path would silently
    redirect the owner's own downloads, which is the very thing the owner directive
    was protecting."""
    t = FakeTransport(plan=plan_with_download())
    run(_mint_once(t))
    calls = sent_calls(t, "Browser.setDownloadBehavior")
    assert len(calls) == 1, "asked for once, not once per hop"
    params = calls[0]
    assert params["behavior"] == "default"
    assert params["eventsEnabled"] is True
    assert "downloadPath" not in params, (
        "a downloadPath would move a human's downloads — we only want the events")


def test_download_events_are_enabled_before_the_first_navigate():
    t = FakeTransport(plan=plan_with_download())
    run(_mint_once(t))
    methods = t.methods()
    assert methods.index("Browser.setDownloadBehavior") < methods.index("Page.navigate"), (
        "the request must precede the navigate that starts the download")


def test_the_cleanup_is_not_implemented_by_aborting_the_request():
    """Measured: aborting a request PREVENTS the mint. So the cleanup must be a cancel
    after the fact, never a `Fetch` interception."""
    t = FakeTransport(plan=plan_with_download())
    run(_mint_once(t))
    assert not [m for m in t.methods() if m.startswith("Fetch.")], (
        "the Fetch domain is deliberately not used — aborting breaks the mint")


# ---------------------------------------------------------------------------
# Degrading safely
# ---------------------------------------------------------------------------
def test_a_browser_that_refuses_download_events_still_mints():
    """Losing the events is a hygiene failure, not a mint failure. The minted URL is
    what the caller asked for and must still come back."""
    t = RefusingBrowser(refuse_prefix="Browser.setDownloadBehavior",
                        plan=plan_with_download())
    result = run(_mint_once(t))
    assert result.url == FILE_URL
    assert cancelled_guids(t) == [], "nothing was addressable, so nothing to cancel"


def test_a_cancel_that_fails_does_not_fail_the_mint():
    """A refused cancel leaves a stray file; `tools/sweep_downloads.py` clears it. It
    must not cost the caller the URL it already earned."""
    t = RefusingBrowser(refuse_prefix="Browser.cancelDownload",
                        plan=plan_with_download())
    result = run(_mint_once(t))
    assert result.url == FILE_URL
    assert cancelled_guids(t) == [GUID], "the attempt is still made"


def test_the_whole_browser_domain_being_absent_still_mints():
    """Worst case: an older build with no `Browser.setDownloadBehavior` at all."""
    t = RefusingBrowser(refuse_prefix="Browser.", plan=plan_with_download())
    assert run(_mint_once(t)).url == FILE_URL


# ---------------------------------------------------------------------------
# guid extraction
# ---------------------------------------------------------------------------
def test_only_real_download_events_yield_guids():
    events = [
        ("Network.requestWillBeSent", {"request": {"url": "x"}}),
        ("Browser.downloadProgress", {"guid": "progress-not-begin"}),  # wrong event
        (DOWNLOAD_BEGIN_EVENT, {}),                                     # no guid
        (DOWNLOAD_BEGIN_EVENT, {"guid": GUID}),
        (DOWNLOAD_BEGIN_EVENT, {"guid": "second-guid"}),
    ]
    assert _stray_download_guids(events) == [GUID, "second-guid"]


def test_a_redirect_only_mint_collects_no_guids():
    """No download event means nothing to cancel — and no cancel call either."""
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, FILE_URL)]})
    assert run(_mint_once(t)).url == FILE_URL
    assert cancelled_guids(t) == []
