# Server preflight — HOLD, no deployment

## Decision

**Primary gates failed: rclone unlock credentials unavailable, host CDP forwarding broken, fresh Takeout views require reauthentication, and candidate source is not frozen.** No service launch is authorized. Pending/new export counts are **unknown**, not zero.

Host observations were taken on **2026-10-13, approximately 19:38–19:44 UTC**; additional module-based discovery smoke and regression testing followed. All remote commands used `ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server`, an outer timeout, and bounded remote/subprocess/network operations.

No checkout was updated; no service/user/browser/container was installed or restarted; no Google parts were minted/downloaded; no account was signed in or selected through the account switcher; no existing ledger/archive object was changed; no data was deleted. Secret values, numeric account IDs, emails, archive IDs and URL token values are omitted. No remote mount was scanned recursively.

## Host and revision evidence

| Item | Sanitized result |
|---|---|
| Root capability | SSH UID 1001; sudo/docker membership; `sudo -n true` and root Python probes succeed |
| Host main repo | `/opt/local_cache_crypt/_projects/takeout-downloader`, HEAD `c0e3b8d32cf54c72c76850cbaaa944d26ac46e3b`; 6 dirty entries, including 4 tracked helper JS files; preserved |
| Deployed `.v3` | HEAD **`1b70ce2d80e2180a2b5eac7024e9fdecbe3df4fd`**, clean when checked; old revision premise confirmed |
| New candidate | Source worktree HEAD **`1f7f9af51c9b3ebbed0598087c11c5172070cf68`**; last status 19 tracked dirty / 19 untracked; watcher/deploy changes are not an immutable approved revision |
| Container | `takeout-webgui`, `takeout-webgui:latest`, running since `2026-08-06T23:19:09.060562845Z` |
| Encrypted volume | `/opt/local_cache_crypt`, `/dev/mapper/cache_crypt`, XFS rw; approximately 300G total / 7.0G used / 293G available |
| Archive mount | `/opt/archives`, fuse.rclone; displayed source `archive_chunked:`. Actual mount argv uses **`archives:` → `/opt/archives`**, VFS `full`, shared cache `/opt/local_cache_crypt/rclone_vfs` |
| rclone | v1.69.1; `/opt/storage.local_1/tools/rclone/rclone-v1.69.1-linux-amd64/rclone` |
| Python | Host 3.12.3; ensurepip absent; initially no host websockets/pip/uv/virtualenv |
| Watch service/user | `systemctl is-active takeout-autopilot-watch.service`: exit 4/inactive; dedicated service user does not exist; neither created |

The container binds the encrypted repo/config, shared VFS cache read-only, and `/opt/archives` rw. Mounts and lifecycle were unchanged. FUSE-reported capacity is not backend durability evidence.

## Encrypted credential gate / canary

Existing config: `/opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf`.

A bounded header test found `RCLONE_ENCRYPT_V` within the first 1 KiB, after a prefix; **contents were never logged**. Ciphertext mode is 0644, on root ext4; no symlink ancestors. Original encryption and permissions were unchanged.

Permitted inspection found:

- No `RCLONE_*` environment **variable names** in the mount process, parent shell, PID 1, or sudo probe environment.
- Mount argv has `--config` and `--cache-dir`, but no `--password-command`.
- No authorized password-command or secure credential-file pointer was established. Other container secrets were not reused.

Actual bounded probe, stdin `/dev/null`, stdout/stderr captured and suppressed:

```text
/opt/storage.local_1/tools/rclone/rclone-v1.69.1-linux-amd64/rclone listremotes --config /opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf --ask-password=false
```

Result: **exit 1**, noninteractive encrypted config not opened. No password argv, secret dump, memory scraping, or encryption downgrade was used.

**No `credentials.env` or copied/decrypted rclone config was provisioned. Backend canary NOT ATTEMPTED.** If later authorized credentials work, permit only one unique tiny retained object under `archives:google-takeout/_autopilot_canary/`, checked directly with size/stat and `hashsum SHA256 --download`, always `--ask-password=false`. No destructive cleanup or real-export mutation.

## Live discovery smoke with current local modules

Host `127.0.0.1:9222` has a docker-proxy listener, but both `/json/version` and `/json/list` repeatedly raise **ConnectionResetError**. Container-local CDP works: **Chrome/149.0.7827.155**. No forwarding repair was attempted.

At the owner's additional request, current local `autopilot` and `takeout2` source was streamed into **isolated scratch**, `/config/autopilot-discovery-smoke` inside the existing container (host encrypted config volume). This is not the deployed `.v3`, not a service installation, and not production configuration. The smoke imported current `autopilot.watch.Account`, `discovery.automation_session`, `SNAPSHOT_JS`, `safe_url`, and `discover_pages`; only the session/snapshot path was usable because authentication prevented verified account discovery. No Watcher, mint, transfer, or account-selection routine ran.

Source hashes identifying the smoke:

```text
autopilot/discovery.py  2cf224a63e24836e558acb5ed5ebdd831c63b94964d70b94f290073276a143e7
autopilot/watch.py      91e881c83f4198717a73bf28e65e4d95e64a1828088689f5c71990e1b13aeed1
```

The module created **one disposable automation target** and cloned the existing human Takeout **archive-view URL**, without reading/navigating the human target or selecting another account. Its request URL had **no query keys**. The new view redirected to authentication. Only query-key names were returned:

```text
continue, dsh, flowEntry, flowName, followup, osid, passive
```

Current module `SNAPSHOT_JS` on that real DOM reported:

- `challenged=true`; **0 independent identity witnesses**;
- **0 archive links, 0 part elements, 0 pagination links**;
- no failed/expired/pending marker observed;
- attribute counts: `data-user-id=0`, `data-download-uri=0`, `data-identifier=1`, `data-email=1`, `data-authuser=1`.

The latter three attributes are on the **authentication DOM**, not verified Takeout export markup; their values were never emitted. No archive-link or part-query key sets were available.

**Exact live blocker: `needs_reauth`. Actual ready exports: UNKNOWN. Actual pending exports: UNKNOWN.** Zero visible export elements on a login view must not be interpreted as zero exports. Authenticated DOM compatibility cannot be certified from this smoke.

Current `safe_url` accepts only `user`, `page`, `pageToken`; it requires a matching numeric `user`. **Neither `rapt` nor `authuser` was observed as a URL query key in a live management/archive link**, so their legitimate authenticated occurrence remains untested. The existing queryless archive URL cannot itself supply verified identity or satisfy the required `user` binding. The authentication redirect's keys/host are outside the allowlist; challenge detection correctly blocks before account execution. No guessed user or stripped token was used to force acceptance.

Source behavior: an identity-less **manage** page fails `account identity unverified`. An identity-less pending **archive** may be classified pending only after independently verified manage identity and cannot become ready without attested parts. No authenticated pending-only page was available to test live.

Tab closure verification initially raced asynchronous target removal. A repeat module smoke with bounded polling confirmed **3 original pages before/after, original target set exactly restored, own disposable target absent**. No human tab was closed or altered.

## Identity and active-download aggregates

Three specifically selected local ledgers were opened using SQLite `mode=ro` and `PRAGMA query_only=ON`. Historical metadata provides **one unambiguous label/ID association**; identifiers/label are not recorded here. That account's fresh manage view also required reauthentication. Therefore there are **0 freshly verified identities**, no complete account inventory, and no deployable account config. No `watch.json` was created.

- Browser downloads read through a separate disposable page: **12 items, 10 complete, 2 cancelled, 0 in-progress/paused**. Enum meanings verified from Chrome's own local downloads module.
- Selected local `Downloads`: three ZIPs, no top-level partial/crdownload/aria2 files. No archive-tree traversal.
- No aria2c/wget/curl process observed; one rclone mount active. Generic Python processes were not assumed to be transfers.
- Existing selected ledgers contain 19, 2, and 68 `pending` **part rows**. Those are not current Google export counts and were not changed.

## Code/import/test readiness

Earlier allowed scratch: `/opt/local_cache_crypt/takeout-autopilot-preflight`, root-owned **0700**, containing `venv/` and `source/`. It is separate from production and was retained, not deleted.

The dedicated host venv was completed without apt/global changes: `venv --without-pip`, pip 25.2 universal wheel fetched from PyPI with published SHA256 verification, then venv-only installation of `websockets>=13,<17`. Result: Python 3.12.3 / websockets **16.1.1**.

Real scratch imports and `python -B -m autopilot watch --help` passed **after including `takeout2`**. Copying only `autopilot` first failed with `ModuleNotFoundError: takeout2`; deployment must include both packages. No live watcher run/dry-run was performed.

The earlier 36-file scratch source fingerprint was `eb09edc60e9b657f53ce2e83206b10cec450d0df8897e3078036414db22438a0` (SHA256 over sorted relative path + NUL + per-file SHA256 + newline). Subsequent comparison detected concurrent changes to `jar.py`/`run.py`. Imports do not certify a later revision.

Focused local test command:

```bash
timeout 100 python -B -m pytest -q -p no:cacheprovider tests/v3/test_discovery.py tests/v3/test_remote.py tests/v3/test_watch.py tests/v3/test_orchestration_reliability.py
```

First run: **93 passed**. Later run after concurrent source/test changes: **93 passed / 3 failed** — all Windows `test_owned_spawned_descendant_terminated` modes (`timeout`, `flood`, `parent-exit`), owned descendant surviving cleanup. Linux behavior of these new tests is unverified. No fixes or rollback of others' changes were attempted.

## Frozen production paths — authoritative

The owner's updated deploy unit/example and requirements were reread. Use only:

```text
CODE=/opt/local_cache_crypt/_projects/takeout-downloader/.v3
PRIVATE=/opt/local_cache_crypt/_projects/takeout-downloader/config/autopilot
PRIVATE/venv/  PRIVATE/state/  PRIVATE/staging/  PRIVATE/cache/
PRIVATE/ledger.sqlite  PRIVATE/watch.json
PRIVATE/credentials.env  # root-private 0600; authorized unlock material only
PRIVATE/rclone.conf     # authorized copy of original ENCRYPTED ciphertext only
```

Service user/group `takeout-autopilot`, create only in deployment phase. WorkingDirectory `.v3`; writable sandbox only `PRIVATE`; encrypted mount required; `KillMode=control-group`. Systemd reads root-private env; do not weaken file permissions to source it as the service user.

Frozen config: `archives:google-takeout`, local view `/opt/archives/google-takeout`, actual rclone binary above, copied encrypted config under `PRIVATE`, host loopback CDP 9222. **100 GiB reserve** (`107374182400`) plus **5 GiB floor** (`5368709120`). Current reported free space exceeds that protected budget, but shared VFS growth must be rechecked. **Production provisioning stopped because identity/credential gates are unavailable.**

## Sanitized command record and gated deployment plan

Significant actual probe commands (remote outer timeout 30–140 seconds, inner 20–120; Python network waits 5–10 seconds):

```text
id; sudo -n true; python3 --version
 git -C <host repo or .v3> rev-parse HEAD
 git -C <host repo or .v3> status --porcelain
 docker inspect takeout-webgui                   # selected aggregate fields only
 df -h /opt/local_cache_crypt /opt/archives
 findmnt -T <selected path> -o TARGET,SOURCE,FSTYPE,OPTIONS
 ss -ltnp 'sport = :9222'
 systemctl is-active takeout-autopilot-watch.service
```

Aggregate scripts were streamed via these exact bounded forms, without secret arguments:

```bash
timeout 45 ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server 'sudo -n timeout 35 python3 -'
timeout 80 ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server 'timeout 65 docker exec -i -w /config/autopilot-discovery-smoke takeout-webgui python3 -B -'
```

Module smoke used `asyncio.wait_for(main(),50)`, `automation_session`, management-only `Page.navigate`, `SNAPSHOT_JS`, query-key-name aggregation, and scoped target cleanup. A repeat used a 30-second asyncio bound plus 15 × 0.2-second closure polls. No Fetch interception/download link navigation.

**Future plan — none executed:**

1. Commander freezes an approved revision, resolves/test-gates current failures, and authorizes a clean `.v3` update only; preserve dirty main repo.
2. Owner supplies secure authorized rclone unlock mechanism, separately repairs host CDP/reauthentication, then repeat read-only module discovery and verify labels/identities/query compatibility. Do not guess or silently strip `rapt`/`authuser`.
3. Deployment phase creates service identity and frozen private directories, checks parent traversal permissions, builds `PRIVATE/venv`, and runs bounded `PRIVATE/venv/bin/python -m pip install -r CODE/requirements-autopilot.txt`.
4. Provision private verified `watch.json`, root-0600 env, and byte-identical **encrypted** config copy; verify encryption/hash without logging contents. Existing ledgers remain untouched; use the new private ledger.
5. Under production identity, validate config/imports, bounded noninteractive credentials, the permitted retained direct-backend canary, disk reserves, and `systemd-analyze verify`/sandbox access.
6. **Wait for a separate commander launch gate** before unit installation, daemon-reload, enable/start. No service launch command was run.

Outstanding blockers: credentials, host CDP, authenticated identities/export counts/DOM/query compatibility, unfrozen source plus regression failures, and production-phase access/sandbox validation.
