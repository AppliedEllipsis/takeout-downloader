"""Live smoke tests for the real HTTP client — the only code here that opens a socket.

WHY THIS FILE EXISTS
--------------------
`autopilot/transport.py::UrllibHttpClient` is the sole component in this package
that makes a real request. Every other test injects the fake client, so before
this file the hand-rolled redirect-suppressing opener (`_NoRedirect`) and the
thread-offloaded stream (`_UrllibStream`) had **never been executed once**. A
typo there is invisible to the entire offline suite and only shows up when a
10 GiB transfer is already in flight. This file executes them for real.

THE TWO RULES
-------------
1. **Offline by default.** Everything that touches the network is gated behind
   `AUTOPILOT_LIVE=1`, so `pytest tests/v3` stays green with no connectivity and
   costs nothing in CI.
2. **Never a Google host.** Not `takeout.google.com`, not anything under
   `*.usercontent.google.com`. Those are the live system, and a stray request
   there is a real side effect on a real account. `_assert_not_google` is called
   on every URL before it is requested, and the sign-in classification path is
   exercised against a throwaway loopback server instead — which is strictly
   better evidence anyway, because it lets us count the requests the server saw
   and prove nothing was chased.

RUNNING THE LIVE TESTS
----------------------
    AUTOPILOT_LIVE=1 /c/Users/User/anaconda3/python.exe -m pytest \
        tests/v3/test_http_client_live.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import pytest

from autopilot.errors import NeedsReauth
from autopilot.transport import (
    ACCOUNTS_HOST,
    TransferError,
    UrllibHttpClient,
    download,
)

# Harmless, well-known public hosts. No auth, no side effects, no Google.
PLAIN_URL = "https://example.com/"
ECHO_HOST = "https://httpbingo.org"
REDIRECT_URL = f"{ECHO_HOST}/redirect-to?url=https%3A%2F%2Fexample.com%2F"
RANGE_URL = f"{ECHO_HOST}/range/1024"

LIVE = os.environ.get("AUTOPILOT_LIVE") == "1"
live = pytest.mark.skipif(
    not LIVE,
    reason="live network test — set AUTOPILOT_LIVE=1 to run",
)

GOOGLE_SUFFIXES = (
    "google.com",
    "googleapis.com",
    "googlevideo.com",
    "gstatic.com",
    "googleusercontent.com",
)


def _assert_not_google(url: str) -> None:
    """Hard guard: refuse to send a request to any Google-operated host.

    This is not politeness. `takeout.google.com` and the `usercontent.google.com`
    file host are the production system this tool drives; poking them from a test
    would spend a real download attempt against a real account.
    """
    host = (urlparse(url).hostname or "").lower()
    assert host, f"no host in {url!r}"
    for suffix in GOOGLE_SUFFIXES:
        assert not (host == suffix or host.endswith("." + suffix)), (
            f"refusing to request Google host {host!r} from a test"
        )


def run(coro):
    """Drive a coroutine from a plain sync test (pytest-asyncio is not installed)."""
    return asyncio.run(coro)


async def _read_all(url: str, headers=None, chunk: int = 512):
    """GET `url` with the real client, reading the body in `chunk`-sized reads."""
    _assert_not_google(url)
    client = UrllibHttpClient(timeout=30.0)
    stream = await client.get(url, headers or {"User-Agent": "Mozilla/5.0",
                                               "Accept": "*/*"})
    try:
        blocks = []
        while True:
            block = await stream.read(chunk)
            if not block:
                break
            # Every read must respect the requested size — a stream that returns
            # more than asked would break the caller's progress accounting.
            assert len(block) <= chunk
            blocks.append(block)
        return stream.status, dict(stream.headers), b"".join(blocks)
    finally:
        await stream.aclose()


# ---------------------------------------------------------------------------
# always-on guards (no network)
# ---------------------------------------------------------------------------
def test_google_guard_rejects_the_real_system_hosts():
    """The guard this file relies on must actually reject the real targets."""
    for url in (
        "https://takeout.google.com/",
        "https://takeout-download.usercontent.google.com/download/takeout-1.zip",
        "https://accounts.google.com/ServiceLogin",
    ):
        with pytest.raises(AssertionError):
            _assert_not_google(url)
    # ...and must not reject the harmless hosts the live tests use.
    _assert_not_google(PLAIN_URL)
    _assert_not_google(REDIRECT_URL)


# ---------------------------------------------------------------------------
# live: plain GET, body read in chunks
# ---------------------------------------------------------------------------
@live
def test_live_plain_get_streams_the_body_in_chunks():
    status, headers, body = run(_read_all(PLAIN_URL, chunk=64))

    assert status == 200
    assert "content-type" in headers
    assert b"Example Domain" in body
    # 559 bytes served in 64-byte reads -> more than one read() actually happened,
    # so the to_thread offload in `_UrllibStream.read` is genuinely exercised.
    assert len(body) > 64
    assert headers.get("content-length") is None or int(headers["content-length"]) == len(body)


# ---------------------------------------------------------------------------
# live: redirects are NOT followed — the whole reason `_NoRedirect` exists
# ---------------------------------------------------------------------------
@live
def test_live_redirect_is_surfaced_not_followed():
    _assert_not_google(REDIRECT_URL)
    status, headers, body = run(_read_all(REDIRECT_URL))

    # A follower would have returned 200 with example.com's HTML. We must see the
    # 3xx itself, with its Location intact, so the transporter can classify it.
    assert status == 302, f"redirect was followed: got {status}"
    assert headers.get("location") == "https://example.com/"
    assert b"Example Domain" not in body


@live
def test_live_download_classifies_a_redirect_instead_of_chasing_it(tmp_path):
    """End-to-end: a real 302 becomes a TransferError, and no bytes are written.

    This is the real-world proof that `_NoRedirect` + `download()` cooperate: the
    transporter, not urllib, decides what a redirect means.
    """
    _assert_not_google(REDIRECT_URL)
    dest = tmp_path / "never.zip"
    client = UrllibHttpClient(timeout=30.0)

    with pytest.raises(TransferError) as exc:
        run(download(client, REDIRECT_URL, str(dest), jar_header="SID=live-test"))
    assert not isinstance(exc.value, NeedsReauth)      # not a sign-in redirect
    assert "unexpected redirect" in str(exc.value)
    assert not dest.exists() or dest.stat().st_size == 0


# ---------------------------------------------------------------------------
# live: Range / 206
# ---------------------------------------------------------------------------
@live
def test_live_range_request_returns_206_with_content_range():
    _assert_not_google(RANGE_URL)
    status, headers, body = run(_read_all(
        RANGE_URL,
        headers={"Range": "bytes=0-99", "User-Agent": "Mozilla/5.0",
                 "Accept": "*/*", "Accept-Encoding": "identity"},
    ))

    assert status == 206, f"server ignored Range (status {status})"
    assert headers["content-range"].startswith("bytes 0-99/")
    total = int(headers["content-range"].rsplit("/", 1)[-1])
    assert total > 100
    assert len(body) == 100


@live
def test_live_download_resumes_from_a_local_prefix(tmp_path):
    """The real resume path: fetch a prefix, then `download()` the rest via Range."""
    _assert_not_google(RANGE_URL)
    client = UrllibHttpClient(timeout=30.0)

    # Ground truth: the whole object, read in chunks like a real transfer.
    _, _, full = run(_read_all(RANGE_URL, chunk=128))
    assert len(full) > 100

    dest = tmp_path / "part.bin"
    dest.write_bytes(full[:100])                      # a partial on disk
    result = run(download(
        client, RANGE_URL, str(dest),
        jar_header="SID=live-test",
        expected_size=len(full),
        rewind=0,                                     # keep the 100 bytes, no rewind
        chunk=128,
    ))

    assert result.http_status == 206
    assert result.resumed_from == 100
    assert result.status == "complete"
    assert dest.read_bytes() == full                  # no gap, no duplication


# ---------------------------------------------------------------------------
# live: aclose() is idempotent
# ---------------------------------------------------------------------------
@live
def test_live_aclose_can_be_called_twice():
    async def scenario():
        _assert_not_google(PLAIN_URL)
        client = UrllibHttpClient(timeout=30.0)
        stream = await client.get(PLAIN_URL, {"User-Agent": "Mozilla/5.0"})
        await stream.read(64)
        await stream.aclose()
        await stream.aclose()            # second close must not raise

    run(scenario())


# ---------------------------------------------------------------------------
# real sockets, no network: the sign-in redirect the transporter must classify
# ---------------------------------------------------------------------------
class _RedirectHandler(BaseHTTPRequestHandler):
    location = ""
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append(self.path)
        self.send_response(302)
        self.send_header("Location", type(self).location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):        # keep pytest output clean
        pass


@pytest.fixture()
def loopback_redirect():
    """A throwaway 127.0.0.1 server that answers 302 with a chosen Location.

    Local sockets only — nothing leaves the machine, so this needs no env gate
    and it lets us count requests to prove the redirect was never chased.
    """
    servers = []

    def start(location: str):
        handler = type("H", (_RedirectHandler,), {"location": location, "seen": []})
        try:
            srv = HTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:           # no socket permission in this sandbox
            pytest.skip(f"cannot bind loopback: {exc}")
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{srv.server_address[1]}/dl", handler

    yield start

    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_signin_redirect_is_classified_without_being_followed(
        loopback_redirect, tmp_path):
    """The measured failure mode (1.9/1.18): a 302 to sign-in must be NeedsReauth.

    Driven over a real socket and a real urllib opener, with a Location pointing
    at the sign-in host. Nothing is sent to Google: `_NoRedirect` turns the 302
    into a surfaceable response, and the request count proves it.
    """
    location = f"https://{ACCOUNTS_HOST}/ServiceLogin?continue=x"
    url, handler = loopback_redirect(location)
    assert urlparse(url).hostname == "127.0.0.1"

    with pytest.raises(NeedsReauth) as exc:
        run(download(UrllibHttpClient(timeout=10.0), url, str(tmp_path / "x.zip"),
                     jar_header="SID=loopback"))
    assert ACCOUNTS_HOST in exc.value.url
    assert handler.seen == ["/dl"], f"redirect was followed: {handler.seen}"


def test_a_plain_redirect_over_a_real_socket_never_reaches_the_target(
        loopback_redirect, tmp_path):
    """A generic 302 is an error, and the target is never contacted."""
    url, handler = loopback_redirect("http://127.0.0.1:1/should-never-be-hit")

    with pytest.raises(TransferError):
        run(download(UrllibHttpClient(timeout=10.0), url, str(tmp_path / "x.zip"),
                     jar_header="SID=loopback"))
    assert handler.seen == ["/dl"]
