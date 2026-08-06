# 02 — BUILD PLAN A (Phases 0–5)

> Scope: scaffolding → `state.py` → `plan.py` → `cookie.py` → `engine.py`.
> Authority: `00-CONTRACTS.md` and `01-IDENTITY-AND-SCRAPE.md` outrank this file.
> If this plan appears to contradict them, STOP and report — do not improvise.
>
> Phases 6+ (api.py, cli.py, extension side-channel, attempt-cost study) are
> **out of scope here** and live in a later plan document.

---

## 0.0 Global rules that bind every phase

| # | Rule | Enforcement |
|---|------|-------------|
| G1 | No Google-host request without `AttemptLedger.reserve()` first | grep audit + unit test asserting the ledger row exists before transport is invoked |
| G2 | **FORBIDDEN:** full-file reads in any default path (`zipfile.testzip`, `f.read()` of a part, `hashlib` over a part) | `HASH_OK` only via explicit opt-in; test asserts bytes-read counter |
| G3 | **FORBIDDEN:** per-part `stat()` loops | exactly one `verify.scan_parts_dir()` per parts dir per pass |
| G4 | **FORBIDDEN:** any network I/O in tests | no `requests`/`httpx`/`socket` to a non-loopback host; fakes only |
| G5 | Discovery never sits between a fresh cookie and the first payload byte | engine ordering test |
| G6 | Every state mutation goes through `state.py` so an event is emitted | no module other than `state.py` executes `INSERT`/`UPDATE` on `job`/`part` |
| G7 | `archive_id` is the only job-matching key | never key on path or label |
| G8 | Fail closed on ambiguity: assume an attempt was consumed | `reconcile_orphans()` on every startup |

### 0.1 Test isolation contract (all phases)

* Every test uses `tmp_path` and an **in-memory or tmp-file SQLite** connection.
* Any Google host is represented by a **fake transport object** injected via
  constructor argument — never monkeypatched at module level.
* CDP is represented by a **loopback fake websocket server** on an ephemeral
  port (Phase 4). `127.0.0.1` is the only permitted network destination.
* Wall-clock is injected as a `clock: Callable[[], float]` argument so burst
  window tests are deterministic. No `time.sleep()` longer than 10 ms in tests.

---

# PHASE 0 — Scaffolding & dev workflow

**Goal.** Make `python -m pytest tests/v2/ -q` a single, fast, hermetic gate that
every later phase extends.

### Files touched

| Path | Action |
|------|--------|
| `D:/_projects/takeout_downloader_script/pytest.ini` | create (or extend if present) |
| `D:/_projects/takeout_downloader_script/tests/v2/conftest.py` | create |
| `D:/_projects/takeout_downloader_script/tests/v2/fixtures/__init__.py` | create |
| `D:/_projects/takeout_downloader_script/tests/v2/fixtures/capture_payload.json` | create |
| `D:/_projects/takeout_downloader_script/tests/v2/fixtures/http/*.json` | create (recorded responses) |
| `D:/_projects/takeout_downloader_script/requirements-dev.txt` | append `pytest`, `pytest-timeout` |
| `D:/_projects/takeout_downloader_script/takeout2/__init__.py` | exists — leave alone |

### Implementation steps

1. Create `pytest.ini` at repo root:

   ```ini
   [pytest]
   testpaths = tests
   python_files = test_*.py
   addopts = -q --timeout=30
   filterwarnings = error::DeprecationWarning
   markers =
       slow: excluded from the default v2 gate
   ```

   `--timeout=30` is a **network tripwire**: a test that tries to reach the
   internet hangs and fails instead of silently passing on a good day.

2. Create `tests/v2/conftest.py` with these fixtures (names are normative —
   later phases depend on them):

   | Fixture | Yields | Notes |
   |---------|--------|-------|
   | `db_conn` | `sqlite3.Connection` on `tmp_path/state.db` | `row_factory = sqlite3.Row`, `check_same_thread=False` |
   | `ledger` | `AttemptLedger(db_conn, budget=5, reserve=1)` | already-built module |
   | `store` | `JobStore(db_conn)` | Phase 2 onward |
   | `fake_clock` | mutable `Clock` with `.now`, `.advance(dt)` | deterministic burst tests |
   | `parts_dir` | `tmp_path/'parts'` (created) | for `scan_parts_dir` |
   | `capture_payload` | parsed `fixtures/capture_payload.json` | Phase 3 |
   | `fake_cdp` | running loopback fake CDP server, yields base URL | Phase 4 |
   | `fake_transport` | scripted response queue | Phase 5 |

3. Add a `make_part_file(parts_dir, name, size, *, valid_zip=True)` helper in
   `conftest.py`. It writes a **sparse-ish** file: `PK\x03\x04` head, seek to
   `size-22`, write EOCD `PK\x05\x06` + 18 zero bytes. Never write 10 GB of
   real bytes; tests use sizes like 4096.

4. Add a guard fixture, `autouse=True`, that raises if any test opens a socket
   to a non-loopback address:

   ```python
   @pytest.fixture(autouse=True)
   def _no_network(monkeypatch):
       real = socket.socket.connect
       def guard(self, addr):
           host = addr[0] if isinstance(addr, tuple) else ""
           if host not in ("127.0.0.1", "::1", "localhost"):
               raise AssertionError(f"network access attempted: {addr}")
           return real(self, addr)
       monkeypatch.setattr(socket.socket, "connect", guard)
   ```

5. Record HTTP fixtures as JSON, not as live captures:
   `{"status": 403, "headers": {...}, "first_bytes_b64": "...", "final_url": "..."}`.
   One file per branch of `classify()`. These feed Phase 5's fake transport.

6. Document the dev loop in `docs/v2/02-BUILD-PLAN-A.md` (this file) and nowhere
   else. Loop is: edit module → run the phase's single test file → run the full
   v2 gate → commit.

### Dev workflow (normative)

```bash
# fast inner loop — one module
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/test_state.py -q

# the gate — must be green before every commit
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/ -q

# audit invariants G1/G2/G3 (should print nothing)
cd /d/_projects/takeout_downloader_script && \
  grep -rnE "testzip|\.read\(\)\s*$|os\.stat\(" takeout2/
```

### DONE-WHEN

- [ ] `pytest.ini` exists with `--timeout=30`.
- [ ] `tests/v2/conftest.py` provides all eight fixtures above.
- [ ] `tests/v2/fixtures/capture_payload.json` matches doc 01 §3 shape exactly
      (`archive_id`, `export_raw`, `account{...}`, `parts_expected`, `dl_counts`,
      `sizes`, `uris`, `captured_at`, `page_url`, `scrape_report`).
- [ ] At least one HTTP fixture per `ReasonCode` branch exists.
- [ ] The `_no_network` guard fires when deliberately violated (add a test that
      asserts the guard raises, then delete/skip it).
- [ ] The existing 88 tests still pass unchanged.

### VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/ -q
```

Expected: `88 passed` (unchanged count; Phase 0 adds fixtures, not tests) in
under ~5 s, no warnings, no skips.

### STOP RULES

* STOP if adding `pytest.ini` breaks the pre-existing `tests/` (v1) suite —
  scope `testpaths` narrower rather than deleting v1 tests.
* STOP if any fixture needs a real Chrome, a real Google cookie, or the network.
* STOP if you feel the need to add `pytest-asyncio`, `responses`, `vcrpy`, or
  any new runtime dependency. Fakes are hand-written stdlib objects.

---

# PHASE 1 — STATUS: **DONE** (do not re-implement)

`contracts.py`, `classify.py`, `ledger.py`, `verify.py` are built, tested, and
green (88 tests). Phases 2–5 **consume** them. This section exists only so
later phases call the real API instead of inventing one.

### 1.1 `takeout2/contracts.py` — vocabulary, zero logic

| Symbol | Kind | Signature / values |
|--------|------|--------------------|
| `CostClass` | enum | `PAYLOAD` `PROBE` `FREE`; property `counts_against_budget -> bool` |
| `JobStatus` | enum | `DISCOVERING READY DOWNLOADING PAUSED NEEDS_COOKIE BUDGET_EXHAUSTED VERIFYING COMPLETE FAILED` |
| `PartStatus` | enum | `PENDING ACTIVE PARTIAL DONE FAILED SKIPPED BUDGET_EXHAUSTED` |
| `VerifyState` | enum | `UNVERIFIED SIZE_OK STRUCT_OK HASH_OK CORRUPT`; property `rank -> int` |
| `ReasonCode` | enum | `OK_COMPLETE OK_PARTIAL AUTH_REDIRECT AUTH_401 LIMIT_EXCEEDED NOT_FOUND END_OF_RANGE RATE_LIMITED NETWORK_ERROR DISK_ERROR ABORTED` |
| `LabelSource` | enum | `UNKNOWN GAIA_FALLBACK SCRAPED_LABEL SCRAPED_EMAIL OPERATOR_OVERRIDE`; `.rank`, `.outranks(other) -> bool` |
| `Confidence` | enum | `LOWEST LOW MEDIUM HIGH` |
| `TERMINAL_JOB_STATES` | frozenset | `{COMPLETE, FAILED}` |
| `TERMINAL_PART_STATES` | frozenset | `{DONE, SKIPPED}` |
| `RETRYABLE_REASONS` / `AUTH_REASONS` / `FATAL_REASONS` | frozensets | scheduler decision sets |
| `EXPORT_TS_RE` | regex | `(\d{8}T\d{6}Z)` |
| `TAKEOUT_FILENAME_RE` | regex | `takeout-(\d{8}T\d{6}Z)-\d+-(\d+)\.zip` (group 2 = part number) |
| `DL_COUNT_RE` | regex | the "Number of times already downloaded: N" scrape |
| `sanitize_label(raw) -> str` | fn | folder-safe label |
| `sanitize_segment(raw) -> str` | fn | folder-safe path segment |
| `format_export_ts(raw) -> str` | fn | `20260616T040104Z` → `2026-06-16-04-01-04` |
| `parse_export_ts(filenames) -> str \| None` | fn | **majority** vote, not first match |
| `AccountIdentity` | frozen dataclass | `(gaia_user, authuser='0', email=None, label=None, label_source=UNKNOWN)`; `.confidence`, `.folder_name()`, `.upgrades_over(other) -> bool` |
| `PartPlan` | dataclass | `(idx, filename=None, url=None, size_expected=None, dl_count_remote=None)`; `.remaining_attempts(budget, local_used=0)` |
| `IdentityRecord` | dataclass | `(archive_id, export_raw, account, parts_expected=None, captured_at=None, page_url=None, export_ts_ambiguous=False, scrape_report=[])`; `.export_ts`, `.relative_dir()` |
| `AttemptCost` | frozen dataclass | `(action, observed_delta, samples)`; `.is_free` |
| `DEFAULTS` | dict | `ATTEMPT_BUDGET=5 BUDGET_RESERVE=1 PARALLEL=4 COOKIE_BUDGET_MS=20000 MAX_DISCOVERY_PROBES=3 VERIFY_LEVEL='STRUCT_OK' CDP_URL='http://127.0.0.1:9222'` |

**Consumption notes.** `PartPlan.remaining_attempts` already blends
`dl_count_remote` with local usage — do not recompute that arithmetic in
`plan.py` or `engine.py`. `IdentityRecord.relative_dir()` is the *only* place
folder naming is decided; Phase 2's rename logic calls it.

### 1.2 `takeout2/classify.py` — pure function

| Symbol | Signature |
|--------|-----------|
| `ResponseFacts` | frozen dataclass `(status, headers, first_bytes=b'', final_url='', expected_partial=False, probing_end=False, transport_error=None)`; `.header(name, default='')`, `.content_type`, `.content_length` |
| `classify(facts: ResponseFacts) -> ReasonCode` | pure; order of checks is significant |
| `is_zip_magic(first_bytes) -> bool` | `PK\x03\x04` / `PK\x05\x06` |
| `looks_like_html(content_type, first_bytes) -> bool` | header + body sniff |
| `ZIP_MAGIC`, `ZIP_MAGIC_EMPTY` | byte constants |

**Consumption notes for Phase 5.** The engine must set:
* `expected_partial=True` whenever it sent a `Range` header. A `200` with
  `expected_partial=True` returns `OK_PARTIAL`, meaning *the server ignored our
  Range — restart from zero, do not append*.
* `probing_end=True` **only** during a deliberate discovery probe. This is the
  single switch that turns HTML into `END_OF_RANGE` instead of `AUTH_REDIRECT`
  (invariant §8.5). Never set it during a payload download.
* `transport_error="..."` for connection resets/timeouts → `NETWORK_ERROR`.

### 1.3 `takeout2/ledger.py` — the budget gate

| Symbol | Signature |
|--------|-----------|
| `AttemptLedger(conn, budget=5, reserve=1)` | creates its own `attempt` + `remote_count` tables |
| `.reserve(archive_id, idx, cost_class=PAYLOAD, force=False, note='') -> Reservation` | raises `BudgetExhausted` unless `force` |
| `.attempt(archive_id, idx, cost_class=PAYLOAD, force=False, note='')` | contextmanager wrapper; auto-settles |
| `.local_used(archive_id, idx) -> int` | non-FREE, non-released rows |
| `.remote_used(archive_id, idx) -> int \| None` | from `remote_count` |
| `.observe_remote(archive_id, idx, count)` | record Google's counter (FREE) |
| `.budget_for(archive_id, idx) -> PartBudget` | |
| `.reconcile_orphans() -> int` | settle dangling reservations as `ABORTED`, fail-closed |
| `.archive_totals(archive_id) -> dict` | `attempts_charged`, `bytes_moved`, `probe_attempts` |
| `.parts_at_risk(archive_id) -> list[PartBudget]` | |
| `Reservation` | `(id, archive_id, idx, cost_class)`; `.commit(outcome, bytes_moved=0, http_status=None, note='')`, `.release(note='not sent')`, `.settled`, context-manager |
| `PartBudget` | frozen; `.local_used`, `.remote_used`, `.budget`, `.reserve`, `.effective_used`, `.remaining`, `.spendable`, `.exhausted`, `.at_risk` |
| `BudgetExhausted` | `RuntimeError(archive_id, idx, used, budget, reserve)` |

**Consumption notes.**
* `effective_used = max(local, remote)` — Google wins. Never lower the ledger.
* `spendable = remaining - reserve`. Schedule against `spendable`, report
  `remaining`.
* `release()` is legitimate **only** when you are certain no packet left the
  host (aborted pre-flight). Otherwise `commit()` the real outcome.
* Exiting a `Reservation` context without committing auto-settles as consumed.

### 1.4 `takeout2/verify.py` — local only, never full-reads by default

| Symbol | Signature |
|--------|-----------|
| `scan_parts_dir(directory) -> dict[str, OnDiskPart]` | **ONE** `os.scandir`; `{}` if dir missing |
| `OnDiskPart` | frozen `(filename, path, size)` |
| `verify_part(path, size_expected=None, level=VerifyState.STRUCT_OK, sha256_expected=None) -> VerifyResult` | 2 seeks at `STRUCT_OK` |
| `iter_verify(paths, level=STRUCT_OK) -> Iterator[(path, VerifyResult)]` | lazy, streamable progress |
| `VerifyResult` | frozen `(state, size_on_disk, detail='', sha256=None)`; `.ok` (≥ `STRUCT_OK`), `.corrupt` |
| `ZIP_LOCAL_HEADER`, `EOCD_SIG`, `MAX_EOCD_SEARCH` | constants (`66 KiB` tail window) |

**Consumption notes.** `size < size_expected` returns `UNVERIFIED` with a
"resumable" detail — that is the **PARTIAL** signal, not a failure.
`size > size_expected` returns `CORRUPT` (appended to a stale file — truncate
and restart, and it costs an attempt). `HASH_OK` requires an explicit
`--deep`/`level=HASH_OK`; nothing in Phases 2–5 may pass it by default.

### DONE-WHEN (Phase 1)

- [x] `python -m pytest tests/v2/ -q` → 88 passed.
- [x] No further work. Any change here is a **contract change** requiring a doc
      update first.

### STOP RULES

* Do NOT add methods to these four modules to make a later phase easier. If a
  later phase needs a new primitive, write it in that phase's own module, or
  stop and escalate a contract amendment.

---

# PHASE 2 — `takeout2/state.py` (JobStore)

## Goal

SQLite-backed job/part/event store. `archive_id` is the ONLY job key.
Every mutation emits an event with a monotonic `seq` for resumable SSE.

## Files touched

- `takeout2/state.py` (new)
- `tests/v2/test_state.py` (new)
- `docs/v2/00-CONTRACTS.md` §2.2 is the DDL spec — copy it verbatim

## Implementation steps

1. Implement the full §2.2 schema: `job`, `part`, `attempt`, `event` tables.
2. `JobStore.open(path)`; thread-safe (single writer lock, WAL).
3. `upsert_job(identity, output_dir)` — never creates a second job for an
   archive_id; delegates identity upgrades to `maybe_upgrade_identity`.
4. `maybe_upgrade_identity` — provenance ladder per `01-IDENTITY…` §4.1:
   only STRICTLY higher rank wins; folder rename is same-mount, atomic,
   best-effort-fsync (Windows can't fsync a dir handle — see the bug fixed
   in `state.py` at 2026-08-05); job row updated to the new path.
5. `emit(kind, archive_id, **data) -> seq`; `events_since(seq)`.
6. `set_job_status`, `job_totals` (single GROUP BY query, never O(n) loop),
   `list_parts(status=…)`, `update_part(... quiet=True)` for high-frequency
   byte progress without event spam.
7. `recover()` — on restart, DOWNLOADING/DISCOVERING/VERIFYING -> NEEDS_COOKIE;
   ACTIVE parts -> PARTIAL (bytes on disk preserved).

## DONE-WHEN

- [ ] All tests in `tests/v2/test_state.py` pass (111 total across v2)
- [ ] `events_since` resumes losslessly (no dropped/duplicated seq)
- [ ] Identity upgrade renames the folder and updates the DB; downgrade is a no-op

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/test_state.py -q
# expected: all pass; the upgrade test proves a rename without a second job
```

## STOP RULES

- STOP if any code reads `manifest.json` to make a control-flow decision.
- STOP if `job_totals` iterates parts in Python — it must be one SQL query.

---

# PHASE 3 — `takeout2/plan.py` (zero-probe discovery)

## Goal

Produce the part list from local/captured data with **zero Google requests**,
per the priority ladder in `00-CONTRACTS.md` §5.1. This kills the v1 discovery
sweep that burned 63 probes and idled the cookie to death.

## Files touched

- `takeout2/plan.py` (new)
- `tests/v2/test_plan.py` (new)
- `takeout2/contracts.py` — reuse `PartPlan`, `IdentityRecord`, regexes

## Implementation steps

1. Priority 1: `parts_expected` + `uris`/`sizes`/`filenames` from the capture
   payload (Phase 7 wires this). Emit `PartPlan(idx=0..N-1)`.
2. Priority 2: persisted plan in `state.db` (`part` rows already seeded).
3. Priority 3: filename arithmetic — `takeout-<ts>-<n>-<idx>.zip` and any
   `-of-MMM-` total. Emit indices without probing.
4. Priority 4 (ONLY with `--allow-discovery-probes`): bounded exponential
   bracketing probe, every probe ledger-reserved as `PROBE`
   (`MAX_DISCOVERY_PROBES=3`), recorded as `END_OF_RANGE` when HTML past end.
5. Record which source won in `event {kind: "plan_source", source, cost_attempts}`.
6. Ingestion: `upsert_parts`, and for every filename in `dl_counts` call
   `ledger.observe_remote(archive_id, idx, count)` — this seeds the budget
   with Google's truth.

## DONE-WHEN

- [ ] A fixture payload with `parts_expected: 62` produces 62 PartPlans, 0 probes
- [ ] Without payload metadata, a persisted plan is used (re-seed is a no-op)
- [ ] Probe path only under explicit opt-in; every probe leaves a ledger row

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/test_plan.py -q
```

## STOP RULES

- STOP if the planner ever makes a network request without an explicit
  `--allow-discovery-probes` flag and a ledger reservation.
- STOP if a probe result is seeded as a real part (the phantom last index bug).

---

# PHASE 4 — `takeout2/cookie.py` (live CDP cookie jar)

## Goal

Pull the LIVE cookie jar from our own Chrome over CDP (`127.0.0.1:9222`),
never the extension's stored `lastCapture`. The live jar is what made the
manual recovery work in v1 (`14-resume-cookies-multiaccount.md` §"breakthrough").

## Files touched

- `takeout2/cookie.py` (new)
- `tests/v2/test_cookie.py` (new; fake CDP websocket server, no Chrome)

## Implementation steps

1. Connect to the browser target over the CDP websocket.
2. `Storage.getCookies`; join cookies whose domain contains `google.com` into
   a `name=value; …` Cookie header (~3.5–4.5 KB, 27 cookies, per doc 14).
3. Track `CookieState { header, pulled_at, age_s }`.
4. `is_fresh(limit_s)` — default `COOKIE_IDLE_LIMIT=90 s`; used by the engine
   to decide whether a burst needs a re-pull.
5. Handle the gotcha: CDP rejects non-localhost Host headers, so this must
   run inside the container. Raise a clear error if run from the host.

## DONE-WHEN

- [ ] Fake CDP server returns 27 cookies; jar renders exactly as the doc shows
- [ ] `is_fresh` crosses its threshold at the configured age
- [ ] Non-localhost hostname raises the container-only error

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/test_cookie.py -q
```

## STOP RULES

- STOP if this ever issues a Google request to "check" the cookie — the canary
  in Phase 5 does that with a ledger reservation.
- STOP if the jar is built from anything but the live CDP call.

---

# PHASE 5 — `takeout2/engine.py` (cookie-window burst scheduler)

## Goal

Implement the burst scheduler exactly as specified in `00-CONTRACTS.md` §5.3
and `05-PARALLELISM-AND-THROUGHPUT.md` §2. The scheduler is what turns the
cookie's idle-fragile/in-flight-stable behaviour into a reliability win.

## Files touched

- `takeout2/engine.py` (new)
- `tests/v2/test_engine.py` (new; fake cookie source + fake transport, network-free)
- `takeout2/ledger.py`, `takeout2/classify.py` — used, not modified

## Implementation steps

1. `Plan -> work set` from local state only (`scan_parts_dir` once, §7.1 of 05).
2. Filter out parts with no spendable budget; order smallest-remaining-first;
   schedule a 1-attempt-left part alone (05 §6).
3. **Canary first** (05 §3.2): start one stream; only after real ZIP bytes are
   seen, open the remaining N-1. A dead cookie costs 1 attempt, not N.
4. Each start: `ledger.reserve(...)` BEFORE the request, settle with the
   classified `ReasonCode`. Retries are forbidden inside a stream — the next
   burst re-schedules it (never an instant retry into a possibly-dead cookie).
5. Transport: `requests.Session` with `HTTPAdapter(max_retries=Retry(total=0…))`
   and `allow_redirects=False` (05 §5.1). Resume via `Range: bytes=<size>-`;
   a 200 response to a Range means truncate-and-restart, never append.
6. Per-chunk write buffer 8 MiB; fsync only at part completion (05 §7).
7. Emit `attempt_spent` events with `{outcome, attempts_left}` so the UI and
   the budget view stay honest (03 §8).

## DONE-WHEN

- [ ] Fake-transport test: burst opens N streams within `COOKIE_BUDGET_MS`
- [ ] Canary test: dead cookie costs exactly 1 attempt, not N
- [ ] Resume test: Range request, 206 path appends; 200 path truncates
- [ ] `classify` is the ONLY thing that decides outcome; engine never guesses

## VALIDATION COMMAND

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/test_engine.py -q
```

## STOP RULES

- STOP if any stream is retried inside itself — retries happen at burst level only.
- STOP if `allow_redirects` is True or retries are non-zero.
- STOP if the preflight between cookie pull and first byte exceeds `TK2_COOKIE_BUDGET_MS`.

---

# Dependency graph (what can be parallel)

```
        ┌───────────────┐
        │ contracts.py  │  DONE
        └───┬───────┬───┘
            │       │
     ┌──────▼──┐ ┌──▼───────┐
     │ state   │ │ classify │  (classify DONE)
     └────┬────┘ └────┬─────┘
          │           │
     ┌────▼────┐ ┌────▼────┐ ┌─────────┐
     │ ledger  │ │ plan    │ │ verify  │  (ledger/verify DONE)
     └────┬────┘ └────┬────┘ └────┬────┘
          └─────┬─────┴─────┬─────┘
                │           │
          ┌─────▼─────┐ ┌───▼─────┐
          │ cookie.py │ │ engine  │
          └─────┬─────┘ └───┬─────┘
                │           │
          ┌─────▼───────────▼─────┐
          │ api.py + cli.py (B §6)│
          └───────────┬───────────┘
                      │
        ┌─────────────▼─────────────┐
        │ ext (B §7) · migrate (B §8)│
        │ cost study (B §9) · cutover│
        │              (B §10)       │
        └───────────────────────────┘

Parallel now: state/ledger/plan/cookie can proceed independently once
contracts/classify/verify are green. engine depends on all four. api/cli
depends on engine + state. The extension, migration, cost study and cutover
can be prepared in parallel with everything above.
```
