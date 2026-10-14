# Alias preflight repair — offline evidence

Canonical checkout: `C:/Users/User/_projects/takeout-downloader-reliability`.
Only written files: `tools/container_watch.py`, `tests/v3/test_container_watch.py`, and this report. Existing unrelated changes were preserved. No unit/docs/other-test edits, staging, commits, deployment, production Docker/rclone, real unlock, Google access, or production worker activation occurred. All executables/configs/processes used for execution tests were synthetic fixtures.

## Disposition

**Reproduced HIGH bypass repaired; final C v3/v2 and nonroot WSL gates are green. Production activation remains deferred.**

Before editing, I read `final-linux-exec-verifier.md` and executed its independent pidfd/alias/preflight reproduction verbatim by extracting its fenced Python into WSL. All five failures reproduced: cache to `/tmp/local`, hasher to `missing:path`, compress cycling to archives, memory, and madeupbackend were accepted. Actual synthetic `preflight -> supervise` returned 0 and created its worker marker. Baseline source/test hashes matched the verifier.

The validator now permits terminal types **only** from the explicit reviewed allowlist: sftp, s3, b2, azureblob, drive, onedrive, webdav, ftp, gcs, swift. Unsupported cache/hasher/compress are conservatively rejected even when pointing at a named network backend. Local, memory, unknown, inline and multi-remote aggregator types are refused. Named alias/crypt/chunker traversal, missing-reference and cycle detection are preserved. Non-string types and non-named destinations also fail closed. No cleanup, pidfd, environment, secret handling or dedicated-UID code was changed.

The shared **38-case refusal table** covers every reproduced bypass, malformed config, named cycles/missing references, unsupported wrappers, local/inline/multi-remote cases, and crypt/chunker refusal paths. Every table case also exercises real Linux synthetic config-dump `preflight -> supervise`: nonzero exit, an archive-gate refusal diagnostic, and an absent worker marker while heartbeat stdin remains open. Positive tests cover all ten terminals both directly and through alias -> chunker -> crypt.

Per the owner's subsequent integration instructions, the owned deployment regression now requires bounded crash recovery (`Restart=on-failure`, `RestartSec=30`, `StartLimitIntervalSec=300`, `StartLimitBurst=5`) while retaining deferred/inactive policy assertions and all original private-CDP, dedicated-identity and configuration checks. It also requires exactly one **mandatory** `EnvironmentFile=/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env`, outside every `ReadWritePaths` entry and host/container PRIVATE, `/work` and `/config` paths. UMask/no-new-privileges checks were added; environment-secrecy tests are unchanged. The unit was changed by the security/import integration lanes, not by this lane. Root parent 0700/file 0600 are required provisioning policy, not permissions certified by these offline artifact checks.

These are configuration-structure and startup-gating checks, **not connectivity, authentication, durability, production permissions/identity, or actual encrypted-config validation**. The production encrypted config remains inaccessible and untouched.

## Final fingerprints (SHA256)

| File | SHA256 |
| --- | --- |
| `tools/container_watch.py` | `3a2fa1f7c8fe5ec8c93f373bc97c2a214f47f47155d9bc90292b1ffe8d4004e9` |
| `tests/v3/test_container_watch.py` | `b05d2954365c01284ada7481d50b666f0a5f28fb8f2c4951fa6a720a86587e70` |
| Read-only verifier report | `82e9033ecc5a0e0e2480083f9eaae1beb2ff01810f3c425c5309efddc10b349d` |
| Read-only deployment unit | `3a073ea144716e6cf74dfbd818649b866d2767d7d44bc352426c6a595739d3dc` |
| Read-only C-origin guard `conftest.py` | `855ab46fb3034b4a3baa5ceb992358dc1bea52502665f71ef998953474ef775a` |

Baseline source: `cd90fd6069da8aca879b46147adfc34561cd2c7cbfd37e965281fe94fdd49d21`.
Baseline tests: `5a042b15a69f05f67d63b18b047e7fb813419006ecb769324421ac28b55dd1b2`.

## Exact final suite commands and results

All final suites below ran **after** both deployment-regression integration updates and the concurrent import lane's canonical v3 fixture-search-path update. Source/test/unit/guard hashes were stable across the final full-suite reruns. Git Bash; Windows Python 3.12.7 and WSL Ubuntu-24.04 Python 3.12.3, UID/GID 1000 (nonroot). Existing scoped pytest import path; no installs/config changes.

### Actual C v3/v2 gate

```bash
cd 'C:/Users/User/_projects/takeout-downloader-reliability' && sha256sum tools/container_watch.py tests/v3/test_container_watch.py conftest.py deploy/takeout-autopilot-watch.service && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q -p no:cacheprovider tests/v3 tests/v2 && sha256sum tools/container_watch.py tests/v3/test_container_watch.py conftest.py deploy/takeout-autopilot-watch.service
```

**Exit 0: 1082 passed, 57 skipped, 1 warning in 100.10s.** No test deselections or ignored files. Warning is the installed FastAPI/Starlette test-client deprecation. Earlier direct import assertions printed C-checkout `autopilot/__init__.py` and `tools/container_watch.py` origins. The current merged `conftest.py` C-origin guard was active throughout collection/execution; its final snapshot additionally derives the shared v3 fixture lookup path from this C checkout. Windows skips are not Linux execution evidence.

### Exact verifier nonroot WSL suite

```bash
cd 'C:/Users/User/_projects/takeout-downloader-reliability' && wsl -d Ubuntu-24.04 -- bash -lc 'id; python3 --version; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -p no:cacheprovider tests/v3/test_container_watch.py tests/v3/test_discovery.py' && sha256sum tools/container_watch.py tests/v3/test_container_watch.py conftest.py deploy/takeout-autopilot-watch.service
```

**Exit 0: 173 passed in 54.29s; no skips.** Includes Linux process cleanup/lease/preflight tests and discovery; no other tests edited.

### Focused final Linux alias gate

```bash
wsl -d Ubuntu-24.04 -- bash -lc 'test "$(id -u)" -ne 0; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -p no:cacheprovider tests/v3/test_container_watch.py -k "alias or destination"'
```

**Exit 0: 101 passed, 34 deselected in 27.28s; no skips.** Includes all 38 real startup-marker refusal regressions.

### Windows focused file

```bash
cd 'C:/Users/User/_projects/takeout-downloader-reliability' && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q -p no:cacheprovider tests/v3/test_container_watch.py
```

**Exit 0: 84 passed, 51 skipped in 0.12s.** Linux/Unix execution tests deliberately skip on Windows.

### Historical failures, now resolved by owner-directed regression update

The first repair-only final WSL run had 172 passes and one failure; Windows had 83 passes, 51 skips and one failure. Both were the original `test_deployment_keeps_container_cdp_private_copy_and_no_restart` expecting `Restart=no` against the import lane's unit snapshot `a6478f515cda66721c89785330809da0ee9439cf935705c4a2f03be15711ebf7`. Those failures were reported, not hidden. The owner then expressly directed updating this owned regression for bounded `on-failure` recovery. That intermediate snapshot passed actual C v3/v2 (1082 passed, 57 skipped) and WSL (173 passed). The subsequent security integration moved the EnvFile outside worker-writable PRIVATE; the final hashes and all final results above include the new protected-path assertion.

## Exact independent verifier reproduction, negative expectations only changed

Executed from Git Bash; extraction avoids maintaining a second copy of the verifier. Initial baseline used the same extraction without the four `replace` calls. pidfd and fixture/heartbeat/supervisor behavior are unchanged; acceptance now raises an assertion and the final expected marker/exit status is reversed.

```bash
python -B - <<'PY' | wsl -d Ubuntu-24.04 -- python3 -B -
from pathlib import Path
s=Path('C:/Users/User/_projects/takeout-downloader-reliability/.pi/reports/final-linux-exec-verifier.md').read_text()
section=s.split('## Exact independent pidfd / alias / preflight reproduction')[1]
code=section.split("<<'PY'\n",1)[1].split('\nPY\n```',1)[0]
code=code.replace("print('FAIL alias gate ACCEPTED',kind,'target',remote)", "raise AssertionError(('alias gate ACCEPTED',kind,remote))")
code=code.replace("print('FAIL backend gate ACCEPTED',kind)", "raise AssertionError(('backend gate ACCEPTED',kind))")
code=code.replace("print('FAIL synthetic unresolved hasher preflight: rc',rc,'synthetic-marker-created',marker.exists())", "print('PASS synthetic unresolved hasher preflight: rc',rc,'synthetic-marker-created',marker.exists())")
code=code.replace('assert rc==0 and marker.exists()', 'assert rc!=0 and not marker.exists()')
print(code)
PY
```

Exit 0:

```text
UID 1000 pidfd available True
PASS real pidfd stale-starttime no-op; matching-starttime TERM; vanished-PID no-op
PASS alias gate refused cache
PASS alias gate refused hasher
PASS alias gate refused compress
PASS backend gate refused memory
PASS backend gate refused madeupbackend
PASS synthetic unresolved hasher preflight: rc 1 synthetic-marker-created False
No production process/config/network accessed
```

## Unchanged independent subtree-cleanup challenge

```bash
python -B - <<'PY' | wsl -d Ubuntu-24.04 -- python3 -B -
from pathlib import Path
s=Path('C:/Users/User/_projects/takeout-downloader-reliability/.pi/reports/final-linux-exec-verifier.md').read_text()
section=s.split('## Exact additional subtree cleanup challenge')[1]
print(section.split("<<'PY'\n",1)[1].split('\nPY\n```',1)[0])
PY
```

Exit 0:

```text
PASS eof double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive; 3 owned IDs
PASS lease double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive; 3 owned IDs
PASS host-sigkill double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive; 3 owned IDs
Offline process cleanup challenge complete
```

Final read-only SHA256 checks confirmed the listed final source/test fingerprints after testing. All final requested offline suites pass. Production provisioning, protected host-secret ownership/modes, installed-unit disabled state, unlock/connectivity/durability and activation remain unverified and deferred.
