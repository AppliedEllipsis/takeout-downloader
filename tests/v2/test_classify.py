"""Fixture-driven tests for takeout2.classify.

Every ReasonCode branch is covered. The regression tests at the bottom encode
the exact misclassifications that cost a 2.8 TB re-download in v1.
"""
from __future__ import annotations

import pytest

from takeout2.classify import ResponseFacts, classify, is_zip_magic, looks_like_html
from takeout2.contracts import AUTH_REASONS, ReasonCode

ZIP = b"PK\x03\x04\x14\x00\x00\x00"
LOGIN_HTML = b"<!DOCTYPE html><html><head><title>Sign in - Google Accounts</title>"
END_HTML = b"<!DOCTYPE html><html><body><p>The requested URL was not found.</p>"
LIMIT_HTML = (b"<!DOCTYPE html><html><body>You have exceeded the download limit "
              b"for this archive. Please request another archive.</body>")


def facts(**kw) -> ResponseFacts:
    base = dict(status=200, headers={"content-type": "application/zip"},
                first_bytes=ZIP, final_url="https://takeout-download.usercontent.google.com/x")
    base.update(kw)
    return ResponseFacts(**base)


class TestHappyPath:
    def test_full_zip_is_complete(self):
        assert classify(facts()) is ReasonCode.OK_COMPLETE

    def test_206_is_partial(self):
        assert classify(facts(status=206)) is ReasonCode.OK_PARTIAL

    def test_range_ignored_by_server_is_treated_as_partial(self):
        """We asked to resume; a 200 means the server ignored Range, so the
        caller must not blindly append to the existing file."""
        assert classify(facts(status=200, expected_partial=True)) is ReasonCode.OK_PARTIAL


class TestAuthFailures:
    def test_final_url_on_accounts_host_is_auth_redirect(self):
        f = facts(status=200,
                  headers={"content-type": "text/html"},
                  first_bytes=LOGIN_HTML,
                  final_url="https://accounts.google.com/ServiceLogin?continue=...")
        assert classify(f) is ReasonCode.AUTH_REDIRECT

    def test_302_to_accounts_is_auth_redirect(self):
        f = facts(status=302,
                  headers={"location": "https://accounts.google.com/ServiceLogin"},
                  first_bytes=b"")
        assert classify(f) is ReasonCode.AUTH_REDIRECT

    def test_401_is_auth(self):
        assert classify(facts(status=401, first_bytes=b"")) is ReasonCode.AUTH_401

    def test_403_without_limit_wording_is_auth(self):
        assert classify(facts(status=403, first_bytes=b"forbidden")) is ReasonCode.AUTH_401

    def test_unsolicited_html_mid_download_is_auth(self):
        """HTML when we were NOT probing the end means the session lapsed."""
        f = facts(headers={"content-type": "text/html"},
                  first_bytes=LOGIN_HTML, probing_end=False)
        assert classify(f) is ReasonCode.AUTH_REDIRECT


class TestLimitExceeded:
    def test_403_with_limit_wording(self):
        assert classify(facts(status=403, first_bytes=LIMIT_HTML)) is ReasonCode.LIMIT_EXCEEDED

    def test_200_html_with_limit_wording_beats_end_of_range(self):
        """Even while probing the end, explicit limit wording wins."""
        f = facts(headers={"content-type": "text/html"},
                  first_bytes=LIMIT_HTML, probing_end=True)
        assert classify(f) is ReasonCode.LIMIT_EXCEEDED

    def test_expired_archive_reported_as_limit(self):
        f = facts(headers={"content-type": "text/html"},
                  first_bytes=b"<html>This archive has expired</html>",
                  probing_end=True)
        assert classify(f) is ReasonCode.LIMIT_EXCEEDED


class TestEndOfRange:
    def test_plain_html_while_probing_end_is_clean_stop(self):
        f = facts(headers={"content-type": "text/html"},
                  first_bytes=END_HTML, probing_end=True)
        assert classify(f) is ReasonCode.END_OF_RANGE

    def test_404_is_not_found(self):
        assert classify(facts(status=404, first_bytes=b"")) is ReasonCode.NOT_FOUND


class TestTransientConditions:
    def test_429_is_rate_limited(self):
        assert classify(facts(status=429, first_bytes=b"")) is ReasonCode.RATE_LIMITED

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_is_network_error(self, status):
        assert classify(facts(status=status, first_bytes=b"")) is ReasonCode.NETWORK_ERROR

    def test_transport_error_short_circuits(self):
        f = facts(transport_error="Connection reset by peer")
        assert classify(f) is ReasonCode.NETWORK_ERROR

    def test_unexpected_redirect_is_transient(self):
        f = facts(status=307, headers={"location": "https://example.com/elsewhere"},
                  first_bytes=b"")
        assert classify(f) is ReasonCode.NETWORK_ERROR

    def test_200_with_unknown_binary_is_distrusted(self):
        f = facts(headers={"content-type": "application/octet-stream"},
                  first_bytes=b"\x00\x01\x02\x03not a zip")
        assert classify(f) is ReasonCode.NETWORK_ERROR


class TestHelpers:
    def test_zip_magic_detection(self):
        assert is_zip_magic(ZIP)
        assert is_zip_magic(b"PK\x05\x06")   # empty-archive EOCD stub
        assert not is_zip_magic(b"<html>")
        assert not is_zip_magic(b"")

    def test_html_sniffing_without_content_type(self):
        assert looks_like_html("", b"<!DOCTYPE html><html>")
        assert looks_like_html("text/html; charset=utf-8", b"")
        assert not looks_like_html("application/zip", ZIP)

    def test_header_lookup_is_case_insensitive(self):
        f = ResponseFacts(status=200, headers={"Content-Type": "application/zip"})
        assert f.content_type == "application/zip"


class TestV1Regressions:
    """The specific bugs documented in docs/webgui/14-resume-cookies-multiaccount.md."""

    def test_end_of_range_never_parks_job_on_cookie(self):
        """Root cause #4: probing one index past the last part returned HTML,
        v1 raised AuthError for ALL html, flipping the job to needs_cookie."""
        f = facts(headers={"content-type": "text/html"},
                  first_bytes=END_HTML, probing_end=True)
        assert classify(f) not in AUTH_REASONS

    def test_stale_cookie_302_is_not_a_range_support_problem(self):
        """The stale capture returned 302 -> ServiceLogin; curl followed it,
        got 200 HTML, and reported 'does not seem to support byte ranges'.
        We must name the real cause: the cookie is dead."""
        f = facts(status=200,
                  headers={"content-type": "text/html"},
                  first_bytes=LOGIN_HTML,
                  final_url="https://accounts.google.com/ServiceLogin",
                  expected_partial=True)
        assert classify(f) is ReasonCode.AUTH_REDIRECT
