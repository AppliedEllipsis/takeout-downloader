# 15 — Live Status Panel, /opt/archives Storage Move & start.md Operator Guide

Changes made in the session after the first 3.08 TB download completed and was
verified. Three deliverables: an extension live status panel, a storage-root
reconfiguration so new accounts land under `/opt/archives/google-takeout`, and a
new `start.md` operator guide written for a lesser-LLM operator. All edits were
applied to the bind-mounted source on the server
(`/opt/local_cache_crypt/_projects/takeout-downloader`) and reloaded without a
container rebuild.

Read alongside `12-operations-runbook.md` (daily ops),
`14-resume-cookies-multiaccount.md` (resume + onboarding), and `start.md` (the
top-level guided walkthrough).

---

## 1. Extension live status panel (commit `f0e9614`, v4.1.0)

**Why:** the popup showed capture state but nothing about the actual download.
To see per-part progress the operator had to open the manager UI separately.

**What:**
- `helpers/popup.html` — added a `#statusPanel` section (job header, overall
  progress bar `#jobBar`/`#jobBarFill`, `#jobMeta`, `#jobHeartbeat`, `#jobErr`,
  a `#partsDetails`/`#partsList` per-part breakdown, and a `#jobLog` tail).
- `helpers/popup.js` — added a `sp` element-ref object, `fmtBytes()`,
  `statusColor()`, `renderStatusPanel(resp)`, and a `pollStatus()` loop that
  polls every 2s via a new background message.
- `helpers/background.js` — added a `getManagerJobs` message handler that
  fetches `/api/jobs`, then the most-recent job's `/api/jobs/{id}` snapshot and
  `/api/jobs/{id}/log?lines=40`, all with the `X-Capture-Token` header. Returns
  `{ ok, jobs, detail, log, at }`.

**Panel shows:** overall bytes-done/total + percent + speed; per-part rows with
status icon (✓ done / ▼ active / 🔑 auth / ✗ error / · pending), index,
percent, bytes, and speed; a heartbeat ("last update Ns ago" with a stall
warning when >30s while downloading); sticky errors; and a live log tail.

**Reads these real job-snapshot fields:** `status`, `totals` (`parts_total`,
`parts_done`, `bytes_total`, `bytes_done`, `speed_bps`), `last_error`,
`updated_at`, and `parts[]` (each: `index`, `filename`, `status`, `done`,
`size`, `speed`).

**Gotcha hit while building:** the patch script applied the panel JS through a
double-quoted shell wrapper, so the `$('id')` element lookups were eaten by
shell `$()` expansion and landed empty. Fixed by rewriting the `sp` block with
`document.getElementById(...)` via a single-quoted heredoc (no `$` expansion).
Lesson: never push JS containing `$(...)` through a double-quoted shell string.

---

## 2. Storage move to /opt/archives/google-takeout (commit `c3475ad`)

**Why:** new accounts should land under `/opt/archives/google-takeout/{account}`
instead of the original `/opt/storage.jfs002/google-takeout`.

**Path formula (unchanged code):**
`output_dir = STORAGE_ROOT / TAKEOUT_SUBDIR / {account_label} / {export_ts}`
where `{account_label}` is auto-derived (email → scraped label → `gaia-<user>`
→ `unknown-account`) and `{export_ts}` comes from the part filename timestamp.

**What changed (config only, no code):**
- root `.env`: `STORAGE_ROOT=/opt/archives`, `TAKEOUT_SUBDIR=google-takeout`.
- `docker-compose.webgui.yml`: added `TAKEOUT_SUBDIR` env passthrough
  (`${TAKEOUT_SUBDIR:-google-takeout}`) and a SECOND read-only bind mount for
  the legacy tree so it stays visible in-container:
  `/opt/storage.jfs002:/opt/storage.jfs002:ro` (rslave, recursive).

**Critical gotcha — venv shadowing:** the first attempt set `STORAGE_ROOT=/opt`
(with `TAKEOUT_SUBDIR=archives/google-takeout`). Because compose mounts
`${STORAGE_ROOT}:${STORAGE_ROOT}`, that bind-mounted host `/opt` over the
container's baked-in `/opt/manager-venv`, the venv vanished inside the
container, and the manager died with **exit 127** (`/opt/manager-venv/bin/python:
not found`). This is the SAME class of failure called out in doc 13 (the
`--env-file` gotcha that lets `STORAGE_ROOT` fall back to `/opt`).
**Fix:** never let the storage mount cover `/opt`. Use a narrower
`STORAGE_ROOT=/opt/archives` and add a separate mount for any other tree you
still need visible.

**Verified after restart:** manager up (s6 svc `up`), venv present at
`/opt/manager-venv/bin/python`, `get_config().takeout_root` ==
`/opt/archives/google-takeout`, `/api/jobs` returns `{"jobs":[]}` (clean slate),
both storage trees visible in-container. The manager runs as **root** inside the
container, so `/opt/archives/google-takeout` at `root:root 755` is writable
as-is (no chown needed).

---

## 3. start.md operator guide (commit `c3475ad`)

**Why:** the workflow needs to be runnable by a lesser LLM (e.g. deepseek-flash)
that is watching/guiding. It needs explicit, verified commands and the gotchas
spelled out, not assumed.

**What:** new top-level `start.md` covering: how to reach the server (SSH alias
`takeout-server`, container names `takeout-webgui`/`takeout-tunnel`, ports
3000 KasmVNC / 8080 manager / 9222 CDP, the `*.trycloudflare.com` portal), where
everything lives, the full new-account onboarding flow, the cookie-cadence
gotcha, the live-cookie-jar manual recovery procedure, and the JuiceFS
verify-cost warning.

**Verified the commands against real code before committing** — caught and fixed
one wrong endpoint: pause/resume/cancel are `POST /api/control/pause` (etc.)
with `{"job_id":"..."}` in the request BODY and the `X-Api-Token` header, NOT a
path-based `/api/jobs/<id>/pause` route. Also confirmed `python3` exists in the
container at `/lsiopy/bin/python3` (Python 3.13) for the CDP cookie-jar script.

---

## Git state at end of session

All synced at `c3475ad` on `feat/internal-downloader`:
- origin (GitHub) = `c3475ad`
- server repo = `c3475ad`
- local Windows copy = behind/diverged (pending reset to origin)

Commits this session: `f0e9614` (status panel), `3559cf9` (doc 14),
`c3475ad` (storage move + start.md). Doc 15 (this file) documents them.
