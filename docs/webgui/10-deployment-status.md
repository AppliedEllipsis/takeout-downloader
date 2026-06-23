# 10 — Live Deployment Status & Operations Runbook

What is actually deployed and running on the server, how to reach it, how to
operate it, and how the security posture was verified. This is the "as-built"
record — distinct from `05-deployment.md` (the plan) and `09-smoke-test.md`
(the test procedure).

> Last deployed: 2026-06-23. Server: `ellipsis@188.245.169.166`
> (`ubuntu-8gb-fsn1-1`, Ubuntu 24.04, 2 vCPU / 7.6 GiB RAM).

---

## 1. TL;DR — current state

| Component | State | Where |
|---|---|---|
| webtop container | **running**, self-healing | `takeout-webgui` |
| Manager (FastAPI) | **healthy** | `127.0.0.1:8080` (in-container) |
| Chromium + CDP | **running**, auto-launch | `127.0.0.1:9222` (in-container) |
| Extension v4 | **loaded** (id `dgbbpdjpfeeaiheekoclkkkbipkikejl`) | unpacked from `/work/helpers` |
| KasmVNC portal | **running, auth-gated** | `127.0.0.1:3000` (in-container) |
| Cloudflare tunnel | **running** (zero-config quick tunnel) | `takeout-tunnel` |
| Telegram | **disabled** (deferred) | — |

**Public URL:** changes on every cloudflared restart (quick tunnel). Get the
current one:

```bash
ssh -i ~/.ssh/takeout_deploy ellipsis@188.245.169.166 \
  'docker logs takeout-tunnel 2>&1 | grep -o "https://[a-z0-9-]*\.trycloudflare\.com" | tail -1'
```

Last known: `https://project-rapids-vacation-research.trycloudflare.com`

**Portal login (TEMPORARY — change before leaving it up with a Google session):**
`takeout` / `passw0rd`

---

## 2. Access paths

### Public (internet) — portal only
The Cloudflare quick tunnel exposes **only** the KasmVNC portal on `:3000`,
behind nginx basic auth (`takeout` / `passw0rd`). Open the trycloudflare URL,
authenticate, and you get the desktop with Chromium already open on Takeout +
the manager UI.

### SSH (operator / agent) — everything else
The manager API (`8080`) and Chrome CDP (`9222`) are **localhost-only** on the
server and never touch the tunnel. Reach them with port-forwards:

```bash
ssh -i ~/.ssh/takeout_deploy \
  -L 8080:127.0.0.1:8080 \
  -L 9222:127.0.0.1:9222 \
  -N ellipsis@188.245.169.166
# then on your machine:
#   http://127.0.0.1:8080/         manager UI
#   http://127.0.0.1:8080/api/control/health
#   http://127.0.0.1:9222/json/version   Chrome DevTools Protocol
```

### SSH key
A dedicated ed25519 key was installed (`~/.ssh/takeout_deploy[.pub]`).
Password auth on the server is **left enabled** (per request). Key login is
confirmed working; the password used to install the key was never written to
disk (throwaway scripts, deleted).

---

## 3. Where things live on the server

```
/opt/storage.local_1/projects/takeout-downloader   # the git clone (branch feat/internal-downloader)
  webgui/.env                  # deployment config — MODE 600, NOT in git
  docker-compose.webgui.yml    # the stack
  config/                      # PERSISTENT browser profile + KasmVNC state (NOT in git)

/opt/storage.jfs002/google-takeout/                 # downloads land here (1.12 PB JuiceFS mount)
  <account-label>/<export-ts>/                       # e.g. braincreation/2026-06-16-04-01-04/
    *.zip, manifest.json, .manager_state.json, takeout_dl.log
```

`STORAGE_ROOT=/opt/storage.jfs002` so downloads use the petabyte-scale JuiceFS
mount, **never** the 75 GB root disk.

---

## 4. webgui/.env (server, mode 600, not committed)

Keys present (values redacted):

```
STORAGE_ROOT=/opt/storage.jfs002
TZ=Etc/UTC
MANAGER_API_TOKEN=<generated 64-hex>      # control plane (pause/resume/cancel/recapture/diagnose)
MANAGER_CAPTURE_TOKEN=<generated 64-hex>  # narrow: POST /api/payload + recapture-pending only
MANAGER_PUBLIC_URL=http://127.0.0.1:8080
CUSTOM_USER=takeout                       # KasmVNC portal basic-auth user
PASSWORD=passw0rd                         # KasmVNC portal basic-auth pass — TEMPORARY
TELEGRAM_ENABLED=false                    # deferred
```

> Compose interpolation reads `${VAR}` from the file passed via `--env-file`,
> and the `environment:` block overrides `env_file:`. So **always run compose
> with `--env-file webgui/.env`** or the tokens/storage resolve empty. See §7.

---

## 5. Operations — common commands

All run from `/opt/storage.local_1/projects/takeout-downloader` on the server.
Prefix everything with the env-file flag (critical — see §7):

```bash
cd /opt/storage.local_1/projects/takeout-downloader
DC="docker compose -f docker-compose.webgui.yml --env-file webgui/.env"
```

| Task | Command |
|---|---|
| Status | `$DC ps` |
| Manager health | `docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health` |
| Get tunnel URL | `docker logs takeout-tunnel 2>&1 \| grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' \| tail -1` |
| Restart manager only (after `git pull`) | `docker exec takeout-webgui s6-svc -r /run/service/takeout-manager` or recreate |
| Recreate webgui (env/profile change) | `$DC up -d --force-recreate webgui` |
| Rebuild (Dockerfile/script change) | `$DC build webgui && $DC up -d --force-recreate webgui` |
| Restart tunnel (new URL) | `$DC restart cloudflared` |
| Manager log | `docker exec takeout-webgui tail -f /work/<...>/takeout_dl.log` |
| Chromium tabs (CDP) | `docker exec takeout-webgui curl -s 127.0.0.1:9222/json` |

### Update workflow (code change)
The repo is bind-mounted at `/work`, so engine + manager + extension changes
need no rebuild — just pull and restart the manager:

```bash
git pull
$DC up -d --force-recreate webgui   # picks up /work changes; clears stale chromium lock
```

Rebuild is only needed when `Dockerfile.webtop`, `manager/requirements.txt`, or
the s6 service scripts (`webgui/*.sh`, COPYed into the image) change.

---

## 6. Security posture (as verified 2026-06-23)

The single most important property: **the tunnel exposes only the auth-gated
portal.** Verified through the PUBLIC trycloudflare URL, not just locally:

| Check | Expected | Result |
|---|---|---|
| Portal via tunnel, no creds | 401 | ✅ 401 |
| Portal via tunnel, `takeout:passw0rd` | 200 | ✅ 200 |
| Manager API (`/api/control/health`) via tunnel | not reachable | ✅ 401 nginx (not manager JSON) |
| Manager UI path via tunnel | not reachable | ✅ 404 |
| CDP (`/json/version`) via tunnel | not reachable | ✅ 404 |
| Host port bindings | `127.0.0.1` only | ✅ 3000/8080/9222 all loopback |

### ⚠ Known risk with the zero-config tunnel
A quick `trycloudflare` tunnel has **no Cloudflare Access** layer. The KasmVNC
password is the ONLY gate between the internet and a browser that will hold your
Google session. Implications:

- `passw0rd` is weak and brute-forceable over a public URL. **Change it before
  logging into Google and leaving the stack up.**
- To change: edit `PASSWORD` in `webgui/.env`, then `$DC up -d --force-recreate webgui`.
- For the stronger posture the design originally specified (email/SSO gate),
  use a free Cloudflare account + a *named* tunnel with an Access policy in
  front of the hostname (see `05-deployment.md` §Cloudflare Access). That
  replaces "URL + weak password" with "your identity + password".

---

## 7. Gotchas learned during deployment (the as-built fixes)

These were real failures hit on the live box that the local build could not
surface. All are fixed and committed.

1. **Root disk was 100% full** — not this project. `/var/log/juicefs.log` had
   grown unbounded to **22 GB**. Truncated in place (`: > file`, keeps the inode
   so JuiceFS keeps writing, no restart) and installed
   `/etc/logrotate.d/juicefs` (daily, maxsize 500M, 7 rotations, `copytruncate`,
   `su root root`). Reclaimed 22 GB → root at 72%.

2. **s6 services need `#!/usr/bin/with-contenv bash`.** Without it, custom
   services run with a clean environment and never see `STORAGE_ROOT` /
   `MANAGER_*_TOKEN` — the manager silently fell back to `/opt` + no tokens.
   Both `manager-service.sh` and `init_custom.sh` now use the `with-contenv`
   shebang.

3. **KDE Plasma ignores XDG `~/.config/autostart` on container boot.** Chromium
   wouldn't auto-launch. Replaced the `.desktop` autostart with a dedicated s6
   service (`webgui/chromium-service.sh`) that waits for the X display, launches
   as user `abc` on `DISPLAY=:1`, and is supervised (restart-on-exit).

4. **Stale Chromium `SingletonLock` after `--force-recreate`.** The lock
   symlinks to `<host>-<pid>` from the previous container; the pid is gone but
   the lock blocks every new launch. The chromium service now removes
   `SingletonLock`/`SingletonCookie`/`SingletonSocket` when no chromium is
   running. Self-healing verified across repeated recreates.

5. **Compose `--env-file` is mandatory.** `${VAR}` interpolation in the
   `environment:` block reads from the project root `.env` (which belongs to a
   *different* project here) unless `--env-file webgui/.env` is passed. Always
   include it.

6. **Ubuntu webtop `chromium` is a snap shim** that won't run headless in the
   container. Switched the base to `lscr.io/linuxserver/webtop:debian-kde`,
   where `chromium` is a real `.deb` (verified Chrome 149 executes).

7. **Telegram token lives in `~/.pi/agent/auth.json`** (`telegram.token`), not
   the env. The `--capture-chat-id` helper now resolves the token from pi auth
   automatically. (Telegram still deferred/disabled.)

---

## 8. What's left (interactive / your call)

1. **Log into Google** via the portal (one-time, 2FA — cannot be automated).
   Everything downstream needs this. The persistent `/config` profile means the
   login survives container restarts.
2. **Live workflow test** — once logged in, the capture→download→resume cycle
   can be driven over CDP and confirmed end to end.
3. **Change the portal password** from `passw0rd` (see §6).
4. **Telegram** (optional) — capture a chat_id, set it in `webgui/.env`, flip
   `TELEGRAM_ENABLED=true`, recreate. (Bot is `@Pi_Lip_bot`; a bot cannot create
   a channel — you create it and add the bot as admin, or just DM the bot.)
5. **Stronger tunnel** (optional) — named Cloudflare tunnel + Access policy for
   an SSO gate instead of basic auth.
