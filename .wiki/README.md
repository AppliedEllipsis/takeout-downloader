# Takeout downloader project wiki

**Last updated:** 2026-10-13. Active code: branch `feat/takeout-autopilot`, workstation `.claude/worktrees/takeout-autopilot`; server deployment `.v3`.

## Start here

This project archives all parts of Google Takeout exports on a remote server. The current v3 implementation is **hybrid**: the authenticated browser mints a file-host URL, then the HTTP transport downloads/resumes to local encrypted staging and the mover copies verified parts to archival storage. Source-account deletion is not a downloader operation.

The September investigation established this design and implemented `autopilot/`. Earlier wiki claims that v3 does not exist or that browser-native downloads are the chosen transport are obsolete. Current reliability audit and deployment work is recorded in `.pi/workflows/unattended-archival.md`. Historical measurements are dated evidence, not a statement of current server health.

| File | Purpose |
|---|---|
| [architecture.md](architecture.md) | Current source map, hybrid data flow and safety boundaries. |
| [reliability-audit.md](reliability-audit.md) | Confirmed blockers, baseline tests, project comparisons and release gates. |
| [decisions.md](decisions.md) | Append-only architecture/security decisions; later entries supersede earlier ones. |
| [runbooks.md](runbooks.md) | Server inspection, tests, authentication, extension deployment and recovery. |
| [infrastructure.md](infrastructure.md) | Server/container/mount map; dated measurements require live rechecking. |
| [credentials.md](credentials.md) | Credential locations and environment names only; never values. |
| [measured-facts.md](measured-facts.md) | September primary evidence for auth, minting, HTTP Range and browser behavior; read retractions. |
| [archival-inventory.md](archival-inventory.md) | Historical archive integrity/coverage inventory; not authorization to delete data. |
| [failure-modes.md](failure-modes.md) | Failure-mode index and tab, quota and memory recovery. |
| [failure-modes-storage.md](failure-modes-storage.md) | Disk exhaustion, FUSE stalls and write failures. |
| [failure-modes-exports.md](failure-modes-exports.md) | Expiry, corrupt/truncated parts and browser locks. |
| [failure-modes-runtime.md](failure-modes-runtime.md) | Tunnels, token mismatches and reboot recovery. |
| [journal.md](journal.md) | Dated changes, exact executed commands and their outcomes. |

## Evidence boundaries

- An offline test pass proves the tested mechanisms, not a new Google export download.
- A file of the expected size is not by itself proof of integrity, product coverage or durable remote upload.
- A completed export is a snapshot, not a backup of subsequent account changes.
- Google may require interactive reauthentication or a second factor. Automation must stop/back off and expose that state, not retry passwords or claim it bypasses authentication.
- Expired or Google-failed exports require new exports; already archived parts remain valuable.
