# 02 — Manager Service

The manager is the brain of the new system. It is a small FastAPI app that:

1. Receives captured payloads from the extension (`POST /api/payload`).
2. Drives the existing `takeout_dl` engine to download every part.
3. Tracks job state and serves a live progress web UI.
4. Exposes a control API the Pi agent calls to debug, decide, and learn.
5. Notifies you over Telegram (see `06-telegram.md`).
6. Records completed runs as **workflow recipes** for repeat-without-LLM.

It is **localhost-only** (port 8080). Nothing about it is exposed by the
Cloudflare tunnel. You reach it as a tab inside the hosted Chrome, or over an
SSH port-forward.

## Why FastAPI + in-process engine

The engine (`takeout_dl.py`) is already a clean module: `parse_payload`,
`validate_cookie`, `discover_exports`, `download_exports`, `validate_output_dir`.
The manager imports these directly rather than shelling out, so it gets:

- Structured progress (the engine's `PartProgress` objects) instead of scraping
  stdout.
- Direct control of the auth-challenge → resume loop.
- One process to supervise.

The engine's current `download_exports()` renders to a terminal
(`ProgressDisplay`). The manager needs progress as **data**, not ANSI. So the
first integration task is a small refactor (see "Engine integration" below) that
adds a progress-callback seam without changing download/integrity logic.

## State model

A **job** is one download run for one workflow. Persisted to
`<outdir>/.manager_state.json` so a manager restart recovers it.

```jsonc
{
  "job_id": "20260620T154500-braincreation",
  "workflow": "braincreation",
  "output_dir": "/opt/storage.jfs002/google-takeout/braincreation",
  "status": "downloading",   // see status enum below
  "parallel": 4,
  "max_exports": 290,
  "created_at": "2026-06-20T15:45:00Z",
  "updated_at": "2026-06-20T15:51:22Z",
  "cookie_captured_at": "2026-06-20T15:45:00Z",
  "totals": {
    "parts_total": 290,
    "parts_done": 41,
    "bytes_total": 3110000000000,
    "bytes_done": 442000000000,
    "speed_bps": 88000000
  },
  "parts": [
    { "index": 0, "filename": "...-000.zip", "size": 10737418240,
      "done": 10737418240, "status": "done" },
    { "index": 41, "filename": "...-041.zip", "size": 10737418240,
      "done": 2200000000, "status": "active" }
  ],
  "last_error": null,
  "recipe_ref": "braincreation"   // links to the saved workflow recipe
}
```

### Job status enum

| Status | Meaning | Next action |
|---|---|---|
| `idle` | no job running | wait for payload |
| `queued` | payload accepted, not started | manager starts engine |
| `downloading` | engine actively pulling parts | stream progress |
| `needs_cookie` | auth challenge; cookie expired | request re-capture (extension) + alert |
| `paused` | user/agent paused | resume on command |
| `complete` | all parts verified | record recipe, notify done |
| `error` | non-auth failure | keep partials, alert, await retry |

The `needs_cookie` ↔ `downloading` transition is the auto-relogin heartbeat of
the whole system.

## HTTP API

All endpoints localhost-only. The control-plane endpoints (`/api/control/*`)
additionally require a bearer token (`MANAGER_API_TOKEN`) so a stray process on
the box can't drive downloads; the extension uses a separate, narrower token
(`MANAGER_CAPTURE_TOKEN`) that can only POST payloads.

### Capture sink

```
POST /api/payload
  Auth: X-Capture-Token: <MANAGER_CAPTURE_TOKEN>
  Body: <the exact v1 payload JSON the engine already understands>
  Behavior:
    - validate_cookie() first; reject 422 if invalid.
    - If a job is in needs_cookie: treat as a cookie refresh, resume it.
    - Else create a new job (output dir derived from workflow name; see below).
  Returns: { "job_id": "...", "status": "queued|downloading", "resumed": bool }
```

This is the single most important new endpoint: it is what the extension POSTs
to (decision 4) and what makes auto-resume work without a human pasting.

### Job/progress (read-only, for the UI)

```
GET  /api/jobs                 -> list of jobs (summary)
GET  /api/jobs/{job_id}        -> full job state (the JSON above)
GET  /api/jobs/{job_id}/events -> Server-Sent Events stream of progress deltas
GET  /api/jobs/{job_id}/log    -> tail of <outdir>/takeout_dl.log
```

The web UI subscribes to the SSE stream for live bars without polling.

### Control plane (Pi agent + you)

```
POST /api/control/start    { workflow, output_dir?, parallel?, max_exports? }
POST /api/control/pause    { job_id }
POST /api/control/resume   { job_id }
POST /api/control/cancel   { job_id }
POST /api/control/recapture { job_id }   # ask the extension to grab a fresh cookie
GET  /api/control/health                 # engine, browser (CDP), disk, cookie age
GET  /api/control/diagnose { job_id }    # structured "why is this stuck" report
```

`/api/control/diagnose` is built for the agent and weaker models: it returns a
machine-readable reason (`cookie_expired`, `disk_full`, `auth_loop`,
`network_stall`, `zip_validation_failed`) plus the recommended next action, so a
model doesn't have to infer state from logs. The decision trees in
`04-decision-trees.md` map 1:1 to these reason codes.

### Repeat-without-LLM

```
GET  /api/recipes                 -> saved workflow recipes
POST /api/recipes/{name}/run      -> replay a recipe (no model needed)
POST /api/recipes/{name}/schedule { cron }   -> periodic replay
```

## Engine integration (the one required refactor)

`download_exports()` today owns a `ProgressDisplay` that writes ANSI to the
terminal. To feed the manager UI we add a **callback seam** without touching the
download or integrity logic:

```python
# takeout_dl.py  (proposed, additive)
def download_exports(exports, payload, output_dir, parallel,
                     progress_cb=None,     # NEW: called with PartProgress snapshots
                     auth_cb=None):        # NEW: called when an auth challenge fires
    ...
    def _set(idx, **kw):
        with lock:
            ...
            if progress_cb:
                progress_cb(progress[idx])   # push a snapshot
    ...
    # on _AuthChallenge:
    if auth_cb:
        auth_cb(auth_info)   # manager flips job -> needs_cookie, alerts, recaptures
```

- When `progress_cb` is `None`, the engine behaves exactly as today (CLI/TUI
  unaffected). The manager passes callbacks; nobody else does.
- The auth-challenge path already stops the pool and keeps partials. The manager
  hooks `auth_cb` to flip status and trigger re-capture instead of the CLI's
  "wait for a fresh payload file" poll. The file-poll path stays as the fallback.

This is the **only** engine change required. Everything else is new code in a
new `manager/` package that imports the engine.

## Output-dir derivation (account + export date)

The manager derives a **two-level** output path per run:

```
<STORAGE_ROOT>/google-takeout/<account-label>/<export-timestamp>/
e.g.  /opt/storage.jfs002/google-takeout/braincreation/2026-06-16-04-01-04/
```

### Where each piece comes from

| Piece | Source | Notes |
|---|---|---|
| `<account-label>` | (1) explicit label in the control/recipe call; (2) account **email** local-part scraped from the Takeout page DOM by the extension; (3) fallback `user`/`authuser` gaia id from the payload URL | `braincreation` is a **label**, not something Google returns. The `user=` URL param is an obfuscated numeric gaia id, not a name. |
| `<export-timestamp>` | parsed from the **filename**: `takeout-20260616T040104Z-...zip` → `20260616T040104Z` | Identical across every part of one export, so it uniquely identifies the export instance. Formatted to `YYYY-MM-DD-HH-MM-SS` (UTC). |

### Derivation rules (deterministic, no LLM)

```
account_label =
    control/recipe override
    else sanitize(email_local_part from extension meta.email)   # e.g. "braincreation"
    else "gaia-" + (meta.user or meta.authuser)                  # stable fallback

export_ts =
    parse first \d{8}T\d{6}Z from any export filename
    -> reformat 20260616T040104Z => 2026-06-16-04-01-04
    else captured_at from payload (rounded to seconds), tagged "-capture"

output_dir = STORAGE_ROOT / "google-takeout" / account_label / export_ts
```

- `sanitize()` lowercases, strips the `@domain`, and allows only `[a-z0-9._-]`
  (so `braincreation@gmail.com` → `braincreation`).
- The extension must therefore add `email` to the payload meta (best-effort,
  scraped from the account switcher DOM); it already carries `user`/`authuser`.
  See `03-extension-v4.md`.
- The same `(account_label, export_ts)` pair always maps to the same folder, so a
  re-capture / resume of the **same** export lands in the **same** directory and
  resumes partials. A genuinely new export (new timestamp) gets a new folder.

### Safety

- The fully derived path runs through the engine's existing
  `validate_output_dir` allowlist. The manager never writes outside the allowed
  roots, same guarantee as the CLI.
- Both path segments are sanitized before joining (no `..`, no separators, no
  absolute-path injection from a hostile email/label).
- The manager is the sole writer per output dir; concurrent jobs target distinct
  dirs.

## Per-run manifest (what was downloaded)

Every job writes a `manifest.json` into its output dir, updated as parts
complete and finalized on `complete`. This is the durable record of "what was
downloaded, file sizes, and times" the operator asked for.

```jsonc
// <output_dir>/manifest.json
{
  "account_label": "braincreation",
  "account_email": "braincreation@gmail.com",   // best-effort, may be null
  "gaia_user": "1153...",                        // meta.user
  "authuser": "0",
  "export_timestamp": "2026-06-16-04-01-04",
  "export_raw": "20260616T040104Z",
  "archive_id": "<j= param>",                    // ties parts to one export
  "job_id": "20260616T154500-braincreation",
  "captured_at": "2026-06-16T15:45:00Z",
  "started_at": "2026-06-16T15:45:03Z",
  "completed_at": "2026-06-16T19:57:11Z",
  "parts_total": 290,
  "parts_done": 290,
  "bytes_total": 3110000000000,
  "bytes_done": 3110000000000,
  "files": [
    {
      "index": 0,
      "filename": "takeout-20260616T040104Z-1-001-part-000.zip",
      "size": 10737418240,
      "sha256": "...",                 // computed at finalize (optional, flag-gated)
      "started_at": "2026-06-16T15:45:03Z",
      "completed_at": "2026-06-16T15:52:40Z",
      "zip_valid": true,
      "status": "done"
    }
    // ...one entry per part
  ]
}
```

Notes:

- Per-file `started_at`/`completed_at` come from the engine's existing
  `PartProgress` transitions, surfaced via the `progress_cb` seam — no new
  download logic, just recording what the engine already knows.
- `zip_valid` reuses the engine's existing end-of-central-directory check.
- `sha256` is optional (off by default; it costs a full re-read of multi-GB
  files). Enable per job via a control flag when you need content-level dedupe.
- The manifest is the data source for the UI's "completed files" table, the
  Telegram `/status` summary, and the repeat-without-LLM recipe.
- `manifest.json` (durable result record) is distinct from `.manager_state.json`
  (transient live job bookkeeping).

## Process layout

```
manager/
  __init__.py
  app.py            # FastAPI app, routes
  jobs.py           # Job model, state machine, persistence
  engine_bridge.py  # imports takeout_dl, runs download_exports in a worker thread
  recipes.py        # workflow recipe store + replay
  notify.py         # Telegram client (see 06-telegram.md)
  diagnose.py       # reason-code health/diagnose logic
  web/              # static progress UI (served at /)
```

Runs under the webtop container (same image, extra process) or as a sibling
container sharing the storage + localhost network. Deployment details in
`05-deployment.md`.

## Open questions to resolve at build time

- In-process engine thread vs subprocess per job. Leaning in-process worker
  thread for v1 (simpler progress), subprocess later if isolation matters.
- SSE vs WebSocket for the UI. SSE is enough (one-way progress) and simpler.
- Where recipes live: `<STORAGE_ROOT>/google-takeout/.recipes/<name>.json`.
