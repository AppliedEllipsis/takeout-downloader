# Canonical documentation gate — FAIL

Verified canonical root first; no D checkout access or tests performed.

Executed:
```sh
if [ -d 'C:/Users/User/_projects/takeout-downloader-reliability/.git' ]; then echo '.git directory: present'; else echo '.git directory: MISSING'; fi
 git -C C:/Users/User/_projects/takeout-downloader-reliability rev-parse HEAD
```
Output: `.git directory: present`; `21495d9ca4551b614d658178fb299854c2050bdb`.

Completely read all eleven requested documentation files using explicit canonical C paths. Compared supervisor, deployment templates, argparse, RunConfig and WatchConfig directly; no CLI execution, pytest, server/service/auth/data actions.

## Required corrections

- **`.wiki/automation.md:30`**: replace `Restart=no` with `Restart=on-failure`, bounded by `RestartSec=30` and StartLimit settings. Evidence: `deploy/takeout-autopilot-watch.service:11–12,27–28`. Disabled/inactive activation gate remains unchanged.
- **`.wiki/reliability-audit.md:43,57–59`**: add the missing `server-core-deploy.md` evidence-index entry; distinguish completed CORE deployment from still-deferred executor/unit installation. Report `server-core-deploy.md:4–5,20–25,44–61,65` proves approved CORE21495 deployment/import/help checks, not worker activation.
- **`.wiki/README.md:16–17`, `.wiki/automation.md:7–18`, `.wiki/infrastructure.md:24–30`, `docs/v3/03-OPERATIONS.md:35–36`**: add current CORE21495 deployed status/link. Preserve dated preflight observations and docs-lane-only “not executed” statements; do not present completed CORE update as solely future work. Append journal evidence after `.wiki/journal-2026-10.md:117`.
- **`.wiki/credentials.md:23`**: remove group-traversal exception for PRIVATE itself: supervisor requires worker-owned PRIVATE with no group/other bits (`tools/container_watch.py:242–251,369`). Ancestor traversal is a separate check.

Changed source/docs-only secret-pattern scan (including changed tests; excluding reports/private config): **25 files, one candidate**, `webgui/init_custom.sh:22`, an environment-derived default expression, not a literal credential. No credential files read. Requested-page relative links all resolve.

Alias repair already explicitly rejects unsupported wrappers and unknown/memory/local terminals (`tools/container_watch.py:268–280`); no duplicate repair finding. No unlock/auth/canary/real export/activation inferred.
