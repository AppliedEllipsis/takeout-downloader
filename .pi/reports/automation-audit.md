# Automation audit — v3 autopilot (unattended archival)

Date: 2026-10-13. Active root: `D:/_projects/takeout_downloader_script/.claude/worktrees/takeout-autopilot`
(branch `feat/takeout-autopilot`, HEAD `1b70ce2`). Findings-only; no source, git, service or download
state was modified. Every claim below was re-read from the code on disk, not copied from the wiki.
Secrets/signed URLs/cookie values are never printed.

---

## 0. What actually exists (baseline)

The autopilot is a **one-shot, single-export CLI**. There is no scheduler, daemon, watcher, cron
entry, systemd unit or s6 service that invokes it. Confirmed three ways:

* `autopilot/__main__.py:53-70` — `run` requires `--archive-id`, `--ledger`, `--staging`, `--archive`
  (all `required=True`); `report` requires `--archive-id`+`--ledger`.
* `webgui/Dockerfile.webtop:57-63` installs exactly **two** long-running services —
  `/custom-services.d/takeout-manager` (the v2 manager) and `/custom-services.d/takeout-chromium`.
  Neither runs the autopilot.
* `grep -rn "python -m autopilot\|run_once_sync" *.sh webgui/ docker-compose*.yml` → no result;
  the only callers are `__main__.py` and `tests/v3/`.

So all current operation is: a human edits `04-FULL-PULL.md` commands, `ssh` + `docker exec`, and runs
`python3 -B -m autopilot run …` once, then again, then again.

---

## 1. Why a fresh export needs manual commands — CONFIRMED

**H1 (High). No discovery of archives or accounts.**
There is no code that enumerates Google's export list. `autopilot/scrape.py:45` (`READ_JS`) reads only
`document.querySelectorAll('[data-download-uri]')` on **one already-known** archive page;
`scrape.read_archive` (`scrape.py:97`) takes a URL. `authuser` appears in the package **only in a
comment** (`mint.py:202-203`) — confirmed:
```
grep -rn "authuser" autopilot/ tools/   # only mint.py comments; no code reads it
```
`ledger.py` is keyed by a single `archive_id` (`ledger.py:320` `job()`; no `jobs()`/list method —
`grep -n "list_jobs\|SELECT \* FROM jobs" autopilot/ledger.py` returns only `job()`).
Consequence: for a fresh export the operator must open the manage page, copy the `j=` archive id and
the account, choose a ledger/staging/archive path, and type all four flags. Nothing derives them.

**H2 (High). No readiness poll for a fresh/in-progress export.**
A page with zero `data-download-uri` links is reported as terminal `failed` "no parts were scraped"
(`run.py:517-523`), never as "not ready yet, wait". So the operator must watch the Takeout UI until the
export flips to *Completed*, then run the CLI by hand.

**H3 (High). The only safe long-pull shape is two manual invocations.**
`run_once` mints inside the transfer loop (`run.py:672`), so a multi-hour export can exhaust the
~45-minute ReAuth window mid-loop. The documented workaround is `--mint-only` first, then transfer
(`docs/v3/04-FULL-PULL.md` Step 3/Step 4), and `--mint-only` exits **1** on success
(`__main__.py:86-92`, `run.py:829`), so chaining is error-prone. There is no
"mint-all-then-transfer" mode inside one process.

---

## 2. Missing multi-account discovery / scheduling — CONFIRMED

**H4 (High).** Same evidence as H1/H2/H3. Additionally:
* The scrape's `parts` come from a single archive URL; the `/manage` list page is never read. The
  measured fact that `/manage` *truncates* its list is not even relevant yet because nothing reads it.
* `RunConfig` has an `account` field (`run.py:111`) but it is metadata only — it is stored on the job
  (`ledger.py:294`) and never used to build a path or select a session.
* No run-level lock/lease exists. Two operators (or a v2 manager transfer) can pull the same export
  concurrently and split the **5-attempts-per-part** allowance; the wiki warns about this race
  (`.wiki/journal.md` 2026-09-21) but no code enforces exclusivity.

---

## 3. ReAuth loop / manual re-auth — CONFIRMED

**H5 (High, and the sharpest defect). The autopilot navigates the tab *away* from the very challenge
page its own tool needs.**
`mint()` raises `NeedsReauth` **after** calling `_leave()`:
```
mint.py:504  await _leave(session, restore_url, strays)
mint.py:505  raise NeedsReauth(r.location)
mint.py:396  async def _leave(...):  -> _restore(session, restore_url)
mint.py:581  async def _restore(...): await session.call("Page.navigate", {"url": url})
```
`restore_url` is the rapt-bearing archive page (`run.py:504`), so after a challenge the tab is parked
on `takeout.google.com`, not on `accounts.google.com/v3/signin/challenge/pwd`. But the remediation tool
only acts on a tab that is *currently* on the accounts host:
```
tools/satisfy_login.py:111-116  pick_signin_target(): return first page whose url contains "accounts.google.com"
```
So immediately after a `needs_reauth`, `satisfy_login.py` finds nothing and prints
`RESULT: no accounts.google.com page target — nothing to satisfy.` (exit 0, `satisfy_login.py:130-132`).
The operator must manually re-provoke the challenge before re-auth.

**H6 (Medium). ReAuth is never automated or alerted; it is a status only.**
`run_once` returns `needs_reauth` with no retry/backoff and no notification
(`run.py:506-511`, `run.py:755-760`). `manager/notify.py` (Telegram) exists but is imported by
**nobody** in `autopilot/` (`grep -rn "notify\|telegram" autopilot/` → empty). `tools/satisfy_login.py`
is never imported by the autopilot; it is a manual tool. `errors.NotAuthorised` is defined
(`errors.py:65`) but never raised anywhere, so a tab already parked on sign-in before a run is not
detected and just fails later.

**H7 (Medium). The v2 manager can still re-drive `NEEDS_COOKIE` jobs.**
Migration step 3 ("stop the manager driving jobs") is still marked NEXT
(`docs/v3/03-OPERATIONS.md` §5); `manager/app.py:246` still selects
`[j for j in orch.list_jobs() if j["status"] == J.NEEDS_COOKIE]` and `/api/control/recapture` still
exists (`manager/app.py:282`). Currently inert only because the extension's alarm is off (H9).

---

## 4. Stale URLs — CONFIRMED

**H8 (Medium-High). Cached minted URLs are never age-checked.**
`record_mint` writes `minted_at` (`ledger.py:402-411`), but `minted_at` is **read nowhere**:
```
grep -rn "minted_at" autopilot/ tools/   # ledger.py:232 (schema), 407 (write), 429 (clear). No SELECT/read.
```
Invalidation happens only reactively: `NeedsReauth`/`RemoteChanged` clear the URL, or three failed
transfers do (`run.py:717-762`, `MAX_CACHED_URL_FAILURES=3` at `run.py:99`). A URL cached near the
export's expiry, or cached days earlier, is reused blindly. The transfer then classifies the resulting
`302 -> ServiceLogin` as `NeedsReauth` (`transport.py:263-267`), which the operator reads as "session
expired" even when the real cause is an expired export. There is no proactive re-mint keyed on
`minted_at` or on `page.expiry`.

---

## 5. Crashes / restarts — CONFIRMED

**H9 (Medium). A Chromium restart or CDP death kills the run and nothing restarts it.**
`CdpError` is an `AutopilotError` (`cdp.py:73-86`) and is caught into a `failed` job status
(`run.py:839-861`), not retried. `chromium-service.sh` self-heals the **browser**, but no service,
cron or loop restarts the **autopilot**. `run_once` holds one page target for the whole run
(`run.py:237-256`, `_default_session_factory`), so if that tab is closed or navigated mid-run the run
aborts with `failed`. For a multi-hour pull this is not "unattended": one browser blip ends it.

---

## 6. Browser interference — CONFIRMED

**H10 (Medium). The autopilot drives an existing tab with no isolation.**
`pick_page_target` (`run.py:237-256`) reuses a page already on `takeout.google.com`; it never opens a
dedicated tab. The same Chromium is shared with the human (KasmVNC), the extension popups, and the v2
manager. A run navigates that tab to the archive page and through mint redirects, hijacking whatever
the operator was doing, and conversely an operator click can steal the tab mid-mint.

**H11 (Medium). Stray mint downloads can accumulate unbounded when the download-events enable fails.**
`_enable_download_events` returns `False` on any refusal (`mint.py:357-368`); strays are then only
collected `if events_enabled:` (`mint.py:470`). The cancel also only fires inside `mint()` via
`_leave` (`mint.py:396-400`). With `autoCancelDownloads` OFF (owner directive, 2026-09-22) a mint
leaves a real part-sized download in `/config/Downloads`; any mint that could not register for events
leaves it **forever**. `tools/sweep_downloads.py` exists but `run_once` never calls it.

---

## 7. Runtime launch configuration — CONFIRMED (one latent item)

**H12 (Low, latent). `init_custom.sh` writes a managed policy with `autoRecapture: true`, contradicting
the fail-closed fix.**
`webgui/init_custom.sh:101-109` writes `takeout-manager.json` containing `"autoRecapture": true`.
The extension reads `autoRecapture` from **storage.local only** (`helpers/background.js:219-231`,
`743-753`) and creates the poll alarm only on `=== true` from local (`background.js:434-436`), so the
managed value is currently **inert**. But it is a live re-arm of the 314-tab flood
(`.wiki/journal.md` 2026-09-19) the moment any read path starts consulting managed storage (the code
already reads `captureToken` from managed at `background.js:303-306`). The launcher flag itself is
correct: `--load-extension=/work/.v3/helpers` (`init_custom.sh:47`).

**Clean:** `chromium-service.sh` supervision loop (no `exec`-and-pile), `mint.py`'s deliberate
"observe-only / never enable `Fetch`" invariant, `cdp.py` bounded event buffer + fail-fast reader, and
`transport.py` `Range`/`If-Range`/ENOSPC handling all read as intended. `tools/` scripts are
report-only by default.

---

## 8. Minimal extension proposal (current APIs, exact names)

Everything below reuses existing functions; nothing requires a new dependency.

1. **Discovery — add `scrape.list_archives(session) -> list[ArchiveRef]`** in `scrape.py`, reusing the
   `CdpSession` + `READ_JS` pattern (`scrape.py:80-118`) but with a JS reader for `/manage` that
   collects each archive row's `j=` id, `user`, `authuser`, status and `Available until` text. Then for
   each archive call the existing `scrape.read_archive(session, scrape.archive_url(archive_id),
   settle=cfg.settle)` and `scrape.recover_rapt_url` / `scrape.provoke_rapt` exactly as `run_once`
   already does (`run.py:474-563`).

2. **Enumerate ledger jobs — add `Ledger.jobs() -> list[sqlite3.Row]`** alongside `Ledger.job()`
   (`ledger.py:320`), i.e. `SELECT * FROM jobs ORDER BY updated_at`. Needed so a scheduler can report
   or resume every archive without being told its id.

3. **Scheduler entrypoint — add an `autopilot/schedule.py` (or a `plan` subcommand in `__main__.py`)**
   that: opens one session via `run._default_session_factory(cfg.cdp_http)`; calls
   `scrape.list_archives`; for each archive builds `run.RunConfig(archive_id=…, ledger_path=…,
   staging_dir=…, archive_dir=…, account=…, min_headroom=…)` and calls
   `run.run_once_sync(cfg)`; then switches on `RunOutcome.status` using the existing
   `__main__._EXIT_FOR_STATUS`. Retry with backoff on `"failed"`/`"incomplete"`; stop on
   `"expired_unrecoverable"`/`"quota_exceeded"`; on `"needs_reauth"` call
   `tools.satisfy_login.run(dry_run=False)` once, then re-run. This is the single missing piece that
   turns the CLI into an unattended loop.

4. **Fix the re-auth trap (H5) — add `leave_tab: bool = True` to `mint.mint()`** (`mint.py:414`). On
   the `NeedsReauth` path, when `leave_tab is False`, skip `_restore` and leave the tab on the
   challenge so `tools.satisfy_login.pick_signin_target` (`satisfy_login.py:111`) can act. Pass
   `leave_tab=False` from the scheduler's reauth branch. (Alternative, no signature change: have
   `run_once` call `tools.satisfy_login.run()` before returning `needs_reauth`.)

5. **Proactive re-mint (H8) — add `Ledger.minted_age(archive_id, idx) -> Optional[float]`** reading
   the already-written `minted_at` (`ledger.py:407`), and in `run_once` before using
   `ledger.minted_url(...)` (`run.py:666`) re-mint when the age exceeds a TTL or when
   `report.parse_expiry(page.expiry)` is reached. This replaces the purely reactive clearing.

6. **Unconditional stray cleanup (H11)** — call `tools.sweep_downloads`' cancel path (or
   `Browser.cancelDownload`) when `_enable_download_events` returns `False` (`mint.py:445`), so a mint
   that cannot register for `Browser.downloadWillBegin` still cannot leak a part-sized file.

---

## 9. SUSPECTED (not confirmed)

* **S1.** Whether the running container's extension honours the managed `autoRecapture:true` at all
  (H12) is unverified from here without the browser; code says no, a live probe via
  `tools/extension_switch.py` would settle it.
* **S2.** Whether a concurrent v2 manager transfer is actually in flight for any current export
  (`manager/app.py:222`) is a live-state question; not checked (server access out of scope).
* **S3.** The exact failure mode when the extension or human closes the autopilot's page target
  mid-run is inferred from `CdpError` semantics; not reproduced.

---

## Top findings (summary, <300 words)

1. **There is no unattended automation.** The autopilot is a single-export CLI
   (`__main__.py:53-70`) and no cron/systemd/s6 service invokes it (`Dockerfile.webtop:57-63`). Fresh
   exports therefore require hand-copied `ssh`+`docker exec` commands.
2. **No multi-account/archive discovery.** `authuser` is never read; nothing enumerates exports;
   `ledger.py` has no job list. A scheduler has no source of work.
3. **The re-auth path defeats its own tool.** `mint()` navigates the tab back to the archive page
   before raising `NeedsReauth` (`mint.py:504-505`, `_leave`→`_restore`), so
   `tools/satisfy_login.py:111-116` then finds no `accounts.google.com` target and does nothing. Re-auth
   is a manual, off-tool click-chain.
4. **Cached minted URLs are never age-checked.** `minted_at` is written but never read; invalidation is
   reactive only. Stale URLs surface as a misleading `needs_reauth`.
5. **A browser blip ends a run with no restart**, and a mint that cannot enable download events can
   leak a part-sized file permanently (events gated at `mint.py:470`).
6. **Latent flood re-arm:** `init_custom.sh:109` writes `autoRecapture: true` into managed storage,
   inert only because the extension reads local storage.

Minimal fix: add `scrape.list_archives`, `Ledger.jobs`, a thin `autopilot/schedule.py` loop over
`run.run_once_sync`, a `leave_tab` flag on `mint.mint`, and a `Ledger.minted_age` TTL re-mint — all
using existing APIs.
