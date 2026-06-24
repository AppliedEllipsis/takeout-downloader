# 11 — Manual Paste, Account Label, Job Deletion, Cache-Busting & Extension Auto-Reload

Changes made on top of the Phase 1-10 build during a live server session. All
edits were applied to the bind-mounted source on the server
(`/opt/storage.local_1/projects/takeout-downloader`) and reloaded via the s6
manager service (no container rebuild). This doc records what changed and why.

---

## 1. Manual paste box in the manager UI

**Why:** the extension auto-POST path is not always usable (token not set in the
extension, or you just want to paste a "Copy all" payload by hand). The manager
UI had no way to start a job without the extension.

**What:**
- `manager/web/index.html` — added a `#paste-panel` section (label input +
  textarea + Start button) above the jobs list.
- `manager/web/app.js` — `submitPaste()` POSTs the raw JSON to
  `/api/payload?label=<label>` with the `X-Capture-Token` header read from an
  injected `<meta>` tag.
- `manager/app.py` — `index()` injects the capture token as
  `<meta name="capture-token">` so the page can authenticate its own POST
  without the operator typing the token. Safe because the page is localhost-only
  (served inside the hosted browser / over an SSH forward).

**Use:** paste the schema-2 "Copy all" JSON (the `{ "schema": 2, "exports": [...],
"cookie": "..." }` object — NOT the broken "Copy all links" output, which emits
malformed JSON). Optionally type an account label. Click Start.

---

## 2. Account label derivation (`unknown-account` fix)

**Why:** jobs landed under `unknown-account/` because the identity
(`user`/`authuser`) lives in the export URLs and the human name is only in the
page DOM — neither was reaching the deriver.

**What (precedence is now): explicit override → email → scraped name/handle →
`gaia-<user>` → `unknown-account`.**
- `manager/orchestrator.py` `_payload_meta()` — parses `user`/`authuser` from the
  export URL query string as a fallback when the `_meta`/`meta` blocks omit them,
  and picks up a `label` field from the payload meta.
- `manager/derive.py` `account_label()` — added the scraped `label` to the
  precedence chain (between email and the gaia fallback).
- `helpers/content.js` — added `scrapeAccountLabel()` which reads the account
  switcher aria-label (`"Google Account: braincreation\n(email)"`) and extracts
  the display name even when it is not a full `@email`. `reportAccountMeta()`
  now scrapes and carries `label`.
- `helpers/background.js` — `buildManagerPayload()` puts `label` into
  `payload._meta` alongside email/user/authuser/archiveId.

**Note:** the explicit label field in the paste box (#1) overrides everything —
type `braincreation` there and the folder is `braincreation/<export-ts>/`
regardless of scrape success.

---

## 3. Job deletion ("clear failed jobs") + control-token fix

**Why:** cancelled/errored jobs lingered in the list with no way to remove them.
While wiring this, found that the UI control buttons (Pause/Cancel/Recapture)
were sending NO auth token, so they silently 401'd against the api-token-gated
endpoints.

**What:**
- `manager/orchestrator.py` — `delete_job()` removes a non-active job from the
  registry and unlinks its `.manager_state.json` so `recover()` won't resurrect
  it on restart. Refuses while `downloading`/`queued` (cancel first). Downloaded
  files are LEFT on disk; only the bookkeeping record is removed.
- `manager/app.py` — `DELETE /api/jobs/{job_id}` (api-token gated). Also injects
  `<meta name="api-token">` into the served page.
- `manager/web/app.js` — all control calls now send `X-Api-Token`; new `delete`
  action; **Remove** button in the job detail head (disabled while active).

---

## 4. Cache-busting (no more hard-refresh after a manager restart)

**Why:** UI/token changes required a manual Ctrl+Shift+R because the browser
cached `index.html`/`app.js`.

**What (`manager/app.py`):**
- `_ASSET_VERSION = str(int(time.time()))` computed once per manager start.
- `index()` rewrites `/ui/app.css` and `/ui/app.js` refs to `?v=<version>`, so a
  plain reload re-fetches them after every restart.
- The served HTML is returned with `Cache-Control: no-store` (it is dynamic — it
  carries the injected tokens + version).

**Effect:** after one final hard-refresh to load this change, all future manager
restarts only need a normal reload. (The extension is the exception — see #5.)

---

## 5. Automated extension reload over CDP

**Why:** extension code (`content.js`/`background.js`) only reloads via
`chrome://extensions`, which is a manual step in the hosted browser. Faking
Ctrl+Shift+R keystrokes is fragile.

**What:**
- `cdp_reload_ext.py` (repo root) — pure-stdlib CDP client (raw-socket
  WebSocket; the container has no node or `websocket`/`websockets` libs). It
  drives the already-open `chrome://extensions` page and calls
  `chrome.developerPrivate.reload(id, {failQuietly:false})`, which returns the
  real `reloadError` / manifest install status — then reloads the
  takeout.google.com tab (`Page.reload ignoreCache`) so the content script
  re-injects, and reports `ENABLED` + clean or fails loud.
- `webgui/reload-extension.sh` — one-command wrapper: copies the client into the
  container and runs it against `127.0.0.1:9222`.

**Use:**
```bash
ssh takeout-server '/opt/storage.local_1/projects/takeout-downloader/webgui/reload-extension.sh'
```

**Why `developerPrivate`, not the service-worker target:** MV3 service workers
unload after ~30s idle, so "no SW target present" is NOT a reliable health
signal (it gave a false FAIL). `developerPrivate` reports actual load/manifest
errors.

**Security:** CDP on `:9222` is full control of the logged-in Chrome. It is
bound to the container loopback only and is NOT proxied through the tunnel
(verified: `/json/version` via the public hostname → 404). Never expose 9222.

---

## 6. Manifest warning fix

- `helpers/manifest.json` — removed the non-standard `_comment_key` top-level
  key (MV3 rejects unknown keys with a load warning). The deterministic-ID
  `key` pin is preserved; the explanation it carried already lives in
  `08-decisions-log.md`.

---

## Operational notes learned this session

- **Never `docker compose restart webgui` to reload the manager.** The
  `cloudflared` service shares webgui's network namespace
  (`network_mode: "service:webgui"`); restarting webgui orphans the tunnel
  (Cloudflare error 1033) AND mints a new `*.trycloudflare.com` URL. Instead,
  reload just the manager: SIGKILL the uvicorn pid and let s6 respawn it
  (`s6-svc -t`/`-r` did not cycle a wedged process; SIGKILL did). This leaves
  the tunnel and its URL intact.
- **`config/` (the KasmVNC profile) holds the live Google session** — now
  gitignored. Never commit it.
- **Takeout download cookies expire fast (well under ~30 min).** Capture and
  submit within a minute or two; a stale cookie makes the engine correctly flip
  the job to `needs_cookie` with zero bytes written (it refuses to save the HTML
  login page as a `.zip`).
