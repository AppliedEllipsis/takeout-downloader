"""Tests for takeout2.verify — local-only, no full reads.

Uses real ZIP files built in tmp_path so the structural checks are tested
against genuine byte layouts rather than hand-rolled fakes.
"""
from __future__ import annotations

import os
import zipfile

import pytest

from takeout2.contracts import VerifyState
from takeout2.verify import (EOCD_SIG, ZIP_LOCAL_HEADER, scan_parts_dir,
                             verify_part)


def make_zip(path, payload=b"hello takeout" * 1000, name="data.txt"):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, payload)
    return path


@pytest.fixture
def good_zip(tmp_path):
    return str(make_zip(tmp_path / "takeout-20260616T040104Z-9-001.zip"))


class TestHappyPath:
    def test_real_zip_reaches_struct_ok(self, good_zip):
        result = verify_part(good_zip)
        assert result.state is VerifyState.STRUCT_OK
        assert result.ok and not result.corrupt

    def test_size_level_matches(self, good_zip):
        size = os.path.getsize(good_zip)
        result = verify_part(good_zip, size_expected=size, level=VerifyState.SIZE_OK)
        assert result.state is VerifyState.SIZE_OK

    def test_hash_level_computes_sha256(self, good_zip):
        result = verify_part(good_zip, level=VerifyState.HASH_OK)
        assert result.state is VerifyState.HASH_OK
        assert result.sha256 and len(result.sha256) == 64

    def test_hash_mismatch_is_corrupt(self, good_zip):
        result = verify_part(good_zip, level=VerifyState.HASH_OK,
                             sha256_expected="0" * 64)
        assert result.state is VerifyState.CORRUPT


class TestTruncationDetection:
    def test_truncated_zip_is_resumable_not_corrupt(self, tmp_path):
        """The dominant real failure: an interrupted stream. Must be reported
        as resumable (UNVERIFIED), never as CORRUPT, or we would throw away
        gigabytes and spend another precious attempt."""
        path = make_zip(tmp_path / "part.zip")
        full = os.path.getsize(path)
        with open(path, "r+b") as fh:
            fh.truncate(full // 2)

        result = verify_part(str(path))
        assert result.state is VerifyState.UNVERIFIED
        assert "truncated" in result.detail.lower()
        assert not result.corrupt

    def test_short_file_against_expected_size_is_resumable(self, tmp_path):
        path = make_zip(tmp_path / "part.zip")
        actual = os.path.getsize(path)
        result = verify_part(str(path), size_expected=actual * 2)
        assert result.state is VerifyState.UNVERIFIED
        assert "incomplete" in result.detail
        assert "resumable" in result.detail

    def test_oversized_file_is_corrupt(self, tmp_path):
        """Bigger than expected means we appended onto a stale file — a real
        v1 hazard when the filename scheme changed between runs."""
        path = make_zip(tmp_path / "part.zip")
        actual = os.path.getsize(path)
        result = verify_part(str(path), size_expected=actual // 2)
        assert result.state is VerifyState.CORRUPT
        assert "oversized" in result.detail


class TestCorruptionDetection:
    def test_html_saved_as_zip_is_corrupt(self, tmp_path):
        """A login page written to a .zip — exactly what a dead cookie yields."""
        path = tmp_path / "part.zip"
        path.write_bytes(b"<!DOCTYPE html><html>Sign in to continue</html>")
        result = verify_part(str(path))
        assert result.state is VerifyState.CORRUPT
        assert "bad magic" in result.detail

    def test_zero_length_is_corrupt(self, tmp_path):
        path = tmp_path / "part.zip"
        path.write_bytes(b"")
        assert verify_part(str(path)).state is VerifyState.CORRUPT

    def test_missing_file_is_unverified(self, tmp_path):
        result = verify_part(str(tmp_path / "nope.zip"))
        assert result.state is VerifyState.UNVERIFIED
        assert "not present" in result.detail


class TestNoFullRead:
    def test_struct_check_does_not_read_whole_file(self, tmp_path, monkeypatch):
        """Guards the prohibition: STRUCT_OK must stay O(1) in file size.
        A full read of a 10 GB part over FUSE takes minutes and can stall.

        Uses incompressible random bytes: a payload of repeated 'x' deflates to
        a few KB, which would make this assertion vacuous.
        """
        path = make_zip(tmp_path / "part.zip", payload=os.urandom(4_000_000))
        total = os.path.getsize(path)
        assert total > 3_000_000, "payload must stay incompressible for this test"

        real_open = open
        read_bytes = {"n": 0}

        class CountingFile:
            def __init__(self, fh):
                self._fh = fh

            def read(self, n=-1):
                data = self._fh.read(n)
                read_bytes["n"] += len(data)
                return data

            def __getattr__(self, item):
                return getattr(self._fh, item)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._fh.close()
                return False

        def counting_open(*args, **kwargs):
            return CountingFile(real_open(*args, **kwargs))

        monkeypatch.setattr("builtins.open", counting_open)
        assert verify_part(str(path)).state is VerifyState.STRUCT_OK

        # head (4 bytes) + at most a 66 KiB tail window — never the whole file.
        assert read_bytes["n"] < 70 * 1024
        assert read_bytes["n"] < total / 10


class TestScanPartsDir:
    def test_single_scan_returns_all_sizes(self, tmp_path):
        for i in range(5):
            make_zip(tmp_path / f"takeout-20260616T040104Z-9-{i:03d}.zip")
        found = scan_parts_dir(str(tmp_path))
        assert len(found) == 5
        assert all(p.size > 0 for p in found.values())

    def test_missing_directory_returns_empty(self, tmp_path):
        assert scan_parts_dir(str(tmp_path / "absent")) == {}

    def test_ignores_subdirectories(self, tmp_path):
        (tmp_path / "logs").mkdir()
        make_zip(tmp_path / "part.zip")
        found = scan_parts_dir(str(tmp_path))
        assert set(found) == {"part.zip"}


class TestSignatureConstants:
    def test_constants_match_the_zip_spec(self):
        assert ZIP_LOCAL_HEADER == b"PK\x03\x04"
        assert EOCD_SIG == b"PK\x05\x06"
