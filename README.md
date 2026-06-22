# 📦 Google Takeout Bulk Downloader

Download Google Takeout archives from a terminal with resume, parallel
downloads, ZIP integrity checks, and optional aria2c acceleration. A
browser extension captures the authenticated download request and hands
it to the TUI as a JSON payload — **you** are the transport, so nothing
is auto-sent anywhere.

> **License:** [GLWTS (Good Luck With That Shit) Public License](./LICENSE).
> **Forked from:** [`Kalainilavann/takeout_downloader_script`](https://github.com/Kalainilavann/takeout_downloader_script) — see [`NOTICE.md`](./NOTICE.md) for attribution, license change history, and what was removed from the upstream repo before publication.

> ⚠️ **Running on a remote server?** The download cookie is bound to the
> IP that started the export. Capture through a SOCKS tunnel so the browser
> uses the server's IP. See **[`docs/SERVER_DOWNLOAD.md`](./docs/SERVER_DOWNLOAD.md)**
> for the full workflow and **[`docs/TROUBLESHOOTING.md`](./docs/TROUBLESHOOTING.md)**
> for auth/cookie failures.

> 🌐 **Web-hosted browser + download manager (new).** Instead of the SOCKS
> tunnel + local proxied Chrome, you can run a real Chrome **on the server**
> inside a webtop (KasmVNC) container reached over a Cloudflare tunnel. Because
> the browser and the downloader share one server IP, the IP-bound cookie is
> always valid — no tunnel dance. A custom extension POSTs captures to a local
> FastAPI **manager** that drives the engine, shows live progress, alerts over
> Telegram, and can replay a workflow with no LLM. Output lands in dated
> per-account folders (`<account>/<export-ts>/`) with a `manifest.json`. See
> **[`docs/webgui/README.md`](./docs/webgui/README.md)** for the full design and
> **[`docs/webgui/09-smoke-test.md`](./docs/webgui/09-smoke-test.md)** to deploy.

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

## Commands

Every command you'll actually run, in one place. **CLI** is the
recommended path; **TUI** is opt-in for users with a local terminal.

### Setup (one-time per machine)

```bash
# Install Python deps for native (non-Docker) usage.
pip install -r requirements.txt

# Optional but recommended: install aria2c for the CLI's high-speed backend.
# Debian/Ubuntu:  apt install aria2
# macOS:          brew install aria2
```

### Load the browser extension (one-time per browser)

`chrome://extensions` (or `edge://extensions`) → enable **Developer
mode** → **Load unpacked** → select the `helpers/` folder. Pin the
extension to the toolbar so you can see the popup.

### Capture a JSON payload (every download)

1. [takeout.google.com](https://takeout.google.com) → **Manage exports**.
2. Click **Download** on any part. Let the request fire (so the redirect
   to `takeout-download.usercontent.google.com` completes), then cancel
   the browser download.
3. Click the extension icon → **Copy as JSON**.
4. Paste into your terminal (CLI) or the TUI's payload box.

### CLI — native Python (no Docker)

```bash
# Paste the JSON at the prompt, then Enter.
python takeout_cli.py

# Or pipe a saved file:
python takeout_cli.py < in.json

# Or point at a file explicitly:
PAYLOAD_FILE=./in.json python takeout_cli.py
```

Useful flags:

```bash
python takeout_cli.py -p 5          # 5 concurrent downloads
python takeout_cli.py --max-parts 200  # cap discovery at 200 parts
python takeout_cli.py --output-dir /srv/storage/google-takeout/me
python takeout_cli.py --engine aria2c  # use legacy aria2c subprocess instead
                                       # of the built-in downloader (default)
```

Useful env vars (also accept from `.env`):

| var | default | what |
|-----|---------|------|
| `OUTPUT_DIR` | auto (JuiceFS if present, else `./downloads`) | where archives land |
| `PARALLEL_DOWNLOADS` | `3` | concurrent downloads |
| `MAX_PARTS` | `500` | discovery safety cap |
| `MAX_AUTH_REPROMPTS` | `5` | fresh-cookie prompts before giving up |
| `TAKEOUT_LOG_FILE` | `<output>/takeout_cli.log` | log file path |
| `TAKEOUT_LOG_MAX_BYTES` | `512000` | rotate log at this size |
| `TAKEOUT_LOG_BACKUP_COUNT` | `3` | keep N rotated backups |
| `NO_COLOR` | unset | disable ANSI colours |

After a run, parse the log:

```bash
python takeout_cli_analyze.py downloads/takeout_cli.log
python takeout_cli_analyze.py downloads/takeout_cli.log --json
python takeout_cli_analyze.py downloads/takeout_cli.log --last=30
python takeout_cli_analyze.py downloads/takeout_cli.log --follow
```

### CLI — Docker (recommended for SSH→tmux→Docker)

```bash
docker compose build                       # one-time per machine
docker compose run --rm takeout-cli        # paste the JSON at the prompt

# Or pipe a file from the host:
echo '<paste your JSON here>' > downloads/in.json
docker compose run --rm takeout-cli < downloads/in.json
```

The container mounts:

- `./downloads` → `/downloads` (default output + resume state)
- `./drop` → `/downloads/drop` (drop box)
- `.` → `/work` (your project folder)
- `/opt` → `/opt` (recursive bind, JuiceFS submounts visible)

### TUI — native Python

```bash
python takeout.py
```

The TUI's payload box is focused on launch. If paste doesn't work (SSH
→ tmux + bracketed-paste markers get stripped), drop a file into the
output dir as `in.json`, `payload.json`, or `curl.txt` and type a
single `.` in the payload box.

### TUI — Docker (opt-in)

The TUI service is hidden behind the `tui` profile because it needs a
real terminal and Textual's paste/redraw is fragile over SSH.

```bash
docker compose --profile tui run --rm takeout
```

Same volumes and outputs as the CLI container.

### Inspecting logs

```bash
# Live-tail the CLI's log from another SSH window:
tail -f downloads/takeout_cli.log

# Live-tail the TUI's log:
tail -f downloads/takeout.log

# Structured summary (CLI only):
python takeout_cli_analyze.py downloads/takeout_cli.log
```

### Reset / wipe state

```bash
# Remove a partial download and start fresh:
rm downloads/*.downloading

# Wipe everything (downloads + state + logs):
rm -rf downloads/*
```

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
- **Internal downloader (default)** — in-process parallel `requests`
  downloader; exact live progress, HTTP `Range` resume, no external
  binary.
- **aria2c backend (optional / legacy)** — `--engine aria2c` hands off to
  aria2c instead (see below).
- **cURL fallback** — bash and PowerShell formats both accepted.

## aria2c engine (optional / legacy)

The CLI no longer needs aria2c — the internal downloader is the default.
You can still opt into aria2c with `--engine aria2c` (or
`TAKEOUT_ENGINE=aria2c`), which requires the binary on PATH:

```bash
apt install aria2     # Debian/Ubuntu
brew install aria2    # macOS
```

Note that Google only permits **single-stream** downloads for Takeout, so
the right aria2c flags are `-x 1 -s 1 -c` — more connections per file get
throttled or rejected. See [`aria2c_integration.py`](./aria2c_integration.py).

## Live grid (CLI)

The CLI downloads with its **internal in-process downloader** by default
(no external binary). It streams each part with `requests`, counts the
bytes itself, and feeds those exact counters straight to the grid — so
progress is always accurate and, crucially, works even when stdout is
**not** a TTY (the common SSH/tmux/Docker case). On a non-TTY it prints a
throttled one-line status instead of the grid, so a piped or logged run
still shows movement rather than dead silence.

When you run on a real terminal it draws a **self-anchoring grid** of
every part using ANSI escape codes — one row per file with a progress
bar, bytes, speed, and ETA. The grid is re-rendered in place several
times a second using *relative* cursor movement (it redraws over its own
previous block rather than jumping to a fixed screen row), so it never
collides with scrolled log output and there's no flicker. Each row
animates live from the downloader's byte counters:

```
  Pass 1 | 0/100 done | 3 active | 100 pending | output: /opt/storage...
  #001  [████·️️️️️️️️️️️·]  18%   1.4 GB/7.5 GB    50.2 MB/s  ETA  2m14s  takeout-...001.zip
  #002  [█··️️️️️️️️️️·]   5%   410.3 MB/7.5 GB  12.7 MB/s  ETA  9m38s  takeout-...002.zip
  #003  [·️️️️️️️️️️·]   0%   0 B/7.5 GB         0 B/s     ETA  --     takeout-...003.zip

  overall: 50 MB/s | eta: 5m
```

The grid is resize-aware: it repaints on `SIGWINCH`, clamps the visible
row count to the terminal height, and truncates each row to the terminal
width so a narrowed window never wraps a row. When space is tight the
filename and progress bar are protected; the size/speed/ETA columns drop
off first.

Set `NO_GRID=1` in the environment to force plain streaming output
(useful when piping to a file or running under `script(1)` for
recording). Pass `--no-color` (or set `NO_COLOR=1`) to strip ANSI colors
from the surrounding log lines.

### Download engine

The internal downloader is the default. It needs no external binary,
gives exact live progress, and detects Google's sign-in challenge page on
the **first chunk** — before a single byte is written to the archive — so
a challenged cookie can never corrupt an output file. Resume is plain
HTTP `Range:` from the on-disk size.

If you'd rather use the legacy `aria2c` subprocess (e.g. you already
depend on its tuning), pass `--engine aria2c` or set
`TAKEOUT_ENGINE=aria2c`. It needs `aria2c` on `PATH`.

## Project structure

```
├── takeout.py                # Core engine: parsing, download, resume, integrity
├── google_takeout_tui.py     # Textual TUI (opt-in, profile `tui`)
├── takeout_cli.py            # Terminal-only CLI (default service)
├── takeout_downloader.py     # In-process parallel HTTP downloader (default engine)
├── takeout_cli_analyze.py    # Offline log analyzer for takeout_cli
├── takeout_payload.py        # JSON payload schema shared with the extension
├── aria2c_integration.py     # Optional aria2c RPC backend (legacy --engine aria2c)
├── dedupe_takeout.py         # Deduplicate downloaded archives by hash
├── build.py                  # PyInstaller single-binary build
├── Dockerfile                # Self-contained image (Python deps + aria2c)
├── docker-compose.yml        # `docker compose run --rm takeout-cli`
│                             # (`takeout` is the TUI, profile `tui`)
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
