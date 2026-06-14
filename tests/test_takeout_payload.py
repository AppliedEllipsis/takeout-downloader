#!/usr/bin/env python3
"""
Tests for takeout_payload — the JSON schema shared by the browser extension
and the TUI. This is the critical trust boundary: if the extension and the
parser drift, downloads silently fail with auth errors.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# Make the project root importable when run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from takeout_payload import (  # noqa: E402
    TakeoutPayload,
    parse_payload,
    SCHEMA_VERSION,
    DEFAULT_HEADERS,
    REQUIRED_COOKIE_MARKERS,
)

# A realistic final-host download URL and a cookie carrying a session marker.
GOOD_URL = (
    "https://takeout-download.usercontent.google.com/download/"
    "takeout-20251207T071725Z-3-003.zip?j=abc&i=2&user=123&authuser=0"
)
GOOD_COOKIE = "__Secure-1PSID=abc123; HSID=def456; SSID=ghi789; SAPISID=jkl"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_extension_json(**overrides):
    """Build a JSON blob shaped exactly like helpers/background.js emits."""
    obj = {
        "schema": SCHEMA_VERSION,
        "captured_at": _now_iso(),
        "source": "extension",
        "url": GOOD_URL,
        "method": "GET",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
            "Accept": "text/html,*/*",
            "Referer": "https://takeout.google.com/",
        },
        "cookie": GOOD_COOKIE,
    }
    obj.update(overrides)
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# from_json — happy path
# ---------------------------------------------------------------------------

def test_from_json_happy_path():
    p = TakeoutPayload.from_json(make_extension_json())
    assert p.url == GOOD_URL
    assert p.cookie == GOOD_COOKIE
    assert p.method == "GET"
    assert p.source == "extension"
    assert p.schema == SCHEMA_VERSION
    # Extension headers should be merged on top of defaults
    assert p.headers["Referer"] == "https://takeout.google.com/"


def test_from_json_backfills_default_headers():
    # Extension sent no headers — defaults must fill in.
    p = TakeoutPayload.from_json(make_extension_json(headers={}))
    assert p.headers["User-Agent"] == DEFAULT_HEADERS["User-Agent"]
    assert p.headers["Accept"] == DEFAULT_HEADERS["Accept"]


# ---------------------------------------------------------------------------
# from_json — rejection cases
# ---------------------------------------------------------------------------

def test_from_json_empty_raises():
    with pytest.raises(ValueError, match="Empty payload"):
        TakeoutPayload.from_json("")


def test_from_json_invalid_json_raises():
    with pytest.raises(ValueError, match="Invalid JSON"):
        TakeoutPayload.from_json("{not valid json")


def test_from_json_non_object_raises():
    with pytest.raises(ValueError, match="must be a JSON object"):
        TakeoutPayload.from_json("[1, 2, 3]")


def test_from_json_wrong_schema_raises():
    with pytest.raises(ValueError, match="Schema version"):
        TakeoutPayload.from_json(make_extension_json(schema=999))


def test_from_json_missing_url_raises():
    blob = json.dumps({"schema": 1, "cookie": GOOD_COOKIE})
    with pytest.raises(ValueError, match="missing 'url'"):
        TakeoutPayload.from_json(blob)


def test_from_json_missing_cookie_raises():
    blob = json.dumps({"schema": 1, "url": GOOD_URL})
    with pytest.raises(ValueError, match="missing 'cookie'"):
        TakeoutPayload.from_json(blob)


def test_from_json_non_takeout_url_raises():
    blob = json.dumps({"schema": 1, "url": "https://example.com/file.zip", "cookie": GOOD_COOKIE})
    with pytest.raises(ValueError, match="doesn't look like a Takeout"):
        TakeoutPayload.from_json(blob)


def test_from_json_ignores_non_string_headers():
    blob = make_extension_json(headers={"X-Num": 42, "Good": "yes", "X-List": [1, 2]})
    p = TakeoutPayload.from_json(blob)
    assert p.headers["Good"] == "yes"
    assert "X-Num" not in p.headers
    assert "X-List" not in p.headers


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def test_validate_ok():
    p = TakeoutPayload.from_json(make_extension_json())
    ok, msg = p.validate()
    assert ok is True
    assert msg is None


def test_validate_rejects_cookie_without_session_marker():
    # Cookie with no known Google session marker → pre-redirect capture.
    blob = make_extension_json(cookie="NID=foo; CONSENT=bar")
    p = TakeoutPayload.from_json(blob)
    ok, msg = p.validate()
    assert ok is False
    assert "session markers" in msg


def test_validate_warns_on_old_capture():
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    p = TakeoutPayload.from_json(make_extension_json(captured_at=old))
    ok, msg = p.validate()
    assert ok is True          # still usable
    assert msg is not None      # but warns
    assert "minutes ago" in msg


def test_validate_accepts_filename_pattern_url():
    # URL not on the canonical host but matches the takeout filename pattern.
    url = "https://other.example.com/path/takeout-20251207T071725Z-3-003.zip"
    blob = json.dumps({"schema": 1, "url": url, "cookie": GOOD_COOKIE, "captured_at": _now_iso()})
    p = TakeoutPayload.from_json(blob)
    ok, _ = p.validate()
    assert ok is True


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------

def test_to_json_from_json_roundtrip():
    p1 = TakeoutPayload.from_json(make_extension_json())
    p2 = TakeoutPayload.from_json(p1.to_json())
    assert p1.url == p2.url
    assert p1.cookie == p2.cookie
    assert p1.headers == p2.headers
    assert p1.method == p2.method


def test_to_curl_contains_url_and_cookie():
    p = TakeoutPayload.from_json(make_extension_json())
    curl = p.to_curl()
    assert GOOD_URL in curl
    assert "Cookie:" in curl
    assert GOOD_COOKIE in curl


def test_to_curl_roundtrips_through_from_curl():
    p1 = TakeoutPayload.from_json(make_extension_json())
    p2 = TakeoutPayload.from_curl(p1.to_curl())
    assert p2.url == p1.url
    assert p2.cookie == p1.cookie


# ---------------------------------------------------------------------------
# from_curl
# ---------------------------------------------------------------------------

def test_from_curl_happy_path():
    curl = f"curl '{GOOD_URL}' -H 'Cookie: {GOOD_COOKIE}'"
    p = TakeoutPayload.from_curl(curl)
    assert p.url == GOOD_URL
    assert p.cookie == GOOD_COOKIE
    # cURL path backfills default headers
    assert p.headers["User-Agent"] == DEFAULT_HEADERS["User-Agent"]


def test_from_curl_missing_url_raises():
    with pytest.raises(ValueError, match="extract URL"):
        TakeoutPayload.from_curl("curl -H 'Cookie: x=y'")


# ---------------------------------------------------------------------------
# parse_payload — auto-detection
# ---------------------------------------------------------------------------

def test_parse_payload_detects_json():
    p = parse_payload(make_extension_json())
    assert p.source == "extension"


def test_parse_payload_detects_curl():
    curl = f"curl '{GOOD_URL}' -H 'Cookie: {GOOD_COOKIE}'"
    p = parse_payload(curl)
    assert p.url == GOOD_URL
    assert p.source in ("curl", "powershell")


def test_parse_payload_empty_raises():
    with pytest.raises(ValueError, match="Empty input"):
        parse_payload("   ")


# ---------------------------------------------------------------------------
# convenience helpers
# ---------------------------------------------------------------------------

def test_cookie_chars():
    p = TakeoutPayload.from_json(make_extension_json())
    assert p.cookie_chars() == len(GOOD_COOKIE)


def test_filename_hint():
    p = TakeoutPayload.from_json(make_extension_json())
    assert p.filename_hint() == "takeout-20251207T071725Z-3-003.zip"


def test_filename_hint_unknown_for_weird_url():
    url = "https://takeout-download.usercontent.google.com/download/x?j=1"
    blob = json.dumps({"schema": 1, "url": url, "cookie": GOOD_COOKIE, "captured_at": _now_iso()})
    p = TakeoutPayload.from_json(blob)
    assert p.filename_hint() == "unknown"


def test_required_cookie_markers_present_in_good_cookie():
    # Sanity: our test cookie actually exercises the marker check.
    assert any(m in GOOD_COOKIE for m in REQUIRED_COOKIE_MARKERS)
