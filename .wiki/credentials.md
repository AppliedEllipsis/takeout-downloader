# Credentials — pointers only

**Updated 2026-10-13. Never record values, cookies, signed URLs or tokens here.**
Owner deferred rclone unlock and Google sign-in. This page grants no authorization
to try a password, unlock a backend, mint a URL or start a worker.

## Locations and permissions

Let `R=/opt/local_cache_crypt/_projects/takeout-downloader` and
`PRIVATE=$R/config/autopilot` on the host.

| Material | Location / rule |
|---|---|
| SSH | Alias `takeout-server`; use `-o BatchMode=yes -o ConnectTimeout=10` |
| Google session | Existing container profile `/config/.chrome-profile`; preserve browser state |
| CDP/cookies | Container-local `http://127.0.0.1:9222`; cookies/UA captured for transfer, not password replay |
| Worker interpreter | Existing container `/opt/manager-venv/bin/python`; no new production host venv |
| Encrypted rclone config | Authorized byte-identical encrypted copy: host `PRIVATE/rclone.conf`, container `/config/autopilot/rclone.conf` |
| Trusted rclone binary | Host `PRIVATE/bin/rclone`, container `/config/autopilot/bin/rclone`; copied trusted server binary |
| Service env | Host-only `/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env`, root:root `0600`; mandatory unit EnvironmentFile, parent root:root `0700`, outside PRIVATE and every effective container mount before provisioning |
| Watch config | Host `PRIVATE/watch.json`, container `/config/autopilot/watch.json`; verified identity, no auth tokens |
| Sensitive ledger | Host `PRIVATE/ledger.sqlite`, container `/config/autopilot/ledger.sqlite`; cached signed URLs require private local storage |
| State/staging/cache/tmp | Under PRIVATE; owner `20001:20001`, directories `0700` with no group/other bits; files `0600`, umask `0077` |

Check **every parent** for traversal and container UID readability/writability.
Host service identity and container worker UID are separate checks. Do not fix
access by making secrets world-readable. Numeric UID mapping and actual unit
access must be proven by the execution/deployment lane.

## Environment names (never values)

- Authorized unlock may use `RCLONE_CONFIG_PASS` or a separately approved secure
  credential mechanism. None has been provisioned by this documentation lane.
  No password in argv, remote names, logs or committed JSON.
- `RCLONE_CACHE_DIR=/config/autopilot/cache` is the **container** worker mapping.
- Optional notifier: `TELEGRAM_ENABLED`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
  Notification failure must not discard queue state.
- `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`: non-secret runtime settings.
- CLI tuning: `AUTOPILOT_MIN_HEADROOM`, `AUTOPILOT_MOVE_TIMEOUT`,
  `AUTOPILOT_MOVE_STALL`; not credentials.

Supervisor forwards env **by name only** (`docker exec -e NAME`), never
`NAME=secret` in argv. Do not print environments, raw rclone output, profile
contents, private identity values or ledger signed URLs in reports.

## Unlock gate and current evidence

[Server preflight](../.pi/reports/server-preflight.md) found encrypted config at
`/opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf`; its original root
ext4 location/mode was not changed. Noninteractive `listremotes --ask-password=false`
failed with exit 1. No authorized unlock pointer was established; no env/config
was provisioned and no backend canary ran. An active FUSE mount does not prove
that a new worker can unlock its configuration.

Before **future separately authorized** activation, the supervisor must perform a
bounded noninteractive backend-config unlock preflight under the effective
container UID with mapped config/binary/env. Current preflight is config-only
`config dump --ask-password=false`, output suppressed, alias/crypt/chunker chains validated to reviewed network terminal types;
unsupported wrappers and local/memory/unknown terminals are refused. It
does **not** contact a backend or prove backend access. Failure prevents launch.
Keep stdin noninteractive and rclone `--ask-password=false`; suppress raw output.
Do not weaken encryption, mine process memory, reuse unrelated secrets or retry
passwords. A backend canary is a separate future authorization, not a release fact.

## Google reauthentication — deferred

Fresh views required `needs_reauth`; ready/pending counts remain unknown. Owner
must later satisfy sign-in/second factor interactively. `tools/satisfy_login.py`,
`TAKEOUT_GPASS` and gitignored `config/gPass.txt` are historical helper pointers,
**not the watcher's auth path and not approved now**. The watcher does not import
password helpers. Do not run password-helper probes for this release. Verified
staged work can archive without reminting; expired/quota-exhausted exports need a
new export, not auth retries.

## Mandatory protected environment layout

Host-only `/opt/local_cache_crypt/takeout-autopilot-secrets` must be `root:root
0700`; its `credentials.env` must be `root:root 0600` and is the mandatory unit
`EnvironmentFile`. Keep it outside worker-writable PRIVATE and **all effective
container mounts BEFORE provisioning secrets**, including ancestor sources,
read-only mounts, volumes and symlink aliases; `/work` and `/config` alone are
not a sufficient exclusion check. Root ownership of a file inside a worker-writable directory
is insufficient: the worker could replace it and inject root environment.

PRIVATE remains `/opt/local_cache_crypt/_projects/takeout-downloader/config/autopilot`
→ `/config/autopilot`, owner `20001:20001`, mode `0700`, no group/other bits.
Ancestor traversal is a separate check, not a PRIVATE permission exception.
No key/credential file was provided or created; activation remains owner-deferred.

## Future mount correction; not live exclusion evidence

All three future archive binds (webGUI, TUI, CLI) are literal
`/opt/archives:/opt/archives`, not a broad `/opt` or an inherited `STORAGE_ROOT`
override. CLI allowed roots are `/opt/archives:/downloads`. Review every other
effective source too, especially configurable read-only VFS cache sources.

Bounded host-side Docker inspect admission before exec rejects source ancestor,
exact or descendant overlap with `/opt/local_cache_crypt/takeout-autopilot-secrets`,
including read-only bind/volume sources and lexical/resolved symlink aliases.
Malformed/missing metadata and failed/timed-out/oversized inspection fail closed.
This point-in-time guard does not prevent later unsafe administrator container/
mount/symlink changes. **Effective exclusion is REQUIRED BEFORE provisioning
secrets**; permissions alone are not containment. No secret file was created, no
existing containers recreated, and no current live-layout certification is claimed.

Legacy `outputDir` needs an explicit safe bind outside all secret ancestors when
outside the new roots. See [future runbook](automation.md#mount-boundary-admission-and-compatibility-runbook--future-only)
and [repair evidence](../.pi/reports/mount-boundary-repair.md).
