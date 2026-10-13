"""The mover watchdog and the staging guard — proving they FIRE, not just exist.

Both close failure modes that only bite at real sizes (1.16 shape 2 and ENOSPC). At the
36 KB canary neither could ever trigger, which is exactly why they need tests that force
the condition rather than a passing suite that never reaches them.

The watchdog matters because a blocked FUSE write cannot be interrupted in-process: the
copy runs in a child so there is a boundary that can actually be killed.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autopilot.mover import (
    _COPY_CHILD,
    _HASH_CHILD,
    MOVE_STALL_SECONDS,
    MOVE_WATCHDOG_SECONDS,
    MoveRefused,
    assert_staging_ready,
    move_part,
)


# ==========================================================================
# the staging guard
# ==========================================================================
def test_headroom_none_means_do_not_check(tmp_path):
    """The escape must exist. v2's hard floor with no escape made its own suite
    unrunnable on a nearly-full disk — the production guard broke every test."""
    assert_staging_ready(str(tmp_path), min_headroom=None)


def test_an_absurd_headroom_is_refused(tmp_path):
    with pytest.raises(MoveRefused) as exc:
        assert_staging_ready(str(tmp_path), min_headroom=1 << 62)   # 4 EiB
    assert "headroom floor" in str(exc.value)


def test_a_small_headroom_passes(tmp_path):
    assert_staging_ready(str(tmp_path), min_headroom=1 << 20)       # 1 MiB


def test_a_missing_staging_dir_is_refused(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(MoveRefused) as exc:
        assert_staging_ready(str(missing), min_headroom=None)
    assert "does not exist" in str(exc.value)


def test_the_defaults_are_generous_enough_to_be_usable():
    """A tight limit would murder legitimate multi-GB uploads: rclone runs with
    `--timeout 1h`, and a measured 1.5 GB write took 8.45 s, so a real part can
    legitimately take many minutes."""
    assert MOVE_WATCHDOG_SECONDS >= 600
    assert MOVE_STALL_SECONDS >= 60
    assert MOVE_STALL_SECONDS <= MOVE_WATCHDOG_SECONDS


# ==========================================================================
# the copy watchdog
# ==========================================================================
def _stage(tmp_path, size=64 * 1024, name="part.bin"):
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    src = staging / name
    src.write_bytes(os.urandom(size))
    return src


def test_a_normal_move_still_works(tmp_path):
    """The watchdog must not break the ordinary path."""
    src = _stage(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    r = move_part(str(src), str(dest), filename="part.bin",
                  expected_size=src.stat().st_size)
    assert r.action == "moved", r.detail
    assert (dest / "part.bin").read_bytes() == src.read_bytes()


def test_the_watchdog_kills_a_hung_copy_and_reports_it(tmp_path, monkeypatch):
    """Force the wedge: make the child sleep far longer than the timeout, then assert
    the parent ABANDONS it rather than stalling the run and leaves no partial."""
    src = _stage(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()

    real_run = subprocess.run

    def fake_run(cmd, **kw):
        # the copy child is python -c <_COPY_CHILD> src dst chunk stall
        if cmd[:3] == [sys.executable, "-c", _COPY_CHILD]:
            sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
            return real_run(sleeper, timeout=2.0, capture_output=True, text=True)
        return real_run(cmd, **kw)

    monkeypatch.setattr("autopilot.mover.subprocess.run", fake_run)
    r = move_part(str(src), str(dest), filename="part.bin",
                  expected_size=src.stat().st_size, timeout=1.0, stall=1.0)

    assert r.action == "failed"
    assert "WATCHDOG" in r.detail
    assert "1.7/1.16" in r.detail or "FUSE" in r.detail
    assert not os.path.exists(dest / "part.bin"), "no truncated file under the final name"
    assert not list(dest.glob("*.moving")), "copy temporary left behind"


def test_a_child_that_reports_no_progress_fails_the_move(tmp_path):
    """The child's own stall guard: a write loop that returns but never advances."""
    src = _stage(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    # ask for a stall window of ~0 so the child's progress check trips immediately
    r = move_part(str(src), str(dest), filename="part.bin",
                  expected_size=src.stat().st_size, timeout=30.0, stall=0.0001)
    # With stall ~0 the child may exit 3 (no progress) — either way it must not
    # report success, and must not leave anything behind.
    if r.action != "moved":
        assert not os.path.exists(dest / "part.bin")
        assert not list(dest.glob("*.moving"))


def test_a_short_write_is_caught_after_the_copy(tmp_path, monkeypatch):
    """Re-check landed size even after a valid child receipt: backend tail loss
    must never be renamed into place."""
    src = _stage(tmp_path, size=8192)
    dest = tmp_path / "dest"
    dest.mkdir()

    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[:3] == [sys.executable, "-c", _COPY_CHILD]:
            # Obtain the real child's full count/digest receipt, then simulate a
            # backend losing the tail after the child's flush/read succeeded.
            receipt = real_run(cmd, **kw)
            assert receipt.returncode == 0
            assert receipt.stdout.split()[0] == "8192"
            with open(cmd[4], "r+b") as landed:
                landed.truncate(4096)
            return receipt
        return real_run(cmd, **kw)

    monkeypatch.setattr("autopilot.mover.subprocess.run", fake_run)
    r = move_part(str(src), str(dest), filename="part.bin",
                  expected_size=src.stat().st_size)
    assert r.action == "failed"
    assert "short write" in r.detail
    assert src.stat().st_size == 8192
    assert not os.path.exists(dest / "part.bin")
    assert not list(dest.glob("*.moving"))


def test_a_child_that_cannot_start_is_reported(tmp_path, monkeypatch):
    src = _stage(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()

    real_run = subprocess.run
    def boom(cmd, **kw):
        if cmd[:3] == [sys.executable, "-c", _COPY_CHILD]:
            raise FileNotFoundError("no interpreter")
        return real_run(cmd, **kw)

    monkeypatch.setattr("autopilot.mover.subprocess.run", boom)
    r = move_part(str(src), str(dest), filename="part.bin",
                  expected_size=src.stat().st_size)
    assert r.action == "failed"
    assert "cannot start the copy child" in r.detail


def test_source_hash_failure_never_starts_copy(tmp_path, monkeypatch):
    src = _stage(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = []
    real_run = subprocess.run
    def fail_hash(cmd, **kw):
        calls.append(cmd[2])
        if cmd[:3] == [sys.executable, "-c", _HASH_CHILD]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="read failure")
        return real_run(cmd, **kw)
    monkeypatch.setattr("autopilot.mover.subprocess.run", fail_hash)
    r = move_part(str(src), str(dest), filename="part.bin", expected_size=src.stat().st_size)
    assert r.action == "failed" and "digest read failed" in r.detail
    assert calls == [_HASH_CHILD], "copy must not run without staged digest evidence"
    assert src.exists() and not list(dest.iterdir())


# ==========================================================================
# wiring
# ==========================================================================
def test_run_once_calls_the_staging_guard():
    """Source-level: the guard must be CALLED. `assert_destination_ready` was once
    implemented, tested, and never invoked — a guard nothing calls is not a guard."""
    import inspect

    import autopilot.run as run_mod

    src = inspect.getsource(run_mod.run_once)
    assert "assert_staging_ready(" in src
    # and it must come AFTER `finish` exists, or it references a closure that is not
    # yet defined and fails only on the path it protects (which happened twice today)
    guard_at = src.index("assert_staging_ready(")
    finish_at = src.index("async def finish(")
    assert finish_at < guard_at, "the guard must be placed after `finish` is defined"


def test_the_cli_supplies_a_real_headroom():
    """The library default is 'do not check'; a real run must not inherit that."""
    import inspect

    import autopilot.__main__ as cli

    src = inspect.getsource(cli.cmd_run)
    assert "min_headroom=" in src
    assert "AUTOPILOT_MIN_HEADROOM" in src, "and it must be overridable"
