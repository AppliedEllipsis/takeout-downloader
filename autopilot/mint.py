"""Mint a signed file-host URL by driving the browser.

Everything here is traceable to a measurement made on 2026-09-19
(`.wiki/measured-facts.md`, `.recon/12-ladder-results-and-architecture.md`).

THE PROTOCOL, and why it looks like this
----------------------------------------
Google will not hand a non-browser client a usable download URL. The redirector
`https://takeout.google.com/takeout/download?j=<archive>&i=<idx>&user=<gaia>`
requires a **`rapt`** (ReAuth Proof Token) that only an interactive browser
session can produce. So minting means *driving the browser*, and the URL we want
is not the response body — it is the **`Location` the redirector hands back**.

    plain navigate  ->  302 -> ... -> 302 takeout-download.usercontent.google.com/...  ->  200

Three things were measured that shape the code:

1. **Minting costs exactly one attempt.** Counter `2 -> 3` on a plain navigate.
2. **Aborting the request PREVENTS the mint.** An intercepted-and-aborted
   navigate bounced to the archive page and never reached the file host. So this
   module observes with `Network` only; it never enables `Fetch` and never fails
   a request. `tests/v3/test_mint.py` asserts this.
3. **A stale `rapt` self-heals.** The redirector answers with
   `302 -> /manage/archive/<id>?user=…&rapt=<fresh>&j=<id>` — no password
   demanded. Rebuilding the redirector with that fresh `rapt` is the second hop.

The stray download the browser also starts is *not* this module's problem: the
extension's auto-cancel handles it (failure mode 1.9 keeps that switch ON).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .cdp import CdpSession
from .errors import HopLimitExceeded, MintError, NeedsReauth, QuotaExceeded

__all__ = [
    "ORIGIN",
    "FILE_HOST",
    "ACCOUNTS_HOST",
    "QUOTA_FLAG",
    "Redirect",
    "MintResult",
    "absolute_uri",
    "parse_query",
    "build_redirector",
    "extract_redirects",
    "mint",
]

ORIGIN = "https://takeout.google.com"
FILE_HOST = "takeout-download.usercontent.google.com"
ACCOUNTS_HOST = "accounts.google.com"

#: Google marks a spent download allowance with this query parameter on its
#: bounce back to the archive page. Measured 2026-09-19; see `QuotaExceeded`.
QUOTA_FLAG = "quotaExceeded=true"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def absolute_uri(raw: str, origin: str = ORIGIN) -> str:
    """Resolve a `data-download-uri` against the page origin.

    **Measured:** the attribute is a *relative* path
    (`takeout/download?j=…`), not an absolute URL, despite the v2 docs calling it
    "exact per-part URLs". Resolving it is mandatory.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return origin.rstrip("/") + "/" + raw.lstrip("/")


def parse_query(url: str) -> dict:
    """First value of each query parameter. Empty values are dropped."""
    try:
        q = parse_qs(urlsplit(url or "").query, keep_blank_values=False)
    except ValueError:
        return {}
    return {k: v[0] for k, v in q.items() if v}


def build_redirector(
    archive_id: str,
    index: int | str,
    user: str,
    rapt: str,
    *,
    origin: str = ORIGIN,
) -> str:
    """Rebuild the redirector with a (possibly refreshed) token.

    Parameter order matches what Google itself emits, so the URL is
    indistinguishable from the one in the page's `data-download-uri`.

    **Measured:** the redirector carries `j`, `i`, `user`, `rapt` — and notably
    **no `authuser`**, contradicting the v2 docs' "authuser always present".
    `authuser` appears on the *file host* URL instead.
    """
    query = urlencode({"j": archive_id, "i": index, "user": user, "rapt": rapt})
    return f"{origin.rstrip('/')}/takeout/download?{query}"


# ---------------------------------------------------------------------------
# Redirect extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Redirect:
    """One hop of a redirect chain, however CDP happened to report it."""

    from_url: str
    status: Optional[int]
    location: str

    @property
    def target(self) -> str:
        return self.location


def extract_redirects(events) -> list[Redirect]:
    """Pull redirect hops out of observed CDP `Network` events.

    Chrome reports a redirect twice and we accept either form, because relying on
    one alone silently loses hops:

    * `Network.requestWillBeSent` carrying a `redirectResponse` — the *previous*
      response, with its `Location`.
    * `Network.responseReceived` for the 3xx itself.
    """
    found: list[Redirect] = []
    seen: set[tuple[str, Optional[int], str]] = set()

    def add(from_url: str, status, location: str) -> None:
        key = (from_url, status, location)
        if location and key not in seen:
            seen.add(key)
            found.append(Redirect(from_url=from_url, status=status, location=location))

    for method, params in events:
        if method == "Network.requestWillBeSent":
            prev = params.get("redirectResponse")
            if not isinstance(prev, dict):
                continue
            headers = {k.lower(): v for k, v in (prev.get("headers") or {}).items()}
            add(prev.get("url", ""), prev.get("status"), headers.get("location", ""))
        elif method == "Network.responseReceived":
            resp = params.get("response")
            if not isinstance(resp, dict):
                continue
            headers = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
            add(resp.get("url", ""), resp.get("status"), headers.get("location", ""))

    return found


def response_urls(events) -> list[str]:
    """URLs of responses seen, for the case where the file host answers directly
    without us catching its `Location`."""
    urls = []
    for method, params in events:
        if method == "Network.responseReceived":
            resp = params.get("response")
            if isinstance(resp, dict) and resp.get("url"):
                urls.append(resp["url"])
    return urls


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class MintResult:
    """A minted URL and the evidence for it."""

    url: str
    redirector: str
    hops: int
    chain: list[Redirect] = field(default_factory=list)
    refreshed_rapt: bool = False
    #: Measured cost of a successful mint. Recorded so callers can put the real
    #: number in the ledger rather than an assumption.
    attempts_spent: int = 1

    def __str__(self) -> str:  # keep tokens out of logs
        return f"<MintResult hops={self.hops} url={_redact(self.url)}>"


def _redact(url: str) -> str:
    """Never log a `rapt` token."""
    import re

    return re.sub(r"((?:rapt|TL)=)[^&\s]{8,}", r"\1<redacted>", url or "")


# ---------------------------------------------------------------------------
# The mint
# ---------------------------------------------------------------------------
async def mint(
    session: CdpSession,
    redirector: str,
    *,
    max_hops: int = 3,
    settle: float = 20.0,
    navigate_timeout: float = 25.0,
    restore_url: Optional[str] = None,
) -> MintResult:
    """Drive the browser so it mints a file-host URL, and return it.

    `settle` is how long to let `Network` events arrive after each navigate. The
    default is generous on purpose: the real chain passes through
    `accounts.google.com/RotateCookiesPage` and took several seconds even on a
    261 KB part.

    Raises `NeedsReauth` when the chain lands on a sign-in/challenge page,
    `HopLimitExceeded` if the archive-page bounce never converges, and
    `MintError` otherwise.

    **Never aborts a request.** See the module docstring — aborting breaks the
    mint, so this function only navigates and observes.
    """
    await session.call("Network.enable")

    chain: list[Redirect] = []
    current = redirector
    refreshed = False

    for hop in range(1, max_hops + 1):
        mark = session.event_count()
        try:
            await session.call("Page.navigate", {"url": current}, timeout=navigate_timeout)
        except asyncio.TimeoutError:
            # A navigation that starts a download does not always resolve its
            # reply promptly. The events still arrive, which is what we need.
            pass
        await session.drain(settle)

        observed = session.events_since(mark)
        redirects = extract_redirects(observed)
        chain.extend(redirects)

        # 1. Did we reach the file host?
        for r in redirects:
            if FILE_HOST in r.location:
                result = MintResult(url=r.location, redirector=redirector, hops=hop,
                                    chain=chain, refreshed_rapt=refreshed)
                await _restore(session, restore_url)
                return result
        for url in response_urls(observed):
            if FILE_HOST in url:
                result = MintResult(url=url, redirector=redirector, hops=hop,
                                    chain=chain, refreshed_rapt=refreshed)
                await _restore(session, restore_url)
                return result

        # 2. Did Google demand ReAuth?
        for r in redirects:
            if ACCOUNTS_HOST in r.location:
                raise NeedsReauth(r.location)

        # 2b. Did Google refuse because the export's download allowance is spent?
        #
        # Checked HERE, before the refreshed-rapt retry below, because a quota
        # refusal is terminal: retrying is guaranteed to fail and merely fills
        # the tab history with identical bounces. Measured chain:
        #
        #   .../settings/takeout/download?...&download=true&rapt=...
        #     -> [302] .../manage/archive/<id>?download=true&rapt=...&quotaExceeded=true
        #
        # Note the bounce DOES carry a rapt, so without this check the retry
        # logic below treats it as a refreshable token and loops until the hop
        # limit — reporting a hop-limit error that names the wrong cause.
        for r in redirects:
            if QUOTA_FLAG in (r.location or ""):
                raise QuotaExceeded(r.location, "Google sent quotaExceeded=true")

        # 3. Did the redirector hand us a refreshed rapt to retry with?
        refresh = next(
            (r for r in redirects if "/manage/archive/" in r.location), None
        )
        if refresh is not None:
            params = parse_query(refresh.location)
            if params.get("rapt") and params.get("j"):
                current = build_redirector(
                    params["j"], params.get("i", 0), params.get("user", ""), params["rapt"]
                )
                refreshed = True
                continue

        # 4. Nothing recognisable — report what we saw rather than guessing.
        #
        # Two things were wrong with the message this replaces. It printed
        # `(from_url, status)` and omitted `location`, the only field that says
        # where the chain actually went; and it claimed there was "no archive
        # bounce" even when the chain was nothing BUT bounces with an unusable
        # token, which is a different diagnosis with a different remedy. Both are
        # fixed: every hop prints its destination, and a bounce that merely lacks
        # a usable token is named as such.
        seen = " | ".join(
            f"{_redact(r.from_url)} -> [{r.status}] {_redact(r.location)}"
            for r in redirects
        )
        if refresh is not None:
            reason = (f"bounced to the archive page without a usable token "
                      f"(rapt={bool(parse_query(refresh.location).get('rapt'))}, "
                      f"j={bool(parse_query(refresh.location).get('j'))})")
        else:
            reason = "no file-host URL, no ReAuth demand, and no archive-page bounce"
        raise MintError(f"{reason}; saw {seen} | looking for file host {FILE_HOST!r}")

    await _restore(session, restore_url)
    raise HopLimitExceeded(
        f"still bouncing after {max_hops} hops; last redirector={_redact(current)}"
    )


async def _restore(session: CdpSession, url: Optional[str]) -> None:
    """Best-effort: put the tab back on a known page.

    Leaves the browser somewhere predictable for the next mint and keeps the
    `rapt`-bearing archive page loaded when one is supplied.
    """
    if not url:
        return
    try:
        await session.call("Page.navigate", {"url": url}, timeout=20.0)
        await session.drain(4.0)
    except Exception:
        pass
