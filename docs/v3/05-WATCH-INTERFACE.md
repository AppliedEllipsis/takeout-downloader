# Unattended watcher contract

**Specified 2026-10-13.** This describes the acceptance criteria for the new unattended layer, not a claim that a fresh Google export has already passed them.

## User-facing surface

```text
python -m autopilot watch --config PATH
python -m autopilot watch --config PATH --once
python -m autopilot watch --config PATH --once --dry-run
```

The existing `run` and `report` commands and exit codes remain compatible. A continuously supervised watcher discovers exports, drives the existing one-export engine and records sanitized operational status. It does not create exports, delete source data, bypass Google authentication, or manipulate human-started browser downloads.

## Acceptance gates

1. **Explicit account identity.** A configured account has a safe storage label and a Takeout manage URL identifying the authenticated account. The discovered archive URL/user must match the configured identity before it can be scheduled. Never silently download one account under another account's label.
2. **Discover, don't guess.** Read archive links from the manage page. New pending exports stay pending until the archive page has actual downloadable parts. Pending/Google-failed/empty pages are never called complete. Do not mint merely to determine readiness.
3. **Local persistent control state.** Ledger, watcher state and lock live on local encrypted disk, not a FUSE mount. State survives restart; writes are atomic. A single-instance OS lock prevents overlapping workers on the same state. Never persist cookies or reauth tokens in watcher logs/state.
4. **Bounded, fair work.** Use cached URLs first and bounded per-export passes. Transient failures receive bounded exponential backoff. Needs-reauth, quota exhaustion, expired exports and storage exhaustion are visible distinct states, not rapid retry loops. One blocked account does not prevent work for another.
5. **Existing parts remain protected.** Resume through the current ledger; do not remint a usable cached URL. Never overwrite an archive part known to be different merely because its filename matches. Reconcile archive evidence without treating size equality as integrity proof.
6. **Real storage budget.** Stage at most the configured bounded work and reserve room for the next part and remote-upload cache. Stop before filling the shared encrypted volume. Cleanup of staging requires successful integrity and durable remote confirmation.
7. **Completion means durable archival.** No false COMPLETE verdict while a FUSE write is only in a local upload cache. Unsupported/unavailable remote confirmation must be visible and fail closed when durable mode is requested.
8. **Authentication is a boundary.** Expose actionable needs-reauth status and resume after the owner satisfies Google. Do not automatically retry rejected passwords or change two-factor authentication.
9. **Supervision and observability.** A service survives restart and temporary browser/storage unavailability. Sanitized status includes last discovery, per-export outcome, retry time and blockers. Notifications are optional; failures to send them do not lose job state. No public unauthenticated management endpoint is introduced.
10. **Offline executable evidence.** Tests simulate pending-to-ready discovery, two accounts, restart/dedup, lock contention, blocked-account fairness, retry backoff, incomplete work, empty pages and token redaction. Transfer integration covers interrupted streams, validator mismatch, corrupt archives and durable-upload failure. Real-server import/service/readiness checks are separate from real Google export end-to-end evidence.

## Ownership and integration

The commander freezes exact module APIs after the audit. Watcher/discovery/CLI, transfer/orchestration, and remote persistence are separate file-ownership lanes. Only the commander integrates wiki/git/deployment. Existing browser, real ledger and archives are read-only until the explicit deployment phase.

## Evidence requirement

Document exact commands, commit IDs, actual loaded code and measured service state. New exports are pending during development; do not report their full downloads as verified until the bytes have actually arrived and passed the integrity/durability gates.
