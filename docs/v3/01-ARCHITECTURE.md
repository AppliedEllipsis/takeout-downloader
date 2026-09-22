# v3 "autopilot" — architecture

**Status:** design, grounded in measurement. Supersedes the v3 sketch the prior session left in a chat
log, and supersedes the cookie-replay architecture of all three earlier generations.
**Evidence:** every design choice below cites a measurement. The measurements live in
`.wiki/measured-facts.md` (worktree) and `.recon/11-root-cause-confirmed.md`,
`.recon/12-ladder-results-and-architecture.md` (main checkout).

---

## 0. The premise, in one page

The project failed for ~9 months across three architectures because it solved the wrong problem. What
was believed: *the download cookie goes stale in 1–2 minutes, so replay it fast from a second client.*
What is true:

| Fact | Evidence |
|---|---|
| The download URL demands an **interactive ReAuth (password) challenge** | The real browser, signed in, was bounced `ServiceLogin → InteractiveLogin → challenge/pwd`, landing on *"To continue, first verify it's you. Enter your password."* |
| Satisfying it appends **`rapt=`** (ReAuth Proof Token) to the archive page URL, which propagates into the download links | Measured on the archive page after satisfying it |
| **`rapt` gates MINTING, not transferring** — the minted file-host URL carries no `rapt` | Measured: the file-host URL is `…?j=…&i=0&user=…&authuser=0` |
| A replayed cookie **can never** mint | No `rapt`, and it cannot answer a prompt. Impossible by construction — not a bug |
| A replayed cookie **does** work against the file host | `Range: bytes=0-1023` + jar → `206`, 1024 bytes, real `PK\x03\x04` magic |
| The file host supports `Range` and exposes a **strong validator** | `ETag: f470fe32-…-1005482974000-0`, `Accept-Ranges: bytes` |
| Bounced requests cost **0** attempts; `Range` requests to the file host cost **0** | Counter Δ0 in every such run |
| The counter is **not** a reliable per-download oracle | Same action gave Δ2 then Δ0; serialised sampling showed no lag; the discriminating test died on CORS |
| The file host sends **no CORS headers** | Page-context `fetch()` of a minted URL fails `TypeError: Failed to fetch` |

**Therefore the architecture is a division of labour:**

> **The browser mints. The transporter moves.** The browser is the only thing that can turn
> `j`/`i`/`user` + a `rapt` into a signed file-host URL — because only a real browser can satisfy the
> ReAuth prompt that produces the `rapt`. Once minted, an ordinary HTTP client with the live cookie jar
> moves the bytes, and `Range` resumes are free.

That is exactly what the one hand-finished 3.08 TB export did by hand. v3 automates it.

---

## 1. Actors

```
┌──────────────────────────── takeout-webgui (container) ────────────────────────────┐
│                                                                                    │
│   CHROMIUM  ──(CDP 127.0.0.1:9222)──┐                                              │
│     • holds the Google session      │                                              │
│     • satisfies ReAuth (a human)    │                                              │
│     • mints signed URLs when the     │                                             │
│       daemon navigates the redirector│                                             │
│                                         ▼                                          │
│                             EXTENSION (helpers/, MV3)                              │
│          • auto-cancel of native downloads is OFF (owner directive 2026-09-22)     │
│       • autoRecapture OFF by default — the tab-flood spawner stays disabled        │
│                                                                                    │
│                              AUTOPILOT DAEMON (python)                             │
│                                • ledger  • scheduler                               │
│                                • transporter (Range, free retries)                 │
│                                • verifier • mover • reporter                       │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
        │ stages to                                   │ moves verified parts to
        ▼                                             ▼
   /config (LUKS, 293 GB free)                   /opt/archives (rclone remote)
   LOCAL disk, cheap full reads                  high-latency FUSE, single copy
```

**Why the daemon is not in the browser:** the transfer needs no browser context once a URL is minted,
and a python process is far easier to reason about, test offline, and control than extension code. The
browser's job is deliberately reduced to the two things only it can do: *hold the session* and *produce
a `rapt`*.

## 2. The state machine

```
                  ┌──────────────────────────────────────────────┐
                  │                                              │
   INIT ──► CHECK_AUTH ──► (no live rapt) ──► NEEDS_REAUTH ───────┤
                  │                              │  human / credential
                  │ (rapt live)                  ▼
                  ▼                        await satisfaction
              SCRAPE  ◄────────────────────────┘
                  │  read /manage/archive/<id>: parts by i=, sizes, dl_counts
                  ▼
               MINT     navigate the redirector; capture the 302 Location from CDP
                  │     Network events (observe-only — see §4.1)
                  ▼
            TRANSFER    Range requests with the live jar; free retries
                  │     ETag-guarded resume; stall watchdog
                  ▼
             VERIFY     on LOCAL disk — header + EOCD (+ hash when asked)
                  │
                  ▼
               MOVE     staging → /opt/archives, idempotent, resumable
                  │
                  ▼
              COMPLETE ──► REPORT
```

**`NEEDS_REAUTH` is a first-class state, not an error.** Every prior generation collapsed a login
redirect into a generic auth failure, parked a job as `needs_cookie`, and — because the park was
indefinite — spawned a tab every minute for three months. v3 must never do that: a challenge is a
*known, expected, actionable* condition with an owner and an alert.

**⚠️ MEASURED CONSTRAINT (2026-09-19): the account has SMS / Google-prompt 2-Step Verification.**
Therefore a stored password **cannot** clear the challenge — the password advances the flow to a 2FA
demand that no script can answer. Consequences, recorded as fact rather than preference:

- **Human-in-the-loop is mandatory for this deployment, not an option.** §4.2's "stored credential"
mode is **not reachable** for this account and must not be built as if it were.
- **v3 needs no secret at all.** That is a materially better security posture than storing a password,
and it falls out of the constraint rather than being a compromise.
- **Alerting is load-bearing.** Because re-auth recurs (the `rapt` window), the notification must be
actionable and the job state resumable. `manager/notify.py` (Telegram) already exists.
- The only route to unattended operation would be switching to an authenticator app and supplying the
TOTP seed — which, combined with the password, **collapses 2FA entirely** for that account. Not a
recommendation; recorded so the trade-off is explicit if it is ever revisited.

## 3. Modules

| Module | Responsibility | Notes |
|---|---|---|
| `autopilot/run.py` | The orchestrator: scrape → mint → transfer → verify → move | ReAuth, expiry and quota become statuses, never exceptions. Contract: `02-RUN-INTERFACE.md` |
| `autopilot/scrape.py` | Read `/manage/archive/<id>` over CDP; build the part list | Key every attribute by the `i=` from `data-download-uri` — **not** by filename (v2's `capturePayload` regression). Assert `distinct(i) == N` before planning |
| `autopilot/mint.py` | Drive minting; capture the `Location` from CDP `Network` events | Navigate plainly and observe — aborting breaks the mint (§4.1); raises `NeedsReauth`, `QuotaExceeded` |
| `autopilot/transport.py` | Move bytes: jar from CDP, `Range` resume, ETag validation, stall watchdog | The only module that talks to the file host |
| `autopilot/cdp.py` | Transport-agnostic CDP client | One background reader routes replies and events; a concurrent reader is mandatory |
| `autopilot/ws_transport.py` | Real-socket transport for `cdp.py` | The only module that imports a websocket library |
| `autopilot/jar.py` | Pull the live Google cookie jar from the browser | The jar authenticates *transfers*; only the transporter needs it |
| `autopilot/ledger.py` | Jobs, parts, attempts, minted-URL cache | **SQLite on local disk.** Never on the rclone FUSE mount (v1 is on it now) |
| `autopilot/verify.py` | Integrity on local staging | **Reuse v2's `verify.py`** — header + EOCD + optional hash. It is measured-good |
| `autopilot/mover.py` | Staging → `/opt/archives` | Streaming, idempotent, no `stat` storms (§4.4) |
| `autopilot/report.py` | Completeness report + alerts | Telegram notifier already exists (`manager/notify.py`) |
| `autopilot/errors.py` | The v3 failure-type family (`NeedsReauth`, `QuotaExceeded`, …) | One import; the distinctions the system depends on are named, not string-matched |
| `autopilot/__main__.py` | CLI: `run` / `report`; maps statuses to exit codes 0/1/2/3/4 | The shell/cron surface — see `02-RUN-INTERFACE.md` |
| `helpers/` | MV3 extension: cancels the browser's stray native downloads (auto-cancel stays ON) | Scrape/mint moved to Python over CDP (`scrape.py`/`mint.py`); the extension no longer drives them |

**Still to be deleted — a plan, not done** (migration step 4; see `03-OPERATIONS.md` §5): the CDP cookie
*capture* pipeline (`takeout2/cookie.py`, still present at 146 lines), the capture→POST→replay path, the
`needs_cookie` park (`manager/app.py` still references it), and the 1-minute recapture alarm — which was
**gated off by default on 2026-09-19** and is no longer created while the opt-in is off, but whose code
remains. All four exist to serve a premise that is now measured false.

⚠️ **Order matters:** the manager must stop driving `NEEDS_COOKIE` jobs (step 3) *before* the extension's
half is deleted (step 4). Removing the extension's half first leaves a live spawner with nothing to stop
it, and re-enabling `autoRecapture` would resume the tab flood.

## 4. The decisions that matter

### 4.1 Minting — **RESOLVED BY EXPERIMENT: navigate plainly, observe with `Network`, never abort**

> **Revised 2026-09-19 after a control experiment overturned the original proposal.** The first version of
> this section claimed CDP `Fetch` interception could mint without moving bytes. **It cannot.** The
> failure is recorded here because the result is more useful than the guess was.

**Measured, in order:**

| Mechanism | Result |
|---|---|
| Navigate the redirector under CDP `Fetch` interception, then abort the request | **Bounces.** `302 -> /manage/archive/<id>?user=…&rapt=<fresh>&j=…`. Never reaches the file host. Retrying with the refreshed `rapt` bounced again. **ΔN = 0, no mint.** |
| **Navigate the redirector plainly, observe only** | **Mints.** `302 takeout/download` → archive `302` → `RotateCookiesPage?rot=3` → **`302 takeout-download.usercontent.google.com/download/…zip`** → `200`. **ΔN = +1.** |

**Aborting the request is what breaks the mint.** A mint requires the redirector request to proceed to
completion.

**The design that follows:**

1. **Navigate the redirector plainly.** Cost: **exactly one attempt** — the first clean cost measurement
   in this project.
2. **Capture the minted URL from CDP `Network` events** (`Network.requestWillBeSent`'s `redirectResponse`,
   plus the file-host `Network.responseReceived`). **Observe-only — nothing is aborted**, so nothing breaks.
3. **Observe-only — nothing is aborted**, so nothing breaks. (Historically the extension's
   `autoCancelDownloads` also cleaned up the stray native download the browser starts; **that switch is
   now OFF by owner directive** and the stray download is left alone. It is not bounded by a cancel any
   more — watch `/config/Downloads` on a large pull. See failure mode 1.22.)
4. **Never abort a request to dodge the attempt.** That attempt is the price of the URL.

**Why this is affordable:** a mint is **Δ1** and a `Range` resume is **Δ0**. A 5-attempt allowance buys
**5 mints**; one mint plus unlimited free resumes finishes a part comfortably. **The budget is no longer
the binding constraint.**

**Also note** the successful chain passes through `accounts.google.com/RotateCookiesPage?og_pid=192&rot=3`
— Google's cookie-rotation endpoint — consistent with rotation driving the `rapt` window.

**Superseded:** the "three candidates" table (CDP `Fetch` interception, `chrome.webRequest.onBeforeRedirect`,
and extension-context `fetch`) is retained only in git history. Candidate 1 is **measured false**; the
others were never needed once the simple approach was shown to work.

### 4.2 ReAuth is the owner's decision

The password challenge is unavoidable, and **for this account it is unavoidably interactive.**

**Measured 2026-09-19: SMS / Google-prompt 2-Step Verification is enabled.** A stored password therefore
**cannot** complete the flow — it advances to a 2FA demand no script can satisfy. So:

- **Human-in-the-loop is the only implemented mode, not a default among options.** Detect the challenge,
  park the job in `NEEDS_REAUTH`, alert the owner, and resume automatically once the session is
  authorised again. **No secret is stored anywhere.**
- ~~Stored credential (opt-in, off by default)~~ — **not buildable for this deployment.** Should the owner
  ever migrate 2FA to an authenticator app, the TOTP seed *plus* the password would together **collapse
  2FA entirely** for the account. That trade-off is recorded in `decisions.md` and is not recommended.

**The prior generations never surfaced this choice at all**, which is part of why they kept being rebuilt.
A secondary requirement follows directly from the constraint: because re-auth recurs, **alerting is
load-bearing** — the notification must be actionable and the job state must be resumable mid-window.

### 4.3 The ledger models minting, not requests

Minting is the scarce operation; `Range` retries are free. So:

- One **minted URL per part**, cached in the ledger with its mint time, and **never re-minted while
  valid** — a re-mint risks spending budget for nothing.
- Attempts are recorded per *mint* and per *transfer*, not per HTTP request.
- `dl_counts` is stored as **telemetry**, not as a ledger invariant, and is used only to warn.

### 4.4 Staging, verification and the mover

The historical ~6-minute-per-part `stat()` pre-pass on the FUSE mount, and the `state.db`-on-FUSE
hazard (still live in v1), both come from treating remote storage as local.

- **Stage to `/config`** (LUKS, 293 GB free, verified). Verify there, where full reads are cheap.
- **Write nothing to `/`** — 13 GB free. Refuse if a staging path is not under the approved root.
- **Move to `/opt/archives`** as a streaming, resumable operation. Never `stat` per part in a loop.
- **Ledger on local disk.** A WAL-mode SQLite database on a network FUSE mount is a corruption risk, and
  v1 is doing exactly that right now.

### 4.5 Budget pessimism

Because the counter is untrustworthy and the true per-part cost is unresolved, the scheduler:

- assumes the **worst** plausible cost per mint and per transfer,
- prefers **`Range` resumes** (consistently measured free) over re-mints,
- never re-mints a part that already has a valid URL,
- stops a part after a **conservative** attempt cap and reports it rather than gambling.

## 5. Invariants (testable)

1. **The browser never downloads Takeout bytes.** Auto-cancel stays ON; minting navigates the
   redirector and observes over CDP `Network`, and never aborts the request (§4.1).
2. **A replayed cookie is used only for *transferring*, never for *minting*.**
3. **The ledger file is never on a FUSE mount.**
4. **Every scraped attribute is keyed by `i=`, and `distinct(i) == N` is asserted before planning.**
5. **A login redirect is never a generic failure** — it is `NEEDS_REAUTH`, with an alert.
6. **No tab is opened without a cap**, and the cap is enforced in the extension, not by convention.
7. **A part is only reported complete after verification on local disk.**
8. **Nothing is written outside the approved staging root** (the mount-death guard v2 got right).

## 6. What we deliberately keep from v2

v2 was not naive; its *premise* was wrong and its *plumbing* was buggy. These are measured-good and
should be carried over rather than rewritten: `verify.py` (header + EOCD + hash), `preflight.py`
(mount checks), `cachewatch.py` (rclone VFS backpressure), the aligned resume rewind, batched `fsync`,
the reserve-attempt-before-request discipline, the zero-retry HTTP session so `classify.py` sees the
raw 302, and the storage preflight that refuses to write onto a nearly-full root.

## 7. Open unknowns

| Unknown | Why it matters | Cost to settle |
|---|---|---|
| The `rapt` window length | batch sizes; whether URLs can be minted ahead | 1–2 attempts per checkpoint, in-browser only |
| Does a minted URL outlive the `rapt`? | decides the whole scheduling design | 1 mint + timed probes |
| True per-part attempt cost | sizing the budget | blocked: the counter is not trustworthy |
| Why the counter read 2 after one download | would tell us if it can ever be trusted | needs a serialised protocol with a second part |

**None of these change the architecture.** They change tuning. That is the point of the design: it is
shaped so that being wrong about them costs throughput, not correctness.
