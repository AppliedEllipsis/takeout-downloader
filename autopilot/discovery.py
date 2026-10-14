"""Read-only Takeout discovery in a single disposable automation target.

No download link is navigated or clicked. Identity absence fails closed.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import UUID

from .cdp import CdpSession
from .ws_transport import WebSocketTransport, browser_endpoint


class DiscoveryBlocked(ValueError):
    pass


def archive_id(value):
    if not isinstance(value, str):
        raise DiscoveryBlocked("invalid archive UUID")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError):
        raise DiscoveryBlocked("invalid archive UUID") from None
    if canonical != value.lower():
        raise DiscoveryBlocked("invalid archive UUID")
    return canonical


def safe_url(url, user_id, *, archive=False, runtime=False, verified_user=False):
    """Validate identity separately from short-lived runtime routing/proof keys.

    Config calls use the default strict mode. A missing URL user can only be
    filled by discovery after independent DOM identity verification, never by
    authuser (an account slot) or rapt (a proof token). Runtime return values are
    navigation-only: use stored_url before returning/persisting a reference.
    """
    if not isinstance(user_id, str) or not re.fullmatch(r"[0-9]{1,32}", user_id):
        raise DiscoveryBlocked("missing or invalid user_id")
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise DiscoveryBlocked("invalid Takeout URL")
    try:
        p = urlsplit(url)
        q = parse_qs(p.query, keep_blank_values=True, strict_parsing=True,
                     max_num_fields=10, errors="strict")
    except (ValueError, UnicodeError):
        raise DiscoveryBlocked("invalid Takeout URL") from None
    if p.scheme != "https" or p.netloc != "takeout.google.com" or p.fragment:
        raise DiscoveryBlocked("unsafe Takeout URL")
    allowed = {"user"} | ({"page", "pageToken", "rapt", "authuser"} if runtime else set())
    if set(q) - allowed or any(len(values) != 1 for values in q.values()):
        raise DiscoveryBlocked("unsupported Takeout URL query")
    if "user" not in q and runtime and verified_user:
        q["user"] = [user_id]
    if q.get("user") != [user_id]:
        raise DiscoveryBlocked("Takeout URL identity mismatch")
    if "authuser" in q and not re.fullmatch(r"0|[1-9][0-9]{0,2}", q["authuser"][0]):
        raise DiscoveryBlocked("invalid account routing key")
    if "page" in q and not re.fullmatch(r"[0-9]{1,10}", q["page"][0]):
        raise DiscoveryBlocked("invalid pagination key")
    for key in ("rapt", "pageToken"):
        if key in q and (not q[key][0] or len(q[key][0]) > 1024 or
                         any(ord(c) <= 32 or ord(c) >= 127 for c in q[key][0])):
            raise DiscoveryBlocked("invalid runtime token key")
    if archive:
        if not p.path.startswith("/manage/archive/"):
            raise DiscoveryBlocked("invalid archive path")
        archive_id(p.path[len("/manage/archive/"):])
    elif p.path != "/manage":
        raise DiscoveryBlocked("invalid manage path")
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q, doseq=True), ""))


def stored_url(url, user_id, *, archive=False):
    """Token-free canonical reference; never persist runtime proof/routing keys."""
    clean = safe_url(url, user_id, archive=archive, runtime=True)
    p = urlsplit(clean)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode({"user": user_id}), ""))


@dataclass(frozen=True)
class DiscoveredExport:
    archive_id: str
    user_id: str
    archive_url: str
    status: str = "pending"
    parts_expected: int = 0


# Only read DOM metadata. Do not return signed download URLs to watcher state.
SNAPSHOT_JS = r"""
(() => {
 const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 2000);
 const els = Array.from(document.querySelectorAll('[data-download-uri]')).slice(0, 10000);
 const users = new Set();
 document.querySelectorAll('[data-user-id]').forEach(e => users.add(e.getAttribute('data-user-id')));
 const parts = els.map(e => {
   const u = new URL(e.getAttribute('data-download-uri').replace(/&amp;/g, '&'), location.href);
   const user = u.searchParams.get('user'); if (user) users.add(user);
   return {user, archive_id:u.searchParams.get('j'), index:u.searchParams.get('i')};
 });
 // Identity from server-rendered archive links, never from location.href.
 links.filter(a => new URL(a.href).hostname === 'takeout.google.com' &&
   new URL(a.href).pathname.startsWith('/manage/archive/')).forEach(a => {
   const user = new URL(a.href).searchParams.get('user'); if(user) users.add(user);
 });
 const text = (document.body ? document.body.innerText : '').slice(0, 100000);
 return JSON.stringify({url:location.href, users:Array.from(users), parts,
   archives:links.filter(a => new URL(a.href).pathname.startsWith('/manage/archive/')).map(a=>a.href),
   next:links.filter(a => a.rel === 'next').map(a=>a.href),
   challenged:/accounts\.google\.com/.test(location.hostname),
   failed:/export failed|couldn't create|could not create/i.test(text),
   expired:/export expired|no longer available/i.test(text)});
})()
"""


async def discover_pages(session, account, *, max_pages=10, max_exports=100, settle=2):
    """Inject a CDP-like session for offline tests; all navigation is allowlisted."""
    user = account.user_id
    url = safe_url(account.manage_url, user)
    seen, archives = set(), {}
    for _ in range(max_pages):
        if url in seen:
            raise DiscoveryBlocked("pagination cycle")
        seen.add(url)
        await session.call("Page.navigate", {"url": url})
        await session.drain(settle)
        data = json.loads(await session.value(SNAPSHOT_JS))
        if data.get("challenged"):
            raise DiscoveryBlocked("needs_reauth")
        if data.get("users") != [user]:
            raise DiscoveryBlocked("account identity unverified")
        safe_url(data.get("url"), user, runtime=True, verified_user=True)
        for link in data.get("archives", []):
            # Only independently verified manage DOM can fill an absent user.
            # Keep proofs in memory for this navigation, never in stored refs.
            link = safe_url(link, user, archive=True, runtime=True, verified_user=True)
            ident = archive_id(urlsplit(link).path.split("/")[-1])
            archives[ident] = link
            if len(archives) > max_exports:
                raise DiscoveryBlocked("export limit exceeded")
        following = data.get("next", [])
        if not following:
            break
        if len(following) != 1:
            raise DiscoveryBlocked("ambiguous pagination")
        url = safe_url(following[0], user, runtime=True, verified_user=True)
    else:
        raise DiscoveryBlocked("pagination limit exceeded")
    result = []
    for ident, link in archives.items():
        await session.call("Page.navigate", {"url": link})
        await session.drain(settle)
        data = json.loads(await session.value(SNAPSHOT_JS))
        if data.get("challenged"):
            raise DiscoveryBlocked("needs_reauth")
        safe_url(data.get("url"), user, archive=True, runtime=True, verified_user=True)
        if urlsplit(data["url"]).path.split("/")[-1].lower() != ident:
            raise DiscoveryBlocked("archive identity mismatch")
        # A pending page can contain no account metadata at all. It remains
        # non-executable; manage identity was already verified. Ready parts must
        # independently attest the same user. Any explicit mismatch blocks.
        if data.get("users") != [user] and (data.get("users") or data.get("parts")):
            raise DiscoveryBlocked("account identity unverified")
        parts = data.get("parts", [])
        indices = set()
        for part in parts:
            if part.get("user") != user or part.get("archive_id", "").lower() != ident:
                raise DiscoveryBlocked("part identity mismatch")
            index = part.get("index")
            if not isinstance(index, str) or not index.isdigit() or len(index) > 5 or int(index) > 10000 or int(index) in indices:
                raise DiscoveryBlocked("invalid part indices")
            indices.add(int(index))
        status = ("expired_unrecoverable" if data.get("expired") else
                  "google_failed" if data.get("failed") else "ready" if parts else "pending")
        result.append(DiscoveredExport(ident, user, stored_url(link, user, archive=True), status, len(parts)))
    return result


def _version(cdp_http):
    with urlopen(cdp_http + "/json/version", timeout=10) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise DiscoveryBlocked("CDP response too large")
    return json.loads(raw)


@asynccontextmanager
async def automation_session(cdp_http):
    """One disposable page; also used by watcher mint passes, never human tabs."""
    local = urlsplit(cdp_http)
    if local.scheme != "http" or local.hostname not in {"127.0.0.1", "localhost", "::1"} or local.username or local.password or local.path or local.query or local.fragment or not 1 <= (local.port or 80) <= 65535:
        raise DiscoveryBlocked("unsafe CDP endpoint")
    version = await asyncio.to_thread(_version, cdp_http)
    endpoint = browser_endpoint(version)
    endpoint_parts = urlsplit(endpoint)
    local = urlsplit(cdp_http)
    if endpoint_parts.scheme != "ws" or endpoint_parts.hostname not in {"127.0.0.1", "localhost", "::1"} or endpoint_parts.port != (local.port or 80) or endpoint_parts.username or endpoint_parts.password or endpoint_parts.query or endpoint_parts.fragment or not endpoint_parts.path.startswith("/devtools/browser/"):
        raise DiscoveryBlocked("unsafe browser websocket endpoint")
    transport = await WebSocketTransport.connect(endpoint)
    async with CdpSession(transport) as browser:
        target = (await browser.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", target):
            raise DiscoveryBlocked("invalid automation target")
        try:
            # Connect directly to only OUR target, never choose a human tab.
            p = urlsplit(cdp_http)
            ws = f"ws://{p.netloc}/devtools/page/{target}"
            page_transport = await WebSocketTransport.connect(ws)
            async with CdpSession(page_transport) as page:
                await page.call("Page.enable")
                yield page
        finally:
            await browser.call("Target.closeTarget", {"targetId": target})


async def discover_async(account, cdp_http):
    safe_url(account.manage_url, account.user_id)
    async with automation_session(cdp_http) as page:
        return await discover_pages(page, account)


def discover(account, cdp_http):
    return asyncio.run(discover_async(account, cdp_http))
