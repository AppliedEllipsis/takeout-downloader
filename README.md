# 📦 Google Takeout Bulk Downloader

Download Google Takeout archives from a terminal with resume, parallel
downloads, ZIP integrity checks, and optional aria2c acceleration. A
browser extension captures the authenticated download request and hands
it to the TUI as a JSON payload — **you** are the transport, so nothing
is auto-sent anywhere.

> **License:** [GLWTS (Good Luck With That Shit) Public License](./LICENSE).
> **Forked from:** [`Kalainilavann/takeout_downloader_script`](https://github.com/Kalainilavann/takeout_downloader_script) — see [`NOTICE.md`](./NOTICE.md) for attribution, license change history, and what was removed from the upstream repo before publication.

## How it works

```
Browser (takeout.google.com)
  │  start a download; the extension captures the request to the FINAL
  │  download host (takeout-download.usercontent.google.com)
  ▼
Chrome/Edge extension  →  "Copy as JSON"  →  your clipboard
  ▼
You paste the JSON into the TUI  →  Start
  ▼
TUI downloads every part (001..NNN) in parallel, with resume + ZIP checks
  ▼
Cookie expires after a few files?  →  TUI rings the bell + flashes its
title bar  →  re-capture in the browser, Copy as JSON, paste, click Resume
```

There is **no web server, no localhost port, and no auto-send**. The
extension only ever writes to your clipboard. The TUI only ever reads
what you paste into it.

## Quick start

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Load the browser extension (Chrome / Edge / any Chromium browser)
#    chrome://extensions → Enable "Developer mode" → "Load unpacked"
#    → select the helpers/ folder

# 3. Launch the TUI
python takeout.py
```

### Or run it in Docker

The CLI is fully self-contained in a container (Python deps + `aria2c`
baked in). It is interactive, so use `run` — not `up -d`:

```bash
docker compose build
docker compose run --rm takeout-cli
```

The TUI is still bundled for users who want a UI on a local terminal —
it's hidden behind the `tui` profile so it doesn't appear in the default
service list:

```bash
docker compose --profile tui run --rm takeout
```

Downloads land in `./downloads` on the host (resume state persists there
too). Set `OUTPUT_DIR` and other defaults in `.env` if you like. There is
no server, no exposed port, and no daemon. The browser extension still
runs in your real browser on the host; the clipboard → paste flow is
identical to running natively.

Then:

1. Go to [takeout.google.com](https://takeout.google.com) → **Manage exports**.
2. Click **Download** on any part. Let it start (so the request hits the
   real download host), then you can cancel the browser download.
3. Click the extension icon → **Copy as JSON**.
4. Paste into the TUI's payload box, set the output dir / file count /
   parallelism, and click **▶ Start**.

### CLI variant (no TUI, friendlier over SSH)

A simpler terminal-only path for `SSH → tmux → Docker` chains where
Textual's paste/redraw is fragile. It uses aria2c directly, prints native
progress (speed/ETA/total), and re-prompts for a fresh capture if the
cookie expires mid-run:

```bash
docker compose build
echo '<paste your JSON here>' > downloads/in.json
docker compose run --rm takeout-cli
```

Or pipe via stdin (the CLI auto-detects JSON completion by brace-balance,
so you don't need a sentinel):

```bash
docker compose run --rm takeout-cli < in.json
```

The CLI searches these payload locations automatically — you can just drop
the file and run:

- `<output_dir>/in.json`, `payload.json`, or `curl.txt`
- `/downloads/in.json`, `/downloads/drop/in.json` (mounted)
- `/work/in.json` (the project folder, mounted)
- Or `PAYLOAD_FILE=/path/to/in.json docker compose run --rm takeout-cli`

Useful env vars (also `.env`):

| var | default | what |
|-----|---------|------|
| `PARALLEL_DOWNLOADS` | `3` | concurrent downloads (`-j` to aria2c) |
| `MAX_PARTS` | `500` | discovery safety cap |
| `MAX_AUTH_REPROMPTS` | `5` | how many times to ask for a fresh cookie before giving up |
| `OUTPUT_DIR` | auto (JuiceFS if present, else `./downloads`) | where archives land |

### No extension? Paste cURL instead

The TUI auto-detects the input format. If you can't install the
extension (Safari, locked-down browser), open DevTools → Network, start a
download, right-click the request to
`takeout-download.usercontent.google.com` → **Copy as cURL** (bash or
PowerShell), and paste that into the same box.

## The JSON payload

The extension produces a self-contained, inspectable payload — you can
read exactly what you're pasting before you paste it:

```json
{
  "schema": 1,
  "captured_at": "2026-06-14T01:23:45.000Z",
  "source": "extension",
  "url": "https://takeout-download.usercontent.google.com/download/takeout-20260614T012345Z-1-001.zip?j=...&i=0&user=...",
  "method": "GET",
  "headers": {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept": "text/html,...",
    "Referer": "https://takeout.google.com/"
  },
  "cookie": "__Secure-1PSID=...; __Secure-3PSID=...; SID=...; ..."
}
```

The schema lives in [`takeout_payload.py`](./takeout_payload.py) and is
the single source of truth for both sides. The TUI validates it before
downloading and warns you if:

- the cookie is missing Google session markers (`__Secure-1PSID`, etc.) —
  usually means the request was captured **before** the redirect chain
  completed, and downloads will return an HTML login page;
- the capture is over an hour old (sessions expire).

### Why JSON and not just cURL?

Google rejects download requests whose `User-Agent` / `Accept` /
`Referer` don't match what Chrome sent. JSON round-trips those headers
losslessly. cURL pasted from PowerShell frequently mangles them.

### Why capture from the final host?

The download links redirect across several Google domains before landing
on `takeout-download.usercontent.google.com`. The cookies that actually
authorize the download (`__Secure-1PSID`, `__Secure-3PSID`, `SIDCC`, …)
are only valid on that **final** host. Cookie-export extensions that grab
them from `takeout.google.com` capture the wrong domain and every
download fails. The extension listens on the final host and tags
pre-redirect captures with a warning. (See
[omgmog.net](https://blog.omgmog.net/post/downloading-google-takeout-to-a-nas/)
for the full write-up of this gotcha.)

## Features

- **Parallel downloads** — configurable 1–20 concurrent parts.
- **Resume** — `.downloading` temp files + HTTP Range; an interrupted
  part resumes from the last byte written.
- **ZIP integrity** — verifies the end-of-central-directory record before
  promoting a temp file to its final `.zip`, catching truncation.
- **Cookie-expiry alert** — audible terminal bell + flashing title bar
  when the session dies mid-run. Paste a fresh capture and click Resume;
  only the unfinished parts re-download.
- **Auth detection** — checks status code, `Content-Type`, redirect
  target, and ZIP magic bytes to catch expired cookies that still
  return HTTP 200.
- **aria2c backend (optional)** — hand off to aria2c for faster,
  resumable transfers (see below).
- **cURL fallback** — bash and PowerShell formats both accepted.

## aria2c acceleration (optional)

```bash
apt install aria2     # Debian/Ubuntu
brew install aria2    # macOS
```

When `aria2c` is on your PATH the TUI detects it on startup. Note that
Google only permits **single-stream** downloads for Takeout, so the
right aria2c flags are `-x 1 -s 1 -c` — more connections per file get
throttled or rejected. See [`aria2c_integration.py`](./aria2c_integration.py).

## Project structure

```
├── takeout.py                # Core engine: parsing, download, resume, integrity
├── google_takeout_tui.py     # Textual terminal UI (the only interface)
├── takeout_payload.py        # JSON payload schema shared with the extension
├── aria2c_integration.py     # Optional aria2c RPC backend
├── dedupe_takeout.py         # Deduplicate downloaded archives by hash
├── build.py                  # PyInstaller single-binary build
├── Dockerfile                # Self-contained TUI image (Python deps + aria2c)
├── docker-compose.yml        # `docker compose run --rm takeout-cli`
│                             # (`takeout` is the TUI service, profile `tui`)
├── requirements.txt          # Python dependencies
└── helpers/                  # Browser extension (Chromium MV3)
    ├── manifest.json
    ├── background.js         # Capture service worker (clipboard only, no network)
    ├── popup.html / popup.js # Copy as JSON / Copy as cURL
    ├── options.html / options.js
    └── icon16/48/128.png
```

## Documentation

- [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — how the extension, payload schema, and TUI fit together; the capture/refresh flow.
- [`docs/EXTENSION.md`](./docs/EXTENSION.md) — installing and using the browser extension.
- [`USAGE.md`](./USAGE.md) — step-by-step usage guide.
- [`docs/BEST_PRACTICES.md`](./docs/BEST_PRACTICES.md) — research notes on Takeout downloading.
- [`docs/RELATED_PROJECTS.md`](./docs/RELATED_PROJECTS.md) — survey of other Takeout tools and what this project borrowed from each.
- [`CHANGELOG.md`](./CHANGELOG.md) — version history.

## Security model

- The extension makes **no network calls that leave the browser**. It
  captures the request, stores it in `chrome.storage.local`, and writes
  to your clipboard only when you click a Copy button.
- The captured Google session cookie lives in the extension's sandboxed
  local storage and on your clipboard — treat both as sensitive.
- The TUI restricts the output directory to a set of allowed prefixes
  (cwd, home, `/opt/`, `/downloads/`, `/tmp/`) to prevent path-traversal
  writes.
- Nothing is logged or transmitted off-machine.

## Credits & acknowledgements

This project learned from a number of other Takeout tools. None are
affiliated, but each shaped a decision here — see
[`docs/RELATED_PROJECTS.md`](./docs/RELATED_PROJECTS.md) for the full
breakdown of what was borrowed (and what was deliberately left out).

- **[tarballz/mass-takeout-downloader](https://github.com/tarballz/mass-takeout-downloader)**
  — jittered/capped backoff, permanent-vs-transient failure split, the
  HTML-as-success trap, and the signed-URL-expiry vs cookie-expiry
  distinction. The single biggest influence on the retry engine.
- **[Croissanthology/takeout-choo-choo](https://github.com/Croissanthology/takeout-choo-choo)**
  — per-run workspace state and the "~5 parallel is the practical max"
  rate-cap guidance.
- **[cschladetsch/PyGoogleTakeoutDownloader](https://github.com/cschladetsch/PyGoogleTakeoutDownloader)**
  — part-range and inter-file-delay ideas (logged as future features); its
  stored-credential model was deliberately *not* adopted.
- **[Max Glenister](https://blog.omgmog.net/post/downloading-google-takeout-to-a-nas/)**
  & **[CorpIT](https://www.corpit.org/how-to-download-google-takeout-with-aria2/)**
  — the definitive cookie-redirect write-up and the exact aria2c flags.
- **[GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper)**,
  **[google-photos-exif](https://github.com/mattwilson1024/google-photos-exif)**,
  **[google-metadata-matcher](https://github.com/Greegko/google-metadata-matcher)**,
  **[google_takeout_parser](https://github.com/purarue/google_takeout_parser)**
  — post-download photo/metadata processing, linked for users as the next step.

## 📜 License & attribution

Licensed under the **[GLWTS (Good Luck With That Shit) Public
License](./LICENSE)**. Fork of
[`Kalainilavann/takeout_downloader_script`](https://github.com/Kalainilavann/takeout_downloader_script),
originally by **Clive Watts**. For full attribution, the license history,
and the safety audit performed before this fork went public, see
**[`NOTICE.md`](./NOTICE.md)**.

## Support This Project ❤️

If you find this extension useful, then please support its continued development:

### Crypto Donation

If you'd prefer to donate directly via cryptocurrency, you can send Bitcoin to:

```
bc1q8nrdytlvms0a0zurp04xwfppflcxwgpyrzw5hn
```

Thank you for supporting free and open source software! 🙏

### Co-vibe coded with AI

Built with human creativity enhanced by artificial intelligence.
