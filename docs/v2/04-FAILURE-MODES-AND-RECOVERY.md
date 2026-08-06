# 04 — FAILURE MODES, RECOVERY & THE OPERATIONAL BIBLE

> **Audience:** you, at 3am, with 3 TB on disk, a job that is not moving, and a
> human asleep. Or a weaker model that must not improvise.
>
> **Companions:** `00-CONTRACTS.md` (enums, schema, budget model — authoritative),
> `01-IDENTITY-AND-SCRAPE.md` (identity, `dl_counts`, provenance),
> `docs/webgui/14-resume-cookies-multiaccount.md` (the original incident report),
> `docs/webgui/12-operations-runbook.md`, `docs/webgui/13-migration-diskfull.md`,
> `start.md`.
>
> **This file never overrides `00-CONTRACTS.md`.** If a command here appears to
> violate a contract invariant, the contract wins and this file has a bug — report
> it, do not "fix" it locally.

---

## 0. READ THIS BEFORE YOU TOUCH ANYTHING

### 0.1 The three facts that govern every decision

1. **Each archive can be downloaded 5 times, total, per part.** Google's own
   words: *"We only allow each archive to be downloaded 5 times; after that,
   please request another archive."* When the attempts are gone, they are gone.
   The only remedy is re-requesting the export from Google, which takes **hours
   to days** and, for a multi-TB account, may take longer than the 7-day archive
   expiry window allows you to then download it. **A wasted attempt is real,
   permanent damage.**

2. **The download cookie dies in ~1–2 minutes when idle, but an in-flight stream
   survives for hours.** Auth is validated at request *start* only. The first
   real run pulled 2.8 TB over ~6 hours on a single continuous session. This
   asymmetry is the single most exploitable property of the system: get the
   stream started, and you are safe.

3. **Archives expire ~7 days after Google creates them.** The clock is running
   from creation, not from when you started downloading. A 3 TB export at
   100 MB/s is ~8.5 hours of pure transfer; at 10 MB/s it is ~3.5 days. Do the
   arithmetic before you idle a job "until morning."

### 0.2 The one-line doctrine

> **Never contact a Google download host to answer a question you could have
> answered from local disk or from the manage-page scrape.**

Every recovery procedure in this document is ordered by that principle. See §3.

### 0.3 Attempt-cost marking convention (used throughout)

Every command in this document is marked:

| Marker | Meaning |
|--------|---------|
| `[FREE]` | Touches no Google download host. Local disk, SQLite, CDP, docker, SSH, the manage page. Costs **0** attempts. Safe to run repeatedly. |
| `[SCRAPE — FREE]` | Reads the Takeout manage page / `dl_counts`. No download request. Costs **0** attempts. Per `01-IDENTITY-AND-SCRAPE.md` §6.2, this is explicitly free. |
| `[COSTS 1 ATTEMPT]` | Issues a request to a Google download host. Assume it decrements the 5-download counter (`00-CONTRACTS.md` §1.2 — conservative until measured). |
| `[COSTS 1 ATTEMPT — RESUME]` | Same cost, but the request continues an existing PARTIAL via `Range`, so a success finishes the part instead of restarting it. Strictly better value than a fresh GET. |
| `[CATASTROPHIC]` | Consumes the archive itself or forces a re-export. Days of latency, possibly unrecoverable inside the expiry window. **Requires human sign-off, always.** |

**If a command is not marked, it is `[FREE]`.** If you are unsure whether
something is free, treat it as `[COSTS 1 ATTEMPT]` and do not run it.

### 0.4 Resolve your paths ONCE, then reuse the variables

The repo has been relocated at least twice (see `docs/webgui/13-migration-diskfull.md`)
and the docs disagree on its path. **Do not trust a hardcoded path from any doc,
including this one.** Resolve it live, at the start of every session:

```bash
# [FREE] --- run on your laptop. Establishes REPO for everything that follows.
ssh takeout-server 'for d in \
    /opt/storage.local_1/projects/takeout-downloader \
    /opt/local_cache_crypt/_projects/takeout-downloader \
    /mnt/local_cache_crypt/_projects/takeout-downloader; do
  [ -f "$d/docker-compose.webgui.yml" ] && echo "REPO=$d"
done'
```

Expected output: exactly **one** `REPO=...` line. If you get zero lines, find it:

```bash
# [FREE]
ssh takeout-server 'find /opt /mnt -maxdepth 4 -name docker-compose.webgui.yml 2>/dev/null'
```

If you get two or more, **STOP, ask a human** — you may be looking at a stale
copy from the migration, and operating on the wrong one will silently do nothing.

Then, for the rest of the session:

```bash
# [FREE] --- paste the resolved value; everything below assumes these.
export REPO=/opt/storage.local_1/projects/takeout-downloader   # <-- from the check above
export TAKEOUT_ROOT=/opt/archives/google-takeout                # <-- verify with the next command
```

Verify the storage root from the manager itself rather than believing the docs:

```bash
# [FREE]
ssh takeout-server 'docker exec takeout-webgui python3 -c "
import sys; sys.path.insert(0,\"/work\")
from manager.config import get_config
print(get_config().takeout_root)"'
```

Expected: `/opt/archives/google-takeout`. If it prints anything else — especially
`/opt` — **STOP, ask a human**: `STORAGE_ROOT=/opt` shadows the container's
`/opt/manager-venv` and the manager will die with exit 127
(`docs/webgui/13-migration-diskfull.md`, gotcha #2).

### 0.5 The environment, in one table

| Thing | Value |
|-------|-------|
| Host | Hetzner, Ubuntu 24.04, **2 vCPU / 7.6 GiB RAM** |
| SSH | `ssh takeout-server` → `ellipsis@188.245.169.166`, key `~/.ssh/takeout_deploy` |
| `/` (root disk) | `/dev/sda1`, **only 75 GB** — has filled up before (a 22 GB `juicefs.log`). This is the #1 infrastructure failure mode. |
| Download target | JuiceFS FUSE mount, **1.12 PB** — effectively infinite space, but slow metadata and stall-prone |
| Repo + Chrome profile | **300 GB LUKS** volume at `/opt/storage.local_1` (fast local random writes) |
| Container 1 | `takeout-webgui` — Chromium 149 + KasmVNC + FastAPI manager, s6-supervised |
| Container 2 | `takeout-tunnel` — cloudflared **QUICK tunnel**; the `*.trycloudflare.com` URL changes on **every restart** |
| Manager API | `http://127.0.0.1:8080` — **inside the container only**, never tunneled |
| Chrome CDP | `http://127.0.0.1:9222` — **inside the container only**, never tunneled |
| Portal | `https://<CUSTOM_USER>:<PASSWORD>@<tunnel-host>/` (creds in `webgui/.env`) |
| Auth tokens | **EMPTY on both sides** (manager + extension policy). Empty matches empty → auto-POST works. **DO NOT fix one side.** See §1.12. |

### 0.6 Things you are FORBIDDEN to do without human sign-off

1. Delete or truncate any file under `$TAKEOUT_ROOT` that is larger than 0 bytes.
2. Issue any request to `takeout-download.usercontent.google.com` for a part
   whose `remaining` attempts is `<= TK2_BUDGET_RESERVE` (default 1).
3. Re-request an export from Google (`[CATASTROPHIC]`).
4. `docker compose down`, `docker compose restart webgui`, or anything else that
   restarts Chromium **while a download stream is live** — you lose the Google
   login and the streams.
5. Set `MANAGER_API_TOKEN` / `MANAGER_CAPTURE_TOKEN` on only one side.
6. Run `zipfile.testzip()`, `unzip -t`, or `sha256sum` on a multi-TB tree over
   the JuiceFS mount (`00-CONTRACTS.md` §5.4 — hard prohibition).
7. `UPDATE`/`DELETE` on `state.db` without first running the dry-run `SELECT`
   from §5 and pasting its output to the human.

---

# 1. FAILURE CATALOGUE

Each entry has a fixed shape so you can scan it fast at 3am:

- **Symptom** — what you literally see.
- **Confirm** — exact command + expected output. Never act on a guess.
- **Root cause** — why it happens.
- **Immediate action** — what to do in the next 60 seconds.
- **Permanent fix** — what stops it recurring.
- **ATTEMPT COST OF RECOVERY** — the number you must know before you act.

### Index

| # | Failure mode | Recovery attempt cost |
|---|--------------|----------------------|
| 1.1 | Cookie idle-death | **0** (refresh is free) |
| 1.2 | Capture→discover→expire→recapture livelock | **0** if you break it correctly; 1/part if you blunder |
| 1.3 | `END_OF_RANGE` misread as auth failure | **0** |
| 1.4 | Label-flip orphans a job | **0** if caught; **catastrophic** if it re-downloads |
| 1.5 | Filename-scheme mismatch on resume | **0** if caught; **1/part** if it restarts |
| 1.6 | Phantom last index | **0** |
| 1.7 | Disk full on `/` | **0** directly; **1/part** for streams it kills |
| 1.8 | JuiceFS FUSE stall | **0** directly; **1/part** for streams it kills |
| 1.9 | Chromium crash / tab flood | **0** |
| 1.10 | Stale `SingletonLock` | **0** |
| 1.11 | cloudflared URL rotation | **0** |
| 1.12 | Token mismatch (401 on auto-POST) | **0** |
| 1.13 | Budget exhaustion | **0** to diagnose; **catastrophic** to truly fix |
| 1.14 | Archive 7-day expiry mid-download | **catastrophic** |
| 1.15 | Partial / corrupt part | **0** to diagnose; **1** to repair |
| 1.16 | `ENOSPC` mid-write | **0** directly; **1/part** for streams it kills |
| 1.17 | Server reboot | **1/part in flight** |
| 1.18 | Container OOM | **1/part in flight** |

---

## 1.1 Cookie idle-death

**Symptom.** Job flips to `NEEDS_COOKIE`. Log shows `AUTH_REDIRECT`. Zero bytes
moving. If you tried curl by hand, curl said something absurd like
`HTTP server doesn't seem to support byte ranges. Cannot resume.` — that message
is a **lie**; see below.

**Confirm.**

```bash
# [FREE] --- what does the job think its state is?
ssh takeout-server "docker exec takeout-webgui curl -s \
  127.0.0.1:8080/api/v2/jobs/$ARCHIVE_ID | python3 -m json.tool"
```

Expected on this failure: `"status": "NEEDS_COOKIE"` and the most recent
`attempt` row carrying `"outcome": "AUTH_REDIRECT"`.

Confirm at the ledger level — this distinguishes 1.1 from 1.3 definitively:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT idx, cost_class, outcome, http_status, bytes_moved, reserved_at
     FROM attempt WHERE archive_id='$ARCHIVE_ID'
     ORDER BY id DESC LIMIT 10;\""
```

- `outcome=AUTH_REDIRECT`, `bytes_moved=0` → **this** failure (cookie dead).
- `outcome=END_OF_RANGE` → **not** this failure, go to §1.3.

**Root cause.** The `Cookie` header for
`takeout-download.usercontent.google.com` is idle-fragile: roughly **1–2 minutes**
of no requests and it stops authenticating. A dead cookie does **not** return a
clean 401. It returns **HTTP 302 → `accounts.google.com/ServiceLogin`**, and
anything that follows redirects lands on a `200 text/html` login page. That is
why `curl -C -` reports "does not support byte ranges": it asked for a range,
got a 200 HTML login page, and drew the wrong conclusion. The bytes-on-disk are
fine; only the credential is dead.

**Immediate action.** Refresh the cookie. This is **free** — no download host is
contacted by a cookie refresh.

1. Preferred: let the extension do it. With `autoRecapture` ON, the extension
   re-clicks Download on the manage page when the manager signals
   `needs_cookie`. Confirm it is trying:

   ```bash
   # [FREE]
   ssh takeout-server "docker exec takeout-webgui curl -s \
     '127.0.0.1:8080/api/v2/jobs/$ARCHIVE_ID/events?since=0' | tail -20"
   ```

   Look for repeated `needs_cookie` events. If they repeat more than ~3 times
   with no `part_progress` in between, you are in the **livelock** (§1.2) — stop
   waiting and go there.

2. Authoritative: pull the **live CDP cookie jar** yourself. This bypasses the
   extension's stale `lastCapture` entirely. Full procedure in **§4** — that is
   the single most valuable recovery tool in this document. Learn it.

**Permanent fix.** Already designed into v2 and enforced by contract:

- `00-CONTRACTS.md` §5.3: the burst scheduler pulls a fresh cookie and opens all
  N streams within ~5 seconds, bounded by `TK2_COOKIE_BUDGET_MS` (default
  20000 ms). If preflight overruns that budget, the burst is **aborted and the
  cookie re-pulled** rather than spent on a doomed request.
- `00-CONTRACTS.md` §8 invariant 4: *discovery never sits between a fresh cookie
  and the first payload byte.*
- `cookie.py` reads the **live CDP jar** (`Storage.getCookies`), never the
  extension's stored capture.

**ATTEMPT COST OF RECOVERY: 0.** A cookie refresh is a manage-page interaction
plus a CDP read. Both are `[FREE]`. The *next* download request costs 1 — but it
would have cost 1 anyway, and now it will actually succeed. **Never retry a
download on a cookie you have not just refreshed** — that is how you burn
attempts for nothing.

---

## 1.2 The capture→discover→expire→recapture livelock

**This is the failure that cost days on the first 3.08 TB run.** It is the most
important entry in this catalogue.

**Symptom.** The job oscillates forever. The event stream shows
`needs_cookie` → `capture received` → `discovering` → `needs_cookie` →
`capture received` → … Parts-done never increments. The extension popup shows a
heartbeat that keeps resetting. Hours pass; `bytes_moved` stays at 0. Nothing is
"broken" — every component is doing its job correctly, and the net result is zero
progress.

**Confirm.** Count cookie events versus payload bytes over a window:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT kind, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last
     FROM event
     WHERE archive_id='$ARCHIVE_ID'
       AND ts > datetime('now','-30 minutes')
     GROUP BY kind ORDER BY n DESC;\""
```

**Livelock signature:** `needs_cookie` count `>= 3` **and** `part_progress`
count `= 0` in the same 30-minute window.

Cross-check that the attempts being spent are moving no bytes:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT COUNT(*) AS attempts, SUM(bytes_moved) AS total_bytes
     FROM attempt
     WHERE archive_id='$ARCHIVE_ID'
       AND reserved_at > datetime('now','-30 minutes');\""
```

If `attempts > 0` and `total_bytes = 0` or near it, **every one of those attempts
was wasted.** This is an actively bleeding wound — each loop iteration may be
costing you a download from a budget of 5.

**Root cause.** Something slow was scheduled *between* the fresh cookie and the
first payload byte, and it takes longer than the cookie's idle life. In v1 there
were two such things:

1. **Discovery re-sweep.** v1 re-probed all 63 part indices with a 1-byte `Range`
   request each on every cookie-refresh resume: ~30–60 s of network time, and — far
   worse — **63 potential attempts against a budget of 5 per part.**
2. **The JuiceFS `stat()` pre-pass.** `download_exports` stat'd every part before
   starting workers. Over 58 completed parts on the FUSE mount that took **~6
   minutes** — three to six times the cookie's entire idle lifetime. The cookie
   was always dead before the 4 incomplete parts were reached.

So: cookie arrives fresh → engine spends 6 minutes asking questions → cookie is
dead → engine correctly reports `needs_cookie` → extension correctly refreshes →
repeat forever. Every participant behaves correctly; the system livelocks.

**Immediate action — break the loop before it burns more budget.**

**Step 1 — stop the bleeding. PAUSE FIRST.** Always.

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui curl -s -X POST \
  -H 'Content-Type: application/json' \
  -d '{\"archive_id\":\"$ARCHIVE_ID\"}' \
  127.0.0.1:8080/api/v2/control/pause"
```

Verify it actually paused before doing anything else:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui curl -s \
  127.0.0.1:8080/api/v2/jobs/$ARCHIVE_ID | python3 -c \
  'import sys,json; print(json.load(sys.stdin)[\"status\"])'"
```

Expected: `PAUSED`. If it still says `DOWNLOADING` or `NEEDS_COOKIE` after 15
seconds, the manager is wedged — go to §1.9 step 3 (SIGKILL-respawn) and pause
again afterwards.

**Step 2 — check the budget damage `[SCRAPE — FREE]`.** Before planning anything,
find out what the loop already cost you. `dl_counts` from the manage page is
Google's own counter and outranks our ledger (`01-IDENTITY-AND-SCRAPE.md` §8):

```bash
# [SCRAPE — FREE] --- ask the extension to re-scrape; issues NO download request
ssh takeout-server "docker exec takeout-webgui curl -s -X POST \
  -H 'Content-Type: application/json' \
  -d '{\"archive_id\":\"$ARCHIVE_ID\"}' \
  127.0.0.1:8080/api/v2/control/rescrape"
sleep 5
# [FREE]
ssh takeout-server "docker exec takeout-webgui curl -s \
  127.0.0.1:8080/api/v2/jobs/$ARCHIVE_ID/budget | python3 -m json.tool"
```

If any part shows `remaining <= 1`, **STOP. Ask a human.** You are one mistake
from §1.13 (budget exhaustion), which is only fixable by a `[CATASTROPHIC]`
re-export.

**Step 3 — do not resume the manager yet.** Resuming re-enters the loop. Instead
do the **manual live-cookie-jar pull (§4)**, which structurally cannot livelock:
you compute the work set from disk *first* (free, slow, unbounded), and only then
pull a cookie and fire — inverting the fatal ordering.

**Permanent fix.** Contractually mandated in v2:

- **Zero-probe discovery** (`00-CONTRACTS.md` §5.1): the part list comes from
  `expectedParts` in the capture payload, or the persisted plan, or filename
  arithmetic — **all free**. A probe sweep requires the explicit
  `--allow-discovery-probes` flag and is capped at `TK2_MAX_DISCOVERY_PROBES`
  (default 3) with exponential bracketing.
- **One `os.scandir()`, never a per-part `stat()`** (`00-CONTRACTS.md` §8
  invariant 3). This turns the 6-minute pre-pass into well under a second.
- **`TK2_COOKIE_BUDGET_MS`**: if preflight exceeds 20 s, abort the burst and
  re-pull rather than firing on a dying cookie.
- **No instant retry** (`00-CONTRACTS.md` §5.3 step 5): a dead stream does *not*
  get retried immediately; the engine re-pulls a cookie and restarts it in the
  next burst window.

Verify the fix is actually in effect — the planner records its discovery source:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 '$JOBDIR/state.db' \
  \"SELECT ts, payload_json FROM event
     WHERE archive_id='$ARCHIVE_ID' AND kind LIKE '%plan%'
     ORDER BY seq DESC LIMIT 3;\""
```

You want a source of `capture_expected_parts`, `persisted_plan`, or
`filename_arithmetic`. If it says `probe_sweep`, attempts were spent on discovery
— record it and tell the human.

**ATTEMPT COST OF RECOVERY: 0 if you pause first and go to §4.** Each blind
resume-and-hope iteration risks **1 attempt per part touched**. This is precisely
the failure mode where impatience is most expensive: the loop *feels* harmless
because "nothing is happening," but it may be silently eating a budget of 5.

---

## 1.3 `END_OF_RANGE` misread as an auth failure

**Symptom.** The whole job flips to `NEEDS_COOKIE` even though the cookie is
demonstrably fresh (you just refreshed it, other parts are streaming fine). It
tends to happen at the *end* of discovery, right after the last real part.

**Confirm.** Look at what the *last* attempt actually returned:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT idx, outcome, http_status, bytes_moved, note
     FROM attempt WHERE archive_id='$ARCHIVE_ID'
     ORDER BY id DESC LIMIT 5;\""
```

Decision table — memorise this, it is the whole distinction:

| `http_status` | Final URL / body | Correct `ReasonCode` | Meaning |
|---------------|------------------|----------------------|---------|
| 302 → followed | `accounts.google.com/ServiceLogin` | `AUTH_REDIRECT` | **Cookie dead.** Refresh (§1.1). |
| 200 | `text/html`, from the download host, at an index past the last real part | `END_OF_RANGE` | **Normal.** Discovery has found the end. Not an error at all. |
| 401 / 403 | body mentions limit/quota | `LIMIT_EXCEEDED` | Budget gone (§1.13). |
| 401 / 403 | otherwise | `AUTH_401` | Real auth failure. |
| 404 | — | `NOT_FOUND` | Index beyond archive end. |
| 200/206 | body starts `PK\x03\x04` | `OK_*` | Real payload. |

Cross-check the index against `parts_expected`. If the failing index is
`>= parts_expected`, it **cannot** be a real part and therefore cannot be an auth
problem:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT parts_expected, status FROM job WHERE archive_id='$ARCHIVE_ID';\""
```

**Root cause.** Both a dead-cookie redirect and a past-the-end probe surface as
HTML. v1's `probe_export` treated *all* HTML as `AuthError`, which flipped the
entire job to `needs_cookie` at the exact moment discovery successfully completed.
Fixed in commit `a531bba`. The distinguishing signal is the **final URL**, not the
content type: if it landed on `accounts.google.com`, it is auth; if it stayed on
the download host, it is end-of-range.

**Immediate action.** Nothing is wrong with your cookie. Do **not** refresh, do
**not** recapture, and above all do **not** retry the download — a retry costs an
attempt to re-learn a fact you already know. Just fix the state:

```bash
# [FREE] --- DRY RUN first, always. See what you would change.
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT idx, status, size_on_disk FROM part
     WHERE archive_id='$ARCHIVE_ID'
       AND idx >= (SELECT parts_expected FROM job WHERE archive_id='$ARCHIVE_ID');\""
```

If that returns rows, they are phantoms — go to §1.6 and §5.4 to drop them.
Then clear the bogus job status:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 '$JOBDIR/state.db' \
  \"UPDATE job SET status='READY', last_error=NULL,
      updated_at=datetime('now')
     WHERE archive_id='$ARCHIVE_ID' AND status='NEEDS_COOKIE';\""
```

**Permanent fix.** `00-CONTRACTS.md` §5.2 makes `classify()` a pure function whose
branch order is normative, and §8 invariant 5 states outright: **`END_OF_RANGE`
never flips a job to `NEEDS_COOKIE`.** `classify.py` must have a unit test with a
recorded fixture for every branch of the table above. If you find this failure in
v2 code, a required test is missing — report it.

**ATTEMPT COST OF RECOVERY: 0.** This is a pure misclassification. Everything on
disk is fine, the cookie is fine, and only a state field is wrong. Treat any
attempt spent "investigating" this as pure waste.

---

## 1.4 Label-flip orphans a job (the 2.8 TB near-miss)

**Symptom.** A second job appears for what is obviously the same export. A new
output directory is created next to the old one — e.g. both
`gaia-1005482974000/2026-06-23-03-59-47/` and
`braincreation/2026-06-23-03-59-47/`. The new one has `parts_done: 0` and starts
downloading from part 0. **Terabytes already on disk are ignored.**

**Confirm — do this FAST; every second it runs, it is burning attempts.**

```bash
# [FREE] --- how many jobs share this archive_id? Must be exactly 1.
ssh takeout-server "docker exec takeout-webgui curl -s \
  127.0.0.1:8080/api/v2/jobs?limit=50 | python3 -c '
import sys, json, collections
jobs = json.load(sys.stdin)
jobs = jobs[\"items\"] if isinstance(jobs, dict) else jobs
c = collections.Counter(j[\"archive_id\"] for j in jobs)
for a, n in c.items():
    print((\"DUPLICATE\" if n > 1 else \"ok       \"), a, n)
for j in jobs:
    print(\"   \", j[\"archive_id\"], j.get(\"account_label\"), j.get(\"output_dir\"))'"
```

And look for sibling directories holding the same export timestamp:

```bash
# [FREE] --- same export_ts under two different labels == orphaning
ssh takeout-server "ls -1d $TAKEOUT_ROOT/*/*/ | sort"
ssh takeout-server "du -sh $TAKEOUT_ROOT/*/*/ 2>/dev/null | sort -h"
```

The signature is unmistakable: two directories with the **same `<export-ts>`**
under **different labels**, one large and one nearly empty.

**Root cause.** v1 keyed job resume on the **label-derived output directory**.
When the DOM scrape improved — from the `gaia-<user>` URL fallback to the friendly
scraped label `braincreation` — the derived directory changed, resume-by-directory
failed to match, and the manager created a **new** job and began re-downloading
2.8 TB. Fixed in commit `fdf8535`. The conceptual error was treating a *better*
label as a *different account*.

**Immediate action.**

1. **PAUSE THE NEW JOB IMMEDIATELY.** This is the single most urgent stop in this
   entire document — the empty job is actively spending attempts re-fetching bytes
   you already own.

   ```bash
   # [FREE] --- run this before you finish reading the rest of this entry
   ssh takeout-server "docker exec takeout-webgui curl -s -X POST \
     -H 'Content-Type: application/json' \
     -d '{\"archive_id\":\"$ARCHIVE_ID\"}' \
     127.0.0.1:8080/api/v2/control/pause"
   ```

2. Establish which directory holds the real bytes:

   ```bash
   # [FREE]
   ssh takeout-server "for d in $TAKEOUT_ROOT/*/*/; do
     printf '%s  parts=%s  bytes=%s\n' \"\$d\" \
       \$(ls -1 \"\$d/parts/\" 2>/dev/null | wc -l) \
       \$(du -sb \"\$d\" 2>/dev/null | cut -f1)
   done"
   ```

3. Consolidate. Because both directories are on the **same JuiceFS mount**, a
   rename is an inode operation — instant even for terabytes
   (`01-IDENTITY-AND-SCRAPE.md` §4.2). Move the *good* bytes under the *better*
   label, never the reverse:

   ```bash
   # [FREE] --- verify same filesystem FIRST; a cross-mount mv copies 3 TB
   ssh takeout-server "stat -c '%d %n' $TAKEOUT_ROOT/gaia-1005482974000/ \
     $TAKEOUT_ROOT/braincreation/"
   # The first field (device id) MUST match on both lines.
   ```

   If the device ids match:

   ```bash
   # [FREE] --- adjust labels/timestamps to your actual case
   ssh takeout-server "
     GOOD=$TAKEOUT_ROOT/gaia-1005482974000/2026-06-23-03-59-47
     NEW=$TAKEOUT_ROOT/braincreation/2026-06-23-03-59-47
     ls -la \"\$NEW/parts/\"          # must be empty or only 0-byte stubs
     rmdir \"\$NEW/parts\" 2>/dev/null || echo 'NOT EMPTY -- STOP, ask a human'
   "
   ```

   If `$NEW/parts/` contains non-zero-byte files, **STOP, ask a human.** Both
   directories hold real data and merging them is a judgement call you should not
   make unsupervised.

   If it was empty, move the good tree and fsync the parent:

   ```bash
   # [FREE]
   ssh takeout-server "
     GOOD=$TAKEOUT_ROOT/gaia-1005482974000/2026-06-23-03-59-47
     DEST=$TAKEOUT_ROOT/braincreation
     mkdir -p \"\$DEST\"
     mv \"\$GOOD\" \"\$DEST/\"
     sync
     ls -la \"\$DEST/2026-06-23-03-59-47/parts/\" | head
   "
   ```

4. Point the surviving job row at the consolidated directory and delete the
   duplicate job row — see §5.5 for the SQL with a dry-run.

**Permanent fix.** `00-CONTRACTS.md` §8 invariant 6 and
`01-IDENTITY-AND-SCRAPE.md` §10 invariant 1: **`archive_id` is the ONLY
job-matching key** — never the folder path, never the label. Identity is for
humans and folder names only. The provenance ladder
(`01-IDENTITY-AND-SCRAPE.md` §4) makes label changes explicitly *directional*: a
higher-provenance label **upgrades** the existing job (keep the row, rename the
directory, emit `identity_upgraded`), and a lower-provenance label is **ignored
entirely** so a failed scrape can never downgrade a good label.

**ATTEMPT COST OF RECOVERY: 0 if you catch it before bytes move.** If the orphan
job got a head start, you have paid **1 attempt for every part it touched** — and
those parts are the ones you already had complete on disk, so the spend bought
literally nothing. On the original incident this nearly consumed the budget for a
2.8 TB archive. Check the duplicate-job condition in every pre-flight (§6).

---

## 1.5 Filename-scheme mismatch on resume

**Symptom.** `parts/` visibly contains large complete files, but the job reports
them as `PENDING` with `size_on_disk: 0` and starts downloading from part 0. `ls`
shows **two naming styles** side by side.

**Confirm.** Compare what is on disk against what state.db expects:

```bash
# [FREE] --- what is actually there
ssh takeout-server "ls -1 $JOBDIR/parts/ | sed -E 's/[0-9]+/N/g' | sort -u"
```

Expected: **exactly one** normalised pattern. Two or more means mismatch. The two
known v1 schemes were:

- `takeout-20260616T040104Z-9-001-part-NN.zip` — derived from the payload
  `exports[]` path
- `takeout-20260616T040104Z-13-NN.zip` — derived from the discovery sweep's
  `Content-Disposition`

Now the authoritative comparison:

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT idx, filename, size_expected, size_on_disk, status
     FROM part WHERE archive_id='$ARCHIVE_ID' ORDER BY idx LIMIT 15;\""
```

Mismatch signature: `part.filename` does not exist under `parts/`, while a
similarly-named large file does. Prove it mechanically:

```bash
# [FREE] --- every expected filename that is MISSING from disk
ssh takeout-server "docker exec takeout-webgui python3 - <<'PY'
import os, sqlite3
jobdir = os.environ['JOBDIR']; aid = os.environ['ARCHIVE_ID']
ondisk = {e.name: e.stat().st_size
          for e in os.scandir(os.path.join(jobdir, 'parts')) if e.is_file()}
db = sqlite3.connect(os.path.join(jobdir, 'state.db'))
rows = db.execute('SELECT idx, filename, size_expected FROM part '
                  'WHERE archive_id=? ORDER BY idx', (aid,)).fetchall()
missing = [(i, f) for i, f, _ in rows if f not in ondisk]
print('expected parts     :', len(rows))
print('files on disk      :', len(ondisk))
print('expected-but-absent:', len(missing))
for i, f in missing[:10]:
    print('   idx', i, '->', f)
claimed = {f for _, f, _ in rows}
orphans = [(n, s) for n, s in ondisk.items() if n not in claimed and s > 0]
print('on-disk-but-unclaimed (non-zero):', len(orphans))
for n, s in sorted(orphans)[:10]:
    print('   %-52s %13d' % (n, s))
PY"
```

If `expected-but-absent > 0` **and** `on-disk-but-unclaimed > 0` with comparable
counts, this is a pure naming mismatch — **the bytes are all there.**

**Root cause.** The first run named parts from one source and a later resume named
them from another. The engine matched by filename, so the 2.8 TB already on disk
was invisible to it. Root cause #2 in `docs/webgui/14-resume-cookies-multiaccount.md`.

**Immediate action.**

1. **PAUSE.** (Same command as §1.4 step 1.) It is re-downloading data you own.
2. Do **not** rename files on the FUSE mount if you can avoid it. Instead, correct
   `part.filename` in state.db to match reality — free, instant, reversible. See
   **§5.3** for the reconciliation SQL with a dry-run.
3. Only if a rename is genuinely required: it is an inode operation on the same
   mount, so it is fast, but do it one file at a time and verify:

   ```bash
   # [FREE] --- one file, explicit, verified. Never a bulk glob rename.
   ssh takeout-server "cd $JOBDIR/parts && \
     ls -la 'takeout-20260616T040104Z-9-001-part-07.zip' && \
     mv -n 'takeout-20260616T040104Z-9-001-part-07.zip' \
           'takeout-20260616T040104Z-9-007.zip' && \
     ls -la 'takeout-20260616T040104Z-9-007.zip'"
   ```

   `mv -n` refuses to clobber an existing target. **Never** use `mv` without `-n`
   here — overwriting a complete 10 GB part with a partial is a
   `[COSTS 1 ATTEMPT]` mistake at best.

**Permanent fix.** Filenames come from **one** authoritative source: the `uris` /
`data-download-uri` fields in the capture payload
(`01-IDENTITY-AND-SCRAPE.md` §6), persisted to `part.filename` and `part.url` at
plan time. Discovery-derived `Content-Disposition` names are never used to rename
an existing plan. Matching is by `(archive_id, idx)` — the primary key — with
filename as a display attribute, and `size_on_disk` is refreshed from a single
`os.scandir()`.

**ATTEMPT COST OF RECOVERY: 0 if caught before the re-download starts.** Once it
starts, **1 attempt per part it re-fetches.** On a 62-part archive with a budget
of 5, a single unnoticed mismatch running overnight can consume a fifth of your
total budget re-downloading files that were already complete.

---

## 1.6 Phantom last index

**Symptom.** The job shows one more part than the archive really has (e.g. 63
rows when `expectedParts` is 62). The extra part is index `parts_expected` — the
first index that does not exist. It is `PENDING` or `FAILED` forever, its file is
0 bytes or a small stub (~1.7 MB of HTML), and it is often confusingly named
`…-001.zip`. The job can never reach `COMPLETE` because one part never finishes.

**Confirm.**

```bash
# [FREE]
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT p.idx, p.filename, p.size_on_disk, p.status, j.parts_expected
     FROM part p JOIN job j USING(archive_id)
     WHERE p.archive_id='$ARCHIVE_ID' AND p.idx >= j.parts_expected
     ORDER BY p.idx;\""
```

Any row returned is a phantom by definition: with `parts_expected = 62` the real
indices are **0–61**.

Confirm the file on disk is a stub, not payload:

```bash
# [FREE] --- a real part starts PK\x03\x04 (504b0304)
ssh takeout-server "ls -la $JOBDIR/parts/ | sort -k5 -n | head -5"
ssh takeout-server "head -c 4 '$JOBDIR/parts/<phantom-filename>' | xxd"
```

Expected for a phantom: something like `3c21 444f` (`<!DO` — an HTML page) or an
empty read. If you see `504b 0304`, **it is a real zip — STOP, ask a human.**

**Root cause.** v1's discovery sweep deliberately probed one index *past* the
last real part to detect the end. That past-the-end probe returns an HTML page
(`END_OF_RANGE`, §1.3), and the phantom index got seeded into the job state along
with whatever bytes the HTML response wrote to disk.

**Immediate action.** Delete the stub, drop the row. Both free. Dry-run first —
full SQL in **§5.4**:

```bash
# [FREE] --- STEP 1: dry run. Read the output before proceeding.
ssh takeout-server "docker exec takeout-webgui sqlite3 -header -column \
  '$JOBDIR/state.db' \
  \"SELECT 'WOULD DELETE part row', idx, filename, size_on_disk
     FROM part
     WHERE archive_id='$ARCHIVE_ID'
       AND idx >= (SELECT parts_expected FROM job WHERE archive_id='$ARCHIVE_ID')
       AND size_on_disk < 104857600;\""
```

The `size_on_disk < 100 MiB` guard is a safety net: it makes it impossible for
this statement to touch a real 10 GB part even if `parts_expected` is wrong.

**Permanent fix.** Zero-probe discovery (`00-CONTRACTS.md` §5.1) never probes
past the end because it never probes at all — `expectedParts` comes from the DOM.
When a bounded probe sweep *is* explicitly enabled, its past-the-end result must
be recorded as `END_OF_RANGE` and **must not create a part row.** The planner
emits indices `0..expectedParts-1`, full stop.

**ATTEMPT COST OF RECOVERY: 0.** Local file deletion and one SQL `DELETE`.
Beware the inverse trap: an operator who sees "part 62 failed" and "retries" it
spends a real attempt to re-download a page of HTML. **Always check
`idx >= parts_expected` before retrying any failing tail part.**

<!-- APPEND-HERE -->
