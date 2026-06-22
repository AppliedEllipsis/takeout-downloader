"""Output-path derivation: account label + export timestamp.

Implements the deterministic rules from docs/webgui/02-manager-service.md
("Output-dir derivation"). No LLM, no guessing — pure string parsing of what
the extension/engine already capture.

Key reality (confirmed in the extension code): Google does NOT hand us a
friendly username. The `user=` URL param is an obfuscated numeric gaia id. The
human-readable label therefore comes from, in order:
  1. an explicit label (control/recipe override)
  2. the account email local-part scraped by the extension (meta.email)
  3. fallback "gaia-<user|authuser>"

The export timestamp IS reliable: every part filename embeds it as
`takeout-20260616T040104Z-...`, identical across all parts of one export.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 20260616T040104Z  (the export-creation instant Google bakes into filenames)
_EXPORT_TS_RE = re.compile(r"(\d{8}T\d{6}Z)")
# A Google-style archive filename part: takeout-<ts>-<n>-<nnn>...
_TAKEOUT_FILE_RE = re.compile(r"takeout-\d{8}T\d{6}Z-")


def sanitize_label(raw: str) -> str:
    """Lowercase, strip an @domain, allow only [a-z0-9._-]. Never empty."""
    if not raw:
        return ""
    raw = raw.strip().lower()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    cleaned = re.sub(r"[^a-z0-9._-]", "-", raw).strip("-._")
    # Collapse runs of separators for tidiness.
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned


def sanitize_segment(raw: str) -> str:
    """Sanitize a single path segment; reject traversal and separators."""
    if not raw:
        return ""
    raw = raw.replace("\\", "/").split("/")[-1]  # no path parts
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")


def account_label(meta: dict | None, override: str | None = None) -> str:
    """Derive the account label per the documented precedence."""
    if override:
        s = sanitize_label(override)
        if s:
            return s
    meta = meta or {}
    email = meta.get("email") or ""
    s = sanitize_label(email)
    if s:
        return s
    gaia = str(meta.get("user") or meta.get("authuser") or "").strip()
    gaia = re.sub(r"[^A-Za-z0-9]", "", gaia)
    if gaia:
        return f"gaia-{gaia}"
    return "unknown-account"


def export_timestamp(filenames: list[str], captured_at: str | None = None) -> str:
    """Return YYYY-MM-DD-HH-MM-SS derived from the export filename timestamp.

    Falls back to captured_at (also reformatted) tagged '-capture' if no
    filename carries a parseable timestamp.
    """
    for fn in filenames or []:
        m = _EXPORT_TS_RE.search(fn or "")
        if m:
            return _fmt_compact(m.group(1))
    # Fallback: captured_at is an ISO string like 2026-06-16T15:45:00.000Z
    if captured_at:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", captured_at)
        if m:
            return "-".join(m.groups()) + "-capture"
    return "unknown-export"


def _fmt_compact(ts: str) -> str:
    """20260616T040104Z -> 2026-06-16-04-01-04 (UTC, as encoded)."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", ts)
    if not m:
        return sanitize_segment(ts)
    return "-".join(m.groups())


@dataclass
class Derivation:
    account_label: str
    export_ts: str
    export_raw: str  # the 20260616T040104Z form, for the manifest


def derive(meta: dict | None, filenames: list[str], captured_at: str | None,
           label_override: str | None = None) -> Derivation:
    label = account_label(meta, label_override)
    ts = export_timestamp(filenames, captured_at)
    raw = ""
    for fn in filenames or []:
        m = _EXPORT_TS_RE.search(fn or "")
        if m:
            raw = m.group(1)
            break
    return Derivation(account_label=label, export_ts=ts, export_raw=raw)
