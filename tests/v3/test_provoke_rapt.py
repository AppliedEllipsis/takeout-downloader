"""`provoke_rapt` — earning a token, and why sign-in alone was not enough.

Measured 2026-09-19: after a full sign-in the archive page STILL served untokened
links, and the run correctly refused with `needs_reauth` — spending nothing, but also
unable to proceed. The token is not something you have; it is something you *earn* by
navigating the redirector once:

    takeout/download?j=<id>&i=0&user=<uid>       (no rapt)
      -> 302 -> manage/archive/<id>?user=...&pli=1&rapt=<fresh>

That is how the project's first successful run happened — by hand at the time.

Offline; the CDP session is a small fake.
"""
import pytest

from autopilot.scrape import provoke_rapt

ARCHIVE = "f9a17be0-65e2-4b1f-a9c6-04c840204419"
REDIRECTOR = (f"https://takeout.google.com/takeout/download"
              f"?j={ARCHIVE}&i=0&user=116943325794238708860")
TOKENED = (f"https://takeout.google.com/manage/archive/{ARCHIVE}"
           f"?user=116943325794238708860&pli=1&rapt=AEjHL4P8fresh")
SIGNIN = "https://accounts.google.com/v3/signin/challenge/pwd?TL=ACv9tz"


class FakeSession:
    """Just enough CDP for this function: navigate, drain, read location."""

    def __init__(self, landed, *, raise_on_value=False):
        self.landed = landed
        self.calls = []
        self.raise_on_value = raise_on_value

    async def call(self, method, params=None, **kw):
        self.calls.append((method, params))
        return {}

    async def value(self, expr):
        if self.raise_on_value:
            raise RuntimeError("session died")
        return self.landed if expr == "location.href" else None

    async def drain(self, seconds, **kw):
        return None

    def navigated(self):
        return [p.get("url") for m, p in self.calls if m == "Page.navigate"]


def test_provocation_returns_the_tokened_url():
    import asyncio

    s = FakeSession(TOKENED)
    got = asyncio.run(provoke_rapt(s, REDIRECTOR))
    assert got == TOKENED
    assert s.navigated() == [REDIRECTOR], "it must navigate the un-tokened redirector"


def test_a_signin_bounce_is_not_a_token():
    import asyncio

    s = FakeSession(SIGNIN)
    assert asyncio.run(provoke_rapt(s, REDIRECTOR)) is None


def test_an_empty_redirector_does_nothing():
    import asyncio

    s = FakeSession(TOKENED)
    assert asyncio.run(provoke_rapt(s, "")) is None
    assert s.navigated() == [], "nothing to navigate, so nothing should be tried"


def test_a_dead_session_returns_none_rather_than_raising():
    """A failure to read the landing URL must not take the run down; the caller
    falls back to its needs_reauth path."""
    import asyncio

    s = FakeSession(TOKENED, raise_on_value=True)
    assert asyncio.run(provoke_rapt(s, REDIRECTOR)) is None


def test_a_rapt_on_the_accounts_host_is_not_accepted():
    """Belt and braces: a token in a sign-in URL is not a usable token."""
    import asyncio

    s = FakeSession("https://accounts.google.com/v3/signin/x?rapt=STALE")
    assert asyncio.run(provoke_rapt(s, REDIRECTOR)) is None


def test_run_once_actually_provokes_before_giving_up():
    """Source-level, because the whole point is that the orchestrator TRIES. A guard
    that is present but never called has bitten this project repeatedly."""
    import inspect

    import autopilot.run as run_mod

    src = inspect.getsource(run_mod.run_once)
    assert "provoke_rapt(" in src, "run_once must try to earn a token"
    provoke_at = src.index("provoke_rapt(")
    giveup_at = src.index('"needs_reauth",\n                    error="archive page served')
    assert provoke_at < giveup_at, "it must provoke BEFORE reporting needs_reauth"
