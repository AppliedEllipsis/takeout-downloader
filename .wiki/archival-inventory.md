# Archival inventory — what Takeout data exists and where

**Verified 2026-09-19** by reading directly through the archive mount, using the project's own
`takeout2/verify.py`. Nothing was copied, moved, or deleted except one bookkeeping file (below).

## Where the archive actually is

Takeout data lives at `/opt/archives/google-takeout/<account>/<export-ts>/`, where `/opt/archives`
is **an rclone FUSE mount** — i.e. the data is already in the archival remote, not on local disk.

The mount's rclone process (verified from `ps`, running as root):

```
/opt/storage.local_1/tools/rclone/rclone-v1.69.1-linux-amd64/rclone
  --config /opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf
  mount archives: /opt/archives
  --cache-dir /opt/local_cache_crypt/rclone_vfs
  --allow-other --vfs-cache-mode full --dir-cache-time 9999h --poll-interval 15s
  --vfs-read-ahead 32M --vfs-read-chunk-size 16M --vfs-read-chunk-size-limit 128M
  --vfs-cache-max-size 100G --vfs-cache-max-age 24h --buffer-size 16M --timeout 1h -v
```

### ⚠️ The provider cannot be determined from this machine

- The remote is named **`archives:`**, wrapping a chunked upstream (`/proc/mounts` reports the device
  as `archive_chunked:`), and it reports a synthetic **1.0 PB** capacity — the signature of an
  rclone **chunker** remote.
- The config at `/opt/storage.local_1/projects/_rclone/rclone-u455805-sub1.conf` is
  **password-encrypted**: any `rclone --config <that> …` invocation prompts
  `Enter configuration password:`. Remote names and credentials are therefore **not readable**
  without the password, and this inventory does not guess.
- **No pCloud remote is configured.** Grepping for "pcloud" matched only the rclone and JuiceFS
  *binaries* (pCloud is a built-in rclone backend, so the string appears in the executables). There
  is no pCloud config, mount, or reference in any config directory.
- Circumstantial evidence points to a **Hetzner Storage Box**: the config is named
  `rclone-u455805-sub1`, mirroring the JuiceFS mount names (`juicefs-u455805-sub1`,
  `juicefs-u457239-sub1`), and the repo's own docs contain the host
  `u455805-sub1.your-storagebox.de`. **Unconfirmed** — the config would have to be decrypted to be sure.

**If a second copy on a different provider is wanted, it does not exist yet and would need to be set
up.** The data is currently on exactly one archival target.

## Verified contents

`STRUCT_OK` = the file begins `PK\x03\x04` **and** an end-of-central-directory record is present in
the tail. This reads the first and last bytes of each object, so it proves the remote holds the full
byte range rather than a truncated or partially-uploaded object.

### `andrew` / `2026-08-06-02-27-46` — ✅ COMPLETE

| Part | Size | Verified |
|---|---|---|
| `takeout-20260806T022731Z-001.zip` | 445.3 KB | ✅ |
| `takeout-20260806T022746Z-1-001.zip` | 155.3 MB | ✅ |
| `takeout-20260806T022746Z-2-001.zip` | 43.0 KB | ✅ |
| `takeout-20260806T022746Z-3-001.zip` | 519.9 MB | ✅ |
| `takeout-20260806T022746Z-4-001.zip` | 367.5 KB | ✅ |
| `takeout-20260806T022746Z-5-001.zip` | 1.2 GB | ✅ |

**6/6 parts OK, 1.8 GB.** The job reports `complete`. This export is safely archived and intact.

### `braincreation` / `2026-06-23-03-59-47` — ⚠️ PARTIAL, 3 parts CORRUPT

| Part | Size | Verified |
|---|---|---|
| `takeout-20260623T035947Z-9-001.zip` | 49.0 GB | ✅ |
| `takeout-20260623T035947Z-9-002.zip` | 49.4 GB | ✅ |
| `takeout-20260623T035947Z-9-003.zip` | 12.8 GB | ✅ |
| `takeout-20260623T035947Z-13-001.zip` | 50.0 GB | ✅ |
| `takeout-20260623T035947Z-13-002.zip` | 50.0 GB | ✅ |
| **`takeout-20260623T035947Z-13-003.zip`** | **49.8 GB** | ❌ **truncated — no EOCD** |
| `takeout-20260623T035947Z-13-004.zip` | 50.0 GB | ✅ |
| **`takeout-20260623T035947Z-13-005.zip`** | **38.8 GB** | ❌ **truncated — no EOCD** |
| **`takeout-20260623T035947Z-13-006.zip`** | **30.7 GB** | ❌ **truncated — no EOCD** |
| `takeout-20260623T035947Z-17-001.zip` | 3.7 GB | ✅ |

**7/10 parts OK, 3 truncated, 384.3 GB total.** Note the group structure: `-9-`, `-13-`, `-17-` —
this export was split into multiple product groups, not one numbered sequence.

**~119 GB of the archive is unrecoverable garbage** (a zip with no EOCD cannot be opened or
repaired). The job's ledger recorded 10 of an expected **63** parts, so this export is roughly
one-sixth complete.

### It can never be completed

The export was created `2026-06-23`; Takeout exports expire ~7 days later, so it died on or about
**2026-06-30**. The download stalled on `2026-06-28` when its cookie expired. **The source no longer
exists**, so the missing 53 parts and the 3 truncated files cannot be re-fetched. This is why the job
was permanently parked in `needs_cookie` and why it spawned a tab every minute for ~3 months —
nothing could ever satisfy it.

## Job retirement, 2026-09-19

The stale `braincreation` job was retired (`DELETE /api/jobs/20260628T001529-braincreation` → HTTP
200). **This removed bookkeeping only; no data was deleted.** Byte-level proof:

| | Bytes | Files |
|---|---|---|
| Before | 412,631,115,353 | 11 |
| After | 412,631,085,735 | 11 |
| **Delta** | **29,618** | **10 zips retained** |

The 29,618-byte delta is **exactly** the size of `.manager_state.json` (29,618 bytes) — the only file
removed. All 10 zip parts remain on the archive mount.

Bookkeeping preserved at
`/opt/archives/google-takeout/_retired-bookkeeping/20260628T001529-braincreation/`:
`manager_state.json` (29,618 B), `manifest.json` (3,038 B), `jobs_snapshot.json` (788 B).

## Upload state

The rclone VFS cache (`/opt/local_cache_crypt/rclone_vfs`) is **empty — 0 files, 0 bytes**. With
`--vfs-cache-mode full`, an in-flight or unuploaded write would appear there. Nothing is pending:
everything visible through the mount is already in the remote.

## Open items

- **Provider identity unconfirmed** (encrypted rclone config). Decrypting it, or asking the owner,
  is the only way to name the backend definitively.
- **No secondary/offsite copy exists.** If the archive target is lost, all of it is lost. Worth
  deciding deliberately.
- The 3 truncated parts (~119 GB) are unopenable and unrecoverable. They were left in place per the
  owner's instruction not to delete data; deleting them would reclaim ~119 GB but they may still be
  worth keeping as evidence of what was attempted.
- `manifest.json` in both exports parses with **0 entries** — the manifest format the verifier expects
  is not what these files contain. Worth checking before v3 relies on it as the completeness oracle.


## Updated 2026-09-21 — the 64-product export is complete

`da982753-42dc-4a58-bba0-a9d3605759dd` (64 products) landed at
`/opt/archives/google-takeout/braincreation/2026-09-19-04-27-12/`:

* **19/19 parts, 141,278,352,495 bytes (141.28 GB)** — every part's size matches Google's
  listing exactly.
* All 18 zips carry a valid end-of-archive record; the `.mbox` is valid at head and tail.
* Nothing was still being written when checked.
* Downloaded by **v2**, not v3 — see `.wiki/decisions.md`, 2026-09-21.
* A separate CRC pass over the zip *members* was launched because this account has
  produced truncated archives before.

The 3 previously-identified truncated parts elsewhere in this account remain unopenable.


## CRC verification — PASSED 2026-09-21

The completeness claim above was upgraded from structural to **content-verified**:

| check | result |
|---|---|
| exact byte count, all 19 parts | 19/19 — 141,278,352,495 of 141,278,352,495 |
| nothing still being written | yes (20 s no-growth window) |
| zip end-of-archive record, all 18 zips | 18/18 present |
| **CRC of every compressed member, all 18 zips** | **18/18 OK, 0 corrupt, 0 failures** |
| mbox head/tail | valid (`From … X-GM-THRID`, clean MIME terminator) |

22.6 minutes, ~94 MB/s. Evidence: `.recon/crc64_result.txt`.

**The 64-product full backup is complete and independently verified.** Note that v2's own
manager UI displayed this job with an **Error** badge — that is v2's bookkeeping, not a data
problem, and the badges should not be trusted as the completeness oracle.
