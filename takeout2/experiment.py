"""Empirical attempt-cost measurement harness (docs/v2/01-IDENTITY-AND-SCRAPE.md §7).

We do not know, empirically, whether a Range probe, an aborted GET, or a
resumed download actually decrements Google Takeout's ~5-download-per-archive
counter. Google's own manage page shows the counter ("Number of times already
downloaded: N") and the extension scrapes it into ``dl_counts`` — so we can
MEASURE the cost by diffing that counter before and after one controlled action:

    before = dl_scrape(part_idx)      # free manage-page reading
    run exactly ONE controlled action
    after  = dl_scrape(part_idx)
    cost   = after - before           # 0 => FREE, n => COSTS n

This module is that measurement harness. It is deliberately
dependency-injectable: ``dl_scrape`` (the manage-page oracle), ``cookie_source``
(the live jar), and ``transport_factory`` (the controlled action) are all
injected, so the full study runs completely offline in tests while the real
deployment points them at ``takeout-download.usercontent.google.com``.

Benchmark mapping (docs/v2/05-PARALLELISM-AND-THROUGHPUT.md §9):

    probe    -> experiment 3: does a Range bytes=0-0 probe cost an attempt?
    abort    -> experiment 4: does a GET aborted after 1 MiB cost an attempt?
    parallel -> experiment 5: is 3-connection within-part parallelism survivable?
    resume   -> experiment 6: does resuming a PARTIAL cost a fresh attempt?
    full     -> experiment 1: baseline full download (always costs 1)

SAFETY: ``run_study`` refuses to run unless ``confirm_throwaway=True`` — every
action here spends real Google download attempts, so it may only run against a
small throwaway export, never the production multi-TB archive. It also gates
every action on the ledger's spendable budget and uses a FRESH part index per
sample, so a study can never push any part past its ~5-attempt ceiling.
"""
from __future__ import annotations

import os
import statistics
from datetime import datetime, timezone
from typing import Callable

from .contracts import AttemptCost, CostClass, DEFAULTS, PartPlan
from .ledger import BudgetExhausted

__all__ = [
    "ACTIONS",
    "ThrowawayRequired",
    "measure_action",
    "run_study",
    "write_findings",
]

#: The controlled actions the study can measure. Keep the tuple order stable —
#: findings tables are written in this order (experiments 3, 4, 5, 6, 1).
ACTIONS = ("probe", "abort", "resume", "full", "parallel")


class ThrowawayRequired(RuntimeError):
    """Raised when run_study is called without confirm_throwaway=True.

    The attempt-cost study spends REAL Google download attempts against the
    per-archive ceiling. It may only run against a small throwaway export,
    never the production multi-TB archive.
    """


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------
def measure_action(
    action: str,
    *,
    part,
    cookie_header: str,
    transport,
    dl_scrape_before: int,
    dl_scrape_after: int,
    ledger,
    archive_id: str,
    budget: int = DEFAULTS["ATTEMPT_BUDGET"],
) -> AttemptCost:
    """Run exactly one controlled action and diff Google's counter.

    ``transport`` is a callable performing exactly one controlled action on
    ``part`` and returning ``(ReasonCode, bytes_moved)``. ``dl_scrape_before``
    and ``dl_scrape_after`` are Google's OWN counter readings for the part —
    scraped free from the manage page — taken before and after the action.
    ``cost = after - before``.

    The action runs through the ledger: an attempt is reserved first (raising
    ``BudgetExhausted`` if the part has no spendable budget) and committed with
    the transport's real outcome. The post-action counter is recorded as the
    remote ground truth for the ledger.

    Timing note: because this function runs the transport itself, a caller
    holding only a *callable* scrape oracle (``run_study``) must take the
    after-reading AFTER this function returns; the two integer readings here
    are for callers (and tests) that already hold both readings.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")

    idx = part.idx
    budget_state = ledger.budget_for(archive_id, idx)
    if budget_state.spendable <= 0:
        raise BudgetExhausted(archive_id, idx, budget_state.effective_used,
                              budget, budget_state.reserve)

    # Conservative until measured: probe is classified PROBE (assumed to cost
    # a full attempt), every byte-moving action is PAYLOAD. The measured delta
    # below is what eventually overrides these assumptions.
    cost_class = CostClass.PROBE if action == "probe" else CostClass.PAYLOAD
    with ledger.attempt(archive_id, idx, cost_class,
                        note=f"cost-study:{action}") as res:
        reason, bytes_moved = transport(part=part, cookie_header=cookie_header)
        res.commit(reason, bytes_moved)

    # Google's own counter after the action is ground truth for the ledger.
    ledger.observe_remote(archive_id, idx, dl_scrape_after)

    delta = max(0, int(dl_scrape_after) - int(dl_scrape_before))
    return AttemptCost(action=action, observed_delta=delta, samples=1)


# --------------------------------------------------------------------------
# study orchestration
# --------------------------------------------------------------------------
def run_study(
    archive_id: str,
    *,
    actions=ACTIONS,
    samples: int = 3,
    cookie_source: Callable[[], str],
    transport_factory: Callable[[str, int], Callable],
    dl_scrape: Callable[[int], int],
    ledger,
    out_path: str = "docs/v2/ATTEMPT-COST-FINDINGS.md",
    confirm_throwaway: bool = False,
) -> dict:
    """Run every action ``samples`` times and write the findings report.

    Each sample runs on a FRESH part index (monotonic across the whole study)
    so no part is ever measured twice. ``dl_scrape(part_idx)`` is the free
    manage-page counter reading, injected; ``transport_factory(action,
    part_idx)`` returns a callable performing exactly that action and returning
    ``(ReasonCode, bytes_moved)``; ``cookie_source()`` returns the Cookie
    header.

    Refuses to run unless ``confirm_throwaway=True`` (see ``ThrowawayRequired``)
    and never spends more than each part's spendable budget — parts with no
    spendable budget are skipped and counted in ``skipped_exhausted``.
    """
    if not confirm_throwaway:
        raise ThrowawayRequired(
            "refusing to run the attempt-cost study without "
            "confirm_throwaway=True: every action spends real Google download "
            "attempts. Run this on a small THROWAWAY export only "
            "(docs/v2/01-IDENTITY-AND-SCRAPE.md §7, docs/v2/05-PARALLELISM-"
            "AND-THROUGHPUT.md §9).")

    results = {
        "archive_id": archive_id,
        "samples": int(samples),
        "out_path": str(out_path),
        "actions": {a: _empty_stats() for a in actions},
    }

    part_idx = 0
    for action in actions:
        stats = results["actions"][action]
        for _ in range(int(samples)):
            idx = part_idx
            part_idx += 1

            # SAFETY: a fresh part per sample plus a spendable gate means the
            # study can never push any part past its attempt budget.
            budget_state = ledger.budget_for(archive_id, idx)
            if budget_state.spendable <= 0:
                stats["skipped_exhausted"] += 1
                continue

            part = PartPlan(idx=idx)
            before = dl_scrape(idx)
            cookie_header = cookie_source()
            transport = transport_factory(action, idx)

            # The controlled action runs inside measure_action, so the true
            # after-reading can only be scraped after it returns; the sample
            # delta below is therefore after - before, timed correctly.
            measure_action(
                action, part=part, cookie_header=cookie_header,
                transport=transport,
                dl_scrape_before=before, dl_scrape_after=before,
                ledger=ledger, archive_id=archive_id, budget=budget_state.budget)
            after = dl_scrape(idx)

            # Google's counter after the action is ground truth — record it.
            ledger.observe_remote(archive_id, idx, after)

            cost = AttemptCost(action=action,
                               observed_delta=max(0, after - before), samples=1)
            stats["samples"].append(cost)
            stats["deltas"].append(cost.observed_delta)

        deltas = stats["deltas"]
        stats["min"] = min(deltas) if deltas else None
        stats["median"] = statistics.median(deltas) if deltas else None
        stats["max"] = max(deltas) if deltas else None

    write_findings(results, out_path)
    return results


def _empty_stats() -> dict:
    return {"samples": [], "deltas": [], "min": None, "median": None,
            "max": None, "skipped_exhausted": 0}


# --------------------------------------------------------------------------
# findings report
# --------------------------------------------------------------------------
_FINDINGS_HEADER = """\
# Attempt-cost findings (empirical)

> **THROWAWAY EXPORT REQUIRED.** These measurements consume real Google
> download attempts against the per-archive ceiling. They MUST be run on a
> small throwaway export — never the production multi-TB archive
> (docs/v2/01-IDENTITY-AND-SCRAPE.md §7, docs/v2/05-PARALLELISM-AND-THROUGHPUT.md §9).
"""

_FINDINGS_FOOTER = """\
> **Conservative assumptions remain in force until a non-zero sample exists.**
> Until an action produces a measured non-zero delta, every request — Range
> probe, aborted GET, resumed download — is assumed to cost 1 attempt against
> the ~5-download-per-archive ceiling (docs/v2/00-CONTRACTS.md §1.2). A
> measured FREE (0) or COSTS 1+ result above supersedes that assumption for
> the action it was measured on.
"""


def write_findings(results: dict, path: str) -> str:
    """Append-or-create the findings markdown report. Returns the path.

    A brand-new file gets the throwaway-requirement header once; every call
    appends a dated study section with the table ``Action | Deltas | Median |
    Verdict`` (FREE when the median delta is 0, else COSTS 1+) and the
    conservative-assumptions footer.
    """
    path = str(path)
    rows = []
    for action in ACTIONS:
        stats = (results.get("actions") or {}).get(action)
        if stats is None:
            continue
        deltas = stats.get("deltas") or []
        median = stats.get("median")
        delta_str = ", ".join(str(d) for d in deltas) if deltas else "—"
        if median is None:
            median_str = "—"
            verdict = "—"
        elif median == 0:
            median_str = "0"
            verdict = "FREE"
        else:
            median_str = (str(int(median)) if float(median).is_integer()
                          else str(median))
            verdict = "COSTS 1+"
        rows.append(f"| {action} | {delta_str} | {median_str} | {verdict} |")
    table = ("| Action | Deltas | Median | Verdict |\n"
             "|---|---|---|---|\n" + "\n".join(rows))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    block = (
        f"\n\n## Study — archive {results.get('archive_id', '?')} @ {stamp}\n\n"
        + table + "\n\n" + _FINDINGS_FOOTER + "\n")

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    new = not (os.path.exists(path) and os.path.getsize(path) > 0)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write(_FINDINGS_HEADER)
        fh.write(block)
    return path
