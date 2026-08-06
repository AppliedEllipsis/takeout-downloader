"""Tests for takeout2.cookie — live CDP jar pull with a fake CDP server."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from takeout2.cookie import CookieError, CookieState, LiveCookieJar

FAKE_COOKIES = [
    {"name": "SID", "value": "AAA", "domain": ".google.com"},
    {"name": "HSID", "value": "BBB", "domain": ".google.com"},
    {"name": "NID", "value": "CCC", "domain": ".google.com"},
    {"name": "some_other_site", "value": "DDD", "domain": ".example.com"},
]


class FakeCDP:
    """Minimal websocket server that answers Storage.getCookies."""

    def __init__(self, cookies=None):
        self.cookies = cookies if cookies is not None else FAKE_COOKIES
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def _serve(self):
        import base64
        import hashlib
        conn, _ = self.sock.accept()
        # Read the HTTP upgrade request.
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        # Compute the RFC 6455 accept key from the client's Sec-WebSocket-Key.
        key = ""
        for line in data.decode("latin1", "replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode())
            .digest()).decode()
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                     b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
        # Wait for the text frame.
        frame = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            frame += chunk
            if len(frame) >= 2:
                break
        # Parse frame: 0x81 <len> <payload>  (len<126 single byte).
        if len(frame) >= 2:
            payload_len = frame[1] & 0x7F
            payload = frame[2:2 + payload_len]
        else:
            payload = b""
        reply = json.dumps({"id": 1, "result": {"cookies": self.cookies}}).encode()
        # Server -> client frame (unmasked). Support payloads > 125 bytes.
        if len(reply) < 126:
            header = bytes([0x81, len(reply)])
        elif len(reply) < 65536:
            header = bytes([0x81, 126]) + len(reply).to_bytes(2, "big")
        else:
            header = bytes([0x81, 127]) + len(reply).to_bytes(8, "big")
        conn.sendall(header + reply)
        conn.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def fake_cdp():
    srv = FakeCDP()
    yield srv
    srv.close()


class TestJarPull:
    def test_pulls_google_cookies_only(self, fake_cdp):
        jar = LiveCookieJar(fake_cdp.url())
        state = jar.pull()
        assert isinstance(state, CookieState)
        assert state.n_cookies == 3          # example.com excluded
        assert "SID=AAA" in state.header
        assert "some_other_site" not in state.header
        assert state.google_domains == [".google.com"]

    def test_header_is_joinable(self, fake_cdp):
        jar = LiveCookieJar(fake_cdp.url())
        state = jar.pull()
        # Cookie header shape for requests/curl.
        parts = state.header.split("; ")
        assert all("=" in p for p in parts)

    def test_freshness_tracks_age(self, fake_cdp):
        jar = LiveCookieJar(fake_cdp.url())
        state = jar.pull()
        assert state.age_s < 1
        assert state.fresh
        # A state we never wait on stays fresh; stale only crosses at the
        # idle limit — enforced by CookieStale in cookie.py, tested below.

    def test_fresh_method_raises_when_too_old(self, fake_cdp):
        jar = LiveCookieJar(fake_cdp.url())
        with pytest.raises(Exception) as exc:
            jar.fresh(max_age_s=-1)   # pull is older than any allowed age
        assert exc.type.__name__ == "CookieStale"


class TestRejectsNonLocalhost:
    @pytest.mark.parametrize("url", [
        "ws://10.0.0.5:9222",
        "ws://example.com:9222",
    ])
    def test_non_localhost_host_rejected(self, url):
        with pytest.raises(CookieError, match="non_localhost"):
            LiveCookieJar(url)


class TestNoCookies:
    def test_empty_jar_raises_actionable_error(self, fake_cdp):
        fake_cdp.cookies = [{"name": "X", "value": "Y", "domain": ".example.com"}]
        jar = LiveCookieJar(fake_cdp.url())
        with pytest.raises(CookieError, match="no_google_cookies"):
            jar.pull()
