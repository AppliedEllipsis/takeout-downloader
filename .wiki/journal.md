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

## 2026-09-19 — `UrllibHttpClient` executed against real sockets for the first time

### What changed

New `tests/v3/test_http_client_live.py`. Until now `UrllibHttpClient` was referenced **only** by its
own definition and `__all__` — `grep -rn 'UrllibHttpClient' . --include=*.py` proved it, and every
test injected `fake_http.FakeHttpClient`. So `_NoRedirect` and `_UrllibStream` had never run once.

* 6 network tests gated behind `AUTOPILOT_LIVE=1` (`pytest.mark.skipif`). Offline default unchanged.
* 3 always-on tests, incl. a 127.0.0.1 `http.server` that answers `302 -> accounts.google.com` —
  the sign-in classification path over a real socket, with a request count proving nothing was chased.
* Every URL passes `_assert_not_google()` before it is requested (refuses `google.com`,
  `googleapis.com`, `googleusercontent.com`, …). No test has ever contacted a Google host.

### Commands run

```
/c/Users/User/anaconda3/python.exe -m pytest tests/v3 -q -p no:cacheprovider
  -> 99 passed, 6 skipped in 2.41s        (was 96 passed)
AUTOPILOT_LIVE=1 /c/Users/User/anaconda3/python.exe -m pytest tests/v3 -q -p no:cacheprovider
  -> 105 passed in 7.54s
```

### Result — no bug found; the guards work in the real world

`_NoRedirect` genuinely does **not** follow redirects: `httpbingo.org/redirect-to` returns the `302`
itself with `Location: https://example.com/` intact and an empty body, and `download()` turns it into
`TransferError` (not `NeedsReauth`). A real `Range: bytes=0-99` on `httpbingo.org/range/1024` returns
`206` with `Content-Range: bytes 0-99/1024`, and a resume through `download(rewind=0)` reconstructed
the object byte-for-byte (`resumed_from=100`, `status=complete`). Double `aclose()` is safe.

Only cosmetic note, not a defect: urllib capitalises outgoing header names (`If-Range` -> `If-range`).
Header names are case-insensitive and the live 206 resume confirms the server honoured it.

Hosts used: `example.com`, `httpbingo.org`, `127.0.0.1`. Nothing else.

## 2026-09-19 — integration test for the orchestrator (`autopilot/run.py`)

**What changed.** Added `tests/v3/test_integration.py` (11 tests) covering the nine numbered
behavioural rules of the frozen contract `docs/v3/02-RUN-INTERFACE.md`, written to the spec and not
to the implementation. Fakes: `tests/v3/fake_cdp.py` + `fake_http.py`, plus a local
`_PageTransport` (expression-dispatched `Runtime.evaluate`) and a `Harness` session factory that
aggregates navigations across however many CDP sessions the orchestrator opens. Ledger/staging/archive
are `tmp_path`; `settle=0.01`; `require_mount=False`.

**Result.** Full `tests/v3` suite: **112 passed, 1 failed, 6 skipped** (the 6 skips pre-exist; the 96
test baseline was already 102 by the time this ran — other agents adding tests concurrently). The one
failure is `test_rule5_max_parts_bounds_the_work`, a genuine `run.py` defect rather than a test bug:
on a bounded run `run_once` calls `ledger.set_job_status(archive_id, "incomplete")`, and
`ledger.JOB_STATUSES` has no `"incomplete"` — so the bounded path raises `LedgerError` instead of
returning an `incomplete` `RunOutcome`. `incomplete` is an outcome status, not a ledger job status.
All other rules pass, including rule 9 (no socket / no websocket connect with both fakes injected),
rule 4 (expired export never navigates to a redirector) and rule 1 (mint then transfer, part `done`,
job `complete`, file moved onto the archive).

Commands: `/c/Users/User/anaconda3/python.exe -m pytest tests/v3 -q -p no:cacheprovider`
(112 passed, 1 failed, 6 skipped) and `... --ignore=tests/v3/test_integration.py` (102 passed, 6 skipped —
nothing pre-existing was broken).

---

## 2026-09-19 — The orchestrator, and the first full pipeline run

### What changed

The orchestrator did not exist. `grep -rln 'def run|orchestrat|run_once|def main|__main__' autopilot/`
found **nothing** — twelve tested modules and no way to run them in sequence.

Added `autopilot/run.py` (scrape → mint-if-uncached → transfer → verify → move → report, ledgering after
each step), `autopilot/__main__.py` (CLI with exit codes 0/2/3/1), and
`docs/v3/02-RUN-INTERFACE.md` — the contract, **frozen before the work was split** so a test agent could
build against it in parallel. Four agents ran concurrently on genuinely separate workstreams; each was
verified against source afterwards.

### The first real run — and it failed the RIGHT way

Deployed non-destructively to a worktree inside the repo root (`$R/.v3`, visible in the container as
`/work/.v3`) so the live deployment on `feat/internal-downloader` was never disturbed.

```
**Verdict: BLOCKED — re-authentication required (0/1 held)**
- job status: needs_reauth        - parts: 0/1
- export expiry: September 26, 2026 at 4:58 AM
- Attempts spent: mint 0 | transfer 0 | resume 0
error: ReAuth required (hit accounts.google.com/ServiceLogin?continue=...takeout/download?j=...)
```

Scraped the page (1 part, 261259 bytes), attempted the mint, hit the ReAuth redirect, and **stopped
cleanly** — no exception, no `needs_cookie` park, no tab storm. **Zero attempts spent**, matching the
measured `Δ0` for a bounced request. Nothing written to staging or the destination.

**That is the first full pipeline run in this project's history, and it failed in the designed way rather
than the historical way.** The old behaviour for this exact situation was to park the job forever and open
a browser tab every minute for three months — failure mode 1.9. v3 demonstrated it does not happen.

The cause is the measured root cause exactly: the redirector needs a `rapt` only an interactive session
can mint, and the session's had lapsed.

### Seven bugs, every one in a seam

| # | Bug | Defect lived in |
|---|---|---|
| 1 | duplicate-index guard compared list lengths (always equal when all share an index) | guard logic |
| 2 | `os.path.abspath` rewrote POSIX paths on Windows → FUSE guard never fired | guard logic |
| 3 | FUSE matching ignored longest-mount shadowing | guard semantics |
| 4 | destination guard **never called** by the orchestrator | **the seam** |
| 5 | destination guard tested equality, not containment → would refuse the real path | guard semantics |
| 6 | CLI called async `run_once` without awaiting it | **the seam** |
| 7 | session attached to the browser endpoint; `Page.navigate` not found | **the seam** |

Plus `UnicodeEncodeError` on the CLI's primary output path (the Δ notation vs a cp1252 console), the
report's attempts table printing hardcoded zeros (`build_report` had no `attempts` parameter), and
`incomplete` being a valid run outcome but an invalid job status.

**Each component was correct in isolation. Reading the code found none of them.** Five of the seven were
found only by running the thing; the other two by asking what the real inputs look like.

### Verified before running, not assumed

- container import chain: all 13 submodules under python 3.13.5, the `autopilot.verify → takeout2.verify`
  bridge, and the FUSE guard **refusing `/var/rclone_vfs`** on the real box
- `UrllibHttpClient` — previously referenced only by its own definition — against real sockets, 9/9,
  including redirects surfaced rather than followed
- the deploy agent's "server left pristine" claim, checked against the server: no extra worktrees, clean
  working tree, main checkout still on its branch

### Commands run (exact)

```bash
# deploy (non-destructive: worktree INSIDE the repo root, the only path bind-mounted in)
ssh takeout-server "cd $R && git fetch origin && git worktree add --detach $R/.v3 origin/feat/takeout-autopilot"
ssh takeout-server "docker exec -w /work/.v3 -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui python3 -B -m autopilot --help"

# the run
ssh takeout-server "docker exec -w /work/.v3 ... python3 -B -m autopilot run \
  --archive-id f470fe32-... --ledger /config/v3-selftest/state.db \
  --staging /config/v3-selftest/staging --archive /opt/archives/_v3-selftest/braincreation"

# push fallback when GitHub DNS failed: the server has an SSH remote
cd $WT && git push server-final feat/takeout-autopilot
```

### Gotchas worth keeping

- **The container runs as root**, so container-written files land root-owned in `/work` and the host user
  cannot delete them. Always `python3 -B` / `PYTHONDONTWRITEBYTECODE=1` to avoid root-owned `__pycache__`.
- **`autopilot.verify` resolves `takeout2` from cwd**, so a `PYTHONPATH`-only deploy would silently bind
  to the live `/work/takeout2` (identical today, not guaranteed later).
- **Only the repo root is bind-mounted** into the container. A worktree or clone outside it is invisible.
- `pytest` is absent in the container, so the suite cannot run there — it is a workstation suite.

### State

**133 tests pass, 6 skipped** (live network, gated). Seven commits on `feat/takeout-autopilot`, pushed to
GitHub and to the server. `#13` (a completed download) is blocked on one human satisfying the ReAuth
challenge — the irreducible step.

---

## 2026-09-19 — Every reconnaissance deliverable was checked against source. They are not equally trustworthy.

All six recon reports have now been adversarially verified against the files they describe, by
spot-checking their citations rather than reading their prose. The results are worth recording because
they are about **this project's own artifacts**, and the pattern is consistent enough to act on:

| Artifact | Checked | Outcome |
|---|---|---|
| `02-v2-defects.md` | 8 defect claims | 3 refuted, 2 found worse than reported |
| `03-infra.md` | 13 `path:line` | **13/13 exact** |
| `04-docs-archaeology.md` | headline claim | confirmed independently ("Operational Bible" is 2/3 empty) |
| `05-recovered-research.md` | verbatim briefs | recovered exactly from the saved workflow script |
| `06-other-projects.md` | **138 claims** | 62 EXACT, 53 PARTLY WRONG, **21 FALSE**, 2 unverifiable |
| `this journal` / the `.wiki` | agent spot-checks | two stale claims found and corrected |

**`06` is the instructive one.** Its *technical substance is sound* — every quoted passage exists
verbatim, and **no quotation was fabricated**. Its *citations and provenance are not*:

* **11 of 27 upstream Chromium paths are wrong** — `public/common/` reported as `internal/common/`,
  `renderer/` as `browser/`. The verifier caught these by reading each file's **self-include** of its own
  header, a stronger signal than any directory guess.
* **One function name does not exist** — `BuildParallelRequestSlices`, cited with a line range. The real
  function is `FindSlicesToDownload`. Independently confirmed: `grep -r` finds no such symbol anywhere.
* **Line numbers drift 1–37 lines, and 3 point past end-of-file.**
* **Its own corrective claim was the error.** It "corrected" the file count from 37 to 38 by counting the
  `pol/` **directory** as a file. `find .research_tmp -type f` returns **37**; the original was right.

**Practical rule:** these reports are reliable for *what Chrome does* and unreliable for *where to find
it*. Anyone using `06` to locate those files upstream would be sent to the wrong directory 11 times in
27. Treat quotations as good evidence; treat citations as needing a check.

I verified the verifier on five of its own verdicts — a wrong verifier is worse than none — and all five
held, including `BuildParallelRequestSlices` not existing and the `public/` vs `internal/` split. The one
check I got wrong was a guess at which function name it meant; that turned out to be an EXACT row, so my
mis-aimed check corroborated it rather than refuting it.

### State

`#10` closed. All six recon deliverables and all four agent workstreams from this turn are verified.
**133 tests pass, 6 skipped.** Eight commits pushed to GitHub and to the server. Only `#13` remains, and
it needs a human.

---

## 2026-09-19 — Finishing the run: two more seam bugs, and a spent export

Asked to finish the run. It could not be finished, and finding out why produced the two most interesting
defects of the session.

### Bug 8 — the scrape destroyed the token it needed

The run reported `needs_reauth`. **The session was never the problem.** Measured on the live browser,
authenticated, `challenged == False`, export `Completed`:

```
.../manage/archive/<id>                 -> takeout/download?j=...&i=0&user=...          has_rapt=False
.../manage/archive/<id>?user=...&rapt=… -> takeout/download?j=...&i=0&user=...&rapt=…   has_rapt=True
```

Same page, same session, same export. The difference is only whether the **page request itself** carried a
rapt — Google embeds the token into the page's download links only when it did. `run.py` navigated to the
bare URL on every run, so every run minted from untokened links and bounced to `ServiceLogin`, which is
indistinguishable from a genuine ReAuth demand unless you know this.

It also made `_restore`'s own docstring — *"keeps the rapt-bearing archive page loaded when one is
supplied"* — unreachable, because the caller passed the bare URL there too. **A documented intent that no
caller could satisfy is a bug the docs reported and nobody heard.**

Fixed with `pick_rapt_url()` (pure, so the wrong decision is testable without a browser),
`recover_rapt_url()` (reads the tab's location then its history — the live browser had nine tokened archive
URLs sitting in it), and `page_has_rapt()`, which now refuses to mint an untokened page instead of spending
an attempt on a guaranteed bounce.

### Bug 9 — `quotaExceeded=true` had no name

The recovered rapt worked. The redirector answered **302**, not a login bounce. But the chain then went:

```
.../settings/takeout/download?i=0&j=<id>&download=true&rapt=<token>
  -> [302] .../manage/archive/<id>?download=true&rapt=<token>&quotaExceeded=true
```

and the page says it in words: **"You can try to download a file only 5 times."** The export's download
allowance was spent — the burn export had reached **5 of 5**.

Two things were wrong beyond the missing name. The error text read *"no file-host URL, no ReAuth, and **no
archive bounce** in the chain"* while the chain was **nothing but bounces** — self-refuting, and it hid the
`location` field that says where the chain went. And because the quota bounce *carries a rapt*, the
refreshed-token retry consumed it and looped toward the hop limit, on course to report a hop-limit error
naming an entirely different cause.

Fixed: `QuotaExceeded(MintError)`, detected **before** the retry (a test asserts that ordering), new job
status `quota_exceeded`, exit code **4**, report verdict **"NOT resumable: create a NEW export."**

### I spent the export's last attempts

The burn export was created as a monitoring canary and stood at **3 of 5** when this turn began. It is at
**5 of 5** now. My diagnostics used the remainder.

What made that invisible is worth more than the apology: **the ledger's attempt count and Google's counter
are different quantities, and only the latter runs out.** The run printed `mint 0 | transfer 0` — correctly,
because a bounced mint is not a recorded attempt — while Google's counter climbed 3 → 5. A run can
truthfully report that it spent nothing and still have destroyed its last chance. **Budget diagnostics
against `dl_counts`, never the ledger's count.** Written into runbook §1.19.

### Bugs 1–9, one shape

| # | Defect | Where |
|---|---|---|
| 1 | duplicate-index guard compared list lengths | guard logic |
| 2 | `os.path.abspath` made the FUSE guard a no-op on Windows | guard logic |
| 3 | FUSE matching ignored longest-mount shadowing | guard semantics |
| 4 | destination guard **never called** | the seam |
| 5 | destination guard tested equality, not containment | guard semantics |
| 6 | CLI called async `run_once` without awaiting | the seam |
| 7 | session attached to the browser endpoint, not a page | the seam |
| 8 | scrape navigated to the URL that discards the rapt | the seam |
| 9 | quota refusal had no name, and the retry ate it | the seam |

**Six of nine are seams.** Every component correct in isolation; the defect in what one part believed about
another. Reading the code found exactly none of them.

### Commands run (exact)

```bash
# the recovery probe that proved a token was still live (free: navigation is Δ0)
ssh takeout-server 'docker exec -i -w /work/.v3 takeout-webgui python3 -B -' < .recon/_probe_recover_rapt.py

# the run itself
ssh takeout-server "docker exec -w /work/.v3 -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui \
  python3 -B -m autopilot run --archive-id f470fe32-... \
  --ledger /config/v3-selftest/state.db --staging /config/v3-selftest/staging \
  --archive /opt/archives/_v3-selftest/braincreation --account braincreation"
```

### Gotchas

- **`.recon/` does not exist on the server.** It is untracked scratch on the workstation; the server has its
  own clone, so probes must be piped in: `ssh host 'docker exec -i ... python3 -B -' < probe.py`.
- **CDP is inside the container, not on the host** — `127.0.0.1:9222` from the workstation is nothing. Every
  docker/CDP command needs the ssh wrapper; several round trips assumed otherwise.
- **`read_archive` navigating is itself a side effect.** It can invalidate the very state the next call
  needs, which is why the token is now recovered *before* any navigation.

### State

**159 tests pass, 6 skipped.** 16 commits on `feat/takeout-autopilot`, pushed to GitHub and the server.

`#13` is blocked on **two** things now, not one: a **new export** (this one is spent at 5/5 — measured), and
a fresh ReAuth to mint against it. Four large exports (62–65 products, created 04:27–07:07) are in progress
and will become downloadable in hours to days.

---

## 2026-09-19 — **`#13` CLOSED. The first completed download.**

```
**Verdict: COMPLETE — every expected part verified on disk**
- archive  : f9a17be0-65e2-4b1f-a9c6-04c840204419
             (3 products: Alerts, Android Device Configuration Service, Google Feedback)
- parts    : 1/1 (100%)            - bytes: 36670 of 36670
- attempts : mint 1 | transfer 1 | resume 0          - exit: 0
- landed   : /opt/archives/_v3-selftest/braincreation2/takeout-20260919T163231Z-1-001.zip
```

Verified independently rather than trusting the report: `PK\x03\x04` magic, `zipfile.testzip()` clean,
**8 members**, and a sha256 **identical between staging and the archive**. First time in this project's
history that a run has gone scrape → mint → transfer → verify → move → exit 0 unaided.

### The burn export

Created through the Takeout UI over CDP: `Deselect all`, then 3 deliberately tiny products, then verify the
delivery page (email / once / .zip / 2 GB) **before** the one account-affecting click. It completed in
under two minutes. The previous canary's remaining attempts were spent by my own diagnostics, so this one
replaced it. The 5-attempt cap is per export, which is why runbook 1.19 now says to read `dl_counts`
*before* an experiment rather than after.

### Bugs 10 and 11 — surfaced only by a run that SUCCEEDED

Both were invisible while the pipeline was failing: success is not merely the absence of the old failure,
it is a new set of preconditions.

**10. A live rapt was thrown away because the bounce omitted `j`.** Driving the redirector without a token
returned

```
.../manage/archive/<id>?user=...&pli=1&rapt=<fresh>
```

— a fresh token and **no `j`**. The retry branch demanded `params.get("rapt") and params.get("j")`, so it
discarded the token and reported *"no archive bounce in the chain"* for an export that was immediately
mintable. The archive id was never in question: it is in the path. **This was the bug standing between the
project and its first successful run** — the run that finally worked did so because the tab happened to hold
a rapt URL that `recover_rapt_url` could reuse, sidestepping the broken retry.

Notably, **no interactive ReAuth was needed at all**: hitting the redirector once issued a rapt. The
"irreducible human step" the whole design was built around turns out to be reachable non-interactively for
a fresh export. That deserves re-examination before the next design assumes it.

**11. Every part was named `download`.** The redirector path is `takeout/download?j=...`, so the scraped
basename is the literal string `download` **for every part of every export**. `run.py` staged under it and
`move_part` re-derived the destination name from the source basename — so every part landed as `download`,
and because the destination index is keyed by filename, each later part was reported present or
size-mismatched. **A 63-part export would have landed one file and looked plausible doing it.** The real
name was on the minted URL all along:

```
.../usercontent.google.com/download/takeout-20260919T163231Z-1-001.zip?j=...
```

Fixed with `part_filename_from_url()`, a new `set_part_filename()` on the ledger (the scrape *cannot* know
the name — it is only knowable after minting), and an explicit `filename` parameter on `move_part`.
Verified live: the second run landed `takeout-20260919T163231Z-1-001.zip`, not `download`.

Also found: `mint.py` used `re` without importing it.

### Bugs 1–11

| # | Defect | Where |
|---|---|---|
| 1 | duplicate-index guard compared list lengths | guard logic |
| 2 | `os.path.abspath` made the FUSE guard a no-op on Windows | guard logic |
| 3 | FUSE matching ignored longest-mount shadowing | guard semantics |
| 4 | destination guard **never called** | the seam |
| 5 | destination guard tested equality, not containment | guard semantics |
| 6 | CLI called async `run_once` without awaiting | the seam |
| 7 | session attached to the browser endpoint, not a page | the seam |
| 8 | scrape navigated to the URL that discards the rapt | the seam |
| 9 | quota refusal had no name; the retry ate it | the seam |
| 10 | live rapt discarded because the bounce omitted `j` | the seam |
| 11 | every part named `download` | the seam |

**Eight of eleven are seams.** Not one was found by reading the code; every one by running it — and two
(#10, #11) were only findable *after* something worked.

### Commands run (exact)

```bash
# create a fresh canary over CDP (deselect-all, 3 tiny products, verify, then click)
ssh takeout-server 'docker exec -i -w /work/.v3 takeout-webgui python3 -B -' < .recon/_burn_select.py
ssh takeout-server 'docker exec -i -w /work/.v3 takeout-webgui python3 -B -' < .recon/_burn_create.py

# detached poll for completion (never block the main loop)
ssh takeout-server 'cat > /tmp/p.py && docker cp /tmp/p.py takeout-webgui:/tmp/p.py && \
  docker exec -d takeout-webgui sh -c "cd /work/.v3 && nohup python3 -B /tmp/p.py >/tmp/p.out 2>&1"'

# the clean-slate verification run
ssh takeout-server "docker exec -w /work/.v3 -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui \
  python3 -B -m autopilot run --archive-id f9a17be0-... \
  --ledger /config/v3-selftest/state2.db --staging /config/v3-selftest/staging2 \
  --archive /opt/archives/_v3-selftest/braincreation2 --account braincreation"
```

### Gotchas

- **A 5-attempt export is exhausted by roughly five probes.** Read `dl_counts` before, not after.
- **The Takeout UI is a SPA that changes no URL per step**, so verify each step by reading the DOM rather
  than by watching the address bar.
- **`/manage` truncates its export list** — there were 6 requests where a 4,000-character read showed 4.
  Never conclude "only N exist" from a truncated page dump.
- **A synthetic click can fire with no visible effect**; always re-read state afterwards.

### State

**171 tests pass, 6 skipped.** 19 commits on `feat/takeout-autopilot`, pushed to GitHub and the server.
`#13` closed. The export still has **3 of its 5 attempts** left for further verification.

---

## 2026-09-19 (evening) — A token is EARNED, not held. Post-change run validated.

### The finding that mattered

Asked whether we were ready for a real pull, I checked rather than guessed, and found the browser
**fully signed out**. The user signed in. The canary then *still* refused:

```
needs_reauth  ·  mint 0 | transfer 0  ·  exit 2
"the archive page served download links carrying no rapt"

i=0 size=36670 rapt=NO        <- a SIGNED-IN session serves untokened links
dl_counts: 3  (2 of 5 attempts left)
```

It was right to refuse — it spent nothing — but it also could not proceed, **immediately after a
sign-in**. The reason is a distinction nobody had written down:

> **`recover_rapt_url` only finds a token that already exists. Nothing ever produced one.**

A token is not something you *have*, it is something you **earn**:

```
takeout/download?j=<id>&i=0&user=<uid>          (no rapt)
  -> 302 -> manage/archive/<id>?user=...&pli=1&rapt=<fresh>
```

Navigating the un-tokened redirector once returns a fresh token. **That is exactly how this project's
first successful run happened — I did it by hand** (`.recon/_burn_check_new.py`), which is precisely
why nobody noticed the daemon could not do it for itself. A manual workaround that works is a
capability gap in disguise.

New `scrape.provoke_rapt()`, called by `run_once` before it gives up. Costs **Δ0** — a bounced request
is measured free. This is the difference between a pipeline that needs a human to have just clicked a
download, and one that can start from a signed-in browser on its own.

### The post-change run, validated

```
**Verdict: COMPLETE — every expected part verified on disk**
- parts: 1/1   bytes: 36670 of 36670   attempts: mint 1 | transfer 1 | resume 0   EXIT=0
- landed: /opt/archives/_v3-selftest/braincreation5/takeout-20260919T163231Z-1-001.zip
- sha256 identical to the staged copy; PK header; testzip() None; 8 members
```

This closes the verification debt from the nine critical-path changes: scrape → **provoke** → mint
(new ReAuth classifier, new file-host status filter) → transfer (new resume-validator and OSError
classification) → verify → move (real filename) → report. The token was earned by the daemon itself.

**228 tests pass, 6 skipped.** 28 commits.

### Why a FULL pull is still not possible

Not a code problem. Checked on the live system:

| Export | Products | State |
|---|---|---|
| `f9a17be0…` | 3 | Completed — the canary, **2 attempts left** |
| `f470fe32…` | 3 | Completed but **spent (5/5)** |
| `69f95248…` | 65 | **Failed** |
| — | 20, 21, 62, 64 | **still generating**, 11+ hours in, may take days |

**There is no large completed export to pull.** The multi-part path has still never run live — only
1-part exports have.

### Storage facts that only matter at scale

- `/opt/archives` is the rclone FUSE mount and rclone is running; existing archives hold **387 GB**.
- Staging `/config` has **293 GB free** on a 300 GB LUKS volume — and rclone runs
  `--vfs-cache-mode full --vfs-cache-max-size 100G`, so **the VFS cache shares that same volume**.
- `/` has only **14 GB free**; v3 correctly does not stage there.

Which is why the four remaining review findings (`#18`) stop being theoretical for a real pull: the
mover's FUSE write has **no watchdog**, so a wedged write hangs a multi-GB part indefinitely; the
staging guard does not exist; the CDP event list grows unbounded over a multi-hour run; and the CDP
reader dies silently. At 36 KB none of these could bite. At real part sizes they can.
