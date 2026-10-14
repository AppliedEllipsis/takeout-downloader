# Reliability release — evidence and gates

**2026-10-13.** Source:
`C:/Users/User/_projects/takeout-downloader-reliability`. D original unchanged
because its disk filled. `1f7f9af` is the already-pushed docs-only audit revision;
implementation/final gate are not inferred from that hash.

## Decision: release work may finish; activation remains HOLD

Owner deferred **rclone unlock and Google sign-in**. No password attempts, real
Google mint/download, worker activation or live backend canary. Unit remains
disabled/inactive by release contract. Export ready/pending counts are **unknown**.
[automation.md](automation.md) supersedes the earlier host-CDP/host-venv worker
plan. No new port, container/browser restart or dirty main-repo overwrite.

## Confirmed repairs versus evidence limits

| Earlier blocker | Candidate behavior / offline evidence |
|---|---|
| ZIP-only verification, weak hash semantics | Exact-size loose parts, full ZIP CRC/TAR/gzip reads; computed digest separated from expected comparison. |
| Escaped stream failures / unsafe resume | Classified partial results, preserved strong validators; header/size mismatch rejected before destination damage. |
| One-time headroom/mount checks | Per-transfer remaining-byte + reserve + floor checks; fail safely and retain evidence. |
| No discovery/supervision | Explicit-account discovery, atomic local state/lock, bounded fair passes and backoff; container supervision is a separate release lane. |
| FUSE completion confused with durability | Direct backend upload, forced exact size/SHA confirmation and persistent receipts before done/cleanup. |
| Authentication/terminal retry loops | Visible reauth/quota/expired/Google-failed/storage blockers; no password automation or speculative remint by age. |

These are candidate mechanism statements, not production export success. Read
reports for source version, scope, failed runs and platform limitations.

## Measured evidence

| Evidence | Actual result / boundary |
|---|---|
| [baseline-tests.md](../.pi/reports/baseline-tests.md) | Initial D-worktree `python -m pytest tests/v3 tests/v2 -q -p no:cacheprovider`: **797 passed, 6 skipped**; predates implementation. |
| [integrity-repair.md](../.pi/reports/integrity-repair.md) | Final assigned suites **110 passed**; supplemental **45 passed, 6 skipped**. Windows descendant containment/local HTTP exercised; no backend. |
| [remote-build.md](../.pi/reports/remote-build.md) | **57 passed**; local rclone hash command and synthetic subprocess backend, not production remote. |
| [orchestration-build.md](../.pi/reports/orchestration-build.md) | Latest focused gate **54 passed**; receipt and cached-only regressions. Prior gates/failure remain recorded. |
| [watcher-build.md](../.pi/reports/watcher-build.md) | Focused **47 passed**; earlier broad **398 passed, 6 skipped, 3 failed** during integration, not final release gate. |
| [release-migration.md](../.pi/reports/release-migration.md) | C migration/hash preservation; broad run **971 passed, 6 skipped, 2 failed** (obsolete source assertion / absent Windows mount fixture). |
| Owner integration update (historical) | Parent repaired those fixtures; focused **38 passed** reported to this lane. Not a whole-suite result; later frozen/mount-repair evidence is recorded below. |
| [server-preflight.md](../.pi/reports/server-preflight.md) | No deployment; old clean `.v3` at `1b70ce2`; unlock exit 1; host CDP reset, container CDP works; fresh view needs reauth; no ready/pending count or canary. |
| [final-core-verifier.md](../.pi/reports/final-core-verifier.md) | Immutable core snapshot `20261013T212824394139`: **974 passed, 6 skipped**, exit 0, plus adversarial/compile/import gates. This snapshot predates the separate supervisor/runtime-token additions; it is not the final actual-checkout gate. |
| [server-core-deploy.md](../.pi/reports/server-core-deploy.md) / [core-release.md](core-release.md) | CORE21495 deployed 22:00:42 UTC; clean `.v3`, 36 fingerprints/0 mismatches; dirty main six entries preserved; no activation. |\n| [server-supervisor-deploy.md](../.pi/reports/server-supervisor-deploy.md) | Supervisor `7fc3ac9` deployed 03:05:31 UTC; clean detached `.v3`, 37 file/blob fingerprints, imports/help/mount checks passed; unit absent/inactive and no worker. No unlock/auth/canary/transfer. |
| [release-docs.md](../.pi/reports/release-docs.md) | Exact documentation-lane commands, checks and boundaries. |

Do not sum passing subtotals: reports cover different/concurrent source states
and overlapping suites. The historical frozen-checkout gate and subsequent mount-repair evidence are
recorded below. The core snapshot predates those gates; no broad Linux-green or
activation approval follows from the later focused results. Fixtures do not
establish authenticated DOM compatibility, backend access or a new export pull.

## Outstanding gates

- Secure unlock preflight and authenticated identity/export readiness remain owner-deferred; no backend canary or real export pull has been performed.
- Supervisor source, example, non-root UID, private path permissions, mapped manager venv, mount admission and stop/orphan behavior are verified offline; installing the unit and provisioning the protected secret path remain deferred.
- CORE-only update/import/help/fingerprints are complete per server-core-deploy.md.
  Executor/unit installation, unit syntax/UID/permissions/stopped-state and scoped
  stop behavior are separate gates. This docs lane performs no deployment.
- **Later owner authorization:** secure unlock preflight, authenticated identity/
  export readiness, optional retained backend canary and separate activation gate.

## Primary-source lessons (not new measurements)

[Google Takeout support](https://support.google.com/accounts/answer/3024190)
documents finite availability/download allowances and interactive verification.
[jm2/carryout](https://pkg.go.dev/github.com/jm2/carryout) informs validated resume
and classified failures; [gtakeout_backup](https://github.com/yottabit42/gtakeout_backup)
informs bulk archival/storage budgeting; [google-photos-backup](https://github.com/othermore/google-photos-backup)
informs polling and visible auth pauses. Lessons are adopted, not source code.
Drive-destination exports have a different OAuth path and may consume source-account
space; they are not the default workaround. Counter/attempt-cost claims stay tied
to dated observations/retractions, not promises of free full transfers. No
source-account deletion is authorized.

## Frozen-gate reconciliation and mount repair (later evidence)

[final-frozen-gate.md](../.pi/reports/final-frozen-gate.md) remains historical HOLD.
Its README/automation wiki SHA drift is reconciled as commander-documented banner
edits only; **no source/config/test mismatch across its 173 comprehensive files**.
This explains the documentation mismatch, not a waiver of the real HIGH
host-secret containment bug: default broad `/opt` mounts exposed the mandatory
EnvFile parent despite the former “Not mounted” claim. Permissions alone did not
establish host-only containment. The report is preserved, not rebaselined.

[mount-boundary-repair.md](../.pi/reports/mount-boundary-repair.md) implements a
separate future-only correction: all three archive binds literal `/opt/archives`,
no broad `STORAGE_ROOT` override; CLI roots `/opt/archives:/downloads`. Bounded
host-side inspect admission rejects lexical/resolved source overlap, including
read-only/volume/symlink cases and malformed metadata. **Effective exclusion from
all container mounts is REQUIRED BEFORE provisioning secrets.** Admission is
point-in-time, not prevention of later unsafe administrator reconfiguration.
No existing containers recreated; no secret file, live mount certification or
activation. See [runbook](automation.md#mount-boundary-admission-and-compatibility-runbook--future-only).

| Measured repair evidence | Scope / limitation |
|---|---|
| Windows full exact C checkout: **1352 passed, 66 skipped, 5 warnings**, exit 0 | Unix/Linux, symlink and opt-in live-network skips are not Windows proof of Linux behavior. |
| Nonroot WSL final focused: **251 passed, no skips**, exit 0 | Mount admission/owned-process/discovery suites only, not broad native Linux validation. |
| Unrestricted WSL v3/v2: collection blocked | Linux pydantic_core unavailable; Windows extension cannot load on Linux. |
| WSL dependency-scoped: **1170 passed, 6 skipped, 2 failed** | Mint-only second-pass transfer failure reproducible; remote flood ready-marker timing failed then passed focused repeat. Combined repeat **289 passed, 1 failed**. |
| Shell/unit checks | Four unchanged raw CRLF shell failures; in-memory LF syntax passes are not raw-file repair. Unit verify exit 0 retains DrvFS mode warnings. |

**Broad native Linux validation remains pending/HOLD.** Totals are measured in the
originating report, not rerun by this documentation lane, not summed or predicted.
Legacy `outputDir` compatibility needs an explicit safe bind outside all secret
ancestors; broad `/opt` is never a compatibility fix. CORE21495 is unchanged and
not recertified; executor/unit installation and owner unlock/sign-in/activation
remain deferred. No blanket final approval follows from this reconciliation.
