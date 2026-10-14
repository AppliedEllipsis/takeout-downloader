# Takeout downloader project wiki

**Updated 2026-10-13 — reliability release, activation deferred.**

**CORE21495 deployed:** [release evidence](core-release.md), 2026-10-13T22:00:42Z; clean `.v3`, 36 module fingerprints/0 mismatches, dirty main six entries preserved. Executor/unit installation remains deferred; worker inactive, unit disabled.

## Start here

v3 is a **hybrid downloader**: authenticated Chromium mints file-host URLs;
Python transfers/resumes bytes with a browser cookie jar. Verified staging is
uploaded directly through rclone and confirmed by backend size/SHA256 before
remote completion or cleanup. Browser-native download is not the archival
transport. No source-account deletion is a downloader operation.

**Authoritative release source:**
`C:/Users/User/_projects/takeout-downloader-reliability`, branch
`feat/takeout-autopilot`. The original D source is preserved unchanged because D
filled up. `1f7f9af` is the already-pushed **docs-only** audit revision, not proof
that reliability implementation is committed or deployed.

| Page | Purpose |
|---|---|
| [automation.md](automation.md) | Execution contract, paths/permissions, owner gates and stop/recovery. |
| [architecture.md](architecture.md) | Hybrid data flow, source map and receipt semantics. |
| [reliability-audit.md](reliability-audit.md) | Evidence index and unresolved release gates. |
| [credentials.md](credentials.md) | Secret pointers and environment names, never values. |
| [infrastructure.md](infrastructure.md) | Current mapping plus dated September inventory. |
| [journal-2026-10.md](journal-2026-10.md) | October evidence and deferred steps. |
| [decisions.md](decisions.md) | Append-only ADRs; later decisions supersede earlier ones. |
| [runbooks.md](runbooks.md) | Historical procedures; use automation.md for this release. |
| [measured-facts.md](measured-facts.md) | Dated browser/auth/Range evidence, including retractions. |
| [archival-inventory.md](archival-inventory.md) | Historical coverage/integrity, not deletion authorization. |
| [failure-modes.md](failure-modes.md) | Failure-mode index and linked runtime/storage/export pages. |
| [journal.md](journal.md) | Append-only earlier investigation history. |
| [v3 documentation](../docs/v3/README.md) | CLI, watcher, operations and future full-pull procedure. |

## Release boundary

- **No worker activation:** owner deferred rclone unlock and Google sign-in; unit
  must remain disabled/inactive. No real Google mint/download or password attempt.
- Host systemd supervises foreground `docker exec` through
  `tools/container_watch.py`; watcher runs as a non-root UID inside existing
  `takeout-webgui`. No new TCP port or browser/container restart.
- Pending/ready export counts are **unknown**. A login page with no export elements
  is not evidence of zero exports. No live backend canary has run.
- Offline repair evidence: Windows **1352 passed / 66 skipped**; nonroot WSL
  focused mount/guardian/discovery **251 passed**. Broad native Linux validation
  remains pending; read [audit evidence](reliability-audit.md), not predicted totals.
- Size, archive CRC and computed SHA256 have different meanings; a backend receipt
  is not proof of product coverage or provider retention.

## Mount-boundary correction — future configuration only

All three future archive binds (webGUI, TUI, CLI) are literal
`/opt/archives:/opt/archives`; no `STORAGE_ROOT` override can broaden them to `/opt`.
CLI allowed roots are `/opt/archives:/downloads`. **Every effective container
mount must exclude `/opt/local_cache_crypt/takeout-autopilot-secrets` BEFORE
provisioning secrets.** Bounded host-side Docker inspect admission is only a
point-in-time gate, not protection against later unsafe administrator changes.
No existing containers were recreated and no secret file was created.

The [frozen gate](../.pi/reports/final-frozen-gate.md) wiki SHA drift is reconciled
as commander-documented banner edits to this page and automation.md only, with
no mismatch in its 173 source/config/test files. That does **not** waive its true
host-secret containment finding. The separate
[mount repair](../.pi/reports/mount-boundary-repair.md) records the correction and
remaining Linux blockers; neither report is blanket release/activation approval.
