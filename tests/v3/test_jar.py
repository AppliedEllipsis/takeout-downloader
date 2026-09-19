"""Offline tests for the cookie jar.

The jar is the credential the *transporter* needs. It is deliberately never used
for minting (that needs a `rapt` only an interactive browser session can produce)
— see `autopilot.mint`.
"""
from __future__ import annotations

import asyncio

import pytest

from autopilot.cdp import CdpSession
from autopilot.jar import CookieError, build_header, pull_jar
from fake_cdp import FakeTransport

COOKIES = [
    {"name": "SID", "value": "secret-sid", "domain": ".google.com"},
    {"name": "HSID", "value": "secret-hsid", "domain": ".google.com"},
    {"name": "SSID", "value": "secret-ssid", "domain": "accounts.google.com"},
    {"name": "OTHER", "value": "nope", "domain": ".example.com"},
]


def run(coro):
    return asyncio.run(coro)


def test_build_header_selects_google_domains_only():
    jar = build_header(COOKIES)
    assert jar.n_cookies == 4
    assert jar.n_google == 3
    assert "SID=secret-sid" in jar.header
    assert "OTHER" not in jar.header
    assert jar.usable is True
    assert ".google.com" in jar.domains and "accounts.google.com" in jar.domains


def test_build_header_handles_an_empty_jar():
    jar = build_header([])
    assert jar.usable is False and jar.header == "" and jar.n_google == 0


def test_build_header_skips_records_without_a_name():
    jar = build_header([{"value": "v", "domain": ".google.com"},
                        {"name": "SID", "value": "v", "domain": ".google.com"}])
    assert jar.header == "SID=v"


def test_jar_str_never_leaks_a_cookie_value():
    jar = build_header(COOKIES)
    text = str(jar)
    assert "secret-sid" not in text
    assert "3/4 google cookies" in text


def test_pull_jar_reads_from_the_browser_over_cdp():
    t = FakeTransport(command_results={"Storage.getCookies": {"cookies": COOKIES}})
    async def go():
        async with CdpSession(t) as session:
            return await pull_jar(session)
    jar = run(go())
    assert jar.n_google == 3
    assert "SID=secret-sid" in jar.header
    assert "Storage.getCookies" in t.methods()


def test_pull_jar_refuses_when_the_browser_has_no_google_cookies():
    t = FakeTransport(command_results={"Storage.getCookies": {"cookies": [
        {"name": "OTHER", "value": "x", "domain": ".example.com"}]}})
    async def go():
        async with CdpSession(t) as session:
            return await pull_jar(session)
    with pytest.raises(CookieError):
        run(go())
