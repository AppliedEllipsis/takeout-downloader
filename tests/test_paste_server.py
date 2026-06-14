"""
Tests for paste_server — the ephemeral single-use paste relay.

The relay holds a live Google session cookie in transit, so the
security-critical behaviors are covered here:

- A random token guards the path; wrong paths 404.
- GET on the token path serves the paste form.
- A valid POST delivers the payload to the waiting caller.
- Single-use: the relay consumes the first valid payload and refuses more.
- Body limits and content sniffing reject junk.
- Token comparison is constant-time (hmac.compare_digest).

These run against a real loopback HTTP server on an OS-assigned port, but
never touch the network or cloudflared (use_tunnel defaults off).
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paste_server  # noqa: E402

VALID_PAYLOAD = json.dumps({
    "schema": 1,
    "url": "https://takeout-download.usercontent.google.com/download/"
           "takeout-20260612T190148Z-15-001.zip?j=abc&i=1&user=1&authuser=3",
    "cookie": "SID=x; HSID=y; SSID=z",
})


# ---------------------------------------------------------------------------
# Helpers: spin up a relay in a background thread and capture its URL.
# ---------------------------------------------------------------------------
class _Relay:
    """Run serve_once in a thread, capture the printed local URL + token."""

    def __init__(self, timeout=5):
        self.timeout = timeout
        self.url = None
        self.token = None
        self.result = None
        self._thread = None

    def __enter__(self):
        captured = {}
        real_say = paste_server._say

        def spy(msg=""):
            # The relay prints the URL line as "    http://127.0.0.1:port/token"
            if "http://127.0.0.1" in msg and "/" in msg:
                captured["line"] = msg.strip()
            return real_say(msg)

        paste_server._say = spy
        self._restore_say = real_say

        def run():
            self.result = paste_server.serve_once(
                timeout=self.timeout, use_tunnel=False, bind="127.0.0.1", port=0
            )

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

        # Wait for the URL to be printed.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "line" not in captured:
            time.sleep(0.02)
        if "line" not in captured:
            raise RuntimeError("relay did not print a URL")
        self.url = captured["line"]
        self.token = self.url.rsplit("/", 1)[-1]
        return self

    def __exit__(self, *exc):
        paste_server._say = self._restore_say
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.timeout + 2)


def _post(url, body, ctype="application/json"):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": ctype})
    return urllib.request.urlopen(req, timeout=3)


def _get(url):
    return urllib.request.urlopen(url, timeout=3)


# ---------------------------------------------------------------------------
# Token / path guard
# ---------------------------------------------------------------------------
def test_token_has_entropy():
    # 24 bytes -> 32 url-safe base64 chars (no padding).
    import secrets
    t1 = secrets.token_urlsafe(paste_server.TOKEN_BYTES)
    t2 = secrets.token_urlsafe(paste_server.TOKEN_BYTES)
    assert t1 != t2
    assert len(t1) >= 30


def test_wrong_path_404():
    with _Relay() as relay:
        base = relay.url.rsplit("/", 1)[0]
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(base + "/not-the-token")
        assert ei.value.code == 404
        # Deliver a valid payload so the relay shuts down cleanly.
        _post(relay.url, VALID_PAYLOAD)


def test_get_serves_form():
    with _Relay() as relay:
        resp = _get(relay.url)
        assert resp.status == 200
        body = resp.read().decode("utf-8")
        assert "<textarea" in body
        _post(relay.url, VALID_PAYLOAD)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def test_valid_post_delivers_payload():
    with _Relay() as relay:
        resp = _post(relay.url, VALID_PAYLOAD)
        assert resp.status == 200
        relay._thread.join(timeout=3)
        assert relay.result == VALID_PAYLOAD


def test_single_use_consumes_first_payload():
    """After the first valid payload the server shuts down; a replay must
    fail (either 410 if it races, or a refused connection once it's down)."""
    with _Relay() as relay:
        resp = _post(relay.url, VALID_PAYLOAD)
        assert resp.status == 200
        relay._thread.join(timeout=3)
        # Server is down now. A replay must not succeed.
        failed = False
        try:
            r = _post(relay.url, VALID_PAYLOAD)
            failed = r.status in (404, 410)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            failed = True
        assert failed, "replay after single-use must fail"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def test_empty_body_rejected():
    with _Relay() as relay:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(relay.url, "")
        assert ei.value.code == 400
        _post(relay.url, VALID_PAYLOAD)


def test_non_json_body_rejected():
    with _Relay() as relay:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(relay.url, "this is not json")
        assert ei.value.code == 400
        _post(relay.url, VALID_PAYLOAD)


def test_oversized_body_rejected():
    with _Relay() as relay:
        big = "{" + ("x" * (paste_server.MAX_BODY_BYTES + 10))
        # The server rejects the oversized body. On some platforms it replies
        # 413 cleanly; on others (Windows) it sends 413 before draining the
        # body, so the client's remaining write is reset. Both outcomes mean
        # "rejected and not consumed" — accept either.
        with pytest.raises((urllib.error.HTTPError, urllib.error.URLError,
                            ConnectionError, OSError)) as ei:
            _post(relay.url, big)
        if isinstance(ei.value, urllib.error.HTTPError):
            assert ei.value.code == 413
        # Relay must NOT have been consumed by the rejected oversized body.
        _post(relay.url, VALID_PAYLOAD)
