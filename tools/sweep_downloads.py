#!/usr/bin/env python3
"""Report — and optionally cancel — the browser downloads a mint leaves behind.

Why this exists
---------------
A mint works by navigating the browser at the redirector, and the chain ends at a
`Content-Disposition: attachment` response. Chrome therefore starts a **real download**
and, since 2026-09-22, nothing cancels it (`autoCancelDownloads` is OFF by owner
directive so that human-started downloads work). Measured cost on the 62-product mint:
**15.4 GB left behind across 14 files**, two of them still growing at ~3 GB.

Deleting those files from the shell is NOT enough: Chrome holds them open, and on Linux
an unlinked-but-open file keeps its blocks until the last descriptor closes. The space
only comes back once Chrome itself cancels the download. So this goes through
`chrome.downloads`, via the extension service worker, over CDP.

Safety
------
* **Report-only by default.** Nothing is cancelled without `--apply`.
* Only `state == "in_progress"` items are ever touched — a finished download is left
  alone, because a finished download might be something a human wanted.
* Every candidate is printed with its size and host before anything happens.

    python3 -B tools/sweep_downloads.py            # report
    python3 -B tools/sweep_downloads.py --apply    # cancel the in-progress ones
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

CDP = "http://127.0.0.1:9222"

_LIST_JS = r"""
new Promise(r => chrome.downloads.search({}, items => r(JSON.stringify(items.map(i => ({
  id: i.id,
  state: i.state,
  bytes: i.bytesReceived,
  total: i.totalBytes,
  filename: (i.filename || '').split('/').pop(),
  host: (() => { try { return new URL(i.url).host; } catch (e) { return ''; } })(),
  error: i.error || null
}))))))
"""

# Cancel every in-progress download, then erase it. Sequential so each erase sees a
# settled state; the whole thing resolves with the number cancelled.
_CANCEL_JS = r"""
new Promise(r => chrome.downloads.search({}, items => {
  const targets = items.filter(i => i.state === 'in_progress');
  let n = 0;
  const step = () => {
    if (!targets.length) return r(n);
    const it = targets.pop();
    chrome.downloads.cancel(it.id, () => {
      chrome.downloads.erase({id: it.id}, () => { n += 1; step(); });
    });
  };
  step();
}))
"""


def targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.load(r)


def human(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


async def run(apply: bool) -> int:
    import websockets

    ts = targets()
    sw = [
        t
        for t in ts
        if t.get("type") == "service_worker" and "chrome-extension://" in (t.get("url") or "")
    ]
    if not sw:
        print("FAIL: no extension service-worker target — is Chromium up on :9222?")
        return 2

    async with websockets.connect(
        sw[0]["webSocketDebuggerUrl"], max_size=2 ** 22, open_timeout=15
    ) as ws:
        seq = [0]

        async def call(method, params=None):
            seq[0] += 1
            mid = seq[0]
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    return msg

        async def evaluate(expr):
            r = await call(
                "Runtime.evaluate",
                {"expression": expr, "awaitPromise": True, "returnByValue": True},
            )
            res = r.get("result", {})
            if res.get("exceptionDetails"):
                print("EXCEPTION:", json.dumps(res["exceptionDetails"])[:400])
            return (res.get("result") or {}).get("value")

        items = json.loads(await evaluate(_LIST_JS))
        live = [i for i in items if i["state"] == "in_progress"]
        done = [i for i in items if i["state"] != "in_progress"]

        print(f"downloads known to Chrome: {len(items)}  "
              f"(in_progress {len(live)}, other {len(done)})")
        print()
        print("--- IN PROGRESS (these are the mint's leftovers) ---")
        for i in sorted(live, key=lambda x: -(x["bytes"] or 0)):
            print("  id=%-5s %10s %s  %s" % (
                i["id"], human(i["bytes"]), i["host"][:38], i["filename"][:40]))
        if not live:
            print("  (none)")

        print()
        print("--- finished / interrupted (left alone, may be a human's) ---")
        for i in sorted(done, key=lambda x: -(x["bytes"] or 0))[:12]:
            print("  id=%-5s %-12s %10s %s  %s" % (
                i["id"], i["state"], human(i["bytes"]), i["host"][:34],
                i["filename"][:34]))

        if not live:
            print()
            print("RESULT: nothing in progress; nothing to cancel.")
            return 0

        if not apply:
            print()
            print("RESULT: report only. Re-run with --apply to cancel the "
                  f"{len(live)} in-progress download(s).")
            return 0

        print()
        print("cancelling + erasing ...")
        n = await evaluate(_CANCEL_JS)
        print(f"cancelled {n}")

        after = json.loads(await evaluate(_LIST_JS))
        still = [i for i in after if i["state"] == "in_progress"]
        print(f"in-progress afterwards: {len(still)}")
        return 0 if not still else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually cancel the in-progress downloads (default: report only)")
    args = ap.parse_args()
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
