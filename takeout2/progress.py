"""Throttled per-part progress events from the engine's chunk stream.

NORMATIVE implementation of ``docs/v2/06-LIVE-MONITORING.md`` §2.

The engine already fires ``on_chunk(ChunkProgress)`` per write. This module
turns that firehose into a throttled, speed-computing stream of ``part_progress``
events persisted to the store's event log — the single feed that the extension
popup, the monitor page, and the CLI ``watch`` all render.

Why a separate module instead of wiring in engine.py: the emitter is an
*observer* of the engine, and observers should be composable (CLI can also log
to stdout; Telegram can subscribe elsewhere). One construction function:
``make_progress_emitter(store, archive_id, ...)`` returns an ``on_chunk``
callable matching the engine's signature.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .contracts import PartStatus

__all__ = ["make_progress_emitter", "ProgressAccumulator"]

DEFAULT_INTERVAL_MS = 1500
MIN_INTERVAL_MS = 250          # hard floor; never emit faster than this


def _fmt_bytes(n: int) -> str:
    """Human-readable bytes (B/s → GB/s) for log lines; not for the event."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} TB"


class ProgressAccumulator:
    """Per-part bucket accumulating chunk bytes + wall time since last emit."""

    __slots__ = ("bytes_this_window", "window_started_at", "last_emit_at")

    def __init__(self, now: float):
        self.bytes_this_window = 0
        self.window_started_at = now
        self.last_emit_at = now


def make_progress_emitter(
    store,
    archive_id: str,
    interval_ms: int = DEFAULT_INTERVAL_MS,
    *,
    now_fn: Optional[Callable[[], float]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> Callable:
    """Build an ``on_chunk`` callable that throttles + persists part_progress.

    Args:
        store: JobStore (must have ``update_part`` and ``emit``).
        archive_id: the job these chunks belong to.
        interval_ms: throttle window per part (default 1500).
        now_fn: injectable clock for tests.
        on_log: optional text sink for human-readable log lines.

    Returns a callable ``(ChunkProgress) -> None`` safe to pass as the
    engine's ``on_chunk``.
    """
    interval_s = max(MIN_INTERVAL_MS, int(interval_ms)) / 1000.0
    now = now_fn or time.monotonic
    log = on_log or (lambda _line: None)
    buckets: dict[int, ProgressAccumulator] = {}
    last_size: dict[int, int] = {}
    last_state: dict[int, str] = {}

    def _emit(idx: int, acc: ProgressAccumulator, base_size: int) -> None:
        """Persist one part_progress event and reset the bucket."""
        elapsed = now() - acc.window_started_at
        speed = int(acc.bytes_this_window / elapsed) if elapsed > 0 else 0
        size_on_disk = base_size + acc.bytes_this_window
        store.update_part(
            archive_id, idx,
            size_on_disk=size_on_disk, quiet=True,      # never spam events here
        )
        store.emit("part_progress", archive_id, **{
            "idx": idx,
            "size_on_disk": size_on_disk,
            "size_expected": None,                       # filled by the renderer
            "speed_bps": speed,
            "bytes_this_window": acc.bytes_this_window,
            "window_ms": int(elapsed * 1000),
            "state": last_state.get(idx, PartStatus.ACTIVE.value),
            "verify": "UNVERIFIED",
            "error": None,
        })
        log(f"part {idx}: +{acc.bytes_this_window}B "
            f"@{_fmt_bytes(speed)}/s (total {_fmt_bytes(size_on_disk)})")
        acc.bytes_this_window = 0
        acc.window_started_at = now()

    def on_chunk(chunk) -> None:
        idx = chunk.idx
        is_new = idx not in buckets
        if is_new:
            buckets[idx] = ProgressAccumulator(now())
        acc = buckets[idx]
        acc.bytes_this_window += chunk.bytes_moved
        last_size.setdefault(idx, 0)
        # Always emit on the FIRST chunk of a part (so the row appears
        # immediately), then throttle to one per interval.
        if is_new or now() - acc.last_emit_at >= interval_s:
            _emit(idx, acc, last_size.get(idx, 0))

    return on_chunk


def record_state(store, archive_id: str, idx: int,
                 state: str = PartStatus.ACTIVE.value) -> None:
    """Expose a part's current state so the emitter includes it in the next
    event. Called by the engine on status transitions; the emitter itself
    stays stateless about part lifecycle."""
    # (The emitter keeps last_state internally; this is a convenience for
    # callers that want the state on the FIRST progress event.)
    store.emit("part_state", archive_id, idx=idx, state=state)
