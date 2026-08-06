"""Watch the rclone VFS write-back cache so a long transfer never deadlocks.

Why this module exists (measured on the production box):

Downloads land on ``/opt/archives``, which is an **rclone FUSE mount** running::

    --vfs-cache-mode full
    --vfs-cache-max-size 100G
    --cache-dir /opt/local_cache_crypt/rclone_vfs
    --vfs-cache-max-age 24h

Every byte we write goes to that *local* cache directory first and is uploaded
to the remote afterwards. One job is 63 parts x 10 GB = **630 GB pushed through
a 100 GB cache**. Downloading faster than rclone uploads is therefore not a
throughput question, it is a *liveness* question: when the cache reaches the
``--vfs-cache-max-size`` cap, writes to the mount **block indefinitely**. There
is no error, no ``ENOSPC``, no retry to make — a multi-day transfer simply
wedges, and the download cookie idles out while we wait on a mutex we cannot
see. The cache disk (``/opt/local_cache_crypt``, 300 G total / 292 G free) has
room to spare, so a plain ``statvfs`` on the filesystem tells us nothing; the
wall we hit is rclone's own accounting of its cache directory.

So we measure the thing rclone measures: the byte total under ``--cache-dir``.

=========  ==========================  ==========================================
State      Trigger                     What the engine should do
=========  ==========================  ==========================================
OK         ratio < ``WARN_RATIO``      keep downloading
WARN       ratio >= ``WARN_RATIO``     keep downloading, log it, tighten polling
PAUSE      ratio >= ``PAUSE_RATIO``    stop issuing writes, let rclone drain
UNKNOWN    cache dir absent            keep downloading — see safety rule below
=========  ==========================  ==========================================

**Safety rule:** ``UNKNOWN`` never pauses. A missing or unmeasurable cache
directory means we are probably not even on an rclone mount (a laptop test, a
plain-disk staging box, a renamed cache dir). Blocking a transfer because we
failed to find a directory would be a self-inflicted outage; being wrong in the
other direction only costs us the protection we never had.

**Hysteresis:** ``next_state`` keeps a PAUSE latched until the cache drains to
``RESUME_RATIO``. Resuming the instant we drop below ``PAUSE_RATIO`` would flap
pause/resume on every 8 MiB chunk and give rclone no room to catch up.

Cost discipline: this module does cheap syscalls only. It never sleeps, never
writes, never touches the network — the engine owns the waiting. ``read_cache_status``
is cheap enough to call between 8 MiB chunks *only* when the caller passes
``measured=`` (a byte count it already has). The real directory walk should be
throttled by the caller to roughly **every 15 s** and its result reused for the
chunks in between; the walk is additionally bounded by ``max_entries`` so it can
never turn into a million-inode stat storm on a 2-vCPU box.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import List, Optional

__all__ = ["CacheState", "CacheStatus", "measure_cache_bytes",
           "read_cache_status", "next_state", "DEFAULT_MAX_BYTES",
           "WARN_RATIO", "PAUSE_RATIO", "RESUME_RATIO", "DEFAULT_MAX_ENTRIES"]

#: Mirrors ``--vfs-cache-max-size 100G`` on the production mount.
DEFAULT_MAX_BYTES = 100 * 1024 ** 3

WARN_RATIO = 0.70
PAUSE_RATIO = 0.85
RESUME_RATIO = 0.60      # hysteresis floor: must drain to here before resuming

#: Upper bound on directory entries visited by one walk. rclone's VFS cache
#: mirrors the remote's tree, so it is shallow and wide for our workload
#: (~63 files), but a shared cache dir could hold far more. Bounding the walk
#: keeps the worst case predictable instead of unbounded.
DEFAULT_MAX_ENTRIES = 200_000


class CacheState(str, enum.Enum):
    """String values are normative — they are logged and persisted."""

    OK = "OK"
    WARN = "WARN"
    PAUSE = "PAUSE"
    UNKNOWN = "UNKNOWN"       # cache dir absent / not measurable


@dataclass(frozen=True)
class CacheStatus:
    """One observation of the write-back cache. Immutable by design: callers
    latch these into state machines and compare them across polls."""

    state: CacheState
    bytes_used: int
    max_bytes: int
    detail: str

    @property
    def fill_ratio(self) -> float:
        """Used/allowed, clamped at 0.0 when there is no meaningful cap.

        ``max_bytes <= 0`` means "no cap configured" (rclone run without
        ``--vfs-cache-max-size``). That must read as 'nothing to fill', not as a
        ZeroDivisionError in the middle of a multi-day transfer.
        """
        if self.max_bytes <= 0:
            return 0.0
        return self.bytes_used / self.max_bytes

    @property
    def should_pause(self) -> bool:
        """True only for PAUSE. UNKNOWN deliberately returns False."""
        return self.state is CacheState.PAUSE


def measure_cache_bytes(cache_dir: str, *,
                        max_entries: int = DEFAULT_MAX_ENTRIES) -> Optional[int]:
    """Total bytes of regular files under ``cache_dir``.

    Recursive ``os.scandir`` — the dirent usually already carries the size, so
    this is one pass rather than a ``stat()`` per file.

    Returns ``None`` if ``cache_dir`` does not exist (the caller turns that into
    :data:`CacheState.UNKNOWN`, which never pauses).

    **Bounded on purpose.** After visiting ``max_entries`` directory entries the
    walk stops and reports the total accumulated so far. An under-report can only
    make us *less* likely to pause, which is the safe direction; walking millions
    of inodes on a 2-vCPU box while a download is in flight is not.

    Permission errors and races (a file rclone just evicted) on individual
    entries are skipped, never raised: a single unreadable inode must not take
    down the transfer's safety valve.
    """
    if not os.path.isdir(cache_dir):
        return None

    total = 0
    visited = 0
    stack: List[str] = [cache_dir]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if visited >= max_entries:
                        return total          # bounded: report what we got
                    visited += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue              # unreadable or vanished entry
        except OSError:
            continue                          # unreadable or vanished directory

    return total


def read_cache_status(cache_dir: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                      warn_ratio: float = WARN_RATIO,
                      pause_ratio: float = PAUSE_RATIO,
                      measured: Optional[int] = None) -> CacheStatus:
    """Classify the cache against the warn/pause thresholds.

    Pass ``measured`` to inject a byte count you already have — that is how the
    engine calls this between 8 MiB chunks without re-walking the tree, and how
    the tests drive exact boundary values. Omit it and this performs one bounded
    walk, which the caller should throttle to roughly every 15 s.

    A missing cache directory yields :data:`CacheState.UNKNOWN`, and UNKNOWN
    never pauses (see the module docstring's safety rule).
    """
    used = measure_cache_bytes(cache_dir) if measured is None else measured

    if used is None:
        return CacheStatus(
            CacheState.UNKNOWN, 0, max_bytes,
            f"cache dir not measurable: {cache_dir!r} — not pausing (safety rule)",
        )

    status_args = (used, max_bytes)
    ratio = (used / max_bytes) if max_bytes > 0 else 0.0
    human = f"{used / 1024 ** 3:.1f}G/{max_bytes / 1024 ** 3:.1f}G ({ratio:.1%})"

    if ratio >= pause_ratio:
        return CacheStatus(CacheState.PAUSE, *status_args,
                           f"cache {human} >= pause {pause_ratio:.0%} — "
                           f"stop writing, let rclone drain or writes will block")
    if ratio >= warn_ratio:
        return CacheStatus(CacheState.WARN, *status_args,
                           f"cache {human} >= warn {warn_ratio:.0%} — "
                           f"upload is falling behind the download")
    return CacheStatus(CacheState.OK, *status_args,
                       f"cache {human} — headroom ok")


def next_state(previous: CacheState, status: CacheStatus, *,
               resume_ratio: float = RESUME_RATIO) -> CacheState:
    """Apply hysteresis to a raw observation.

    Once we are in PAUSE we stay in PAUSE until the cache has drained to
    ``resume_ratio``, even though the raw state already reads WARN or OK. Without
    this latch the engine would resume the moment it dipped below
    ``PAUSE_RATIO``, immediately refill, and flap on every chunk — giving rclone
    no contiguous window to upload.

    UNKNOWN always wins: if we can no longer measure the cache we release the
    pause rather than block a transfer on a measurement we do not have.
    """
    if status.state is CacheState.UNKNOWN:
        return CacheState.UNKNOWN
    if previous is CacheState.PAUSE and status.fill_ratio > resume_ratio:
        return CacheState.PAUSE
    return status.state
