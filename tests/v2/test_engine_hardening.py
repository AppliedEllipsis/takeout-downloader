"""Tests for the multi-TB reliability hardening in engine.py.

These cover the paths that had NO coverage before and where a bug is
expensive rather than merely annoying:

* ``_real_fetch`` was never executed by any test, which let a
  ``self._session = None`` name collision shadow the ``_session()`` method.
  That would have raised "'NoneType' object is not callable" on the very
  first real request to Google — after an attempt had already been reserved
  against the irreplaceable 5-per-part budget.
* A torn tail on resume silently corrupts a 10 GiB part; you only discover it
  at extract time, days later.
* A trickling stream never trips the socket read timeout, so without the
  no-progress watchdog a multi-day transfer wedges forever holding an attempt.
"""
from __future__ import annotations

import io
import os

import pytest

from takeout2.contracts import ReasonCode
from takeout2.engine import (BurstEngine, EngineConfig, StallAbort,
                             aligned_resume_offset, zero_out_attempts)


# ---------------------------------------------------------------- alignment
class TestAlignedResumeOffset:
    def test_zero_and_negative_stay_zero(self):
        assert aligned_resume_offset(0, 8) == 0
        assert aligned_resume_offset(-5, 8) == 0

    def test_rewinds_to_aligned_boundary(self):
        # 25 MiB on disk, 8 MiB rewind -> trim to 16 MiB (aligned), not 17.
        mib = 1024 * 1024
        assert aligned_resume_offset(25 * mib, 8 * mib) == 16 * mib

    def test_small_file_rewinds_to_zero(self):
        # Less on disk than the rewind window: start over rather than trust it.
        assert aligned_resume_offset(1000, 8192) == 0

    def test_exact_multiple_drops_one_window(self):
        # A torn tail can land exactly on a boundary, so still re-fetch.
        assert aligned_resume_offset(16, 8) == 8

    def test_rewind_disabled_keeps_size(self):
        assert aligned_resume_offset(12345, 0) == 12345

    def test_result_is_never_greater_than_input(self):
        for size in (1, 7, 8, 9, 4096, 10 ** 7):
            assert aligned_resume_offset(size, 4096) <= size


# ---------------------------------------------------------------- session
class TestSessionNotShadowed:
    """The regression that would have crashed the first real download.

    Original bug: ``__init__`` did ``self._session = None`` while the class
    also defined a ``_session()`` method, so the instance attribute shadowed
    the method and ``self._session()`` raised "'NoneType' object is not
    callable" — on the real network path only, which every other test
    bypasses via an injected ``fetch=``. The fix is structural (the attribute
    is now ``_session_obj``), and the first test below is what keeps any
    future rename from reintroducing the collision.
    """

    def test_no_none_attribute_shadows_a_method(self):
        eng = _engine(tmp="ignored", make_dir=False)
        for name, value in vars(eng).items():
            klass_attr = getattr(type(eng), name, None)
            assert not (callable(klass_attr) and value is None), (
                f"instance attr {name!r}=None shadows method "
                f"{type(eng).__name__}.{name}() — calling it raises "
                f"'NoneType' object is not callable")

    def test_http_session_is_callable_and_cached(self):
        eng = _engine(tmp="ignored", make_dir=False)
        s1 = eng._http_session()
        assert s1 is not None
        assert eng._http_session() is s1, "session should be cached"

    def test_real_fetch_reaches_the_transport(self, tmp_path):
        """The coverage that was missing and let the crash hide."""
        eng = _engine(tmp_path)
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"q" * 32])))
        _reason, total = eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert total == 32

    def test_no_retry_session_cannot_retry(self):
        session = zero_out_attempts()
        adapter = session.get_adapter("https://takeout.google.com/")
        retries = adapter.max_retries
        assert retries.total == 0
        assert retries.connect == 0
        assert retries.read == 0
        assert retries.status == 0
        assert session.max_redirects == 0


# ---------------------------------------------------------------- fakes
class FakeRaw:
    """Minimal stand-in for urllib3's raw stream."""

    def __init__(self, chunks, head=b"PK\x03\x04"):
        self._head = head
        self._chunks = list(chunks)
        self.closed = False

    def read(self, n=-1):
        if self._head is not None:
            head, self._head = self._head, None
            return head
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeResp:
    def __init__(self, raw, status=206, headers=None, url=""):
        self.raw = raw
        self.status_code = status
        self.headers = headers or {"Content-Range": "bytes 0-9/10"}
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.last_headers = None
        self.last_url = None

    def get(self, url, headers=None, stream=None, timeout=None,
            allow_redirects=None):
        self.last_headers = headers
        self.last_url = url
        return self.resp


class FakePart:
    def __init__(self, idx=1, filename="takeout-001.zip", url="https://x/y",
                 size_expected=None):
        self.idx = idx
        self.filename = filename
        self.url = url
        self.size_expected = size_expected


def _engine(tmp, make_dir=True, config=None, now_fn=None):
    if make_dir:
        os.makedirs(tmp, exist_ok=True)
    return BurstEngine(store=None, ledger=None, cookie_source=None,
                       parts_dir=str(tmp), config=config, now_fn=now_fn)


# ---------------------------------------------------------------- transport
class TestRealFetchTransport:
    def test_requests_identity_encoding(self, tmp_path):
        """A gzip-encoding proxy would corrupt the .zip we write raw."""
        eng = _engine(tmp_path)
        resp = FakeResp(FakeRaw([b"data"]))
        eng._session_obj = FakeSession(resp)
        eng._real_fetch(FakePart(), 0, "COOKIE=1", write=None)
        assert eng._session_obj.last_headers["Accept-Encoding"] == "identity"

    def test_sends_range_header_when_resuming(self, tmp_path):
        eng = _engine(tmp_path)
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"tail"])))
        eng._real_fetch(FakePart(), 4096, "C=1", write=None)
        assert eng._session_obj.last_headers["Range"] == "bytes=4096-"

    def test_no_range_header_on_fresh_start(self, tmp_path):
        eng = _engine(tmp_path)
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"x"])))
        eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert "Range" not in eng._session_obj.last_headers

    def test_writes_bytes_and_reports_total(self, tmp_path):
        eng = _engine(tmp_path)
        chunks = [b"a" * 100, b"b" * 50]
        eng._session_obj = FakeSession(FakeResp(FakeRaw(chunks)))
        reason, total = eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert reason in (ReasonCode.OK_PARTIAL, ReasonCode.OK_COMPLETE)
        assert total == 150
        written = (tmp_path / "takeout-001.zip").read_bytes()
        # The 4 KiB classification sniff is consumed before the body loop, so
        # what lands on disk is exactly the streamed chunks.
        assert len(written) == 150

    def test_response_is_always_closed(self, tmp_path):
        eng = _engine(tmp_path)
        resp = FakeResp(FakeRaw([b"z"]))
        eng._session_obj = FakeSession(resp)
        eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert resp.closed is True

    def test_emits_progress_per_chunk(self, tmp_path):
        seen = []
        eng = _engine(tmp_path)
        eng.on_chunk = seen.append
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"a" * 10] * 3)))
        eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert len(seen) == 3
        assert all(p.bytes_moved == 10 for p in seen)
        assert all(p.idx == 1 for p in seen)

    def test_non_ok_classification_moves_no_bytes(self, tmp_path):
        eng = _engine(tmp_path)
        # 302 to accounts.google.com = dead cookie, not a range problem.
        resp = FakeResp(FakeRaw([b"nope"], head=b""), status=302,
                        headers={"Location": "https://accounts.google.com/x"},
                        url="https://accounts.google.com/x")
        eng._session_obj = FakeSession(resp)
        reason, total = eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert total == 0
        assert reason is not ReasonCode.OK_COMPLETE
        assert reason is not ReasonCode.OK_PARTIAL

    def test_missing_url_raises_before_any_request(self, tmp_path):
        eng = _engine(tmp_path)
        with pytest.raises(RuntimeError, match="no URL"):
            eng._real_fetch(FakePart(url=""), 0, "C=1", write=None)


# ---------------------------------------------------------------- watchdog
class TestStallWatchdog:
    def test_stall_abort_raised_when_no_progress(self, tmp_path):
        """A trickle must be killed; the socket timeout would never fire."""
        clock = {"t": 0.0}

        def fake_now():
            # Every call advances 100s, so idle time blows past the 30s limit.
            clock["t"] += 100.0
            return clock["t"]

        cfg = EngineConfig(stall_abort_s=30.0)
        eng = _engine(tmp_path, config=cfg, now_fn=fake_now)
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"x" * 10] * 50)))
        with pytest.raises(StallAbort) as err:
            eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert err.value.bytes_moved > 0
        assert err.value.idle_s >= 30.0

    def test_watchdog_disabled_by_zero(self, tmp_path):
        clock = {"t": 0.0}

        def fake_now():
            clock["t"] += 100.0
            return clock["t"]

        cfg = EngineConfig(stall_abort_s=0)
        eng = _engine(tmp_path, config=cfg, now_fn=fake_now)
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"x" * 10] * 3)))
        reason, total = eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert total == 30  # completed, never aborted

    def test_healthy_stream_is_not_aborted(self, tmp_path):
        cfg = EngineConfig(stall_abort_s=30.0)
        eng = _engine(tmp_path, config=cfg)  # real monotonic clock
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"y" * 100] * 5)))
        reason, total = eng._real_fetch(FakePart(), 0, "C=1", write=None)
        assert total == 500

    def test_partial_bytes_survive_a_stall_abort(self, tmp_path):
        """Aborting must not throw away what we already paid an attempt for."""
        clock = {"t": 0.0}

        def fake_now():
            clock["t"] += 100.0
            return clock["t"]

        cfg = EngineConfig(stall_abort_s=30.0)
        eng = _engine(tmp_path, config=cfg, now_fn=fake_now)
        eng._session_obj = FakeSession(FakeResp(FakeRaw([b"k" * 64] * 10)))
        with pytest.raises(StallAbort):
            eng._real_fetch(FakePart(), 0, "C=1", write=None)
        on_disk = (tmp_path / "takeout-001.zip")
        assert on_disk.exists()
        assert on_disk.stat().st_size > 0, "aborted bytes must be kept for resume"

    def test_stall_abort_carries_a_readable_message(self):
        err = StallAbort(2048, 195.0)
        assert "2048" in str(err)
        assert "195" in str(err)


# ---------------------------------------------------------------- config
class TestHardeningDefaults:
    def test_watchdog_and_rewind_are_on_by_default(self):
        cfg = EngineConfig()
        assert cfg.stall_abort_s > 0, "watchdog must default ON"
        assert cfg.resume_rewind > 0, "torn-tail rewind must default ON"
        assert cfg.stall_resume_attempts == 1, "exactly one resume, budget is 5"
        assert cfg.fsync_interval_s > 0

    def test_within_part_parallelism_still_forbidden(self):
        # N connections might cost N of the 5 attempts; unmeasured = forbidden.
        assert EngineConfig().max_streams_per_part == 1

    def test_require_mount_defaults_off_for_dev(self):
        assert EngineConfig().require_mount is False


# ---------------------------------------------------------------- backpressure
class TestCacheBackpressure:
    """The rclone VFS cache is 100 GB; one job pushes 630 GB through it."""

    def test_no_cache_dir_configured_never_blocks(self, tmp_path):
        eng = _engine(tmp_path)                      # cache_dir defaults None
        from takeout2.engine import BurstResult
        assert eng._wait_for_cache(BurstResult()) is True

    def test_healthy_cache_passes_immediately(self, tmp_path, monkeypatch):
        cache = tmp_path / "vfs"
        cache.mkdir()
        (cache / "small").write_bytes(b"x" * 10)
        cfg = EngineConfig(cache_dir=str(cache), cache_max_bytes=10_000)
        eng = _engine(tmp_path, config=cfg)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda s: pytest.fail("must not sleep"))
        from takeout2.engine import BurstResult
        assert eng._wait_for_cache(BurstResult()) is True

    def test_full_cache_pauses_then_gives_up(self, tmp_path, monkeypatch):
        """A wedged cache must end the burst, not hang forever."""
        cache = tmp_path / "vfs"
        cache.mkdir()
        (cache / "big").write_bytes(b"x" * 1000)
        cfg = EngineConfig(cache_dir=str(cache), cache_max_bytes=1000,
                           cache_wait_max_s=60)
        eng = _engine(tmp_path, config=cfg)
        slept = []
        monkeypatch.setattr("takeout2.engine.time.sleep", slept.append)
        from takeout2.engine import BurstResult
        ok = eng._wait_for_cache(BurstResult())
        assert ok is False, "should give up rather than wedge"
        assert slept, "should have waited for the cache to drain"

    def test_unmeasurable_cache_does_not_block(self, tmp_path):
        cfg = EngineConfig(cache_dir=str(tmp_path / "absent"))
        eng = _engine(tmp_path, config=cfg)
        from takeout2.engine import BurstResult
        assert eng._wait_for_cache(BurstResult()) is True


# ---------------------------------------------------------------- rate limit
class TestRateLimitBackoff:
    def test_rate_limited_sleeps_before_continuing(self, tmp_path, monkeypatch):
        eng = _engine(tmp_path)
        slept = []
        monkeypatch.setattr("takeout2.engine.time.sleep", slept.append)
        assert eng._maybe_backoff(ReasonCode.RATE_LIMITED, attempt=1) is True
        assert slept and slept[0] > 0, "a 429 must never be retried instantly"

    def test_gives_up_after_max_retries(self, tmp_path, monkeypatch):
        eng = _engine(tmp_path)
        monkeypatch.setattr("takeout2.engine.time.sleep", lambda s: None)
        assert eng._maybe_backoff(ReasonCode.RATE_LIMITED, attempt=99) is False

    def test_auth_failure_is_never_waited_on(self, tmp_path, monkeypatch):
        """A dead cookie does not heal by waiting; re-requesting costs an attempt.

        So: no sleep at all, and the burst stops (False) rather than opening
        more streams with a cookie we know is dead.
        """
        eng = _engine(tmp_path)
        monkeypatch.setattr("takeout2.engine.time.sleep",
                            lambda s: pytest.fail("must not sleep on auth failure"))
        assert eng._maybe_backoff(ReasonCode.AUTH_REDIRECT, attempt=1) is False

    def test_rate_limit_waits_but_keeps_going(self, tmp_path, monkeypatch):
        """Distinguishes 'wait then continue' from 'stop'."""
        eng = _engine(tmp_path)
        monkeypatch.setattr("takeout2.engine.time.sleep", lambda s: None)
        assert eng._maybe_backoff(ReasonCode.RATE_LIMITED, attempt=1) is True

    def test_success_does_not_sleep(self, tmp_path, monkeypatch):
        eng = _engine(tmp_path)
        monkeypatch.setattr("takeout2.engine.time.sleep",
                            lambda s: pytest.fail("must not sleep on success"))
        assert eng._maybe_backoff(ReasonCode.OK_COMPLETE, attempt=1) is True

    def test_backoff_can_be_disabled(self, tmp_path, monkeypatch):
        cfg = EngineConfig(rate_limit_backoff=False)
        eng = _engine(tmp_path, config=cfg)
        monkeypatch.setattr("takeout2.engine.time.sleep",
                            lambda s: pytest.fail("disabled means no sleep"))
        assert eng._maybe_backoff(ReasonCode.RATE_LIMITED, attempt=1) is True
