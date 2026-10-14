# Supervisor release journal

## 2026-10-14 — Native validation complete; deployment inactive

What changed: foreground nonroot container supervision, PID-owned cleanup and lease, fail-closed named backend/config gate, effective Docker-mount admission, narrow Compose archive mounts, strict token-free stored discovery URLs, portable test roots/default collection, and shell LF policy.

Decisions: keep owner-deferred unlock/auth/activation blocked; no real Google download or remote-backend claim. Protected root credentials stay outside worker-writable and effective container mounts; verify exclusion before provisioning any secret. Existing containers are not recreated by this source release.

Executed commands/results:

```bash
python -m pytest -q -rs
# Windows after mount repair: 1352 passed, 66 skipped, no failures.
python -B -m pytest -q -rs tests/v3/test_mint_only.py tests/v3/test_remote.py
# Windows portable fixture follow-up: 39 passed.
wsl -d Ubuntu-24.04 -- bash -lc 'unset PYTHONPATH PYTHONHOME AUTOPILOT_LIVE; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability; /home/user/tmp/takeout-linux-release-venv/bin/python -m pytest -q -rs tests/v3 tests/v2'
# Native Linux: 1211 passed, 6 live-network skips, no failures.
wsl -d Ubuntu-24.04 -- bash -lc 'unset PYTHONPATH PYTHONHOME AUTOPILOT_LIVE; export STORAGE_ROOT=/tmp/takeout-release-storage-2; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability; /home/user/tmp/takeout-linux-release-venv/bin/python -m pytest -q -rs .'
# Native Linux full: 1410 passed, 8 skipped (6 live, 2 optional lupa), no failures.
```

Full evidence: [Linux release gate](../.pi/reports/linux-release-gate.md), [mount boundary](../.pi/reports/mount-boundary-repair.md), [core deployment](../.pi/reports/server-core-deploy.md). Native Linux dependency failures and fixture races in earlier HOLD reports are superseded by this isolated native run, not silently hidden. Source code fingerprint changes are limited to documented repairs; deliberate wiki banner edits explain the earlier doc-only hash mismatch.

The verified core commit is already deployed. Supervisor commit/push and final server state are appended in deployment evidence after the operations complete. This entry records no service activation.
