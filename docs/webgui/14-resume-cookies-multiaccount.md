# 14 — Resume Internals, Live-Cookie-Jar Recovery & Multi-Account Onboarding

This documents the hard-won findings from finishing the first 3.08 TB download
(account `braincreation`), the bugs fixed along the way, the manual recovery
procedure that finally cracked the resume, and how to onboard a NEW Google
account from scratch.

Read alongside `12-operations-runbook.md` (daily ops) and
`13-migration-diskfull.md` (the disk-full root cause + repo relocation).

---

## TL;DR — what was actually wrong

The download stalled at 58/62 parts for a long time across several sessions.
Root causes, in the order they bit us:

1. **Disk full on `/`** (see doc 13) — Chrome SIGKILL/`Trace/breakpoint trap`,
   JuiceFS FUSE hangs. Fixed by relocating off `/dev/sda1` + log rotation.
2. **Filename-scheme mismatch on resume** — the first run named parts from the
   payload `exports[]` path (`takeout-…-9-001-part-NN.zip`); a later resume
   named them from the discovery sweep's content-disposition
   (`takeout-…-13-NN.zip`). Different names → the engine didn't recognize the
   2.8 TB already on disk → it started re-downloading everything.
3. **Discovery burned the short-lived cookie** — on every cookie-refresh resume
   the runner re-swept all 63 part indices (each a 1-byte Range probe, ~30–60s
   total over the network). Takeout download cookies live only ~1–2 minutes
   when idle, so the cookie died during discovery, before any real bytes moved.
   Livelock: capture → discover → expire → recapture → discover → …
4. **End-of-range HTML misread as auth failure** — probing one index past the
   last real part returns an HTML page; the engine treated ALL html as
   `AuthError`, flipping the whole job to `needs_cookie`.
5. **Label change orphaned the job** — once the DOM scrape started resolving the
   friendly label `braincreation` (instead of the `gaia-…` URL fallback), the
   output dir changed, and resume-by-dir created a NEW job instead of resuming.

All five are fixed in code (see "Fixes landed" below). The download was
ultimately completed by a **manual live-cookie-jar pull** (see procedure).

---

## The cookie is the whole game

The single most important operational fact about Google Takeout downloads:

- The download cookie (the `Cookie` header sent to
  `takeout-download.usercontent.google.com`) is **extremely short-lived when
  idle** — roughly 1–2 minutes. BUT an **in-flight download stream keeps going**
  for hours: auth is checked at request *start*, not continuously. The first
  run pulled 2.8 TB over ~6 hours on a single continuous session.
- Therefore: **any idle gap longer than ~1–2 min kills the next request.** The
  manager's per-part JuiceFS `stat()` pre-pass over 58 complete parts took ~6
  minutes — long enough to idle the cookie to death before the 4 incomplete
  parts were ever reached.
- The extension's stored `lastCapture.cookie` goes stale fast. A probe with a
  stale cookie returns **HTTP 302 → accounts.google.com/ServiceLogin** (NOT a
  clean 401). curl following that redirect lands on an HTML login page, which
  is why `curl -C -` reported "HTTP server does not seem to support byte
  ranges" (it got a 200 HTML, not a 206).

### The breakthrough: pull the LIVE cookie jar, not the stored capture

The reliable cookie source is **Chrome's live cookie jar**, read over CDP, NOT
the extension's `lastCapture`:

```
Storage.getCookies  → ~27 google.com cookies → ~3.5–4.5 KB Cookie header
```

This is always as fresh as the logged-in browser session. With it, a Range
probe of an incomplete part returns **HTTP 206 + application/octet-stream**
(resumable), where the stale 2109-char `lastCapture` cookie returned 302.

---

## PROCEDURE — manual resume of stuck partials (live-cookie-jar pull)

Use this when the manager livelocks on `needs_cookie` but the data is mostly on
disk and you just need the last few partial parts. It bypasses the manager's
slow pre-pass entirely.

Prereqs: stack up, Chrome logged into Google, CDP on `127.0.0.1:9222`,
takeout manage page open.

1. **Pause the manager job** so it stops fighting you:
   ```bash
   docker exec takeout-webgui curl -s -X POST \
     -H "X-Api-Token: $TOKEN" \
     127.0.0.1:8080/api/jobs/<job_id>/pause
   ```
   (`$TOKEN` = `MANAGER_API_TOKEN` from `webgui/.env`.)

2. **Identify incomplete parts** — compare on-disk size to target size. A part
   whose file size == its known target is complete; smaller = partial; missing
   = not started. (The manager state file is the source of target sizes, but it
   may be stale after manual surgery — trust the actual byte sizes on disk.)

3. **Read the live cookie jar over CDP** (run INSIDE the container — Chrome's
   DevTools rejects non-localhost Host headers, so host-side CDP gets
   connection-reset):
   - Open a CDP websocket to the browser target.
   - Call `Storage.getCookies`.
   - Join cookies whose `domain` contains `google.com` into a
     `name=value; name=value; …` Cookie header.

4. **Probe one incomplete part** with that cookie + `Range: bytes=0-0`. Expect
   **HTTP 206**. If you get **302 → ServiceLogin**, the session is logged out —
   re-login in the browser and retry. If **200 text/html**, same problem.

5. **Fire all incomplete parts in parallel** with resumable curl, detached
   (a 50 GB part won't fit in a tool timeout):
   ```bash
   curl -sS -C - --retry 3 \
     -H "Cookie: <live-jar-cookie>" \
     -H "User-Agent: <UA from capture>" \
     -o "<dir>/<expected-filename>" \
     "https://takeout-download.usercontent.google.com/download/<file>?j=<archive>&user=<gaia>&authuser=0&i=<index>"
   ```
   `curl -C -` resumes from the current on-disk size via Range. In-flight
   streams complete even after the cookie later rotates.

6. **Verify each part** when curl exits: on-disk size == target size, head bytes
   are `PK\x03\x04`, and the tail contains the EOCD signature `PK\x05\x06`.
   (Full `zipfile.testzip()` reads every byte — see the verification warning.)

7. **Finalize the manager job** so the UI reflects reality — rewrite
   `.manager_state.json` (status=complete, all real parts done, correct totals)
   and re-finalize the manifest. Drop any phantom end-of-range index.

### Gotcha: the phantom last index

The discovery sweep probes one index PAST the last real part to detect the end.
That phantom (here `i=62`, a 0-byte / 1.7 MB stub named `…-001.zip`) can get
seeded into the job state. It is NOT a real part — the original payload had
`expectedParts: 62` (indices 0–61). Remove the 0-byte file and drop index 62
from the state when finalizing.

---

## Fixes landed (code)

| Commit | Fix |
|--------|-----|
| `fdf8535` | Resume matches on stable `archive_id`, not the label-derived output dir, so a label change can't orphan an in-progress job. |
| `a531bba` | `probe_export` treats end-of-range HTML as a clean stop (returns invalid probe) instead of raising `AuthError`. Genuine auth still caught via the `accounts.google.com` redirect / 401 / 403. |
| `1287cf7` | (a) `engine_bridge` skips re-discovery on a cookie-refresh resume when `self._exports` is already cached — the fresh cookie is spent on bytes, not re-probing. (b) `_payload_meta` parses `archive_id` from the URL `j=` param as a fallback (matches the user/authuser URL fallbacks). |
| `616eac2` | Extension auto-cancels Chrome's native download of a part (the manager downloads server-side; the browser copy was wasted disk and a disk-full contributor). |
| `f0e9614` | Extension live status panel — per-part progress, heartbeat (time since last update + stall warning), error surface, log tail. |
| `21db89a` | Compose log rotation (10m×3) on both services — prevents the unbounded selkies log spam that filled `/`. |

---

## Known remaining sharp edges

- **The JuiceFS stat/verify pre-pass is slow.** `download_exports` stats every
  part before workers start; `verify_exports` (final) runs `zipfile.testzip()`
  which reads EVERY byte of all parts — a full 3 TB read back over the FUSE
  mount, potentially hours, with FUSE-stall risk. For very large archives,
  prefer the tail-only EOCD check over a full testzip, or verify out-of-band.
- **Cookie cadence vs pre-pass.** Even with skip-rediscovery, if the pre-pass
  for a huge archive exceeds the cookie's idle life, the FIRST resume after a
  manager restart (empty export cache) can still expire mid-pre-pass. The
  live-cookie-jar manual pull is the fallback.
- **Quick-tunnel URL is unstable** — cloudflared mints a new
  `*.trycloudflare.com` hostname on every reconnect. For a stable URL, switch to
  a named tunnel with a CF account token (compose has the commented setup).

---

## Onboarding a NEW Google account — full procedure

The system is built to handle multiple accounts; each gets its own
`<account-label>/<export-ts>/` folder under the storage root. To add a new one:

### 1. Log the new account into the hosted Chromium
- Open the portal (`https://<user>:<pass>@<tunnel>.trycloudflare.com/`).
- In the hosted Chromium, sign into the new Google account. The profile at
  `config/.chrome-profile` persists it across restarts.
- NOTE: the profile holds ONE primary signed-in account at a time for the
  download cookie. For truly separate accounts, either (a) do them
  sequentially, or (b) use Chrome profiles / `authuser=N` multi-login (the
  capture parses `authuser` from the URL, so multi-login is supported as long
  as the download URL carries the right `authuser`).

### 2. Create the export on takeout.google.com
- Go to takeout.google.com, select data, create the export. Wait for Google to
  email/notify that the archive is ready (can be hours/days for large accounts).

### 3. Capture + auto-submit
- On the Takeout "Manage exports" page, click **Download** on any one part.
- The extension (v4.1+) will:
  - capture the cookie + URL,
  - auto-cancel Chrome's native download (server-side download instead),
  - auto-POST to the manager (autoPost on),
  - the manager derives the label: explicit override → scraped email/name →
    `gaia-<user>` → `unknown-account`.
- Watch the extension popup's **live status panel** for per-part progress,
  heartbeat, and errors.

### 4. Verify extension settings (one-time per profile)
The extension reads `managerUrl` + `captureToken` from:
- `chrome.storage.local` (set via the options page or CDP), AND
- a managed-policy file `init_custom.sh` writes to
  `/etc/chromium/policies/managed/takeout-manager.json`.

Confirm in the popup that "Manager" shows connected and the token is set. If
auto-POST 401s, the capture token isn't reaching the extension — set it on the
options page or re-run the storage-set over CDP.

### 5. Account-label folder
The download lands at `<STORAGE_ROOT>/google-takeout/<label>/<export-ts>/`. For
`braincreation` that was `…/braincreation/2026-06-23-03-59-47/`. Each account +
export timestamp is isolated, so multiple accounts/exports never collide.

### 6. If resume is needed
Cookies expire; large downloads span multiple cookie refreshes. With
autoRecapture ON, the extension re-clicks Download to refresh the cookie when
the manager signals `needs_cookie`. If it livelocks (see cookie section), fall
back to the manual live-cookie-jar pull procedure above.

---

## Is everything coded + documented for future accounts?

**Yes, with the caveats above.** The pipeline is account-agnostic:
- Label derivation handles any account (email scrape, gaia fallback, override).
- Output dirs are isolated per account+export.
- archive_id matching means re-captures resume the right job regardless of
  label.
- The extension auto-submits and shows live status.

The two operational realities a future operator MUST know are documented here:
1. **Cookie cadence** — keep downloads moving; idle gaps kill the cookie. Use
   the live-cookie-jar pull for stuck partials.
2. **JuiceFS verify cost** — don't run a full testzip over multi-TB archives on
   the FUSE mount; use tail/EOCD checks.
