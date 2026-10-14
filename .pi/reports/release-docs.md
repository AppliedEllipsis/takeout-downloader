# Documentation-only release reconciliation

**Actor:** takeout-actor; **date:** 2026-10-13.
**Canonical root:** `C:/Users/User/_projects/takeout-downloader-reliability`.
Read `.pi/workflows/finish-release.md` before edits. Original D source was not
edited; disk-full migration and byte manifests are documented separately.

## Scope

Edited only assigned documentation:

- `.wiki/README.md`, `architecture.md`, `credentials.md`, `infrastructure.md`,
  `reliability-audit.md`;
- new `.wiki/automation.md`, `.wiki/journal-2026-10.md`;
- appended ADR to `.wiki/decisions.md` (earlier text preserved);
- `docs/v3/README.md`, `02-RUN-INTERFACE.md`, `03-OPERATIONS.md`,
  `04-FULL-PULL.md`, `05-WATCH-INTERFACE.md`;
- this requested report.

No code, test, service or config edits. No commit, push, deployment, delegation,
global configuration, server/browser command, password attempt, real Google
mint/download, rclone unlock/canary or worker activation. Other lanes' modified
wiki/code/tests/deploy files were read where relevant and left untouched.

## Documentation changes

- Reconciled browser mint + cookie/UA HTTP transfer hybrid; removed stale claims
  that browser-native downloads replace cookie replay for byte transfer.
- Documented current run remote flags, explicit cached-only behavior, mint-only
  exit 1, production budgets and ledger-only report/size-hint limits.
- Distinguished full ZIP member CRC/TAR/gzip validation, loose exact-size checks,
  computed SHA256 versus trusted equality, backend exact path/size/SHA/time receipt
  and stable-receipt reuse versus a fresh audit. No FUSE-only durability claim.
- Recorded host root systemd bridge/non-root 20001:20001 container worker, foreground
  exec, existing manager venv, encrypted PRIVATE mapping including bin/config/tmp,
  private permissions, env-name-only forwarding, heartbeat/EOF/scoped subreaper
  cleanup, disabled unit and mandatory config-only unlock/alias preflight. This
  preflight is not backend network access or a canary.
- Separated actual dated report evidence from FUTURE commands/steps. Preserved
  September inventory under an explicit historical-only boundary and earlier
  ADR/journal history. Runtime rapt/authuser keys are navigation-only, never
  identity proof/config/state; persisted export references are canonical.
- C source migration/D unchanged and already-pushed docs-only `1f7f9af` documented.
  Migrated broad 971 pass/6 skip/2 fixture failures and owner-reported repaired
  focused 38 pass are not a final gate. New core-snapshot report is cited as
  **974 passed, 6 skipped** for that immutable snapshot only; final later merged
  actual-checkout gate remains unknown here. No totals predicted or summed.

## Exact executed local verification

All commands below ran after `cd /c/Users/User/_projects/takeout-downloader-reliability`.
`PYTHONDONTWRITEBYTECODE=1` plus `-B` avoided source-tree bytecode; pytest cache
provider was disabled. No live flag was enabled.

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m autopilot run --help
PYTHONDONTWRITEBYTECODE=1 python -B -m autopilot watch --help
PYTHONDONTWRITEBYTECODE=1 python -B tools/container_watch.py --help
```

Each printed the current documented options and exited **0**. Supervisor help
only: neither host bridge nor inside worker was launched.

Initial focused batch:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider tests/v3/test_cli.py tests/v3/test_cli_remote_args.py tests/v3/test_exit_codes.py tests/v3/test_discovery.py tests/v3/test_container_watch.py
```

**17 failed, 68 passed, 11 skipped in 0.40s; exit 1.** Existing `test_cli.py` lines
23–25 prepend the original D-worktree path to `sys.path`, causing new discovery
tests to load the old D `safe_url`/`discover_pages` implementation (traceback
confirmed D path and missing `runtime` argument). This is a test-import
contamination result, **not a claim that current C discovery lacks the API**.
No out-of-scope test/code fix was made. Bytecode/cache writing stayed disabled.
Read-only follow-up confirmed ordinary C `autopilot.discovery.__file__` and
`safe_url(..., runtime=False, verified_user=False)` outside that test collection.

Clean-process focused batch excluding the contaminating historical test:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider tests/v3/test_cli_remote_args.py tests/v3/test_exit_codes.py tests/v3/test_discovery.py tests/v3/test_container_watch.py tests/v3/test_watch.py
```

**96 passed, 11 skipped in 0.60s; exit 0.** Skips are Linux-only supervisor
process/guardian/preflight tests on Windows. This passes CLI/runtime-query/watch
mechanisms in scope, not a full suite or live POSIX containment/backend proof.

Earlier attempted bulk docs write via a long bash/Python heredoc was truncated
by the command transport and exited 1 with an unterminated string SyntaxError;
compilation failed before any write. Retried as separate file writes successfully.
A read-only `ls tools/container_watch.py` initially exited 2 before the concurrent
execution lane produced the file; the later file/unit/example were reread and
used to reconcile exact UID, config-only preflight, private tmp and ExecStart.

## Additional documentation checks

Scoped whitespace command:

```bash
git diff --check -- .wiki/README.md .wiki/architecture.md .wiki/credentials.md .wiki/infrastructure.md .wiki/reliability-audit.md .wiki/automation.md .wiki/journal-2026-10.md .wiki/decisions.md docs/v3/README.md docs/v3/02-RUN-INTERFACE.md docs/v3/03-OPERATIONS.md docs/v3/04-FULL-PULL.md docs/v3/05-WATCH-INTERFACE.md .pi/reports/release-docs.md
```

**Exit 0, no output.** New/untracked docs were additionally checked in Python.
The first docs-validator attempt incorrectly applied new-content whitespace rules
to all historical ADR text and found pre-existing trailing whitespace. History was
not rewritten: the final validator checks only the appended ADR suffix for whitespace
and asserts the complete earlier text is an identical decoded HEAD prefix.

Exact successful validator command:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B - <<'PY'
from pathlib import Path
import json, re, subprocess
from autopilot.__main__ import build_parser
from autopilot.watch import WatchConfig
paths = ['.wiki/README.md','.wiki/architecture.md','.wiki/credentials.md','.wiki/infrastructure.md','.wiki/reliability-audit.md','.wiki/automation.md','.wiki/journal-2026-10.md','.wiki/decisions.md','docs/v3/README.md','docs/v3/02-RUN-INTERFACE.md','docs/v3/03-OPERATIONS.md','docs/v3/04-FULL-PULL.md','docs/v3/05-WATCH-INTERFACE.md','.pi/reports/release-docs.md']
base=subprocess.check_output(['git','show','HEAD:.wiki/decisions.md']).decode('utf-8')
assert Path('.wiki/decisions.md').read_text(encoding='utf-8').startswith(base), 'ADR history changed'
links=0
for name in paths:
 p=Path(name); s=p.read_text(encoding='utf-8')
 assert sum(line.startswith('```') for line in s.splitlines()) % 2 == 0, name+' unclosed fence'
 changed=s[len(base):] if name=='.wiki/decisions.md' else s
 assert not any(line.rstrip()!=line for line in changed.splitlines()), name+' trailing whitespace'
 for target in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)',s):
  if '://' in target or target.startswith('#'):
   continue
  assert (p.parent / target.split('#',1)[0]).resolve().exists(), (name,target)
  links+=1
run=build_parser()._subparsers._group_actions[0].choices['run']
flags={o for action in run._actions for o in action.option_strings if o.startswith('--') and o!='--help'}
interface=Path('docs/v3/02-RUN-INTERFACE.md').read_text(encoding='utf-8')
assert all(flag in interface for flag in flags), sorted(flag for flag in flags if flag not in interface)
example=json.loads(re.search(r'```json\n(.*?)\n```',Path('docs/v3/05-WATCH-INTERFACE.md').read_text(encoding='utf-8'),re.S).group(1))
actual=json.loads(Path('deploy/watch.example.json').read_text(encoding='utf-8'))
assert set(example)==set(actual)==set(WatchConfig.__dataclass_fields__), 'watch fields'
for field in set(example)-{'accounts'}:
 assert example[field]==actual[field], field
assert set(example['accounts'][0])=={'name','manage_url','user_id'}
print('PASS',len(paths),'docs;',links,'local links;',len(flags),'run flags; watch schema/example; ADR prefix unchanged')
PY
```

**Exit 0:** `PASS 14 docs; 66 local links; 19 run flags; watch schema/example; ADR prefix unchanged`.
This checks local-link targets, balanced fences, new-content trailing whitespace,
all current run flags, watcher JSON field/value consistency with the candidate
example, and append-only ADR history. It does not validate a live config or resolve
placeholder identities. No live state is inferred.

After the final full-pull edit, replayed the exact validator stored above with:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B - <<'PY'
from pathlib import Path
text=Path('.pi/reports/release-docs.md').read_text(encoding='utf-8')
marker="PYTHONDONTWRITEBYTECODE=1 python -B - <<'PY'\n"
script=text.split(marker,1)[1].split('\nPY\n```',1)[0]
exec(compile(script, '.pi/reports/release-docs.md:validator', 'exec'))
PY
```

**PASS: 14 docs; 67 local links; 19 run flags; watch schema/example; ADR prefix
unchanged.** The extra local link is the full-pull supervisor authority reference.
Scoped `git diff --check` again produced no output. A subsequent optional `ls`
for an execution-lane report glob returned 2 because no such report existed yet;
that listing is not a test or release gate. No final merged-checkout result was
invented to fill the gap.

## Remaining owner/integration gates

- Current actual-checkout final suite, supervisor Linux stop/preflight evidence,
  secret review, approved immutable release and commander integration/deployment
  report. Core snapshot success does not certify later runtime additions.
- Existing historical CLI test's hardcoded D import path can contaminate selected
  collections; integration owner should isolate/fix it in its own scope.
- Owner-authorized unlock/sign-in/readiness and separate launch permission remain
  deferred. Unit must remain disabled/inactive; pending/ready export counts unknown;
  no live backend canary or new export completeness claim.
