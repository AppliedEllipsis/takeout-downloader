"""The cookie-window burst scheduler.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §5.3 and
``docs/v2/05-PARALLELISM-AND-THROUGHPUT.md`` §2.

The one design fact that makes this module work:

    The download cookie dies after ~1-2 min IDLE, but an IN-FLIGHT stream
    survives for hours (auth is checked only at request *start*).

Therefore the scheduler's goal is NOT "keep N downloads going forever". It is:

    START all the streams inside one narrow window after a fresh cookie pull,
    then leave them alone for as long as they want to run.

Safety rules enforced here (each is a "STOP and reconsider" invariant):

    * A reservation is taken BEFORE a request, never after.
    * Never instant-retry a stream into the same possibly-dead cookie.
    * Canary first: prove the cookie with one stream before opening the rest,
      so a dead cookie costs 1 attempt, not N.
    * No library-internal retries, no redirect-following (classify.py must
      see the raw 302). Disabled explicitly.
    * A 200 response to a Range resume means the server ignored Range:
      truncate and restart, never append (that creates the "oversized"
      corruption verify.py detects).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .classify import ResponseFacts, classify
from .contracts import (CostClass, DEFAULTS, JobStatus, PartStatus, ReasonCode,
                        VerifyState)
from .cookie import LiveCookieJar
from .verify import scan_parts_dir, verify_part

log = logging.getLogger("takeout2.engine")

__all__ = [
    "BurstResult", "EngineConfig", "BurstEngine", "ChunkProgress",
    "zero_out_attempts", "NO_RETRY_SESSION",
]

WRITE_CHUNK = 8 * 1024 * 1024          # 8 MiB, per 05-PARALLELISM §7
COOKIE_BUDGET_S = DEFAULTS["COOKIE_BUDGET_MS"] / 1000.0


@dataclass
class ChunkProgress:
    idx: int
    bytes_moved: int
    wall_ms: int
    net_ms: int
    write_ms: int


@dataclass
class BurstResult:
    started: int = 0
    completed_ok: int = 0
    completed_partial: int = 0
    failed: dict[str, int] = field(default_factory=dict)   # ReasonCode -> n
    budget_exhausted: int = 0
    attempts_spent: int = 0
    bytes_moved: int = 0
    canary_passed: bool = False


@dataclass
class EngineConfig:
    parallel: int = DEFAULTS["PARALLEL"]
    cookie_budget_s: float = COOKIE_BUDGET_S
    write_chunk: int = WRITE_CHUNK
    verify_level: VerifyState = VerifyState.STRUCT_OK
    attempt_budget: int = DEFAULTS["ATTEMPT_BUDGET"]
    budget_reserve: int = DEFAULTS["BUDGET_RESERVE"]
    max_streams_per_part: int = 1      # within-part parallel: FORBIDDEN >1
    stall_s: int = 90
    dead_s: int = 600


def zero_out_attempts():
    """Build a requests.Session that can never retry on its own.

    A retry we did not reserve is a ledger violation: it could be Google
    counting a second attempt that our ledger never saw. Redirect-following
    is also off so classify.py sees the raw 302 (a dead cookie, not a
    "range not supported" lie).
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0,
                                      redirect=0, status=0)))
    session.max_redirects = 0
    return session


NO_RETRY_SESSION = None  # lazily built in BurstEngine; see ._session()


# --------------------------------------------------------------------------
class BurstEngine:
    """Owns one burst: plan from local state, canary, open N streams.

    Pure orchestration — the actual bytes move through ``fetch`` callables
    injected here so tests can substitute a fake transport with no network.
    """

    def __init__(self, store, ledger, cookie_source: LiveCookieJar,
                 parts_dir: str,
                 config: Optional[EngineConfig] = None,
                 *, fetch: Optional[Callable] = None,
                 on_chunk: Optional[Callable[[ChunkProgress], None]] = None,
                 now_fn: Optional[Callable[[], float]] = None):
        self.store = store
        self.ledger = ledger
        self.cookie_source = cookie_source
        self.parts_dir = parts_dir
        self.config = config or EngineConfig()
        self.fetch = fetch or self._real_fetch
        self.on_chunk = on_chunk or (lambda _: None)
        self._now = now_fn or time.monotonic
        self._session = None

    # -- session ----------------------------------------------------------
    def _session(self):
        if self._session is None:
            self._session = zero_out_attempts()
        return self._session

    # -- burst -------------------------------------------------------------
    def run_burst(self, archive_id: str,
                  candidates: Optional[Iterable] = None) -> BurstResult:
        """One cookie-window burst. Non-blocking per stream; this function
        returns once the burst has been OPENED (streams continue elsewhere)."""
        result = BurstResult()

        # 1. Plan from local state ONLY (one scandir, no stat storm).
        on_disk = scan_parts_dir(self.parts_dir)

        if candidates is None:
            candidates = self.store.list_parts(archive_id)

        work = []
        for part in candidates:
            if part.status in (PartStatus.DONE, PartStatus.SKIPPED):
                continue
            budget = self.ledger.budget_for(archive_id, part.idx)
            if budget.spendable <= 0:
                self.store.update_part(archive_id, part.idx,
                                       status=PartStatus.BUDGET_EXHAUSTED)
                result.budget_exhausted += 1
                continue
            # Smallest-remaining-first so nearly-finished parts close fast
            # (05 §2.3).
            remaining = part.remaining_bytes
            work.append((0 if remaining is None else remaining, part))

        if not work:
            return result
        work.sort(key=lambda t: (t[0], t[1].idx))
        batch = [part for _, part in work[: self.config.parallel]]

        # 2. Fresh cookie inside the budget window.
        t0 = self._now()
        try:
            cookie = self.cookie_source.fresh()
        except Exception as exc:  # noqa: BLE001
            self.store.set_job_status(archive_id, JobStatus.NEEDS_COOKIE,
                                      error=f"cookie pull failed: {exc}")
            return result

        # 3. Canary first (05 §3.2): prove the cookie with ONE stream before
        #    exposing the other attempts.
        first = batch[0]
        canary_ok = self._run_one(archive_id, first, cookie, on_disk, result)
        result.canary_passed = canary_ok
        if not canary_ok:
            # The cookie is dead or the part is gone; do NOT open the rest.
            return result

        # 4. Open the remaining streams, respecting the cookie budget wall.
        for part in batch[1:]:
            if self._now() - t0 > self.config.cookie_budget_s:
                log.warning("cookie budget exhausted before opening all "
                            "streams; %d left for the next burst", len(batch) - 1)
                break
            self._run_one(archive_id, part, cookie, on_disk, result)

        return result

    # -- single stream ------------------------------------------------------
    def _run_one(self, archive_id: str, part, cookie, on_disk: dict,
                 result: BurstResult) -> bool:
        """Run one part within the burst. Returns True if it started cleanly.

        ``on_disk`` is the scandir result so we do not re-stat the directory.
        """
        existing = on_disk.get(part.filename or "")
        have = existing.size if existing else 0
        target = part.size_expected

        if have > 0:
            # Local byte check first: if we already have a complete, valid
            # part, we are DONE and we did NOT spend an attempt.
            check = verify_part(
                existing.path, size_expected=target,
                level=self.config.verify_level)
            if check.state is VerifyState.STRUCT_OK or check.state is VerifyState.HASH_OK:
                self.store.update_part(archive_id, part.idx,
                                       status=PartStatus.DONE,
                                       verify_state=check.state,
                                       size_on_disk=have)
                result.completed_ok += 1
                return True

        # Reserve the attempt BEFORE the request — the non-negotiable rule.
        try:
            reservation = self.ledger.reserve(
                archive_id, part.idx, CostClass.PAYLOAD)
        except RuntimeError:
            self.store.update_part(archive_id, part.idx,
                                   status=PartStatus.BUDGET_EXHAUSTED)
            result.budget_exhausted += 1
            return False

        self.store.update_part(archive_id, part.idx, status=PartStatus.ACTIVE,
                               bump_attempts=True, quiet=True)
        start = self._now()
        try:
            outcome, bytes_moved = self.fetch(
                part, have, cookie.header,
                write=self._write_part(archive_id, part, have))
            reservation.commit(outcome, bytes_moved=bytes_moved,
                               note=f"{outcome.value} {bytes_moved}B")
            result.attempts_spent += 1
            result.bytes_moved += bytes_moved
        except Exception as exc:  # noqa: BLE001
            # The stream died mid-flight; fail closed on the reservation.
            reservation.commit(ReasonCode.NETWORK_ERROR, bytes_moved=0,
                               note=f"stream exception: {exc}")
            result.attempts_spent += 1
            outcome = ReasonCode.NETWORK_ERROR

        elapsed = self._now() - start

        if outcome is ReasonCode.OK_COMPLETE:
            # Verify on disk with the bytes we just moved.
            on_disk = scan_parts_dir(self.parts_dir)  # refresh sizes
            existing = on_disk.get(part.filename or "")
            if existing:
                check = verify_part(existing.path, size_expected=target,
                                    level=self.config.verify_level)
                if check.ok:
                    self.store.update_part(archive_id, part.idx,
                                           status=PartStatus.DONE,
                                           verify_state=check.state,
                                           size_on_disk=existing.size)
                    result.completed_ok += 1
                    return True
                # verify failed despite OK_COMPLETE: mark partial/corrupt.
                self.store.update_part(archive_id, part.idx,
                                       status=PartStatus.PARTIAL,
                                       verify_state=check.state,
                                       size_on_disk=existing.size,
                                       error=check.detail)
                result.completed_partial += 1
                return False
            self.store.update_part(archive_id, part.idx,
                                   status=PartStatus.PARTIAL,
                                   error="bytes moved but file missing after fetch")
            result.completed_partial += 1
            return False

        if outcome is ReasonCode.OK_PARTIAL:
            # Resumable: bytes are on disk, do NOT throw them away.
            on_disk = scan_parts_dir(self.parts_dir)
            existing = on_disk.get(part.filename or "")
            size_now = existing.size if existing else have
            self.store.update_part(archive_id, part.idx,
                                   status=PartStatus.PARTIAL,
                                   size_on_disk=size_now, quiet=True)
            result.completed_partial += 1
            return False

        # Everything else is a failure that must NOT be retried inside this
        # burst — the next burst (after a fresh cookie) may reschedule it.
        result.failed[outcome.value] = result.failed.get(outcome.value, 0) + 1
        self.store.update_part(archive_id, part.idx,
                               status=PartStatus.PARTIAL if have else PartStatus.FAILED,
                               error=f"{outcome.value} after {elapsed:.1f}s",
                               quiet=True)
        # Auth failures park the whole job on a fresh cookie; the caller's
        # next burst starts only after the extension re-captures.
        from .contracts import AUTH_REASONS
        if outcome in AUTH_REASONS:
            self.store.set_job_status(archive_id, JobStatus.NEEDS_COOKIE,
                                      error=f"{outcome.value} while fetching part {part.idx}")
        return False

    # -- write path ---------------------------------------------------------
    def _write_part(self, archive_id: str, part, have: int):
        """Return a write callback that streams chunks to disk with progress."""
        target = os.path.join(self.parts_dir, part.filename or f"part-{part.idx:03d}.zip")
        os.makedirs(self.parts_dir, exist_ok=True)
        mode = "ab" if have > 0 else "wb"   # resume appends; fresh starts clean

        def write_chunk(chunk: bytes, net_ms: int) -> int:
            t0 = self._now()
            with open(target, mode) as fh:
                fh.write(chunk)
                fh.flush()
            write_ms = int((self._now() - t0) * 1000)
            self.on_chunk(ChunkProgress(
                idx=part.idx, bytes_moved=len(chunk),
                wall_ms=net_ms, net_ms=net_ms, write_ms=write_ms))
            return len(chunk)

        return write_chunk

    # -- real transport -----------------------------------------------------
    def _real_fetch(self, part, have: int, cookie: str, write):
        """The only place real bytes move. Reserved, classified, no retries."""
        url = part.url
        if not url:
            raise RuntimeError(f"part {part.idx} has no URL")
        headers = {"Cookie": cookie}
        if have > 0:
            headers["Range"] = f"bytes={have}-"
        resp = self._session().get(url, headers=headers, stream=True,
                                   timeout=(10, 300),
                                   allow_redirects=False)
        try:
            facts = ResponseFacts(
                status=resp.status_code,
                headers=dict(resp.headers),
                first_bytes=resp.raw.read(4096) if resp.raw else b"",
                final_url=resp.url or "",
                expected_partial=(have > 0),
            )
            reason = classify(facts)
            if reason is ReasonCode.OK_PARTIAL or reason is ReasonCode.OK_COMPLETE:
                # Range-ignored: 200 to a resume means truncate & restart.
                if have > 0 and reason is ReasonCode.OK_COMPLETE:
                    truncate = os.path.join(
                        self.parts_dir, part.filename or f"part-{part.idx:03d}.zip")
                    if os.path.exists(truncate):
                        os.truncate(truncate, 0)
                total = 0
                with open(os.path.join(
                        self.parts_dir, part.filename or f"part-{part.idx:03d}.zip"),
                        "ab" if have > 0 and reason is not ReasonCode.OK_COMPLETE else "wb") as fh:
                    while True:
                        chunk = resp.raw.read(self.config.write_chunk)
                        if not chunk:
                            break
                        t0 = self._now()
                        fh.write(chunk)
                        fh.flush()
                        write_ms = int((self._now() - t0) * 1000)
                        total += len(chunk)
                        self.on_chunk(ChunkProgress(
                            idx=part.idx, bytes_moved=len(chunk),
                            wall_ms=0, net_ms=0, write_ms=write_ms))
                # fsync once at completion (05 §7).
                with open(os.path.join(
                        self.parts_dir, part.filename or f"part-{part.idx:03d}.zip"),
                        "ab") as fh:
                    fh.flush()
                    os.fsync(fh.fileno())
                return reason, total
            # Not OK: drain nothing, report the classification.
            return reason, 0
        finally:
            resp.close()
