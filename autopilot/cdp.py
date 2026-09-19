"""A small, transport-agnostic Chrome DevTools Protocol client.

Design notes, each traceable to a measurement:

1. **A concurrent reader is mandatory.** The first mint experiment failed
   because a command helper awaited its own reply while interception events were
   discarded, so the request stayed paused forever and the navigation hung. Here
   one background task owns the socket: replies are routed to futures, events
   are appended to a list.

2. **The `Fetch` domain is deliberately not exposed.** Aborting the redirector
   request *prevents the mint* — measured 2026-09-19: an aborted navigate bounced
   to the archive page instead of reaching the file host, while a plain navigate
   minted. This client can therefore only ever *observe*, never intercept. That
   is an invariant of the design, not an omission.

3. **The transport is injectable**, so the whole protocol is testable offline
   against scripted messages — the same dependency-injection discipline
   `takeout2/experiment.py` already uses. Only `ws_transport.py` touches a real
   socket.

No assumption is made about *which* websockets library version is present; the
adapter handles that.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable, Optional, Protocol

from .errors import AutopilotError

__all__ = ["Transport", "CdpSession", "CmdError", "CdpError", "MAX_EVENTS"]

#: How many events to retain. The reader used to append every event forever and nothing
#: ever trimmed: `run_once` opens ONE session for a whole run, `mint` takes a fresh mark
#: per hop, and each navigation emits dozens of `Network` events — so a many-part,
#: multi-hour export grew this list without bound for no benefit, since a mark is only
#: ever taken immediately before the action it measures.
#:
#: The cap is generous (a few MB of dicts) because trimming is only safe when the
#: caller's window is far smaller than the buffer, which it is: dozens of events versus
#: tens of thousands.
MAX_EVENTS = 50_000


class Transport(Protocol):
    """Anything with async send/recv of JSON text. See `ws_transport.py`."""

    async def send(self, payload: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class CmdError(RuntimeError):
    """A CDP command returned an `error` object."""

    def __init__(self, method: str, error: dict) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {error.get('message', error)}")


class CdpError(AutopilotError):
    """The session is unusable: the connection died, or it was closed.

    Deliberately part of the project's error family so `run_once` can turn it into a
    job status instead of a traceback. Raised the moment the reader task dies so that
    in-flight and subsequent calls FAIL FAST rather than each waiting out its own
    timeout — a run whose browser connection dropped should report that, not hang.

    Reconnecting is not attempted on purpose: a new CDP target has a different UUID,
    so resuming would silently re-attach to a possibly different page and continue
    against state nobody has verified. Reporting is the honest option.
    """


class CdpSession:
    """Observe-only CDP session over an injected transport.

    Typical use::

        transport = await WebSocketTransport.connect(browser_ws_url)
        async with CdpSession(transport) as session:
            await session.call("Network.enable")
            result = await mint(session, redirector_url)

    The session is *not* safe for concurrent `call()`s against the same id space
    from multiple tasks in a way that reorders semantics — but the reader makes
    it safe to call while events stream, which is the case that matters here.
    """

    def __init__(self, transport: Transport, *, default_timeout: float = 30.0) -> None:
        self._transport = transport
        self._default_timeout = default_timeout
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[tuple[str, dict]] = []
        #: Events trimmed off the front of `_events`. Kept so `event_count()` stays
        #: monotonic and `events_since(i)` can still map an absolute index onto the
        #: retained window.
        self._dropped = 0
        self._reader: Optional[asyncio.Task] = None
        self._closed = False
        #: Set when the reader dies. Any later call raises immediately.
        self._dead: Optional[BaseException] = None

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "CdpSession":
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def start(self) -> None:
        """Start the background reader. Idempotent."""
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def aclose(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        await self._transport.close()

    # -- reader ------------------------------------------------------------
    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                raw = await self._transport.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure here is fatal
                # This used to `return` silently. The consequence: `_closed` stayed
                # False and `_reader` non-None, so nothing restarted it and nothing
                # marked the session dead — every later `call()` simply waited out its
                # full timeout, and a run whose socket had dropped appeared to hang with
                # no diagnosis. Now the death is recorded, in-flight calls are failed
                # at once, and later calls raise without waiting.
                self._dead = exc
                self._fail_pending(exc)
                return
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if "id" in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                continue
            method = msg.get("method")
            if method:
                self._events.append((method, msg.get("params") or {}))
                if len(self._events) > MAX_EVENTS:
                    drop = len(self._events) - MAX_EVENTS
                    del self._events[:drop]
                    self._dropped += drop

    def _fail_pending(self, exc: BaseException) -> None:
        """Fail every in-flight call at once, so nobody waits out a timeout."""
        err = CdpError(f"the CDP reader died ({type(exc).__name__}: {exc}); "
                       "this session is unusable")
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    # -- commands ----------------------------------------------------------
    async def call(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict:
        """Send a command and await its reply. Raises `CmdError` on CDP error.

        Raises `CdpError` immediately if the session is closed or its reader has died —
        fail fast rather than waiting out a timeout on a socket that is already gone.
        """
        if self._closed:
            raise CdpError("the CDP session is closed")
        if self._dead is not None:
            raise CdpError(
                f"the CDP reader died earlier ({type(self._dead).__name__}: "
                f"{self._dead}); this session is unusable")
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._transport.send(
                json.dumps({"id": msg_id, "method": method, "params": params or {}})
            )
        except Exception as exc:  # noqa: BLE001 - a send failure is fatal for the session
            self._pending.pop(msg_id, None)
            self._dead = exc
            raise CdpError(
                f"cannot send {method} on the CDP connection "
                f"({type(exc).__name__}: {exc})") from exc
        try:
            reply = await asyncio.wait_for(
                fut, timeout=timeout if timeout is not None else self._default_timeout
            )
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise
        if "error" in reply:
            raise CmdError(method, reply["error"])
        return reply.get("result") or {}

    async def value(self, expression: str, *, timeout: Optional[float] = None) -> Any:
        """`Runtime.evaluate` an expression and return its value.

        Uses `awaitPromise` so the expression may return a Promise, and
        `returnByValue` so we get a plain value rather than a remote handle.
        """
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            return None
        return (result.get("result") or {}).get("value")

    # -- event observation -------------------------------------------------
    def event_count(self) -> int:
        """Number of events seen so far — record this before an action.

        Monotonic even after trimming: it counts events *observed*, not events retained.
        """
        return self._dropped + len(self._events)

    def events_since(self, index: int) -> list[tuple[str, dict]]:
        """Events observed since `index`. Index-based rather than draining, so
        nothing is lost if the reader consumed an event before we looked.

        `index` is an absolute count from `event_count()`, so it is translated onto the
        retained window here. Events trimmed between a mark and this call are gone —
        safe in practice because callers take a fresh mark immediately before the action
        they measure, and the window is tens of events against a cap of `MAX_EVENTS`.
        """
        start = index - self._dropped
        if start < 0:
            start = 0
        return self._events[start:]

    def find_events(
        self, index: int, *methods: str
    ) -> Iterable[tuple[str, dict]]:
        wanted = set(methods)
        for method, params in self.events_since(index):
            if method in wanted:
                yield method, params

    async def drain(self, seconds: float, *, tick: float = 0.1) -> None:
        """Let events arrive for `seconds` without issuing commands.

        Yields to the loop repeatedly rather than sleeping once, so the reader
        task makes progress even under a scripted/test transport.
        """
        remaining = seconds
        while remaining > 0:
            step = min(tick, remaining)
            await asyncio.sleep(step)
            remaining -= step
