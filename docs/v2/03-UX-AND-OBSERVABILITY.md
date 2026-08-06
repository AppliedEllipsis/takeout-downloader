# 03 — UX & OBSERVABILITY

> How the operator *feels* a multi-day, attempt-limited download. The UI's
> single most important job: **the attempt budget must be impossible to miss.**
> A part with one download left must *feel* dangerous.

Every string, layout, and threshold below is normative. Implementers: render
exactly this, don't "improve" it.

---

## 1. The three surfaces

| Surface | Where | Purpose |
|---------|-------|---------|
| CLI | SSH terminal | **Primary.** Everything is scriptable, `--json` everywhere |
| Extension popup | Inside the KasmVNC webtop | Live glanceable status while the browser is open |
| Telegram | Phone | Only for events that need a human *now* |

All three consume the **same event stream** (`00-CONTRACTS.md` §6), so they
can never disagree. If a new field lands in one, it lands in all.

---

## 2. CLI command surface

```
takeout2 status                    # one-shot snapshot
takeout2 watch                     # live full-screen dashboard
takeout2 run --payload in.json     # start a job
takeout2 budget <archive_id>       # attempt accounting (the money view)
takeout2 verify <archive_id>       # local-only structural verification
takeout2 identity <archive_id>     # who/what/when, with provenance
takeout2 doctor                    # preflight health
takeout2 migrate                   # v1 state -> state.db (dry-run by default)
```

Every command accepts `--json` and emits the identical data structure the
human view renders from. `watch` on a non-TTY degrades to repeated `status`.

### 2.1 `takeout2 status` — rendered

```
▄ takeout2 ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  braincreation / 2026-06-16-04-01-04        [SCRAPED_EMAIL · HIGH]
  archive j=…ab9f2         62 parts · 620 GB expected · 3.08 TB done
  state   DOWNLOADING                        since 14:02 (2h 41m)
  ────────────────────────────────────────────────────────────────────
  parts     55 done · 4 active · 2 partial · 1 pending · 0 exhausted
  bytes      3.08 TB / 3.18 TB   (96.9%)
  speed      247 MB/s aggregate   (EWMA 30s)
  eta        11 min
  budget     ⚠ 1 part has 1 attempt left · 0 at 0  (see: budget)
  cookie     42 s old · burst open · canary passed
  errors     2 last (both NETWORK_ERROR, auto-recoverable)
```

### 2.2 `takeout2 budget <archive_id>` — rendered (normative shape)

```
archive 9f3a…  braincreation / 2026-06-16-04-01-04   [SCRAPED_EMAIL, HIGH]
part  filename                              size    on-disk  local  google  left  state
   0  takeout-…-9-000.zip                  10.0G     10.0G      1       1     4  DONE STRUCT_OK
   7  takeout-…-9-007.zip                  10.0G      3.2G      4       4     1  PARTIAL ⚠ last attempt
  61  takeout-…-9-061.zip                  10.0G         0      5       5     0  BUDGET_EXHAUSTED ✖
                                                       ── archive total: 5 of 62 parts at risk
```

Columns: `local` = our ledger, `google` = Google's own `dl_count` scrape.
Where they differ, **the larger number is the truth** (`01-IDENTITY…` §8).
The `google` column is what makes this view trustworthy — it is not our guess,
it is Google's counter.

Color rules for `left`:
- `4–5` normal
- `2–3` amber (`⚠`)
- `1`  **red, bold** — "last attempt"
- `0`  `BUDGET_EXHAUSTED` — inverse video, never auto-retried

### 2.3 `takeout2 doctor` — preflight

| Check | Pass | Fail |
|-------|------|------|
| storage root writable | `rw 1.12 PB free` | `ROOT DISK FULL RISK` |
| `state.db` opens + WAL | `ok` | `run reconcile_orphans` |
| CDP reachable (`127.0.0.1:9222`) | `Chrome 149` | `login in the webtop` |
| cookie jar present + age | `42 s old` | `open the manage page` |
| disk headroom | `> 2× export size free` | `free space needed` |
| log rotation on | `10m×3` | `docker inspect` |
| budget health | `0 exhausted` | `see: budget` |

Exit code 0 if all pass, 1 otherwise — scriptable in the runbook's watchdog.

---

## 3. Progress semantics (formulas)

| Quantity | Formula | Window |
|----------|---------|--------|
| Instantaneous speed (per part) | EWMA over per-chunk bytes / wall_ms | 30 s |
| Aggregate speed | Σ per-part EWMA | 30 s |
| Part ETA | `(size_expected − size_on_disk) / part_speed` | — |
| Job ETA | `Σ(remaining of pending+active) / aggregate_speed` | must include not-yet-started parts |
| **Stalled** | no bytes for `TK2_STALL_S` (default **90 s**) | per part |
| **Dead** | no bytes for `TK2_DEAD_S` (default **600 s**) | per part → part FAILED |

Stall ≠ failure: a stalled part is shown with `⏳` and its own countdown; it
becomes a candidate for the next burst only if it goes dead.

---

## 4. Budget UX — the part that must *feel* dangerous

### The danger prompt (before spending a last attempt)

```
┌────────────────────────────────────────────────────────────┐
│ ⚠  PART 7 HAS 1 DOWNLOAD ATTEMPT LEFT                      │
│                                                            │
│  This is Google's own counter, scraped from the page.       │
│  If this attempt fails, part 7 is PERMANENTLY LOST unless   │
│  you re-request the entire export (days of waiting).        │
│                                                            │
│  The cookie was refreshed 4 s ago and a canary passed.      │
│                                                            │
│  [r] Retry anyway (single stream, alone)                   │
│  [w] Wait — let me re-verify the cookie first              │
│  [a] Abandon this part; park it as BUDGET_EXHAUSTED        │
│  [v] Show verify --detail for this part                    │
└────────────────────────────────────────────────────────────┘
```

`[w]` re-runs the canary on a *different, healthy* part so the last attempt is
only spent against a proven-good cookie. The one-shot part is scheduled alone,
never inside a burst (`05-PARALLELISM…` §6).

### BUDGET_EXHAUSTED

Rendered as a full-width banner in `watch`; the part row inverts. The
extension popup turns red and shows a `🔑`-style `⚠` with the count. Telegram
fires `budget_warning` once (never re-fires until resolved). The part is never
touched again without `--force`.

---

## 5. Extension popup

```
┌─────────────────────────────────────────┐
│ takeout2  ● connected    v2.0        ⚙  │
│ braincreation / 2026-06-16-04-01-04     │
│ 55/62 done · 96.9% · 247 MB/s · ~11 min │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░         │
│                                         │
│ budget ⚠ part 7: last attempt          │   ← cannot be missed
│ cookie 42 s old · burst open ✓         │
│ heart  14 s since last update ✓        │
│ ─────────────────────────────────────── │
│ 007  ▓▓▓▓▓▓▓▓▓▓▓▓░░░░  3.2/10 GB  71 MB/s │
│ 011  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  9.9/10 GB  88 MB/s │
│ 013  ⏳ stalled 47 s                    │
│ ─────────────────────────────────────── │
│ log  tail…                              │
└─────────────────────────────────────────┘
```

Rules:
- Poll snapshot every 2 s **or** receive SSE push — whichever is fresh. On a
  500-part job, show only: active rows + at-risk rows + summary. Never render
  500 rows.
- **Heartbeat**: `heart` shows seconds since the last event *of any kind*.
  v1 had no idle signal; here, >20 s shows `⚠`, >60 s shows `stalled?` and the
  poller retries the connection.
- **Stall warning** per part after 90 s of no bytes.
- The budget warning is drawn in the header, above every fold, always.

---

## 6. Error presentation

| ReasonCode | User sees | Actionable? | Recommended action |
|------------|-----------|-------------|--------------------|
| `OK_COMPLETE` | — | — | — |
| `OK_PARTIAL` | `part N resumed (+X GB)` | no | keep going |
| `AUTH_REDIRECT` | `cookie expired — recapturing` | yes | extension auto-recaps; else open manage page |
| `AUTH_401` | `session rejected (401)` | yes | re-login in webtop, re-capture |
| `LIMIT_EXCEEDED` | `⚠ download limit hit` | **yes, human** | see budget; re-request export only as last resort |
| `NOT_FOUND` | `part index past end` | no | safe stop if `idx >= parts_expected` |
| `END_OF_RANGE` | `reached end of archive` | no | clean discovery stop, NOT an error |
| `RATE_LIMITED` | `Google throttled us` | wait | back off `TK2_RATE_BACKOFF_S` (default 300) |
| `NETWORK_ERROR` | `connection reset` | no | eligible for next burst |
| `DISK_ERROR` | `local disk full!` | **yes, human** | free space; FUSE stall check |
| `ABORTED` | `cancelled by operator` | no | — |

Never show a raw traceback. The one exception is `--debug`.

---

## 7. Notifications (Telegram)

v1's Telegram was deferred and never wired (`10-deployment-status.md` §7.7).
v2 wires it, but only for events that need a human *now*:

| Event | Always fire? | Throttle |
|-------|--------------|----------|
| `started` | yes | — |
| `complete` | yes | — |
| `failed` | yes | — |
| `needs_cookie` | yes | — (it parks the job) |
| `budget_warning` | yes | once per part until resolved |
| `LIMIT_EXCEEDED` | yes | once |
| `milestone` | **no** (v1 spammed 667 msg/hr) | `TK2_TG_MILESTONE_MIN` default 60 min |

Message format (normative):
```
⚠ budget_warning
part 7 of 62 (braincreation / 06-16) has 1 attempt left
Reply /budget for detail, /force-7 to spend it deliberately
```

---

## 8. Structured logging

JSONL to `logs/engine.log`, rotated 10 MB × 5.

```json
{"ts":"2026-06-16T14:02:11Z","lv":"info","ev":"burst_open","n":4,"cookie_age_s":2}
{"ts":"2026-06-16T14:02:11Z","lv":"info","ev":"chunk","idx":7,"bytes":8388608,"net_ms":66,"write_ms":5}
{"ts":"2026-06-16T14:02:12Z","lv":"warn","ev":"part_risk","idx":7,"left":1}
{"ts":"2026-06-16T14:02:40Z","lv":"error","ev":"attempt","idx":11,"outcome":"NETWORK_ERROR","attempts_left":3}
```

Every `attempt` line carries `outcome` + `attempts_left` — the audit trail that
makes the budget view honest.

---

## 9. Degradation matrix

| Situation | Behaviour |
|-----------|-----------|
| No TTY / piped | `status`-style output, no ANSI, no full-screen |
| Narrow terminal (<80 cols) | summary-only view; parts table collapses to counts |
| 500+ parts | active + at-risk rows only; summary rows for the rest |
| SSE disconnected | poll `/api/v2/jobs/{id}` at 5 s; resume stream with `?since=` |
| `--json` anywhere | identical data, machine-readable |
| Operator SSH session dies | job runs on; `takeout2 watch` reattaches from `state.db` |
| Browsing, no CLI | extension popup is the full surface (it IS the same stream) |

---

## 10. Invariants

1. All three surfaces render from the same event stream; none may invent state.
2. The budget line is always visible: header in the popup, banner in watch.
3. A part at 1 attempt left requires the explicit danger prompt.
4. No tracebacks except `--debug`.
5. `--json` is a first-class output of every command, not an afterthought.
6. `watch` reattaches from `state.db`; it never needs the manager to be "fresh".
