# Verified core release — 2026-10-13

## What changed

Format-aware ZIP/TAR/gzip/loose-part validation; safe validated resume and classified interruption; direct remote size and downloaded-SHA256 confirmation; local receipt migrations; retained staging on failures; strict part/account/filename identity; cached-only partial-mint recovery; fair persistent export queue and bounded backoff. No source account deletion.

## Verification evidence

Independent immutable snapshot: `C:/Users/User/AppData/Local/Temp/takeout-release-snapshots/20261013T212824394139`. Manifest: 241 entries, zero mismatches. Tree digest: `66076ff0062c6922e4082e4ffc3c097ee41437d49f112f2d42693608f40cce12`.

```bash
python -m pytest tests/v3 tests/v2 -q
# 974 passed, 6 live-network tests skipped, exit 0.
python -m pytest tests/v3/test_cdp_longrun.py tests/v3/test_mover.py -q
# 38 passed in the release checkout after portable/behavioral fixture fixes.
python -m autopilot --help
python -m takeout2 --help
# Both exit 0 in the independent snapshot.
```

The verifier executed prior adversarial cases, not just the producer summaries. All mandatory gates passed: CRC/format/hash-read failures; incomplete/overrun streams; strong resume; redaction; downloaded backend hash/no-clobber; owned descendants; staged-data retention; receipt identity; partial-mint completeness; lock/backoff/dry-run/state safety. See `.pi/reports/final-core-verifier.md`. This is offline/localhost/subprocess evidence, not a live Google or remote-storage claim.

## Decisions and release boundaries

The original D worktree is preserved; D is full. Release checkout is `C:/Users/User/_projects/takeout-downloader-reliability`. Core source is staged from the exact immutable snapshot so concurrent execution/doc work does not ride along. The supervisor/query-compatibility work is a separate verified unit.

Owner deferred rclone unlock and Google sign-in. Download activation stays disabled; fresh export counts and a real backend canary are unverified. Preserve browser/profile, dirty server main checkout, and existing rclone mount.

## Commit procedure journal

```bash
python C:/Users/User/AppData/Local/Temp/stage_takeout_core.py
git -C C:/Users/User/_projects/takeout-downloader-reliability diff --cached --check
git -C C:/Users/User/_projects/takeout-downloader-reliability commit -q -F C:/Users/User/AppData/Local/Temp/takeout_core_commit.txt
```

The staging script verifies snapshot hashes, refuses a pre-existing staged diff, and stages only explicit core package/test/runtime-requirement paths plus this wiki evidence. It does not overwrite the changing working tree. Commit hash and push outcome are recorded in the deployment journal.
