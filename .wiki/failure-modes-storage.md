# Failure modes — storage (1.7, 1.8, 1.16)

Scope: the three storage-layer failure modes of `docs/v2/04-FAILURE-MODES-AND-RECOVERY.md` — root-disk
exhaustion, FUSE stalls, and mid-write `ENOSPC` — written against the **measured** mounts in
[infrastructure.md](infrastructure.md) and [measured-facts.md](measured-facts.md), with the two v2
reusable mechanisms (`takeout2/preflight.py`, `takeout2/cachewatch.py`) called out for v3 to inherit.

Resolve these once per session before running any recovery command (paths verified in
`infrastructure.md:33-44`):

```bash
export JOBDIR=/opt/local_cache_crypt/_projects/takeout-downloader     # is /work /config inside the container
export TAKEOUT_ROOT=/opt/archives/google-takeout                      # rclone FUSE mount
export CU=/config/.chrome-profile                                     # container-only path
```

---

## 1.7 — Disk full on `/`

**Symptom.** A running download dies with `OSError: [Errno 28] No space left on device`; the part is
left with a partial byte count and `verify.py` reports `PARTIAL`. The box may become unresponsive —
this is the failure that has already taken the server down once (`docs/v2/04-FAILURE-MODES-AND-RECOVERY.md:118`,
"has filled up before (a 22 GB `juicefs.log`)").

**Root cause.** `/` is `/dev/sda1`, ext4, **75 G with only 13 G free (84 % used)**
(`.wiki/infrastructure.md:15`). Nothing about the download *should* land there — staging is
`/opt/local_cache_crypt` (LUKS+xfs, 300 G, 293 G free, `infrastructure.md:16`) and the destination is
the FUSE mount (`infrastructure.md:35`). It lands there anyway via three routes:

1. **Detached mount.** When rclone dies, the kernel does not fail `/opt/archives` — it makes it an
   ordinary empty directory **on the root filesystem**. The engine sees an empty parts dir, concludes
   every part is missing and streams 10 GB onto `/`. This is the incident `takeout2/preflight.py:7-22`
   was written from.
2. **Unbounded logs.** `juicefs.log` reached 22 GB before (`04-…md:118`).
3. **Wrong staging root.** `STORAGE_ROOT=/opt` shadows the container's `/opt/manager-venv`, exit 127
   (`04-…md:118` region; see also `infrastructure.md:50`).

**Detection.**

```bash
# free space on every filesystem that matters (host)
ssh takeout-server 'df -h / /opt/local_cache_crypt; echo ---; df -h /opt/archives'
# is the destination actually a mount, or a root-fs directory wearing its name?
ssh takeout-server 'findmnt -no TARGET,FSTYPE,SOURCE /opt/archives'
```
Expected healthy: `/` with ≥ 20 GiB free; `/opt/archives` reporting `fuse.rclone`. If `findmnt`
returns nothing for `/opt/archives`, **the mount is detached — every subsequent write goes to `/`.**

The v2 preflight encodes exactly this signal — a mount failure **short-circuits** and deliberately
does *not* probe free space, because a detached mountpoint reports the root disk's (sufficient) free
space and would green-light the write (`preflight.py:331-353`). The engine logs
`"mount check failed; free space deliberately NOT probed"` (`preflight.py:352`) and emits
`ReasonCode.DISK_ERROR` (`takeout2/contracts.py:121`).

**Recovery.** Free space first; do not restart the download until the mount is verified.

```bash
# 1. [FREE] find what is actually eating / (top 20 by size, root fs only)
ssh takeout-server 'du -x -h --max-depth=2 / 2>/dev/null | sort -h | tail -20'
# 2. [FREE] act on WHAT STEP 1 FOUND — check the known suspect first, but do not
#    assume it. `juicefs.log` reached 22 GB in a documented past incident
#    (04-…md:118), which is why it is the first thing to look at. It is only
#    ~17 KB right now (measured 2026-09-19), so on this occasion it is NOT the
#    cause and truncating it would change nothing. Always size it before acting:
ssh takeout-server 'ls -la /var/log/juicefs.log'
#    then, only if it is genuinely large, truncate rather than rm — truncate keeps
#    the inode and any open fd valid, so a live writer keeps working:
#      ssh takeout-server 'truncate -s 0 /var/log/juicefs.log'
# 3. [FREE] confirm headroom recovered
ssh takeout-server 'df -h /'
# 4. [FREE] re-assert the storage mount before writing again
ssh takeout-server 'findmnt -no TARGET,FSTYPE /opt/archives && ls /opt/archives/google-takeout | head'
```

If `/opt/archives` is detached, remount rclone (config is password-encrypted, `measured-facts.md:152`)
**before** any resume. The exact rclone invocation and its pass **UNVERIFIED** — it lives in the host
systemd unit / `rclone.conf`; recover it rather than guessing:

```bash
# [FREE] find the authoritative rclone invocation
ssh takeout-server 'systemctl cat rclone* 2>/dev/null; pgrep -a rclone'
```

**Never** delete anything under `$TAKEOUT_ROOT` larger than 0 bytes without human sign-off
(`04-…md` §0.6 rule 1).

**Verification.** `df -h /` shows ≥ 20 GiB free (the `DEFAULT_MIN_HEADROOM` floor, `preflight.py:56`),
the mount type reads `fuse.rclone`, and a fresh `preflight_write` returns `ok` with
`checks["free_space_checked"] == true`.

**Attempt cost — 0 directly; 1/part for streams it kills.** Killing a stream costs nothing by itself;
restarting the part needs a **mint**, measured at **Δ1** (`measured-facts.md`, "a MINT costs exactly
Δ1"). A cached minted URL resumed by `Range` is measured **Δ0**, which is the only way this stays
free.

**What v3 must change.** `require_mount` must default **True** in production and be overridden only
for local dev — v2 ships it as `False` (`takeout2/engine.py:103`), which disables the exact guard that
prevents the original outage. Stage exclusively on the LUKS volume, assert a sentinel file inside the
destination (catches a mounted-but-hung/empty FUSE mount, `preflight.py:262-274`), and cap every log
file by size.

---

## 1.8 — JuiceFS / FUSE stall

**Symptom.** The transfer is *silent*. No bytes move, no error, no retry — the stream simply wedges.
An `ls` or `stat` on the FUSE path blocks for minutes or forever. The historical signature is the
**stat storm**: a per-part `stat()` pre-pass over **58 parts on the FUSE mount took ~6 minutes**
(`04-…md:313-318`), three to six times the cookie's ~1–2 min idle lifetime, so the cookie was always
dead before the work started — a livelock in which every participant behaves correctly.

**Root cause.** On a network-backed FUSE mount, metadata operations are round-trips. A per-part
`stat()` loop serialises them (`04-…md:313`), and a stalled FUSE request is **uninterruptible** — the
process sits in `D` state and the engine's socket read timeout never fires because the connection is
not the thing that is stuck.

⚠️ **Which mount this is, is currently contradictory.** v2's environment table calls the download
target a *JuiceFS* FUSE mount of 1.12 PB (`04-…md:119`). The **measured** 2026-09-19 state says
`/opt/archives` is **`fuse.rclone`** (`archive_chunked:`) and that the three JuiceFS mounts
(`/opt/storage.jfs002/003/004`) are **historical staging volumes, bind-mounted read-only**
(`infrastructure.md:35-36`). The procedure below is written for *whatever* FUSE mount is the active
data path; whether JuiceFS is still written to at all is **UNVERIFIED**.

**Detection.**

```bash
# 1. is anything actually blocked in the filesystem? (D state = uninterruptible sleep)
ssh takeout-server 'ps -eo pid,stat,wchan:24,comm | awk "\$2 ~ /D/"'
# 2. does the mount answer AT ALL, bounded? (never run an unbounded ls here)
ssh takeout-server 'timeout 5 ls /opt/archives/google-takeout >/dev/null; echo "exit=$?"'
# 3. what type is it, and where is it backed?
ssh takeout-server 'findmnt -no TARGET,FSTYPE,SOURCE /opt/archives'
# 4. recent FUSE/rclone errors
ssh takeout-server 'dmesg -T | grep -iE "fuse|juicefs|rclone" | tail -20'
```
Exit `124` from step 2 is the stall. `0` means the mount answers and the problem is elsewhere.

**Recovery.** Abort the wedged stream, do **not** force-unmount a live mount, then resume by `Range`.

```bash
# 1. [FREE] pause the engine so nothing refills the queue while you work
#    NOTE: archive_id is a PATH parameter, not a JSON body field. The route is
#    /control/{action}/{archive_id} (takeout2/api.py:325), mounted at /api/v2.
ssh takeout-server 'docker exec takeout-webgui curl -s -X POST \
  "127.0.0.1:8080/api/v2/control/pause/$ARCHIVE_ID"'
# 2. the engine's own watchdog is the right abort: stall_abort_s=180 with zero bytes
#    (takeout2/engine.py:90) then exactly ONE Range resume (stall_resume_attempts=1, engine.py:93).
#    If it did not fire, inspect rather than SIGKILL — a killed writer can leave a torn tail.
ssh takeout-server 'docker exec takeout-webgui ps -eo pid,stat,etime,cmd | grep -i python'
# 3. AVOID `umount -f` / `umount -l` on a mount with live writers: lazy unmount hides the
#    mountpoint from new callers while existing writes continue to a detached tree.
#    Prefer restarting the rclone/juicefs process; the mount then fails loudly instead of hanging.
ssh takeout-server 'systemctl restart rclone* 2>/dev/null || pgrep -a rclone'
# 4. [FREE] verify the mount answers within a bound before resuming
ssh takeout-server 'timeout 5 ls /opt/archives/google-takeout >/dev/null; echo "exit=$?"'
```

**Verification.** Step 4 exits `0`; no process remains in `D` state; a fresh part listing completes in
well under a second using **one `os.scandir()`** rather than a per-part `stat()` (`04-…md:379`), and
`aligned_resume_offset` (`engine.py:303`) rewinds by `resume_rewind` so a torn tail is re-fetched
instead of appended to (`engine.py:96`).

**Attempt cost — 0 directly; 1/part for streams it kills.** The stall itself is free. The engine
resumes stall-killed parts with exactly one `Range` resume; a `Range` request is measured **Δ0**
(`measured-facts.md`, "`Range` requests against the file host cost **0**"), so a clean stall-resume
recovers for free. The **1/part** applies only when recovery re-mints instead of resuming — a mint is
**Δ1**.

**What v3 must change.** Treat every FUSE syscall as unbounded-latency: one `os.scandir()` per
listing and **never** a `stat()` per part; bound each filesystem operation with a watchdog that aborts
on *zero bytes for N seconds* rather than relying on socket timeouts; and **never put the ledger on a
FUSE mount** — the live `state.db` (plus `-wal`/`-shm`) currently sits on `/opt/archives`
(`infrastructure.md`, "The v2 ledger sits ON the rclone FUSE mount"). It belongs on the LUKS volume.

---

## 1.16 — `ENOSPC` mid-write

**Symptom.** Two shapes, and the second is the dangerous one:

1. **Loud:** `OSError: [Errno 28] No space left on device` raised mid-stream after N GiB were written;
   the part is left `PARTIAL`.
2. **Silent:** the write **blocks indefinitely** with no error at all. There is no `ENOSPC` to catch
   and no retry to make — the transfer simply wedges (`takeout2/cachewatch.py:14-17`).

Shape 2 is the rclone VFS write-back cache: with `--vfs-cache-mode full
--vfs-cache-max-size 100G --cache-dir /opt/local_cache_crypt/rclone_vfs`, every byte is written
locally first and uploaded afterwards. One job is **63 parts × 10 GB = 630 GB pushed through a 100 GB
cache** (`cachewatch.py:9-12`), so when the cache hits its cap, writes to the mount block on a mutex
the engine cannot see.

**Root cause.** Two independent budgets, both shared:

| Budget | Size | Shared by |
|---|---|---|
| `/opt/local_cache_crypt` (LUKS+xfs) | **300 G, 293 G free** | `/config` + `/work` + the rclone VFS cache — **one physical budget** (`infrastructure.md:34`, `:42-44`) |
| rclone VFS cache cap | **100 G** (`--vfs-cache-max-size`) | the whole archive transfer in flight |

A plain `statvfs` on the filesystem tells you **nothing** about the wall you hit, because the cache
disk has room to spare (`cachewatch.py:19-20`). The wall is rclone's own accounting.

**Detection.**

```bash
# 1. real free space on the shared LUKS volume (host path — /config is container-only)
ssh takeout-server 'df -h /opt/local_cache_crypt'
# 2. the number that actually matters: bytes rclone has staged but not yet uploaded
ssh takeout-server 'du -sh /opt/local_cache_crypt/rclone_vfs'
# 3. if the engine is running, its own classification (OK / WARN / PAUSE / UNKNOWN)
ssh takeout-server 'docker exec takeout-webgui ps aux | grep -i takeout2'
```
Thresholds are in `cachewatch.py:66-68`: **WARN at 70 %** (70 G), **PAUSE at 85 %** (85 G), resume only
after draining to **60 %** (60 G). A `PAUSE` logged with no error string is this failure, not a bug.

**Recovery.** Stop writing, let rclone drain, resume by `Range`.

```bash
# 1. [FREE] stop issuing writes — pause the engine, which releases the burst
#    archive_id is a PATH parameter (takeout2/api.py:325), not a JSON body field.
ssh takeout-server 'docker exec takeout-webgui curl -s -X POST \
  "127.0.0.1:8080/api/v2/control/pause/$ARCHIVE_ID"'
# 2. watch the cache drain; interval matters — do not busy-poll a FUSE tree
ssh takeout-server 'while :; do du -sh /opt/local_cache_crypt/rclone_vfs; sleep 60; done'
# 3. if it is NOT draining, the upload side is dead — check rclone, do not delete cache files
ssh takeout-server 'pgrep -a rclone; journalctl -u "rclone*" -n 40 --no-pager 2>/dev/null'
# 4. resume only below the 60% hysteresis floor in cachewatch.py:68
```

**Never** delete files under `/opt/local_cache_crypt/rclone_vfs` by hand: that cache is the only copy
of bytes the archive does not yet have, and one copy is all there is (`measured-facts.md:152`).

**Verification.** `du -sh` falls below ~60 G and stops falling; the engine logs `CacheState.OK`
(`cachewatch.py:64`); the part's `size_on_disk` increases again on the next read. Then run
`verify.py` — the part completed through a stall and its tail may be torn; `resume_rewind` rewinds a
`WRITE_CHUNK` before resuming (`engine.py:96`).

**Attempt cost — 0 directly; 1/part for streams it kills.** Backpressure and draining contact no
Google host. The engine stalls out at `stall_abort_s = 180` with zero bytes (`engine.py:90`) and is
allowed exactly one `Range` resume (`engine.py:93`). A resume is **Δ0**; a re-mint is **Δ1** — so the
recovery is free only while the minted URL is still cached and valid. Whether a minted URL survives a
VFS stall of arbitrary length is **UNVERIFIED**.

**What v3 must change.**

1. **Treat cache fullness as a liveness signal, not a disk-usage metric.** Reuse `cachewatch.py`:
   `PAUSE` at 85 % *before* the cap, with hysteresis to 60 % (`cachewatch.py:66-68`), and never busy
   poll the walk more than ~every 15 s with a bounded entry count (`DEFAULT_MAX_ENTRIES = 200_000`,
   `cachewatch.py:74`).
2. **`UNKNOWN` must never pause.** A missing/unmeasurable cache dir means "probably not an rclone
   mount"; blocking there would be a self-inflicted outage (`cachewatch.py:28-31`).
3. **Stage locally, verify, then move.** Download to the LUKS volume, run integrity verification
   there, and only then move to `/opt/archives` — this removes the 100 G cache cap from the download's
   critical path entirely (`decisions.md`, the v3 browser-native ADR's "full integrity verification on
   local staging, moving to `/opt/archives`").
4. **Budget the three LUKS consumers as one pool.** `/config`, `/work` and the VFS cache share
   293 G; the engine must subtract the other two before promising a part's worth of space.
