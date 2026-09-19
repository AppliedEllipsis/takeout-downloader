"""Pull the live Google cookie jar out of the browser.

The jar is what makes a *transfer* possible. Measured 2026-09-19:

* the **file host** is cookie-authenticated — the same `Range` request returns
  `206` with the jar and `302 -> accounts.google.com/ServiceLogin` without it;
* a **replayed jar works** against the file host, including for a plain HTTP
  client (that is how the one export in this project's history was finished);
* the jar is **not** what mints — minting needs a `rapt` that only an
  interactive browser session can produce. See `autopilot.mint`.

So this module exists for the transporter, and it never tries to authorise
anything on its own.

Cookie **values are secrets** and are never logged by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .cdp import CdpSession
from .errors import AutopilotError

__all__ = ["GOOGLE_SUFFIX", "CookieJar", "pull_jar", "build_header"]

#: Cookies are selected by domain suffix, matching what the v2 code did.
GOOGLE_SUFFIX = "google.com"


@dataclass
class CookieJar:
    """An assembled `Cookie:` header plus enough provenance to debug it."""

    header: str = ""
    n_cookies: int = 0
    n_google: int = 0
    domains: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.header)

    def __str__(self) -> str:  # never leak values
        return (f"<CookieJar {self.n_google}/{self.n_cookies} google cookies, "
                f"{len(self.header)} byte header, domains={self.domains}>")


class CookieError(AutopilotError):
    """The jar could not be assembled."""


def build_header(cookies: Iterable[dict], suffix: str = GOOGLE_SUFFIX) -> CookieJar:
    """Assemble a Cookie header from CDP cookie records.

    Pure and therefore unit-testable: `pull_jar` is just CDP plus this.
    """
    cookies = list(cookies or [])
    google = [c for c in cookies if suffix in (c.get("domain") or "")]
    header = "; ".join(f"{c['name']}={c['value']}" for c in google if c.get("name"))
    return CookieJar(
        header=header,
        n_cookies=len(cookies),
        n_google=len(google),
        domains=sorted({c.get("domain", "") for c in google}),
    )


async def pull_jar(session: CdpSession, *, suffix: str = GOOGLE_SUFFIX) -> CookieJar:
    """Read the browser's cookies over CDP and assemble the header.

    Free: `Storage.getCookies` reads the browser's jar and downloads nothing.
    """
    result = await session.call("Storage.getCookies")
    jar = build_header(result.get("cookies") or [], suffix)
    if not jar.usable:
        raise CookieError(
            "browser holds no matching cookies — is it signed in? "
            f"(saw {jar.n_cookies} cookies, none matching {suffix!r})"
        )
    return jar
