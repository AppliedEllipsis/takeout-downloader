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
    "zero_out_attempts", "NO_RETRY_SESSION", "StallAbort",
    "aligned_resume_offset",
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
    #: Wall-clock seconds with ZERO bytes before we call a stream dead and
    #: abort it. The socket read timeout cannot catch a trickle (a stream
    #: sending 1 byte every 4 minutes never times out but will never finish),
    #: so this watchdog is what actually frees a wedged attempt.
    stall_abort_s: float = 180.0
    #: After a stall abort, attempt exactly ONE Range resume. A resume may
    #: cost one of Google's 5 attempts, so this is deliberately capped at 1.
    stall_resume_attempts: int = 1
    #: Rewind this many bytes to an aligned boundary before resuming, so a
    #: torn tail from a killed process is re-fetched rather than appended to.
    resume_rewind: int = WRITE_CHUNK
    #: fsync at most this often (seconds) instead of flush()ing every chunk;
    #: flushing each 8 MiB chunk thrashes the FUSE layer for no safety gain.
    fsync_interval_s: float = 30.0
    #: Assert the parts dir is a real mount point before writing. True in
    #: production (storage is a FUSE mount whose death would dump terabytes
    #: onto a nearly-full root disk); False for local dev and tests.
    require_mount: bool = False
    #: rclone VFS cache dir to watch for upload backlog. Downloading faster
    #: than rclone uploads fills the cache and stalls writes forever.
    cache_dir: Optional[str] = None
    #: Must match rclone's --vfs-cache-max-size.
    cache_max_bytes: int = 100 * 1024 ** 3
    #: How long to wait for the cache to drain before giving up on the burst.
    cache_wait_max_s: float = 1800.0
    #: Honour Retry-After / exponential backoff on 429 and transient 5xx.
    rate_limit_backoff: bool = True


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


NO_RETRY_SESSION = None  # lazily built in BurstEngine; see ._http_session()


class StallAbort(Exception):
    """Raised internally when the no-progress watchdog kills a stream.

    Carries the bytes achieved so the caller can decide whether a single
    Range resume is worth an attempt.
    """

    def __init__(self, bytes_moved: int, idle_s: float):
        super().__init__(f"no bytes for {idle_s:.0f}s after {bytes_moved}B")
        self.bytes_moved = bytes_moved
        self.idle_s = idle_s


def aligned_resume_offset(size_on_disk: int, rewind: int) -> int:
    """Safe byte offset to resume from, given what is on disk.

    A process killed mid-write (or a FUSE mount that only partially flushed)
    can leave a torn tail: ``getsize()`` reports bytes that are not really
    there. Appending after that garbage silently corrupts a 10 GiB part and
    you only find out at extract time, days later. So rewind to an aligned
    boundary below the reported size and re-fetch the overlap.
    """
    if size_on_disk <= 0:
        return 0
    if rewind <= 0:
        return size_on_disk
    trimmed = size_on_disk - rewind
    if trimmed <= 0:
        return 0
    return trimmed - (trimmed % rewind)


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
        # NOTE: this must NOT be named ``_session`` — that would shadow the
        # ``_http_session()`` method with None and make the real transport
        # raise "'NoneType' object is not callable" on first contact, AFTER
        # an attempt has already been reserved.
        self._session_obj = None

    # -- session ----------------------------------------------------------
    def _http_session(self):
        if self._session_obj is None:
            self._session_obj = zero_out_attempts()
        return self._session_obj

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
            # Backpressure: never pile more bytes into a full upload cache.
            if not self._wait_for_cache(result):
                break
            self._run_one(archive_id, part, cookie, on_disk, result)
            # If Google is rate limiting us, stop opening streams and wait
            # rather than burning the 5-attempt budget on refusals.
            if result.failed.get(ReasonCode.RATE_LIMITED.value):
                if not self._maybe_backoff(
                        ReasonCode.RATE_LIMITED,
                        attempt=result.failed[ReasonCode.RATE_LIMITED.value]):
                    break

        return result

    # -- single stream ------------------------------------------------------
    def _run_one(self, archive_id: str, part, cookie, on_disk: dict,
                 result: BurstResult) -> bool:
        """Run one part within the burst. Returns True if it started cleanly.

        ``on_disk`` is the scandir result so we do not re-stat the directory.
        """
        existing = on_disk.get(part.filename or "")
        have_raw = existing.size if existing else 0
        have = have_raw
        target = part.size_expected

        if have_raw > 0:
            # Local byte check first: if we already have a complete, valid
            # part, we are DONE and we did NOT spend an attempt.
            check = verify_part(
                existing.path, size_expected=target,
                level=self.config.verify_level)
            if check.state is VerifyState.STRUCT_OK or check.state is VerifyState.HASH_OK:
                self.store.update_part(archive_id, part.idx,
                                       status=PartStatus.DONE,
                                       verify_state=check.state,
                                       size_on_disk=have_raw)
                result.completed_ok += 1
                return True
            # Incomplete: rewind to an aligned boundary so a torn tail from a
            # killed process is re-fetched instead of appended to.
            have = aligned_resume_offset(have_raw, self.config.resume_rewind)
            if have < have_raw:
                try:
                    os.truncate(existing.path, have)
                except OSError as exc:
                    log.warning("part %d: could not trim torn tail: %s",
                                part.idx, exc)
                    have = have_raw
                else:
                    log.info("part %d: rewound %d B to aligned offset %d "
                             "before resume", part.idx, have_raw - have, have)

        # Storage preflight: never write a 10 GiB part onto the root disk
        # because the FUSE mount died and left an empty directory behind.
        need = None
        if target:
            need = max(0, target - have)
        guard = self._check_storage(need or 0)
        if guard is not None and guard.failed:
            self.store.update_part(archive_id, part.idx,
                                   status=PartStatus.PARTIAL,
                                   error=f"storage preflight: {guard.reason}")
            log.error("part %d: refusing to write — %s", part.idx, guard.reason)
            return False

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
        except StallAbort as stalled:
            # The watchdog killed a wedged stream. Bytes achieved are real and
            # on disk, so record them as partial progress on this attempt.
            reservation.commit(ReasonCode.OK_PARTIAL,
                               bytes_moved=stalled.bytes_moved,
                               note=f"stall abort: {stalled}")
            result.attempts_spent += 1
            result.bytes_moved += stalled.bytes_moved
            outcome = ReasonCode.OK_PARTIAL
            log.warning("part %d: %s — aborted", part.idx, stalled)
            resumed = self._resume_after_stall(
                archive_id, part, cookie, result, stalled.bytes_moved)
            if resumed is not None:
                outcome = resumed
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

    # -- guards -------------------------------------------------------------
    def _wait_for_cache(self, result: BurstResult) -> bool:
        """Block while the rclone VFS upload backlog is too full to accept more.

        Writing into a full ``--vfs-cache-max-size`` cache does not fail — it
        stalls indefinitely, which combined with a long transfer looks exactly
        like a hang. Pausing here is strictly better: we hold no attempt and
        no cookie while waiting. Returns False if it never drained.
        """
        if not self.config.cache_dir:
            return True
        try:
            from .cachewatch import CacheState, next_state, read_cache_status
        except Exception:  # noqa: BLE001
            return True
        state = CacheState.OK
        waited = 0.0
        while True:
            try:
                status = read_cache_status(
                    self.config.cache_dir,
                    max_bytes=self.config.cache_max_bytes)
            except Exception as exc:  # noqa: BLE001
                log.debug("cache watch unavailable: %s", exc)
                return True
            state = next_state(state, status)
            if state is not CacheState.PAUSE:
                if status.state is CacheState.WARN:
                    log.warning("upload cache %.0f%% full — downloading faster "
                                "than rclone uploads", status.fill_ratio * 100)
                return True
            if waited >= self.config.cache_wait_max_s:
                log.error("upload cache still %.0f%% full after %.0fs; "
                          "stopping burst so we do not wedge on a full cache",
                          status.fill_ratio * 100, waited)
                return False
            log.warning("upload cache %.0f%% full — pausing %ds for rclone to "
                        "drain (no attempt held)", status.fill_ratio * 100, 30)
            time.sleep(30)
            waited += 30

    def _maybe_backoff(self, outcome, attempt: int, headers=None) -> bool:
        """Sleep if the outcome says we are being rate limited.

        Retrying a 429 immediately is the worst thing we can do: it spends the
        irreplaceable 5-attempts-per-part budget on a request Google has
        already told us it will refuse. Returns False when we should give up.
        """
        if not self.config.rate_limit_backoff:
            return True
        try:
            from .backoff import decide
        except Exception:  # noqa: BLE001
            return True
        try:
            decision = decide(outcome, attempt=attempt, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.debug("backoff policy unavailable: %s", exc)
            return True
        if decision.should_wait:
            log.warning("rate limited: waiting %.0fs (%s) before any further "
                        "request", decision.delay_s, decision.source)
            time.sleep(decision.delay_s)
            return True
        if decision.give_up:
            # give_up without should_wait means "this will not heal by
            # waiting" (a dead cookie, a 404, an exhausted limit). Stop the
            # burst, but do not pretend we were rate limited.
            log.warning("not retryable by waiting: %s", decision.detail)
            return False
        return True

    def _check_storage(self, need_bytes: int):
        """Return a failed PreflightResult if writing is unsafe, else None.

        Imported lazily so the engine still works if the optional guard
        module is absent (dev boxes, minimal installs).
        """
        try:
            from .preflight import preflight_write
        except Exception:  # noqa: BLE001
            return None
        try:
            return preflight_write(
                self.parts_dir, need_bytes,
                require_mount=self.config.require_mount)
        except Exception as exc:  # noqa: BLE001
            # A broken guard must never block a healthy transfer.
            log.debug("preflight unavailable: %s", exc)
            return None

    def _resume_after_stall(self, archive_id: str, part, cookie,
                            result: BurstResult, bytes_before: int):
        """One Range resume after a watchdog abort. Returns the new outcome.

        Capped at ``stall_resume_attempts`` (default 1) because a resume may
        cost one of Google's 5 attempts per part. Returns None when no resume
        was attempted.
        """
        if self.config.stall_resume_attempts < 1:
            return None
        fresh = scan_parts_dir(self.parts_dir).get(part.filename or "")
        have_raw = fresh.size if fresh else bytes_before
        have = aligned_resume_offset(have_raw, self.config.resume_rewind)
        if have < have_raw and fresh:
            try:
                os.truncate(fresh.path, have)
            except OSError:
                have = have_raw
        try:
            reservation = self.ledger.reserve(
                archive_id, part.idx, CostClass.PAYLOAD)
        except RuntimeError:
            self.store.update_part(archive_id, part.idx,
                                   status=PartStatus.BUDGET_EXHAUSTED)
            result.budget_exhausted += 1
            return None
        log.info("part %d: one resume after stall from offset %d",
                 part.idx, have)
        self.store.update_part(archive_id, part.idx, status=PartStatus.ACTIVE,
                               bump_attempts=True, quiet=True)
        try:
            outcome, moved = self.fetch(
                part, have, cookie.header,
                write=self._write_part(archive_id, part, have))
            reservation.commit(outcome, bytes_moved=moved,
                               note=f"stall resume {outcome.value} {moved}B")
            result.attempts_spent += 1
            result.bytes_moved += moved
            return outcome
        except StallAbort as again:
            # Stalled twice: stop. Another resume would spend a third attempt
            # on a link that is clearly not moving.
            reservation.commit(ReasonCode.OK_PARTIAL,
                               bytes_moved=again.bytes_moved,
                               note=f"stall abort on resume: {again}")
            result.attempts_spent += 1
            result.bytes_moved += again.bytes_moved
            log.warning("part %d: stalled again on resume, giving up", part.idx)
            return ReasonCode.OK_PARTIAL
        except Exception as exc:  # noqa: BLE001
            reservation.commit(ReasonCode.NETWORK_ERROR, bytes_moved=0,
                               note=f"stall resume exception: {exc}")
            result.attempts_spent += 1
            return ReasonCode.NETWORK_ERROR

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
        """The only place real bytes move. Reserved, classified, no retries.

        Hardening for multi-day / multi-TB transfers:
        * ``Accept-Encoding: identity`` — we write ``resp.raw`` bytes straight
          to disk with no decoding, so a gzip-encoding proxy would otherwise
          silently produce a corrupt .zip that still passes the size check.
        * no-progress watchdog — a trickling stream never trips the socket
          read timeout but will never finish either.
        * periodic fsync instead of flush-every-chunk — far less FUSE thrash.
        """
        url = part.url
        if not url:
            raise RuntimeError(f"part {part.idx} has no URL")
        target = os.path.join(
            self.parts_dir, part.filename or f"part-{part.idx:03d}.zip")
        headers = {"Cookie": cookie, "Accept-Encoding": "identity"}
        if have > 0:
            headers["Range"] = f"bytes={have}-"
        resp = self._http_session().get(url, headers=headers, stream=True,
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
            if reason is not ReasonCode.OK_PARTIAL and reason is not ReasonCode.OK_COMPLETE:
                # Not OK: drain nothing, report the classification.
                return reason, 0

            # Range-ignored: 200 to a resume means truncate & restart.
            restart = have > 0 and reason is ReasonCode.OK_COMPLETE
            if restart and os.path.exists(target):
                os.truncate(target, 0)
            mode = "ab" if (have > 0 and not restart) else "wb"

            total = 0
            last_progress = self._now()
            last_fsync = self._now()
            os.makedirs(self.parts_dir, exist_ok=True)
            with open(target, mode) as fh:
                while True:
                    t_read = self._now()
                    chunk = resp.raw.read(self.config.write_chunk)
                    now = self._now()
                    if not chunk:
                        break
                    net_ms = int((now - t_read) * 1000)
                    t0 = now
                    fh.write(chunk)
                    write_ms = int((self._now() - t0) * 1000)
                    total += len(chunk)
                    last_progress = self._now()

                    # Periodic durability instead of per-chunk flush().
                    if (self._now() - last_fsync) >= self.config.fsync_interval_s:
                        fh.flush()
                        os.fsync(fh.fileno())
                        last_fsync = self._now()

                    self.on_chunk(ChunkProgress(
                        idx=part.idx, bytes_moved=len(chunk),
                        wall_ms=net_ms + write_ms, net_ms=net_ms,
                        write_ms=write_ms))

                    # Watchdog: a read that returned data resets the clock
                    # above; this guards the case where reads keep returning
                    # tiny amounts very slowly.
                    idle = self._now() - last_progress
                    if self.config.stall_abort_s and idle >= self.config.stall_abort_s:
                        fh.flush()
                        os.fsync(fh.fileno())
                        raise StallAbort(total, idle)

                # One durable fsync at completion (05 §7) — same handle, no
                # reopen of a 10 GiB file.
                fh.flush()
                os.fsync(fh.fileno())
            return reason, total
        finally:
            resp.close()
