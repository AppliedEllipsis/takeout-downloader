# Final release verification and handover

## 2026-10-14 — Completed code, activation held

The current release is the browser-mint/resumable-HTTP hybrid with strict format/CRC validation, direct backend SHA256/size receipts, account-checked export discovery, bounded retry and foreground non-root container supervision. Host secrets are outside worker-writable storage; shipped Compose mounts no longer expose all of `/opt`. Docker inspection rejects overlapping effective mounts before exec.

### Executed verification

- Windows full project after mount-boundary repair: **1352 passed, 66 skipped**, no failures.
- Native isolated Linux v3/v2: **1211 passed, 6 skipped**, no failures.
- Native isolated Linux full project: **1410 passed, 8 skipped**, no failures. Six skips are explicit live-network opt-ins; two require optional lupa.
- Linux admission/guardian/discovery focused gate: **251 passed**, no skips.
- Last portable mint-cache/process-tree fixture check on Windows: **39 passed**.

Exact native commands (no borrowed Windows wheels):

```bash
wsl -d Ubuntu-24.04 -- bash -lc 'set -o pipefail; unset PYTHONPATH PYTHONHOME AUTOPILOT_LIVE; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability; /home/user/tmp/takeout-linux-release-venv/bin/python -m pytest -q -rs tests/v3 tests/v2'
wsl -d Ubuntu-24.04 -- bash -lc 'set -o pipefail; unset PYTHONPATH PYTHONHOME AUTOPILOT_LIVE; export STORAGE_ROOT=/tmp/takeout-release-storage-2; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability; /home/user/tmp/takeout-linux-release-venv/bin/python -m pytest -q -rs .'
```

Evidence: [native Linux](../.pi/reports/linux-release-gate.md), [mount repair](../.pi/reports/mount-boundary-repair.md), [core deployed](../.pi/reports/server-core-deploy.md). Earlier HOLD reports remain dated evidence, superseded by the recorded repairs and native gates; they are not silently rewritten as PASS. The earlier wiki hash changes were explicit commander banner corrections; source/config/test hashes stayed fixed during that test run.

### Operational boundary

CORE21495 and supervisor source `7fc3ac97ef5db6f335cd70a05d2d6b77b3c43229` are deployed and verified; see [server deployment evidence](../.pi/reports/server-supervisor-deploy.md). The systemd unit is still absent/inactive and no worker is running. Owner explicitly deferred the rclone configuration unlock and Google reauthentication. Keep the worker disabled/inactive. No credential file, backend canary, fresh authenticated export discovery or real Google download has been performed. No source-account data was deleted; browser/container/rclone services and dirty main checkout are preserved.

The release checkout is `C:/Users/User/_projects/takeout-downloader-reliability`; the original D worktree was preserved when D filled. `.gitattributes` pins shell scripts to LF; existing raw CRLF on the Windows filesystem is not a Linux deployment artifact. Git blobs and future checkouts must honor the LF policy.
