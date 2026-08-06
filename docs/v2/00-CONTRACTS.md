# 00 — SHARED CONTRACTS (authoritative)

> **This file is the single source of truth for every module in v2.**
> Implementers MUST NOT invent alternative names, states, or schemas.
> If something here seems wrong, STOP and report it — do not "fix" it locally.

---

## 0. The one-paragraph problem statement

Google Takeout allows **each archive to be downloaded 5 times** (Google support,
verbatim: *"We only allow each archive to be downloaded 5 times; after that,
please request another archive"*), and archives **expire in ~7 days**. The
download cookie is **idle-fragile (~1–2 minutes)** but an **in-flight stream
survives for hours** because auth is validated only at request *start*. The v1
engine burned both budgets on non-payload work: a 1-byte `Range` probe per part
during discovery (63 probes = 63 possible attempts + ~30–60 s of cookie life),
a multi-minute JuiceFS `stat()` pre-pass, and a final `zipfile.testzip()` that
re-read every byte over FUSE.

**v2 design axiom: bytes-on-the-wire is the ONLY acceptable reason to contact a
Google host.** Every other question (how many parts? what size? is it valid?)
must be answered from local state or from bytes already on disk.

---

## 1. Attempt budget model (the core abstraction)

### 1.1 Definitions

| Term | Meaning |
|------|---------|
| `archive` | One Takeout export, identified by the stable `archive_id` (the `j=` URL param). |
| `part` | One numbered file within an archive (index `i=`), typically 10 GB. |
| `attempt` | Any HTTP request to a Google download host that Google may count against the per-archive download limit. |
| `budget` | `ATTEMPT_BUDGET_PER_PART` (default **5**, configurable, treated as a hard ceiling). |
| `reservation` | A row written to the ledger **before** a request is issued. No reservation → no request. |

### 1.2 Cost classification — MEMORIZE THIS TABLE

| Class | Examples | Assumed cost | Allowed? |
|-------|----------|--------------|----------|
| `PAYLOAD` | Full/resumed GET that transfers archive bytes | 1 attempt | YES — this is the point |
| `PROBE` | `Range: bytes=0-0`, HEAD, existence check | **1 attempt (assume worst case)** | Only with explicit `allow_probe=True` and budget headroom |
| `FREE` | Anything to a non-Google host; reading local disk; CDP calls to our own Chrome | 0 | Always |

> **Unverified assumption, deliberately conservative:** we do NOT know whether an
> aborted GET or a `Range` probe truly decrements Google's counter. We assume it
> DOES. Phase 6 contains an empirical probe to measure it on a throwaway export.
> Until measured, code must behave as if every request costs one attempt.

### 1.3 The hard rule

```
No module may call requests/httpx/curl/aria2c against a Google host
without first obtaining a Reservation from AttemptLedger.reserve().
```

A reservation that is never `commit()`ed or `release()`d is auto-reconciled as
**consumed** on next startup (fail-closed: assume the request happened).

---

## 2. Storage layout & state

### 2.1 On-disk layout (per archive)

```
<STORAGE_ROOT>/google-takeout/<account-label>/<export-ts>/
├── parts/                       # the .zip parts themselves
│   └── takeout-<...>-<NNN>.zip
├── state.db                     # SQLite — jobs, parts, attempts, events (SOURCE OF TRUTH)
├── manifest.json                # human/machine summary, regenerated from state.db
└── logs/
    └── engine.log
```

**`state.db` is the source of truth. `manifest.json` is a derived view** and may
be regenerated at any time with `takeout2 manifest rebuild`. Never parse
`manifest.json` to make control-flow decisions.

### 2.2 SQLite schema (v2 — DDL is normative, copy verbatim)

```sql
PRAGMA journal_mode=WAL;          -- concurrent reader (CLI/API) + writer (engine)
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS job (
  archive_id      TEXT PRIMARY KEY,
  account_label   TEXT NOT NULL,
  account_email   TEXT,
  gaia_user       TEXT,
  authuser        TEXT,
  export_ts       TEXT NOT NULL,
  output_dir      TEXT NOT NULL,
  parts_expected  INTEGER,
  status          TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  finished_at     TEXT,
  last_error      TEXT,
  attempt_budget  INTEGER NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS part (
  archive_id      TEXT NOT NULL,
  idx             INTEGER NOT NULL,
  filename        TEXT,
  url             TEXT,
  size_expected   INTEGER,
  size_on_disk    INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL,
  verify_state    TEXT NOT NULL DEFAULT 'UNVERIFIED',
  attempts_used   INTEGER NOT NULL DEFAULT 0,
  sha256          TEXT,
  first_started   TEXT,
  last_activity   TEXT,
  completed_at    TEXT,
  last_error      TEXT,
  PRIMARY KEY (archive_id, idx)
);

CREATE TABLE IF NOT EXISTS attempt (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id      TEXT NOT NULL,
  idx             INTEGER NOT NULL,
  cost_class      TEXT NOT NULL,        -- PAYLOAD | PROBE | FREE
  reserved_at     TEXT NOT NULL,
  settled_at      TEXT,
  outcome         TEXT,                 -- see ReasonCode enum
  bytes_moved     INTEGER NOT NULL DEFAULT 0,
  http_status     INTEGER,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS attempt_part ON attempt(archive_id, idx);

CREATE TABLE IF NOT EXISTS event (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id      TEXT,
  ts              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  payload_json    TEXT NOT NULL
);
```

> `event.seq` is the **monotonic cursor** for SSE resume. Clients send
> `?since=<seq>` and receive only newer events. This replaces v1's bounded
> 256-event queue that silently dropped events.

---

## 3. Enums (normative string values — exact spelling matters)

### 3.1 `JobStatus`
```
DISCOVERING | READY | DOWNLOADING | PAUSED | NEEDS_COOKIE
| BUDGET_EXHAUSTED | VERIFYING | COMPLETE | FAILED
```

### 3.2 `PartStatus`
```
PENDING | ACTIVE | PARTIAL | DONE | FAILED | SKIPPED | BUDGET_EXHAUSTED
```
- `PARTIAL` = bytes on disk, stream interrupted, resumable.
- `SKIPPED` = deliberately not downloaded (user filter / already satisfied).
- `BUDGET_EXHAUSTED` = attempts_used >= budget; requires human decision.

### 3.3 `VerifyState`
```
UNVERIFIED | SIZE_OK | STRUCT_OK | HASH_OK | CORRUPT
```
Escalating confidence. `STRUCT_OK` (head magic + tail EOCD) is the **default
acceptance bar**. `HASH_OK` is opt-in only.

### 3.4 `ReasonCode` — outcome of every attempt
```
OK_COMPLETE        # stream finished, size matches
OK_PARTIAL         # stream ended early, bytes gained, resumable
AUTH_REDIRECT      # 302 -> accounts.google.com/ServiceLogin  (cookie dead)
AUTH_401           # explicit 401/403 auth failure
LIMIT_EXCEEDED     # Google says download limit hit for this archive
NOT_FOUND          # 404 - part index beyond end of archive
END_OF_RANGE       # 200 text/html at index past last real part (NOT an auth error)
RATE_LIMITED       # 429 / explicit throttle
NETWORK_ERROR      # connection reset, timeout, DNS
DISK_ERROR         # local write failure, ENOSPC
ABORTED            # cancelled by operator
```

> **Critical distinction (v1 bug a531bba):** `END_OF_RANGE` and `AUTH_REDIRECT`
> both surface as HTML. They are NOT the same. `END_OF_RANGE` = clean stop of
> discovery. `AUTH_REDIRECT` = refresh cookie. Detection rule in §5.2.

---

## 4. Module layout & ownership

Each file below has exactly ONE owner during codegen. Do not edit another
module's file.

```
takeout2/
├── __init__.py
├── contracts.py      # enums, dataclasses, ReasonCode — NO logic, no imports beyond stdlib
├── ledger.py         # AttemptLedger: SQLite, reserve/commit/release, budget enforcement
├── state.py          # JobStore: job/part CRUD, event append, SSE cursor queries
├── classify.py       # HTTP response -> ReasonCode. Pure function, heavily unit-tested
├── cookie.py         # CookieSource: live CDP jar pull + freshness tracking
├── plan.py           # PartPlanner: derive part list WITHOUT network (zero-probe discovery)
├── verify.py         # local-only verification: magic, EOCD, size, optional sha256
├── engine.py         # Downloader: the cookie-window burst scheduler
├── api.py            # FastAPI: routes, SSE with ?since cursor
└── cli.py            # rich-based CLI: status, watch, run, budget, verify, doctor
```

**Dependency direction (strictly one-way, no cycles):**
```
contracts.py  <-  everything
ledger.py, state.py  <-  engine.py, api.py, cli.py
classify.py, cookie.py, plan.py, verify.py  <-  engine.py
engine.py  <-  api.py, cli.py
```

---

## 5. Key algorithms (normative pseudocode)

### 5.1 Zero-probe discovery (`plan.py`)

v1 swept `i=0..MAX_PARTS` with a 1-byte Range probe each. **v2 must not.**

Priority order for determining the part list — use the FIRST that succeeds:

1. **`expectedParts` from the capture payload** (the Takeout page DOM exposes
   it). Emit indices `0..expectedParts-1`. Cost: **0 attempts**.
2. **Persisted plan in `state.db`** from a previous run. Cost: **0 attempts**.
3. **Filename arithmetic** — the part filename template contains the total
   (`...-NNN-of-MMM.zip` style) when present. Cost: **0 attempts**.
4. **Bounded probe sweep** — ONLY if 1–3 all fail AND the operator passed
   `--allow-discovery-probes`. Probes at most `MAX_DISCOVERY_PROBES` (default 3)
   using *exponential bracketing*, not a linear sweep, and every probe is
   ledger-reserved as `PROBE`.

The planner MUST record which source it used in `event` so the operator can see
whether any attempts were spent on discovery.

### 5.2 Response classification (`classify.py`) — pure function

```python
def classify(status, headers, first_bytes, final_url) -> ReasonCode:
    # order matters
    if final_url contains "accounts.google.com" -> AUTH_REDIRECT
    if status in (401, 403):
        if body mentions download limit/quota -> LIMIT_EXCEEDED
        return AUTH_401
    if status == 429 -> RATE_LIMITED
    if status == 404 -> NOT_FOUND
    if status == 200 and content_type startswith "text/html":
        # v1's fatal ambiguity - resolve by intent, not by content alone
        return END_OF_RANGE        # caller decides if this is fatal
    if status in (200, 206) and first_bytes[:2] == b"PK" -> OK_*
    ...
```
Unit tests MUST cover every branch with recorded fixtures.

### 5.3 Cookie-window burst scheduling (`engine.py`) — THE central design

Because auth is checked only at stream *start*, and the idle cookie lives
~1–2 min, the scheduler must **start every worker inside one narrow window**:

```
1. Compute the full work set from local state ONLY (no network, no stat storm).
   Use a single os.scandir() of parts/ -- NOT one stat() per part.
2. Pull a FRESH cookie from the live CDP jar (cookie.py).  [FREE]
3. Immediately open N concurrent streams (default N=4), all within ~5 s.
   Each start is ledger-reserved as PAYLOAD.
4. Once streams are running they may run for hours; do not re-auth them.
5. If a stream dies, do NOT instantly retry (that burns an attempt on a
   possibly-dead cookie). Instead: re-pull a fresh cookie, then restart it
   inside the next burst window.
6. Never let the pre-flight between step 2 and step 3 exceed COOKIE_BUDGET_MS
   (default 20000). If it does, abort the burst and re-pull the cookie.
```

**Anti-pattern to avoid (v1's livelock):** capture → discover → cookie expires →
recapture → discover → … Discovery must never sit between a fresh cookie and
first bytes.

### 5.4 Verification (`verify.py`) — never touches the network, never full-reads

| Level | Check | Cost on a 10 GB part |
|-------|-------|----------------------|
| `SIZE_OK` | `size_on_disk == size_expected` | ~0 |
| `STRUCT_OK` | head 4 bytes == `PK\x03\x04` **and** `PK\x05\x06` (EOCD) found in last 64 KiB | 2 seeks |
| `HASH_OK` | full sha256 | full read — **opt-in only** |

> **Hard prohibition:** `zipfile.testzip()` and any full-file read are FORBIDDEN
> in the default path. On the JuiceFS FUSE mount a 3 TB testzip took hours and
> risked FUSE stalls (`docs/webgui/14-…`, "Known remaining sharp edges").
> `HASH_OK` may only be triggered by an explicit `--deep` flag.

---

## 6. HTTP API contract (`api.py`)

All v1 routes keep working. New/changed:

| Route | Method | Notes |
|-------|--------|-------|
| `/api/v2/jobs` | GET | paginated: `?limit=50&offset=0`; summaries only, never parts |
| `/api/v2/jobs/{archive_id}` | GET | job + aggregate counters; `?parts=1` to include parts |
| `/api/v2/jobs/{archive_id}/parts` | GET | paginated parts, `?status=` filter |
| `/api/v2/jobs/{archive_id}/events` | GET | **SSE with `?since=<seq>`**; replays missed events |
| `/api/v2/jobs/{archive_id}/budget` | GET | attempts used/remaining per part + archive total |
| `/api/v2/control/{pause,resume,cancel}` | POST | `{archive_id}` |
| `/api/v2/doctor` | GET | preflight: disk, cookie freshness, CDP reachable, budget health |

SSE event envelope (normative):
```json
{"seq": 1234, "ts": "2026-...Z", "kind": "part_progress",
 "archive_id": "...", "data": {"idx": 7, "done": 123, "size": 456, "bps": 789}}
```
`kind` ∈ `job_status | part_progress | part_done | attempt_spent |
needs_cookie | budget_warning | error | heartbeat`.

`heartbeat` MUST be emitted every 10 s even when idle so clients can detect
stalls (v1 had no idle signal).

---

## 7. Configuration (env vars, all prefixed `TK2_`)

| Var | Default | Meaning |
|-----|---------|---------|
| `TK2_ATTEMPT_BUDGET` | `5` | per-part attempt ceiling |
| `TK2_BUDGET_RESERVE` | `1` | keep this many attempts unspent for emergencies |
| `TK2_PARALLEL` | `4` | concurrent payload streams |
| `TK2_COOKIE_BUDGET_MS` | `20000` | max preflight time between cookie pull and first byte |
| `TK2_MAX_DISCOVERY_PROBES` | `3` | only used with `--allow-discovery-probes` |
| `TK2_VERIFY_LEVEL` | `STRUCT_OK` | default acceptance bar |
| `TK2_CDP_URL` | `http://127.0.0.1:9222` | live cookie jar source |
| `TK2_STORAGE_ROOT` | *(must be set)* | see §2.1 |

---

## 8. Non-negotiable invariants (a reviewer checks these)

1. No Google-host request without a ledger reservation.
2. No full-file read in the default verification path.
3. No per-part `stat()` loop — one `os.scandir()` per directory.
4. Discovery never sits between a fresh cookie and the first payload byte.
5. `END_OF_RANGE` never flips a job to `NEEDS_COOKIE`.
6. Job resume keys on `archive_id`, never on the label-derived output path.
7. Every state mutation goes through `state.py` (so every mutation emits an event).
8. A part at `attempts_used >= budget - reserve` must not be auto-retried;
   it parks in `BUDGET_EXHAUSTED` and raises a `budget_warning` event.
