# Architecture

> ⚠️ **PRELIMINARY — pending verification.** This file was scaffolded during
> reconnaissance before the source-grounded inventory completed. The authoritative
> inventory is being produced as `.recon/01-architecture.md` (main checkout) and
> **must be merged into this file** before this map is trusted.
>
> Treat everything below as a sketch with known gaps, not as the map.

## What the project does

Downloads **all** parts of a Google Takeout export on a self-hosted server, using a
Docker desktop environment with a browser to authenticate, then **validates** that the
download is complete.

## The three generations (all coexist in the repo)

| Gen | Mechanism | Key locations | Status |
|---|---|---|---|
| **TUI** (v6) | Clipboard-paste of a download URL; TUI drives the transfer | `google_takeout_tui.py`, `takeout_dl.py`, `takeout_cli.py` | Legacy |
| **v1 manager** | Webtop-hosted manager auto-POSTs a **replayed cookie** to a separate downloader | `manager/`, `helpers/` (browser extension), `webgui/` | Legacy, was in production |
| **v2 engine** | Ledger + burst engine; same cookie-replay premise, far more engineering | `takeout2/`, `docs/v2/` (8 normative docs) | Existing, ~382 network-free tests, **never ran end-to-end against Google** |
| **v3 autopilot** | **Chromium downloads the files itself**; extension = hands, daemon = brain | *(not yet written)* | **In design — this is the current work** |

## The shared root mistake

All three existing generations extract the Google cookie from the browser and replay
it from a separate HTTP client (`requests` / `aria2c` / `curl`). The cookie dies after
~1–2 minutes idle (302 → `accounts.google.com/ServiceLogin`), is IP-bound, and an
already-started stream keeps running for hours. **Every layer of automation inserted
latency between "fresh cookie" and "first byte"**, and each inserted step caused a
livelock in production.

## v2 module sketch *(unverified — confirm against `.recon/01-architecture.md`)*

| Module | Responsibility |
|---|---|
| `takeout2/state.py` (or equivalent) | SQLite ledger of jobs/parts/attempts |
| `takeout2/plan.py` | Builds the per-part plan from scraped page data |
| `takeout2/engine.py` | `run_burst` — the download orchestrator |
| `takeout2/cookie.py` | CDP cookie capture (the defect carrier) |
| `takeout2/verify.py` | Integrity checks (historically: zip header + EOCD only) |
| `takeout2/cachewatch.py` | Watches the cache/disk |
| `takeout2/preflight.py` | Mount/path preflight checks |
| `manager/` | FastAPI manager app + Telegram notifier |

## Browser / extension

| Piece | Where | Role |
|---|---|---|
| Chromium launcher | `webgui/chromium-service.sh` | Starts Chromium with `--user-data-dir=/config/.chrome-profile --remote-debugging-port=9222` |
| Container init | `webgui/init_custom.sh`, s6 supervision | Bootstraps the desktop environment |
| Managed policy | `webgui/profile-seed/managed-policy.json` | Chromium enterprise policy |
| Extension | `helpers/manifest.json`, `content.js`, `background.js` | Scrapes the Manage-exports page, triggers downloads — **and currently auto-cancels native Takeout downloads** (v3 must reverse this) |

## Data flow

```
                              ┌─ (v1/v2, to be retired) ─┐
Google Takeout Manage page ──►│ extension scrapes parts  │
                              └──────────┬───────────────┘
                                         │ part URLs + sizes
                                         ▼
                        ┌────────────────────────────────┐
                        │ cookie extracted via CDP (9222)│
                        └──────────┬─────────────────────┘
                                   │ replayed from the server
                                   ▼
              requests / aria2c / curl  ──►  staging  ──►  verify  ──►  /opt/archives
                                              (/config)     (size+PK+EOCD)   (rclone FUSE)

              ── v3 replaces everything above the staging line ──
              Chromium downloads directly ──► staging (/config, 293 GB)
                     ▲                                │
                     │ CDP/chrome.downloads           ▼
              extension (hands) ◄──► daemon (brain: ledger, budget, verify, move, report)
```

## Critical invariants

1. **Two good tabs must survive any tab cleanup:** `takeout.google.com/manage` and the webgui page.
2. **Stage on `/config` (293 GB, LUKS `cache_crypt`)** — never on `/` (13 GB free).
3. **`/opt/archives` is remote-backed rclone FUSE** — treat as high-latency network storage; avoid `stat`-heavy patterns (the historical livelock).
4. **Memory is the binding constraint** (7 GB total, 0 available at probe time). Budget it explicitly.
5. **The browser is long-lived and stateful** (43 days at probe). Don't assume a restart is free.

## Known-unknowns to resolve before implementation

- The exact **spawner** of one tab per tick (root cause of the 314-tab backlog).
- Which of the 8 alleged v2 defects are real (adversarial verification in progress).
- Google's **"5 downloads per archive"** accounting — never measured in this project;
  it is the central unknown of the whole design.
- Whether a Chromium restart preserves Google auth.

## File map

*(Intentionally omitted until `.recon/01-architecture.md` lands. A file map built from
guesswork is worse than none.)*
