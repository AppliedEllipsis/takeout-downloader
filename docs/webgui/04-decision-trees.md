# 04 — Decision Trees & Runbooks

This doc exists so a **weaker model or a human operator** can run, debug, and
recover the system without re-deriving how it works. Every branch maps to a
machine-readable reason code returned by `GET /api/control/diagnose` (see
`02-manager-service.md`). Read a reason code, jump to its section, do the steps.

## Master decision tree: "the download isn't progressing"

```
START: a job is not making progress
  │
  ├─ Call GET /api/control/diagnose?job_id=...
  │     → returns one of: cookie_expired | disk_full | auth_loop |
  │       network_stall | zip_validation_failed | browser_down | manager_down
  │
  ├─ reason == cookie_expired ─────────────► §A Cookie refresh
  ├─ reason == auth_loop ──────────────────► §B Auth loop (real logout)
  ├─ reason == disk_full ──────────────────► §C Disk full
  ├─ reason == network_stall ──────────────► §D Network stall
  ├─ reason == zip_validation_failed ──────► §E Bad ZIP
  ├─ reason == browser_down ───────────────► §F Browser/CDP down
  └─ reason == manager_down ───────────────► §G Manager down
```

If `/api/control/diagnose` itself is unreachable, go straight to **§G**.

## §A — cookie_expired (the normal, expected case)

This happens roughly every 45 minutes. It is **not an error**; it is the
designed heartbeat.

```
cookie_expired
  │
  ├─ Is autoRecapture ON in the extension?  (GET extension getState)
  │     ├─ YES → extension should already be re-capturing.
  │     │        Wait 30s. Re-check diagnose.
  │     │          ├─ now downloading? → DONE (auto-resume worked)
  │     │          └─ still cookie_expired after 2 cycles? → treat as auth_loop §B
  │     └─ NO  → POST /api/control/recapture {job_id}  (ask manager to trigger it)
  │              └─ still stuck? → §B
  │
  └─ Manual fallback (always works):
        1. Open KasmVNC portal → Chromium → takeout.google.com.
        2. Click Download on any export part; let it start.
        3. Click the pinned extension → "Send now" (POST) — or Copy as JSON.
        4. If you copied: paste into the manager UI "refresh cookie" box.
        5. Job resumes from partials. No restart, no lost files.
```

## §B — auth_loop (Google forced a real logout)

The extension re-captured but the cookie is still invalid, or re-capture yields a
sign-in page. This means the **session is genuinely dead**, not just the
short-lived download cookie.

```
auth_loop
  │
  ├─ Manager has already: fired Telegram "Manual login needed" + KasmVNC sound.
  │
  ├─ FIX (human, ~1 min):
  │     1. Open KasmVNC portal → Chromium.
  │     2. Go to accounts.google.com; complete login / 2FA as normal.
  │     3. The profile persists, so this is rare (only on Google-forced re-auth).
  │     4. Trigger one Takeout download to seed a fresh cookie.
  │     5. Extension auto-POSTs; job resumes.
  │
  └─ DO NOT: store passwords or automate 2FA (explicit non-goal). Escalate to the
     human via Telegram instead. This is by design.
```

## §C — disk_full

```
disk_full
  ├─ Manager paused the job (status=error, last_error=disk_full), partials kept.
  ├─ FIX:
  │    1. Check space:  df -h /opt/storage.*  (over SSH)
  │    2. Free space or point the workflow at another allowed root.
  │    3. POST /api/control/resume {job_id}
  └─ Prevention: manager pre-checks free space vs payload bytes_total before start
     and refuses with a clear message if it won't fit.
```

## §D — network_stall

```
network_stall  (no bytes for N seconds, but cookie still valid)
  ├─ Engine already retries with exponential backoff (MAX_RETRIES, RETRY_BACKOFF).
  ├─ Wait one backoff cycle. Re-check diagnose.
  │    ├─ recovered → DONE
  │    └─ still stalled after max retries → manager marks error; POST resume to retry.
  └─ If ALL parts stall at once → suspect upstream/network; check server
     connectivity (curl https://takeout-download.usercontent.google.com over SSH).
```

## §E — zip_validation_failed

```
zip_validation_failed  (a finished file failed the PK\x05\x06 end-of-archive check)
  ├─ The engine does NOT finalize a bad file; it stays .downloading.
  ├─ FIX: POST /api/control/resume {job_id} — the part re-downloads via Range.
  ├─ If it fails validation repeatedly on the SAME part:
  │     - The source export may be genuinely corrupt on Google's side, OR
  │     - a cookie expired mid-stream and HTML was appended.
  │     - Delete that part's .downloading file; resume re-fetches from zero.
  └─ Never ship a job complete with a failed part; manager keeps it incomplete.
```

## §F — browser_down (CDP unreachable on :9222)

```
browser_down
  ├─ Downloads in flight DO NOT need the browser — they keep running.
  │   Only re-capture needs it. So a browser crash mid-download is non-fatal
  │   until the next cookie expiry.
  ├─ FIX:
  │    1. webtop should auto-restart Chromium. Wait ~20s.
  │    2. Verify: curl http://127.0.0.1:9222/json/version  (over SSH)
  │    3. Persistent profile = still logged in. Trigger a capture to confirm.
  └─ If Chromium won't start: check container logs (§G tools), shm_size, /config.
```

## §G — manager_down

```
manager_down  (API unreachable)
  ├─ Downloads MAY still be running if only the API thread died; but treat as
  │   "control lost" and recover the process.
  ├─ FIX:
  │    1. SSH in. Check the service:  docker compose ps / systemctl status.
  │    2. Restart it. Job state persisted to <outdir>/.manager_state.json is
  │       reloaded; engine partials on disk are intact.
  │    3. Manager re-attaches to the job and resumes.
  └─ Absolute fallback (manager unrecoverable): run the engine directly, exactly
     like today's documented flow:
        tmux new -s takeout
        python3 takeout_dl.py --payload /tmp/payload.json \
          --out /opt/storage.*/google-takeout/<workflow> --parallel 4 \
          --max-exports <N>
     The new system is a convenience layer over this; the CLI always works.
```

## Cold-start runbook (first ever run)

```
1. Deploy stack (05-deployment.md). Confirm KasmVNC portal loads via tunnel.
2. In Chromium: log into Google once. Verify api.ipify.org shows the SERVER ip.
3. Load extension v4 (or it's pre-seeded). Set managerUrl + captureToken.
4. Start a Takeout export; click Download on a part.
5. Extension POSTs → manager creates job → progress UI shows bars.
6. Watch one cookie_expired cycle auto-resolve (§A) to confirm the heartbeat.
7. Let it finish. Confirm Telegram "done" + a recipe is saved.
```

## Repeat-without-LLM runbook

```
1. GET /api/recipes  → confirm the workflow you want exists.
2. POST /api/recipes/<name>/run.
3. Manager: opens Takeout in the logged-in browser, triggers export + capture
   via the extension automation, feeds payload to engine. No model involved.
4. Monitor via Telegram or the UI. Any failure → the §A–§G trees above apply
   identically (a recipe run is just a job).
5. Optional: POST /api/recipes/<name>/schedule {cron} for periodic runs.
```

## Reason-code ↔ section quick map

| Reason code | Section | Auto-recoverable? | Needs human? |
|---|---|---|---|
| `cookie_expired` | §A | Yes (auto-recapture) | only if it becomes auth_loop |
| `auth_loop` | §B | No | Yes — manual Google login |
| `disk_full` | §C | No | Yes — free space |
| `network_stall` | §D | Usually (retries) | only if persistent |
| `zip_validation_failed` | §E | Often (resume) | only if a part is truly corrupt |
| `browser_down` | §F | Usually (webtop restart) | rarely |
| `manager_down` | §G | On restart | only if process won't start |
