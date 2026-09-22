#!/usr/bin/env python3
"""Satisfy the Google "Password challenge" (ReAuth) form over CDP.

Owner directive: the temp password lives in `/config/gPass.txt` (server:
`<repo>/config/gPass.txt`, which is gitignored). This script reads it FROM THE
FILE (never from argv, never from the environment) so the secret cannot appear
in `ps`, in shell history, or in this project's logs.

Why a CDP input event and not `location.href = ...` / a plain fetch:
    Google only raises the INTERACTIVE challenge for user-initiated navigation.
    Measured earlier: CDP `Input.dispatchMouseEvent` on the archive page's
    download anchor produced `accounts.google.com/v3/signin/challenge/pwd`, while
    a programmatic navigation produced only a passive
    `accounts.google.com/ServiceLogin?passive=1209600`. A CDP-dispatched input
    event is *trusted*, so it is accepted as user-initiated.

Safety properties (deliberate):
  * Never prints the password, its length-in-context, or any field value.
  * `--dry-run` locates the input/button and reports geometry only.
  * One attempt per invocation. No retry loop: a rejected password on a repeated
    Google challenge can escalate to a lockout, and we will not risk that
    automatically. A rejection is reported, not retried.
  * Exit codes: 0 = satisfied or nothing to do, 2 = could not find the form,
    3 = gPass.txt missing/empty, 4 = password rejected.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request

CDP = "http://127.0.0.1:9222"
GPASS_CANDIDATES = [
    os.environ.get("TAKEOUT_GPASS", ""),
    "/config/gPass.txt",
    "/work/gPass.txt",
    os.path.expanduser("~/.takeout-gpass"),
]

# Locate the password form + its submit button. Google's sign-in DOM uses
# stable ids on the password step; fall back to structural selectors.
LOCATE_JS = r"""
(() => {
  const box = (el) => {
    const b = el.getBoundingClientRect();
    return {x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2),
            w: Math.round(b.width), h: Math.round(b.height)};
  };
  const input = document.querySelector('input[type="password"]');
  const btn = document.querySelector('#passwordNext button')
           || document.querySelector('#passwordNext')
           || document.querySelector('button[jsname="V67aGc"]')
           || document.querySelector('button[type="submit"]');
  return JSON.stringify({
    url: location.href.split('?')[0],
    title: document.title,
    hasPassword: !!input,
    hasButton: !!btn,
    inputBox: input ? box(input) : null,
    buttonBox: btn ? box(btn) : null,
    // Value LENGTH only — never the value.
    valueLen: input ? (input.value || '').length : null,
    visible: input ? (input.offsetParent !== null) : null
  });
})()
"""

# After the submit we only look at whether we are still on a password step and
# whether Google rendered an error. No value is read.
POST_JS = r"""
(() => {
  const input = document.querySelector('input[type="password"]');
  const err = document.querySelector('[jsname="B34EJ"], .dEOOab, [role="alert"]');
  return JSON.stringify({
    url: location.href.split('?')[0],
    title: document.title,
    stillPassword: !!input,
    error: err ? (err.innerText || '').trim().slice(0, 200) : null
  });
})()
"""


def read_password() -> str:
    for path in GPASS_CANDIDATES:
        if not path:
            continue
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                pw = fh.read().strip()
            if pw:
                print(f"credential source: {path} ({len(pw)} chars, value not shown)")
                return pw
            print(f"FAIL: {path} exists but is empty")
            sys.exit(3)
    print("FAIL: no gPass.txt found. Looked in:")
    for path in GPASS_CANDIDATES:
        if path:
            print("   ", path)
    sys.exit(3)


def targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.load(r)


def pick_signin_target(ts):
    pages = [t for t in ts if t.get("type") == "page"]
    for t in pages:
        if "accounts.google.com" in (t.get("url") or ""):
            return t
    return None


async def run(dry_run: bool) -> int:
    import websockets

    ts = targets()
    print("=== page targets ===")
    for t in ts:
        if t.get("type") == "page":
            print("   %-30s %s" % ((t.get("title") or "")[:30], (t.get("url") or "").split("?")[0][:90]))

    target = pick_signin_target(ts)
    if target is None:
        print("RESULT: no accounts.google.com page target — nothing to satisfy.")
        return 0

    print("sign-in target:", (target.get("url") or "").split("?")[0])
    password = None if dry_run else read_password()

    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=2 ** 22, open_timeout=20
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

        async def mouse_click(x, y):
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
                if kind != "mouseMoved":
                    params["buttons"] = 1
                await call("Input.dispatchMouseEvent", params)

        state = json.loads(await evaluate(LOCATE_JS))
        print("form state:", json.dumps(state))
        if not state.get("hasPassword"):
            print("RESULT: no password input on this page — nothing to satisfy.")
            return 0
        if not state.get("hasButton"):
            print("RESULT: password input found but no submit button — refusing to guess.")
            return 2

        if dry_run:
            print("DRY RUN: located the form; not typing anything.")
            return 0

        await mouse_click(state["inputBox"]["x"], state["inputBox"]["y"])
        await asyncio.sleep(0.4)

        # Type character-by-character with trusted key events. A synthetic
        # `value = ...` assignment would not fire the same events Google's
        # handler listens to.
        for ch in password:
            await call(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "text": ch, "unmodifiedText": ch, "key": ch},
            )
            await call("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
        await asyncio.sleep(0.5)

        conf = json.loads(await evaluate(LOCATE_JS))
        print("typed length now:", conf.get("valueLen"), "expected:", len(password))
        if conf.get("valueLen") != len(password):
            # Fall back to a single insertText (also a trusted input event).
            await call(
                "Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "modifiers": 2}
            )
            await call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "modifiers": 2})
            await call("Input.insertText", {"text": password})
            await asyncio.sleep(0.4)
            conf = json.loads(await evaluate(LOCATE_JS))
            print("after insertText, length:", conf.get("valueLen"), "expected:", len(password))
            if conf.get("valueLen") != len(password):
                print("RESULT: could not fill the password field.")
                return 2

        if not conf.get("hasButton"):
            print("RESULT: submit button vanished after typing.")
            return 2

        await mouse_click(conf["buttonBox"]["x"], conf["buttonBox"]["y"])
        print("submitted; waiting for the redirect chain ...")

        for _ in range(24):  # up to ~24s
            await asyncio.sleep(1.0)
            post = json.loads(await evaluate(POST_JS))
            if post.get("error"):
                print("RESULT: PASSWORD REJECTED —", post["error"])
                print("         not retrying automatically (lockout risk).")
                return 4
            if not post.get("stillPassword"):
                print("RESULT: password step cleared:", json.dumps(post))
                return 0

        print("RESULT: still on the password step after 24s; no error text rendered.")
        return 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="locate the form, type nothing")
    args = ap.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
