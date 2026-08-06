"""Pure backoff policy: how long to wait, and when to stop trying entirely.

NORMATIVE companion to ``takeout2/classify.py``.

WHY THIS MODULE EXISTS
----------------------
Google permits **5 downloads per archive part, ever**, and the archive itself
expires ~7 days after creation. That makes every outbound request an
irreplaceable resource: a burnt attempt cannot be bought back with time,
patience, or a fresh cookie.

``classify.py`` already names a 429 correctly (``ReasonCode.RATE_LIMITED``) and
a 5xx correctly (``ReasonCode.NETWORK_ERROR``) — but naming is not pacing.
Without a policy layer the engine would re-issue the request on the next loop
iteration and shred the 5-attempt ceiling in under a second, permanently
losing the part. This module is that policy layer.

DESIGN RULES (all three are load-bearing)
-----------------------------------------
1. **Purely computational.** No ``time.sleep``, no sockets, no disk. The
   caller (``engine.py``) performs the actual sleep. A policy that sleeps
   cannot be unit-tested; a policy that only *computes* a duration can be
   exhaustively tested in microseconds.
2. **No ambient randomness.** Jitter is derived from an injectable ``rand``
   callable. With ``rand=None`` the result is the un-jittered midpoint, so
   default behaviour is reproducible.
3. **Waiting is only ever the answer for transient conditions.** A dead
   cookie does not heal by waiting: sleeping 15 minutes and re-requesting
   converts one wasted attempt into two. Auth reasons therefore return
   ``should_wait=False`` and are handed back to the caller to park the job in
   ``NEEDS_COOKIE``.

REASON CLASSES USED (real ``ReasonCode`` members, verified against
``contracts.py``)
-----------------------------------------------------------------
* transient / waitable: ``RATE_LIMITED`` (429), ``NETWORK_ERROR``
  (502/503/504/500, unexpected redirects, transport errors, untrusted bodies)
* never waitable, cookie is dead: ``AUTH_REDIRECT``, ``AUTH_401``
  (i.e. ``contracts.AUTH_REASONS``)
* never waitable, operator required: ``LIMIT_EXCEEDED``, ``DISK_ERROR``
  (i.e. ``contracts.FATAL_REASONS``)
* never waitable, nothing is wrong: ``OK_COMPLETE``, ``OK_PARTIAL``,
  ``END_OF_RANGE``, ``NOT_FOUND``, ``ABORTED``
"""
from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from .contracts import AUTH_REASONS, FATAL_REASONS, ReasonCode

__all__ = [
    "DEFAULT_BASE_S", "DEFAULT_CAP_S", "MAX_RATE_LIMIT_RETRIES",
    "TRANSIENT_REASONS", "NO_WAIT_REASONS",
    "SOURCE_RETRY_AFTER", "SOURCE_EXPONENTIAL", "SOURCE_NONE",
    "BackoffDecision", "parse_retry_after", "backoff_delay", "decide",
]

#: First rate-limit wait. Deliberately generous: Google's 429 window is
#: minutes, not seconds, and an early retry costs a permanent attempt.
DEFAULT_BASE_S = 30.0

#: Ceiling on any single wait, including a clamped ``Retry-After``. 15 minutes
#: is the point past which a download cookie is likely to have idled out
#: anyway, so waiting longer buys nothing.
DEFAULT_CAP_S = 900.0

#: How many times a transient reason may be retried before we stop. With
#: 5 attempts total per part and a reserve, 4 is already the whole budget.
MAX_RATE_LIMIT_RETRIES = 4

SOURCE_RETRY_AFTER = "retry-after"
SOURCE_EXPONENTIAL = "exponential"
SOURCE_NONE = "none"

#: The only reasons for which waiting can possibly change the outcome.
#: ``NETWORK_ERROR`` is what ``classify.py`` returns for every 5xx, so this
#: covers 500/502/503/504 as well as transport-level failures.
TRANSIENT_REASONS = frozenset({
    ReasonCode.RATE_LIMITED,
    ReasonCode.NETWORK_ERROR,
})

#: Reasons where sleeping is actively harmful or simply pointless.
NO_WAIT_REASONS = frozenset({
    ReasonCode.OK_COMPLETE,
    ReasonCode.OK_PARTIAL,
    ReasonCode.END_OF_RANGE,
    ReasonCode.NOT_FOUND,
    ReasonCode.ABORTED,
}) | AUTH_REASONS | FATAL_REASONS


def parse_retry_after(value: Optional[str], *, now: Optional[float] = None) -> Optional[float]:
    """Seconds to wait according to a ``Retry-After`` header value.

    Accepts both forms RFC 9110 permits:

    * delta-seconds — ``"120"``
    * HTTP-date     — ``"Wed, 21 Oct 2026 07:28:00 GMT"``

    Returns ``None`` when the value is absent or unparseable, so the caller can
    fall back to exponential backoff. Never returns a negative number: a date
    already in the past clamps to ``0.0``.

    ``now`` is injectable purely so HTTP-date handling is testable without
    freezing the clock.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    # delta-seconds. Accept a float spelling too; servers occasionally send it.
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        target = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None

    reference = time.time() if now is None else now
    return max(0.0, target - reference)


def backoff_delay(attempt: int, *, base: float = DEFAULT_BASE_S,
                  cap: float = DEFAULT_CAP_S, jitter: float = 0.2,
                  rand: Optional[Callable[[], float]] = None) -> float:
    """Exponential backoff for a 1-based ``attempt``, capped and jittered.

    ``base * 2**(attempt - 1)`` is clamped to ``cap`` first, then multiplied by
    ``1 + jitter * (2 * rand() - 1)``, then clamped into ``[0.0, cap]`` so
    ``cap`` is a hard ceiling rather than a suggestion.

    ``rand`` must yield a float in ``[0.0, 1.0]``. When it is ``None`` the
    midpoint ``0.5`` is used, which cancels the jitter term exactly — that is
    what keeps the default path deterministic and free of ambient randomness.
    """
    if attempt < 1:
        attempt = 1
    cap = max(0.0, cap)
    base = max(0.0, base)

    # 2**(attempt-1) grows fast; clamp the exponent so a bogus attempt number
    # cannot produce an OverflowError instead of a delay.
    exponent = min(attempt - 1, 32)
    raw = min(base * (2.0 ** exponent), cap)

    draw = 0.5 if rand is None else float(rand())
    draw = min(1.0, max(0.0, draw))
    jittered = raw * (1.0 + jitter * (2.0 * draw - 1.0))
    return max(0.0, min(jittered, cap))


@dataclass(frozen=True)
class BackoffDecision:
    """What the caller should do next. Frozen: a decision is a fact, not state."""

    should_wait: bool
    delay_s: float
    source: str        # "retry-after" | "exponential" | "none"
    give_up: bool      # True => stop, do NOT spend another attempt
    detail: str

    @property
    def delay_ms(self) -> int:
        """Convenience for loggers/schedulers that think in milliseconds."""
        return int(round(self.delay_s * 1000.0))


def _no_wait(detail: str, *, give_up: bool = False) -> BackoffDecision:
    return BackoffDecision(should_wait=False, delay_s=0.0, source=SOURCE_NONE,
                           give_up=give_up, detail=detail)


def decide(reason, *, attempt: int, headers: Optional[Mapping[str, str]] = None,
           max_retries: int = MAX_RATE_LIMIT_RETRIES, cap: float = DEFAULT_CAP_S,
           base: float = DEFAULT_BASE_S, rand: Optional[Callable[[], float]] = None,
           now: Optional[float] = None) -> BackoffDecision:
    """Decide whether to wait, for how long, or to stop entirely.

    ``attempt`` is 1-based and counts the attempt that JUST FAILED.

    Semantics:

    * ``RATE_LIMITED`` / ``NETWORK_ERROR`` — honour a sane positive
      ``Retry-After`` (``source="retry-after"``, clamped to ``cap`` so an
      absurd ``99999`` cannot park the job past archive expiry); otherwise
      exponential (``source="exponential"``).
    * ``attempt > max_retries`` — ``give_up=True, should_wait=False``.
      Rationale: every further retry spends part of the irreplaceable
      5-downloads-per-part budget, and a part with no attempts left is
      permanently unrecoverable. Stopping while attempts remain keeps a
      human-driven recovery possible; grinding through them does not.
    * ``AUTH_REDIRECT`` / ``AUTH_401`` — **never** waited on
      (``should_wait=False, source="none"``). A dead cookie does not heal by
      waiting; the caller must park the job in ``NEEDS_COOKIE`` and capture a
      fresh cookie. ``give_up=True`` here means "not retryable as-is", not
      "budget exhausted".
    * ``LIMIT_EXCEEDED`` / ``DISK_ERROR`` — no wait, ``give_up=True``; an
      operator decision is required.
    * ``OK_COMPLETE`` / ``OK_PARTIAL`` / ``END_OF_RANGE`` / ``NOT_FOUND`` /
      ``ABORTED`` — no wait, nothing to back off from.

    Performs no sleeping and no I/O; the caller does the sleeping.
    """
    if reason in AUTH_REASONS:
        return _no_wait(
            f"{getattr(reason, 'value', reason)}: cookie is dead — waiting cannot "
            f"revive it and re-requesting costs an attempt; recapture the cookie",
            give_up=True,
        )

    if reason in FATAL_REASONS:
        return _no_wait(
            f"{getattr(reason, 'value', reason)}: fatal — operator decision required",
            give_up=True,
        )

    if reason not in TRANSIENT_REASONS:
        # OK_*, END_OF_RANGE, NOT_FOUND, ABORTED and anything unknown.
        return _no_wait(f"{getattr(reason, 'value', reason)}: not a transient condition")

    if attempt > max_retries:
        return _no_wait(
            f"{getattr(reason, 'value', reason)}: attempt {attempt} exceeds "
            f"max_retries={max_retries} — stopping rather than spending more of "
            f"the irreplaceable 5-attempt budget",
            give_up=True,
        )

    retry_after_raw: Optional[str] = None
    if headers:
        for key, value in headers.items():
            if key.lower() == "retry-after":
                retry_after_raw = value
                break

    requested = parse_retry_after(retry_after_raw, now=now)
    if requested is not None and requested > 0.0:
        clamped = min(requested, max(0.0, cap))
        note = "" if clamped == requested else f" (clamped from {requested:.0f}s to cap {cap:.0f}s)"
        return BackoffDecision(
            should_wait=True,
            delay_s=clamped,
            source=SOURCE_RETRY_AFTER,
            give_up=False,
            detail=(f"{getattr(reason, 'value', reason)}: server asked for "
                    f"{requested:.0f}s{note}; waiting {clamped:.1f}s "
                    f"(attempt {attempt}/{max_retries})"),
        )

    delay = backoff_delay(attempt, base=base, cap=cap, rand=rand)
    return BackoffDecision(
        should_wait=True,
        delay_s=delay,
        source=SOURCE_EXPONENTIAL,
        give_up=False,
        detail=(f"{getattr(reason, 'value', reason)}: no usable Retry-After; "
                f"exponential wait {delay:.1f}s (attempt {attempt}/{max_retries})"),
    )
