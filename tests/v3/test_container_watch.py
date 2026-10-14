"""Offline only. Linux process-tree tests explicitly skip on Windows."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tools import container_watch as c


def test_exec_argv_names_only_and_nonroot_container_paths():
    secrets = {name: "DO_NOT_LOG_" + name for name in c.SECRET_ENV_NAMES}
    args = c.docker_argv("takeout-webgui", "20001:20001", c.PRIVATE + "/watch.json", secrets)
    assert args[:4] == ["/usr/bin/docker", "exec", "-i", "--user"]
    assert "20001:20001" in args and c.CODE in args and c.PYTHON in args
    assert "-I" in args and "--inside" in args
    assert "-d" not in args and "-t" not in args
    for name, value in secrets.items():
        assert name in args and value not in " ".join(args)
        assert name + "=" not in " ".join(args)


@pytest.mark.parametrize("identity", ["0:0", "0:20001", "1000:0", "root", "-1:1", "1:1 --privileged"])
def test_root_or_shell_identity_refused(identity):
    with pytest.raises(c.Refused):
        c.docker_argv("takeout-webgui", identity, c.PRIVATE + "/watch.json")


@pytest.mark.parametrize("container,config", [("evil;cmd", c.PRIVATE + "/watch.json"), ("takeout-webgui", "/tmp/watch.json")])
def test_unbounded_exec_inputs_refused(container, config):
    with pytest.raises(c.Refused):
        c.docker_argv(container, "20001:20001", config)


class FakePipe(io.BytesIO):
    def close(self):
        self.was_closed = True  # retain bytes for assertions


class FakeDocker:
    def __init__(self, stop=None, code=0, failure=False):
        self.stdin = FakePipe()
        self.code, self.stop, self.failure = code, stop, failure
        self.waited = False
        self.calls = 0
    def poll(self):
        self.calls += 1
        if self.failure:
            return self.code
        if self.calls > 1 and self.stop:
            self.stop.set()
        return self.code if self.waited else None
    def wait(self, timeout=None):
        assert getattr(self.stdin, "was_closed", False)
        self.waited = True
        return self.code


def test_host_stop_closes_stdin_and_waits_for_guardian(monkeypatch):
    monkeypatch.setattr(c, "write_heartbeat", lambda pipe: pipe.write(b".\n"))
    stop = threading.Event()
    proc = FakeDocker(stop=stop)
    assert c.host_bridge(["offline"], popen=lambda *a, **k: proc, stop=stop, interval=0.001) == 0
    assert proc.stdin.getvalue() == b".\n"
    assert proc.waited


def test_host_reports_docker_failure_and_closes_stdin():
    proc = FakeDocker(code=17, failure=True)
    assert c.host_bridge(["offline"], popen=lambda *a, **k: proc) == 17
    assert proc.stdin.was_closed


def test_host_exception_still_delivers_eof(monkeypatch):
    monkeypatch.setattr(c, "write_heartbeat", lambda pipe: pipe.write(b".\n"))
    proc = FakeDocker()
    def broken_write(*a):
        raise RuntimeError("offline parent failure")
    proc.stdin.write = broken_write
    with pytest.raises(RuntimeError):
        c.host_bridge(["offline"], popen=lambda *a, **k: proc)
    assert proc.stdin.was_closed and proc.waited


# The same refusal table exercises pure validation and real preflight->supervise.
# In particular, the five independently reproduced bypasses must not start a
# worker. No entry contains real credentials or contacts a network backend.
REFUSED_ALIAS_CONFIGS = [
    pytest.param({}, id="missing-archives"),
    pytest.param({"archives": {"type": "alias", "remote": "missing:x"}}, id="alias-missing"),
    pytest.param({"archives": {"type": "alias", "remote": "archives:x"}}, id="alias-cycle"),
    pytest.param({"archives": {"type": "alias", "remote": "/local/path"}}, id="alias-local-path"),
    pytest.param({"archives": {"type": "alias", "remote": ":s3:secret"}}, id="alias-inline"),
    pytest.param({"archives": {"type": "alias", "remote": ["one:x", "two:y"]}}, id="alias-multiremote"),
    pytest.param({"archives": {"type": "alias", "remote": "second:x"},
                  "second": {"type": "alias", "remote": "archives:y"}}, id="named-cycle"),
    pytest.param({"archives": {"type": "alias", "remote": "wrapped:root"},
                  "wrapped": {"type": "cache", "remote": "/tmp/local"}}, id="cache-local-bypass"),
    pytest.param({"archives": {"type": "alias", "remote": "wrapped:root"},
                  "wrapped": {"type": "hasher", "remote": "missing:path"}}, id="hasher-missing-bypass"),
    pytest.param({"archives": {"type": "alias", "remote": "wrapped:root"},
                  "wrapped": {"type": "compress", "remote": "archives:path"}}, id="compress-cycle-bypass"),
    pytest.param({"archives": {"type": "union", "upstreams": "one:x two:y"},
                  "one": {"type": "s3"}, "two": {"type": "sftp"}}, id="union-network-upstreams"),
    pytest.param({"archives": {"type": "combine", "upstreams": "one=one:x two=two:y"},
                  "one": {"type": "s3"}, "two": {"type": "sftp"}}, id="combine-network-upstreams"),
    pytest.param({"archives": {"type": ["s3"]}}, id="non-string-type"),
    pytest.param({"archives": {}}, id="missing-type"),
    pytest.param({"archives": "s3"}, id="non-object-remote"),
] + [
    pytest.param({"archives": {"type": kind}}, id="terminal-" + kind)
    for kind in ("local", "memory", "madeupbackend", "union", "combine", "S3", "inline", "multiremote")
] + [
    pytest.param({"archives": {"type": kind, "remote": "backend:root"},
                  "backend": {"type": "s3"}}, id=kind + "-even-with-network-target")
    for kind in ("cache", "hasher", "compress")
] + [
    pytest.param({"archives": {"type": kind, "remote": target},
                  "disk": {"type": "local"}}, id=kind + "-" + label)
    for kind in ("crypt", "chunker")
    for label, target in (("missing", "missing:path"), ("cycle", "archives:path"),
                          ("local-path", "/tmp/local"), ("named-local", "disk:path"),
                          ("inline", ":s3:secret"), ("multiremote", ["one:x", "two:y"]))
]


@pytest.mark.parametrize("aliases", REFUSED_ALIAS_CONFIGS)
def test_alias_missing_cycle_inline_or_local_refused(aliases):
    with pytest.raises(c.Refused):
        c.validate_aliases(aliases, "archives:google-takeout")


@pytest.mark.parametrize("destination", ["/tmp/local", ":s3:secret", ["one:x", "two:y"], "archives:x\n", None])
def test_destination_requires_single_named_reference(destination):
    with pytest.raises(c.Refused):
        c.validate_aliases({"archives": {"type": "s3"}}, destination)


# Spell out expected reviewed types rather than deriving them from production.
@pytest.mark.parametrize("kind", ["sftp", "s3", "b2", "azureblob", "drive", "onedrive", "webdav", "ftp", "gcs", "swift"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "alias-chunker-crypt"])
def test_alias_resolves_without_backend_access(kind, wrapped):
    if wrapped:
        data = {"archives": {"type": "alias", "remote": "chunks:root"},
                "chunks": {"type": "chunker", "remote": "encrypted:root"},
                "encrypted": {"type": "crypt", "remote": "backend:root"},
                "backend": {"type": kind}}
    else:
        data = {"archives": {"type": kind}}
    c.validate_aliases(data, "archives:google-takeout")


def fake_preflight(monkeypatch, *, code=0, text=None, header=b"RCLONE_ENCRYPT_V0:\n", heartbeat=True):
    calls = []
    cfg = SimpleNamespace(rclone_binary=c.PRIVATE + "/bin/rclone",
                          rclone_config=c.PRIVATE + "/rclone.conf", archive_remote="archives:google-takeout")
    monkeypatch.setattr(c, "_private_file", lambda *a, **k: None)
    monkeypatch.setattr(c, "_private_dir", lambda *a, **k: None)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.BytesIO(header))
    def spawn(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(stdout=io.BytesIO(text if text is not None else b'{"archives":{"type":"s3"}}'),
                               stderr=io.BytesIO(b"NEVER_EMIT_SECRET"), poll=lambda: code, returncode=code)
    monkeypatch.setattr(c.subprocess, "Popen", spawn)
    hb = SimpleNamespace(alive=lambda: heartbeat)
    return cfg, hb, calls


def test_noninteractive_config_only_preflight_argv_secrecy(monkeypatch, capsys):
    cfg, hb, calls = fake_preflight(monkeypatch)
    c.preflight(cfg, hb, env={"RCLONE_CONFIG_PASS": "NEVER_EMIT_SECRET"})
    args, kw = calls[0]
    assert args[1:3] == ["config", "dump"]
    assert "--ask-password=false" in args and "NEVER_EMIT_SECRET" not in " ".join(args)
    assert kw["stdin"] == subprocess.DEVNULL and kw["start_new_session"] is True
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("kwargs", [{"code": 1}, {"text": b"NOT_JSON_SECRET"},
                                    {"text": b'{}'}, {"text": b"x" * (c.OUTPUT_LIMIT + 1)},
                                    {"header": b"[plaintext]\npassword=secret"}, {"heartbeat": False}])
def test_preflight_refusal_no_captured_output(monkeypatch, capsys, kwargs):
    cfg, hb, _ = fake_preflight(monkeypatch, **kwargs)
    with pytest.raises(c.Refused) as exc:
        c.preflight(cfg, hb)
    assert "SECRET" not in str(exc.value) and "secret" not in str(exc.value)
    assert capsys.readouterr() == ("", "")


def test_private_file_permissions_and_symlink_refusal(tmp_path):
    if os.name == "nt":
        pytest.skip("Unix file modes/symlinks unconfirmed on Windows")
    path = tmp_path / "rclone.conf"
    path.write_bytes(b"RCLONE_ENCRYPT_V0:")
    path.chmod(0o600)
    c._private_file(path)
    path.chmod(0o644)
    with pytest.raises(c.Refused):
        c._private_file(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(c.Refused):
        c._private_file(link)


linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper/pidfd/EOF behavior unconfirmed on Windows")


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("offline subprocess deadline exceeded")


def running(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rindex(")") + 2:].split()[0] != "Z"
    except FileNotFoundError:
        return False


def guardian_code(tmp_path, mode):
    # Worker launches a descendant in its own session; that child creates a
    # second session and both ignore TERM to exercise scoped KILL escalation.
    leaf_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    child_code = (
        "import os,subprocess,sys,time,signal; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"p=subprocess.Popen([sys.executable,'-c',{leaf_code!r}],start_new_session=True); "
        f"open({str(tmp_path / 'descendants')!r},'w').write(str(os.getpid())+' '+str(p.pid)); "
        "time.sleep(60)"
    )
    worker_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True); "
        f"open({str(tmp_path / 'worker')!r},'w').write(str(__import__('os').getpid())); "
        f"\nwhile not Path({str(tmp_path / 'descendants')!r}).exists(): time.sleep(0.01)\n"
        "time.sleep(0.1); " + ("sys.exit(7)" if mode == "worker-failure" else "time.sleep(60)")
    )
    return (
        "import os,sys; from tools import container_watch as c; "
        f"open({str(tmp_path / 'guardian')!r},'w').write(str(os.getpid())); "
        "hb=c.Heartbeat(timeout=2); "
        f"sys.exit(c.supervise([sys.executable,'-c',{worker_code!r}],hb,cleanup_grace=0.2))"
    )


@linux_only
@pytest.mark.parametrize("mode", ["eof", "stop", "lease", "worker-failure", "host-failure"])
def test_guardian_cleans_worker_and_descendant_sessions_not_unrelated(tmp_path, mode):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    guardian = None
    pids = []
    try:
        code = guardian_code(tmp_path, mode)
        if mode == "host-failure":
            host = "from tools import container_watch as c; c.host_bridge(" + repr([sys.executable, "-c", code]) + ",interval=0.05)"
            guardian = subprocess.Popen([sys.executable, "-c", host], stdin=subprocess.DEVNULL)
        else:
            guardian = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE)
            guardian.stdin.write(b".\n")
            guardian.stdin.flush()
        wait_for(lambda: (tmp_path / "descendants").exists() and (tmp_path / "descendants").stat().st_size > 0)
        pids = [int((tmp_path / "worker").read_text()), *map(int, (tmp_path / "descendants").read_text().split())]
        inside_pid = int((tmp_path / "guardian").read_text())
        if mode == "eof":
            guardian.stdin.close()
        elif mode == "stop":
            guardian.send_signal(signal.SIGTERM)
        elif mode == "host-failure":
            guardian.kill()  # writer dies abruptly; Docker-like child's pipe EOF
        assert guardian.wait(timeout=8) == (7 if mode == "worker-failure" else -signal.SIGKILL if mode == "host-failure" else 0)
        wait_for(lambda: all(not running(pid) for pid in [*pids, inside_pid]))
        assert unrelated.poll() is None
    finally:
        if guardian:
            if guardian.stdin and not guardian.stdin.closed:
                guardian.stdin.close()
            if guardian.poll() is None:
                guardian.kill()
                guardian.wait()
        for pid in pids:
            if running(pid):
                os.kill(pid, signal.SIGKILL)  # exact test-owned IDs only
        unrelated.terminate()
        unrelated.wait()


@linux_only
def test_preflight_refusal_never_starts_worker(tmp_path):
    code = (
        "import sys; from tools import container_watch as c; "
        "hb=c.Heartbeat(timeout=2); "
        "defcheck=lambda: (_ for _ in ()).throw(c.Refused('offline locked config')); "
        f"c.supervise([sys.executable,'-c',\"open({str(tmp_path / 'worker-started')!r},'w').write('bad')\"],hb,check=defcheck,cleanup_grace=0.1)"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    _, err = p.communicate(b".\n", timeout=5)
    assert p.returncode != 0 and b"offline locked config" in err
    assert not (tmp_path / "worker-started").exists()


@linux_only
@pytest.mark.parametrize("aliases", REFUSED_ALIAS_CONFIGS)
def test_real_alias_preflight_refusal_never_starts_worker(tmp_path, aliases):
    # Synthetic config dump only: no real rclone, unlock, Docker or worker.
    tmp_path.chmod(0o700)
    binary = tmp_path / "bin/rclone"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\nprint({json.dumps(aliases)!r})\n")
    binary.chmod(0o700)
    config = tmp_path / "rclone.conf"
    config.write_bytes(b"RCLONE_ENCRYPT_V0:\nSYNTHETIC_NOT_CREDENTIALS")
    config.chmod(0o600)
    marker = tmp_path / "worker-started"
    worker = f"open({str(marker)!r},'w').write('bad')"
    code = (
        "import sys; from types import SimpleNamespace; from tools import container_watch as c; "
        f"c.PRIVATE={str(tmp_path)!r}; "
        f"cfg=SimpleNamespace(rclone_binary={str(binary)!r},rclone_config={str(config)!r},archive_remote='archives:google-takeout'); "
        "hb=c.Heartbeat(timeout=3); "
        f"c.supervise([sys.executable,'-c',{worker!r}],hb,"
        "check=lambda:c.preflight(cfg,hb),cleanup_grace=0.1)"
    )
    with subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as guardian:
        try:
            # Keep the pipe open: refusal must be the config gate, not stdin EOF.
            guardian.stdin.write(b".\n")
            guardian.stdin.flush()
            assert guardian.wait(timeout=5) != 0
            output = guardian.stdout.read() + guardian.stderr.read()
            assert b"rclone archive" in output
            assert b"SYNTHETIC_NOT_CREDENTIALS" not in output
            assert not marker.exists()
        finally:
            guardian.stdin.close()
            if guardian.poll() is None:
                guardian.kill()
                guardian.wait()


@linux_only
def test_heartbeat_eof_and_invalid_data():
    for data in (b"", b"invalid"):
        read, write = os.pipe()
        try:
            os.write(write, data)
            os.close(write)
            assert not c.Heartbeat(read).alive()
        finally:
            os.close(read)


def test_deployment_keeps_container_cdp_private_copy_and_bounded_recovery():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "deploy/takeout-autopilot-watch.service").read_text()
    config = json.loads((root / "deploy/watch.example.json").read_text())
    assert "User=root" in unit and "--identity 20001:20001" in unit
    assert "Restart=on-failure" in unit and "RestartSec=30" in unit
    assert "StartLimitIntervalSec=300" in unit and "StartLimitBurst=5" in unit
    assert "KillMode=control-group" in unit and "TimeoutStopSec=25" in unit
    # Offline policy checks only; do not query or activate an installed unit.
    assert "KEEP DISABLED/INACTIVE" in unit and "activation deferred" in unit
    env_files = [line.split("=", 1)[1] for line in unit.splitlines()
                 if line.startswith("EnvironmentFile=")]
    # Exactly one mandatory file; '-' would make the root credential load optional.
    protected_env = "/opt/local_cache_crypt/takeout-autopilot-secrets/credentials.env"
    assert env_files == [protected_env]
    writable_paths = [path for line in unit.splitlines() if line.startswith("ReadWritePaths=")
                      for path in line.split("=", 1)[1].split()]
    host_private = "/opt/local_cache_crypt/_projects/takeout-downloader/config/autopilot"
    assert host_private in writable_paths
    for path in [*writable_paths, host_private, c.PRIVATE, "/work", "/config"]:
        path = path.lstrip("-+").rstrip("/")
        assert protected_env != path and not protected_env.startswith(path + "/")
    assert "UMask=0077" in unit and "NoNewPrivileges=true" in unit
    assert "--config /config/autopilot/watch.json" in unit
    assert config["rclone_binary"] == c.PRIVATE + "/bin/rclone"
    assert config["rclone_config"] == c.PRIVATE + "/rclone.conf"
    assert config["cdp_http"] == "http://127.0.0.1:9222"
    assert config["staging_reserve"] == 100 << 30
    assert '"autoRecapture": false' in (root / "webgui/init_custom.sh").read_text()


def test_worker_environment_drops_unrelated_container_secrets(monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG_PASS", "authorized-secret")
    monkeypatch.setenv("RCLONE_CONFIG_ARCHIVES_REMOTE", ":local:/unsafe")
    monkeypatch.setenv("MANAGER_CAPTURE_TOKEN", "unrelated-secret")
    monkeypatch.setenv("PYTHONPATH", "/unsafe")
    env = c.private_environment()
    assert env["RCLONE_CONFIG_PASS"] == "authorized-secret"
    assert "RCLONE_CONFIG_ARCHIVES_REMOTE" not in env
    assert "MANAGER_CAPTURE_TOKEN" not in env and "PYTHONPATH" not in env
    assert env["RCLONE_CACHE_DIR"] == c.PRIVATE + "/cache"


@linux_only
@pytest.mark.parametrize("mode", ["locked", "hang", "flood"])
def test_real_offline_preflight_refuses_and_reclaims_detached_child(tmp_path, mode):
    # A synthetic executable, NOT production rclone and NOT an unlock attempt.
    binary = tmp_path / "bin/rclone"
    binary.parent.mkdir()
    child = (
        "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"open({str(tmp_path / 'preflight-child')!r},'w').write(str(os.getpid())); time.sleep(60)"
    )
    action = {"locked": "print('SENSITIVE_CAPTURED_OUTPUT'); sys.exit(1)",
              "hang": "time.sleep(60)",
              "flood": "print('SENSITIVE_CAPTURED_OUTPUT' * 60000); time.sleep(60)"}[mode]
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os,subprocess,sys,time\nfrom pathlib import Path\n"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True)\n"
        f"while not Path({str(tmp_path / 'preflight-child')!r}).exists(): time.sleep(0.01)\n"
        + action + "\n"
    )
    binary.chmod(0o700)
    config = tmp_path / "rclone.conf"
    config.write_bytes(b"RCLONE_ENCRYPT_V0:\nSYNTHETIC_NOT_CREDENTIALS")
    config.chmod(0o600)
    code = (
        "import sys; from types import SimpleNamespace; from tools import container_watch as c; "
        f"c.PRIVATE={str(tmp_path)!r}; "
        f"cfg=SimpleNamespace(rclone_binary={str(binary)!r},rclone_config={str(config)!r},archive_remote='archives:google-takeout'); "
        "hb=c.Heartbeat(timeout=10); "
        f"c.supervise([sys.executable,'-c',\"open({str(tmp_path / 'worker-started')!r},'w').write('bad')\"],hb,"
        "check=lambda:c.preflight(cfg,hb,timeout=2.5),cleanup_grace=0.2)"
    )
    guardian = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_pid = None
    try:
        guardian.stdin.write(b".\n")
        guardian.stdin.flush()
        wait_for(lambda: (tmp_path / "preflight-child").exists() and (tmp_path / "preflight-child").stat().st_size > 0)
        child_pid = int((tmp_path / "preflight-child").read_text())
        assert guardian.wait(timeout=8) != 0
        output = guardian.stdout.read() + guardian.stderr.read()
        assert b"SENSITIVE_CAPTURED_OUTPUT" not in output
        assert b"noninteractive preflight refused" in output
        assert not (tmp_path / "worker-started").exists()
        assert not running(child_pid)
    finally:
        guardian.stdin.close()
        if guardian.poll() is None:
            guardian.kill()
            guardian.wait()
        if child_pid and running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@linux_only
def test_full_heartbeat_pipe_cannot_block_supervisor():
    read, write = os.pipe()
    pipe = os.fdopen(write, "wb", buffering=0)
    try:
        os.set_blocking(write, False)
        while True:
            try:
                os.write(write, b"x" * 4096)
            except BlockingIOError:
                break
        started = time.monotonic()
        with pytest.raises(BlockingIOError):
            c.write_heartbeat(pipe)
        assert time.monotonic() - started < 1
    finally:
        pipe.close()
        os.close(read)


@linux_only
def test_private_directory_rejects_shared_browser_access(tmp_path):
    tmp_path.chmod(0o700)
    c._private_dir(tmp_path)
    tmp_path.chmod(0o755)
    with pytest.raises(c.Refused):
        c._private_dir(tmp_path)


class FakeInspect:
    def __init__(self, output, *, code=0, hang=False):
        self.stdout = io.BytesIO(output)
        self.returncode = code
        self.hang = hang
        self.killed = False
        self.waited = False

    def poll(self):
        return None if self.hang and not self.killed else self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        assert timeout == 2
        self.waited = True
        return self.returncode


SAFE_MOUNTS_JSON = json.dumps([{'Type': 'bind', 'Source': '/opt/archives',
                               'Destination': '/opt/archives', 'RW': True}]).encode()


def test_host_inspects_before_exec_and_preserves_identity_and_env_names(monkeypatch):
    calls = []
    def inspect(container):
        calls.append(('inspect', container))
    def bridge(argv):
        assert calls == [('inspect', 'takeout-webgui')]
        calls.append(('exec', argv))
        return 19
    monkeypatch.setattr(c, 'inspect_mount_boundary', inspect)
    monkeypatch.setattr(c, 'host_bridge', bridge)
    monkeypatch.setenv('RCLONE_CONFIG_PASS', 'SYNTHETIC_NEVER_PRINT')
    assert c.host('takeout-webgui', '20001:20001', c.PRIVATE + '/watch.json') == 19
    argv = calls[1][1]
    assert '--user' in argv and '20001:20001' in argv
    assert 'RCLONE_CONFIG_PASS' in argv and 'SYNTHETIC_NEVER_PRINT' not in ' '.join(argv)


def test_host_inspection_refusal_never_executes(monkeypatch, capsys):
    def refuse(container):
        raise c.Refused('container mount inspection refused')
    monkeypatch.setattr(c, 'inspect_mount_boundary', refuse)
    monkeypatch.setattr(c, 'host_bridge', lambda argv: pytest.fail('exec after failed admission'))
    monkeypatch.setattr(sys, 'argv', ['container_watch.py'])
    assert c.main() == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err.splitlines() == ['container watcher refused; check private provisioning/preflight']


def test_invalid_identity_refuses_before_inspection_or_exec(monkeypatch):
    monkeypatch.setattr(c, 'inspect_mount_boundary', lambda _: pytest.fail('inspect invalid input'))
    monkeypatch.setattr(c, 'host_bridge', lambda _: pytest.fail('exec invalid input'))
    with pytest.raises(c.Refused):
        c.host('takeout-webgui', '0:0', c.PRIVATE + '/watch.json')


def test_bounded_inspect_only_mounts_no_stdout_or_raw_metadata(capsys):
    calls = []
    proc = FakeInspect(SAFE_MOUNTS_JSON)
    def spawn(argv, **kw):
        calls.append((argv, kw))
        return proc
    c.inspect_mount_boundary('takeout-webgui', popen=spawn)
    argv, kw = calls[0]
    assert argv == ['/usr/bin/docker', 'inspect', '--type', 'container', '--format',
                    '{{json .Mounts}}', 'takeout-webgui']
    assert kw == dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, shell=False)
    assert proc.stdout.closed and not proc.killed
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('output,code,hang', [
    (b'SENSITIVE_RAW_METADATA', 0, False), (b'null', 0, False), (b'{}', 0, False),
    (b'[]', 0, False), (b'', 0, False), (bytes([255]) + b'SECRET', 0, False),
    (b'x' * (c.OUTPUT_LIMIT + 1), 0, False), (SAFE_MOUNTS_JSON, 1, False),
    (SAFE_MOUNTS_JSON, 0, True), (b'x' * (c.OUTPUT_LIMIT + 1), 0, True),
    (json.dumps([{'Type': 'bind', 'Source': '/opt', 'Destination': '/opt', 'RW': False}]).encode(), 0, False),
], ids=['raw-text', 'null', 'object', 'empty-list', 'empty-output', 'invalid-utf8',
        'flood', 'nonzero', 'timeout', 'flood-hang', 'readonly-ancestor'])
def test_inspect_refuses_missing_malformed_failed_timeout_flood_or_unsafe(output, code, hang, capsys):
    proc = FakeInspect(output, code=code, hang=hang)
    started = time.monotonic()
    with pytest.raises(c.Refused) as exc:
        c.inspect_mount_boundary('takeout-webgui', popen=lambda *a, **k: proc, timeout=0.01)
    assert time.monotonic() - started < 2
    assert str(exc.value) == 'container mount inspection refused'
    assert capsys.readouterr() == ('', '')
    assert proc.stdout.closed
    if hang:
        assert proc.killed and proc.waited


def test_inspect_launch_failure_suppressed(capsys):
    def spawn(*a, **k):
        raise OSError('SENSITIVE_RAW_ERROR')
    with pytest.raises(c.Refused) as exc:
        c.inspect_mount_boundary('takeout-webgui', popen=spawn)
    assert str(exc.value) == 'container mount inspection refused'
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('mode', ['safe', 'hang', 'flood'])
def test_real_synthetic_inspect_client_bounded_and_reaped(mode, capsys):
    # Substitute Python for Docker at the subprocess boundary. No Docker command
    # is executed; these clients emit only synthetic metadata and own no worker.
    action = {
        'safe': f'import sys; sys.stdout.buffer.write({SAFE_MOUNTS_JSON!r})',
        'hang': 'import time; time.sleep(60)',
        'flood': "import sys,time; sys.stdout.buffer.write(b'x' * 2000000); sys.stdout.flush(); time.sleep(60)",
    }[mode]
    clients = []
    def spawn(argv, **kwargs):
        assert argv[:2] == ['/usr/bin/docker', 'inspect']
        client = subprocess.Popen([sys.executable, '-B', '-c', action], **kwargs)
        clients.append(client)
        return client
    started = time.monotonic()
    if mode == 'safe':
        c.inspect_mount_boundary('takeout-webgui', popen=spawn, timeout=2)
    else:
        with pytest.raises(c.Refused):
            c.inspect_mount_boundary('takeout-webgui', popen=spawn, timeout=0.5)
    assert time.monotonic() - started < 5
    assert len(clients) == 1 and clients[0].poll() is not None
    assert capsys.readouterr() == ('', '')
