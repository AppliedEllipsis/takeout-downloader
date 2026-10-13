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
* **Backend confirmation is the archive receipt.** Remote mode never indexes FUSE
  or adopts its size-only files. Native-local copies require hash comparison.
* **Everything is injectable** (`session_factory`, `http_client`, `now`) so the
  whole pipeline is testable with no browser, no network and no clock.
"""
from __future__ import annotations

import asyncio
import os
import uuid
import zipfile
from urllib.parse import urlsplit
from contextlib import asynccontextmanager, AsyncExitStack
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
from .ledger import AttemptKind, Ledger, open_ledger, safe_filename, carries_part_identity, fuse_mount_for, _read_mounts
from .mint import mint as mint_part, part_filename_from_url
from .mover import (
    assert_destination_ready,
    assert_staging_ready,
    index_destination,
    move_part,
)
from .report import build_report, parse_expiry
from .scrape import (
    ArchivePage,
    PartLink,
    archive_url,
    page_has_rapt,
    read_archive,
    recover_rapt_url,
)
from .transport import TransferError, RemoteChanged, download
from .verify import verify_local, VerifyOutcome
from .remote import RemoteConfig, archive_remote_part, verify_remote_part
from .mint import parse_query

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
    #: Safety floor added to remaining bytes and staging_reserve per transfer.
    #: None means no additional floor; remaining-byte capacity is always checked.
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

    archive_remote: Optional[str] = None
    rclone_binary: str = "rclone"
    rclone_config: Optional[str] = None
    remote_timeout: float = 21600
    staging_reserve: int = 0
    cached_transfer_only: bool = False

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


def page_from_ledger(ledger, archive_id: str, *, cached_transfer_only: bool = False) -> Optional[ArchivePage]:
    """Reconstruct ALL rows, never a minted subset; retain expected-count evidence."""
    rows = ledger.parts(archive_id)
    if not rows:
        return None
    if not cached_transfer_only:
        for r in rows:
            if not safe_filename(r.filename or "") or not carries_part_identity(r.filename or ""):
                return None
            if r.status != "done" and not r.minted_url:
                return None
    job = ledger.job(archive_id)
    count = max(len(rows), (job["expected_count"] or 0) if job else 0)
    return ArchivePage(parts=[PartLink(index=r.idx, raw_uri="", filename=r.filename or "",
                                     size=str(r.size_expected) if r.size_expected else "")
                              for r in rows],
                       expiry=job["expiry_at"] if job else None, part_of_N=[count])


def _get_json(url: str, timeout: float) -> dict:
    import json
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def pick_page_target(targets, *, prefer_host: str = "takeout.google.com"):
    """Choose which page target to drive. Pure, so it is testable offline.

    Prefer a tab already on the Takeout host (the session cookie is right there
    and the page is warm); unrelated tabs are never driven.

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
    preferred = [t for t in pages if urlsplit(t.get("url") or "").hostname == prefer_host]
    if not preferred:
        raise BrowserUnavailable("no Takeout tab available; refusing to drive unrelated tabs")
    return preferred[0]


def _default_session_factory(cdp_http: str, timeout: float = 20.0, *, read_only: bool = False):
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
            if read_only:
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if not pages:
                    raise BrowserUnavailable("no page session available for cookie read")
                target = pages[0]  # cookie/UA read ONLY; never navigate this human tab
            else:
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


def _safe_path(directory: str, filename: str) -> str:
    if not safe_filename(filename) or not carries_part_identity(filename):
        raise AutopilotError("unsafe or missing real part filename")
    path = os.path.join(directory, filename)
    if os.path.normcase(os.path.realpath(directory)) != os.path.normcase(os.path.abspath(directory)):
        raise AutopilotError("unsafe directory path")
    if os.path.islink(path):
        raise AutopilotError("unsafe part path")
    return path


def _verify_part(path, **kwargs):
    if path.lower().endswith(".zip") and os.path.isfile(path) and not zipfile.is_zipfile(path):
        size = os.path.getsize(path)
        if size >= (kwargs.get("size_expected") or 0):
            return VerifyOutcome(False, "CORRUPT", "declared ZIP lacks a valid central directory", size, False)
    result = verify_local(path, **kwargs)
    if result.ok and not result.sha256:
        return VerifyOutcome(False, "UNVERIFIED", "SHA256 evidence unavailable", result.size, True)
    return result


def _sanitized(exc) -> str:
    import re
    return re.sub(r"https?://[^\s]+", "<url>", str(exc))[:300]


def _remote_path(cfg, name):
    root = cfg.destination.split(":", 1)[-1].strip("/")
    return cfg.destination.rstrip("/") + ("/" if root else "") + name


def _remote_confirmed(result, cfg, name, size, digest) -> bool:
    return (result.confirmed and result.action in ("uploaded", "already-present")
            and result.remote_path == _remote_path(cfg, name)
            and result.size == size and result.sha256 == digest)


def _staged_complete(cfg, row) -> bool:
    name = row["filename"] or ""
    if not safe_filename(name) or not carries_part_identity(name) or not row["size_expected"]:
        return False
    path = _safe_path(cfg.staging_dir, name)
    return os.path.isfile(path) and os.path.getsize(path) == row["size_expected"]


async def run_once(cfg: RunConfig, *, session_factory: Optional[Callable] = None,
                   http_client=None, now: Optional[datetime] = None) -> RunOutcome:
    """One bounded pass. Completion requires backend/native hash confirmation."""
    now = now or datetime.now(timezone.utc)
    injected_session_factory = session_factory is not None
    session_factory = session_factory or _default_session_factory(cfg.cdp_http)
    http_client = http_client or _default_http_client()
    os.makedirs(cfg.staging_dir, exist_ok=True)
    ledger = open_ledger(cfg.ledger_path)
    expiry = None
    expected_count = 0
    list_evidence = False
    remote = (RemoteConfig(cfg.archive_remote, cfg.rclone_binary, cfg.rclone_config,
                           cfg.remote_timeout) if cfg.archive_remote else None)

    async def finish(status, error=None):
        ledger.set_job_status(cfg.archive_id, status, error=error)
        rows = ledger.parts(cfg.archive_id)
        job = ledger.job(cfg.archive_id)
        count = max(expected_count, len(rows), job["expected_count"] or 0)
        report = build_report(rows, archive_id=cfg.archive_id, account=cfg.account,
                              status=status, expiry_at=expiry, now=now,
                              attempts=ledger.attempts_by_kind(cfg.archive_id),
                              attempt_kinds=ledger.attempt_kinds_by_part(cfg.archive_id))
        report.parts_expected = count
        report.parts_done = sum(r.status == "done" for r in rows)
        if not report.complete or not list_evidence:
            report.verdict = "INCOMPLETE"
        if not list_evidence:
            report.warnings.append("Expected-count evidence is absent; recorded rows are not proof of the complete export list.")
        return RunOutcome(cfg.archive_id, status, count, report.parts_done,
                          sum(r.size_on_disk for r in rows if r.status == "done"),
                          report.as_markdown(), error, list(report.warnings))

    try:
        prior = ledger.job(cfg.archive_id)
        if prior and cfg.account and prior["account"] and cfg.account != prior["account"]:
            return RunOutcome(cfg.archive_id, "failed", error="account mismatch for recorded archive")
        ledger.upsert_job(cfg.archive_id, account=cfg.account, output_dir=cfg.archive_dir)
        list_evidence = bool(prior and prior["expected_count"])
        if cfg.work_url:
            work = urlsplit(cfg.work_url)
            if (work.scheme != "https" or work.hostname != "takeout.google.com"
                    or work.username or work.password
                    or work.path != "/manage/archive/" + cfg.archive_id):
                return await finish("failed", "unsafe archive work URL or archive mismatch")
        if prior and prior["status"] in TERMINAL_BLOCKED_STATUSES:
            expiry = prior["expiry_at"]
            return await finish(prior["status"], prior["error"] or "terminal export")
        if cfg.mint_only and cfg.cached_transfer_only:
            return await finish("failed", "mint_only and cached_transfer_only are incompatible")
        if cfg.staging_reserve < 0 or (cfg.min_headroom is not None and cfg.min_headroom < 0):
            return await finish("failed", "headroom and reserve must be nonnegative")
        assert_staging_ready(cfg.staging_dir, min_headroom=cfg.min_headroom)
        async with AsyncExitStack() as stack:
            session = None
            jar = None

            async def browser(*, read_only=False):
                nonlocal session
                if session is None:
                    factory = session_factory
                    if read_only and not injected_session_factory:
                        factory = _default_session_factory(cfg.cdp_http, read_only=True)
                    session = await stack.enter_async_context(factory())
                return session

            page = page_from_ledger(ledger, cfg.archive_id,
                                    cached_transfer_only=cfg.cached_transfer_only)
            if page is None and not cfg.mint_only:
                recorded = ledger.parts(cfg.archive_id)
                if recorded and all(r.status == "done" or r.minted_url or
                                    _staged_complete(cfg, ledger.part(cfg.archive_id, r.idx)) for r in recorded):
                    page = page_from_ledger(ledger, cfg.archive_id, cached_transfer_only=True)
            if page is None and cfg.cached_transfer_only:
                return await finish("incomplete", "no recorded part list; cached-only never scrapes or mints")
            if page is None:
                session = await browser()
                work_url = cfg.resolved_work_url()
                if not cfg.work_url:
                    work_url = await recover_rapt_url(session, cfg.archive_id) or work_url
                page = await read_archive(session, work_url, settle=cfg.settle)
                list_evidence = not page.challenged and bool(page.parts)
                if page.challenged:
                    return await finish("needs_reauth", "archive authentication challenge; left visible")
            page.assert_indexed()
            if not page.parts:
                return await finish("failed", "no parts were recorded")

            def validate_page(p):
                users = {x.user for x in p.parts if x.user}
                if len(users) > 1 or any(x.archive_id and x.archive_id != cfg.archive_id for x in p.parts):
                    raise AutopilotError("archive/account mismatch in discovered parts")
                user = next(iter(users), None)
                job = ledger.job(cfg.archive_id)
                requested = parse_query(cfg.work_url or "").get("user")
                if requested and job["user_id"] and requested != job["user_id"]:
                    raise AutopilotError("requested account mismatch")
                known = job["user_id"] or requested or (cfg.account if (cfg.account or "").isdigit() else None)
                if user and known and known != user:
                    raise AutopilotError("discovered account mismatch")
                for x in p.parts:
                    if x.redirector:
                        parsed = urlsplit(x.redirector)
                        if parsed.scheme != "https" or parsed.hostname != "takeout.google.com" or parsed.username or parsed.password:
                            raise AutopilotError("unsafe redirector")
                return user

            user = validate_page(page)
            expiry = page.expiry
            expected_count = max([len(page.parts), ledger.job(cfg.archive_id)["expected_count"] or 0]
                                 + [int(n) for n in page.part_of_N])
            if list_evidence:
                ledger.record_part_list(cfg.archive_id, expected_count=expected_count, user_id=user)
            ledger.upsert_job(cfg.archive_id, expiry_at=expiry)
            ledger.upsert_parts(cfg.archive_id, [(p.index, p.filename, _int_size(p.size)) for p in page.parts])
            exp = parse_expiry(expiry)
            if exp is not None and now > exp:
                return await finish("expired_unrecoverable", "export expired; new export required")
            restore_url = page.url or cfg.resolved_work_url()
            needs_mint = not cfg.cached_transfer_only and any(
                ledger.part(cfg.archive_id, p.index)["status"] != "done"
                and not ledger.minted_url(cfg.archive_id, p.index)
                and not _staged_complete(cfg, ledger.part(cfg.archive_id, p.index))
                for p in page.parts)
            if needs_mint and not page_has_rapt(page):
                # The historical provoke helper navigates a download redirector without
                # ownership tracking. Do not let a token probe leak browser downloads.
                return await finish("needs_reauth", "archive links lack rapt; human authentication required")

            for p in page.parts:
                row = ledger.part(cfg.archive_id, p.index)
                if row["status"] != "done":
                    continue
                name = row["filename"] or ""
                valid = False
                if remote and safe_filename(name) and row["sha256"]:
                    # Stable backend receipts are the durable evidence. Re-reading
                    # all prior multi-GB parts on every max_parts=1 pass is quadratic.
                    # Changed paths, legacy/unconfirmed rows still need backend proof.
                    valid = (row["remote_path"] == _remote_path(remote, name)
                             and bool(row["remote_verified_at"])
                             and row["size_expected"] is not None
                             and row["size_on_disk"] == row["size_expected"]
                             and len(row["sha256"]) == 64
                             and all(c in "0123456789abcdef" for c in row["sha256"]))
                    if not valid:
                        result = await asyncio.to_thread(verify_remote_part, remote, filename=name,
                                                        expected_size=row["size_expected"], sha256=row["sha256"])
                        valid = _remote_confirmed(result, remote, name, row["size_expected"], row["sha256"])
                        if valid:
                            ledger.record_verified(cfg.archive_id, p.index, sha256=result.sha256,
                                                   size=result.size, remote_path=result.remote_path)
                elif not remote and row["sha256"]:
                    assert_destination_ready(cfg.archive_dir, require_mount=cfg.require_mount)
                    if not fuse_mount_for(cfg.archive_dir, _read_mounts()):
                        verified = _verify_part(_safe_path(cfg.archive_dir, name),
                                                size_expected=row["size_expected"], hash_check=True,
                                                sha256_expected=row["sha256"])
                        valid = verified.ok
                if not valid:
                    ledger.invalidate_receipt(cfg.archive_id, p.index)

            todo = [p for p in page.parts if ledger.part(cfg.archive_id, p.index)["status"] != "done"]
            if cfg.cached_transfer_only:
                todo = [p for p in todo if ledger.minted_url(cfg.archive_id, p.index)]
            if cfg.max_parts is not None:
                todo = todo[:max(0, cfg.max_parts)]
            for part in todo:
                idx = part.index
                row = ledger.part(cfg.archive_id, idx)
                expected = _int_size(row["size_expected"])
                minted_url = row["minted_url"]
                name = row["filename"] or ""
                staged_complete = _staged_complete(cfg, row)
                # CRC supplies evidence for ZIP adoption; opaque files need a trusted digest.
                if not remote and not cfg.mint_only and safe_filename(name) and carries_part_identity(name) and expected:
                    assert_destination_ready(cfg.archive_dir, require_mount=cfg.require_mount)
                    if not fuse_mount_for(cfg.archive_dir, _read_mounts()):
                        dest = _safe_path(cfg.archive_dir, name)
                        if os.path.isfile(dest):
                            adopted = _verify_part(dest, size_expected=expected, hash_check=True,
                                                   sha256_expected=row["sha256"])
                            if adopted.ok and (row["sha256"] or name.lower().endswith(".zip")):
                                ledger.record_verified(cfg.archive_id, idx, sha256=adopted.sha256, size=expected)
                                ledger.set_part_status(cfg.archive_id, idx, "done")
                                continue
                if not minted_url and not staged_complete:
                    if cfg.cached_transfer_only:
                        continue
                    if not part.redirector:
                        ledger.set_part_status(cfg.archive_id, idx, "pending", error="no cached URL or redirector")
                        continue
                    ledger.set_job_status(cfg.archive_id, "minting")
                    try:
                        result = await mint_part(await browser(), part.redirector, settle=cfg.settle, restore_url=restore_url)
                    except NeedsReauth:
                        return await finish("needs_reauth", "mint authentication challenge; left visible")
                    except QuotaExceeded:
                        return await finish("quota_exceeded", "export download allowance exhausted")
                    except MintError as exc:
                        ledger.set_part_status(cfg.archive_id, idx, "failed", error=_sanitized(exc))
                        continue
                    minted_url = result.url
                    ledger.record_mint(cfg.archive_id, idx, minted_url)
                    if not getattr(result, "download_cleanup_confirmed", True):
                        return await finish("failed", "automation download cancellation failed; URL cached, human cleanup required")
                if minted_url:
                    query = parse_query(minted_url)
                    if ((query.get("j") and query["j"] != cfg.archive_id)
                            or (query.get("i") and query["i"] != str(idx))):
                        raise AutopilotError("cached URL archive/part mismatch")
                    url_name = part_filename_from_url(minted_url)
                    if url_name:
                        if name != url_name and carries_part_identity(name) and staged_complete:
                            raise AutopilotError("staged filename and cached URL mismatch")
                        name = url_name
                staged = _safe_path(cfg.staging_dir, name)
                ledger.set_part_filename(cfg.archive_id, idx, name)
                if cfg.mint_only:
                    continue
                if expected is None:
                    ledger.set_part_status(cfg.archive_id, idx, "pending", error="known exact size required")
                    continue
                have = os.path.getsize(staged) if os.path.isfile(staged) else 0
                if have != expected:
                    remaining = max(0, expected - have) if row["etag"] else expected
                    assert_staging_ready(cfg.staging_dir, min_headroom=remaining + cfg.staging_reserve + (cfg.min_headroom or 0))
                    if not minted_url:
                        ledger.set_part_status(cfg.archive_id, idx, "pending", error="no cached URL")
                        continue
                    if jar is None:
                        jar = await pull_jar(await browser(read_only=True))
                    ledger.set_job_status(cfg.archive_id, "transferring")
                    optional_headers = {}
                    if getattr(jar, "user_agent", None):
                        import inspect
                        if "user_agent" in inspect.signature(download).parameters:
                            optional_headers["user_agent"] = jar.user_agent
                    try:
                        transfer = await download(http_client, minted_url, staged, jar_header=jar.header,
                                                  expected_size=expected, etag=row["etag"], expected_filename=name,
                                                  **optional_headers)
                    except NeedsReauth as exc:
                        ledger.record_failed_transfer(cfg.archive_id, idx, exc)
                        ledger.clear_mint(cfg.archive_id, idx)
                        return await finish("needs_reauth", "cached URL rejected authentication")
                    except RemoteChanged as exc:
                        ledger.record_failed_transfer(cfg.archive_id, idx, exc)
                        ledger.clear_mint(cfg.archive_id, idx)
                        ledger.set_part_status(cfg.archive_id, idx, "partial", error="remote changed")
                        continue
                    except TransferError as exc:
                        ledger.record_failed_transfer(cfg.archive_id, idx, exc)
                        ledger.set_part_status(cfg.archive_id, idx, "partial", error=_sanitized(exc))
                        continue
                    if transfer.status != "already-complete":
                        ledger.record_transfer(cfg.archive_id, idx,
                                               AttemptKind.RESUME if transfer.resumed_from else AttemptKind.TRANSFER,
                                               transfer.status, bytes_moved=transfer.bytes_written,
                                               size_on_disk=transfer.total_bytes, etag=transfer.etag)
                    if not transfer.complete or transfer.error:
                        # A failed stream can leave exactly the expected byte count.
                        # Preserve those bytes as evidence, but never let a later
                        # already-complete shortcut turn the failed read into success.
                        if transfer.error and os.path.isfile(staged) and os.path.getsize(staged) >= expected:
                            os.rename(staged, staged + ".corrupt-" + uuid.uuid4().hex)
                        ledger.set_part_status(cfg.archive_id, idx, "partial", error=_sanitized(transfer.error) if transfer.error else None)
                        continue
                ledger.set_job_status(cfg.archive_id, "verifying")
                outcome = _verify_part(staged, size_expected=expected, hash_check=True,
                                       sha256_expected=row["sha256"])
                if not outcome.ok:
                    # Byte-complete corrupt ZIPs must not short-circuit forever.
                    if os.path.isfile(staged) and os.path.getsize(staged) >= expected and not outcome.resumable:
                        os.rename(staged, staged + ".corrupt-" + uuid.uuid4().hex)
                    ledger.set_part_status(cfg.archive_id, idx, "partial" if outcome.resumable else "failed", error=outcome.detail)
                    continue
                ledger.record_verified(cfg.archive_id, idx, sha256=outcome.sha256, size=expected)
                ledger.set_job_status(cfg.archive_id, "moving")
                confirmed = False
                if remote:
                    result = await asyncio.to_thread(archive_remote_part, staged, remote, filename=name,
                                                    expected_size=expected, sha256=outcome.sha256)
                    confirmed = _remote_confirmed(result, remote, name, expected, outcome.sha256)
                    if confirmed:
                        ledger.record_verified(cfg.archive_id, idx, sha256=outcome.sha256,
                                               size=expected, remote_path=result.remote_path)
                else:
                    assert_destination_ready(cfg.archive_dir, require_mount=cfg.require_mount)
                    native = not fuse_mount_for(cfg.archive_dir, _read_mounts())
                    moved = await asyncio.to_thread(move_part, staged, cfg.archive_dir, filename=name,
                                                   expected_size=expected,
                                                   **{k:v for k,v in (("timeout", cfg.move_timeout), ("stall", cfg.move_stall)) if v is not None})
                    assert_destination_ready(cfg.archive_dir, require_mount=cfg.require_mount)
                    native = native and not fuse_mount_for(cfg.archive_dir, _read_mounts())
                    if native and moved.action in ("moved", "already-present", "skipped-present"):
                        verified = _verify_part(_safe_path(cfg.archive_dir, name), size_expected=expected,
                                                hash_check=True, sha256_expected=outcome.sha256)
                        confirmed = verified.ok
                if confirmed:
                    ledger.set_part_status(cfg.archive_id, idx, "done")
                    if cfg.clean_staging:
                        try:
                            os.unlink(staged)
                        except OSError:
                            pass
                else:
                    ledger.set_part_status(cfg.archive_id, idx, "partial", error="archive not backend/hash confirmed; staged bytes retained")
            summary = ledger.summary(cfg.archive_id)
            complete = list_evidence and summary["done"] == expected_count and summary["parts"] == expected_count and expected_count > 0
            return await finish("complete" if complete and not cfg.mint_only else "incomplete")
    except CookieError as exc:
        return await finish("needs_reauth", _sanitized(exc))
    except (AutopilotError, OSError) as exc:
        return await finish("failed", _sanitized(exc))
    finally:
        ledger.close()


def run_once_sync(cfg: RunConfig, **kwargs) -> RunOutcome:
    return asyncio.run(run_once(cfg, **kwargs))
