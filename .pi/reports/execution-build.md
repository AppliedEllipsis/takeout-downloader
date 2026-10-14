# Execution build — offline implementation complete; activation deferred

## Scope / verified premise

Canonical edited checkout: `C:/Users/User/_projects/takeout-downloader-reliability`.
Read `.pi/workflows/finish-release.md` and `.pi/reports/server-preflight.md` first.
`cd C:/Users/User/_projects/takeout-downloader-reliability && git status --short && pwd` confirmed the canonical C checkout and existing changes in other lanes. Those changes were preserved. The requested supervisor and its test did not exist; discovery, example/unit and init script were read before editing. Original `D:/_projects/takeout_downloader_script` was not edited.

Prior evidence records host CDP proxy resets and working container-local CDP. This build implements that decision; **no new live CDP/server probe was performed**. No commit, push, deployment, delegation, service enable/start/restart, Docker exec against a real container, profile copy, password/unlock attempt, Google sign-in, mint or real download was performed. No live backend or export success is claimed. Current live identities/export counts and live disabled/inactive service state were not re-probed; no activation command was issued.

Only the seven assigned code/config files and this requested report were written.

## Implementation

- `tools/container_watch.py`: foreground host bridge using `/usr/bin/docker exec -i`, no detach/TTY/new port. Existing `/opt/manager-venv/bin/python` runs the inside guardian from `/work/.v3`; watcher uses that same interpreter and checkout. The host controller needs Docker authority; worker runs as dedicated **numeric UID:GID 20001:20001**, not root and not granted Docker-group access. This number must be reserved/verified and provisioned by the deployment owner; no identity was created here.
- Docker forwards only allowlisted **environment variable names** (`RCLONE_CONFIG_PASS`, Telegram names). Secret values never enter supervisor argv or its diagnostics. The worker environment is reconstructed, removing unrelated container secrets, user PYTHONPATH and arbitrary RCLONE config overrides. The initial guardian runs isolated Python; worker disables user site imports. Cache/home/tmp paths are under container-private `/config/autopilot`.
- Nonblocking host heartbeat writes avoid hanging on a full Docker stdin pipe. Stop/exception closes stdin and waits for guardian cleanup; abrupt parent death closes the pipe. An independent **8-second inside lease** handles a stalled/disconnected stream even without delivered EOF.
- Linux guardian sets process-local child-subreaper and no-new-privileges before starting preflight or worker. `/proc` ancestry plus starttime-checked pidfds identifies **only owned descendants**. TERM grace then repeated KILL/reap scans cover worker failure, EOF, stop, lease expiry, orphaned descendants and separate descendant sessions. No broad `pkill`, UID-wide or executable-name signalling is used.
- Preflight runs **before watcher launch**: private owned directory, non-symlink paths, restricted file modes/ownership, fixed copied binary/config paths, encrypted config header, then bounded `rclone config dump --config ... --ask-password=false` with stdin `/dev/null`. Both sensitive output streams are captured, bounded and suppressed. Alias/crypt/chunker references must resolve to a nonlocal backend; missing references/cycles/local/inline/unresolved multi-remote configurations fail closed. This is a local config/alias check, **not a backend durability or connectivity check**. Tests used only synthetic executables/data, never production rclone/unlock material.
- `deploy/takeout-autopilot-watch.service`: root host controller, private root-readable EnvironmentFile, encrypted mount conditions, explicit nonroot container identity, `Restart=no`, `TimeoutStopSec=25`, `KillMode=control-group`, existing sandbox. Comments explicitly retain disabled/inactive activation gate. Container process cleanup does **not** rely on the host unit cgroup.
- `deploy/watch.example.json`: encrypted host PRIVATE maps to `/config/autopilot`; rclone paths now `/config/autopilot/bin/rclone` and `/config/autopilot/rclone.conf`, loopback CDP stays `127.0.0.1:9222`, direct remote/local-view roots unchanged, 100 GiB reserve plus 5 GiB floor unchanged. Account ID remains a deliberately invalid placeholder.
- `autopilot/discovery.py`: default/config URL mode allows only an explicitly matching numeric `user`. Runtime mode validates single-valued `rapt`, numeric `authuser`, bounded pagination keys; unknown, duplicate, blank/malformed/control-bearing keys fail without echoing values. Queryless/user-less observed links/locations can be bound only after independent manage DOM identity verification. Account slots/proof tokens never attest identity; archive UUID/location and part user/UUID remain independently checked. Runtime proofs may be used for that in-memory navigation, but returned references are canonical **user-only URLs**, stripping proof/routing/pagination tokens before watcher refs/state. Config continues to reject runtime keys.
- `webgui/init_custom.sh`: managed `autoRecapture=false`. Also normalized this assigned shell file to LF after real Linux syntax checking exposed original CRLF heredoc delimiters. Git textual diff remains only the requested boolean change. Script was syntax-checked, not executed.

## Exact final verification commands / results

All Windows checkout commands below ran from the canonical C root. `-B` and disabled pytest cache avoided source bytecode/cache writes.

```bash
cd C:/Users/User/_projects/takeout-downloader-reliability && python -B -m pytest -q -p no:cacheprovider tests/v3/test_container_watch.py tests/v3/test_discovery.py tests/v3/test_watch.py tests/v3/test_orchestration_reliability.py tests/v3/test_extension_defaults.py
```

**129 passed, 13 skipped in 6.80s; exit 0.** Skips explicitly mark Linux process/mode semantics unconfirmed on Windows.

```bash
cd C:/Users/User/_projects/takeout-downloader-reliability && python -B -c "from pathlib import Path; files=('tools/container_watch.py','autopilot/discovery.py','tests/v3/test_container_watch.py','tests/v3/test_discovery.py'); [compile(Path(p).read_bytes(),p,'exec') for p in files]; print('compile: 4 files OK')"
```

**`compile: 4 files OK`; exit 0.** Compilation in memory, no pyc output.

```bash
cd C:/Users/User/_projects/takeout-downloader-reliability && git diff --check -- tools/container_watch.py tests/v3/test_container_watch.py autopilot/discovery.py tests/v3/test_discovery.py deploy/takeout-autopilot-watch.service deploy/watch.example.json webgui/init_custom.sh
```

**No output; exit 0.** Several assigned files are untracked in this inherited worktree, so this check is supplemented by real tests and compilation above.

```bash
wsl -d Ubuntu-24.04 -- bash -lc 'cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -p no:cacheprovider tests/v3/test_container_watch.py tests/v3/test_discovery.py && bash -n webgui/init_custom.sh && systemd-analyze verify deploy/takeout-autopilot-watch.service'
```

**Linux/WSL Python 3.12.3: 79 passed in 15.20s; shell syntax and unit verification exit 0.** No Linux packages installed: pytest's existing pure-Python Windows site-packages were exposed only through this command's environment, plugin autoload disabled. Unit verification emitted these WSL NTFS mode warnings:

```text
Configuration file .../deploy/takeout-autopilot-watch.service is marked executable. Please remove executable permission bits. Proceeding anyway.
Configuration file .../deploy/takeout-autopilot-watch.service is marked world-writable. Please remove world writability permission bits. Proceeding anyway.
```

Deployment must install the unit root-owned **0644**; no host filesystem/systemd configuration was changed to silence WSL mount-mode warnings.

Linux process tests actually spawned offline Python processes and verified EOF, SIGTERM stop, expired heartbeat, worker exit failure, abrupt host writer SIGKILL, TERM-ignoring descendant sessions, grandchildren, full heartbeat pipe, and unrelated-process survival. Synthetic preflight tests covered locked/error, timeout and output-flood executables with detached descendants, suppressed sensitive-looking output and absence of worker-start marker. Other tests cover env/argv secrecy, strict config, runtime query compatibility, independent user/UUID mismatch and returned-reference redaction.

**Linux-only claims remain explicitly unconfirmed on native Windows.** WSL confirms the implemented Linux primitives with synthetic processes, not real Docker stdin/shim propagation, production UID/mount permissions, the deployed manager interpreter or actual systemd stop wiring. Those need separate approved production checks without worker activation.

## Intermediate observations / fixes (not concealed)

- `wsl -d Ubuntu-24.04 -- bash -lc 'cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && python3 --version && python3 -m pytest --version'`: Python 3.12.3; `No module named pytest`, exit 1. Reused existing pure-Python packages via command-scoped PYTHONPATH instead of installing/changing global configuration.
- First Linux focused run with the final-form WSL pytest environment but the then-current tests: **71 passed, 1 failed**. Worker-failure fixture exited after a fixed 0.5-second startup sleep before the detached child could publish its PID; changed fixture to wait for the child marker before exiting and extended its short test lease. This was a test synchronization issue, not accepted cleanup evidence. Subsequent run **72 passed**.
- After adding real synthetic preflight/extra env tests: **76 passed** on Linux; then `bash -n webgui/init_custom.sh` failed (`line 120: syntax error: unexpected end of file`, exit 2). The assigned file had **118 CRLF lines**, breaking Linux heredoc terminators. Normalized that file to LF; real Linux shell check then passed. Windows/MSYS syntax checking alone had not detected this.
- Two repeated Linux runs after nonblocking heartbeat/private permissions additions: **78 passed in 15.93s** and **78 passed in 16.34s**. Final pagination/config-strictness test addition yields the **79 passed** result above.
- Final scoped `git status --short -- tools/container_watch.py tests/v3/test_container_watch.py autopilot/discovery.py tests/v3/test_discovery.py deploy/takeout-autopilot-watch.service deploy/watch.example.json webgui/init_custom.sh`: init script modified; other six assigned files untracked, consistent with inherited state/new files. No staging/commit performed.

## Build fingerprint

Exact command:

```bash
cd C:/Users/User/_projects/takeout-downloader-reliability && python -B -c "import hashlib; from pathlib import Path; files=('tools/container_watch.py','tests/v3/test_container_watch.py','autopilot/discovery.py','tests/v3/test_discovery.py','deploy/takeout-autopilot-watch.service','deploy/watch.example.json','webgui/init_custom.sh'); [print(hashlib.sha256(Path(p).read_bytes()).hexdigest(),p) for p in files]"
```

```text
cd90fd6069da8aca879b46147adfc34561cd2c7cbfd37e965281fe94fdd49d21 tools/container_watch.py
5a042b15a69f05f67d63b18b047e7fb813419006ecb769324421ac28b55dd1b2 tests/v3/test_container_watch.py
93d601df0280893937268eb66a52234954d3b70a332e2306fc88011b3219130b autopilot/discovery.py
79a2a4f25ce29adc9d8f9ae8a14200d91e78b157174f037c2c47b4ea1db5b5c3 tests/v3/test_discovery.py
76b7cce295d15b569da05bdf3f070bbc610e026ac2ab06ed58e2de46cb1aca19 deploy/takeout-autopilot-watch.service
3d6b5a73e5cc25ccb10643cb2e1b4217c5f7587a83392d31071bd72901a258a7 deploy/watch.example.json
f8615cfcfa6fc46660a110c55b02ca71ee01ecf1872cacb530320e1bc769f878 webgui/init_custom.sh
```

## Remaining owner/commander gates — not executed

1. Whole-suite frozen/merged verification and secret review in the commander lane; this scoped result is not a full release certification.
2. Reserve dedicated service UID/GID (unit selects 20001), verify it is not the browser/manager/shared UID and has no Docker-group grant. PRIVATE must be its own 0700 directory with accessible encrypted parent traversal; watch.json/encrypted rclone.conf 0600, trusted copied rclone regular executable not group/world writable. Root-private host credentials.env stays root 0600 and is forwarded by names, not weakened to worker-readable. Provision private cache/tmp/state/staging as needed.
3. Trusted byte/hash-verified copy of actual approved rclone to host `PRIVATE/bin/rclone`, encrypted ciphertext to `PRIVATE/rclone.conf`; both already map inside `/config/autopilot`. No copied binary/config/credentials were provisioned by this build. Noninteractive preflight will refuse startup without an authorized unlock and resolvable alias.
4. Verify production interpreter imports, mapped checkout/private mount, numeric identity, loopback CDP read-only, and unit syntax/permissions. No port repair, container/browser restart or profile copy is needed/authorized.
5. Owner separately supplies unlock and Google sign-in/verified account identity. Authenticated DOM compatibility, backend access/durability and fresh export availability remain unconfirmed. **Keep host systemd disabled/inactive; do not launch worker, mint or transfer until separate authorization.**
