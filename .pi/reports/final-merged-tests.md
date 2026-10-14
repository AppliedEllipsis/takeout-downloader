# Final merged-tests report

**Checkout:** `C:/Users/User/_projects/takeout-downloader-reliability`
**Agent scope:** import-root test fixes, the checkout import-origin guard, and
`deploy/takeout-autopilot-watch.service`. No commit, push, deploy, unit enable,
rclone unlock, Google sign-in, mint, download, password attempt, or worker
activation was performed. The original `D:` checkout was not edited by this
lane.

## Changes made

- Replaced all four hardcoded `D:` import roots found under `tests/v3` with
  `Path(__file__).resolve().parents[2]`:
  `test_cli.py`, `test_cdp_target.py`, `test_report_attempts.py`, and
  `test_run_destination_guard.py`.
- Added an actual origin guard to `conftest.py`. It puts this checkout first,
  imports `autopilot`, and rejects any loaded `autopilot` package/submodule
  whose `__file__` resolves outside this checkout. The check runs at startup,
  after collection, and before each test. A search of `tests/v3` and
  `conftest.py` found no remaining hardcoded checkout import roots; the
  `container_watch`, `discovery`, and `remote` tests were not changed.
- Set the service to bounded crash recovery, without enabling it:
  `StartLimitIntervalSec=300`, `StartLimitBurst=5`, `Restart=on-failure`, and
  `RestartSec=30`. The unit remains an inactive/deferred deployment artifact;
  no systemd command was run.

## Verification

### Import-root and requested suite

With the old D-worktree path deliberately supplied through `PYTHONPATH`:

```text
PYTHONPATH=D:\_projects\takeout_downloader_script\.claude\worktrees\takeout-autopilot \\
python -m pytest tests/v3/test_cli.py tests/v3/test_discovery.py -q
47 passed in 0.28s
```

The imported-origin guard therefore selected the C checkout rather than the D
checkout while the contamination path was present.

Required actual-checkout command:

```text
python -m pytest tests/v3 tests/v2 -q
1025 passed, 19 skipped, 1 failed in 99.94s
```

The sole failure is the out-of-scope existing
`tests/v3/test_container_watch.py::test_deployment_keeps_container_cdp_private_copy_and_no_restart` assertion that the unit contain `Restart=no`. That assertion directly conflicts with this task's required `Restart=on-failure`; the container-watch test and producer are owned by the independent repair lane and were not modified here. Excluding that out-of-scope test, the same command completed:

```text
python -m pytest tests/v3 tests/v2 -q --ignore=tests/v3/test_container_watch.py
998 passed, 6 skipped, 1 warning in 100.16s
```

This report does **not** claim that the independent container-watch alias
repair is covered; that work was not changed or tested by this lane.

### Broader/all-test attempts and separate legacy collection issues

- `python -m pytest -q` stopped during collection on the duplicate basename
  `test_review_fixes`: `manager/tests/test_review_fixes.py` was imported when
  pytest collected `tests/v3/test_review_fixes.py`.
- `python -m pytest --import-mode=importlib -q` instead exposed 12 legacy
  collection errors from v3 tests importing sibling fixtures as bare modules
  (`test_integration`, `fake_cdp`, `fake_http`, and
  `orchestration_fixtures`). No out-of-scope tests were altered to mask these
  issues.

### Smoke/static checks

- `python -m compileall -q autopilot tests/v3 tests/v2`: passed.
- `python -m autopilot run --help`: exit 0.
- `python -m autopilot watch --help`: exit 0.
- `python tools/container_watch.py --help`: exit 0 (help only; no supervisor
  or worker started).
- Direct origin assertion passed:
  `autopilot.__file__` resolved to
  `C:\Users\User\_projects\takeout-downloader-reliability\autopilot\__init__.py`.
- `git diff --check`: passed.

## SHA-256 evidence

Hashes are for the files owned/changed by this lane at report time:

```text
1785c3f710c935bb9a7661a619fd5babc433c04138a870e1f74dea8b65338760  conftest.py
45da3a71107792b8e0c9ede0b4846f29c4964c72753c858bff144d0ef4571fca  tests/v3/test_cli.py
4709c1c41dc33c19717ae6cd87d0553763003c5db1dfdcdde96499e75165faec  tests/v3/test_cdp_target.py
130112a327b39d00c6c904506c951a1888e81977bff8a451deb2a2e971b6d9af  tests/v3/test_report_attempts.py
b9328270b22f4522fdd465f3d8a6965ebed2643a33c308292421ed4fe025c282  tests/v3/test_run_destination_guard.py
a6478f515cda66721c89785330809da0ee9439cf935705c4a2f03be15711ebf7  deploy/takeout-autopilot-watch.service
```

The report intentionally records the current merged-checkout evidence and the
known out-of-scope test conflict; it does not certify a final frozen gate or
alias-repair coverage.
