"""The orchestrator: turns twelve tested modules into one machine.

Until this module existed, every part worked alone and none worked in sequence —
`grep -rln 'def run|orchestrat|run_once|def main|__main__' autopilot/` found
nothing. This is the wiring.

The interface is frozen in `docs/v3/02-RUN-INTERFACE.md`; this file implements it.

DESIGN RULES, each traceable to a measurement or a documented incident
---------------------------------------------------------------------
* **Mint only when the cache says so.** A mint is measured `Δ1`; a `Range` resume
  is `Δ0`. Re-minting a part that already holds a URL spends a scarce attempt for
  nothing.
* **`NeedsReauth` is a status, never an exception escaping the loop.** Every prior
  generation collapsed a login redirect into a generic error and parked the job
  forever, which is what produced three months of tab floods (failure modes
  1.9/1.18). Here it becomes `RunOutcome.status == "needs_reauth"` and the run stops
  cleanly with the ledger intact.
* **An expired export is terminal and is never minted against.** The source objects
  are gone; a fresh mint cannot succeed "however the request is made".
* **The destination is indexed ONCE per run**, then updated in memory. The
  historical livelock was a per-part `stat()` pre-pass taking ~6 minutes on the
  FUSE mount.
* **Everything is injectable** (`session_factory`, `http_client`, `now`) so the
  whole pipeline is testable with no browser, no network and no clock.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .errors import (
    AutopilotError,
    BrowserUnavailable,
    MintError,
    NeedsReauth,
    QuotaExceeded,
)
from .jar import CookieError, pull_jar
from .ledger import AttemptKind, Ledger, open_ledger
from .mint import mint as mint_part, part_filename_from_url
from .mover import (
    DestinationIndex,
    MoveRefused,
    assert_destination_ready,
    assert_staging_ready,
    index_destination,
    move_part,
    plan_moves,
)
from .report import build_report, parse_expiry
from .scrape import (
    ArchivePage,
    PartLink,
    archive_url,
    page_has_rapt,
    provoke_rapt,
    read_archive,
    recover_rapt_url,
)
from .transport import TransferError, RemoteChanged, download
from .verify import verify_local

__all__ = [
    "RUN_OUTCOME_STATUSES",
    "RunConfig",
    "RunOutcome",
    "run_once",
    "run_once_sync",
]

RUN_OUTCOME_STATUSES = (
    "complete",
    "needs_reauth",
    "incomplete",
    "expired_unrecoverable",
    # Added when the quota condition was named. It was missing from this tuple for a
    # while after `run_once` began RETURNING it — `JOB_STATUSES` and
    # `_EXIT_FOR_STATUS` were both updated and this one was forgotten, which is the
    # same vocabulary drift that once broke `incomplete` (see `ledger.py`).
    # `docs/v3/02-RUN-INTERFACE.md` points readers here as the canonical list, so a
    # missing member is a contract violation rather than a cosmetic gap. Found by
    # three independent reviewers; a test now asserts that every status the
    # orchestrator can return appears here.
    "quota_exceeded",
    "failed",
)

#: Statuses that must STOP a run rather than drive it. An export that has expired or
#: spent its download allowance cannot be rescued by trying again, and the frozen
#: contract's rule 4 requires `run_once` to honour the recorded status even when the
#: page no longer renders the evidence (e.g. `Available until` disappears).
TERMINAL_BLOCKED_STATUSES = ("expired_unrecoverable", "quota_exceeded")

#: How many failed transfers against a CACHED minted URL before it is discarded.
#: One failure may be a transient blip, and re-minting costs Δ1 against an export
#: that allows only 5 attempts in total — so a single blip must not trigger a fresh
#: mint. But a URL that has failed this many times is dead, and without a bound the
#: part stays wedged on it forever (`RemoteChanged` used to be the only invalidation).
MAX_CACHED_URL_FAILURES = 3


@dataclass
class RunConfig:
    archive_id: str
    ledger_path: str
    staging_dir: str
    archive_dir: str
    account: Optional[str] = None
    cdp_http: str = "http://127.0.0.1:9222"
    require_mount: bool = True
    max_parts: Optional[int] = None
    verify_hash: bool = False
    settle: float = 20.0
    work_url: Optional[str] = None
    #: Earn every part's URL and stop, without transferring. Measured reason: a minted
    #: URL outlives the ~45-minute ReAuth window (206 after 81 minutes), and minting is
    #: what the window gates — so a long pull should mint everything first, then
    #: transfer on a later pass against the cached URLs.
    mint_only: bool = False
    #: Refuse to stage when free space is below this many bytes. `None` = do not check,
    #: which is the default for tests and development; the CLI supplies a real value.
    #: v2's equivalent had NO escape and made its own suite unrunnable on a full disk,
    #: so the guard meant to protect production broke every test.
    min_headroom: Optional[int] = None
    #: Watchdog for one file's copy into the archive (seconds), and how long it may make
    #: no progress before the copy child gives up. See `mover.MOVE_WATCHDOG_SECONDS`.
    move_timeout: Optional[float] = None
    move_stall: Optional[float] = None
    #: Delete each staged part once its archive copy is verified. Staging is scratch and
    #: the archive is the copy of record, but keeping both costs a full duplicate of the
    #: export — on the 64-product pull that is 131.6 GB of a 300 GB volume which must
    #: also hold rclone's 100 GB VFS cache (measured 2026-09-21: 194 GB free vs ~232 GB
    #: wanted, so the default cannot fit a full pull). Opt-in, because the staged copy
    #: is the only way to re-verify without re-downloading. Never fires before a move
    #: has been verified.
    clean_staging: bool = False

    def resolved_work_url(self) -> str:
        return self.work_url or archive_url(self.archive_id)


@dataclass
class RunOutcome:
    archive_id: str
    status: str
    parts_expected: int = 0
    parts_done: int = 0
    bytes_moved: int = 0
    report_markdown: str = ""
    error: Optional[str] = None
    notes: list = field(default_factory=list)

    def __str__(self) -> str:
        return (f"<RunOutcome {self.archive_id} {self.status} "
                f"{self.parts_done}/{self.parts_expected} parts, "
                f"{self.bytes_moved} bytes>")


def _int_size(value) -> Optional[int]:
    """`data-size` / `Content-Length` arrive as strings."""
    try:
        n = int(str(value).strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def page_from_ledger(ledger, archive_id: str) -> Optional[ArchivePage]:
    """Rebuild the work-list from the ledger when the browser cannot supply one.

    **Why this exists — measured 2026-09-22.** The transfer pass scraped the archive page
    before moving anything, and that scrape needs a live `rapt`. So a run whose parts were
    ALL already minted still died with `needs_reauth` before transferring a byte, which
    invalidated the entire point of `--mint-only`. The observed failure:

        error: the archive page served download links carrying no rapt, and provoking one
        by navigating the redirector did not produce a token either. No download attempt
        was spent.

    The scrape has two jobs: discover the part list, and obtain a live token. When every
    part already carries a cached `minted_url` and a real `filename`, the ledger supplies
    both the part list and the URLs — so the browser is not needed for either, and the
    transfer becomes genuinely window-independent, as documented.

    Returns `None` when the ledger is NOT self-sufficient, so the caller keeps its normal
    behaviour rather than silently proceeding on a partial view of the export. The
    condition is deliberately strict: one part without a URL, or still holding the
    placeholder name `download`, and this declines.

    **Provenance caveat, surfaced on stderr:** completeness is then judged against the part
    list this ledger recorded during an earlier authenticated scrape, not a fresh page read.
    If that scrape had been partial, so is this view. The run says so when it takes this
    path rather than letting a green verdict imply a check that did not happen.
    """
    rows = ledger.parts(archive_id)
    if not rows:
        return None

    parts: list = []
    for r in rows:
        name = (r.filename or "").strip()
        if not r.minted_url or not name or name == "download" or r.idx is None:
            return None
        parts.append(PartLink(
            index=int(r.idx),
            raw_uri="",
            filename=name,
            size=str(r.size_expected) if r.size_expected else "",
        ))
    if not parts:
        return None

    page = ArchivePage(url="", parts=parts)
    job = ledger.job(archive_id)
    if job is not None:
        try:
            page.expiry = job["expiry_at"]
        except (KeyError, TypeError, IndexError):
            page.expiry = None
    return page


def _get_json(url: str, timeout: float) -> dict:
    import json
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def pick_page_target(targets, *, prefer_host: str = "takeout.google.com"):
    """Choose which page target to drive. Pure, so it is testable offline.

    Prefer a tab already on the Takeout host (the session cookie is right there
    and the page is warm); otherwise any page target will do.

    Raises if there is no page target at all: a browser with no tab cannot be
    navigated, and the browser-level endpoint cannot substitute (see below).
    """
    pages = [t for t in (targets or [])
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise AutopilotError(
            "no page target available — the browser has no tab to drive. "
            "The browser-level CDP endpoint cannot be used instead: measured "
            "2026-09-19, it rejects Page.navigate, Network.enable and "
            "Runtime.evaluate with \"wasn't found\" (only Storage.getCookies works)."
        )
    preferred = [t for t in pages if prefer_host in (t.get("url") or "")]
    return (preferred or pages)[0]


def _default_session_factory(cdp_http: str, timeout: float = 20.0):
    """Attach to a **page target**, not the browser endpoint.

    Measured 2026-09-19, and this was a live failure rather than a theory:

        CmdError: Page.navigate failed: 'Page.navigate' wasn't found

    The `Page`, `Network` and `Runtime` domains belong to a page target. On the
    browser-level endpoint every one of them fails with "wasn't found" — so a
    browser-level session can read cookies but cannot drive anything. A page
    target does **both** (`Storage.getCookies` returned the same 28 cookies), so
    one session suffices.

    `ws://host:port` bare is rejected with HTTP 404, so the URL must come from
    `/json/list` (per-target) and its UUID changes on every browser restart.
    """
    from .cdp import CdpSession
    from .ws_transport import WebSocketTransport

    @asynccontextmanager
    async def _factory():
        # Classify any failure to reach or select a target as `BrowserUnavailable`.
        #
        # Without this the connect raised raw `urllib`/socket errors out of
        # `run_once`, which escaped the CLI as a traceback and left the job parked on
        # `scraping` with no recorded error. `pick_page_target` already raises
        # `AutopilotError` for the no-tab case, so only the transport errors need
        # translating here — and doing it in the factory (rather than a pre-flight in
        # `run_once`) keeps the injectable seam intact, which is what the tests rely on.
        try:
            targets = await asyncio.to_thread(
                _get_json, cdp_http.rstrip("/") + "/json/list", timeout)
            target = pick_page_target(targets)
            transport = await WebSocketTransport.connect(target["webSocketDebuggerUrl"])
        except AutopilotError:
            raise
        except Exception as exc:  # noqa: BLE001 - any cause means "no browser"
            raise BrowserUnavailable(
                f"{type(exc).__name__}: {exc}") from exc
        async with CdpSession(transport) as session:
            yield session

    return _factory


def _default_http_client():
    from .transport import UrllibHttpClient

    return UrllibHttpClient()


async def run_once(
    cfg: RunConfig,
    *,
    session_factory: Optional[Callable] = None,
    http_client=None,
    now: Optional[datetime] = None,
) -> RunOutcome:
    """One pass over an export. See the frozen contract for the exact rules."""
    now = now or datetime.now(timezone.utc)
    session_factory = session_factory or _default_session_factory(cfg.cdp_http)
    http_client = http_client or _default_http_client()

    os.makedirs(cfg.staging_dir, exist_ok=True)

    # `open_ledger` refuses a FUSE path (measured: v1 keeps WAL-mode SQLite on the
    # rclone mount). It raises rather than returning an outcome: a bad ledger path
    # is a caller bug, not a run result.
    ledger: Ledger = open_ledger(cfg.ledger_path)

    # ---- 0a. capture any TERMINAL status NOW, before anything overwrites it ----
    #
    # The frozen contract's rule 4 requires `run_once` to stop when the job is
    # already `expired_unrecoverable`, or has spent its download allowance, and to
    # never mint against it. Two earlier attempts at this guard were silently inert,
    # so both are recorded here:
    #
    #   1. It was first written before `_finish` existed (it is a closure created
    #      further down), so it would have raised `NameError` on exactly the path it
    #      protected. No test exercised that path, so nothing noticed.
    #   2. It was then placed after the closure — but the line
    #      `ledger.set_job_status(cfg.archive_id, "scraping")` runs between them and
    #      overwrites the status, so the guard compared "scraping" against the
    #      terminal set and never fired.
    #
    # Hence: read the row FIRST, and act on the snapshot after `_finish` exists. The
    # `set_job_status("scraping")` call is what defeated the naive version, so the
    # snapshot has to be taken on this side of it.
    _prior = ledger.job(cfg.archive_id)
    _terminal_status: Optional[str] = None
    _terminal_error: Optional[str] = None
    _terminal_expiry: Optional[str] = None
    if _prior is not None and _prior["status"] in TERMINAL_BLOCKED_STATUSES:
        _terminal_status = _prior["status"]
        _terminal_error = _prior["error"]
        _terminal_expiry = _prior["expiry_at"]

    try:
        ledger.upsert_job(cfg.archive_id, account=cfg.account,
                          output_dir=cfg.archive_dir)
        ledger.set_job_status(cfg.archive_id, "scraping")

        async def _finish(status: str, *, error: Optional[str] = None) -> RunOutcome:
            rows = ledger.parts(cfg.archive_id)
            # `present` so the report can see bytes that are in the archive but were not
            # transferred by this ledger. Without it a resumed run reports `0/68` on an
            # export that is complete on disk, because v2 (or an earlier ledger) did the
            # downloading — observed live on the 62-product export.
            try:
                _present = index_destination(cfg.archive_dir)
            except Exception:
                # A missing or unmounted destination must not break reporting; the report
                # falls back to ledger-only, which is what it always used to do.
                _present = None
            report = build_report(rows, archive_id=cfg.archive_id, account=cfg.account,
                                  status=status, expiry_at=_expiry_seen, now=now,
                                  attempts=ledger.attempts_by_kind(cfg.archive_id),
                                  attempt_kinds=ledger.attempt_kinds_by_part(cfg.archive_id),
                                  present=_present)
            summary = ledger.summary(cfg.archive_id)
            return RunOutcome(
                archive_id=cfg.archive_id,
                status=status,
                parts_expected=report.parts_expected,
                parts_done=report.parts_done,
                bytes_moved=sum(r.size_on_disk for r in rows),
                report_markdown=report.as_markdown(),
                error=error,
                notes=list(report.warnings),
            )

        _expiry_seen: Optional[str] = None

        # ---- 0b. act on the terminal snapshot captured above ---------------
        # Placed here because `_finish` is a closure created just above; the
        # snapshot itself was taken before `set_job_status("scraping")` could
        # overwrite it. See the comment at the top of `run_once`.
        if _terminal_status is not None:
            _expiry_seen = _terminal_expiry
            return await _finish(
                _terminal_status,
                error=(_terminal_error or "").strip()
                      or (f"job is already {_terminal_status}; refusing to re-attempt "
                          "a terminal export — see runbooks 1.14 / 1.19"),
            )

        # ---- 0c. staging must have room --------------------------------
        # The architecture requires an approved-root and headroom check; the only thing
        # that existed was `os.makedirs`. Staging shares a volume with rclone's VFS
        # cache (`--vfs-cache-max-size 100G`) on the box this was written for, so one
        # full transfer transiently occupies staging PLUS cache, and running out
        # mid-write wedges a partial (failure mode 1.16).
        #
        # `cfg.min_headroom` defaults to None = do not check, deliberately: v2's
        # hard-coded 20 GiB floor had no escape and made its own suite unrunnable on a
        # nearly-full development disk, so the guard meant to protect production broke
        # every test. The escape here is explicit, and the CLI supplies a real value.
        #
        # Placed AFTER `_finish` exists — this guard was nearly written next to
        # `makedirs` above, which would have referenced the closure before it was
        # created and failed only on the path it was meant to protect.
        try:
            assert_staging_ready(cfg.staging_dir, min_headroom=cfg.min_headroom)
        except MoveRefused as exc:
            ledger.set_job_status(cfg.archive_id, "failed", error=str(exc))
            return await _finish("failed", error=f"refusing to stage: {exc}")

        # Opening the browser session is the first thing that can fail, and it used
        # to fail UNCAUGHT: `_default_session_factory` fetches `/json/list`, so a
        # Chromium that is down, or that has no page target, raised straight out of
        # `run_once` past the CLI as a traceback — leaving the job parked on
        # `scraping` with no error recorded.
        #
        # It is classified in `_default_session_factory` (as `BrowserUnavailable`)
        # and caught at the outer `except` below, NOT by probing `/json/list` here.
        # A local pre-flight was tried first and was wrong: probing the CDP endpoint
        # directly bypassed every test's injected `session_factory`, so the suite
        # made real sockets and 13 tests failed. The factory is the injectable seam —
        # whatever IT raises is the browser's availability, by definition.
        async with session_factory() as session:
            # ---- 1. scrape -------------------------------------------------
            # Navigate to a *rapt-bearing* archive URL when one is recoverable.
            #
            # This was the bug that killed the first real run. The bare URL
            # `.../manage/archive/<id>` serves `data-download-uri` links with no
            # `rapt`, even on an authenticated session with the export marked
            # Completed — because the rapt is embedded into the page's links
            # only when the page request itself carried one. The redirector
            # minted from such a link bounces to ServiceLogin, which is
            # indistinguishable from "ReAuth required" unless you know this.
            #
            # The browser's own tab keeps those URLs in its navigation history,
            # and reusing one costs nothing (navigation is a measured Δ0).
            work_url = cfg.resolved_work_url()
            rapt_url = None
            if not cfg.work_url:
                rapt_url = await recover_rapt_url(session, cfg.archive_id)
                if rapt_url:
                    work_url = rapt_url

            page = await read_archive(session, work_url, settle=cfg.settle)

            # ---- offline fallback: the ledger may already be sufficient --------
            #
            # This is what makes the transfer pass genuinely window-independent, which is
            # the whole justification for `--mint-only`. Without it the transfer scraped
            # the page, needed a live rapt for that scrape, and died with `needs_reauth`
            # before moving anything — measured 2026-09-22, on an export whose URLs were
            # ALL already cached. Minting first bought nothing.
            #
            # Only taken when EVERY part has a cached URL and a real name
            # (`page_from_ledger` returns None otherwise), so this can never mask a
            # partial view of the export. When it does fire, the run needs no browser
            # work at all: no scrape, no mint, no token.
            _from_ledger = False
            if page.challenged or not page.parts:
                # Capture WHY before `page` is reassigned, or the note below reports the
                # ledger page's own state instead of the reason we fell back.
                _why = "challenge" if page.challenged else "no parts"
                _ledger_page = page_from_ledger(ledger, cfg.archive_id)
                if _ledger_page is not None:
                    page = _ledger_page
                    _from_ledger = True
                    print(f"note: the archive page was unavailable ({_why}), but all "
                          f"{len(page.parts)} part(s) already have cached URLs — "
                          f"working from the ledger, no token needed. Completeness will "
                          f"be judged against the part list this ledger recorded during "
                          f"its last authenticated scrape.", file=sys.stderr)

            if page.challenged:
                ledger.set_job_status(cfg.archive_id, "needs_reauth",
                                      error=f"challenge at {page.url}")
                return await _finish("needs_reauth",
                                     error=f"the archive page is a sign-in page: {page.url}")

            # NOTE: deliberately NO special case for an empty page. Zero parts is not a
            # ReAuth situation — it means the export has nothing downloadable yet — and
            # reporting `needs_reauth` would send an operator to re-authenticate for no
            # reason. `test_rule7_zero_parts_is_never_complete` pins that it must end
            # `failed` or `incomplete` with an explanation, and an earlier draft of this
            # very change broke that invariant by adding such a branch.

            # Keep the tokened page loaded. `_restore`'s docstring promises this,
            # and it is worth having: a tab left on the rapt page is what lets the
            # *next* run recover the token for free instead of needing ReAuth.
            restore_url = page.url if "rapt=" in (page.url or "") else work_url

            _expiry_seen = page.expiry
            exp = parse_expiry(page.expiry)

            # ---- 2. expiry guard, BEFORE any mint --------------------------
            if exp is not None and now > exp:
                ledger.set_job_status(cfg.archive_id, "expired_unrecoverable",
                                      error=f"export expired at {page.expiry}")
                return await _finish(
                    "expired_unrecoverable",
                    error=(f"export expired at {page.expiry} — the source objects are gone; "
                           "a NEW export is the only remedy"))

            # ---- 3. index the parts ----------------------------------------
            try:
                page.assert_indexed()
            except AutopilotError as exc:
                ledger.set_job_status(cfg.archive_id, "failed", error=str(exc))
                return await _finish("failed", error=f"scrape is unusable: {exc}")

            if not page.parts:
                # Zero parts is emphatically NOT success.
                ledger.set_job_status(cfg.archive_id, "failed",
                                      error="no parts were scraped")
                return await _finish("failed",
                                     error="no parts were scraped — nothing to do, "
                                           "and this must not read as success")

            ledger.upsert_parts(cfg.archive_id,
                                [(p.index, p.filename, _int_size(p.size)) for p in page.parts])

            # ---- 3b. a tokened link is REQUIRED to mint --------------------
            # Measured: a rapt-less redirector bounces to ServiceLogin. That is not a
            # ReAuth requirement — it means the page was loaded without a rapt (see the
            # block comment in `scrape.py`). Detecting it here saves a mint attempt and
            # stops the run reporting "ReAuth required" when the session was never at
            # fault.
            #
            # But detecting it is not enough: a token can be EARNED. Measured
            # 2026-09-19 — a fully signed-in session still serves untokened links, and
            # navigating the redirector once bounces back with a fresh token. So the
            # first response is to go get one, not to demand a human. Previously the
            # run could only work when someone had just clicked through a download,
            # and reported `needs_reauth` otherwise — including right after a sign-in.
            if not page_has_rapt(page):
                _redirector = next((p.redirector for p in page.parts if p.redirector), "")
                if _redirector:
                    _provoked = await provoke_rapt(session, _redirector,
                                                   settle=cfg.settle)
                    if _provoked:
                        page = await read_archive(session, _provoked, settle=cfg.settle)
                        restore_url = (page.url if "rapt=" in (page.url or "")
                                       else _provoked)
                        if page.challenged:
                            ledger.set_job_status(cfg.archive_id, "needs_reauth",
                                                  error=f"challenge at {page.url}")
                            return await _finish(
                                "needs_reauth",
                                error=f"the archive page is a sign-in page: {page.url}")

            if not page_has_rapt(page):
                ledger.set_job_status(
                    cfg.archive_id, "needs_reauth",
                    error="archive page served un-tokened download links")
                return await _finish(
                    "needs_reauth",
                    error=("the archive page served download links carrying no "
                           f"rapt, and provoking one by navigating the redirector "
                           "did not produce a token either (loaded {0}). A human "
                           "must satisfy the interactive ReAuth challenge. No "
                           "download attempt was spent.").format(work_url))

            # ---- 4. the jar (only the transporter needs it) ----------------
            try:
                jar = await pull_jar(session)
            except CookieError as exc:
                ledger.set_job_status(cfg.archive_id, "needs_reauth", error=str(exc))
                return await _finish("needs_reauth", error=str(exc))

            # ---- 5. work the parts -----------------------------------------
            # Assert the destination really is the mount BEFORE indexing it.
            #
            # This call was missing at first: `assert_destination_ready` was
            # implemented and tested, the CLI passed `require_mount` in, and
            # `cfg.require_mount` was never read — so failure mode 1.7 was
            # unguarded on the only path that matters. When the rclone daemon dies
            # the kernel turns /opt/archives into an ordinary empty directory on a
            # 13 GB root filesystem, and an unguarded mover writes terabytes into
            # it. A well-tested guard that nothing calls is not a guard.
            #
            # Unlike a FUSE *ledger* path (a caller bug, which raises), a detached
            # destination mount is an environment fault that comes and goes, so it
            # becomes a `failed` outcome rather than an exception — the operator
            # gets a clear message and exit 1 instead of a traceback.
            try:
                assert_destination_ready(cfg.archive_dir, require_mount=cfg.require_mount)
            except MoveRefused as exc:
                ledger.set_job_status(cfg.archive_id, "failed", error=str(exc))
                return await _finish("failed",
                                     error=f"refusing to write: {exc}")

            # The phase label has to name the phase that is actually running.
            # A `--mint-only` run reaches this line too (the destination-ready
            # check is shared by both paths), so an unconditional `transferring`
            # made a mint-only run claim it was transferring while it minted and
            # moved nothing at all — observed on the 62-product mint, which
            # reported `transferring` with 63 of 68 URLs earned and zero bytes on
            # the wire. Nothing keys off the value, which is exactly why nobody
            # noticed: the only consumer is a human reading the status.
            ledger.set_job_status(cfg.archive_id,
                                  "minting" if cfg.mint_only else "transferring")
            dest_index = index_destination(cfg.archive_dir)
            todo = [p for p in page.parts if _needs_work(ledger, cfg.archive_id, p.index)]
            if cfg.max_parts is not None:
                todo = todo[: max(0, cfg.max_parts)]

            # ---- consult the destination index BEFORE minting anything -----
            #
            # `index_destination` and `plan_moves` were written for exactly this and
            # the orchestrator never called them: the index was built here, written
            # to later, and never READ. So a part whose bytes were already in the
            # archive could still be re-minted (Δ1 against a 5-attempt allowance) and
            # re-transferred. `test_moving_is_idempotent_by_planning` proved the
            # mechanism correct in isolation while nothing in the run used it —
            # a test that gave false confidence about production behaviour.
            #
            # Only parts whose REAL name is already recorded can be checked here,
            # because the scrape can only ever produce the placeholder `download`
            # (the redirector basename). For a fresh ledger the name is not knowable
            # until after minting, so this prevents duplicate work on re-runs and on
            # a ledger that lost its part rows — which is exactly when it matters.
            already_present = []
            _name_to_idx: dict = {}
            for _p in todo:
                _row = ledger.part(cfg.archive_id, _p.index)
                _name = (_row["filename"] if _row else None) or ""
                _size = _int_size(_p.size)
                if _name and _name != "download" and _size is not None:
                    _name_to_idx[_name] = _p.index
                    already_present.append(
                        (_name, os.path.join(cfg.staging_dir, _name), _size))
            _skipped: set = set()
            for _item in plan_moves(already_present, dest_index):
                if _item.action == "skip-present" and _item.filename in _name_to_idx:
                    _idx = _name_to_idx[_item.filename]
                    ledger.set_part_status(cfg.archive_id, _idx, "done")
                    _skipped.add(_idx)
            if _skipped:
                todo = [p for p in todo if p.index not in _skipped]

            for part in todo:
                idx = part.index
                # NOTE: `part.filename` is the *redirector* basename, which is the
                # literal string `download` for every part of every export — so it
                # is never used to name a file. The real name comes off the minted
                # URL below and is threaded through staging, ledger and move.
                staged = os.path.join(cfg.staging_dir, part.filename or f"part-{idx}.zip")
                expected = _int_size(part.size)

                # ---- mint, only if the cache says so -----------------------
                minted_url = ledger.minted_url(cfg.archive_id, idx)
                if minted_url is None:
                    if not part.redirector:
                        ledger.set_part_status(cfg.archive_id, idx, "failed",
                                               error="no redirector URL on the page")
                        continue
                    try:
                        result = await mint_part(session, part.redirector,
                                                 settle=cfg.settle,
                                                 restore_url=restore_url)
                    except NeedsReauth as exc:
                        ledger.set_job_status(cfg.archive_id, "needs_reauth", error=str(exc))
                        return await _finish("needs_reauth", error=str(exc))
                    except QuotaExceeded as exc:
                        # Terminal for THIS export. Caught before the generic
                        # handler below so it is never downgraded to a per-part
                        # failure that a later pass would pointlessly retry.
                        ledger.set_job_status(cfg.archive_id, "quota_exceeded",
                                              error=str(exc))
                        return await _finish("quota_exceeded", error=str(exc))
                    except (MintError, AutopilotError) as exc:
                        ledger.set_part_status(cfg.archive_id, idx, "failed",
                                               error=f"mint failed: {exc}")
                        continue
                    minted_url = result.url
                    ledger.record_mint(cfg.archive_id, idx, minted_url)

                # ---- the real filename, from the minted URL ----------------
                # Measured 2026-09-19: the scraped name is `download` for EVERY
                # part, so staging and the destination index both collapsed a
                # multi-part export onto one filename. The file host's URL path
                # carries the true name, e.g. `takeout-20260919T163231Z-1-001.zip`.
                real_name = (part_filename_from_url(minted_url)
                             or (part.filename if part.filename not in ("", "download") else "")
                             or f"part-{idx}.zip")
                if real_name != part.filename:
                    part.filename = real_name
                    ledger.set_part_filename(cfg.archive_id, idx, real_name)
                staged = os.path.join(cfg.staging_dir, real_name)

                # ---- mint-only: stop after every URL is earned --------------
                # Measured 2026-09-19: a minted URL still served `206` with real zip
                # bytes **81 minutes after it was minted**, long past the ~45-minute
                # ReAuth window. Minting is what the window gates; TRANSFERRING only
                # needs the jar.
                #
                # That matters at real scale. This loop mints per part as it goes, so a
                # 63-part export taking hours would run out of window part-way and
                # stop with `needs_reauth` — unable to earn the later URLs. Minting all
                # of them first, while the window is fresh, makes the long transfer
                # pass independent of it. Since minted URLs are already cached in the
                # ledger, a second ordinary run picks them up and never mints again.
                if cfg.mint_only:
                    continue

                # ---- transfer (Range resume is free) -----------------------
                ledger.set_job_status(cfg.archive_id, "transferring")
                try:
                    transfer = await download(
                        http_client, minted_url, staged,
                        jar_header=jar.header,
                        expected_size=expected,
                        etag=ledger.part(cfg.archive_id, idx)["etag"],
                    )
                except NeedsReauth as exc:
                    # A transfer that is rejected for auth means THIS URL did not
                    # work — session-wide or URL-expiry, we cannot tell which. It
                    # was the only failure path that neither recorded the attempt
                    # nor dropped the cached URL, so after a human re-authed, the
                    # next run re-used the same rejected URL, failed identically,
                    # and set `needs_reauth` again: a status a human could never
                    # leave. Observed by two independent reviewers; it is the exact
                    # v2 "park forever" shape this rebuild exists to remove.
                    ledger.record_failed_transfer(cfg.archive_id, idx, exc)
                    ledger.clear_mint(cfg.archive_id, idx)
                    ledger.set_job_status(cfg.archive_id, "needs_reauth", error=str(exc))
                    return await _finish("needs_reauth", error=str(exc))
                except RemoteChanged as exc:
                    # the remote object is not what we planned against: the cached
                    # URL is useless, so drop it and let a later pass re-mint
                    ledger.record_failed_transfer(cfg.archive_id, idx, exc)
                    ledger.clear_mint(cfg.archive_id, idx)
                    ledger.set_part_status(cfg.archive_id, idx, "partial",
                                           error=f"remote changed: {exc}")
                    continue
                except (TransferError, AutopilotError) as exc:
                    # A failed GET is still a GET. It used to book nothing, so the
                    # ledger reported `transfer 0` for runs that had made real
                    # requests — and the counter is the only budget signal this
                    # project has.
                    ledger.record_failed_transfer(cfg.archive_id, idx, exc)
                    # A cached URL that just failed is not evidence of a working
                    # URL, but neither is a transient blip: re-minting costs Δ1 and
                    # the export's allowance is only 5. So retry the cached URL a
                    # bounded number of times, then drop it rather than wedge the
                    # part on it forever (the only other invalidation was
                    # RemoteChanged).
                    if ledger.failed_transfer_count(cfg.archive_id, idx) >= MAX_CACHED_URL_FAILURES:
                        ledger.clear_mint(cfg.archive_id, idx)
                    ledger.set_part_status(cfg.archive_id, idx, "failed", error=str(exc))
                    continue

                kind = AttemptKind.RESUME if transfer.resumed_from else AttemptKind.TRANSFER
                # `already-complete` means `download()` short-circuited because the
                # staged file was already the expected size — it made ZERO requests,
                # so booking it would increment the attempt counter for work that was
                # not attempted, and the counter is the only budget signal here.
                if transfer.status != "already-complete":
                    ledger.record_transfer(cfg.archive_id, idx, kind, transfer.status,
                                           bytes_moved=transfer.bytes_written,
                                           size_on_disk=transfer.total_bytes,
                                           etag=transfer.etag)

                if not transfer.complete:
                    ledger.set_part_status(cfg.archive_id, idx, "partial")
                    continue

                # ---- verify on LOCAL disk (cheap reads) --------------------
                ledger.set_job_status(cfg.archive_id, "verifying")
                outcome = verify_local(staged, size_expected=expected,
                                       hash_check=cfg.verify_hash)
                if not outcome.ok:
                    ledger.set_part_status(
                        cfg.archive_id, idx,
                        "partial" if outcome.resumable else "failed",
                        error=outcome.detail)
                    continue

                # ---- move (rename last; index kept in memory) --------------
                ledger.set_job_status(cfg.archive_id, "moving")
                moved = move_part(
                    staged, cfg.archive_dir, filename=real_name,
                    expected_size=expected,
                    **{k: v for k, v in (("timeout", cfg.move_timeout),
                                         ("stall", cfg.move_stall)) if v is not None})
                if moved.action == "moved":
                    dest_index.names[real_name] = expected or 0
                    ledger.set_part_status(cfg.archive_id, idx, "done")
                    # Free the staged copy once the archive copy is verified.
                    #
                    # Staging is scratch; the archive is the copy of record. Keeping
                    # both costs a full duplicate of the export, and on the 64-product
                    # pull that is 131.6 GB of a 300 GB volume that must also hold
                    # rclone's 100 GB VFS cache — i.e. it does not fit (measured
                    # 2026-09-21: 194 GB free vs ~232 GB wanted). OPT-IN because the
                    # staged copy is the only way to re-verify without re-downloading,
                    # and for a small export that safety net is worth its size.
                    # Only ever after a verified move, never before.
                    if cfg.clean_staging:
                        try:
                            os.unlink(staged)
                        except OSError as exc:
                            print(f"warning: part {idx}: moved and verified, but the "
                                  f"staged copy could not be removed ({exc})",
                                  file=sys.stderr)
                else:
                    # Verified but not moved: the part is NOT done — it is still
                    # only staged, and saying otherwise would overstate completeness.
                    ledger.set_part_status(cfg.archive_id, idx, "partial",
                                           error=f"move failed: {moved.detail}")

            # ---- 6. verdict -------------------------------------------------
            summary = ledger.summary(cfg.archive_id)
            remaining = summary["parts"] - summary["done"]
            status = "complete" if remaining == 0 and summary["parts"] else "incomplete"
            ledger.set_job_status(cfg.archive_id, status)
            return await _finish(status)
    except BrowserUnavailable as exc:
        # A browser that cannot be reached means this job cannot proceed, and it
        # must not be left mid-flight with no explanation. Narrow on purpose: this
        # type is raised only by the session factory's own connect, so catching it
        # here does not swallow defects elsewhere in the run.
        _why = f"cannot reach the browser at {cfg.cdp_http}: {exc}"
        try:
            ledger.set_job_status(cfg.archive_id, "failed", error=_why)
        except Exception:  # noqa: BLE001 - the ledger itself may be the problem
            pass
        return await _finish("failed", error=_why)
    except AutopilotError as exc:
        # A classified failure that escaped a specific handler still becomes a REPORTED
        # status rather than a traceback. Without this, a CDP session whose reader died
        # mid-run (now `CdpError`, which is an `AutopilotError`) propagated straight out
        # of `run_once`, past the CLI, and left the job parked on whatever status it held
        # with no record of why.
        #
        # Deliberately scoped to the project's own error family: a `TypeError` or
        # `KeyError` is a defect and SHOULD surface as a traceback.
        _why = f"{type(exc).__name__}: {exc}"
        try:
            ledger.set_job_status(cfg.archive_id, "failed", error=_why)
        except Exception:  # noqa: BLE001 - the ledger itself may be the problem
            pass
        return await _finish("failed", error=_why)
    finally:
        ledger.close()


def _needs_work(ledger: Ledger, archive_id: str, idx) -> bool:
    row = ledger.part(archive_id, idx)
    if row is None:
        return True
    return row["status"] not in ("done",)


def run_once_sync(cfg: RunConfig, **kwargs) -> RunOutcome:
    """Blocking wrapper for CLI and shell callers."""
    return asyncio.run(run_once(cfg, **kwargs))
