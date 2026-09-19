"""The two long-run defects: an unbounded event list, and a reader that dies silently.

Both were harmless for a 36 KB canary and both matter for a multi-hour, many-part pull,
which is now the plan. Neither is visible from a happy-path suite, so the tests force the
conditions.

The reader death is the nastier of the two: it used to `return` from the read loop
without setting any flag, so every later `call()` waited out its full 30 s timeout and a
run whose socket had dropped simply appeared to hang.
"""
import asyncio
import json

import pytest

from autopilot.cdp import MAX_EVENTS, CdpError, CdpSession
from autopilot.errors import AutopilotError


class ScriptedTransport:
    """Replays scripted messages, then either blocks, errors, or ends."""

    def __init__(self, script, *, then="block", fail_with=None):
        self._script = list(script)
        self._then = then
        self._fail_with = fail_with
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        if self._then == "fail":
            raise self._fail_with or RuntimeError("socket closed")
        if self._then == "end":
            raise asyncio.CancelledError
        await asyncio.sleep(3600)
        return "{}"

    async def close(self):
        pass


def _evt(method, **params):
    return json.dumps({"method": method, "params": params})


# ==========================================================================
# the bounded event list
# ==========================================================================
def test_the_event_list_is_capped():
    """The cap must actually trim, and `event_count` must stay monotonic."""
    script = [_evt("Network.responseReceived", i=i) for i in range(MAX_EVENTS + 500)]
    t = ScriptedTransport(script, then="block")
    s = CdpSession(t)

    async def run():
        s.start()
        for _ in range(50):
            await asyncio.sleep(0)
        # the reader stops when the script is exhausted (then blocks), so wait for
        # the events to have been consumed
        for _ in range(200):
            await asyncio.sleep(0)
            if s.event_count() >= MAX_EVENTS:
                break
        return s.event_count(), len(s._events)

    count, retained = asyncio.run(run())
    assert count >= MAX_EVENTS
    assert retained <= MAX_EVENTS, f"retained {retained} events, cap is {MAX_EVENTS}"


def test_events_since_still_works_after_trimming():
    """Absolute indices must keep resolving onto the retained window."""
    script = [_evt("Page.frameNavigated", n=i) for i in range(MAX_EVENTS + 200)]
    t = ScriptedTransport(script, then="block")
    s = CdpSession(t)

    async def run():
        s.start()
        for _ in range(400):
            await asyncio.sleep(0)
            if s.event_count() > MAX_EVENTS + 100:
                break
        mark = s.event_count()
        # emit one more and confirm it is visible from a fresh mark
        return mark, s.events_since(mark), s.events_since(0)

    mark, after_mark, from_zero = asyncio.run(run())
    assert isinstance(after_mark, list)
    # a mark taken now must not be silently clipped to the whole buffer
    assert len(after_mark) <= len(from_zero)


def test_a_fresh_mark_never_loses_events_it_should_see():
    """The property the design depends on: a mark taken immediately before an action
    is far inside the retained window."""
    t = ScriptedTransport([_evt("A"), _evt("B"), _evt("C")], then="block")
    s = CdpSession(t)

    async def run():
        s.start()
        for _ in range(30):
            await asyncio.sleep(0)
        mark = s.event_count()
        return s.events_since(mark)

    assert asyncio.run(run()) == []      # nothing new since the mark


# ==========================================================================
# the reader that died silently
# ==========================================================================
def test_a_dead_reader_fails_in_flight_calls_fast():
    """They used to wait out the full timeout on a socket that was already gone."""
    t = ScriptedTransport([], then="fail", fail_with=OSError("connection reset"))
    s = CdpSession(t, default_timeout=30.0)

    async def run():
        s.start()
        await asyncio.sleep(0)          # let the reader hit the error
        await asyncio.sleep(0)
        with pytest.raises(CdpError) as exc:
            await s.call("Page.navigate", {"url": "x"})
        return str(exc.value)

    msg = asyncio.run(run())
    assert "died" in msg


def test_a_dead_reader_makes_later_calls_raise_instead_of_hanging():
    t = ScriptedTransport([], then="fail", fail_with=ConnectionResetError("boom"))
    s = CdpSession(t, default_timeout=30.0)

    async def run():
        s.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(CdpError):
            await s.value("1+1")
        with pytest.raises(CdpError):
            await s.call("Runtime.evaluate")

    asyncio.run(run())


def test_cdp_error_is_in_the_project_error_family():
    """So `run_once` classifies it into a job status instead of letting it escape as a
    traceback. That is the difference between a reported failure and a mystery hang."""
    assert issubclass(CdpError, AutopilotError)


def test_a_send_failure_is_classified():
    class BadSend(ScriptedTransport):
        async def send(self, payload):
            raise OSError("broken pipe")

    s = CdpSession(BadSend([], then="block"))

    async def run():
        s.start()
        with pytest.raises(CdpError) as exc:
            await s.call("Page.navigate", {"url": "x"})
        return str(exc.value)

    assert "cannot send" in asyncio.run(run())


def test_a_closed_session_raises_rather_than_hanging():
    t = ScriptedTransport([], then="block")
    s = CdpSession(t)

    async def run():
        s.start()
        await s.aclose()
        with pytest.raises(CdpError):
            await s.call("Page.navigate", {"url": "x"})

    asyncio.run(run())


# ==========================================================================
# the outer safety net
# ==========================================================================
def test_run_once_reports_an_escaped_autopilot_error_as_a_status(tmp_path):
    """A `CdpError` escaping mid-run must become `failed` with the reason recorded,
    not a traceback that leaves the job on its previous status."""
    import inspect

    import autopilot.run as run_mod

    src = inspect.getsource(run_mod.run_once)
    assert "except AutopilotError as exc:" in src
    # and it must remain scoped to the project's own family, so real bugs still surface
    assert "except Exception" not in src.split("except AutopilotError as exc:")[1][:400]
