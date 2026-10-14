# Final core verification report

**Verifier:** `takeout-verifier` (findings only)
**Snapshot tested:** `C:/Users/User/AppData/Local/Temp/takeout-release-snapshots/20261013T212824394139`
**Canonical checkout:** `C:/Users/User/_projects/takeout-downloader-reliability`

## Scope and safety

- All source/test execution used the immutable snapshot, not the D: original.
- No Google sign-in, password attempt, token mint, real Takeout download, rclone unlock, worker activation, deployment, commit, push, or service restart was performed.
- Network activity was limited to test-created localhost HTTP fixtures; remote backends were subprocess stubs.
- Prior refuter scripts were copied to named scratch, with only their hard-coded import/base paths redirected to the snapshot/scratch. The original refuter files were not changed.
- `PYTHONDONTWRITEBYTECODE=1` was used. Pytest caches and compiled bytecode were directed to named scratch.

## Snapshot integrity

Command used to hash every snapshot file (sorted relative path plus per-file SHA-256):

```text
python - <<'PY'
import hashlib,json,pathlib
p=pathlib.Path(r'C:/Users/User/AppData/Local/Temp/takeout-release-snapshots/20261013T212824394139')
files=[x for x in p.rglob('*') if x.is_file()]
h=hashlib.sha256()
for x in sorted(files,key=lambda q:str(q).lower()):
    rel=x.relative_to(p).as_posix(); b=x.read_bytes(); fh=hashlib.sha256(b).hexdigest()
    h.update(rel.encode()); h.update(b'\0'); h.update(fh.encode()); h.update(b'\n')
print('final_files',len(files),'final_bytes',sum(x.stat().st_size for x in files),'tree_sha256',h.hexdigest())
PY
```

Result: **PASS** — `final_files 242`, `final_bytes 2854580`, `tree_sha256 66076ff0062c6922e4082e4ffc3c097ee41437d49f112f2d42693608f40cce12`.

The supplied `snapshot-manifest.json` was also checked: **241 entries, 0 mismatches**.

## Required suite / compile / CLI / import gates

### Whole v3/v2 suite — PASS

Exact required test command (cache redirected through environment so the snapshot remained unchanged):

```text
cd C:/Users/User/AppData/Local/Temp/takeout-release-snapshots/20261013T212824394139
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-o cache_dir=C:/Users/User/AppData/Local/Temp/takeout-release-verifier-20261013T212824394139/pytest-cache-exact" python -m pytest tests/v3 tests/v2 -q
```

Result: **`974 passed, 6 skipped, 1 warning in 100.97s (0:01:40)`**, exit `0`.

### Compile/import/CLI — PASS

Command:

```text
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
# py_compile all snapshot Python files to named scratch, then import:
# autopilot, autopilot.verify, autopilot.transport, autopilot.remote,
# autopilot.watch, takeout2, takeout2.cli, takeout2.verify
PY
PYTHONDONTWRITEBYTECODE=1 python -m autopilot --help
PYTHONDONTWRITEBYTECODE=1 python -m takeout2 --help
```

Results: **148 Python files compiled to scratch**, all 8 imports succeeded, `python -m autopilot --help` exit `0`, and `python -m takeout2 --help` exit `0`.

## Mandatory adversarial gates

| Gate | Result | Evidence |
|---|---|---|
| Corrupt ZIP/TGZ rejected; failed hash is not OK | **PASS** | Refuter `acceptance.py`: opaque ZIP `CORRUPT False`, corrupt TGZ `CORRUPT False`, injected second-open hash failure `UNVERIFIED False`; ZIP parser exceptions all returned `CORRUPT False`. `tests/v3/test_transfer_integrity.py`: 42 passed. |
| Interrupted/overrun cannot complete | **PASS** | Refuter `repro.py`: absent expected-size stream returned `partial` (`16/64`), overrun returned `partial` with `0` bytes, reset returned `partial`; focused transfer suite passed. |
| Strong-validator resume | **PASS** | Refuter corrected to the implementation's aligned resume offset (`bytes 3-7/8`) returned `strong_resume complete`; localhost interruption/resume coverage is included in `test_transfer_integrity.py` and passed. The unmodified prior fixture requested an invalid `bytes 4-7/8` range when `rewind=1`; it correctly received `RemoteChanged` rather than silently appending. |
| No secret echo/redaction | **PASS** | Refuter HTTP/auth/error cases did not emit supplied token/password values; the credential case printed only a redacted URL. Focused transfer and watch redaction tests passed. |
| Direct remote size + downloaded SHA-256 + no-clobber | **PASS** | `tests/v3/test_remote.py`: **32 passed**. This includes forced backend byte hash verification, size re-check, mismatch preservation, `--ignore-existing`, wrong-hash/hash-error failures, and no upload on existing mismatched objects. |
| Owned process descendants killed | **PASS** | `tests/v3/test_remote.py` passed all timeout/flood/parent-exit descendant-cleanup cases. Prior `timeout_repro.py` replay caught `ValueError rclone command timed out` and reported `owned_descendant_alive_after_timeout False`. |
| Verified staging survives backend failure | **PASS** | `tests/v3/test_orchestration_reliability.py`: **36 passed**, including backend failure retaining verified staging and browser-free re-upload. |
| Receipt identity, exact digest/size/path, and migration | **PASS** | The 36 orchestration tests passed receipt identity/path and claimed-confirmation checks; the focused migration/ledger batch also passed old-schema URL preservation and migration coverage. |
| Cached-only partial mint cannot complete falsely | **PASS** | Orchestration reliability tests passed cached partial mint/auth-block behavior, expected-part count retention, and no subset completion. |
| Fair account backoff | **PASS** | `tests/v3/test_watch.py` in the focused batch passed two-account reauth fairness and bounded retry behavior. |
| OS instance lock | **PASS** | Watch focused tests passed lock contention and release/reacquisition. |
| Dry-run has no effects | **PASS** | Watch focused tests passed dry-run with no discovery/runner invocation and no filesystem effects. |
| Redacted persisted state/notifications | **PASS** | Watch focused tests passed exception redaction, notification failure state preservation, loaded-state secret dropping, and bounded Telegram diagnostics. |

Focused command/results:

```text
python -m pytest -q tests/v3/test_transfer_integrity.py
42 passed in 1.29s

python -m pytest -q tests/v3/test_remote.py
32 passed in 6.91s

python -m pytest -q tests/v3/test_orchestration_reliability.py
36 passed in 6.29s

python -m pytest -q tests/v3/test_watch.py tests/v3/test_review_fixes.py tests/v2/test_migrate.py tests/v2/test_backoff.py
98 passed in 10.28s
```

The broader reliability-focused command covered the same gates plus filename/mover, transfer-without-browser, verify, ledger, and preflight tests: **290 passed in 26.04s**.

## Independent prior-refuter replay

Scratch replay directory:
`C:/Users/User/AppData/Local/Temp/takeout-release-verifier-20261013T212824394139/refuter`

Commands were run from the snapshot root as:

```text
PYTHONDONTWRITEBYTECODE=1 python <scratch>/acceptance.py
PYTHONDONTWRITEBYTECODE=1 python <scratch>/transport_cases.py
PYTHONDONTWRITEBYTECODE=1 python <scratch>/repro.py
PYTHONDONTWRITEBYTECODE=1 python <scratch>/timeout_repro.py
PYTHONDONTWRITEBYTECODE=1 python <scratch>/timeout_repro2.py
```

Observed results:

- `acceptance.py`: all archive/hash/parser cases returned the expected non-OK verdicts; loose HTML was OK and login HTML was not.
- `transport_cases.py`: missing/malformed/changed/weak resume headers returned `RemoteChanged`; 429 returned `RateLimited` with retry-after; filename mismatch returned `TransferError`; interrupted nonzero stream returned `partial`. Its final strong-resume fixture itself advertised the wrong range for `rewind=1`, so the implementation correctly raised `RemoteChanged` and the script exited nonzero at that deliberately inconsistent case.
- A scratch-only corrected replay using the aligned range returned `strong_resume complete`.
- `repro.py`: same-size corrupt ZIP was `CORRUPT`; login media was `CORRUPT`; incomplete/overrun/reset cases were `partial`; the credential error contained no supplied secret.
- `timeout_repro.py`: timeout was caught as `ValueError rclone command timed out`; `owned_descendant_alive_after_timeout False`.
- `timeout_repro2.py`: exit `0`, emitted no assertion output, and left no `survived.marker`; the stronger three-mode descendant tests above supplied the positive assertion.

## Final finding

All mandatory core reliability gates exercised against the immutable snapshot **PASS**. The only nonzero refuter replay was caused by that prior script's inconsistent strong-resume response range; the implementation rejected it safely, and the corrected replay plus localhost resume tests passed. No live backend/export success is claimed.
