# Architecture and source map

**Updated 2026-10-13.** Supersedes the preliminary September map. Live health and new-export end-to-end readiness are being audited; do not infer them from this source map.

## Current design: v3 hybrid autopilot

Browser authentication and URL minting are separate from byte transfer. The real Chromium session navigates the Takeout redirector and captures the file-host URL. The Python HTTP transport uses a fresh browser cookie jar and HTTP Range to resume. Parts are staged locally, verified, and copied to archive storage. Cached URLs and progress persist in a local SQLite ledger.

```text
Takeout archive page
  -> CDP scrape (part indices and expected sizes)
  -> browser mint (interactive reauth may be required)
  -> persistent ledger (local encrypted disk)
  -> HTTP transfer (cookie jar, validator and resume)
  -> local staging -> validation -> archive mover -> completeness report
```

The legacy code remains for compatibility and reference. Do not launch a legacy downloader alongside v3 against the same browser/ledger/staging directory.

## File map

| Path | Purpose |
|---|---|
| `autopilot/__main__.py` | CLI dispatch, exit-status contract and production RunConfig defaults. |
| `autopilot/run.py` | Injectable one-export orchestration (`RunConfig`, `RunOutcome`, `run_once`, `run_once_sync`). |
| `autopilot/scrape.py` | Archive-page metadata, index/completeness checks and reauth-navigation helpers. |
| `autopilot/mint.py` | Capture file-host URLs through browser navigation; clean up automation-owned stray downloads. |
| `autopilot/cdp.py` | CDP session/event protocol abstraction. |
| `autopilot/ws_transport.py` | Real websocket adapter and advertised CDP endpoint discovery. |
| `autopilot/jar.py` | Pull browser cookies/user agent for authenticated HTTP transfer. |
| `autopilot/transport.py` | Resumable file-host byte transfer and remote-change detection. |
| `autopilot/ledger.py` | SQLite jobs, parts, attempts, naming aliases and local-filesystem checks. |
| `autopilot/verify.py` | Staged-file validation helpers. |
| `autopilot/mover.py` | Staging/destination preflight, one-pass archive indexing and bounded copy. |
| `autopilot/report.py` | Completeness, expiry and archive-presence reports. |
| `autopilot/errors.py` | Named operational errors including reauth and quota. |
| `tools/satisfy_login.py` | Single-attempt password challenge helper; secret file pointer, never a 2FA bypass. |
| `tools/extension_switch.py` | Inspect/change explicit extension switches. |
| `tools/sweep_downloads.py` | Browser-download cleanup tool; not part of source-account deletion. |
| `tools/repair_part_filenames.py` | Recover real part filenames using matching archive evidence. |
| `tools/xwd_to_png.py` | Convert virtual-desktop captures without ImageMagick. |
| `helpers/` | Chromium extension and content scripts; human-initiated downloads must remain untouched. |
| `webgui/` | Chromium/webtop launch and container bootstrap configuration. |
| `docker-compose.webgui.yml` | Browser desktop deployment and bind mounts. |
| `tests/v3/` | Network-free protocol, transfer, integrity, orchestration and CLI regression tests. |
| `docs/v3/` | Architecture, frozen run interface and operational/full-pull guides. |
| `.wiki/` | Maintained project map, decisions and measured operating evidence. |
| `.pi/workflows/unattended-archival.md` | Reliability audit/build/verification/deployment plan and resume boundaries. |
| `takeout2/`, `tests/v2/`, `docs/v2/` | Older burst-engine generation and its tests/design. |
| `manager/`, `helpers/`, `webgui/` | Historical manager/browser integration; some infrastructure is still shared. |
| `takeout_dl.py`, `takeout_cli.py`, `takeout.py`, `google_takeout_tui.py` | Legacy interactive download entry points. |
| `.recon/` in main checkout | Historical, untracked recon scripts/evidence. Not production orchestration. |

## Deployment and storage boundaries

- Workstation authoritative branch: `feat/takeout-autopilot` in `.claude/worktrees/takeout-autopilot`.
- Server host repo: `/opt/local_cache_crypt/_projects/takeout-downloader`; v3 checkout: `.v3`.
- Browser container: `takeout-webgui`; code appears at `/work/.v3`, persistent configuration at `/config`.
- Local staging and ledger live on the encrypted cache volume, **not** `/opt/archives` and not the small root disk.
- `/opt/archives` was observed as an rclone FUSE mount. A successful FUSE copy/readback can still be cache-local; remote durability needs separate evidence.
- Staging, browser downloads and rclone VFS cache share the same physical disk budget.
- Existing browser session and human downloads must be preserved during deploy. Verify running extension code, not just repository timestamps.

## Current audit gates

Fresh export discovery, supervision/restart, multi-account identity, retry/backoff, strict archive integrity, shared-disk headroom and remote-upload confirmation are audit subjects. The existing one-export `run` CLI is not by itself an unattended service. See the workflow plan and current journal for verified changes.
