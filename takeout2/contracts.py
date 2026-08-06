"""Shared contracts for takeout2 — enums, dataclasses, and pure helpers.

NORMATIVE. This module is the Python mirror of ``docs/v2/00-CONTRACTS.md``.
Every other module imports its vocabulary from here.

Hard rules encoded in this file:
  * Cost classification of outbound requests (``CostClass``).
  * The full ``ReasonCode`` vocabulary, including the critical distinction
    between ``END_OF_RANGE`` (clean stop) and ``AUTH_REDIRECT`` (cookie dead)
    which v1 conflated and which cost us a 2.8 TB re-download.
  * Label provenance ranking, so a better label upgrades a job instead of
    orphaning it.

This module MUST remain dependency-free (stdlib only) and side-effect free.
No I/O, no network, no SQLite. It is safe to import from anywhere.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "CostClass", "JobStatus", "PartStatus", "VerifyState", "ReasonCode",
    "LabelSource", "Confidence", "AccountIdentity", "PartPlan", "IdentityRecord",
    "AttemptCost", "TERMINAL_JOB_STATES", "TERMINAL_PART_STATES",
    "RETRYABLE_REASONS", "AUTH_REASONS", "FATAL_REASONS",
    "EXPORT_TS_RE", "TAKEOUT_FILENAME_RE", "DL_COUNT_RE",
    "sanitize_label", "sanitize_segment", "format_export_ts", "parse_export_ts",
    "DEFAULTS",
]


# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------
class CostClass(str, enum.Enum):
    """How much a request is assumed to cost against Google's per-archive limit.

    We assume the WORST CASE until measured empirically (see
    docs/v2/01-IDENTITY-AND-SCRAPE.md §7): a probe is assumed to cost a full
    attempt. Behaving conservatively can only waste time; behaving
    optimistically can permanently burn an archive.
    """

    PAYLOAD = "PAYLOAD"   # a real byte-moving GET — the only justified cost
    PROBE = "PROBE"       # Range 0-0 / HEAD — assumed to cost 1 attempt
    FREE = "FREE"         # non-Google host, local disk, our own Chrome via CDP

    @property
    def counts_against_budget(self) -> bool:
        return self is not CostClass.FREE


# --------------------------------------------------------------------------
# State enums — string values are normative, do not rename
# --------------------------------------------------------------------------
class JobStatus(str, enum.Enum):
    DISCOVERING = "DISCOVERING"
    READY = "READY"
    DOWNLOADING = "DOWNLOADING"
    PAUSED = "PAUSED"
    NEEDS_COOKIE = "NEEDS_COOKIE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class PartStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PARTIAL = "PARTIAL"                    # bytes on disk, resumable
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # needs a human decision


class VerifyState(str, enum.Enum):
    """Escalating confidence. STRUCT_OK is the default acceptance bar.

    HASH_OK requires a full read of the file and is therefore OPT-IN ONLY:
    a full testzip over a multi-TB archive on the JuiceFS FUSE mount took
    hours and risked FUSE stalls.
    """

    UNVERIFIED = "UNVERIFIED"
    SIZE_OK = "SIZE_OK"
    STRUCT_OK = "STRUCT_OK"
    HASH_OK = "HASH_OK"
    CORRUPT = "CORRUPT"

    @property
    def rank(self) -> int:
        return _VERIFY_RANK[self]


_VERIFY_RANK = {
    VerifyState.CORRUPT: -1,
    VerifyState.UNVERIFIED: 0,
    VerifyState.SIZE_OK: 1,
    VerifyState.STRUCT_OK: 2,
    VerifyState.HASH_OK: 3,
}


class ReasonCode(str, enum.Enum):
    """Outcome of a single attempt. See docs/v2/00-CONTRACTS.md §3.4."""

    OK_COMPLETE = "OK_COMPLETE"
    OK_PARTIAL = "OK_PARTIAL"
    AUTH_REDIRECT = "AUTH_REDIRECT"      # 302 -> accounts.google.com
    AUTH_401 = "AUTH_401"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"    # the 5-download ceiling
    NOT_FOUND = "NOT_FOUND"
    END_OF_RANGE = "END_OF_RANGE"        # HTML past last part — NOT an auth error
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    DISK_ERROR = "DISK_ERROR"
    ABORTED = "ABORTED"


TERMINAL_JOB_STATES = frozenset({JobStatus.COMPLETE, JobStatus.FAILED})
TERMINAL_PART_STATES = frozenset({PartStatus.DONE, PartStatus.SKIPPED})

#: Transient conditions — safe to retry, but ONLY inside a fresh cookie burst
#: window and ONLY with budget headroom. Never retry instantly in a tight loop.
RETRYABLE_REASONS = frozenset({
    ReasonCode.NETWORK_ERROR,
    ReasonCode.RATE_LIMITED,
    ReasonCode.OK_PARTIAL,
})

#: Cookie problems — park the job in NEEDS_COOKIE and get a fresh capture.
#: Note END_OF_RANGE is deliberately ABSENT: that was v1's fatal conflation.
AUTH_REASONS = frozenset({
    ReasonCode.AUTH_REDIRECT,
    ReasonCode.AUTH_401,
})

#: Nothing the machine can do — requires an operator decision.
FATAL_REASONS = frozenset({
    ReasonCode.LIMIT_EXCEEDED,
    ReasonCode.DISK_ERROR,
})


# --------------------------------------------------------------------------
# Identity provenance
# --------------------------------------------------------------------------
class LabelSource(str, enum.Enum):
    """Where an account label came from. Ordering is meaningful.

    A label may only ever be replaced by one of STRICTLY HIGHER rank. This is
    what prevents a transient failed scrape from downgrading a good label and
    orphaning an in-progress multi-terabyte job.
    """

    UNKNOWN = "UNKNOWN"
    GAIA_FALLBACK = "GAIA_FALLBACK"
    SCRAPED_LABEL = "SCRAPED_LABEL"
    SCRAPED_EMAIL = "SCRAPED_EMAIL"
    OPERATOR_OVERRIDE = "OPERATOR_OVERRIDE"

    @property
    def rank(self) -> int:
        return _LABEL_RANK[self]

    def outranks(self, other: "LabelSource") -> bool:
        return self.rank > other.rank


_LABEL_RANK = {
    LabelSource.UNKNOWN: 0,
    LabelSource.GAIA_FALLBACK: 1,
    LabelSource.SCRAPED_LABEL: 2,
    LabelSource.SCRAPED_EMAIL: 3,
    LabelSource.OPERATOR_OVERRIDE: 4,
}


class Confidence(str, enum.Enum):
    LOWEST = "LOWEST"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_CONFIDENCE_BY_SOURCE = {
    LabelSource.OPERATOR_OVERRIDE: Confidence.HIGH,
    LabelSource.SCRAPED_EMAIL: Confidence.HIGH,
    LabelSource.SCRAPED_LABEL: Confidence.MEDIUM,
    LabelSource.GAIA_FALLBACK: Confidence.LOW,
    LabelSource.UNKNOWN: Confidence.LOWEST,
}


# --------------------------------------------------------------------------
# Regexes — the export timestamp is the one field Google gives us reliably
# --------------------------------------------------------------------------
#: 20260616T040104Z — the export-creation instant baked into every filename.
EXPORT_TS_RE = re.compile(r"(\d{8}T\d{6}Z)")

#: takeout-20260616T040104Z-9-001.zip
TAKEOUT_FILENAME_RE = re.compile(r"takeout-(\d{8}T\d{6}Z)-\d+-(\d+)\.zip", re.I)

#: "takeout-....zip (Number of times already downloaded: 5)"
#: Google's OWN attempt counter, scraped from the manage page. Ground truth.
DL_COUNT_RE = re.compile(
    r"(takeout-\d{8}T\d{6}Z-\d+-\d+\.zip)\s*"
    r"\(Number of times already downloaded:\s*(\d+)\)",
    re.I,
)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def sanitize_label(raw: Optional[str]) -> str:
    """Lowercase, strip any @domain, allow only [a-z0-9._-]. May return ''."""
    if not raw:
        return ""
    value = raw.strip().lower()
    if "@" in value:
        value = value.split("@", 1)[0]
    value = re.sub(r"[^a-z0-9._-]", "-", value).strip("-._")
    return re.sub(r"-{2,}", "-", value)


def sanitize_segment(raw: Optional[str]) -> str:
    """Sanitize one path segment; strips traversal and separators."""
    if not raw:
        return ""
    value = raw.replace("\\", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-._")


def format_export_ts(raw: str) -> str:
    """20260616T040104Z -> 2026-06-16-04-01-04 (folder-safe, UTC as encoded)."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", raw or "")
    if not m:
        return sanitize_segment(raw)
    return "-".join(m.groups())


def parse_export_ts(filenames) -> Optional[str]:
    """Return the majority ``YYYYMMDDTHHMMSSZ`` across filenames, else None.

    Majority rather than first-match: if a page scrape mixes two exports, the
    first filename could be the wrong one. Callers should flag ambiguity when
    more than one distinct value appears.
    """
    counts: dict[str, int] = {}
    for name in filenames or []:
        m = EXPORT_TS_RE.search(name or "")
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountIdentity:
    """Who an export belongs to. Display/foldering only — NEVER a job key."""

    gaia_user: str
    authuser: str = "0"
    email: Optional[str] = None
    label: Optional[str] = None
    label_source: LabelSource = LabelSource.UNKNOWN

    @property
    def confidence(self) -> Confidence:
        return _CONFIDENCE_BY_SOURCE[self.label_source]

    def folder_name(self) -> str:
        """Deterministic folder name; always non-empty."""
        for candidate in (self.label, self.email):
            cleaned = sanitize_label(candidate)
            if cleaned:
                return cleaned
        gaia = re.sub(r"[^A-Za-z0-9]", "", self.gaia_user or "")
        return f"gaia-{gaia}" if gaia else "unknown-account"

    def upgrades_over(self, other: Optional["AccountIdentity"]) -> bool:
        """True if adopting self over other is a strict provenance improvement."""
        if other is None:
            return True
        return self.label_source.outranks(other.label_source)


@dataclass
class PartPlan:
    """One part of an archive, planned WITHOUT any network request."""

    idx: int
    filename: Optional[str] = None
    url: Optional[str] = None
    size_expected: Optional[int] = None
    #: Google's own counter from the manage page — authoritative when present.
    dl_count_remote: Optional[int] = None

    def remaining_attempts(self, budget: int, local_used: int = 0) -> int:
        used = max(local_used, self.dl_count_remote or 0)
        return max(0, budget - used)


@dataclass
class IdentityRecord:
    """The full capture-time identity of an export."""

    archive_id: str
    export_raw: str
    account: AccountIdentity
    parts_expected: Optional[int] = None
    captured_at: Optional[str] = None
    page_url: Optional[str] = None
    export_ts_ambiguous: bool = False
    scrape_report: list = field(default_factory=list)

    @property
    def export_ts(self) -> str:
        return format_export_ts(self.export_raw)

    def relative_dir(self) -> str:
        return f"{self.account.folder_name()}/{self.export_ts}"


@dataclass(frozen=True)
class AttemptCost:
    """Result of the empirical attempt-cost study (doc 01 §7)."""

    action: str
    observed_delta: int
    samples: int

    @property
    def is_free(self) -> bool:
        return self.observed_delta == 0


#: Defaults mirroring the TK2_* environment variables in 00-CONTRACTS.md §7.
DEFAULTS = {
    "ATTEMPT_BUDGET": 5,
    "BUDGET_RESERVE": 1,
    "PARALLEL": 4,
    "COOKIE_BUDGET_MS": 20_000,
    "MAX_DISCOVERY_PROBES": 3,
    "VERIFY_LEVEL": VerifyState.STRUCT_OK.value,
    "CDP_URL": "http://127.0.0.1:9222",
}
