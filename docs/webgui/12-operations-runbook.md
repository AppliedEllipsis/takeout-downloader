# 12 — Operations Runbook & Recovery

Day-to-day commands and recovery procedures for the web-hosted Takeout
downloader. Covers start, stop, crash recovery, cookie refresh, and verifying
downloads. Assumes the repo is cloned at
`/opt/storage.local_1/projects/takeout-downloader` and the server is reachable
via the `takeout-server` SSH alias.

---

## Start the stack

```bash
ssh takeout-server
cd /opt/storage.local_1/projects/takeout-downloader
docker compose -f docker-compose.webgui.yml up -d
```

The webtop (KasmVNC + Chromium) takes ~30 seconds to finish its first-boot
init. Wait until both containers are healthy:

```bash
docker compose -f docker-compose.webgui.yml ps
# Expected: takeout-webgui + takeout-tunnel both Up
```

Grab the quick-tunnel URL (random `*.trycloudflare.com` hostname):

```bash
docker logs takeout-tunnel 2>&1 | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | tail -1
```

Open it in a browser. The login gate is HTTP basic-auth:

- **User:** `takeout`
- **Password:** `passw0rd` (from `webgui/.env` — change this)

To skip the prompt, embed the creds in the URL (browsers sometimes strip this):
```
https://takeout:passw0rd@<random>.trycloudflare.com/
```

**WARNING:** The quick-tunnel URL changes on every `docker compose restart webgui`
— the tunnel shares webgui's network namespace, so restarting webgui orphans it
(Cloudflare error 1033). To reload manager code without killing the tunnel, use
the s6 SIGKILL-respawn method (see below).

---

## Stop the stack

```bash
cd /opt/storage.local_1/projects/takeout-downloader
docker compose -f docker-compose.webgui.yml down
```

This removes the containers and the tunnel network, but leaves all downloaded
files on disk. The browser profile (`config/`) and the source code are
bind-mounts and survive `down` unchanged.

---

## Reload the manager without changing the tunnel URL

**Never** do `docker compose restart webgui` — it tears down the shared network
namespace, killing the tunnel. Instead, reload ONLY the Python process:

```bash
# SIGKILL the uvicorn process; s6 respawns it with updated code
docker exec takeout-webgui sh -c "kill -9 \$(pgrep -f uvicorn)"
sleep 6
# Verify it's back
docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health
```

This works because the manager service (`custom-svc-takeout-manager`) is
supervised by s6. After a SIGKILL, s6 detects the death and restarts it from the
bind-mounted source — so any host-side code edit takes effect. The tunnel
container is untouched.

`s6-svc -t` and `s6-svc -r` did NOT successfully cycle a wedged process;
SIGKILL was the only reliable signal.

---

## Reload the extension from the command line

After editing `helpers/*.js` or `helpers/manifest.json`, run:

```bash
cd /opt/storage.local_1/projects/takeout-downloader
./webgui/reload-extension.sh
```

This drives the hosted Chromium over CDP (`127.0.0.1:9222`, loopback-only) using
`chrome.developerPrivate.reload(id, {failQuietly:false})` and reports real
manifest/install errors. It also reloads the `takeout.google.com` tab
(`Page.reload ignoreCache`) so the new content script injects.

If the `chrome://extensions` tab is not open (e.g., after a Chrome restart), the
script fails. To recover:

1. From inside the hosted Chromium, open `chrome://extensions`.
2. Or, navigate an existing CDP-attached page to `chrome://extensions`.
3. Or, restart the container stack (which seeds extensions in the profile).

**Note:** CDP on 9222 is full control of the logged-in Chrome session. It is
bound to `127.0.0.1` inside the container and is **never tunneled** — the
Cloudflare quick tunnel only proxies `:3000` (the KasmVNC webtop). Never expose
`:9222` publicly.

---

## Known Chrome stability issue (as of 2026-06-24)

Chrome on this container (VirtIO GPU, Ubuntu 24.04, Chromium 149) crashes or
hangs on heavy pages like `takeout.google.com`. The crash signature is `Trace/
breakpoint trap (core dumped)` or the page sits at a loading spinner. This
affects ALL pages after prolonged uptime, not just takeout — it is a
Chrome/container issue, not an extension bug.

**Workarounds applied** (see `webgui/init_custom.sh`):
- `--disable-gpu` — uses software rendering (avoids broken virtio GPU driver,
  which fails VA-API init: `virtio_gpu_drv_video.so init failed`)
- `--disable-software-rasterizer` — avoids software rasterizer path

**If Chrome is still unstable after a restart**, additional mitigations to try:

1. **Restart the container stack.** A `docker compose down && docker compose up`
   gives Chrome a fresh start without profile fragmentation.
2. **Clear the Chrome profile.** Move `/config/.chrome-profile` aside and let
   the init script seed a fresh one:
   ```bash
   docker exec takeout-webgui mv /config/.chrome-profile /config/.chrome-profile.old
   docker compose restart webgui   # regenerates profile
   ```
   You'll need to log into Google again.
3. **Reduce Chrome memory pressure.** The container has 7.5 GB; Chrome + KasmVNC
   + manager + a 62-part Takeout page can push this. Add `--process-per-site`
   or `--renderer-process-limit=2` to the launcher:
   ```bash
   # Edit webgui/init_custom.sh, add after --disable-gpu:
   echo "  --renderer-process-limit=2 \\\\" >> ...
   ```
4. **Disable the extension temporarily.** Remove `--load-extension=/work/helpers`
   from the launcher to isolate whether extension content scripts contribute to
   the hang. This is diagnostic only (capturing won't work).

---

## Resume a job after cookie expiry

Takeout download cookies are IP-bound and short-lived (often < 30 min). After
the cookie expires:

1. The job flips to `needs_cookie` in the manager.
2. The 58 done parts are safe on disk; the in-progress parts have HTTP Range
   partials that will resume.
3. In the hosted Chromium, go to `takeout.google.com` → Manage exports → click
   **Download** on any one part.
4. The extension auto-captures and POSTs a fresh payload.
5. The manager matches the cookie to the existing job (by output directory /
   export timestamp) and resumes — skipping completed parts, resuming partials.
6. **Must submit within a minute or two** — the cookie expires fast.

---

## Monitor download progress

From your laptop (requires the `takeout-server` SSH alias and Python):

```bash
python monitor_job.py
# Windows PowerShell:
C:\Users\User\anaconda3\python.exe monitor_job.py
```

Polls `GET /api/jobs` every 5s, redraws a single in-place line (parts done,
bytes, %, speed, last error). Exits when the job hits `complete` or `error`.

Watching from the server directly:

```bash
ssh takeout-server 'watch -n5 "docker exec takeout-webgui curl -s 127.0.0.1:8080/api/jobs"'
```

---

## Verify downloaded files

List zip files and check magic bytes (real ZIP = `PK\x03\x04`):

```bash
ssh takeout-server 'JOBDIR=/opt/storage.jfs002/google-takeout/*/*/
docker exec takeout-webgui python3 -c "
import os, glob
for f in sorted(glob.glob(\"$JOBDIR/*.zip\")):
    with open(f,\"rb\") as fh:
        magic = fh.read(2)
    sz = os.path.getsize(f)
    print(f\"{f.split(chr(47))[-1]:40s} magic={magic.hex()} size={sz/1e9:.1f}GB{\" DONE\" if magic==b\"PK\" else \" BAD\"}\")
"
```

Quick EOCD check (Central Directory signature = complete zip, not truncated):

```bash
ssh takeout-server 'JOBDIR=/opt/storage.jfs002/google-takeout/gaia-1005482974000/2026-06-23-03-59-47
docker exec takeout-webgui python3 -c "
import os, struct
f=\"$JOBDIR/takeout-...-part-000.zip\"  # pick any completed part
sz = os.path.getsize(f)
with open(f,\"rb\") as fh:
    fh.seek(-65536, 2) if sz > 65536 else fh.seek(0)
    tail = fh.read()
print(\"EOCD present:\", b\"PK\\x05\\x06\" in tail)
"
```

Full `zip -T` test (reads entire file — slow for 50GB parts):

```bash
ssh takeout-server 'docker exec takeout-webgui unzip -t /path/to/part-000.zip'
```

---

## Collected fixes and where they live

| Fix | File(s) | Commit |
|-----|---------|--------|
| Manager UI paste box + token injection | `manager/web/index.html`, `app.js`, `manager/app.py` | `a19b590` |
| Account label from URLs + DOM scrape | `manager/orchestrator.py`, `derive.py`, `helpers/content.js`, `background.js` | `a19b590` |
| Job deletion + control-token fix | `manager/orchestrator.py`, `app.py`, `web/app.js` | `a19b590` |
| Cache-busting (no-stock + versioned assets) | `manager/app.py` | `a19b590` |
| Extension auto-reload over CDP | `cdp_reload_ext.py`, `webgui/reload-extension.sh` | `a19b590` |
| Manifest `_comment_key` removal | `helpers/manifest.json` | `a19b590` |
| `config/` gitignore | `.gitignore` | `a19b590` |
| Live job monitor (in-place redraw) | `monitor_job.py` | `261ecb3` |
| Extension CSP crash + MAIN-world spy | `helpers/page-spy.js`, `manifest.json`, `content.js` | `6a2a14e` |
| Manager options (token/URL fields) | `helpers/options.html`, `options.js` | `6a2a14e` |
| Manifest load crash fix (list→dict) | `manager/manifest.py` | `6a2a14e` |
| Chrome `--disable-gpu` launcher | `webgui/init_custom.sh` | `6a2a14e` |

---

## Service architecture (quick reference)

| Port | Service | Binding | Tunneled? |
|------|---------|---------|-----------|
| 3000 | KasmVNC webtop (nginx + Selkies) | `0.0.0.0` (container) → `127.0.0.1:3000` (host) | **Yes** — cloudflared exposes this |
| 8080 | Takeout Manager (FastAPI / uvicorn) | `127.0.0.1` (container) | No — localhost only |
| 9222 | Chrome DevTools Protocol | `127.0.0.1` (container) | No — localhost only |

```
  Internet
     │
  cloudflared tunnel (quick, *.trycloudflare.com)
     │
  nginx basic-auth (takeout:passw0rd)
     │
  KasmVNC webtop (:3000)
     ├── Chromium (logged into Google, profile on /config)
     │     ├── https://takeout.google.com
     │     └── http://127.0.0.1:8080/  (manager UI)
     └── s6-supervised services
           ├── custom-svc-takeout-manager  (uvicorn manager.app:app)
           ├── custom-svc-takeout-chromium (supervision loop)
           └── custom-svc-takeout-*       (s6-overlay)
```

**Storage root:** `/opt/storage.jfs002/google-takeout/<account>/<export-ts>/`
**Repo root:** `/opt/storage.local_1/projects/takeout-downloader`
