# Baseline Test Report — takeout-autopilot

Root: `D:/_projects/takeout_downloader_script/.claude/worktrees/takeout-autopilot`
Date: audit run; interpreter: Python 3.12.7, pytest 7.4.4 (anaconda).
No source, install, git, network, browser, archive or server actions taken.

## Command run

```
python -m pytest tests/v3 tests/v2 -q -p no:cacheprovider
```

- **Result: `797 passed, 6 skipped, 1 warning in 94.48s`** (exit code 0).
- No failures, no errors, no collection errors, no dependency/import errors.
- Run was network-free: the only socket-opening file is gated behind
  `AUTOPILOT_LIVE=1` (`tests/v3/test_http_client_live.py:56-59`).

## Skips (6) — all intentional, no work attempted

Every skip is a live-network test in `tests/v3/test_http_client_live.py`
(`live = pytest.mark.skipif(not LIVE, ...)`, line 56), confirming
`AUTOPILOT_LIVE` was unset:

| Line | Reason |
|------|--------|
| 131, 147, 159, 180, 196, 225 | `live network test — set AUTOPILOT_LIVE=1 to run` |

No Google host, loopback server, browser or archive was contacted.

## Warning (1) — third-party, not repo code

`C:\Users\User\anaconda3\Lib\site-packages\fastapi\testclient.py:1`:
`StarletteDeprecationWarning: Using 'httpx' with 'starlette.testclient' is
deprecated; install 'httpx2' instead.` — dependency-level only.

## CLI --help / import smoke (CONFIRMED)

- `python -c "import autopilot, autopilot.__main__, takeout2"` → `import OK`
  (exit 0). Resolved to the worktree `autopilot/__init__.py`.
- `python -m autopilot --help` → usage `{-h} {run,report}`, exit 0.
- `python -m autopilot report --help` → usage printed, exit 0.

## Clean sections

- Dependency issues: none observed.
- Skipped non-network tests: none (the `skipif` guards in
  `tests/v2/test_cli.py:385` and `tests/v2/test_preflight.py:170` did **not**
  trigger under this environment).
- Infinite/network work during the run: none observed within the 1200s timeout.

## SUSPECTED
- None. The 6 skips are the only unexecuted tests; they cover the sole
  socket-opening component (`autopilot/transport.py::UrllibHttpClient`), so its
  `_NoRedirect`/`_UrllibStream` paths remain unverified until `AUTOPILOT_LIVE=1`
  is deliberately run against a non-Google host.

## Summary (top findings)

The v3+v2 baseline is green: 797 passed, 6 skipped, 0 failures/errors in ~95s,
exit 0. All six skips are the network-gated live tests in
`tests/v3/test_http_client_live.py` (AUTOPILOT_LIVE unset) — no network was
attempted and no Google/browser/archive/server was touched. The single warning
is a third-party Starlette/httpx deprecation, not repo code. CLI imports and
both `--help` paths succeed. No dependency problems, no infinite work, no
timeouts. The only residual risk is SUSPECTED-only: the live HTTP transport
paths remain unexecuted by design. Baseline is sound for builds to begin.
