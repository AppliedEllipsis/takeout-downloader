#!/usr/bin/env python3
"""
Internal parallel HTTP downloader for Google Takeout archives.
==============================================================

Replaces the aria2c subprocess with an in-process ``requests``-based
downloader. The motivation: aria2c works fine, but progress feedback meant
scraping another process's human-readable stdout, which is fragile and —
worse — silently disabled whenever stdout was not a TTY (the common
SSH/tmux/Docker case), leaving the user with no feedback at all.

Owning the byte loop ourselves means:

  * Progress is exact (we count the bytes), so the live grid can never lie
    or sit stuck at "queued".
  * Auth-challenge HTML is detected on the *first chunk*, before a single
    byte is written to the archive file — so a sign-in page can never
    corrupt an output file (the old "every .zip is 1.2 MB of HTML" bug is
    structurally impossible here).
  * Resume is plain HTTP ``Range:`` from the on-disk size — no second
    process, no control files.
  * No aria2c binary dependency.

Design
------
``InternalDownloader.download(parts, on_progress=...)`` spawns a bounded
thread pool (one worker per concurrent slot). Each worker pulls a part off
a queue, streams it to disk with ``Range`` resume, and updates its own
slot in a shared progress table. The calling thread is handed a snapshot
of that table via the ``on_progress`` callback at a fixed cadence, so the
renderer is fully decoupled from the download loop and is trivially
testable without a TTY.

Thread-safety: each worker writes only its own ``PartProgress`` object;
the snapshot is taken under a lock. Counters are plain ints/floats updated
by a single owning thread, so no per-field locking is needed.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests


# Bytes per read from the socket. 1 MiB balances syscall overhead against
# progress granularity (a 1 MiB chunk on a 50 MiB/s link updates ~50x/sec,
# plenty smooth; the renderer throttles to its own cadence anyway).
DEFAULT_CHUNK_SIZE = 1024 * 1024

# (connect, read) timeouts. The read timeout is generous because Google can
# stall mid-stream under load; the worker's retry loop handles a genuine
# hang by reconnecting with Range from where it stopped.
DEFAULT_TIMEOUT = (10, 60)

ZIP_MAGIC = b"PK\x03\x04"
ZIP_EOCD = b"PK\x05\x06"


def _looks_like_html_bytes(body: bytes) -> bool:
    """True if ``body`` starts like an HTML document (a Google sign-in
    challenge page). Mirrors takeout_cli._looks_like_html_bytes so the
    downloader stays import-independent of the CLI module."""
    if not body:
        return False
    head = body[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head:
        return False
    if head.startswith(b"<!"):
        return b"html" in head[:32].lower() or b"doctype" in head[:32].lower()
    return head.lower().startswith(b"<html")


class AuthChallenge(Exception):
    """Raised inside a worker when Google serves a sign-in page instead of
    the archive (expired cookie or anti-bot challenge)."""

    def __init__(self, message: str, body: bytes = b""):
        super().__init__(message)
        self.body = body


@dataclass
class PartProgress:
    """Live state for one part. Workers mutate their own instance; the
    renderer reads snapshots. ``status`` drives the row glyph."""
    num: int
    filename: str
    url: str
    total: int = 0           # total bytes (0 = unknown until headers arrive)
    done: int = 0            # bytes on disk so far (incl. resumed prefix)
    speed_bps: float = 0.0   # smoothed instantaneous speed
    status: str = "queued"   # queued | active | done | error | auth
    error: str = ""

    @property
    def pct(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, int(self.done * 100 / self.total)))


@dataclass
class DownloadResult:
    """Outcome of a ``download()`` call."""
    completed: list[int] = field(default_factory=list)   # part nums done
    failed: list[int] = field(default_factory=list)       # part nums failed
    auth_failed: bool = False                             # cookie died
    auth_body: bytes = b""                               # saved challenge HTML
    auth_url: str = ""                                   # part that triggered it

    @property
    def ok(self) -> bool:
        return not self.failed and not self.auth_failed


class InternalDownloader:
    """In-process parallel downloader with Range resume and live progress.

    Parameters
    ----------
    cookie, headers : the captured Takeout auth (headers excludes Cookie;
        it is added per-request).
    output_dir : where archives land.
    parallel : max concurrent downloads.
    chunk_size, timeout : socket tunables (see module constants).
    """

    def __init__(self, cookie: str, headers: dict, output_dir: Path,
                 parallel: int = 5,
                 chunk_size: int = DEFAULT_CHUNK_SIZE,
                 timeout: tuple = DEFAULT_TIMEOUT,
                 max_tries: int = 5,
                 retry_wait: float = 5.0,
                 logger=None):
        self.cookie = cookie
        self.headers = {k: v for k, v in (headers or {}).items()
                        if k.lower() != "cookie"}
        self.output_dir = Path(output_dir)
        self.parallel = max(1, parallel)
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.max_tries = max_tries
        self.retry_wait = retry_wait
        self.log = logger

        self._progress: dict[int, PartProgress] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()          # cooperative cancel
        self._auth = threading.Event()           # an auth challenge happened
        self._auth_info: tuple[str, bytes] = ("", b"")

    # -- logging helpers (no-op if no logger wired) ------------------------
    def _debug(self, msg: str) -> None:
        if self.log:
            self.log.debug(msg)

    def _warn(self, msg: str) -> None:
        if self.log:
            self.log.warning(msg)

    # -- public API --------------------------------------------------------
    def snapshot(self) -> list[PartProgress]:
        """Thread-safe copy of every part's current progress, ordered the
        same as the parts were submitted."""
        with self._lock:
            return [PartProgress(**vars(p)) for p in self._progress.values()]

    def request_stop(self) -> None:
        """Ask all workers to finish their current chunk and exit (e.g. on
        Ctrl-C). Partial files are left on disk for resume."""
        self._stop.set()

    def download(self, parts: list[dict],
                 on_progress: Optional[Callable[[list[PartProgress]], None]] = None,
                 progress_interval: float = 0.25) -> DownloadResult:
        """Download every part in ``parts`` (each ``{num,url,filename,size,have}``)
        that isn't already ``have``. Calls ``on_progress(snapshot)`` every
        ``progress_interval`` seconds on the calling thread until done.

        Returns a :class:`DownloadResult`. On the first auth challenge, all
        workers are stopped and ``auth_failed`` is set so the caller can
        re-prompt for a fresh cookie and resume.
        """
        self._stop.clear()
        self._auth.clear()
        self._auth_info = ("", b"")

        todo = [p for p in parts if not p.get("have")]
        with self._lock:
            self._progress = {}
            for p in parts:
                self._progress[p["num"]] = PartProgress(
                    num=p["num"], filename=p["filename"], url=p["url"],
                    total=p.get("size", 0) or 0,
                    done=(p.get("size", 0) or 0) if p.get("have") else 0,
                    status="done" if p.get("have") else "queued",
                )

        result = DownloadResult()
        result.completed = [p["num"] for p in parts if p.get("have")]

        if not todo:
            if on_progress:
                on_progress(self.snapshot())
            return result

        work: "queue.Queue[dict]" = queue.Queue()
        for p in todo:
            work.put(p)

        n_workers = min(self.parallel, len(todo))
        threads: list[threading.Thread] = []
        for _ in range(n_workers):
            t = threading.Thread(target=self._worker, args=(work, result),
                                 daemon=True)
            t.start()
            threads.append(t)

        # Render loop on the calling thread: snapshot + callback at a fixed
        # cadence until all workers exit. This is what makes feedback
        # independent of any subprocess stdout — we drive it ourselves.
        try:
            while any(t.is_alive() for t in threads):
                if on_progress:
                    on_progress(self.snapshot())
                time.sleep(progress_interval)
        except KeyboardInterrupt:
            self.request_stop()
            raise
        finally:
            for t in threads:
                t.join(timeout=30)

        # Final snapshot so the renderer shows the finished state.
        if on_progress:
            on_progress(self.snapshot())

        if self._auth.is_set():
            result.auth_failed = True
            result.auth_url, result.auth_body = self._auth_info

        return result

    # -- worker ------------------------------------------------------------
    def _worker(self, work: "queue.Queue[dict]", result: DownloadResult) -> None:
        while not self._stop.is_set():
            try:
                part = work.get_nowait()
            except queue.Empty:
                return
            try:
                self._download_one(part)
                with self._lock:
                    result.completed.append(part["num"])
            except AuthChallenge as e:
                # Cookie is dead for every part, not just this one. Flag it,
                # stash the challenge body, and stop the whole pool.
                if not self._auth.is_set():
                    self._auth_info = (part["url"], e.body)
                    self._auth.set()
                self._stop.set()
                self._set(part["num"], status="auth", error=str(e))
            except Exception as e:  # noqa: BLE001 — record and move on
                self._warn(f"part {part['num']:03d} failed: {e!r}")
                self._set(part["num"], status="error", error=str(e))
                with self._lock:
                    result.failed.append(part["num"])
            finally:
                work.task_done()

    def _download_one(self, part: dict) -> None:
        """Download a single part with Range resume and live byte counting.

        Retries transient network errors up to ``max_tries``, reconnecting
        with a fresh Range from wherever the on-disk file stopped. An auth
        challenge is NOT retried — it raises immediately so the pool stops.
        """
        num = part["num"]
        dest = self.output_dir / part["filename"]
        dest.parent.mkdir(parents=True, exist_ok=True)

        self._set(num, status="active")
        last_err = ""

        for attempt in range(1, self.max_tries + 1):
            if self._stop.is_set():
                return
            existing = dest.stat().st_size if dest.exists() else 0
            # If we already have at least the known total, we're done.
            total_known = part.get("size", 0) or 0
            if total_known and existing >= total_known:
                self._set(num, done=existing, total=total_known, status="done")
                return

            try:
                self._stream_to_disk(num, dest, part["url"], existing, total_known)
                # Success: mark done using the final on-disk size.
                final = dest.stat().st_size if dest.exists() else 0
                self._set(num, done=final,
                          total=(self._progress[num].total or final),
                          status="done")
                return
            except AuthChallenge:
                raise  # never retry an auth failure
            except (requests.RequestException, OSError) as e:
                last_err = repr(e)
                self._debug(f"part {num:03d} attempt {attempt}/{self.max_tries} "
                            f"error: {last_err}")
                if attempt < self.max_tries and not self._stop.is_set():
                    # Back off, then resume from the new on-disk size.
                    self._set(num, speed_bps=0.0)
                    time.sleep(self.retry_wait)
                    continue
                raise

        raise RuntimeError(last_err or "exhausted retries")

    def _stream_to_disk(self, num: int, dest: Path, url: str,
                        existing: int, total_known: int) -> None:
        """One GET attempt. Streams to ``dest`` resuming from ``existing``
        bytes. Raises AuthChallenge if the first bytes look like HTML."""
        headers = dict(self.headers)
        headers["Cookie"] = self.cookie
        resume = existing > 0
        if resume:
            headers["Range"] = f"bytes={existing}-"

        with requests.get(url, headers=headers, stream=True,
                          timeout=self.timeout, allow_redirects=True) as resp:
            final_host = resp.url.split("/")[2] if "://" in resp.url else ""
            ctype = resp.headers.get("content-type", "").lower()

            # Auth challenge: redirect to sign-in or an HTML body.
            if final_host.endswith("accounts.google.com"):
                raise AuthChallenge(f"redirected to {final_host}")
            if "text/html" in ctype:
                body = self._peek(resp)
                raise AuthChallenge(f"server returned HTML (ct={ctype[:40]})",
                                    body=body)
            if resp.status_code in (401, 403):
                raise AuthChallenge(f"HTTP {resp.status_code}")
            resp.raise_for_status()

            # Decide append vs overwrite. If we asked for a Range but the
            # server ignored it (200 instead of 206), it's sending the whole
            # file from 0 — we must truncate and restart, not append.
            mode = "wb"
            start = 0
            if resume and resp.status_code == 206:
                mode = "ab"
                start = existing
            elif resume and resp.status_code == 200:
                self._debug(f"part {num:03d}: server ignored Range; "
                            f"restarting from 0")

            # Total size: from Content-Range (resumed) or Content-Length.
            total = total_known
            cr = resp.headers.get("content-range", "")
            if cr and "/" in cr:
                try:
                    total = int(cr.rsplit("/", 1)[1])
                except ValueError:
                    pass
            elif resp.status_code == 200:
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit():
                    total = int(cl)
            self._set(num, total=total or total_known)

            done = start
            first = True
            # Speed: exponential moving average over wall-clock deltas.
            ema = 0.0
            t_prev = time.monotonic()
            b_prev = done

            with dest.open(mode) as fh:
                for chunk in resp.iter_content(chunk_size=self.chunk_size):
                    if self._stop.is_set():
                        # Flush what we have; resume picks up from here.
                        fh.flush()
                        return
                    if not chunk:
                        continue
                    # Guard the very first bytes: an HTML page slipping past
                    # the content-type check (some challenges send
                    # octet-stream) must never be written to the archive.
                    if first:
                        first = False
                        if start == 0 and _looks_like_html_bytes(chunk):
                            raise AuthChallenge(
                                "first bytes look like an HTML sign-in page",
                                body=chunk[:4096])
                    fh.write(chunk)
                    done += len(chunk)

                    # Update the byte count every chunk so the grid always
                    # reflects real progress, even for parts that finish
                    # inside one speed-EMA window. Speed is smoothed on its
                    # own >=0.5s wall-clock window so the rate isn't noisy.
                    now = time.monotonic()
                    dt = now - t_prev
                    if dt >= 0.5:
                        inst = (done - b_prev) / dt
                        ema = inst if ema == 0.0 else (0.6 * ema + 0.4 * inst)
                        t_prev, b_prev = now, done
                        self._set(num, done=done, speed_bps=ema)
                    else:
                        self._set(num, done=done)

            self._set(num, done=done, speed_bps=0.0)

    @staticmethod
    def _peek(resp, max_bytes: int = 4096) -> bytes:
        body = b""
        try:
            for chunk in resp.iter_content(chunk_size=2048):
                body += chunk
                if len(body) >= max_bytes:
                    break
        except requests.RequestException:
            pass
        return body

    # -- progress table ----------------------------------------------------
    def _set(self, num: int, **fields) -> None:
        with self._lock:
            p = self._progress.get(num)
            if p is None:
                return
            for k, v in fields.items():
                setattr(p, k, v)
