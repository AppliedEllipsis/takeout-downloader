# Google Takeout Downloader — Best Practices Research

> Compiled from 10+ external sources: GitHub repos, blog write-ups, gists,
> Reddit/Stack Overflow threads, Google docs. Each section ends with a
> concrete **Apply to this repo** recommendation so the findings translate
> into code changes.

## Sources surveyed

| Source | What it is | Stars/Notes |
|---|---|---|
| [Fallenstedt/google-takeout-sucks](https://github.com/Fallenstedt/google-takeout-sucks) | Go CLI, OAuth + Drive API downloader | 46★, worker-pool, downloads from Drive folder |
| [yottabit42/gtakeout_backup](https://github.com/yottabit42/gtakeout_backup) | Bash, cURL + rclone pipeline | 133★, 50-cURL paste workflow |
| [smashah/…/goog-takeout-bulk-download.sh](https://gist.github.com/smashah/67863f6c5f500c9098ad7c7e74eefc11) | Bash, wget + slot workers | real-time progress UI |
| [Max Glenister — Pulling Google Takeout to a NAS](https://blog.omgmog.net/post/downloading-google-takeout-to-a-nas/) | Blog + scripts, **CurlWget** approach | definitive cookie-redirect write-up |
| [CorpIT — Download Takeout with aria2](https://www.corpit.org/how-to-download-google-takeout-with-aria2/) | aria2c how-to | exact `aria2c` flags |
| [albertz/chrome-ext-google-takeout-downloader](https://github.com/albertz/chrome-ext-google-takeout-downloader) | Chrome ext (MV2) | downloads API automation |
| [jprouty/google-takeout-downloader-chrome-ext](https://github.com/jprouty/google-takeout-downloader-chrome-ext) | Chrome ext (MV3, TS) | modern extension pattern |
| [nelsonjchen/gargantuan-takeout-rocket (GTR)](https://github.com/nelsonjchen/gargantuan-takeout-rocket) | Serverless Azure transloader | 1 GB/s, 1000 MB chunked shotgun |
| [Google Data Portability API](https://developers.google.com/data-portability) | Official API | OAuth scope `takeout.download` |
| [Stewart McGown — Takeout API gist](https://gist.github.com/stewartmcgown/7f5dcbf4ccd385637786f9581b620e6a) | Reverse-engineered API spec | `takeout_pa.exports.*` |
| [Vipin PG — Gmail Takeout + Rclone](https://vipinpg.com/blog/building-a-gmail-takeout-incremental-backup-pipeline-with-bash-and-rclone/) | Recurring backup pipeline | cron-friendly |
| [StackOverflow 54316824](https://stackoverflow.com/questions/54316824/automate-google-takeout-download) | Puppeteer/Selenium automation | end-to-end flows |

---

## 1. Authentication — the cookie-redirect trap

### What every source agrees on

Google Takeout URLs go through a **redirect chain** across multiple Google
domains before landing on `takeout-download.usercontent.google.com`. The
critical auth cookies (`__Secure-1PSID`, `__Secure-3PSID`, `SIDCC`, …) are
tied to the **final** domain — they do **not** survive the redirect in most
cookie-export extensions.

> "The obvious approach was to export browser cookies to a `cookies.txt`
> file (via an extension like 'Get cookies.txt LOCALLY') and pass them to
> `curl` or `aria2c`. **This doesn't work**." — Max Glenister

The reliable way is to copy the request **after Chrome has already followed
the redirect** — either:

- Right-click the request to `takeout-download.usercontent.google.com` in
  DevTools → *Copy as cURL (bash)* / *(PowerShell)*, **or**
- Use the **CurlWget** Chrome extension on an in-flight download. It
  captures Chrome's actual headers (User-Agent, Accept, Accept-Language,
  Referer, full Cookie string) pointed at the final host — no redirect.

> "Chrome had already followed the redirect, so the cookies are tied to
> the final domain, not the Takeout one."

### Cookie lifetime is short

> "Each session got me about **7 files (~14 GB)** before they expired and
> downloads started coming back as HTML again."

**Apply to this repo:**
- ✅ **Already done**: PowerShell + bash cURL parser
  (`extract_cookie_from_curl`, `extract_cookies_from_powershell`).
- 🟡 **Add**: a "⚠ cookie may have expired" hint to the Web UI when the
  response is HTML (`Content-Type: text/html` and starts with `<`) — we
  already detect this in `auth_failed`, but the message should remind
  users *exactly how* to re-grab (right-click → Copy as cURL, or
  CurlWget) instead of "auth failed".
- 🟡 **Add**: a `--cookie-warning-after N` knob that flips the Web UI to
  amber once N files have succeeded since the last cookie set, so users
  re-grab *before* the next batch dies.
- 🟢 **Add helper**: ship a small **CurlWget helper** doc snippet or a
  helper userscript that auto-extracts the `Cookie:` header from
  CurlWget's clipboard output — Max Glenister's one-liner:
  `grep -oP "(?<=Cookie: )[^'\"]*" | tr -d '\n' > cookie.txt`.

---

## 2. URL discovery — two strategies in the wild

### Strategy A: Direct URL reconstruction (no browser scrape)

After one CurlWget capture, you have everything to build *every* URL:

```
https://takeout-download.usercontent.google.com/download/{filename}?j={J}&i={i}&user={USER_ID}&authuser=0
```

- `J` — job id, **shared** across all files in the export
- `i` — zero-based file index
- `user` — numeric Google user id
- `authuser=0` — always `0` for personal accounts
- `filename` — `{BASE_NAME}-001.zip` … `{BASE_NAME}-NNN.zip`

This is what `extract_url_parts()` in `takeout.py` already does. ✅

### Strategy B: DOM scrape the Takeout page

```
document.querySelectorAll('a.WpHeLc')   // class name obfuscated by Google
```

Albertz's extension uses the `downloads` permission + DevTools scraping to
discover every download button on `takeout.google.com/settings/takeout/*`,
then triggers Chrome's download API for each.

> "Google obfuscates its CSS class names, so `WpHeLc` will likely change
> at some point."

This is fragile but useful when you don't have a CurlWget capture.

**Apply to this repo:**
- ✅ The `takeout-extractor.user.js` helper already intercepts the Takeout
  page and POSTs the URL + cookie to the server. Good.
- 🟡 **Add**: optional *DOM-scrape fallback* in `takeout-extractor.user.js`
  that, if the user hasn't started a download yet, walks the page for
  download links and either auto-downloads them or populates a list to
  send. Mirrors `albertz`'s approach but stays opt-in.
- 🟢 **Document**: the URL-reconstruction approach in `README.md` /
  `USAGE.md` — once a user has captured one CurlWget, they can paste
  just *one* cURL and the script handles all N files without re-touching
  the browser. Currently buried in the existing flow.

---

## 3. Parallelism — the sweet spot

### yottabit42 (133★, the most-cited bash tool)

> "Typical home and business networks and machines can handle **between 3
> and 10 simultaneous downloads in parallel**."

Their tiered recommendation by bandwidth:

| Speed | Parallel |
|---|---|
| ≤100 Mbps | 2 |
| 101–300 Mbps | 3 |
| 301–500 Mbps | 4 |
| ≥501 Mbps | 6 |

### Max Glenister

Used a serial loop with `curl` (one at a time) because cookies only
last ~7 files. Parallelism amplifies cookie burn.

### GTR (Azure transloader)

> "Speeds of up to 350 MB/s or more (with more possible overall from
> parallelism) … downloads the file in chunks of 1000 MB."

Uses **serverless chunks in parallel**, not parallel files — a different
problem than ours.

### Fallenstedt (Go)

Worker pool sized to **`runtime.NumCPU()`**, channel-dispatched
(`processCh`, `resCh`, `errCh`).

### This repo

```python
DEFAULT_PARALLEL = 1
MAX_PARALLEL = 20
```

**Apply to this repo:**
- 🟡 **Change `DEFAULT_PARALLEL` to `4`** (was 1). 1 serial is the
  conservative mode for cookie-burn scenarios; 4 is the sweet spot from
  yottabit42. Make it tunable via env.
- 🟡 **Add**: `PARALLEL_RECOMMENDED` table in `USAGE.md` mirroring
  yottabit42's bandwidth tiers, plus the "lower if cookies expire fast"
  caveat from Max Glenister.

---

## 4. The `-x 1 -s 1` aria2c gotcha

CorpIT's definitive aria2c command:

```bash
aria2c -x 1 -s 1 -c --load-cookies=cookies.txt --file-allocation=none "URL"
```

- **`-x 1 -s 1`** — single connection per file, no splitting. **Google
  blocks multi-stream downloads.** Our existing
  `aria2c_integration.py` allows up to 16 connections-per-file — that's
  *wrong* for Takeout.
- **`-c`** — resume.
- **`--load-cookies=cookies.txt`** — Netscape-format cookies file.
- **`--file-allocation=none`** — don't preallocate, saves space on
  large archives.

**Apply to this repo:**
- 🟡 **Fix `aria2c_integration.py`**: pass `--split=1` and
  `--max-connection-per-server=1` (or `-s 1 -x 1`) **by default** for
  Takeout URLs. Optionally allow multi-stream for the *Drive API* path
  (which is what GTR exploits at 1 GB/s) but never for the Takeout
  redirect URLs.
- 🟡 **Document**: `README.md` should warn "Google blocks multi-stream —
  don't enable `split > 1` on Takeout URLs" with a link to CorpIT.
- 🟡 **Consider**: `file-allocation=none` by default in the manager so
  preallocation doesn't lie about progress on sparse files.

---

## 5. Resume & partial downloads — three patterns

### Pattern 1: HTTP Range resume (`.downloading` temp files)

What `takeout.py` already does. ✅ **Best practice in the industry.**

### Pattern 2: Skip-if-too-small heuristic (Max Glenister)

```bash
# Skip any file over 1 MB that already exists, so a mid-way cookie
# expiry just means refreshing it and running the script again.
if [[ -f "$output" ]] && [[ $(stat -c%s "$output") -gt 1048576 ]]; then
    echo "[$((i+1))/$TOTAL] $filename already exists, skipping"
    continue
fi
```

Plus an explicit cleanup of small HTML errors from failed cookies:

```bash
find "$DIR" -name "takeout-part-*.zip" -size -1M -delete 2>/dev/null || true
```

### Pattern 3: curl's `--continue-at -` + `--retry-all-errors`

```bash
curl \
    --retry 10 \
    --retry-delay 15 \
    --retry-all-errors \
    --continue-at - \
    --progress-bar \
    -o "$output" \
    "$url"
```

`--retry-all-errors` is **curl ≥ 7.71** — retries on any failure, not
just transient ones. Combined with `--retry 10 --retry-delay 15` this is
the most resilient single-file download pattern out there.

### Combine with cookie validation

Every existing tool validates by **file size** (skip if too small) or
**Content-Type** (HTML = auth failure). Our project does both. ✅

**Apply to this repo:**
- 🟢 **Add `MAX_RETRIES=10` (was 3)** as the default. Max Glenister's
  experience shows transient errors persist for many minutes on flaky
  links; 3 retries × backoff gives up too soon. Also bump
  `RETRY_BACKOFF_BASE` from 2.0 → 2.5 to spread retries further apart
  when cookies are nearing expiry.
- 🟡 **Add a "tiny file = auth-fail" cleanup pass** that runs once at
  startup (before the download loop) and removes any `< 1 MB` files
  matching the takeout filename pattern. Currently we delete them only
  after a failed download — Max Glenister's pattern catches stale junk
  from previous interrupted runs.
- 🟢 **Add `requests.Session` + `--retry-all-errors`-equivalent for
  Python**. `requests` doesn't expose `--retry-all-errors` natively; use
  `urllib3.util.Retry` with `retry_on_exception=set([...])` plus status
  codes 429/500/502/503/504 — covers the same set.

---

## 6. ZIP integrity — what's robust

### CorpIT's recommendation

> Google includes checksum files. Verify with:
> ```
> md5sum takeout-001.zip
> sha1sum takeout-001.zip
> ```
> Then compare with the checksum files in your Takeout folder.

### Our current check

```python
# ZIP end-of-central-directory verification
# (PK\x05\x06 in last 1024 bytes)
```

This catches *truncation* but not *bit-rot* in the middle. CorpIT's
md5/sha1 comparison is much stronger.

### yottabit42's approach

Verifies via `tar tzf` after `pigz`/`tar` extraction — assumes you
decompress to check.

### Pure-Python zipfile test

```python
import zipfile
try:
    with zipfile.ZipFile(p) as zf:
        bad = zf.testzip()   # returns first bad file or None
except zipfile.BadZipFile:
    bad = "<bad>"
```

This reads the **central directory** at the end and tries to open each
entry — much more thorough than a magic-bytes sniff.

**Apply to this repo:**
- 🟡 **Upgrade ZIP check**: keep the EOCD sniff as a fast pre-check,
  then on completion call `zipfile.ZipFile(...).testzip()` and treat
  any non-`None` result as a hard fail (re-download).
- 🟢 **Look for checksum sidecars**: after download, check for
  `*.zip.sha1` / `*.zip.md5` next to each archive and verify if
  present. This is what CorpIT does and it's free correctness.

---

## 7. Dedupe — what comes *after* download

### Our `dedupe_takeout.py`

Exists, walks an extracted tree, hashes, removes duplicates.

### yottabit42 — jdupes with hardlinks

```bash
jdupes -LNOr "${f}"        # -L hardlink, -N nofollow, -O order, -r recurse
```

Hardlinks make dedupe **zero-space** — every duplicate becomes a pointer
to one inode. Best practice on any filesystem that supports it (ext4,
btrfs, ZFS, APFS).

### othermore/google-photos-backup

> "Automatically eliminating 100% of duplicates both within the current
> backup batch and across the entire historical backup archive using
> zero-space hardlinks."

Same hardlink approach but **across** batches.

### Vipin PG — rclone sync

Incremental sync to cloud means unchanged archives never re-upload —
rclone's hash-based skip handles it.

**Apply to this repo:**
- ✅ `dedupe_takeout.py` exists.
- 🟡 **Document** `jdupes -L` as a faster alternative (hashes with
  parallel I/O, hardlink mode) in `dedupe_takeout.py` or `USAGE.md`.
- 🟡 **Add cross-batch dedupe option** to `dedupe_takeout.py`: a
  `--historical-dir <existing_backup>` flag that compares new hashes
  against the previous backup and hardlinks matches.
- 🟡 **Add `--verify-against-zips`** option: instead of decompressing,
  compare the new ZIP's hash against a previous ZIP and skip re-download
  if identical (useful for the recurring-export pattern).

---

## 8. Recurring / scheduled exports — automation gap

### Google's own scheduling

Takeout supports **monthly / every 2 months / every year** scheduled
exports delivered to Drive or emailed. So the user-facing workflow is:

1. Set up a recurring export in Google Takeout UI (once)
2. Script detects when the new archive is ready and downloads it

### Vipin PG's pipeline

- Cronicle scheduler on Proxmox host
- Bash script + rclone to Drive → NAS
- Incremental: `rclone sync` skips unchanged archives
- Hardlinks dedupe inside the staging dir

### Our project gap

We have a one-shot downloader. There's no "wait for new archive then
pull it" mode.

**Apply to this repo:**
- 🟢 **Add `takeout.py watch` subcommand** (or `takeout.py --watch`):
  poll `https://takeout.google.com/settings/takeout/downloads` (via
  the user's cookie) every N minutes, scrape for new "Ready" exports,
  and trigger the existing download path when one appears. Could also
  parse the Takeout API (`takeout_pa.exports.list`) if available.
- 🟢 **Add `takeout.py recurring`** convenience that does the above and
  re-prompts for a new cURL each session. Cron-friendly.

---

## 9. Browser-extension hardening — the lesson from albertz

### albertz manifest

```json
{
    "manifest_version": 2,
    "permissions": [
        "downloads",
        "storage",
        "https://takeout.google.com/settings/takeout/*",
        "https://accounts.google.com/signin/v2/challenge/pwd?..."
    ],
    "content_security_policy": "script-src 'self'; default-src 'self'",
    "background": {"scripts": ["bg.js"], "persistent": false},
    "content_scripts": [
        {"matches": ["https://takeout.google.com/settings/takeout/*"],
         "js": ["content.js"]}
    ]
}
```

### Our `helpers/manifest.json`

MV3 service-worker based — already modern. ✅

### Lessons

- **`persistent: false`** background — saves memory; we use MV3 service
  worker which is even better.
- **CSP `'self'` only** — prevents inline-script XSS. We should mirror
  this in any web-side code (Flask templates).
- **Narrow URL permissions** — only the takeout domain. Our extension
  already does this ✅.
- **No `tabs` permission** — not needed if you only inject into known
  URLs. Our extension already avoids this ✅.

**Apply to this repo:**
- 🟢 **Add CSP meta tag to `popup.html` / `options.html`** mirroring
  albertz: `<meta http-equiv="Content-Security-Policy"
  content="default-src 'self'">`. Defense in depth.
- 🟢 **Make the `serverUrl` confirmation prompt two-step** (URL bar
  preview → confirm) for any URL that isn't `localhost` / `127.0.0.1`.
  We already have the one-shot prompt; albertz doesn't show that the
  user is typing an arbitrary URL. Consider adding a host-validation
  step.

---

## 10. Worker pattern — Fallenstedt's Go template

Fallenstedt's `cmd/download.go` is the cleanest worker-pool pattern in
any open-source Takeout downloader:

```go
processCh := make(chan *drive.File)
resCh    := make(chan string)
errCh    := make(chan error)
doneCh   := make(chan struct{})

go func() {
    defer close(processCh)
    for _, driveFile := range r {
        processCh <- driveFile
    }
}()

for w := 1; w <= runtime.NumCPU(); w++ {
    wg.Add(1)
    go download.DownloadWorker(w, processCh, errCh, resCh, srv, cfg, &wg)
}
```

Key ideas:
- **Channels for jobs / results / errors / done** — clean shutdown.
- **Worker count = `runtime.NumCPU()`** — I/O-bound so could be higher,
  but CPU count is a reasonable default.
- **Producer goroutine closes the job channel** — workers exit cleanly
  when range over `processCh` drains.

### Our `TakeoutDownloader`

Uses `concurrent.futures.ThreadPoolExecutor` with `as_completed()`. Clean
enough. ✅

**Apply to this repo:**
- 🟢 **Add a graceful-shutdown channel pattern** in
  `TakeoutDownloader`: currently we use a `should_stop` flag; instead
  use a `threading.Event` so workers wake up immediately on stop
  instead of polling between downloads.
- 🟢 **Add a `--workers` flag** that defaults to
  `min(8, os.cpu_count() or 4)` — mirrors Fallenstedt's reasoning but
  caps at 8 because Google rate-limits above ~10.

---

## 11. URL parameter whack-a-mole

Every source confirms these query params on the final URL:

```
?j=<JOB_ID>&i=<INDEX>&user=<USER_ID>&authuser=0
```

- **`j` is per-export**, not per-file. Save it once.
- **`i` is zero-indexed**, sequential. `extract_url_parts()` in our
  `takeout.py` currently parses `file_num` from the filename, not `i`
  — but `i` and `file_num` are typically aligned. ✅
- **`authuser`** is always `0` for personal accounts; `1+` for Google
  Workspace multi-account users.
- **`user`** is the numeric ID — long-lived, no rotation.

**Apply to this repo:**
- 🟢 **Document**: add a section to `README.md` explaining the URL
  anatomy so users understand what they're pasting and why one cURL
  works for all files.

---

## 12. The official Data Portability API

Google has an official [Data Portability
API](https://developers.google.com/data-portability) (formerly Takeout
v1). Scope:

```
https://www.googleapis.com/auth/dataportability.takeout.download
```

Plus the reverse-engineered
[gist](https://gist.github.com/stewartmcgown/7f5dcbf4ccd385637786f9581b620e6a)
documents `takeout_pa.exports.*` (the v2 internal API).

### Pros

- **OAuth** — no cookie rotation; refresh tokens last indefinitely.
- **Server-side archive generation** — scriptable.
- **Status polling** — `exports.get` returns `state` + `urls`.

### Cons

- Each Google product requires **separate OAuth consent** per scope.
- Many users report the API is still flaky / partial — only some
  products are supported.
- Higher barrier to entry than "paste your cURL".

**Apply to this repo:**
- 🟢 **Long-term**: optional `takeout.py oauth` mode that uses the Data
  Portability API for users who don't want to deal with cURL cookies.
  Out of scope for now; would need a credentials flow + per-product
  scope request.
- 🟢 **Short-term**: in `README.md`, mention the API as the
  "future direction" and link to Google's docs.

---

## 13. Failure modes the docs gloss over

These are things users actually hit that no README warns about:

| Failure | Symptom | Fix |
|---|---|---|
| **Cookies tied to wrong domain** | HTML login page saved as `.zip` | Use CurlWget, not "Get cookies.txt LOCALLY" |
| **Cookies expired mid-export** | ~7 files succeed then HTML | Re-grab CurlWget every ~5 files (or use `--watch` mode once built) |
| **`.zip` actually HTML** | File is `<500 bytes` and starts with `<!DOCTYPE` | Delete + re-download with fresh cookie |
| **Multi-stream blocked** | aria2 hangs / 403 | `-x 1 -s 1` |
| **Filename collision on rerun** | `takeout-001.zip` already exists | We handle with `SizeHistory` ✅; albertz uses Chrome's `promptForDownload` toggle |
| **Network share permissions** | "Permission denied" on SMB | `USAGE.md` already mentions it ✅ |
| **Quota exceeded** | 429 from `takeout-download.usercontent.google.com` | Retry with backoff — covered by `MAX_RETRIES` ✅ but not 429-specific |
| **APP/2FA required mid-session** | Auth challenge URL in redirect | Re-grab cURL with the new cookies |
| **Export never finishes** | "Export in progress" indefinitely | Google's side; nothing to do |
| **Browser session times out** | Cookie invalid even with new capture | Re-login to Google in browser first |

**Apply to this repo:**
- 🟢 **Add a "Troubleshooting" matrix to `USAGE.md`** mirroring the
  table above. Most are one-liner fixes.
- 🟢 **Treat HTTP 429 as retryable**: add `429` to the
  `Retry-After`-aware retry set in the download loop. Many users hit
  this on large exports.

---

## 14. Feature gaps vs. competitors — ranked

| Feature | This repo | yottabit42 | Fallenstedt | GTR | Max Glenister | Albertz ext |
|---|---|---|---|---|---|---|
| Parallel downloads | ✅ | ✅ | ✅ | ✅ (chunks) | ❌ serial | ✅ |
| Resume partial | ✅ | ❌ | ❌ | n/a (transload) | ✅ `--continue-at -` | n/a |
| Cookie refresh mid-run | ✅ | ❌ | n/a (OAuth) | n/a | ✅ | n/a |
| ZIP integrity check | ✅ (EOCD) | ❌ | ❌ | n/a | ❌ | ❌ |
| aria2c backend | ✅ | ❌ | ❌ | ❌ (Azure) | ❌ | ❌ |
| Dedupe helper | ✅ | ✅ (jdupes) | ❌ | ❌ | ❌ | ❌ |
| Recurring export | ❌ | ❌ | ❌ | ✅ (Azure) | ❌ | ❌ |
| Drive-folder mode | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ |
| Browser extension | ✅ MV3 | ❌ | ❌ | ✅ MV3 | ❌ | ✅ MV2 |
| Web UI | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| TUI | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `tmux`/`screen` story | implicit | explicit | n/a | n/a | explicit (`tmux new -s takeout`) | n/a |
| Multi-stream warning | ❌ | ❌ | n/a | n/a | ❌ | n/a |
| Checksum sidecar verify | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

### Where we win

- **Multi-interface** (TUI + Web + standalone CLI + Docker + extension).
- **Resume + cookie refresh mid-session** — only us and Max Glenister.
- **ZIP integrity check** — only us.
- **aria2c backend** — only us.
- **Security hardening** (Basic Auth, CSP, path validation, cookie
  stripping from API) — best in class.

### Where we lose

- **Recurring scheduled exports** — GTR is unique here.
- **Drive-folder OAuth download** — Fallenstedt's whole product.
- **Multi-stream aria2 warnings** — silent footgun.
- **Checksum sidecar verify** — CorpIT and Google recommend it.
- **Cross-batch hardlink dedupe** — yottabit42 + othermore.

---

## 15. Recommended next steps (concrete, ordered)

In rough order of effort vs. payoff:

1. **🟢 30 min — fix aria2c `-s 1 -x 1` default** for Takeout URLs
   (Section 4). Prevents silent breakage.
2. **🟢 30 min — bump `MAX_RETRIES` to 10 and add 429 to retry set**
   (Section 5, Section 13). One-line wins.
3. **🟢 1 hr — stronger ZIP check using `zipfile.testzip()`** (Section
   6). Catches truncation that's currently silent.
4. **🟡 1 hr — "tiny file = auth-fail" pre-cleanup sweep** (Section 5).
   Removes stale junk from previous runs.
5. **🟡 1 hr — CSP meta tags on extension popups** (Section 9). Defense
   in depth.
6. **🟡 2 hr — `DEFAULT_PARALLEL = 4` + bandwidth-tier doc table**
   (Section 3).
7. **🟡 3 hr — cookie-expiry warning hint** (Section 1). Better UX.
8. **🟡 4 hr — checksum sidecar verification** (Section 6). Best
   correctness.
9. **🟠 8 hr — `takeout.py watch` subcommand** (Section 8). Recurring
   export automation.
10. **🟠 8 hr — jdupes integration in `dedupe_takeout.py`** (Section 7).
    Faster, cross-batch dedupe.
11. **🔴 1+ day — Data Portability OAuth mode** (Section 12). Long-term
    replacement for cookie-paste workflow.

---

## 16. One-liner takeaways (TL;DR)

- **Use CurlWget, not Get cookies.txt LOCALLY.** Cookies must follow the
  redirect to `takeout-download.usercontent.google.com`.
- **`-x 1 -s 1` is mandatory for aria2c on Takeout URLs.** Google blocks
  multi-stream.
- **Cookies expire after ~5–7 files (~10–15 GB).** Plan a refresh.
- **Hardlink dedupe (jdupes) beats hash-then-delete** for repeat
  exports.
- **`--retry-all-errors --continue-at - --retry 10 --retry-delay 15`** is
  the most resilient curl recipe.
- **`zipfile.testzip()` is stronger than magic-byte sniffing.**
- **Default parallelism should be 4, not 1**, but cap at ~8 to avoid
  rate limits.
- **There's no good way to fully automate** Takeout beyond Google's own
  scheduled exports — every "fully automated" tool eventually needs a
  human to re-grab a cookie.
