# Google Takeout Downloader — Usage Guide

**Version 5.0.0**

A self-hosted tool for downloading Google Takeout archives on a remote server with resumable, multi-threaded downloads, aria2c support, and browser helpers to capture download links.

---

## Table of Contents

1. [Quick Start (Docker)](#1-quick-start-docker)
2. [Authentication](#2-authentication)
3. [Getting Takeout Links](#3-getting-takeout-links)
4. [Browser Helpers](#4-browser-helpers)
   - [Option A: Chrome Extension (Recommended)](#option-a-chrome-extension-recommended)
   - [Option B: Tampermonkey Userscript](#option-b-tampermonkey-userscript)
   - [Option C: Bookmarklet](#option-c-bookmarklet)
5. [Web UI Usage](#5-web-ui-usage)
6. [TUI (Terminal) Usage](#6-tui-terminal-usage)
7. [aria2c Integration](#7-aria2c-integration)
8. [Configuration Reference](#8-configuration-reference)
9. [Security](#9-security)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Quick Start (Docker)

### Prerequisites

- Docker 20.10+ and Docker Compose v2+
- A Google account with a Takeout export ready ([takeout.google.com](https://takeout.google.com))

### Steps

```bash
# 1. Clone or download the project
cd takeout_downloader_script

# 2. Create your config from the template
cp .env.example .env

# 3. Edit .env — at minimum set auth credentials
#    AUTH_USER=your_username
#    AUTH_PASS=your_strong_password
#    See Section 8 for all options
nano .env   # or use any editor

# 4. Create the host mount directory for downloads
sudo mkdir -p /opt/takeout
sudo chown $(whoami) /opt/takeout

# 5. Start the container
docker compose up -d

# 6. Open in browser
#    http://your-server-ip:5001
#    Log in with AUTH_USER / AUTH_PASS
```

The web UI will be available at **port 5001** (remapped from container's 5000).

### Without Docker Compose

```bash
docker build -t takeout-downloader .

docker run -d \
  --name takeout-downloader \
  -p 5001:5000 \
  -e AUTH_USER=admin \
  -e AUTH_PASS=your_password \
  -e OUTPUT_DIR=/opt/takeout \
  -v /opt/takeout:/opt/takeout \
  -v /path/to/.env:/app/.env:ro \
  takeout-downloader
```

### Verify It's Running

```bash
# Check container health
docker inspect --format='{{.State.Health.Status}}' takeout-downloader
# Should print: healthy

# Test auth
curl -u admin:your_password http://localhost:5001/api/auth-check
# Should return: {"authenticated":true}
```

---

## 2. Authentication

**Authentication is required for any internet-facing deployment.** The server uses HTTP Basic Auth.

### Setting Credentials

In your `.env` file:

```env
AUTH_USER=admin
AUTH_PASS=changeme    # ← change this!
```

Or via Docker environment variables:

```bash
docker run -e AUTH_USER=admin -e AUTH_PASS=your_password ...
```

### How Auth Works

- **Web UI:** A login overlay appears when you open the page. Enter your credentials once — the browser caches them for the session.
- **API:** All endpoints require an `Authorization: Basic <base64>` header. Browsers handle this automatically after the first login prompt.
- **WebSocket:** SocketIO connections also require valid credentials (checked on connect).
- **No auth configured?** The server starts with a **warning** and allows unauthenticated access — only safe for localhost development.

### Security Notes

- Passwords are compared using `secrets.compare_digest()` (timing-attack safe)
- The cookie is **never** returned in any API response (stripped by `sanitize_state()`)
- All responses include `Content-Security-Policy`, `X-Frame-Options: DENY`, and `X-Content-Type-Options: nosniff` headers

---

## 3. Getting Takeout Links

Google Takeout downloads are authenticated — each file requires your browser cookies. You need to **capture the URL and cookie** from your browser and send them to the downloader server.

### The Problem

When Google finishes preparing your Takeout, you get download links like:

```
https://storage.cloud.google.com/takeout-20260613T071725Z-001.zip?...
```

These links only work with your browser cookies. You can't just `wget` them. You need to:

1. **Start a Takeout export** at [takeout.google.com](https://takeout.google.com)
2. **Wait for it to finish** (Google emails you when ready)
3. **Capture the URL and cookie** from your browser
4. **Send them to the downloader server**

### Three Ways to Capture

| Method | Ease | Captures httpOnly cookies | Auto-send | Install required |
|--------|------|---------------------------|------------|-------------------|
| **Chrome Extension** | ★★★ | ✅ Yes | ✅ Yes | One-time |
| **Tampermonkey Userscript** | ★★☆ | ⚠️ Partial* | ✅ Yes | Tampermonkey |
| **Bookmarklet** | ★☆☆ | ❌ No | ✅ Yes | Drag to bookmarks |

> \* The userscript intercepts `XMLHttpRequest` and `fetch` headers, but **cannot** read `httpOnly` cookies from `document.cookie`. The Chrome extension uses `webRequest` API which sees the full header including `httpOnly` cookies. **If your Takeout cookies are httpOnly (they likely are), use the Chrome extension.**

### Manual Method (No Helper Required)

If you don't want to install anything:

1. Open the Takeout page in your browser
2. Open DevTools → Network tab
3. Click a download link
4. Find the request in Network tab → right-click → **Copy as cURL (bash)**
5. Paste that cURL command into the Web UI's "cURL Input" field

This works for any cookie type (httpOnly or not) because the browser copies the full headers.

---

## 4. Browser Helpers

### Option A: Chrome Extension (Recommended)

The most robust method. Captures full cookies including `httpOnly` via the `webRequest` API.

#### Install

1. Open Chrome → `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `helpers/` folder from this project
5. The extension icon appears in your toolbar

#### Configure

1. Click the extension icon → ⚙️ **Options**
2. Set your server URL: `http://your-server:5001`
3. Set auth credentials (same as `AUTH_USER`/`AUTH_PASS`)
4. Set output directory (default: `/opt/takeout`)
5. Optionally enable **Auto-send** (captures are sent automatically)

#### Use

1. Navigate to [takeout.google.com](https://takeout.google.com)
2. Click any download link
3. The extension **automatically captures** the URL + full Cookie header
4. If auto-send is on → the download starts on your server immediately
5. If auto-send is off → click the extension icon → **Send Last Capture**

The extension popup shows:
- Time since last capture
- The captured filename
- A "Send" button and an "Update Cookie" button (for refreshing expired cookies)

#### Update Cookie Mid-Download

If a download fails with an auth error (cookie expired while downloading), you can:
1. Visit takeout.google.com again (your browser will have a fresh cookie)
2. Click any download link → extension captures the new cookie
3. Click **Update Cookie** in the popup → sends to `/api/update-cookie`
4. The server restarts failed downloads with the fresh cookie

---

### Option B: Tampermonkey Userscript

Good if you already use Tampermonkey. Captures cookies from XHR/fetch requests but **cannot read httpOnly cookies**.

#### Install

1. Install [Tampermonkey](https://www.tampermonkey.com/) browser extension
2. Open `helpers/takeout-extractor.user.js` (or the raw file on GitHub)
3. Tampermonkey detects it and offers to install → click **Install**

#### Configure

The userscript adds a floating panel on takeout.google.com:
- **Server URL**: Enter `http://your-server:5001`
- **Auto-send**: Toggle to automatically forward captures
- **Send Last cURL**: Manually send the last captured request

#### Use

1. Navigate to takeout.google.com
2. The floating panel appears
3. Click download links → the userscript intercepts the request
4. Send or auto-send to your server

> **Limitation:** If Google uses `httpOnly` cookies (likely), the userscript won't capture them from `document.cookie`. It can intercept `Set-Cookie` headers from XHR/fetch responses, but may miss cookies set by the browser's cookie jar. **Use the Chrome extension for full cookie capture.**

---

### Option C: Bookmarklet

Simplest install, but **cannot capture httpOnly cookies** — only `document.cookie`.

#### Install

1. Open `helpers/bookmarklet.html` in a browser
2. Drag the **"Send to Takeout Downloader"** link to your bookmarks bar

#### Use

1. Navigate to takeout.google.com
2. Click the bookmarklet
3. A prompt asks for your server URL (default: `http://localhost:5000`)
4. The bookmarklet:
   - Reads `document.cookie`
   - Finds download links on the page (`<a>` tags containing "takeout" or "storage.cloud.google.com")
   - If found, uses the first matching link; otherwise prompts for a URL
   - Prompts for auth credentials (optional)
   - POSTs everything to `/api/start`
5. An alert shows success or error

> **Limitation:** `document.cookie` excludes `httpOnly` cookies. If Google's takeout download requires an httpOnly cookie (likely), this method won't work alone. You'd need to manually paste a cURL (see "Manual Method" above) or use the Chrome extension.

---

## 5. Web UI Usage

After logging in at `http://your-server:5001`, you'll see:

### Main Form

| Field | Description | Default |
|-------|-------------|---------|
| **cURL Input** | Paste a `curl` command copied from DevTools | — |
| **Output Directory** | Where files are saved on the server | `/opt/takeout` |
| **Parallel Downloads** | Number of concurrent downloads | 6 |
| **File Count** | How many files to attempt (001–N) | 100 |
| **Start** button | Begins the download | — |
| **Stop** button | Stops current downloads | — |
| **Update Cookie** | Send a fresh cookie mid-download | — |

### Live Progress

- **Log panel:** Real-time log messages (download started, completed, errors, retries)
- **Stats bar:** Files completed/failed/skipped, bytes downloaded
- **Progress per file:** Percentage and speed shown per active download

### The cURL Input

The cURL input accepts commands copied from Chrome DevTools:

```bash
curl 'https://storage.cloud.google.com/takeout-20260613T071725Z-001.zip?...' \
  -H 'Cookie: SID=xxxxxx; HSID=yyyyyy; ...' \
  -H 'Accept: text/html' \
  ...
```

The server parses out:
- The **base URL** (to derive sequential file URLs: 001, 002, 003...)
- The **Cookie header** (used for all download requests)
- The **file extension** (.zip, .tgz, .tbz2)

### Cookie Expiration

Google Takeout cookies expire (typically after a few hours). If your download is large and the cookie expires mid-download:

1. Get a fresh cookie (visit takeout.google.com, click a download link)
2. Use **Update Cookie** in the Web UI or browser helper
3. Failed files will automatically retry with the new cookie

---

## 6. TUI (Terminal) Usage

For running directly on a server without Docker (or inside a container interactively):

```bash
# Install dependencies
pip install -r requirements.txt

# Run the TUI
python google_takeout_tui.py

# Or via the main entry point
python takeout.py --tui
```

The TUI provides:
- cURL input field
- Output directory selection
- Parallel download count
- File count
- Live progress and stats
- aria2c availability indicator

### Inside Docker

```bash
docker exec -it takeout-downloader python google_takeout_tui.py
```

Note: The TUI requires a terminal with Unicode support. Inside Docker, you may need `docker exec -it` with a proper TTY.

---

## 7. aria2c Integration

aria2c provides multi-segment, high-performance downloads as an alternative to the pure-Python downloader.

### Enable aria2c in Docker

The container already has aria2c installed. To enable it:

**Option 1: Auto-start (simplest)**

In `.env`:
```env
ARIA2C_ENABLED=true
```

The server will auto-start an aria2c daemon on port 6800 inside the container.

**Option 2: Standalone aria2c container (recommended for production)**

Uncomment the `aria2c` service block in `docker-compose.yml`:

```yaml
  aria2c:
    image: aria2/aria2:latest
    container_name: aria2c
    restart: unless-stopped
    ports:
      - "6800:6800"
    volumes:
      - /opt/takeout:/opt/takeout
    command: >
      aria2c
      --enable-rpc
      --rpc-listen-all
      --rpc-listen-port=6800
      --rpc-secret=takeout
      --dir=/opt/takeout
      --continue=true
      --max-concurrent-downloads=10
      --split=16
      --max-connection-per-server=16
      --file-allocation=none
      --log-level=notice
```

Then set in `.env`:
```env
ARIA2C_ENABLED=true
ARIA2C_RPC_URL=http://aria2c:6800/jsonrpc
ARIA2C_RPC_SECRET=takeout
```

### aria2c Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ARIA2C_ENABLED` | `false` | Enable aria2c backend |
| `ARIA2C_RPC_URL` | `http://localhost:6800/jsonrpc` | JSON-RPC endpoint |
| `ARIA2C_RPC_SECRET` | _(empty)_ | RPC secret token |
| `ARIA2C_SPLIT` | `16` | Connections per server |

### Using aria2c Programmatically

```python
from aria2c_integration import Aria2cManager, takeout_download_via_aria2c

# Quick one-shot download
stats = takeout_download_via_aria2c(
    cookie="SID=xxx; HSID=yyy",
    base_url="https://storage.cloud.google.com/takeout-20260613T071725Z",
    query_string="?param=value",
    extension=".zip",
    file_count=50,
    output_dir="/opt/takeout",
    parallel=10,
)
print(stats)  # {'total': 50, 'complete': 48, 'error': 2, 'removed': 0, 'results': [...]}

# Manual control
manager = Aria2cManager("http://localhost:6800/jsonrpc", secret="takeout")
gid = manager.add_download(url, filename="file.zip", cookie=cookie_str)
status = manager.get_status(gid)
manager.wait_for_completion([gid], callback=lambda s: print(s))
```

---

## 8. Configuration Reference

All settings are configured via `.env` file or Docker environment variables.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| **Auth** | | | |
| `AUTH_USER` | _(empty)_ | **Yes** | Username for HTTP Basic Auth |
| `AUTH_PASS` | _(empty)_ | **Yes** | Password for HTTP Basic Auth |
| **Flask** | | | |
| `SECRET_KEY` | _(auto-generated)_ | No | Session security key. Set for persistent sessions across restarts. |
| **CORS** | | | |
| `CORS_ORIGINS` | `http://localhost:5000` | No | Comma-separated allowed origins. Set to your domain for remote access. |
| **Downloads** | | | |
| `OUTPUT_DIR` | `/opt/takeout` | No | Base directory for saved files |
| `PARALLEL_DOWNLOADS` | `6` | No | Concurrent download threads |
| `FILE_COUNT` | `100` | No | Max files to download per batch (capped at 1000) |
| **aria2c** | | | |
| `ARIA2C_ENABLED` | `false` | No | Use aria2c instead of Python downloader |
| `ARIA2C_RPC_URL` | `http://localhost:6800/jsonrpc` | No | aria2c JSON-RPC endpoint |
| `ARIA2C_RPC_SECRET` | _(empty)_ | No | RPC auth token (matches `--rpc-secret`) |
| `ARIA2C_SPLIT` | `16` | No | Connections per file in aria2c |
| **Advanced** | | | |
| `MAX_RETRIES` | `3` | No | Retry attempts per failed file |
| `RETRY_BACKOFF` | `2.0` | No | Exponential backoff base (2.0 = 1s, 2s, 4s) |

### Allowed Output Directories

The server validates the output directory against an allowlist:

| Path | Use Case |
|------|----------|
| `/opt/` | **Default.** Best for Docker with volume mount. |
| `/downloads/` | Secondary mount point in Docker. |
| `/tmp/` | Temporary/testing downloads. |
| Current working directory | When running standalone. |
| User home directory | When running standalone. |

Any subdirectory of these is also allowed (e.g., `/opt/takeout/project-1`).

**Rejected paths:** `/etc/`, `/var/`, `/usr/`, `/root/.ssh/`, or any directory outside the allowlist.

---

## 9. Security

### What's Protected

| Attack Vector | Mitigation |
|---------------|------------|
| **Unauthenticated access** | HTTP Basic Auth on all endpoints + WebSocket |
| **Cookie leakage** | `sanitize_state()` strips cookie from all API/SocketIO responses |
| **Path traversal** | `validate_output_dir()` restricts to allowlisted directories |
| **XSS** | No `innerHTML` assignments — all DOM updates via `createElement`/`textContent` |
| **Clickjacking** | `X-Frame-Options: DENY` header |
| **MIME sniffing** | `X-Content-Type-Options: nosniff` header |
| **CSP violations** | `Content-Security-Policy` restricts scripts/styles/connect sources |
| **Rate limiting** | `/api/start` limited to 1 request per 5 seconds |
| **CORS** | Restricted to `CORS_ORIGINS` (not wildcard `*`) |
| **Timing attacks** | `secrets.compare_digest()` for password comparison |
| **Stack overflow** | Iterative retry loop (not recursion) with `MAX_RETRIES=3` |
| **File count abuse** | Clamped to `MAX_FILE_COUNT=1000` |
| **ZIP corruption** | EOCD signature check (`PK\x05\x06`) on last 1024 bytes |

### What's NOT Protected

- **No HTTPS by default.** Use a reverse proxy (nginx/Caddy) with TLS for production.
- **No rate limiting on all endpoints.** Only `/api/start` is rate-limited.
- **No CSRF tokens.** Basic Auth provides implicit CSRF protection (browser auto-sends credentials).
- **Dev server in Docker.** The container uses Werkzeug (not production-grade). Add `gunicorn` + `gevent` for production workloads.

### Production Hardening

```nginx
# nginx reverse proxy example
server {
    listen 443 ssl;
    server_name takeout.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/takeout.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/takeout.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## 10. Troubleshooting

### Container won't start

```bash
# Check logs
docker logs takeout-downloader

# Common issue: port already in use
# Change port mapping: -p 5002:5000

# Common issue: .env mount missing
# Make sure .env exists: cp .env.example .env
```

### "Authentication required" keeps appearing

- Make sure `AUTH_USER` and `AUTH_PASS` are set in `.env` or Docker env
- Clear browser cache and re-enter credentials
- Check the browser console for 401 responses

### Cookie expired / downloads failing with auth errors

1. Visit [takeout.google.com](https://takeout.google.com) in your browser
2. Click a download link (doesn't matter which file)
3. Use **Update Cookie** in the web UI or browser extension
4. Failed downloads will automatically retry with the new cookie

### Downloads not saving to /opt/takeout

- Check the volume mount: `docker inspect takeout-downloader | grep -A5 Mounts`
- Check directory permissions: `docker exec takeout-downloader ls -la /opt/takeout`
- Make sure the host directory exists: `mkdir -p /opt/takeout`

### aria2c not detected

```bash
# Check if aria2c is installed in the container
docker exec takeout-downloader which aria2c

# Start aria2c manually
docker exec takeout-downloader aria2c --enable-rpc --rpc-listen-all --dir=/opt/takeout &

# Set ARIA2C_ENABLED=true in .env and restart
```

### "Rate limited" error

The `/api/start` endpoint allows 1 request per 5 seconds. Wait and retry, or adjust the rate limit in `google_takeout_web.py`.

### Bookmarklet can't capture cookies

This is expected — `document.cookie` cannot read `httpOnly` cookies. Use:
1. **Chrome extension** (captures full headers via `webRequest` API), or
2. **Manual cURL copy** from DevTools → Network tab → Copy as cURL

### Connection refused from browser helper

- Make sure the server URL is correct (including port)
- If using Docker, use the **host-mapped port** (default 5001), not 5000
- If the server is remote, check firewall rules allow the port
- Set `CORS_ORIGINS` to match your accessing URL
