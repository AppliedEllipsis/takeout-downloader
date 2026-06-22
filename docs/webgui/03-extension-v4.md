# 03 — Extension v4

Extension v4 evolves the existing `helpers/` MV3 extension from a
**capture-to-clipboard** tool into a **capture-and-POST** agent that talks to the
manager service and supports auto-re-capture and agent control. The capture
logic that already works (listening on the final download host, enumerating all
exports) is **kept as-is**. We add a transport, an automation hook, and a control
channel.

> Golden rule: do not touch the part of `background.js` that captures from
> `takeout-download.usercontent.google.com`. That is the hard-won correct
> behavior (see `docs/ARCHITECTURE.md` redirect-chain section). v4 wraps it.

## What changes vs v3.1.0

| Area | v3 (today) | v4 (this plan) |
|---|---|---|
| Transport | clipboard only (`Copy as JSON`) | **POST to manager** `http://127.0.0.1:8080/api/payload`; clipboard kept as fallback |
| Trigger | user clicks popup | user click **or** manager-requested auto-re-capture |
| Cookie refresh | manual: re-capture + paste | automatic: manager asks, extension re-captures from logged-in profile |
| Agent control | none | manager can drive capture via CDP + a content-script bridge |
| Config | auto-copy, badge | + manager URL, capture token, auto-POST toggle, auto-recapture toggle |

## New permissions

```jsonc
// manifest.json additions
"permissions": [
  "webRequest", "storage", "activeTab", "notifications", "cookies",
  "scripting",            // NEW: trigger a fresh export/download programmatically
  "alarms"                // NEW: backoff timer for auto-recapture retries
],
"host_permissions": [
  "https://takeout.google.com/*",
  "https://takeout-download.usercontent.google.com/*",
  "https://storage.cloud.google.com/*",
  "http://127.0.0.1:8080/*"   // NEW: POST captures to the local manager
]
```

`127.0.0.1:8080` is the only new network destination, and it is localhost inside
the same container. Nothing leaves the box.

## Transport: POST to manager

On a successful capture (the same event that fills the clipboard today), v4 also:

```
1. Read settings: managerUrl, captureToken, autoPost (default ON).
2. If autoPost:
     POST {managerUrl}/api/payload
       headers: X-Capture-Token: {captureToken}
       body:    <the exact v1 payload JSON, unchanged>
3. On 2xx  -> badge "✓", notification "Sent to manager (job <id>)".
   On 4xx  -> badge "!", notification with the manager's error; keep clipboard copy.
   On fail -> fall back to clipboard, notification "Manager unreachable, copied instead".
```

The payload body is **byte-identical** to what `parse_payload` already accepts,
so the engine side needs no new parsing.

## Account identity in the payload meta (for dated subfolders)

The manager derives the output path `<root>/google-takeout/<account-label>/<export-ts>/`
(see `02-manager-service.md`). The extension supplies the raw material:

| Field | How v4 gets it | Status |
|---|---|---|
| `meta.user` | `user=` param on the download URL | already captured (obfuscated gaia id) |
| `meta.authuser` | `authuser=` param (`/u/0/`) | already captured |
| `meta.email` | **NEW**: best-effort scrape of the signed-in account from the Takeout page DOM (account-switcher `aria-label` / `[data-email]` / the `og` profile block) | new in v4; may be `null` if the DOM hides it |
| export timestamp | parsed by the **manager** from any export filename (`takeout-20260616T040104Z-...`) | no extension change — already in the filenames |

> The export timestamp is NOT a new extension field. It is already encoded in
> every part filename the extension enumerates today; the manager parses it.
> Only `meta.email` is genuinely new, and it is best-effort: if scraping fails,
> the manager falls back to `gaia-<user>` so a run never blocks on a missing name.

Scrape rule (content script, runs on `takeout.google.com`):

```
1. Try the account switcher button aria-label (often "Google Account: Name (email)").
2. Try any [data-email] / [data-identifier] attribute in the header.
3. Regex the page for a single \b[\w.+-]+@[\w.-]+\.\w+\b near the account block.
4. If none found -> email: null (manager uses the gaia fallback).
```

The email is only used to build a folder label; it is never sent anywhere except
the localhost manager, inside the same payload that already carries the cookie.

## Auto-re-capture (the auto-relogin heartbeat)

This is what removes the every-45-minute manual step. The manager owns the
trigger; the extension owns the browser action.

```
Manager detects auth challenge (engine auth_cb) → job = needs_cookie
        │
        │  (a) preferred: manager calls Chromium CDP to run the recapture, OR
        │  (b) extension polls {managerUrl}/api/control/recapture-pending
        ▼
Extension recapture routine:
  1. Ensure a takeout.google.com tab exists (scripting.executeScript / create).
  2. Trigger a download on the most recent export (re-click Download) so a fresh
     request hits the final host.
  3. webRequest listener captures the fresh cookie exactly as on a manual run.
  4. POST the fresh payload to /api/payload  → manager resumes the SAME job.
If the recapture does not yield a valid cookie (e.g. Google forced full re-auth):
  5. Extension notification + manager Telegram alert: "Manual login needed".
  6. Back off via alarms (e.g. 1, 2, 5 min) and retry the routine a few times,
     in case it was a transient redirect rather than a real logout.
```

Key safety property: the extension never types credentials. It only re-clicks a
download in an already-authenticated session. If the session is truly dead, it
escalates to a human (you) via Telegram + sound. This matches decision 3.

## Agent control surface

The Pi agent controls the browser two ways, both localhost:

1. **CDP (`:9222`)** — direct Chrome DevTools Protocol: open tabs, click,
   evaluate JS, read the DOM. Best for "drive the browser" and learning a
   workflow. Reached over the SSH forward.
2. **Manager control API** — higher-level intents (`/api/control/recapture`,
   `/api/control/diagnose`). The manager may fulfill these via CDP under the
   hood. Best for "decide and act on download state".

To let the agent (and the manager) ask the extension to do extension-only things
(read `chrome.storage`, force a capture), v4 adds a tiny message bridge:

```
content-script (on takeout.google.com)  <-- window.postMessage -->  page hook
background.js exposes runtime messages:
  { action: "forceCapture" }        -> run capture now, return payload
  { action: "getState" }            -> { hasCapture, captureCount, lastPostStatus }
  { action: "setManagerConfig", ... }
The manager reaches these by evaluating a small JS shim over CDP, so no extra
public port is needed.
```

## Popup / options additions

- Popup: show manager connection status (reachable? last POST result?), a
  **Send now** button (manual POST), and the existing Copy as JSON / cURL.
- Options: `managerUrl` (default `http://127.0.0.1:8080`), `captureToken`,
  `autoPost` (default ON), `autoRecapture` (default ON), retry/backoff settings.

## Bookmarks, pinned extension, and links (the in-browser UX)

These are profile-level, configured once on the persistent `/config` profile
(see `05-deployment.md` for how they're seeded):

- **Bookmark bar**: Takeout home, Takeout "Manage exports", Manager UI
  (`http://127.0.0.1:8080`), Manager recipes page.
- **Pinned extension**: the v4 action popup is pinned; its popup lists the same
  quick links (decision: the popup doubles as a launcher).
- **Default tabs** on Chromium start: a Takeout tab and a Manager UI tab.

## What stays identical (do not regress)

- Capture host + header extraction logic in `background.js`.
- The v1 payload schema (`takeout_payload.py`). v4 emits the same shape.
- Clipboard `Copy as JSON` / `Copy as cURL` (now a fallback, still present).
- The pre-redirect warning path.

## Build order for the extension

1. Add settings + `host_permissions` for the manager; keep everything else.
2. Add auto-POST on capture (clipboard still fires).
3. Add the `forceCapture`/`getState` runtime messages.
4. Add the auto-re-capture routine driven by manager state.
5. Seed bookmarks/pinned/default-tabs in the profile (deployment step).

Each step is independently testable; see `04-decision-trees.md` for how to
verify capture + POST + resume end to end.
