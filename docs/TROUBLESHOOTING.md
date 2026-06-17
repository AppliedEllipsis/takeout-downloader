# Troubleshooting `takeout_dl.py`

Symptom-first guide. Most failures are one root cause: the cookie does not match
the IP doing the download.

---

## "Auth check failed: Redirected to Google sign-in"

**Cause:** the cookie was captured from a different IP than the one running the
download. Google binds the Takeout session to the originating IP.

**Fix:** capture through a SOCKS tunnel so your browser uses the server IP. See
`docs/SERVER_DOWNLOAD.md` → "The fix: capture and download from the same IP".

Quick check that the cookie is IP-bound rather than malformed:
```bash
# From the SERVER, a real cookie returns 200 + content-disposition + binary.
# An IP-mismatched cookie returns 200 text/html from accounts.google.com.
```

---

## Files download but are tiny / 0-byte / won't unzip

**Cause:** Google served the HTML sign-in page and it got saved as `.zip`.

**Check:**
```bash
head -c 4 FILE.zip | xxd                          # real ZIP: 504b 0304 (PK..)
head -c 200 FILE.zip | grep -ic 'doctype\|html'   # HTML page: >0
```

**Fix:** re-capture a valid cookie (SOCKS), delete the HTML files, re-run. The
script's default validation (size + ZIP signature) catches this; never run with
validation disabled for `.zip` archives.

---

## "All done … downloaded and verified" but archives are fake

**Cause (historical):** running with `--no-validate` treated 0-byte placeholders
and HTML-as-ZIP files as complete.

**Fix:** keep validation on (the default). Validation checks both the on-disk
size against the expected size and the ZIP EOCD/`PK` signature.

---

## Every part overwrites the same filename

**Cause (historical):** the extension's display filename is truncated and
identical for all 290 parts (e.g. `…20260616T040104Z-12-005.zip`).

**Fix:** already handled. For multi payloads the script builds unique names from
the URL basename + part index (`…-part-000.zip`, `…-part-001.zip`, …) and
de-duplicates collisions.

---

## Download hangs on a "pre-flight" request

**Cause (historical, `takeout_cli.py`):** a blocking full-GET pre-flight with no
feedback before downloads start.

**Fix:** `takeout_dl.py` validates per-file as it streams; there is no blocking
pre-flight. If a run appears stuck, check `takeout_dl.log` for the per-file
heartbeat and `du -sh` the output dir to confirm bytes are landing.

---

## Cookie dies partway through a large export

Expected — the session lasts ~45 minutes. The script does NOT exit: it prints
`Waiting for a fresh payload at /tmp/payload.json` and polls that file every 5s.
Re-capture through the proxied Chrome (SOCKS), then overwrite the payload:

```bash
rm /tmp/payload.json && nano /tmp/payload.json   # paste fresh "Copy ALL exports" JSON
```

Within 5s the running process detects the new cookie (it must differ from the
stale one), validates it, and resumes from the partials on disk. No restart, no
lost progress. Completed files are skipped via an on-disk size check.

---

## Stale 0-byte / HTML files from old runs

The internal downloader never writes HTML to disk, so new runs won't create
poison files. If you have leftovers from an older aria2c-based run, purge them
by signature (keeps only real ZIPs):
```bash
cd /opt/storage.jfs002/google-takeout/braincreation
for f in *.zip; do
  [ "$(head -c4 "$f" | xxd -p)" = "504b0304" ] || rm -f "$f"
done
```
Keep valid partials — they resume from on-disk bytes via HTTP Range.

---

## JSON paste errors

- `Invalid payload: JSON appears incomplete` — on a non-TTY run, pass
  `--payload /path/to/file` instead of relying on the interactive prompt.
- `Payload missing url or cookie` — the server copy of the script was older than
  the multi-payload-aware version, or the payload lacks both a top-level `url`
  and an `exports` array. Re-upload the latest `takeout_dl.py` and re-capture.

---

## Killing a stuck run on the server

```bash
pkill -f takeout_dl.py
# verify:
pgrep -af takeout_dl || echo ALL_CLEAR
# if anything lingers, kill by PID:
kill -9 <PID>
```
The internal downloader has no separate daemon — killing the Python process
stops everything. Partials on disk are kept and resume on the next run.
