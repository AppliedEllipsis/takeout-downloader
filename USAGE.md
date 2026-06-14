# Google Takeout Downloader — Usage Guide

**Version 6.0.0**

A terminal tool for downloading Google Takeout archives with resumable,
parallel downloads and optional aria2c acceleration. A browser extension
captures the download payload (URL + cookies + headers) and hands it to the
TUI as JSON via your clipboard. You are the relay — nothing is sent over the
network between the browser and the TUI.

---

## Table of Contents

1. [How it works](#1-how-it-works)
2. [Install](#2-install)
3. [Install the browser extension](#3-install-the-browser-extension)
4. [Create a Takeout export](#4-create-a-takeout-export)
5. [Capture a payload](#5-capture-a-payload)
6. [Run the CLI](#6-run-the-cli)  ← recommended
7. [Run the TUI (opt-in)](#7-run-the-tui-opt-in)
8. [Cookie expiry and the refresh alert](#8-cookie-expiry-and-the-refresh-alert)
9. [cURL fallback (no extension)](#9-curl-fallback-no-extension)
10. [aria2c integration](#10-aria2c-integration)
11. [Deduplication](#11-deduplication)
12. [Configuration reference](#12-configuration-reference)
13. [Troubleshooting](#13-troubleshooting)
14. [After the download: processing photo archives](#14-after-the-download-processing-photo-archives)

---

## 1. How it works

```
Browser (takeout.google.com starts a download)
  → extension captures {url, cookie, headers} from the FINAL download host
  → click the extension → "Copy as JSON"  →  your clipboard
  → paste the JSON into the TUI → Start
  → TUI downloads every part in parallel, with resume + integrity checks
```

There is no server, no localhost port, and no auto-send. The payload lives
on your clipboard and is visible/inspectable before you paste it. When the
Google session expires mid-download, the TUI rings the terminal bell and
flashes its title; you re-capture and paste a fresh payload to resume.

---

## 2. Install

### Prerequisites

- Python 3.9+
- A Google account with a Takeout export ([takeout.google.com](https://takeout.google.com))
- A Chromium-based browser (Chrome, Edge, Brave) for the extension
- Optional: `aria2c` for high-speed multi-connection downloads

### Steps

```bash
cd takeout_downloader_script

# Install dependencies
pip install -r requirements.txt

# Launch the TUI
python takeout.py
```

### Or run it in Docker

Both the CLI and the TUI are self-contained in a single image — Python
dependencies and `aria2c` are baked in, so the only host requirement is
Docker.

Both services are **interactive** (they need a real terminal), so you do
*not* start them as background daemons. Use `run`, which attaches your
terminal:

**CLI** (default service, recommended for SSH/tmux/Docker):

```bash
docker compose build
docker compose run --rm takeout-cli
```

To pin a custom output directory, create a `.env` file (gitignored) in the
project root:

```bash
cat > .env << 'EOF'
OUTPUT_DIR=/srv/storage/google-takeout/my-takeout
PARALLEL_DOWNLOADS=3
MAX_PARTS=500
ALLOWED_DIRS=/opt:/downloads
EOF
```

Then run with no flags — the CLI picks up `OUTPUT_DIR` from `.env`:

```bash
docker compose run --rm takeout-cli   # no flags needed
```

NOTE: Docker Compose has a quirk where `docker compose run --rm takeout-cli
--out /path` (with equals) and `--out /path` (with space) both fail because
runc treats `--out` as an executable. To avoid this, always use `.env` or
the `OUTPUT_DIR` env var. The CLI also persists the last-used folder to
`~/.takeout-cli.json` after the first prompt, so subsequent runs default to
the same folder automatically.

**TUI** (opt-in via profile, use only on a local terminal):

```bash
docker compose --profile tui run --rm takeout
```

Notes:

- Downloads land in `./downloads` on the host. Resume state
  (`.downloading` parts, `.takeout_sizes.json`) persists there too, so a
  removed container never loses progress. Inside the container the output
  dir is always `/downloads`.
- Configure defaults by creating a `.env` (copied from `.env.example`);
  it is optional and loaded automatically if present.
- There is no exposed port and no server. The browser extension still
  runs in your normal browser on the host — you copy the JSON payload and
  paste it into the containerized TUI exactly as you would natively.
- `aria2c` is already on the image's PATH, so the TUI auto-detects it.

---

## 3. Install the browser extension

The extension lives in `helpers/`. It is a Manifest V3 extension.

1. Open your browser's extensions page:
   - Chrome/Brave: `chrome://extensions`
   - Edge: `edge://extensions`
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select the `helpers/` directory.
4. The "Takeout Downloader Helper" icon appears in your toolbar. Pin it.

The extension only requests access to Google Takeout hosts. It makes **no
network calls** of its own — it just reads the request headers your browser
already sends and stores the latest capture locally.

---

## 4. Create a Takeout export

1. Go to [takeout.google.com](https://takeout.google.com).
2. Select the products you want (e.g. Google Photos).
3. Choose **Send download link via email** as the delivery method.
4. Pick `.zip` and an archive size (2 GB or 4 GB is fine — more, smaller
   parts means each part finishes before the cookie can expire).
5. Wait for the email, then open the download page.

---

## 5. Capture a payload

1. On the Takeout download page, click **Download** on any part. Save it or
   cancel the browser's own download — the click is what matters; it makes
   the browser issue the authenticated request the extension captures.
2. Click the extension icon. You should see:
   `✓ Captured: takeout-…-001.zip (3s ago, cookie 1234 chars, #1)`
3. Click **Copy as JSON**.

The JSON looks like this (cookie shown truncated):

```json
{
  "schema": 1,
  "captured_at": "2026-06-14T12:00:00.000Z",
  "source": "extension",
  "url": "https://takeout-download.usercontent.google.com/download/takeout-20260614T071725Z-3-001.zip?j=...&i=0&user=...&authuser=0",
  "method": "GET",
  "headers": {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept": "text/html,...",
    "Referer": "https://takeout.google.com/"
  },
  "cookie": "__Secure-1PSID=...; __Secure-3PSID=...; SID=...; HSID=..."
}
```

> **Pre-redirect warning.** If the popup warns about a "pre-redirect
> capture", the cookies were captured on `takeout.google.com` rather than
> the final `takeout-download.usercontent.google.com` host, and the download
> will fail with an HTML login page. Click Download again and re-capture —
> the extension prefers the final host automatically.

---

## 6. Run the CLI

The CLI is the recommended entrypoint — it works reliably over
SSH → tmux → Docker chains and uses aria2c's native progress display.

### Native

```bash
python takeout_cli.py
```

You'll see:

```
==========================================
  Google Takeout Downloader — paste, go.
==========================================

Paste the JSON payload from the browser extension.
  (Right-click in terminal -> Paste, then press Enter.)
  The reader detects when the JSON is complete automatically.
  Press Ctrl-C to quit.
```

Right-click in the terminal, choose **Paste**, press **Enter**. The
reader auto-detects when the JSON is complete (brace-balance scan,
string-aware) so you don't need Ctrl-D.

The CLI then:

1. **Discovers** all parts (numbered ZIPs) via 1-byte Range probes —
   this also validates auth up front.
2. **Downloads** with aria2c, native console progress (speed/ETA/total).
3. **Verifies** each part: size matches the probe + ZIP EOCD signature.
4. **Resumes** partials with aria2c's `-c` flag.
5. **Re-prompts** for a fresh JSON if the cookie dies mid-run.

### Docker

```bash
docker compose build                  # one-time
docker compose run --rm takeout-cli   # paste the JSON at the prompt
```

Or pipe a file from the host (handy for scripting):

```bash
echo '<paste your JSON here>' > downloads/in.json
docker compose run --rm takeout-cli < downloads/in.json
```

### Useful flags

```bash
python takeout_cli.py -p 5              # 5 concurrent downloads
python takeout_cli.py --max-parts 200   # cap discovery at 200 parts
python takeout_cli.py --output-dir /srv/storage/google-takeout/me
```

### Useful env vars

| var | default | what |
|-----|---------|------|
| `OUTPUT_DIR` | auto (JuiceFS if present, else `./downloads`) | where archives land |
| `PARALLEL_DOWNLOADS` | `3` | concurrent downloads (`-j` to aria2c) |
| `MAX_PARTS` | `500` | discovery safety cap |
| `MAX_AUTH_REPROMPTS` | `5` | fresh-cookie prompts before giving up |
| `TAKEOUT_LOG_FILE` | `<output>/takeout_cli.log` | log file path |
| `TAKEOUT_LOG_MAX_BYTES` | `512000` | rotate log at this size |
| `TAKEOUT_LOG_BACKUP_COUNT` | `3` | keep N rotated backups |
| `NO_COLOR` | unset | disable ANSI colours |

### After a run

```bash
# Structured summary
python takeout_cli_analyze.py downloads/takeout_cli.log

# Machine-readable
python takeout_cli_analyze.py downloads/takeout_cli.log --json

# Live tail
python takeout_cli_analyze.py downloads/takeout_cli.log --follow
```

---

## 7. Run the TUI (opt-in)

The TUI is opt-in because Textual's paste/redraw is fragile over SSH
→ tmux → Docker. Use it only on a local terminal.

### Native

```bash
python takeout.py
```

The TUI's payload box is focused on launch. If paste doesn't work
(bracketed-paste markers get stripped over SSH+tmux+bracketed-paste),
drop a file into the output dir as `in.json`, `payload.json`, or
`curl.txt` and type a single `.` in the payload box.

### Docker

```bash
docker compose --profile tui run --rm takeout
```

## 8. Cookie expiry and the refresh alert

Google sessions for Takeout downloads expire fast — often after ~5–7 parts
(~10–15 GB). When that happens mid-run:

### CLI behavior

The CLI's discovery probe is also its auth check — if Google returns a
signin page (302 → `accounts.google.com` → HTML) on any probe, the CLI
prints a re-prompt:

```
[ERROR] Auth failed during discovery (redirected to accounts.google.com).
[INFO]  Re-capture in your browser, then paste the new JSON below.
```

You right-click in the terminal → Paste the fresh JSON → Enter. The CLI
returns to discovery, finds the parts that aren't fully downloaded yet,
and aria2c resumes the partials with `-c`. If auth keeps failing,
after `MAX_AUTH_REPROMPTS` (default 5) the CLI exits cleanly so the
session isn't burned.

### TUI behavior

When the cookie dies, the TUI:

- **Rings the terminal bell** (audible) every 5 seconds.
- **Flashes the title bar** between the normal title and
  `🔔 NEEDS FRESH CAPTURE`.
- Shows a red **COOKIE EXPIRED** alert panel and turns the border red.
- Relabels **Start** to **▶ Resume**.

To resume:

1. Go back to the browser, click **Download** on a part again.
2. Extension → **Copy as JSON**.
3. Paste the fresh JSON over the old one in the TUI.
4. Click **▶ Resume** (or press `S`).

Only the parts that haven't completed are re-downloaded. Partial `.downloading`
files resume from their last byte via HTTP Range. Cumulative stats are
preserved across a resume.

Press `X` (Stop) during a refresh alert to silence it and abandon the run.

---

## 9. cURL fallback (no extension)

If you can't install the extension (Safari, locked-down browser), paste a
cURL command instead — the TUI auto-detects JSON vs cURL.

1. On the Takeout download page, open DevTools (`F12`) → **Network**.
2. Click **Download** on a part. Pause the browser download.
3. Find the request to `takeout-download.usercontent.google.com`.
4. Right-click → **Copy** → **Copy as cURL** (bash or PowerShell both work).
5. Paste into the TUI and click Start.

The cURL path backfills missing headers (User-Agent / Accept / Referer) with
known-good defaults, since pasted cURL often strips them.

---

## 10. aria2c integration

For maximum throughput, install aria2c:

```bash
apt install aria2     # Debian/Ubuntu
brew install aria2    # macOS
```

When `aria2c` is on your PATH, the TUI reports it as available on startup.

> **Important for Takeout:** Google only allows **single-stream** downloads
> from the download host. Multi-connection splitting (`-x N -s N` with N > 1)
> gets rejected. Use `-x 1 -s 1 -c` — one connection, continue/resume on. The
> benefit of aria2c here is robust resume and retry, not parallel segments.

---

## 11. Deduplication

After downloading and extracting, `dedupe_takeout.py` finds and collapses
duplicate files (common across overlapping Takeout exports):

```bash
python dedupe_takeout.py --help
```

---

## 12. Configuration reference

Both the CLI and the TUI read values from the environment (or a local
`.env`, loaded automatically if `python-dotenv` is installed). All are
optional.

### Shared

| Variable | Meaning | Default |
|----------|---------|---------|
| `OUTPUT_DIR` | Default output directory | `./downloads` (or JuiceFS path if present) |
| `PARALLEL_DOWNLOADS` | Concurrent downloads | CLI: `3`, TUI: `1` |
| `MAX_RETRIES` | Retries per part on transient errors | `6` |
| `RETRY_BACKOFF` | Exponential backoff base seconds | `2.0` |
| `RETRY_MAX_WAIT` | Cap on a single backoff sleep (seconds) | `120.0` |

### CLI only

| Variable | Meaning | Default |
|----------|---------|---------|
| `MAX_PARTS` | Discovery safety cap | `500` |
| `MAX_AUTH_REPROMPTS` | Fresh-cookie prompts before giving up | `5` |
| `TAKEOUT_LOG_FILE` | Log file path | `<output>/takeout_cli.log` |
| `TAKEOUT_LOG_MAX_BYTES` | Rotate log at this size | `512000` |
| `TAKEOUT_LOG_BACKUP_COUNT` | Keep N rotated backups | `3` |
| `NO_COLOR` | Disable ANSI colours | unset |

### TUI only

| Variable | Meaning | Default |
|----------|---------|---------|
| `FILE_COUNT` | Default max part count (1–1000) | `100` |
| `TAKEOUT_SETTINGS` | Path to settings JSON | `~/.takeout_downloader.json` |
| `TAKEOUT_LOG_FILE` | Mirror log lines to this file | `./takeout.log` |
| `ALLOWED_DIRS` | Extra allowed output roots (`:`-separated) | unset |

### Retry behaviour (both)

Retries use exponential backoff with **full jitter** (a random wait between 0
and the capped exponential) so parallel workers don't retry in lockstep and
hammer Google at the same instant. On HTTP `429`/`503` both UIs honour the
`Retry-After` header when present.

Output directories are restricted to the current working directory, your home
directory, `/opt/`, `/downloads/`, and `/tmp/` to prevent path traversal.

---

## 13. Troubleshooting

**CLI: `python: can't open file '/app/takeout_cli.py': [Errno 2] No such file`**
The Docker image you're running is stale. The Dockerfile didn't include
`takeout_cli.py` until recently — rebuild:

```bash
docker compose build --no-cache takeout-cli   # full rebuild
# or just:
docker compose build
```

**TUI: `docker compose run --rm takeout` exits silently with no output**
The TUI service is behind the `--profile tui` profile now. Run:

```bash
docker compose --profile tui run --rm takeout
```

**Every download saves an HTML file instead of a zip.**
The cookie was captured pre-redirect (wrong host). Re-capture from a request
to `takeout-download.usercontent.google.com`. The extension flags this for you.

**Downloads stop after a handful of files.**
Normal — the session expired.

- **CLI**: re-prompts for a fresh JSON. Paste it, Enter, it resumes.
- **TUI**: watch for the bell + title flash, re-capture, paste, click ▶ Resume.

**"Cookie doesn't contain any known Google session markers."**
The payload's cookie is missing `__Secure-1PSID` / `SID` etc. You captured the
wrong request. Click Download again and re-capture.

**"Output directory is outside allowed directories."**
Choose a path under cwd, home, `/opt/`, `/downloads/`, or `/tmp/`. The CLI
falls back to `./downloads` automatically; the TUI shows an error toast.

**aria2c rejects the download / returns errors with high `-x`.**
Use `-x 1 -s 1 -c`. Google blocks multi-stream Takeout downloads. The CLI
already passes these flags.

**The bell doesn't make a sound.**
Some terminals map the bell to a silent visual flash. The title-bar flash and
red alert panel still fire regardless. Check your terminal's bell settings.

**CLI: paste doesn't seem to do anything when I right-click in tmux.**
SSH + tmux + Docker strips bracketed-paste markers. Workarounds:

1. Use `--output-dir` and pipe a file: `python takeout_cli.py < in.json`
2. Drop the JSON in the output dir as `in.json` and run the CLI normally —
   the prompt will read it from disk
3. Use `PAYLOAD_FILE=./in.json python takeout_cli.py`

**Downloads fail immediately with 403/401 even right after a fresh capture.**
Two different expiries are at play. The *session cookie* expires fast (minutes
to a few parts) — re-capture and Resume. The *export itself* (the signed
download URLs) expires about a **week** after Google generates it; once that
happens no cookie will help and you must regenerate the export from
[takeout.google.com/manage](https://takeout.google.com/manage). (Distinction
documented by `tarballz/mass-takeout-downloader`.)

---

## 14. After the download: processing photo archives

This tool gets the `.zip`/`.tgz` parts onto disk. If your export is **Google
Photos**, the extracted archive needs post-processing — Google stores each
photo's real date and GPS in a sidecar `.json` file rather than in the image
EXIF, so a naive import lands everything with the wrong timestamps. These
companion tools fix that (none are affiliated with this project):

- **[GooglePhotosTakeoutHelper (`gpth`)](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper)**
  — organizes the whole archive into one chronological folder, restores dates,
  handles albums. Has a no-UI mode for headless/NAS use.
- **[google-photos-exif](https://github.com/mattwilson1024/google-photos-exif)**
  — writes the missing `DateTimeOriginal` EXIF from the JSON sidecars.
- **[purarue/google_takeout_parser](https://github.com/purarue/google_takeout_parser)**
  — parses the *non-photo* data (Search history, Activity, YouTube, Location)
  into typed Python objects.

Export tips that make post-processing easier (learned from those projects):

- When the format choice is offered, **export JSON, not HTML** — the HTML
  parsers downstream are slow and fragile.
- For Google Photos, **deselect custom (non-date) albums** at export time, or
  every photo gets duplicated (once under its date album, once under each
  custom album).
- If your export spans multiple archives, **merge the `Takeout/` folders**
  (merge same-named subfolders, don't overwrite) before running any of the
  photo tools.

See [`docs/RELATED_PROJECTS.md`](./docs/RELATED_PROJECTS.md) for the full
survey of tools studied and what this project borrowed from each.
