"""Target selection for the CDP session — the bug that killed the first real run.

```
CmdError: Page.navigate failed: 'Page.navigate' wasn't found
```

The factory attached to the **browser-level** endpoint. Measured 2026-09-19:

| method | PAGE target | BROWSER endpoint |
|---|---|---|
| Page.enable / Page.navigate | ok | "wasn't found" |
| Network.enable | ok | "wasn't found" |
| Runtime.evaluate | ok | "wasn't found" |
| Storage.getCookies | ok (28 cookies) | ok (28 cookies) |

So a page target does everything v3 needs and the browser endpoint does almost
nothing. `pick_page_target` is pure so this is testable without a browser.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest

ROOT = r"D:\_projects\takeout_downloader_script\.claude\worktrees\takeout-autopilot"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autopilot.errors import AutopilotError  # noqa: E402
from autopilot.run import pick_page_target  # noqa: E402


def t(url, type_="page", ws="ws://x"):
    return {"type": type_, "url": url, "webSocketDebuggerUrl": ws}


def test_prefers_a_takeout_tab():
    targets = [t("https://example.com/"), t("https://takeout.google.com/manage"),
               t("https://accounts.google.com/x")]
    assert "takeout.google.com" in pick_page_target(targets)["url"]


def test_falls_back_to_any_page_when_no_takeout_tab_exists():
    targets = [t("https://example.com/"), t("chrome://newtab")]
    assert pick_page_target(targets)["url"] == "https://example.com/"


def test_ignores_non_page_targets():
    targets = [t("chrome-extension://abc/background.js", type_="service_worker"),
               t("https://example.com/")]
    assert pick_page_target(targets)["type"] == "page"


def test_ignores_targets_without_a_debugger_url():
    targets = [{"type": "page", "url": "https://x/", "webSocketDebuggerUrl": ""},
               t("https://y/")]
    assert pick_page_target(targets)["url"] == "https://y/"


def test_raises_clearly_when_there_is_no_page_target():
    with pytest.raises(AutopilotError) as e:
        pick_page_target([t("chrome-extension://x", type_="service_worker")])
    assert "no page target" in str(e.value)
    assert "Page.navigate" in str(e.value), "the error should name the real cause"


def test_raises_on_empty_target_list():
    with pytest.raises(AutopilotError):
        pick_page_target([])


def test_the_factory_attaches_to_a_page_target_not_the_browser(monkeypatch):
    """Guards the wiring: the factory must use /json/list, not /json/version."""
    import asyncio

    import autopilot.run as run_mod

    seen = {}

    # `_get_json` runs through `asyncio.to_thread`, so the fake must be SYNC — an
    # async fake makes to_thread return the coroutine object instead of the data.
    def fake_get_json(url, timeout):
        seen["url"] = url
        return [t("https://takeout.google.com/manage",
                  ws="ws://127.0.0.1:9222/devtools/page/ABC")]

    class FakeTransport:
        async def send(self, payload): pass
        async def recv(self):
            await asyncio.sleep(999)
            return "{}"
        async def close(self): pass

    async def fake_connect(url, **kw):
        seen["ws"] = url
        return FakeTransport()

    monkeypatch.setattr(run_mod, "_get_json", fake_get_json)
    import autopilot.ws_transport as ws_mod
    monkeypatch.setattr(ws_mod.WebSocketTransport, "connect",
                        classmethod(lambda cls, url, **kw: fake_connect(url, **kw)))

    async def go():
        factory = run_mod._default_session_factory("http://127.0.0.1:9222")
        async with factory() as _session:
            return True

    assert asyncio.run(go()) is True
    assert seen["url"].endswith("/json/list"), (
        "must query /json/list for targets, not /json/version — the browser "
        f"endpoint cannot navigate. Got {seen['url']}")
    assert "/devtools/page/" in seen["ws"], (
        f"must attach to a page target, got {seen['ws']}")
