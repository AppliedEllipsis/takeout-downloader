# Mount-boundary documentation update

## Scope and premise

Exact checkout: `C:/Users/User/_projects/takeout-downloader-reliability`.
Documentation only; no D access, code/test/config modification, test execution,
Git command, Docker/server/auth/credential/data action, commit or deployment.
Read the mount repair's disposition/limitations and frozen-gate findings, both
actual Compose files and `tools/container_watch.py` (the supervisor; there is no
`autopilot/supervisor.py`). Existing documentation was read before editing.

Only assigned `.wiki/README.md`, `architecture.md`, `automation.md`,
`credentials.md`, `infrastructure.md`, `reliability-audit.md`,
`journal-2026-10.md`, append-only `decisions.md`, and
`docs/v3/03-OPERATIONS.md`, `04-FULL-PULL.md`, `05-WATCH-INTERFACE.md` were updated,
plus this report. October journal and decisions were appended; historical reports,
ADRs and dated infrastructure inventory were preserved. Current unconditional
“Not mounted”/browser-bind-only claims were corrected to required exclusion.

## Reconciled contract

- All three future archive binds are literal `/opt/archives:/opt/archives`, not
  broad `/opt` or inherited STORAGE_ROOT overrides; webGUI manager root is fixed
  `/opt/archives`; CLI roots `/opt/archives:/downloads`.
- Every effective mount must exclude
  `/opt/local_cache_crypt/takeout-autopilot-secrets` BEFORE provisioning secrets.
  Root modes do not make an exposed source host-only. Other effective mounts and
  configurable read-only VFS host cache sources still require review.
- Host inspect admission precedes exec, with 10-second deadline, 1 MiB stdout cap,
  suppressed stderr and bounded owned-client cleanup. Lexical/resolved ancestor,
  exact and descendant overlap refuses for bind/volume sources, including
  read-only/symlink cases; malformed/missing metadata and failures fail closed.
- Admission is point-in-time, NOT prevention of later unsafe administrator
  container/mount/symlink reconfiguration. Pre-provision exclusion is independent.
- Legacy outputDir outside new roots needs an explicit reviewed safe bind outside
  all secret ancestors, plus output-root review where applicable, never broad /opt.
- Future configuration only: no existing containers recreated and no secret file
  created. No live-layout certification or recertification of CORE21495.

Frozen-gate README/automation wiki SHA drift is reconciled as commander-documented
banner edits only, with no mismatch across its 173 source/config/test files.
Historical HOLD/report hashes remain intact. This is not a waiver of the genuine
containment bug, a rebaseline, or blanket final approval.

## Evidence attribution and remaining gates

Measured in `mount-boundary-repair.md`, not rerun by this documentation lane:
Windows full C **1352 passed / 66 skipped**; final nonroot WSL focused
mount/guardian/discovery **251 passed, no skips**. Broad native Linux validation
remains pending/HOLD. Unrestricted collection lacks Linux pydantic_core;
dependency-scoped run had **1170 passed / 6 skipped / 2 failed**. Mint-only failure
remains reproducible; remote flood ready-marker timing passed repeat (combined
repeat **289 passed / 1 failed**). Four unchanged raw-CRLF shell failures and
DrvFS unit-mode warnings remain documented. No totals summed or predicted.
Executor/unit installation, owner unlock/sign-in and activation remain deferred.

## Local documentation validation

Edits used explicit `cd` to the C checkout and in-memory Python `-B` scripts,
writing only assigned Markdown. Initial oversized edit-command wrapper was
truncated and failed parsing (SyntaxError), before any script execution/writes;
smaller explicit commands then completed successfully. No bytecode/cache or
helper script was written. No test suite was run under the documentation-only
assignment. Relative Markdown file targets and heading anchors are checked
locally; final results are appended below.

Validation result: **PASS** — 12 assigned Markdown files, 102 relative file links and 8 heading-anchor references checked; no missing targets/anchors. External URLs were not contacted. Checks read only linked documentation/report targets; no Git or test actions.
