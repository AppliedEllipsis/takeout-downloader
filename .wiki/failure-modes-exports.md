# Failure modes — exports (1.10, 1.14, 1.15, 1.19)

Scope: the three export/browser-profile failure modes of `docs/v2/04-FAILURE-MODES-AND-RECOVERY.md`'s index
that v2 never wrote — archive expiry, partial/corrupt parts, and (grouped here by batch) the stale Chromium
single-process lock, a browser-profile issue rather than an export one. Attempt costs are the index's
advertised figures (`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:169`, `:173-174`); every other claim is cited
to a real file or to `measured-facts.md` / `archival-inventory.md`. **UNVERIFIED** = not measured.

---

## 1.10 — Stale `SingletonLock`

**Advertised cost: 0.**

**Symptom.**

After a container recreate (`--force-recreate`) or an unclean stop, **Chromium never launches**. The s6
service still looks "up" — it is a supervision loop, so s6 sees a live process — but no browser appears and
`127.0.0.1:9222` never answers. In the profile directory:

```
/config/.chrome-profile/SingletonLock -> <hostname>-<pid>
```

where `<pid>` belongs to a chromium from the **previous container**
(`docs/webgui/10-deployment-status.md:197-202`).

**Root cause.**

Chromium guards a profile with three entries — `SingletonLock` (a symlink encoding `<host>-<pid>`),
`SingletonCookie`, `SingletonSocket`. A container recreate changes the PID namespace, so the recorded PID
can never re-match; the lock is permanently stale yet still blocks every launch. The service clears it
under a guard that can be wrong in one direction (`webgui/chromium-service.sh:46-51`; same guard at
`webgui/init_custom.sh:35-36`):

```bash
if ! pgrep -x chromium >/dev/null 2>&1; then
    rm -f "$PROFILE_DIR/SingletonLock" \
          "$PROFILE_DIR/SingletonCookie" \
          "$PROFILE_DIR/SingletonSocket" 2>/dev/null || true
```

(`PROFILE_DIR=/config/.chrome-profile`, `chromium-service.sh:14`.) **The guard is an exact process-name
match.** If any process named exactly `chromium` exists — a zombie, a half-dead main process, a re-parented
renderer — the lock is *not* cleared and relaunch can never succeed. That asymmetry is the residual risk.

**Detection.**

```bash
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "
  ls -l /config/.chrome-profile/Singleton*; echo ---; pgrep -ax chromium | head -5"'
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/list"'
```

`SingletonLock` present **and** its target `<pid>` absent from `pgrep -ax chromium` ⇒ stale. If CDP is
silent and no chromium is visible, the service loop should already have fixed it within its 10-second tick
(`chromium-service.sh:52-57`); a lock surviving >30 s means the guard is misfiring.

**Recovery.**

```bash
# 1. Confirm nothing real owns the profile. If a chromium IS running, STOP — see gotchas.
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "pgrep -ax chromium"'

# 2. Only if that output is empty: clear the three entries.
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c \
  "rm -f /config/.chrome-profile/SingletonLock \
         /config/.chrome-profile/SingletonCookie \
         /config/.chrome-profile/SingletonSocket"'

# 3. The supervision loop relaunches within 10s; check it did.
ssh -o BatchMode=yes takeout-server 'docker logs --tail 30 takeout-webgui 2>&1 | grep -i chromium'
```

**Gotchas.** Deleting a *live* instance's `Singleton*` entries can corrupt the profile — hence step 1. And
success is **not** "the profile is logged in": the profile was measured **logged out** on 2026-09-19
(`infrastructure.md`), so re-auth is a separate human step.

**Verification.**

```bash
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "pgrep -c chromium; curl -s 127.0.0.1:9222/json/list | head -c 200"'
```

Non-zero chromium count and `/json/list` returning a target array (the measured working shape, `runbooks.md` §2).

**Attempt cost.**

**0.** Local filesystem removal plus process inspection inside the container; no Google endpoint is touched, so
no download counter moves. The index agrees (`docs/v2/04-...md:169`).

**What v3 must change.**

1. **Make the guard positive, not negative** — confirm the recorded PID is dead *and* that no process holds
   the profile (`fuser`/`lsof`), rather than trusting `pgrep -x chromium` — and **log the guard's decision**
   (`stale lock cleared` / `lock held by live pid N`) so a misfire is visible.
2. **Don't conflate "browser started" with "browser usable."** Health is CDP answering and a signed-in
   `takeout.google.com/manage` tab, not a running process.

---

## 1.14 — Archive 7-day expiry mid-download

**Advertised cost: catastrophic.**

**Symptom.**

Downloads stop partway through an export and **never resume**. Every attempt redirects to
`accounts.google.com` or 4xxes, however fresh and correct the cookie is. The job sits in `NEEDS_COOKIE`
indefinitely — observed live for **~3 months** on the `braincreation` job, spawning a browser tab every
minute (`archival-inventory.md`, "It can never be completed"; the spawner is `failure-modes.md` §1.9).

The archive page is the oracle: it shows `Available until <date>` and `Overall status:` on
`https://takeout.google.com/manage/archive/<id>` — the page that carries the download links (`/manage`
does not). `.recon/verify_dom.py:118-121` already probes this text (`has_expires: /expir/i.test(t)`).

**Root cause.**

**Takeout exports expire ~7 days after Google creates them, and the clock runs from creation, not from
when you started downloading** (`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:35-36`). Once the window closes
the source objects are deleted server-side and every consequence is permanent. Measured
(`archival-inventory.md`): `braincreation` created **2026-06-23**, stalled **2026-06-28**, expired
**~2026-06-30**.

Of an expected **63 parts** the ledger recorded **10** — roughly one-sixth. Of those, **3 are truncated**
with no end-of-central-directory record and cannot be opened or repaired: `-13-003` (49.8 GB), `-13-005`
(38.8 GB), `-13-006` (30.7 GB) — **~119 GB of unrecoverable garbage**. `andrew`, verified the same day,
was **6/6 OK**.

**Detection.**

```bash
# 1. Read the expiry oracle on the archive page (free — a page read, no download).
#    Needs a live jar and a satisfied ReAuth challenge; see measured-facts.md for the chain.
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui python3 /tmp/tk_dom.py'
#   look for:  Available until <date>   /   Overall status: <…>

# 2. Cross-check creation time, which is in the export directory name.
ssh -o BatchMode=yes takeout-server 'ls -1d /opt/archives/google-takeout/*/*'
#   e.g. …/braincreation/2026-06-23-03-59-47
```

Declare expiry when `Available until` has passed **or** the export directory is >~7 days old. The "~" is
everywhere in v2 (`docs/v2/00-CONTRACTS.md:13`, `docs/v2/07-...md:25`); the actual tolerance, i.e. any
post-expiry grace, is **UNVERIFIED**. Treat the boundary conservatively.

**Recovery.**

**There is no resumable recovery. Do not attempt to resume an expired export.** The bytes are gone, and a
fresh mint against an expired `j=` cannot succeed however the request is made. **The only honest recovery
is to accept the loss and request a new export.** What you can do is stop the damage and salvage what was
already fetched:

```bash
# 1. STOP THE TAB SPAWNER FIRST, or tabs refill during cleanup (failure-modes.md §1.9); it fires
#    because ANY job is NEEDS_COOKIE (manager/app.py:243-247). Then verify the parts you hold — free:
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c \
  "cd /work && python3 -m takeout2 verify <archive_id> --db /opt/archives/state.db"'

# 2. Retire the unsatisfiable job so it stops being re-driven — BOOKKEEPING ONLY.
#    Measured 2026-09-19: DELETE /api/jobs/20260628T001529-braincreation -> HTTP 200;
#    412,631,115,353 -> 412,631,085,735 bytes; 11 -> 11 files; the 29,618-byte delta is
#    exactly .manager_state.json. No zip was touched.  (.recon/retire_stale.sh)
ssh -o BatchMode=yes takeout-server 'bash /tmp/tk_retire.sh'

# 3. Request a NEW export. A human must be signed in for this (the profile was measured
#    logged out 2026-09-19). Nothing on this host can re-create the source.
```

⚠️ **The v2 job API answered HTTP 000 (nothing serving) on 2026-09-19** (`infrastructure.md`, port 8080), so
confirm the manager is up before relying on `DELETE /api/jobs/<id>` — the measured run above predates that
probe. Fallback: remove `.manager_state.json` from the job directory, keeping a copy as `_retired-bookkeeping/`
does.

**The old export is not resumable.** The 53 never-fetched and 3 truncated parts cannot be re-fetched from
an expired archive. Truncation repair (1.15) applies **only** while the export is live and shows a future
`Available until`.

**Verification.**

- `Available until` is past (or the export dir is >7 days old) — premise confirmed.
- The retired job is gone from the list and part bytes are unchanged: 11 → 11 files, delta exactly
  `.manager_state.json`. The spawner is quiet (sign-in pages **0**, `failure-modes.md` §1.9), and the intact
  parts still read `STRUCT_OK` while the 3 truncated ones still report no EOCD — **don't expect that to
  change**, it is the accepted loss until the new export arrives.

**Attempt cost.**

**Catastrophic.** The index marks it so (`docs/v2/04-...md:173`), and the measurement explains why: the
recovery is not an attempt against the old archive but a **whole new export** — a new creation wait (hours to
days, and for a multi-TB account possibly longer than the new 7-day window allows: `docs/v2/04-...md:24-27`),
a human re-auth, and re-downloading everything. For `braincreation` that means redoing a 3.08 TB / 63-part
export minus nothing, since the 10 held parts are one-sixth of it and 3 of those are garbage.

**What v3 must change.**

1. **Model the deadline as a first-class field:** `expires_at = created_at + window`, read from
   `Available until` when present, else derived from the export timestamp.
2. **Schedule against the deadline, not availability** — rank parts by `deadline - now`; finish an export
   before starting another.
3. **Never park a job indefinitely.** `NEEDS_COOKIE` must carry a max age tied to the deadline; past it the
   job becomes terminal (`EXPIRED_UNRECOVERABLE`) and **stops being re-driven** — the fix for both the
   3-month zombie job and the tab flood it caused.
4. **Alert early and loudly** — at 48 h remaining, and again if progress falls behind a linear pace. Expiry
   is the one failure with no recovery, so it needs the best warning.

---

## 1.15 — Partial / corrupt part

**Advertised cost: 0 to diagnose; 1 to repair.**

**Symptom.**

A part is on disk but wrong. The project's own verifier distinguishes the shapes (`takeout2/verify.py`):

| State | Message | Meaning |
|---|---|---|
| `UNVERIFIED` | `incomplete: <n>/<expected> bytes (x%) — resumable` (`verify.py:127-128`) | short read — the ordinary partial |
| `UNVERIFIED` | `no EOCD in tail — file is truncated, resume it` (`verify.py:154`) | full-looking but truncated |
| `CORRUPT` | `bad magic … expected PK\x03\x04 (an HTML error page saved as .zip looks like this)` (`verify.py:146-148`) | an error page, not a zip |
| `CORRUPT` | `oversized: <n> > expected <m> — likely appended to a stale file` (`verify.py:131-133`) | resume appended to the wrong file |

Observed live: 3 of 10 `braincreation` parts truncated with no EOCD — `-13-003` 49.8 GB, `-13-005`
38.8 GB, `-13-006` 30.7 GB (`archival-inventory.md`).

**Root cause.**

The transfer ended without its closing record — process killed, cookie died mid-stream, connection dropped.
For `.zip` the central directory is written last and every entry's offset refers to it, so a file without an
EOCD cannot be opened or repaired: **there is no partial zip.** The only repair that works is completing the
byte range — `Range`-resuming the remainder from the source.

**Detection.**

The verifier is measured-good and must be **reused, not reinvented**:

```bash
# STRUCT_OK = head is PK\x03\x04 AND an EOCD record is found in the tail — two seeks, no full read.
# (takeout2/verify.py:40-45, :97-102; EOCD search window MAX_EOCD_SEARCH = 66 KiB, :45)
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c \
  "cd /work && python3 -m takeout2 verify <archive_id> --db /opt/archives/state.db"'
```

Two properties must not be lost:

- **`STRUCT_OK` is the default bar** (`verify.py:20`, `:106`; `contracts.py:81-93`). **`HASH_OK` is opt-in
  only** via `--deep` (`cli.py:872`, `cli.py:1352`), because a full sha256 is a **full read** — on the rclone
  FUSE mount that is hours with real stall risk (`verify.py:9-15`). Never make it the default.
- **Truncation is `UNVERIFIED`, not `CORRUPT`** (`verify.py:152-155`), deliberately, so the pipeline
  distinguishes *resumable* from *ruined*. A scheduler treating `UNVERIFIED` as failure throws away a part one
  free `Range` request would have completed.

**Recovery.**

**Check expiry first (1.14). Repair requires a live export; an expired one has no source.** With a future
`Available until` and a responsive file host:

```bash
# Prefer Range against the file host: measured Δ0 on the download counter
# (measured-facts.md, "Attempt costs"). Needs the jar + a minted URL; no rapt needed.

# 1. Learn the remaining byte range. The file host reports all three:
#    ETag: <strong validator>   Accept-Ranges: bytes   Content-Range: bytes 0-1023/<total>
#    Measured: Content-Range: bytes 0-1023/261259 — a correct total IS reported.
#    (The request implementation itself is UNVERIFIED — no v3 client exists yet.)

# 2. Compare <total> against the ledger's size_expected, then resume from the on-disk size. Resume
#    must append to the SAME file; a stale/short file yields the "oversized" CORRUPT case (verify.py:131-133).

# 3. Re-verify (free, local):
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c \
  "cd /work && python3 -m takeout2 verify <archive_id> --db /opt/archives/state.db"'
```

**Never concatenate a truncated zip with a separately fetched tail** unless the split point is exact — a
truncated zip plus its tail is not a zip. **UNVERIFIED:** no measured procedure exists on this system for
off-line reassembly of a split part; do not improvise one. And **do not use `manifest.json` as the completeness
oracle** — it parses with **0 entries** for both exports (`archival-inventory.md`).

**Verification.**

Re-run `takeout2 verify`: the part must report `STRUCT_OK` (`"magic + EOCD present"`, `verify.py:159-160`) with
`size_on_disk == size_expected`. For a high-stakes part `--deep` adds a sha256 **at the cost of a full read**
— opt in deliberately. A digest is only *compared* when `sha256_expected` is supplied (`verify.py:169-175`);
Google publishes no per-part digest, so **UNVERIFIED** whether one exists to use.

**Attempt cost.**

**0 to diagnose** — verification is local-only and never touches the network by construction (`verify.py:1`,
`:110-112`). **1 to repair** — repair needs a fresh mint, measured **Δ1**; the subsequent `Range` resumes are
**Δ0** (`measured-facts.md`, resolved table). The index's "1" (`docs/v2/04-...md:174`) holds **only if the
client caches the minted URL instead of re-minting per attempt** — the exact mistake that made this project's
budget model wrong for its whole life (§1.13).

**What v3 must change.**

1. **Keep the two-seek structural check as the default bar** — don't add a full-read check that makes
   multi-TB verification take hours.
2. **Preserve the `UNVERIFIED`-vs-`CORRUPT` distinction in the scheduler.** Truncated ⇒ resume (free);
   bad magic/oversized ⇒ discard and re-fetch. Collapsing them costs a mint for a part that needed none.
3. **Verify against a `size_expected` recorded at mint time, before the first byte is written**, and **cache
   the minted URL** so a repair costs exactly the one mint the index assumes.
4. **Verify before moving to the archive**, not after — a truncation discovered post-upload means
   re-transferring it back through the network filesystem. And **never claim completeness from
   `manifest.json`** until the format is confirmed — measured as unusable.

---

## 1.19 — Export download allowance exhausted

**Measured 2026-09-19, live, on the burn export.** Google caps how many times a single export may be
downloaded, states the cap in plain words, and refuses every further attempt.

**Symptom.**

```
**Verdict: BLOCKED — export download allowance exhausted (0/1 held). NOT resumable: create a NEW export.**
- job status: quota_exceeded        - exit code: 4
```

The chain, from the ledger's own record:

```
.../settings/takeout/download?i=0&j=<id>&download=true&rapt=<token>
  -> [302] .../manage/archive/<id>?download=true&rapt=<token>&quotaExceeded=true
```

And the archive page says it outright:

> **You can try to download a file only 5 times.**
> *You've tried to download or have downloaded this file 5 times, which is the maximum number of times
> you can take this action. You can create a new request at any time.*

**What it is not.** Not ReAuth (§1.9) — the session is fine and the bounce carries a live `rapt`. Not an
expired export (§1.14) — the export is inside its window and the page still reads `Completed`. Not a
cookie problem, and not a bug in the mint.

**Why it looked like something else.** Two ways, both now fixed:

1. Before `QuotaExceeded` existed this surfaced as an opaque `MintError` reading *"no file-host URL, no
   ReAuth, and no archive bounce in the chain"* — wrong on its own terms, because the chain was nothing
   but bounces, and wrong about the remedy, because the cure is a new export.
2. **The quota bounce carries a `rapt`.** The refreshed-token retry therefore consumed it and looped
   toward the hop limit, on course to report a hop-limit error naming an entirely different cause. The
   quota check now runs *before* the retry, and a test asserts that ordering rather than trusting it.

**Detect it** — the flag rides on the bounce, not on a status code:

```
QUOTA_FLAG = "quotaExceeded=true"     # autopilot/mint.py
```

**Fix it.** There is no repair, no retry, and no waiting: create a **new export**. For a small canary that
is minutes; a full account export is hours to days.

**Attempt cost.** **0** to diagnose, and **0** for every subsequent retry — a bounced request is a measured
Δ0. The attempts this failure consumes are Google's, not the ledger's.

**The trap that makes it expensive.** **The ledger's attempt count and Google's counter are different
quantities, and only the latter runs out.** This run printed `mint 0 | transfer 0` while Google's counter
went from 3 to its maximum of 5, because a bounced mint is never recorded as a spent attempt — measured,
and correct as telemetry. So a run can truthfully report that it spent nothing while destroying its last
chance. **Budget diagnostics against the archive's own counter (`dl_counts`, the *"Number of times already
downloaded: N"* text), never against the ledger's.** The same independence is why `dl_counts` is unusable
as an invariant (`measured-facts.md`).

**Canary sizing — learned the hard way.** The cap is per *export*, so a small canary reaches it in five
attempts, i.e. a handful of probes. The burn export was created as a monitoring canary and stood at
**3 of 5** before this session's diagnostics; it reached **5 of 5** during them. A canary meant for
repeated measurement must be **re-created on a schedule**, and `dl_counts` read *before* each experiment
rather than after.


---

## 1.20 — Percent-encoded part filename

**Advertised cost: 0. Actual cost: 0 attempts — but the archive silently holds a name
Google never used, and a later completeness check calls a present part missing.**

### Symptom

The archive contains a file like:

```
All%20mail%20Including%20Spam%20and%20Trash-002.mbox
```

No human wrote that name. The real one is `All mail Including Spam and Trash-002.mbox`.
Nothing errors; the part verifies, sizes match, the run reports `COMPLETE`.

It surfaces later as a **false missing part**: a comparison against Google's listing
reports the decoded name absent while the encoded file sits right there — observed
2026-09-21, where a 19-part export read as 18/19 complete despite being exactly complete.

### Cause

`part_filename_from_url` took the basename of the minted URL's path verbatim:

```
https://takeout-download.usercontent.google.com/download/
    All%20mail%20Including%20Spam%20and%20Trash-002.mbox?j=...
```

The path segment is percent-encoded because a URL cannot contain a raw space.

### Recovery

`unquote()` the basename — **and not the whole URL**, which would corrupt the query
string that the signature checks depend on.

**A literal `+` must survive.** Only `%XX` decodes; a filesystem has no query semantics,
so `unquote_plus` is wrong here.

### Prevention

Pinned by `tests/v3/test_filename_encoding.py`: the real live string decodes, a non-zip
extension survives, `+` is untouched, and the redirector basename `download` is still
rejected (that rejection is the multi-part data-loss guard from `docs/v3/01-ARCHITECTURE.md`).

**Cross-check that caught it:** v2 pulled the same export and wrote the *decoded* name. Two
downloaders disagreeing on what to call the same file is a correctness bug, not cosmetics.
When two implementations of one job disagree, one of them is wrong — find out which before
trusting either.

### The neighbouring trap

This part is an **`.mbox`, not a `.zip`**. Google serves non-zip parts, and the largest
single part of the 64-product export is that 13.58 GB mbox. Anything that assumes a `.zip`
suffix — completeness counting, verification, globbing — will mis-handle it. Note also that
a size-up that sums only `.zip` parts understates the export by 13.58 GB (141.28 GB actual
vs ~131.6 GB estimated).
