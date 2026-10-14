# Unattended watcher — current interface and acceptance contract

**Updated 2026-10-13. Candidate mechanisms are offline-tested; live activation is
owner-deferred.** Unknown pending/ready counts; no live backend canary or new Google
export pull. No password attempt, new port or browser/container restart.

## User-facing surface

```text
python -m autopilot watch --config PATH
python -m autopilot watch --config PATH --once
python -m autopilot watch --config PATH --once --dry-run
```

`--once` performs a real tick and can discover/mint/transfer/upload: **not read-only**.
`--dry-run` validates config and returns account labels without browser/DB/runner
work; it does not prove permissions, unlocked backend or authenticated readiness.
The run/report commands retain their separate interfaces/exit codes. Watch returns
sanitized status/config refusal; per-export run codes are not watch exit semantics.

The production watcher is launched only through host systemd and
`tools/container_watch.py`, bounded foreground docker exec as non-root UID inside
existing `takeout-webgui`. Worker uses `/opt/manager-venv/bin/python`, `/work/.v3`
and container-local CDP. Mandatory encrypted backend-config unlock preflight must
succeed before launch; it is config-only alias validation, **not backend network
access/canary/durability evidence**. Only reviewed network terminal types are
allowed after alias/crypt/chunker resolution; unsupported wrappers and
local/memory/unknown terminals are refused. Unit remains disabled/inactive.
See [automation](../../.wiki/automation.md).

## Configuration schema

All production filesystem paths in watcher JSON are **container paths**. This
illustrative template deliberately has invalid identity placeholders: replace
only after owner-authenticated identity proof; do not activate it now.

```json
{
  "state_dir": "/config/autopilot/state",
  "ledger": "/config/autopilot/ledger.sqlite",
  "staging_root": "/config/autopilot/staging",
  "archive_root": "/opt/archives/google-takeout",
  "archive_remote": "archives:google-takeout",
  "cdp_http": "http://127.0.0.1:9222",
  "poll_seconds": 60,
  "retry_max_seconds": 3600,
  "reauth_retry_seconds": 300,
  "min_headroom": 5368709120,
  "staging_reserve": 107374182400,
  "rclone_binary": "/config/autopilot/bin/rclone",
  "rclone_config": "/config/autopilot/rclone.conf",
  "accounts": [
    {
      "name": "owner",
      "manage_url": "https://takeout.google.com/manage?user=REPLACE_WITH_VERIFIED_NUMERIC_ID",
      "user_id": "REPLACE_WITH_VERIFIED_NUMERIC_ID"
    }
  ]
}
```

Unknown fields are refused. Paths must be absolute/non-symlink with safe derived
labels/UUIDs; state/ledger/staging must be local, not FUSE. Account entries are
exactly `name`, `manage_url`, `user_id`; duplicate labels/IDs are refused. Intervals
must be finite 1–604800 seconds, retry maximum at least poll interval; byte budgets
are nonnegative bounded integers. Named remote only, no inline credentials/URLs.
Loopback HTTP CDP only, no public endpoint. Required directories/access are checked
under the effective container UID before activation, not inferred from dry-run.

Production reserve is 100 GiB plus 5 GiB floor and remaining/restart part bytes.
`WatchConfig` library reserve default is 5 GiB, **not** the release policy; use the
explicit production config. `archive_remote` here is the root; watcher derives
safe `<label>/<UUID>` directories. Run CLI `--archive-remote` takes a **full export
directory**. No host PRIVATE paths or new host venv worker in this JSON.

## Identity and authentication boundary

Configured numeric `user_id`, manage URL and independently attested DOM/link/part
identity must agree. Labels, query parameters or old ledger associations alone
are not proof. Manage identity absence/ambiguity fails closed. An identity-less
pending archive is non-executable and can remain pending only after independently
verified manage identity; ready parts must attest the matching user/export/index.

Discovery uses a disposable automation target, bounded pages/exports and read-only
metadata. It never clicks download/mint for readiness and closes only its own tab.
Legitimate ephemeral runtime `rapt`/`authuser` query keys are validated for
navigation only: duplicates/unknown keys and invalid values fail closed. Neither
key attests identity; a missing URL user can be filled only after independent DOM
identity verification. Strict config rejects these keys, and returned export
references are canonical user-only URLs. Tokens never belong in committed config,
watcher status, queue URLs, notifications or logs. Do not guess/strip tokens to
force ready. October smoke saw an auth page, **not authenticated export markup**;
live compatibility remains unverified.

Owner sign-in/second factor is a boundary. No password helper imports, automatic
rejected-password retry or credential bypass. Cached usable URLs/verified staging
remain protected; no arbitrary-age remint.

## State, scheduling and observability

| Item | Contract |
|---|---|
| Local control | `state/watch-status.json` atomically written, bounded whitelisted state; `state/watch.lock` OS-owned single-instance lock retained across ticks |
| Restart/dedup | Preserve queue/ledger/progress/receipts; no duplicate worker or repeated terminal work |
| Readiness | Pending/empty pages poll without mint; zero parts never complete |
| Work | Separate cached-aware mint batches and cached-only `max_parts=1` transfer passes; fair accounts/exports |
| Backoff | Distinct reauth and transient retries; exponential failures capped by config; notification failures do not lose state |
| Visible job states | `pending`, `ready`, `incomplete`, `failed`, `needs_reauth`, `storage_blocked`, `complete`, `expired_unrecoverable`, `quota_exceeded`, `google_failed` |
| Account blockers | `needs_reauth`, `identity_or_discovery_blocked`, retry time and last discovery |
| Sanitization | No cookies, signed URLs, reauth tokens/passwords/raw exceptions; optional bounded Telegram via env names only |
| Completion | Nonempty full expected list, local integrity and exact backend receipts; no FUSE-only done/cleanup |

Terminal complete/expired/quota/Google-failed jobs are not rapidly retried. A mint
batch with status incomplete means work remains, not durable success. `mint_status`
and part counts/retry times distinguish earned URLs, auth blocks and transfers.
Failure status is explicit even if notifications fail. Historical matching done
receipts avoid forced full backend rehash on each tick; separate audits are needed
for fresh mutation/retention evidence.

## Acceptance evidence and limits

Offline tests cover pending-to-ready, multiple accounts, identity mismatch,
restart/dedup, lock contention, blocked-account fairness, backoff, incomplete/empty
pages and redaction. Transfer/backend fixtures cover interrupted validated resumes,
corrupt members, wrong remote digests and failed upload confirmation. Supervisor
and runtime-token tests are a separate execution-lane gate. Cite exact reports in
[reliability audit](../../.wiki/reliability-audit.md), not expected totals.

No export creation, source deletion, human-download sweep or public management API
is introduced. Actual imported code, unit syntax/UID/paths/stopped state and
read-only container-local browser version are separate deployment evidence.
Authenticated discovery/backend canary/full Google pull require later explicit
owner gates. None is claimed successful by this documentation release.

## Protected environment and release boundary

Mandatory unit `EnvironmentFile` is host-only
`/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env` (`root:root 0600`),
under `root:root 0700`, outside worker-writable PRIVATE and **all effective
container mounts BEFORE provisioning secrets**. Check source ancestors, read-only
mounts, volumes and resolved symlinks, not just `/work` and `/config`. A worker-writable parent permits replacement and root environment
injection even when the file is root-owned. PRIVATE remains host
`/opt/local_cache_crypt/_projects/takeout-downloader/config/autopilot` →
`/config/autopilot`, owner `20001:20001`, mode `0700`, no group/other bits;
ancestor traversal is separate. No key/credential file was provided or created.

[CORE21495 is deployed](../../.wiki/core-release.md), not activated. Current unit
uses `Restart=on-failure`, `RestartSec=30`, five starts per 300 seconds and stays
disabled/inactive. Config-only alias preflight allows reviewed network terminal
types and rejects unsupported wrappers/local/memory/unknown; it proves neither
backend connectivity, canary nor durability. Owner-deferred gates remain.

## Mount admission and legacy output compatibility — FUTURE ONLY

Future webGUI, TUI and CLI archive binds are literal
`/opt/archives:/opt/archives`. No `STORAGE_ROOT` override can broaden them to
`/opt`; webGUI manager root is fixed `/opt/archives` and CLI allowed roots are
`/opt/archives:/downloads`. Legacy `outputDir` outside those roots requires an
**explicit safe bind outside all secret ancestors** plus reviewed output-root
configuration where applicable. Do not restore broad `/opt` for compatibility.

Every effective mount must exclude
`/opt/local_cache_crypt/takeout-autopilot-secrets` BEFORE provisioning secrets.
Review other binds/volumes too, including the configurable read-only VFS cache
source. Root ownership/modes are necessary access controls, not proof of exclusion.
Before exec, host supervisor admission uses Docker inspect with a 10-second
bound, 1 MiB stdout cap, suppressed stderr and bounded owned-client cleanup.
Lexical/resolved source ancestor, exact or descendant overlap refuses even for
read-only bind/volume mounts and symlink aliases. Empty/missing/malformed metadata,
unsupported types, resolution errors, failed inspection, timeout and flood fail
closed. Refusal is not permission to bypass supervision.

This is point-in-time admission, NOT prevention when an administrator later
creates/reconfigures an unsafe container or changes mounts/symlinks. Repeat
layout review after changes; pre-provision exclusion is a separate mandatory gate.
No existing containers were recreated and no secret file was created.
[Repair evidence](../../.pi/reports/mount-boundary-repair.md): Windows **1352 passed /
66 skipped**, nonroot WSL focused **251 passed**; broad native Linux validation
pending with dependency/test/raw-CRLF blockers retained. These are measured offline
results, not live safety/full-pull proof. The
[frozen gate](../../.pi/reports/final-frozen-gate.md) wiki SHA drift is reconciled
as commander banner edits only, with no mismatch in its 173 source/config/test
files; this does not waive the true containment bug or rewrite historical HOLD.
See [runbook](../../.wiki/automation.md#mount-boundary-admission-and-compatibility-runbook--future-only)
and [audit](../../.wiki/reliability-audit.md). No final approval, executor activation
or recertification of deployed CORE21495 is inferred.
