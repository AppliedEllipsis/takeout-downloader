# October 2026 — release journal

This is an append-only release history. Earlier journal entries remain in
[journal.md](journal.md); later evidence may supersede dated observations, not
rewrite them. Commands below are attributed to their originating reports. This
documentation lane did not repeat server/backend probes.

## 2026-10-13 — Audit and candidate reliability work

- `1f7f9af51c9b3ebbed0598087c11c5172070cf68` is the already-pushed **docs-only**
  audit/contract revision. It is not an implemented release/deployment revision.
- Initial offline baseline command, from the original D worktree:
  `python -m pytest tests/v3 tests/v2 -q -p no:cacheprovider` → **797 passed,
  6 skipped, exit 0**. [baseline-tests.md](../.pi/reports/baseline-tests.md).
- Candidate lanes added format-aware full archive verification, interrupted-stream
  resume classification, direct forced-read backend confirmation/receipts,
  cached-only bounded orchestration and explicit-account discovery/watch state.
  [Audit evidence](reliability-audit.md) links their exact commands, failed runs
  and limitations. Overlapping subtotals are not a whole-suite result.

## 2026-10-13 — Server preflight HOLD

[server-preflight.md](../.pi/reports/server-preflight.md) records bounded read-only
SSH/container/scratch probes, approximately 19:38–19:44 UTC and follow-up smoke:

- Old server `.v3` clean at `1b70ce2`; dirty main checkout preserved. No deployed
  checkout update, browser/container restart or service activation.
- Actual noninteractive encrypted-config probe:

  ```text
  /opt/storage.local_1/tools/rclone/rclone-v1.69.1-linux-amd64/rclone listremotes --config /opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf --ask-password=false
  ```

  **Exit 1**; raw output suppressed, no authorized unlock mechanism established.
  No copied/decrypted config or credentials.env provisioned; backend canary
  **not attempted**. No password guessing or unrelated-secret reuse.
- Host CDP docker-proxy connections reset; container-local CDP worked with
  Chrome/149.0.7827.155. Isolated disposable-target module smoke needed reauth,
  saw no verified identity/export metadata and restored the original page set.
  **Ready and pending export counts unknown**, not zero.
- Historical preflight host-venv/host-CDP worker plan is superseded by the approved
  container supervisor decision below; its measured observations remain history.

## 2026-10-13 — Move canonical release source off full D disk

[release-migration.md](../.pi/reports/release-migration.md): canonical source is now
`C:/Users/User/_projects/takeout-downloader-reliability`, branch
`feat/takeout-autopilot`. D original source remains unchanged. Migration manifests
record 274 byte-selected paths and before/after SHA256 evidence; only the
then-authorized test change differed after migration.

Exact reported broad gate:

```bash
python -m pytest tests/v3 tests/v2 -q
```

**971 passed, 6 skipped, 2 failed.** Failures were an obsolete escaped-error source
assertion and a Windows mount fixture. Parent reports subsequent fixture fixes
with focused **38 passed**. That focused update is not a final whole-suite result;
**final actual-checkout gate is not known to this documentation lane**. No commit,
push or deployment is inferred from migration/tests.

## 2026-10-13 — Owner-deferred activation and execution decision

[finish-release.md](../.pi/workflows/finish-release.md) freezes ownership and target:
host systemd → `tools/container_watch.py` → bounded **foreground docker exec** →
explicit non-root UID in existing `takeout-webgui`. Code `/work/.v3`; encrypted
host PRIVATE maps `/config/autopilot`; existing `/opt/manager-venv/bin/python` and
trusted copied PRIVATE/bin/rclone. No new port or browser/container restart.
Env forwarded by name, stdin heartbeat/EOF and scoped tree cleanup prevent orphan
worker/download descendants. Mandatory backend unlock preflight before launch.

Owner deferred rclone unlock and Google sign-in. Unit remains disabled/inactive;
no worker activation, real Google mint/download, password attempt or live canary.
No source-account deletion. Ready/pending counts remain unknown. Final implementation
release evidence is still a gate, not a predicted passing total or deployment.

## 2026-10-13 — Documentation-only reconciliation

Updated assigned current wiki/v3 pages, added [automation.md](automation.md) and
this October journal, and appended the container execution ADR to
[decisions.md](decisions.md). Prior ADR/journal history remains unchanged.
[release-docs.md](../.pi/reports/release-docs.md) records exact local docs checks,
CLI syntax/focused tests and scope. No code/service/config edit, delegation,
commit, push, deployment, server request or live data operation by this lane.

**Future steps, not executed:** final frozen-source verification and commander
integration; safe separately permitted clean `.v3` update/import/unit/UID checks;
later owner unlock/sign-in, authenticated identity/readiness proof, optional
retained backend canary and separate activation approval. Never describe these
as completed before their measured reports exist.


## 2026-10-13 — Core snapshot evidence arrived during documentation lane

[final-core-verifier.md](../.pi/reports/final-core-verifier.md) reports the immutable
snapshot `C:/Users/User/AppData/Local/Temp/takeout-release-snapshots/20261013T212824394139`.
Its exact whole-suite command was:

```text
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-o cache_dir=C:/Users/User/AppData/Local/Temp/takeout-release-verifier-20261013T212824394139/pytest-cache-exact" python -m pytest tests/v3 tests/v2 -q
```

**974 passed, 6 skipped, 1 warning, exit 0.** Compile/import/CLI and mandatory
adversarial gates passed within the report's scope. This frozen core snapshot
predates the separate supervisor/runtime-token additions: **the later merged
actual-checkout gate remains unknown here**. No backend unlock, Google request,
worker activation, commit/push/deployment or live archival success follows from it.

Docs-lane focused syntax/tests subsequently ran in C; initial selected-test
collection imported D discovery because existing `test_cli.py` prepends a
hardcoded D path. No source fix was made outside ownership. A clean-process focused
batch excluding that contaminating test passed **96 tests, 11 skipped**; Windows
skips are Linux supervisor containment tests, not evidence of POSIX stop behavior.
Exact commands/failure details are retained in release-docs.md. D was not edited;
bytecode writing/cache provider were disabled for these runs.

## 2026-10-13T22:00:42Z — CORE21495 deployed (supersedes future CORE-update notes)

[server-core-deploy.md](../.pi/reports/server-core-deploy.md): approved and deployed
SHA `21495d9ca4551b614d658178fb299854c2050bdb`; clean detached `.v3` fast-forward
`1b70ce2d80e2180a2b5eac7024e9fdecbe3df4fd` → approved SHA. Mode/owner remained
`775 ellipsis:ellipsis`. Container imports resolve `/work/.v3/autopilot` and
`/work/.v3/takeout2`; top-level/run/watch help passed. LF-normalized tracked Python
module fingerprints: **36 checked, 0 mismatches**.

Dirty main stayed `c0e3b8d32cf54c72c76850cbaaa944d26ac46e3b`, branch
`feat/internal-downloader`, with exactly six preserved entries:

```text
 M helpers/background.js
 M helpers/options.js
 M helpers/overlay.js
 M helpers/popup.js
?? .v3/
?? helpers.bak-1790097405/
```

Final bounded check found no watch/run/manager jobs/takeout2 worker. No worker
activation, browser/container/rclone restart, unlock, password, canary or export
work. Executor/unit installation remains deferred. Prior 974-pass/6-skip snapshot
boundary is unchanged; no later merged total is inferred. This docs lane only
records the originating deployment report, not a repeated server operation.

## 2026-10-13 — Security layout supersession, no credentials provisioned

Earlier root-env-inside-PRIVATE instructions are superseded: mandatory unit
EnvironmentFile is `/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env`,
root:root 0600 under root:root 0700, outside worker PRIVATE and browser binds.
PRIVATE stays worker-owned 20001:20001 0700 with no group/other bits; ancestor
traversal is separate. No key/credential file was provided or created. Current
unit Restart=on-failure, 30-second delay, five starts per 300 seconds; still
disabled/inactive. Alias repair permits reviewed network terminals and rejects
unsupported wrappers/local/memory/unknown: config-only, not connectivity/canary/
durability. Owner-deferred activation remains unchanged.


## 2026-10-13 — Mount-boundary repair documentation and frozen-gate reconciliation

Later evidence supersedes earlier “final gate unknown” and unconditional
“outside browser binds” statements above; dated history is preserved.
[final-frozen-gate.md](../.pi/reports/final-frozen-gate.md) remains a historical
HOLD. README/automation wiki SHA drift is commander-documented banner edits only,
with no mismatch in its 173 source/config/test files. This explains wiki drift,
not a waiver of its real HIGH containment bug: broad `/opt` mounts exposed the
mandatory root EnvFile parent. Root-only permissions did not establish host-only.

[mount-boundary-repair.md](../.pi/reports/mount-boundary-repair.md) records a separate
future-only correction: all three archive binds literal `/opt/archives`, no broad
`STORAGE_ROOT` override; CLI roots `/opt/archives:/downloads`. Bounded host-side
Docker inspect admission before exec rejects lexical/resolved source overlap,
including read-only/volume/symlink and malformed cases. **All effective mounts
must exclude `/opt/local_cache_crypt/takeout-autopilot-secrets` BEFORE provisioning
secrets.** Admission is point-in-time, not prevention of later unsafe administrator
container/mount/symlink changes. Legacy `outputDir` needs an explicit safe bind
outside secret ancestors, not a broad `/opt` compatibility fallback.

Measured originating report: Windows full C **1352 passed, 66 skipped**; nonroot
WSL focused admission/guardian/discovery **251 passed, no skips**. Broad native
Linux validation pending/HOLD: unavailable Linux pydantic_core blocks unrestricted
collection; dependency-scoped run had 1170 passed, 6 skipped, 2 failed; mint-only
failure remains reproducible, remote flood timing passed repeat. Four unchanged
raw-CRLF shell failures remain; normalized syntax checks do not repair raw files.
No totals were rerun or summed in this documentation-only lane.

No existing containers recreated, no secret file created, no live inspection,
server/auth/data/Git action or activation. CORE21495 unchanged/not recertified;
executor/unit installation and owner unlock/sign-in remain deferred. Assigned
wiki/v3 docs and append-only security ADR updated; link validation and scope are
recorded in [mount-doc-update.md](../.pi/reports/mount-doc-update.md). No final
approval or commit/deploy prediction follows from this documentation update.
