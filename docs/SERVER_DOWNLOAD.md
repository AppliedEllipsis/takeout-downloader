# Server-Side Takeout Download (`takeout_dl.py`)

The supported workflow for pulling a full Google Takeout export straight onto
the server (`/opt/storage.jfs002/google-takeout/...`). `takeout_dl.py` uses a
built-in threaded downloader (no `aria2c`); `takeout.py` / `takeout_cli.py` /
`google_takeout_tui.py` are kept only for reference.

---

## TL;DR

```bash
# 1. Open a SOCKS tunnel through the server (leave it open)
ssh -D 1080 -N ellipsis@188.245.169.166

# 2. Launch a proxied Chrome (separate profile) on your local machine
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --user-data-dir="$TEMP/takeout-proxy" \
  --proxy-server="socks5://127.0.0.1:1080" \
  --host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"

# 3. Confirm https://api.ipify.org shows 188.245.169.166

# 4. Capture the payload on takeout.google.com, "Copy ALL exports",
#    paste into /tmp/payload.json on the server.

# 5. Run on the server (tmux recommended so you can watch live)
tmux new -s takeout
python3 /tmp/takeout_dl.py \
  --payload /tmp/payload.json \
  --out /opt/storage.jfs002/google-takeout/braincreation \
  --parallel 4
```

---

## The core constraint: the cookie is IP-bound

Google ties the Takeout download session to **the IP address that initiated the
download**. The captured cookie only works from that same IP.

If you capture the payload in your home browser and then run the downloader on
the server, every request redirects to `accounts.google.com` and Google serves
a ~1.2 MB HTML sign-in page instead of the archive.

### Symptoms of an IP/auth mismatch
- `Auth check failed: Redirected to Google sign-in (accounts.google.com)`
- Range probe, full GET, and manage-page fetch all return `200 text/html` from
  `accounts.google.com`.
- A response body that starts with `<!doctype html>` instead of `PK\x03\x04`.

### Why pre-warming / manage-page hits don't help
The old `takeout_cli.py` tried fetching the manage page and a "pre-flight" full
GET before downloading. None of that establishes IP trust — if the session was
created from a different IP, every endpoint still redirects to sign-in. Verified
directly on the server with a fresh cookie.

---

## The fix: capture and download from the same IP (SOCKS)

1. **SSH dynamic tunnel** opens a SOCKS5 proxy on your local port 1080 that
   exits through the server:
   ```bash
   ssh -D 1080 -N ellipsis@188.245.169.166
   ```
   Leave it open. It looks like it's hanging — that's correct.

2. **Proxied Chrome** routes all browser traffic (and DNS) through the tunnel:
   ```bash
   "/c/Program Files/Google/Chrome/Application/chrome.exe" \
     --user-data-dir="$TEMP/takeout-proxy" \
     --proxy-server="socks5://127.0.0.1:1080" \
     --host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"
   ```

3. **Verify** `https://api.ipify.org` shows the server IP (`188.245.169.166`).
   If it shows your home IP, the proxy isn't active — stop and fix it.

4. **Capture** on `takeout.google.com`: start the download, click the extension,
   "Copy ALL exports", and write the JSON to `/tmp/payload.json` on the server.

---

## How the downloader avoids fake files

`takeout_dl.py` uses an internal threaded downloader. For every part it inspects
the **first bytes of the response before opening the output file**:

- Redirect to `accounts.google.com`, `text/html` content-type, HTTP 401/403, or
  a body starting with `<!doctype html>` / `<html` → it raises an auth challenge
  and **nothing is written to disk**.
- Only genuine archive bytes (`PK\x03\x04`) are streamed to the `.zip`.

This is the structural fix for the old aria2c problem: Google's sign-in page is
a clean `200` with a matching `Content-Length`, so aria2c happily saved it as a
1.2 MB "complete" `.zip`. The internal downloader can never do that.

---

## Running the download

### Basic (tmux, watch live)
```bash
tmux new -s takeout
python3 /tmp/takeout_dl.py \
  --payload /tmp/payload.json \
  --out /opt/storage.jfs002/google-takeout/braincreation \
  --parallel 4
# detach: Ctrl-b then d    reattach: tmux attach -t takeout
```

### Useful flags
| Flag | Effect |
|------|--------|
| `--parallel N` | Concurrent files. Default 4. Each file is single-stream; Takeout doesn't support segmented downloads. |
| `--max-exports N` | Only fetch the first N parts. Use `1` for a quick validation test. |
| `--payload PATH` | JSON payload file. Watched for fresh cookies on re-prompt. |
| `--out DIR` | Output directory (must be under an allowed prefix). |
| `--fresh` | Ignore saved state and re-discover. |
| `--no-validate` | Skip ZIP validation (not recommended for `.zip` archives). |

### Concurrency note
Default is **4 concurrent files, single-stream each**. More parallelism
increases the chance Google rate-caps the session and burns the cookie faster.

---

## Monitoring

```bash
# Script log
tail -f /opt/storage.jfs002/google-takeout/braincreation/takeout_dl.log

# Files landing + total size (in a second tmux pane)
watch -n5 'ls -la /opt/storage.jfs002/google-takeout/braincreation/*.zip | wc -l; \
  du -sh /opt/storage.jfs002/google-takeout/braincreation'
```

### Confirm a file is real (not HTML)
```bash
head -c 4 FILE.zip | xxd                          # must be: 504b 0304  (PK..)
head -c 200 FILE.zip | grep -ic 'doctype\|html'   # must be 0
```

---

## Cookie expiry and auto-resume

The session cookie lasts roughly **45 minutes** (about 4 x 10 GB parts). On a
multi-terabyte export you will hit sign-in redirects partway through. The script
handles this without restarting:

1. When a part gets the sign-in page, the downloader stops the pool and prints:
   ```
   Waiting for a fresh payload at /tmp/payload.json
   ```
   It then polls that file every 5 seconds.

2. In a second tmux pane, re-capture through the proxied Chrome and overwrite
   the payload:
   ```bash
   rm /tmp/payload.json && nano /tmp/payload.json
   # paste the fresh "Copy ALL exports" JSON, then Ctrl-O, Enter, Ctrl-X
   ```

3. The script detects the new cookie (different from the stale one), validates
   it, and **resumes automatically** from the partials on disk via HTTP Range.
   No restart, no lost progress. Completed files are skipped by size check.

Keep the SOCKS tunnel and proxied Chrome up the whole time so each fresh cookie
is bound to the server IP.

---

## Payload shapes

`takeout_dl.py` accepts two forms from the extension:

- **Single capture** — one captured download URL plus the cookie. Discovery
  sweeps `i=0,1,2,…` until two consecutive invalid responses.
- **Multi payload** (`multi: true` with an `exports` array) — the extension has
  already enumerated every part URL and size. The script trusts that list
  directly and does not re-probe. Output filenames are made unique from the URL
  basename + part index (e.g. `takeout-20260616T040104Z-12-006-part-000.zip`),
  because the extension's display filename is truncated and identical across all
  parts.

---

## Verified working

A 1-file test from the server IP (via the SOCKS capture) pulled **633 MB of real
archive data** with a `PK\x03\x04` header and no HTML — confirming the SOCKS
approach resolves the auth problem. The internal downloader's HTML-rejection and
Range-resume were verified with a local test harness (HTML response rejected and
never written; real archive resumed to full size on the next pass).
