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
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .errors import AutopilotError
from .ledger import _normalize, fuse_mount_for

__all__ = [
    "MovePlanItem",
    "MoveResult",
    "MoveRefused",
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
              expected_size: Optional[int] = None,
              chunk: int = 4 << 20) -> MoveResult:
    """Copy `source` into `dest_dir` under a temp name, then rename.

    `expected_size` is the staged file's size; it is re-checked after the copy so
    a short write on a flaky remote cannot be renamed into place.
    """
    filename = os.path.basename(source)
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
    try:
        with open(source, "rb") as src, open(tmp_path, "wb") as dst:
            moved = 0
            while True:
                block = src.read(chunk)
                if not block:
                    break
                dst.write(block)
                moved += len(block)
            dst.flush()
            os.fsync(dst.fileno())
        written = os.path.getsize(tmp_path)
        if written != size:
            os.unlink(tmp_path)
            return MoveResult(filename, "failed",
                              detail=f"short write: {written} != {size}")
        # rename last: a server-side move on rclone, and no truncated file ever
        # appears under the final name.
        os.replace(tmp_path, final_path)
        return MoveResult(filename, "moved", bytes_moved=moved)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return MoveResult(filename, "failed", detail=str(exc))
