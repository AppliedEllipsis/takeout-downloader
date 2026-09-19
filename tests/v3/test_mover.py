"""Offline tests for the mover.

The two that matter:

* `test_refuses_a_destination_that_is_not_the_mount` — when the rclone FUSE daemon
  dies, the kernel turns `/opt/archives` into an ordinary empty directory **on the
  root filesystem** (~13 GB free). A mover without this check will happily write
  terabytes into it. That is failure mode 1.7 and it has taken this server down
  once.
* `test_plan_moves_is_pure` — planning must not touch the filesystem at all. The
  historical livelock was a per-part `stat()` pre-pass taking ~6 minutes on the
  FUSE mount; the anti-measure is one `scandir`, then pure planning.
"""
from __future__ import annotations

import os

import pytest

from autopilot.mover import (
    PARTIAL_SUFFIX,
    DestinationIndex,
    MoveRefused,
    assert_destination_ready,
    index_destination,
    is_mount_point,
    move_part,
    plan_moves,
)

MOUNTS = [("/", "ext4"), ("/opt/archives", "fuse.rclone"), ("/config", "xfs")]


# ---------------------------------------------------------------------------
# mount safety
# ---------------------------------------------------------------------------
def test_is_mount_point():
    assert is_mount_point("/opt/archives", MOUNTS) is True
    assert is_mount_point("/opt/archives/", MOUNTS) is True     # trailing slash
    assert is_mount_point("/opt/archives/sub/dir", MOUNTS) is False  # not the root itself
    assert is_mount_point("/config", MOUNTS) is True


def test_refuses_a_destination_that_is_not_the_mount(tmp_path):
    # exists, but is not listed as a mount point -> the mount is probably detached
    with pytest.raises(MoveRefused) as e:
        assert_destination_ready(str(tmp_path), mounts=MOUNTS, require_mount=True)
    assert "not a mount point" in str(e.value)


def test_refuses_a_destination_that_does_not_exist():
    with pytest.raises(MoveRefused):
        assert_destination_ready("/definitely/not/here", mounts=MOUNTS, require_mount=True)


def test_require_mount_false_allows_dev_and_tests(tmp_path):
    assert_destination_ready(str(tmp_path), mounts=MOUNTS, require_mount=False)


def test_a_real_mount_point_passes(tmp_path):
    # Use an injected table naming a directory that genuinely exists here —
    # /config is container-only and does not exist on the workstation.
    mounts = [(str(tmp_path), "fuse.rclone")]
    assert_destination_ready(str(tmp_path), mounts=mounts, require_mount=True)
    assert is_mount_point(str(tmp_path), mounts) is True


def test_no_mount_table_means_the_check_cannot_run():
    # On Windows there is no /proc/mounts; the guard must not claim success, but it
    # also must not block. It is simply unable to verify.
    assert_destination_ready(".", mounts=[], require_mount=True)


# ---------------------------------------------------------------------------
# indexing + pure planning
# ---------------------------------------------------------------------------
def test_index_destination_maps_name_to_size(tmp_path):
    (tmp_path / "a.zip").write_bytes(b"x" * 10)
    (tmp_path / "b.zip").write_bytes(b"y" * 25)
    os.mkdir(tmp_path / "subdir")           # directories are excluded
    idx = index_destination(str(tmp_path))
    assert idx.names == {"a.zip": 10, "b.zip": 25}
    assert idx.size_of("a.zip") == 10
    assert idx.size_of("nope") is None


def test_plan_moves_is_pure_and_classifies_each_case():
    idx = DestinationIndex(path="/dest", names={"present.zip": 100, "wrong.zip": 7})
    plan = plan_moves(
        [
            ("present.zip", "/stage/present.zip", 100),   # already there, same size
            ("wrong.zip", "/stage/wrong.zip", 100),       # present but different size
            ("new.zip", "/stage/new.zip", 100),           # absent
        ],
        idx,
    )
    assert [p.action for p in plan] == ["skip-present", "skip-size-mismatch", "move"]
    # nothing on disk called from here: the only I/O was the scandir that built idx


# ---------------------------------------------------------------------------
# the move
# ---------------------------------------------------------------------------
def test_move_part_copies_then_renames(tmp_path):
    stage = tmp_path / "stage"
    dest = tmp_path / "dest"
    stage.mkdir()
    dest.mkdir()
    src = stage / "p.zip"
    payload = b"takeout-bytes" * 1000
    src.write_bytes(payload)

    r = move_part(str(src), str(dest), expected_size=len(payload))

    assert r.action == "moved" and r.bytes_moved == len(payload)
    assert (dest / "p.zip").read_bytes() == payload
    # the temp name is gone
    assert not (dest / ("p.zip" + PARTIAL_SUFFIX)).exists()


def test_the_temp_suffix_is_not_v2s_partial_suffix(tmp_path):
    """A mover temp file must never be mistaken for a resumable v2 partial."""
    assert PARTIAL_SUFFIX == ".moving"
    assert PARTIAL_SUFFIX != ".partial"


def test_move_part_refuses_when_the_staged_size_disagrees(tmp_path):
    stage = tmp_path / "stage"
    dest = tmp_path / "dest"
    stage.mkdir()
    dest.mkdir()
    src = stage / "p.zip"
    src.write_bytes(b"x" * 10)

    r = move_part(str(src), str(dest), expected_size=999)

    assert r.action == "failed"
    assert "!=" in r.detail
    assert not (dest / "p.zip").exists(), "nothing may be renamed into place"


def test_move_part_reports_a_missing_source(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    r = move_part(str(tmp_path / "absent.zip"), str(dest), expected_size=1)
    assert r.action == "failed" and "stat source" in r.detail


def test_moving_is_idempotent_by_planning(tmp_path):
    """A re-run after a crash must skip what is already there."""
    stage = tmp_path / "stage"
    dest = tmp_path / "dest"
    stage.mkdir()
    dest.mkdir()
    src = stage / "p.zip"
    src.write_bytes(b"z" * 50)
    move_part(str(src), str(dest), expected_size=50)

    idx = index_destination(str(dest))
    plan = plan_moves([("p.zip", str(src), 50)], idx)
    assert plan[0].action == "skip-present"
