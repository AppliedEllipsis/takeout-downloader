"""Recovery CLI must expose the same checked backend mode as the watcher."""
import pytest

import autopilot.__main__ as cli
from autopilot.run import RunOutcome


def run_args(tmp_path):
    return [
        "run", "--archive-id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "--ledger", str(tmp_path / "ledger.sqlite"),
        "--staging", str(tmp_path / "staging"),
        "--archive", str(tmp_path / "archive"),
    ]


def test_checked_remote_arguments_reach_engine(tmp_path, monkeypatch):
    captured = []

    def runner(cfg):
        captured.append(cfg)
        return RunOutcome(cfg.archive_id, "incomplete")

    monkeypatch.setattr(cli, "run_once_sync", runner)
    args = cli.build_parser().parse_args(run_args(tmp_path) + [
        "--archive-remote", "archives:takeout/owner/export",
        "--rclone-binary", "custom-rclone",
        "--rclone-config", str(tmp_path / "encrypted.conf"),
        "--remote-timeout", "42.5",
        "--staging-reserve", "1024",
        "--cached-transfer-only",
        "--work-url", "https://takeout.google.com/manage/archive/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa?user=123",
    ])
    assert cli.cmd_run(args) == cli.EXIT_OTHER
    cfg = captured[0]
    assert cfg.archive_remote == "archives:takeout/owner/export"
    assert cfg.rclone_binary == "custom-rclone"
    assert cfg.rclone_config == str(tmp_path / "encrypted.conf")
    assert cfg.remote_timeout == 42.5
    assert cfg.staging_reserve == 1024
    assert cfg.cached_transfer_only is True
    assert cfg.work_url.endswith("?user=123")


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_remote_timeout_is_finite_positive(tmp_path, value):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(run_args(tmp_path) + ["--remote-timeout=" + value])
    assert exc.value.code == 2


def test_negative_staging_reserve_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(run_args(tmp_path) + ["--staging-reserve=-1"])
    assert exc.value.code == 2
