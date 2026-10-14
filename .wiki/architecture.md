# Architecture and source map

**Updated 2026-10-13.** Release source is the C checkout; activation is deferred.
[Execution contract](automation.md) supersedes older host-worker and
browser-native descriptions.

## Hybrid data flow

```text
verified account manage page -> discovered export UUID / complete part list
  -> pending: poll only; ready: bounded watcher pass
  -> browser mint (human reauth boundary; cached URLs first)
  -> HTTP transfer (cookie jar, validated Range/strong validator)
  -> encrypted staging -> format/size/CRC validation + computed SHA256
  -> direct rclone upload -> exact size + forced backend SHA256 + size
  -> ledger receipt -> done -> optional staging cleanup
```

The browser authenticates/mints, not bulk-transfers. Cookie replay is used for
file-host **transfer**, not to replace interactive Google authentication. No
Fetch interception, password retry loop or human-download sweep is authorized.
Mint cancellation is scoped to the automation-owned frame/download GUID;
cancellation refusal is an explicit blocker, not permission to cancel human work.

## Execution boundaries

```text
host systemd unit (disabled now)
  -> tools/container_watch.py, bounded foreground docker-exec supervisor
     -> existing takeout-webgui, explicit non-root UID
        /opt/manager-venv/bin/python, code /work/.v3
        PRIVATE mapped as /config/autopilot
        CDP http://127.0.0.1:9222 inside container
```

[CORE21495 is deployed](core-release.md); executor/unit installation is separate.
Host-only root credentials must be outside worker-owned PRIVATE and **all
effective container mounts before provisioning**, not merely `/work` and
`/config`; see [protected layout](credentials.md). Alias/crypt/chunker preflight permits only
reviewed network terminals, refusing unsupported wrappers/local/memory/unknown.
It is config-only, not connectivity/canary/durability evidence.

Backend-config unlock preflight must succeed **before watcher launch**. Supervisor stdin
heartbeat/EOF and scoped process-tree cleanup cover lost host supervision;
killing only the docker client is insufficient. No new port, host-CDP repair,
browser restart or container recreation is authorized for this release.

## Integrity and receipt semantics

| Evidence | Means | Does not mean |
|---|---|---|
| Exact expected size | Complete-length loose part (`SIZE_OK`) | Authenticity/content checksum |
| ZIP directory + every member CRC | Archive members read/decompressed and validated | Trusted source SHA256/product coverage |
| TAR full reads, gzip CRC/ISIZE | Declared stream validated; invalid tails rejected | Source authenticity |
| Computed SHA256 | Digest of observed bytes; detail states whether compared | Trusted equality merely because state is `HASH_OK` |
| SHA256 matched trusted expectation | Equality to supplied expected digest | Correctness of an untrusted expectation |
| Confirmed backend receipt | Exact path, size, staged/backend SHA256 and confirmation time | Permanent immutability/replication/retention |

ZIP CRCs cannot be disabled with compatibility `crc_check=False`. Declared formats
must match bytes; obvious login/error HTML is rejected, but legitimate known-size
HTML/mail/media can be accepted. Unknown-size loose data needs a trusted digest.
Hash-read failure is UNVERIFIED, never retained success.

Remote mode does not treat FUSE fsync or size equality as durability. It uses
`copyto --ignore-existing`, forced `hashsum SHA256 --download` and exact size
checks. Existing different objects fail closed, even same-size/different-content.
Matching persistent done receipts avoid re-reading multi-GB objects on every
one-part pass; they are historical confirmation, **not a fresh backend audit**.
Changed destination/missing receipt requires verification. Use a single writer:
rclone has no generic transactional create against arbitrary other writers.

## Source map

| Path | Responsibility |
|---|---|
| `autopilot/__main__.py` | run/report/watch CLI and run exit codes |
| `autopilot/watch.py`, `discovery.py` | Identity, disposable targets, pending polling, atomic status/OS lock, fairness/backoff |
| `autopilot/run.py` | Cached-only passes, part/disk limits and completion gates |
| `autopilot/mint.py`, `scrape.py` | Metadata and browser-owned URL earning/cancellation |
| `autopilot/cdp.py`, `ws_transport.py`, `jar.py` | CDP and cookies/user agent |
| `autopilot/transport.py`, `verify.py` | Validated resume, classified partial failures, full format checks |
| `autopilot/remote.py`, `mover.py` | Bounded backend subprocesses; legacy verified native-local copy |
| `autopilot/ledger.py`, `report.py` | Local SQLite progress/receipts; evidence-limited reports |
| `tools/container_watch.py` | Approved lifecycle supervisor; execution-lane release artifact |
| `deploy/` | Unit/example templates, not installation/activation evidence |
| `requirements-autopilot.txt`, `takeout2/` | Runtime dependency list and required legacy contracts package |
| `tests/v3/`, `tests/v2/` | Offline tests; opt-in public-network tests remain skipped |
| `.pi/workflows/finish-release.md`, `.pi/reports/` | Frozen ownership and measured evidence |

Legacy manager/browser infrastructure remains shared. Do not run legacy and v3
workers against the same browser/ledger/staging concurrently. Preserve the dirty
server main checkout; release updates target only its clean `.v3` worktree.

## Mount admission and archive compatibility

Future Compose archive sources/targets for webGUI, TUI and CLI are fixed literal
`/opt/archives`; no broad `/opt` bind or inherited `STORAGE_ROOT` override. webGUI
manager `STORAGE_ROOT` is fixed `/opt/archives`; CLI allowed roots are
`/opt/archives:/downloads`. Other mounts still require effective-layout review.

Before Docker exec, the host supervisor inspects every effective mount with a
10-second deadline and 1 MiB stdout cap, suppressed stderr and bounded owned-client
cleanup. Bind/volume sources are checked lexically and after real-path resolution
against `/opt/local_cache_crypt/takeout-autopilot-secrets`; ancestor, exact and
descendant overlap is rejected even read-only or through symlinks. Missing/empty/
malformed metadata, unsupported types, failed inspection, resolution error, timeout
and flood fail closed. Environment values are not printed or put in exec argv.

This is point-in-time admission, not prevention when an administrator later
creates/reconfigures an unsafe container or changes symlinks. Effective mount
exclusion is REQUIRED BEFORE provisioning secrets. Existing containers were not
recreated; synthetic layout evidence is not live inspection. Legacy `outputDir`
outside the new roots needs an explicitly reviewed safe bind outside all secret
ancestors, not restoration of `/opt`. See the
[security ADR](decisions.md#2026-10-13--effective-mount-exclusion-before-secrets-future-compose-only)
and [repair evidence](../.pi/reports/mount-boundary-repair.md).
