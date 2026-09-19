"""Real-socket transport for `autopilot.cdp`.

The only module in the package that imports a websocket library. Everything else
takes a transport, which is what makes the mint protocol testable offline.

Version note: the container ships `websockets` 16.x and the workstation 13.x.
Both expose an async-context-manager `connect()` returning an object with
`send`/`recv`/`close`, which is all this adapter needs.
"""
from __future__ import annotations

from typing import Any

__all__ = ["WebSocketTransport", "browser_endpoint"]


class WebSocketTransport:
    """Adapts a `websockets` connection to `autopilot.cdp.Transport`."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, payload: str) -> None:
        await self._ws.send(payload)

    async def recv(self) -> str:
        data = await self._ws.recv()
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return data

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass

    @classmethod
    async def connect(cls, url: str, *, max_size: int = 2 ** 24,
                      open_timeout: float = 15.0) -> "WebSocketTransport":
        import websockets  # local import: keeps the package import-light

        ws = await websockets.connect(url, max_size=max_size,
                                      open_timeout=open_timeout)
        return cls(ws)


def browser_endpoint(version_json: dict) -> str:
    """Pull the browser-level CDP websocket URL out of `/json/version`.

    **Measured trap:** the bare authority `ws://127.0.0.1:9222` is rejected by
    Chrome with `HTTP 404` — there is no websocket endpoint at the bare host and
    port. The URL must carry the `/devtools/browser/<uuid>` path, and **that UUID
    changes on every browser restart**, so it can never be hardcoded. Always
    discover it here.
    """
    url = (version_json or {}).get("webSocketDebuggerUrl")
    if not url:
        raise ValueError("no webSocketDebuggerUrl in /json/version response")
    return url
