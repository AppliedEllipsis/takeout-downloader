"""Direct backend archival. Confirmation requires a forced backend byte read.

No FUSE/cache durability inference is made. A named rclone remote must already
be configured; inline backend options and credential-bearing URLs are forbidden.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RemoteConfig:
    destination: str
    binary: str = "rclone"
    config_file: Optional[str] = None
    timeout: float = 21600


@dataclass
class RemoteResult:
    action: str
    confirmed: bool = False
    remote_path: str = ""
    size: int = 0
    sha256: Optional[str] = None
    detail: str = ""


def _safe_filename(name):
    if (not isinstance(name, str) or not name or name in (".", "..")
            or name.endswith((".", " ")) or name.startswith("-")
            or any(c in name for c in '/\\:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError("unsafe filename")
    if name.split('.')[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("unsafe filename")
    return name


def _path(cfg, filename, expected_size, digest=None):
    _safe_filename(filename)
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
        raise ValueError("expected size must be a nonnegative integer")
    if not isinstance(cfg.destination, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[^:@?=#%\\\x00-\x1f\x7f]*", cfg.destination):
        raise ValueError("unsafe remote destination; use a configured named remote")
    root = cfg.destination.split(":", 1)[1]
    for part in root.strip('/').split('/'):
        if part:
            _safe_filename(part)
    if not math.isfinite(cfg.timeout) or cfg.timeout <= 0:
        raise ValueError("invalid remote timeout")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
        raise ValueError("invalid expected SHA256")
    return cfg.destination.rstrip('/') + ('/' if root else '') + filename


_LIMIT = 65536


class _WindowsJob:
    """A private kill-on-close job, assigned BEFORE the child can spawn."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                        ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                        ('active_limit', w.DWORD), ('affinity', c.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in
                        ('reads', 'writes', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t),
                        ('peak_job', c.c_size_t)]
        self.c = c
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError('cannot create owned process job')
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.close()
            raise OSError('cannot configure owned process job')

    def assign_and_resume(self, p):
        from ctypes import wintypes as w
        if not self.kernel.AssignProcessToJobObject(self.handle, int(p._handle)):
            raise OSError('cannot assign owned process job')
        # Popen closes the primary thread handle; resume through the owned
        # process handle after assignment, avoiding a descendant-spawn race.
        resume = self.c.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = [w.HANDLE]
        resume.restype = self.c.c_long
        if resume(int(p._handle)) != 0:
            raise OSError('cannot resume owned process')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _kill_owned(p, job):
    if job is not None:
        job.close()  # all descendants, even if the original process exited
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # private session created by Popen
        except ProcessLookupError:
            pass
    if p.poll() is None:
        p.kill()
    p.wait()


def _command(cfg, args):
    """Bound pipes and time; reclaim the OWNED group/job on every exit path."""
    argv = [cfg.binary, *args, '--ask-password=false', '--retries', '1', '--low-level-retries', '1']
    if cfg.config_file is not None:
        argv += ['--config', os.fspath(cfg.config_file)]
    job, p = None, None
    try:
        kwargs = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | 0x4}
                  if os.name == 'nt' else {'start_new_session': True})
        if os.name == 'nt':
            job = _WindowsJob()
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, shell=False, **kwargs)
        if job is not None:
            job.assign_and_resume(p)
    except OSError:
        if p is not None:
            _kill_owned(p, job)
        elif job is not None:
            job.close()
        raise ValueError("rclone unavailable or could not start owned process") from None
    buffers = [bytearray(), bytearray()]
    overflow, read_error = threading.Event(), threading.Event()
    def drain(pipe, buf):
        try:
            while True:
                block = pipe.read(4096)
                if not block:
                    break
                if len(buf) + len(block) > _LIMIT:
                    overflow.set()
                buf.extend(block[:max(0, _LIMIT - len(buf))])
        except (OSError, ValueError):
            read_error.set()
        finally:
            pipe.close()
    threads = [threading.Thread(target=drain, args=(pipe, buf), daemon=True)
               for pipe, buf in zip((p.stdout, p.stderr), buffers)]
    error = None
    try:
        for t in threads:
            t.start()
        deadline = time.monotonic() + cfg.timeout
        while True:
            if overflow.is_set():
                error = "rclone output limit exceeded"
                break
            if read_error.is_set():
                error = "rclone output read failed"
                break
            if p.poll() is not None:
                break
            if time.monotonic() >= deadline:
                error = "rclone command timed out"
                break
            time.sleep(min(0.01, cfg.timeout))
    finally:
        # Cleanup also covers nonzero exits and descendants holding pipes open.
        _kill_owned(p, job)
        for t in threads:
            if t.ident is not None:
                t.join(timeout=1)
    if error or overflow.is_set() or read_error.is_set() or any(t.is_alive() for t in threads):
        raise ValueError(error or "rclone output limit exceeded or read failed")
    return p.returncode, buffers[0].decode('utf-8', errors='replace')


def _stat(cfg, path):
    code, text = _command(cfg, ['lsjson', path, '--stat', '--no-modtime', '--no-mimetype'])
    if code in (3, 4):  # rclone directory/object-not-found exit codes only
        return None
    if code:
        raise ValueError(f"rclone stat failed (exit {code})")
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict) or obj.get('IsDir') is not False or type(obj.get('Size')) is not int or obj['Size'] < 0:
            raise ValueError()
        return obj['Size']
    except (ValueError, TypeError):
        raise ValueError("rclone stat did not identify a regular object") from None


def _verify(cfg, path, size, digest):
    if _stat(cfg, path) != size:
        raise ValueError("remote size mismatch or missing object")
    # rclone v1.68.2 help confirms SHA256 and --download: download bytes even
    # when the backend advertises a native checksum. Never use native hash alone.
    code, text = _command(cfg, ['hashsum', 'SHA256', path, '--download'])
    if code:
        raise ValueError(f"rclone backend read failed (exit {code})")
    lines = text.strip().splitlines()
    if len(lines) != 1 or not re.fullmatch(r"[a-fA-F0-9]{64}\s+.+", lines[0]):
        raise ValueError("invalid backend SHA256 output")
    actual = lines[0][:64].lower()
    if actual != digest.lower():
        raise ValueError("remote SHA256 mismatch")
    if _stat(cfg, path) != size:
        raise ValueError("remote size changed during verification")
    return actual


def verify_remote_part(cfg: RemoteConfig, *, filename, expected_size, sha256) -> RemoteResult:
    path = ''
    try:
        path = _path(cfg, filename, expected_size, sha256)
        if sha256 is None:
            raise ValueError("expected SHA256 required")
        digest = _verify(cfg, path, expected_size, sha256)
        return RemoteResult('already-present', True, path, expected_size, digest)
    except (ValueError, OSError, TypeError) as exc:
        return RemoteResult('failed', remote_path=path, detail=str(exc)[:256])


def archive_remote_part(source, cfg: RemoteConfig, *, filename, expected_size, sha256=None) -> RemoteResult:
    path = ''
    try:
        path = _path(cfg, filename, expected_size, sha256)
        if not os.path.isfile(source) or os.path.getsize(source) != expected_size:
            raise ValueError("local source size mismatch or not a file")
        h = hashlib.sha256()
        with open(source, 'rb') as f:
            for block in iter(lambda: f.read(4 << 20), b''):
                h.update(block)
        digest = h.hexdigest()
        if sha256 is not None and digest != sha256.lower():
            raise ValueError("local SHA256 mismatch")
        existing = _stat(cfg, path)
        if existing is not None:
            actual = _verify(cfg, path, expected_size, digest)
            return RemoteResult('already-present', True, path, expected_size, actual)
        # --ignore-existing protects an object appearing after our preflight.
        # Such a race is still verified below and never size-only adopted.
        code, _ = _command(cfg, ['copyto', os.path.abspath(source), path, '--ignore-existing'])
        if code:
            raise ValueError(f"rclone upload failed (exit {code})")
        actual = _verify(cfg, path, expected_size, digest)
        return RemoteResult('uploaded', True, path, expected_size, actual)
    except OSError:
        return RemoteResult('failed', remote_path=path, detail='local file or subprocess I/O failed')
    except (ValueError, TypeError) as exc:
        return RemoteResult('failed', remote_path=path, detail=str(exc)[:256])
