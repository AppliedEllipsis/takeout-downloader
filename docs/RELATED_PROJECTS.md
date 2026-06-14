# Related projects — research & lessons integrated

A survey of other Google Takeout download/processing tools, what each does
well, and what we borrowed (or deliberately left out). Done 2026-06.

The field splits cleanly into two camps:

1. **Downloaders** — get the archive parts off Google. Directly comparable
   to this project.
2. **Photo post-processors** — take the *extracted* Takeout and fix photo
   metadata / organize albums. Adjacent; out of our current scope but
   worth linking for users as a "next step."

---

## Downloaders

### [tarballz/mass-takeout-downloader](https://github.com/tarballz/mass-takeout-downloader) — MV3 extension, bounded concurrency
The most mature competitor and the richest source of lessons. It scrapes
the Takeout *manage-exports* page, then drives Chrome's `chrome.downloads`
API N-at-a-time. Lessons we acted on:

| Lesson | What they do | What we did |
|--------|--------------|-------------|
| **Thundering herd** | exponential backoff **with jitter**, capped at 2 min | Added `compute_backoff()` with full jitter + `RETRY_MAX_WAIT` cap (was lockstep `2**attempt`). |
| **Permanent vs transient** | 403/401/disk-full skip retries; `NETWORK_*`/`SERVER_FAILED`/stalls retry up to 6× | We raised `MAX_RETRIES` 3→6 and already return immediately on `AUTH_FAILED`/`NOT_FOUND`/`INTEGRITY_FAILED`. |
| **HTML-as-success trap** | a "completed" download with `text/html` mime is treated as garbage, erased, retried | We already detect this (content-type + `PK` magic-byte check on first chunk). Confirmed our approach matches theirs. |
| **Rate limiting** | per-account throttle is real | Added explicit `429`/`503` handling that honours `Retry-After` then falls back to jittered backoff. |
| **Signed URLs expire ~1 week** | distinct from cookie expiry | Documented in `USAGE.md` troubleshooting — "regenerate the export" vs "re-capture the cookie." |
| **resume not restart** | calls `chrome.downloads.resume()` on resumable interruptions | We already do HTTP `Range` resume from the `.downloading` temp file. |

What we did **not** take: their whole-page-scrape + `chrome.downloads`
model. We're deliberately a capture→clipboard→TUI relay (the user is the
trust boundary), so the browser never auto-drives downloads.

### [Croissanthology/takeout-choo-choo](https://github.com/Croissanthology/takeout-choo-choo) — parallel resumable bash + train dashboard
Per-run workspace dir holding `headers.txt`, `urls.txt`, `download.log`,
`run.pid`. Knobs worth noting:
- `PARALLEL=5` — calls 5 "the practical max given Google's per-account
  rate cap." Matches the 3–10 consensus; reinforced our recommended
  default of ~4.
- `LIMIT_RATE` per-connection cap — useful when sharing a link. **Candidate
  future feature** (we don't throttle bandwidth yet).
- Separate `headers.txt` + `urls.txt` skeleton from a Copy-as-cURL — same
  data our JSON payload carries, just split across files.

### [cschladetsch/PyGoogleTakeoutDownloader](https://github.com/cschladetsch/PyGoogleTakeoutDownloader) — Python, credential-based
Logs in with **stored email/password + 2FA secret** and refreshes auth
tokens automatically. Has `--start`/`--end` index range, `--delay` between
files, `--continue` to resume. We **rejected** the stored-credential model
on security grounds (storing a Google password + TOTP secret is a much
bigger blast radius than a short-lived session cookie). But two ideas are
worth adopting:
- **`--start`/`--end` part range** — lets you re-pull a known-bad part
  without rescanning. *Candidate CLI feature.*
- **Configurable inter-file delay** — gentler on the rate cap than firing
  all parallel workers at once.

### [JessSanChen/google-takeout-downloader](https://github.com/JessSanChen/google-takeout-downloader) & [feragostb-lab/takeoutDownloader](https://github.com/feragostb-lab/takeoutDownloader)
Smaller extensions that scrape download links from the page DOM. feragostb
has two nice touches:
- **i18n link detection** — matches both "Download" and "Descargar", and
  reads `aria-label` to detect already-downloaded parts in multiple
  languages. A reminder that the Takeout UI is localized; pure English
  text-matching is fragile.
- **A test fixture** (`GoogleTakeoutDemo.html`) asserting "131 links found,
  54 already downloaded, 77 to download." Good pattern — capture a real
  page once and regression-test the scraper against it.

---

## Photo post-processors (adjacent — the "next step" after download)

These operate on the **extracted** Takeout, not the download. We link them
from `USAGE.md` so users know what to do with the zips once they land.

### [TheLastGimbus/GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper) (`gpth`)
The de-facto standard. Organizes the whole archive into one chronological
folder, restores dates, handles albums. Key takeaways relevant to *us*:
- Confirms our docs should tell photo users to **merge multiple archives'
  `Takeout/` folders** before processing (same-named dirs merged, not
  overwritten).
- Has a no-UI mode specifically for headless/NAS/Synology — same
  environment many of our users download into.

### [mattwilson1024/google-photos-exif](https://github.com/mattwilson1024/google-photos-exif)
Best documentation of the **JSON-sidecar matching quirks**, which matter if
we ever add a post-process step:
- A media file `foo.jpg` may have `foo.json` **or** `foo.jpg.json`.
- Numbered duplicates are counter-intuitive: `foo(1).jpg` → `foo.jpg(1).json`.
- Google truncates sidecar base names around ~46 chars.
- Export tip we adopted into docs: **deselect custom (non-date) albums** at
  export time or you get duplicate copies of every photo.

### [Greegko/google-metadata-matcher](https://github.com/Greegko/google-metadata-matcher)
Minimal, focused merger of Google Photos metadata into media. Same problem
space as google-photos-exif; useful as a smaller reference implementation.

### [purarue/google_takeout_parser](https://github.com/purarue/google_takeout_parser)
Different axis entirely — parses the **non-photo** Takeout data (search
History, Activity, YouTube, Location) into typed Python objects, with a
cache and a CLI/REPL. Strong advice we surface in docs: **export JSON, not
HTML**, whenever Takeout gives the choice — the HTML parsers are slow and
fragile. Relevant if we ever add a "what's in this archive?" inspect mode.

---

## Summary of concrete changes made this round

Code (in `takeout.py` + `google_takeout_tui.py`):
- `compute_backoff(attempt)` — exponential backoff with **full jitter**,
  capped at `RETRY_MAX_WAIT` (default 120 s). Replaces lockstep `2**attempt`
  in every retry site across both the engine and the TUI download path.
- `_retry_after_seconds(response)` — honours `Retry-After` on 429/503.
- Explicit **429 / 503 rate-limit handling** with a `RATE_LIMITED` terminal
  error after retries are exhausted.
- `MAX_RETRIES` default **3 → 6**; new `RETRY_MAX_WAIT` env knob.

Docs:
- This file.
- `USAGE.md`: signed-URL-expiry-vs-cookie-expiry distinction; pointer to
  `gpth` / google-photos-exif for post-download photo processing; the
  "deselect custom albums / export JSON not HTML" export tips.

Deliberately **not** adopted:
- Stored Google credentials / automated login (PyGoogleTakeoutDownloader) —
  security blast radius too large vs. a short-lived cookie.
- Whole-page DOM scrape + `chrome.downloads` auto-drive (mass-takeout,
  feragostb, JessSanChen) — conflicts with our user-as-trust-boundary
  relay model.

Candidate future features logged:
- `--start`/`--end` part range for targeted re-pulls.
- Per-connection bandwidth cap (`LIMIT_RATE` equivalent).
- Configurable inter-file delay.
- A post-download "inspect / fix photo metadata" handoff to gpth.
