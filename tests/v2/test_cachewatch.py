"""Tests for takeout2.cachewatch — bounded, cheap, network-free.

The subject is the rclone ``--vfs-cache-mode full`` write-back cache: if it
fills to ``--vfs-cache-max-size`` the mount blocks writes forever, so these
tests pin the exact threshold boundaries, the UNKNOWN-never-pauses safety rule,
and the PAUSE hysteresis latch.

Real directory trees in ``tmp_path`` (not fakes) so the walk is exercised
against genuine dirents on both Windows and Linux.
"""
from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest

from takeout2.cachewatch import (DEFAULT_MAX_BYTES, PAUSE_RATIO, RESUME_RATIO,
                                 WARN_RATIO, CacheState, CacheStatus,
                                 measure_cache_bytes, next_state,
                                 read_cache_status)


def write_file(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


@pytest.fixture
def cache_tree(tmp_path):
    """A nested cache dir totalling exactly 6144 bytes."""
    root = tmp_path / "rclone_vfs"
    write_file(root / "part-001.zip", 1024)
    write_file(root / "part-002.zip", 2048)
    write_file(root / "vfs" / "crypt" / "part-003.zip", 3072)
    (root / "empty_dir").mkdir()
    return root


class TestMeasureCacheBytes:
    def test_nested_tree_totals_exactly(self, cache_tree):
        assert measure_cache_bytes(str(cache_tree)) == 1024 + 2048 + 3072

    def test_absent_dir_returns_none(self, tmp_path):
        assert measure_cache_bytes(str(tmp_path / "no_such_cache")) is None

    def test_empty_dir_is_zero_not_none(self, tmp_path):
        """Distinguishing 0 from None matters: 0 means measured-and-empty (OK),
        None means unmeasurable (UNKNOWN, must not pause)."""
        (tmp_path / "empty").mkdir()
        assert measure_cache_bytes(str(tmp_path / "empty")) == 0

    def test_file_path_instead_of_dir_returns_none(self, tmp_path):
        path = write_file(tmp_path / "not_a_dir", 10)
        assert measure_cache_bytes(str(path)) is None


class TestBoundedWalk:
    def test_stops_early_and_still_returns_a_number(self, tmp_path):
        """A 2-vCPU box must never walk millions of inodes mid-download."""
        root = tmp_path / "big"
        root.mkdir()
        for i in range(40):
            write_file(root / f"f{i:03d}.bin", 100)

        full = measure_cache_bytes(str(root))
        assert full == 40 * 100

        bounded = measure_cache_bytes(str(root), max_entries=5)
        assert isinstance(bounded, int)
        assert 0 <= bounded < full

    def test_bound_applies_across_subdirectories(self, tmp_path):
        root = tmp_path / "deep"
        for d in range(6):
            for f in range(6):
                write_file(root / f"d{d}" / f"f{f}.bin", 50)

        full = measure_cache_bytes(str(root))
        assert full == 36 * 50
        assert measure_cache_bytes(str(root), max_entries=4) < full

    def test_under_report_is_the_safe_direction(self, tmp_path):
        """Truncating the walk can only lower the ratio, i.e. make us less
        likely to pause — never spuriously wedge a healthy transfer."""
        root = tmp_path / "safe"
        root.mkdir()
        for i in range(20):
            write_file(root / f"f{i}.bin", 1000)
        assert measure_cache_bytes(str(root), max_entries=3) <= \
            measure_cache_bytes(str(root))


class TestErrorTolerance:
    def _scandir_with_bad_entry(self, bad_name):
        real_scandir = os.scandir

        class BadEntry:
            def __init__(self, entry):
                self._entry = entry

            @property
            def name(self):
                return self._entry.name

            @property
            def path(self):
                return self._entry.path

            def is_dir(self, follow_symlinks=True):
                return self._entry.is_dir(follow_symlinks=follow_symlinks)

            def is_file(self, follow_symlinks=True):
                return self._entry.is_file(follow_symlinks=follow_symlinks)

            def stat(self, follow_symlinks=True):
                raise PermissionError(13, "Permission denied")

        class Wrapper:
            def __init__(self, path):
                self._it = real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._it.close()
                return False

            def __iter__(self):
                for entry in self._it:
                    if entry.name == bad_name:
                        yield BadEntry(entry)
                    else:
                        yield entry

        return Wrapper

    def test_permission_error_on_one_entry_is_skipped(self, tmp_path, monkeypatch):
        root = tmp_path / "cache"
        write_file(root / "readable.bin", 500)
        write_file(root / "locked.bin", 700)

        monkeypatch.setattr(os, "scandir", self._scandir_with_bad_entry("locked.bin"))
        # The locked file is skipped; the readable one still counts.
        assert measure_cache_bytes(str(root)) == 500

    def test_unreadable_subdirectory_is_skipped(self, tmp_path, monkeypatch):
        root = tmp_path / "cache"
        write_file(root / "top.bin", 400)
        write_file(root / "sub" / "inner.bin", 900)

        real_scandir = os.scandir

        def flaky_scandir(path):
            if os.path.basename(str(path)) == "sub":
                raise PermissionError(13, "Permission denied")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", flaky_scandir)
        assert measure_cache_bytes(str(root)) == 400

    def test_never_raises_on_a_vanishing_entry(self, tmp_path, monkeypatch):
        """rclone evicts cache files under us; a race must not kill the valve."""
        root = tmp_path / "cache"
        write_file(root / "gone.bin", 100)

        monkeypatch.setattr(os, "scandir", self._scandir_with_bad_entry("gone.bin"))
        assert measure_cache_bytes(str(root)) == 0


class TestStateThresholds:
    MAX = 1000

    def status(self, used, **kw):
        return read_cache_status("/irrelevant", max_bytes=self.MAX,
                                 measured=used, **kw)

    def test_zero_is_ok(self):
        assert self.status(0).state is CacheState.OK

    def test_just_below_warn_is_ok(self):
        assert self.status(699).state is CacheState.OK

    def test_exactly_warn_ratio_is_warn(self):
        s = self.status(int(self.MAX * WARN_RATIO))
        assert s.fill_ratio == pytest.approx(WARN_RATIO)
        assert s.state is CacheState.WARN

    def test_between_warn_and_pause_is_warn(self):
        assert self.status(800).state is CacheState.WARN

    def test_just_below_pause_is_warn(self):
        assert self.status(849).state is CacheState.WARN

    def test_exactly_pause_ratio_is_pause(self):
        s = self.status(int(self.MAX * PAUSE_RATIO))
        assert s.fill_ratio == pytest.approx(PAUSE_RATIO)
        assert s.state is CacheState.PAUSE
        assert s.should_pause is True

    def test_above_pause_is_pause(self):
        assert self.status(999).state is CacheState.PAUSE

    def test_over_cap_is_pause(self):
        assert self.status(self.MAX * 3).state is CacheState.PAUSE

    def test_custom_ratios_are_honoured(self):
        assert self.status(300, warn_ratio=0.2, pause_ratio=0.25).state is CacheState.PAUSE
        assert self.status(300, warn_ratio=0.2, pause_ratio=0.9).state is CacheState.WARN

    def test_ok_and_warn_do_not_pause(self):
        assert self.status(0).should_pause is False
        assert self.status(800).should_pause is False

    def test_detail_is_human_readable(self):
        assert "%" in self.status(900).detail


class TestUnknownIsSafe:
    def test_missing_dir_is_unknown(self, tmp_path):
        s = read_cache_status(str(tmp_path / "absent"))
        assert s.state is CacheState.UNKNOWN

    def test_unknown_must_not_pause(self, tmp_path):
        """THE safety rule: failing to measure the cache must never block the
        transfer. Being unable to protect it is not a reason to stop it."""
        s = read_cache_status(str(tmp_path / "absent"))
        assert s.should_pause is False
        assert s.fill_ratio == 0.0

    def test_unknown_detail_explains_itself(self, tmp_path):
        assert "not pausing" in read_cache_status(str(tmp_path / "absent")).detail

    def test_real_dir_is_measured_without_injection(self, cache_tree):
        s = read_cache_status(str(cache_tree), max_bytes=10_000)
        assert s.state is CacheState.OK
        assert s.bytes_used == 6144


class TestFillRatioMath:
    def test_basic_ratio(self):
        assert CacheStatus(CacheState.OK, 250, 1000, "").fill_ratio == 0.25

    def test_max_bytes_zero_does_not_divide_by_zero(self):
        s = CacheStatus(CacheState.OK, 500, 0, "")
        assert s.fill_ratio == 0.0

    def test_negative_max_bytes_is_also_guarded(self):
        assert CacheStatus(CacheState.OK, 500, -1, "").fill_ratio == 0.0

    def test_no_cap_never_pauses(self):
        """rclone without --vfs-cache-max-size has no wall to hit."""
        s = read_cache_status("/irrelevant", max_bytes=0, measured=10 ** 12)
        assert s.state is CacheState.OK
        assert s.should_pause is False

    def test_ratio_can_exceed_one(self):
        assert CacheStatus(CacheState.PAUSE, 2000, 1000, "").fill_ratio == 2.0


class TestHysteresis:
    MAX = 1000

    def status(self, used):
        return read_cache_status("/irrelevant", max_bytes=self.MAX, measured=used)

    def test_pause_latches_between_resume_and_pause(self):
        """Raw state says WARN (0.75), but we came from PAUSE and have not
        drained to RESUME_RATIO — stay paused or we flap every chunk."""
        s = self.status(750)
        assert s.state is CacheState.WARN
        assert next_state(CacheState.PAUSE, s) is CacheState.PAUSE

    def test_pause_latches_just_above_resume_ratio(self):
        s = self.status(int(self.MAX * RESUME_RATIO) + 1)
        assert next_state(CacheState.PAUSE, s) is CacheState.PAUSE

    def test_releases_exactly_at_resume_ratio(self):
        s = self.status(int(self.MAX * RESUME_RATIO))
        assert s.fill_ratio == pytest.approx(RESUME_RATIO)
        assert next_state(CacheState.PAUSE, s) is CacheState.OK

    def test_releases_below_resume_ratio(self):
        assert next_state(CacheState.PAUSE, self.status(100)) is CacheState.OK

    def test_ok_escalates_to_warn_and_pause_normally(self):
        assert next_state(CacheState.OK, self.status(750)) is CacheState.WARN
        assert next_state(CacheState.OK, self.status(900)) is CacheState.PAUSE
        assert next_state(CacheState.WARN, self.status(900)) is CacheState.PAUSE

    def test_warn_does_not_latch(self):
        """Only PAUSE is sticky; WARN is purely advisory."""
        assert next_state(CacheState.WARN, self.status(100)) is CacheState.OK

    def test_custom_resume_ratio(self):
        s = self.status(300)
        assert next_state(CacheState.PAUSE, s, resume_ratio=0.1) is CacheState.PAUSE
        assert next_state(CacheState.PAUSE, s, resume_ratio=0.5) is CacheState.OK

    def test_unknown_releases_a_latched_pause(self, tmp_path):
        unknown = read_cache_status(str(tmp_path / "absent"))
        assert next_state(CacheState.PAUSE, unknown) is CacheState.UNKNOWN

    def test_full_drain_cycle(self):
        """One realistic pass: fill to the wall, drain, resume."""
        state = CacheState.OK
        seen = []
        for used in (100, 720, 880, 870, 700, 610, 590, 200):
            state = next_state(state, self.status(used))
            seen.append(state)
        assert seen == [CacheState.OK, CacheState.WARN, CacheState.PAUSE,
                        CacheState.PAUSE, CacheState.PAUSE, CacheState.PAUSE,
                        CacheState.OK, CacheState.OK]


class TestImmutabilityAndConstants:
    def test_cache_status_is_frozen(self):
        s = CacheStatus(CacheState.OK, 1, 2, "")
        with pytest.raises(FrozenInstanceError):
            s.state = CacheState.PAUSE          # type: ignore[misc]

    def test_default_max_bytes_matches_the_mount_flag(self):
        assert DEFAULT_MAX_BYTES == 100 * 1024 ** 3

    def test_ratios_are_ordered(self):
        assert RESUME_RATIO < WARN_RATIO < PAUSE_RATIO < 1.0

    def test_state_values_are_normative_strings(self):
        assert CacheState.OK == "OK"
        assert CacheState.UNKNOWN.value == "UNKNOWN"
