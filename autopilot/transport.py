"""Move bytes from a minted URL. The other half of "browser mints, transporter moves".

WHAT IS MEASURED AND WHAT IS NOT
---------------------------------
Measured 2026-09-19 (`.wiki/measured-facts.md`):

* A **`Range` resume is free** — `Δ0` on Google's counter, repeatedly.
* The **file host requires the cookie jar**: the same request returns `206` with
  the jar and `302 -> accounts.google.com/ServiceLogin` without it.
* A **replayed jar works** against the file host, from a plain HTTP client.
* The host advertises a **strong validator** (`ETag`) and **`Accept-Ranges: bytes`**,
  and `Content-Range` reports the correct total size.

**NOT measured, and deliberately not asserted anywhere in this module:** what a
*fresh, full* client-side `GET` costs on the counter. Every measured `Δ0` was a
`Range` request. Do not let a caller budget a first full transfer as free.

WHY THE CASES BELOW EXIST
-------------------------
Each guard corresponds to a way this project actually lost data or wasted days:

* **Torn tails.** A process killed mid-write (or a FUSE mount that only partly
  flushed) leaves `getsize()` reporting bytes that are not really there.
  Appending after that silently corrupts a 10 GiB part, discovered at extract
  time days later. → `aligned_resume_offset`, ported from `takeout2/engine.py:152`.
* **A server that ignores `Range`.** If it answers `200` instead of `206`, the
  body is the whole file from byte 0 — appending it to a partial file produces
  garbage of roughly double length. → truncate and restart.
* **A changed remote object.** `If-Range` makes the server answer `200` when the
  validator no longer matches, which is exactly the signal to start over.
* **Treating a sign-in redirect as a generic failure.** That is what produced
  three months of `needs_cookie` parks and a tab flood (failure modes 1.9/1.18).
  → `NeedsReauth`, and a *different* error when we simply forgot the jar.
"""
from __future__ import annotations

import asyncio
import errno
import os
import re
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol

from .errors import AutopilotError, NeedsReauth, redact_error

__all__ = [
    "CHUNK",
    "REWIND",
    "ACCOUNTS_HOST",
    "TransferResult",
    "TransferError",
    "RemoteChanged",
    "RateLimited",
    "HttpStream",
    "HttpClient",
    "UrllibHttpClient",
    "aligned_resume_offset",
    "download",
]

CHUNK = 1 << 20          # 1 MiB read size
REWIND = 8 << 20         # 8 MiB aligned rewind, matching v2's WRITE_CHUNK
ACCOUNTS_HOST = "accounts.google.com"


# ---------------------------------------------------------------------------
# Torn-tail protection
# ---------------------------------------------------------------------------
def aligned_resume_offset(size_on_disk: int, rewind: int) -> int:
    """Safe byte offset to resume from, given what is on disk.

    Ported from `takeout2/engine.py:152` — pure arithmetic, kept identical so
    the measured-good behaviour is preserved. A process killed mid-write can
    leave a torn tail: `getsize()` reports bytes that are not really there, and
    appending after that garbage silently corrupts the part. So rewind to an
    aligned boundary below the reported size and re-fetch the overlap.
    """
    if size_on_disk <= 0:
        return 0
    if rewind <= 0:
        return size_on_disk
    trimmed = size_on_disk - rewind
    if trimmed <= 0:
        return 0
    return trimmed - (trimmed % rewind)


# ---------------------------------------------------------------------------
# HTTP abstraction (injectable, so this module is testable with no network)
# ---------------------------------------------------------------------------
class HttpStream(Protocol):
    """A response whose body is read incrementally.

    `status` and `headers` (lower-cased keys) must be available **before** the
    body is read, and the client must **not** follow redirects — the transporter
    classifies 3xx itself, because a sign-in redirect is a `NeedsReauth`, not a
    normal redirect to chase.
    """

    status: int
    headers: Mapping[str, str]

    async def read(self, n: int) -> bytes: ...
    async def aclose(self) -> None: ...


class HttpClient(Protocol):
    async def get(self, url: str, headers: Mapping[str, str]) -> HttpStream: ...


class _NoRedirect:
    """urllib handler that refuses to follow a redirect, so the 3xx surfaces."""

    def __init__(self) -> None:
        import urllib.request

        class _Handler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: D102
                return None  # -> urllib raises HTTPError carrying the 3xx

        self._opener = urllib.request.build_opener(_Handler)

    def open(self, url: str, headers: Mapping[str, str], timeout: float):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers=dict(headers))
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc  # a response-like object: .status/.headers/.read()


class _UrllibStream:
    def __init__(self, resp) -> None:
        self._resp = resp
        self.status = int(getattr(resp, "status", None) or getattr(resp, "code", 0) or 0)
        self.headers = {k.lower(): v for k, v in (resp.headers or {}).items()}

    async def read(self, n: int) -> bytes:
        return await asyncio.to_thread(self._resp.read, n)

    async def aclose(self) -> None:
        try:
            await asyncio.to_thread(self._resp.close)
        except Exception:
            pass


class UrllibHttpClient:
    """Default client: stdlib only, no new dependency, redirects NOT followed.

    Blocking urllib calls are pushed to a worker thread so the API stays async.
    An aiohttp-backed client can be dropped in later without touching the
    transporter, which is why the protocol exists.
    """

    def __init__(self, timeout: float = 120.0) -> None:
        self._no_redirect = _NoRedirect()
        self.timeout = timeout

    async def get(self, url: str, headers: Mapping[str, str]) -> HttpStream:
        resp = await asyncio.to_thread(self._no_redirect.open, url, headers, self.timeout)
        return _UrllibStream(resp)


# ---------------------------------------------------------------------------
# Result / errors
# ---------------------------------------------------------------------------
class TransferError(AutopilotError):
    """A transfer failed for a reason that is not an authentication demand."""


class RateLimited(TransferError):
    """The server asked us to wait; this is not a reason to mint a new URL."""
    def __init__(self, message: str, retry_after: Optional[str] = None):
        super().__init__(message)
        self.retry_after = retry_after


class RemoteChanged(TransferError):
    """The remote object no longer matches the local expectations.

    Raised when `Content-Range` reports a different total than the ledger
    expected, or when a `Range` request is unsatisfiable. Restarting from zero
    is the caller's decision, because it costs an attempt.
    """


@dataclass
class TransferResult:
    status: str                  # "complete" | "partial" | "already-complete"
    bytes_written: int = 0
    total_bytes: int = 0
    resumed_from: int = 0
    http_status: Optional[int] = None
    etag: Optional[str] = None
    restarted: bool = False      # server ignored Range / validator changed
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.status in ("complete", "already-complete")

    def __str__(self) -> str:
        return (f"<TransferResult {self.status} wrote={self.bytes_written} "
                f"total={self.total_bytes} from={self.resumed_from} "
                f"http={self.http_status} restarted={self.restarted}>")


ProgressFn = Callable[[int, int], None]


# ---------------------------------------------------------------------------
# The transfer
# ---------------------------------------------------------------------------
async def download(
    client: HttpClient,
    url: str,
    dest: str,
    *,
    jar_header: str,
    expected_size: Optional[int] = None,
    etag: Optional[str] = None,
    rewind: int = REWIND,
    chunk: int = CHUNK,
    on_progress: Optional[ProgressFn] = None,
    expected_filename: Optional[str] = None,
    user_agent: str = "Mozilla/5.0",
    host_validator: Optional[Callable[[str], bool]] = None,
) -> TransferResult:
    """Download `url` to `dest`, resuming if `dest` already holds bytes.

    Returns a `TransferResult`; `status == "partial"` means the stream ended
    early and the bytes on disk are a valid resume point (a retry costs `Δ0`).

    Raises `NeedsReauth` when a sign-in/challenge page is returned, and
    `TransferError` when the jar was simply missing (a programming error, not a
    session problem), `RemoteChanged` when the remote object differs.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or not host:
        raise TransferError("unsafe download URL")
    allowed = bool(host_validator(host)) if host_validator else (
        host in {"localhost", "127.0.0.1"} or host.endswith(".googleusercontent.com") or host.endswith(".google.com"))
    if not allowed:
        raise TransferError(f"refusing foreign download host {host!r}")

    if not jar_header:
        # Measured: without the jar the file host answers 302 -> ServiceLogin.
        # Sending nothing and blaming the session would be a misdiagnosis.
        raise TransferError(
            "no cookie jar supplied — the file host is cookie-authenticated "
            "(measured 2026-09-19); pass jar_header from autopilot.jar"
        )

    have = os.path.getsize(dest) if os.path.exists(dest) else 0

    if expected_size is not None and have == expected_size:
        # Nothing to do, and no request is made: a re-download would cost an
        # attempt for zero benefit. (Resumes are free; acquisitions are not.)
        return TransferResult(status="already-complete", total_bytes=have,
                              bytes_written=0, resumed_from=have, etag=etag)

    start = aligned_resume_offset(have, rewind) if have else 0

    # **No validator, no resume.** Fixed 2026-09-19: `If-Range` used to be sent only
    # when an etag was supplied, so a resume with no recorded etag sent a bare
    # `Range` and the server had no way to say "this object was replaced". If the
    # replacement happened to be the SAME SIZE, the new object's tail would land on
    # the old object's prefix, the byte count would match, and `verify_local` would
    # accept the hybrid as structurally fine — silent corruption, no error anywhere.
    #
    # A resume costs Δ0 attempts and a fresh acquisition costs Δ1, so this is a real
    # trade — but bandwidth is recoverable and a corrupt archive is not. Decided
    # BEFORE the headers are built, so no `Range` is sent at all in this case.
    resumed = start > 0 and bool(etag) and not etag.startswith("W/")
    if start > 0 and not resumed:
        start = 0

    headers = {
        "Cookie": jar_header,
        "User-Agent": user_agent,
        "Accept": "*/*",
        # a byte-range download must not be transparently content-encoded
        "Accept-Encoding": "identity",
    }
    if start > 0:
        headers["Range"] = f"bytes={start}-"
        # `If-Range` is always sent on a resume, because `resumed` requires it: if the
        # validator no longer matches, the server answers 200 with the whole body
        # instead of a 206, which is the correct signal to start over.
        headers["If-Range"] = etag

    try:
        stream = await client.get(url, headers)
    except Exception as exc:
        return TransferResult("partial", total_bytes=have, resumed_from=have,
                              etag=etag, error=redact_error(f"client operation failed: {type(exc).__name__}: {exc}"))
    stream_error = None
    try:
        status = stream.status
        hdrs = {k.lower(): v for k, v in stream.headers.items()}

        if status in (301, 302, 303, 307, 308):
            location = hdrs.get("location", "")
            if ACCOUNTS_HOST in location:
                raise NeedsReauth(location, "file host redirected to sign-in")
            raise TransferError(f"unexpected redirect -> {redact_error(location)}")

        if status in (401, 403):
            raise NeedsReauth(url, f"file host returned HTTP {status}")

        if expected_filename:
            cd = hdrs.get("content-disposition", "")
            m = re.search(r"filename\s*=\s*\"?([^\";]+)", cd, re.I)
            if m and os.path.basename(m.group(1).strip()) != os.path.basename(expected_filename):
                raise TransferError("Content-Disposition filename does not match expected filename")
        ctype = hdrs.get("content-type", "").lower()
        if "text/html" in ctype or "application/xhtml" in ctype:
            raise NeedsReauth(url, "HTML authentication/error response")
        if status == 429:
            raise RateLimited("HTTP 429 rate limited", hdrs.get("retry-after"))
        if 500 <= status < 600:
            raise TransferError(f"transient HTTP {status}; retry without reminting")

        # Check declared lengths BEFORE opening/truncating the destination.
        clen = hdrs.get("content-length")
        if clen is not None and not re.fullmatch(r"[0-9]+", clen):
            raise RemoteChanged("invalid Content-Length")
        try:
            response_length = int(clen) if clen is not None else None
        except ValueError:
            raise RemoteChanged("invalid Content-Length") from None
        effective_size = expected_size
        restarted = False
        if status == 206:
            cr = hdrs.get("content-range", "")
            match = re.match(r"\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$", cr)
            if not match:
                raise RemoteChanged("invalid Content-Range")
            try:
                rng_start, rng_end, total = map(int, match.groups())
            except ValueError:
                raise RemoteChanged("invalid Content-Range") from None
            if rng_end < rng_start or rng_start != start or rng_end >= total:
                raise RemoteChanged("invalid Content-Range for requested offset")
            if expected_size is not None and total != expected_size:
                raise RemoteChanged(f"remote total {total} != expected {expected_size}")
            clen = hdrs.get("content-length")
            if clen is not None and (not clen.isdigit() or int(clen) != rng_end - rng_start + 1):
                raise RemoteChanged("Content-Length disagrees with Content-Range")
            resp_etag = hdrs.get("etag")
            if not etag or not resp_etag or etag.startswith("W/") or resp_etag.startswith("W/") or resp_etag != etag:
                raise RemoteChanged("206 resume requires a matching strong ETag")
            effective_size = total
            response_length = rng_end - rng_start + 1
            mode, write_from = "r+b" if os.path.exists(dest) else "wb", start
        elif status == 200:
            # Server ignored Range, or If-Range said the object changed. Either
            # way the body starts at byte 0: appending it would corrupt the file.
            mode, write_from = "wb", 0
            restarted = start > 0
            if response_length is not None:
                if expected_size is not None and response_length != expected_size:
                    raise RemoteChanged("Content-Length disagrees with expected size")
                effective_size = response_length
        elif status == 416:
            raise RemoteChanged(
                f"range unsatisfiable for local size {have} (expected {expected_size})"
            )
        else:
            raise TransferError(f"unexpected HTTP {status} from the file host")

        written = 0
        stream_error = None
        # Disk errors must be CLASSIFIED, not allowed to escape.
        #
        # Fixed 2026-09-19: there was no handler here, and `AutopilotError`
        # subclasses `Exception`, not `OSError` — so an `ENOSPC` mid-write
        # propagated straight out of `download()`, past `run.py`'s
        # `except (TransferError, AutopilotError)`, past the CLI (which caught only
        # `LedgerOnFuseMount` and `KeyboardInterrupt`), and surfaced as an uncaught
        # traceback. The ledger recorded NOTHING: the part kept whatever status it
        # had and the job stayed in `transferring` indefinitely, and the partial file
        # was left behind with no explanation. That is failure mode 1.16 producing a
        # silent wedge instead of a diagnosis.
        try:
            with open(dest, mode) as fh:
                fh.seek(write_from)
                fh.truncate(write_from)
                while True:
                    try:
                        block = await stream.read(chunk)
                    except Exception as exc:
                        stream_error = redact_error(f"stream read failed: {type(exc).__name__}: {exc}")
                        break
                    if not block:
                        break
                    if ((effective_size is not None and write_from + written + len(block) > effective_size)
                            or (response_length is not None and written + len(block) > response_length)):
                        stream_error = "response exceeded declared/expected size"
                        break
                    count = fh.write(block)
                    written += count
                    if count != len(block):
                        stream_error = "short disk write"
                        break
                    if on_progress:
                        try:
                            on_progress(write_from + written, effective_size or 0)
                        except Exception as exc:
                            stream_error = redact_error(
                                f"progress callback failed: {type(exc).__name__}: {exc}")
                            break
                fh.flush()
                os.fsync(fh.fileno())
        except asyncio.CancelledError:
            _rewind_error_tail(dest, write_from + written, effective_size, rewind)
            raise
        except OSError as exc:
            _rewind_error_tail(dest, write_from + written, effective_size, rewind)
            if exc.errno == errno.ENOSPC:
                raise TransferError(
                    f"out of disk space after {written} bytes written to {dest!r} "
                    f"(failure mode 1.16): {exc}"
                ) from None
            raise TransferError(
                f"disk write failed after {written} bytes to {dest!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from None
    finally:
        try:
            await stream.aclose()
        except Exception as exc:
            # Closing must neither leak signed URLs nor turn a full-length
            # response with a stream failure into success. Pending classified
            # auth/header exceptions remain the primary error.
            stream_error = stream_error or redact_error(
                f"stream close failed: {type(exc).__name__}: {exc}")

    total_bytes = write_from + written
    if stream_error:
        total_bytes = _rewind_error_tail(dest, total_bytes, effective_size, rewind)
    complete = not stream_error and effective_size is not None and total_bytes == effective_size
    if not complete and not stream_error:
        stream_error = ("response length unknown; integrity not established" if effective_size is None
                        else f"incomplete response: {total_bytes}/{effective_size} bytes")
    return TransferResult(
        status="complete" if complete else "partial",
        bytes_written=written,
        total_bytes=total_bytes,
        resumed_from=write_from,
        http_status=status,
        etag=_strong_etag(hdrs.get("etag")),
        restarted=restarted,
        error=stream_error,
    )


def _strong_etag(value):
    return value if value and not value.startswith("W/") else None


def _rewind_error_tail(dest, total, expected, rewind):
    """Do not let a failed full-length write become already-complete next time.

    Preserve safe partial prefixes. Re-fetch at least one byte when rewind is
    disabled; normal callers retain the usual aligned overlap protection.
    """
    if total and (expected is None or total >= expected):
        safe = aligned_resume_offset(total, max(1, rewind))
        try:
            with open(dest, "r+b") as fh:
                fh.truncate(safe)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise TransferError(f"cannot rewind failed transfer tail: {type(exc).__name__}: {exc}") from None
        return safe
    return total


def _total_from_content_range(value: str) -> Optional[int]:
    """`bytes 0-1023/261259` -> 261259. Handles `*` (unknown) and junk."""
    if "/" not in value:
        return None
    tail = value.rsplit("/", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None


def _start_from_content_range(value: str) -> Optional[int]:
    """`bytes 1024-2047/261259` -> 1024. `None` when unparseable.

    Companion to `_total_from_content_range`, which parsed only the total — so a
    `206` whose body began somewhere other than the requested offset was appended
    at whatever offset the server chose, and nothing noticed.
    """
    m = re.match(r"\s*bytes\s+(\d+)-\d+/\s*(?:\d+|\*)\s*$", value or "")
    return int(m.group(1)) if m else None
