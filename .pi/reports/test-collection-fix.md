# Test collection repair (interim)

Checkout: `C:/Users/User/_projects/takeout-downloader-reliability`, HEAD `21495d9`; Python `C:/Users/User/anaconda3/python.exe`.

Only `conftest.py` and new `pytest.ini` changed by this lane, plus this report. Default importlib collection separates duplicate basenames; conftest explicitly inserts checkout-derived `tests/v3` for existing bare sibling-fixture imports, after the checkout root. The actual autopilot-origin guard and its hooks are unchanged. No tests excluded, renamed, or assertions weakened.

Commands below ran from that checkout:

- Before repair: `python -m pytest --collect-only -q`: 1326 collected, one collision (`manager/tests/test_review_fixes.py` versus `tests/v3/test_review_fixes.py`).
- Before repair: `python -m pytest --collect-only -q --import-mode=importlib`: 1161 collected, 12 sibling-import errors (`test_integration`, `fake_cdp`, `fake_http`, `orchestration_fixtures`).
- After repair: `python -m pytest --collect-only -q`: **1340 collected**, zero errors.
- `python -m pytest tests/v3 tests/v2 -q`: **1082 passed, 57 skipped**, one warning; 100.53s.
- `python -m pytest -q`: **1283 passed, 57 skipped**, five warnings; 118.07s.
- `python -c "import conftest, autopilot; print(autopilot.__file__); conftest._assert_autopilot_origins()"`: canonical C-checkout origin confirmed.

Residual failures: **none**. Warnings concern existing FastAPI/Starlette deprecations. `AUTOPILOT_LIVE` was unset; external live tests retained their existing opt-in skips. Reviewed executable network paths used mocks or loopback fixtures; no external requests, production edits, installs, global configuration, Git mutations, auth, or deployment performed.

Results are **interim**, not a final merged gate: alias-helper producer changes remain independently owned/concurrent.
