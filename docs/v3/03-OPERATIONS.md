# v3 operations — release HOLD and future runbook

**2026-10-13.** Owner deferred unlock, Google sign-in and worker activation.
Unit must stay disabled/inactive. No real mint/download/password attempt/backend
canary; ready/pending counts unknown. Commands marked FUTURE have **not** been
executed by this documentation lane.

## Runtime authority

[Wiki automation](../../.wiki/automation.md) is the complete path/permission and
stop/recovery contract. [Credentials](../../.wiki/credentials.md) records names
and pointers only. [Server preflight](../../.pi/reports/server-preflight.md)
records dated actual probes, not current deployment success.

| Component | Approved target |
|---|---|
| Canonical workstation source | `C:/Users/User/_projects/takeout-downloader-reliability`; D original unchanged (full disk) |
| Server code | `/opt/local_cache_crypt/_projects/takeout-downloader/.v3` → `/work/.v3`; preserve dirty main repo |
| PRIVATE | Host repo `config/autopilot` → `/config/autopilot` inside existing `takeout-webgui` |
| Worker | Existing `/opt/manager-venv/bin/python`, explicit non-root container UID |
| Supervision | Host systemd → `tools/container_watch.py` → bounded foreground docker exec |
| rclone | Trusted `PRIVATE/bin/rclone` + encrypted `PRIVATE/rclone.conf`, container-mapped paths |
| CDP | Existing container-local `http://127.0.0.1:9222`; no new listener/forward |
| Archive authority | Direct `archives:google-takeout`; FUSE `/opt/archives` is only a view |

Earlier host-worker/PRIVATE-venv and host-path JSON templates are superseded.
Host `/config` does not exist. Existing venv must import both `autopilot` and
`takeout2` and the websocket runtime dependency. Templates are not installation
proof. Browser/extension reload or restart is **not** a release step.

## Gates before any future activation

1. Final actual-checkout tests/compile/import/CLI and independent supervisor/identity
   evidence, secret review and immutable approved source. Repair evidence is
   Windows 1352 passed / 66 skipped and focused nonroot WSL 251 passed; broad
   native Linux validation remains pending (see the repair report below).
2. [CORE21495 deployed](../../.wiki/core-release.md) at 22:00:42 UTC: clean
   `.v3`, 36 fingerprints/0 mismatches, dirty main six entries preserved.
   Executor/unit installation remains deferred, not inferred from CORE deployment.
3. Verify encrypted mount, explicit numeric container UID, all ancestor traversal,
   private config readability and state/ledger/staging/cache/tmp writability. Keep env
   root-0600, private files/dirs and umask 0077; never broaden secrets to fix access.
4. Check unit syntax and stopped/disabled state. Unit must supervise a foreground
   exec; heartbeat/EOF and scoped tree cleanup must prevent orphan downloads.
5. Current supervisor preflight is bounded noninteractive encrypted `config dump`
   and alias-chain validation, **not network/backend access**; no canary or remote
   credential-validity proof follows from it. **Later owner authorization:** unlock credentials and Google interactive sign-in;
   verified account DOM/URLs/readiness. Mandatory bounded noninteractive backend-config
   unlock preflight must succeed before watcher launch. No password guessing.
6. Separate launch approval. Optional tiny retained canary needs its own permission;
   none has run. It proves storage mechanics, not Google export completion.

Production budget: remaining/restart bytes + 100 GiB reserve (`107374182400`) +
5 GiB floor (`5368709120`) on the shared encrypted disk. Browser downloads and
VFS cache growth count too. Recheck per part; no root-disk fallback.

## FUTURE inspection examples — no activation

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server \
  'systemctl is-enabled takeout-autopilot-watch.service; systemctl is-active takeout-autopilot-watch.service'
ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server \
  'df -h /opt/local_cache_crypt; findmnt -T /opt/local_cache_crypt'
```

For local CLI syntax, `python -B -m autopilot run --help` and
`python -B -m autopilot watch --help` do not contact Google/backend. Exact executed
local commands/results: [release-docs.md](../../.pi/reports/release-docs.md).
`watch --once` is **not** read-only: it can discover, mint, transfer and upload.
`--once --dry-run` validates config only and does not prove backend/Google readiness.
Do not run a live tick during this hold.

## FUTURE stop and recovery

```bash
# Host: scoped worker stop, NOT a browser/container stop.
sudo systemctl stop takeout-autopilot-watch.service
sudo systemctl disable takeout-autopilot-watch.service
systemctl is-active takeout-autopilot-watch.service
systemctl is-enabled takeout-autopilot-watch.service
```

Prove this worker tree/owned transfers are gone; killing the docker client alone
is insufficient. No `pkill -f python`, Chromium restart, broad download sweep,
mount unmount or deletion of state/ledger/partial/quarantine evidence. Never remove
a lock file to evade an active OS lock. Retain sanitized status and progress.

- **Backend/preflight failure:** keep stopped, retain staging; owner supplies unlock
  later. No FUSE fallback or decrypted/public config.
- **Reauth/identity blocked:** owner signs in/verifies account later; no automatic
  password helper or guessed label/user ID. Cached valid work remains useful.
- **Storage/mount blocked:** restore encrypted mount/headroom before retry; preserve
  partials/receipts. Direct backend failure never authorizes staging cleanup.
- **Network interruption:** resume with saved strong validator; changed remote/auth
  failure is explicit, no arbitrary-age remint.
- **Corrupt archive:** quarantine and inspect; size-only adoption cannot repair CRC.
- **Expired/quota/Google failed:** terminal; new owner-created export required.
- **Restart:** reuse local ledger/state/receipts and single OS lock; no concurrent
  legacy/v3 writers or arbitrary remote writers. Start/restart is not approved now.

A ledger-only report needs no browser; optional `--archive-dir` size accounting
is not a fresh integrity/backend audit. Do not delete source data based on it.
See [run contract](02-RUN-INTERFACE.md) for exit codes and
[future full pull](04-FULL-PULL.md) for per-export command templates.

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
