# CORE21495 — deployed core evidence, not activation

[Server deployment report](../.pi/reports/server-core-deploy.md) records
2026-10-13T22:00:42Z deployment of approved
`21495d9ca4551b614d658178fb299854c2050bdb`.

- Clean detached server `.v3` fast-forwarded `1b70ce2` → `21495d9`; remains clean.
- Container imports resolve `/work/.v3/autopilot/__init__.py` and
  `/work/.v3/takeout2/__init__.py`; run/watch/top-level help checks passed.
- LF-normalized Git-blob module fingerprints: **36 checked, 0 mismatches**.
- Dirty main remains `c0e3b8d`, branch `feat/internal-downloader`, six unchanged
  entries: four modified helpers JS files, untracked `.v3/` and
  `helpers.bak-1790097405/`.
- Final bounded process check found no watcher/run/manager jobs/takeout2 worker.
  Chromium/rclone were untouched; no restart, CDP, unlock, canary or export work.

Executor/unit installation is deferred. Unit remains disabled/inactive;
activation requires owner authorization, protected host-only credentials and
identity/backend gates in [automation](automation.md).

The [core verifier](../.pi/reports/final-core-verifier.md) snapshot
`20261013T212824394139` reported **974 passed, 6 skipped** before separate
supervisor/runtime-token additions. It is not a later merged-checkout result;
no final count is predicted. See [evidence index](reliability-audit.md).
