# Full pull — the repeatable procedure

Run this end to end or step by step. Every command is copy-pasteable. Written 2026-09-21,
on the second attempt at a real pull, so it records what actually happened rather than
what was expected.

Companion: `03-OPERATIONS.md` (live state, exit codes, container requirements),
`02-RUN-INTERFACE.md` (the frozen contract).

---

## Why the order is what it is — measured, not assumed

**A minted URL outlives the ReAuth window.** Measured 2026-09-19:

```
minted at 18:04:18Z   ·   tested ~19:25   ·   HTTP 206   ·   first bytes PK\x03\x04
```

81 minutes later it still served real zip bytes. The ReAuth window is ~45 minutes. So:

- **Minting is what the window gates.** Each part needs a mint, and a mint needs a live window.
- **Transferring needs only the jar.** It does not expire with the window.

A 63-part export takes hours. If you mint per part as you go, you run out of window
part-way and the run stops with `needs_reauth`, unable to earn the later URLs — **the pull
cannot finish in one pass**. Hence: mint everything first, then transfer.

---

## Step 0 — reach the browser and sign in

The browser lives inside the container. Its desktop is exposed as a **KasmVNC portal on
port 3000**, published to the server's loopback only:

```bash
# on the workstation
ssh -N -L 3000:127.0.0.1:3000 takeout-server
# then open
http://localhost:3000
```

`3000 -> 3000/tcp` and `8080 -> 8080/tcp` are bound to `127.0.0.1` on the server, so the
forward is the only way in. Verified: the forwarded portal answers **HTTP 200** and serves
the KasmVNC viewer (PWA manifest, no `WWW-Authenticate` on `/`).

In the portal, sign in to Google. **Two different states look similar and mean different
things:**

| What you see | What it means |
|---|---|
| `accounts.google.com/v3/signin/accountchooser` | pick an account — **not usable yet** |
| `accounts.google.com/v3/signin/challenge/pwd` | the ReAuth challenge — satisfy it |
| `takeout.google.com/manage?rapt=…` | signed in **and** holding a token; minting will work |

> Only the last one is ready. A signed-in session that has never satisfied a challenge
> still serves **untokened** download links — the run will earn a token itself
> (`provoke_rapt`), but if that comes back with a challenge, the window has lapsed and
> you must satisfy it before minting.

---

## Step 1 — readiness, before spending anything

```bash
ssh takeout-server 'docker exec -i -w /work/.v3 \
  -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui python3 -B -' < .recon/_readiness.py
```

Confirms: signed in?, which archives are downloadable, remaining allowance, and that the
destination mount is up.

**Check Google's counter, not the ledger's.** They are different quantities and only
Google's runs out — an export allows **5 downloads**, and it refuses the 6th with
`quotaExceeded=true`. The archive page shows `Number of times already downloaded: N`.

```bash
# disk headroom (staging shares its volume with rclone's 100 GB VFS cache)
ssh takeout-server 'df -h /opt/local_cache_crypt | tail -1; mount | grep opt/archives'
```

Required: the archive is a **`fuse.rclone` mount**. If it is not, `run_once` refuses —
correctly, because a detached mount turns `/opt/archives` into an ordinary directory on a
14 GB root filesystem and a mover would write terabytes into it (failure mode 1.7).

---

## Step 2 — mint every URL while the window is fresh

```bash
ID=<archive id>;  ACCT=<account>
ssh takeout-server "docker exec -w /work/.v3 -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui \
  python3 -B -m autopilot run \
    --archive-id $ID --mint-only \
    --ledger  /config/v3-selftest/full.db \
    --staging /config/v3-selftest/full \
    --archive /opt/archives/google-takeout/$ACCT/$ID \
    --account $ACCT"
```

Expected: a report with `Verdict: INCOMPLETE`, and **exit code 1**.

> **Exit 1 here is success, not failure** — and this matters when scripting. `incomplete`
> means "work remains", which is true: URLs are earned, bytes are not moved. Only
> `complete` exits 0. So a chained `--mint-only && transfer` would never run the
> transfer; use `;` or `||true`, or check the report rather than the code.
> (Observed 2026-09-21 on the 5-part test export.)

`incomplete` is the CORRECT verdict at this step. Nothing may claim the pull is done.

Do this immediately after signing in. It is fast (Δ1 per part) and it is the only step
that needs the window.

---

## Step 3 — the transfer pass

```bash
ssh takeout-server "docker exec -w /work/.v3 -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui \
  python3 -B -m autopilot run \
    --archive-id $ID \
    --ledger  /config/v3-selftest/full.db \
    --staging /config/v3-selftest/full \
    --archive /opt/archives/google-takeout/$ACCT/$ID \
    --account $ACCT"
```

Hours, for a large export. Window-independent: it uses the cached URLs from step 2 and
**never mints**. Re-run until **exit 0**.

**Resuming is the normal case, not the exceptional one.** Every re-run continues:
completed parts are skipped, partial files are `Range`-resumed (Δ0), and the destination
index prevents re-fetching what is already archived. Ctrl-C and re-run freely.

Exit codes: `0` complete · `2` re-auth needed (re-do step 0, then re-run) · `3` export
expired · `4` allowance spent (new export required) · `1` other, read the `error:` line.

---

## Step 4 — confirm the result

```bash
ssh takeout-server "docker exec takeout-webgui sh -c \
  'ls -la /opt/archives/google-takeout/$ACCT/$ID | head -20'"
```

A part is done only when it is **on the archive**, named from the minted URL's basename
(`takeout-YYYYMMDDTHHMMSSZ-N-NNN.zip`) — never `download`.

---

## Gotchas, each of which has cost time here

- **The window is ~45 min and it lapses silently.** Work done hours earlier still reports
  `needs_reauth` correctly, with 0 attempts spent. Just sign in again.
- **`--mint-only` before any long transfer.** Without it a multi-hour pull is
  architecturally unable to finish (see the top of this file).
- **Never budget against the ledger's attempt count.** A run reported `mint 0 | transfer 0`
  while Google's counter went 3 → 5. Read `dl_counts`.
- **A 5-attempt export is exhausted by about five probes.** Read the counter *before* an
  experiment, not after.
- **Extension changes need a Chromium restart.** `chrome.runtime.reload()` reports success
  and does not take effect here (measured twice). `pkill -f chromium` inside the container
  and the s6 loop relaunches it within ~4 s; clear `SingletonLock`/`Cookie`/`Socket` if it
  does not.
- **`chrome.alarms` persist across browser restarts.** An alarm's presence does NOT mean old
  code is loaded — and it misled a reload check here until a behavioural test settled it.
- **`.recon/` does not exist on the server**; pipe probes in over stdin (`docker exec -i … -`).
- **CDP lives inside the container**, at `127.0.0.1:9222` there. It is published to the
  server's loopback too, but not to the workstation.
- **Only the repo root is bind-mounted** (`/work`), so the `.v3` worktree must live inside it.
- **Editing two copies is a trap.** The live extension is the **main checkout's** copy
  (`--load-extension=/work/helpers`); the test suite reads the **worktree's**.

## Known untidiness

- Two localhost-only listeners on the workstation (`13099`, `13100`) were started while
  testing the portal forward and could not be attributed to a process from the shell
  (`wmic`/`tasklist`/PowerShell cannot resolve the owning PID). They forward to the
  server's loopback portal and are not externally reachable. They die with the session; a
  reboot clears them. Do not blanket-kill `ssh.exe` — that takes out your Bitvise sessions.
