# 08 — SELF-DRIVING UX CONTRACT (normative)

> **Companions:** `00-CONTRACTS.md` (attempt budget, status enums — authoritative),
> `06-LIVE-MONITORING.md` (event schema), `07-RELIABILITY-HARDENING.md` (guards).
> This file defines the *autonomy* layer and the surfaces that expose it.
>
> **This file never overrides `00-CONTRACTS.md`.**

---

## 0. The problem being solved

Everything in v2 is currently manual: you capture, then **you** run the CLI, and
if the cookie dies it stops until **you** notice. A 3 TB / 63-part transfer takes
days, and the download cookie idles out in ~1–2 minutes. A human cannot babysit
that.

**Decision (operator, explicit):** the tool is *fully automatic*. A capture
starts the download. A dead cookie heals itself and notifies. The operator never
touches the CLI for a normal run.

The blast radius of "automatic" is bounded by the canary-first burst
(`engine.run_burst`): a bad capture costs **1 attempt on one part**, not 5 on all.

---

## 1. The Runner (the keystone — nothing else is autonomous without it)

`takeout2/runner.py`. One daemon thread per archive. Ported from the proven v1
pattern in `manager/engine_bridge.py` (a `threading.Event` the job parks on).

### 1.1 State machine

```
        capture arrives
              │
              ▼
      ┌───────────────┐   parts known    ┌──────────────┐
      │  DISCOVERING  │─────────────────▶│    READY     │
      └───────────────┘                  └──────┬───────┘
                                                │ auto-start
                                                ▼
   ┌─────────────┐   cookie dead    ┌────────────────────┐
   │NEEDS_COOKIE │◀─────────────────│    DOWNLOADING     │
   └──────┬──────┘                  └─────────┬──────────┘
          │ fresh capture POSTed              │ all parts DONE
          │ (Event.set → wake)                ▼
          └──────────────────────▶     ┌──────────────┐
                                       │   COMPLETE   │
                                       └──────────────┘
   PAUSED  ← operator            BUDGET_EXHAUSTED ← needs a human decision
```

`JobStatus` values are exactly those in `contracts.py` — no new ones.

### 1.2 Invariants (non-negotiable)

| # | Invariant | Why |
|---|-----------|-----|
| R1 | **One runner per `archive_id`.** Starting a running job is a no-op, not a second thread. | Two threads on one part = two attempts for one file. |
| R2 | The runner **never retries instantly**. Between bursts it sleeps `burst_gap_s` (default 5 s). | An instant retry loop would drain all 5 attempts in seconds. |
| R3 | On auth failure the runner parks in `NEEDS_COOKIE` and **waits on an Event** — it does not poll Google. | Polling with a dead cookie spends attempts for zero bytes. |
| R4 | The runner is a **daemon thread**; process exit never blocks on it. | Manager restarts must not hang. |
| R5 | Every attempt still goes through `ledger.reserve()` in the engine. The runner adds **zero** new request paths. | The ledger is the only source of attempt truth. |
| R6 | `BUDGET_EXHAUSTED` **stops the runner** and requires an explicit operator action to clear. | Never auto-spend the last attempt. |
| R7 | Runner state lives in memory; **`state.db` remains the source of truth**. A restart re-derives everything. | Crash safety. |

### 1.3 Public API

```python
class JobRunner:                      # one per archive_id
    def start(self) -> None           # idempotent (R1)
    def stop(self, *, wait: bool = False) -> None
    def notify_cookie(self) -> None   # wake a NEEDS_COOKIE runner (R3)
    def is_alive(self) -> bool
    def snapshot(self) -> dict        # {archive_id, status, alive, last_error,
                                      #  bursts, cookie_waits, started_at}

class RunnerSupervisor:               # process-wide registry
    def ensure(self, archive_id) -> JobRunner    # create-or-get (R1)
    def get(self, archive_id) -> Optional[JobRunner]
    def stop_all(self, *, wait=False) -> None
    def notify_cookie(self, archive_id) -> bool
    def snapshot_all(self) -> list[dict]
```

---

## 2. Control-plane routes (added to `takeout2/api.py`)

All mounted under `/api/v2`. Read routes stay open; **control routes require the
capture token** when one is configured.

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/jobs/{archive_id}/start` | Start (or ensure) the runner. Idempotent. |
| POST | `/jobs/{archive_id}/pause` | Stop the runner, job → `PAUSED`. |
| POST | `/jobs/{archive_id}/resume` | Clear `PAUSED`, start the runner. |
| GET | `/jobs/{archive_id}/runner` | Runner snapshot (alive, bursts, waits). |
| GET | `/runners` | All runner snapshots. |
| POST | `/jobs/{archive_id}/clear-budget-block` | Explicit human unblock for R6. |

`POST /capture` gains **auto-start**: after upserting the job it calls
`supervisor.ensure(archive_id).start()` unless `autostart=false` is passed or the
job is `PAUSED`/`BUDGET_EXHAUSTED`. A capture for a job already in `NEEDS_COOKIE`
calls `notify_cookie()` — that is the self-heal path.

---

## 3. Overlay on takeout.google.com (`helpers/overlay.js`)

Injected by `content.js`. **Shadow DOM** (`attachShadow({mode:'open'})`) so
Google's CSS cannot reach in and nothing we do leaks out.

### 3.1 Layout

```
┌──────────────────────────────────────────┐
│ ▼ Takeout Downloader        ● healthy    │  ← header, click = collapse
├──────────────────────────────────────────┤
│ braincreation · 2026-06-23               │
│ ████████████░░░░░░░░  12/63 · 19%        │
│ 412 GB / 3.08 TB · 84 MB/s · ETA 8h 12m  │
│ ⚠ 2 parts at 1 attempt left              │
├──────────────────────────────────────────┤
│ [ ⏸ Pause ]  [ ↻ Recapture ]  [ 📈 Monitor ]│
├──────────────────────────────────────────┤
│ ▸ Parts (12 done, 3 active, 48 pending)  │
│ ▸ Capture history (7)                    │
│ ▸ Activity                               │
└──────────────────────────────────────────┘
```

### 3.2 Rules

* **Collapsed by default state is remembered** in `chrome.storage.local`
  (`overlayCollapsed`).
* Position: fixed, bottom-right, `z-index: 2147483600` (below Chrome UI, above
  Google's). Draggable by the header; position persisted.
* Data source: `GET {mgr}/api/v2/jobs/{id}?parts=1` + the SSE stream, exactly as
  `monitor.html` does. **The overlay opens no new backend routes.**
* Guard banners reuse the **same `classifyGuard()` rules** as the popup and
  monitor page (storage > rate > cache > stall). Three implementations, one
  behavior — a parity test enforces this.
* If the manager is unreachable the overlay shows `○ manager offline` and keeps
  retrying every 5 s. It must **never** throw into Google's page context.
* Never blocks or overlays Google's own download buttons.

---

## 4. Notification on self-heal (operator choice)

Each successful auto-recapture emits an event of kind `self_heal` and fires the
existing v1 notifier (`manager/notify.py`, Telegram) when configured:

> `🔄 braincreation: cookie expired, re-captured automatically, resumed at part 14/63.`

A **failed** self-heal (extension could not re-scrape, e.g. Google signed you
out) escalates to `needs_human` and always notifies.

---

## 5. Definition of done

1. Click Download once on Takeout → job appears, starts, and finishes days later
   with **zero further interaction**.
2. Killing the cookie mid-run → auto-recapture → resume, with a notification.
3. Restarting the manager mid-run → runners re-derive from `state.db` and
   continue.
4. `BUDGET_EXHAUSTED` never auto-clears.
5. The overlay, popup, and monitor page never disagree about a job's state.
6. All existing tests stay green; new runner tests are network-free.

<!-- APPEND-HERE -->
