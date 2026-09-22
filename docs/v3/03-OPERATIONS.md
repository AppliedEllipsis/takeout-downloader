# v3 autopilot — operations

How to run it, where it lives, what it needs, and the order in which the v2 cookie machinery gets
removed. Everything here was measured on the live server on **2026-09-19**; anything not measured is
marked **UNVERIFIED**.

Companion docs: `docs/v3/01-ARCHITECTURE.md` (why it works this way), `docs/v3/02-RUN-INTERFACE.md`
(the frozen contract), `.wiki/failure-modes*.md` (what goes wrong and what to do).

---

## 1. Verified state of the live system

Read directly from the running container and the live extension on 2026-09-19.

| Thing | Value | Note |
|---|---|---|
| Container | `takeout-webgui`, Up 6 weeks | `takeout-tunnel` also up |
| Chromium | `149.0.7827.155`, CDP on `127.0.0.1:9222` | **inside** the container, not on the host |
| Page targets | **2** | was **314** at the flood peak |
| Memory | 3.1 GiB / 7.6 GiB | was **0 → 4.7 GiB** while the flood ran |
| `local.autoRecapture` | **`false`** | the tab-flood fix; now also the code default |
| `local.autoCancelDownloads` | **`false`** | flipped 2026-09-22 by owner directive — the workflow depends on real browser downloads. A mint therefore leaves a real part-sized download in `/config/Downloads` (on `cache_crypt`, 300 GB; not the 14 GB root disk). Reverse with `.recon/_cancel_off.py` + one edit. |
| `chrome.storage.managed` | **empty `[]`** | proves the managed-policy `autoRecapture` was inert — nothing ever wrote it |
| v2 manager `:8080` | HTTP 200 | alive; **all jobs `complete`**, none parked in `needs_cookie` |
| webgui `:3000` | HTTP 200 | |
| Archive mount | `/opt/archives` (rclone) | writable; VFS cache empty |
| Python in container | 3.13.5, `websockets` 16.0, `aiohttp` | **no `websocket-client`**, **no `pytest`** |

The extension is loaded with `--load-extension=/work/helpers`, and `/work` is the **main checkout**
(`/opt/local_cache_crypt/_projects/takeout-downloader`). **So the live extension is the main checkout's
copy, not the worktree's.** Two files can exist with the same name and only one is live — see §6.

---

## 2. Running v3

```bash
ARCHIVE=<the archive id from the manage page>

ssh takeout-server "docker exec -w /work/.v3 \
    -e PYTHONDONTWRITEBYTECODE=1 takeout-webgui \
    python3 -B -m autopilot run \
      --archive-id $ARCHIVE \
      --ledger   /config/v3-selftest/state.db \
      --staging  /config/v3-selftest/staging \
      --archive  /opt/archives/<account>/<archive-id> \
      --account  <account>"
```

**Exit codes** — branch on these, do not parse the report:

| Code | Status | Meaning | Resumable? |
|---|---|---|---|
| 0 | `complete` | every part verified on disk and moved | — |
| 2 | `needs_reauth` | a navigation landed on a sign-in page | yes, after a human authorises |
| 3 | `expired_unrecoverable` | the export's window closed; bytes are gone server-side | **no** — a new export is the only remedy |
| 4 | `quota_exceeded` | Google's per-export download allowance is spent | **no** — a new export is the only remedy |
| 1 | other | see the report's `error:` line | varies |

Codes 3 and 4 are deliberately distinct from 1: **both are terminal**, and a caller that files them as
generic will retry forever against something that can never succeed. Runbooks §1.14 and §1.19.

The report is printed to stdout as markdown; `--json` emits the same as JSON.

**Cost model.** A mint is **Δ1**, a `Range` resume is **Δ0**, and a bounced request is **Δ0**. A fresh
full transfer was **never measured** — the report says so in its own attempts table, and that is
intentional rather than an oversight. See `measured-facts.md`.

⚠️ **Budget against `dl_counts`, never against the ledger's attempt count.** They are different
quantities and only Google's runs out. A run was observed printing `mint 0 | transfer 0` while Google's
counter went 3 → 5. Read the *"Number of times already downloaded: N"* text **before** an experiment.

---

## 3. Where it lives, and why

The live checkout `/work` is on `feat/internal-downloader` and must not be disturbed. v3 is deployed to
a **git worktree inside the repo root**:

```bash
ssh takeout-server "cd /opt/local_cache_crypt/_projects/takeout-downloader && \
  git fetch origin && \
  git worktree add --detach .v3 origin/feat/takeout-autopilot"
```

`.v3` is *inside* the repo, so it appears in the container as `/work/.v3` — **only the repo root is
bind-mounted**, so a worktree anywhere else is invisible to the container. To update:

```bash
ssh takeout-server "cd <repo> && git fetch origin && \
  git -C .v3 checkout --detach origin/feat/takeout-autopilot && git -C .v3 log --oneline -1"
```

---

## 4. Container-side requirements

- **`python3 -B` / `PYTHONDONTWRITEBYTECODE=1`, always.** The container runs as **root**, so any
  `__pycache__` it writes lands root-owned inside the bind mount and the host user cannot delete it.
- **cwd matters.** `autopilot.verify` resolves `takeout2` from the current working directory, so run
  with `-w /work/.v3`. A `PYTHONPATH`-only deploy would silently bind to the live `/work/takeout2` —
  identical today, not guaranteed later.
- **`pytest` is absent in the container.** The suite is a workstation suite:
  `/c/Users/User/anaconda3/python.exe -m pytest tests/v3 -q -p no:cacheprovider`.
- WebSocket support comes from `websockets` 16.0. `websocket-client` is **not** installed and is not
  needed; do not add a dependency for it.

---

## 5. The migration, in order

v3 does **not** need the cookie-capture machinery at all — it mints over CDP. So the v2 path is to be
deleted rather than repaired. Do it in this order; each step is independently safe.

**Step 1 — the spawner (DONE).** `autoRecapture` now defaults to `false` and all four read sites fail
closed. The alarm is created only on explicit opt-in. `tests/v3/test_extension_defaults.py` pins this.
This is a **latent** fix: the live storage was already `false`, so nothing visibly changes — it closes
the hole that a **profile reset** would otherwise reopen.

**Step 2 — run v3 alongside v2 (NEXT).** Point v3 at a real archive, confirm a `COMPLETE`, and confirm
the report's file lands under its real `takeout-*.zip` name — not `download`. Nothing is removed yet.

**Step 3 — stop the manager driving jobs.** The manager's spawner is
`manager/app.py:246` (`pending = [j for j in orch.list_jobs() if j["status"] == J.NEEDS_COOKIE]`) feeding
`:222` / `:444-456`. Until it is removed, any job that reaches `NEEDS_COOKIE` re-arms the tab flood
whenever `autoRecapture` is turned back on. **Disable before deleting.**

**Step 4 — delete the cookie-replay path.** Scope measured 2026-09-19:

| Area | What |
|---|---|
| `takeout2/cookie.py` | whole module (146 lines): `LiveCookieJar`, `CookieError`, `CookieState` |
| `takeout2/engine.py` | `:39` import, `:179` `cookie_source` parameter |
| `takeout2/cli.py` | `:228-229`, `:804-807`, `:1178-1179` — three `LiveCookieJar` constructions |
| `manager/v2integration.py` | `:70`, `:99` — builds a fresh jar per burst |
| `manager/engine_bridge.py` | `:84-87` — flips a job to `NEEDS_COOKIE` and waits |
| `manager/app.py`, `manager/diagnose.py` | `:222`, `:246`, `:51`, `:61` — the park and its diagnostics |
| `helpers/background.js` | the capture listener, the POST-to-manager path, `triggerRecapture` |
| `helpers/content.js` | `:999` the `recaptureDownload` handler |
| `helpers/overlay.js`, `popup.js`, `options.js` | `needs_cookie` UI states and the checkbox |
| `tests/v2/test_cookie.py` | the tests for the thing being deleted |

**Step 5 — retire rather than rewrite the runbooks.** Every `needs_cookie` runbook step becomes obsolete;
mark them superseded instead of silently deleting, so anyone following an old copy learns why.

---

## 6. Gotchas that have already cost time

- **`.recon/` does not exist on the server.** It is untracked scratch on the workstation; the server has
  its own clone. Pipe probes in:
  `ssh takeout-server 'docker exec -i -w /work/.v3 takeout-webgui python3 -B -' < probe.py`
- **CDP is inside the container.** `127.0.0.1:9222` from the workstation is nothing at all — the failure
  is silent-empty rather than an error. Always wrap in the ssh + `docker exec`.
- **Two copies of every tracked file exist.** The main checkout and the `.v3` worktree have separate
  working trees, and the branches have diverged. Editing the wrong copy produces a change that is
  invisible to both the test suite and production. **The live extension is the main checkout's copy**
  (`--load-extension=/work/helpers`); the test suite reads the worktree's.
- **`/manage` truncates its export list.** A 4,000-character read showed 4 requests where 6 existed.
  Never conclude "only N exist" from a page dump.
- **The Takeout UI is an SPA that changes no URL per step**, so verify each step by reading the DOM, not
  the address bar. A synthetic click can also fire with no visible effect — always re-read state after.
- **A 5-attempt export is exhausted by roughly five probes.** Read `dl_counts` before, not after.
