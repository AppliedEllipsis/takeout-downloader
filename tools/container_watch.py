"""Foreground Docker bridge and Linux-only, scoped worker guardian.

Host runs with Docker authority; only the dedicated numeric UID/GID runs inside.
No secret values are placed in exec argv. EOF or a missed stdin heartbeat closes
all descendants of the in-container subreaper, including new sessions. This is
not a container restart/stop facility and never signals unrelated processes.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import signal
import stat
import subprocess
import sys
import threading
import time

PRIVATE = "/config/autopilot"
CODE = "/work/.v3"
PYTHON = "/opt/manager-venv/bin/python"
# Docker obtains values from the host environment; argv contains NAMES only.
SECRET_ENV_NAMES = ("RCLONE_CONFIG_PASS", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ENABLED")
HEARTBEAT_TIMEOUT = 8.0
CLEANUP_SECONDS = 5.0
OUTPUT_LIMIT = 1 << 20
HOST_SECRETS = "/opt/local_cache_crypt/takeout-autopilot-secrets"
INSPECT_TIMEOUT = 10.0
# Reviewed network terminals only. New backends/wrappers require explicit review;
# config reference validation does not establish connectivity or durability.
NETWORK_BACKENDS = frozenset({
    "sftp", "s3", "b2", "azureblob", "drive", "onedrive", "webdav", "ftp", "gcs", "swift",
})


class Refused(RuntimeError):
    """Only fixed, non-sensitive diagnostics are permitted."""


def docker_argv(container, identity, config, environ=None):
    environ = os.environ if environ is None else environ
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
        raise Refused("invalid container name")
    if not re.fullmatch(r"[1-9][0-9]{0,8}:[1-9][0-9]{0,8}", identity):
        raise Refused("dedicated non-root UID:GID required")
    if config != PRIVATE + "/watch.json":
        raise Refused("private container config required")
    argv = ["/usr/bin/docker", "exec", "-i", "--user", identity, "--workdir", CODE]
    for name in SECRET_ENV_NAMES:
        if name in environ:
            argv += ["--env", name]
    argv += ["--env", "PYTHONPATH=", container, PYTHON, "-I", "-B",
             CODE + "/tools/container_watch.py", "--inside", "--config", config]
    return argv


def validate_mounts(data, *, protected=HOST_SECRETS):
    """Reject overlapping host sources, including read-only and symlink aliases.

    Non-strict resolution also protects the future secret directory before it
    exists. Compare lexical AND resolved paths: neither symlinks on the source
    nor symlinks on the protected path may disguise an exposing ancestor.
    """
    def paths(value):
        if (not isinstance(value, str) or not value.startswith("/")
                or value.startswith("//") or any(ord(c) < 32 for c in value)):
            raise Refused("container mount metadata refused")
        try:
            return (PurePosixPath(value), Path(value).resolve())
        except (OSError, ValueError, RuntimeError):
            raise Refused("container mount metadata refused") from None

    secrets = paths(protected)
    if not isinstance(data, list) or not data:
        raise Refused("container mount metadata refused")
    for mount in data:
        if (not isinstance(mount, dict) or not isinstance(mount.get("Type"), str)
                or mount["Type"] not in {"bind", "volume", "tmpfs"}
                or not isinstance(mount.get("Source"), str)
                or not isinstance(mount.get("RW"), bool)):
            raise Refused("container mount metadata refused")
        paths(mount.get("Destination"))
        if mount["Type"] == "tmpfs":
            if mount["Source"] != "":
                raise Refused("container mount metadata refused")
            continue
        sources = paths(mount["Source"])
        for source, secret in zip(sources, secrets):
            if source == secret or source in secret.parents or secret in source.parents:
                raise Refused("container host-secret mount boundary refused")


def inspect_mount_boundary(container, *, popen=None, timeout=INSPECT_TIMEOUT):
    """Bounded, suppressed host-only admission probe; never runs inside Docker.

    This point-in-time check is NOT a provisioning boundary or a defense against
    an administrator independently creating/reconfiguring an unsafe container.
    All effective mounts must exclude host secrets BEFORE secrets are provisioned.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
        raise Refused("invalid container name")
    popen = subprocess.Popen if popen is None else popen
    p = None
    try:
        p = popen(["/usr/bin/docker", "inspect", "--type", "container", "--format",
                   "{{json .Mounts}}", container], stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
        buffer = bytearray()
        overflow = threading.Event()

        def drain():
            try:
                while True:
                    block = p.stdout.read(4096)
                    if not block:
                        break
                    if len(buffer) + len(block) > OUTPUT_LIMIT:
                        overflow.set()
                        break
                    buffer.extend(block)
            except OSError:
                overflow.set()
            finally:
                p.stdout.close()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while p.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                raise Refused("container mount inspection refused")
            time.sleep(0.02)
        reader.join(timeout=0.5)
        if p.returncode or overflow.is_set() or reader.is_alive():
            raise Refused("container mount inspection refused")
        validate_mounts(json.loads(buffer))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise Refused("container mount inspection refused") from None
    finally:
        if p is not None and p.poll() is None:
            p.kill()  # Only this supervisor's own inspect client, never a container.
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise Refused("container mount inspection refused") from None


def host(container, identity, config):
    argv = docker_argv(container, identity, config)
    inspect_mount_boundary(container)
    return host_bridge(argv)


def _handlers(stop):
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    return previous


def _restore(previous):
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def write_heartbeat(pipe):
    # Never let an unresponsive Docker client block the host supervisor. A full
    # pipe is a bridge failure; closing it expires the independent inside lease.
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    if os.write(fd, b".\n") != 2:
        raise OSError("heartbeat write incomplete")


def host_bridge(argv, *, popen=subprocess.Popen, interval=1.0, stop=None):
    """Keep the Docker stream open until stop; do not detach or use a TTY."""
    stop = stop or threading.Event()
    previous = _handlers(stop)
    child = None
    try:
        child = popen(argv, stdin=subprocess.PIPE, shell=False)
        while child.poll() is None and not stop.is_set():
            try:
                write_heartbeat(child.stdin)
            except (BrokenPipeError, OSError):
                break
            stop.wait(interval)
        # Closing is intentional: the Linux guardian must clean before Docker
        # exits. If the client hangs, its last heartbeat still expires inside.
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            code = child.wait(timeout=HEARTBEAT_TIMEOUT + CLEANUP_SECONDS + 5)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            return 1
        return 0 if stop.is_set() and code == 0 else code
    finally:
        if child is not None and child.poll() is None:
            # On a parent exception, EOF still reaches the inside guardian.
            try:
                child.stdin.close()
            except OSError:
                pass
            try:
                child.wait(timeout=HEARTBEAT_TIMEOUT + CLEANUP_SECONDS + 5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        _restore(previous)


class Heartbeat:
    def __init__(self, fd=0, *, timeout=HEARTBEAT_TIMEOUT):
        self.fd, self.timeout = fd, timeout
        self.last = time.monotonic()
        self.stop = threading.Event()

    def alive(self):
        if self.stop.is_set():
            return False
        ready, _, _ = select.select([self.fd], [], [], 0)
        if ready:
            data = os.read(self.fd, 4096)
            if not data or any(c not in b".\n" for c in data):
                return False
            self.last = time.monotonic()
        return time.monotonic() - self.last < self.timeout


def become_subreaper():
    if sys.platform != "linux":
        raise Refused("inside guardian requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, this process only
        raise Refused("cannot establish scoped subreaper")
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS, inherited by owned children
        raise Refused("cannot restrict worker privilege gains")


def _proc(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rindex(")") + 2:].split()
        return int(fields[1]), int(fields[19])  # PPID and starttime
    except (OSError, ValueError, IndexError):
        return None


def descendants():
    """Use ancestry, NOT UID, executable names, or global process groups."""
    records = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            record = _proc(int(entry.name))
            if record:
                records[int(entry.name)] = record
    owned = {os.getpid()}
    while True:
        new = {pid for pid, (parent, _) in records.items() if parent in owned} - owned
        if not new:
            return {pid: records[pid][1] for pid in owned if pid != os.getpid()}
        owned.update(new)


def _signal_owned(pid, started, sig):
    # A pidfd prevents reuse between identity check and signal delivery.
    try:
        fd = os.pidfd_open(pid)
        try:
            record = _proc(pid)
            if record and record[1] == started:
                signal.pidfd_send_signal(fd, sig)
        finally:
            os.close(fd)
    except ProcessLookupError:
        pass


def cleanup_owned(*, grace=CLEANUP_SECONDS):
    """Adopted orphan sessions remain direct children of this subreaper.

    Rescan during TERM grace and KILL escalation to catch late forks/reparenting.
    Only the guardian's own descendants are signalled and reaped.
    """
    deadline = time.monotonic() + grace
    hard_deadline = deadline + 2
    while True:
        owned = descendants()
        for pid, started in owned.items():
            _signal_owned(pid, started, signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL)
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break
        if not descendants():
            return
        if time.monotonic() > hard_deadline:
            raise Refused("owned process cleanup did not complete")
        time.sleep(0.02)


def private_environment():
    env = {name: os.environ[name] for name in SECRET_ENV_NAMES if name in os.environ}
    env.update(HOME=PRIVATE, TMPDIR=PRIVATE + "/tmp", PATH="/usr/bin:/bin",
               RCLONE_CACHE_DIR=PRIVATE + "/cache", PYTHONUNBUFFERED="1",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def _private_file(path, *, executable=False):
    p = Path(path)
    if any(q.is_symlink() for q in (p, *p.parents)):
        raise Refused("private file symlink refused")
    try:
        info = p.stat()
        mode = info.st_mode
    except OSError:
        raise Refused("required private file unavailable") from None
    if info.st_uid not in {0, os.getuid()} or not stat.S_ISREG(mode) or mode & 0o022 or (not executable and mode & 0o077):
        raise Refused("unsafe private file permissions")
    if executable and not os.access(p, os.X_OK):
        raise Refused("trusted private rclone is not executable")


def _private_dir(path):
    p = Path(path)
    if any(q.is_symlink() for q in (p, *p.parents)):
        raise Refused("private directory symlink refused")
    try:
        info = p.stat()
    except OSError:
        raise Refused("private directory unavailable") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("private directory ownership/permissions refused")


def validate_aliases(data, destination):
    """Resolve named wrappers to a reviewed network type, without backend I/O."""
    if not isinstance(destination, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[^\x00\r\n]*", destination):
        raise Refused("rclone archive alias unresolved")
    name = destination.split(":", 1)[0]
    seen = set()
    while True:
        if name in seen or name not in data or not isinstance(data[name], dict):
            raise Refused("rclone archive alias unresolved")
        seen.add(name)
        remote = data[name]
        kind = remote.get("type")
        if not isinstance(kind, str):
            raise Refused("rclone archive backend refused")
        if kind in {"alias", "crypt", "chunker"}:
            target = remote.get("remote")
            if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[^\x00\r\n]*", target):
                raise Refused("rclone archive alias unresolved")
            name = target.split(":", 1)[0]
        elif kind in NETWORK_BACKENDS:
            return
        else:
            # Includes unsupported wrappers (cache/hasher/compress), local,
            # memory, multi-remote aggregators and unknown types. Never assume
            # an unreviewed type is terminal: its remote may hide a local path,
            # missing reference or cycle.
            raise Refused("rclone archive backend refused")


def preflight(cfg, heartbeat, *, env=None, timeout=20):
    """Encrypted unlock/config-only probe; stdin closed, no network operation.

    Config dump is sensitive. Both pipes are bounded and suppressed; exceptions
    and diagnostics never include captured output, env values or original errors.
    The outer guardian's finally block owns cleanup on every refusal.
    """
    if cfg.rclone_binary != PRIVATE + "/bin/rclone" or cfg.rclone_config != PRIVATE + "/rclone.conf" or cfg.archive_remote != "archives:google-takeout":
        raise Refused("production private rclone paths/alias required")
    _private_dir(PRIVATE)
    _private_file(cfg.rclone_binary, executable=True)
    _private_file(cfg.rclone_config)
    try:
        with open(cfg.rclone_config, "rb") as f:
            encrypted = b"RCLONE_ENCRYPT_V0:" in f.read(1024)
    except OSError:
        raise Refused("encrypted config unavailable") from None
    if not encrypted:
        raise Refused("encrypted rclone config required")
    argv = [cfg.rclone_binary, "config", "dump", "--config", cfg.rclone_config, "--ask-password=false"]
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, start_new_session=True)
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    def drain(pipe, buf):
        try:
            while True:
                block = pipe.read(4096)
                if not block:
                    break
                if len(buf) + len(block) > OUTPUT_LIMIT:
                    overflow.set()
                buf.extend(block[:max(0, OUTPUT_LIMIT - len(buf))])
        except OSError:
            overflow.set()
        finally:
            pipe.close()
    threads = [threading.Thread(target=drain, args=(pipe, buf), daemon=True)
               for pipe, buf in zip((p.stdout, p.stderr), buffers)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    while p.poll() is None:
        if overflow.is_set() or not heartbeat.alive() or time.monotonic() > deadline:
            raise Refused("rclone noninteractive preflight refused")
        time.sleep(0.02)
    for thread in threads:
        thread.join(timeout=0.5)
    if p.returncode or overflow.is_set() or any(t.is_alive() for t in threads) or not heartbeat.alive():
        raise Refused("rclone noninteractive preflight refused")
    try:
        data = json.loads(buffers[0])
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise Refused("rclone noninteractive preflight refused") from None
    validate_aliases(data, cfg.archive_remote)


def supervise(argv, heartbeat, *, env=None, check=None, cleanup_grace=CLEANUP_SECONDS):
    """Linux ownership boundary, also injectable for offline process tests."""
    become_subreaper()
    previous = _handlers(heartbeat.stop)
    try:
        if not heartbeat.alive():
            raise Refused("host heartbeat unavailable")
        if check:
            check()
        if not heartbeat.alive():
            raise Refused("host heartbeat unavailable")
        worker = subprocess.Popen(argv, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        while worker.poll() is None:
            if not heartbeat.alive():
                return 0
            time.sleep(0.05)
        return worker.returncode
    finally:
        cleanup_owned(grace=cleanup_grace)
        _restore(previous)


def inside(config):
    if sys.platform != "linux" or os.getuid() == 0 or os.getgid() == 0:
        raise Refused("Linux dedicated non-root identity required")
    if config != PRIVATE + "/watch.json":
        raise Refused("private container config required")
    _private_dir(PRIVATE)
    _private_file(config)
    # -I deliberately excludes PYTHONPATH and the script directory. Add only
    # the frozen checkout, never the browser profile or user site-packages.
    sys.path.insert(0, CODE)
    from autopilot.watch import WatchConfig
    cfg = WatchConfig.load(config)
    for key in ("state_dir", "staging_root", "ledger"):
        if not getattr(cfg, key).startswith(PRIVATE + "/"):
            raise Refused("encrypted private worker paths required")
    env = private_environment()
    os.environ.clear()
    os.environ.update(env)
    heartbeat = Heartbeat()
    # -m needs the checkout; isolated Python would exclude cwd. The worker has
    # a sanitized environment and -s disables user-site imports.
    return supervise([PYTHON, "-s", "-B", "-u", "-m", "autopilot", "watch", "--config", config],
                     heartbeat, env=env, check=lambda: preflight(cfg, heartbeat, env=env))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--container", default="takeout-webgui")
    parser.add_argument("--identity", default="20001:20001")
    parser.add_argument("--config", default=PRIVATE + "/watch.json")
    args = parser.parse_args()
    try:
        return inside(args.config) if args.inside else host(args.container, args.identity, args.config)
    except (Refused, OSError, ValueError):
        print("container watcher refused; check private provisioning/preflight", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
