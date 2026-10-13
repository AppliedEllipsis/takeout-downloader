import json
import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest

from autopilot.discovery import DiscoveryBlocked, discover_pages, safe_url

def offline_async(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


ID = "12345678-1234-1234-1234-123456789abc"
USER = "12345"
MANAGE = "https://takeout.google.com/manage?user=" + USER
ARCHIVE = "https://takeout.google.com/manage/archive/" + ID + "?user=" + USER


class Session:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.navigated = []

    async def call(self, method, params):
        assert method == "Page.navigate"
        self.navigated.append(params["url"])

    async def drain(self, delay):
        pass

    async def value(self, js):
        return json.dumps(next(self.pages))


def page(url, **kw):
    return dict(url=url, users=[USER], archives=[], next=[], parts=[], **kw)


@offline_async
async def test_pagination_then_metadata_no_download_navigation():
    next_url = MANAGE + "&page=2"
    first = page(MANAGE)
    first["next"] = [next_url]
    second = page(next_url)
    second["archives"] = [ARCHIVE, ARCHIVE]
    final = page(ARCHIVE)
    final["parts"] = [{"user": USER, "archive_id": ID, "index": "0"}]
    s = Session([first, second, final])
    found = await discover_pages(s, SimpleNamespace(user_id=USER, manage_url=MANAGE))
    assert len(found) == 1 and found[0].status == "ready"
    assert s.navigated == [MANAGE, next_url, ARCHIVE]
    assert all("download" not in u for u in s.navigated)


@offline_async
async def test_pending_not_complete():
    first = page(MANAGE)
    first["archives"] = [ARCHIVE]
    found = await discover_pages(Session([first, page(ARCHIVE)]), SimpleNamespace(user_id=USER, manage_url=MANAGE))
    assert found[0].status == "pending" and found[0].parts_expected == 0


@offline_async
@pytest.mark.parametrize("users", [[], ["999"], [USER, "999"]])
async def test_missing_or_wrong_identity_blocks(users):
    p = page(MANAGE)
    p["users"] = users
    with pytest.raises(DiscoveryBlocked):
        await discover_pages(Session([p]), SimpleNamespace(user_id=USER, manage_url=MANAGE))


@offline_async
@pytest.mark.parametrize("following", [MANAGE, "https://evil.test/?user=" + USER, "https://takeout.google.com/takeout/download?user=" + USER])
async def test_unsafe_or_cycle_pagination(following):
    p = page(MANAGE)
    p["next"] = [following]
    with pytest.raises(DiscoveryBlocked):
        await discover_pages(Session([p]), SimpleNamespace(user_id=USER, manage_url=MANAGE))


@offline_async
async def test_pagination_bound():
    p = page(MANAGE)
    p["next"] = [MANAGE + "&page=2"]
    with pytest.raises(DiscoveryBlocked, match="limit"):
        await discover_pages(Session([p]), SimpleNamespace(user_id=USER, manage_url=MANAGE), max_pages=1)


@pytest.mark.parametrize("url,user", [("https://takeout.google.com/manage", USER), (MANAGE, ""), (MANAGE + "&rapt=secret", USER), (MANAGE, "999")])
def test_url_explicit_selection(url, user):
    with pytest.raises(DiscoveryBlocked):
        safe_url(url, user)


@offline_async
async def test_dedicated_target_closed_on_success_and_failure(monkeypatch):
    import autopilot.discovery as d
    calls, sockets = [], []
    monkeypatch.setattr(d, "_version", lambda http: {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/offline"})
    async def connect(url):
        sockets.append(url)
        return url
    monkeypatch.setattr(d.WebSocketTransport, "connect", connect)
    class Cdp:
        def __init__(self, transport):
            self.transport = transport
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def call(self, method, params=None):
            calls.append((method, params))
            return {"targetId": "our-target"} if method == "Target.createTarget" else {}
    monkeypatch.setattr(d, "CdpSession", Cdp)
    async with d.automation_session("http://127.0.0.1:9222"):
        pass
    with pytest.raises(RuntimeError):
        async with d.automation_session("http://127.0.0.1:9222"):
            raise RuntimeError("offline")
    assert sum(method == "Target.createTarget" for method, p in calls) == 2
    assert sum(method == "Target.closeTarget" for method, p in calls) == 2
    assert all(p["targetId"] == "our-target" for method, p in calls if method == "Target.closeTarget")
    assert all("our-target" in url for url in sockets if "/page/" in url)
