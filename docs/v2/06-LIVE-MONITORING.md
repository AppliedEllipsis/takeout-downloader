# 06 — LIVE MONITORING: EVENT CONTRACT (normative)

> Companion to `00-CONTRACTS.md` §6. Defines the real-time monitoring stream
> that feeds the extension popup, the in-webtop monitor page, and the CLI
> `watch`. Implementers MUST emit/consume exactly these events.

---

## 1. The two surfaces (same stream, two renderers)

| Surface | Where | Transport |
|---------|-------|-----------|
| Extension popup | inside the webtop, click the icon | SSE `?since=` + 2 s fallback poll |
| In-webtop monitor page | `http://127.0.0.1:8080/ui/monitor.html` | SSE `?since=` + 2 s fallback poll |

Both render the SAME events. Neither may invent state; both reconnect with
`?since=<last_seq>` so a dropped connection misses nothing.

---

## 2. Engine → store progress emission (the new plumbing)

The engine already exposes `on_chunk(ChunkProgress(idx, bytes_moved, wall_ms,
net_ms, write_ms))`. The API layer wraps it into **throttled** `part_progress`
events:

```
PER_PART throttle window: 1500 ms  (TK2_PROGRESS_INTERVAL_MS)
on_chunk fires (per stream):
  accumulate {bytes_moved, wall_ms} into a per-part bucket
  if now - last_emit[idx] >= 1500:
      speed_bps = bytes_this_window / (window_ms/1000)
      emit part_progress {idx, size_on_disk, size_expected, speed_bps,
                          bytes_this_window, window_ms, state, verify, error}
      reset bucket
```

`size_on_disk` comes from the accumulating bucket (bytes_moved delta), NOT a
`stat()` per chunk — that would be a per-part stat storm (forbidden in §00).

---

## 3. Event schema (normative)

### `part_progress` — the high-frequency one
```json
{"seq": 123, "ts": "2026-...Z", "kind": "part_progress", "archive_id": "j-...",
 "data": {
   "idx": 7,
   "size_on_disk": 3145728,
   "size_expected": 10737418240,
   "speed_bps": 25165824,
   "bytes_this_window": 786432,
   "window_ms": 31250,
   "state": "ACTIVE",          // PartStatus value
   "verify": "UNVERIFIED",     // VerifyState value
   "error": null
 }}
```
Throttled to one per part per ~1.5 s. This is what draws the progress bars.

### `part_done` — terminal for a part
```json
{"seq": 124, "kind": "part_done", "archive_id": "j-...",
 "data": {"idx": 7, "size_on_disk": 10737418240, "size_expected": 10737418240,
          "verify": "STRUCT_OK", "sha256": null, "state": "DONE"}}
```

### `part_error` — a part failed (with the reason)
```json
{"seq": 125, "kind": "part_error", "archive_id": "j-...",
 "data": {"idx": 11, "outcome": "NETWORK_ERROR", "attempts_left": 3,
          "state": "PARTIAL", "error": "NETWORK_ERROR after 12.4s"}}
```

### `job_status` — coarse state (already exists in §00)
### `attempt_spent` — the ledger audit trail (already exists in §00)
### `heartbeat` — idle signal every 10 s (already exists in §00)

---

## 4. What the extension popup MUST render (the user's ask)

A per-part table where every cell comes from the events:

```
┌ takeout2 · braincreation / 2026-06-16-04-01-04 ──────────────┐
│ 55/62 done · 96.9% · 247 MB/s agg · ~11 min · budget ⚠ 1    │
│ ─────────────────────────────────────────────────────────── │
│ part  state  progress ▓▓▓▓▓▓▓░░░  on-disk    speed    err  │
│   7   ACTIVE ▓▓▓▓▓▓▓▓▓░░ 31%     3.2/10 GB  24 MB/s  —     │
│  11   ACTIVE ▓▓▓▓▓▓▓▓▓▓▓ 99%     9.9/10 GB  88 MB/s  —     │
│  13   PARTIAL  —         (stalled 47 s)  ⏳                 │
│  42   DONE   ▓▓▓▓▓▓▓▓▓▓▓ 100%    10.0/10 GB  —       ✓     │
│  61   ERROR   —         (3 attempts left)  ✖ NETWORK_ERROR │
│ ─────────────────────────────────────────────────────────── │
│ budget: 1 part ⚠ last attempt · 0 exhausted · cookie 42 s   │
└─────────────────────────────────────────────────────────────┘
```

Rules:
- **All in one popup** — no separate monitor needed for the operator.
- Progress % = `size_on_disk / size_expected` from `part_progress`.
- Speed = `speed_bps` from the event (human-formatted: B/s → KB/s → MB/s → GB/s).
- Stalled = no `part_progress` for a part in 90 s → `⏳ stalled Ns` (CLI stall rule, mirrored here).
- Errors = `part_error.error` + `attempts_left` shown inline; red row.
- Budget line always visible (already in v4.2's `renderV2Budget`; keep it).
- Identity header (label + export_ts) from the job snapshot.

## 5. What the monitor page MUST render

Same table + the job list so the operator can switch jobs, plus a connected
indicator and SSE reconnection with `?since=`. One static HTML+JS file at
`manager/web/monitor.html` (served at `/ui/monitor.html`). No build step, no
framework — vanilla JS + `fetch`/`EventSource` fallback.

## 6. SSE consumption (both surfaces)

```
1. connect  /api/v2/jobs/{archive_id}/events?since=<cursor>
2. on each event: apply (part_progress -> update row; part_done -> row done;
   part_error -> row error; job_status -> header; heartbeat -> reset stall timer)
3. on close/error: reconnect with the LAST seq seen (lossless by construction)
4. fallback: if SSE fails twice, poll /api/v2/jobs/{id}?parts=1 every 2 s
```

## 7. Invariants

1. `part_progress` never fires more than once per part per ~1.5 s.
2. Speed is computed from chunk deltas, never from `stat()`.
3. Both surfaces render ONLY from events/polls — no parallel data source.
4. Reconnect always resumes with `?since=<last_seq>` — no duplicate rows.
5. The engine never blocks on a slow consumer (emit is fire-and-forget).
