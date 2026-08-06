"""Pure classification of HTTP responses into ReasonCode.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §5.2.

This module is deliberately pure: it takes plain values (status, headers,
first bytes, final URL) and returns a ``ReasonCode``. No sockets, no I/O.
That makes every branch trivially testable from fixtures, which matters
because misclassification here is what burned a 2.8 TB download in v1.

The two failure modes we must never repeat:

1. **HTML treated as auth failure.** Probing one index PAST the last real
   part returns an HTML page. v1 raised ``AuthError`` for all HTML, flipping
   the whole job to ``needs_cookie`` and starting the
   capture -> discover -> expire -> recapture livelock.

2. **302 mistaken for a range problem.** A stale cookie yields
   ``302 -> accounts.google.com/ServiceLogin``. Following it lands on a 200
   HTML login page, which made ``curl -C -`` report "server does not seem to
   support byte ranges" — a completely misleading symptom.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from .contracts import ReasonCode

__all__ = ["ResponseFacts", "classify", "is_zip_magic", "looks_like_html"]

ZIP_MAGIC = b"PK\x03\x04"
#: An empty archive / EOCD-only stub also starts with PK, so accept both.
ZIP_MAGIC_EMPTY = b"PK\x05\x06"

_AUTH_HOST_RE = re.compile(r"accounts\.google\.com", re.I)
_HTML_SNIFF_RE = re.compile(rb"^\s*(<!doctype html|<html|<HTML)", re.I)

#: Phrases Google uses when the per-archive download ceiling is hit. Matched
#: case-insensitively against the first bytes of an HTML body.
_LIMIT_PHRASES = (
    b"download limit",
    b"too many times",
    b"downloaded 5 times",
    b"exceeded",
    b"no longer available",
    b"request another archive",
)

#: Phrases indicating an expired/removed export rather than an auth problem.
_EXPIRED_PHRASES = (
    b"archive has expired",
    b"expired",
)


def is_zip_magic(first_bytes: Optional[bytes]) -> bool:
    if not first_bytes:
        return False
    return first_bytes.startswith(ZIP_MAGIC) or first_bytes.startswith(ZIP_MAGIC_EMPTY)


def looks_like_html(content_type: str, first_bytes: Optional[bytes]) -> bool:
    if content_type and "text/html" in content_type.lower():
        return True
    if first_bytes and _HTML_SNIFF_RE.match(first_bytes):
        return True
    return False


@dataclass(frozen=True)
class ResponseFacts:
    """Everything classification needs, and nothing it doesn't.

    ``first_bytes`` should be at most a few KiB — enough to sniff HTML and to
    read the ZIP magic. Never pass a whole part in here.
    """

    status: int
    headers: Mapping[str, str]
    first_bytes: bytes = b""
    final_url: str = ""
    #: True when the caller sent a Range header and therefore EXPECTS 206.
    expected_partial: bool = False
    #: True when this request is a discovery probe past the known end, where
    #: HTML is a legitimate "stop here" signal rather than an error.
    probing_end: bool = False
    #: Set when the transport raised before/while reading the body.
    transport_error: Optional[str] = None

    def header(self, name: str, default: str = "") -> str:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    @property
    def content_type(self) -> str:
        return self.header("content-type")

    @property
    def content_length(self) -> Optional[int]:
        raw = self.header("content-length")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


def classify(facts: ResponseFacts) -> ReasonCode:
    """Map a response to a ReasonCode. Order of checks is significant."""

    # 0. Transport blew up before we learned anything useful.
    if facts.transport_error:
        return ReasonCode.NETWORK_ERROR

    body_head = (facts.first_bytes or b"")[:4096]
    lowered = body_head.lower()

    # 1. Redirected to the sign-in host => the cookie is dead. This must be
    #    checked BEFORE the generic HTML branch, because the login page is
    #    served as a perfectly ordinary 200 text/html.
    if facts.final_url and _AUTH_HOST_RE.search(facts.final_url):
        return ReasonCode.AUTH_REDIRECT

    # 2. Explicit redirect status pointing at the auth host.
    if facts.status in (301, 302, 303, 307, 308):
        location = facts.header("location")
        if _AUTH_HOST_RE.search(location or ""):
            return ReasonCode.AUTH_REDIRECT
        return ReasonCode.NETWORK_ERROR  # unexpected redirect; treat as transient

    # 3. Hard auth / quota statuses. A 403 may be either an auth failure or
    #    the download ceiling, so inspect the body before deciding.
    if facts.status in (401, 403):
        if any(p in lowered for p in _LIMIT_PHRASES):
            return ReasonCode.LIMIT_EXCEEDED
        return ReasonCode.AUTH_401

    if facts.status == 429:
        return ReasonCode.RATE_LIMITED

    if facts.status == 404:
        return ReasonCode.NOT_FOUND

    if facts.status >= 500:
        return ReasonCode.NETWORK_ERROR

    # 4. Success statuses that are NOT actually archive bytes.
    if facts.status in (200, 206):
        if looks_like_html(facts.content_type, body_head):
            # The ambiguous case. Resolve by content first, then by intent.
            if any(p in lowered for p in _LIMIT_PHRASES):
                return ReasonCode.LIMIT_EXCEEDED
            if any(p in lowered for p in _EXPIRED_PHRASES):
                return ReasonCode.LIMIT_EXCEEDED
            # HTML with no limit/expiry wording, while deliberately probing
            # past the end, is the clean stop signal — NOT an auth error.
            if facts.probing_end:
                return ReasonCode.END_OF_RANGE
            # HTML we did not ask for, mid-download, means the session lapsed.
            return ReasonCode.AUTH_REDIRECT

        # 5. Real bytes.
        if facts.status == 206:
            return ReasonCode.OK_PARTIAL
        if facts.expected_partial and facts.status == 200:
            # We asked to resume and got a full body: the server ignored our
            # Range. Caller must restart the file from zero rather than
            # appending, so surface it as partial-progress semantics.
            return ReasonCode.OK_PARTIAL
        if is_zip_magic(body_head) or not body_head:
            return ReasonCode.OK_COMPLETE
        # 200 with a non-ZIP, non-HTML body: unknown payload, do not trust it.
        return ReasonCode.NETWORK_ERROR

    return ReasonCode.NETWORK_ERROR
