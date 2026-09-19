"""Offline tests for the mint protocol.

Each test corresponds to a measurement. Where a test exists specifically to stop a
regression that already cost this project real time, the docstring says so.

**No pytest plugins required.** Async cases are driven with `asyncio.run` inside
plain sync tests, so the suite runs on any interpreter with pytest — which is the
point, given this project's habit of passing tests in an environment that cannot
run the code.
"""
from __future__ import annotations

import asyncio

import pytest

from autopilot.cdp import CdpSession
from autopilot.errors import HopLimitExceeded, MintError, NeedsReauth
from autopilot.mint import (
    FILE_HOST,
    ORIGIN,
    absolute_uri,
    build_redirector,
    extract_redirects,
    mint,
    parse_query,
)
from fake_cdp import FakeTransport, redirect_event, response_event

ARCHIVE_ID = "f470fe32-76d6-43a6-9321-dbc76834919e"
USER = "116943325794238708860"
RAPT = "AEjHL4MkL0nNJQx5Ugb0UsKyyCKdJS"
FRESH_RAPT = "AEjHL4MFRESHtokenB0UsKyyCKdJS999"

REDIRECTOR = build_redirector(ARCHIVE_ID, 0, USER, RAPT)
FILE_URL = (
    f"https://{FILE_HOST}/download/takeout-20260919T045820Z-1-001.zip"
    f"?j={ARCHIVE_ID}&i=0&user=1005482974000&authuser=0"
)
ARCHIVE_REFRESH_URL = (
    f"{ORIGIN}/manage/archive/{ARCHIVE_ID}?user={USER}&rapt={FRESH_RAPT}&j={ARCHIVE_ID}"
)
REBUILT_REDIRECTOR = build_redirector(ARCHIVE_ID, 0, USER, FRESH_RAPT)
CHALLENGE_URL = (
    "https://accounts.google.com/v3/signin/challenge/pwd"
    "?TL=ACv9tzEyMERAmxQTT5X-GlF72fkOYy-6-aWG2NAzW708vKurQVt8kHCKqcZnlr5e&authuser=0"
)

FAST = dict(settle=0.05, navigate_timeout=2)


def run(coro):
    """Drive a coroutine to completion without needing pytest-asyncio."""
    return asyncio.run(coro)


async def _mint_once(transport, url=REDIRECTOR, **kw):
    params = {**FAST, **kw}
    async with CdpSession(transport) as session:
        return await mint(session, url, **params)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def test_absolute_uri_resolves_a_relative_data_download_uri():
    # Measured: the attribute is relative ("takeout/download?j=…"), despite the
    # v2 docs calling it an "exact per-part URL".
    assert absolute_uri("takeout/download?j=x&i=0") == f"{ORIGIN}/takeout/download?j=x&i=0"
    assert absolute_uri("/takeout/download?j=x") == f"{ORIGIN}/takeout/download?j=x"
    assert absolute_uri("https://already/absolute") == "https://already/absolute"
    assert absolute_uri("") == ""


def test_parse_query_takes_the_first_value_and_drops_blanks():
    got = parse_query(f"https://x/y?j={ARCHIVE_ID}&i=0&user={USER}&empty=")
    assert got == {"j": ARCHIVE_ID, "i": "0", "user": USER}


def test_build_redirector_shape_matches_what_google_emits():
    url = build_redirector(ARCHIVE_ID, 0, USER, RAPT)
    assert url.startswith(f"{ORIGIN}/takeout/download?")
    assert f"j={ARCHIVE_ID}" in url and "i=0" in url and f"user={USER}" in url
    assert "rapt=" in url
    # Measured: the redirector carries NO authuser; authuser belongs to the
    # file-host URL. (The v2 docs claim otherwise — see measured-facts.md.)
    assert "authuser" not in url


def test_redact_keeps_tokens_out_of_logs():
    from autopilot.mint import _redact

    assert RAPT not in _redact(REDIRECTOR)
    assert "<redacted>" in _redact(REDIRECTOR)


# ---------------------------------------------------------------------------
# Redirect extraction
# ---------------------------------------------------------------------------
def test_extract_redirects_from_the_redirectResponse_form():
    events = [("Network.requestWillBeSent",
               redirect_event(REDIRECTOR, FILE_URL)["params"])]
    got = extract_redirects(events)
    assert len(got) == 1
    assert got[0].status == 302 and got[0].location == FILE_URL


def test_extract_redirects_from_the_responseReceived_form():
    events = [("Network.responseReceived",
               response_event(REDIRECTOR, 302, FILE_URL)["params"])]
    got = extract_redirects(events)
    assert len(got) == 1 and got[0].location == FILE_URL


def test_extract_redirects_deduplicates_the_two_forms():
    ev = redirect_event(REDIRECTOR, FILE_URL)
    events = [(ev["method"], ev["params"]), (ev["method"], ev["params"])]
    assert len(extract_redirects(events)) == 1


def test_extract_redirects_ignores_events_without_a_location():
    events = [("Network.responseReceived", response_event(FILE_URL, 200)["params"])]
    assert extract_redirects(events) == []


# ---------------------------------------------------------------------------
# mint()
# ---------------------------------------------------------------------------
def test_mint_direct_hit_returns_the_file_host_url():
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, FILE_URL)]})
    result = run(_mint_once(t))
    assert result.url == FILE_URL
    assert result.hops == 1
    assert result.refreshed_rapt is False
    assert result.attempts_spent == 1


def test_mint_accepts_a_file_host_response_without_a_location():
    t = FakeTransport(plan={REDIRECTOR: [response_event(FILE_URL, 200)]})
    assert run(_mint_once(t)).url == FILE_URL


def test_mint_follows_the_rapt_refresh_and_uses_the_fresh_token():
    # Measured: a stale rapt yields 302 -> archive page carrying a NEW rapt.
    t = FakeTransport(plan={
        REDIRECTOR: [redirect_event(REDIRECTOR, ARCHIVE_REFRESH_URL)],
        REBUILT_REDIRECTOR: [redirect_event(REBUILT_REDIRECTOR, FILE_URL)],
    })
    result = run(_mint_once(t))
    assert result.url == FILE_URL
    assert result.hops == 2
    assert result.refreshed_rapt is True
    navs = t.navigated()
    assert len(navs) == 2
    assert FRESH_RAPT in navs[1]      # the rebuilt redirector carried the NEW token
    assert RAPT not in navs[1]


def test_mint_raises_needs_reauth_on_the_password_challenge():
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, CHALLENGE_URL)]})
    with pytest.raises(NeedsReauth) as e:
        run(_mint_once(t))
    assert "challenge/pwd" in e.value.url


def test_mint_raises_needs_reauth_on_servicelogin():
    login = f"https://accounts.google.com/ServiceLogin?continue={REDIRECTOR}"
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, login)]})
    with pytest.raises(NeedsReauth):
        run(_mint_once(t))


def test_mint_stops_at_the_hop_limit_instead_of_looping_forever():
    # A bounce that never converges must terminate: the tab-storm failure mode
    # (1.9) was exactly an unbounded retry loop.
    t = FakeTransport(plan={
        REDIRECTOR: [redirect_event(REDIRECTOR, ARCHIVE_REFRESH_URL)],
        REBUILT_REDIRECTOR: [redirect_event(REBUILT_REDIRECTOR, ARCHIVE_REFRESH_URL)],
    })
    with pytest.raises(HopLimitExceeded):
        run(_mint_once(t, max_hops=3))


def test_mint_reports_an_unrecognisable_chain_rather_than_guessing():
    t = FakeTransport(plan={REDIRECTOR: []})
    with pytest.raises(MintError):
        run(_mint_once(t))


# ---------------------------------------------------------------------------
# THE INVARIANT
# ---------------------------------------------------------------------------
def test_mint_never_enables_or_uses_the_fetch_domain():
    """Aborting a request PREVENTS the mint — measured, and this is the guard.

    A control run proved it: navigating the redirector plainly minted, while the
    same navigation under `Fetch` interception with `failRequest` bounced to the
    archive page and never reached the file host. If someone later "optimises"
    the mint by intercepting to save bytes, this test fails and points at the
    measurement that forbids it.
    """
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, FILE_URL)]})
    run(_mint_once(t))
    used = t.methods()
    assert used, "no commands were sent at all"
    offenders = [m for m in used if m.startswith("Fetch.")]
    assert offenders == [], (
        "the mint must never touch the Fetch domain — aborting a request breaks "
        f"the mint (measured 2026-09-19). Saw: {offenders}"
    )


def test_mint_enables_network_so_the_redirect_chain_is_observed():
    t = FakeTransport(plan={REDIRECTOR: [redirect_event(REDIRECTOR, FILE_URL)]})
    run(_mint_once(t))
    assert "Network.enable" in t.methods()
