"""Pre-write guards. Answers ONE question: is it safe to write here, now?

NORMATIVE guard for the disk-safety rule in ``docs/v2/00-CONTRACTS.md`` §5.4
(``ReasonCode.DISK_ERROR`` is the outcome a caller should record when a check
here fails).

Why this module exists at all — a real incident on this box:

``/opt/archives`` is an **rclone FUSE mount**. When rclone dies, the kernel does
not make that path fail; it makes it *ordinary*. The mountpoint reverts to a
plain, empty directory **on the root filesystem** — a filesystem with ~15 GB
free (81 % full). The downloader, seeing an empty parts dir, cheerfully decides
every part is missing and starts streaming a 10 GB part onto the root disk.
The box fills and the whole server goes down. This has already happened.

Two properties follow from that story and drive the whole design:

1. **The mount check must come first and must short-circuit.** If the path is
   not a real mount, ``statvfs()`` measures the *root* disk. The root disk very
   often has "enough" free space for one part, so the space check would
   *wrongly pass* and green-light exactly the write we are trying to prevent.
   A failed mount check therefore never falls through to a space check.

2. **"Mounted" is not the same as "alive".** A hung or freshly-remounted-empty
   FUSE mount still reports as mounted to ``os.path.ismount()``. The only cheap
   proof that the *storage* is really there is to read something we know we put
   there — hence the optional ``sentinel`` file.

Design constraints, because this runs before EVERY part:

  * Pure and cheap: a couple of ``stat``/``statvfs`` calls. No writes, no
    sleeps, no network, no retries.
  * Never raises for a missing platform API. ``os.statvfs`` does not exist on
    Windows, so ``shutil.disk_usage`` is the documented fallback and the
    substitution is recorded in ``PreflightResult.checks``.
  * ``checks`` is ALWAYS populated, especially on failure — a preflight that
    blocks a multi-terabyte job must say precisely why.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["PreflightResult", "check_is_mount", "check_free_space",
           "preflight_write", "disk_free", "nearest_existing",
           "DEFAULT_MIN_HEADROOM", "GIB"]

GIB = 1024 ** 3

#: Never let a write drive the target filesystem below this much free space.
#: 20 GiB is deliberately larger than one Takeout part (~10 GB later in a
#: multi-part export, ~2 GB early) so a single miscounted part cannot fill a
#: disk even if the size estimate was wrong by 100 %.
DEFAULT_MIN_HEADROOM = 20 * 1024 ** 3   # 20 GiB


def _gib(n: Optional[int]) -> str:
    """Human GiB for operator-facing reasons. ``None`` renders as ``?``."""
    if n is None:
        return "?"
    return f"{n / GIB:.2f} GiB"


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str = ""                 # "" when ok, else short human cause
    checks: dict = field(default_factory=dict)   # diagnostics, always populated

    @property
    def failed(self) -> bool:
        return not self.ok


# --------------------------------------------------------------------------
# Platform probes — module-level ON PURPOSE so tests can inject numbers
# --------------------------------------------------------------------------
def nearest_existing(path: str) -> str:
    """Walk UP from ``path`` to the nearest ancestor that exists.

    The parts dir usually does not exist yet on a first run; the thing we must
    interrogate is the storage root that would *hold* it. Returns an absolute
    path, falling back to the filesystem root if nothing up the chain exists.
    """
    current = os.path.abspath(path or ".")
    while True:
        if os.path.exists(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:        # hit the root, nothing existed
            return current
        current = parent


def disk_free(path: str) -> dict:
    """Free bytes for an UNPRIVILEGED writer, plus provenance.

    Returns a dict — never raises. Keys:

    ``free_bytes``
        Usable bytes, or ``None`` when it could not be determined.
    ``total_bytes``
        Filesystem size when known, else ``None``.
    ``source``
        ``"statvfs"``, ``"disk_usage"`` or ``"unavailable"``.
    ``error``
        Stringified ``OSError`` when the probe itself failed.

    ``f_bavail`` (not ``f_bfree``) is the correct field: on ext4 ~5 % of the
    device is reserved for root, so ``f_bfree`` overstates what our non-root
    downloader may actually consume.

    This function is module-level and referenced by name inside
    :func:`check_free_space` so the test suite can monkeypatch it and exercise
    boundary arithmetic deterministically on any platform.
    """
    if hasattr(os, "statvfs"):
        try:
            st = os.statvfs(path)
        except OSError as exc:
            return {"free_bytes": None, "total_bytes": None,
                    "source": "statvfs", "error": str(exc)}
        return {"free_bytes": st.f_bavail * st.f_frsize,
                "total_bytes": st.f_blocks * st.f_frsize,
                "source": "statvfs", "error": None}

    # Windows / any platform without statvfs.
    if hasattr(shutil, "disk_usage"):
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            return {"free_bytes": None, "total_bytes": None,
                    "source": "disk_usage", "error": str(exc)}
        return {"free_bytes": usage.free, "total_bytes": usage.total,
                "source": "disk_usage", "error": None}

    return {"free_bytes": None, "total_bytes": None,
            "source": "unavailable",
            "error": "no statvfs and no shutil.disk_usage on this platform"}


def _fstype(path: str) -> Optional[str]:
    """Best-effort filesystem type. ``None`` when unknowable.

    Purely diagnostic: it is what tells a human reading the log "you thought
    you were writing to rclone, you were writing to ext4". Never used for a
    pass/fail decision, so a ``None`` here must not fail a check.
    """
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            entries = []
            for line in fh:
                fields = line.split()
                if len(fields) >= 3:
                    entries.append((fields[1], fields[2]))
    except OSError:
        return None

    target = os.path.abspath(path)
    best_point, best_type = "", None
    for mount_point, fs_type in entries:
        if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) \
                and len(mount_point) >= len(best_point):
            best_point, best_type = mount_point, fs_type
    return best_type


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------
def _mount_checks(path: str, sentinel: Optional[str]) -> dict:
    resolved = nearest_existing(path)
    checks: dict = {
        "path": path,
        "resolved": resolved,
        "is_mount": False,
        "fstype": _fstype(resolved),
        "sentinel": sentinel,
        "sentinel_ok": None,
    }
    try:
        checks["is_mount"] = bool(os.path.ismount(resolved))
    except OSError as exc:                      # pragma: no cover - exotic
        checks["error"] = str(exc)

    if sentinel:
        # Deliberately anchored at the requested path, not at ``resolved``:
        # the sentinel proves THIS directory's storage is live, and a missing
        # parts dir is itself a reason to distrust the mount.
        sentinel_path = os.path.join(path, sentinel)
        checks["sentinel_path"] = sentinel_path
        ok = False
        try:
            if os.path.isfile(sentinel_path):
                with open(sentinel_path, "rb") as fh:
                    fh.read(1)                 # readable, not merely present
                ok = True
            else:
                checks["sentinel_error"] = "not a readable file"
        except OSError as exc:
            checks["sentinel_error"] = str(exc)
        checks["sentinel_ok"] = ok
    return checks


def _evaluate_mount(checks: dict, *, require_mount: bool) -> PreflightResult:
    if require_mount and not checks["is_mount"]:
        return PreflightResult(
            False,
            f"{checks['resolved']} is not a mount point — the archive volume is "
            f"detached; writing here would land on the root filesystem",
            checks,
        )
    if checks.get("sentinel") and checks.get("sentinel_ok") is False:
        return PreflightResult(
            False,
            f"sentinel {checks['sentinel']!r} unreadable under {checks['path']} "
            f"— mount looks present but its storage is empty or hung",
            checks,
        )
    return PreflightResult(True, "", checks)


def check_is_mount(path: str, *, sentinel: Optional[str] = None) -> PreflightResult:
    """Assert ``path``'s storage root is a real, live mount.

    Two independent assertions, both needed:

    * ``os.path.ismount()`` on the nearest existing ancestor — catches the
      "rclone died and the mountpoint is now a plain root-fs directory" case.
    * ``sentinel`` (optional, relative filename) must exist AND be readable —
      catches the mount that is technically mounted but hung or empty, which
      ``ismount`` alone reports as perfectly fine.
    """
    return _evaluate_mount(_mount_checks(path, sentinel), require_mount=True)


def check_free_space(path: str, need_bytes: int, *,
                     min_headroom: int = DEFAULT_MIN_HEADROOM) -> PreflightResult:
    """Assert writing ``need_bytes`` still leaves ``min_headroom`` free.

    Fails when ``free - need_bytes < min_headroom``. Note that ``need_bytes=0``
    still enforces the headroom floor: a disk already below the floor is not a
    disk we should append even one byte to.
    """
    # The parts dir may not exist yet on a first run; the filesystem that
    # would hold it is the one whose headroom matters.
    resolved = nearest_existing(path)
    probe = disk_free(resolved)
    free = probe.get("free_bytes")
    checks: dict = {
        "path": path,
        "resolved": resolved,
        "free_bytes": free,
        "need_bytes": need_bytes,
        "min_headroom": min_headroom,
        "would_remain": None if free is None else free - need_bytes,
        "total_bytes": probe.get("total_bytes"),
        "source": probe.get("source"),
    }
    if probe.get("error"):
        checks["error"] = probe["error"]

    if free is None:
        if probe.get("source") == "unavailable":
            # No API at all. Do not block the operator on a platform quirk;
            # record loudly that the guarantee was not obtained.
            checks["limitation"] = ("free space could not be measured on this "
                                    "platform; headroom NOT enforced")
            return PreflightResult(True, "", checks)
        checks["limitation"] = "free space probe failed; treating as unsafe"
        return PreflightResult(
            False,
            f"cannot determine free space at {path}: {probe.get('error')}",
            checks,
        )

    would_remain = checks["would_remain"]
    if would_remain < min_headroom:
        return PreflightResult(
            False,
            f"insufficient space at {path}: {_gib(free)} free, need "
            f"{_gib(need_bytes)}, would leave {_gib(would_remain)} "
            f"below the {_gib(min_headroom)} headroom floor",
            checks,
        )
    return PreflightResult(True, "", checks)


# --------------------------------------------------------------------------
# Composite gate
# --------------------------------------------------------------------------
def preflight_write(parts_dir: str, need_bytes: int = 0, *,
                    require_mount: bool = True,
                    sentinel: Optional[str] = None,
                    min_headroom: int = DEFAULT_MIN_HEADROOM) -> PreflightResult:
    """The one call a writer should make before opening a part for append.

    Order is load-bearing: mount FIRST, and a mount failure SHORT-CIRCUITS.
    Probing free space on a detached mountpoint measures the root filesystem,
    which usually has room for one part — so continuing would return ``ok`` for
    precisely the write that once took this server down.

    ``require_mount=False`` relaxes only the ``ismount`` assertion (local dev,
    and the Windows test runners where ``tmp_path`` is never a mount). The
    sentinel assertion, if a sentinel is given, still applies.
    """
    mount_checks = _mount_checks(parts_dir, sentinel)
    mount_checks["require_mount"] = require_mount
    mount = _evaluate_mount(mount_checks, require_mount=require_mount)
    if mount.failed:
        checks = dict(mount.checks)
        checks["free_space_checked"] = False
        checks["short_circuited"] = "mount check failed; free space deliberately NOT probed"
        return PreflightResult(False, mount.reason, checks)

    space = check_free_space(parts_dir, need_bytes, min_headroom=min_headroom)
    checks = {**mount.checks, **space.checks, "free_space_checked": True}
    return PreflightResult(space.ok, space.reason, checks)
