"""Zero-probe discovery — learn the part list WITHOUT contacting Google.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §5.1.

v1 swept ``i=0..MAX_PARTS`` with a 1-byte Range probe per part. On a 62-part
archive that is 63 probes, each of which we must assume costs a download
attempt, and each of which shortens the already ~1-2 min cookie lifetime. v2
never probes unless the operator explicitly opts in.

Priority ladder (use the FIRST that succeeds):

1. ``expectedParts`` + ``uris``/``sizes``/``filenames``/``dl_counts`` from the
   capture payload (the extension scrapes these free from the manage page).
2. A persisted plan already in ``state.db``.
3. Filename arithmetic (``takeout-<ts>-<n>-<idx>.zip``).
4. Bounded probe sweep — ONLY with ``allow_probes=True``; every probe is a
   ledger-reserved ``PROBE``; exponential bracketing, never a linear sweep.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .contracts import (DL_COUNT_RE, TAKEOUT_FILENAME_RE, DEFAULTS, PartPlan)

log = logging.getLogger("takeout2.plan")

__all__ = [
    "PlanResult", "plan_from_payload", "plan_from_filenames",
    "plans_from_state", "resolve_plan",
]


@dataclass
class PlanResult:
    """What the planner produced and how, with the attempt cost it incurred."""

    source: str                    # "PAYLOAD" | "STATE_DB" | "FILENAMES" | "PROBES"
    parts: list[PartPlan] = field(default_factory=list)
    probes_used: int = 0
    cost_attempts: int = 0         # 0 unless the PROBE path ran
    ambiguous: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.parts)


# --------------------------------------------------------------------------
# Payload-driven planning — the free path
# --------------------------------------------------------------------------
def _filename_idx(name: str) -> Optional[int]:
    """Extract ``<idx>`` from ``takeout-<ts>-<n>-<idx>.zip``."""
    m = TAKEOUT_FILENAME_RE.search(name or "")
    if not m:
        return None
    try:
        return int(m.group(2))
    except ValueError:
        return None


def plan_from_payload(payload: Optional[dict]) -> PlanResult:
    """Build PartPlans from capture-payload metadata. Costs ZERO attempts.

    Accepts the shape the extension POSTs (``docs/v2/01-IDENTITY…`` §6):

        parts_expected: int                    # "part X of N" scrape
        uris:   {filename: download_uri}
        sizes:  {filename: bytes}
        filenames: [str]
        dl_counts: {filename: n}               # Google's own counter
    """
    payload = payload or {}
    parts_expected = payload.get("parts_expected")
    uris = payload.get("uris") or {}
    sizes = payload.get("sizes") or {}
    filenames = payload.get("filenames") or []
    dl_counts = payload.get("dl_counts") or {}

    if not parts_expected and not filenames:
        return PlanResult(source="PAYLOAD", message="payload carries no part metadata")

    if not filenames and uris:
        filenames = list(uris.keys())
    if not filenames and parts_expected and sizes:
        filenames = [f"part-{i:03d}.zip" for i in range(parts_expected)]

    n = int(parts_expected) if parts_expected else len(filenames)
    parts: list[PartPlan] = []
    seen: set[str] = set()

    for idx in range(n):
        name = filenames[idx] if idx < len(filenames) else f"part-{idx:03d}.zip"
        if name in seen:
            continue
        seen.add(name)
        parts.append(PartPlan(
            idx=idx,
            filename=name,
            url=uris.get(name),
            size_expected=sizes.get(name),
            dl_count_remote=dl_counts.get(name),
        ))

    # Ambiguity guard: two distinct export timestamps in one payload is a
    # scrape error, not two exports (00-CONTRACTS §5).
    ts_values = {m for name in filenames
                 if (m := re.search(r"takeout-(\d{8}T\d{6}Z)-", name))}

    result = PlanResult(source="PAYLOAD", parts=parts, cost_attempts=0,
                        ambiguous=len(ts_values) > 1,
                        message=f"{len(parts)} parts from payload metadata")
    return result


# --------------------------------------------------------------------------
# Filename-arithmetic planning — also free
# --------------------------------------------------------------------------
def plan_from_filenames(filenames: list[str]) -> PlanResult:
    """Derive the part list purely from filenames already in hand.

    Supports both ``takeout-<ts>-<n>-<idx>.zip`` (index embedded) and
    ``...-of-<total>.zip`` style naming when present.
    """
    names = sorted({f for f in (filenames or []) if f})
    if not names:
        return PlanResult(source="FILENAMES", message="no filenames given")

    # Try the embedded-index scheme first.
    indexed: list[tuple[int, str]] = []
    for name in names:
        idx = _filename_idx(name)
        if idx is not None:
            indexed.append((idx, name))
    if indexed:
        indexed.sort(key=lambda t: t[0])
        return PlanResult(
            source="FILENAMES",
            parts=[PartPlan(idx=i, filename=name) for i, name in indexed],
            cost_attempts=0,
            message=f"{len(indexed)} parts from filename indices",
        )

    # Fall back to lexical ordering with a "of N" total if present.
    of_total = None
    for name in names:
        m = re.search(r"of[-_]?(\d+)\.zip$", name, re.I)
        if m:
            of_total = int(m.group(1))
            break
    parts = [PartPlan(idx=i, filename=name) for i, name in enumerate(names)]
    result = PlanResult(source="FILENAMES", parts=parts, cost_attempts=0,
                        message=f"{len(parts)} parts from filename ordering")
    if of_total is not None:
        result.ambiguous = of_total != len(parts)
    return result


# --------------------------------------------------------------------------
# Persisted plan — free, and the resume-critical path
# --------------------------------------------------------------------------
def plans_from_state(store, archive_id: str) -> PlanResult:
    """Rebuild plans from part rows already persisted in state.db.

    This is what makes a cookie-refresh resume skip re-discovery entirely:
    the plan was computed once, cheaply, and is reused.
    """
    rows = store.list_parts(archive_id) if store else []
    if not rows:
        return PlanResult(source="STATE_DB", message="no persisted plan")
    parts = [
        PartPlan(idx=r.idx, filename=r.filename, url=r.url,
                 size_expected=r.size_expected)
        for r in rows
    ]
    return PlanResult(source="STATE_DB", parts=parts, cost_attempts=0,
                      message=f"{len(parts)} parts from persisted plan")


# --------------------------------------------------------------------------
# Bounded probe sweep — opt-in only, every probe ledger-reserved
# --------------------------------------------------------------------------
def plan_from_probes(*, first_url: str, max_probes: int,
                     ledger, archive_id: str,
                     budget: int = DEFAULTS["ATTEMPT_BUDGET"],
                     classify_fun=None) -> PlanResult:
    """Exponential bracketing sweep as the LAST resort.

    ``classify_fun`` maps a response to a ReasonCode; it must return
    ``END_OF_RANGE`` for HTML past the end (never ``AUTH_REDIRECT``). This
    keeps the phantom-last-index bug out: a past-the-end hit is a stop, not
    a part.
    """
    probes_used = 0
    parts: list[PartPlan] = []
    idx = 0
    step = 1
    # Exponential bracketing: probe 0, 1, 2, 4, 8 ... then bisect the gap.
    while probes_used < max_probes:
        with ledger.attempt(archive_id, idx, cost_class="PROBE",
                            note=f"discovery probe idx={idx}") as res:
            probes_used += 1
            reason = classify_fun(first_url, idx) if classify_fun else None
            res.commit(reason if reason else "END_OF_RANGE")
        if reason == "END_OF_RANGE" and idx > 0:
            # Past the end; the previous bracketing bound is the last real idx.
            break
        if idx == 0:
            parts.append(PartPlan(idx=0))
        idx += step
        step *= 2
    if not parts and idx == 0:
        parts.append(PartPlan(idx=0))
    return PlanResult(source="PROBES", parts=parts, probes_used=probes_used,
                      cost_attempts=probes_used,
                      message=f"{probes_used} probes spent on discovery")


# --------------------------------------------------------------------------
# The priority ladder
# --------------------------------------------------------------------------
def resolve_plan(*, payload: Optional[dict], store, archive_id: str,
                 filenames: Optional[list[str]] = None,
                 allow_probes: bool = False,
                 ledger=None, first_url: str = "",
                 classify_fun=None,
                 max_probes: int = DEFAULTS["MAX_DISCOVERY_PROBES"]) -> PlanResult:
    """Run the ladder; return the first usable plan.

    Priority: PAYLOAD -> STATE_DB -> FILENAMES -> PROBES(opt-in).
    Never probes unless ``allow_probes`` and a ledger are both given.
    """
    if payload:
        result = plan_from_payload(payload)
        if result.ok:
            return result

    result = plans_from_state(store, archive_id)
    if result.ok:
        return result

    if filenames:
        result = plan_from_filenames(filenames)
        if result.ok:
            return result

    if allow_probes and ledger and first_url:
        return plan_from_probes(first_url=first_url, max_probes=max_probes,
                                ledger=ledger, archive_id=archive_id,
                                classify_fun=classify_fun)

    return PlanResult(source="PROBES", message=(
        "no plan derivable from local state; pass --allow-discovery-probes "
        "to permit a bounded probe sweep"))
