"""Tests for takeout2.progress — throttled per-part progress events."""
from __future__ import annotations

import sqlite3

import pytest

from takeout2.progress import make_progress_emitter, ProgressAccumulator
from takeout2.state import JobStore


class FakeChunk:
    def __init__(self, idx, bytes_moved, wall_ms=20, net_ms=18, write_ms=2):
        self.idx = idx
        self.bytes_moved = bytes_moved
        self.wall_ms = wall_ms
        self.net_ms = net_ms
        self.write_ms = write_ms


@pytest.fixture
def store():
    return JobStore(sqlite3.connect(":memory:", check_same_thread=False))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class TestThrottling:
    def test_emits_once_per_window_per_part(self, store):
        clock = FakeClock()
        emitter = make_progress_emitter(store, "j-1", interval_ms=1500,
                                        now_fn=clock)
        # First chunk: immediate emit (row appears). Rest: same window, no emit.
        emitter(FakeChunk(0, 1 << 20))
        for _ in range(9):
            emitter(FakeChunk(0, 1 << 20))
        events = [e for e in store.events_since(0) if e.kind == "part_progress"]
        assert len(events) == 1
        assert events[0].data["size_on_disk"] == 1 << 20   # first-chunk value
        assert events[0].data["bytes_this_window"] == 1 << 20

    def test_emits_again_after_window_elapses(self, store):
        clock = FakeClock()
        emitter = make_progress_emitter(store, "j-1", interval_ms=1500,
                                        now_fn=clock)
        emitter(FakeChunk(0, 1 << 20))
        clock.advance(2.0)
        emitter(FakeChunk(0, 1 << 20))
        events = [e for e in store.events_since(0) if e.kind == "part_progress"]
        assert len(events) == 2

    def test_speed_is_computed_from_window(self, store):
        clock = FakeClock()
        emitter = make_progress_emitter(store, "j-1", interval_ms=1500,
                                        now_fn=clock)
        emitter(FakeChunk(0, 1 << 20))
        clock.advance(2.0)
        emitter(FakeChunk(0, 3 << 20))
        events = [e for e in store.events_since(0) if e.kind == "part_progress"]
        # Second event: 3 MiB over 2 s → 1.5 MiB/s
        assert events[1].data["speed_bps"] == int(3 * 1048576 / 2.0)
        assert events[1].data["bytes_this_window"] == 3 << 20
        assert events[1].data["window_ms"] == 2000

    def test_per_part_buckets_are_independent(self, store):
        clock = FakeClock()
        emitter = make_progress_emitter(store, "j-1", interval_ms=1500,
                                        now_fn=clock)
        emitter(FakeChunk(0, 1 << 20))
        emitter(FakeChunk(1, 2 << 20))
        events = [e for e in store.events_since(0) if e.kind == "part_progress"]
        assert len(events) == 2
        by_idx = {e.data["idx"]: e.data["size_on_disk"] for e in events}
        assert by_idx == {0: 1 << 20, 1: 2 << 20}

    def test_hard_floor_respects_min_interval(self, store):
        clock = FakeClock()
        emitter = make_progress_emitter(store, "j-1", interval_ms=10)
        for _ in range(5):
            emitter(FakeChunk(0, 1 << 10))
        # interval forced to >= 250ms; clock never advances -> 1 event
        events = [e for e in store.events_since(0) if e.kind == "part_progress"]
        assert len(events) == 1


class TestStateAndErrors:
    def test_state_carried_in_event(self, store):
        clock = FakeClock()
        emitter = make_progress_emitter(store, "j-1", interval_ms=1500,
                                        now_fn=clock)
        # Simulate the engine marking ACTIVE before the first progress.
        from takeout2.contracts import PartStatus
        store.update_part("j-1", 0, status=PartStatus.ACTIVE)
        emitter(FakeChunk(0, 1 << 20))
        events = [e for e in store.events_since(0) if e.kind == "part_progress"]
        assert events[0].data["state"] == PartStatus.ACTIVE.value
        assert events[0].data["verify"] == "UNVERIFIED"
        assert events[0].data["error"] is None

    def test_log_callback_fires(self, store):
        clock = FakeClock()
        lines = []
        emitter = make_progress_emitter(store, "j-1", interval_ms=1500,
                                        now_fn=clock, on_log=lines.append)
        emitter(FakeChunk(0, 1 << 20))
        assert any("part 0" in l for l in lines)


class TestProgressAccumulator:
    def test_bucket_fields(self):
        acc = ProgressAccumulator(now=5.0)
        assert acc.bytes_this_window == 0
        assert acc.window_started_at == 5.0
        assert acc.last_emit_at == 5.0
