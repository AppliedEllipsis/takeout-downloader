# Orchestration build evidence

## Scope / verified premises
Read `.pi/workflows/build-contract.md` first. Worked only in the frozen orchestration lane, its offline integration fixtures/tests, and this report. RemoteConfig/RemoteResult and both remote functions exist; verify_local exposes SHA256/CRC. No commits, deployment, wiki changes, browser/server/archive operations, web research, or real network tests.

Changed production paths: `autopilot/run.py`, `autopilot/ledger.py`, `autopilot/mint.py`, `autopilot/jar.py`.
Changed owned test paths under `tests/v3/`: `test_integration.py`, `test_jar.py`, `test_mint.py`, `test_mint_stray_downloads.py`, `test_review_fixes.py`, `test_signin_classification.py`, `test_destination_index_is_used.py`, `test_refresh_and_filename.py`, `test_quota.py`, `test_cdp_target.py`; added `orchestration_fixtures.py`, `test_orchestration_reliability.py`. Other builders' files were not edited.

## Implementation
- Exact new RunConfig fields/defaults. Per-transfer capacity check includes remaining/restart bytes + reserve + headroom; mount rechecked before each legacy copy and after copy.
- Direct staged-to-backend archival; exact confirmed path/size/SHA receipt required. Additive SQLite migrations retain cached URLs/progress. Stable done receipts matching current destination/SHA/size/time are trusted without re-reading prior multi-GB objects on every max_parts=1 pass; changed-path or unconfirmed receipts require backend verification. An audit remains separate.
- Native-local copies require destination hash comparison; FUSE fsync/size never authorizes completion or cleanup. Failed backend uploads retain verified staging. Known staged files can upload without browser/remint.
- Cached-only reconstructs ALL rows, including done/no-URL and unminted rows; never scrapes/mints or calls a minted subset complete. Missing legacy expected-count evidence cannot be converted into a completion claim. Mint-only honors cache and returns incomplete.
- Transients preserve URLs, partial validators/errors. Only explicit auth/RemoteChanged invalidates URLs. Corrupt byte-complete ZIPs and error-bearing full-size stream results are safely quarantined, preserving evidence and cached retry eligibility.
- Portable/decoded filename, duplicate-name, URL identity and account guards. Cookie suffix matching is exact/subdomain. UA is optional and passed only when the transfer adapter supports it.
- Mint requires download-event/frame ownership before navigation, cancels only exact automation-owned downloads, leaves challenges visible, and preserves no-Fetch. Cancellation refusal preserves the earned URL but stops orchestration with a reported cleanup failure. Cached jar reads can attach read-only to an authblocked tab without navigating it.

## Commands / results (Windows Python, temporary files only)
`python -m py_compile autopilot/run.py autopilot/ledger.py autopilot/mint.py autopilot/jar.py` — passed.

Broad owned gate:
`python -m pytest -q tests/v3/test_orchestration_reliability.py tests/v3/test_integration.py tests/v3/test_ledger.py tests/v3/test_jar.py tests/v3/test_mint.py tests/v3/test_mint_only.py tests/v3/test_mint_stray_downloads.py tests/v3/test_transfer_without_browser.py tests/v3/test_review_fixes.py tests/v3/test_destination_index_is_used.py tests/v3/test_filename_encoding.py tests/v3/test_part_filename_preserved.py tests/v3/test_refresh_and_filename.py tests/v3/test_quota.py tests/v3/test_run_destination_guard.py tests/v3/test_signin_classification.py tests/v3/test_cdp_target.py --tb=short`
— **211 passed** before the subsequent stable-receipt throughput change.

Latest post-throughput gate:
`python -m pytest -q tests/v3/test_orchestration_reliability.py tests/v3/test_integration.py tests/v3/test_mint_only.py --tb=short`
— **54 passed** (includes all 36 reliability regression cases).

Cross-lane snapshot:
`python -m pytest -q tests/v3/test_verify.py tests/v3/test_transport.py tests/v3/test_remote.py tests/v3/test_mover.py tests/v3/test_watch.py tests/v3/test_discovery.py tests/v3/test_provoke_rapt.py tests/v3/test_cdp_target.py tests/v3/test_report_sees_the_archive.py --tb=short`
— **144 passed, 1 failed**: untouched `test_provoke_rapt.py::test_run_once_actually_provokes_before_giving_up` requires the historical untracked redirector-probe call in source. The orchestrator intentionally refuses that unsafe probe rather than leaking browser downloads; un-tokened mint work requests human authentication. Helper/browser tracking integration can be reconsidered separately without restoring the unsafe call.

## Boundaries
No whole-suite or live durability claim. Integrity-repair actor now owns verify/transport/errors/remote and test_mover_watchdog; none were modified here. Local orchestration defensively refuses a declared ZIP lacking a real central directory and verification without a computed SHA. Actual browser cancellation and backend durability remain untested live; refusal is explicit, never a human-download sweep.
