# Failure modes — the 12 the "Operational Bible" never wrote

`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md` indexes **18 failure modes with recovery-attempt costs**
but documents only **§1.1–§1.6**, ending at a `<!-- APPEND-HERE -->` sentinel (line 788). Three v2 docs
are truncated the same way (04, 07, 08). This set fills the gap. Owner decision 2026-09-19: **all 12
get written.**

Every attempt cost below is either the index's advertised figure or a **measurement** from this session
(see `measured-facts.md`). Where something is unknown it is marked **UNVERIFIED**.

## Index

| # | Failure mode | Advertised cost | Documented in |
|---|---|---|---|
| 1.7 | Disk full on `/` | 0 directly; 1/part for streams it kills | [failure-modes-storage.md](failure-modes-storage.md) |
| 1.8 | JuiceFS FUSE stall | 0 directly; 1/part for streams it kills | [failure-modes-storage.md](failure-modes-storage.md) |
| **1.9** | **Chromium crash / tab flood** | **0** | **below** |
| 1.10 | Stale `SingletonLock` | 0 | [failure-modes-exports.md](failure-modes-exports.md) |
| 1.11 | cloudflared URL rotation | 0 | [failure-modes-runtime.md](failure-modes-runtime.md) |
| 1.12 | Token mismatch (401 on auto-POST) | 0 | [failure-modes-runtime.md](failure-modes-runtime.md) |
| **1.13** | **Budget exhaustion** | 0 to diagnose; **catastrophic** to fix | **below** |
| 1.14 | Archive 7-day expiry mid-download | **catastrophic** | [failure-modes-exports.md](failure-modes-exports.md) |
| 1.15 | Partial / corrupt part | 0 to diagnose; **1** to repair | [failure-modes-exports.md](failure-modes-exports.md) |
| 1.16 | `ENOSPC` mid-write | 0 directly; 1/part for streams it kills | [failure-modes-storage.md](failure-modes-storage.md) |
| 1.17 | Server reboot | **1**/part in flight | [failure-modes-runtime.md](failure-modes-runtime.md) |
| **1.18** | **Container OOM** | **1**/part in flight | **below** |
| 1.20 | Percent-encoded part filename | **0** — but silently misnames the archive | [failure-modes-exports.md](failure-modes-exports.md) |
| 1.21 | Staging never freed → `ENOSPC` on a large pull | 0 to diagnose; **blocks the pull** | [failure-modes-storage.md](failure-modes-storage.md) |
| 1.22 | Stray browser download left running by a mint | **0** attempts; consumes the archive volume | [failure-modes-storage.md](failure-modes-storage.md) |

> `1.19` (export download allowance exhausted) lives in
> [failure-modes-exports.md](failure-modes-exports.md). `1.20` and `1.21` were added
> 2026-09-21, found by minting and transferring the real exports rather than by review.
> `1.22` was added 2026-09-22, when the owner directive to stop cancelling browser
> downloads removed the cleanup that used to hide it.

---

## 1.9 — Chromium crash / tab flood

**Diagnosed and fixed live 2026-09-19. Attempt cost of recovery: 0.**

**Symptom.**

Hundreds of `accounts.google.com/v3/signin/identifier` `page` targets accumulate in the container's
Chromium. Observed peak: **406 CDP targets** — 314 sign-in pages, 87 `accounts.youtube.com`
`CheckConnection` iframes, 2 `restart` pages. Container memory climbs to its 7.57 GiB limit; host `free`
shows **0 available**; 119 Chromium processes.

**Root cause.** A self-perpetuating tab spawner.

1. **`helpers/background.js:419`** — a 1-minute alarm: `chrome.alarms.create(RECAPTURE_ALARM, { periodInMinutes: 1 })`.
2. **`helpers/background.js:424-440`** `pollRecapturePending()` — returns early only if `!s.autoRecapture`
   (line 427); otherwise fetches `GET /api/control/recapture-pending` and calls `triggerRecapture()` when
   the body says `pending: true`.
3. **`manager/app.py:243-247`** — returns `pending: true` whenever **any job has status `NEEDS_COOKIE`**.
   Commit `b7a3f39` deliberately makes `NEEDS_COOKIE` park **indefinitely**.
4. **`helpers/background.js:444-456`** `triggerRecapture()`:
   ```js
   const tabs = await chrome.tabs.query({ url: 'https://takeout.google.com/*' });
   if (!tabs[0]) { await chrome.tabs.create({ url: 'https://takeout.google.com/manage', active: false }); }
   ```

**Why it never stops:** the guard is a **URL-pattern** query. While logged out the tab it opens redirects
to `accounts.google.com`, so `takeout.google.com/*` never matches, so the next tick opens *another* one.
**One tab per minute, forever** — the exit condition is unreachable.

A secondary spawner exists — `manager/recipes.py:236-242` does an unconditional `PUT /json/new?<url>` —
but it is **not on a timer** (`Recipe.schedule_cron` is written and read by nothing).

**⚠️ The trap — the managed policy flag is INERT.**

`webgui/init_custom.sh:96-116` writes `"autoRecapture": true` into Chromium's managed policy. **Editing it
changes nothing.** The extension reads `chrome.storage.managed` **only for `captureToken`**
(`background.js:293-294`); `autoRecapture` and `managerUrl` come from **`chrome.storage.local`**
(`getManagerSettings()`, `background.js:213-222`). The template also hardcodes `true`, so it regenerates on
every container recreate. Anyone who "fixed" this by flipping the policy would have seen no change and
concluded the bug was unfixable.

**Detection.**

```bash
# how many sign-in pages are stacked?
ssh takeout-server 'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/list"' \
  | grep -o 'accounts\.google\.com/v3/signin' | wc -l
free -m | head -2                                   # 0 available is the smoking gun
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' takeout-webgui
pgrep -c chromium
```

**Recovery.** (verified)

```bash
# 1. NEUTRALISE THE SPAWNER FIRST — or the cleanup refills.
#    Flip the value the extension actually reads, over CDP on the service-worker target.
#    websockets is present in the container; websocket-client is installed nowhere.
scp .recon/flip_recapture.py takeout-server:/tmp/tk_flip.py
ssh takeout-server 'docker cp /tmp/tk_flip.py takeout-webgui:/tmp/tk_flip.py >/dev/null \
  && docker exec takeout-webgui python3 /tmp/tk_flip.py'
#   BEFORE: {"autoRecapture":true}   AFTER: {"autoRecapture":false}   RE-READ: {"autoRecapture":false}

# 2. THEN close accumulated sign-in pages (only `page` targets whose URL contains
#    accounts.google.com), preserving the manage tab, the webgui tab and the extension worker.
#    Nothing in this procedure terminates a process. If memory pressure has already culled
#    the renderers (observed), there is nothing to close — verify rather than assume.

# 3. Verify all criteria.
```

**Verification.** Results 2026-09-19 04:33Z:

| Criterion | Want | Got |
|---|---|---|
| sign-in pages | 0 | **0** |
| total CDP targets | ≤ 6 | **3** |
| in-flight downloads | 0 | **0** |
| host memory available | > 2 GB | **4,729 MB** |
| `autoRecapture` persisted | false | **false** |

**Gotchas.**

- **Ordering is the whole trick.** Stop the spawner *after* closing tabs and they refill.
- **`--restore-last-session` was removed** in `7bfb1d7`, so a restart does **not** restore a tab pile.
- **The main process survives.** At 406 targets the 43-day-old main process persisted; only the tab
  *renderers* were culled. Do not assume a tab flood implies the browser restarted —
  `pgrep -c chromium` fell 119 → 12 with the same main PID.
- Recovery cost is **0**; every step is a management action.

**Attempt cost — 0.** Index figure, and confirmed: every step of the recovery is a management
action (flip a storage flag, close tabs, read the target list). No download attempt is spent, and
no request to Google is made at all.

**What v3 must change.**

1. **Make the guard state-based, not URL-pattern-based** — never re-open a tab until the previous one
   resolves, fails or times out.
2. **Bound `NEEDS_COOKIE` from the server side** with a max age or attempt cap. Parking indefinitely is
   what turns a transient logout into a resource leak.
3. **Delete or wire up the inert policy keys**, or read `chrome.storage.managed` for them too. A control
   that silently does nothing is worse than no control.
4. **Hard tab cap in the extension** (e.g. 5) so no future bug can escalate to 300+.

---

## 1.13 — Budget exhaustion

**Advertised cost: 0 to diagnose; catastrophic to truly fix. Measured 2026-09-19.**

**Symptom.**

A part will not download any more, or the operator is afraid to try. No loud error — the failure mode is
*an absence*: attempts stop being spendable, and a part sits partial forever.

**Root cause.**

Google allows roughly **5 downloads per archive file**. Every layer that retries, probes, re-mints or
resumes can consume that allowance if its accounting is wrong — and this project's accounting was wrong
for its whole life, because the cost was never measured.

**What is now measured** (see `measured-facts.md`):

| Action | ΔN |
|---|---|
| Request bounced to sign-in (client or browser) | **0** |
| `Range` request to the file host with the live jar | **0** |
| **Mint** — navigate the redirector and let it proceed | **1** |
| Mint attempt **aborted** mid-flight (CDP `Fetch`, `failRequest`) | **0 — and it does not mint at all** |

**So the budget model is:** a mint is **1**, a resume is **0**. A 5-attempt allowance buys **5 mints**, and
one mint plus unlimited free resumes finishes a part. **The budget is not the binding constraint** — but
only if the design refrains from re-minting.

**Caveat, stated plainly:** the manage-page counter is **not** a fine-grained oracle. The same action
produced Δ2 and then Δ0 before a **cache hit** was identified as the explanation. Treat `dl_counts` as
**telemetry, never as a ledger invariant.**

**Detection.**

Read the counter for the specific part on `https://takeout.google.com/manage/archive/<id>` — **not** on
`/manage`, which has no download links:

```bash
ssh takeout-server 'docker exec takeout-webgui python3 /tmp/tk_dom.py'   # see .recon/what_page.py
# look for:  <filename>.zip (Number of times already downloaded: N)
```
Warn at **N ≥ 3**. Treat **N ≥ 5** as exhausted.

**Recovery.**

- **Diagnose is free.** Reading the page costs 0.
- **A resumed part is recoverable at zero cost** while the allowance is not spent: prefer `Range` resumes,
  which are measured free, over any re-mint.
- **A truly exhausted part cannot be fixed.** The honest options are to wait for a **new export** or accept
  the loss. There is no trick. This is why the index marks the cost "catastrophic to truly fix".

**Verification.**

Counter stops incrementing for that part across repeated `Range` attempts, and the part completes with
`verify.py` reporting `STRUCT_OK`.

**Attempt cost — 0 to diagnose, catastrophic to truly fix** (index figure). Diagnosing is free: the
counter is a page read. Recovery is free while allowance remains — a `Range` resume is measured
**Δ0** and a re-mint is **Δ1**. Once the allowance is truly gone there is no recovery at all; that
asymmetry is why the index calls it catastrophic.

**What v3 must change.**

1. **Model the ledger around minting, not requests.** Minting is scarce; retries are free.
2. **Cache minted URLs in the ledger and never re-mint while one is valid.**
3. **Never abort a request to dodge an attempt** — that wastes the mint without buying anything (§4.1).
4. **Stop a part at a conservative cap and report it** rather than gambling the remainder.
5. **Store `dl_counts` as telemetry** and surface it to the operator, but never let a scheduler treat it
   as truth.

---

## 1.18 — Container OOM

**Advertised cost: 1/part in flight. Occurred live 2026-09-19.**

**Symptom.**

Host `free` shows **0 available**. `docker stats` shows the container at its ceiling
(`takeout-webgui mem=6.66GiB / 7.566GiB`). Pages render slowly or not at all; CDP still answers but the
browser is unusable. Tab renderers start disappearing.

**Root cause.**

The container has **7 GB total** with the browser, the manager daemon and Xvfb all inside it. The
proximate cause in this incident was **failure mode 1.9** — 314 sign-in tabs, one renderer each — which is
what makes 1.9 and 1.18 the same incident seen from two ends.

**What actually happened (measured).**

| | Before | After |
|---|---|---|
| CDP targets | **406** | **3** |
| Sign-in pages | 314 | **0** |
| Container memory | 6.66 GiB | **2.59 GiB** |
| Host available | **0 MB** | **4,742 MB** |
| Chromium processes | 119 | 12 |

**The main Chromium process did not restart** — PID `2585533` reported `43-05:04:01` uptime throughout.
Only the tab **renderers** were culled under memory pressure, and because `--restore-last-session` was
removed in `7bfb1d7` they never returned. The collapse was observed happening between two samples 90 s
apart, **with no intervention**.

**Detection.**

```bash
free -m | head -2
docker stats --no-stream --format '{{.Name}} mem={{.MemUsage}} cpu={{.CPUPerc}}' takeout-webgui
pgrep -c chromium
ssh takeout-server 'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/list"' | grep -c '"type"'
dmesg -T | grep -i "killed process" | tail -5     # OOM-killer evidence, if permitted
```

**Recovery.**

1. **Fix the spawner first (1.9)** — neutralise `autoRecapture`, or the memory comes straight back.
2. **Then reduce the target count.** Closing tabs is safe; killing Chromium is not (see gotchas).
3. **Do not restart Chromium as a first move.** It may be unnecessary — the renderer cull reclaims most
   memory on its own — and it discards a long-lived session.

**Verification.**

Host `available` back above ~2 GB, `docker stats` well under the limit, target count small, and the
extension service worker still alive (it hosts the spawner guard).

**Gotchas.**

- **The cheap win is the renderer cull**, and it happens without you. Measure before acting.
- **A restart costs more than it looks like.** The browser had been up 43 days; the profile persists at
  `/config/.chrome-profile` (LUKS volume, survives container re-creation), but in-memory session state
  does not. Whether a restart preserves Google auth is **UNVERIFIED**.
- **Container memory is shared.** Any v3 daemon living in the same container competes with the browser
  for the same 7 GB.

**Attempt cost — 1 per part in flight** (index figure). An OOM tears down in-flight streams, and
each one needs a fresh **mint** (**Δ1**) to resume — the bytes already on disk are kept, and the
`Range` resume that follows is free. Bystander parts that were not in flight cost nothing.

**What v3 must change.**

1. **Budget memory explicitly** — a hard cap on tabs, and a documented ceiling for the daemon.
2. **Never write unbounded loops** that allocate per tick (the root of 1.9).
3. **Detect pressure before it bites:** alert on available memory below a threshold rather than waiting
   for the kernel.
4. **Keep the daemon out of the browser container if possible**, or account for the shared ceiling.

| 1.23 | ZIP-only validator declares a complete mixed-format export broken | **0** — but invites a needless re-pull | [failure-modes-exports.md](failure-modes-exports.md) |

| 1.24 | A truncated part that every size check passes | **0** — but the bytes are unrecoverable | [failure-modes-storage.md](failure-modes-storage.md) |
| 1.25 | A re-scrape destroyed real part filenames | **0** — but completeness reporting went blind | [failure-modes-exports.md](failure-modes-exports.md) |
| 1.26 | A complete archive reported as BLOCKED 0/N | **0** — but it feeds a delete decision | [failure-modes-exports.md](failure-modes-exports.md) |
