# 08 — Build Decisions Log

Running record of decisions made *during* implementation (not the up-front
design Q&A — that's in `README.md`). Newest at the bottom of each phase. This is
the "why" companion to the code, so a later model/operator doesn't re-litigate
settled choices.

---

## Phase 1 — Engine callback seam (2026-06-22)

- **Additive-only refactor.** `download_exports()` gained `progress_cb=None` and
  `auth_cb=None`. With both `None` the function is byte-identical to before
  (verified: 3/3 fake parts download to valid zips, no behavior change). This
  keeps the CLI/TUI untouched while giving the manager structured progress.
- **Snapshot inside the lock, callback outside it.** `progress_cb` receives a
  `PartProgress` *copy* taken while holding the lock, but is *invoked* after
  releasing it. Prevents an observer from deadlocking or slowing the download
  pool.
- **Callback exceptions are swallowed** (logged at debug). A buggy observer must
  never break a download.
- **`auth_cb` fires exactly once**, guarded by the existing cross-thread
  `auth.set()` event, so only the first worker to hit a challenge notifies.

---

## Phase 2 — Manager service core (2026-06-22)

- **In-process worker thread, not subprocess per job.** The engine is a clean
  importable module, so the manager imports it and runs `download_exports` in a
  daemon thread (`manager/engine_bridge.py`). Simpler progress (direct callback)
  and one process to supervise. Subprocess isolation can come later if needed;
  noted as an open question in `02-manager-service.md`.
- **Isolated venv `.venv-manager/`.** The global anaconda env had
  `starlette 1.0.0`, which is incompatible with `fastapi 0.118` (needs
  `starlette <0.49`) — `FastAPI()` construction raised
  `Router.__init__() got an unexpected keyword argument 'on_startup'`. Pinned a
  working set in `manager/requirements.txt` (`starlette==0.48.0`). The venv is
  gitignored; deployment uses the requirements file. This also makes the
  manager's deps reproducible and decoupled from whatever the host Python has.
- **Derivation values live on `job.meta`.** `account_label`, `export_ts`,
  `export_raw` are merged into `job.meta` at job creation so the manifest (which
  reads identity from `job.meta`) records them. Single source of truth for the
  dated-folder identity.
- **Output dir derived once, at payload intake**, then frozen on the job. A
  re-capture/resume of the same export resolves to the same
  `<account>/<export-ts>/` dir and resumes partials; a genuinely new export
  (new timestamp) gets a new dir. Every derived path still passes through the
  engine's `validate_output_dir` allowlist — the manager never writes outside
  allowed roots.
- **Resume vs new-job routing** keys on the output dir: if a job for that exact
  dir is alive and waiting (`needs_cookie`/`downloading`/`paused`), a fresh
  payload is treated as a cookie refresh (`set_payload` wakes the runner);
  otherwise a new job starts.
- **SSE (not WebSocket) for progress.** One-way progress only; SSE is simpler
  and needs no extra deps. Matches the doc's call.
- **Two-token model present from the start.** `MANAGER_CAPTURE_TOKEN` (narrow:
  POST /api/payload) and `MANAGER_API_TOKEN` (control plane). In Phase 2 the
  control plane is read-only; tokens are enforced in Phase 4. Empty token =
  open surface (dev only), logged as a warning.

---

## Phase 8 — Telegram (2026-06-22)

- **stdlib only.** `notify.py` uses `urllib` for the Bot API (sendMessage,
  getUpdates) so it adds no dependency. The notifier is a no-op when token/chat
  id are unset, so the manager runs fully without Telegram.
- **Job-snapshot-in, text-out.** `send_event(kind, job_dict)` formats from a
  plain dict, so it's testable offline (no engine import). Verified by
  monkeypatching `_api_call`.
- **Confirm flow fix.** `/run` and `/recapture` are destructive, so they arm a
  pending-confirm and require `/yes`. The first implementation re-armed forever
  because `/yes` re-dispatched the command, which then re-armed. Fixed with a
  `_confirmed` marker that the re-dispatch sets and clears in a `finally`.
- **Chat-scoped authz.** `_chat_ok()` accepts only the configured chat id;
  refactored to take a raw id OR an update/message dict so the poll loop and the
  test share one path.

---

## Phase 9 — Repeat-without-LLM (2026-06-22)

- **CDP trigger injected, not hardcoded.** `RecipeStore` takes a `trigger`
  callable. Production uses `CdpTrigger` (drives the hosted Chromium over
  DevTools Protocol to re-open Takeout). Tests pass a fake trigger, so replay
  logic is verified without a browser. With no trigger, `run()` is a safe no-op.
- **Recipes auto-recorded on completion.** The event sink records a recipe from
  the completed job snapshot, so a successful first run becomes replayable with
  no extra step. Identity meta is carried so a replay re-derives the dated dir.

---

## Phase 10 — Hardening & docs (2026-06-22)

- **Manifest race fix (caught by the full re-run).** `record_part()` is called
  per-part from the progress callback; under parallel completion the
  read-modify-write of the files dict lost an entry intermittently (Phase 2 test
  flapped: 2 of 3 files in the manifest). Fixed by making `finalize()` the
  authoritative reconciler: it rebuilds the files list from the job's `parts`
  dict (the source of truth, which was always correct) rather than trusting the
  incremental writes. Re-ran Phase 2 ×3 + full suite: stable. Lesson: incremental
  side-writes from a parallel callback need a final reconcile pass.
- **Security self-check at startup.** If `MANAGER_HOST` is overridden to a
  non-localhost bind without a capture token, the manager logs a loud SECURITY
  warning. By design it binds 127.0.0.1 and is reached via SSH forward; this is
  the backstop if someone changes that.
- **Secrets hygiene.** `.gitignore` covers `.env`/`.env.*`; only `.env.example`
  (placeholders) is tracked. Tokens are generated with `openssl rand -hex 32`
  and live in the server `.env` (mode 600), never in the image or git.

---

## Post-build review fixes (2026-06-22)

Three parallel review subagents (same model) audited the manager Python, the
extension v4 JS, and the deployment infra. Findings triaged and addressed:

**Fixed (IMPORTANT):**
- **Lock held across a blocking network call.** `accept_payload`'s resume path
  called `_emit()` (→ Telegram `urllib` POST, 10s timeout) while holding the
  global orchestrator lock, stalling every other thread. Restructured so all
  mutation happens under the lock, then `_emit`/`start` run after releasing it.
- **Zombie jobs after restart.** `recover()` registered jobs in `_jobs` but
  never built a `JobRunner`. Pause/resume/recapture 404'd, and a fresh payload
  for a recovered dir fell through to create a DUPLICATE job. Added `_make_runner()`
  (single source of runner wiring) and call it in both `recover()` and the
  resume branch. Regression test: `manager/tests/test_review_fixes.py`.
- **Extension read identity from the wrong source.** `buildManagerPayload` took
  `user`/`authuser`/`archiveId` from `accountMeta` (scraped off the page URL,
  which lacks `user=`). Now parsed from `capture.url` (the download URL, which
  carries the real `user`/`authuser`/`j=` params), with scraped email as the
  friendly-label supplement. This is what makes the `gaia-<id>` folder fallback
  actually work.
- **Chromium install was a snap shim.** On the Ubuntu webtop base both
  `chromium` and `chromium-browser` are snap transitional packages that don't
  run in-container. Switched `Dockerfile.webtop` to the **Debian** webtop base
  (`webtop:debian-kde`) where `chromium` is a real `.deb`.

**Fixed (MINOR):** `Job.set_status` `assert`→explicit `ValueError` (survives
`python -O`); `forceCapture` now persists `lastPostStatus`; pinned a
deterministic extension `key` in the manifest so the ID is stable
(`dgbbpdjpfeeaiheekoclkkkbipkikejl`) and keyed the managed-storage policy on the
real ID instead of a name; guarded the `.desktop`/launcher writes so manual
edits survive a reboot.

**Decided, documented (not changed):**
- **Read routes are intentionally open** (`/api/jobs`, `/events`, `/log`,
  `GET /api/recipes`). The manager binds 127.0.0.1 only and is reached via SSH
  forward; snapshots carry account email/gaia-id/paths but **never the cookie**
  (verified). Write/control routes are all token-gated. If the threat model
  tightens, gate reads behind the API token too — the helper `_check_api_token`
  is already there.

---

## Build verification (2026-06-23)

Actually built and ran the Docker images locally (not just syntax-checked):

- **`takeout-dl`** (lean engine image, `Dockerfile.takeout_dl`) — builds clean.
- **`takeout-webgui`** (`webgui/Dockerfile.webtop`, Debian webtop base) — builds
  clean. Verified the review fix in-image:
  - `chromium` resolves to `/usr/bin/chromium`, a **real binary** (Chromium
    149, Debian trixie), `--version` executes, and there is **no snap** anywhere
    — confirming the Ubuntu→Debian base switch fixed the snap-shim risk the
    deploy review flagged.
  - Manager venv at `/opt/manager-venv` imports `manager.app` (22 routes) and
    serves `GET /api/control/health` (200) bound to 127.0.0.1 inside the
    container.
  - `init_custom.sh` + `manager-service.sh` land in the s6 dirs
    (`/custom-cont-init.d`, `/custom-services.d`), executable, pass `bash -n`.
- **Not yet exercised** (needs the server / a real Google session): KasmVNC
  portal + audio, GPU passthrough, the full capture→download→resume cycle, and
  the Cloudflare tunnel. These are the Phase 6/7 runtime gates in
  `09-smoke-test.md`. SSH to the server is currently rejected
  (`Permission denied (publickey,password)`), so server-side steps are blocked.

---

## Deployment to server (2026-06-23)

Deployed to `ellipsis@188.245.169.166` (Hetzner, Ubuntu 24.04, KDE-Plasma webtop).
Key-based SSH installed (dedicated `takeout_deploy` ed25519 key); password auth
left enabled per request. All findings below are from the live deploy.

- **Disk crisis fixed first.** Root fs was 100% full (508 MB free / 75 GB). Cause
  was NOT the project: `/var/log/juicefs.log` had grown to 22 GB unbounded since
  May. Truncated in place (`: >`, keeps the inode so JuiceFS keeps writing, no
  restart) → 22 GB reclaimed. Installed `/etc/logrotate.d/juicefs`
  (daily, maxsize 500M, rotate 7, compress, **copytruncate** since JuiceFS holds
  the fd open, **su root root** because /var/log perms are non-standard).
- **Dedicated env file.** The root `.env` belongs to another project (a CLI
  takeout export). Pointed compose `env_file:` at `webgui/.env` (mode 600,
  gitignored) and run compose with `--env-file webgui/.env` so the
  `environment:` `${VAR}` interpolation also reads it. STORAGE_ROOT set to
  `/opt/storage.jfs002` so archives land on the 1 PB JuiceFS mount, NOT the
  75 GB root disk.
- **s6 `with-contenv` shebang is mandatory.** Custom services/init scripts run
  with a clean env under s6-overlay; `#!/bin/bash` meant the manager saw no
  STORAGE_ROOT/tokens (health showed `/opt` + tokens unset). Fixed both
  `manager-service.sh` and `init_custom.sh` to `#!/usr/bin/with-contenv bash`.
- **Chromium launched via an s6 service, not XDG autostart.** This webtop runs
  KDE Plasma; `~/.config/autostart/*.desktop` is not honored reliably on
  container boot. Added `webgui/chromium-service.sh` (waits for X display :1,
  clears stale Singleton* locks, launches as user `abc` via `s6-setuidgid`).
  s6 restarts it on exit (closed window/crash self-heals). Removed the redundant
  XDG autostart block from init.
- **Stale SingletonLock on recreate.** Chromium writes `SingletonLock ->
  <host>-<pid>`; after `docker compose up --force-recreate` that pid is gone but
  the symlink remains and blocks every new launch. The chromium service removes
  the stale locks when no chromium is running — verified self-healing across
  repeated force-recreates.

### Verified live (server, no manual intervention after recreate)
- Both images build on the server. `chromium` is a real Debian binary
  (Chrome 149, trixie), not a snap shim.
- Manager: health 200, `storage_root=/opt/storage.jfs002/google-takeout`,
  1.12 PB free, both tokens set. Web UI 200.
- Chromium: 39 procs, CDP 200 on 127.0.0.1:9222, both tabs open
  (takeout.google.com + manager). Extension loaded with the pinned ID
  `dgbbpdjpfeeaiheekoclkkkbipkikejl` (confirms manifest `key` + managed policy).
- KasmVNC portal: 200 on 3000. Host ports 3000/8080/9222 all bound 127.0.0.1.

### Still requires a human (cannot be automated)
- **Google login** in the portal (one-time, interactive — 2FA).
- **Cloudflare tunnel**: needs a real tunnel token/hostname + Access policy
  (`CLOUDFLARE_TUNNEL_TOKEN` empty, cloudflared not started).
- **Telegram**: `TELEGRAM_ENABLED=false` until a chat_id is captured.
- The live capture→download→resume cycle (needs the Google login first).

## Live deployment + zero-config tunnel (2026-06-23)

The system is deployed and running on the server (188.245.169.166), reached via
a zero-config Cloudflare quick tunnel.

### Portal auth (TEMPORARY credential — CHANGE THIS)
- The KasmVNC portal is gated by nginx basic auth (linuxserver webtop
  `CUSTOM_USER` + `PASSWORD`).
- **Current credentials: `takeout` / `passw0rd` — TEMPORARY, set on request.**
  Change before any real use: edit `PASSWORD` in `webgui/.env` on the server and
  `docker compose -f docker-compose.webgui.yml --env-file webgui/.env up -d --force-recreate webgui`.
- Stored as an apr1 hash in `/etc/nginx/.htpasswd` inside the container; the
  plaintext lives only in `webgui/.env` (mode 600, gitignored).

### Tunnel: zero-config quick tunnel (no Cloudflare account)
- `cloudflared tunnel --url http://127.0.0.1:3000` (compose `cloudflared`
  service). Emits a random `*.trycloudflare.com` hostname on each start — it is
  NOT stable across restarts. Re-read it from `docker logs takeout-tunnel`.
- There is NO Cloudflare Access layer in this mode (that needs a CF account +
  named tunnel). The basic-auth password is therefore the ONLY gate — which is
  why a password is mandatory before the tunnel is opened.

### Exposure verification (all PASS, tested via the public URL)
- portal, no creds -> 401; with creds -> 200.
- manager API (`/api/control/health`) via tunnel -> 401 (nginx), never manager JSON.
- manager UI path via tunnel -> 404.
- CDP (`/json/version`) via tunnel -> 404 (not proxied).
- Host bindings remain `127.0.0.1:3000/8080/9222` only — manager + CDP never
  bind 0.0.0.0. The tunnel only ever sees the auth-gated portal on 3000.

### Telegram: deferred
- Skipped for now. `TELEGRAM_ENABLED=false`; manager runs fully without it.
- Token resolves from `~/.pi/agent/auth.json` (bot @Pi_Lip_bot) when re-enabled;
  `python -m manager.notify --capture-chat-id` needs the bot to have received a
  message first (DM it, or add it as a channel admin and post once).


### Post-Phase-10 live-session changes (see 11-session-changes.md)
- **Manual paste box** added to the manager UI so a job can be started without
  the extension; the page self-injects the capture token (localhost-only).
- **Account label**: derivation now falls back to URL `user=` and a DOM-scraped
  display name (e.g. `braincreation`); precedence is override → email → scraped
  label → `gaia-<user>` → `unknown-account`.
- **Job deletion**: `DELETE /api/jobs/{id}` + Remove button (keeps files on
  disk, drops the record). Fixed a pre-existing bug where UI control buttons
  sent no api token and silently 401'd.
- **Cache-busting**: HTML served `no-store`; assets get `?v=<manager-start>` so
  a plain reload picks up new JS/CSS after a restart.
- **Extension auto-reload over CDP**: `webgui/reload-extension.sh` +
  `cdp_reload_ext.py` reload the extension by ID via `chrome.developerPrivate`
  and report real manifest errors. CDP (:9222) stays loopback-only, never tunneled.
- **manifest.json**: removed non-standard `_comment_key` (MV3 load warning).
- **Ops**: reload the manager via SIGKILL + s6 respawn, NEVER
  `docker compose restart webgui` (shares the tunnel's netns → CF 1033 + new
  quick-tunnel URL). `config/` (KasmVNC profile = live Google session) is now
  gitignored.

### Operations runbook
- **Recovery & daily procedures:** documented in `12-operations-runbook.md`.
  Covers start/stop, manager reload without killing the tunnel, extension
  reload over CDP, Chrome stability workarounds, cookie-refresh resume flow,
  progress monitoring, and zip verification.
- **Chrome stability:** on this VirtIO-GPU container, Chromium ~149 crashes on
  heavy pages after prolonged uptime. Mitigations: `--disable-gpu` in the
  launcher, container restart, profile clearing. Still an open issue — may
  need a Chromium version upgrade or the profile to be seeded fresh.

## Disk-full root cause + repo migration (session 13)

Chrome SIGKILL/Trace-trap crashes root-caused to /dev/sda1 at 100%. Unbounded
selkies container logs were a major contributor — added json-file log rotation
(10m x3) to both services. Repo relocated off the full disk to
/mnt/local_cache_crypt/_projects/takeout-downloader (300G LUKS local) via fresh
clone (old .git corrupted by disk-full). Two compose gotchas: (1) MUST start with
--env-file webgui/.env or ${STORAGE_ROOT} falls back to /opt and shadows the
manager venv (exit 127) + blanks tokens; (2) STORAGE_ROOT now also in root .env.
See 13-migration-diskfull.md.

### Resume cookie/livelock + multi-account (see 14-resume-cookies-multiaccount.md)
- Takeout download cookie dies in ~1-2 min when IDLE; the manager re-discovery
  sweep (63 one-byte probes over JuiceFS) burned it before downloads started ->
  livelock. Fixed: cache exports, skip re-discovery on resume (engine_bridge).
- Stored lastCapture cookie expires fast; the LIVE Chrome cookie jar (CDP
  Storage.getCookies, ~3.5KB, 27 google cookies) is the freshest source and
  returns HTTP 206 resumable where lastCapture returns 302->login.
- Resume now matches on stable archive_id (j= URL param), not label-derived
  output dir, so a label change (gaia-NNN -> braincreation) cannot orphan a job.
- Filename scheme differs between payload-exports run (-part-NN) and sweep run
  (-13-NN); reconcile by index+size before resuming or it re-downloads all.
- Recovery escape hatch: pull live jar via CDP, fire 4 parallel curl -C - Range
  resumes direct to disk, bypassing the manager pre-pass entirely.

## Live status panel + /opt/archives relocation + start.md (session 15)

Extension v4.1 live status panel: popup polls background getManagerJobs (which
fetches /api/jobs + job detail + log tail with the capture token), renders
per-part progress, heartbeat/stall warning, errors, and a log tail. See
15-status-panel-archives-startmd.md.

New-account save path moved to /opt/archives/google-takeout/{account}/{export-ts}
via STORAGE_ROOT=/opt/archives + TAKEOUT_SUBDIR=google-takeout. GOTCHA: the
originally-planned STORAGE_ROOT=/opt would bind-mount host /opt over the image
baked-in /opt/manager-venv and kill the manager (exit 127) — same venv-shadow
trap as doc 13. Fix: narrower STORAGE_ROOT=/opt/archives + a SECOND read-only
bind mount for the legacy /opt/storage.jfs002 tree so both stay visible.
Manager runs as root in-container, so /opt/archives (root:root 755) is writable
as-is.

start.md: operator guide for a lesser LLM (server SSH alias, container names,
ports, tunnel, full new-account onboarding, cookie-cadence + live-jar recovery,
JuiceFS verify-cost warning). Verified commands against real code before commit;
fixed a wrong endpoint — pause is POST /api/control/pause with job_id in the
BODY, not a path route.
