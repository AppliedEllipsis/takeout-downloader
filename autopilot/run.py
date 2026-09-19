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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .errors import AutopilotError, MintError, NeedsReauth
from .jar import CookieError, pull_jar
from .ledger import AttemptKind, Ledger, open_ledger
from .mint import mint as mint_part
from .mover import (
    DestinationIndex,
    MoveRefused,
    assert_destination_ready,
    index_destination,
    move_part,
    plan_moves,
)
from .report import build_report, parse_expiry
from .scrape import archive_url, read_archive
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
    "failed",
)


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


def _get_json(url: str, timeout: float) -> dict:
    import json
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _default_session_factory(cdp_http: str, timeout: float = 20.0):
    """Connect to the browser-level CDP endpoint.

    The bare `ws://host:port` is rejected with HTTP 404 — measured — so the
    endpoint must be discovered from `/json/version`, and its UUID changes on
    every browser restart.
    """
    from .cdp import CdpSession
    from .ws_transport import WebSocketTransport, browser_endpoint

    @asynccontextmanager
    async def _factory():
        info = await asyncio.to_thread(_get_json, cdp_http.rstrip("/") + "/json/version", timeout)
        transport = await WebSocketTransport.connect(browser_endpoint(info))
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
    try:
        ledger.upsert_job(cfg.archive_id, account=cfg.account,
                          output_dir=cfg.archive_dir)
        ledger.set_job_status(cfg.archive_id, "scraping")

        async def _finish(status: str, *, error: Optional[str] = None) -> RunOutcome:
            rows = ledger.parts(cfg.archive_id)
            report = build_report(rows, archive_id=cfg.archive_id, account=cfg.account,
                                  status=status, expiry_at=_expiry_seen, now=now,
                                  attempts=ledger.attempts_by_kind(cfg.archive_id))
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

        async with session_factory() as session:
            # ---- 1. scrape -------------------------------------------------
            page = await read_archive(session, cfg.resolved_work_url(), settle=cfg.settle)
            if page.challenged:
                ledger.set_job_status(cfg.archive_id, "needs_reauth",
                                      error=f"challenge at {page.url}")
                return await _finish("needs_reauth",
                                     error=f"the archive page is a sign-in page: {page.url}")

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

            ledger.set_job_status(cfg.archive_id, "transferring")
            dest_index = index_destination(cfg.archive_dir)
            todo = [p for p in page.parts if _needs_work(ledger, cfg.archive_id, p.index)]
            if cfg.max_parts is not None:
                todo = todo[: max(0, cfg.max_parts)]

            for part in todo:
                idx = part.index
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
                                                 restore_url=cfg.resolved_work_url())
                    except NeedsReauth as exc:
                        ledger.set_job_status(cfg.archive_id, "needs_reauth", error=str(exc))
                        return await _finish("needs_reauth", error=str(exc))
                    except (MintError, AutopilotError) as exc:
                        ledger.set_part_status(cfg.archive_id, idx, "failed",
                                               error=f"mint failed: {exc}")
                        continue
                    minted_url = result.url
                    ledger.record_mint(cfg.archive_id, idx, minted_url)

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
                    ledger.set_job_status(cfg.archive_id, "needs_reauth", error=str(exc))
                    return await _finish("needs_reauth", error=str(exc))
                except RemoteChanged as exc:
                    # the remote object is not what we planned against: the cached
                    # URL is useless, so drop it and let a later pass re-mint
                    ledger.clear_mint(cfg.archive_id, idx)
                    ledger.set_part_status(cfg.archive_id, idx, "partial",
                                           error=f"remote changed: {exc}")
                    continue
                except (TransferError, AutopilotError) as exc:
                    ledger.set_part_status(cfg.archive_id, idx, "failed", error=str(exc))
                    continue

                kind = AttemptKind.RESUME if transfer.resumed_from else AttemptKind.TRANSFER
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
                moved = move_part(staged, cfg.archive_dir, expected_size=expected)
                if moved.action == "moved":
                    dest_index.names[part.filename or os.path.basename(staged)] = expected or 0
                    ledger.set_part_status(cfg.archive_id, idx, "done")
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
