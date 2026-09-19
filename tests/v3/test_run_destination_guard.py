"""The destination-mount guard must actually be APPLIED by the orchestrator.

`assert_destination_ready` in `mover.py` was implemented and unit-tested from the
start. `RunConfig.require_mount` existed and the CLI set it. But `run_once` never
called the guard, so `require_mount` was a dead field.

That left failure mode 1.7 unguarded on the only path that matters: when the
rclone daemon dies, the kernel turns `/opt/archives` into an ordinary empty
directory on a 13 GB root filesystem, and an unguarded mover writes terabytes into
it. That incident has already taken this server down once.

Unit tests could not see it, because they test the guard, not the wiring. This
file tests the wiring — the fourth bug of this shape in this project, a safety
mechanism that looks right and silently does nothing.

**The mount table is injected** (`monkeypatch` on `autopilot.ledger._read_mounts`)
because this workstation has no `/proc/mounts`: on Windows the real reader returns
`[]` and the guard deliberately degrades to a no-op rather than pretending to have
verified. Injecting the table is what makes the refusal observable here at all.
"""
from __future__ import annotations

import sys
from dataclasses import replace

ROOT = r"D:\_projects\takeout_downloader_script\.claude\worktrees\takeout-autopilot"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import autopilot.ledger as ledger_mod  # noqa: E402
from autopilot.run import run_once  # noqa: E402

from test_integration import go, scenario  # noqa: E402  (agent-written helpers)

NOT_A_MOUNT_TABLE = [("/", "ext4"), ("/opt/archives", "fuse.rclone")]


def test_the_guard_is_applied_when_require_mount_is_true(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_mod, "_read_mounts", lambda: NOT_A_MOUNT_TABLE)

    s = scenario(tmp_path, n_parts=1)
    dest = tmp_path / "not-a-mount"
    dest.mkdir()

    cfg = replace(s["cfg"], archive_dir=str(dest), require_mount=True)
    outcome = go({**s, "cfg": cfg})

    assert outcome.status == "failed", f"expected a refusal, got {outcome.status}"
    assert "FUSE mount" in (outcome.error or ""), outcome.error
    # the property that matters: refuse BEFORE transferring anything
    assert s["client"].requests == [], "must refuse before any transfer"
    assert list(dest.iterdir()) == [], "nothing may be written to the unverified path"


def test_the_escape_hatch_actually_waives_the_check(tmp_path, monkeypatch):
    """`require_mount=False` is the documented dev/test escape."""
    monkeypatch.setattr(ledger_mod, "_read_mounts", lambda: NOT_A_MOUNT_TABLE)

    s = scenario(tmp_path, n_parts=1)
    dest = tmp_path / "localdev"
    dest.mkdir()

    cfg = replace(s["cfg"], archive_dir=str(dest), require_mount=False)
    outcome = go({**s, "cfg": cfg})

    assert "FUSE mount" not in (outcome.error or ""), (
        "the escape hatch did not waive the mount check: " + str(outcome.error))


def test_a_real_mount_point_satisfies_the_guard(tmp_path, monkeypatch):
    """When the destination IS the mount, the run proceeds normally."""
    s = scenario(tmp_path, n_parts=1)
    dest = tmp_path / "is-a-mount"
    dest.mkdir()
    monkeypatch.setattr(ledger_mod, "_read_mounts",
                        lambda: [("/", "ext4"), (str(dest), "fuse.rclone")])

    cfg = replace(s["cfg"], archive_dir=str(dest), require_mount=True)
    outcome = go({**s, "cfg": cfg})

    assert "mount point" not in (outcome.error or ""), outcome.error
