from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    assert "User=sheltercheck" in app_unit
    assert "--config /etc/sheltercheck/config.toml" in app_unit
    assert "Requires=signal-cli.service" in app_unit
    assert "ProtectSystem=strict" in app_unit


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
