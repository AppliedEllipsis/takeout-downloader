# Handoff — JSON payload relay feature

## TL;DR for the incoming agent

There are **two agents** working this repo (same working tree). You (the
newer agent) appear to be ahead of me. I'm stopping and handing off because
your model of the project is more current than mine. Specifically:

- I was still planning around **Tampermonkey userscript + bookmarklet + Web UI**.
- You have already moved to **Chrome-extension-only**, and the new
  `manifest.json` description says the payload targets the **TUI** (not Web).
- **Please treat your direction as authoritative.** This file documents
  what I found in the tree so you don't have to re-derive it, plus the
  loose ends I see.

## The shared vision (what we both agree on)

User becomes the **relay / trust boundary**. No silent auto-send.

```
Browser (takeout.google.com)
  → Chrome extension captures {url, cookie, headers}
  → "Copy as JSON"  →  USER'S CLIPBOARD (visible, inspectable)
  → user pastes into TUI
  → TUI parses JSON → download
```

Auto-send (direct POST from extension → server) is **removed entirely**.
Payload is **fully self-contained** JSON. Import is **dedicated button +
auto-detect** (`{` prefix → JSON, else cURL).

## Current state of the working tree (as of this handoff)

`git status` (uncommitted, nothing committed yet):

```
 M helpers/background.js     (419 lines churned — reworked to JSON copy flow)
 M helpers/manifest.json     (v1.0.0 → v2.0.0)
 M helpers/options.html
 M helpers/options.js
 M helpers/popup.html
 M helpers/popup.js
?? takeout_payload.py        (NEW — Python schema/parser, 307 lines)
?? docs/BEST_PRACTICES.md    (NEW/untracked)
```

### What already exists and looks DONE (your work, I did not touch):

1. **`takeout_payload.py`** — Python side of the schema. Key surface:
   - `SCHEMA_VERSION = 1`
   - `DEFAULT_HEADERS` dict (UA/Accept/Referer that Google requires)
   - `REQUIRED_COOKIE_MARKERS` — used to detect pre-redirect captures
   - `TAKEOUT_FILENAME_RE` — `takeout-YYYYMMDDTHHMMSSZ-N-NNN.zip` matcher
   - `@dataclass TakeoutPayload` with:
     - `from_json(text)` — strict parse, raises `ValueError` on schema
       mismatch / missing url / missing cookie / non-takeout url
     - `from_curl(curl_text)` — reuses takeout.py extractors
     - `validate() -> (ok, warning_or_error)` — checks cookie markers,
       URL pattern, and warns if capture is >60 min old
     - `to_json(indent=2)`, `to_curl()`, `cookie_chars()`, `filename_hint()`
   - `parse_payload(text)` — top-level auto-detect (`{` → JSON else cURL)

   Schema v1 (from the module docstring):
   ```json
   {
     "schema": 1,
     "captured_at": "ISO-8601 UTC",
     "source": "extension|curl|powershell|manual",
     "url": "https://...",
     "method": "GET",
     "headers": { "User-Agent": "...", "Accept": "...", "Referer": "..." },
     "cookie": "SID=...; HSID=...; ..."
   }
   ```
   NOTE: this schema is **capture-only** — it does NOT carry server
   settings (output_dir/parallel/file_count) or server url/auth. That
   differs from the "fully self-contained incl. server+auth" idea I had
   queued. **Your schema is the source of truth now.** If server config
   should travel in the payload, it needs to be added here first.

2. **`helpers/manifest.json`** — MV3, v2.0.0:
   - description: "...copies them as JSON for the Takeout Downloader **TUI**"
   - permissions: `webRequest, storage, activeTab, notifications`
   - host_permissions now include
     `takeout-download.usercontent.google.com/*` (the real download host)
   - `background.service_worker` is now `type: "module"`
   - `content_security_policy.extension_pages` set
   - `minimum_chrome_version: 102`
   - NOTE: no `clipboardWrite` permission — if copy uses
     `navigator.clipboard.writeText` from the popup that's fine (popup has
     a focused document), but verify it works; otherwise add the perm or
     use the textarea+execCommand fallback.

3. **`helpers/background.js`, `popup.html`, `popup.js`, `options.html`,
   `options.js`** — all reworked (548 insertions / 432 deletions total).
   I did NOT read these line-by-line; assume they're your JSON-copy
   implementation. Verify they no longer `fetch()` the server and that the
   remote-confirm / autoSend dialogs are gone.

### Files I expected to change but you may have intentionally dropped:

- `helpers/takeout-extractor.user.js` (Tampermonkey) — **still on disk,
  unmodified.** You said userscript is being removed. **Action: delete it**
  (and any references in README/USAGE) if extension-only is the decision.
- `helpers/bookmarklet.html` — **still on disk, unmodified.** Same deal:
  delete if bookmarklet is dropped.

## Loose ends / open decisions (need your call)

1. **TUI import not done yet.** `google_takeout_tui.py` has NOT been
   touched. It still imports from `takeout` only. Needs:
   - an "Import JSON" button + paste modal (Textual `ModalScreen`)
   - call `parse_payload()` / `TakeoutPayload.from_json()`
   - surface `validate()` warnings (esp. the >60min and cookie-marker ones)
   - auto-detect: if the cURL TextArea content starts with `{`, route
     through `parse_payload` on Start
   - feed result into the existing download path (it currently uses
     `extract_cookie_from_curl` / `extract_url_from_curl`; `to_curl()`
     bridges payload → existing path cleanly)

2. **Web UI: is it still in scope?** Your manifest text says "TUI". My old
   plan included `google_takeout_web.py` import UI. If Web is being
   dropped from this feature, say so; otherwise it needs the same
   import-button + auto-detect treatment. `google_takeout_web.py` is
   **untouched** right now.

3. **No tests yet.** `takeout_payload.py` has zero test coverage. Suggested
   `tests/test_takeout_payload.py`: round-trip `to_json→from_json`,
   reject bad schema/missing fields, `validate()` cookie-marker + age
   warnings, `from_curl` happy path, `parse_payload` auto-detect branch.

4. **Schema scope mismatch** (see note under item 1 above): decide whether
   server settings + auth belong in the payload. Current code = no.

5. **Docs drift.** README.md and USAGE.md still describe three helpers
   (extension/userscript/bookmarklet), auto-send, `/api/start` POST flow,
   and the per-session remote-confirm prompt. All of that is now wrong.
   `docs/BEST_PRACTICES.md` is untracked (yours?) — README/USAGE not yet
   updated.

## What I changed this session

**Nothing in code.** I only read files and created this `handoff.md`.
(I accidentally created a stray `nul` file via a bad shell redirect and
already deleted it — `git status` is clean of it.)

## My original todo list (for reference — yours supersedes it)

1. [in_progress] Define JSON payload schema + helpers in `takeout_payload.py`
   → **actually already DONE by you**, mark complete
2. [pending] Add tests for takeout_payload module → still TODO
3. [pending] Strip auto-send from Tampermonkey userscript; add Copy JSON
   → **obsolete if userscript is deleted**
4. [pending] Strip auto-send from Chrome extension; add Copy JSON
   → **appears DONE by you** (verify)
5. [pending] Replace bookmarklet with Copy-as-JSON variant
   → **obsolete if bookmarklet is deleted**
6. [pending] Add Import JSON button + modal + auto-detect to TUI → still TODO

## Suggested next actions for you (in order)

1. Delete `helpers/takeout-extractor.user.js` + `helpers/bookmarklet.html`
   if extension-only is final; scrub their mentions from README/USAGE.
2. Confirm the extension files (`background.js`/`popup.*`/`options.*`) do
   what you intend (no server fetch, clipboard copy works).
3. Implement TUI import (button + modal + auto-detect) against
   `takeout_payload.parse_payload`.
4. Decide Web UI in/out of scope; implement if in.
5. Add `tests/test_takeout_payload.py`.
6. Rewrite README/USAGE for the relay model; remove auto-send and the
   2 dropped helpers.
7. Decide whether server settings/auth belong in the schema; if yes,
   extend `takeout_payload.py` first, then the extension JSON, then TUI.
