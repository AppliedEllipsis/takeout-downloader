"""Failure types for the v3 autopilot.

Kept in one module so callers can catch the whole family with a single import,
and so the *distinctions* the system depends on are named rather than inferred
from string matching.

Reference: `docs/v3/01-ARCHITECTURE.md`, and the measurements in
`.wiki/measured-facts.md`.
"""
from __future__ import annotations

__all__ = [
    "AutopilotError",
    "NeedsReauth",
    "MintError",
    "HopLimitExceeded",
    "QuotaExceeded",
    "NotAuthorised",
    "BrowserUnavailable",
]


class AutopilotError(Exception):
    """Base class for every autopilot failure."""


class NeedsReauth(AutopilotError):
    """Google demanded an interactive ReAuth (password) challenge.

    **This is not a generic auth failure and must never be treated as one.**
    Measured 2026-09-19: the download URL returns a chain ending at
    `accounts.google.com/v3/signin/challenge/pwd` — *"To continue, first verify
    it's you"* — even for a real browser with a live session.

    Every prior generation collapsed this into a generic error, parked the job
    as `needs_cookie` indefinitely, and (because the park never timed out) opened
    a browser tab every minute for roughly three months. See failure modes 1.9
    and 1.18.

    The correct response is to enter `NEEDS_REAUTH`, alert a human, and resume
    after the session is authorised again. The account has SMS/Google-prompt
    2-Step Verification, so a stored credential cannot clear this — see
    `docs/v3/01-ARCHITECTURE.md` §4.2.
    """

    def __init__(self, url: str, detail: str = "") -> None:
        self.url = url
        self.detail = detail
        super().__init__(
            f"ReAuth required (hit {url or 'a sign-in page'})"
            + (f": {detail}" if detail else "")
        )


class BrowserUnavailable(AutopilotError):
    """The browser could not be reached, or has no page target to drive.

    Raised by the session factory's own connect, so it means "this job cannot
    start" rather than "a command failed part-way through". It exists so a dead
    Chromium becomes a classified failure with a recorded job status instead of an
    uncaught traceback that leaves the job parked on `scraping` forever.
    """


class NotAuthorised(AutopilotError):
    """The browser tab is already sitting on a sign-in/challenge page.

    Distinct from `NeedsReauth`: that is raised when a *navigation* bounces to
    sign-in, this when we discover the tab is parked there before starting.
    """


class MintError(AutopilotError):
    """Minting failed for a reason that is not a ReAuth demand."""


class QuotaExceeded(MintError):
    """The export's own download allowance is spent. Terminal for this export.

    **Measured 2026-09-19**, from the redirect chain itself:

        .../settings/takeout/download?...&download=true&rapt=...
          -> [302] .../manage/archive/<id>?download=true&rapt=...&quotaExceeded=true

    and the archive page says so in words:

        "You can try to download a file only 5 times."
        "You've tried to download or have downloaded this file 5 times, which is
         the maximum number of times you can take this action."
        "You can create a new request at any time."

    This is emphatically **not** ReAuth, not an expired export, and not a bug in
    the cookie jar. Every retry is guaranteed to fail, so retrying is pure waste:
    the remedy is a NEW export. Before this type existed the condition surfaced
    as an opaque `MintError` reading "no file-host URL, no ReAuth, and no archive
    bounce" — which was also self-contradictory, because there *was* a bounce.

    Note the ledger's attempt count and Google's counter are different things,
    and only the latter runs out.
    """

    def __init__(self, url: str = "", detail: str = "") -> None:
        self.url = url
        self.detail = detail
        super().__init__(
            "the export's download allowance is exhausted"
            + (f" ({detail})" if detail else "")
            + ". Google caps downloads per export (measured: 5) and refuses every "
            "further attempt. Create a NEW export; no retry of this one can "
            "succeed."
        )


class HopLimitExceeded(MintError):
    """The redirector kept bouncing to the archive page past `max_hops`.

    Measured behaviour: hitting the redirector with a stale `rapt` returns
    `302 -> /manage/archive/<id>?user=…&rapt=<fresh>&j=<id>`, which carries a
    refreshed token we can retry with. If that never converges, something else
    is wrong and we stop rather than loop.
    """
