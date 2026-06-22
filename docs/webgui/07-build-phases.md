# 07 — Build Phases

Ordered phases with acceptance gates. Each phase is independently shippable and
testable. Do not start a phase until the previous gate passes. This is the
master checklist that drives the actual build (and guides weaker models).

> Status legend: ⬜ not started · 🔶 in progress · ✅ done

## Phase 0 — Prep & reference (no server changes)

⬜ Confirm engine seam is feasible: read `download_exports()` and locate the
   `_set()` and `_AuthChallenge` points where `progress_cb`/`auth_cb` hook in.
⬜ Confirm server paths: storage root (`/opt/storage.*/google-takeout`), repo
   clone (`/opt/storage.local_1/projects/takeout-downloader`).
⬜ Confirm webtop image + GPU/audio support on the target server.

**Gate:** a written note in this folder confirming the two hook points exist and
the storage/clone paths are correct on the server.

## Phase 1 — Engine callback seam (the only engine change) ✅

✅ Add optional `progress_cb` and `auth_cb` params to `download_exports()`.
✅ Call `progress_cb(part)` from `_set()`; call `auth_cb(auth_info)` on challenge.
✅ Defaults `None` → identical CLI/TUI behavior.
✅ Unit test: run a tiny fake export set, assert callbacks fire and that with
   `progress_cb=None` output is byte-identical to today.

**Gate:** existing CLI download still works unchanged; callbacks observed in test.

**DONE 2026-06-22.** Implemented in `takeout_dl.py`:
- `download_exports(..., progress_cb=None, auth_cb=None)` — both optional.
- `progress_cb` called from `_set()` with a `PartProgress` copy, never under the
  lock (snapshot taken inside lock, callback fired outside). Exceptions in the
  callback are swallowed (logged at debug) so a bad observer can't break a download.
- `auth_cb` called exactly once on the first auth challenge (guarded by the
  existing cross-thread `auth.set()`), with a copy of `{"url", "body"}`.
- Verified: with callbacks `None`, downloads produce byte-identical valid zips
  (3/3 test parts). With callbacks set, 9 progress events observed across
  active→done. auth_cb fires once on an HTML-login challenge, no fake zip is
  written, and `AuthError` still propagates. `py_compile` clean.

## Phase 2 — Manager service core (localhost only) ✅

✅ `manager/` package: `app.py`, `jobs.py`, `engine_bridge.py` (+ `config.py`,
   `derive.py`, `manifest.py`, `orchestrator.py`, `__main__.py`).
✅ `POST /api/payload` → validate, derive dated dir, create job, run engine in a
   worker thread.
✅ `GET /api/jobs`, `GET /api/jobs/{id}`, SSE `/events`, `/log`.
✅ Job state persisted to `<outdir>/.manager_state.json`; `recover()` on restart.
✅ `validate_output_dir` enforced on every derived path.
✅ Dated output dir `<root>/google-takeout/<account>/<export-ts>/` + per-run
   `manifest.json` (sizes + per-file times + zip_valid).

**Gate:** POST a real captured payload (from the existing v3 extension, manual)
→ download runs, progress visible via `GET /api/jobs/{id}`, resumes after a
manager restart.

**DONE 2026-06-22.** e2e test (`manager/tests/test_e2e_phase2.py`) drives a fake
Takeout server end to end: payload POST → dated dir
`braincreation/2026-06-16-04-01-04` → 3 valid zips → manifest with sizes+times.
Runs in an isolated `.venv-manager` (fastapi 0.118 + starlette 0.48, pinned in
`manager/requirements.txt`) because the global anaconda env had an incompatible
starlette 1.0. See `08-decisions-log.md`.

## Phase 3 — Progress web UI

✅ Static UI at `/`: jobs list, per-job parts table, live bars over SSE.
✅ Cookie-age indicator + `needs_cookie` banner.
✅ Buttons wired to control endpoints (pause/resume/recapture).

**Gate:** open `http://127.0.0.1:8080` in the hosted Chrome; watch a live
download; pause/resume works.

**DONE 2026-06-22.** Single-page UI in `manager/web/` (index.html + app.css +
app.js), served at `/` and `/ui/`. Jobs list (left), per-job parts table with
live per-part progress bars driven by the SSE `/api/jobs/{id}/events` stream,
overall totals header, cookie-age indicator, and a `needs_cookie` banner. Control
buttons (pause/resume/cancel/recapture) call the Phase 4 endpoints. Verified the
UI assets + endpoints serve via TestClient.

## Phase 4 — Control plane + diagnose

✅ `/api/control/*` (start/pause/resume/cancel/recapture) behind
   `MANAGER_API_TOKEN`.
✅ `/api/control/health` and `/api/control/diagnose` with the reason codes from
   `04-decision-trees.md`.
✅ Capture token (`MANAGER_CAPTURE_TOKEN`) separate from the control token.

**Gate:** each reason code can be provoked in a test and returns the documented
recommended action.

**DONE 2026-06-22.** Control endpoints in `manager/app.py` gated by
`MANAGER_API_TOKEN` (X-Api-Token header); `/api/payload` independently gated by
`MANAGER_CAPTURE_TOKEN` (X-Capture-Token). `manager/diagnose.py` maps live job
state to the exact reason-code set (cookie_expired, auth_loop, disk_full,
network_stall, zip_validation_failed, browser_down, manager_down, ok) with
section refs + recommended actions. recapture_count escalates cookie_expired ->
auth_loop after 2 cycles. `/api/control/health` reports disk free + cookie age.
Verified in `manager/tests/test_phase4_control.py`.

## Phase 5 — Extension v4 ✅

✅ Add `host_permissions` for `127.0.0.1:8080`, settings (managerUrl, tokens,
   toggles).
✅ Auto-POST on capture (clipboard still fires as fallback).
✅ `forceCapture` / `getState` runtime messages.
✅ Auto-re-capture routine driven by `needs_cookie`.

**Gate:** start a download in Chromium → payload auto-POSTs → manager job starts
with no manual paste. Force a cookie expiry → extension re-captures → job resumes.

**DONE 2026-06-22.** Extension bumped to v4.0.0 (manifest: +`scripting`,
+`alarms`, +`http://127.0.0.1:8080/*` host permission). Capture listener
UNCHANGED (the hard-won final-host logic is untouched). Added, all additive:
`maybeAutoPost()` POSTs every capture to `/api/payload` with `X-Capture-Token`
(clipboard still fires as fallback on any failure); `_meta` carries
email/user/authuser/archiveId so the manager derives `<account>/<export-ts>/`.
Content script scrapes the account email from the page DOM (`reportAccountMeta`)
and handles `recaptureDownload` (re-clicks Download, never types credentials).
An `alarms` poll hits `/api/control/recapture-pending` every minute and triggers
a re-capture when a job is in `needs_cookie`. New runtime messages
`getState`/`setManagerConfig`/`forceCapture`/`setAccountMeta`. Popup gained a
manager-status line, a Send-now button, an Open-manager link, and an
auto-send toggle. Manager-side contract verified in
`manager/tests/test_phase5_v4contract.py` (email→label, recapture-pending token
gating + pending flag). JS validated with `node --check` (all three files).

## Phase 6 — webtop container + persistence ✅

✅ Compose service from `linuxserver/webtop` (KasmVNC, audio, `/config` volume).
✅ Chromium launched with `--remote-debugging-port=9222` (localhost).
✅ Manager runs alongside (same container init or sibling), storage mounted.
✅ Seed profile: extension loaded, bookmarks, pinned action, default tabs.

**Gate:** open the KasmVNC portal locally (before tunnel), log into Google,
confirm profile + extension + bookmarks persist across a container restart, and
that audio reaches the browser tab.

**DONE 2026-06-22 (infra authored; runtime gate is server-side).** Files:
- `webgui/Dockerfile.webtop` — `lscr.io/linuxserver/webtop:ubuntu-kde` base +
  chromium + a `/opt/manager-venv` Python venv (manager deps installed from
  `manager/requirements.txt`).
- `webgui/init_custom.sh` (`/custom-cont-init.d`) — writes the chromium launcher
  (CDP on 127.0.0.1:9222, `--load-extension=/work/helpers`, opens Takeout +
  manager tabs), an autostart `.desktop`, seeds bookmarks + the extension's
  `managerUrl`/`captureToken` into the profile on first boot.
- `webgui/manager-service.sh` (`/custom-services.d`) — s6-supervised uvicorn
  running `manager.app:app` from the bind-mounted `/work` (auto-restart =
  crash recovery).
- `docker-compose.webgui.yml` — webgui + cloudflared services. **8080 and 9222
  bind 127.0.0.1 only** (verified); `/opt` rbind+rslave for JuiceFS submounts;
  `/config` persistent profile; shm 2gb; `/dev/dri` passthrough.
- `webgui/profile-seed/` (Bookmarks, managed-policy.json, README) + `.env.example`.
- The runtime gate (log into Google, confirm persistence + audio) can only be
  exercised on the server with Docker; YAML/JSON/shell all syntax-validated locally.

## Phase 7 — Cloudflare tunnel (portal only) ✅

✅ `cloudflared` tunnel exposing **only** KasmVNC `:3000`.
✅ Cloudflare Access policy (email/SSO) in front of the portal hostname.
✅ Verify manager `:8080` and CDP `:9222` are NOT routed by the tunnel.

**DONE 2026-06-22 (config + helpers authored; live gate is server-side).**
- `webgui/cloudflared/config.yml` — one ingress rule mapping the portal
  hostname to `http://127.0.0.1:3000`, then a catch-all `http_status:404`. No
  ingress for 8080/9222, so they are unroutable from the edge by construction.
- `webgui/cloudflared/setup-tunnel.sh` — creates the named tunnel, writes the
  DNS route, prints the Access-policy steps (allow only your email, short
  session). Idempotent-ish: warns if the tunnel already exists.
- `webgui/cloudflared/verify-exposure.sh` — the negative test: confirms the
  portal answers through the public hostname AND that 8080/9222 return
  connection-refused/timeout from the edge (only reachable via SSH forward).
- Live gate (real CF hostname + Access prompt) runs on the server; both scripts
  syntax-validated locally.

**Gate:** portal reachable at the CF hostname only after Access auth; `:8080`
and `:9222` reachable only via SSH forward, never via the tunnel.

## Phase 8 — Telegram ✅

✅ `manager/notify.py` + chat-id capture helper.
✅ State-change events, then rate-limited progress.
✅ Command long-poll (`/status`, `/health`, then control commands + confirms).

**Gate:** a real download posts started/progress/complete to your channel;
`/status` and `/recapture` work from Telegram.

**DONE 2026-06-22.** `manager/notify.py` (stdlib urllib only — no new deps):
`TelegramNotifier` (no-op unless token+chat_id+enabled) with `send()` and
`send_event()` formatting the started/milestone/needs_cookie/login_needed/error/
complete/resumed messages from the doc table; milestones rate-limited to
`TELEGRAM_PROGRESS_INTERVAL` and mutable via `/mute`. `CommandPoller` long-polls
`getUpdates`, rejects any chat but `TELEGRAM_CHAT_ID` (`_chat_ok`), and dispatches
`/status /jobs /health /diagnose /pause /resume /recapture /recipes /run /mute
/unmute` 1:1 onto the orchestrator control methods; destructive commands
(`/recapture`, `/run`) require a `/yes` confirm. `--capture-chat-id` CLI helper.
Wired into `app.py` startup as an orchestrator event subscriber + background
poller; error events are enriched with the diagnose reason code. Verified offline
in `manager/tests/test_phase8_telegram.py` (API call monkeypatched). The live
gate (real channel) is server-side.

## Phase 9 — Repeat-without-LLM ✅

✅ Record a completed run as a recipe (`<root>/google-takeout/.recipes/<name>.json`).
✅ `POST /api/recipes/{name}/run` replays: re-open Takeout, trigger export +
   capture via the extension automation, feed engine — no model.
✅ Optional `schedule {cron}`.

**Gate:** with no LLM/agent attached, trigger `/run <name>` (Telegram or API)
and watch a fresh export download end to end.

**DONE 2026-06-22.** `manager/recipes.py`: `RecipeStore` persists one JSON per
recipe under `<takeout_root>/.recipes/`. A recipe is auto-recorded on job
`complete` (account label, parallelism, part/byte totals, identity meta). `run()`
invokes a pluggable replay trigger; the production `CdpTrigger` drives the hosted
Chromium over CDP (`:9222`) to reopen Takeout so the extension auto-captures a
fresh export — no LLM in the loop. HTTP: `GET /api/recipes`,
`POST /api/recipes/{name}/run`, `/schedule`, `DELETE`. Telegram `/recipes` +
`/run <name>` front the same store. Verified in
`manager/tests/test_phase9_recipes.py` (record → persist → reload → replay via a
fake trigger; missing-recipe and no-trigger paths are safe no-ops). The live
CDP replay can only be exercised on the server with the browser running; the
trigger seam keeps the store logic testable offline.

## Phase 10 — Hardening & docs

⬜ Tokens in `.env` (mode 600), not in image/git.
⬜ Security flag: confirm no unauthenticated control surface is exposed.
⬜ Update repo `README`/`docs` to point at this folder.
⬜ Runbook smoke test: follow `04-decision-trees.md` for each failure path.

**Gate:** a fresh operator (or weaker model) can deploy and run a full workflow
using only `docs/webgui/`.

## Dependency graph

```
P0 ─► P1 ─► P2 ─► P3 ─► P4 ─► P5 ─┐
                 └─────────────────┼─► P6 ─► P7
                                   │         └─► P8
                                   └─► P9 (needs P5 + P6)
                                            └─► P10
```

Phases 1-4 are pure local dev (no server). Phases 6-7 are server/infra. Phase 5
(extension) can proceed in parallel with 6 once the manager API (P2/P4) is
stable.
