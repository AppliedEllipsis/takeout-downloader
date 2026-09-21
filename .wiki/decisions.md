# Decisions (ADR log — append-only)

Newest first. Never rewrite history; supersede with a new entry noting
`Supersedes: <date>`.

---

## 2026-09-19 — No stored credential: 2FA makes human-in-the-loop mandatory

**Context.** The owner offered to place a Google password in the container so v3 could satisfy the ReAuth
challenge unattended. Asked whether 2-Step Verification is enabled, the owner answered **yes — SMS or
Google prompt**, and asked for stored-credential mode kept permanently.

**Decision: do not store a credential, and do not build stored-credential mode.**

**Rationale — the proposal cannot work.** With SMS/prompt 2FA, a password advances the flow only to a 2FA
demand that no script can answer. The password would buy exactly one step towards a wall, while placing
the single largest secret in the system into a container that also runs a browser, an extension and a
public-facing dev server. That is pure downside.

Additional correction: **Google has no per-app "temp" password for web sign-in.** The only way to supply
one would have been to change the real account password — invalidating existing sessions and risking an
unfamiliar-login flag — with no benefit given 2FA.

**Consequences.**

1. **Human-in-the-loop is mandatory for this deployment**, not one option among several. §4.2 of the v3
design now states this as a measured constraint.
2. **v3 stores no secret at all** — a materially better security posture than the proposal, and one that
falls out of the constraint rather than being a compromise.
3. **Alerting becomes load-bearing.** Because re-auth recurs (the `rapt` window), the notification must be
actionable and job state resumable mid-window. `manager/notify.py` (Telegram) already exists.
4. **Experiments must be batched around one auth window.** Every remaining test needs a mint, and a mint
needs authorisation, so the tests are grouped rather than triggered one at a time.
5. **The only route to unattended operation** would be migrating 2FA to an authenticator app and supplying
the TOTP seed — which, together with the password, would **collapse 2FA entirely** for that account.
Recorded so the trade-off is explicit; **not recommended**.

---

## 2026-09-19 — MEASURED: the architecture is the hybrid, and the founding premise was a misdiagnosis

**This ADR is determined by measurement, not argument.** It supersedes the conditional adjudication
below, whose one open condition is now resolved. Full evidence:
`.recon/11-root-cause-confirmed.md`, `.recon/12-ladder-results-and-architecture.md`; reference table in
[measured-facts.md](measured-facts.md).

### The root cause, finally named

The Takeout download URL demands an **interactive ReAuth (password) challenge** — Google's
"To continue, first verify it's you. Enter your password." We reached it with the *real browser*, a
*live* session, the right IP and UA. It has nothing to do with cookie lifetime.

Satisfying it appends **`rapt=`** (ReAuth Proof Token) to the archive page URL, which propagates into
the download links. **`rapt` gates MINTING, not transferring** — the minted file-host URL carries no
`rapt` at all.

So a replayed cookie **cannot mint, ever**: it has no `rapt` and cannot answer an interactive prompt.
**Impossible by construction — not a bug to fix.** Nine months and three architectures were spent on
it.

### The four deciders — answered

| Question | Measured answer |
|---|---|
| Does a `Range` resume cost an attempt? | **NO — ΔN = 0.** Per the adjudication's own rule, this resolves the verdict to **the hybrid / Option B** |
| Do strong validators exist on the file host? | **YES** — `ETag: f470fe32-…-1005482974000-0`, `Accept-Ranges: bytes`. The feared "Chromium resume is dead on arrival" does **not** apply |
| Is the file host cookie-independent? | **NO** — no cookie → `302 ServiceLogin`. But a **replayed jar works** (`206` + real zip bytes) |
| What does acquiring one part cost? | **2 attempts**, not 1 |

### Decisions

1. **Adopt the hybrid.** Browser mints (it must — nothing else can) → a client transfers with the jar,
   `Range`-resuming freely. This is exactly what the one hand-finished 3.08 TB export did.
2. **Delete cookie replay rather than repair it.** `takeout2/cookie.py`, the `Storage.getCookies`
   capture path, and the capture→POST→replay pipeline are dead weight. The scheme/endpoint bugs found
   earlier are moot once the premise is abandoned.
3. **Re-model the ledger around minting, not requests.** Minting is the scarce resource; retries are
   free. `CostClass.PROBE`/`PAYLOAD` mis-prices both. Correct strategy: **mint once, then resume
   aggressively; never re-mint.**
4. **ReAuth is a first-class, unavoidable step.** A human must answer the challenge periodically, or
   the design stores a credential and fills the form. **That is a security decision for the owner**,
   not an implementation detail — do not slip it in.
5. **Budget honestly.** The real allowance is ~**2 full part-acquisitions**, not the 5 the design
   assumed. Every scheduling choice must be re-derived.

### Consequences

- The docs' "~1–2 min idle cookie death" is the redirector's `rapt` requirement; the "~45 min
designed heartbeat" is the ReAuth window. Both were named wrongly, which is why every fix missed.
- The v2 "zero-probe discovery" completeness oracle derives `N` from an
  `aria-label="part X of N"` that **was absent** on the export tested. It may be omitted for
  single-part exports; the oracle needs an `N=1` path.
- **Still unmeasured and now the top priority: the `rapt` window length** — replace the docs' four
  contradictory figures (1–2 min / <30 min / ~45 min / ~1 h) with one number, and learn whether a
  minted URL outlives it (which decides whether URLs can be batched up front).

---

## 2026-09-19 — Adjudication returned **hybrid = Option B**, conditional on one unmeasured fact

**Decision taken from evidence, not preference.** `.recon/08-arch-adjudication.md` (721 lines,
confidence-tagged: *verified / well-attested / anecdotal / unresolved / contested*).

**Verdict.** A real Chromium in the container (same egress IP, same UA, same session)
authenticates, clears the ReAuth challenge, and **mints** the signed
`takeout-download.usercontent.google.com` URL per part. A **controlled HTTP client of ours** moves
the bytes with `Range` resume, rate cap, stall detection and the per-part attempt ledger. The
browser is mandatory — but as the **minting authority**, not necessarily as the transport.

**The decisive negative finding.** Option A's stated justification — "bypass the cookie problem
entirely" — is **not supported**. Chromium's own download stack carries the user's cookies
(`cookies_allowed: YES, cookies_store: "user"`, read from Chromium source). Option A does not escape
cookies; it only changes *who attaches them*.

**Why the browser is mandatory in both options.** `rapt=` is the ReAuth Proof Token, minted by an
interactive browser action, gating the **redirector**, satisfied once per authorization window (not
per file). A headless client cannot satisfy it. Note this is a *different* role from "the thing that
writes bytes".

**The condition the verdict rests on** (never measured in this project):

| Condition | Winner |
|---|---|
| A `Range` resume against an already-minted URL does **not** increment the counter | **B, clearly** |
| It **does** increment the counter | **A and B tie on budget** — B keeps only a control/observability advantage (rate cap, stall detection, accounting). **The verdict flips, and the owner's preference for A should then carry it.** |
| The file host requires a **live** cookie jar (DBSC / `__Secure-1PSIDTS` rotation) | **A wins outright** — only the browser can hold a valid session at request time |

**The honest dissent, recorded rather than buried (report §3).**

1. **Our own hardest data point favours B:** the only at-scale success in this repo's history was
   Option-B-shaped (4 parallel `curl -C -` against a live CDP jar, 3.08 TB). **Option A has never
   been tried here against a real export** — the `local-lessons` recommendation is a *design
   inference*, not a result. The report says plainly: 
   *"which is precisely why the verdict should be held loosely"*.
2. **B's mechanism is on a deprecation path.** `POST /RotateCookies` declares a **600 s** rotation
   interval (a plausible mechanical cause of the observed 1–2 min death), and **Device Bound Session
   Credentials** went GA for Chrome on Windows in May 2026 covering personal Google accounts — a
   device-bound credential **cannot be replayed by an HTTP client at all**. Linux/Debian status is
   unnamed, so this is a risk rather than a present fact, but it is a **one-way ratchet**: B's value
   decays, A's does not. A multi-year design should weight that.
3. **`conradstorz`'s ADR is also not a measurement.** Its headline reason is a *capability* argument,
   not a *budget* one — it never claims Chromium spends more attempts.
4. **`tarballz/mass-takeout-downloader` is a working, shipping Option A:** an MV3 extension driving
   `chrome.downloads` with bounded concurrency (1–5, default 3), `resume()` on `canResume`, a 30 s
   `chrome.alarms` stall watchdog, surviving service-worker teardown. It also documents the one real
   trap: `chrome.downloads.download` saves whatever it gets, so an HTML auth response lands as a
   `pwd.htm` file — navigating a tab instead lets the browser honour content-type.

**Consequence — the cheapest next steps are nearly free.** Reports §4.0–§4.3 give a 4-step ladder
with stop rules:

1. **§4.0 (free, no attempts, and may decide it in under a minute):** one `HEAD` or
   `Range: bytes=0-0` against a minted URL, read `ETag`/`Last-Modified`. If neither exists,
   Chromium's resume is **dead on arrival** (`kAllowDownloadResumptionWithoutStrongValidators`
   is `FEATURE_DISABLED_BY_DEFAULT` on desktop) and Option A loses `Range` resume entirely. Also
   record the URL shape: a `sig=` parameter implies the long-lived self-auth model; only
   `j/i/user/authuser[/rapt]` implies the short-TTL redirector model.
2. **§4.2 (1–2 requests):** the cookie-independence test — `curl --range 0-1023` against a minted URL
   with no `Cookie` header. *"The outcome most likely to flip the verdict, and it costs one request
   to find out."*
3. **§4.1 (≤10 attempts, throwaway export):** the attempt-cost study; the architectural decider.

**Status:** the browser-native ADR of the same date remains **provisionally superseded**. Do not begin
design until step 1 and step 2 have run — both are cheaper than the argument they would settle.

---

## 2026-09-19 — Owner approved: 314-tab cleanup in one pass, and all 12 missing runbooks

**Two owner decisions taken this session.**

**(1) Server cleanup runs Phase 0→3 in one pass** once recon identifies the tab-spawner.
Stop the spawner → close the 314 `accounts.google.com` sign-in pages (preserving the
`takeout.google.com/manage` tab, the webgui tab, and the extension service worker) → verify
memory reclaimed. Read-only preconditions gate the start; the owner can abort between phases.
Nothing terminates a process and no download is cancelled. Rationale: the box is at **0
available memory** and the experiment protocol (`.recon/09-decisive-experiment.md`) cannot run
reliably until it is freed — a failed page scrape on a starved box is ambiguous between
"Google changed the page" and "out of RAM".

**(2) v3 writes all 12 missing failure-mode procedures** (1.7–1.18), not just the four live
ones. The project indexed 18 failure modes with recovery-attempt costs and never wrote the
remedies for 12 of them. Full list to document: 1.7 disk full on `/`, 1.8 JuiceFS FUSE stall,
1.9 Chromium crash / tab flood, 1.10 stale `SingletonLock`, 1.11 cloudflared URL rotation,
1.12 token mismatch (401 on auto-POST), 1.13 budget exhaustion, 1.14 archive 7-day expiry
mid-download, 1.15 partial/corrupt part, 1.16 `ENOSPC` mid-write, 1.17 server reboot,
1.18 container OOM. Also repair the three truncated docs (04, 07, 08) so no cross-reference
points into the void.

**Consequences.** Cleanup is blocked only on recon agent `03-infra` naming the spawner. The
runbook work lands with the v3 docs deliverable (task #7), and 1.9 becomes the worked example
of the format — it is the incident this session actually diagnosed.

---

## 2026-09-19 — Resume the v3 "autopilot" work; verify before designing

**Context.** A prior Claude Code session (`d23c798e-4de8-4a2a-ac22-6c008cb3aa93`,
2026-09-19 02:39–03:28Z, Opus 5) analysed why this project has never automatically
finished a Google Takeout export and chose a "v3 autopilot" direction. It made
**zero commits** and died on a five-hour rate limit while its own research grid was
running — **1 of 7 research agents completed**.

**Decision.** Do **not** resume straight into design/implementation. First
independently verify the prior session's factual base against the real source
files, then design on confirmed ground.

**Rationale.** The prior session's conclusions were a model reporting to itself;
its 6-claim factual base was never adversarially checked, and 78% of its research
died. This project's own docs contradict each other about cookie lifetime (1–2 min
vs ~10 vs <30 vs ~45 min) and parallelism ("single stream only" vs "4 parallel
curls working") *precisely because nothing was ever measured*. Building v3 on that
base would repeat the project's signature error: acting on belief instead of measurement.

**Alternatives considered.**
- Resume design immediately from the summarized research — rejected: unverified base.
- Re-run the dead web research first — rejected: the dead agents' transcripts
  (~4.0 MB) and their fetched-scratch corpus (~257 KB) are recoverable from disk
  without re-spending tokens.
- Only recover the research and stop — rejected by owner; full build approved.

**Consequences.** A 5-agent read-only recon grid writes bounded reports to
`.recon/`; each ends with a "Contradictions found" section so the confirmation is
falsifiable. Design is gated on it. Cost: some upfront time. Benefit: the v3 design
rests on measured facts, and the recon doubles as the "why v1/v2 failed" writeup
that task 5 of the old plan required anyway.

---

## 2026-09-19 — v3 adopts browser-native downloads (supersedes the cookie-replay architecture)

**Context.** All three existing generations (TUI, v1 manager, v2 engine) share one
mechanism: extract the Google cookie from the browser and replay it from a separate
HTTP client (`requests`/`aria2c`/`curl`). It has never worked reliably.

**Decision.** v3 lets **Chromium itself perform the downloads**, driven over CDP
from inside the container. No cookie extraction, no cookie replay. The browser
extension is the "hands" (reads the Manage-exports page, triggers downloads,
handles re-auth prompts, caps/cleans tabs); a small server-side daemon is the
"brain" (ledger of accounts/sessions/timeouts/attempts, disk budgeting, full
integrity verification on local staging, moving to `/opt/archives`, repair,
completeness report, alerts). Staging on `/config` (293 GB verified free).

**Rationale.** The cookie dies ~1–2 minutes idle, is IP-bound, and every
automation layer adds latency between fresh-cookie and first-byte — the source of
the 63-probe sweep, the JuiceFS `stat()` pre-pass, `END_OF_RANGE` misreads and the
2.8 TB restart. A browser-native download uses the browser's own always-fresh
session, same IP, same UA. This is also what the owner asked for originally
("use my remote server, and it's desktop environ in docker with browser to auth
the session").

**Consequences.**
- Must **reverse** existing code that auto-cancels every native Chrome Takeout
  download, and the project's earlier explicit rejection of the browser-native model.
- Must budget **memory** explicitly: the box has 7 GB with 0 available, and Chromium
  is long-lived (43 days) and stateful.
- `chrome.downloads` becomes the source of truth for progress; Google's "5 downloads
  per archive" allowance must be tracked in the ledger (still **never measured** —
  see open unknowns).
- v1/v2 stay selectable for reference/rollback.

**Supersedes:** the cookie-replay architecture of all three prior generations.
**⚠️ PROVISIONALLY SUPERSEDED (same day):** see the "BLOCKER: adjudicate browser-native vs
httpx-download" entry below, written after a first-hand counter-source was recovered. This
entry was authored before that evidence surfaced.

---

## 2026-09-19 — BLOCKER: adjudicate browser-native vs httpx-download BEFORE design

**Context.** The v3 direction approved by the owner (browser-native downloads) is now
contradicted by a first-hand source recovered from the dead research agents.

| | Position | Source |
|---|---|---|
| **A** | **Let Chromium download.** No cookie extraction/replay — same IP, same UA, always-fresh session. | Our own repo's empirical history (`.recon/05-recovered-research.md` §3, angle `local-lessons`); also the owner's original request |
| **B** | **Let `httpx` move the bytes.** Browser authenticates + scrapes the manifest + resolves each part URL; a server-side client downloads. Justified because *"routing 300+ GB through the browser's download manager would forfeit resume-by-`Range`, the rate cap, and stall detection."* | `conradstorz/Google-Takeout-Downloader` ADR-0001, recovered in `.recon/05-recovered-research.md` §4.1 |

**Both are first-hand. They cannot both be right.**

**Awkward evidence for B that must not be waved away:** the one export that ever finished
(62–63 parts, 3.08 TB) was completed by **4 parallel single-connection `curl -C -`** using a
manually pulled live CDP cookie jar — i.e. a cookie-replay client *did* move the final bytes at
scale. A blanket "cookie replay never works" claim is therefore **false**; what failed was
*automated, stale* cookie replay, not replay as such.

**Decision.** Do not begin design until this is adjudicated. Four questions decide it, all
blockers: (1) does a resumed/Range/aborted download consume one of Google's 5 attempts; (2) can a
download be resumed after the signed URL expires, or does it restart and spend a fresh attempt;
(3) is the URL IP-bound, cookie-bound, or both; (4) can the "verify it's you"/`rapt=` re-auth be
satisfied automatically by a browser but not by a headless client.

**Consequences.** Agent `arch-adjudicate` gathers web evidence into
`.recon/08-arch-adjudication.md`. The Chromium resume semantics are answerable from the ~1.05 MB of
Chromium source already on disk in `.research_tmp/` **with no network at all** — the cheapest
decisive evidence available. If evidence proves insufficient, the correct output is "undecided +
the decisive experiment", not manufactured confidence.

---

## 2026-09-19 — v3 must ship the missing recovery runbooks as a deliverable

**Context.** Recon found that `docs/v2/04-FAILURE-MODES-AND-RECOVERY.md` — the document the
project calls its "Operational Bible" — indexes **18 failure modes with recovery costs but
documents only 6** (§1.1–§1.6), ending at a literal `<!-- APPEND-HERE -->` sentinel at line
788. Three v2 docs are truncated the same way (04, 07, 08). A dozen internal cross-references
point into the void, including the procedure the doc twice calls "the single most valuable
recovery tool in this document".

**Decision.** v3 ships **written recovery procedures** as a first-class deliverable, not an
afterthought, and prioritises the four that are live or catastrophic:

| # | Failure mode | Why it matters now |
|---|---|---|
| 1.9 | Chromium crash / tab flood | **Is the 314-tab incident happening right now** |
| 1.18 | Container OOM | The box is at **0 available memory** |
| 1.7 | Disk full on `/` | `/` has **13 GB free**; staging must go to `/config` (293 GB) |
| 1.14 | Archive 7-day expiry mid-download | Costs the whole export |

**Rationale.** The project did not merely fail technically — it *anticipated* these failure
modes, annotated their attempt costs, and never wrote the remedies. That is why the same
incidents recurred across three generations and ~9 months. Undocumented recovery knowledge is
lost between sessions and re-derived at full price each time; the owner has already paid for
this finding three times over.

**Consequences.** The cleanup plan for the live incident (`.recon/07-live-state-and-cleanup-plan.md`)
becomes the first entry in that runbook set. Any v3 worked example that cannot articulate "what
happens when this breaks, and what do I type" is considered unfinished.

---

## 2026-09-19 — Treat the DOM scrape as UNVERIFIED until re-confirmed live

**Context.** The planned v3 depends on reading the Takeout manage page. Recon established that
this knowledge is **not** empirically verified: it is carried in `docs/v2/` as *normative
assertions in tables* ("what MUST be captured"), whose only provenance is
"`helpers/content.js:168–174` already scrapes this" — a v1 **source-code** observation, and the
plan itself admits the data "just never reaches the backend".

Only a narrower set is empirically confirmed by the one completed production run: the URL
parameter set (`j`, `user`, `authuser`, `i`), the live CDP `Storage.getCookies` jar, 206-vs-302
semantics, `PK`+EOCD verification, and the on-disk filename shape.

**Decision.** v3 re-verifies every selector (`data-download-uri`, `data-size`, "part X of N",
`dl_counts`) against the **live** manage page before depending on it, and records the result as
measured fact in the wiki. Until then, those selectors are classified *probable, unverified*.

**Rationale.** Google ships rotating obfuscated class names; the project's own doc warns only
`aria-label`, `title`, and `data-*` are semi-stable. Building the v3 foundation on an unverified
scrape would repeat the project's signature error — acting on belief where a five-minute
measurement was available.

---

## 2026-09-19 — Full v3 build scope approved by owner

**Decision.** Owner confirmed: (1) the recovered picture of the project is correct;
(2) resume at **full scope** — finish research → design v3 → implement → test →
docs → commit; (3) plan a **Chromium tab cleanup as step 1**, read-only-first with
approval before touching the live box; (4) work stays in the
`.claude/worktrees/takeout-autopilot` worktree on `feat/takeout-autopilot`;
(5) distil the recovered scratch corpus (~257 KB of other projects' source) into
design inputs.

**Consequences.** Cleanup is gated separately from implementation. The live server
must not be mutated without an explicit go-ahead.

---

## 2026-09-19 — The extension's tab-flood switch now fails CLOSED

**Context.** Failure mode 1.9, measured: while any v2 job sat in `needs_cookie`, the extension's
one-minute alarm opened another browser tab, every minute, for about three months. Peak **314 tabs**,
memory **0 → 4.7 GB**. The live instance was fixed by setting `chrome.storage.local.autoRecapture`
to `false`.

**The gap.** That fix lived in **storage**, and storage does not survive a profile reset. The code
default was still `true`, and two config-merge sites read `d.autoRecapture !== false` — which treats a
*missing* value as **ENABLED**, so any merge that simply omitted the key silently re-armed the spawner.
`chrome.storage.managed` was measured **empty**, which is why the managed-policy flag documented
elsewhere was inert: nothing ever wrote it, and this `local` value was the only one that mattered.

**Decision.** Fail closed in all four places: the default becomes `false`, both merge sites require
`=== true`, and the alarm is created only on an explicit opt-in (it was previously created
unconditionally, so we polled the manager every minute forever and one bug in the early return would
resume the flood). v3 needs none of this — it mints over CDP — so the switch costs nothing off.

**Alternatives considered.** (a) Remove the recapture path entirely — deferred to migration step 4, so
that each v2 stage is disabled before deletion. (b) Leave it and rely on the storage value — rejected:
a profile reset would silently reinstate a three-month flood.

**Consequences.** A latent production hole is closed on **both** copies of the file, because they are two
separate working trees and the live Chromium loads the extension from the **main checkout**
(`--load-extension=/work/helpers`), not the worktree. The worktree carries
`tests/v3/test_extension_defaults.py`, which pins all four properties textually — this repo has no JS
harness, and the bug class here is precisely "a guard that silently reverts". The production branch is
now **3 commits ahead of upstream, unpushed** by deliberate choice.

---

## 2026-09-19 — The v2 removal is disable-then-delete, in five ordered steps

**Decision.** `docs/v3/03-OPERATIONS.md` records the migration as five independently safe steps rather
than one refactor: (1) spawner fails closed **[done]**; (2) run v3 alongside v2 and confirm a `COMPLETE`
with the real `takeout-*.zip` filename; (3) stop the manager driving `NEEDS_COOKIE` jobs; (4) delete the
cookie-replay modules; (5) mark obsolete runbook steps superseded rather than deleting them.

**Why this order.** Step 3 must precede step 4. Until the manager stops re-driving `NEEDS_COOKIE` jobs,
turning `autoRecapture` back on re-arms the flood — so deleting the extension's half first would leave a
live spawner with nothing to stop it.

**Consequences.** The measured removal scope (exact files and lines across `takeout2/`, `manager/` and
`helpers/`) is recorded in the doc, so the deletion is mechanical rather than archaeological.


## 2026-09-21 — Do not race v2 for an export it is already pulling

**Context.** The plan was: test the workflow on the 20-product export, then pull the
64-product "full backup" last. A readiness sweep found v2's manager actively writing into
`google-takeout/braincreation/2026-09-19-04-27-12/` — the very export v3 had just minted.
By the time the mint pass finished, v2 had completed it: 19/19 parts, 141.28 GB, every
byte count matching Google's listing.

**Decision.** The v3 transfer was **cancelled**, not run. The export is declared complete
on the strength of a read-only check, and v3's minted URLs for it are simply left cached.

**Why.** The download allowance is **5 attempts per PART**, not per export. v2 had already
spent attempts on these parts. Running v3 concurrently would have competed for the same
allowance: at best it would re-download bytes already on disk, at worst it would exhaust
the remaining attempts and leave two partial copies and no reclaimable allowance. Neither
outcome is a backup. Minting was harmless (Δ1 each, URLs cached); transferring was not.

**Consequences.** A pull must be preceded by a check of what is already on disk, and the
check must decode filenames before comparing — the first comparison reported a false
"missing" part purely because v3's ledger held a percent-encoded name while v2 had written
the decoded one. **A completeness comparison is only as good as its name matching.**

## 2026-09-21 — The attempt KIND, not the count, decides the zero-bytes warning

**Context.** `--mint-only` triggered the v2 NETWORK_ERROR warning on **every** part: "1
attempt(s) spent with zero bytes on disk". The heuristic was `attempts > 0 and
size_on_disk == 0 and status in (failed, pending)`.

**Decision.** `ledger.attempt_kinds_by_part()` returns `idx -> set(kinds)`, `build_report`
takes it as `attempt_kinds`, and only `transfer`/`resume` attempts can raise the warning.
When the kinds are not supplied the warning is **suppressed**, not guessed.

**Why.** A mint legitimately books an attempt while moving zero bytes — that is what
minting is. The v2 signature being looked for is a *transfer* booked with no request ever
sent. A bare count cannot tell them apart, and a warning that fires on correct behaviour is
worse than no warning: it trains the operator to ignore the channel. Nineteen false alarms
on the 64-product export would have done exactly that.

**Consequences.** A caller that does not supply kinds loses the warning. Accepted: the only
real caller does supply them, and silence is safer than a false alarm here.

## 2026-09-21 — Part filenames are percent-decoded

**Context.** The 64-product export's part 18 is
`All%20mail%20Including%20Spam%20and%20Trash-002.mbox`. `part_filename_from_url` took the
URL basename verbatim.

**Decision.** `unquote()` the basename.

**Why.** The archive would otherwise hold a name no human wrote, unreconcilable with
Google's own listing — and v2, pulling the same export, wrote the decoded name. Two
downloaders disagreeing on what to call the same file is a correctness bug, not cosmetics.

**Consequences.** Pinned by tests: the real live string decodes; a non-zip extension
survives; a literal `+` is **not** turned into a space, because a filesystem has no query
semantics.

## 2026-09-21 — `--clean-staging`, opt-in

**Context.** The mover copies and verifies but never removes the staged file. Measured on
the server: staging (19 parts retained) 131.6 GB + rclone VFS cache (100 G max) ≈ **232 GB**
wanted against **194 GB** free. A full pull would hit ENOSPC mid-run.

**Decision.** Add `--clean-staging`, which unlinks each staged part **after** its archive
copy is verified. Off by default.

**Why opt-in.** The staged copy is the only way to re-verify without re-downloading, and
for a small export that safety net is worth its bytes. For a 141 GB pull it is not.

**Consequences.** With it, staging holds one part at a time (max 12.6 GB) and the total is
~120 GB — it fits. A failure to unlink is reported on stderr and does **not** fail the
part, because the bytes are already safe in the archive. The unlink happens only after a
verified move, never before.
