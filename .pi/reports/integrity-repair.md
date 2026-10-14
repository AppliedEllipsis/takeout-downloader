# Integrity repair — scoped release blockers

Worktree: `D:/_projects/takeout_downloader_script/.claude/worktrees/takeout-autopilot`.
Native runtime: Windows / Python 3.12. No commit, deployment, Google request, real backend, global configuration change, or source-data modification.

## Scope and repairs

Changed only `autopilot/{verify,transport,errors,remote}.py`, `tests/v3/{test_remote,test_mover_watchdog}.py`, new `tests/v3/test_transfer_integrity.py`, and this requested report. Preserved pre-existing changes in owned files; did not edit run/ledger/mint/jar/mover/watcher/wiki.

- Declared ZIP/TAR/TGZ/gzip formats must match bytes. ZIP validation always reads member CRCs (the compatibility `crc_check=False` flag cannot disable integrity). gzip is consumed through CRC/ISIZE trailers; TAR reads member data, checks terminators, and rejects nonzero trailing records which Python's tarfile otherwise ignores.
- Known-size loose HTML documents remain supported; identifiable login/error pages and HTML disguised as other formats fail. Unknown-size loose data needs a trusted digest; merely computing SHA256 is not integrity evidence.
- Hash-read failure is UNVERIFIED, never prior SIZE_OK/STRUCT_OK. ZIP/zlib/encryption/unsupported compression/parser errors become explicit CORRUPT verdicts.
- Transport derives totals from Content-Length/Content-Range, rejects invalid/inconsistent declarations before opening/truncating the destination, preserves partial prefixes on rejected headers, and never marks unknown/truncated/error streams complete. Error-tainted full-length tails are safely rewound, including cancellation/close failures; strong ETags are retained for legitimate resume. No auth retry or remint was added.
- Exposed errors redact signed URL paths, userinfo, queries and token/rapt credentials; internal `.url` attributes remain available for existing control flow.
- Remote commands own a POSIX session/process group or Windows kill-on-close job. Windows starts suspended, assigns the job before any descendant can spawn, then resumes. Cleanup covers timeout, output-limit, read failure, nonzero exits, and descendants holding pipes open. Existing `--download`, exact size/SHA256, and `--ignore-existing` no-clobber semantics remain.
- Watchdog injectors match exact `_COPY_CHILD` versus `_HASH_CHILD`. Short-write regression now uses a real full copy receipt then simulates landed tail loss. Added source-hash failure preventing copy. Updated the staging guard source assertion for the other actor's existing `finish` closure rename without weakening ordering checks.

## Before/after evidence

All commands below ran from the worktree above.

Baseline:

```
python -m pytest -q tests/v3/test_verify.py tests/v3/test_transport.py tests/v3/test_remote.py tests/v3/test_mover_watchdog.py
```

**60 passed, 4 failed** (3 outdated subprocess injectors; 1 existing closure-name assertion).

New regressions before production repairs:

```
python -m pytest -q tests/v3/test_transfer_integrity.py tests/v3/test_remote.py
```

**32 failed, 35 passed**. Red output: `C:/Users/User/AppData/Local/Temp/takeout-refuter/repair-red.txt`. Initial oversized pytest parameter IDs caused two Windows fixture errors; explicit archive-case IDs fixed those before the recorded red run.

Additional independently red edge cases:

```
python -m pytest -q tests/v3/test_transfer_integrity.py -k 'tar_rejects_garbage or close_failure'
python -m pytest -q tests/v3/test_transfer_integrity.py -k cancellation
```

Respectively **3 failed, 38 deselected** and **1 failed, 41 deselected**, before their fixes. Outputs: `repair-edge-red.txt`, `repair-cancel-red.txt` in the same temp directory.

Final assigned suites:

```
python -m pytest -q tests/v3/test_verify.py tests/v3/test_transport.py tests/v3/test_remote.py tests/v3/test_mover_watchdog.py tests/v3/test_transfer_integrity.py
```

**110 passed in 10.90s**. Includes real localhost Urllib interrupted-stream/strong-ETag resume; owned spawned descendant tests for timeout, flood and parent-exit, checking both delayed marker absence and actual Windows process termination.

Supplemental compatibility:

```
python -m pytest -q tests/v3/test_mover.py tests/v3/test_http_client_live.py tests/v3/test_review_fixes.py
```

**45 passed, 6 skipped in 11.57s**. The six opt-in public-network tests stayed skipped; no public/Google network access was enabled.

```
python -m compileall -q autopilot/verify.py autopilot/transport.py autopilot/errors.py autopilot/remote.py tests/v3/test_transfer_integrity.py tests/v3/test_remote.py tests/v3/test_mover_watchdog.py
git diff --check -- autopilot/verify.py autopilot/transport.py autopilot/errors.py tests/v3/test_mover_watchdog.py
```

Both completed successfully.

## Independent supplied repros

Ran each supplied script, unchanged:

```
python C:/Users/User/AppData/Local/Temp/takeout-refuter/acceptance.py
python C:/Users/User/AppData/Local/Temp/takeout-refuter/repro.py
python C:/Users/User/AppData/Local/Temp/takeout-refuter/transport_cases.py
python C:/Users/User/AppData/Local/Temp/takeout-refuter/timeout_repro.py
python C:/Users/User/AppData/Local/Temp/takeout-refuter/timeout_repro2.py
```

- `acceptance.py`: opaque ZIP and corrupt TGZ CORRUPT; legitimate HTML SIZE_OK; login HTML CORRUPT; failed second hash-open UNVERIFIED; all three injected ZIP exceptions CORRUPT, no parser traceback.
- `repro.py`: corrupt ZIP CORRUPT; known-size loose bytes SIZE_OK; disguised login CORRUPT; absent expected size with truncated Content-Length **partial 16/64**; overrun **partial**, safely rewound; 401 message redacted; reset partial.
- `transport_cases.py`: missing/malformed/changed/weak validator cases rejected, Retry-After 17 retained, filename rejected, reset preserves 8 bytes. Its final hardcoded `bytes 4-7/8` conflicts with `rewind=1`, which correctly requests byte 3. The unchanged script therefore exits 1 with RemoteChanged, rather than incorrectly accepting mismatched bytes. Its header prints also use lowercase keys against its uppercase-key client dictionary.
- `timeout_repro.py`: `owned_descendant_alive_after_timeout False`. One rerun hit an outer 30-second tool timeout while its unrelated `tasklist.exe` check was pending; rerunning with 120 seconds completed successfully with False.
- `timeout_repro2.py`: exits 0 without output unchanged because its generated stub embeds a Windows backslash path in an outer non-raw Python string, causing startup syntax failure before the timeout assertion.

Replayed the latter two fixture defects **in memory only**, without editing supplied repros:

```
python - <<'PY'
from pathlib import Path
base = Path('C:/Users/User/AppData/Local/Temp/takeout-refuter')
s = (base/'transport_cases.py').read_text().replace('expected_size=8,etag=E,rewind=1)', 'expected_size=8,etag=E,rewind=0)')
exec(compile(s, str(base/'transport_cases.py'), 'exec'), {'__name__': '__main__'})
s = (base/'timeout_repro2.py').read_text().replace('% marker)', '% marker.as_posix())')
exec(compile(s, str(base/'timeout_repro2.py'), 'exec'), {'__name__': '__main__'})
PY
```

Corrected results: `strong_resume complete`; `timeout_exception ValueError rclone command timed out`; `owned_descendant_survived False`. Production guards were not relaxed to accommodate defective fixtures.

## Remaining risks / boundaries

- POSIX group cleanup and the portable descendant regression are implemented but not executed on this native Windows host. Self-daemonizing descendants which deliberately leave a POSIX session are outside process-group containment; Windows jobs contain ordinary descendant trees even after parent exit.
- Archive validation entails full reads/decompression (TAR/gzip may require multiple passes); no performance claim for multi-GB exports is made. CRC/structure and computed hashes do not replace a trusted digest/backend durability proof.
- Loose known-size parts retain the existing size-only contract. HTML classification uses recognizable markers, not a general content-authenticity oracle.
- A filesystem refusing tail truncation produces a classified failure, not a completed transfer; inaccessible or unreliable staging must not be treated as integrity evidence. Process death outside handled failures still requires the caller's existing verifier/digest gate.
- No real rclone backend was exercised. Existing subprocess tests continue to verify forced backend reads and no-clobber semantics using synthetic local fixtures.
