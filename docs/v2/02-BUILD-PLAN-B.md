# 02 — BUILD PLAN B (Phases 6–10)

> Companion to `02-BUILD-PLAN-A.md` (phases 0–5: `state.py`, `plan.py`,
> `cookie.py`, `engine.py`). **Authoritative inputs:** `00-CONTRACTS.md`
> (esp. §6 routes, §7 config, §8 invariants) and `01-IDENTITY-AND-SCRAPE.md`
> (esp. §6 scrape fields, §7 cost probe, §9 CLI surface).
> If this file appears to contradict either, those files win — STOP and report.

**Already built and green (88 tests):** `takeout2/contracts.py`,
`takeout2/classify.py`, `takeout2/ledger.py`, `takeout2/verify.py`.
Tests live in `tests/v2/`.

**Attempt cost of this whole plan: 0 attempts — EXCEPT Phase 9, which is the
measured experiment and costs up to 15 attempts on a THROWAWAY export.**

| Phase | Deliverable | Google attempts | Network to Google? |
|-------|-------------|-----------------|--------------------|
| 6 | `takeout2/api.py`, `takeout2/cli.py` | 0 | no — fakes/fixtures |
| 7 | extension: scrape → POST wiring | 0 | page DOM only, no download request |
| 8 | v1 → `state.db` migration | 0 | no |
| 9 | attempt-cost study | **≤15, throwaway export only** | YES, deliberately |
| 10 | production cutover + rollback | 0 | no |

**Global rules for every phase below**
1. No Google-host request without `AttemptLedger.reserve()` (§8.1).
2. Every state mutation goes through `state.py` so an `event` row is emitted (§8.7).
3. `archive_id` is the only job-matching key (01 §10.1).
4. No full-file reads, no `testzip`, no per-part `stat()` loops (§8.2, §8.3).
5. Tests must pass with the network fully unavailable (Phase 9 excepted).

---

# PHASE 6 — `api.py` + `cli.py`

## Goal
Expose v2 state over HTTP with cursor-resumable SSE and paginated reads, and
give the operator a rich-based CLI that never needs the browser or the network.
**All v1 routes keep working, unchanged, in the same FastAPI app.**

## Files touched
| Path | Action |
|------|--------|
| `D:/_projects/takeout_downloader_script/takeout2/api.py` | NEW (owner: phase 6) |
| `D:/_projects/takeout_downloader_script/takeout2/cli.py` | NEW (owner: phase 6) |
| `D:/_projects/takeout_downloader_script/takeout2/__init__.py` | add `__version__`, no logic |
| `D:/_projects/takeout_downloader_script/manager/app.py` | ONE mount line only (see step 2) |
| `D:/_projects/takeout_downloader_script/tests/v2/test_api.py` | NEW |
| `D:/_projects/takeout_downloader_script/tests/v2/test_cli.py` | NEW |
| `D:/_projects/takeout_downloader_script/tests/v2/conftest.py` | NEW — shared `state.db` fixture factory |
| `D:/_projects/takeout_downloader_script/requirements.txt` | add `rich>=13`, `typer>=0.12`, `httpx` (test client) |

## Steps

1. **`api.py` skeleton — a router, not an app.** Build
   `router = APIRouter(prefix="/api/v2")` plus a factory
   `def make_app(store_factory) -> FastAPI` used only by tests. Dependency
   injection is a single callable so tests pass a temp-dir store:
   ```python
   def get_store(archive_id: str | None = None) -> JobStore: ...
   ```
   Never import `manager.*` from `takeout2.*` (dependency direction, §4).

2. **Mount into v1 app.** In `manager/app.py`, after the existing route
   definitions, append exactly:
   ```python
   try:
       from takeout2.api import router as v2_router
       app.include_router(v2_router)
   except Exception as e:  # v2 optional during rollout
       log.warning("v2 router not mounted: %s", e)
   ```
   Do not modify or reorder any v1 route. v1 paths (`/api/jobs`,
   `/api/payload`, `/api/control/*`, `/api/recipes/*`) are frozen.

3. **`GET /api/v2/jobs`** — `?limit=50&offset=0&status=`. Returns
   `{"total": N, "limit": L, "offset": O, "jobs": [summary...]}`.
   Summary = job row + aggregate counters computed in SQL
   (`COUNT(*)`, `SUM(size_on_disk)`, `SUM(size_expected)`, per-status counts).
   **Never** include the parts array. `limit` clamped to `[1, 500]`.

4. **`GET /api/v2/jobs/{archive_id}`** — job + aggregates. `?parts=1` inlines
   parts **only if `parts_expected <= 500`**; otherwise return
   `{"parts_omitted": true, "parts_url": "..."}`. 404 on unknown archive.

5. **`GET /api/v2/jobs/{archive_id}/parts`** — `?limit=100&offset=0&status=`.
   Ordered by `idx ASC`. Each row includes `attempts_used`,
   `attempts_used_remote`, `remaining`, `verify_state`, `size_on_disk`,
   `size_expected`. `remaining` comes from `ledger.budget_for()`, never
   recomputed inline.

6. **`GET /api/v2/jobs/{archive_id}/events` — SSE with `?since=<seq>`.**
   The replacement for v1's lossy 256-slot queue. Algorithm:
   ```
   a. cursor = int(since or 0)
   b. REPLAY: SELECT seq,ts,kind,payload_json FROM event
              WHERE archive_id=? AND seq > ? ORDER BY seq LIMIT 1000
              -> yield each; cursor = last seq
      repeat until the query returns < 1000 rows (catch-up loop)
   c. LIVE: poll every 500 ms with the same query bounded by cursor
   d. every 10 s with no rows: yield kind="heartbeat" with the current cursor
   e. yield on request.is_disconnected() -> break, close cursor
   ```
   Use DB polling, not an in-process pub/sub: the engine is a separate process
   and WAL mode makes the reader lock-free. Envelope must match §6 verbatim:
   ```json
   {"seq":1234,"ts":"...Z","kind":"part_progress","archive_id":"...",
    "data":{"idx":7,"done":123,"size":456,"bps":789}}
   ```
   Emit as `id: <seq>\nevent: <kind>\ndata: <json>\n\n` so a browser
   `EventSource` auto-resumes via `Last-Event-ID`. Honour that header when
   `?since` is absent.

7. **`GET /api/v2/jobs/{archive_id}/budget`** — per-part
   `{idx, filename, local, remote, effective, remaining, state}` plus
   `{"archive": {"budget": 5, "reserve": 1, "parts_at_risk": [...],
   "parts_exhausted": [...]}}`. Sourced from
   `AttemptLedger.archive_totals()` / `parts_at_risk()` — do not re-query
   `attempt` in `api.py`.

8. **`POST /api/v2/control/{pause,resume,cancel}`** — body `{"archive_id": "..."}`.
   Writes a control row/flag through `state.py` and emits a `job_status`
   event. The API never touches sockets or subprocesses; the engine polls
   the control flag. Return `{"ok": true, "status": "<JobStatus>"}`.
   409 if the transition is illegal (e.g. resume a `COMPLETE` job).

9. **`GET /api/v2/doctor`** — the preflight aggregator. Checks, each
   `{name, ok, detail, severity}`:

   | Check | Pass condition |
   |-------|----------------|
   | `storage_root` | set, exists, writable |
   | `root_disk` | `/` free > 10 GB (the 75 GB disk, 04 §0.5) |
   | `storage_free` | target mount free > 2× largest remaining part |
   | `state_db` | opens, `PRAGMA integrity_check` = ok, WAL mode on |
   | `cookie_age` | freshest cookie < 120 s old (from `cookie.py`) |
   | `cdp` | `TK2_CDP_URL/json/version` reachable — **local host only, FREE** |
   | `budget_health` | no part with `remaining <= reserve` in a non-terminal state |
   | `orphan_reservations` | `ledger.reconcile_orphans()` dry count == 0 |

   `severity ∈ {info, warn, fatal}`. HTTP is always 200; the body carries the
   verdict. `?fix=1` may only run `reconcile_orphans()` — nothing else.

10. **`POST /api/v2/capture`** — the v2 capture sink for Phase 7's payload
    (§7 defines the body). Behaviour:
    ```
    validate -> upsert job by archive_id -> upsert parts from uris/sizes
    -> ledger.observe_remote() for each dl_count
    -> reconcile per 01 §8 -> identity upgrade per 01 §4.1
    -> emit events: capture_received, identity_upgraded?, budget_warning*
    -> 200 {"archive_id","parts_expected","label","label_source",
            "reconciled":[{"idx","local","remote","action"}],"warnings":[]}
    ```
    Never 500 on a partial scrape: missing fields become `null` and land in the
    response `warnings`. Accept `X-Capture-Token` with the same semantics as v1.

11. **`cli.py` with `typer` + `rich`.** Commands, exactly per 01 §9 plus the
    plan's set:

    | Command | Behaviour | Reads |
    |---------|-----------|-------|
    | `takeout2 status [archive_id]` | one-shot table: parts done/total, bytes, budget at-risk count | local `state.db` |
    | `takeout2 watch <archive_id>` | `rich.Live` table refreshed from SSE `?since` cursor; `--api` to use HTTP, default direct DB | DB or API |
    | `takeout2 run <archive_id>` | start/resume engine; `--parallel`, `--dry-run`, `--force` | DB |
    | `takeout2 budget <archive_id>` | normative table from 01 §9; `--refresh` sets a `rescrape_requested` flag the extension polls | DB |
    | `takeout2 verify <archive_id>` | `verify.py` walk; `--deep` for `HASH_OK`; `--level SIZE_OK\|STRUCT_OK` | disk |
    | `takeout2 identity <archive_id>` | identity record + provenance + `scrape_report` table; `--set-label X` → `OPERATOR_OVERRIDE` + guarded rename (01 §4.2) | DB |
    | `takeout2 doctor` | renders `/api/v2/doctor` checks; exit 1 if any `fatal` | DB + local |
    | `takeout2 migrate ...` | Phase 8 entry point | DB + v1 files |
    | `takeout2 manifest rebuild <archive_id>` | regenerate `manifest.json` from `state.db` (§2.1) | DB |

12. **CLI must work with the API down.** Default transport is direct SQLite
    (read-only URI `file:...?mode=ro`) so `status`/`budget`/`verify` work when
    the manager is dead. `--api URL` switches to HTTP. `watch` without `--api`
    polls the `event` table with the same `since` cursor logic.

13. **`budget` output** must match 01 §9 character-for-character in column
    order: `part, filename, size, on-disk, local, google, left, state`, then
    the archive total footer line. Mark `⚠ last attempt` at
    `remaining == reserve` and `✖` at `remaining == 0`.

14. **Exit codes** (scripting contract):
    `0` ok · `1` fatal check / corrupt · `2` bad usage ·
    `3` `BUDGET_EXHAUSTED` blocked the action · `4` job not found.

15. **Tests.** `conftest.py` provides `tmp_state(tmp_path)` building a
    `state.db` with 1 job / 62 parts / mixed statuses / seeded `attempt` rows,
    plus `fake_events(n)`. Required cases:
    - pagination: `limit=10&offset=60` returns 2 rows, `total=62`
    - SSE resume: read 5 events, reconnect with `since=<5th seq>`, assert
      **zero duplicates and zero gaps** across 1000 seeded events
    - SSE heartbeat: with no new events, a `heartbeat` arrives within 11 s
      (monkeypatch the interval to 0.1 s in tests)
    - `END_OF_RANGE` never appears as `needs_cookie` in any emitted event
      (invariant §8.5) — assert over a fixture event stream
    - doctor: each check flips `ok=False` under an injected fault
    - CLI: `status`, `budget`, `verify` all succeed with the FastAPI app never
      started (proves DB-direct transport)
    - v1 regression: existing `tests/test_takeout_payload.py` and
      `tests/test_security.py` still pass after the router mount

## DONE-WHEN
- [ ] All 8 v2 routes from §6 exist and are exercised by a test.
- [ ] SSE resume proven lossless across a reconnect (no dup, no gap).
- [ ] `heartbeat` emitted on an idle stream.
- [ ] Every v1 route still returns the same shape (regression suite green).
- [ ] `api.py` imports nothing from `manager/`.
- [ ] All 9 CLI commands run against a fixture DB with no network and no API.
- [ ] `budget` output matches 01 §9 layout.
- [ ] No route computes `remaining` itself; all go through `ledger`.

## VALIDATION COMMAND
```bash
cd D:/_projects/takeout_downloader_script
python -m pytest tests/v2 tests/test_takeout_payload.py tests/test_security.py -q
python -m takeout2.cli doctor --db "$TMP/state.db"
python -m takeout2.cli budget 9f3a --db "$TMP/state.db"
```
Expected:
```
1xx passed  (88 pre-existing + new, 0 failed)
doctor: 8 checks, 8 ok            -> exit 0
archive 9f3a…  braincreation / 2026-06-16-04-01-04   [SCRAPED_EMAIL, HIGH]
part  filename                              size    on-disk  local  google  left  state
   0  takeout-…-9-000.zip                  10.0G     10.0G      1       1     4  DONE STRUCT_OK
...
                                                     ── archive total: 5 of 62 parts at risk
```

## STOP RULES
- STOP if mounting the v2 router changes ANY v1 response shape.
- STOP if the SSE resume test shows a single duplicated or missing `seq` —
  a lossy cursor is worse than v1's honest drop.
- STOP if `api.py` needs `import manager` — that is a dependency-cycle bug.
- STOP if any route or CLI command issues an outbound request to a Google host.
  Phase 6 is entirely offline.
- STOP if `doctor` needs to write anything other than via
  `reconcile_orphans()`.

<!-- CHUNK-2 -->

# PHASE 7 — Extension side-channel (dl_counts, identity, parts metadata)

## Goal

Wire the free data the extension already scrapes from the Takeout manage page
into the v2 pipeline, so discovery costs **zero attempts** and the attempt
budget is seeded from Google's own counter.

**This is the single highest-value change in the project.** `helpers/content.js`
already computes everything (line ~169: `dl_counts` = "Number of times already
downloaded: N"; line ~182: `buttonData` with `data-size`; line ~235:
`expectedParts` from "part X of N"). It just never reaches the backend.

## Files touched

- `helpers/content.js` — build a structured capture payload; multi-strategy scrapers
- `helpers/background.js` — POST the structured payload
- `helpers/popup.js` — show scrape_report + budget instead of raw counts
- `takeout2/api.py` — `POST /api/v2/capture` sink (mentioned in Plan B Phase 6)
- `tests/v2/test_extension_payload.py` — shape tests (no Chrome needed)

## Steps

1. In `content.js`, build `capturePayload()` returning EXACTLY:
   ```js
   {
     archive_id, user, authuser,
     parts_expected,          // from "part X of N" aria-labels, else button count
     uris:     { filename: uri },
     sizes:    { filename: size },         // from data-size
     dl_counts:{ filename: n },           // Google's own counter
     account:  { email, label, label_source },
     export_ts_raw,                        // regex over filenames (see contracts)
     scrape_report: [ {field, source, ok, ms} ],   // provenance for every field
     locale_warning: bool,
     captured_at: Date.now()
   }
   ```
2. Multi-strategy per field per `01-IDENTITY-AND-SCRAPE.md` §6.1: every
   scraper returns `{value, source, ok}`; a miss is `null` + reason, NEVER an
   empty string that silently becomes a folder name.
3. `dlCounts` regex stays; add numeric fallback for non-English locales
   (`locale_warning: true` when the English form misses but buttons exist).
4. Re-scrape + re-POST every 60 s while the manage page is open, on SPA
   navigation, and on manager request — costs zero attempts
   (`01-IDENTITY…` §6.2).
5. `background.js` adds the v2 capture POST (keep v1 endpoint for back-compat).
6. `api.py` `POST /api/v2/capture` validates shape, feeds `plan.py`, updates
   `ledger.observe_remote()` for every `dl_count`, and returns `{ok, archive_id}`.

## DONE-WHEN

- [ ] `curl -X POST /api/v2/capture` with a fixture payload records dl_counts in state.db
- [ ] A missing-`dl_counts` payload still creates a job (graceful degradation)
- [ ] `scrape_report` shows which strategy won for each field

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/test_extension_payload.py -q
# plus manual: open webtop, click Download on a part, inspect popup -> 'budget ⚠ part 7: last attempt'
```

## STOP RULES

- STOP if the extension issues any download request itself (it must only scrape + POST).
- STOP if a locale miss silently zeroes `dl_counts` — it must set `locale_warning`.
- STOP if `parts_expected` is ever derived by probing Google.

---

# PHASE 8 — Migrate v1 state into state.db

## Goal

Adopt existing in-progress downloads (v1 `.manager_state.json` + `manifest.json`
under an existing output dir) into v2 `state.db` with **zero re-downloads**.
Idempotent, reversible, dry-run first.

## Files touched

- `takeout2/migrate.py` (new)
- `takeout2/state.py` (reuse `upsert_job` / `upsert_parts` / `update_part`)
- `takeout2/cli.py` — `takeout2 migrate [--dry-run] [--output-dir DIR]`
- `tests/v2/test_migrate.py`

## Steps

1. Read `.manager_state.json` (v1 shape: `jobs.py` Job class) and `manifest.json`.
2. For each v1 job:
   - `archive_id` from v1 `archive_id` or the `j=` fallback (commit `1287cf7`).
   - `upsert_job` with identity from v1 meta (label provenance from `derive.py`).
   - `upsert_parts` from v1 parts, mapping `done` → `size_on_disk`, and status.
   - `attempts_used` = v1 recorded attempts (if any) else 0.
   - verify_state = v1 `zip_valid` ? STRUCT_OK : UNVERIFIED (never trust blindly).
3. **Dry-run default**: print exactly what would change, change nothing.
4. `--apply` writes state.db and renames nothing; the v1 files are left in place
   (reversible: delete `state.db` and v1 still works).
5. After apply, run `verify` — every part the migration marked DONE must pass
   STRUCT_OK locally before the job is trusted.

## DONE-WHEN

- [ ] `--dry-run` output matches the v1 files with 0 surprises
- [ ] Applying twice is a no-op (idempotent)
- [ ] `state.db` absent still leaves v1 fully functional (reversible)

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script
python -m takeout2.cli migrate --output-dir /opt/storage.jfs002/google-takeout/braincreation --dry-run
python -m takeout2.cli migrate --output-dir /opt/storage.jfs002/google-takeout/braincreation --apply
python -m takeout2.cli verify <archive_id>
```

## STOP RULES

- STOP if migration ever deletes or overwrites a v1 file.
- STOP if any part's bytes are re-fetched during migration.
- STOP if `archive_id` is derived from the label path — it comes from state or the `j=` param only.

---

# PHASE 9 — Empirical attempt-cost study (the experiment that unlocks everything)

## Goal

Measure, against **Google's own `dl_count` counter**, whether a Range probe, an
aborted GET, or a resume costs a download attempt. This answers the open
questions in `00-CONTRACTS.md` §1.2 and `05-PARALLELISM…` §4/§9.

**Must run on a THROWAWAY export. Never the real multi-TB one.**

## Files touched

- `docs/v2/ATTEMPT-COST-FINDINGS.md` (results — the deliverable)
- `takeout2/experiment.py` (small, disposable driver)
- `tests/v2/test_experiment.py` (fixture-driven, network-free)

## Protocol (per action, on one throwaway part P)

```
1. scrape dl_count for P        -> before
2. perform EXACTLY ONE controlled action:
      (a) Range: bytes=0-0 probe
      (b) GET, abort after 1 MiB
      (c) GET, abort after 5 GiB  (resume case)
      (d) full successful download
      (e) 3 concurrent Range connections (within-part parallel test)
3. wait for the page to refresh; re-scrape -> after
4. cost(action) = after - before
5. repeat each action ≥ 3 times; record min/median/max in ATTEMPT-COST-FINDINGS.md
```

If `dl_count` refuses to refresh, fall back to `attempts_used` diffing on a
fresh export where we control every request.

## Expected findings (hypotheses to confirm or refute)

| Action | If cost = 0 | If cost = 1 | Consequence |
|--------|-------------|-------------|-------------|
| Range probe | probes are FREE → canary is free | probes burn attempts → forbid non-payload probes entirely | `05` §2.3 ordering stays; `00` §1.2 flips to optimistic |
| Aborted GET | aborts are FREE → safe to abandon | aborts cost → only abort when truly stuck | schedule N smaller vs longer |
| **Resume** | resuming is FREE → PARTIAL is cheap | resuming costs → ~4 interrupts/part max | THE key number for multi-day reliability |
| 3-conn parallel | parallel is FREE → `-x3` viable | parallel = 3 attempts → NEVER | settles §4 of `05` |

## DONE-WHEN

- [ ] Every action has ≥ 3 samples with min/median/max in ATTEMPT-COST-FINDINGS.md
- [ ] The conservative assumptions in `00-CONTRACTS.md` §1.2 are confirmed or explicitly revised (with a doc update)
- [ ] `05` §4 (within-part parallelism) is re-opened only if cost measured = 0

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script
python -m takeout2.experiment --archive-id <throwaway> --actions probe abort resume parallel --samples 3
cat docs/v2/ATTEMPT-COST-FINDINGS.md
```

## STOP RULES

- STOP if this runs against the real multi-TB export. It is throwaway-only.
- STOP if `dl_count` refresh is flaky — record it, don't guess the number.
- STOP if any action touches a part that already has `dl_count >= 4`.

---

# PHASE 10 — Production cutover + rollback

## Goal

Switch the live stack to v2 without interrupting an in-progress multi-TB run
(migrate, not abandon) and with a one-command rollback.

## Environment facts

Hetzner `takeout-server` (Ubuntu 24.04, 2 vCPU/7.6 GiB), containers
`takeout-webgui` + `takeout-tunnel`, repo bind-mounted at `/work`, downloads
on the 1.12 PB JuiceFS mount, root disk 75 GB (has filled before).

## Steps

1. **Freeze + migrate**: pause the v1 job (if running), run Phase 8 migration.
2. **Feature-flag**: `TK2_ENABLED=1` env var gates v2 routes; v1 routes untouched.
3. **Rolling restart**: `git pull && docker compose -f docker-compose.webgui.yml restart webgui`.
4. **Smoke**: `curl -s http://127.0.0.1:8080/api/v2/doctor` → all checks ok;
   `curl -s http://127.0.0.1:8080/api/health` (v1) still ok.
5. **Canary run**: start ONE part of a small export, watch `watch`, confirm
   budget/cookie/SSE behave; then full run.
6. **Rollback**: `TK2_ENABLED=0` + restart returns to v1 engine; v1 state files
   were never deleted (Phase 8 left them).

## DONE-WHEN

- [ ] `doctor` passes on the live stack
- [ ] A small canary export completes with STRUCT_OK on all parts
- [ ] `TK2_ENABLED=0` + restart provably returns to v1 (rollback verified)

## VALIDATION COMMAND

```bash
ssh takeout-server 'docker exec takeout-webgui curl -s http://127.0.0.1:8080/api/v2/doctor'
ssh takeout-server 'docker logs takeout-tunnel 2>&1 | grep -oE "https://[a-z-]+\\.trycloudflare\\.com" | tail -1'
```

## STOP RULES

- STOP if v1 health fails after the restart — v2 must not break the existing stack.
- STOP if migration reports any DONE part that fails local STRUCT_OK verification.
- STOP if rollback was not verified before starting a multi-TB run.

---

# Definition of Done (whole project)

- [ ] 111+ v2 tests green, network-free
- [ ] Zero-probe discovery (no Google request to learn the part list)
- [ ] Every Google request ledger-reserved; `budget` shows local+google truth
- [ ] Identity upgrades rename folders without orphaning jobs
- [ ] `STRUCT_OK` verification only (no full-file reads in default path)
- [ ] `END_OF_RANGE` never parks a job on cookie
- [ ] SSE `?since=` resumes losslessly
- [ ] Extension re-scrapes identity + dl_counts every 60 s, zero attempts
- [ ] Empirical cost study recorded in ATTEMPT-COST-FINDINGS.md
- [ ] Rollback to v1 verified

