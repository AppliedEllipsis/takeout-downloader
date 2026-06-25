# 13 — Disk-Full Root Cause, Repo Migration & Deployment Gotchas

This documents the session where the Chrome SIGKILL crashes were finally
root-caused (disk full on `/`), the project was relocated off the full disk,
and two non-obvious compose deployment bugs were found and fixed.

---

## TL;DR

- **Root cause of all the Chrome crashes:** `/dev/sda1` (`/`) hit **100% full**.
  Chrome aborts (`SIGKILL` / `Trace/breakpoint trap`) when it can't write its
  profile/cache/crash dumps; JuiceFS FUSE stalled (`folio_wait_bit_common`); the
  manager's state writes turned flaky. All three symptoms we chased for a whole
  session were one cause: no free space.
- **Biggest contributor:** unbounded container logs (selkies/KasmVNC video spam
  in `docker logs`) with **no log rotation**. Fixed with a `logging:` block.
- **Repo relocated** off the full disk to the local LUKS volume
  `/mnt/local_cache_crypt/_projects/takeout-downloader`.
- **Downloads still target** the old JuiceFS path (`STORAGE_ROOT=/opt/storage.jfs002`)
  — unchanged, so the in-flight 58/62-part job resumes to the same place.

---

## Storage layout (after migration)

| Path | Backing | Role |
|---|---|---|
| `/` = `/dev/sda1` (75 G) | local SSD | OS + Docker data-root (was the bottleneck) |
| `/mnt/local_cache_crypt` (300 G) | **LUKS** `/dev/mapper/cache_crypt`, local | **project repo + Chrome profile + temp** (fast, random-write safe) |
| `/mnt/archives` (1 PB) | rclone WebDAV→crypt→chunker (Hetzner Storage Box) | **finished archives only** (write-once, sequential) |
| `/opt/storage.jfs002` (1 PB) | JuiceFS | current download target (in-flight job) |

**Why the split:** `/mnt/archives` is a chunker remote (5 G chunks) — fine for
write-once archive blobs, terrible for random writes (Chrome SQLite profile,
`.manager_state.json` ticks, HTTP-Range resume temp). Those live on the local
LUKS disk. Cold finished archives can later move to `/mnt/archives`.

---

## The broken rclone `archives:` remote (fixed)

`/mnt/archives` returned `Input/output error` on every write. Root cause in
`/opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf`:

```ini
[hetzner_raw]
type = webdav
url = u455805.your-storagebox.de          # WRONG: no scheme -> "unsupported protocol scheme """
user = other                              # WRONG: must be the sub-account user
```

Fixes:
- `url = https://u455805-sub1.your-storagebox.de` (scheme + the **sub1** WebDAV host;
  the bare account host returns 200 unauth and serves no WebDAV; the `-sub1`
  host returns 401 = the real authenticated endpoint).
- `user = u455805-sub1`.
- The WebDAV password also had to be re-set (`rclone config update hetzner_raw pass ...`).

A running `rclone mount` caches the config at startup — after editing the
config you must remount for it to take effect.

---

## Repo migration (corrupt .git → fresh clone)

The disk-full event corrupted the old git repo (`unable to read tree`, invalid
reflog entries). Since HEAD still matched GitHub, the clean path was a **fresh
clone** at the new location rather than moving a broken `.git`:

```bash
git clone https://github.com/AppliedEllipsis/takeout-downloader.git \
  /mnt/local_cache_crypt/_projects/takeout-downloader
git -C ... checkout feat/internal-downloader
# Restore the gitignored runtime state (NOT in git):
cp /old/.env /old/webgui/.env  -> new location
cp -a /old/config  -> new location   # Chrome profile = persisted Google login (preserve!)
```

Compose binds are **relative** (`.:/work`, `./config:/config`), so starting
from the new directory relocates the repo + Chrome profile automatically.

---

## Two compose deployment gotchas (both cost real debug time)

### 1. MUST start with `--env-file webgui/.env`

Compose `${VAR}` substitution reads the **root `.env`** (or `--env-file`),
NOT the service's `env_file:`. `env_file:` only injects env *inside* the
container; it does not feed `${...}` interpolation in the compose file itself.

The tokens (`MANAGER_API_TOKEN`, `MANAGER_CAPTURE_TOKEN`) and `STORAGE_ROOT`
live in `webgui/.env`. Starting without the flag made them resolve empty.

**Always:**
```bash
docker compose --env-file webgui/.env -f docker-compose.webgui.yml up -d
```
Symptom if you forget: `health` shows `capture_token_set:false, api_token_set:false`.

### 2. `STORAGE_ROOT` unset → `/opt` shadows the manager venv

The storage passthrough bind is:
```yaml
- type: bind
  source: ${STORAGE_ROOT:-/opt}
  target: ${STORAGE_ROOT:-/opt}
```
If `STORAGE_ROOT` isn't in the substitution env, it falls back to `/opt` and
mounts **all of host `/opt`** over the container — burying the image's
`/opt/manager-venv`. The manager then crash-loops with **exit 127** (python
not found).

**Fix:** ensure `STORAGE_ROOT=/opt/storage.jfs002` is present where compose
reads substitution (we added it to the root `.env` too, belt-and-suspenders),
and always start with `--env-file webgui/.env`. Verify after boot:
```bash
docker exec takeout-webgui ls -la /opt/manager-venv/bin/python   # must exist
```

---

## Log rotation (the actual disk-full fix) — commit 21db89a

Added to BOTH services in `docker-compose.webgui.yml`:
```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```
Caps each container's logs at 30 MB instead of unbounded. The selkies/KasmVNC
video pipeline logs continuously; without this it fills `/` over days.

---

## Correct start/verify sequence (post-migration)

```bash
cd /mnt/local_cache_crypt/_projects/takeout-downloader
docker compose --env-file webgui/.env -f docker-compose.webgui.yml up -d

# new quick-tunnel URL (changes every cloudflared restart):
docker logs takeout-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1

# health MUST show both tokens true:
docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health
# {"ok":true,...,"capture_token_set":true,"api_token_set":true}

# venv must be un-shadowed:
docker exec takeout-webgui ls /opt/manager-venv/bin/python
```

Portal login (basic auth, embeddable in URL):
`https://takeout:passw0rd@<tunnel-host>/`
