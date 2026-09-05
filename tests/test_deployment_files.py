from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

from sheltercheck.roster import load_released_members, load_roster


ROOT = Path(__file__).resolve().parents[1]


def test_deployment_shell_scripts_have_valid_syntax_and_are_executable() -> None:
    for name in ("install.sh", "update.sh", "backup.sh"):
        path = ROOT / "deploy" / name
        assert os.access(path, os.X_OK)
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_systemd_units_use_dedicated_identity_and_loopback() -> None:
    signal_unit = (ROOT / "deploy" / "signal-cli.service").read_text(encoding="utf-8")
    app_unit = (ROOT / "deploy" / "sheltercheck.service").read_text(encoding="utf-8")

    assert "User=sheltercheck" in signal_unit
    assert "--http 127.0.0.1:8080" in signal_unit
    assert "--data-dir /var/lib/sheltercheck/signal-cli" in signal_unit
    assert "--no-receive-stdout" in signal_unit
    assert "ExecStartPost=" in signal_unit
    assert "signal_cli_readiness.py" in signal_unit
    assert "RestartSec=10" in signal_unit
    assert "StartLimitIntervalSec=0" in signal_unit
    assert "User=sheltercheck" in app_unit
    assert "--config /etc/sheltercheck/config.toml" in app_unit
    assert "Wants=network-online.target signal-cli.service" in app_unit
    assert "Requires=signal-cli.service" not in app_unit
    assert "ExecStartPre=" in app_unit
    assert "signal_cli_readiness.py" in app_unit
    assert "ProtectSystem=strict" in app_unit


def _readiness_module() -> ModuleType:
    path = ROOT / "deploy" / "signal_cli_readiness.py"
    spec = importlib.util.spec_from_file_location("signal_cli_readiness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signal_cli_readiness_requires_loaded_account(capsys) -> None:
    module = _readiness_module()
    module._get_http_check = lambda base_url, timeout: None
    module._list_accounts = lambda base_url, timeout: ("+380000000001",)
    assert module.wait_until_ready(
        "http://127.0.0.1:8080",
        timeout_seconds=1,
        request_timeout_seconds=0.5,
        retry_interval_seconds=0.01,
        wait_for_account=False,
    ) is True
    output = capsys.readouterr()
    assert "HTTP API is reachable" in output.out
    assert "ready: 1 account loaded" in output.out

    module._list_accounts = lambda base_url, timeout: ()
    assert module.wait_until_ready(
        "http://127.0.0.1:8080",
        timeout_seconds=1,
        request_timeout_seconds=0.5,
        retry_interval_seconds=0.01,
        wait_for_account=False,
    ) is False
    output = capsys.readouterr()
    assert "no accounts loaded" in output.err
    assert "systemd recovery can retry" in output.err


def test_signal_cli_readiness_can_wait_across_automatic_restart(capsys) -> None:
    module = _readiness_module()
    module._get_http_check = lambda base_url, timeout: None
    responses = iter(((), ("+380000000001",)))
    module._list_accounts = lambda base_url, timeout: next(responses)

    assert module.wait_until_ready(
        "http://127.0.0.1:8080",
        timeout_seconds=1,
        request_timeout_seconds=0.5,
        retry_interval_seconds=0.01,
        wait_for_account=True,
    ) is True
    output = capsys.readouterr()
    assert "not ready: no accounts loaded; retrying" in output.err
    assert "ready: 1 account loaded" in output.out


def test_repository_examples_are_synthetic_and_production_paths_are_absolute() -> None:
    roster_path = ROOT / "examples" / "roster.example.csv"
    released_path = ROOT / "examples" / "released_today.example.txt"
    roster = load_roster(roster_path)
    released = load_released_members(released_path, roster)
    assert len(roster) == 3
    assert len(released) == 2
    config = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert 'roster_file = "/var/lib/sheltercheck/roster.csv"' in config
    assert 'released_file = "/var/lib/sheltercheck/released_today.txt"' in config
    assert 'state_db = "/var/lib/sheltercheck/state.sqlite3"' in config


def test_installer_deploys_readiness_helper_and_update_rechecks_signal() -> None:
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    helper = ROOT / "deploy" / "signal_cli_readiness.py"
    assert os.access(helper, os.X_OK)
    assert '"${SOURCE_DIR}/deploy/signal_cli_readiness.py"' in install
    assert '"${APP_DIR}/signal_cli_readiness.py"' in install
    assert "systemctl restart signal-cli.service" in update
    assert "--wait-for-account" in update


def test_gitignore_covers_production_state_and_identity_files() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (
        "config.toml",
        "data/",
        "state/",
        "debug_events/",
        "signal-cli/",
        ".venv/",
        "*.sqlite",
        "*.sqlite3",
        "*.sqlite-wal",
        "*.sqlite-shm",
    ):
        assert pattern in ignored
