# 📦 Google Takeout Bulk Downloader

Download Google Takeout archives on a remote server with resume, parallel downloads, and aria2c support.

> **License:** [GLWTS (Good Luck With That Shit) Public License](./LICENSE).
> **Forked from:** [`Kalainilavann/takeout_downloader_script`](https://github.com/Kalainilavann/takeout_downloader_script) — see [`NOTICE.md`](./NOTICE.md) for attribution, license change history, and what was removed from the upstream repo before publication.

## ⚠️ What changed when this fork went public

Before being made public, this repository's history was rewritten
(`git-filter-repo`) to remove the following content inherited from the
upstream project:

| Removed item                         | Why                                                                                                |
|--------------------------------------|----------------------------------------------------------------------------------------------------|
| `outscorn/*.zip` (3 files, ~3.3 MB)  | Contained an obfuscated Lua payload + a suspicious ~651 KB `gcc.exe` (real GCC is ~50 MB+). Pattern matched a malware dropper. See [`NOTICE.md`](./NOTICE.md#outscorn--removed). |
| `_tmp/*.json`, `_final_test.txt`     | Local test artifacts that were accidentally committed upstream.                                    |
| `/smb/takeout` mount path            | Author's personal NAS layout, leaked via `docker-compose.yml`. Normalized to `/path/to/downloads`. |

A full secrets/credentials scan of every commit (API keys, GitHub PATs,
GCP/AWS creds, private keys, real Google cookies, personal emails,
non-localhost IPs) was performed — **nothing real was found**. Only
generic env-var-driven config knobs (`AUTH_USER`, `AUTH_PASS`,
`SECRET_KEY`) and the obvious `changeme` default remain.

The original upstream authors remain in commit metadata — that's
intentional, since this is a fork that explicitly acknowledges its
provenance. See [`NOTICE.md`](./NOTICE.md) for the full audit.

## 🆕 What this fork adds over the upstream project

The upstream
[`Kalainilavann/takeout_downloader_script`](https://github.com/Kalainilavann/takeout_downloader_script)
shipped as a **single Python script** (`google_takeout_downloader.py`)
that ran from the terminal and relied on interactive cookie prompts.
This fork turned that into a multi-interface project with proper
security hardening, containerized deployment, and browser integration.
Here is what is genuinely new compared to the original:

### New interfaces

| Component | File | What it does |
|-----------|------|--------------|
| **TUI** | `google_takeout_tui.py` | Textual/Rich terminal UI with a live parallel-downloads table, log widget, and progress bars. |
| **Web UI** | `google_takeout_web.py` | Flask + Flask-SocketIO browser UI for running this on a headless server / NAS. Real-time progress pushed over WebSocket. |
| **Unified core** | `takeout.py` | The shared download / retry / integrity engine used by both TUI and Web. Replaces the old single-purpose script. |
| **aria2c backend** | `aria2c_integration.py` | Optional `aria2` RPC integration with auto-start daemon, random RPC-secret generation, and 16-connections-per-file multi-source downloads. |

### Browser helpers (capture the cURL / cookie from your browser)

| Component | File |
|-----------|------|
| **Bookmarklet** | `helpers/bookmarklet.html` — drag-to-bookmarks-bar prompt that posts the takeout URL + cookie to your server. |
| **Tampermonkey userscript** | `helpers/takeout-extractor.user.js` — auto-intercepts network requests on `takeout.google.com` and can auto-send. |
| **Chrome extension (MV3)** | `helpers/manifest.json`, `background.js` (service worker), `popup.html` / `popup.js`, `options.html` / `options.js` — captures cookies including `httpOnly` ones that the bookmarklet can't reach. |

### Security hardening (not in the upstream)

- HTTP Basic Auth on every web endpoint (optional but enforced for any
  internet-facing deployment).
- Flask session secret key handling (env-driven; auto-generated if unset).
- CORS allowlist via `CORS_ORIGINS` env var — **no wildcards** in
  production.
- `Content-Security-Policy` header set on every response.
- Cookie value stripped from `/api/status` JSON responses so it never
  leaks into the browser.
- Path-traversal protection on `output_dir` (restricted to cwd, home,
  `/opt/`, `/downloads/`, `/tmp/`).
- Rate-limit on `/api/start` (1 request / 5 s).
- XSS-safe DOM updates in the Web UI (`textContent` only, no
  `innerHTML`).
- Non-localhost `serverUrl` in the Chrome extension triggers a
  one-shot confirmation prompt so it can't silently exfiltrate cookies.

### Download-engine improvements

- **HTTP Range resume** with `.downloading` temp files — interruption
  no longer loses partial work; resumes from the byte that was last
  written.
- **Exponential backoff retries** (`MAX_RETRIES`, `RETRY_BACKOFF`).
- **ZIP end-of-central-directory verification** before promoting a
  `.downloading` file to a final `.zip` (catches truncated downloads).
- **Cookie refresh mid-session** without restarting — paste a new
  cURL via the Web UI, TUI, or `/api/update-cookie`, and only the
  failed files retry.
- **Auth detection** checks status code, `Content-Type`, response URL
  redirect target, and ZIP magic bytes — catches expired-cookie
  responses that still return 200.
- **PowerShell cURL support** — the cURL parser also accepts the
  escaped format that Windows PowerShell produces.

### Deployment & build

- `Dockerfile` (Python 3.11-slim with `aria2` + `curl` for healthcheck).
- `docker-compose.yml` (mounts `.env`, downloads dir, and output dir).
- `build.py` + GitHub Actions workflow produces single-binary TUI and
  Web builds via PyInstaller.
- `dedupe_takeout.py` — post-download hash-based deduplication helper.

### What was removed

See [`NOTICE.md`](./NOTICE.md#safety-cleanup) for the full audit. In
short:

- `outscorn/*.zip` (3 prebuilt zips containing obfuscated Lua + a
  suspicious `gcc.exe` — likely a malware dropper inherited from the
  upstream). **Purged from history.**
- `_tmp/*.json`, `_final_test.txt` (test artifacts accidentally
  committed upstream). **Purged from history.**
- `/smb/takeout` (upstream author's personal NAS mount path).
  Normalized to `/path/to/downloads` throughout history.

## 🔒 `.gitignore` keeps your secrets out

The shipped `.gitignore` blocks the following from ever being committed
even if you `git add .`:

- `.env`, `.env.*` (except `.env.example`), `*.pem`, `*.key`, `*.p12`,
  `cookies.*`, `curl_*.txt`, `auth_*.txt`
- `downloads/`, `*.zip`, `*.downloading`, `*.partial`
- `_tmp/`, `_final_test*`, local test/scratch files
- `outscorn/` (explicitly blocked — do not re-add)
- Python: `__pycache__/`, `venv/`, `.pytest_cache/`, `.mypy_cache/`,
  `*.egg-info/`, `.coverage`
- PyInstaller: `build/`, `dist/`, `*.spec`, `*.exe`
- IDE/OS junk (`.idea/`, `.vscode/`, `.DS_Store`, `Thumbs.db`)

**Always start by copying `.env.example` to `.env`** and filling in your
real values — `.env` is ignored.

## ✨ Features

- **Parallel Downloads** — Download multiple takeout parts simultaneously (1-20)
- **Resume Support** — `.downloading` temp files survive interruptions; resume via HTTP Range headers
- **Auto-Retry** — Exponential backoff on transient errors (3 retries by default)
- **ZIP Integrity Verification** — Validates end-of-central-directory record before finalizing
- **aria2c Backend** — Optional multi-connection-per-file downloads (16 connections × 10 files)
- **Authentication** — HTTP Basic Auth on web UI (`.env` configured)
- **Cookie Update** — Mid-download cookie refresh without restarting
- **Path Validation** — Output directory restricted to allowed paths (cwd, home, /opt/, /downloads/, /tmp/)
- **XSS Protection** — CSP headers, no innerHTML, all dynamic content via textContent
- **CORS Control** — Configurable allowed origins (no wildcards in production)
- **3 Interfaces** — TUI (terminal), Web UI, and headless CLI
- **Helper Tools** — Browser bookmarklet, Tampermonkey userscript, Chrome extension

## 🚀 Quick Start

### Docker (Recommended for Remote Servers)

```bash
# 1. Copy and edit config
cp .env.example .env
# Edit .env — set AUTH_USER, AUTH_PASS, OUTPUT_DIR, CORS_ORIGINS

# 2. Start
docker-compose up -d

# 3. Open http://your-server:5001
```

### Local Python

```bash
pip install -r requirements.txt

# TUI mode (terminal)
python takeout.py

# Web mode
python takeout.py --web --port 5000

# Or directly
python google_takeout_web.py
```

## ⚙️ Configuration

All settings via `.env` file or environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_USER` | *(unset)* | HTTP Basic Auth username (required for remote) |
| `AUTH_PASS` | *(unset)* | HTTP Basic Auth password |
| `SECRET_KEY` | *(auto-generated)* | Flask session signing key |
| `CORS_ORIGINS` | `http://localhost:5000` | Comma-separated SocketIO allowed origins |
| `OUTPUT_DIR` | `/downloads` | Download destination |
| `PARALLEL_DOWNLOADS` | `6` | Concurrent file downloads |
| `FILE_COUNT` | `100` | Max takeout parts to attempt |
| `ARIA2C_ENABLED` | `false` | Use aria2c as download backend |
| `ARIA2C_RPC_URL` | `http://localhost:6800/jsonrpc` | aria2c RPC endpoint |
| `ARIA2C_RPC_SECRET` | *(auto-generated)* | aria2c RPC secret token |
| `MAX_RETRIES` | `3` | Retries per file on transient errors |

### Allowed Output Directories

For security, `output_dir` must be within one of:
- Current working directory
- User home directory
- `/opt/`
- `/downloads/`
- `/tmp/`

## 🔐 Security

- **Auth Required**: Set `AUTH_USER` + `AUTH_PASS` for any internet-facing deployment
- **No Cookie Leakage**: API responses strip the raw Google cookie
- **CORS**: Origin-restricted (no `*` in production)
- **CSP Headers**: `Content-Security-Policy` set on all responses
- **Path Traversal**: Output directory validated against allowlist
- **Rate Limiting**: `/api/start` limited to 1 request per 5 seconds
- **Integrity**: ZIP end-of-central-directory verified before finalizing

## 📡 API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/` | GET | Yes | Web UI |
| `/api/start` | POST | Yes | Start download session |
| `/api/status` | GET | Yes | Current download state (cookie stripped) |
| `/api/update-cookie` | POST | Yes | Update cookie mid-download |
| `/api/auth-check` | GET | Yes | Verify auth credentials |
| `/api/aria2c-check` | GET | Yes | Check aria2c availability |

## 🔧 Helper Tools

### Bookmarklet
Drag the link from `helpers/bookmarklet.html` to your bookmarks bar. Click it on a takeout page to capture the download URL and send it to your server.

### Tampermonkey Userscript
Install `helpers/takeout-extractor.user.js` — it auto-intercepts network requests on takeout.google.com and can auto-send to your server.

### Chrome Extension
Load `helpers/` as an unpacked extension in `chrome://extensions`. It uses `webRequest` to capture cookies (including httpOnly) that the bookmarklet can't access.

## 🏎️ aria2c Integration

For maximum download speed, enable aria2c:

```bash
# Install
apt install aria2   # Debian/Ubuntu
brew install aria2  # macOS

# Docker — already included in the Dockerfile
```

Set `ARIA2C_ENABLED=true` in `.env`. The system will:
1. Auto-detect aria2c in PATH
2. Start an aria2c daemon with RPC enabled (if not already running)
3. Enqueue all takeout URLs with 16 connections per file
4. Poll status and report progress back to the web UI

## 📂 Project Structure

```
├── takeout.py               # Core engine: parsing, download, resume, integrity
├── google_takeout_web.py     # Flask + SocketIO web UI
├── google_takeout_tui.py     # Textual terminal UI
├── aria2c_integration.py    # aria2c RPC backend
├── dedupe_takeout.py         # Deduplicate takeout files by hash
├── build.py                  # PyInstaller build script
├── Dockerfile                # Docker image (includes aria2c)
├── docker-compose.yml        # Docker Compose config
├── requirements.txt          # Python dependencies
├── .env.example              # Configuration template
└── helpers/
    ├── takeout-extractor.user.js  # Tampermonkey userscript
    ├── bookmarklet.html           # Bookmarklet with instructions
    ├── manifest.json              # Chrome extension manifest
    ├── background.js              # Extension background worker
    ├── popup.html/js              # Extension popup UI
    └── options.html/js            # Extension settings
```

## 📋 How It Works

1. **Extract cURL**: You paste a cURL command from Chrome DevTools (or use the helper tools to capture it automatically)
2. **Parse URL Pattern**: The script detects the `takeout-TIMESTAMP-BATCH-NNN.zip` pattern and generates URLs for parts 001-NNN
3. **Parallel Download**: Downloads multiple parts simultaneously with resume support
4. **Auth Detection**: Checks HTTP status codes, content type, response URL, and ZIP magic bytes to detect cookie expiration
5. **Cookie Refresh**: On auth failure, prompts for a new cURL (or accepts via API) and continues where it left off
6. **Integrity**: Verifies ZIP end-of-central-directory before finalizing each file

## 📜 License & Attribution

This project is licensed under the **[GLWTS (Good Luck With That Shit)
Public License](./LICENSE)**.

It is a fork of
[`Kalainilavann/takeout_downloader_script`](https://github.com/Kalainilavann/takeout_downloader_script),
originally created by **Clive Watts**. The upstream project shipped
without a `LICENSE` file; this fork adopts GLWTS as its license. For
full attribution, the change log from "no license / upstream" →
GLWTS, the safety audit details, and instructions for fetching the
previous version of the code, see **[`NOTICE.md`](./NOTICE.md)**.
