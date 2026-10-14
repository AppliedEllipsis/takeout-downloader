# Final canonical documentation gate — FAIL

**Scope:** frozen C checkout `HEAD 21495d9ca4551b614d658178fb299854c2050bdb`; wiki/v3, unit, example, supervisor and CLI reviewed. No tests, duplicate test lane, or server action was run.

## Passed

- Unit/example/supervisor paths align: host-only `/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env` (`root:root 0600`) under `root:root 0700`, outside worker PRIVATE and `/work`/`/config`; PRIVATE maps to `/config/autopilot`, `20001:20001`, `0700`; unit `ReadWritePaths` is PRIVATE only.
- Current release pages state `Restart=on-failure`, CORE21495 deployment evidence/link, owner-deferred unlock/auth/canary/worker activation, and config-only terminal allowlist limits. Historical ADRs remain append-only and the latest security/supervision entries supersede them.
- Read-only checks: markdown links `106 checked, 0 missing`; changed code/docs secret scan `0` actual-value hits (only placeholders/synthetic fixtures); `git diff --check` exit `0`.

## Exact remaining corrections

1. Remove duplicated CORE21495 banner lines in `.wiki/README.md:3-4` and `.wiki/automation.md:7,9`.
2. Correct stale contradictions in `docs/v3/01-ARCHITECTURE.md:135,237`: it says auto-cancel stays **ON**, while current code/ADR says standing global auto-cancel **OFF** and mint cancellation is post-capture/GUID-scoped. Mark the September design section historical or update it to the current rule.
3. Add the current release-boundary/core-release/`Restart=on-failure` notice to v3/01, or explicitly exclude that page from “current” documentation; it currently lacks that boundary.

These are documentation-only blockers; no credential/auth/canary/worker success is claimed.
