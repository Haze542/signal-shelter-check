from __future__ import annotations

from pathlib import Path

import sheltercheck.health as health_module
from sheltercheck.__main__ import main
from sheltercheck.database import StateDatabase
from sheltercheck.signal_client import SignalClientError

from conftest import ACI_1, ACI_2, AUTHOR


def deployment_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_dir = tmp_path / "production-data"
    data_dir.mkdir()
    roster = data_dir / "roster.csv"
    released = data_dir / "released_today.txt"
    database_path = data_dir / "state.sqlite3"
    roster.write_text(
        "signal_aci,display_name,phone\n"
        f"{ACI_1},Іваненко І.І,+380501111111\n"
        f"{ACI_2},Петренко П.П,+380502222222\n",
        encoding="utf-8",
    )
    released.write_text("Іваненко І.І\n", encoding="utf-8")
    database = StateDatabase(database_path)
    database.close()

    config = tmp_path / "production-config.toml"
    config.write_text(
        f'''signal_cli_url = "http://127.0.0.1:8080"
monitor_group_id = "monitor-group"
report_group_id = "report-group"
command_group_id = "command-group"
command_author_uuids = ["{AUTHOR}"]
trigger_texts = ["Всі в укритті?"]
trigger_author_uuids = ["{AUTHOR}"]
accepted_reactions = ["➕"]
wait_seconds = 600
state_db = "{database_path}"
roster_file = "{roster}"
released_file = "{released}"
''',
        encoding="utf-8",
    )
    return config, roster, released, database_path


def test_health_success_and_failure_exit_codes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config, _, _, _ = deployment_files(tmp_path)

    async def healthy(config_value) -> None:
        return None

    monkeypatch.setattr(health_module, "check_signal_daemon", healthy)
    assert main(["--config", str(config), "--health"]) == 0
    output = capsys.readouterr().out
    assert "Application config: OK" in output
    assert "Signal daemon: OK" in output
    assert "Roster: 2 members" in output
    assert "Released today: 1 members" in output
    assert "SQLite: OK" in output
    assert "STATUS: OK" in output

    async def unhealthy(config_value) -> None:
        raise SignalClientError("connection refused on 127.0.0.1:8080")

    monkeypatch.setattr(health_module, "check_signal_daemon", unhealthy)
    assert main(["--config", str(config), "--health"]) == 1
    output = capsys.readouterr().out
    assert "Signal daemon: FAILED" in output
    assert "connection refused" in output
    assert "STATUS: FAILED" in output


def test_released_show_and_validate_with_absolute_paths_outside_repository(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config, _, _, _ = deployment_files(tmp_path)
    unrelated_cwd = tmp_path / "somewhere-else"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    assert main(["--config", str(config), "released", "show"]) == 0
    assert capsys.readouterr().out == "Released today: 1\n\nІваненко І.І\n"

    assert main(["--config", str(config), "released", "validate"]) == 0
    output = capsys.readouterr().out
    assert "Released list: valid" in output
    assert "Released today: 1 members" in output


def test_released_import_valid_is_atomic_and_invalid_preserves_file(
    tmp_path: Path, capsys
) -> None:
    config, _, released, _ = deployment_files(tmp_path)
    valid_import = tmp_path / "valid.txt"
    valid_import.write_text(" Петренко П.П \n\nІваненко І.І\n", encoding="utf-8")

    assert main(["--config", str(config), "released", "import", str(valid_import)]) == 0
    assert released.read_text(encoding="utf-8") == "Петренко П.П\nІваненко І.І\n"
    assert "imported successfully" in capsys.readouterr().out

    before = released.read_bytes()
    invalid_import = tmp_path / "invalid.txt"
    invalid_import.write_text("Невідомий Н.Н\n", encoding="utf-8")
    assert main(["--config", str(config), "released", "import", str(invalid_import)]) == 2
    assert released.read_bytes() == before
    assert "unknown display_name" in capsys.readouterr().err
