# Remote lane build evidence

## Scope and premise
Read `.pi/workflows/build-contract.md` first. Implemented only owned source/tests and this report:
- `autopilot/remote.py` (new)
- `autopilot/mover.py`
- `tests/v3/test_remote.py` (new)
- `tests/v3/test_mover.py`

No server, browser, real remote/archive, wiki, deployment, configuration, or commits were changed. Existing unrelated work was preserved. All test data was temporary local data.

## Implementation
Frozen RemoteConfig/RemoteResult and archive/verify APIs implemented. Named-remote validation rejects inline credentials/URLs and unsafe paths. Direct `copyto --ignore-existing` preserves pre-existing mismatches, including same-size/different-digest objects. Source SHA256 is computed and optionally compared to the trusted supplied digest. Confirmation requires exact backend size, `hashsum SHA256 --download` digest comparison, then another size check. Every command has `--ask-password=false`, no shell, bounded time and capped stdout/stderr (64 KiB each); raw subprocess errors/output are not returned.

Mover rejects unsafe names/path traversal/symlink paths, compares existing destination content, digest-checks copied bytes in its watchdog child, and rechecks an initially observed FUSE mount before/after publication. Unique exclusive temporary files and atomic no-clobber hard-link publication replace destructive `os.replace`. Results explicitly say local verification is not remote durability.

## Commands/results
- Local `rclone hashsum --help`, `lsjson --help`, `copyto --help`: confirmed download hashing, stat shape, immutable/ignore-existing capabilities. Local available executable is v1.68.2, not the commander's reported server v1.69.1; no server access attempted.
- Actual local-only command in TemporaryDirectory: `rclone hashsum SHA256 <temporary-file> --download --ask-password=false --config <temporary-empty-config>`: exit 0, digest matched Python hashlib. This also verifies the exact algorithm spelling and flags without remote access.
- `python -m py_compile autopilot/mover.py autopilot/remote.py`: passed.
- `python -m pytest tests/v3/test_remote.py tests/v3/test_mover.py -q`: **57 passed** (final run, 3.36s).

Remote fixtures execute a real Python subprocess emulating rclone argv/output/state (portable across Windows), not mocked command results. Regressions cover forced reads, repeat adoption, existing size/hash mismatches preserved without upload, wrong hashes, upload/read/stat failures, malformed stat, deadlines, oversized output, unavailable executable, traversal/credentials, absent digest/invalid sizes, password-prompt disablement. Mover regressions cover same-size copy corruption, mount loss, child timeout, traversal, existing mismatches, and concurrent destination publication.

## Limitations / unconfirmed
No live backend or encrypted production config validated; credential provisioning remains commander's work. Remote byte hashes establish observed backend contents, not provider replication/retention guarantees. Rclone has no generic transactional conditional-create API: ignore-existing protects objects observed by rclone, but cannot guarantee atomic exclusion against arbitrary concurrent backend writers; use a single archival writer. Verification cannot prevent subsequent external mutation.

Hard-link publication fails closed on filesystems that do not support it (including some FUSE mounts); production remote archival must use the new direct backend API. Mount rechecks are only possible for mounts observed in the available table; the legacy explicit assert_destination_ready no-table behavior is unchanged. Destination planning remains a size-based hint, not authorization to mark archival completion; orchestration must use confirmed remote or verified native-local results. No orchestration-owned files changed.
