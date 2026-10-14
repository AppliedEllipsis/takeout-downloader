# Automation — reliability release operating contract

**2026-10-13. Activation owner-deferred; unit disabled/inactive.** This is the
approved target contract; CORE deployment is complete, not executor/unit
installation or final merged-test evidence. Frozen ownership: [finish-release.md](../.pi/workflows/finish-release.md).

**CORE21495 deployed:** [release evidence](core-release.md), 2026-10-13T22:00:42Z; clean `.v3`, 36 module fingerprints/0 mismatches, dirty main six entries preserved. **Supervisor source 7fc3ac9 deployed:** [server evidence](../.pi/reports/server-supervisor-deploy.md), 2026-10-14T03:05:31Z. Executor/unit installation remains deferred; worker inactive, unit disabled.

## What is known / what is not

- Source moved to `C:/Users/User/_projects/takeout-downloader-reliability` because
  D filled up; D original preserved. `1f7f9af` is already-pushed docs-only history.
- [Server preflight](../.pi/reports/server-preflight.md): host CDP docker-proxy
  resets, container-local CDP works; fresh Google views need reauth. **Ready and
  pending counts unknown.** rclone unlock failed noninteractively; no live canary.
- Owner deferred unlock/sign-in and activation. No mint/download/password attempt,
  new TCP port or browser/container restart is authorized.
- Initial migrated suite: 971 passed / 6 skipped / 2 fixture failures; parent
  reports repaired fixture gate 38 passed. Later mount-repair full Windows gate:
  **1352 passed / 66 skipped**; native isolated Linux v3/v2 **1211 passed / 6 skipped**;
  native full project **1410 passed / 8 skipped**; nonroot WSL focused gate **251 passed**.
  See [evidence index](reliability-audit.md); do not combine totals.

## Execution contract

Host systemd supervises **foreground** `docker exec` using
`tools/container_watch.py`. The existing `takeout-webgui` runs the watcher as an
explicit **non-root UID**, in `/work/.v3`, using its existing
`/opt/manager-venv/bin/python`. Both `autopilot` and `takeout2` must be importable.
The trusted server rclone is copied into PRIVATE/bin, not installed globally.

The current unit runs the host bridge as **root** for Docker authority, with
container UID:GID **20001:20001** (reserve/provision before activation; never add
the worker to the docker group). `Restart=on-failure`, `RestartSec=30`, `StartLimitIntervalSec=300`,
`StartLimitBurst=5` (five starts per 300 seconds), `TimeoutStopSec=25` and
`KillMode=control-group` apply on the host; the container tree is outside that
cgroup and needs its own Linux subreaper guardian. An 8-second heartbeat lease
and 5-second scoped cleanup are implemented, not proof of live stop behavior.

Current candidate unit's **FUTURE ONLY** ExecStart (read from the file, not run):

```text
/usr/bin/python3 -I -B /opt/local_cache_crypt/_projects/takeout-downloader/.v3/tools/container_watch.py --container takeout-webgui --identity 20001:20001 --config /config/autopilot/watch.json
```

`deploy/takeout-autopilot-watch.service` and `deploy/watch.example.json` now use
this container mapping. Their presence is not installation, UID provisioning,
backend unlock, worker activation or deployment proof.

Supervisor must bound startup/preflight/stop, forward env by **name only**, and
prevent orphan watcher/download/rclone descendants. Host stdin heartbeat/EOF
provides container-side loss-of-supervisor detection; scoped process-tree cleanup
must terminate only this worker tree. No `docker exec -d`, broad `pkill`, browser
restart, extra management listener or host-CDP forwarding fix. Systemd restart
policy does not authorize activation while owner holds the gate.

**Mandatory backend-config unlock preflight precedes watcher launch**, under the
same container UID, mapped encrypted rclone config and authorized env. Current
preflight runs `rclone config dump --config /config/autopilot/rclone.conf
--ask-password=false` with closed stdin, a 20-second bound and suppressed capped
pipes (1 MiB each). It validates the `archives` alias/crypt/chunker chain to an
explicitly reviewed network terminal type. Unsupported wrappers and local/memory/
unknown terminals are rejected; only alias/crypt/chunker wrappers resolve. **Config-only, no backend network I/O:** it proves unlock/
alias resolution, not backend reachability, credentials validity, Google readiness
or durability. Failure means no watcher. A healthy FUSE mount is not enough.

## Host ↔ container path map

`R=/opt/local_cache_crypt/_projects/takeout-downloader`;
`CODE=$R/.v3`; `PRIVATE=$R/config/autopilot`.

| Host | Container | Purpose |
|---|---|---|
| `CODE` | `/work/.v3` | Released code; preserve dirty host main repo |
| `PRIVATE/watch.json` | `/config/autopilot/watch.json` | Account/storage watcher config |
| `PRIVATE/ledger.sqlite` | `/config/autopilot/ledger.sqlite` | Local SQLite progress and signed URL cache |
| `PRIVATE/state/` | `/config/autopilot/state/` | `watch-status.json`, `watch.lock` |
| `PRIVATE/staging/` | `/config/autopilot/staging/` | Bounded part staging/quarantined evidence |
| `PRIVATE/cache/` | `/config/autopilot/cache/` | Private rclone cache setting |
| `PRIVATE/tmp/` | `/config/autopilot/tmp/` | Private guardian/worker TMPDIR |
| `PRIVATE/bin/rclone` | `/config/autopilot/bin/rclone` | Trusted copied executable |
| `PRIVATE/rclone.conf` | `/config/autopilot/rclone.conf` | Encrypted config copy, not decrypted config |
| `/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env` | Must be excluded from every effective mount before provisioning | Mandatory root:root `0600` EnvironmentFile; parent root:root `0700`; values forwarded by name |
| Existing container venv | `/opt/manager-venv/bin/python` | Worker interpreter; no new production host venv |
| `/opt/archives/google-takeout` | Same path | Historical local FUSE view, **not remote receipt authority** |
| `archives:google-takeout` | rclone named remote | Direct archival root; per-account/UUID dirs derived by watcher |

Guardian sanitizes HOME to `/config/autopilot`, TMPDIR to `/config/autopilot/tmp`,
PATH to `/usr/bin:/bin`, and cache to `/config/autopilot/cache`. Provision tmp too.
Encrypted config must be a non-symlink regular private file (no group/other bits);
trusted binary must be executable and not group/other writable.

The PRIVATE volume must be mounted encrypted local disk. Never SQLite/state/lock
on FUSE, never staging on root. Host `/config` does not exist. Old host-path JSON,
`PRIVATE/venv` production-worker examples and host-loopback CDP assumptions are
superseded. Read-only scratch venv evidence remains historical, not the runtime.

## Config and permissions

- `watch.json` uses **container paths**, loopback CDP and explicit numeric Google
  identity in `user_id` and matching manage URL `?user=...`. Storage labels are
  safe labels, not evidence of identity. Placeholder identities must refuse.
- No rapt/cookies/passwords in committed JSON or watcher status. Discovery accepts validated ephemeral `rapt` and bounded numeric `authuser`
  runtime keys for navigation only. Neither key proves account identity; a missing
  URL user can be filled only after independent DOM identity verification.
  Strict config rejects those keys; returned/persisted export references are
  canonical user-only URLs. Never commit/log runtime tokens. Do not strip/guess
  tokens to bypass identity or auth. Authenticated live markup remains unverified.
- PRIVATE must be worker-owned `20001:20001`, mode `0700`, no group/other bits.
  Use private files `0600`, umask `0077`;
  readable encrypted config/watch config and writable state/ledger/staging/cache/tmp
  for the chosen container UID. Trusted executable must be executable by that UID
  but not writable by untrusted users. Check all ancestor traversal permissions.
- Service host user/group and container UID are distinct; docker authority is
  privileged. Unit/environment access must be narrowly scoped and verified, not
  assumed from UID names. Never `chmod -R 777` or source root-private env as worker.
- Budget includes next-part remaining/restart bytes + `107374182400` reserve
  (100 GiB) + `5368709120` floor (5 GiB). Browser downloads and shared VFS growth
  still consume the same encrypted physical disk; FUSE free space is not capacity
  evidence. CLI/library defaults differ from this production budget.

Full config surface: [watch interface](../docs/v3/05-WATCH-INTERFACE.md).
Secrets: [credentials.md](credentials.md). Run flags:
[run interface](../docs/v3/02-RUN-INTERFACE.md).

## Exact commands: inspection versus execution

The following are **future read-only operational examples**, not commands executed
by this docs lane or proof of deployed code. Run via bounded noninteractive SSH;
do not dump config/env/ledger contents.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server \
  'systemctl is-enabled takeout-autopilot-watch.service; systemctl is-active takeout-autopilot-watch.service'
ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server \
  'df -h /opt/local_cache_crypt; findmnt -T /opt/local_cache_crypt'
ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server \
  'git -C /opt/local_cache_crypt/_projects/takeout-downloader/.v3 rev-parse HEAD'
```

Local docs-lane executed commands/results are in
[release-docs.md](../.pi/reports/release-docs.md). Earlier executed server probes
and their dated outcomes are in [server-preflight.md](../.pi/reports/server-preflight.md).
Neither section is a completed deployment recipe.

## Stop and recovery — FUTURE only if a worker was separately authorized

```bash
# Host: stop supervised worker and keep activation disabled; does not stop Chromium.
sudo systemctl stop takeout-autopilot-watch.service
sudo systemctl disable takeout-autopilot-watch.service
systemctl is-active takeout-autopilot-watch.service
systemctl is-enabled takeout-autopilot-watch.service
```

After stop, prove this worker tree and its owned transfers are gone. A dead docker
client alone is not proof. Do not close human tabs, kill unrelated Python/rclone,
unmount archives, clear browser downloads or delete a lock file to defeat a live
OS lock. Stale lock pathname is harmless after the OS releases its lock.

| State | Recovery, preserving progress |
|---|---|
| Unlock/preflight failure | Stay stopped; obtain authorized unlock later. No password guessing or insecure config conversion. |
| `needs_reauth` | Owner signs in later; preserve cached URLs/parts. No automatic password helper. |
| Identity/discovery blocked | Verify account in authenticated DOM; fix private config after proof, not guessed label/URL. |
| Pending / empty page | Poll without mint; never infer complete or zero exports from login. |
| `storage_blocked` / mount loss | Stop, recheck encrypted mount and shared headroom; retain staged/partial files; no root fallback. |
| Partial stream | Validated resume with saved strong validator; no age-based remint. Explicit changed/auth failures may invalidate cache. |
| Corrupt part | Keep quarantine/evidence; investigate or retry safely with existing valid URL. Size alone cannot repair CRC. |
| Backend confirmation failure | Retain verified staging, no done/cleanup. Existing different remote object is not overwritten. |
| Expired / quota / Google failed | Visible terminal blocker; owner creates a new export, no endless retry. |
| Restart / lock contention | Preserve ledger/state; one worker only. Verify previous owned tree stopped before restart. |

Restart/start/enable are **not authorized now**. Before any future activation:
final release evidence, safe approved code update/imports, unit syntax/UID/path
checks, authorized unlock preflight, verified identities/readiness and a separate
owner launch decision. A separately approved tiny retained backend canary can
check mechanics; no such canary has run, and it never proves a Google full pull.

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

## Mount-boundary admission and compatibility runbook — FUTURE ONLY

1. Before provisioning any secret, establish that **every effective mount** of
   every relevant container excludes
   `/opt/local_cache_crypt/takeout-autopilot-secrets`. Check source ancestors and
   resolved symlinks, not just mount destinations or file permissions. Root 0700/
   0600 restricts access but never makes an exposed directory host-only.
2. Future webGUI/TUI/CLI Compose archive binds are literal
   `/opt/archives:/opt/archives`; webGUI manager `STORAGE_ROOT` is fixed there.
   No `STORAGE_ROOT` override may restore broad `/opt`. CLI allowed roots:
   `/opt/archives:/downloads`. Review downloads/drop, `/work`, `/config`, the
   configurable VFS cache host source (even read-only), volumes and legacy mounts.
3. Supervisor admission runs host-only `/usr/bin/docker inspect --type container
   --format '{{json .Mounts}}'` before exec: 10-second deadline, 1 MiB stdout cap,
   suppressed stderr and bounded owned-client kill/wait. Lexical and real-path
   bind/volume source overlap (ancestor/exact/descendant) refuses, regardless of
   read-only status. Missing/empty/malformed metadata, unsupported types, resolution
   errors, inspection failure, timeout and flood fail closed. Do not dump metadata
   or secrets to logs. Admission refusal means stay stopped, not bypass the guard.
4. Legacy `outputDir` outside the new roots requires an **explicit safe bind**
   outside all secret ancestors and a reviewed output-root configuration where
   applicable. Do not broaden `/opt` or mount a secret ancestor for compatibility.
5. Repeat effective-layout review after any administrator mount/symlink changes.
   Admission is point-in-time, NOT prevention if an administrator independently
   creates/reconfigures an unsafe container or bypasses supervision. The mandatory
   pre-provision exclusion requirement is independent of admission.

[Repair evidence](../.pi/reports/mount-boundary-repair.md) is future configuration
only: no existing container recreation, secret file, live mount inspection or
executor activation. CORE21495 is unchanged/not recertified.
[Frozen-gate](../.pi/reports/final-frozen-gate.md) SHA drift is commander-documented
banner edits to README/automation only; all 173 source/config/test fingerprints
matched in that gate. This reconciliation does not waive the true containment
bug or rewrite the historical HOLD. Broad native Linux validation remains pending.
