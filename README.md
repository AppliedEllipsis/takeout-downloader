# Google Takeout Bulk Downloader

A comprehensive Python tool to bulk download Google Takeout archives using browser cookies for authentication. Available as **unified CLI**, **GUI**, **Web interface**, and **Docker** deployment.

![Version](https://img.shields.io/badge/Version-2.0.0-blue) ![CLI](https://img.shields.io/badge/CLI-Available-green) ![GUI](https://img.shields.io/badge/GUI-Available-brightgreen) ![Web](https://img.shields.io/badge/Web-Available-blue) ![Docker](https://img.shields.io/badge/Docker-Ready-2496ED) ![Python](https://img.shields.io/badge/Python-3.8+-blue) ![Platform](https://img.shields.io/badge/Platform-Linux%20|%20Windows%20|%20macOS-orange)

## What's New in v2.0

- **🔗 Unified Script** - Single `takeout.py` with `--web` and `--gui` flags
- **📥 Resume Partial Downloads** - Interrupted downloads resume from where they left off
- **✅ ZIP Verification** - Validates downloaded files are not corrupted
- **⏱️ Auth Expiry Countdown** - Shows time remaining before session expires
- **🚦 Speed Limiting** - Control bandwidth usage with `--speed-limit`
- **📧 Email/Webhook Notifications** - Get notified via email or webhook
- **📂 Auto-Extract & Organize** - Extract ZIPs and organize by Google service type

## Quick Start

### Unified Script (Recommended)

```bash
# CLI mode (default)
python takeout.py --cookie "YOUR_COOKIE" --url "https://..."

# Web interface
python takeout.py --web --port 5000

# Desktop GUI
python takeout.py --gui
```

### Pre-built Binaries

Download the latest release for your platform (no Python required):

| Mode | Linux | Windows | macOS |
|------|-------|---------|-------|
| **CLI** | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) |
| **GUI** | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) |
| **Web** | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) | [Download](https://github.com/clivewatts/takeout_downloader_script/releases/latest) |

## Features

### Core Features
- **📦 Bulk Downloads** - Automatically downloads all numbered Takeout files
- **⚡ Parallel Downloads** - Configurable concurrent downloads (default: 6)
- **🔄 Resume Support** - Resumes partial downloads and skips completed files
- **📋 cURL Paste Support** - Just paste the entire cURL command, cookie is extracted automatically
- **📊 Progress Tracking** - Real-time progress with ETA and download speed

### Reliability
- **✅ ZIP Verification** - Validates downloaded files using CRC checks
- **🔐 Auth Failure Detection** - Detects expired sessions via ZIP magic bytes
- **⏱️ Auth Expiry Warning** - Warns ~15 minutes before session expires
- **🔁 Auto-Retry** - Prompts for new cookie and resumes on auth failure

### Notifications
- **🔔 Desktop Notifications** - Native notifications on all platforms
- **🔊 Sound Alerts** - Audio alerts for auth expiry and completion
- **📧 Email Notifications** - SMTP email alerts for long-running downloads
- **🌐 Webhook Support** - POST notifications to any URL (Slack, Discord, etc.)

### Post-Processing
- **📂 Auto-Extract** - Automatically extract downloaded ZIPs
- **🗂️ Organize by Type** - Sort extracted files by Google service (Photos, Drive, Mail, etc.)
- **🗑️ Auto-Cleanup** - Optionally delete ZIPs after extraction

### Interfaces
- **🖥️ Modern GUI** - User-friendly graphical interface with dark theme
- **🌐 Web Interface** - Browser-based UI for headless/NAS environments
- **⌨️ CLI** - Full-featured command-line interface
- **🐳 Docker Ready** - One-command deployment with docker-compose

## Installation

```bash
# Clone the repository
git clone https://github.com/clivewatts/takeout_downloader_script.git
cd takeout_downloader_script

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install requests
```

## Configuration

Copy the example environment file and configure:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```bash
# Full cookie header from browser
GOOGLE_COOKIE="your_cookie_here"

# First download URL from Google Takeout
TAKEOUT_URL="https://takeout-download.usercontent.google.com/download/takeout-YYYYMMDDTHHMMSSZ-N-001.zip?..."

# Output directory
OUTPUT_DIR="/path/to/downloads"

# Number of files to download
FILE_COUNT=100

# Parallel downloads (optional, default: 6)
PARALLEL_DOWNLOADS=6
```

## Quick Setup - Just Paste cURL!

The easiest way to use this tool: **just paste the cURL command** and everything is auto-extracted!

### One-Step Setup

1. Go to [Google Takeout](https://takeout.google.com) → **Manage exports**
2. Open DevTools (`F12`) → **Network** tab
3. Click the **Download** button for **file 1**
4. Right-click the request → **Copy** → **Copy as cURL**
5. Paste into the app - **both cookie AND URL are extracted automatically!**

That's it! The GUI will show:
- ✓ Cookie extracted from cURL
- ✓ URL auto-filled from cURL

### How It Works

When you create a Google Takeout export, Google splits your data into multiple 2GB ZIP files:

```
takeout-20251204T101148Z-3-001.zip
takeout-20251204T101148Z-3-002.zip
...
takeout-20251204T101148Z-3-730.zip
```

The cURL command contains both:
- **The download URL** - for file 001, which we increment to get 002, 003, etc.
- **The cookie** - for authentication

### Manual Setup (Alternative)

If you prefer to set things up manually in `.env`:

**Get the URL:**
- Right-click the "Download" button for file 1 → **Copy link address**

**Get the Cookie:**
- Copy as cURL → find `-H 'Cookie: ...'` → copy the value

## Getting Your Cookie (Details)

The cookie authenticates your requests. Google sessions typically expire after ~1 hour.

### Chrome / Chromium / Edge

1. Go to the Google Takeout download page
2. Open DevTools (`F12`) → **Network** tab
3. Click any download link
4. Find the request in the Network tab
5. Right-click → **Copy** → **Copy as cURL (bash)**
6. Paste the entire cURL command when prompted, or extract the `Cookie:` header for `.env`

### Firefox

1. Go to the Google Takeout download page
2. Open DevTools (`F12`) → **Network** tab
3. Click any download link
4. Find the request in the Network tab
5. Right-click → **Copy Value** → **Copy as cURL**
6. Paste the entire cURL command when prompted, or extract the `Cookie:` header for `.env`

### Extracting Cookie for `.env`

If you prefer to put the cookie in `.env` instead of pasting cURL each time:

1. Copy as cURL (as above)
2. Find the part that says `-H 'Cookie: ...'`
3. Copy everything between the quotes after `Cookie:`
4. Paste as `GOOGLE_COOKIE` in your `.env`

## Usage

### 🐳 Docker (Recommended for NAS/Headless)

The easiest way to run on a NAS or headless server:

```bash
# Quick start with docker-compose
docker-compose up -d

# Or build and run manually
docker build -t takeout-downloader .
docker run -d -p 5000:5000 -v $(pwd)/downloads:/downloads takeout-downloader
```

Then open your browser to `http://your-server:5000`

**Docker environment variables:**
| Variable | Description | Default |
|----------|-------------|---------|
| `OUTPUT_DIR` | Download directory inside container | `/downloads` |
| `PARALLEL_DOWNLOADS` | Concurrent downloads | `6` |
| `FILE_COUNT` | Max files to download | `100` |

### 🌐 Web Interface (Headless Servers)

Run the web interface directly (without Docker):

```bash
# Using unified script (recommended)
python takeout.py --web --port 5000

# Or standalone
python google_takeout_web.py --host 0.0.0.0 --port 5000
```

Open `http://your-server:5000` in your browser. The web UI provides:
- Paste area for cURL commands
- Real-time progress via WebSocket
- Download statistics and speed
- Activity log

### 🖥️ GUI Application (Desktop)

Launch the graphical interface:

```bash
# Using unified script (recommended)
python takeout.py --gui

# Or standalone
python google_takeout_gui.py
```

The GUI provides:
- Easy paste area for cookies/cURL commands
- Directory browser for output location
- Real-time progress and speed display
- Download log with color-coded status
- Start/Stop controls

### ⌨️ Command-Line Interface

```bash
# Using unified script (recommended)
python takeout.py --cookie "YOUR_COOKIE" --url "https://..."

# With all features
python takeout.py \
  --cookie "your_cookie" \
  --url "https://takeout-download..." \
  --output "/path/to/downloads" \
  --count 100 \
  --parallel 6 \
  --speed-limit 50 \
  --auto-extract \
  --organize \
  --webhook "https://hooks.slack.com/..."

# Or use legacy script
python google_takeout_downloader.py
```

### Command-Line Options

#### Basic Options
| Option | Description | Default |
|--------|-------------|---------|
| `--web` | Start web interface | - |
| `--gui` | Start desktop GUI | - |
| `--cookie` | Full cookie header string | From `.env` |
| `--url` | First download URL | From `.env` |
| `--output`, `-o` | Output directory | `./downloads` |
| `--count`, `-n` | Max files to download | `100` |
| `--parallel`, `-p` | Concurrent downloads | `6` |

#### Advanced Options
| Option | Description | Default |
|--------|-------------|---------|
| `--speed-limit` | Speed limit in MB/s (0 = unlimited) | `0` |
| `--no-resume` | Disable resume support | Enabled |
| `--no-verify` | Disable ZIP verification | Enabled |

#### Auto-Extract Options
| Option | Description | Default |
|--------|-------------|---------|
| `--auto-extract` | Auto-extract downloaded ZIPs | Disabled |
| `--extract-dir` | Directory for extracted files | Same as output |
| `--organize` | Organize by Google service type | Disabled |
| `--delete-after-extract` | Delete ZIP after extraction | Disabled |

#### Notification Options
| Option | Description | Default |
|--------|-------------|---------|
| `--webhook` | Webhook URL for notifications | - |
| `--email` | Email address for notifications | - |
| `--smtp-host` | SMTP server host | - |
| `--smtp-port` | SMTP server port | `587` |
| `--smtp-user` | SMTP username | - |
| `--smtp-password` | SMTP password | - |

#### Web Server Options
| Option | Description | Default |
|--------|-------------|---------|
| `--host` | Web server host | `0.0.0.0` |
| `--port` | Web server port | `5000` |

## Re-authentication

When your session expires mid-download, the script will:

1. Pause downloads
2. Prompt you to paste a new cURL command
3. Automatically extract the cookie
4. Resume downloading from where it left off

```
============================================================
AUTHENTICATION EXPIRED
============================================================

To get a new cookie:
1. Open Chrome DevTools (F12) on Google Takeout
2. Go to Network tab
3. Click a download link
4. Right-click the request -> Copy -> Copy as cURL

Paste the ENTIRE cURL command below (or 'q' to quit):
------------------------------------------------------------
```

## Notifications & Alerts

The script includes desktop notifications and sound alerts (Linux):

- **🔐 Auth Expired** - Critical notification + sound when authentication fails
- **⚠️ Auth Warning** - Warning at ~45 minutes (sessions typically expire after ~1 hour)
- **✅ Complete** - Notification + sound when all downloads finish

### Requirements for Notifications

```bash
# Desktop notifications (usually pre-installed)
sudo zypper install libnotify-tools  # openSUSE
sudo apt install libnotify-bin       # Ubuntu/Debian

# Sound alerts (PulseAudio)
# Usually pre-installed with desktop environments
```

### Example Output

```
[takeout-3-001.zip] Starting (2.00 GB)
[takeout-3-001.zip] 25% (ETA: 2h 15m)
[takeout-3-001.zip] 50% (ETA: 1h 45m)
[takeout-3-001.zip] 75% (ETA: 58m)
[takeout-3-001.zip] Done!

⚠️  Auth session active for 45+ minutes - may expire soon

============================================================
Download complete! 50 succeeded, 0 failed
Files saved to: /path/to/downloads
Total downloaded: 100.00 GB in 1:23:45
============================================================
```

## Tips

- **NFS/SMB Mounts**: If downloading to a network share, mount with your user permissions:
  ```bash
  sudo mount -t cifs //server/share /mnt/share -o guest,uid=1000,gid=1000
  ```

- **Parallel Downloads**: Start with 2-4 parallel downloads. Too many may trigger rate limiting.

- **Large Exports**: Google Takeout splits exports into 2GB files. A full Google account backup can be 100+ files.

- **Run in Background**: Use `screen` or `tmux` to keep downloads running after closing terminal:
  ```bash
  screen -S takeout
  ./venv/bin/python google_takeout_downloader.py
  # Press Ctrl+A, D to detach
  # screen -r takeout to reattach
  ```

## Building from Source

To create standalone executables:

```bash
# Install build dependencies
pip install pyinstaller

# Build GUI app (desktop) - default
python build.py

# Build CLI (terminal)
python build.py --cli

# Build Web server (headless/NAS)
python build.py --web

# Build all three (CLI, GUI, Web)
python build.py --all-apps

# Output will be in dist/ folder
```

### Automated Builds (GitHub Actions)

The repository includes a GitHub Actions workflow that automatically builds executables for all platforms when you create a release tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This will create a GitHub Release with binaries for:

**CLI (Terminal)**
- `Google_Takeout_CLI-linux-x64`
- `Google_Takeout_CLI-windows-x64.exe`
- `Google_Takeout_CLI-macos-x64`

**GUI Application (Desktop)**
- `Google_Takeout_Downloader-linux-x64`
- `Google_Takeout_Downloader-windows-x64.exe`
- `Google_Takeout_Downloader-macos-x64.zip`

**Web Server (Headless/NAS)**
- `Google_Takeout_Web-linux-x64`
- `Google_Takeout_Web-windows-x64.exe`
- `Google_Takeout_Web-macos-x64`

## License

MIT License
