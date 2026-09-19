"""Offline tests for the verify wrapper.

The distinction under test is the one the scheduler depends on:

    UNVERIFIED  -> "truncated, resume it"   (a free Range resume finishes it)
    CORRUPT     -> genuinely wrong           (do not waste a resume on it)

Treating a resumable part as ruined throws away bytes that cost nothing to
recover. `takeout2/verify.py` gets this right; these tests pin it so the v3
wrapper cannot drift from it.
"""
from __future__ import annotations

import zipfile

import pytest

from autopilot.errors import AutopilotError
from autopilot.verify import verify_dir, verify_local, verify_state_ok


def make_zip(path, payload: bytes = b"hello takeout" * 100) -> int:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", payload)
    return path.stat().st_size


def test_a_real_zip_verifies_ok(tmp_path):
    p = tmp_path / "ok.zip"
    size = make_zip(p)
    out = verify_local(str(p), size_expected=size)
    assert out.ok is True
    assert out.state == "STRUCT_OK"
    assert out.resumable is False and out.corrupt is False


def test_a_truncated_zip_is_resumable_not_corrupt(tmp_path):
    """Header present, EOCD gone — an interrupted download, not a ruined one."""
    p = tmp_path / "cut.zip"
    size = make_zip(p)
    p.write_bytes(p.read_bytes()[: size // 2])

    out = verify_local(str(p), size_expected=size)
    assert out.ok is False
    assert out.resumable is True, "a truncated part must be resumable"
    assert out.corrupt is False, "truncation must NOT be reported as corruption"
    assert "truncat" in out.detail.lower() or "incomplete" in out.detail.lower()


def test_a_short_file_against_expected_size_is_resumable(tmp_path):
    p = tmp_path / "short.zip"
    size = make_zip(p)
    out = verify_local(str(p), size_expected=size + 10_000)
    assert out.ok is False and out.resumable is True and out.corrupt is False


def test_an_oversized_file_is_corrupt(tmp_path):
    p = tmp_path / "big.zip"
    size = make_zip(p)
    out = verify_local(str(p), size_expected=size - 10)
    assert out.ok is False
    assert out.corrupt is True, "oversized is genuinely wrong, not resumable"


def test_a_non_zip_is_not_ok(tmp_path):
    p = tmp_path / "nope.zip"
    p.write_bytes(b"this is not a zip at all" * 50)
    out = verify_local(str(p))
    assert out.ok is False


def test_a_missing_file_is_a_verdict_not_an_exception(tmp_path):
    """Measured: `verify_part` returns `UNVERIFIED / "file not present"`.

    It does NOT raise, so a caller can treat "nothing on disk yet" as an ordinary
    state rather than catching an exception. The wrapper's `except OSError` is
    therefore for unreadable paths (a dead FUSE mount), not for absence.
    """
    out = verify_local(str(tmp_path / "absent.zip"))
    assert out.ok is False
    assert out.state == "UNVERIFIED"
    assert "not present" in out.detail
    assert out.corrupt is False


def test_a_directory_is_reported_corrupt_not_resumable(tmp_path):
    """Measured: a directory yields `CORRUPT / "zero-length file"`.

    The detail string is not graceful — a directory is not a zero-length file — but
    the verdict is right where it matters: not ok, and **not resumable**, so a
    scheduler will not spend a free `Range` resume on it.
    """
    d = tmp_path / "adir"
    d.mkdir()
    out = verify_local(str(d))
    assert out.ok is False
    assert out.corrupt is True
    assert out.resumable is False


def test_verify_state_ok_accepts_only_the_finished_states():
    from takeout2.contracts import VerifyState

    assert verify_state_ok(VerifyState.STRUCT_OK) is True
    assert verify_state_ok(VerifyState.HASH_OK) is True
    assert verify_state_ok(VerifyState.UNVERIFIED) is False


def test_verify_dir_scans_once_and_returns_a_name_map(tmp_path):
    make_zip(tmp_path / "a.zip")
    make_zip(tmp_path / "b.zip", b"other" * 500)
    found = verify_dir(str(tmp_path))
    assert set(found) >= {"a.zip", "b.zip"}


def test_verify_dir_on_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert verify_dir(str(tmp_path / "nope")) == {}
