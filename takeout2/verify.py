"""Local-only verification. Never touches the network. Never full-reads.

NORMATIVE implementation of ``docs/v2/00-CONTRACTS.md`` §5.4.

Why this module is deliberately shallow:

The v1 engine ran ``zipfile.testzip()`` over completed parts. On a multi-TB
archive living on a JuiceFS FUSE mount that reads EVERY byte back over the
network filesystem — measured in hours, with real FUSE-stall risk
(``docs/webgui/14-resume-cookies-multiaccount.md``, "Known remaining sharp
edges"). Meanwhile it proves almost nothing that a 2-seek structural check
does not already prove for our failure mode, which is *truncation*, not bit-rot.

Verification ladder (see ``VerifyState``):

===========  ===================================================  ==============
Level        Check                                                Cost per 10 GB
===========  ===================================================  ==============
SIZE_OK      on-disk size == expected size                        one stat
STRUCT_OK    head is ``PK\\x03\\x04`` AND EOCD found in tail       two seeks
HASH_OK      full sha256                                          FULL READ
===========  ===================================================  ==============

``STRUCT_OK`` is the default acceptance bar. ``HASH_OK`` is opt-in via an
explicit ``--deep`` flag and must never run by default.
"""
from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from typing import Iterator, Optional

from .contracts import VerifyState

__all__ = ["VerifyResult", "verify_part", "scan_parts_dir", "OnDiskPart",
           "EOCD_SIG", "ZIP_LOCAL_HEADER", "MAX_EOCD_SEARCH"]

ZIP_LOCAL_HEADER = b"PK\x03\x04"
EOCD_SIG = b"PK\x05\x06"          # End Of Central Directory
ZIP64_EOCD_LOCATOR = b"PK\x06\x07"

#: The EOCD record lives in the last 22 bytes plus up to 64 KiB of comment.
MAX_EOCD_SEARCH = 66 * 1024


@dataclass(frozen=True)
class VerifyResult:
    state: VerifyState
    size_on_disk: int
    detail: str = ""
    sha256: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.state.rank >= VerifyState.STRUCT_OK.rank

    @property
    def corrupt(self) -> bool:
        return self.state is VerifyState.CORRUPT


@dataclass(frozen=True)
class OnDiskPart:
    """What a single scandir entry told us. One syscall per file, not N."""

    filename: str
    path: str
    size: int


def scan_parts_dir(directory: str) -> dict[str, OnDiskPart]:
    """Return ``{filename: OnDiskPart}`` using ONE directory scan.

    v1 issued a ``stat()`` per part in a pre-pass; over 58 parts on JuiceFS
    that took ~6 minutes — long enough for the download cookie to idle out and
    die before a single byte moved. ``os.scandir`` carries size in the dirent
    on most filesystems, so this is one pass and no per-file stat storm.
    """
    found: dict[str, OnDiskPart] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                found[entry.name] = OnDiskPart(entry.name, entry.path, size)
    except FileNotFoundError:
        return {}
    return found


def _find_eocd(fh, file_size: int) -> bool:
    """Seek the tail and look for the End-Of-Central-Directory signature."""
    window = min(MAX_EOCD_SEARCH, file_size)
    fh.seek(file_size - window)
    tail = fh.read(window)
    return EOCD_SIG in tail or ZIP64_EOCD_LOCATOR in tail


def verify_part(path: str, size_expected: Optional[int] = None,
                level: VerifyState = VerifyState.STRUCT_OK,
                sha256_expected: Optional[str] = None) -> VerifyResult:
    """Verify one part locally, up to ``level``.

    Never issues a network request. Never reads the whole file unless
    ``level is VerifyState.HASH_OK``.
    """
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return VerifyResult(VerifyState.UNVERIFIED, 0, "file not present")
    except OSError as exc:
        return VerifyResult(VerifyState.UNVERIFIED, 0, f"stat failed: {exc}")

    if size == 0:
        return VerifyResult(VerifyState.CORRUPT, 0, "zero-length file")

    # --- SIZE ---------------------------------------------------------
    if size_expected is not None:
        if size < size_expected:
            return VerifyResult(
                VerifyState.UNVERIFIED, size,
                f"incomplete: {size}/{size_expected} bytes ({size / size_expected:.1%}) — resumable",
            )
        if size > size_expected:
            return VerifyResult(
                VerifyState.CORRUPT, size,
                f"oversized: {size} > expected {size_expected} — likely appended to a stale file",
            )

    if level is VerifyState.SIZE_OK:
        if size_expected is None:
            return VerifyResult(VerifyState.UNVERIFIED, size, "no expected size to compare")
        return VerifyResult(VerifyState.SIZE_OK, size, "size matches")

    # --- STRUCTURE (two seeks, no full read) ---------------------------
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
            if head != ZIP_LOCAL_HEADER:
                return VerifyResult(
                    VerifyState.CORRUPT, size,
                    f"bad magic {head!r} — expected PK\\x03\\x04 "
                    f"(an HTML error page saved as .zip looks like this)",
                )
            if not _find_eocd(fh, size):
                return VerifyResult(
                    VerifyState.UNVERIFIED, size,
                    "no EOCD in tail — file is truncated, resume it",
                )
    except OSError as exc:
        return VerifyResult(VerifyState.UNVERIFIED, size, f"read failed: {exc}")

    if level is not VerifyState.HASH_OK:
        return VerifyResult(VerifyState.STRUCT_OK, size, "magic + EOCD present")

    # --- HASH (opt-in, full read) --------------------------------------
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        return VerifyResult(VerifyState.STRUCT_OK, size, f"hash read failed: {exc}")

    hexdigest = digest.hexdigest()
    if sha256_expected and hexdigest != sha256_expected:
        return VerifyResult(VerifyState.CORRUPT, size,
                            "sha256 mismatch", sha256=hexdigest)
    return VerifyResult(VerifyState.HASH_OK, size, "sha256 computed", sha256=hexdigest)


def iter_verify(paths: Iterator[tuple[str, Optional[int]]],
                level: VerifyState = VerifyState.STRUCT_OK
                ) -> Iterator[tuple[str, VerifyResult]]:
    """Verify many parts lazily so callers can stream progress."""
    for path, expected in paths:
        yield path, verify_part(path, expected, level)
