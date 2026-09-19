"""Read the Takeout archive page so we know what to mint.

**Measured 2026-09-19 — a page-location trap worth stating loudly:** the download
links live on `https://takeout.google.com/manage/archive/<id>`, **not** on
`/manage`. The list page only *links* to the archive page. A readiness check that
watches `/manage` will sit reporting "no download links" while a completed export
is right there — which is exactly what happened to this project's first monitor.

Cost: zero. Reading the page spends no download attempt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .cdp import CdpSession
from .errors import AutopilotError
from .mint import absolute_uri

__all__ = [
    "ARCHIVE_PATH",
    "MANAGE_PATH",
    "PartLink",
    "ArchivePage",
    "archive_url",
    "read_archive",
    "READ_JS",
]

MANAGE_PATH = "/manage"
ARCHIVE_PATH = "/manage/archive/"

#: Mirrors the scrape the v1 extension already performed (`helpers/content.js`),
#: but keyed by the `i=` index rather than by filename — see `PartLink.index`.
READ_JS = r"""
(() => {
  const els = Array.from(document.querySelectorAll('[data-download-uri]'));
  const text = document.body ? document.body.innerText : '';
  // "(Number of times already downloaded: N)" — telemetry, not an invariant.
  const dlRe = /(takeout-\d{8}T\d{6}Z-\d+-\d+\.zip)\s*\(Number of times already downloaded:\s*(\d+)\)/g;
  const dl = {}; let m;
  while ((m = dlRe.exec(text)) !== null) dl[m[1]] = parseInt(m[2], 10);
  const parts = els.map((el) => {
    const raw = (el.getAttribute('data-download-uri') || '').replace(/&amp;/g, '&');
    const iMatch = raw.match(/[?&]i=(\d+)/);
    const jMatch = raw.match(/[?&]j=([^&]+)/);
    const uMatch = raw.match(/[?&]user=([^&]+)/);
    const rMatch = raw.match(/[?&]rapt=([^&]+)/);
    return {
      raw_uri: raw,
      index: iMatch ? parseInt(iMatch[1], 10) : null,
      archive_id: jMatch ? jMatch[1] : null,
      user: uMatch ? uMatch[1] : null,
      has_rapt: !!rMatch,
      size: el.getAttribute('data-size'),
      aria: el.getAttribute('aria-label') || '',
      filename: (raw.split('?')[0].split('/').pop() || '')
    };
  });
  const expiry = (text.match(/Available until ([^\n]+)/) || [])[1] || null;
  const status = (text.match(/Overall status:\s*([^\n]+)/) || [])[1] || null;
  return JSON.stringify({
    url: location.href,
    title: document.title,
    parts,
    dl_counts: dl,
    expiry,
    status,
    challenged: /challenge\/pwd|ServiceLogin|InteractiveLogin/.test(location.href),
    // present only for multi-part exports; absent on a 1-part export, measured
    part_of_N: (() => {
      const s = new Set();
      parts.forEach((p) => { const mm = /part\s+(\d+)\s+of\s+(\d+)/i.exec(p.aria || '');
                             if (mm) s.add(parseInt(mm[2], 10)); });
      return Array.from(s);
    })()
  });
})()
"""


@dataclass
class PartLink:
    """One downloadable part, keyed by its `i=` index.

    **Why by index and not by filename:** v2's `capturePayload` keyed its maps by
    the basename in `data-download-uri`, and when the URL path reused one
    basename for every part the maps collapsed to a single entry. The older v1
    scraper had keyed by `i=` and did not have that bug.
    """

    index: Optional[int]
    raw_uri: str
    archive_id: Optional[str] = None
    user: Optional[str] = None
    has_rapt: bool = False
    size: Optional[str] = None
    aria: str = ""
    filename: str = ""
    #: Absolute redirector URL, resolvable only once we know the page origin.
    redirector: str = ""


@dataclass
class ArchivePage:
    url: str = ""
    title: str = ""
    parts: list[PartLink] = field(default_factory=list)
    dl_counts: dict = field(default_factory=dict)
    expiry: Optional[str] = None
    status: Optional[str] = None
    challenged: bool = False
    part_of_N: list = field(default_factory=list)

    @property
    def indices(self) -> list[int]:
        return sorted(p.index for p in self.parts if p.index is not None)

    def assert_indexed(self) -> None:
        """Fail loudly if the index keying did not work.

        `distinct(i) == count` is the invariant that catches the v2 collapse: if
        two parts share an index, or any part has none, the scrape is unusable and
        planning on top of it would be worse than stopping.

        Note the `set(...)`: an earlier version of this guard compared
        `len(self.indices) != len(self.parts)`, which is **always equal when every
        part carries a duplicate index** — so it silently passed the exact case it
        was written to catch. `tests/v3/test_scrape.py` caught that.
        """
        missing = [p for p in self.parts if p.index is None]
        if missing:
            raise AutopilotError(
                f"{len(missing)} part(s) had no parseable i= index; refusing to plan"
            )
        distinct = set(self.indices)
        if len(distinct) != len(self.parts):
            raise AutopilotError(
                f"duplicate i= indices: {len(self.parts)} parts but "
                f"{len(distinct)} distinct indices"
            )


def archive_url(archive_id: str, *, origin: str = "https://takeout.google.com") -> str:
    return f"{origin.rstrip('/')}{ARCHIVE_PATH}{archive_id}"


async def read_archive(session: CdpSession, url: str, *, settle: float = 6.0) -> ArchivePage:
    """Navigate to an archive page and read its parts.

    Free: a page read costs no download attempt (measured). Works on both
    `/manage/archive/<id>` and (to detect a stale view) `/manage`.
    """
    await session.call("Page.navigate", {"url": url})
    await session.drain(settle)
    raw = await session.value(READ_JS)
    if not raw:
        raise AutopilotError(f"archive read returned nothing for {url}")
    data = json.loads(raw)

    page = ArchivePage(
        url=data.get("url", ""),
        title=data.get("title", ""),
        dl_counts=data.get("dl_counts") or {},
        expiry=data.get("expiry"),
        status=data.get("status"),
        challenged=bool(data.get("challenged")),
        part_of_N=data.get("part_of_N") or [],
    )
    for p in data.get("parts") or []:
        redirector = absolute_uri(p.get("raw_uri") or "",
                                 origin=_origin_of(data.get("url") or url))
        page.parts.append(
            PartLink(
                index=p.get("index"),
                raw_uri=p.get("raw_uri") or "",
                archive_id=p.get("archive_id"),
                user=p.get("user"),
                has_rapt=bool(p.get("has_rapt")),
                size=p.get("size"),
                aria=p.get("aria") or "",
                filename=p.get("filename") or "",
                redirector=redirector,
            )
        )
    return page


def _origin_of(url: str) -> str:
    parts = url.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else "https://takeout.google.com"
