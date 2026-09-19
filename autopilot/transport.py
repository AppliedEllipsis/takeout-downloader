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
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol

from .errors import AutopilotError, NeedsReauth

__all__ = [
    "CHUNK",
    "REWIND",
    "ACCOUNTS_HOST",
    "TransferResult",
    "TransferError",
    "RemoteChanged",
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
) -> TransferResult:
    """Download `url` to `dest`, resuming if `dest` already holds bytes.

    Returns a `TransferResult`; `status == "partial"` means the stream ended
    early and the bytes on disk are a valid resume point (a retry costs `Δ0`).

    Raises `NeedsReauth` when a sign-in/challenge page is returned, and
    `TransferError` when the jar was simply missing (a programming error, not a
    session problem), `RemoteChanged` when the remote object differs.
    """
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
    if start < have:
        # Re-fetch the overlap rather than append after a possibly torn tail.
        with open(dest, "r+b") as fh:
            fh.truncate(start)

    headers = {
        "Cookie": jar_header,
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        # a byte-range download must not be transparently content-encoded
        "Accept-Encoding": "identity",
    }
    if start > 0:
        headers["Range"] = f"bytes={start}-"
        if etag:
            # If the validator no longer matches, the server answers 200 with the
            # whole body instead of a 206 — the correct signal to start over.
            headers["If-Range"] = etag

    stream = await client.get(url, headers)
    try:
        status = stream.status
        hdrs = {k.lower(): v for k, v in stream.headers.items()}

        if status in (301, 302, 303, 307, 308):
            location = hdrs.get("location", "")
            if ACCOUNTS_HOST in location:
                raise NeedsReauth(location, "file host redirected to sign-in")
            raise TransferError(f"unexpected redirect -> {location[:120]}")

        if status in (401, 403):
            raise NeedsReauth(url, f"file host returned HTTP {status}")

        restarted = False
        if status == 206:
            cr = hdrs.get("content-range", "")
            total = _total_from_content_range(cr)
            if expected_size is not None and total is not None and total != expected_size:
                raise RemoteChanged(
                    f"remote total {total} != expected {expected_size} "
                    f"(Content-Range: {cr!r})"
                )
            mode, write_from = "ab", start
        elif status == 200:
            # Server ignored Range, or If-Range said the object changed. Either
            # way the body starts at byte 0: appending it would corrupt the file.
            mode, write_from = "wb", 0
            restarted = start > 0
        elif status == 416:
            raise RemoteChanged(
                f"range unsatisfiable for local size {have} (expected {expected_size})"
            )
        else:
            raise TransferError(f"unexpected HTTP {status} from the file host")

        written = 0
        with open(dest, mode) as fh:
            while True:
                block = await stream.read(chunk)
                if not block:
                    break
                fh.write(block)
                written += len(block)
                if on_progress:
                    on_progress(write_from + written, expected_size or 0)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        await stream.aclose()

    total_bytes = write_from + written
    complete = expected_size is None or total_bytes == expected_size
    return TransferResult(
        status="complete" if complete else "partial",
        bytes_written=written,
        total_bytes=total_bytes,
        resumed_from=write_from,
        http_status=status,
        etag=hdrs.get("etag"),
        restarted=restarted,
    )


def _total_from_content_range(value: str) -> Optional[int]:
    """`bytes 0-1023/261259` -> 261259. Handles `*` (unknown) and junk."""
    if "/" not in value:
        return None
    tail = value.rsplit("/", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None
