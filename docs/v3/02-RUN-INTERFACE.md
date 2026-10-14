# v3 run interface — current contract

**Updated 2026-10-13.** Read from candidate `autopilot/run.py` and
`autopilot/__main__.py`; supersedes the September-only frozen field list.
Activation/sign-in/unlock remain owner-deferred. Commands below describe interfaces,
not authorization to run them against Google or the backend.

## CLI surface

```text
python -m autopilot run --archive-id ID --ledger PATH --staging DIR --archive DIR [options]
python -m autopilot report --archive-id ID --ledger PATH [--account LABEL] [--archive-dir DIR] [--json]
python -m autopilot watch --config PATH [--once] [--dry-run]
```

| Run option | Default / meaning |
|---|---|
| `--archive-remote REMOTE:DIR` | None; **full export directory**, enables direct checked backend upload, not FUSE copy |
| `--rclone-binary PATH` | `rclone`; production container `/config/autopilot/bin/rclone` |
| `--rclone-config PATH` | None; production encrypted `/config/autopilot/rclone.conf`, never password |
| `--remote-timeout SECONDS` | 21600; finite positive per-command timeout |
| `--staging-reserve BYTES` | 0; nonnegative shared-cache reserve; production uses 107374182400 |
| `--cached-transfer-only` | False; ledger part-list reconstruction, no scrape/mint; may still read browser cookies for unfinished transfer |
| `--work-url URL` | Derived archive URL if absent; explicit account-selected URL, no persisted reauth token |
| `--account LABEL` | Optional storage/report label; not identity proof |
| `--cdp-http URL` | `http://127.0.0.1:9222`, **inside container** in production |
| `--max-parts N` | None; bound part work, not whole-export completion |
| `--mint-only` | False; earn URLs/cache and return incomplete while bytes remain; no transfer |
| `--clean-staging` | False; opt-in cleanup only after confirmed backend or verified native-local copy |
| `--verify-hash` | Compute SHA256 in addition to format validation; does not supply a trusted expected digest |
| `--allow-unmounted-archive` | DEV ONLY; bypass legacy destination-mount assertion, not integrity/durability gates |
| `--json` | Emit outcome JSON rather than markdown |

Run CLI headroom: `AUTOPILOT_MIN_HEADROOM` defaults to **5368709120** (5 GiB).
Legacy mover tuning: `AUTOPILOT_MOVE_TIMEOUT`, `AUTOPILOT_MOVE_STALL`. Production
watcher has its own explicit config budgets; CLI reserve default 0 is not that
production policy. `--archive` remains the view/native-local destination argument
in remote mode; durability authority is `--archive-remote`.

## Public Python API

```python
async def run_once(cfg, *, session_factory=None, http_client=None, now=None): ...
def run_once_sync(cfg, **kw): ...  # asyncio.run wrapper
```

`RunConfig` required fields: `archive_id`, `ledger_path`, `staging_dir`,
`archive_dir`. Optional fields/defaults:

```python
account = None
cdp_http = "http://127.0.0.1:9222"
require_mount = True
max_parts = None
verify_hash = False
settle = 20.0
work_url = None
mint_only = False
min_headroom = None          # library only; CLI supplies 5 GiB
move_timeout = None
move_stall = None
clean_staging = False
archive_remote = None
rclone_binary = "rclone"
rclone_config = None
remote_timeout = 21600
staging_reserve = 0
cached_transfer_only = False
```

`RunOutcome`: `archive_id`, `status`, `parts_expected`, `parts_done`,
`bytes_moved`, `report_markdown`, `error`, `notes`. Normal operational blockers
return classified outcomes/reports; invalid setup may still be refused by CLI.
No error must expose signed URL paths/queries, cookies or tokens.

## Completion and integrity contract

- Full expected part-list evidence is required. Empty pages and a cached minted
  subset are never COMPLETE. Cached-only reconstructs all ledger rows, including
  done/no-URL/unminted rows; legacy missing expected-count evidence stays unknown.
- Per part: valid cache/mint → validated transfer → local format/size/CRC and SHA
  → backend confirmation or verified native-local copy → receipt/done → optional
  cleanup. Per-part capacity/mount checks stop safely; failed uploads retain staging.
- ZIP validation reads every member CRC even with compatibility `crc_check=False`.
  TAR/gzip streams are fully read; declared formats must match bytes. Known-size
  loose HTML/media/mail can pass size validation; login/error HTML is refused.
- Computed SHA256 is **not trusted comparison**. `HASH_OK` can represent hash
  computation after other checks; read detail/expected digest, not enum alone.
  Unknown-size loose bytes need a trusted expected digest. Failed hash reads are
  UNVERIFIED; errors/truncated streams do not become complete by size alone.
- Direct remote mode uses bounded noninteractive rclone with `--ask-password=false`,
  exact size checks and `hashsum SHA256 --download` matching staged SHA. Different
  existing objects are preserved/refused. Receipt records exact path/size/SHA/time;
  matching stable receipts are reused, not re-audited each tick. Subsequent external
  mutation and provider retention are outside that historical receipt.
- Native-local mode verifies copied bytes. FUSE fsync/size never authorizes durable
  completion/cleanup. Directory indexing is a planning/report hint, not a receipt.
- Transient stream failures preserve partial bytes/strong validator. Cached URL
  invalidation needs explicit auth/remote-change evidence, not a timer. Corrupt
  byte-complete files are quarantined safely, not silently adopted.

## Exit codes and report limits

| Run code | Status | Meaning |
|---|---|---|
| 0 | `complete` | All expected parts passed the run's archival gates |
| 2 | `needs_reauth` | Owner action; preserve usable cached work |
| 3 | `expired_unrecoverable` | Terminal; new export required |
| 4 | `quota_exceeded` | Terminal; new export required |
| 1 | `incomplete`, `failed`, other | Work remains or failed; inspect sanitized error |

Successful mint-only with unfinished parts exits **1**, not 0. Never blindly chain
`--mint-only && transfer` or mask every error with `|| true`.

`report` uses the local ledger, requires no browser/network by default, and exits
0 only when its expected-part accounting is complete, otherwise 1. Optional
`--archive-dir` indexes filenames/sizes and can count prior-tool parts as held;
that informational count is **not fresh CRC/backend durability proof**. A report
or historical done receipt is not authorization to delete source-account data.
`watch` exits are its own interface, not these per-export status codes.

## Protected environment and release boundary

Mandatory unit `EnvironmentFile` is host-only
`/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env` (`root:root 0600`),
under `root:root 0700`, outside worker-writable PRIVATE and browser `/work`,
`/config` binds. A worker-writable parent permits replacement and root environment
injection even when the file is root-owned. PRIVATE remains host
`/opt/local_cache_crypt/_projects/takeout-downloader/config/autopilot` →
`/config/autopilot`, owner `20001:20001`, mode `0700`, no group/other bits;
ancestor traversal is separate. No key/credential file was provided or created.

[CORE21495 is deployed](../../.wiki/core-release.md), not activated. Current unit
uses `Restart=on-failure`, `RestartSec=30`, five starts per 300 seconds and stays
disabled/inactive. Config-only alias preflight allows reviewed network terminal
types and rejects unsupported wrappers/local/memory/unknown; it proves neither
backend connectivity, canary nor durability. Owner-deferred gates remain.
