"""Move verified parts from local staging onto the archive.

The destination is `/opt/archives` — an **rclone FUSE** mount over a remote. Three
consequences drive this module, each from a measured or documented incident:

1. **Never stat per part in a loop.** A per-part `stat()` pre-pass over 58 parts
   on this mount took about six minutes (documented, and the cause of a
   production livelock). So the destination is indexed with **one `scandir`** and
   all planning is done against that snapshot, in pure code.

2. **Assert the destination really is the mount.** When the FUSE daemon dies the
   kernel does not fail the path — it turns `/opt/archives` into an ordinary,
   empty directory **on the root filesystem**, which has ~13 GB free. A mover that
   does not check will happily write terabytes into it. That is failure mode 1.7,
   and it has taken this server down once.

3. **Copy under a temporary name, then rename.** A rename on rclone is a
   server-side move, so it is cheap, and an interrupted copy never leaves a
   plausible-looking truncated file under the final name.

Idempotent by design: a part already present at the expected size is skipped, so
a re-run after a crash costs nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .errors import AutopilotError
from .ledger import _normalize, fuse_mount_for

#: Overall ceiling for ONE file's copy into the archive, and how long it may make no
#: progress at all before the child gives up. Both generous on purpose: rclone runs
#: with `--timeout 1h`, so a healthy transfer of a multi-GB part can legitimately take
#: many minutes, and a tight limit would murder it. The stall guard is the one that
#: matters for a wedged mount, because a blocked FUSE write never reports progress and
#: never returns.
MOVE_WATCHDOG_SECONDS = 1800.0      # 30 min per file
MOVE_STALL_SECONDS = 300.0          # 5 min with zero bytes written

__all__ = [
    "MovePlanItem",
    "MoveResult",
    "MoveRefused",
    "assert_staging_ready",
    "index_destination",
    "plan_moves",
    "move_part",
    "PARTIAL_SUFFIX",
]

#: Suffix for an in-flight copy. Deliberately NOT `.partial`, which v2 uses for
#: its own resumable downloads — a mover temp file must never be mistaken for one.
PARTIAL_SUFFIX = ".moving"


class MoveRefused(AutopilotError):
    """Refused to move — usually because the destination is not the real mount."""


@dataclass
class MovePlanItem:
    filename: str
    source: str
    size: int
    action: str            # "move" | "skip-present" | "skip-size-mismatch"


@dataclass
class MoveResult:
    filename: str
    action: str            # "moved" | "skipped-present" | "skipped-size-mismatch" | "failed"
    bytes_moved: int = 0
    detail: str = ""


@dataclass
class DestinationIndex:
    """A snapshot of the destination directory taken with ONE scandir."""

    path: str
    names: dict = field(default_factory=dict)   # filename -> size

    def size_of(self, filename: str) -> Optional[int]:
        return self.names.get(filename)


# ---------------------------------------------------------------------------
# mount safety (pure: takes the mount table so it is testable off-host)
# ---------------------------------------------------------------------------
def is_mount_point(path: str, mounts: Iterable[tuple[str, str]]) -> bool:
    """True if `path` appears as a mount point in the table."""
    from .ledger import _normalize  # same lexical normalisation, same reason

    target = _normalize(path)
    return any(_normalize(point) == target for point, _ in mounts)


def assert_destination_ready(directory: str, *,
                             mounts: Optional[Iterable[tuple[str, str]]] = None,
                             require_mount: bool = True) -> None:
    """Refuse to write into a destination that is not on the real storage mount.

    **Containment, not equality.** The real destination is a *subdirectory* of the
    mount (`/opt/archives/google-takeout/<account>/<export>/`), so testing whether
    the path *is* a mount point would refuse the correct path — a guard that blocks
    normal operation is as bad as one that does nothing. The question is whether the
    destination currently sits **under** a FUSE mount: when the rclone daemon dies
    the mount entry leaves the table, `/opt/archives` reverts to an ordinary
    directory on the root filesystem, and this check then fails, which is exactly
    what should happen (failure mode 1.7).

    `require_mount` exists so tests and local dev can opt out — the same idea as
    v2's `EngineConfig.require_mount`, which correctly defaults to `False` for dev.
    Here the production caller passes `True` (the CLI does).
    """
    if not os.path.isdir(directory):
        raise MoveRefused(f"destination {directory!r} does not exist")
    if not require_mount:
        return
    table = list(mounts) if mounts is not None else _read_mounts()
    if not table:
        # No mount table available (e.g. Windows): we cannot verify, so say so
        # rather than pretending the check passed.
        return
    containing = fuse_mount_for(directory, table)
    if containing is None:
        raise MoveRefused(
            f"destination {directory!r} is not on a FUSE mount — the storage mount "
            "is probably detached, and writing here would land on the root "
            "filesystem (failure mode 1.7)"
        )


def _read_mounts() -> list[tuple[str, str]]:
    from .ledger import _read_mounts as read

    return read()


# ---------------------------------------------------------------------------
# indexing + planning (pure given the index)
# ---------------------------------------------------------------------------
def index_destination(directory: str) -> DestinationIndex:
    """One `scandir` over the destination. Never call this per part."""
    names: dict = {}
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if entry.is_file():
                        names[entry.name] = entry.stat().st_size
                except OSError:
                    continue
    except OSError as exc:
        raise MoveRefused(f"cannot index {directory!r}: {exc}") from exc
    return DestinationIndex(path=directory, names=names)


def plan_moves(sources: Iterable[tuple[str, str, int]],
               dest_index: DestinationIndex) -> list[MovePlanItem]:
    """Decide, purely, what to do with each `(filename, source_path, size)`.

    No filesystem access happens here at all — that is the anti-stat-storm
    measure: the only I/O was the single `scandir` that built the index.
    """
    plan: list[MovePlanItem] = []
    for filename, source, size in sources:
        present = dest_index.size_of(filename)
        if present is None:
            action = "move"
        elif present == size:
            action = "skip-present"
        else:
            action = "skip-size-mismatch"
        plan.append(MovePlanItem(filename=filename, source=source, size=size, action=action))
    return plan


# ---------------------------------------------------------------------------
# the move
# ---------------------------------------------------------------------------
def move_part(source: str, dest_dir: str, *,
              filename: Optional[str] = None,
              expected_size: Optional[int] = None,
              chunk: int = 4 << 20,
              timeout: float = MOVE_WATCHDOG_SECONDS,
              stall: float = MOVE_STALL_SECONDS) -> MoveResult:
    """Copy `source` into `dest_dir` under a temp name, then rename.

    `filename` is the name to land under, and it is **deliberately not derived**
    from `source`. Measured 2026-09-19: the staged path came from the scraped
    redirector basename, which is the literal string `download` for every part of
    every export. Deriving the destination from the source therefore named every
    part `download`, and because the destination index is keyed by filename, a
    multi-part export collapsed into one file with the rest reported as already
    present. The caller supplies the real name from the minted URL.

    `expected_size` is the staged file's size; it is re-checked after the copy so
    a short write on a flaky remote cannot be renamed into place.
    """
    filename = filename or os.path.basename(source)
    try:
        size = os.path.getsize(source)
    except OSError as exc:
        return MoveResult(filename, "failed", detail=f"cannot stat source: {exc}")
    if expected_size is not None and size != expected_size:
        return MoveResult(
            filename, "failed",
            detail=f"staged size {size} != expected {expected_size}",
        )

    tmp_path = os.path.join(dest_dir, filename + PARTIAL_SUFFIX)
    final_path = os.path.join(dest_dir, filename)

    # The copy runs in a CHILD PROCESS, with a hard timeout.
    #
    # Why a process and not a thread or a socket timeout: the destination is an rclone
    # FUSE mount, and a wedged write blocks inside the kernel's FUSE layer. That cannot
    # be interrupted from the thread that issued it — `signal`/`close`/`timeout` do not
    # reach it — so the only boundary that can actually reclaim the run is process
    # death. Before this, one stuck write meant the whole run hung with no output and no
    # diagnosis, which is failure mode 1.16 shape 2.
    #
    # The timeout is deliberately GENEROUS and progress-aware rather than tight: rclone
    # runs with `--timeout 1h`, so a legitimately slow upload can legitimately take a
    # long time. A tight per-file limit would murder healthy transfers. The operator can
    # override it per run.
    moved = 0
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _COPY_CHILD,
             source, tmp_path, str(chunk), str(stall)],
            capture_output=True, text=True, timeout=timeout + 30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            _unlink_quietly(tmp_path)
            return MoveResult(filename, "failed",
                              detail=(detail[-1] if detail else
                                      f"copy child exited {proc.returncode}"))
        moved = int((proc.stdout or "0").strip() or 0)
    except subprocess.TimeoutExpired:
        _unlink_quietly(tmp_path)
        return MoveResult(
            filename, "failed",
            detail=(f"WATCHDOG: the copy to {dest_dir!r} made no progress for "
                    f"{timeout}s and was killed. A blocked FUSE write cannot be "
                    f"interrupted in-process, so the child was abandoned rather "
                    f"than left to wedge the run. Check the rclone mount "
                    f"(failure mode 1.7/1.16)."),
        )
    except FileNotFoundError as exc:
        _unlink_quietly(tmp_path)
        return MoveResult(filename, "failed",
                          detail=f"cannot start the copy child: {exc}")

    try:
        written = os.path.getsize(tmp_path)
        if written != size:
            _unlink_quietly(tmp_path)
            return MoveResult(filename, "failed",
                              detail=f"short write: {written} != {size}")
        # rename last: a server-side move on rclone, and no truncated file ever
        # appears under the final name.
        os.replace(tmp_path, final_path)
        return MoveResult(filename, "moved", bytes_moved=moved)
    except OSError as exc:
        _unlink_quietly(tmp_path)
        return MoveResult(filename, "failed", detail=str(exc))


#: The child's copy loop. Kept as a literal so the parent can spawn it without
#: importing this module again in a fresh interpreter (and so the exact bytes that run
#: are the ones reviewed here).
#:
#: argv: source, tmp_path, chunk, stall_seconds
#: stdout: total bytes written
_COPY_CHILD = r"""
import os, sys, time
src, dst_path, chunk, stall = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
n = 0
last = time.time()
with open(src, 'rb') as s, open(dst_path, 'wb') as d:
    while True:
        b = s.read(chunk)
        if not b:
            break
        d.write(b)
        n += len(b)
        now = time.time()
        # Progress watchdog INSIDE the child too: a write that returns but never
        # makes progress would otherwise sit here forever without tripping the
        # parent's timeout, because the parent only bounds the whole call.
        if now - last > stall:
            sys.stderr.write('no progress for %ss after %d bytes\n' % (stall, n))
            sys.exit(3)
        last = now
    d.flush()
    os.fsync(d.fileno())
print(n)
"""


def _unlink_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def assert_staging_ready(path: str, *, min_headroom: Optional[int] = None) -> None:
    """Refuse to stage where there is no room.

    The architecture requires an approved-root and headroom check, and it did not
    exist at all: `run_once` called `os.makedirs(cfg.staging_dir)` and nothing else.
    Staging shares a volume with rclone's VFS cache (`--vfs-cache-max-size 100G`) on the
    box this was written for, so a full transfer transiently occupies staging **plus**
    cache, and `ENOSPC` mid-write leaves a wedged partial behind (failure mode 1.16).

    `min_headroom=None` means "do not check" and is the DEFAULT, deliberately. v2 had a
    hard-coded 20 GiB floor with no escape, which made its own test suite unrunnable on
    a nearly-full development disk — the guard protecting production broke every test.
    The escape here is explicit and the caller decides; the CLI supplies a real value
    for real runs.
    """
    if not os.path.isdir(path):
        raise MoveRefused(f"staging directory does not exist: {path!r}")
    if min_headroom is None:
        return
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        raise MoveRefused(f"cannot measure free space at {path!r}: {exc}") from exc
    if free < min_headroom:
        raise MoveRefused(
            f"staging {path!r} has {free / 2**30:.1f} GiB free, below the "
            f"{min_headroom / 2**30:.1f} GiB headroom floor"
        )
