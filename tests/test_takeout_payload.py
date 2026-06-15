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


# ---------------------------------------------------------------------------
# Schema v2 — multi-payload with archiveId + expectedParts
# ---------------------------------------------------------------------------

def _make_v2_multi(expected_parts=5, sizes=None):
    """Build a v2 multi-payload shaped like the new extension emits."""
    sizes = sizes or [162871683, 545181980, 1257224630, 411587, 44082]
    return {
        "schema": 2,
        "captured_at": _now_iso(),
        "source": "extension",
        "multi": True,
        "archiveId": "ccb0dc6c-ba0c-466f-9228-2ebd83fbcd20",
        "expectedParts": expected_parts,
        "exports": [
            {
                "url": (
                    "https://takeout-download.usercontent.google.com/"
                    f"download/takeout-20260612T190148Z-15-{i+1:03d}.zip"
                    f"?j=ccb0dc6c&i={i}&user=1&authuser=3"
                ),
                "partIndex": i,
                "size": s,
            }
            for i, s in enumerate(sizes[:expected_parts])
        ],
        "cookie": GOOD_COOKIE,
        "headers": {"User-Agent": "Mozilla/5.0 Chrome/120"},
    }


def test_v2_multi_parses_all_exports_with_sizes():
    from takeout_payload import parse_multi_payload_meta
    text = json.dumps(_make_v2_multi())
    payloads, meta = parse_multi_payload_meta(text)
    assert len(payloads) == 5
    assert meta.archiveId == "ccb0dc6c-ba0c-466f-9228-2ebd83fbcd20"
    assert meta.expectedParts == 5
    assert meta.has_full_metadata is True
    # Sizes keyed by 0-based part index.
    assert meta.sizes[0] == 162871683
    assert meta.sizes[4] == 44082
    # Each payload has its own i= so the URLs are distinct.
    assert payloads[0].url.endswith("i=0&user=1&authuser=3")
    assert payloads[4].url.endswith("i=4&user=1&authuser=3")
    # Cookie + headers are shared.
    for p in payloads:
        assert p.cookie == GOOD_COOKIE
        assert p.headers["User-Agent"] == "Mozilla/5.0 Chrome/120"


def test_v1_multi_payload_is_rejected_with_clear_error():
    """v1 multi-payloads lack archiveId/expectedParts so we refuse them
    rather than silently degrade to a parts-unaware download."""
    v1 = _make_v2_multi()
    v1["schema"] = 1
    del v1["archiveId"]
    del v1["expectedParts"]
    for e in v1["exports"]:
        e.pop("partIndex", None)
        e.pop("size", None)
    from takeout_payload import parse_multi_payload
    with pytest.raises(ValueError, match=r"v2 is required"):
        parse_multi_payload(json.dumps(v1))


def test_v1_single_export_still_works():
    """v1 single-export captures are still accepted everywhere."""
    blob = make_extension_json(schema=1)
    p = TakeoutPayload.from_json(blob)
    assert p.url == GOOD_URL


def test_v2_single_export_works():
    """v2 single-export captures (without multi) also work."""
    blob = make_extension_json(schema=2)
    p = TakeoutPayload.from_json(blob)
    assert p.url == GOOD_URL


def test_unknown_schema_is_rejected():
    blob = make_extension_json(schema=99)
    with pytest.raises(ValueError, match="Schema version 99"):
        TakeoutPayload.from_json(blob)


def test_meta_empty_for_non_multi_payload():
    """Legacy single-export payloads return empty meta — callers can
    safely check `meta.has_full_metadata`."""
    from takeout_payload import parse_multi_payload_meta
    text = make_extension_json()
    payloads, meta = parse_multi_payload_meta(text)
    assert len(payloads) == 1
    assert meta.archiveId is None
    assert meta.expectedParts is None
    assert meta.sizes == {}
    assert meta.has_full_metadata is False


def test_meta_partial_when_sizes_missing():
    """archiveId+expectedParts without per-part sizes is still valid
    metadata; the CLI just probes to fill in sizes."""
    from takeout_payload import parse_multi_payload_meta
    payload = _make_v2_multi(expected_parts=3, sizes=[0, 0, 0])
    text = json.dumps(payload)
    payloads, meta = parse_multi_payload_meta(text)
    assert meta.has_full_metadata is True
    assert meta.sizes == {}  # all zero, so empty


def test_v2_multi_falls_back_to_single_when_multi_false():
    """If a v2 payload omits `multi`, treat as single (same as v1)."""
    from takeout_payload import parse_multi_payload
    text = make_extension_json(schema=2)
    payloads = parse_multi_payload(text)
    assert len(payloads) == 1
    assert payloads[0].url == GOOD_URL
