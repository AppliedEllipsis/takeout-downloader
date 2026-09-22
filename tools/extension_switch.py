#!/usr/bin/env python3
"""Flip the extension's switches from the command line.

Two switches live in `chrome.storage.local` and both are load-bearing:

  autoCancelDownloads  OFF (standing state since 2026-09-22). When ON, the extension
                       cancels *every* native download from the Takeout file host —
                       including one a human started. The owner's workflow depends on
                       real browser downloads, so it stays OFF. Consequence: a mint
                       leaves a real part-sized download running (failure mode 1.22);
                       watch `/config/Downloads`.

  autoRecapture        OFF (the default, and the tab-flood fix). When ON, the spawner
                       opens a tab per captured URL; at the flood peak this reached
                       **314 page targets** and took the container to OOM.

Run it inside the container, where CDP lives:

    python3 -B tools/extension_switch.py                 # report only
    python3 -B tools/extension_switch.py --cancel-downloads on
    python3 -B tools/extension_switch.py --recapture off

Anything not named on the command line is left untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

CDP = "http://127.0.0.1:9222"
KEYS = ("autoCancelDownloads", "autoRecapture")


def targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.load(r)


async def run(changes: dict) -> int:
    import websockets

    ts = targets()
    sw = [
        t
        for t in ts
        if t.get("type") == "service_worker" and "chrome-extension://" in (t.get("url") or "")
    ]
    if not sw:
        print("FAIL: no extension service-worker target. Is Chromium up on :9222?")
        for t in ts[:12]:
            print("   ", t.get("type"), (t.get("url") or "")[:70])
        return 2

    print("SW target:", (sw[0].get("url") or "")[:80])

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
                print("   EXCEPTION:", json.dumps(res["exceptionDetails"])[:300])
            return (res.get("result") or {}).get("value")

        keys = "['" + "','".join(KEYS) + "']"
        read = f"new Promise(r => chrome.storage.local.get({keys}, d => r(JSON.stringify(d))))"

        print("BEFORE:", await evaluate(read))

        if changes:
            payload = json.dumps(changes)
            setexpr = (
                f"new Promise(r => chrome.storage.local.set({payload}, "
                f"() => chrome.storage.local.get({keys}, d => r(JSON.stringify(d)))))"
            )
            print("AFTER :", await evaluate(setexpr))
            print("RE-READ:", await evaluate(read))

        # The guards the code actually branches on, evaluated rather than assumed.
        cancel_guard = await evaluate(
            f"new Promise(r => chrome.storage.local.get(['autoCancelDownloads'], "
            f"d => r(d.autoCancelDownloads === false)))"
        )
        print("GUARD  cancel-listener will SKIP cancelling:", cancel_guard)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cancel-downloads", choices=("on", "off"))
    ap.add_argument("--recapture", choices=("on", "off"))
    args = ap.parse_args()

    changes = {}
    if args.cancel_downloads:
        changes["autoCancelDownloads"] = args.cancel_downloads == "on"
    if args.recapture:
        changes["autoRecapture"] = args.recapture == "on"

    if not changes:
        print("(no changes requested — reporting current state only)")
    return asyncio.run(run(changes))


if __name__ == "__main__":
    sys.exit(main())
