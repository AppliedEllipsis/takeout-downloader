# Architecture (v6.0)

This document describes how the Google Takeout Downloader works after the
v6.0 rewrite that dropped the web UI in favor of a **TUI + browser
extension** relay.

## The relay model

The user is the trust boundary. Nothing is auto-sent anywhere.

```
┌─────────────────────────────┐
│ Browser (takeout.google.com)│
│  download starts            │
└──────────────┬──────────────┘
               │ webRequest.onBeforeSendHeaders
               ▼
┌─────────────────────────────┐
│ Chrome extension (MV3)      │
│  captures {url, cookie,     │
│  headers} from the FINAL    │
│  download host              │
└──────────────┬──────────────┘
               │ "Copy as JSON"
               ▼
        ┌──────────────┐
        │  CLIPBOARD   │  ← visible, inspectable by the user
        └──────┬───────┘
               │ paste
               ▼
┌─────────────────────────────┐
│ TUI (google_takeout_tui.py) │
│  parse_payload() → download │
└─────────────────────────────┘
```

There is **no HTTP server, no localhost socket, no native messaging, and
no port**. The clipboard is the entire transport. This removes the
attack surface that the old auto-send-to-localhost flow carried, and it
works on any machine without firewall or permission prompts.

## Components

| File | Role |
|------|------|
| `takeout.py` | Core download engine: URL pattern parsing, parallel downloads, HTTP Range resume, ZIP integrity check, size-history tracking, cURL/PowerShell extractors. Also the CLI entry point (`main()` → launches the TUI). |
| `takeout_payload.py` | The schema contract between extension and TUI. `TakeoutPayload` dataclass with `from_json`, `from_curl`, `validate`, `to_json`, `to_curl`, and the top-level `parse_payload` auto-detector. |
| `google_takeout_tui.py` | The only interface. Textual app: paste area, settings, live downloads table, log, and the cookie-expiry **refresh alert** (bell + title flash). |
| `aria2c_integration.py` | Optional aria2c RPC backend for high-speed downloads. |
| `dedupe_takeout.py` | Post-download hash-based deduplication helper. |
| `helpers/` | The MV3 Chrome/Edge extension (capture-only). |

## The payload schema (v1)

Defined and enforced in `takeout_payload.py`. Both sides MUST conform.

```json
{
  "schema": 1,
  "captured_at": "2026-06-14T01:23:45.678Z",
  "source": "extension",
  "url": "https://takeout-download.usercontent.google.com/download/takeout-...zip?j=...&i=0&user=...",
  "method": "GET",
  "headers": {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept": "text/html,...",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://takeout.google.com/"
  },
  "cookie": "SID=...; HSID=...; __Secure-1PSID=...; ..."
}
```

Notes:

- The schema is **capture-only**. It carries no server settings, output
  directory, parallelism, or auth — those are TUI-side inputs.
- `from_json` is strict: it rejects unknown schema versions, missing
  `url`/`cookie`, and URLs that don't look like Takeout links.
- `validate()` returns `(ok, message)`. It hard-fails when the cookie
  has none of the known Google session markers (`__Secure-1PSID`,
  `__Secure-3PSID`, `SID`, `HSID`, ...) because that almost always means
  the capture happened **before** the redirect chain completed. It
  soft-warns (ok=True, message set) when the capture is older than 60
  minutes.

## The redirect-chain gotcha

This is the single most important thing the design works around.

Google Takeout download links redirect across several domains before
landing on `takeout-download.usercontent.google.com`. The session
cookies that authorize the download (`__Secure-1PSID`, `__Secure-3PSID`,
`SIDCC`, ...) are only valid on that **final** host. Cookie-export
extensions and "Copy as cURL" on the pre-redirect request capture
cookies tied to `takeout.google.com`, which the download host rejects —
every download then returns an HTML login page instead of a ZIP.

The extension captures from the final host directly (it listens on
`takeout-download.usercontent.google.com/*`), so the cookies are correct.
If it ever captures a pre-redirect request, it tags the capture
`pre_redirect: true` and the popup warns the user.

Reference: <https://blog.omgmog.net/post/downloading-google-takeout-to-a-nas/>

## Cookie expiry and the refresh alert

Google sessions expire after a handful of files (~5–7 files / ~10–15 GB
in practice). When a download comes back as HTML, a login redirect, a
401/403, or a too-small/non-ZIP body, the engine reports `AUTH_FAILED`.

The TUI then:

1. Stops the in-flight downloads.
2. Enters "needs refresh" mode: red border on the input panel, an alert
   panel with instructions, and the Start button relabels to **Resume**.
3. Rings the terminal bell (`App.bell()`) and flashes the title bar
   every 5 seconds (`REFRESH_ALERT_INTERVAL`) until the user acts.

The user re-captures in the browser, clicks the extension's **Copy as
JSON**, pastes it in, and clicks **Resume**. The downloader reloads the
fresh cookie via the `to_curl()` bridge and continues from the last good
file. Cumulative stats are preserved across a refresh; a fresh Start
resets them.

## Download engine details

- **Resume**: partial downloads are written to `*.downloading` temp
  files. On retry, an HTTP `Range` header resumes from the last byte.
  The file is renamed to its final `.zip` only after integrity passes.
- **Integrity**: before finalizing, the engine checks for the ZIP
  end-of-central-directory signature (`PK\x05\x06`) in the last 1 KB.
- **Auth detection** is layered: HTTP status (401/403), final response
  URL (`accounts.google`), `Content-Type: text/html`, first-chunk ZIP
  magic bytes (`PK`), and a minimum-size floor.
- **Retries**: transient HTTP/network errors retry with exponential
  backoff up to `MAX_RETRIES`.
- **Path safety**: the output directory is restricted to an allowlist
  (cwd, home, `/opt/`, `/downloads/`, `/tmp/`) via
  `validate_output_dir`.

## What was removed in v6.0

- `google_takeout_web.py` (Flask + SocketIO web UI, 1768 lines).
- `Dockerfile`, `docker-compose.yml` (the web deployment).
- `helpers/takeout-extractor.user.js` (Tampermonkey userscript).
- `helpers/bookmarklet.html` (bookmarklet).
- Flask / Flask-SocketIO dependencies.
- The extension's auto-send-to-server logic, `serverUrl`/auth fields, and
  the per-session remote-host confirmation prompt.

See [`CHANGELOG.md`](../CHANGELOG.md) for the full version history and
[`BEST_PRACTICES.md`](./BEST_PRACTICES.md) for the research that informed
the design.
