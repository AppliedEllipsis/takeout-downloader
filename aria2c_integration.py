"""
Google Takeout aria2c RPC Integration
=====================================

Provides multi-threaded, resumable downloads of Google Takeout archives
via aria2c's JSON-RPC interface.  aria2c handles connection pooling,
segmented downloads (split), automatic retries, and resume out of the box
— far more efficiently than a pure-Python threaded downloader.

Quick start::

    from aria2c_integration import takeout_download_via_aria2c

    result = takeout_download_via_aria2c(
        cookie="SID=...; HSID=...; ...",
        base_url="https://takeout.google.com/.../takeout-20251207T071725Z-3-",
        query_string="authuser=0",
        extension=".zip",
        file_count=100,
        output_dir="/downloads",
        parallel=10,
    )

Architecture
------------
- ``detect_aria2c()`` — check whether the ``aria2c`` binary is on PATH.
- ``start_aria2c()`` — launch aria2c as a subprocess with XML-RPC enabled.
- ``Aria2cManager`` — thin wrapper around aria2c's JSON-RPC endpoint
  (uses ``requests`` for plain HTTP POST — no xmltodict or xmlrpc.client).
- ``takeout_download_via_aria2c()`` — convenience function that wires
  everything together: start aria2c (or attach to a running instance),
  enqueue a batch of takeout files, wait for completion, return stats.

Requirements: ``requests``, ``aria2c`` binary on PATH.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests as _requests

from takeout import extract_url_parts, validate_output_dir

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_RPC_PORT = 6800
DEFAULT_RPC_URL = f"http://localhost:{DEFAULT_RPC_PORT}/jsonrpc"
_POLL_INTERVAL = 2.0  # seconds between status polls
_RPC_TIMEOUT = 10  # seconds for individual RPC calls

# ---------------------------------------------------------------------------
# Binary detection
# ---------------------------------------------------------------------------

def detect_aria2c() -> bool:
    """Return ``True`` if the ``aria2c`` binary is available on PATH.

    Checks both ``shutil.which`` and a trial ``aria2c --version`` execution.
    """
    if shutil.which("aria2c") is None:
        return False
    try:
        result = subprocess.run(
            ["aria2c", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Start aria2c daemon
# ---------------------------------------------------------------------------

def start_aria2c(
    rpc_port: int = DEFAULT_RPC_PORT,
    secret: Optional[str] = None,
    download_dir: str = "/downloads",
) -> subprocess.Popen:
    """Start aria2c as a background subprocess with JSON-RPC enabled.

    Parameters
    ----------
    rpc_port:
        Port for the JSON-RPC listener.
    secret:
        RPC secret token.  If ``None`` a random hex token is generated via
        ``os.urandom``.
    download_dir:
        Default directory for downloaded files.

    Returns
    -------
    subprocess.Popen
        The process object for the aria2c daemon.

    Raises
    ------
    FileNotFoundError
        If ``aria2c`` is not found on PATH.
    RuntimeError
        If the process exits immediately after starting.
    """
    if not detect_aria2c():
        raise FileNotFoundError(
            "aria2c binary not found on PATH.  Install it with: "
            "apt install aria2  /  brew install aria2  /  choco install aria2"
        )

    if secret is None:
        secret = os.urandom(16).hex()

    # Ensure download directory exists so aria2c doesn't refuse to start.
    os.makedirs(download_dir, exist_ok=True)

    cmd = [
        "aria2c",
        "--enable-rpc",
        f"--rpc-listen-port={rpc_port}",
        f"--rpc-secret={secret}",
        f"--dir={download_dir}",
        "--continue=true",
        "--max-concurrent-downloads=10",
        "--split=16",
        "--max-connection-per-server=16",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--log-level=notice",
        "--disable-ipv6=true",
    ]

    logger.info("Starting aria2c: %s", " ".join(cmd))

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Give aria2c a moment to start up, then verify it's still alive.
    try:
        ret = process.wait(timeout=0.3)
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        raise RuntimeError(
            f"aria2c exited immediately with code {ret}: {stderr}"
        )
    except subprocess.TimeoutExpired:
        # Good — it's still running
        pass

    # Brief wait for RPC socket to be ready
    rpc_url = f"http://localhost:{rpc_port}/jsonrpc"
    _wait_for_rpc(rpc_url, timeout=5)

    logger.info("aria2c started (pid=%d, rpc=%s)", process.pid, rpc_url)
    return process


def _wait_for_rpc(rpc_url: str, timeout: float = 5.0) -> None:
    """Block until the aria2c RPC endpoint responds, or raise RuntimeError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = _requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": "probe", "method": "aria2.getVersion", "params": []},
                timeout=2,
            )
            if resp.status_code == 200:
                return
        except _requests.ConnectionError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"aria2c RPC endpoint {rpc_url} not reachable within {timeout}s")


# ---------------------------------------------------------------------------
# Aria2cManager — JSON-RPC client
# ---------------------------------------------------------------------------

class Aria2cManager:
    """Thin wrapper around aria2c's JSON-RPC interface.

    All calls are plain HTTP POST with a JSON body — no extra libraries
    required beyond ``requests``.
    """

    def __init__(
        self,
        rpc_url: str = DEFAULT_RPC_URL,
        rpc_secret: Optional[str] = None,
    ):
        self.rpc_url = rpc_url
        self.rpc_secret = rpc_secret

    # -- internal helpers ---------------------------------------------------

    def _rpc(self, method: str, params: Optional[list] = None) -> dict:
        """Send a JSON-RPC request and return the ``result`` field.

        Raises ``ConnectionError`` if the RPC endpoint is unreachable.
        Raises ``RuntimeError`` if aria2c returns an error.
        """
        if params is None:
            params = []

        # Prepend the secret token (aria2c convention: first param = "token:SECRET")
        if self.rpc_secret:
            params = [f"token:{self.rpc_secret}"] + params

        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": method,
            "params": params,
        }

        try:
            resp = _requests.post(
                self.rpc_url,
                json=payload,
                timeout=_RPC_TIMEOUT,
            )
        except _requests.ConnectionError as exc:
            raise ConnectionError(
                f"Cannot reach aria2c RPC at {self.rpc_url}. "
                "Is aria2c running?"
            ) from exc

        body = resp.json()

        if "error" in body:
            raise RuntimeError(
                f"aria2c RPC error on {method}: {body['error']}"
            )

        return body.get("result", {})

    # -- public API ---------------------------------------------------------

    def is_available(self) -> bool:
        """Check if aria2c RPC is reachable (calls ``aria2.getVersion``)."""
        try:
            self._rpc("aria2.getVersion")
            return True
        except (ConnectionError, RuntimeError, _requests.RequestException):
            return False

    def add_download(
        self,
        url: str,
        filename: str,
        cookie: str,
        options: Optional[dict] = None,
    ) -> str:
        """Add a single URI to aria2c and return the download GID.

        Parameters
        ----------
        url:
            Full download URL.
        filename:
            Output filename (passed as ``out`` option).
        cookie:
            Cookie string (e.g. ``"SID=…; HSID=…"``).
        options:
            Additional aria2c options dict (merged, caller's keys win).

        Returns
        -------
        str
            The GID assigned by aria2c.
        """
        opts: Dict[str, object] = {
            "header": [f"Cookie: {cookie}"],
            "out": filename,
            "continue": True,
            "max-tries": 5,
            "retry-wait": 10,
        }
        if options:
            opts.update(options)

        gid = self._rpc("aria2.addUri", [[url], opts])
        logger.debug("add_download: %s → GID %s", filename, gid)
        return gid

    def get_status(self, gid: str) -> dict:
        """Query detailed status for a download (``aria2.tellStatus``).

        Returns the full status dict including keys like ``status``,
        ``totalLength``, ``completedLength``, ``downloadSpeed``, etc.
        """
        return self._rpc("aria2.tellStatus", [gid])

    def get_global_stat(self) -> dict:
        """Return global download statistics (``aria2.getGlobalStat``)."""
        return self._rpc("aria2.getGlobalStat")

    def remove_download(self, gid: str) -> bool:
        """Remove a completed / failed download result.

        Returns ``True`` on success, ``False`` on error.
        """
        try:
            self._rpc("aria2.removeDownloadResult", [gid])
            return True
        except RuntimeError:
            return False

    def purge_downloads(self) -> bool:
        """Purge all completed / errored download results.

        Returns ``True`` on success, ``False`` on error.
        """
        try:
            self._rpc("aria2.purgeDownloadResult")
            return True
        except RuntimeError:
            return False

    def shutdown(self) -> bool:
        """Gracefully shut down the aria2c daemon.

        Returns ``True`` on success, ``False`` on error.
        """
        try:
            self._rpc("aria2.shutdown")
            return True
        except RuntimeError:
            return False

    def add_takeout_batch(
        self,
        base_url: str,
        query_string: str,
        extension: str,
        cookie: str,
        file_range: range,
        output_dir: Optional[str] = None,
    ) -> List[str]:
        """Enqueue a batch of Google Takeout files.

        Constructs URLs of the form::

            {base_url}{NNN}{extension}?{query_string}

        for each number in *file_range* (e.g. ``range(1, 101)``).

        Parameters
        ----------
        base_url:
            URL stem up to and including the batch number and dash,
            e.g. ``"https://…/takeout-20251207T071725Z-3-"``.
        query_string:
            Appended after ``?`` on each URL.
        extension:
            File extension including the dot, e.g. ``".zip"``.
        cookie:
            Cookie string forwarded in every request header.
        file_range:
            A ``range`` producing the 3-digit file numbers.
        output_dir:
            If given, override the per-download ``dir`` option.

        Returns
        -------
        list[str]
            GIDs for all enqueued downloads.
        """
        gids: List[str] = []
        for num in file_range:
            url = f"{base_url}{num:03d}{extension}"
            if query_string:
                url += f"?{query_string}"

            # Derive filename from the trailing path segment
            filename = url.split("/")[-1].split("?")[0]

            opts: Dict[str, object] = {}
            if output_dir:
                opts["dir"] = output_dir

            gid = self.add_download(url, filename, cookie, options=opts)
            gids.append(gid)

        logger.info("Enqueued %d takeout files (GIDs: %s…%s)", len(gids), gids[0] if gids else "", gids[-1] if gids else "")
        return gids

    def wait_for_completion(
        self,
        gids: List[str],
        callback: Optional[Callable[[Dict[str, dict]], None]] = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> Dict[str, str]:
        """Block until all downloads reach a terminal state.

        Terminal states: ``complete``, ``error``, ``removed``.

        Parameters
        ----------
        gids:
            List of GIDs to monitor.
        callback:
            Optional function called after each poll with a dict
            ``{gid: status_dict}`` for all *active* (non-terminal) downloads.
            Useful for progress display.
        poll_interval:
            Seconds between polls.

        Returns
        -------
        dict[str, str]
            ``{gid: final_status}`` for every input GID.
        """
        pending = set(gids)
        results: Dict[str, str] = {}

        while pending:
            active_status: Dict[str, dict] = {}
            completed_this_round: set[str] = set()

            for gid in list(pending):
                try:
                    info = self.get_status(gid)
                except RuntimeError:
                    # GID might have been purged — treat as removed
                    results[gid] = "removed"
                    completed_this_round.add(gid)
                    continue

                status = info.get("status", "unknown")
                if status in ("complete", "error", "removed"):
                    results[gid] = status
                    completed_this_round.add(gid)
                else:
                    active_status[gid] = info

            pending -= completed_this_round

            if callback and active_status:
                callback(active_status)

            if pending:
                time.sleep(poll_interval)

        return results


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def takeout_download_via_aria2c(
    cookie: str,
    base_url: str,
    query_string: str,
    extension: str,
    file_count: int,
    output_dir: str = "/downloads",
    parallel: int = 10,
    rpc_port: int = DEFAULT_RPC_PORT,
    rpc_secret: Optional[str] = None,
    auto_shutdown: bool = True,
) -> dict:
    """High-level helper: start aria2c, enqueue a takeout batch, wait, return stats.

    If aria2c is already running on *rpc_port* the function attaches to the
    existing instance instead of launching a new one.

    Parameters
    ----------
    cookie:
        Full cookie string (``"SID=…; HSID=…; …"``).
    base_url:
        URL stem up to and including batch number and dash.
    query_string:
        Query portion appended after ``?``.
    extension:
        File extension including dot (``".zip"``).
    file_count:
        Number of takeout files (001..NNN).
    output_dir:
        Download destination directory.
    parallel:
        Max concurrent downloads (tells aria2c via ``--max-concurrent-downloads``).
        Only affects the startup flags if we launch aria2c ourselves; if
        attaching to a running daemon this is a no-op for concurrency.
    rpc_port:
        Port for the JSON-RPC listener.
    rpc_secret:
        Secret token; auto-generated if ``None``.
    auto_shutdown:
        If ``True`` and we started aria2c ourselves, shut it down when done.

    Returns
    -------
    dict
        ``{"total": N, "complete": N, "error": N, "removed": N, "results": {gid: status}}``
    """
    # Validate output dir early
    resolved_dir = str(validate_output_dir(output_dir))

    manager = Aria2cManager(
        rpc_url=f"http://localhost:{rpc_port}/jsonrpc",
        rpc_secret=rpc_secret,
    )

    aria2c_proc: Optional[subprocess.Popen] = None
    started_ourselves = False

    # --- Attach to existing or start fresh ---
    if manager.is_available():
        logger.info("Attaching to existing aria2c on port %d", rpc_port)
    else:
        logger.info("No aria2c on port %d — starting one", rpc_port)
        aria2c_proc = start_aria2c(
            rpc_port=rpc_port,
            secret=rpc_secret,
            download_dir=resolved_dir,
        )
        started_ourselves = True
        # Re-create manager with the generated secret
        if rpc_secret is None:
            rpc_secret = os.urandom(16).hex()
            # start_aria2c already generated its own; we need to read it back.
            # For simplicity, re-instantiate manager after confirming RPC is up
            # by calling is_available below.
            manager = Aria2cManager(
                rpc_url=f"http://localhost:{rpc_port}/jsonrpc",
                rpc_secret=rpc_secret,
            )

        # Verify RPC reachable after start
        if not manager.is_available():
            raise RuntimeError("aria2c started but RPC is not reachable")

    # If we started aria2c ourselves we still don't know the secret that
    # start_aria2c generated internally.  Re-derive: start_aria2c generates
    # os.urandom(16).hex() when secret=None, but we can't recover it.
    # Solution: always pass an explicit secret when we start ourselves.
    if started_ourselves and rpc_secret is None:
        # Generate one and restart — this is the cleanest path.
        rpc_secret = os.urandom(16).hex()
        if aria2c_proc is not None:
            aria2c_proc.terminate()
            try:
                aria2c_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                aria2c_proc.kill()
        aria2c_proc = start_aria2c(
            rpc_port=rpc_port,
            secret=rpc_secret,
            download_dir=resolved_dir,
        )
        manager = Aria2cManager(
            rpc_url=f"http://localhost:{rpc_port}/jsonrpc",
            rpc_secret=rpc_secret,
        )

    # --- Enqueue batch ---
    file_range = range(1, file_count + 1)
    gids = manager.add_takeout_batch(
        base_url=base_url,
        query_string=query_string,
        extension=extension,
        cookie=cookie,
        file_range=file_range,
        output_dir=resolved_dir,
    )

    # --- Progress callback ---
    def _progress(active: Dict[str, dict]) -> None:
        completed = sum(1 for g in gids if g not in active)
        total_len = len(gids)
        speeds: List[float] = []
        for info in active.values():
            try:
                spd = int(info.get("downloadSpeed", "0"))
                speeds.append(spd)
            except (ValueError, TypeError):
                pass
        total_speed = sum(speeds)
        speed_str = f"{total_speed / (1024 * 1024):.1f} MB/s" if total_speed else "—"
        sys.stdout.write(
            f"\r  [{completed}/{total_len}] "
            f"Active: {len(active)}  Speed: {speed_str}   "
        )
        sys.stdout.flush()

    # --- Wait ---
    results = manager.wait_for_completion(gids, callback=_progress)
    sys.stdout.write("\n")

    # --- Tally ---
    complete = sum(1 for s in results.values() if s == "complete")
    errored = sum(1 for s in results.values() if s == "error")
    removed = sum(1 for s in results.values() if s == "removed")

    stats = {
        "total": len(gids),
        "complete": complete,
        "error": errored,
        "removed": removed,
        "results": results,
    }

    logger.info(
        "Batch complete: %d/%d OK, %d errors, %d removed",
        complete, len(gids), errored, removed,
    )

    # --- Optional shutdown ---
    if auto_shutdown and started_ourselves:
        logger.info("Shutting down aria2c (auto_shutdown=True)")
        try:
            manager.shutdown()
        except Exception:
            pass
        if aria2c_proc is not None:
            try:
                aria2c_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                aria2c_proc.kill()

    return stats


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

    print("aria2c detected:", detect_aria2c())

    mgr = Aria2cManager()
    print("aria2c RPC available:", mgr.is_available())
