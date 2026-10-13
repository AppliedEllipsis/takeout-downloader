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


def test_run_once_refuses_an_untokened_page_without_probe_or_mint(tmp_path, monkeypatch):
    """An absent page token is a hard safety boundary, not an invitation to probe.

    The redirector probe can start an unmanaged browser download.  The orchestrator
    must report ``needs_reauth`` without navigating that redirector or minting a URL.
    """
    import asyncio
    from contextlib import asynccontextmanager

    import autopilot.run as run_mod
    import autopilot.scrape as scrape_mod
    from autopilot.scrape import ArchivePage, PartLink
    from autopilot.run import RunConfig

    archive = "f9a17be0-65e2-4b1f-a9c6-04c840204419"
    work_url = f"https://takeout.google.com/manage/archive/{archive}"
    redirector = f"https://takeout.google.com/takeout/download?j={archive}&i=0&user=116943325794238708860"
    page = ArchivePage(
        url=work_url,
        parts=[PartLink(index=0, raw_uri=redirector, redirector=redirector,
                        archive_id=archive, user="116943325794238708860",
                        has_rapt=False, size="1",
                        filename="takeout-20260919T000000Z-1-001.zip")],
        part_of_N=[1],
    )
    session = FakeSession(work_url)

    @asynccontextmanager
    async def factory():
        yield session

    async def fake_read(*_args, **_kwargs):
        return page

    async def unsafe_probe(*_args, **_kwargs):
        raise AssertionError("unsafe redirector probe was attempted")

    async def unsafe_mint(*_args, **_kwargs):
        raise AssertionError("mint was attempted without a page token")

    monkeypatch.setattr(run_mod, "read_archive", fake_read)
    monkeypatch.setattr(scrape_mod, "provoke_rapt", unsafe_probe)
    monkeypatch.setattr(run_mod, "mint_part", unsafe_mint)
    cfg = RunConfig(archive_id=archive, ledger_path=str(tmp_path / "ledger.db"),
                    staging_dir=str(tmp_path / "staging"),
                    archive_dir=str(tmp_path / "archive"), require_mount=False,
                    work_url=work_url, settle=0)

    outcome = asyncio.run(run_mod.run_once(cfg, session_factory=factory,
                                           http_client=object()))

    assert outcome.status == "needs_reauth"
    assert session.navigated() == [], "no browser navigation is safe for this guard"
