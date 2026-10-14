# Integrity refuter

**FAIL — commit gate.** Focused command:

`python -m pytest -q tests/v3/test_verify.py tests/v3/test_transport.py tests/v3/test_remote.py tests/v3/test_mover.py`

Output: `80 passed in 3.87s` (these are baseline claims, not sufficient for the cases below).

Independent command `python C:/Users/User/AppData/Local/Temp/takeout-refuter/acceptance.py` produced:

* opaque exact-size `.zip`: `SIZE_OK, True`; corrupt exact-size `.tgz`: `SIZE_OK, True` — size alone verifies invalid archives.
* genuine loose `.html`: `CORRUPT`; obvious login HTML: `CORRUPT` — no supported distinction.
* injected second hash-open failure: `SIZE_OK, True` (`hash read failed...`) — reports verified after failed digest.
* injected ZIP `zlib.error`: `ESCAPED:error`; encrypted `RuntimeError` and unsupported `NotImplementedError` also escaped.

`python .../repro.py` produced `absent_expected_incomplete complete 16 16 None` and `overrun complete 16 16 response exceeded expected size`: transport can mark truncated/overrun streams complete when expected length is absent or an overrun occurs after an exact chunk. It also printed a signed `?token=SECRET` in the 401 `NeedsReauth` exception. **FAIL** credentials are echoed. Repair: enforce Content-Length/stream-error failure, make overrun non-complete, redact all URLs, add extension/format validation and fail-closed hash/ZIP exception handling.

**PASS:** focused remote tests prove `--ignore-existing` plus `hashsum SHA256 --download` rejects same-size wrong hashes; malformed/missing/weak validators, 429 Retry-After, filename/path guards, and mover no-clobber pass. **FAIL/unproven:** remote timeout calls only `p.kill()` (no process-tree/job kill); repair owned-process termination. No live backend/network used.
