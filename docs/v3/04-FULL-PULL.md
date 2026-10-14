# Full pull — FUTURE owner-gated procedure

**Updated 2026-10-13. NOT executed for this release.** Owner deferred rclone unlock,
Google sign-in and worker activation. No real mint/download/password attempt or
live backend canary. Pending/ready export counts unknown. Do not copy these
commands into a live session until separately authorized.

## 1. Establish the gates, not assumptions

Follow [operations](03-OPERATIONS.md) and [automation](../../.wiki/automation.md):
final code gates, safe `.v3` revision/imports, encrypted storage/permissions,
non-root UID, container venv, private config, authorized unlock preflight and
verified authenticated account identity. No new port/browser restart. Do not run
manual `run` alongside the watcher/legacy downloader; single archival writer.

Discover actual available exports after owner sign-in. Compare **all** expected
parts against existing receipt/integrity evidence before spending attempts.
Decode safe filenames/aliases; matching size alone is not content/backend proof.
An empty/login/pending page is never completion and never a count of zero exports.
Quotas/counter behavior are dated telemetry, not a promise of free transfers.
Do not guess expiry from cached URL age or remint a valid URL merely to test it.

## 2. Separate URL earning from cached-only transfer

Minting needs an authenticated browser window; byte transfer uses cached file-host
URLs/cookies and may outlast that window. September's observed window/resume costs
are historical, not guaranteed current lifetimes. Refuse untokened/auth-challenged
mint work rather than doing an untracked redirector probe. Only automation-owned
browser downloads may be cancelled; human downloads/tabs remain untouched.

The watcher handles separate mint batches and bounded cached passes automatically.
Production launches only via the approved supervisor/unit described in
[automation](../../.wiki/automation.md); do not bypass its preflight/heartbeat
with a manual or detached exec. For understanding the underlying one-export CLI,
the following is an **argument reference, not a production launch recipe**.
Paths are container paths and variables are placeholders, never real identities:

```bash
# REFERENCE ONLY, NOT EXECUTED. Future use needs approved lifecycle supervision.
# Never put cookies, passwords or reauth tokens in argv.
/opt/manager-venv/bin/python -B -m autopilot run \
  --archive-id "$ID" --account "$LABEL" --work-url "$WORK_URL" \
  --ledger /config/autopilot/ledger.sqlite \
  --staging "/config/autopilot/staging/$LABEL/$ID" \
  --archive "/opt/archives/google-takeout/$LABEL/$ID" \
  --archive-remote "archives:google-takeout/$LABEL/$ID" \
  --rclone-binary /config/autopilot/bin/rclone \
  --rclone-config /config/autopilot/rclone.conf \
  --remote-timeout 21600 --staging-reserve 107374182400 \
  --mint-only --json
```

Authorized env is supplied privately through the supervisor by **name only**;
`RCLONE_CACHE_DIR` is sanitized to `/config/autopilot/cache`. The released
supervisor currently launches `watch`, not arbitrary `run` commands. No manual
run bypass is approved here; long work requires the foreground guardian with
heartbeat/scoped tree cleanup, not an unguarded docker client.

Mint-only normally returns `incomplete`, exit **1**, when URLs are earned but parts
are not archived. Inspect status/error: do not use `&& transfer` or mask every
failure with `|| true`.

## 3. Bounded transfer-pass argument reference (FUTURE)

At the engine level the same arguments above replace `--mint-only` with:

```text
--cached-transfer-only --max-parts 1 --verify-hash --json
```

The explicit `--cached-transfer-only` matters: an ordinary run may scrape/mint;
it is **not** inherently transfer-only. Cached-only reconstructs the full known
ledger list and cannot call a cached subset complete. It may still need browser
cookies, but verified staged bytes can upload without browser/remint.

Repeat only after inspecting the classified result and backoff/storage budget.
Cleanup is an optional additional `--clean-staging`; never add it before accepting
receipt semantics. Failed confirmation retains staging. Validated partial streams
resume with saved strong validator; corrupt complete bytes are quarantined.

| Run exit | Action |
|---|---|
| 0 complete | Expected part list passed archival gates; verify evidence scope below |
| 1 incomplete/failed | Read sanitized status/error; bounded retry only if appropriate |
| 2 needs_reauth | Owner acts later; preserve cached/staged work, no password loop |
| 3 expired / 4 quota | Terminal; owner requests new export |

## 4. Completion evidence, not a directory listing

Require exact expected part count/indices and sizes, full applicable archive
checks, staged SHA256 and confirmed exact backend path/size/SHA/time. ZIP validation
reads every member CRC; TAR/gzip streams are fully read. Known-size loose mail/media
has size-level validation unless a trusted expected digest is supplied. A computed
SHA256 is not source-authenticity comparison.

Backend SHA is forced with rclone `hashsum SHA256 --download`, bracketed by exact
size checks; FUSE fsync/readback or `ls` cannot substitute. Same-name mismatched
objects fail closed, never overwrite. Stable ledger receipts are historical
confirmation reused for throughput, **not a fresh backend audit** and not a
provider retention guarantee.

A future `python -m autopilot report --archive-id ID --ledger PATH` reports local
accounting only; optional `--archive-dir` may count size-matching prior-tool files,
not CRC/backend evidence. No source-account deletion is authorized. Record exact
executed commands, loaded revision, sanitized outcomes and failed/unknown evidence
before any claim of a new export complete. This release has no such live claim.

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
