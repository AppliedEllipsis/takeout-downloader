# 05 — PARALLELISM & THROUGHPUT

> Companion to `00-CONTRACTS.md` §5.3. How to move terabytes fast **without
> spending attempts**. Read `01-IDENTITY-AND-SCRAPE.md` §7 first — several
> numbers here are marked UNMEASURED and that doc explains how to measure them.

---

## 1. Why concurrency here has a strange shape

Three measured facts (from `docs/webgui/14-resume-cookies-multiaccount.md`)
determine the entire design:

| Fact | Consequence |
|------|-------------|
| The cookie dies after **~1–2 min idle** | You cannot leisurely prepare work between requests |
| An **in-flight stream survives for hours** (auth checked only at request start) | Once started, a stream is safe — 2.8 TB moved over ~6 h on one session |
| Each part allows **~5 download attempts total** | A failed start is expensive and sometimes unrecoverable |

Together these produce a counter-intuitive rule:

> **Starting a stream is dangerous and time-critical. Running a stream is safe
> and unlimited.**

So the goal is not "maximize concurrent throughput" in the usual sense. It is:
**get as many streams as possible STARTED inside one narrow window, then leave
them alone for as long as they want to run.**

### 1.1 The v1 anti-pattern

```
capture cookie → discover 63 parts (63 probes, 30-60 s) → cookie now dead
   → first real request fails → recapture → discover again → ...
```
This livelock never moved a byte and burned probes on every lap.

### 1.2 The v2 burst window

```
   t=0     pull LIVE cookie jar over CDP                    [FREE]
   t<20s   open N streams, all reserved in the ledger       [N × PAYLOAD]
   t>20s   streams run for hours; cookie may rotate freely
```

Everything expensive (planning, scanning, verification) happens **before**
`t=0`, from local state only.

---

## 2. The scheduler

### 2.1 State machine

```
        ┌──────────────┐
        │ PLANNING     │  local only: scandir + state.db, no network
        └──────┬───────┘
               │ work set computed
               ▼
        ┌──────────────┐   cookie stale / dead
        │ COOKIE_FRESH │◄──────────────────────┐
        └──────┬───────┘                       │
               │ jar pulled (age < 5 s)        │
               ▼                               │
        ┌──────────────┐  preflight > budget   │
        │ BURST_OPEN   │──────────────────────►┤
        └──────┬───────┘  (abort, re-pull)     │
               │ N streams started             │
               ▼                               │
        ┌──────────────┐                       │
        │ STREAMING    │  hours; no re-auth    │
        └──────┬───────┘                       │
               │ a stream dies                 │
               └───────────────────────────────┘
                 never retry into a stale cookie
```

### 2.2 Pseudocode (normative)

```python
def run_burst(store, ledger, cookie_source, archive_id, n=4):
    # --- PHASE 1: plan from local state only. No network. No per-part stat. ---
    on_disk = scan_parts_dir(parts_dir)          # ONE scandir
    work = [p for p in store.list_parts(archive_id)
            if p.status in (PENDING, PARTIAL)
            and ledger.budget_for(archive_id, p.idx).spendable > 0]
    work.sort(key=lambda p: p.remaining_bytes or 0)   # see §2.3

    if not work:
        return

    # --- PHASE 2: reserve BEFORE touching the network -----------------------
    batch = work[:n]
    reservations = [ledger.reserve(archive_id, p.idx, CostClass.PAYLOAD)
                    for p in batch]

    # --- PHASE 3: fresh cookie, then start everything fast ------------------
    t0 = monotonic()
    cookie = cookie_source.pull_live_jar()        # FREE (our own Chrome, CDP)
    for part, res in zip(batch, reservations):
        if monotonic() - t0 > COOKIE_BUDGET_S:    # 20 s default
            res.release("preflight exceeded cookie budget; never sent")
            continue
        start_stream(part, cookie, res)           # non-blocking

    # --- PHASE 4: let them run. Do NOT re-auth, do NOT poll Google. ---------
    await_all(streams)
```

### 2.3 Ordering: smallest-remaining-first

Order the batch by **remaining** bytes ascending. Rationale:

- Parts nearly finished complete soonest → frees a slot → more parts started
  per cookie window.
- A part with 200 MB left that fails costs the same one attempt as a part with
  10 GB left, but risks far fewer wasted bytes.
- Finishing parts converts `PARTIAL` (fragile, resumable, attempt-consuming) to
  `DONE` (safe forever) as quickly as possible.

**Exception:** if any part is at `remaining == 1` attempt, schedule it FIRST
and alone, with maximum care — see §6.

### 2.4 When a stream dies

```
stream dies
  ├─ bytes were gained?  → mark PARTIAL, keep the bytes, do NOT retry now
  └─ zero bytes gained?  → classify the response
        ├─ AUTH_*        → job → NEEDS_COOKIE, wait for fresh capture
        ├─ LIMIT_EXCEEDED→ part → BUDGET_EXHAUSTED, alert operator
        └─ NETWORK_ERROR → eligible for the NEXT burst, never an instant retry
```

**Never instant-retry.** An immediate retry runs against the same possibly-dead
cookie and simply burns a second attempt to learn what you already knew.

---

## 3. Choosing N (concurrent streams)

### 3.1 The constraints

| Constraint | Value | Effect on N |
|-----------|-------|-------------|
| vCPU | **2** (Hetzner) | TLS + FUSE writeback compete for CPU; high N thrashes |
| RAM | **7.6 GiB** | buffers × N must stay far below this |
| Destination | JuiceFS FUSE, 1.12 PB | high per-syscall overhead; concurrent writers amplify |
| Root disk | **75 GB**, has filled before | never stage more than a couple of parts locally |
| Attempt exposure | 5/part | N streams = N attempts at risk simultaneously if the cookie is bad |

### 3.2 The attempt-exposure argument (the one people miss)

If the cookie is dead and you start N streams, **all N fail and you lose N
attempts** — one per part. With N=8 that is 8 of your ~5-per-part budget spread
across 8 parts in a single bad second.

Mitigation, mandatory: **canary the burst.**

```
1. Start ONE stream. Wait for first bytes (or classification).  [1 attempt]
2. If it yields real ZIP bytes → the cookie is good → start the remaining N-1.
3. If it returns AUTH_* → stop. You lost 1 attempt, not N.
```

This converts a possible N-attempt loss into a guaranteed 1-attempt probe, using
a request that is doing useful work anyway. **Always canary.**

### 3.3 Recommendation

| N | Verdict |
|---|---------|
| 1 | Too slow for multi-TB; no failure isolation benefit over canary+3 |
| **4** | **DEFAULT.** Good throughput on 2 vCPU; modest attempt exposure; FUSE stays healthy |
| 6–8 | Only if measured goodput actually improves AND FUSE latency stays flat |
| >8 | Do not. CPU-bound on 2 vCPU; FUSE write amplification; large attempt exposure |

Tuning procedure (run on a throwaway export, see §9):
1. Fix chunk size at 8 MiB.
2. Run N=2, 4, 6, 8 for 10 minutes each; record aggregate goodput and p95 write latency.
3. Choose the largest N where goodput still rises **and** p95 write latency has not doubled.

### 3.4 Worked example — is N=4 enough?

Assumption (**UNMEASURED — verify on the server with `iperf3` or a single-stream test**): 1 Gbit/s link ≈ 125 MB/s aggregate.

```
Export:      62 parts × 10 GB   = 620 GB
At 125 MB/s aggregate           = 620e9 / 125e6 ≈ 4,960 s ≈ 1.4 hours
```
If the link is the bottleneck, **N above ~4 buys nothing** — 4 streams already
saturate 1 Gbit if each sustains ~31 MB/s. The real reason for N>1 is not
bandwidth but **latency hiding**: a stalled or slow part must not idle the link.

Per-stream throughput is the number to measure first. If Google throttles a
single connection to, say, 20 MB/s, then N=4 → 80 MB/s and raising N helps
until the link saturates. **Measure single-stream throughput before tuning N.**

---

## 4. Within-part parallelism (multi-connection Range) — DO NOT, pending measurement

`aria2c -x16` style multi-connection downloading is the standard trick for
speed. **It is potentially catastrophic here.**

If each Range connection is counted by Google as a separate download attempt,
then `-x5` on one part **exhausts that part's entire budget in a single run**.

| | If connections cost 1 attempt each | If the whole download costs 1 |
|---|---|---|
| `-x5` on a part | budget gone instantly, archive may be unrecoverable | 5× faster |

**Ruling: FORBIDDEN by default.** One connection per part. Set
`TK2_WITHIN_PART_CONNECTIONS=1` and do not raise it until the experiment in
`01-IDENTITY-AND-SCRAPE.md` §7 has measured the cost, using Google's own
`dl_count` scrape as the oracle:

```
scrape dl_count for part P            -> before
download P with exactly 3 Range connections
re-scrape dl_count                    -> after
cost = after - before      # 1 => safe to parallelize;  3 => never do this
```

Until that number is in `ATTEMPT-COST-FINDINGS.md`, assume the worst.

---

## 5. Engine choice

| Engine | Request control | Resume | Observability | CPU | Verdict |
|--------|----------------|--------|---------------|-----|---------|
| `aria2c` | **Poor** — internal retries and per-file connection splitting can silently issue extra requests | good | RPC | low | **REJECT** — hidden retries can burn attempts without a reservation |
| `curl -C -` | good, but `--retry` must be off | good | parse stderr | low | Acceptable as a manual fallback (see runbook) |
| **Python `requests`/`httpx` streaming** | **Total** — exactly one request per call, we own every byte | explicit `Range` | native, per-chunk | moderate | **CHOSEN** |

**Decision: pure-Python streaming.**

The deciding factor is not speed, it is *auditability*. We must be able to
guarantee "one reservation = one request". Any library that retries internally
breaks the ledger invariant and can burn an archive silently.

### 5.1 Mandatory settings

```python
# Disable ALL automatic retry behaviour — a retry we did not reserve is a
# ledger violation and a possibly-burned attempt.
adapter = HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0,
                                        redirect=0, status=0))
session.mount("https://", adapter)

# Do NOT follow redirects: a 302 to accounts.google.com is a DEAD COOKIE
# signal we must classify, not silently chase into an HTML login page.
resp = session.get(url, headers=headers, stream=True,
                   allow_redirects=False, timeout=(10, 300))
```

> Following the redirect is exactly what made `curl -C -` report the
> nonsensical "server does not seem to support byte ranges" — it had chased a
> login page. `classify.py` handles this correctly; do not defeat it.

### 5.2 Resume

```python
if bytes_on_disk > 0:
    headers["Range"] = f"bytes={bytes_on_disk}-"
    expect 206  # a 200 means the server IGNORED Range -> truncate & restart,
                # never append (that produces the "oversized" corruption
                # verify.py detects)
```

---

## 6. Parts with one attempt left

A part at `remaining == 1` is a one-shot. Treat it specially:

1. Schedule it **alone**, never inside a multi-stream burst.
2. Pull the cookie and immediately canary-verify it on a **different, healthy**
   part first, so the last attempt is only spent against a proven-good cookie.
3. Require an explicit operator confirmation (`--force`) — see the danger
   prompt in `03-UX-AND-OBSERVABILITY.md` §4.
4. If it fails, park in `BUDGET_EXHAUSTED` and alert. Do not auto-retry.

---

## 7. I/O path on JuiceFS

| Setting | Value | Why |
|---------|-------|-----|
| Write chunk | **8 MiB** | Large enough to amortise FUSE syscall overhead; small enough for responsive progress |
| `fsync` per chunk | **NO** | Murders FUSE throughput; the archive is re-downloadable, the FS is not a database |
| `fsync` at part completion | **YES, once** | Durability at the only point that matters |
| Preallocation | No | JuiceFS/FUSE gains nothing; wastes a pass |
| Per-part `stat()` pre-pass | **FORBIDDEN** | 58 parts took ~6 min and idled the cookie to death. Use ONE `scan_parts_dir()` |
| Local staging on the 300 GB LUKS disk | **NO** by default | Doubles I/O; the 75 GB root has filled before. Only consider it if FUSE writes prove to be the bottleneck |

### 7.1 Memory budget

```
per stream ≈ 8 MiB write buffer + ~1 MiB TLS/HTTP overhead ≈ 9 MiB
N=4  →  ~36 MiB      (trivial on 7.6 GiB)
N=8  →  ~72 MiB      (still trivial)
```
**Memory is not the limit — CPU and FUSE are.** Do not raise N thinking RAM is
spare capacity.

---

## 8. Measuring what is actually happening

Log per chunk (to the structured log, not the event table):

```json
{"idx": 7, "bytes": 8388608, "wall_ms": 71, "net_ms": 66, "write_ms": 5}
```

Diagnosis table:

| Symptom | Reading | Cause |
|---------|---------|-------|
| `net_ms` high, `write_ms` low | Google/network bound | Raising N may help |
| `write_ms` high, `net_ms` low | **FUSE bound** | LOWER N; check JuiceFS health |
| both low, throughput still poor | per-connection throttle | Only within-part parallelism would help — see §4, currently forbidden |
| periodic `write_ms` spikes | FUSE flush stalls | Lower N; check the 75 GB root disk |

Goodput = bytes that ended up in a `DONE` part ÷ wall time. Bytes downloaded
into a part that later failed verification are **not** goodput.

---

## 9. Benchmark plan (throwaway export only)

| # | Experiment | Attempt cost | Answers |
|---|-----------|--------------|---------|
| 1 | Single stream, 1 part, full download | 1 | baseline per-stream MB/s; is there a per-connection cap? |
| 2 | N = 2/4/6/8, 10 min each | 1 per part touched | goodput curve, FUSE p95 latency → pick N |
| 3 | Range probe, then re-scrape `dl_count` | 1 (measured) | **does a probe cost an attempt?** |
| 4 | Abort a GET after 1 MiB, re-scrape | 1 (measured) | does an aborted GET cost an attempt? |
| 5 | 3 Range connections on one part, re-scrape | 1 or 3 (that's the question) | is within-part parallelism survivable? |
| 6 | Resume a PARTIAL with `Range`, re-scrape | 1 (measured) | **does resuming cost a fresh attempt?** ← most valuable |

Experiment 6 is the highest-value measurement in the project: if resuming is
free or cheap, long downloads become robust; if every resume costs an attempt,
then a part may only be interrupted ~4 times ever, and N must drop while
per-stream reliability becomes paramount.

Record every result in `docs/v2/ATTEMPT-COST-FINDINGS.md` with sample sizes.
Until then, the conservative assumptions in `00-CONTRACTS.md` §1.2 hold.

---

## 10. Invariants

1. One reservation = exactly one HTTP request. No library-internal retries.
2. Never follow redirects; a 302 is a classification signal.
3. Always canary a burst with a single stream before opening the rest.
4. Never instant-retry a failed stream into the same cookie.
5. Within-part parallelism stays at 1 until measured.
6. One `scandir` per burst, never a per-part `stat` loop.
7. Nothing between the cookie pull and the first byte may exceed `TK2_COOKIE_BUDGET_MS`.
8. A part with 1 attempt left is scheduled alone, after the cookie is proven good.
