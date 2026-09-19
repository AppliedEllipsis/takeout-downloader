# Failure modes — runtime & exposure (1.11, 1.12, 1.17)

Scope: the three "operational bible" gaps whose symptom is *the deployment stops being reachable /
stops authenticating / stops existing*, rather than a storage or export problem. Companion to
`failure-modes.md` (index) and `measured-facts.md` (the numbers).

---

## 1.11 — cloudflared URL rotation

**Advertised cost: 0.** Live-broken as of 2026-09-19: the tunnel is not rotating, it is down.

**Symptom.**

The bookmarked portal URL returns nothing (`curl` → `HTTP 000`) and will not return with the same
hostname — the deployment runs cloudflared in **QUICK (zero-config) mode**, so the
`https://<random>.trycloudflare.com` host is minted fresh on every restart
(`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:122`). The container log loops on:

```
ERR Failed to dial a quic connection error="failed to dial to edge with quic:
  write udp [::]:42498->198.41.192.227:7844: sendmsg: network is unreachable"
INF Retrying connection in up to 1m4s
```

**Root cause.**

1. **Rotation, by design.** `docker-compose.webgui.yml:128-139` runs
   `command: tunnel --no-autoupdate --url http://127.0.0.1:3000` with **no** `--edge-ip-version` and
   **no** `--protocol`. A quick tunnel has no account and no stable hostname.
2. **The outage.** The host has a global IPv6 address (`2a01:4f8:c012:9c7f::1/64`) but **no IPv6 default
   route**; cloudflared binds `[::]` and tries to reach an IPv4 edge over **QUIC (UDP)** →
   `network is unreachable`. The container reports **`restarts=0`, `unless-stopped`, started 2026-08-06**
   — silently failing for weeks while `docker ps` shows it `Up` *(measured `infrastructure.md`)*.

**Detection.**

```bash
ssh -o BatchMode=yes takeout-server 'docker port takeout-webgui | grep 3000'   # 127.0.0.1:3000 (compose:107)
ssh -o BatchMode=yes takeout-server 'curl -s -o /dev/null -w "%{http_code}\n" 127.0.0.1:3000'
#   200 -> app is fine, only the tunnel is broken -> use the SSH forward
ssh -o BatchMode=yes takeout-server 'docker logs --tail 20 takeout-tunnel 2>&1'
#   "quic ... network is unreachable" -> this failure
ssh -o BatchMode=yes takeout-server \
  'docker logs takeout-tunnel 2>&1 | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | tail -1'
```

`restarts=0` + `Up` is **not** health. Read the log, never the status column.

**Recovery.**

Primary access is the SSH forward — no server change, no public exposure, independent of the tunnel
(portal binds loopback):

```bash
ssh -N -L 3000:127.0.0.1:3000 takeout-server     # then open http://localhost:3000
```

Same shape reaches the two other loopback-only surfaces, **never** tunneled (compose:107-109):

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8080:127.0.0.1:8080 -L 9222:127.0.0.1:9222 takeout-server
```

Only if a public URL is genuinely required (after the fix below):

```bash
ssh -o BatchMode=yes takeout-server \
  'cd <REPO> && docker compose --env-file webgui/.env -f docker-compose.webgui.yml restart cloudflared'
# never `docker compose restart webgui` — it tears down the shared netns and kills the tunnel
# (docs/webgui/12-operations-runbook.md:44-47)
```

**Verification.**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000      # via the forward: 200
ssh -o BatchMode=yes takeout-server \
  'docker logs --tail 5 takeout-tunnel 2>&1 | grep -c "Registered tunnel connection"'
```

A `trycloudflare.com` URL counts as verified only when fetching **it** reaches the portal — KasmVNC
basic auth means a `401` challenge, not `200`; `000` means still broken.

**Attempt cost.**

**0** — SSH, loopback `curl`, Docker. No Google download host is contacted. Advertised figure confirmed.

**What v3 must change.**

1. **Do not depend on a public URL for operations.** Ship the SSH forward as the documented default;
   the tunnel is an optional convenience.
2. **Force IPv4 + TCP** on the tunnel command (`--edge-ip-version 4 --protocol http2`) so a host with a
   broken IPv6 route can still dial out. *(UNVERIFIED — proposed, not applied, needs approval.)*
3. **Health must be end-to-end, not per-process.** `restarts=0` masked a weeks-long outage; alert on the
   edge handshake, not the container status.
4. **Prefer token-mode with a stable hostname** when public access is required, so rotation stops being
   an operational event (compose:121-127).

---

## 1.12 — Token mismatch (401 on auto-POST)

**Advertised cost: 0.**

**Symptom.**

The extension's capture POST is rejected; payloads never land. Manager logs show `401` with detail
`bad capture token` (or `bad api token`), and `GET /api/control/health` reports the two sides disagreeing.
On the **live deployment both flags are `false`** — that is the *working* configuration, not the broken
one. Breakage starts the moment somebody sets exactly one side.

**Root cause.**

Auth is a **single shared static secret compared with `!=`** — no signing, no rotation, no per-client
identity:

- `manager/app.py:110-112` — `if cfg.capture_token and token != cfg.capture_token: raise HTTPException(401, "bad capture token")`
- `manager/app.py:115-117` — the same for `api_token`
- `manager/config.py:45-46` — both from `MANAGER_CAPTURE_TOKEN` / `MANAGER_API_TOKEN`

The convention is **empty ⇒ that surface is OPEN** ("empty-matches-empty"): the `if cfg.capture_token`
guard skips auth entirely when `""`. Both sides are empty today, so every auto-POST succeeds.

The two sides are configured in **different places**, which is why single-sided edits are easy:

| Side | Source |
|---|---|
| Manager | `MANAGER_CAPTURE_TOKEN` / `MANAGER_API_TOKEN`, via Compose from `webgui/.env` |
| Extension | `chrome.storage.local` first, then the managed policy `/etc/chromium/policies/managed/takeout-manager.json` (`webgui/init_custom.sh:88-99`; `CAPTURE_TOKEN="${MANAGER_CAPTURE_TOKEN:-}"` at `:21-22`) |
| In-page paste box | tokens injected into the HTML `<head>` (`manager/app.py:359-365`) |

Two traps that turn a 30-second fix into a lost evening:

1. **The extension only sends the header when it has one** — `helpers/background.js:267`, with
   `captureToken: ''` documented at `:36` as "empty => dev/open manager". An empty extension token is
   *silent*, not an error, until the manager demands one.
2. **`chrome.storage.local` overrides the managed policy** — `background.js:215-218` reads local storage;
   `:304` only falls through to `resolveManagedToken()` (`:291-297`) when local is empty. Editing the
   policy file alone may change **nothing**.

**Detection.**

```bash
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health'
#   false+false = OPEN (working) ; true+false or false+true = BROKEN
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui cat /etc/chromium/policies/managed/takeout-manager.json'
#   managed fallback only — a non-empty chrome.storage.local value wins over it
ssh -o BatchMode=yes takeout-server 'docker logs --tail 50 takeout-webgui 2>&1 | grep -i "bad .* token"'
```

`false / false` is **not** a misconfiguration — do not "fix" it.

**Recovery.**

Decide the end state, then change **both** sides in one pass. Never one.

**Option A — keep it open (current live state): blank `MANAGER_API_TOKEN` and `MANAGER_CAPTURE_TOKEN`
in `webgui/.env`.** **Option B — set a real token everywhere: put the same value in
`MANAGER_CAPTURE_TOKEN`.** Both then recreate the webgui, which also re-renders the policy through
`init_custom.sh`:

```bash
ssh -o BatchMode=yes takeout-server \
  'cd <REPO> && docker compose --env-file webgui/.env -f docker-compose.webgui.yml up -d --force-recreate webgui'
```

Option B additionally needs the extension side, authoritative — write `chrome.storage.local`, not just
the managed policy.

```bash
# Use the CDP helper convention from .recon/flip_recapture.py (container has `websockets`;
#     websocket-client is installed nowhere).
scp .recon/set_extension_token.py takeout-server:/tmp/tk_token.py
ssh -o BatchMode=yes takeout-server \
  'docker cp /tmp/tk_token.py takeout-webgui:/tmp/tk_token.py >/dev/null \
   && docker exec takeout-webgui python3 /tmp/tk_token.py'
```

The paste box needs no manual action either way: `GET /` injects the current tokens into the served HTML
(`app.py:359-365`).

> **Hard rule (`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:126`): DO NOT fix one side.** Setting only the
> manager's (or only the extension's) token breaks the auto-POST while looking like a security
> improvement — and its `401` is indistinguishable from dead Google auth to a tired operator.

**Verification.**

```bash
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health'
# Option A: false / false   Option B: true / true (equal, both sides)
ssh -o BatchMode=yes takeout-server 'docker logs --tail 20 takeout-webgui 2>&1 | grep -c "bad .* token"'  # want 0
```

**Attempt cost.**

**0** — token checks and container recreation touch no Google download host. Confirmed.

**What v3 must change.**

1. **Remove the ambiguity of empty.** Fail closed at boot with a loud error, or make "open" an explicit
   `AUTH_DISABLED=true` decision instead of a blank string.
2. **One source of truth for the secret** — derive the extension's value from an authenticated manager
   bootstrap endpoint instead of the env + managed-policy + `storage.local` chain that lets sides drift.
3. **Make the mismatch self-diagnosing:** the extension reports token *presence*; disagreement alerts,
   instead of surfacing as a bare `401`.
4. **Never inject secrets into HTML by default** — `app.py:359-365` is localhost-only today and becomes
   an exfiltration vector the moment the page is tunneled.

---

## 1.17 — Server reboot

**Advertised cost: 1 per part in flight.**

**Symptom.**

After a host/Docker restart: jobs that were `DOWNLOADING` are stalled with partial files on disk, the
Chromium tab that held the Google session is gone, `GET /` works but nothing progresses. Occasionally
worse — the manager is up but `health` reports `capture_token_set:false, api_token_set:false` on a
deployment that previously had tokens, i.e. the reboot silently created failure mode **1.12**.

**Root cause.**

A reboot destroys exactly what was never designed to be durable:

- **In-flight streams die.** Auth is validated at request *start* only
  (`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:47-51`); once the process is killed that property is gone
  and the interrupted part is a partial on disk.
- **Minted URLs do not survive.** v2 never persists the minted file-host URL, so finishing an interrupted
  part needs a **new mint** — measured at **Δ1** (`measured-facts.md`: "a MINT costs exactly Δ1"), while a
  `Range` request is Δ0. That is why this failure costs **1/part in flight** rather than 0.
- **The browser session is in-memory.** The profile persists at `/config/.chrome-profile` on the LUKS
  volume (survives container re-creation), but whether a restart preserves **Google auth** is explicitly
  **UNVERIFIED** (`infrastructure.md` open questions). `--restore-last-session` was removed in commit
  `7bfb1d7`, so tabs do **not** come back (`docs/webgui/10-deployment-status.md:219-223`).
- **The CDP address changes.** Bare `ws://127.0.0.1:9222` returns **HTTP 404**; the endpoint is
  `ws://127.0.0.1:9222/devtools/browser/<uuid>` and **the UUID changes per browser restart**
  (`measured-facts.md`, `runbooks.md` §6). Hardcoded `TK2_CDP_URL` breaks after reboot.
- **The token trap.** Compose reads `${...}` from the root `.env` / `--env-file`, not a service's
  `env_file:`. Starting without `--env-file webgui/.env` resolves tokens to empty
  (`docs/webgui/13-migration-diskfull.md:90-100`) — benign only while the other side is also empty.
- **Supervision is a contradiction, not a guarantee.** The manager and Chromium are s6-supervised
  (`docs/webgui/12-operations-runbook.md:266-269`), but the runbook records that **`s6-svc -t` and
  `s6-svc -r` did NOT cycle a wedged process; SIGKILL was the only reliable signal**
  (`12-operations-runbook.md:81`, `11-session-changes.md:144`) — while `10-deployment-status.md:125` still
  recommends `s6-svc -r`. **The docs disagree; do not trust `s6-svc` on a wedged service.**

**Detection.**

```bash
ssh -o BatchMode=yes takeout-server 'uptime -p; who -b'
ssh -o BatchMode=yes takeout-server \
  'docker inspect -f "{{.Name}} started={{.State.StartedAt}} restarts={{.RestartCount}}" takeout-webgui takeout-tunnel'
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health'   # tokens must match intent (1.12)
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui ls -la /opt/manager-venv/bin/python'
#   missing / exit 127 -> STORAGE_ROOT fell back to /opt (13-migration-diskfull.md:104-115)
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/version"'
#   prints the CURRENT webSocketDebuggerUrl — the UUID is new; discard any cached TK2_CDP_URL
ssh -o BatchMode=yes takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '/opt/archives/google-takeout/<account>/<export-ts>/state.db' \
  \"SELECT idx, status, bytes_moved, size_on_disk FROM part WHERE status IN ('DOWNLOADING','PENDING') ORDER BY idx;\""
```

**Recovery.**

Do not resume a single part until the stack checks pass — resuming against a broken stack spends the
mint and yields another partial.

```bash
# 0. resolve REPO first — never trust a hardcoded path (04-FAILURE-MODES-AND-RECOVERY.md §0.4)
ssh -o BatchMode=yes takeout-server 'for d in /opt/storage.local_1/projects/takeout-downloader \
    /opt/local_cache_crypt/_projects/takeout-downloader; do
  [ -f "$d/docker-compose.webgui.yml" ] && echo "REPO=$d"; done'

# 1. bring the stack back WITH the env file (not optional — 13-migration-diskfull.md:96-100)
ssh -o BatchMode=yes takeout-server \
  'cd <REPO> && docker compose --env-file webgui/.env -f docker-compose.webgui.yml up -d'

# 2. re-open the work tab (no restore-last-session since 7bfb1d7)
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui curl -s "127.0.0.1:9222/json/new?https://takeout.google.com/manage" >/dev/null'
#    a human must satisfy the ReAuth challenge if the profile came back logged out (infrastructure.md)

# 3. only now resume the interrupted parts — use the manager's resume path so the ledger records
#    the mint; Range itself is free, the re-mint is the 1
```

**Never** `docker compose down`, or `docker compose restart webgui`, while a stream is live
(`04-FAILURE-MODES-AND-RECOVERY.md:139-141`) — same damage as this failure mode, self-inflicted.

**Verification.**

```bash
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui curl -s 127.0.0.1:8080/api/control/health'   # ok:true, tokens as intended
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui ls /opt/manager-venv/bin/python'
ssh -o BatchMode=yes takeout-server \
  'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/list" | grep -c accounts.google.com'
#   want 0 sign-in pages (otherwise you are at the start of failure mode 1.9)
# loudest signal: bytes_moved advancing on the resumed part, with exactly ONE new attempt row per part.
```

**Attempt cost.**

**1 per part in flight** — as advertised, now explained: the partial survives on disk (free), but
completing it requires a **fresh mint** because v2 never persists a minted URL, and a mint is **Δ1**
(`measured-facts.md`). Resumes are Δ0, so a reboot does not make retries expensive; it makes each
interrupted part cost exactly one mint to re-enter.

**What v3 must change.**

1. **Persist the minted URL (with its validity window) in the ledger** so a reboot resumes at zero cost
   instead of re-minting. This is the change that deletes the "1/part" price tag.
2. **Reproducible boot from the repo** — one documented start command that always carries
   `--env-file webgui/.env`, plus a post-boot self-check that fails loudly on empty tokens or a shadowed
   venv.
3. **Discover the CDP endpoint at runtime, every time** — never bake the browser UUID into stored config.
4. **Record enough stream state to resume without a human**: last byte written, the ETag, and remaining
   budget, so recovery is a computation rather than an investigation.
5. **Treat the browser session as renewable, not a 43-day artifact** — re-auth must be an explicit,
   monitored state, not an unverified hope that the profile remembers.
6. **Resolve the `s6-svc` contradiction**: prove `s6-svc -r` cycles a wedged service and fix the docs, or
   standardise on the SIGKILL-respawn path (`12-operations-runbook.md:64-81`) and delete the other
   instruction.
