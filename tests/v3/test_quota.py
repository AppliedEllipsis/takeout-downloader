"""`quotaExceeded=true` is a terminal, named condition — not an opaque mint error.

Measured 2026-09-19 on a live export that had reached its allowance:

    .../settings/takeout/download?...&download=true&rapt=...
      -> [302] .../manage/archive/<id>?download=true&rapt=...&quotaExceeded=true

and the page says it in words: "You can try to download a file only 5 times."

Before this existed, the condition surfaced as a generic `MintError` reading
"no file-host URL, no ReAuth, and no archive bounce" — wrong on its own terms,
since the chain was nothing but bounces, and wrong about the remedy, since the
cure is a new export rather than a retry or a re-auth.

All offline.
"""
import asyncio

import pytest

from autopilot.errors import AutopilotError, MintError, NeedsReauth, QuotaExceeded
from autopilot.mint import QUOTA_FLAG, extract_redirects

AID = "f470fe32-76d6-43a6-9321-dbc76834919e"
BOUNCE_WITH_QUOTA = (
    f"https://takeout.google.com/manage/archive/{AID}"
    "?download=true&rapt=AEjHL4P8&quotaExceeded=true"
)


def _redirect_event(from_url, location):
    """A `Network.responseReceived` for a 3xx, as Chrome reports it."""
    return ("Network.responseReceived", {
        "response": {"url": from_url, "status": 302,
                     "headers": {"location": location}},
    })


# --------------------------------------------------------------------------
# the type itself
# --------------------------------------------------------------------------
def test_quota_error_is_not_a_reauth_error():
    """Conflating these was the whole bug: they have opposite remedies."""
    exc = QuotaExceeded(BOUNCE_WITH_QUOTA, "Google sent quotaExceeded=true")
    assert isinstance(exc, AutopilotError)
    assert not isinstance(exc, NeedsReauth)


def test_quota_error_says_the_remedy_is_a_new_export():
    msg = str(QuotaExceeded(BOUNCE_WITH_QUOTA))
    assert "allowance is exhausted" in msg
    assert "NEW export" in msg
    assert "no retry" in msg.lower() or "no retry of this one" in msg


def test_quota_error_is_a_mint_error_so_existing_handlers_still_catch_it():
    """Structural safety: a caller that only knows MintError must not crash
    with an unhandled type."""
    assert issubclass(QuotaExceeded, MintError)


def test_the_flag_is_the_measured_literal():
    assert QUOTA_FLAG == "quotaExceeded=true"


# --------------------------------------------------------------------------
# detection, via the real extraction path
# --------------------------------------------------------------------------
def test_extract_redirects_surfaces_the_quota_bounce():
    events = [_redirect_event("https://takeout.google.com/settings/takeout/download?j=x",
                              BOUNCE_WITH_QUOTA)]
    redirects = extract_redirects(events)
    assert redirects, "the bounce must be extracted or detection is impossible"
    assert any(QUOTA_FLAG in (r.location or "") for r in redirects)


def test_detection_does_not_fire_on_an_ordinary_archive_bounce():
    """A plain refreshed-token bounce must keep working — it is the retry path."""
    clean = f"https://takeout.google.com/manage/archive/{AID}?user=1&rapt=AEjHL4P8&j={AID}"
    events = [_redirect_event("https://takeout.google.com/takeout/download?j=x", clean)]
    redirects = extract_redirects(events)
    assert not any(QUOTA_FLAG in (r.location or "") for r in redirects)


def test_detection_does_not_fire_on_a_file_host_location():
    good = "https://takeout-download.usercontent.google.com/download/takeout-x.zip?j=x"
    events = [_redirect_event("https://takeout.google.com/takeout/download?j=x", good)]
    redirects = extract_redirects(events)
    assert not any(QUOTA_FLAG in (r.location or "") for r in redirects)


def test_quota_check_precedes_the_refresh_retry_in_source():
    """The ordering is load-bearing, so it is asserted rather than assumed.

    The quota bounce CARRIES a rapt, so the refreshed-token retry below would
    happily consume it and loop to the hop limit — reporting a hop-limit error
    that names a completely different cause.
    """
    import inspect

    import autopilot.mint as mint_mod

    src = inspect.getsource(mint_mod.mint)
    quota_at = src.index("QUOTA_FLAG in")
    refresh_at = src.index('"/manage/archive/" in r.location')
    assert quota_at < refresh_at, (
        "the quota check must run BEFORE the refreshed-rapt retry, otherwise a "
        "spent export looks like a token-refresh loop")
