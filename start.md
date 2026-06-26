# start.md — Operator Runbook for a New Google Takeout Download

> **Who this is for.** You are an AI operator (or a human) running ONE new
> Google account's Takeout download end-to-end, supervised by a person. Follow
> the steps **in order**. Do not skip the verification steps. When a step says
> STOP and ask, actually stop. This file is self-contained; you should not need
> to guess anything.

---

## 0. The one-paragraph mental model

A long-running Chromium browser lives on a remote server (you reach it through a
web portal). A human logs that browser into a Google account and creates a
Takeout export. A browser **extension** watches for the user clicking
"Download", captures the auth cookie + URL, and POSTs it to a local **manager**
service. The manager downloads all the parts **server-side** (not through the
browser) into a per-account folder. Your job is to drive that flow, watch
progress, and recover if it stalls. **The cookie is short-lived — keep downloads
moving.**

---

## 1. Where everything is

| Thing | Value |
|-------|-------|
| SSH to server | `ssh takeout-server` (alias already in `~/.ssh/config`; host `188.245.169.166`, user `ellipsis`, key `~/.ssh/takeout_deploy`) |
| Project dir on server | `/opt/local_cache_crypt/_projects/takeout-downloader` |
| Browser portal (KasmVNC) | reached via the cloudflared tunnel URL (see step 2) |
| Manager API (inside server) | `http://127.0.0.1:8080` — bound to localhost only |
| Chrome DevTools (CDP) | `http://127.0.0.1:9222` — localhost only |
| Two containers | `takeout-webgui` (browser+manager), `takeout-tunnel` (cloudflared) |
| Download destination | `/opt/archives/google-takeout/<account-label>/<export-ts>/` |
| Git source of truth | GitHub origin `feat/internal-downloader`; server repo matches it |

**Run manager API calls from INSIDE the container**, because the manager binds
to localhost:

```bash
ssh takeout-server 'docker exec takeout-webgui bash -lc "<command>"'
```

The API token lives in `webgui/.env` as `MANAGER_API_TOKEN`. Read it once:

```bash
ssh takeout-server 'grep MANAGER_API_TOKEN /opt/local_cache_crypt/_projects/takeout-downloader/webgui/.env'
```

Most control-plane calls need the header `-H "X-Api-Token: <that-token>"`.

---

## 2. Get the portal URL and confirm the stack is up

```bash
# Containers should both say "Up"
ssh takeout-server 'docker ps --format "{{.Names}} | {{.Status}}"'

# Current public tunnel URL (cloudflared mints a NEW *.trycloudflare.com on
# every reconnect — always re-read it, never hardcode it):
ssh takeout-server 'docker logs takeout-tunnel 2>&1 | grep -oE "https://[a-z-]+\.trycloudflare\.com" | tail -1'
```

Portal login user/pass come from `webgui/.env` (`CUSTOM_USER` / `PASSWORD`).
Open `https://<user>:<pass>@<tunnel-host>/` in a browser to reach the hosted
Chromium desktop.

> **GOTCHA:** The tunnel URL changes on every cloudflared restart. If the portal
> 404s or won't load, re-run the `docker logs takeout-tunnel` line above to get
> the fresh URL.

---

## 3. Confirm the manager is healthy and pointed at the right folder

```bash
TOKEN=$(ssh takeout-server 'grep -oP "MANAGER_API_TOKEN=\K.*" /opt/local_cache_crypt/_projects/takeout-downloader/webgui/.env')

# Jobs list (empty list = clean slate, ready for a new account):
ssh takeout-server "docker exec takeout-webgui curl -s -H 'X-Api-Token: $TOKEN' 127.0.0.1:8080/api/jobs"

# Confirm downloads will land under /opt/archives/google-takeout:
ssh takeout-server 'docker exec takeout-webgui python3 -c "import sys; sys.path.insert(0,\"/work\"); from manager.config import get_config; print(get_config().takeout_root)"'
```

Expected `takeout_root`: `/opt/archives/google-takeout`. If it prints anything
else, **STOP** and tell the human — the storage config is wrong and downloads
will land in the wrong place.

---

## 4. Log the new account in + create the export (human does this)

In the hosted Chromium (via the portal):

1. Sign into the **new** Google account. The profile at `config/.chrome-profile`
   persists across restarts.
2. Go to `takeout.google.com`, select the data, and create the export.
3. Wait for Google to say the archive is ready. **This can take hours or days**
   for large accounts. There is nothing to download until it's ready.

> **GOTCHA — one primary account at a time.** The browser profile holds ONE
> primary signed-in account for the download cookie. To do multiple accounts,
> run them **sequentially** (finish one, then switch). Multi-login via
> `authuser=N` works only if the download URL carries the right `authuser` —
> don't rely on it unless the human explicitly set it up.

---

## 5. Capture + auto-submit (the normal happy path)

On the Takeout **"Manage exports"** page, the human clicks **Download** on any
ONE part. The extension (v4.1+) then automatically:

- captures the cookie + download URL,
- cancels Chrome's own native download of that part (the manager downloads
  server-side instead — a browser copy would waste disk),
- POSTs the capture to the manager,
- the manager derives the account label (email scrape → display name →
  `gaia-<user>` → `unknown-account`) and starts the job.

Open the **extension popup** and watch the **live status panel**: overall
progress bar, per-part rows (✓ done / ▼ active / 🔑 auth / ✗ error), a heartbeat
("last update Ns ago", warns if stalled), an error line, and a log tail.

A new job folder appears at
`/opt/archives/google-takeout/<label>/<export-ts>/`.

---

## 6. Watch progress from the shell (don't rely only on the popup)

```bash
# Find the active job id:
ssh takeout-server "docker exec takeout-webgui curl -s -H 'X-Api-Token: $TOKEN' 127.0.0.1:8080/api/jobs"

# Full per-part snapshot:
ssh takeout-server "docker exec takeout-webgui curl -s -H 'X-Api-Token: $TOKEN' 127.0.0.1:8080/api/jobs/<job_id>"

# Live log tail:
ssh takeout-server "docker exec takeout-webgui curl -s -H 'X-Api-Token: $TOKEN' '127.0.0.1:8080/api/jobs/<job_id>/log?lines=80'"
```

Healthy = `parts_done` climbing, `speed_bps` non-zero, heartbeat fresh.

---

## 7. THE CRITICAL RULE — the cookie is short-lived

This is the #1 thing that breaks downloads. Read it twice.

- The download cookie is **valid only ~1–2 minutes when idle.**
- BUT an **in-flight download stream keeps going for hours** — auth is checked
  only at request *start*, not continuously. (The first account pulled 2.8 TB
  over ~6 hours on one continuous session.)
- Therefore **any idle gap longer than ~1–2 min kills the next request.**
- A **stale** cookie does NOT return a clean 401. It returns **HTTP 302 →
  accounts.google.com/ServiceLogin**, and following it lands on an HTML login
  page. A naive `curl -C -` then reports "server does not support byte ranges"
  (it got 200 HTML, not 206).

If the job flips to `needs_cookie`, the extension (with autoRecapture ON)
re-clicks Download to refresh the cookie automatically. If that **livelocks**
(capture → expire → recapture, no bytes moving), go to step 8.

---

## 8. RECOVERY — manual live-cookie-jar pull (for stuck partials)

Use this when the manager livelocks on `needs_cookie` but most data is already
on disk and you just need the last few partial parts. It bypasses the manager's
slow pre-pass entirely. **Prereqs:** stack up, Chrome logged into Google, CDP on
`127.0.0.1:9222`, the Takeout manage page open.

1. **Pause the job** so it stops fighting you. The control plane takes the
   `job_id` in the POST body (NOT in the URL path), gated by the API token:
   ```bash
   ssh takeout-server "docker exec takeout-webgui curl -s -X POST -H 'X-Api-Token: $TOKEN' -H 'Content-Type: application/json' -d '{\"job_id\":\"<job_id>\"}' 127.0.0.1:8080/api/control/pause"
   ```
   (Resume is the same with `/api/control/resume`; cancel with `/api/control/cancel`.)

2. **Find incomplete parts** — compare on-disk file size to target size. Equal =
   done; smaller = partial; missing = not started. **Trust the actual byte sizes
   on disk**, not the state file (it can be stale after manual surgery).
   ```bash
   ssh takeout-server 'ls -la /opt/archives/google-takeout/<label>/<export-ts>/'
   ```

3. **Read the LIVE cookie jar over CDP** — this is the fresh cookie source, NOT
   the extension's stored `lastCapture`. **Must run INSIDE the container**
   (Chrome's DevTools rejects non-localhost Host headers; host-side CDP gets
   connection-reset). Open a CDP websocket to the browser target, call
   `Storage.getCookies`, and join every cookie whose `domain` contains
   `google.com` into a `name=value; name=value; …` header. This yields ~27
   cookies / ~3.5–4.5 KB — always as fresh as the logged-in session.

4. **Probe ONE incomplete part** with that cookie + `Range: bytes=0-0`. Expect
   **HTTP 206**. If **302 → ServiceLogin** or **200 text/html**, the session is
   logged out — have the human re-login in the browser, then retry from step 3.

5. **Fire all incomplete parts in parallel**, detached (a 50 GB part won't fit
   in a tool timeout):
   ```bash
   curl -sS -C - --retry 3 \
     -H "Cookie: <live-jar-cookie>" \
     -H "User-Agent: <UA from capture>" \
     -o "<dir>/<expected-filename>" \
     "https://takeout-download.usercontent.google.com/download/<file>?j=<archive>&user=<gaia>&authuser=0&i=<index>"
   ```
   `curl -C -` resumes from current on-disk size via Range. In-flight streams
   finish even after the cookie later rotates.

6. **Verify each part** when curl exits:
   - on-disk size == target size,
   - first 4 bytes are `PK\x03\x04` (zip magic),
   - the tail contains the EOCD signature `PK\x05\x06`.
   > **GOTCHA — do NOT run a full `zipfile.testzip()` on multi-TB archives over
   > the JuiceFS FUSE mount.** It reads every byte back (hours, FUSE-stall risk).
   > Use the tail/EOCD check above instead, or verify out-of-band.

7. **Finalize the job** so the UI matches reality — rewrite
   `.manager_state.json` (status=complete, all real parts done, correct totals)
   and re-finalize the manifest.

> **GOTCHA — phantom last index.** Discovery probes one index PAST the last real
> part to find the end. That phantom (e.g. a 0-byte or tiny `…-001.zip` stub)
> can get seeded into the state. It is NOT a real part. If `expectedParts: 62`,
> the real indices are 0–61 — delete the phantom file and drop the extra index
> when finalizing.

---

## 9. When the download is fully done

- All parts `done`, total bytes match.
- Spot-verify with the tail/EOCD check (step 8.6), NOT a full testzip.
- Tell the human the final path and total size:
  `/opt/archives/google-takeout/<label>/<export-ts>/`.

---

## 10. Extension settings sanity (one-time per browser profile)

If auto-POST returns 401 or the popup shows "Manager: not connected":

- The extension reads `managerUrl` + `captureToken` from `chrome.storage.local`
  (set via the options page) AND a managed-policy file at
  `/etc/chromium/policies/managed/takeout-manager.json` (written by
  `webgui/init_custom.sh`).
- Fix: open the extension options page in the hosted Chromium and set the
  manager URL (`http://127.0.0.1:8080`) and the capture token
  (`MANAGER_CAPTURE_TOKEN` from `webgui/.env`). Re-check the popup shows
  connected.

---

## 11. Restarting the stack (only if needed)

```bash
ssh takeout-server 'cd /opt/local_cache_crypt/_projects/takeout-downloader && docker compose -f docker-compose.webgui.yml up -d'
```

> **GOTCHA — config split.** Compose reads the **root `.env`** for interpolation
> (`STORAGE_ROOT`, `TAKEOUT_SUBDIR`), while runtime secrets live in
> **`webgui/.env`**. They are different files. `STORAGE_ROOT=/opt/archives`,
> `TAKEOUT_SUBDIR=google-takeout`.
>
> **GOTCHA — never set `STORAGE_ROOT=/opt`.** The container bakes its Python
> venv at `/opt/manager-venv`. Mounting host `/opt` over container `/opt`
> shadows the venv and the manager dies with exit 127. Keep `STORAGE_ROOT`
> pointed at a subdir like `/opt/archives`; the old `/opt/storage.jfs002` tree
> is kept visible via a separate read-only bind mount.
>
> A restart kills the current Chromium login session — the human must re-login.

---

## 12. If you get stuck — escalate with facts

When asking the human for help, include: the job id, the latest
`/api/jobs/<id>` snapshot, the last ~40 log lines, and which step you're on.
Don't guess at destructive fixes (deleting parts, rewriting state) without
confirmation. Read `docs/webgui/14-resume-cookies-multiaccount.md` for the deep
background on every gotcha above.
