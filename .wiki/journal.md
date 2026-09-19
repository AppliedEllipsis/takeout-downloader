# Work journal

Dated entries. **Record the exact commands you ran and what they returned** — this is
the durable "commands we ran" record. Newest entries at the bottom.

---

## 2026-09-19 — Session recovery, recon grid, live probes

### What changed

Nothing in the codebase. This session was **forensics + verification + planning**:
recovering the work of a rate-limited Claude Code session, then independently
verifying its factual base before continuing.

### Context recovered

The prior session was `d23c798e-4de8-4a2a-ac22-6c008cb3aa93` ("google takeout download
research"), Opus 5, running `02:39:48Z → 03:28:02Z` (~48 min, 370 transcript records)
in worktree `.claude/worktrees/takeout-autopilot` on branch `feat/takeout-autopilot`.
It died on a five-hour rate limit. **It made zero commits.**

Its plan (5 tracked tasks) was: (1) research, (2) design v3, (3) implement v3,
(4) test + adversarial review, (5) docs + commit. Only task 1 ever started.

Its research workflow `takeout-research` (`wf_e3331a1c-72d`) fanned out 6 researchers
+ a critic + gap-fill. **1 of 7 agents completed**; the other 6 died on the same rate
limit. Harness usage: `subagent_tokens 1,112,820`, `tool_uses 450`, `duration_ms 608,054`.

### Commands run (workstation)

```bash
# locate the session
ls -la ~/.claude/projects/ | head -50
# → D---projects-takeout-downloader-script--claude-worktrees-takeout-autopilot

# enumerate the transcript without loading 1.25 MB into context
node -e '...' "<path>/d23c798e-....jsonl"
# → types: {last-prompt:20, agent-setting:20, mode:20, attachment:29, user:67,
#           assistant:91, ai-title:19, agent-name:19, worktree-state:13, ...}
# → first ts 2026-09-19T02:39:48.872Z, last 2026-09-19T03:28:02.646Z
# → tool counts: Bash:22, Read:15, TaskCreate:5, Write:2, Glob:1,
#                EnterWorktree:1, Workflow:1, ToolSearch:1, TaskUpdate:1

# workflow post-mortem
# → journal.jsonl: 7 agents launched, 6 {"type":"failed"}, 1 {"type":"result"}
# → failure reason: apiErrorStatus 429, rateLimitType "five_hour",
#                   overageDisabledReason "org_level_disabled"

# git state
git worktree list
# → main checkout      c0e3b8d [feat/internal-downloader]
# → .claude/worktrees/takeout-autopilot  c0e3b8d [feat/takeout-autopilot]
git -C .claude/worktrees/takeout-autopilot diff --stat c0e3b8d
# → (empty)  ⇒ the branch has NO changes vs c0e3b8d
```

Article-level extracts written (main checkout, untracked):
`.session_extract.txt` (assistant narration + all tool calls),
`.research_findings.txt` (57 KB — the one completed agent's full report),
`.failed_agents_tails.txt`.

### Scratch artifacts recovered (untracked, in the worktree — 8 items, ~257 KB)

Fetched from the web by the dead researchers before they were killed:

```bash
git -C .claude/worktrees/takeout-autopilot status --porcelain
# → .gtr_readme.tmp (24 KB), .research_tmp/ (37 files),
#   .tmp_carryout_fetch.go (38 KB), .tmp_carryout_sniff.go, .tmp_carryout_takeout.go,
#   .tmp_dl_item.cc (105 KB), .tmp_dl_utils.cc (37 KB),
#   .tmp_pgtd_download_takeout.py (36 KB)
```

`.research_tmp/` holds Chromium C++ download internals (`downloads_api.cc`,
`download_item_impl.cc`, `parallel_download_utils.cc`, `download_interrupt_reason_values.h`,
`devtools_download_manager_delegate.cc`, …), Playwright-style TS, and Chromium policy
YAML under `pol/`. The Go files are a GPL-3.0 project that maps a captured Takeout URL +
export inventory to per-part URLs. This corpus is the user's "look at other projects"
research, already on disk.

### Live server probes (read-only)

```bash
ssh -o BatchMode=yes takeout-server 'hostname; uptime; free -g | head -2; \
  df -h / /config /opt/archives; docker ps --format "{{.Names}} | {{.Status}}"'
# → ubuntu-8gb-fsn1-1, up 147 days, load 0.14
# → Mem: 7 total, 7 used, 0 free, 0 available
# → / = 75G, 13G free (84%) ; /opt/archives = archive_chunked rclone FUSE (1.0P virtual)
# → NO df line for /config  ← it does not exist on the host
# → takeout-webgui | Up 6 weeks ; takeout-tunnel | Up 6 weeks

scp -q .recon/probe2.sh takeout-server:/tmp/tk_probe2.sh
ssh -o BatchMode=yes takeout-server 'bash /tmp/tk_probe2.sh'
# → /dev/mapper/cache_crypt  300G  6.9G  293G  3%  /opt/local_cache_crypt   ← LUKS+xfs
# → du config = 213M ; du rclone_vfs = 0
# → pgrep -c chromium = 119
# → CDP total targets: 406
#      314  ('page',    accounts.google.com/v3/signin/identifier)
#       87  ('iframe',  accounts.youtube.com/accounts/CheckConnection)
#        2  ('page',    accounts.google.com/restart)
#        1  ('page',    takeout.google.com/manage)
#        1  ('page',    http://127.0.0.1:8080/)
#        1  ('service_worker', chrome-extension://dgbbpdjpfeeaiheekoclkkkbipkikejl/background.js)
# → chromium main process: etime 43-04:26:47, RSS ~1.08 GB,
#      --user-data-dir=/config/.chrome-profile --remote-debugging-port=9222

docker inspect takeout-webgui --format "{{range .Mounts}}...{{end}}"
# → /opt/local_cache_crypt/_projects/takeout-downloader/config -> /config   (rw)
# → /opt/local_cache_crypt/_projects/takeout-downloader        -> /work     (rw)
# → /opt/local_cache_crypt/rclone_vfs                          -> /var/rclone_vfs (ro)
# → /opt/storage.jfs002                                        -> same      (ro)
# → /opt/archives                                              -> /opt/archives (rw)
```

### Verified vs refuted

| Prior claim | Result |
|---|---|
| 314 stacked Google sign-in tabs | ✅ **verified exactly** (314 `page` targets) |
| Container RAM exhausted | ✅ verified (7/7 GB, 0 available) |
| `/config` on 300 GB disk with 293 GB free | ✅ verified (293 G avail on `cache_crypt`) |
| v2 cookie path can't run on server (missing `websocket-client`) | ⏳ pending — recon agent #2, reproducible locally |
| "only 2 CPUs" | ❌ **not confirmed** — host load avg 0.14; the pressure is memory, not CPU |

### Decisions made

- Verify the prior session's factual base before designing on it (see decisions.md).
- v3 adopts browser-native downloads — reverses the project's earlier explicit
  rejection of that model.
- Full v3 build scope approved by owner; tab cleanup planned as step 1, gated on approval.
- Work stays in the `feat/takeout-autopilot` worktree.

### Recon grid launched (5 read-only background agents → `.recon/`)

`01-architecture.md` · `02-v2-defects.md` (adversarial) · `03-infra.md` ·
`04-docs-archaeology.md` · `05-recovered-research.md` · `06-other-projects.md` (corpus distil).

### Pitfalls hit

- `ctx_execute` with `intent` set returns **indexed section titles, not stdout** —
  lost the first extraction until run without `intent` / written to a file.
- Inline multi-line `ssh '...'` got mangled by Git Bash; the host also lacks
  `python3` on `PATH`. Fix: write a script, `scp` it, run it (Runbook 1).
- `df /config` on the host prints nothing — `/config` is container-only. Briefly
  looked like a refutation of the prior session's disk claim; it was not.

### Do not commit

The worktree's 8 untracked items are agent scratch. `.recon/` (main checkout) is
recon scratch. Decide before the final commit what, if anything, becomes part of the repo.

---

## 2026-09-19 (cont.) — v2 defect verification + docs archaeology

### `recon-v2defects` agent died (provider 429)

The delegated agent was killed after 11 tool-uses by `429 "Too many concurrent requests"`
(6 agents at once exceeded the provider limit). Its workstream was completed inline.
**Lesson: cap grids at 4 concurrent agents.** Partial findings were recoverable from its
progress summary, which is why the inline continuation was cheap.

### v2 defects — 3 of 8 claims REFUTED

Full report: `.recon/02-v2-defects.md`. Verdicts: #1 verified, #2 verified (understated),
#3 verified, **#4 refuted**, **#5 refuted**, #6 partly true, #7 verified, #8 verified.

Key evidence:

```bash
# D1 — executed with websocket-client 1.8.0 (global Anaconda python; .venv-manager lacks it)
'http://127.0.0.1:9222'  -> ValueError: scheme http is invalid   => SCHEME REJECTED
'ws://127.0.0.1:9222'    -> TimeoutError: timed out              => scheme accepted
# contracts.py:356 default is "http://127.0.0.1:9222"; cookie.py:123 feeds it to websocket-client.
# cookie.py:131-138 `_via_http()` unconditionally raises, so ANY websocket failure is
# misreported as "websocket module unavailable" — a phantom cause.
```

```bash
# D2 — dependency reality
#   global anaconda python : websocket 1.8.0 OK, pytest 7.4.4 OK
#   project venv .venv-manager : websocket ABSENT, pytest ABSENT, fastapi 0.118.0 (older)
# => the "472 v2 tests pass" baseline came from an interpreter that is NOT the project's,
#    and the shipping venv cannot run the test suite at all.
```

- **D4 REFUTED:** `engine.py:236` batches `work[:parallel]`; streams are non-blocking per
  stream; `runner.py` provides real threads. Design is N parts × 1 connection (within-part
  parallelism deliberately forbidden at `engine.py:83`) — coherent with the 4-parallel-curl
  manual success.
- **D5 REFUTED, different defect found:** the watchdog is real (`engine.py:666-669` raises
  `StallAbort`, plus a `(10,300)` read timeout at `engine.py:613`). But **`stall_s=90`
  (line 84) and `dead_s=600` (line 85) are declared and never read anywhere** — dead config.
  Effective no-progress abort is 180 s.
- **D6 partly true:** CLI default is `./state.db` (safe) but the project's own help text
  points at `/opt/archives/state.db` — an rclone FUSE mount (verified fuse.rclone).
- **Bonus verified:** `helpers/background.js:689-707` **cancels every native Chrome Takeout
  download** (default on) → hard prerequisite for v3, not a cleanup detail.
- **Bonus verified:** `verify.py` default `STRUCT_OK` = `PK\x03\x04` + EOCD only, two seeks;
  a `HASH_OK` level exists and is off.

### Docs archaeology — the "Operational Bible" is two-thirds empty

Full report: `.recon/04-docs-archaeology.md` (557 lines). **I independently verified the
headline claim:**

```bash
wc -l docs/v2/04-FAILURE-MODES-AND-RECOVERY.md   # -> 788
grep -c '^### ' docs/v2/04-FAILURE-MODES-AND-RECOVERY.md  # -> 7 (just §0.1-0.6 + Index)
tail -1 docs/v2/04-FAILURE-MODES-AND-RECOVERY.md  # -> <!-- APPEND-HERE -->
grep -rn "APPEND-HERE" docs/  # -> 04:788, 07:552, 08:174  (THREE truncated docs)
```

`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md` indexes **18 failure modes (1.1–1.18) with
recovery-attempt costs**, but only **§1.1–§1.6 have bodies**. Twelve exist as index rows only.

**The missing twelve include the incidents live on the server right now:**

| Missing | Advertised cost | Real-world status |
|---|---|---|
| **1.9 Chromium crash / tab flood** | 0 | ← **the 314-tab incident** |
| **1.18 Container OOM** | 1/part in flight | ← box at **0 available memory** |
| **1.7 Disk full on `/`** | 0 directly; 1/part for killed streams | ← `/` has **13 GB free** |
| 1.13 Budget exhaustion | 0 to diagnose; catastrophic to fix | unmeasured |
| 1.14 Archive 7-day expiry mid-download | catastrophic | — |

So the project *anticipated* these failures, annotated their costs, and **never wrote the
remedies** — the recovery procedure it calls "the single most valuable recovery tool in this
document" is absent, and a dozen cross-references point into the void.

### Corrections this recon made to our own briefing

The "Contradictions found" requirement paid off — the agent corrected the brief itself:

1. **"~10 min" cookie lifetime does not exist** in the docs. The real set is ~1–2 min idle /
   <30 min / ~45 min / ~1 h (third-party). The "~10" is a *concurrency* ceiling.
2. **"4 parallel single-connection curl"** — only "4 parallel" is verbatim; the procedure
   says "fire all incomplete parts in parallel" and never states a count of 4, nor
   "single-connection". 4 = the number of parts outstanding.
3. **The ~6-minute `stat()` pre-pass** is ~6 min for the **whole** pass over 58 parts — not
   per part.
4. **Label-flip and filename-scheme are TWO distinct root causes**, not one.
5. **"Finished by hand" needs nuance:** automation delivered **58 of 62 parts**; the manual
   pull closed only the **last 4 partials**.
6. **Part size vs total contradicts:** 62 parts × "typically 10 GB" ≈ 620 GB, but the export
   is 3.08 TB (and `start.md:254` mentions a 50 GB part). Unreconciled in the corpus.
7. **Test counts disagree across four docs:** 111 / 155 / 212 / 382 — and the prior session
   claimed 472.

### The most consequential epistemic finding

**The DOM/URL knowledge — the foundation of the planned v3 — is NOT empirically verified.**
It is carried as **normative assertions in tables** ("what MUST be captured"), whose only
provenance is "`helpers/content.js:168–174` already scrapes this" — a **v1 source-code
observation**, and the plan itself admits the data "just never reaches the backend".

What *is* confirmed empirically (by the one completed production run) is narrower: the URL
parameter set `?j=…&user=…&authuser=0&i=…`, the live CDP `Storage.getCookies` jar method,
206-vs-302 semantics, `PK` magic + EOCD verification, and the on-disk filename shape.

**Implication:** v3 must re-verify the scraping selectors against the live manage page before
depending on them, and must treat `data-download-uri`/`data-size`/"part X of N"/`dl_counts`
as *probable, unverified*.

---

## 2026-09-19 (cont.) — Live cleanup executed: tab flood diagnosed and neutralised

**Owner-approved Phase 0→3, executed in one pass. No job data touched. No process killed.**

### The incident partly resolved itself mid-investigation

Phase 0 sampling caught a collapse in progress: sample A read **406 targets**, sample B 90 s later
read **3**. The 314 sign-in tabs were culled under memory pressure — but the **main Chromium process
did not restart**: PID 2585533 still reports `43-05:04:01` uptime throughout. Only the tab *renderers*
were reaped, and with `--restore-last-session` removed in `7bfb1d7` the tabs never came back.
`pgrep -c chromium` fell **119 → 12**; host memory `0 available` → **4.7 GB**.

### The spawner is `helpers/background.js`, not `takeout2/`

Confirmed chain: `background.js:419` (1-min alarm) → `:424-440` `pollRecapturePending()` →
`manager/app.py:243-247` (`pending: true` while any job is `NEEDS_COOKIE`) → `:444-456`
`chrome.tabs.create()`. Self-perpetuating because the guard is a **URL-pattern** query
(`chrome.tabs.query({url:'https://takeout.google.com/*'})`) and a logged-out tab redirects to
`accounts.google.com`, so it never matches → one new tab per minute, forever. `takeout2/` contains
**zero** tab-opening code.

### ⚠️ The fix the project would have tried first was a no-op

`webgui/init_custom.sh:96-116` writes `"autoRecapture": true` into Chromium's managed policy — and it
is **inert**. Verified on the live container: the extension reads `chrome.storage.managed` **only for
`captureToken`** (`background.js:293-294`). `autoRecapture` and `managerUrl` are read from
`chrome.storage.local` (`getManagerSettings()`, `background.js:213-222`). Flipping the policy would
have changed nothing and looked like an unfixable bug.

### The actual fix

Set the value the extension really reads, over CDP on the service-worker target — using the
container's `websockets 16.0` (`websocket-client` is installed nowhere, locally or on the server):

```bash
scp .recon/flip_recapture.py takeout-server:/tmp/tk_flip.py
ssh takeout-server 'docker cp /tmp/tk_flip.py takeout-webgui:/tmp/tk_flip.py >/dev/null \
  && docker exec takeout-webgui python3 /tmp/tk_flip.py'
# BEFORE: {"autoRecapture":true}
# AFTER : {"autoRecapture":false}
# RE-READ: {"autoRecapture":false}
```

The guard at `background.js:427` (`if (!s.autoRecapture) return;`) now short-circuits the poll.
**Job data untouched** — the `braincreation` job parked in `needs_cookie` since 2026-06-28 (7/63
parts, 412 GB) is left exactly as it was; only the loop that reacted to it was disabled.

### Verification (04:33Z) — all criteria met

| Criterion | Want | Got |
|---|---|---|
| sign-in pages | 0 | **0** |
| total CDP targets | ≤ 6 | **3** |
| in-flight downloads | 0 | **0** |
| host memory available | > 2 GB | **4,729 MB** |
| `autoRecapture` persisted | false | **false** |
| extension SW alive | yes | ✅ |

Remaining targets: one `takeout.google.com` tab, the extension SW, and
`accounts.google.com/RotateCookiesPage?og_pid=192&rot=3&…` — a live instance of the very
cookie-rotation endpoint the adjudication flagged (§3.2) as the likely cause of the 1–2 min cookie
death. Empirical support for the deprecation-ratchet argument.

### Correction to an earlier claim

I reported port **8080 as "dead"**. It is not. Host `curl 127.0.0.1:8080` → `HTTP 000`, but
**inside the container it returns `HTTP 200`** — the manager binds `--host 127.0.0.1` *inside* the
container, so the published port cannot reach a loopback-bound listener. The manager is healthy.

### Deliverable started

`.wiki/failure-modes.md` created — failure mode **1.9 (Chromium crash / tab flood)** is now fully
documented with symptom, root cause, the inert-policy trap, the verified recovery procedure,
results, gotchas, and the four changes v3 must make. The other 11 are indexed with their advertised
costs, per the owner's decision to write all 12.

### Pitfalls hit

- `ctx_execute` with `intent` set returns indexed section titles, not stdout — drop `intent` or write
  to a file.
- Multi-line inline bash is blocked by the harness; write a script to a project-local path and run it.
- The host has **no** `python`/`python3` on `PATH` and `.venv-manager` has neither pytest nor
  websocket; the only usable interpreter is `/c/Users/User/anaconda3/python.exe`. Inside the
  container, `python3` is 3.13.5 with `websockets`/`aiohttp` but no `websocket-client`.
- Agent-grid concurrency: 6 concurrent agents triggered provider `429`; **cap at 4**.

---

## 2026-09-19 (cont.) — ROOT CAUSE FOUND: it was never a cookie problem

**This is the session's decisive result.** Everything below was measured against the burn export
`f470fe32-76d6-43a6-9321-dbc76834919e` (BrainCreation, 3 products, 261 KB, 1 part).

### The finding

The browser — signed in, live session, correct IP and UA — was sent to the archive's download URL and
ended up at:

```
https://accounts.google.com/v3/signin/challenge/pwd?…&authuser=0
"Welcome | braincreation@gmail.com | To continue, first verify it's you | Enter your password"
```

A **password ReAuth challenge**. After the owner satisfied it, the archive URL came back carrying
**`rapt=`** (ReAuth Proof Token), which propagated into the page's download links.

**`rapt` gates MINTING, not transferring** — the minted file-host URL carries no `rapt` at all. So a
replayed cookie can never mint: **impossible by construction.** The docs' "~1–2 min idle cookie death"
was the redirector's `rapt` requirement; their "~45 min designed heartbeat" was the ReAuth window.
Neither was ever named correctly, which is why ~9 months and three architectures never fixed it.

### The four deciders — measured

| Question | Answer |
|---|---|
| `Range` resume cost | **ΔN = 0 → FREE** (the decisive cell; resolves to the hybrid) |
| Strong validators on the file host | **YES** — `ETag: f470fe32-…-1005482974000-0`, `Accept-Ranges: bytes` |
| File host cookie-independent? | **NO** — no cookie → `302 ServiceLogin`; but a **replayed jar works** (`206`, real zip bytes) |
| Cost to acquire one part | **2 attempts**, not 1 |

### Commands and results

```bash
# the ladder (browser mints via Network-captured navigation; client probes via aiohttp)
docker exec takeout-webgui python3 /tmp/tk_lad.py     # -> /config/ladder_result.json
ssh takeout-server 'docker exec takeout-webgui python3 /tmp/tk_lad2.py'   # -> /config/ladder2_result.json
```

Attempt-cost table (Google's own `Number of times already downloaded` counter is ground truth):

| Action | ΔN |
|---|---|
| 5 client requests bounced to sign-in | 0 |
| Browser navigation to the redirector, bounced to the challenge | 0 |
| Client `Range` to the file host **with** the jar → `206` | **0** |
| Browser mint **+ full download** | **2** |

### Mistakes made and corrected in this stretch

- **`autoCancelDownloads` had to be disabled** (`background.js:702-704`, checked as `=== false`; not in
  `DEFAULTS` nor `MANAGER_CONFIG_KEYS`, so only settable directly via `chrome.storage.local`).
- **My first D/E probes hit the wrong URL** — the filter `"usercontent" in url` matched
  `lh3.googleusercontent.com` (a 3,800-byte avatar PNG) instead of
  `takeout-download.usercontent.google.com`. Caught it because the returned `Content-Range` read
  `/3800`, not `/261259`. Corrected in `ladder2.py`.
- **My first readiness monitor watched `/manage`** and reported `uris=0` while a completed export sat
  on the list page — download links live on **`/manage/archive/<id>`**.
- Earlier I claimed the cookie scheme bug was fixable with no code change via
  `TK2_CDP_URL=ws://127.0.0.1:9222`. **Wrong** — a bare `ws://host:port` returns HTTP 404; the URL
  needs `/devtools/browser/<uuid>`, which changes per restart. Corrected in three documents.

### Decisions recorded (see decisions.md)

Adopt the hybrid (browser mints, client transfers with the jar, free `Range` retries); **delete**
cookie replay rather than repair it; re-model the ledger around *minting* as the scarce resource;
treat ReAuth as an unavoidable first-class step (an explicit owner security decision); budget
~**2** part-acquisitions, not 5.

### Still unmeasured

- **The `rapt` window length** — now the top priority; replaces four contradictory doc figures.
- Whether a minted URL **outlives** the `rapt` (decides whether URLs can be batched up front).
- The 2-attempt decomposition (redirector = 1 + file host = 1?).

### Left in a non-default state

- `autoCancelDownloads: false` — **restore when the experiments finish.** ← RESOLVED, see below
- `autoRecapture: false` — leave as-is (tab-flood fix).
- The browser tab holds a **live `rapt`** — don't navigate it away if the window is wanted.
- Burn export is at **2 of 5** attempts used.

---

## 2026-09-19 (cont.) — the 12 missing runbooks, written and verified

Owner approved all 12 (`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md` indexes 18 modes but documents only
§1.1–§1.6). Split across four files, one format contract, three written by background agents and one by me.

### Where they live

| File | Modes |
|---|---|
| `.wiki/failure-modes.md` | **index of all 12**, plus 1.9 · 1.13 · 1.18 in full (the three needing this session's measurements) |
| `.wiki/failure-modes-storage.md` | 1.7 · 1.8 · 1.16 |
| `.wiki/failure-modes-exports.md` | 1.10 · 1.14 · 1.15 |
| `.wiki/failure-modes-runtime.md` | 1.11 · 1.12 · 1.17 |

Every section carries the same seven sub-blocks: Symptom · Root cause · Detection · Recovery ·
Verification · Attempt cost · What v3 must change. Final check: **all 12 present, all 12 sections
complete, all fences balanced, 281 / 258 / 307 / 330 lines.**

### Verification — the part that mattered

Each brief required `path:line` citations and forbade inventing procedures (unknowns marked
**UNVERIFIED**). I then checked the citations myself rather than trusting the summaries:

**13 citations spot-checked against source — all 13 exact**, including verbatim quotes from
`12-operations-runbook.md:44-47` (restarting webgui orphans the tunnel, Cloudflare 1033),
`app.py:359-365` (tokens injected into the HTML `<head>`), `config.py:45-46`, `verify.py:40-45`,
`:127-128`, `:152-155`, `chromium-service.sh:46-57`, `preflight.py:56`, `engine.py:103`,
`compose:128-139`, and `04-…md:118`.

**One real defect found and fixed:** the v2 pause route is `/control/{action}/{archive_id}`
(`takeout2/api.py:325`) — `archive_id` is a **path parameter**, not a JSON body field. The storage
runbook's curl put it in `-d '{"archive_id":…}'`, which would have **404'd silently**; an operator
would have believed the engine was paused while it kept writing. Fixed in both places.

**One format inconsistency found and fixed:** storage used inline `**Symptom.**`; runtime and exports
used `### Symptom`; my own `failure-modes.md` used `###` too and was additionally **missing the
`Attempt cost` sub-block in all three entries** while I required it of the agents. All four files now
share one convention and all four are complete.

**One self-correction:** I flagged the storage agent's "truncate the known offender
`/var/log/juicefs.log`" step as fabricated because the file is 17 KB today — but it had cited
`04-…md:118` properly, describing a **real past incident** where that log hit 22 GB. My edit had
over-claimed "there is no fixed offender"; rewritten to keep the documented history, add the fresh
measurement, and say *size it before acting*.

### Incidental findings worth keeping

- **`preflight.py`'s own docstring records the root fs at ~15 GB free (81 %)**; measured today it is
  **13 GB (84 %)**. The disk is still slowly filling → 1.7 is a **live** risk, and the existing
  20 GiB `DEFAULT_MIN_HEADROOM` guard cannot currently pass on this box.
- **The tunnel fix has an exact home**: `docker-compose.webgui.yml:128-139` runs
  `command: tunnel --no-autoupdate --url http://127.0.0.1:3000`, where `--edge-ip-version 4
  --protocol http2` would go. Marked **UNVERIFIED — proposed, not applied**.
- **`verify.py` maps truncation to `UNVERIFIED`, never `CORRUPT`**, deliberately, so the pipeline can
tell *resumable* from *ruined* — and the corpus now records why the scheduler must preserve that
distinction.

### Housekeeping settled

`autoCancelDownloads` **restored to `true`** (the hybrid architecture makes the browser downloading
bytes pure waste; the owner was right). `autoRecapture` remains **`false`** (tab-flood fix).
Burn-export counter stands at **3**; no further experiments planned on it.
