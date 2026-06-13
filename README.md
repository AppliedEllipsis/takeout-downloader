# 📦 Google Takeout Bulk Downloader

Download Google Takeout archives on a remote server with resume, parallel downloads, and aria2c support.

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
