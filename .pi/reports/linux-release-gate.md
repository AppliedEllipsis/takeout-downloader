# Linux release gate

Scope: `feat/takeout-autopilot`, HEAD `21495d9ca4551b614d658178fb299854c2050bdb`; no production files changed. Relevant tail of `mount-boundary-repair.md` recorded the native-tempdir mount failure and descendant ready-marker race.

Changes: mint-only fixtures now explicitly pass `require_mount=False`; second pass asserts transfer and zero remint. Descendant fixture uses bounded ready polling and a 4-second child marker delay, then retains marker and process-termination assertions for Windows jobs and POSIX. Added `.gitattributes`: `*.sh text eol=lf`.

Hashes (SHA-256):
- `tests/v3/test_mint_only.py` `97d8463f51b98614f32212bcc451cbcfa02e569406df32b4bbbae943e5aedda8`
- `tests/v3/test_remote.py` `e9db48bcab43ab1b5c8d83a5414bcd3b277d3718712801f275155ede836388b1`
- `.gitattributes` `67fa27cb83434db743630986f0b784a89c8acee9430e67dabbfe60d94fb8f6ab`

Windows (`AUTOPILOT_LIVE` unset): `cd /c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 AUTOPILOT_LIVE= python -B -m pytest -q -rs tests/v3/test_mint_only.py tests/v3/test_remote.py` => **39 passed**.

WSL isolated venv: `/home/user/tmp/takeout-linux-release-venv/bin/python`; no `PYTHONPATH`/`PYTHONHOME`. Origins were verified for pytest, autopilot, websockets, websocket-client, requests, httpx, fastapi, pydantic/pydantic_core, dotenv, yaml, cryptography; project imports resolved from `/mnt/c/Users/User/_projects/takeout-downloader-reliability`.

Exact Linux v3/v2 command (`AUTOPILOT_LIVE` unset): `wsl -d Ubuntu-24.04 -- bash -lc 'set -o pipefail; unset PYTHONPATH PYTHONHOME AUTOPILOT_LIVE; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability; /home/user/tmp/takeout-linux-release-venv/bin/python -m pytest -q -rs tests/v3 tests/v2'` => **1211 passed, 6 skipped, 1 warning**. Exact all-project command with test-only `STORAGE_ROOT=/tmp/takeout-release-storage-2`: `wsl -d Ubuntu-24.04 -- bash -lc 'set -o pipefail; unset PYTHONPATH PYTHONHOME AUTOPILOT_LIVE; export STORAGE_ROOT=/tmp/takeout-release-storage-2; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability; /home/user/tmp/takeout-linux-release-venv/bin/python -m pytest -q -rs .'` => **1410 passed, 8 skipped, 5 warnings**. Skips: six live-network tests (live flag unset), two lupa tests (lupa absent). No residual failures.

Shell evidence: four unchanged scripts remain CRLF in the Windows worktree while Git blobs are LF (`chromium-service.sh`, `setup-tunnel.sh`, `verify-exposure.sh`, `manager-service.sh`); normalized in-memory `bash -n` passed, raw DrvFS checks remain known failures. No shell rewrite was made; policy prevents recurrence.
