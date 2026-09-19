"""Integrity checking — a thin wrapper over v2's already-good verifier.

Deliberately **not** reimplemented. `takeout2/verify.py` is measured-good and its
semantics carry a distinction this project depends on:

    STRUCT_OK    head is ``PK\\x03\\x04`` AND an EOCD record is found in the tail
    UNVERIFIED   short, or no EOCD — "truncated, resume it"
    CORRUPT      genuinely wrong (oversized, bad magic)

Truncation maps to `UNVERIFIED`, **never** `CORRUPT`, on purpose: a scheduler that
treats a resumable part as ruined throws away bytes that a free `Range` resume
would have finished.

The one thing this wrapper adds is the vocabulary the rest of v3 speaks, plus a
`verify_state_ok()` helper so callers do not have to know v2's enum.

This is the single intentional v3 → v2 dependency. If v2 is ever removed, this
module is the one place to port (the EOCD scan is at `takeout2/verify.py:97-102`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from takeout2.contracts import VerifyState
from takeout2.verify import scan_parts_dir, verify_part

from .errors import AutopilotError

__all__ = [
    "VerifyOutcome",
    "verify_local",
    "verify_dir",
    "verify_state_ok",
    "verify_state_resumable",
]

#: States that mean "this part is finished".
_OK = {VerifyState.STRUCT_OK, VerifyState.HASH_OK}
#: States that mean "the bytes on disk are a valid resume point".
_RESUMABLE = {VerifyState.UNVERIFIED}


@dataclass
class VerifyOutcome:
    ok: bool
    state: str
    detail: str
    size: int
    resumable: bool

    @property
    def corrupt(self) -> bool:
        return not self.ok and not self.resumable


def verify_state_ok(state) -> bool:
    return state in _OK


def verify_state_resumable(state) -> bool:
    return state in _RESUMABLE


def verify_local(path: str, *, size_expected: Optional[int] = None,
                 hash_check: bool = False) -> VerifyOutcome:
    """Verify one part on **local** disk.

    Cheap: `STRUCT_OK` is two seeks, so this belongs on the staging volume where
    reads are fast — not on the archive FUSE mount, where the historical
    per-part `stat()` pre-pass over 58 parts took about six minutes.
    """
    level = VerifyState.HASH_OK if hash_check else VerifyState.STRUCT_OK
    try:
        result = verify_part(path, size_expected=size_expected, level=level)
    except OSError as exc:
        # Measured 2026-09-19: verify_part raises for NEITHER case — a missing file
        # returns `UNVERIFIED / "file not present"`, and a directory returns
        # `CORRUPT / "zero-length file"`. So this branch is belt-and-braces for a
        # genuinely unreadable path (permissions, a dead FUSE mount); it is not how
        # absence is handled.
        raise AutopilotError(f"cannot verify {path!r}: {exc}") from exc
    return VerifyOutcome(
        ok=bool(result.ok),
        state=getattr(result.state, "name", str(result.state)),
        detail=getattr(result, "detail", "") or "",
        size=int(getattr(result, "size", 0) or 0),
        resumable=verify_state_resumable(result.state),
    )


def verify_dir(directory: str) -> dict:
    """One directory scan, returned as `{filename: OnDiskPart}`.

    Use this instead of a per-part `stat()` loop: a single `scandir` is what
    keeps the mover from re-creating the FUSE stat-storm livelock.
    """
    if not os.path.isdir(directory):
        return {}
    return scan_parts_dir(directory)
