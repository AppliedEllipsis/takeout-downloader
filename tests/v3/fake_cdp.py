"""Scripted CDP transport for offline tests.

This is the whole reason `autopilot.cdp` takes a transport: the mint protocol can
be tested against a *recording* of what Chrome does — no browser, no network, no
credentials, and **no third-party pytest plugin**. It mirrors the
dependency-injection discipline `takeout2/experiment.py` already used for the
attempt-cost study.

A "plan" maps a navigated URL to the `Network` events Chrome would emit for it.
Every command gets `{"result": {}}` except `Runtime.evaluate`, which pops the next
scripted value.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Optional


def redirect_event(from_url: str, to_url: str, status: int = 302) -> dict:
    """`Network.requestWillBeSent` carrying a `redirectResponse` (form A)."""
    return {
        "method": "Network.requestWillBeSent",
        "params": {
            "request": {"url": to_url},
            "redirectResponse": {
                "url": from_url,
                "status": status,
                "headers": {"Location": to_url},
            },
        },
    }


def response_event(url: str, status: int = 200, location: Optional[str] = None) -> dict:
    """`Network.responseReceived` (form B)."""
    headers = {"Content-Type": "application/octet-stream"}
    if location:
        headers["Location"] = location
    return {
        "method": "Network.responseReceived",
        "params": {"response": {"url": url, "status": status, "headers": headers}},
    }


class FakeTransport:
    """Records every outbound message; replays scripted events on navigate."""

    def __init__(self, plan: Optional[dict] = None,
                 values: Optional[list] = None,
                 command_results: Optional[dict] = None) -> None:
        self.plan = plan or {}
        #: method name -> result dict, for commands the tests need real answers to
        #: (e.g. Storage.getCookies). Everything else replies `{}`.
        self.command_results = command_results or {}
        self.sent: list[str] = []
        self._outbox: Optional[asyncio.Queue] = None
        self._values = deque(values or [])
        self.closed = False

    def _queue(self) -> asyncio.Queue:
        # Created lazily so the queue binds to whichever loop is running.
        if self._outbox is None:
            self._outbox = asyncio.Queue()
        return self._outbox

    # -- Transport protocol -------------------------------------------------
    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        msg = json.loads(payload)
        method = msg.get("method", "")
        params = msg.get("params") or {}
        reply: dict[str, Any] = {"id": msg.get("id"), "result": {}}

        if method == "Page.navigate":
            for event in self.plan.get(params.get("url", ""), []):
                await self._queue().put(json.dumps(event))
        elif method == "Runtime.evaluate":
            value = self._values.popleft() if self._values else None
            reply["result"] = {"result": {"type": "string", "value": value}}
        elif method in self.command_results:
            reply["result"] = self.command_results[method]

        # The reply is queued AFTER the events, so the reader resolves an
        # in-flight command only once its events have been seen — the ordering a
        # real browser exhibits.
        await self._queue().put(json.dumps(reply))

    async def recv(self) -> str:
        return await self._queue().get()

    async def close(self) -> None:
        self.closed = True

    # -- assertion helpers --------------------------------------------------
    def methods(self) -> list[str]:
        return [json.loads(s).get("method", "") for s in self.sent]

    def navigated(self) -> list[str]:
        return [json.loads(s)["params"]["url"] for s in self.sent
                if json.loads(s).get("method") == "Page.navigate"]
