import json
from types import SimpleNamespace

import pytest

import autopilot.watch as module
from autopilot.discovery import DiscoveredExport
from autopilot.watch import InstanceLock, LockBusy, WatchConfig, Watcher, watch

ID = "12345678-1234-1234-1234-123456789abc"


def config(tmp_path, accounts=1):
    return WatchConfig.from_dict(dict(
        state_dir=str(tmp_path / "state"), ledger=str(tmp_path / "ledger.db"),
        staging_root=str(tmp_path / "staging"), archive_root=str(tmp_path / "archive"),
        archive_remote="archive:takeout", cdp_http="http://127.0.0.1:9222",
        poll_seconds=10, retry_max_seconds=100, reauth_retry_seconds=1000,
        min_headroom=100, staging_reserve=200,
        accounts=[dict(name="account" + str(i), user_id=str(100 + i),
                       manage_url="https://takeout.google.com/manage?user=" + str(100 + i))
                  for i in range(accounts)]))


def export(a, status="ready", count=2):
    return DiscoveredExport(ID, a.user_id, "https://takeout.google.com/manage/archive/" + ID + "?user=" + a.user_id, status, count)


def outcome(status="incomplete", expected=2, done=0):
    return SimpleNamespace(status=status, parts_expected=expected, parts_done=done)


@pytest.fixture
def frozen_config():
    # Exercise the real, concurrently integrated frozen RunConfig API.
    assert {"archive_remote", "staging_reserve", "cached_transfer_only"} <= set(module.RunConfig.__dataclass_fields__)


def worker(cfg, **kw):
    from pathlib import Path
    Path(cfg.state_dir).mkdir()
    return Watcher(cfg, notifier=lambda msg: None, **kw)


def test_pending_to_ready_then_restart_dedup(tmp_path, frozen_config):
    cfg = config(tmp_path)
    ready = [False]
    calls = []
    clock = [100]
    def discovery(a, c):
        e = export(a, "ready" if ready[0] else "pending", 2 if ready[0] else 0)
        return [e, e]
    def runner(c):
        calls.append(c)
        return outcome("incomplete" if c.mint_only else "complete", done=0 if c.mint_only else 2)
    w = worker(cfg, discovery=discovery, runner=runner, clock=lambda: clock[0])
    w.tick()
    assert calls == []
    ready[0] = True
    w.tick()
    assert len(calls) == 2
    assert calls[0].mint_only and calls[0].max_parts is None
    assert calls[1].cached_transfer_only and calls[1].max_parts == 1
    assert calls[1].min_headroom == 100 and calls[1].staging_reserve == 200
    assert calls[1].archive_remote.endswith("account0/" + ID)
    w2 = Watcher(cfg, discovery=discovery, runner=runner, clock=lambda: 200, notifier=lambda m: None)
    w2.load()
    w2.tick()
    assert len(calls) == 2
    assert w2.state["jobs"]["account0/" + ID]["status"] == "complete"


def test_two_accounts_reauth_cached_fairness(tmp_path, frozen_config):
    cfg = config(tmp_path, 2)
    calls, messages = [], []
    now = [100]
    def runner(c):
        calls.append((c.account, c.mint_only, c.cached_transfer_only))
        if c.account == "account0":
            return outcome("needs_reauth" if c.mint_only else "incomplete", done=1)
        return outcome("incomplete" if c.mint_only else "complete", done=2)
    w = worker(cfg, discovery=lambda a, c: [export(a)], runner=runner, clock=lambda: now[0])
    w.notifier = messages.append
    w.tick()
    assert w.state["jobs"]["account0/" + ID]["status"] == "needs_reauth"
    assert w.state["jobs"]["account1/" + ID]["status"] == "complete"
    now[0] += 10
    w.tick()
    assert calls[-1] == ("account0", False, True)
    assert sum(m for a, m, t in calls if a == "account0") == 1
    assert any("needs_reauth" in m for m in messages)


def test_backoff_and_exception_redaction(tmp_path, frozen_config):
    cfg = config(tmp_path)
    calls, now = [], [100]
    def runner(c):
        calls.append(c)
        raise RuntimeError("https://secret?rapt=PASSWORD Cookie=secret")
    w = worker(cfg, discovery=lambda a, c: [export(a)], runner=runner, clock=lambda: now[0])
    w.tick()
    j = w.state["jobs"]["account0/" + ID]
    assert j["retry_at"] == 120
    now[0] = 110
    w.tick()
    assert len(calls) == 1
    now[0] = 120
    w.tick()
    assert j["retry_at"] == 160
    raw = w.path.read_text()
    assert "PASSWORD" not in raw and "rapt" not in raw and "Cookie" not in raw


def test_zero_part_false_success(tmp_path, frozen_config):
    cfg = config(tmp_path)
    w = worker(cfg, discovery=lambda a, c: [export(a)], runner=lambda c: outcome("complete", 0, 0), clock=lambda: 100)
    w.tick()
    assert w.state["jobs"]["account0/" + ID]["status"] == "incomplete"


def test_discovery_mismatch_blocks_before_runner(tmp_path, frozen_config):
    cfg = config(tmp_path)
    calls = []
    wrong = DiscoveredExport(ID, "999", "https://takeout.google.com/manage/archive/" + ID + "?user=999", "ready", 1)
    w = worker(cfg, discovery=lambda a, c: [wrong], runner=calls.append, clock=lambda: 100)
    w.tick()
    assert not calls and w.state["accounts"]["account0"]["status"] == "identity_or_discovery_blocked"


def test_lock_contention(tmp_path):
    import subprocess
    import sys
    with InstanceLock(tmp_path / "lock"):
        script = "from autopilot.watch import InstanceLock, LockBusy\nimport sys\ntry:\n with InstanceLock(sys.argv[1]): pass\nexcept LockBusy: sys.exit(7)\n"
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "lock")], capture_output=True, timeout=15)
        assert result.returncode == 7
        with pytest.raises(LockBusy):
            with InstanceLock(tmp_path / "lock"):
                pytest.fail("lock admitted a second worker")
    with InstanceLock(tmp_path / "lock"):
        pass


def test_dry_run_no_writes_no_effects(tmp_path):
    cfg = config(tmp_path)
    def forbidden(*args):
        pytest.fail("dry-run effect")
    assert watch(cfg, dry_run=True, discovery=forbidden, runner=forbidden)["status"] == "dry_run"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [("poll_seconds", True), ("min_headroom", "5"), ("staging_reserve", -1), ("archive_remote", "x:../escape"), ("cdp_http", "http://evil.test:9222"), ("rclone_binary", 3)])
def test_config_invalid_types_before_effects(tmp_path, field, value):
    from dataclasses import replace
    cfg = replace(config(tmp_path), **{field: value})
    with pytest.raises(ValueError):
        Watcher(cfg)
    assert list(tmp_path.iterdir()) == []


def test_notification_failure_preserves_state(tmp_path, frozen_config):
    w = worker(config(tmp_path), discovery=lambda a, c: [export(a)], runner=lambda c: outcome(), clock=lambda: 100)
    w.notifier = lambda m: (_ for _ in ()).throw(RuntimeError("token"))
    w.tick()
    assert w.path.exists()


def test_terminal_blocks_no_runner(tmp_path, frozen_config):
    calls = []
    w = worker(config(tmp_path), discovery=lambda a, c: [export(a, "quota_exceeded")], runner=calls.append)
    w.tick()
    assert not calls
    assert w.state["jobs"]["account0/" + ID]["status"] == "quota_exceeded"


def test_cli_watch_dry_run(tmp_path, capsys):
    from autopilot.__main__ import main
    from dataclasses import asdict
    cfg = asdict(config(tmp_path))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    assert main(["watch", "--config", str(path), "--once", "--dry-run"]) == 0
    assert "dry_run" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == [path]



def test_discovery_reauth_backoff_restart_and_other_account(tmp_path, frozen_config):
    from autopilot.discovery import DiscoveryBlocked
    cfg = config(tmp_path, 2)
    calls = []
    def discovery(a, c):
        calls.append(a.name)
        if a.name == "account0":
            raise DiscoveryBlocked("needs_reauth")
        return [export(a)]
    runner_calls = []
    def runner(c):
        runner_calls.append(c.account)
        return outcome("incomplete" if c.mint_only else "complete", done=2)
    w = worker(cfg, discovery=discovery, runner=runner, clock=lambda: 100)
    w.tick()
    assert w.state["accounts"]["account0"]["retry_at"] == 1100
    assert runner_calls == ["account1", "account1"]
    w2 = Watcher(cfg, discovery=discovery, runner=runner, clock=lambda: 110, notifier=lambda m: None)
    w2.load()
    w2.tick()
    assert calls.count("account0") == 1
    assert w2.state["accounts"]["account0"]["status"] == "needs_reauth"


def test_mint_only_incomplete_is_urls_earned_not_complete(tmp_path, frozen_config):
    w = worker(config(tmp_path), discovery=lambda a, c: [export(a)], runner=lambda c: outcome(), clock=lambda: 100)
    w.tick()
    job = w.state["jobs"]["account0/" + ID]
    assert job["mint_status"] == "urls_earned"
    assert job["status"] == "incomplete" and job["parts_done"] == 0


def test_telegram_bounded_no_token_diagnostics(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:OFFLINE_SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    requests = []
    def fail(req, timeout):
        requests.append((req, timeout))
        raise RuntimeError(req.full_url)
    monkeypatch.setattr(module, "urlopen", fail)
    module.notify("account0: needs_reauth")
    assert len(requests) == 1 and requests[0][1] == 5
    assert json.loads(requests[0][0].data)["text"] == "account0: needs_reauth"
    assert capsys.readouterr().out == "" and capsys.readouterr().err == ""


def test_loaded_state_whitelist_drops_secrets(tmp_path):
    cfg = config(tmp_path)
    w = worker(cfg)
    w.path.write_text(json.dumps({"jobs": {"account0/" + ID: {"status": "complete", "minted_url": "https://x?rapt=SECRET", "cookie": "SECRET"}}}))
    w.load()
    w.save()
    assert "SECRET" not in w.path.read_text() and "minted_url" not in w.path.read_text()
