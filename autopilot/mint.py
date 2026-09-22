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

The stray download the browser also starts is *not* this module's problem, but it
is no longer disposed of for us: **`autoCancelDownloads` is OFF** (owner
directive 2026-09-22 — the workflow depends on real browser downloads), so a mint
leaves a real part-sized download running in `/config/Downloads`. It lands on the
300 GB `cache_crypt` volume, not the 14 GB root disk. See failure mode 1.22.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

from .cdp import CdpSession
from .errors import HopLimitExceeded, MintError, NeedsReauth, QuotaExceeded

__all__ = [
    "ORIGIN",
    "FILE_HOST",
    "ACCOUNTS_HOST",
    "QUOTA_FLAG",
    "SIGNIN_MARKERS",
    "is_signin_location",
    "Redirect",
    "MintResult",
    "absolute_uri",
    "parse_query",
    "build_redirector",
    "archive_id_from_archive_url",
    "part_filename_from_url",
    "extract_redirects",
    "file_host_hits",
    "response_urls",
    "mint",
]

ORIGIN = "https://takeout.google.com"
FILE_HOST = "takeout-download.usercontent.google.com"
ACCOUNTS_HOST = "accounts.google.com"

#: Google marks a spent download allowance with this query parameter on its
#: bounce back to the archive page. Measured 2026-09-19; see `QuotaExceeded`.
QUOTA_FLAG = "quotaExceeded=true"

#: Substrings that identify a **genuine interactive sign-in demand**.
SIGNIN_MARKERS = (
    "/servicelogin",
    "/v3/signin/",
    "/signin/",
    "interactivelogin",
    "/challenge/",
)

#: `accounts.google.com` paths that are a NORMAL part of a SUCCESSFUL mint.
#:
#: Measured (`docs/v3/01-ARCHITECTURE.md`): the healthy chain is
#:
#:     302 takeout/download -> archive 302 -> RotateCookiesPage?rot=3
#:       -> 302 takeout-download.usercontent.google.com/...zip -> 200
#:
#: so the accounts host appears on the path to success. Treating any
#: `accounts.google.com` hop as a ReAuth demand therefore misclassifies a working
#: mint as an auth failure — which is exactly what the old
#: `ACCOUNTS_HOST in r.location` test did. It only appeared to work because the
#: file-host check happens to run first when both hops arrive in one batch, so the
#: bug was TIMING-DEPENDENT: a slower chain delivering the RotateCookiesPage hop a
#: batch earlier would report `needs_reauth` for an export that was minting fine.
BENIGN_ACCOUNTS_PATH_MARKERS = (
    "/rotatecookiespage",
)


def is_signin_location(url: str) -> bool:
    """True only for a URL that really demands an interactive challenge.

    Pure, so the classification that decides `needs_reauth` is testable offline.
    A hop to the accounts host is NOT sufficient — see
    `BENIGN_ACCOUNTS_PATH_MARKERS`.
    """
    low = (url or "").lower()
    if ACCOUNTS_HOST not in low:
        return False
    if any(marker in low for marker in BENIGN_ACCOUNTS_PATH_MARKERS):
        return False
    return any(marker in low for marker in SIGNIN_MARKERS)


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


def archive_id_from_archive_url(url: str) -> Optional[str]:
    """The `<id>` in `/manage/archive/<id>`.

    Pure, and needed because the refresh bounce does not always carry `j` —
    measured 2026-09-19. The id is in the path either way.
    """
    m = re.search(r"/manage/archive/([^/?#]+)", url or "")
    return m.group(1) if m else None


def part_filename_from_url(url: str) -> str:
    """The real part filename, taken from the minted file-host URL.

    **Measured 2026-09-19 — this was a silent multi-part data-loss bug.** The
    redirector path is `takeout/download?j=...`, so the *scraped* basename is
    always the literal string `download` for every part. Staging files under that
    name means a multi-part export writes `download` over and over, and the
    destination index — which is keyed by filename — then reports every later
    part as already present. A 63-part export would land one file.

    The minted URL carries the true name in its path:

        https://takeout-download.usercontent.google.com/download/
            takeout-20260919T163231Z-1-001.zip?j=...

    Returns `""` when the path yields nothing archive-like, so the caller decides;
    it never invents a name.

    **Percent-decoding is not cosmetic.** Observed live 2026-09-21 on the 64-product
    export, part 18:

        .../download/All%20mail%20Including%20Spam%20and%20Trash-002.mbox?j=...

    Left encoded, that part would land on the archive as
    `All%20mail%20Including%20Spam%20and%20Trash-002.mbox` — a name no human wrote and
    that no later tool would match against Google's own listing. Note too that this
    part is an `.mbox`, NOT a `.zip`: Google serves non-zip parts, so this function
    must not assume an extension.
    """
    path = urlsplit(url or "").path or ""
    name = path.rstrip("/").split("/")[-1] if path else ""
    if not name or name.lower() in ("download", "takeout"):
        return ""
    return unquote(name)


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
    without us catching its `Location`.

    Deliberately unfiltered — see `file_host_hits()` for the status-checked form.
    """
    urls = []
    for method, params in events:
        if method == "Network.responseReceived":
            resp = params.get("response")
            if isinstance(resp, dict) and resp.get("url"):
                urls.append(resp["url"])
    return urls


def file_host_hits(events) -> list[tuple[str, int]]:
    """`(url, status)` for every FILE_HOST response that actually SUCCEEDED.

    **Fixed 2026-09-19.** The mint's step 1 previously consulted
    `response_urls()` — which has no status filter — and did so BEFORE the
    ReAuth/quota/refresh branches. So a file host answering
    `302 -> accounts.google.com/ServiceLogin` appeared as a file-host *response*,
    the mint reported SUCCESS, and the dead URL was written into the ledger's mint
    cache. Every later run then re-used that URL and failed — exactly the "park
    forever" shape this project spent three months in.

    Only a 2xx from the file host means a URL was minted. Redirects and errors do
    not, and must fall through so the ReAuth/quota handling can classify them.
    """
    hits: list[tuple[str, int]] = []
    for method, params in events:
        if method != "Network.responseReceived":
            continue
        resp = params.get("response")
        if not isinstance(resp, dict):
            continue
        url = resp.get("url") or ""
        if FILE_HOST not in url:
            continue
        status = resp.get("status")
        if isinstance(status, int) and 200 <= status < 300:
            hits.append((url, status))
    return hits


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
        # Only a 2xx from the file host counts as a mint. A file host answering
        # `302 -> accounts.google.com` is NOT a successful mint, and treating it as
        # one cached a dead URL that every later run then re-used. Checked here,
        # before the ReAuth/quota branches, so those can classify it instead.
        for url, _status in file_host_hits(observed):
            result = MintResult(url=url, redirector=redirector, hops=hop,
                                chain=chain, refreshed_rapt=refreshed)
            await _restore(session, restore_url)
            return result

        # 2. Did Google demand ReAuth?
        #
        # `is_signin_location`, NOT `ACCOUNTS_HOST in location`: the measured
        # SUCCESSFUL chain routes through `accounts.google.com/RotateCookiesPage`,
        # so matching the host alone turned healthy mints into `needs_reauth`
        # whenever that hop arrived before the file-host hop.
        for r in redirects:
            if is_signin_location(r.location):
                # Leave the tab somewhere predictable before bailing. `_restore`'s
                # own docstring promises this and it was only called on the SUCCESS
                # paths and before the hop limit — so a mint that failed on a
                # challenge, a quota refusal or an unrecognisable chain left the tab
                # sitting on a sign-in page, which is exactly where the NEXT run's
                # `recover_rapt_url` cannot find a token.
                await _restore(session, restore_url)
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
                await _restore(session, restore_url)
                raise QuotaExceeded(r.location, "Google sent quotaExceeded=true")

        # 3. Did the redirector hand us a refreshed rapt to retry with?
        #
        # `j` is NOT required to retry. Measured 2026-09-19 on a fresh export:
        # the bounce carries a live `rapt` but omits `j` entirely —
        #
        #   .../manage/archive/<id>?user=...&pli=1&rapt=<fresh>
        #
        # and demanding `j` there discarded a perfectly good token, turning a
        # mintable export into "no archive bounce in the chain". The archive id
        # is already known: it is in the path, and in the redirector in use.
        refresh = next(
            (r for r in redirects if "/manage/archive/" in r.location), None
        )
        if refresh is not None:
            params = parse_query(refresh.location)
            current_params = parse_query(current)
            rapt = params.get("rapt")
            archive_id = (params.get("j")
                          or current_params.get("j")
                          or archive_id_from_archive_url(refresh.location))
            user = params.get("user") or current_params.get("user", "")
            index = params.get("i", current_params.get("i", 0))
            if rapt and archive_id:
                current = build_redirector(archive_id, index, user, rapt)
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
        # Restore before raising, for the same reason as the branches above: the tab
        # must not be left mid-chain for the next run to inherit.
        await _restore(session, restore_url)
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
