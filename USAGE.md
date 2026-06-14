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
6. [Run the TUI](#6-run-the-tui)
7. [Cookie expiry and the refresh alert](#7-cookie-expiry-and-the-refresh-alert)
8. [cURL fallback (no extension)](#8-curl-fallback-no-extension)
9. [aria2c integration](#9-aria2c-integration)
10. [Deduplication](#10-deduplication)
11. [Configuration reference](#11-configuration-reference)
12. [Troubleshooting](#12-troubleshooting)

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

## 6. Run the TUI

```bash
python takeout.py
```

1. Paste the JSON payload into the big text area at the top.
2. Set the output directory, max files, and parallel count.
3. Click **▶ Start** (or press `S`).

The TUI shows a live table of active downloads, a stats line (done / failed /
skipped / MB / speed), and a scrolling log.

Keys: `S` start · `X` stop · `C` clear log · `Q` quit.

Settings:

| Field | Meaning | Default |
|-------|---------|---------|
| Output dir | Where parts are saved (must be under cwd, home, `/opt/`, `/downloads/`, `/tmp/`) | `./downloads` |
| Max files | Highest part number to try (stops early on the first 404) | `100` |
| Parallel | Concurrent downloads, 1–20 | `1` |

---

## 7. Cookie expiry and the refresh alert

Google sessions for Takeout downloads expire fast — often after ~5–7 parts
(~10–15 GB). When that happens mid-run, the TUI:

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

## 8. cURL fallback (no extension)

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

## 9. aria2c integration

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

## 10. Deduplication

After downloading and extracting, `dedupe_takeout.py` finds and collapses
duplicate files (common across overlapping Takeout exports):

```bash
python dedupe_takeout.py --help
```

---

## 11. Configuration reference

The TUI reads a few values from the environment (or a local `.env`, loaded
automatically if `python-dotenv` is installed). All are optional.

| Variable | Meaning | Default |
|----------|---------|---------|
| `OUTPUT_DIR` | Default output directory | `./downloads` |
| `PARALLEL_DOWNLOADS` | Default parallel count (1–20) | `1` |
| `FILE_COUNT` | Default max part count (1–1000) | `100` |
| `MAX_RETRIES` | Retries per part on transient errors | `3` |
| `RETRY_BACKOFF` | Exponential backoff base seconds | `2.0` |

Output directories are restricted to the current working directory, your home
directory, `/opt/`, `/downloads/`, and `/tmp/` to prevent path traversal.

---

## 12. Troubleshooting

**Every download saves an HTML file instead of a zip.**
The cookie was captured pre-redirect (wrong host). Re-capture from a request
to `takeout-download.usercontent.google.com`. The extension flags this for you.

**Downloads stop after a handful of files.**
Normal — the session expired. Watch for the bell + title flash, re-capture,
paste, and Resume.

**"Cookie doesn't contain any known Google session markers."**
The payload's cookie is missing `__Secure-1PSID` / `SID` etc. You captured the
wrong request. Click Download again and re-capture.

**"Output directory is outside allowed directories."**
Choose a path under cwd, home, `/opt/`, `/downloads/`, or `/tmp/`.

**aria2c rejects the download / returns errors with high `-x`.**
Use `-x 1 -s 1 -c`. Google blocks multi-stream Takeout downloads.

**The bell doesn't make a sound.**
Some terminals map the bell to a silent visual flash. The title-bar flash and
red alert panel still fire regardless. Check your terminal's bell settings.
