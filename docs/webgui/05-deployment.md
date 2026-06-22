# 05 — Deployment

Server: `ellipsis@188.245.169.166`. Repo already cloned at
`/opt/storage.local_1/projects/takeout-downloader`. This doc is the operator
runbook: build the webtop+browser+manager stack, wire the Cloudflare tunnel to
**only** the KasmVNC portal, and harden everything else to localhost.

> Nothing here is executed yet. This is the plan. Build phases and acceptance
> gates live in `07-build-phases.md`.

## Target layout on the server

```
/opt/storage.local_1/projects/takeout-downloader/
  docker-compose.webgui.yml      # NEW: the webtop + manager stack
  webgui/
    Dockerfile.webtop            # webtop + Chromium flags + manager process
    init_custom.sh               # webtop init: install manager deps, seed profile
    profile-seed/                # bookmarks, pinned ext, default tabs, extension
    cloudflared/
      config.yml                 # tunnel: portal only
  manager/                       # the FastAPI app (see 02-manager-service.md)
  helpers/                       # extension v4 (see 03-extension-v4.md)
  takeout_dl.py                  # engine (one additive refactor)
```

Output archives still land under `/opt/storage.*/google-takeout/<workflow>`,
exactly as the CLI does today.

## The compose stack

Two containers sharing a localhost network namespace, or one container running
both. Recommended: **one webtop container** that also runs the manager as a
background process (simplest localhost wiring, shared profile + storage). A
sibling-container split is the fallback if the manager needs independent restart.

```yaml
# docker-compose.webgui.yml  (PLANNED shape, not final)
services:
  webgui:
    build:
      context: .
      dockerfile: webgui/Dockerfile.webtop
    container_name: takeout-webgui
    security_opt:
      - seccomp:unconfined         # Chromium under KasmVNC
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - TITLE=Takeout Browser
      - STORAGE_ROOT=/opt           # output root (allowlist-checked)
      - MANAGER_API_TOKEN           # from .env (control plane)
      - MANAGER_CAPTURE_TOKEN       # from .env (extension POST)
      - TELEGRAM_TOKEN              # from .env (see 06-telegram.md)
      - TELEGRAM_CHAT_ID            # from .env
    volumes:
      - ./config:/config            # PERSISTENT browser profile + KasmVNC creds
      - .:/work                     # live source (engine + manager), like today
      - type: bind
        source: /opt
        target: /opt
        bind:
          propagation: rslave
          recursive: enabled        # JuiceFS/encfs submounts, same as today
    devices:
      - /dev/dri:/dev/dri           # GPU passthrough (KasmVNC perf)
    shm_size: "2gb"                 # prevent Chromium crashes
    ports:
      - "127.0.0.1:3000:3000"       # KasmVNC portal  -> cloudflared attaches here
      - "127.0.0.1:8080:8080"       # manager UI+API  -> SSH-forward only
      - "127.0.0.1:9222:9222"       # Chromium CDP    -> SSH-forward only
    restart: unless-stopped

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: takeout-tunnel
    command: tunnel --config /etc/cloudflared/config.yml run
    volumes:
      - ./webgui/cloudflared:/etc/cloudflared:ro
    network_mode: "service:webgui"  # share webgui's localhost so it can reach :3000
    depends_on: [webgui]
    restart: unless-stopped
```

> Note the port bindings are pinned to `127.0.0.1` on the host. Even on the
> server, 8080 and 9222 never bind `0.0.0.0`. Only cloudflared (sharing the
> namespace) bridges 3000 outward.

## Chromium launch flags (inside the webtop)

Chromium must come up logged-in-capable, CDP-enabled, and pointed at the manager.
Launched from the webtop autostart (`init_custom.sh` seeds this):

```bash
chromium \
  --user-data-dir=/config/.chrome-profile \   # PERSISTENT: login survives restart
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \              # agent CDP (localhost only)
  --load-extension=/work/helpers \            # extension v4 (unpacked)
  --no-first-run --no-default-browser-check \
  --restore-last-session \                     # reopen takeout + manager tabs
  https://takeout.google.com/ http://127.0.0.1:8080/
```

CDP bound to `127.0.0.1` matters: combined with the `127.0.0.1:9222` host bind,
the debugging port is unreachable except through the SSH forward.

## Cloudflare tunnel — portal only

```yaml
# webgui/cloudflared/config.yml
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: takeout.<your-domain>
    service: http://127.0.0.1:3000      # KasmVNC portal ONLY
  - service: http_status:404            # everything else refused
```

The tunnel maps exactly one hostname to the portal. There is no ingress rule for
8080 or 9222, so the manager and CDP are unreachable from the internet even if
someone guesses the tunnel hostname.

### Cloudflare Access (gate the portal)

The portal drives a Google-logged-in browser, so it must not be open. Put a
Cloudflare Access policy in front of `takeout.<your-domain>`:

- Application: `takeout.<your-domain>`
- Policy: allow only your email (one-time PIN or your IdP).
- Session duration: short (e.g. 24h).

Result: reaching the portal requires Cloudflare Access identity **and** then the
KasmVNC password. Two gates before anyone touches the browser.

## Profile seeding (bookmarks, pinned ext, default tabs)

One-time, baked into `webgui/profile-seed/` and copied into
`/config/.chrome-profile` on first boot by `init_custom.sh` if absent:

- **Bookmarks** (`Bookmarks` JSON): Takeout home, Manage exports, Manager UI,
  Manager recipes.
- **Pinned extension**: set the v4 extension as pinned in the toolbar
  (`Preferences` → `extensions.pinned_extensions`).
- **Default/restored tabs**: Takeout + Manager UI (the launch flags above).
- **Extension settings**: pre-seed `managerUrl`, `captureToken` into the
  extension's `chrome.storage.local` via a managed-storage policy file so you
  don't paste the token by hand.

After first boot the profile is persistent; reseeding only happens if `/config`
is wiped.

## Audio (the "support sound" requirement)

webtop/KasmVNC carries audio in the browser session natively — no PulseAudio
forwarding needed. The manager uses it for the cookie-expiry / login-needed
**alert sound** by playing a short clip in a manager-UI tab (Web Audio), which
KasmVNC streams to your portal. Telegram is the out-of-band alert when you're not
looking at the portal.

## Secrets handling

- `.env` on the server holds `MANAGER_API_TOKEN`, `MANAGER_CAPTURE_TOKEN`,
  `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. File mode `600`, owned by the deploy user.
- The Telegram token is the same one already in `~/.pi/agent/auth.json`
  (`telegram.token`); copy it into the server `.env` (don't hardcode in compose).
- Never bake tokens into the image or commit them. `.gitignore` already covers
  `.env`.
- KasmVNC password set via webtop env/`/config`; treat as account-equivalent.

## Update workflow (same spirit as today)

```bash
cd /opt/storage.local_1/projects/takeout-downloader
git pull
# code (engine + manager + extension) is bind-mounted at /work, so:
docker compose -f docker-compose.webgui.yml restart webgui   # picks up new code
# rebuild only when the Dockerfile or system deps change:
docker compose -f docker-compose.webgui.yml build webgui
```

## Verification (smoke tests, expanded in 07)

1. Portal reachable via the Cloudflare hostname, Access prompt appears.
2. `curl 127.0.0.1:8080/api/control/health` over SSH forward returns healthy.
3. `curl 127.0.0.1:9222/json/version` over SSH forward returns Chrome version.
4. 8080/9222 are **not** reachable from the public hostname (negative test).
5. A one-part download (`max_exports=1`) completes end to end from a capture.
