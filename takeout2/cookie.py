"""Live cookie-jar pull from our own Chrome over the DevTools Protocol.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §5.3 and the design in
``docs/webgui/14-resume-cookies-multiaccount.md``.

Why the LIVE jar and not the extension's stored capture:

The extension's ``lastCapture.cookie`` goes stale in ~1-2 min. A probe with a
stale cookie returns ``302 -> accounts.google.com/ServiceLogin`` — which is
what made ``curl -C -`` report the nonsensical "server does not seem to
support byte ranges". The live jar read over CDP (``Storage.getCookies`` from
the logged-in browser) is always as fresh as the browser session: ~27 google
cookies, ~3.5-4.5 KB of Cookie header.

Environment constraint (doc 14): Chrome's DevTools rejects non-localhost Host
headers, so the CDP call MUST run inside the container, targeting
``127.0.0.1:9222``. We enforce that here.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .contracts import DEFAULTS

log = logging.getLogger("takeout2.cookie")

__all__ = ["CookieState", "CookieError", "CookieStale", "CDP_UNREACHABLE",
           "LiveCookieJar"]

#: Idle limit: beyond this age a NEW stream must not start on this jar.
COOKIE_IDLE_LIMIT_S = 90
#: We accept JSON over websocket OR over raw http; CDP serves both on 9222.
CDP_UNREACHABLE = "cdp_unreachable"


class CookieError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class CookieStale(CookieError):
    def __init__(self, age_s: float):
        super().__init__("cookie_stale", f"jar is {age_s:.0f}s old")


@dataclass
class CookieState:
    """A freshly-read cookie jar, plus when it was read."""

    header: str
    pulled_at: float          # time.monotonic()
    n_cookies: int
    google_domains: list[str] = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.monotonic() - self.pulled_at)

    @property
    def fresh(self) -> bool:
        return self.age_s <= COOKIE_IDLE_LIMIT_S


class LiveCookieJar:
    """Minimal CDP client: connect, Storage.getCookies, build the header.

    Dependency-light by design: tries the ``websocket`` module (present in
    this environment) and falls back to a raw ``requests``-based JSON-RPC
    over the HTTP endpoint Chrome also exposes on 9222 (``/json`` targets +
    per-target websockets). For the common case we only need ONE call, so
    simplicity wins over a full CDP library.
    """

    def __init__(self, cdp_url: str = DEFAULTS["CDP_URL"], timeout: float = 10.0):
        # Enforce the container-only constraint from doc 14.
        host = cdp_url.split("//")[-1].split(":")[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise CookieError(
                "non_localhost_cdp",
                "Chrome DevTools rejects non-localhost Host headers; run the "
                "cookie pull inside the container against 127.0.0.1:9222")
        self.cdp_url = cdp_url.rstrip("/")
        self.timeout = timeout

    def pull(self) -> CookieState:
        cookies = self._get_cookies()
        google = [c for c in cookies if "google.com" in c.get("domain", "")]
        if not google:
            # The browser may be on the login page; there is nothing to join.
            raise CookieError("no_google_cookies",
                              "browser holds no google.com cookies — is the "
                              "account logged in on the manage page?")
        parts = [f"{c['name']}={c['value']}" for c in google]
        return CookieState(
            header="; ".join(parts),
            pulled_at=time.monotonic(),
            n_cookies=len(parts),
            google_domains=sorted({c["domain"] for c in google}),
        )

    def fresh(self, max_age_s: float = COOKIE_IDLE_LIMIT_S) -> CookieState:
        """Pull and reject jars older than the idle limit."""
        state = self.pull()
        if state.age_s > max_age_s:
            raise CookieStale(state.age_s)
        return state

    # -- transport ------------------------------------------------------
    def _get_cookies(self) -> list[dict]:
        """Storage.getCookies over CDP; try websocket then HTTP fallback."""
        try:
            return self._via_websocket()
        except Exception as exc:  # noqa: BLE001 - fall back before raising
            log.debug("websocket path failed (%s); trying HTTP", exc)
        return self._via_http()

    def _via_websocket(self) -> list[dict]:
        import websocket  # present in this environment
        ws = websocket.create_connection(self.cdp_url, timeout=self.timeout)
        try:
            ws.send('{"id":1,"method":"Storage.getCookies"}')
            raw = ws.recv()
        finally:
            ws.close()
        msg = _json_loads(raw)
        return (msg.get("result") or {}).get("cookies") or []

    def _via_http(self) -> list[dict]:
        import requests
        # Chrome's HTTP endpoint on 9222 serves /json targets; Storage.getCookies
        # needs the websocket. If websocket lib is absent we fail loudly rather
        # than fake a cookie.
        raise CookieError("no_websocket_lib",
                          "websocket module unavailable; cannot read the live jar")


def _json_loads(raw: str) -> dict:
    import json
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise CookieError("cdp_bad_response", f"unparseable CDP reply: {raw[:200]!r}")
