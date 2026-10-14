# v3 autopilot — current reliability documentation

**Updated 2026-10-13. Owner deferred activation, rclone unlock and Google sign-in.**
No real Google mint/download or password attempt. No live backend canary; pending
and ready export counts are unknown. No new port or browser/container restart.

| Page | Purpose |
|---|---|
| [01-ARCHITECTURE.md](01-ARCHITECTURE.md) | September design history; current execution/receipt rules below supersede stale details |
| [02-RUN-INTERFACE.md](02-RUN-INTERFACE.md) | Current one-export API/CLI, remote options and exit codes |
| [03-OPERATIONS.md](03-OPERATIONS.md) | Runtime paths, permissions, checks and stop/recovery |
| [04-FULL-PULL.md](04-FULL-PULL.md) | Future owner-gated full pull; not an executed release procedure |
| [05-WATCH-INTERFACE.md](05-WATCH-INTERFACE.md) | Watch config, identity, scheduling and status contract |
| [Wiki automation](../../.wiki/automation.md) | Authoritative host/container execution map and activation boundaries |
| [Reliability evidence](../../.wiki/reliability-audit.md) | Measured reports and unresolved final gates |
| [October journal](../../.wiki/journal-2026-10.md) | Dated source move/evidence/deferred work |

## Current design

1. **Browser mints; HTTP transporter moves.** Browser cookies/user agent support
   file-host transfer, not bypass of interactive Google authentication.
2. **Cache valid URLs; do not remint by age.** Mint only with tracked automation
   ownership; preserve human tabs/downloads. Authentication is a visible pause.
3. **Stage/ledger/state on encrypted local disk.** ZIP members get full CRC reads;
   loose known-size files have explicitly weaker evidence. Computing a digest is
   not comparing to a trusted source digest.
4. **Durable mode uploads directly through rclone.** Exact backend size and forced
   SHA256 comparison gate receipt/done/cleanup; FUSE fsync/size is insufficient.
5. **Watcher schedules bounded fair work.** Strict account identity, pending poll,
   OS lock, atomic sanitized status and backoff; no export creation/source deletion.
6. **Host supervises inside-container non-root worker.** `tools/container_watch.py`,
   foreground docker exec, existing manager venv and container-local CDP; mandatory
   backend unlock preflight. Unit remains disabled/inactive.

## Source and evidence boundary

Canonical source: `C:/Users/User/_projects/takeout-downloader-reliability`, branch
`feat/takeout-autopilot`. D original is unchanged (disk full). `1f7f9af` is already
pushed **docs-only**, not the implemented release. Migrated baseline was
971 passed / 6 skipped / 2 fixture failures; parent reports repaired focused
fixtures 38 passed. Final actual-checkout whole-suite gate is not known here.
Do not predict totals or infer deployment from offline passes. Earlier September
measurements/ADRs remain dated history, including attempt-cost retractions.

## Protected environment and release boundary

Mandatory unit `EnvironmentFile` is host-only
`/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env` (`root:root 0600`),
under `root:root 0700`, outside worker-writable PRIVATE and browser `/work`,
`/config` binds. A worker-writable parent permits replacement and root environment
injection even when the file is root-owned. PRIVATE remains host
`/opt/local_cache_crypt/_projects/takeout-downloader/config/autopilot` →
`/config/autopilot`, owner `20001:20001`, mode `0700`, no group/other bits;
ancestor traversal is separate. No key/credential file was provided or created.

[CORE21495 is deployed](../../.wiki/core-release.md), not activated. Current unit
uses `Restart=on-failure`, `RestartSec=30`, five starts per 300 seconds and stays
disabled/inactive. Config-only alias preflight allows reviewed network terminal
types and rejects unsupported wrappers/local/memory/unknown; it proves neither
backend connectivity, canary nor durability. Owner-deferred gates remain.
