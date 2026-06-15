#!/usr/bin/env python3
"""
Google Takeout Extension ↔ TUI Payload Schema
=============================================

Defines the JSON schema that the browser extension produces and the TUI
consumes. Both sides MUST conform to this schema; any drift will cause
silent auth failures.

The extension captures a Google Takeout download request in the browser,
serializes it as JSON, the user copies it to the clipboard, then pastes
it into the TUI. The TUI parses the JSON back into a download payload.

Why JSON and not raw cURL?
  - Round-trips headers, referrer, user-agent, and query string losslessly.
  - User-Agent, Accept, Referer headers are critical: Google rejects
    requests that don't match what Chrome sent. cURL pasted from a
    PowerShell session often strips or mangles these.
  - Future-proof for adding fields without breaking older extensions.

Schema v1
---------
    {
      "schema":      1,             # int, required. Bump on breaking changes.
      "captured_at": "ISO-8601 UTC",# string, required. When the request was captured.
      "source":      "extension",   # string. "extension" | "curl" | "powershell" | "manual"
      "url":         "https://...", # string, required. The actual download URL.
      "method":      "GET",         # string. Almost always GET for Takeout.
      "headers": {                  # dict, optional. Extra headers the server saw.
        "User-Agent":  "...",
        "Accept":      "...",
        "Accept-Language": "...",
        "Referer":     "https://takeout.google.com/"
      },
      "cookie":      "SID=...; HSID=...; ..."  # string, required.
    }

Why we don't capture and re-send every header automatically?
  - We only need what Google's takeout-download.usercontent.google.com
    cares about. The defaults below cover ~all cases.
  - Adding every header bloats the clipboard payload with noise.

Notes
-----
- The extension MUST capture from `takeout-download.usercontent.google.com`
  (the FINAL domain after the redirect chain). Capturing pre-redirect from
  `takeout.google.com` saves cookies that aren't valid for the download
  host and every download returns an HTML login page.
- See https://blog.omgmog.net/post/downloading-google-takeout-to-a-nas/
  for the full write-up of the redirect-chain gotcha.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Tuple


# Schema v2 (2026-06-14) added multi-export part metadata:
#   - top-level `archiveId` and `expectedParts`
#   - per-export `partIndex` (0-based) and `size` (bytes)
# These let the CLI auto-detect the part count from the payload instead
# of asking the user, and skip Range probes when sizes are already known.
#
# Compatibility policy:
#   - Schema 1 single-export captures: still accepted everywhere.
#   - Schema 1 multi-payloads: rejected by parse_multi_payload with a
#     clear "re-capture" message — they don't carry the new metadata
#     so the CLI can't safely auto-download them.
#   - Schema 2 single + multi: full new features.
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)

# Headers that the download server actually validates against.
# Anything else is captured but ignored, to keep the clipboard payload small.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://takeout.google.com/",
}

# Match the takeout filename pattern: takeout-YYYYMMDDTHHMMSSZ-N-NNN.zip
TAKEOUT_FILENAME_RE = re.compile(
    r"takeout-\d{8}T\d{6}Z-\d+-\d{3}\.\w+",
    re.IGNORECASE,
)

# Validation: cookie header MUST contain at least one of these secure markers
# that Google uses for authenticated download requests. If we got a cookie
# with none of these, we likely captured it pre-redirect and it won't work.
REQUIRED_COOKIE_MARKERS = (
    "__Secure-1PSID",
    "__Secure-3PSID",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
)


@dataclass
class TakeoutPayload:
    """A captured Google Takeout download payload.

    Use ``TakeoutPayload.from_json()`` or ``TakeoutPayload.from_curl()``
    to construct; do NOT instantiate directly except in tests.
    """

    url: str
    cookie: str
    headers: dict = field(default_factory=lambda: dict(DEFAULT_HEADERS))
    method: str = "GET"
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = "extension"
    schema: int = SCHEMA_VERSION

    # ------------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------------

    @classmethod
    def from_json(cls, text: str) -> "TakeoutPayload":
        """Parse a JSON blob produced by the extension (or the TUI's own
        export). Raises ``ValueError`` on schema mismatch or missing fields.
        """
        if not text or not text.strip():
            raise ValueError("Empty payload")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ValueError("Payload must be a JSON object")

        schema = data.get("schema", 1)
        if schema not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"Schema version {schema} not supported "
                f"(this TUI expects version {SCHEMA_VERSION}, "
                f"also accepts {SUPPORTED_SCHEMA_VERSIONS[0]} for single-export)"
            )

        url = data.get("url")
        cookie = data.get("cookie")
        if not url:
            raise ValueError("Payload missing 'url'")
        if not cookie:
            raise ValueError("Payload missing 'cookie'")
        if "takeout" not in url.lower():
            raise ValueError(
                "URL doesn't look like a Takeout download link "
                f"(expected 'takeout' in URL, got: {url[:60]}...)"
            )

        # Sanitize headers — keep only dict[str,str]
        raw_headers = data.get("headers", {}) or {}
        headers = dict(DEFAULT_HEADERS)
        for k, v in raw_headers.items():
            if isinstance(k, str) and isinstance(v, str):
                headers[k] = v

        return cls(
            url=url,
            cookie=cookie,
            headers=headers,
            method=data.get("method", "GET"),
            captured_at=data.get("captured_at")
            or datetime.now(timezone.utc).isoformat(),
            source=data.get("source", "extension"),
        )

    @classmethod
    def from_curl(cls, curl_text: str) -> "TakeoutPayload":
        """Parse a pasted cURL command (bash or PowerShell).

        This is the legacy path; the extension is the primary capture
        mechanism but we keep cURL paste as a fallback for users who
        can't install the extension (e.g., Safari, locked-down browsers).
        """
        from takeout import (
            extract_url_from_curl,
            extract_cookie_from_curl,
        )
        url = extract_url_from_curl(curl_text)
        cookie = extract_cookie_from_curl(curl_text)
        if not url:
            raise ValueError("Could not extract URL from cURL command")
        if not cookie:
            raise ValueError("Could not extract Cookie header from cURL command")

        # cURL pastes often lose headers; defaults backfill what's missing.
        return cls(
            url=url,
            cookie=cookie,
            headers=dict(DEFAULT_HEADERS),
            source="curl" if "curl " in curl_text[:32] else "powershell",
        )

    # ------------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------------

    def validate(self) -> Tuple[bool, Optional[str]]:
        """Check the payload is likely to succeed.

        Returns ``(ok, warning_or_error)``. ``warning_or_error`` is None on
        success, otherwise a human-readable explanation.
        """
        if not self.cookie:
            return False, "Empty cookie"

        # Check the cookie actually has a Google session marker. If not, the
        # extension likely captured pre-redirect and the request will fail
        # with an HTML login page.
        if not any(marker in self.cookie for marker in REQUIRED_COOKIE_MARKERS):
            return False, (
                "Cookie doesn't contain any known Google session markers "
                f"({', '.join(REQUIRED_COOKIE_MARKERS[:3])}, ...). "
                "The extension may have captured the request before the "
                "redirect chain completed. Re-capture from a request to "
                "takeout-download.usercontent.google.com (not takeout.google.com)."
            )

        # Check we have a valid-looking takeout URL.
        if "takeout-download.usercontent.google.com" not in self.url and \
           "storage.cloud.google.com" not in self.url:
            # Fallback to filename pattern
            if not TAKEOUT_FILENAME_RE.search(self.url):
                return False, (
                    "URL doesn't match a known Takeout download pattern. "
                    "Expected takeout-download.usercontent.google.com or "
                    "takeout-YYYYMMDDTHHMMSSZ-N-NNN.zip in the path."
                )

        # The session typically expires after a few hundred MB to ~15 GB.
        # We can't know how much has been downloaded since the cookie was
        # captured, but we can warn if it's old.
        try:
            captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - captured).total_seconds() / 60
            if age_minutes > 30:
                return True, (
                    f"⚠ Cookie was captured {int(age_minutes)} minutes ago. "
                    "Google sessions expire after ~1 hour, but parallel "
                    "downloads can trigger challenges sooner. Consider "
                    "re-capturing if downloads fail."
                )
        except (ValueError, TypeError):
            pass  # Bad timestamp, don't block on it

        return True, None

    # ------------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        """Serialize back to JSON. Useful for the TUI's "export" button and
        for clipboard round-trips in tests.
        """
        return json.dumps(asdict(self), indent=indent, sort_keys=True)

    def to_curl(self) -> str:
        """Convert to a cURL command string for display in logs / copy-to-
        clipboard for users who want to feed it to a different tool.
        """
        lines = [f"curl '{self.url}' \\"]
        for k, v in self.headers.items():
            lines.append(f"  -H '{k}: {v}' \\")
        lines.append(f"  -H 'Cookie: {self.cookie}'")
        return "\n".join(lines)

    # ------------------------------------------------------------------------
    # Convenience for the TUI
    # ------------------------------------------------------------------------

    def cookie_chars(self) -> int:
        return len(self.cookie)

    def filename_hint(self) -> str:
        """Best-effort filename extracted from the URL path.
        Returns 'unknown' if it can't be parsed.
        """
        # URL ends with the takeout filename
        tail = self.url.split("?")[0].rstrip("/").split("/")[-1]
        if tail and tail.lower().endswith((".zip", ".tgz")):
            return tail
        return "unknown"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def parse_payload(text: str) -> TakeoutPayload:
    """Auto-detect JSON vs cURL and parse accordingly.

    Detection: starts with ``{`` → JSON, else treat as cURL/powershell.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty input")
    if text.startswith("{"):
        return TakeoutPayload.from_json(text)
    return TakeoutPayload.from_curl(text)


def parse_multi_payload(text: str) -> list[TakeoutPayload]:
    """Parse a multi-export payload produced by the extension's
    "Copy ALL exports" button.

    Returns a list of TakeoutPayload objects, one per export.
    If the payload is not multi, returns a single-element list
    with the regular payload.

    Schema v2 multi-payloads carry top-level ``archiveId`` and
    ``expectedParts`` so the CLI can skip its "How many parts?" prompt.
    Schema v1 multi-payloads are rejected — re-capture with the current
    extension version.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty input")
    if not text.startswith("{"):
        return [TakeoutPayload.from_curl(text)]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")

    # Not a multi payload → regular single-export (any supported schema)
    if not data.get("multi"):
        return [TakeoutPayload.from_json(text)]

    # Multi payload: enforce schema v2. v1 multi-payloads lack the
    # archiveId / expectedParts metadata that lets the CLI safely
    # auto-download, so we refuse rather than silently degrading.
    schema = data.get("schema", 1)
    if schema < 2:
        raise ValueError(
            f"Multi payload is schema v{schema}, but v2 is required for "
            "auto-download (carries archiveId and expectedParts). "
            "Re-capture with the current Takeout Downloader Helper "
            "extension — the page should show \"Part X of N\" buttons "
            "and the extension will emit a v2 multi-payload."
        )

    # Multi payload: extract shared fields and clone per-export
    base = {
        "schema": schema,
        "captured_at": data.get("captured_at")
        or datetime.now(timezone.utc).isoformat(),
        "source": data.get("source", "extension"),
        "headers": data.get("headers", {}) or {},
        "cookie": data.get("cookie", ""),
    }

    exports = data.get("exports", [])
    if not exports:
        raise ValueError("Multi payload has no exports")

    payloads: list[TakeoutPayload] = []
    for exp in exports:
        url = exp.get("url") if isinstance(exp, dict) else str(exp)
        if not url:
            continue
        payload = TakeoutPayload(
            url=url,
            cookie=base["cookie"],
            headers=dict(DEFAULT_HEADERS, **base.get("headers", {})),
            method="GET",
            captured_at=base["captured_at"],
            source=base["source"],
            schema=base["schema"],
        )
        payloads.append(payload)

    if not payloads:
        raise ValueError("Multi payload has no valid export URLs")

    return payloads


@dataclass
class MultiPayloadMeta:
    """Top-level metadata extracted from a v2 multi-payload.

    The CLI uses ``expectedParts`` to skip its "How many parts?" prompt
    when the extension has already detected the count from the page.
    ``archiveId`` lets the CLI filter manifest-fetch results so it never
    pulls in URLs from other Takeouts the user has on their account.
    ``sizes`` is keyed by part index (0-based) so the CLI can show
    "155.3 MB" up front and skip Range probes when known.
    """
    archiveId: Optional[str] = None
    expectedParts: Optional[int] = None
    sizes: dict = field(default_factory=dict)

    @property
    def has_full_metadata(self) -> bool:
        return bool(self.archiveId) and bool(self.expectedParts)


def parse_multi_payload_meta(text: str) -> tuple[list[TakeoutPayload], MultiPayloadMeta]:
    """Like :func:`parse_multi_payload`, but also returns the top-level
    v2 metadata (``archiveId``, ``expectedParts``, per-part ``sizes``).

    For non-multi or schema-v1 payloads the meta is empty.
    """
    payloads = parse_multi_payload(text)
    meta = MultiPayloadMeta()

    text = (text or "").strip()
    if not text.startswith("{"):
        return payloads, meta

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return payloads, meta

    if not isinstance(data, dict) or not data.get("multi"):
        return payloads, meta

    meta.archiveId = data.get("archiveId") or None
    ep = data.get("expectedParts")
    if isinstance(ep, int) and ep > 0:
        meta.expectedParts = ep

    for i, exp in enumerate(data.get("exports", []) or []):
        if not isinstance(exp, dict):
            continue
        size = exp.get("size")
        if isinstance(size, int) and size > 0:
            meta.sizes[i] = size

    return payloads, meta
