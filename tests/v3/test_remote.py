"""Offline subprocess backend: no remote, browser, or live archive access."""
import hashlib
import json
import subprocess
import sys

import pytest

from autopilot.remote import RemoteConfig, archive_remote_part, verify_remote_part

PAYLOAD = b'archival payload'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def backend(tmp_path, monkeypatch):
    script = tmp_path / 'rclone_stub.py'
    script.write_text('''
import sys, os, json, hashlib, time
from pathlib import Path
root = Path(__file__).parent
mode = (root / 'mode').read_text()
a = sys.argv[1:]
with (root / 'calls').open('a') as f: f.write(json.dumps(a)+'\\n')
if mode == 'timeout': time.sleep(10)
if mode == 'flood': print('x'*1000000); sys.exit(1)
if mode == 'error': print('secret-token=do-not-leak', file=sys.stderr); sys.exit(9)
p = root / 'object'
if a[0] == 'lsjson':
    if not p.exists(): sys.exit(4)
    if mode == 'malformed': print('{}'); sys.exit(0)
    print(json.dumps({'IsDir': False, 'Size': p.stat().st_size}))
elif a[0] == 'copyto':
    assert '--ignore-existing' in a
    if mode == 'copy-error': sys.exit(7)
    if not p.exists(): p.write_bytes(Path(a[1]).read_bytes())
elif a[0] == 'hashsum':
    assert a[1] == 'SHA256' and '--download' in a
    if mode == 'hash-error': sys.exit(8)
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    if mode == 'wrong-hash': h = '0'*64
    print(h + '  part.zip')
''', encoding='utf-8')
    (tmp_path / 'mode').write_text('normal')
    real_popen = subprocess.Popen
    def popen(argv, **kwargs):
        return real_popen([sys.executable, str(script), *argv[1:]], **kwargs)
    monkeypatch.setattr('autopilot.remote.subprocess.Popen', popen)
    src = tmp_path / 'source'
    src.write_bytes(PAYLOAD)
    return tmp_path, src, RemoteConfig('archive:export', binary=sys.executable, timeout=2)


def upload(backend, **kwargs):
    root, src, cfg = backend
    return archive_remote_part(src, cfg, filename='part.zip', expected_size=len(PAYLOAD), **kwargs)


def test_upload_force_read_and_verify(backend):
    root, _, cfg = backend
    result = upload(backend, sha256=DIGEST)
    assert result.action == 'uploaded' and result.confirmed
    assert result.remote_path == 'archive:export/part.zip'
    assert result.sha256 == DIGEST and result.size == len(PAYLOAD)
    calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
    assert [c[0] for c in calls] == ['lsjson', 'copyto', 'lsjson', 'hashsum', 'lsjson']
    assert '--download' in calls[3]
    assert verify_remote_part(cfg, filename='part.zip', expected_size=len(PAYLOAD), sha256=DIGEST).confirmed
    assert upload(backend).action == 'already-present'


@pytest.mark.parametrize('data', [b'wrong', b'x'*len(PAYLOAD)])
def test_mismatch_preserved(backend, data):
    root, _, _ = backend
    (root / 'object').write_bytes(data)
    r = upload(backend)
    assert r.action == 'failed' and not r.confirmed
    assert (root / 'object').read_bytes() == data
    assert 'copyto' not in (root / 'calls').read_text()


@pytest.mark.parametrize('mode', ['wrong-hash', 'hash-error', 'copy-error', 'error', 'malformed', 'timeout', 'flood'])
def test_failures_never_confirm(backend, mode):
    root, _, cfg = backend
    (root / 'mode').write_text(mode)
    if mode == 'malformed': (root / 'object').write_bytes(PAYLOAD)
    if mode == 'timeout': cfg.timeout = .1
    r = upload(backend)
    assert r.action == 'failed' and not r.confirmed
    assert len(r.detail) <= 256 and 'secret-token' not in r.detail


@pytest.mark.parametrize('filename', ['../escape', '/escape', 'x\\y', 'x:y', '', '..', 'NUL', 'bad\nname'])
def test_unsafe_filename_no_command(backend, filename):
    root, src, cfg = backend
    r = archive_remote_part(src, cfg, filename=filename, expected_size=len(PAYLOAD))
    assert r.action == 'failed' and not (root / 'calls').exists()


@pytest.mark.parametrize('destination', ['https://user:password@host/path', ':s3,secret=foo:bucket', 'r:../path', 'r:a/../b', 'r:path?token=secret', 'C:\\mount', 'r:a//../b'])
def test_unsafe_remote_no_command(backend, destination):
    root, _, cfg = backend
    cfg.destination = destination
    assert upload(backend).action == 'failed'
    assert not (root / 'calls').exists()


def test_wrong_local_expected_hash_no_upload(backend):
    root, _, _ = backend
    assert upload(backend, sha256='0'*64).action == 'failed'
    assert not (root / 'calls').exists()


def test_unavailable_binary(tmp_path):
    src = tmp_path / 'source'
    src.write_bytes(PAYLOAD)
    cfg = RemoteConfig('archive:export', binary=str(tmp_path / 'does-not-exist'))
    assert archive_remote_part(src, cfg, filename='p.zip', expected_size=len(PAYLOAD)).action == 'failed'
    assert not verify_remote_part(cfg, filename='p.zip', expected_size=len(PAYLOAD), sha256=DIGEST).confirmed


def test_missing_digest_and_invalid_size_fail_closed(backend):
    root, _, cfg = backend
    for size, digest in [(len(PAYLOAD), None), (None, DIGEST), (-1, DIGEST), (True, DIGEST)]:
        r = verify_remote_part(cfg, filename='part.zip', expected_size=size, sha256=digest)
        assert r.action == 'failed' and not r.confirmed
    assert not (root / 'calls').exists()


def test_every_command_disables_password_prompt(backend):
    root, _, _ = backend
    assert upload(backend).confirmed
    for line in (root / 'calls').read_text().splitlines():
        assert '--ask-password=false' in json.loads(line)


@pytest.mark.parametrize('mode', ['timeout', 'flood', 'parent-exit'])
def test_owned_spawned_descendant_terminated(tmp_path, monkeypatch, mode):
    import time
    import autopilot.remote as remote
    script = tmp_path / 'owned_tree.py'
    marker = tmp_path / 'descendant-survived'
    ready = tmp_path / 'ready'
    script.write_text('''import subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
child = "import time,os; from pathlib import Path; p=Path(%r); p.with_name('ready').write_text(str(os.getpid())); time.sleep(1); p.write_text('alive'); time.sleep(30)" % str(root / 'descendant-survived')
subprocess.Popen([sys.executable, '-c', child])
while not (root / 'ready').exists(): time.sleep(.005)
mode = sys.argv[1]
if mode == 'flood':
    while True: print('x' * 4096, flush=True)
if mode == 'parent-exit': sys.exit(9)
time.sleep(30)
''', encoding='utf-8')
    real_popen = subprocess.Popen
    def spawn(argv, **kwargs):
        return real_popen([sys.executable, str(script), mode], **kwargs)
    monkeypatch.setattr(remote.subprocess, 'Popen', spawn)
    cfg = RemoteConfig('archive:export', binary=sys.executable, timeout=.5)
    if mode == 'parent-exit':
        code, _ = remote._command(cfg, ['lsjson', 'archive:export/part.zip'])
        assert code == 9
    else:
        with pytest.raises(ValueError):
            remote._command(cfg, ['lsjson', 'archive:export/part.zip'])
    assert ready.exists(), 'regression must actually spawn and run the owned child'
    time.sleep(1.2)
    assert not marker.exists(), 'owned descendant survived command cleanup'
    pid = int(ready.read_text())
    import os
    if os.name == 'nt':
        import ctypes as c
        from ctypes import wintypes as w
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 0) == 0, 'owned child still running'
            finally:
                kernel.CloseHandle(handle)
    else:
        # A terminated orphan can briefly remain a zombie awaiting init's reap.
        from pathlib import Path
        proc_stat = Path(f'/proc/{pid}/stat')
        if proc_stat.exists():
            assert proc_stat.read_text().split(') ')[1].startswith('Z ')
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
