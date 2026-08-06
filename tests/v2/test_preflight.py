"""Tests for takeout2.preflight — the guard that stops us filling the root disk.

No network, no real FUSE, no writes outside tmp_path. Free-space arithmetic is
exercised by monkeypatching the module-level ``disk_free`` probe, which keeps
the boundary tests exact and identical on Windows and Linux (Windows has no
``os.statvfs``, so the numbers must never come from the real platform API in a
test that asserts on a boundary).
"""
from __future__ import annotations

import os

import pytest

from takeout2 import preflight
from takeout2.preflight import (DEFAULT_MIN_HEADROOM, GIB, PreflightResult,
                                check_free_space, check_is_mount, disk_free,
                                nearest_existing, preflight_write)


def fake_probe(free_bytes, *, total_bytes=None, source="statvfs", error=None):
    """Build a replacement for ``preflight.disk_free`` returning fixed numbers."""
    def _probe(path):
        return {"free_bytes": free_bytes,
                "total_bytes": total_bytes if total_bytes is not None else 500 * GIB,
                "source": source, "error": error}
    return _probe


@pytest.fixture
def free_100gib(monkeypatch):
    monkeypatch.setattr(preflight, "disk_free", fake_probe(100 * GIB))


# --------------------------------------------------------------------------
# Result object
# --------------------------------------------------------------------------
class TestPreflightResult:
    def test_failed_is_the_inverse_of_ok(self):
        assert PreflightResult(True).failed is False
        assert PreflightResult(False, "boom").failed is True

    def test_is_frozen(self):
        result = PreflightResult(True)
        with pytest.raises(Exception):
            result.ok = False        # type: ignore[misc]

    def test_ok_result_has_empty_reason(self, tmp_path, free_100gib):
        result = check_free_space(str(tmp_path), 1 * GIB)
        assert result.ok and result.reason == ""


# --------------------------------------------------------------------------
# Free space
# --------------------------------------------------------------------------
class TestCheckFreeSpace:
    def test_plenty_of_room_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(200 * GIB))
        result = check_free_space(str(tmp_path), 10 * GIB)
        assert result.ok
        assert result.checks["free_bytes"] == 200 * GIB
        assert result.checks["would_remain"] == 190 * GIB

    def test_insufficient_space_fails(self, tmp_path, monkeypatch):
        """The production shape: 15 GB free on root, a 10 GB part inbound."""
        monkeypatch.setattr(preflight, "disk_free", fake_probe(15 * GIB))
        result = check_free_space(str(tmp_path), 10 * GIB)
        assert result.failed
        assert "insufficient space" in result.reason
        assert result.checks["would_remain"] == 5 * GIB

    def test_reason_is_human_gib(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(15 * GIB))
        result = check_free_space(str(tmp_path), 10 * GIB)
        assert "15.00 GiB" in result.reason
        assert "10.00 GiB" in result.reason
        assert "20.00 GiB" in result.reason

    def test_checks_always_populated_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(1 * GIB))
        result = check_free_space(str(tmp_path), 0)
        assert result.failed
        for key in ("free_bytes", "need_bytes", "min_headroom", "would_remain"):
            assert key in result.checks, key

    def test_custom_headroom_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(15 * GIB))
        assert check_free_space(str(tmp_path), 10 * GIB,
                                min_headroom=1 * GIB).ok

    def test_probe_error_is_treated_as_unsafe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free",
                            fake_probe(None, error="[Errno 5] I/O error"))
        result = check_free_space(str(tmp_path), 1 * GIB)
        assert result.failed
        assert "cannot determine free space" in result.reason
        assert result.checks["limitation"]

    def test_missing_platform_api_does_not_raise(self, tmp_path, monkeypatch):
        """No statvfs AND no disk_usage: must degrade, never explode."""
        monkeypatch.setattr(preflight, "disk_free",
                            fake_probe(None, source="unavailable",
                                       error="no API"))
        result = check_free_space(str(tmp_path), 1 * GIB)
        assert result.ok
        assert "NOT enforced" in result.checks["limitation"]


class TestHeadroomBoundary:
    def test_exactly_at_the_limit_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free",
                            fake_probe(DEFAULT_MIN_HEADROOM + 10 * GIB))
        result = check_free_space(str(tmp_path), 10 * GIB)
        assert result.ok
        assert result.checks["would_remain"] == DEFAULT_MIN_HEADROOM

    def test_one_byte_over_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free",
                            fake_probe(DEFAULT_MIN_HEADROOM + 10 * GIB - 1))
        result = check_free_space(str(tmp_path), 10 * GIB)
        assert result.failed
        assert result.checks["would_remain"] == DEFAULT_MIN_HEADROOM - 1

    def test_need_zero_still_enforces_headroom(self, tmp_path, monkeypatch):
        """A disk already under the floor must not accept even a zero-byte part."""
        monkeypatch.setattr(preflight, "disk_free",
                            fake_probe(DEFAULT_MIN_HEADROOM - 1))
        assert check_free_space(str(tmp_path), 0).failed

    def test_need_zero_passes_when_above_floor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free",
                            fake_probe(DEFAULT_MIN_HEADROOM))
        assert check_free_space(str(tmp_path), 0).ok

    def test_default_headroom_is_20_gib(self):
        assert DEFAULT_MIN_HEADROOM == 20 * 1024 ** 3


# --------------------------------------------------------------------------
# Mount detection
# --------------------------------------------------------------------------
class TestCheckIsMount:
    def test_tmp_path_is_not_a_mount(self, tmp_path):
        """tmp_path is an ordinary directory — exactly what a dead rclone
        mountpoint degrades into, so it must fail the strict check."""
        result = check_is_mount(str(tmp_path))
        assert result.failed
        assert "not a mount point" in result.reason
        assert result.checks["is_mount"] is False

    def test_checks_report_resolution_details(self, tmp_path):
        result = check_is_mount(str(tmp_path / "parts" / "deeper"))
        assert result.checks["resolved"] == str(tmp_path)
        assert "is_mount" in result.checks
        assert "fstype" in result.checks
        assert "sentinel_ok" in result.checks

    def test_filesystem_root_is_a_mount(self):
        root = os.path.abspath(os.sep)
        if not os.path.ismount(root):            # pragma: no cover
            pytest.skip("platform does not report the root as a mount")
        assert check_is_mount(root).ok

    def test_require_mount_false_passes_on_tmp_path(self, tmp_path):
        result = preflight_write(str(tmp_path), 0, require_mount=False,
                                 min_headroom=0)
        assert result.ok, result.reason
        assert result.checks["require_mount"] is False


class TestSentinel:
    def test_missing_sentinel_fails(self, tmp_path):
        """A mounted-but-empty FUSE mount: ismount says yes, storage is gone."""
        result = preflight_write(str(tmp_path), 0, require_mount=False,
                                 sentinel=".tk2-mount-ok", min_headroom=0)
        assert result.failed
        assert "sentinel" in result.reason
        assert result.checks["sentinel_ok"] is False

    def test_present_sentinel_passes(self, tmp_path):
        (tmp_path / ".tk2-mount-ok").write_text("archives volume\n")
        result = preflight_write(str(tmp_path), 0, require_mount=False,
                                 sentinel=".tk2-mount-ok", min_headroom=0)
        assert result.ok, result.reason
        assert result.checks["sentinel_ok"] is True

    def test_sentinel_that_is_a_directory_fails(self, tmp_path):
        (tmp_path / ".tk2-mount-ok").mkdir()
        result = check_is_mount(str(tmp_path), sentinel=".tk2-mount-ok")
        assert result.failed
        assert result.checks["sentinel_ok"] is False

    def test_no_sentinel_requested_leaves_it_unevaluated(self, tmp_path):
        result = check_is_mount(str(tmp_path))
        assert result.checks["sentinel_ok"] is None


# --------------------------------------------------------------------------
# The actual incident: mount failure must short-circuit
# --------------------------------------------------------------------------
class TestShortCircuit:
    def test_free_space_not_probed_when_mount_check_fails(self, tmp_path,
                                                          monkeypatch):
        """THE bug this module exists to prevent.

        If rclone died, tmp_path-like plain dirs live on the ROOT filesystem,
        which usually has room for one part — so a free-space check would
        return ok and green-light the write that fills the root disk. The space
        check must therefore never be reached at all.
        """
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return PreflightResult(True, "", {"spy": True})

        monkeypatch.setattr(preflight, "check_free_space", spy)
        monkeypatch.setattr(preflight, "disk_free", fake_probe(500 * GIB))

        result = preflight_write(str(tmp_path), 10 * GIB, require_mount=True)

        assert result.failed
        assert calls == [], "free-space check ran despite a failed mount check"
        assert result.checks["free_space_checked"] is False
        assert "short_circuited" in result.checks

    def test_sentinel_failure_also_short_circuits(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(preflight, "check_free_space",
                            lambda *a, **k: calls.append(1) or PreflightResult(True))
        result = preflight_write(str(tmp_path), 0, require_mount=False,
                                 sentinel="absent-marker")
        assert result.failed and calls == []

    def test_space_check_runs_when_mount_check_passes(self, tmp_path,
                                                      monkeypatch):
        calls = []

        def spy(path, need, **kwargs):
            calls.append((path, need, kwargs))
            return PreflightResult(True, "", {"spy": True})

        monkeypatch.setattr(preflight, "check_free_space", spy)
        result = preflight_write(str(tmp_path), 7 * GIB, require_mount=False)
        assert result.ok
        assert len(calls) == 1
        assert calls[0][1] == 7 * GIB
        assert result.checks["free_space_checked"] is True


class TestPreflightWriteComposite:
    def test_merged_checks_carry_both_halves(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(200 * GIB))
        result = preflight_write(str(tmp_path), 1 * GIB, require_mount=False)
        assert result.ok, result.reason
        for key in ("is_mount", "fstype", "free_bytes", "would_remain",
                    "min_headroom", "free_space_checked"):
            assert key in result.checks, key

    def test_space_failure_surfaces_through_composite(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(15 * GIB))
        result = preflight_write(str(tmp_path), 10 * GIB, require_mount=False)
        assert result.failed
        assert "insufficient space" in result.reason
        assert result.checks["free_space_checked"] is True

    def test_defaults_require_a_mount(self, tmp_path):
        assert preflight_write(str(tmp_path)).failed

    def test_nonexistent_parts_dir_is_evaluated_via_its_parent(self, tmp_path,
                                                               monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(200 * GIB))
        target = tmp_path / "acct" / "2026-06-16-04-01-04"
        result = preflight_write(str(target), 1 * GIB, require_mount=False)
        assert result.ok, result.reason
        assert result.checks["resolved"] == str(tmp_path)


# --------------------------------------------------------------------------
# Helpers / purity
# --------------------------------------------------------------------------
class TestHelpers:
    def test_nearest_existing_walks_up(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        assert nearest_existing(str(deep)) == str(tmp_path)

    def test_nearest_existing_returns_path_when_it_exists(self, tmp_path):
        assert nearest_existing(str(tmp_path)) == str(tmp_path)

    def test_disk_free_works_on_the_real_platform(self, tmp_path):
        """Whichever branch this platform takes, it must return usable numbers
        and never raise — Windows has no os.statvfs."""
        probe = disk_free(str(tmp_path))
        assert probe["source"] in {"statvfs", "disk_usage"}
        assert probe["error"] is None
        assert isinstance(probe["free_bytes"], int) and probe["free_bytes"] > 0

    def test_disk_free_on_missing_path_does_not_raise(self, tmp_path):
        probe = disk_free(str(tmp_path / "definitely-absent"))
        assert probe["free_bytes"] is None or probe["free_bytes"] >= 0


class TestPurity:
    def test_preflight_write_creates_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(200 * GIB))
        before = sorted(os.listdir(tmp_path))
        preflight_write(str(tmp_path / "parts"), 10 * GIB, require_mount=False)
        preflight_write(str(tmp_path), 10 * GIB, require_mount=True)
        assert sorted(os.listdir(tmp_path)) == before
        assert not (tmp_path / "parts").exists()

    def test_repeated_calls_are_stable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preflight, "disk_free", fake_probe(200 * GIB))
        results = [preflight_write(str(tmp_path), 1 * GIB, require_mount=False)
                   for _ in range(5)]
        assert all(r.ok for r in results)
        assert len({r.reason for r in results}) == 1
