# Measured facts — the reference v3 must be built against

Every row below was **measured on the live system on 2026-09-19**, not inferred or copied from the
v2 docs. Where a v2 doc assertion was contradicted, the doc is named.

**Full evidence:** `.recon/02-v2-defects.md`, `.recon/04-docs-archaeology.md`,
`.recon/10-decider-results.md`, `.recon/11-root-cause-confirmed.md`,
`.recon/12-ladder-results-and-architecture.md` — all in the **main checkout**
(`D:/_projects/takeout_downloader_script/.recon/`), untracked.

---

## The credential chain

| Step | URL | `rapt` | `authuser` |
|---|---|---|---|
| ReAuth challenge | `accounts.google.com/v3/signin/challenge/pwd` | — | `authuser=0` |
| Archive page, **after** satisfying it | `takeout.google.com/manage/archive/<id>?user=…&rapt=AEjHL4…` | ✅ yes | no |
| `data-download-uri` on that page (the **redirector**) | `takeout/download?j=<archive-uuid>&i=0&user=<21-digit>&rapt=…` | ✅ yes | no |
| The **minted** file-host URL | `takeout-download.usercontent.google.com/download/takeout-…zip?j=<uuid>&i=0&user=1005482974000&authuser=0` | ❌ **no** | ✅ yes |

**`rapt` gates MINTING, not transferring.** Satisfying the password challenge appends `rapt` to the
archive page URL → it propagates into the download links → the redirector consumes it → the file host
needs no `rapt` at all.

**Page location:** download links live on **`/manage/archive/<id>`**, *not* on `/manage`. The list page
only links to it. Any scraper or readiness check watching `/manage` will never see a download URI.

## The download host, measured

```
GET https://takeout-download.usercontent.google.com/download/takeout-…zip?j=…&i=0&user=…&authuser=0
    Range: bytes=0-1023
    Cookie: <28-cookie jar>
->  206
    ETag: f470fe32-76d6-43a6-9321-dbc76834919e-1005482974000-0     ← strong validator EXISTS
    Accept-Ranges: bytes                                           ← Range resume is viable
    Content-Range: bytes 0-1023/261259                             ← correct total reported
    Content-Type: application/octet-stream
    Content-Disposition: attachment; filename="takeout-…zip"
    body starts PK\x03\x04                                         ← real zip bytes
```

Without a `Cookie` header the same request returns **302 → `accounts.google.com/ServiceLogin`**. The
file host **is** cookie-authenticated, and a **replayed jar works**.

## Attempt costs (Google's own counter is the oracle)

| Action | ΔN |
|---|---|
| Client request bounced to sign-in (any combination of cookie/Range) | **0** |
| Browser navigation to the redirector, **bounced** to the challenge | **0** |
| Browser mint **+ full download** of the 261 KB part | **2** |
| Client `Range` request to the file host with the jar → `206` | **0** |

**Model (PROVISIONAL — see the retraction below):** minting is the scarce resource; retries are free.
→ **mint once, then resume aggressively; never re-mint.**

### ⚠️ RETRACTION — the attempt-cost numbers are NOT settled

**A later run contradicted the "2 per part" figure, and it must not be relied on.**

| Run | Action | Counter |
|---|---|---|
| ladder (auto-cancel **OFF**) | browser mint + one full download of the 261 KB part | 0 → **2** |
| mint2 (auto-cancel **ON**) | browser mint + a download that **completed** (`chrome.downloads`: state `complete`, 261,259 bytes) | 2 → **2 (Δ0)** |

**RESOLVED (later, cleanly): a MINT costs exactly Δ1.** A control run that navigated the redirector
plainly and observed only measured `counter 2 -> 3` — **Δ+1** — with the stray download cancelled and
erased by the extension (no new `chrome.downloads` entry). That also explains the earlier confusion: the
Δ2 was a mint *plus* a client-side fetch, and the Δ0 was a **cache hit**.

| Action | ΔN |
|---|---|
| Request bounced to sign-in (client or browser) | **0** |
| `Range` request to the file host with the jar | **0** |
| **Mint** (navigate the redirector and let it proceed) | **1** |
| Mint attempt that is **aborted** under CDP `Fetch` interception | **0 — and it does not mint at all** (bounces to the archive page) |

**Budget model, now measured:** a mint is **1** and a resume is **0**. A 5-attempt allowance buys **5
mints**; one mint plus unlimited free resumes finishes a part. **The budget is not the binding
constraint.**

**Caveat retained:** the counter still is not a *fine-grained* oracle — it gave Δ2 and Δ0 for superficially
similar actions before the cache-hit explanation was found. Treat it as **telemetry**, not a ledger
invariant, and prefer the conservative reading.

**Also established by that run** (see `chrome.downloads` state via the extension):

- **The auto-cancel DOES work** — there is an `interrupted / USER_CANCELED` entry proving the listener fires.
- **But it loses the race on small files.** The listener does two async hops
  (`chrome.downloads.onCreated` → `chrome.storage.local.get` → `cancel`); a 261 KB download finishes first,
  so it completes uncancelled. A 50 GB Takeout part would lose that race to us instead. **So auto-cancel is
  effective for the real workload and useless as a measurement guard on a tiny burn export.**

### ⚠️ The counter is NOT a trustworthy oracle — and I could not make it one

Further attempts to pin down the cost, all inconclusive:

| Attempt | Result |
|---|---|
| Serialised sampling — counter re-read every 30 s for ~5 min, nothing else touching Google | **stable at 2** — no lag observed |
| Cache-bypass — page-context `fetch(redirector, {cache:'no-store'})`, to discriminate "cache hit" from "lazy counter" | **`TypeError: Failed to fetch`** — the redirect lands cross-origin and the file host sends **no CORS headers**, so page JS cannot read it at all |
| After that failed fetch, counter re-sampled every 30 s for 90 s | still **2** (correctly — no fetch actually happened) |

Two candidate explanations remain **undiscriminated**:

- **(a)** the counter counts Google-side fetches, and the second download was served from **Chrome's cache**, so Google never saw it → the counter is truthful;
- **(b)** the counter is lazy / does not count per download → it cannot be used for accounting.

Consistent facts: **2 completed browser downloads exist and the counter reads 2** — which fits (a) for the
second download but not for the first, where the counter already read 2 after a *single* download. I cannot
reconcile that from the data in hand.

**Conclusion — stop perfecting the instrument, and design for uncertainty:**

1. The v2 ledger's premise — a precise, immediate, per-download counter — is **not verified**, and may not
   be verifiable at this granularity by indirect probing.
2. What **is** consistent across every run: **failed/bounced requests cost 0**, and **`Range` requests
   against the file host cost 0**. Those are the operations a scheduler should prefer.
3. Treat `dl_counts` as **observed telemetry, not a ledger invariant**. Assume a small per-part budget,
   never re-mint needlessly, and lean on free `Range` resumes.

### A hard constraint for any in-browser measurement

**The file host sends no CORS headers.** Page-context JavaScript cannot fetch a minted URL and read the
response — a cross-origin redirect fails with `TypeError: Failed to fetch`. Any in-browser measurement, and
any v3 inspection, must use **CDP `Network` events** (which bypass CORS) or let Chrome perform the download
and read `chrome.downloads` state — **not `fetch()` from the page**.

## DOM facts on the archive page

| Fact | Measured | v2 doc claim |
|---|---|---|
| `data-download-uri` | present; a **relative** path (`takeout/download?…`), *not* absolute | "exact per-part URLs" — misleading |
| `data-size` | present (`261259`) ✅ | ✅ |
| `dl_counts` regex `…(Number of times already downloaded: N)` | **works** ✅ | ✅ |
| `authuser` | **absent on the redirector**, present on the file host | "always present" ❌ |
| `rapt` / `sig` on the redirector | `rapt` ✅ (once challenged), `sig` ❌ | — → short-TTL redirector model |
| `aria-label` "part X of N" | **absent** on this 1-part export | v2's completeness oracle derives `N` from it ⚠️ |

## Environment

| Fact | Value |
|---|---|
| Chromium | `149.0.7827.155` (the docs' "149" was accurate) |
| CDP endpoint | `ws://127.0.0.1:9222/devtools/browser/<uuid>` — **bare `ws://host:port` returns HTTP 404**; the UUID changes per browser restart |
| Cookie jar | 49 cookies total, **28 `google.com`**, ~3.5 KB assembled header; includes `__Secure-1PSIDTS` |
| Python in container | 3.13.5 with `websockets` + `aiohttp`; **no `websocket-client`** |
| Python on workstation | no `python` on `PATH`; `.venv-manager` has neither `pytest` nor `websocket`; only `/c/Users/User/anaconda3/python.exe` works |
| v2 test suite | ⚠️ **`8 failed, 464 passed` of 472 collected** (7.2 s) — **not** "472 pass". The 8 failures are **environmental**, not code: `C:` is **99 % full (14 GB free of 952 GB)** and the project's own `DEFAULT_MIN_HEADROOM = 20 GiB` guard (`takeout2/preflight.py:56`) refuses the writes the tests perform into `tmp_path`. Only runnable under the global Anaconda interpreter. |
| v3 test suite (new) | **23 passed** — and deliberately touches **no disk and no network**, so it passes on the same 99 %-full host. |
| Archive mount | `/opt/archives` = rclone remote (`archives:` over `archive_chunked:`), config **password-encrypted**; **provider unconfirmed**; **no pCloud remote exists**; **one copy only** |
| Staging disk | `/dev/mapper/cache_crypt` (LUKS+xfs) 300 G, **293 G free**; root `/` has only **13 G free** |

### ⚠️ The v2 suite fails on a full dev disk — and that is a design smell

Measured 2026-09-19: **`8 failed, 464 passed`** of 472 collected. Every failure is the same thing —
`takeout2/engine.py:325` refusing to write because the pytest `tmp_path` sits on a `C:` drive with
**14 GB free**, below the **20 GiB** `DEFAULT_MIN_HEADROOM` floor.

**Nothing to do with any code change** — verified by moving `autopilot/`, `conftest.py` and `tests/v3/`
entirely aside and re-running: still 8 failures.

Two consequences worth keeping:

1. **"472 tests pass" was never true.** 472 is the *collected* count. The claim appears in `decisions.md`,
   the prior session's notes and the recon report; the real figure is **464/8**, and it is
   **host-disk dependent**, so it will change again as this disk fills or is freed.
2. **The guard that protects production breaks the test suite.** `EngineConfig.require_mount` documents a
   "False for local dev and tests" escape (`takeout2/engine.py:103`) — but `min_headroom` has **no such
   escape** (`preflight.py:56,277,334` only). So a dev box with a nearly-full system drive cannot run its
   own tests, and the failure looks like a code regression. **v3's tests must be disk-independent** — the
   23 new v3 tests are, deliberately, and pass on this same 99 %-full host.

## The founding misdiagnosis

The project's entire premise — *capture the cookie, replay it from another client* — treated a
**password ReAuth challenge** as a **cookie-lifetime** problem. A replayed jar cannot mint because it
carries no `rapt` and cannot answer an interactive prompt: **impossible by construction, not a bug.**

The docs' "~1–2 min idle cookie death" is the redirector's `rapt` requirement. The docs' "~45 min
heartbeat" is the ReAuth window. Neither was ever named correctly.

The one export that ever finished (3.08 TB, by hand) worked because a human satisfied the challenge and
`curl` then transferred with the jar — **exactly the hybrid this measurement now prescribes.**

---

## Corrections and additions — 2026-09-19, after the first successful run

**A rapt is issued by hitting the redirector, with no interactive challenge.** Measured on the fresh export
`f9a17be0-…`: navigating the redirector `takeout/download?j=…&i=0&user=…` — carrying no token at all —
returned

```
302 -> .../manage/archive/<id>?user=...&pli=1&rapt=<fresh>
```

**No password prompt, no `challenge/pwd` page.** The archive page then served tokened download links and the
mint succeeded (Δ1). This **qualifies the founding misdiagnosis below**: the ReAuth challenge is what you
hit when the *existing* session is stale, but a fresh export hands out a rapt on first contact. Any design
treating the human step as unconditional is wrong for a first run.

Three further measurements from the same session:

* **Google caps downloads per export at 5.** The refusal arrives as `quotaExceeded=true` on the
  archive-page bounce, and the page states it: *"You can try to download a file only 5 times."* Terminal —
  a retry is guaranteed to fail. See runbook 1.19.
* **The ledger's attempt count and Google's counter are different quantities, and only the latter runs
  out.** A run reported `mint 0 | transfer 0` while Google's counter went 3 → 5, because a bounced mint is
  never recorded as a spent attempt. Budget diagnostics against `dl_counts`, never the ledger.
* **A scraped part filename is always the literal string `download`.** The redirector path is
  `takeout/download?j=…`, so the basename carries no part identity — a multi-part export collides on one
  name. The real name appears only on the *minted* URL
  (`…/download/takeout-20260919T163231Z-1-001.zip?j=…`), which is why it cannot be recorded at scrape time.

**A completed run, for reference** (archive `f9a17be0-65e2-4b1f-a9c6-04c840204419`, 3 products, 1 part):
verdict `COMPLETE`, exit 0, `36670 of 36670` bytes, mint 1 / transfer 1 / resume 0, landed as
`takeout-20260919T163231Z-1-001.zip` with a sha256 **identical between staging and the archive mount**.


## Measured 2026-09-21

* **The full 64-product export is 141.28 GB in 19 parts**, not the ~131.6 GB an earlier
  size-up reported — that estimate summed only the `.zip` parts and omitted the 13.58 GB
  `.mbox`. Exact totals come from Google's own per-part sizes, so read them from the
  ledger, never from a directory listing.
* **The download allowance is 5 attempts PER PART, not per export.** Page text: "You can
  try to download a file only 5 times", with per-zip counters. A 19-part export therefore
  carries 95 attempts, and a competing pull spends the same allowance as the original.
* **A minted URL is not the only way to get bytes, but it is the only way to get them
  without the browser.** The transfer pass consumes no minting, so it is genuinely
  window-independent: the 5-part test transferred 2.65 GB with `mint 0` on that pass.
* **v2 wrote the mbox as `All mail Including Spam and Trash-002.mbox`** — decoded. v3
  produced the percent-encoded form, which is how the filename bug was found.
* **`/opt/archives/google-takeout/<account>/` directories are named for the export
  timestamp** (`2026-09-19-04-27-12` for `takeout-20260919T042712Z`), not for the archive
  id. Pairing an archive id to its directory requires the timestamp from the part names.
* **rclone's VFS cache held 97 GB** (`/opt/local_cache_crypt/rclone_vfs`, `drwx------ root`)
  against `--vfs-cache-max-size 100G`, on the same 300 GB volume as staging. Any staging
  plan must budget for it: 194 GB free vs ~232 GB wanted for a naive full pull.
* **`--mint-only` exits 1, not 0.** Only `complete` maps to 0; `incomplete` means work
  remains, which is true after minting.
* **A 20-second no-growth window plus exact per-part byte counts is enough to say a pull
  has stopped**, without reading the data: no file grew, and 19/19 sizes matched exactly.
* **`zipfile.testzip()` reads every byte** — ~10+ minutes on 127 GB of JuiceFS, and it is
  not needed to answer "is the file whole?". A truncated zip loses its EOCD, so two seeks
  per file is the right instrument. The heavy CRC check is a separate, deliberate pass.
