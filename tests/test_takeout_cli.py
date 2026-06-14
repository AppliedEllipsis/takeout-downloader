"""
Tests for takeout_cli — focused on the bits that don't hit the network.

The real download path (discovery + aria2c) is tested end-to-end with mocks
in test_takeout_cli_flow.py. Here we cover:

- Log file creation + rotation (RotatingFileHandler)
- The CLI's aria2c batch-file builder (header plumbing)
- Auth-failure detection (signin redirect / HTML response)
- The analyze script's parser
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import takeout_cli  # noqa: E402
import takeout_cli_analyze as analyze  # noqa: E402
from takeout_payload import parse_payload  # noqa: E402


# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
def test_logger_writes_to_file(tmp_path):
    log_path = tmp_path / "test.log"
    logger = takeout_cli._install_logger(log_path)
    logger.info("hello world")
    logger.info("with %s", "interpolation")
    # Flush handlers.
    for h in logger.handlers:
        h.flush()
    content = log_path.read_text(encoding="utf-8")
    assert "hello world" in content
    assert "with interpolation" in content
    assert "INFO" in content


def test_logger_rotates(tmp_path):
    log_path = tmp_path / "rot.log"
    # Tiny rotation budget so we can prove it triggers in a test.
    logger = takeout_cli._install_logger(log_path)
    # Patch the maxBytes on the RotatingFileHandler for this test only.
    for h in logger.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.maxBytes = 256
    # Write enough to trigger multiple rotations.
    for i in range(200):
        logger.info("line %03d %s", i, "x" * 50)
    for h in logger.handlers:
        h.flush()
    # Main log + .1 + .2 (and maybe .3) should exist.
    assert log_path.exists()
    backups = sorted(tmp_path.glob("rot.log.*"))
    assert backups, "expected at least one rotated backup"
    # Backup sizes stay under the cap.
    for b in backups:
        assert b.stat().st_size <= 256 * 2  # small headroom for one overage


# ---------------------------------------------------------------------------
# aria2c batch file builder
# ---------------------------------------------------------------------------
def _fixture_payload():
    p = json.loads((ROOT / "tests" / "fixtures" / "sample_payload.json").read_text())
    return parse_payload(json.dumps(p))


def test_aria2_input_skips_have_parts(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "url": "https://example/-001.zip", "filename": "f-001.zip",
         "size": 100, "have": False},
        {"num": 2, "url": "https://example/-002.zip", "filename": "f-002.zip",
         "size": 100, "have": True},
        {"num": 3, "url": "https://example/-003.zip", "filename": "f-003.zip",
         "size": 100, "have": False},
    ]
    body = takeout_cli.build_aria2_input(parts, payload, tmp_path)
    assert "f-001.zip" in body
    assert "f-002.zip" not in body
    assert "f-003.zip" in body
    # Cookie header on every URL block
    assert body.count("header=Cookie:") == 2
    # per-file out= lines
    assert body.count("  out=") == 2


def test_aria2_input_empty_when_all_have(tmp_path):
    payload = _fixture_payload()
    parts = [
        {"num": 1, "url": "x", "filename": "a.zip", "size": 1, "have": True},
    ]
    body = takeout_cli.build_aria2_input(parts, payload, tmp_path)
    assert body == ""


# ---------------------------------------------------------------------------
# Auth detection
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, url, status, headers=None):
        self.url = url
        self.status_code = status
        self.headers = headers or {}

    def close(self):
        pass


def test_probe_redirects_to_google_login_is_auth_error():
    fake = _FakeResp(
        "https://accounts.google.com/v3/signin/identifier?continue=...",
        200,
        {"content-type": "text/html; charset=utf-8"},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli._probe_part(
                takeout_cli.requests.Session(),
                "https://takeout-download.usercontent.google.com/download/x.zip",
                {"Cookie": "SID=test; __Secure-1PSID=test; HSID=test; SSID=test"},
            )


def test_probe_404_is_none():
    fake = _FakeResp(
        "https://takeout-download.usercontent.google.com/download/x-099.zip",
        404, {},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        assert takeout_cli._probe_part(
            takeout_cli.requests.Session(), "x", {},
        ) is None


def test_probe_206_returns_size():
    fake = _FakeResp(
        "https://takeout-download.usercontent.google.com/download/x-001.zip",
        206,
        {"content-range": "bytes 0-0/1234567",
         "content-type": "application/octet-stream"},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        size = takeout_cli._probe_part(
            takeout_cli.requests.Session(), "x", {},
        )
    assert size == 1234567


def test_probe_html_from_takeout_host_is_auth_error():
    """If takeout-download returns 200 with text/html, it's an error page."""
    fake = _FakeResp(
        "https://takeout-download.usercontent.google.com/download/x-001.zip",
        200,
        {"content-type": "text/html; charset=utf-8",
         "content-length": "4321"},
    )
    with mock.patch.object(takeout_cli.requests.Session, "get",
                           return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli._probe_part(
                takeout_cli.requests.Session(), "x", {},
            )


# ---------------------------------------------------------------------------
# Discovery end-to-end with mocks (no real network)
# ---------------------------------------------------------------------------
def test_discover_parts_stops_at_404(tmp_path):
    """Probe 001 + 002 succeed, 003 + 004 are 404 -> end-of-set."""
    payload = _fixture_payload()
    responses = {
        "001": _FakeResp("https://takeout-download.usercontent.google.com/download/x-001.zip",
                         206, {"content-range": "bytes 0-0/100"}),
        "002": _FakeResp("https://takeout-download.usercontent.google.com/download/x-002.zip",
                         206, {"content-range": "bytes 0-0/200"}),
        "003": _FakeResp("https://takeout-download.usercontent.google.com/download/x-003.zip",
                         404, {}),
        "004": _FakeResp("https://takeout-download.usercontent.google.com/download/x-004.zip",
                         404, {}),
    }

    def fake_get(self, url, **kw):
        for k, r in responses.items():
            if k in url:
                return r
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch.object(takeout_cli.requests.Session, "get", fake_get):
        parts = takeout_cli.discover_parts(payload, tmp_path)

    nums = [p["num"] for p in parts]
    assert nums == [1, 2]
    assert parts[0]["size"] == 100
    assert parts[1]["size"] == 200


def test_discover_raises_auth_error_on_first_probe(tmp_path):
    payload = _fixture_payload()
    fake = _FakeResp("https://accounts.google.com/v3/signin",
                     200, {"content-type": "text/html"})
    with mock.patch.object(takeout_cli.requests.Session, "get", return_value=fake):
        with pytest.raises(takeout_cli.AuthError):
            takeout_cli.discover_parts(payload, tmp_path)


# ---------------------------------------------------------------------------
# Log analyzer
# ---------------------------------------------------------------------------
def test_analyzer_parses_synthetic_log(tmp_path):
    log_text = (
        "2026-06-14 08:00:00.000 INFO   ========================================\n"
        "2026-06-14 08:00:00.001 INFO     Google Takeout Downloader (aria2c)\n"
        "2026-06-14 08:00:00.002 INFO   ========================================\n"
        "2026-06-14 08:00:01.123 DEBUG  probe #001 GET https://...\n"
        "2026-06-14 08:00:01.456 DEBUG    <- 206 ct=application/octet-stream host=...\n"
        "2026-06-14 08:00:01.457 DEBUG  probe #001 -> 1234567 bytes\n"
        "2026-06-14 08:00:01.500 INFO     001       1.2 MB  need\n"
        "2026-06-14 08:00:02.000 WARNING Auth failed probing part 002 (redirected to accounts.google.com).\n"
        "2026-06-14 08:00:02.500 ERROR   Something bad happened\n"
    )
    log_file = tmp_path / "syn.log"
    log_file.write_text(log_text, encoding="utf-8")

    lines = list(analyze.parse_lines(log_file.open(encoding="utf-8")))
    assert len(lines) == 9
    sessions = analyze.split_into_sessions(lines)
    assert len(sessions) == 1
    summary = analyze.summarize(sessions[0])
    assert summary["auth_failures"] == 1
    assert len(summary["errors"]) == 1
    assert summary["probes"] == [(1, "1234567 bytes")]


def test_analyzer_handles_rotated_backups(tmp_path):
    """If both foo.log and foo.log.1 exist, both are read in the right order."""
    (tmp_path / "f.log.1").write_text(
        "2026-06-14 08:00:00.000 INFO   Google Takeout Downloader (aria2c)\n"
        "2026-06-14 08:00:00.001 INFO     run A\n",
        encoding="utf-8",
    )
    (tmp_path / "f.log").write_text(
        "2026-06-14 09:00:00.000 INFO   Google Takeout Downloader (aria2c)\n"
        "2026-06-14 09:00:00.001 INFO     run B\n",
        encoding="utf-8",
    )
    # Walk parse — but cmd_summary reads files directly; do it inline here.
    parts: list = []
    for fname in ("f.log.1", "f.log"):
        with (tmp_path / fname).open(encoding="utf-8") as f:
            parts.extend(analyze.parse_lines(f))
    assert len(parts) == 4
    # Order is oldest backup first, then current file.
    assert "run A" in parts[1].msg
    assert "run B" in parts[3].msg


# ---------------------------------------------------------------------------
# human_size
# ---------------------------------------------------------------------------
def test_human_size_basic():
    assert takeout_cli.human_size(0) == "0 B"
    assert takeout_cli.human_size(512) == "512.0 B"
    assert takeout_cli.human_size(1024) == "1.0 KB"
    assert takeout_cli.human_size(1024 * 1024) == "1.0 MB"
    assert takeout_cli.human_size(1024 * 1024 * 1024) == "1.0 GB"
