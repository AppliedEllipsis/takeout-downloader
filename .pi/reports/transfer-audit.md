# Transfer audit — autopilot transport / verify / mover / ledger

Scope: `autopilot/{transport,verify,mover,ledger}.py`, `tests/v3/`. Read-only; no source
edits, no archive writes, no network. Evidence below is from reading the current code on
branch `feat/internal-downloader` and from offline runs of `tests/v3`. `README`/
`architecture`/`infrastructure` were treated as stale; every claim is re-derived here.

Environment for the reproduce: `/c/Users/User/anaconda3/python.exe`, cwd
`.claude/worktrees/takeout-autopilot`. `pytest tests/v3/test_verify.py test_transport.py
test_mover.py test_ledger.py -q` → **54 passed in 0.47s** (baseline suite is green; the
defects below are not caught by it).

---

## CONFIRMED

### F1 — CRITICAL (data loss / remote durability): a byte-complete non-zip part can never verify or move

A Takeout export contains non-zip parts (measured: the 62-product export is 40 `.zip` + 27
`.mp4` + 1 `.mbox`; the 64-product export carries a 13.58 GB `.mbox`). The v3 verifier is
zip-only, and the orchestrator moves a part only after verification passes.

Evidence chain:

- `takeout2/verify.py:145` — `if head != ZIP_LOCAL_HEADER:` returns `VerifyState.CORRUPT`.
  `ZIP_LOCAL_HEADER = b"PK\x03\x04"` (line 30).
- `autopilot/verify.py:42` — `_RESUMABLE = {VerifyState.UNVERIFIED}`; `CORRUPT` is neither
  ok (`verify.py:39-40`) nor resumable (`verify.py:62-64`).
- `autopilot/run.py:784-790`:
  ```
  outcome = verify_local(staged, size_expected=expected, hash_check=cfg.verify_hash)
  if not outcome.ok:
      ledger.set_part_status(..., "partial" if outcome.resumable else "failed", ...)
      continue
  ```
  A complete `.mp4` → `ok=False`, `resumable=False` → status `failed`, `continue` before the
  `move_part` call (run.py:795). The bytes stay in staging; nothing reaches `/opt/archives`.
- `_needs_work` (run.py:862-866) re-selects anything not `done`, so the part is re-processed
  on every run and fails verification again. With `expected_size == staged size`, `download`
  short-circuits as `already-complete` (transport.py:238-243, run.py:772), so no new Google
  requests are spent — but the export can never reach `complete` and `--clean-staging`
  (run.py:813, only after a verified move) never frees the staged copy.

Offline reproduction (script wrote only to a temp dir on `C:`):

```
MP4  size 5012
MP4   ok=False state=CORRUPT resumable=False corrupt=True
      detail="bad magic b'\x00\x00\x00\x18' ... expected PK\x03\x04"
ZIP   ok=True state=STRUCT_OK detail='magic + EOCD present'
TRUNC ok=False state=UNVERIFIED resumable=True detail='incomplete: 1004/200000 ... resumable'
```

This is failure mode **1.23** re-entering through v3's own pipeline: v2 downloaded the bytes
and then flagged 28 non-zip parts as validation failures; v3 cannot even get them into the
archive. For an unattended remote-archival job this is the single highest-priority defect: a
"successful" run against a mixed export silently omits every non-zip part and reports
`incomplete`.

Concrete fix (pick one, deterministic):
- In `run.py:784`, choose the level by the extension that was already derived from the
  minted URL: for non-`.zip` parts pass `size_expected` only (v2 `VerifyState.SIZE_OK`,
  `takeout2/verify.py:130-135`), which is the correct bar for `.mp4`/`.mbox`.
- Or teach `verify_part` that a known non-zip extension is verified by size/hash rather than
  by the zip magic. Do **not** simply drop the magic check for `.zip` — the HTML-page-saved-
  as-zip case it catches is real.

Test gap: `tests/v3/test_verify.py::test_a_non_zip_is_not_ok` asserts `out.ok is False` with
no size expectation, and thereby **locks in** the defect. There is no test that a legitimate
non-zip part at its exact expected size is accepted. `tests/v3/test_integration.py` uses only
zip parts.

### F2 — HIGH (validation): `--verify-hash` supplies no integrity guarantee

`autopilot/verify.py:73` selects `VerifyState.HASH_OK` when `hash_check=True`, and
`verify_local` (verify.py:66-84) exposes no `sha256_expected`. In `takeout2/verify.py`, the
HASH branch hashes the file and, when `sha256_expected is None`, returns `HASH_OK` with
`"sha256 computed"` and no comparison. So the deep flag is `STRUCT_OK` plus a **full read of
every byte over the FUSE mount** — expensive (measured: `zipfile.testzip()` ~10 min on 127
GB) and it does not detect wrong-but-well-formed bytes. Either thread an expected digest
through and compare, or relabel the flag as a read-through smoke test rather than integrity.

### F3 — MEDIUM (resume correctness / test gap): the etag-gated resume rule is untested

`autopilot/transport.py:260-268` intentionally refuses to resume without a validator:
```
resumed = start > 0 and bool(etag)
if start > 0 and not resumed:
    start = 0
```
This is the fix for silent same-size corruption, yet no test pins it. `test_transport.py:83`
exercises resume **with** an etag; `test_content_range_total_mismatch_is_flagged_not_appended`
and `test_range_unsatisfiable...` pass no etag, so `start` is reset to 0 and **no `Range`/
`If-Range` header is sent** — those tests silently do not exercise the resume path they
appear to. `_start_from_content_range` (transport.py:381, added to catch a mis-ranged 206)
is not imported or tested at all. Add: (a) partial file + no etag ⇒ exactly one 200 GET from
offset 0 and no `Range` header; (b) a 206 whose `Content-Range` start ≠ requested ⇒
`RemoteChanged`; (c) response etag different from requested ⇒ `RemoteChanged`.

### F4 — MEDIUM (remote durability): the mount guard runs once, before the loop

`assert_destination_ready` is called exactly once (run.py:600). Failure mode 1.7 is a FUSE
daemon that dies and leaves `/opt/archives` as an ordinary directory on the ~13 GB root FS.
A detach **after** that check is undetected and each subsequent `move_part` (run.py:795)
writes into the root filesystem. Re-assert before each move (cheap: the mount table) or bound
the number of moves between checks.

### F5 — MEDIUM (shared staging/VFS budget): headroom measured once, no VFS reserve

`__main__.py:147` defaults `min_headroom` to 5 GiB; `assert_staging_ready`
(`mover.py:322-344`) measures free space once, at run.py:421, before any transfer. Staging
and rclone's VFS cache live on the same 300 GB volume (`--vfs-cache-max-size 100G`,
measured 97 GB held). Nothing reserves the VFS growth or re-checks during the run, and
`clean_staging` is off by default (run.py:142, 813), so a full pull transiently holds
staged + archived + cache. This is the documented "194 GB free vs ~232 GB wanted" gap; the
guard's 5 GiB floor does not cover it. Budget = largest remaining part + VFS ceiling, and
re-evaluate periodically.

### F6 — LOW (identity): `set_part_filename` has no placeholder guard

`autopilot/ledger.py:383-390` writes any non-empty `filename` unconditionally, unlike
`upsert_parts` (ledger.py:340-350) which guards on `carries_part_identity`. `run.py:702-708`
can fabricate `part-{idx}.zip` when `part_filename_from_url` returns a placeholder, and
stamps it as the permanent name. Separately, `carries_part_identity` (`ledger.py:74-95`)
accepts any name containing a dot, so an error page saved as `pwd.htm`/`signin.html` would
qualify as a "real" part name. Apply the same guard in `set_part_filename`.

### F7 — LOW (dead branch): `plan_moves` "skip-size-mismatch" is never consumed

`mover.py:179` classifies a present-but-different-size destination file as
`skip-size-mismatch`, but the pre-mint loop only acts on `"skip-present"` (run.py:648-651).
No data-loss results (`move_part` later overwrites via `os.replace`, mover.py:279), but the
branch is dead and misleading about what a wrong-size archive copy triggers.

---

## SUSPECTED (not confirmed by offline test)

- **Network read errors are not classified.** `transport.py` wraps `open/write` in a
  `try/except OSError` (lines ~318-338) but does not guard `stream.read()` (line ~322).
  `_UrllibStream.read` (`transport.py:141-142`) calls `urllib` `resp.read`. If a connection
  drops mid-body with a known Content-Length, `http.client` can raise `IncompleteRead`
  (an `HTTPException`, **not** an `OSError` and **not** an `AutopilotError`), which would
  escape the `except (TransferError, AutopilotError)` in run.py (~748) and the `except
  AutopilotError` at run.py:~840, surfacing as a traceback with the job left mid-status.
  Recommend catching `Exception` around `read()` and raising `TransferError`, and a test
  with a stream whose `read` raises. UNCONFIRMED because no fake stream in `tests/v3` raises
  mid-read and I did not exercise a live socket.
- **`failed_transfer_count` is cumulative and never reset** (`ledger.py:489-505`;
  `MAX_CACHED_URL_FAILURES = 3`, run.py:105,762). Three transient failures spread across
  separate runs (days apart) permanently discard a valid cached minted URL, forcing a fresh
  `Δ1` mint on a 5-attempt-per-part allowance. Bounding per-run rather than lifetime would be
  safer. UNCONFIRMED as a real-world trigger; the counting logic itself is confirmed.

## Sections that are clean

- `aligned_resume_offset` arithmetic and the torn-tail truncate→append sequence
  (`transport.py:200-247`) match the ported v2 semantics and are tested.
- Auth classification (`NeedsReauth` on sign-in redirect / 401 / 403) and the missing-jar
  programming-error split are correct and tested (`transport.py:284-297`).
- `record_failed_transfer` and the `NeedsReauth`-path `clear_mint` (run.py:738-746) close the
  "re-use a rejected URL forever" park; consistent with the 2026-09-22 journal.
- FUSE-ledger refusal, `fuse_mount_for` longest-prefix semantics, and the `_normalize`
  Windows/POSIX handling (`ledger.py:181-235`) are correct and tested.

## Exact test gaps (highest value first)

1. Non-zip part at exact expected size must verify OK and move (currently the suite asserts
   the opposite). — F1.
2. `verify_local`/`verify_part` HASH level with a known-but-wrong digest must fail; without an
   expected digest, the flag's guarantee must be documented/tested. — F2.
3. Resume without `etag` sends no `Range` and restarts at offset 0. — F3.
4. Mis-ranged `206` (`Content-Range` start ≠ requested) ⇒ `RemoteChanged`; resp etag ≠
   requested etag ⇒ `RemoteChanged`; `_start_from_content_range` unit tests. — F3.
5. `assert_destination_ready` re-checked before each move (or a bounded cadence). — F4.
6. `staging + VFS` budget test with a synthetic VFS-ceiling term. — F5.
7. `set_part_filename` must not store a placeholder / dot-bearing error-page name. — F6.
8. A stream whose `read()` raises mid-body must surface as `TransferError`, not a traceback. — SUSPECTED.

---

## Summary (top findings)

**F1 (critical):** v3 cannot archive any non-zip Takeout part. `verify_part` marks every
non-`PK\x03\x04` file `CORRUPT` (`takeout2/verify.py:145`), which `verify_local` maps to
not-resumable (`verify.py:42`), and `run.py:784-790` then sets the part to `failed` and skips
`move_part` — even when the bytes are complete and the size is exact. Every mixed export
(27 `.mp4` + 1 `.mbox` in the 62-product set) can never reach `complete`; the bytes sit in
staging forever. This is failure mode 1.23 in v3's own path. **F2:** `--verify-hash` does a
full read but compares no digest, so it guarantees nothing beyond `STRUCT_OK`. **F3:** the
etag-gated resume rule and `_start_from_content_range` have no tests; two "resume" tests
never send a `Range`. **F4/F5:** the mount guard and the staging headroom check each run
once, so a mid-run FUSE detach or VFS-cache growth is unguarded. **F6:** `set_part_filename`
lacks the placeholder guard its sibling `upsert_parts` has. The suite is green (54 passed)
because `test_a_non_zip_is_not_ok` encodes F1 as expected behaviour.
