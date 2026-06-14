# Browser Extension

The `helpers/` directory is a Manifest V3 browser extension (Chrome /
Edge / any Chromium browser) that captures Google Takeout download
requests and lets you copy them as a JSON payload for the TUI.

It is **capture-only**: it makes no network requests of its own. The
captured data lives in `chrome.storage.local` and only leaves the
browser when you click **Copy as JSON** (or **Copy as cURL**) and paste
it somewhere.

## Install (unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked**.
4. Select the `helpers/` directory.

The extension icon appears in the toolbar. Pin it for easy access.

## Files

| File | Role |
|------|------|
| `manifest.json` | MV3 manifest. v2.0.0. Declares `webRequest`, `storage`, `activeTab`, `notifications` permissions and host permissions for the Takeout hosts. No localhost. |
| `background.js` | Service worker. Listens on `webRequest.onBeforeSendHeaders`, builds the capture object, stores it, and updates the toolbar badge. Handles `getCapture` / `clearCapture` / preference messages from the popup. |
| `popup.html` / `popup.js` | The toolbar popup: capture status, JSON preview, **Copy as JSON**, **Copy as cURL**, **Clear**, and an auto-copy toggle. |
| `options.html` / `options.js` | Minimal preferences (auto-copy, badge style). |
| `icon16/48/128.png` | Toolbar icons. |

## How capture works

The service worker registers a `webRequest.onBeforeSendHeaders` listener
scoped to:

- `https://takeout-download.usercontent.google.com/*` (the **final**
  download host — this is the one that matters)
- `https://storage.cloud.google.com/*`
- `https://takeout.google.com/*` (pre-redirect; tagged and warned)

For each matching request it extracts the `Cookie` header plus the
`User-Agent`, `Accept`, `Accept-Language`, `Referer`, and `Origin`
headers, and stores a capture object matching the schema in
`takeout_payload.py`.

When the capture comes from `takeout.google.com` rather than the final
download host, it is tagged `pre_redirect: true` and the popup shows a
warning, because those cookies will not authorize the download (see
[`ARCHITECTURE.md`](./ARCHITECTURE.md#the-redirect-chain-gotcha)).

## Usage

1. Go to [takeout.google.com](https://takeout.google.com) → **Manage
   exports** and start downloading any file. (You can cancel the browser
   download once it has started — the request has already been captured.)
2. Click the extension icon. The popup shows the captured file, cookie
   length, and age.
3. Click **Copy as JSON**.
4. Paste into the TUI's payload box and click **Start**.

When the TUI later beeps for a refresh (cookie expired), repeat: trigger
a download in the browser, **Copy as JSON**, paste, **Resume**.

## Copy as cURL

The **Copy as cURL** button produces an equivalent `curl` command
(headers + cookie). The TUI accepts this too — `parse_payload`
auto-detects cURL vs JSON. JSON is preferred because it round-trips the
headers losslessly; cURL pasted from some shells mangles them.

## Privacy / security

- No data leaves the browser except via the clipboard, which you
  trigger explicitly.
- The captured cookie is your live Google session — treat the clipboard
  contents as a secret. Paste it only into the TUI.
- Captures persist in `chrome.storage.local` until you click **Clear** or
  uninstall the extension. Use **Clear** when you're done.
