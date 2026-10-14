# Documentation corrections

Canonical C checkout only; no D access by this lane.

Changed paths:
- `.wiki/{README,architecture,automation,credentials,infrastructure,reliability-audit}.md`
- `.wiki/journal-2026-10.md` and `.wiki/decisions.md` (append-only)
- `.wiki/core-release.md` (new deployment evidence summary)
- `docs/v3/{README,02-RUN-INTERFACE,03-OPERATIONS,04-FULL-PULL,05-WATCH-INTERFACE}.md`
- `.pi/reports/doc-corrections.md`

Read canonical-doc-review.md/server-core-deploy.md; reconciled CORE21495 deployment, exact journal timestamp/SHA/fingerprints, preserved dirty-main six entries and deferred executor/unit installation. Documented mandatory host-only protected EnvironmentFile, strict worker PRIVATE permissions, replacement/injection risk, bounded restart policy and reviewed-network-only config preflight. Historical entries retained; appended security supersession ADR.

Read-only checks: 87 relative links resolve; static AST argparse comparison passed (built-in `--help`, external rclone flags accounted separately); five unit-reference assertions passed; scoped `git diff --check` passed. Initial static check flagged built-in `--help`; corrected validator classification, not source. No tests, CLI execution, source/unit edits, Git mutations, server/auth/data operations or credentials creation.

Unresolved: final merged-checkout gate/count, executor/unit installation and live stop/permissions evidence; authenticated discovery, backend connectivity/canary/durability and activation remain unproven/deferred. Preserved 974-pass/6-skip immutable-core snapshot boundary; no merged total predicted.
