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

__all__ = ["Transport", "CdpSession", "CmdError"]


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
        self._reader: Optional[asyncio.Task] = None
        self._closed = False

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
            except Exception:
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

    # -- commands ----------------------------------------------------------
    async def call(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict:
        """Send a command and await its reply. Raises `CmdError` on CDP error."""
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._transport.send(
            json.dumps({"id": msg_id, "method": method, "params": params or {}})
        )
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
        """Number of events seen so far — record this before an action."""
        return len(self._events)

    def events_since(self, index: int) -> list[tuple[str, dict]]:
        """Events observed since `index`. Index-based rather than draining, so
        nothing is lost if the reader consumed an event before we looked."""
        return self._events[index:]

    def find_events(
        self, index: int, *methods: str
    ) -> Iterable[tuple[str, dict]]:
        wanted = set(methods)
        for method, params in self._events[index:]:
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
