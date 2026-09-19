# Infrastructure

**Verified live 2026-09-19** by read-only SSH probes (`.recon/probe2.sh` in the main checkout).
Everything here is measured, not assumed. Commands used are in [runbooks.md](runbooks.md).

## Host

| Fact | Value |
|---|---|
| SSH alias | `takeout-server` |
| Hostname | `ubuntu-8gb-fsn1-1` |
| RAM | **7 GB total — 7 used, 0 free, 0 available** |
| Uptime at probe | 147 days |
| Load average | 0.14, 0.18, 0.13 (idle — the constraint is **memory, not CPU**) |
| Root disk `/` | `/dev/sda1` ext4 — 75 G, 63 G used, **13 G free (84%)** ⚠️ |
| Staging disk | `/dev/mapper/cache_crypt` **LUKS + xfs** — 300 G, 6.9 G used, **293 G free** |

> ⚠️ **Never stage downloads on `/`** — only 13 GB free. Stage on the encrypted volume.

## Containers

| Container | Status | Notes |
|---|---|---|
| `takeout-webgui` | Up 6 weeks | The browser + manager environment. The memory hog. |
| `takeout-tunnel` | Up 6 weeks | Networking/tunnel. |

`docker stats` at probe time: `takeout-webgui mem=6.66GiB / 7.566GiB, cpu=86.65%`.

## Mounts on the host

| Mount | Type | Notes |
|---|---|---|
| `/` | ext4 | 13 G free |
| `/opt/local_cache_crypt` | xfs, **LUKS-encrypted** (`cache_crypt`) | 293 G free. Backs `/config` **and** `/work` **and** the rclone VFS cache — **all three share one physical budget**. |
| `/opt/archives` | **fuse.rclone** (`archive_chunked:`) | Archive destination. **Remote-backed, chunked** — not local disk. |
| `/opt/storage.jfs002/003/004` | **fuse.juicefs** | Historical staging volumes. |

## Container bind mounts (authoritative — from `docker inspect`)

| Host source | Container path | RW |
|---|---|---|
| `/opt/local_cache_crypt/_projects/takeout-downloader/config` | `/config` | rw |
| `/opt/local_cache_crypt/_projects/takeout-downloader` | `/work` | rw |
| `/opt/local_cache_crypt/rclone_vfs` | `/var/rclone_vfs` | **ro** |
| `/opt/storage.jfs002` | `/opt/storage.jfs002` | **ro** |
| `/opt/archives` | `/opt/archives` | rw |

> **Gotcha:** `/config` does **not** exist on the host. It is a container-only path.
> Running `df /config` on the host silently returns nothing — check
> `/opt/local_cache_crypt` instead. This briefly looked like a contradiction during recon.

`/config` currently holds ~213 MB (the Chromium profile). The rclone VFS cache is empty (0 B).

## Chromium (inside `takeout-webgui`)

| Fact | Value |
|---|---|
| User data dir | `/config/.chrome-profile` |
| Remote debugging | `127.0.0.1:9222` (CDP) |
| Binary | `/usr/lib/chromium/chromium`, built on Debian GNU/Linux 13 (trixie) |
| Launch flags | `--load-extension --enable-remote-extensions --disable-dev-shm-usage --enable-gpu-rasterization --no-default-browser-check --disable-pings --media-router=0` |
| Processes | **119** |
| Main process age | **43 days** (PID 2585533, RSS ~1.08 GB) |
| Extension | `chrome-extension://dgbbpdjpfeeaiheekoclkkkbipkikejl/background.js` |

> **The browser is long-lived and stateful.** It had been up 43 days at probe time.
> Any design assuming "just restart the browser" discards weeks of accumulated
> session state. Design for a browser that never restarts.

## Discrete services / ports

**Published port mappings (from `docker port`, verified 2026-09-19):**

| Container port | Host binding | Serves | Live state |
|---|---|---|---|
| 3000 | `127.0.0.1:3000` | **webgui / remote desktop** (the thing to open) | ✅ **HTTP 200** |
| 8080 | `127.0.0.1:8080` | manager API | ❌ **HTTP 000 — nothing serving** |
| 9222 | `127.0.0.1:9222` | Chromium DevTools (CDP) | ✅ live |
| — | via `takeout-tunnel` | external reachability | ❌ **BROKEN** (see below) |

All binds are **127.0.0.1 only** — there is no direct public port.

### ⚠️ The tunnel is DOWN, so the documented URL is dead

`takeout-tunnel` is a cloudflared **QUICK** tunnel (`--url http://127.0.0.1:3000`), so its
`*.trycloudflare.com` hostname changes on every restart. Last known hostname:
`https://project-rapids-vacation-research.trycloudflare.com` (docs/webgui/10-deployment-status.md:33).

**Verified 2026-09-19: that URL is unreachable** (`curl` → `HTTP 000`) and the container is in a
permanent retry loop:

```
ERR Failed to dial a quic connection error="failed to dial to edge with quic:
  write udp [::]:42498->198.41.192.227:7844: sendmsg: network is unreachable"
INF Retrying connection in up to 1m4s
```

The container reports `restarts=0`, policy `unless-stopped`, started **2026-08-06** — it has been
silently failing for weeks while appearing healthy.

**Likely cause (high confidence):** the host has a global IPv6 address
(`2a01:4f8:c012:9c7f::1/64`) but **no IPv6 default route** (`ip -6 route` shows only the link-local
and on-link prefix). cloudflared binds an IPv6 socket `[::]` and tries to reach an IPv4 edge →
`network is unreachable`. QUIC is also UDP, which is commonly filtered.

**Probable fix (not yet applied — needs approval):** add `--edge-ip-version 4` and `--protocol http2`
(forces TCP, IPv4) to the tunnel command.

### ✅ Working access path right now: SSH port-forward (no server change)

The webgui answers **HTTP 200** on the host's `127.0.0.1:3000`, so a local forward bypasses the
broken tunnel entirely:

```bash
ssh -L 3000:127.0.0.1:3000 takeout-server
# then open http://localhost:3000 in your browser
```

This is the recommended access method regardless of tunnel state — it needs no public exposure and
no config change.

> **Note:** this failure is documented failure mode **1.11 "cloudflared URL rotation"** — one of
the twelve missing procedures. `docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:122` notes the URL changes
on restart, but **nothing documents what to do when the tunnel cannot connect at all.**

## Known live degradation (2026-09-19)

- **314 `page` targets** stuck at `accounts.google.com/v3/signin/identifier`, plus
  87 `accounts.youtube.com` iframes and 2 `restart` pages = **406 targets, 403
  google-account** targets. These consume essentially the whole box's memory.
- Exactly **one** good `takeout.google.com/manage` tab and one webgui tab exist and must be preserved.
- Root cause is a loop that opens a tab per tick; cleanup ordering matters (stop the
  spawner **before** closing tabs or they refill). Plan: `.recon/07-live-state-and-cleanup-plan.md`.

## Accounts and export history

One directory per account under `/opt/archives/google-takeout/` (verified):

| Account label | Export(s) on disk |
|---|---|
| **`andrew`** | `2026-08-06-02-27-46` (most recent activity) |
| **`braincreation`** | `2026-06-23-03-59-47` (the 62-part / 3.08 TB export) |

Download root is `<storage_root>/google-takeout/<account>/<export-ts>/` (`manager/config.py`,
`takeout_subdir` via env `TAKEOUT_SUBDIR`).

⚠️ **The v2 ledger sits ON the rclone FUSE mount.** `/opt/archives/google-takeout/` contains
`state.db`, `state.db-shm` and `state.db-wal` (156 KB WAL) — i.e. the production deployment puts a
WAL-mode SQLite database on a network-backed FUSE filesystem. This **verifies** the `state.db` hazard
previously assessed as "partially true": it is not merely a doc example, it is the live
configuration. Move it to the LUKS volume. *(v3 refuses such a path outright — `autopilot/ledger.py`.)*

✅ **The Chromium profile IS signed in — corrected 2026-09-19.** An earlier reading of "logged out"
(based on tab *titles* `Sign in - Google Accounts`) was an artifact of stale renderers from a period when
the session had lapsed. Verified twice since, independently:

1. `Account: Google Account: BrainCreation` read from the archive page's own DOM.
2. A screenshot of the desktop, read visually, shows the Google Account → Summary page with the export
   list and **no sign-in prompt**.

So no human sign-in is needed to *read* the manage page. A human *is* still needed to satisfy the ReAuth
password challenge that minting requires (see `measured-facts.md`).

## The desktop is visible and clickable (X11 + VNC)

The container runs Chromium against **`DISPLAY=:1`** (Xvfb, virtual screen `15360x8640x24`, live viewport
**2142x1372**), served to the browser over KasmVNC. That means the whole desktop — not just the page DOM —
is observable and drivable. Verified 2026-09-19.

| Capability | Tool | Status |
|---|---|---|
| **See the browser viewport** | CDP `Page.captureScreenshot` → PNG (2146x1145, ~119 KB) | ✅ verified, and read with a vision model |
| **See the whole desktop** | `xwd -root` → `tools/xwd_to_png.py` → PNG (2144x1372, ~198 KB) | ✅ verified coherent (no stride artifacts) |
| **Move/click the pointer** | `xdotool mousemove` / `click` | ✅ present, not yet exercised |
| **Type / send keys** | `xdotool type` / `key` | ✅ present, not yet exercised |
| **List / target windows** | `xdotool search --name …` | ✅ active window reads as `Google Takeout - Chromium` |
| **Clipboard** | `xclip` | ✅ present |
| ImageMagick, netpbm, ffmpeg, numpy | — | ❌ **absent** (hence the converter tool) |
| Pillow | in container `python3` | ✅ present |

**Why this matters beyond convenience:** it is a *fallback for anything CDP cannot reach* — native Chrome
dialogs, the download shelf, KDE notifications — and it gives **pixels as ground truth** for state that the
DOM reports misleadingly. The whole-desktop capture immediately showed something the DOM read had missed:
**two tabs open** (`Google Takeout` and `Download history`).

**Design consequence for ReAuth (§4.2):** the human burden can drop from *"open the webgui and type a
password"* to *"approve a prompt"*. `xdotool` can focus the Chromium window and type the password step; with
SMS/Google-prompt 2FA the second factor still needs the owner's phone. That is a smaller ask than the
design currently assumes, and is worth weighing before implementing the re-auth path.

## Open questions

- What spawns a tab per tick? (recon agent #3)
- Is `/opt/archives` (rclone FUSE) fast enough for the mover, or does its latency
  re-create the historical ~6-minute per-part `stat()` livelock?
- Does a Chromium restart preserve Google auth? **Unverified assumption.**
- Why is port 8080 (manager API) not serving? Is the manager down, or on another port?
