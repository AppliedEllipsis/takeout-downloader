# Watcher build evidence

Working directory: `D:/_projects/takeout_downloader_script/.claude/worktrees/takeout-autopilot`.
Read `.pi/workflows/build-contract.md` first, then `docs/v3/05-WATCH-INTERFACE.md`. No commit, deployment, wiki edit, real browser/server/export access, or archive/ledger mutation. Tests used temporary files and injected offline services.

## Owned paths implemented
- `autopilot/watch.py`: strict config validation, loopback CDP, production remote, atomic whitelisted queue/status, Windows/POSIX OS lock held across ticks, restart/dedup, pending handling, independent account backoff, terminal blockers, mint URL batch separated from cached-only one-part transfers, preserved needs-reauth, sanitized optional bounded Telegram notifications. Default runner injects an isolated target session instead of the engine's human-tab selection.
- `autopilot/discovery.py`: manage links then archive metadata; verified DOM/part identity, explicit user-selected URLs, UUID checks, bounded pagination/export enumeration, no download/mint clicks; disposable automation target closed on success/failure.
- `autopilot/__main__.py`: standalone watch dispatch and sanitized refusal, unchanged run/report dispatch.
- `requirements-autopilot.txt`: websocket-only external runtime dependency.
- `deploy/watch.example.json`, `deploy/takeout-autopilot-watch.service`: configuration template and host unit; **not installed**. Placeholder user identity intentionally refuses execution until replaced.
- `tests/v3/test_watch.py`, `tests/v3/test_discovery.py`: new offline regressions.

## Executed commands / measured results
From the working directory above:
1. `python -m pytest -q tests/v3/test_watch.py tests/v3/test_discovery.py tests/v3/test_cli.py tests/v3/test_exit_codes.py` — final **47 passed**, 0 skipped, 0 failures (0.40s).
2. `python -m compileall -q autopilot/watch.py autopilot/discovery.py autopilot/__main__.py` — success.
3. `python -m pytest -q tests/v3` — **398 passed, 6 skipped, 3 failed** (93s), during concurrent integration. Failures exclusively in untouched `test_mover_watchdog.py`: watchdog diagnostic assertion, copy-child monkeypatch assuming every subprocess is a copy (new hash child has fewer arguments), and cannot-start diagnostic assertion. These are not silently fixed outside ownership.

The initial discovery test attempt exposed missing pytest-asyncio and skipped coroutines; tests now use standard-library asyncio wrappers and actually execute. A remote traversal regression initially failed and was corrected before final verification. Lock contention includes a separate interpreter process. Other evidence includes two accounts, pending-to-ready, restart/dedup, reauth/cached-transfer fairness, persisted account backoff, zero-part false success, mint-only URL earning (not durable completion), dry-run without filesystem/browser/runner effects, unsafe pagination and identity blockers, notification failure tolerance, and state redaction.

## Integration / unconfirmed boundaries
The frozen RunConfig additions were absent initially; later inspection found **no missing fields**, and final watcher tests construct the **real RunConfig**, not a replacement. Discovery/runner/network remain injected in offline tests; no actual download, rclone backend durability, host systemd activation, mount encryption, or live Google DOM identity markup was verified. Identity absent/ambiguous on a manage page fails closed; empty pending archive pages never execute. Operators must provision encrypted local state/staging and real identity/remote configuration. The full v3 suite is not claimed green; its three out-of-lane failures require integration-owner follow-up. No password tools are imported or automatically invoked.
