# Unattended archival reliability audit

**2026-10-13.** Baseline revision `1b70ce2`, branch `feat/takeout-autopilot`. Current code is the hybrid downloader; top-level checkout and older wiki descriptions are not the deployment authority.

## Confirmed priorities

| Finding | Evidence | Required correction |
|---|---|---|
| Complete loose media/mail parts are rejected as non-ZIP. | `autopilot/verify.py` delegates to ZIP-only `takeout2/verify.py`; `run.py` stops before moving failed verification. | Format-aware validation, exact expected sizes, ZIP member CRC and honest digest semantics. |
| Hash mode computes a digest without an expected digest comparison. | `verify_local` supplied no trusted expected SHA256. | Separate computed digest from verified equality; compare uploaded bytes with staged bytes. |
| Native transfer reads can fail outside the recorded partial-result path. | `transport.download` guarded disk errors but not all stream/client exceptions. | Preserve partial bytes/validator and classify interruption. |
| Mount and free-space checks run once before a long export. | `run.py` preflight and loop; staging shares the rclone cache volume. | Recheck per part, reserve next-part/cache budget and stop safely. |
| Existing one-shot CLI has no discovery or supervisor. | `__main__.py` only run/report; container services invoke manager and Chromium, not autopilot. | Persistent account-checked export queue, readiness polling, bounded work, backoff and supervision. |
| Reauth, browser loss and terminal export states lack unattended notification/recovery. | `run.py`, `mint.py`; no watcher invokes the engine again. | Distinct visible statuses, cached-part transfer, safe auth pauses and restart. |
| FUSE copy completion is not proof of backend upload. | Mover checks local/FUSE file size and fsync, with asynchronous rclone VFS writes possible. | Direct backend upload plus forced backend digest/size confirmation before cleanup or done. |

Detailed local findings and initial build contracts live in `.pi/reports/` and `.pi/workflows/`. Suspected findings are not promoted to verified defects without reproduction. In particular, do not expire cached minted URLs merely because of age: unnecessary reminting spends a scarce attempt.

## Baseline executable evidence

From the active worktree:

```bash
python -m pytest tests/v3 tests/v2 -q
```

Result before implementation: **797 passed, 6 skipped, exit 0**. Skipped tests require explicit `AUTOPILOT_LIVE`; this is an offline baseline, not a new Google export end-to-end test. The green baseline did not cover the production blockers above.

## Other projects and primary-source lessons

The read-only research lane compared these sources. We adopt design lessons, not third-party source code. Claims about current project maintenance and per-request attempt costs remain source-specific, not new measurements here.

- [Google Takeout support](https://support.google.com/accounts/answer/3024190): finite export availability/download allowance, password re-entry and possible additional verification. Authentication is a real boundary, not something to bypass.
- [jm2/carryout](https://pkg.go.dev/github.com/jm2/carryout): verified resume, file-identity checks, CRC validation, classified failures and pausing instead of spending attempts with stale authentication.
- [yottabit42/gtakeout_backup](https://github.com/yottabit42/gtakeout_backup): bulk archival/rclone workflow; operator limits and storage need explicit budgeting.
- [othermore/google-photos-backup](https://github.com/othermore/google-photos-backup): recurring export polling, minimal staging and stale-backup alerts; explicitly acknowledges Google re-verification.
- [Fallenstedt/google-takeout-sucks](https://github.com/Fallenstedt/google-takeout-sucks) and [rclone Drive](https://rclone.org/drive/): Drive-destination exports use a different OAuth-based retrieval path. This is not a drop-in fix for authenticated direct archive URLs. With source accounts already short of storage, we do not make Add-to-Drive the default.

Some proposed project names could not be verified; they are not evidence. September download-counter measurements include retractions: avoid asserting that all kinds of full acquisitions are free.

## Release gates

Read [the watcher contract](../docs/v3/05-WATCH-INTERFACE.md). Independent verification must check mixed-file archives, corrupt ZIP members, interrupted validated resumes, account identity, blocked-job fairness, remote confirmation failures, disk/mount loss, restart and sanitized status.

New exports are still pending. Offline fixtures and a small separate remote-storage canary can prove mechanisms without spending a Google export attempt; they do not prove that every newly requested export is complete. No source account data is deleted as part of this work.
