# Final Linux execution verifier — HOLD: alias preflight is not fail-closed

Canonical checkout: `C:/Users/User/_projects/takeout-downloader-reliability`.
Scope: actual `tools/container_watch.py`, `tests/v3/test_container_watch.py`, their execution report and relevant watcher/unit configuration. Findings only; no source/test edits. No changes made to original D checkout. Only this report was explicitly written; offline tests used temporary synthetic fixtures. No production rclone, password/unlock attempt, Google/browser action, real download/mint, watcher activation, Docker invocation, SSH, commit/push/deployment or service operation occurred.

## Release finding

**HIGH — alias/backend validation can approve unresolved, cyclic or local wrapper destinations before launching the worker.** At `tools/container_watch.py:259–267`, only `alias`, `crypt`, and `chunker` are traversed. Every other syntactically lower-case backend name except `local`, `union`, and `combine` is treated as a terminal backend. Consequently:

- `archives -> wrapped`, with `wrapped.type=cache, remote=/tmp/local`, passes.
- `archives -> wrapped`, with `wrapped.type=hasher, remote=missing:path`, passes.
- `archives -> wrapped`, with `wrapped.type=compress, remote=archives:path`, passes (unexamined cycle).
- `type=memory` and even `type=madeupbackend` pass.

This falsifies the execution report's general claim that unresolved/local configurations fail closed. A completely synthetic config-dump executable demonstrated the **actual `preflight` → `supervise` path**, returning 0 and creating a synthetic worker marker for the missing hasher target. No real watcher was launched. Genuine config dump success is not evidence that an unexamined wrapper's target resolves to an approved nonlocal backend. These configurations may fail later, but startup has already passed the claimed mandatory pre-watcher gate.

Recommended owner repair (not performed): explicitly handle or reject every supported wrapper, reject memory/local terminals and unknown backend types, and add missing/cyclic/local wrapper regression cases plus a preflight-before-launch assertion. Conservative rejection of unsupported wrappers is sufficient. Existing tests cover only the three traversed wrappers; none challenge these accepted terminal names.

**Gate disposition: HOLD for execution/preflight claim; do not activate.** The already-pushed immutable core gate does not cover this later supervisor finding.

## Confirmed offline Linux behavior

WSL Ubuntu-24.04, Python 3.12.3, nonroot UID/GID 1000. Existing scoped pure-Python pytest was used; no installs/global configuration changes. WSL user's supplementary Docker membership is not evidence about production UID 20001, and no Docker command was run.

- Producer-focused suite: **79 passed, no skips**. This includes real subprocess EOF, stop, lease, host-writer SIGKILL, failed worker, detached TERM-ignoring descendant sessions, synthetic locked/hanging/flooding preflight cleanup, suppressed synthetic output, and absence of the synthetic worker-start marker on tested refusal paths.
- Additional real pidfd challenge: mismatched starttime did not kill a live same-UID Python process; matching starttime delivered TERM; vanished PID was a no-op. This checks the guard and real pidfd delivery, not an actual kernel PID-reuse event.
- Independent cleanup challenge: double-forked orphan/new session **and a new session forked from the worker's TERM handler during cleanup** were reclaimed for EOF, lease expiry and abrupt host writer SIGKILL. Unrelated same-UID/same-executable Python survived each challenge. Three owned recorded PIDs per scenario plus the guardian were no longer running.
- Inspected Docker argv is foreground/non-TTY/non-detached and forwards only allowlisted environment names, not values. Worker environment drops unrelated RCLONE overrides and PYTHONPATH. Passing existing tests substantiate these properties.

These prove Linux mechanisms with synthetic processes, **not real Docker shim/stream propagation, production interpreter/imports, actual mount permissions, UID/GID 20001 reservation/collision, installed unit state, current download inventory, or live credentials/backend behavior**. Those remain unverified; owner-deferred unlock/sign-in remains deferred.

## Exact baseline command and output

Executed from Git Bash:

```bash
cd 'C:/Users/User/_projects/takeout-downloader-reliability' && sha256sum tools/container_watch.py tests/v3/test_container_watch.py && wsl -d Ubuntu-24.04 -- bash -lc 'id; python3 --version; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -p no:cacheprovider tests/v3/test_container_watch.py tests/v3/test_discovery.py'
```

Output (supplementary groups omitted; no credentials/account data):

```text
cd90fd6069da8aca879b46147adfc34561cd2c7cbfd37e965281fe94fdd49d21 *tools/container_watch.py
5a042b15a69f05f67d63b18b047e7fb813419006ecb769324421ac28b55dd1b2 *tests/v3/test_container_watch.py
uid=1000(user) gid=1000(user)
Python 3.12.3
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed in 14.30s
```

Exit 0. Hashes match execution-build.md and were identical after adversarial verification.

## Exact independent pidfd / alias / preflight reproduction

```bash
wsl -d Ubuntu-24.04 -- python3 -B - <<'PY'
import os, sys, subprocess, signal, time, tempfile, json
from pathlib import Path
sys.path.insert(0, '/mnt/c/Users/User/_projects/takeout-downloader-reliability')
from tools import container_watch as c
assert os.getuid()!=0
print('UID',os.getuid(),'pidfd available',hasattr(os,'pidfd_open'))
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
try:
    start=c._proc(p.pid)[1]
    c._signal_owned(p.pid,start+1,signal.SIGKILL)
    time.sleep(.05)
    assert p.poll() is None
    c._signal_owned(p.pid,start,signal.SIGTERM)
    assert p.wait(timeout=3)==-signal.SIGTERM
    c._signal_owned(p.pid,start,signal.SIGKILL)
    print('PASS real pidfd stale-starttime no-op; matching-starttime TERM; vanished-PID no-op')
finally:
    if p.poll() is None: p.kill(); p.wait()
for kind, remote in [('cache','/tmp/local'),('hasher','missing:path'),('compress','archives:path')]:
    data={'archives': {'type':'alias','remote':'wrapped:root'}, 'wrapped': {'type':kind,'remote':remote}}
    try:
        c.validate_aliases(data,'archives:google-takeout')
        print('FAIL alias gate ACCEPTED',kind,'target',remote)
    except c.Refused: print('PASS alias gate refused',kind)
for kind in ['memory','madeupbackend']:
    try:
        c.validate_aliases({'archives':{'type':kind}},'archives:google-takeout')
        print('FAIL backend gate ACCEPTED',kind)
    except c.Refused: print('PASS backend gate refused',kind)
with tempfile.TemporaryDirectory(prefix='final-exec-verifier-') as td:
    root=Path(td); (root/'bin').mkdir(); root.chmod(0o700)
    binary=root/'bin/rclone'; conf=root/'rclone.conf'; marker=root/'synthetic-worker-marker'
    binary.write_text('#!'+sys.executable+'\nprint(\'{"archives":{"type":"alias","remote":"wrapped:root"},"wrapped":{"type":"hasher","remote":"missing:path"}}\')\n')
    binary.chmod(0o700); conf.write_bytes(b'RCLONE_ENCRYPT_V0:\nSYNTHETIC'); conf.chmod(0o600)
    code="import sys; sys.path.insert(0,'/mnt/c/Users/User/_projects/takeout-downloader-reliability'); from tools import container_watch as c; from types import SimpleNamespace; "
    code+=f"c.PRIVATE={td!r}; cfg=SimpleNamespace(rclone_binary={str(binary)!r},rclone_config={str(conf)!r},archive_remote='archives:google-takeout'); "
    code+="hb=c.Heartbeat(timeout=3); "
    worker=f"open({str(marker)!r},'w').write('offline')"
    code+=f"sys.exit(c.supervise([sys.executable,'-c',{worker!r}],hb,check=lambda:c.preflight(cfg,hb),cleanup_grace=.1))"
    g=subprocess.Popen([sys.executable,'-B','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    g.stdin.write(b'.\n'); g.stdin.flush(); rc=g.wait(timeout=5)
    print('FAIL synthetic unresolved hasher preflight: rc',rc,'synthetic-marker-created',marker.exists())
    assert rc==0 and marker.exists()
print('No production process/config/network accessed')
PY
```

Output:

```text
UID 1000 pidfd available True
PASS real pidfd stale-starttime no-op; matching-starttime TERM; vanished-PID no-op
FAIL alias gate ACCEPTED cache target /tmp/local
FAIL alias gate ACCEPTED hasher target missing:path
FAIL alias gate ACCEPTED compress target archives:path
FAIL backend gate ACCEPTED memory
FAIL backend gate ACCEPTED madeupbackend
FAIL synthetic unresolved hasher preflight: rc 0 synthetic-marker-created True
No production process/config/network accessed
```

Exit 0 means successful reproduction of the negative finding, **not gate success**.

## Exact additional subtree cleanup challenge

```bash
wsl -d Ubuntu-24.04 -- python3 -B - <<'PY'
import os, sys, signal, subprocess, tempfile, time
from pathlib import Path
ROOT='/mnt/c/Users/User/_projects/takeout-downloader-reliability'
assert os.getuid()!=0

def alive(pid):
    try:
        s=Path(f'/proc/{pid}/stat').read_text(); return s[s.rindex(')')+2:].split()[0]!='Z'
    except FileNotFoundError: return False

def wait(pred, timeout=5):
    end=time.monotonic()+timeout
    while not pred():
        assert time.monotonic()<end,'deadline exceeded'
        time.sleep(.01)

for mode in ['eof','lease','host-sigkill']:
    with tempfile.TemporaryDirectory(prefix='final-cleanup-verifier-') as td:
        root=Path(td); records=root/'pids'; ready=root/'ready'; late=root/'late'; gid=root/'guardian'
        worker=f'''import os, signal, time
from pathlib import Path
records=Path({str(records)!r})
def note(role):
    with records.open('a') as f: f.write(role+' '+str(os.getpid())+chr(10))
def terminate(*args):
    p=os.fork()
    if p==0:
        os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN)
        note('late'); Path({str(late)!r}).write_text('ready'); time.sleep(30); os._exit(0)
    os._exit(0)
signal.signal(signal.SIGTERM,terminate)
note('worker')
p=os.fork()
if p==0:
    os.setsid()
    q=os.fork()
    if q!=0: os._exit(0)
    os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN)
    note('double-orphan'); Path({str(ready)!r}).write_text('ready'); time.sleep(30); os._exit(0)
os.waitpid(p,0)
time.sleep(30)
'''
        guardian=f"import sys,os; sys.path.insert(0,{ROOT!r}); from tools import container_watch as c; open({str(gid)!r},'w').write(str(os.getpid())); sys.exit(c.supervise([sys.executable,'-B','-c',{worker!r}],c.Heartbeat(timeout=1),cleanup_grace=.3))"
        unrelated=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)'],start_new_session=True)
        p=None
        try:
            if mode=='host-sigkill':
                host=f"import sys; sys.path.insert(0,{ROOT!r}); from tools import container_watch as c; c.host_bridge({[sys.executable,'-B','-c',guardian]!r},interval=.05)"
                p=subprocess.Popen([sys.executable,'-B','-c',host],stdin=subprocess.DEVNULL)
            else:
                p=subprocess.Popen([sys.executable,'-B','-c',guardian],stdin=subprocess.PIPE)
                p.stdin.write(b'.\n'); p.stdin.flush()
            wait(lambda: ready.exists())
            if mode=='eof': p.stdin.close()
            if mode=='host-sigkill': p.kill()
            expected=-signal.SIGKILL if mode=='host-sigkill' else 0
            assert p.wait(timeout=6)==expected
            wait(lambda: late.exists())
            owned=[int(row.split()[1]) for row in records.read_text().splitlines()]
            wait(lambda: all(not alive(pid) for pid in owned+[int(gid.read_text())]))
            assert unrelated.poll() is None
            print('PASS',mode,'double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive;',len(owned),'owned IDs')
        finally:
            if p and p.stdin and not p.stdin.closed: p.stdin.close()
            if p and p.poll() is None: p.kill(); p.wait()
            if records.exists():
                for row in records.read_text().splitlines():
                    pid=int(row.split()[1])
                    if alive(pid): os.kill(pid,signal.SIGKILL)
            unrelated.terminate(); unrelated.wait()
print('Offline process cleanup challenge complete')
PY
```

Output, exit 0:

```text
PASS eof double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive; 3 owned IDs
PASS lease double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive; 3 owned IDs
PASS host-sigkill double-fork orphan + TERM-time fork/new session reaped; unrelated same-UID Python alive; 3 owned IDs
Offline process cleanup challenge complete
```

An earlier attempt of this challenge used an escaped newline instead of `chr(10)` in the generated worker's `f.write` expression. It exited 1 with `SyntaxError: unterminated string literal` and then `AssertionError: deadline exceeded`; it is **not cleanup evidence**. Its finally block terminated the unrelated fixture and any surviving recorded owned processes. The complete successful command above avoids the nested command-string escaping problem.

## Read-only final fingerprint/status check

```bash
cd 'C:/Users/User/_projects/takeout-downloader-reliability' && sha256sum tools/container_watch.py tests/v3/test_container_watch.py && nl -ba tools/container_watch.py | sed -n '165,218p;250,350p'; git status --short -- tools/container_watch.py tests/v3/test_container_watch.py
```

Exit 0. Both SHA256 values remained exactly those listed above. Numbered output confirmed the cited ancestry, starttime/pidfd, cleanup, alias and preflight-before-launch lines. Scoped status:

```text
?? tests/v3/test_container_watch.py
?? tools/container_watch.py
```

No files were staged or committed. Production provisioning/activation is neither performed nor certified by this report.
