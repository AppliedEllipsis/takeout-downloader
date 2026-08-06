# 07 — RELIABILITY HARDENING FOR MULTI-DAY / MULTI-TB TRANSFERS (normative)

> **Companions:** `00-CONTRACTS.md` (§1.2 attempt-cost table, §5.4 verification
> prohibitions, §7 `TK2_*` config), `04-FAILURE-MODES-AND-RECOVERY.md` (the
> failure catalogue this file extends), `06-LIVE-MONITORING.md` (the events an
> operator watches these guards through).
>
> **This file never overrides `00-CONTRACTS.md`.** If a guard described here
> appears to violate a contract invariant, the contract wins and this file has a
> bug — report it, do not "fix" it locally.
>
> Implemented in `takeout2/engine.py`, `takeout2/preflight.py`,
> `takeout2/backoff.py`, `takeout2/cachewatch.py`; armed in production by the
> `TK2_*` block of `docker-compose.webgui.yml`.

---

## 0. WHY this file exists (read before the table)

The v2 engine was correct on paper and green on 212 tests, and it would still
have failed in production. The reason is that **every property that matters here
is a property of *duration and scale*, not of logic**:

| The environment | The consequence |
|-----------------|-----------------|
| 5 downloads per part, **ever** (`00-CONTRACTS.md` §1.2) | A wasted request is permanent damage. There is no retry budget to spend on learning. |
| Archives expire in **~7 days** from creation | A guard that "eventually recovers" after 6 hours of wedged transfer has already spent the resource it was protecting. |
| One job = **63 parts x 10 GB = 630 GB**, days of wall clock | Every failure mode with a probability per hour becomes a certainty. |
| Host: Hetzner Ubuntu, **2 vCPU / 7.6 GiB**, root disk **75 GB with ~15 GB free** | Local resource exhaustion takes the *server* down, not just the job. |
| Target is an **rclone FUSE mount** with `--vfs-cache-max-size 100G` | Failures present as *silence*: no error, no `ENOSPC`, just a write that never returns. |

The unifying pattern in all eight items below: **the dangerous failures are the
silent ones.** A 429 that is classified but not paced, a stream that trickles one
byte per minute, a mount that quietly became an ordinary directory, a cache that
blocks instead of erroring, a torn tail that is appended to rather than
re-fetched — none of these raise. Each either burns an irreplaceable attempt or
silently corrupts a 10 GB part discovered only at extract time, days later. These
guards exist to convert silence into a decision.

---

## 1. Summary table

Attempt-cost markers follow `04-FAILURE-MODES-AND-RECOVERY.md` §0.3.

| # | Risk | Symptom on a 3 TB run | Guard | Knob | Default | Costs an attempt? |
|---|------|----------------------|-------|------|---------|-------------------|
| 1 | `_session` shadowed the `_session()` method | Every part fails instantly with `'NoneType' object is not callable` — **after** the reservation was written | rename to `_session_obj` / `_http_session()`; `_real_fetch` now has direct tests | — (structural) | — | **Yes, 1 per part, for nothing.** Would have burned the whole 5-attempt budget of every part in minutes |
| 2 | Trickle stream never times out | One part sits `ACTIVE` for days at 0 B/s; socket read timeout `(10, 300)` never fires | no-progress watchdog raises `StallAbort`, then **exactly one** Range resume | `TK2_STALL_ABORT_S` / `TK2_STALL_RESUME` | `180` s / `1` | Yes — the wedged attempt is already spent; the capped resume may spend one more |
| 3 | rclone dies, mountpoint reverts to a root-FS directory | 10 GB part streams onto a 75 GB disk with ~15 GB free; **server goes down** | `preflight_write()` — mount check FIRST, short-circuits before free space | `TK2_REQUIRE_MOUNT` | `1` in prod (`require_mount=False` in code default) | No — refuses **before** the reservation is taken |
| 4 | 429 classified but not paced | Immediate retry into a refusal; 5-attempt budget shredded in under a second | `backoff.decide()` — `Retry-After` (clamped) else exponential + jitter | `TK2_RATE_BACKOFF` | `1` | No — the wait *prevents* spending; auth reasons stop instead of waiting |
| 5 | rclone VFS cache reaches `--vfs-cache-max-size` | Writes block **indefinitely**; indistinguishable from a hang; cookie idles out | `cachewatch` + `_wait_for_cache()` with hysteresis 0.70 / 0.85 / 0.60 | `TK2_CACHE_DIR` / `TK2_CACHE_MAX_BYTES` / `TK2_CACHE_WAIT_MAX_S` | `/var/rclone_vfs` / `107374182400` / `1800` s | No — pauses holding **no attempt and no cookie** |
| 6 | Torn tail from a killed process / partial FUSE flush | `Range: bytes={getsize()}-` appends after bytes that are not really there; corrupt 10 GB part found at extract time | `aligned_resume_offset()` rewinds to an 8 MiB boundary and re-fetches the overlap | `TK2_RESUME_REWIND` | `8388608` (`WRITE_CHUNK`) | No extra attempt — it changes *where* the resume starts, not how many are issued |
| 7 | gzip-encoding proxy on the path | `resp.raw` written undecoded → corrupt `.zip` that still passes the size check | `Accept-Encoding: identity` on every request in `_real_fetch` | — (unconditional) | — | No — but it prevents a re-download that would cost 1/part |
| 8 | `flush()` per 8 MiB chunk + reopen of a 10 GB file to `fsync` | FUSE thrash and stalls on a 2-vCPU box; `dead_s`/watchdog trips on our own I/O | periodic `os.fsync()` on the **already-open** handle | `TK2_FSYNC_INTERVAL_S` | `30.0` s | No |

---

## 2. Item 1 — the latent crash (highest value fix in this document)

**Scenario.** `BurstEngine.__init__` set `self._session = None`. The class also defined a
method `_session()` that lazily built the no-retry `requests.Session`. The attribute
shadowed the method, so `_real_fetch`'s `self._session().get(...)` raised
`TypeError: 'NoneType' object is not callable` on **first contact with a Google host**.

**Why it specifically hurts.** In `_run_one`, `ledger.reserve()` happens *before*
`self.fetch(...)` — invariant #1 of `00-CONTRACTS.md` §1.3, no request without a
reservation. So the crash landed in the generic `except Exception` handler, which commits
the reservation as `NETWORK_ERROR` with `bytes_moved=0` and increments `attempts_spent`.
Every part would consume one attempt for zero bytes, on every burst, in a loop — **the
entire 5-attempt budget of a 63-part archive destroyed in minutes, recoverable only by a
`[CATASTROPHIC]` re-export.**

**Why it was invisible.** All 212 tests at the time constructed
`BurstEngine(..., fetch=fake_fetch)`. The `fetch` seam that makes the engine testable also
made `_real_fetch` the single uncovered line of the hot path: 100 % of the scheduler logic
exercised, 0 % of the transport.

**Mechanism now.** The attribute is `self._session_obj`; the accessor is `_http_session()`,
with a comment at the assignment site stating the hazard so the name is not "tidied" back.
`tests/v2/test_engine_hardening.py` adds `TestSessionNotShadowed` — including
`test_no_none_attribute_shadows_a_method`, a structural assertion that no `None`-valued
instance attribute shares a name with a callable on the class — and
`TestRealFetchTransport`, which drives `_real_fetch` against a fake session/response and
asserts on real headers, Range behaviour, byte totals, progress callbacks and
`resp.close()`.

**Knob / default.** None; structural. **Attempt cost:** before the fix, **1 attempt per
part per burst for zero bytes**; after, none.

> ### The general lesson (applies to every injectable seam here)
>
> **An injection seam moves the risk, it does not remove it.** Where production passes
> `fetch=None` and tests pass `fetch=fake`, the default branch is the one line no test
> ever runs — and it is the line that touches the irreplaceable resource. Rules for v2:
> (1) any `x=None` parameter whose `None` branch builds the real implementation MUST
> have a test exercising the real branch with a fake at the *layer below* (fake
> `requests.Session`, not fake `fetch`); (2) a structural test must assert no instance
> attribute shadows a method; (3) the same audit applies to `_check_storage`,
> `_wait_for_cache` and `_maybe_backoff`, which import lazily inside `try/except` and
> return "allow" when the module is missing — that fail-open is deliberate (a broken
> guard must never block a healthy transfer) and therefore *also* untested by default,
> so each now has explicit tests for both branches.

---

## 3. Item 2 — the no-progress watchdog

**Scenario + why it specifically hurts.** `_real_fetch` uses `timeout=(10, 300)`: 10 s
connect, 300 s read. That read timeout only fires when a socket read returns **nothing**
for 300 s, so a stream delivering a handful of bytes every few minutes resets the timer
forever — it never times out, and at that rate it will never finish a 10 GB part either.
The wedged stream holds an attempt already reserved and committed against the part: the
part sits `ACTIVE`, `06-LIVE-MONITORING.md` renders it stalled, and nothing in the
transport layer will ever free it — while the ~7-day expiry clock keeps running. A trickle
is strictly worse than a clean disconnect, because a disconnect at least ends the attempt
and lets the next burst reschedule the part.

**Mechanism.** In the read loop of `_real_fetch`, `last_progress` is compared against
`self.config.stall_abort_s`. On breach it does a final `fh.flush()` + `os.fsync()` — the
bytes already moved are real and must survive — then raises `StallAbort(total, idle)`,
carrying `bytes_moved` and `idle_s` so the caller can price the decision. `_run_one` catches
`StallAbort` specifically (before the generic handler), commits the reservation as
`OK_PARTIAL` with the achieved byte count and note `stall abort: ...`, then calls
`_resume_after_stall()` — which re-scandirs for the true on-disk size, applies
`aligned_resume_offset()` (item 6), reserves a **new** `CostClass.PAYLOAD` attempt and issues
one Range fetch. If that second stream *also* raises `StallAbort` it commits `OK_PARTIAL`
and returns — it does not loop. A third attempt would spend a third of the part's permanent
budget on a link that has now demonstrated twice that it is not moving.
`stall_resume_attempts < 1` disables the resume entirely and returns `None`.

**Knobs.** `TK2_STALL_ABORT_S` → `EngineConfig.stall_abort_s`, default **`180.0`** s;
`TK2_STALL_RESUME` → `stall_resume_attempts`, default **`1`**. `stall_abort_s = 0` disables
the watchdog (`test_watchdog_disabled_by_zero`). The separate, coarser
`EngineConfig.stall_s = 90` / `dead_s = 600` drive operator-facing "stalled" rendering only;
they abort nothing.

**Attempt cost.** The aborted attempt was already spent — the watchdog does not create the
cost, it stops it being wasted indefinitely. The single resume is
`[COSTS 1 ATTEMPT — RESUME]`: same price as a fresh GET, but a success *finishes* the part
instead of restarting it, so it is strictly better value. Two stalls, two attempts, stop.

---

## 4. Item 3 — storage preflight (mount before space, and the subdirectory trap)

**Scenario + why it specifically hurts.** `/opt/archives` is an rclone FUSE mount. When
rclone dies the kernel does not make that path *fail* — it makes it *ordinary*. The
mountpoint reverts to a plain, empty directory on the **root filesystem**: 75 GB total,
~15 GB free, already 81 % full. The engine scandirs the parts dir, sees nothing, concludes
every part is missing, and streams a 10 GB part onto `/`. The root disk fills and the
server goes down — **this has already happened on this box.** It is not the job that dies,
it is the host: the manager, Chromium, the CDP cookie jar and every in-flight stream. Per
`04-FAILURE-MODES-AND-RECOVERY.md` §1.7/§1.17 that is **1 attempt per stream in flight**,
so with `TK2_PARALLEL=4` a local disk event permanently costs four attempts on top of the
outage.

**Mechanism.** `preflight_write(parts_dir, need_bytes, require_mount=...)`, called from
`BurstEngine._check_storage()` with `need_bytes = max(0, size_expected - have)` **before**
the ledger reservation in `_run_one`. A failure sets the part `PARTIAL` with
`storage preflight: <reason>` and returns without ever reserving.

### 4.1 WHY the mount check must short-circuit

> If the path is not a real mount, `os.statvfs()` measures the **root** disk. The root
> disk very often has "enough" free space for one part — so the free-space check would
> **wrongly PASS** and green-light precisely the write we are preventing.

So `preflight_write` evaluates `_mount_checks` / `_evaluate_mount` first and on failure
returns immediately with `checks["free_space_checked"] = False` and
`checks["short_circuited"] = "mount check failed; free space deliberately NOT probed"`. The
space check is not merely skipped — it is *recorded as skipped*, because a preflight that
blocks a multi-terabyte job must say precisely why.

### 4.2 The subtle fix: `ismount()` is only true AT the mount point

The first implementation asserted `os.path.ismount()` on the nearest existing ancestor.
Here the mount is `/opt/archives` while the engine writes to
`/opt/archives/google-takeout/<label>/<export-ts>/parts` — a *subdirectory* of a healthy
mount, where `ismount()` is always `False`. The guard therefore **blocked every legitimate
write.** A guard that blocks the happy path is worse than no guard: it gets switched off.
`enclosing_mount(path)` fixes it: start at `nearest_existing(path)` (the parts dir often does
not exist on a first run), climb until `ismount()` is true, return that mount point — or
`None` when the climb reaches the filesystem root, which is the genuine "this lives on the
root disk" answer we refuse. `_mount_checks` then sets `is_root_fs` when
`os.path.dirname(mount_at) == mount_at`, and `is_mount = mount_at is not None and not
is_root`:

| Real path | `enclosing_mount()` | Verdict |
|-----------|--------------------|---------|
| `/opt/archives/google-takeout/.../parts`, rclone alive | `/opt/archives` | **ALLOW** — subdir of a dedicated storage mount |
| same path, rclone dead | `/` | **BLOCK** — `is_root_fs`, "the archive volume is detached" |
| `C:\...\tmp\parts` (Windows runner) | `C:\` | **BLOCK** under `require_mount=True` — a drive root also satisfies `ismount()` and is not dedicated storage |

### 4.3 "Mounted" is not "alive"; and free space once trusted

A hung or freshly-remounted-empty FUSE mount still reports as mounted, so
`check_is_mount(path, sentinel=...)` optionally requires a known file to exist **and be
readable** (`fh.read(1)`, not merely `isfile`); it is anchored at the requested path, not
`nearest_existing()`, and still applies when `require_mount=False`. `check_free_space()` then
fails when `free - need_bytes < min_headroom`, with `DEFAULT_MIN_HEADROOM` = **20 GiB** —
deliberately larger than one 10 GB part so a single miscounted part cannot fill a disk even
if the estimate was wrong by 100 %; `need_bytes=0` still enforces the floor. `disk_free()`
uses `f_bavail * f_frsize`, not `f_bfree` (ext4 reserves ~5 % for root, which `f_bfree` would
count as ours), falls back to `shutil.disk_usage` without `statvfs`, and with neither returns
`ok=True` plus a `checks["limitation"]` note that headroom was NOT enforced. A probe that
*errors* fails closed.

**Knob / default.** `TK2_REQUIRE_MOUNT` → `require_mount`; code default **`False`** (dev +
Windows runners), production **`1`**. `_check_storage` fails *open* if `takeout2.preflight`
cannot be imported — a broken guard must never block a healthy transfer. **Attempt cost:
zero** — it runs before `ledger.reserve()`, so a refusal costs nothing and is repeatable.

---

## 5. Item 4 — rate-limit backoff, and the `give_up` semantics

**Scenario + why it specifically hurts.** `classify.py` correctly named a 429
`ReasonCode.RATE_LIMITED` — and nothing slept; the burst loop moved to the next iteration
and re-issued at once. Naming is not pacing. Without a policy layer the engine spends the
irreplaceable 5-attempts-per-part budget on requests Google has *already said it will
refuse* — shredding it in under a second and making the part permanently unrecoverable.
Google's 429 window is minutes, not milliseconds.

**Mechanism.** `takeout2/backoff.py` is **purely computational**: no `time.sleep`, no
sockets, no disk, no ambient randomness (jitter comes from an injectable `rand`;
`rand=None` yields the un-jittered midpoint, so the default path is reproducible).
`BurstEngine._maybe_backoff()` does the sleeping. `decide(reason, *, attempt, headers=...)`
returns a frozen `BackoffDecision(should_wait, delay_s, source, give_up, detail)`, `source`
∈ `"retry-after"` / `"exponential"` / `"none"`:

| Reason class | Members | Outcome |
|--------------|---------|---------|
| `TRANSIENT_REASONS` | `RATE_LIMITED`, `NETWORK_ERROR` (what `classify.py` returns for every 5xx) | wait: `Retry-After` if sane and positive, else exponential |
| `AUTH_REASONS` | `AUTH_REDIRECT`, `AUTH_401` | `should_wait=False`, `give_up=True` |
| `FATAL_REASONS` | `LIMIT_EXCEEDED`, `DISK_ERROR` | `should_wait=False`, `give_up=True` — operator decision |
| nothing wrong | `OK_COMPLETE`, `OK_PARTIAL`, `END_OF_RANGE`, `NOT_FOUND`, `ABORTED` | no wait, no give-up |

`parse_retry_after()` accepts both RFC 9110 forms — delta-seconds (`"120"`, and the float
spelling servers sometimes send) and HTTP-date — returning `None` when absent or
unparseable so the caller falls back to exponential, and clamping a past date to `0.0`.
`backoff_delay()` computes `min(base * 2**(attempt-1), cap)`, applies
`1 + jitter*(2*rand()-1)` (`jitter=0.2`), then re-clamps into `[0.0, cap]` — **`cap` is a
hard ceiling, not a suggestion** — with the exponent clamped at 32 so a bogus attempt
number cannot raise `OverflowError` instead of producing a delay. Defaults:
`DEFAULT_BASE_S = 30.0` (generous on purpose; an early retry costs a permanent attempt),
`DEFAULT_CAP_S = 900.0` (15 min — past that the cookie has likely idled out anyway, and a
`Retry-After: 99999` must not park the job past archive expiry), `MAX_RATE_LIMIT_RETRIES =
4` (with 5 attempts and a reserve, 4 is already the whole budget).

### 5.1 CRITICAL semantic: `give_up=True` with `should_wait=False`

The two ways to stop are **not** the same thing, and conflating them is how a dead cookie
becomes three wasted attempts:

| Signal | Meaning | Correct response |
|--------|---------|------------------|
| `should_wait=True, give_up=False` | transient; waiting can change the outcome | sleep `delay_s`, continue |
| `should_wait=False, give_up=True`, reason ∈ `AUTH_REASONS` | **"stop, not retryable"** — a dead cookie does **not** heal by waiting; sleeping 15 min and re-requesting turns one wasted attempt into two | stop the burst; park `NEEDS_COOKIE`, recapture |
| `should_wait=False, give_up=True`, `attempt > max_retries` | rate-limit **budget** exhaustion — stop *while attempts remain* so human recovery stays possible | stop the burst; operator decision |
| `should_wait=False, give_up=False` | nothing to back off from | continue |

`_maybe_backoff` mirrors this exactly and logs the give-up branch as
`"not retryable by waiting: %s"`, chosen so an operator at 3am does not mistake a dead
cookie for throttling. `backoff.py` never mutates state — the `AUTH_REASONS` path in
`_run_one` is what flips the job to `NEEDS_COOKIE`.

**Knob / default.** `TK2_RATE_BACKOFF` → `rate_limit_backoff`, default **`1`** (on).
`_maybe_backoff` fails open if `takeout2.backoff` cannot be imported or `decide()` raises.
**Attempt cost: zero, and negative in effect** — the wait *prevents* attempts being spent
on refusals. `NO_WAIT_REASONS` is the complement:
`OK_COMPLETE | OK_PARTIAL | END_OF_RANGE | NOT_FOUND | ABORTED` ∪ `AUTH_REASONS` ∪
`FATAL_REASONS`.

---

## 6. Item 5 — VFS cache backpressure (a failure with no error)

**Scenario + why it specifically hurts.** Downloads land on `/opt/archives`, an rclone FUSE
mount run with `--vfs-cache-mode full --vfs-cache-max-size 100G --cache-dir
/opt/local_cache_crypt/rclone_vfs --vfs-cache-max-age 24h`. Every byte we write goes to
that **local** cache first and is uploaded afterwards, and one job is 63 x 10 GB =
**630 GB pushed through a 100 GB cache**. This is not throughput, it is *liveness*: at the
`--vfs-cache-max-size` cap, writes to the mount **block indefinitely** — no error, no
`ENOSPC`, no retry to make. A multi-day transfer simply wedges, holding attempts, while the
cookie idles out on a mutex we cannot see, *indistinguishable from a hang*. A plain
`statvfs` tells us nothing (`/opt/local_cache_crypt` is 300 G total / 292 G free); the wall
is rclone's own accounting of its cache dir, so that is what we measure.

**Mechanism.** `measure_cache_bytes(cache_dir, max_entries=DEFAULT_MAX_ENTRIES)` walks with
recursive `os.scandir` (the dirent usually carries the size, so one pass, not a `stat()` per
file), returns `None` when the dir is absent, and is **bounded** at
`DEFAULT_MAX_ENTRIES = 200_000` — an under-report only makes us *less* likely to pause,
which is the safe direction; walking millions of inodes on a 2-vCPU box mid-transfer is not.
Per-entry `OSError` (permissions, a file rclone just evicted) is skipped, never raised.
`read_cache_status(cache_dir, max_bytes=..., measured=None)` returns a frozen
`CacheStatus(state, bytes_used, max_bytes, detail)`; `fill_ratio` yields `0.0` when
`max_bytes <= 0` (no cap configured must read as "nothing to fill", not a
`ZeroDivisionError` mid-transfer). Pass `measured=` to inject a count you already have; the
real walk should be throttled to ~every 15 s.

| `CacheState` | Trigger | Engine behaviour |
|--------------|---------|------------------|
| `OK` | ratio < `WARN_RATIO` | keep downloading |
| `WARN` | ratio >= **`WARN_RATIO = 0.70`** | keep downloading, log "downloading faster than rclone uploads" |
| `PAUSE` | ratio >= **`PAUSE_RATIO = 0.85`** | stop issuing writes, let rclone drain |
| `UNKNOWN` | cache dir absent / unmeasurable | **keep downloading** — safety rule |

`next_state(previous, status)` latches: once in `PAUSE` we stay until the cache drains to
**`RESUME_RATIO = 0.60`**, even though the raw observation already reads `WARN`/`OK`.
Without the latch the engine resumes the instant it dips below 0.85, refills, and **flaps on
every 8 MiB chunk**, giving rclone no contiguous upload window. `UNKNOWN` always wins and
releases the pause — which is the **normative safety rule: `UNKNOWN` never pauses.** An
unmeasurable cache dir means we are probably not on an rclone mount at all (laptop test,
plain-disk staging, renamed dir); blocking a transfer because we failed to find a directory
would be a self-inflicted outage, while being wrong the other way only costs protection we
never had.

`_wait_for_cache()` runs before opening each stream after the canary, loops on 30 s sleeps
logging `"upload cache N% full — pausing 30s for rclone to drain (no attempt held)"`, and
returns `False` once `waited >= cache_wait_max_s` so the burst stops rather than wedging.
The critical property: the pause happens **between** streams, holding **no reservation and
no cookie**. Nothing is spent while waiting; the next burst re-pulls a fresh cookie per
`00-CONTRACTS.md` §5.3 step 5.

### 6.1 Deployment gotcha — the guard silently no-opped

The engine runs inside `takeout-webgui`. The cache lives on the **host** at
`/opt/local_cache_crypt/rclone_vfs`, which was **not visible inside the container**.
`measure_cache_bytes` returned `None`, `read_cache_status` returned `UNKNOWN`, and by the
safety rule the guard *correctly* did nothing — armed, configured, logging nothing,
protecting nothing. Fixed by bind-mounting it **read-only** (we only measure it) at
`/var/rclone_vfs`, with `create_host_path: true`.

> **Operator rule:** a guard whose failure mode is "do nothing" must be verified by
> observing it *act*, never by the absence of complaints. See §9.2.

**Knobs / defaults.** `TK2_CACHE_DIR` → `cache_dir`, production **`/var/rclone_vfs`** (code
default `None`, which returns immediately and never blocks); `TK2_CACHE_MAX_BYTES` →
`cache_max_bytes`, **`107374182400`** (must match `--vfs-cache-max-size 100G`);
`TK2_CACHE_WAIT_MAX_S` → `cache_wait_max_s`, **`1800.0`** s. **Attempt cost: zero** —
pausing holds no attempt and no cookie, and prevents the wedge that would strand one
attempt per in-flight stream.

---

## 7. Item 6 — torn-tail resume alignment

**Scenario + why it specifically hurts.** Resume built its header from the size on disk,
`Range: bytes={os.path.getsize(target)}-`. A process killed mid-write — an OOM kill on a
7.6 GiB box, a container restart, a reboot, or a FUSE mount that only partially flushed —
leaves a **torn tail**: `getsize()` reports bytes that are not really there. Appending after
that garbage produces a part of exactly the right *length*, so it passes `SIZE_OK`; its head
magic and EOCD are both intact, so it also passes **`STRUCT_OK` — the default acceptance
bar** (`00-CONTRACTS.md` §5.4). The corruption is in the middle. `HASH_OK` is opt-in only
and full-file reads are prohibited on the FUSE mount, so **the damage surfaces at extract
time, days later, when the archive may already have expired.** That is the worst failure
shape: it looks like success.

**Mechanism.** `aligned_resume_offset(size_on_disk, rewind)` returns `0` for a
non-positive size, `size_on_disk` when `rewind <= 0`, and otherwise
`trimmed - (trimmed % rewind)` where `trimmed = size_on_disk - rewind` (clamped to `0`).
It rewinds below the reported size and floors to an aligned boundary, so the overlap is
**re-fetched** rather than trusted. `_run_one` applies it to any existing incomplete part,
`os.truncate()`s to that offset and logs `"part N: rewound X B to aligned offset Y before
resume"`; if the truncate fails it falls back to `have_raw` rather than aborting.
`_resume_after_stall()` applies the same rewind. Ordering note: the local byte check comes
**first** — if the file already verifies `STRUCT_OK`/`HASH_OK` the part is marked `DONE`
and **no attempt is spent at all**, so rewinding only happens on a part we were going to
resume anyway.

**Knob / default.** `TK2_RESUME_REWIND` → `resume_rewind`, default **`WRITE_CHUNK` =
`8 * 1024 * 1024`** (8 MiB, matching `05-PARALLELISM-AND-THROUGHPUT.md` §7).
`resume_rewind <= 0` disables rewinding.

**Attempt cost.** **No additional attempt** — it changes *where* a resume starts, not
how many requests are issued. The re-fetched overlap is at most 16 MiB of a 10 GB part
(~0.16 % of the bytes) in exchange for eliminating a silent corruption whose only
repair is a full re-download at `[COSTS 1 ATTEMPT]`. Related invariant already in
`engine.py`: a **200** response to a Range resume means the server ignored `Range`, so
the target is truncated to 0 and restarted — never appended to. That is the
"oversized" corruption `verify.py` detects.

---

## 8. Items 7 & 8 — transport and durability details

**Item 7 — `Accept-Encoding: identity`.** `_real_fetch` writes `resp.raw.read(...)` bytes
**straight to disk with no decoding** — deliberately, so we never buffer a 10 GB body and the
classifier sees the true first 4096 bytes. If anything on the path (a transparent proxy, a
middlebox, a future Google change) applied `Content-Encoding: gzip`, we would write the
**compressed** stream into a file named `.zip`: smaller than `size_expected` so it looks
`PARTIAL` and gets resumed, or at a plausible size where it **passes the size check while
being entirely unopenable**. Undetectable without a full read, which is prohibited. Every
request therefore sends `headers = {"Cookie": cookie, "Accept-Encoding": "identity"}`
(asserted by `test_requests_identity_encoding`). Unconditional — no knob, because there is no
scenario in which we want an encoded body we do not decode. Cost: zero to enforce; prevents a
re-download at 1 attempt per affected part.

**Item 8 — fsync policy.** Before: `fh.flush()` after **every 8 MiB chunk**, plus a **reopen
of the finished 10 GB file** to `fsync` it at completion. On a 2-vCPU box writing through
rclone FUSE that is ~1280 forced round-trips per 10 GB part, times four parallel streams, for
no safety gain — the bytes are already in the kernel's hands and a crash loses at most one
chunk, which the item-6 rewind re-fetches anyway. The thrash costs real throughput on a
transfer measured in days, and the added write latency can itself push a slow stream toward
the stall watchdog; reopening a 10 GB file on FUSE purely to `fsync` is gratuitous metadata
work on the filesystem whose metadata is the slow part. Now: one open handle for the whole
stream, with `fh.flush(); os.fsync(fh.fileno())` only when
`(now - last_fsync) >= self.config.fsync_interval_s`. Two other fsyncs remain, both correct —
immediately before raising `StallAbort` (so the aborted attempt's bytes survive) and once at
completion, **on the same handle, no reopen.**

**Knob / default.** `TK2_FSYNC_INTERVAL_S` → `fsync_interval_s`, **`30.0`** s. **Attempt
cost:** zero; worst case on an abrupt kill is <=30 s of unsynced bytes, which
`aligned_resume_offset()` discards and re-fetches inside the existing resume.

---

## 9. Verification

### 9.1 Mutation testing (the only proof that matters here)

A test that passes both with and against a fix proves nothing. Every guard here was
validated by **reverting the fix and requiring the suite to go red**:

| Fix reverted | Expected failure |
|--------------|------------------|
| rename `_session_obj` back to `_session` | `TestSessionNotShadowed::test_no_none_attribute_shadows_a_method` and `test_http_session_is_callable_and_cached` fail; `test_real_fetch_reaches_the_transport` raises `TypeError: 'NoneType' object is not callable` |
| remove the watchdog check | `TestStallWatchdog::test_stall_abort_raised_when_no_progress` fails; `test_partial_bytes_survive_a_stall_abort` loses its bytes |
| raise `stall_resume_attempts` above 1 | the resume-cap assertions fail |
| probe free space before the mount check | the detached-mount case returns `ok=True`; `tests/v2/test_preflight.py` fails on `short_circuited` / `free_space_checked=False` |
| replace `enclosing_mount()` with `ismount(nearest_existing(...))`, or drop the `is_root_fs` refusal | the healthy-subdirectory case BLOCKs, or a root-FS path wrongly ALLOWs |
| make `decide()` wait on `AUTH_REASONS`, or drop the `Retry-After` cap | `TestRateLimitBackoff::test_auth_failure_is_never_waited_on` fails; the clamp assertion in `tests/v2/test_backoff.py` fails |
| remove the `next_state` hysteresis latch | the pause/resume flap test fails |
| remove `Accept-Encoding: identity`, or restore flush-per-chunk / reopen-to-fsync | `test_requests_identity_encoding` fails; the fsync-interval assertions fail |

`test_no_none_attribute_shadows_a_method` deserves special mention: it does not test
behaviour, it tests **the class shape**. It is the only kind of test that would have caught
item 1 without a live network, and it will catch the next instance of the same mistake
anywhere on `BurstEngine`.

### 9.2 Suite size, and verifying on the live box

`pytest tests/v2 --collect-only` reports **382 tests**; `pytest tests --collect-only
--ignore=tests/v2` reports **199**. The v2 count grew from the pre-hardening 212, and *where*
the new tests went is the point: `tests/v2/test_engine_hardening.py`
(`TestRealFetchTransport`, `TestStallWatchdog`, `TestSessionNotShadowed`,
`TestCacheBackpressure`, `TestRateLimitBackoff`, `TestAlignedResumeOffset`,
`TestHardeningDefaults`) plus `test_preflight.py`, `test_backoff.py`, `test_cachewatch.py`.
`TestRealFetchTransport` fakes the **`requests.Session`**, one layer *below* the `fetch=`
seam — exactly the lesson from item 1. All 199 v1 tests still pass; no v1 behaviour changed.

On the live box, both checks are `[FREE]`: run `enclosing_mount()` +
`preflight_write(..., require_mount=True)` on the real production parts path inside the
container (expected `ALLOW`, §10.1), and `read_cache_status("/var/rclone_vfs").detail` to
confirm the cache is measurable. An `UNKNOWN ... not measurable` detail means the bind mount
is missing and the backpressure guard is a no-op — treat that as a **fault**, not as "no
problem detected".

---

## 10. Deployment

Armed in the `# --- v2 multi-TB reliability guards (takeout2) ---` block of
`docker-compose.webgui.yml`; consumed in `takeout2/cli.py` when it builds `EngineConfig`.

| Env var | `EngineConfig` field | Production | Code default | Meaning |
|---------|---------------------|------------|--------------|---------|
| `TK2_STALL_ABORT_S` | `stall_abort_s` | `180` | `180.0` | Seconds of zero progress before `StallAbort` |
| `TK2_STALL_RESUME` | `stall_resume_attempts` | `1` | `1` | Range resumes allowed after a stall abort |
| `TK2_RESUME_REWIND` | `resume_rewind` | unset → default | `8388608` | Bytes rewound to an aligned boundary before resume |
| `TK2_FSYNC_INTERVAL_S` | `fsync_interval_s` | unset → default | `30.0` | Minimum seconds between `fsync`s on the open handle |
| `TK2_REQUIRE_MOUNT` | `require_mount` | **`1`** | `0` / `False` | Refuse to write unless the enclosing mount is real and non-root |
| `TK2_CACHE_DIR` | `cache_dir` | **`/var/rclone_vfs`** | `None` | rclone VFS cache to measure (read-only bind) |
| `TK2_CACHE_MAX_BYTES` | `cache_max_bytes` | `107374182400` | `100 * 1024**3` | Must match `--vfs-cache-max-size 100G` |
| `TK2_CACHE_WAIT_MAX_S` | `cache_wait_max_s` | unset → default | `1800.0` | Give up on the burst after this much cache waiting |
| `TK2_RATE_BACKOFF` | `rate_limit_backoff` | `1` | `1` / `True` | Honour `Retry-After` / exponential backoff |
| `TK2_PARALLEL` | `parallel` | `4` | `4` | Concurrent payload streams (`00-CONTRACTS.md` §7) |

Boolean parsing in `cli.py` is deliberately literal: `require_mount` is true unless the
value is `"0"`, `""` or `"false"`; `rate_limit_backoff` is true unless `"0"` or `"false"`.
`TK2_CACHE_DIR=""` disables the cache guard entirely (`or None`). Item 5 additionally
requires the read-only bind of `${TK2_CACHE_HOST_DIR:-/opt/local_cache_crypt/rclone_vfs}`
to `/var/rclone_vfs` (`create_host_path: true`) — without it `TK2_CACHE_DIR` points at
nothing and the guard silently no-ops (§6.1).

### 10.1 Verified real-path result, and restart discipline

The outcome that motivated commit `0b3f3c2` ("preflight must accept subdirs of a live
mount, refuse the root FS"), measured on the production box:

```
enclosing_mount("/opt/archives/google-takeout/braincreation/2026-06-16-04-01-04/parts")
    == "/opt/archives"        ->  is_mount=True, is_root_fs=False  ->  ALLOW

enclosing_mount("<detached path>") == "/"  ->  is_root_fs=True     ->  BLOCK
  reason: "nearest mount is the root filesystem — the archive volume is detached"
  checks: free_space_checked=False,
          short_circuited="mount check failed; free space deliberately NOT probed"
```

The parts dir is four levels below the mount point and `os.path.ismount()` is `False` at
every one of those levels — the guard permits the write anyway, which is precisely why
`enclosing_mount()` climbs instead of testing in place. Both verdicts are reached with
**zero** attempts spent, before any reservation.

Applying these env vars requires `docker compose -f docker-compose.webgui.yml up -d webgui`,
which restarts Chromium. Per `04-FAILURE-MODES-AND-RECOVERY.md` §0.6 item 4, **never do this
while a download stream is live** — you lose the Google login and every in-flight stream, at
1 attempt per stream. Arm the guards between bursts, not during one.

---

## 11. Still unmeasured

Everything above is engineering against an *assumed* cost model. The model itself is still
unverified, and this section exists so no future reader mistakes conservatism for knowledge.

| Action | Assumed cost | Actually measured? |
|--------|--------------|--------------------|
| `Range: bytes=0-0` probe / HEAD | 1 | **No** |
| A GET aborted after N bytes (including our own `StallAbort`) | 1 | **No** |
| A Range resume of a `PARTIAL` | 1 | **No** |
| Parallel Range connections within one part | 1 each | **No** |

Consequences this document does **not** relax: the conservative assumptions in
**`00-CONTRACTS.md` §1.2 stay in force** (assume every request to a Google download host
costs one attempt — behaving conservatively can only waste time; behaving optimistically can
permanently burn an archive); `stall_resume_attempts` is capped at **1** precisely *because*
the resume cost is unknown (if a resume were measured free the cap could be raised and the
watchdog would get strictly better — but that is a change to make **after** the measurement);
and `max_streams_per_part` stays **1** (`EngineConfig`, asserted by
`test_within_part_parallelism_still_forbidden`) for the same reason.

**How to measure it.** `takeout2/experiment.py` is the harness. Google's manage page exposes
the counter ("Number of times already downloaded: N") and the extension already scrapes it
into `dl_counts`, which is `[SCRAPE — FREE]` per `01-IDENTITY-AND-SCRAPE.md` §6.2 — so the
cost is directly observable: read the counter, run **exactly one** controlled action, read it
again, and `after - before` is the cost (`0` => FREE, `n` => COSTS n). The study maps onto the
benchmarks in `05-PARALLELISM-AND-THROUGHPUT.md` §9: `probe` (experiment 3), `abort` (4),
`parallel` (5), `resume` (6), `full` (1, baseline, always costs 1). `dl_scrape`,
`cookie_source` and `transport_factory` are all injected, so the whole study runs offline in
`tests/v2/test_experiment.py`.

**Safety gate, non-negotiable:** `run_study` refuses to run unless `confirm_throwaway=True`.
Every action spends real Google attempts, so it may only run against a **small throwaway
export, never the production multi-TB archive**. It also gates each action on the ledger's
spendable budget and uses a fresh part index per sample, so a study can never push any part
past its ceiling. Until that study has run, treat every entry in this document's "Costs an
attempt?" column as a *worst-case assumption we have chosen to act on* — not a measurement.

<!-- APPEND-HERE -->
