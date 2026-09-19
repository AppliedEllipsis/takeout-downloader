"""Scripted HTTP client for offline transporter tests.

Lets the byte-moving logic be tested with no network and no server: give it a
status, headers and a body, and it behaves like the file host.
"""
from __future__ import annotations

from typing import Mapping, Optional


class FakeStream:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes,
                 truncate_body_after: Optional[int] = None) -> None:
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self._pos = 0
        #: simulate the connection dropping mid-body (an interrupted transfer)
        self._limit = truncate_body_after
        self.closed = False

    async def read(self, n: int) -> bytes:
        if self._limit is not None and self._pos >= self._limit:
            return b""
        end = self._pos + n
        if self._limit is not None:
            end = min(end, self._limit)
        block = self._body[self._pos:end]
        self._pos += len(block)
        return block

    async def aclose(self) -> None:
        self.closed = True


class FakeHttpClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    async def get(self, url: str, headers: Mapping[str, str]):
        self.requests.append((url, {k.lower(): v for k, v in headers.items()}))
        if not self._responses:
            raise AssertionError("FakeHttpClient ran out of scripted responses")
        return self._responses.pop(0)

    # -- assertions helpers -------------------------------------------------
    def last_headers(self) -> dict:
        return self.requests[-1][1] if self.requests else {}
