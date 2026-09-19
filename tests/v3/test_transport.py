"""Offline tests for the transporter.

The three cases that matter most are the data-integrity ones: a server that
ignores `Range`, a changed remote object, and a torn tail. Each corresponds to a
way this project actually corrupted or discarded bytes.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from autopilot.errors import NeedsReauth
from autopilot.transport import (
    RemoteChanged,
    TransferError,
    _total_from_content_range,
    aligned_resume_offset,
    download,
)
from fake_http import FakeHttpClient, FakeStream

URL = (
    "https://takeout-download.usercontent.google.com/download/"
    "takeout-20260919T045820Z-1-001.zip?j=abc&i=0&user=123&authuser=0"
)
JAR = "SID=1; HSID=2; SSID=3"
ETAG = "f470fe32-76d6-43a6-9321-dbc76834919e-1005482974000-0"
TOTAL = 320


def run(coro):
    return asyncio.run(coro)


def body(n: int, start: int = 0) -> bytes:
    return bytes((i % 251) for i in range(start, start + n))


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_aligned_resume_offset_rewinds_to_a_boundary():
    # Ported behaviour from takeout2/engine.py:152 — must not drift.
    assert aligned_resume_offset(0, 64) == 0
    assert aligned_resume_offset(100, 64) == 0        # 36 trims to nothing
    assert aligned_resume_offset(192, 64) == 128      # 128 is already aligned
    assert aligned_resume_offset(200, 64) == 128      # 136 -> floor to 128
    assert aligned_resume_offset(500, 0) == 500       # rewind disabled


def test_total_from_content_range():
    assert _total_from_content_range("bytes 0-1023/261259") == 261259
    assert _total_from_content_range("bytes 0-1023/*") is None
    assert _total_from_content_range("garbage") is None


# ---------------------------------------------------------------------------
# happy path / resume
# ---------------------------------------------------------------------------
def test_full_download_writes_the_file(tmp_path):
    dest = tmp_path / "p.zip"
    client = FakeHttpClient([FakeStream(200, {"Content-Length": str(TOTAL)},
                                        body(TOTAL))])
    r = run(download(client, URL, str(dest), jar_header=JAR, expected_size=TOTAL))
    assert r.complete and r.bytes_written == TOTAL and r.resumed_from == 0
    assert dest.read_bytes() == body(TOTAL)


def test_resume_appends_from_the_offset(tmp_path):
    dest = tmp_path / "p.zip"
    dest.write_bytes(body(192))                      # a partial file on disk
    client = FakeHttpClient([FakeStream(
        206,
        {"Content-Range": f"bytes 128-{TOTAL - 1}/{TOTAL}", "ETag": ETAG},
        body(TOTAL - 128, start=128),
    )])
    r = run(download(client, URL, str(dest), jar_header=JAR,
                     expected_size=TOTAL, etag=ETAG, rewind=64))

    assert r.status == "complete"
    assert r.resumed_from == 128 and r.bytes_written == TOTAL - 128
    assert r.http_status == 206
    # the file is now the whole object, with no duplicated middle section
    assert dest.read_bytes() == body(TOTAL)
    h = client.last_headers()
    assert h["range"] == "bytes=128-"
    assert h["if-range"] == ETAG


def test_already_complete_makes_no_request_at_all(tmp_path):
    # Re-downloading a finished part would spend an attempt for nothing.
    dest = tmp_path / "p.zip"
    dest.write_bytes(body(TOTAL))
    client = FakeHttpClient([])                      # nothing scripted
    r = run(download(client, URL, str(dest), jar_header=JAR, expected_size=TOTAL))
    assert r.status == "already-complete"
    assert client.requests == []


# ---------------------------------------------------------------------------
# the data-integrity cases
# ---------------------------------------------------------------------------
def test_a_server_ignoring_range_restarts_instead_of_appending(tmp_path):
    """A 200 in reply to a Range request means the body starts at byte 0.

    Appending it to a partial file would produce a file of roughly double the
    intended length with a corrupted middle. We must truncate and rewrite.
    """
    dest = tmp_path / "p.zip"
    dest.write_bytes(body(192))
    client = FakeHttpClient([FakeStream(200, {}, body(TOTAL))])

    r = run(download(client, URL, str(dest), jar_header=JAR,
                     expected_size=TOTAL, etag=ETAG, rewind=64))

    assert r.restarted is True
    assert r.resumed_from == 0
    assert r.complete
    assert len(dest.read_bytes()) == TOTAL          # not 192 + 320
    assert dest.read_bytes() == body(TOTAL)


def test_early_connection_loss_leaves_a_usable_partial(tmp_path):
    dest = tmp_path / "p.zip"
    client = FakeHttpClient([FakeStream(200, {}, body(TOTAL),
                                        truncate_body_after=100)])
    r = run(download(client, URL, str(dest), jar_header=JAR, expected_size=TOTAL))
    assert r.status == "partial"
    assert r.total_bytes == 100
    assert len(dest.read_bytes()) == 100            # a valid resume point


def test_content_range_total_mismatch_is_flagged_not_appended(tmp_path):
    dest = tmp_path / "p.zip"
    dest.write_bytes(body(192))
    client = FakeHttpClient([FakeStream(
        206, {"Content-Range": "bytes 128-999/999"}, body(100, start=128))])
    with pytest.raises(RemoteChanged):
        run(download(client, URL, str(dest), jar_header=JAR,
                     expected_size=TOTAL, rewind=64))


def test_range_unsatisfiable_is_flagged_as_remote_changed(tmp_path):
    dest = tmp_path / "p.zip"
    dest.write_bytes(body(192))
    client = FakeHttpClient([FakeStream(416, {}, b"")])
    with pytest.raises(RemoteChanged):
        run(download(client, URL, str(dest), jar_header=JAR,
                     expected_size=TOTAL, rewind=64))


# ---------------------------------------------------------------------------
# auth classification
# ---------------------------------------------------------------------------
def test_signin_redirect_raises_needs_reauth():
    client = FakeHttpClient([FakeStream(
        302, {"Location": "https://accounts.google.com/ServiceLogin?continue=x"}, b"")])
    with pytest.raises(NeedsReauth) as e:
        run(download(client, URL, "unused.zip", jar_header=JAR))
    assert "accounts.google.com" in e.value.url


def test_401_is_treated_as_a_reauth_not_a_generic_error():
    client = FakeHttpClient([FakeStream(401, {}, b"")])
    with pytest.raises(NeedsReauth):
        run(download(client, URL, "unused.zip", jar_header=JAR))


def test_missing_jar_is_a_programming_error_not_a_session_problem():
    """Measured: without the jar the file host bounces to sign-in.

    Blaming the session for that would send an operator to re-authenticate when
    the actual bug is that the jar was never passed. So it is a plain
    TransferError, and **no request is made**.
    """
    client = FakeHttpClient([])
    with pytest.raises(TransferError) as e:
        run(download(client, URL, "unused.zip", jar_header=""))
    assert not isinstance(e.value, NeedsReauth)
    assert client.requests == []


def test_unexpected_redirect_is_an_error_not_silently_followed():
    client = FakeHttpClient([FakeStream(302, {"Location": "https://elsewhere/x"}, b"")])
    with pytest.raises(TransferError):
        run(download(client, URL, "unused.zip", jar_header=JAR))
