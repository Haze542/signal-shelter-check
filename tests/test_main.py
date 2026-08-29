from __future__ import annotations

from pathlib import Path

from sheltercheck.__main__ import main

from conftest import ACI_1, AUTHOR


def test_validate_config_cli_remains_compatible(tmp_path: Path, capsys) -> None:
    (tmp_path / "roster.csv").write_text(
        "signal_aci,display_name,phone\n"
        f"{ACI_1},Іваненко І.І,+380501111111\n",
        encoding="utf-8",
    )
    (tmp_path / "released_today.txt").write_text("Іваненко І.І\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f'''signal_cli_url = "http://127.0.0.1:8080"
monitor_group_id = "monitor"
report_group_id = "report"
command_group_id = "commands"
command_author_uuids = ["{AUTHOR}"]
trigger_texts = ["Всі в укритті?"]
trigger_author_uuids = ["{AUTHOR}"]
accepted_reactions = ["➕"]
wait_seconds = 600
state_db = "state.sqlite3"
roster_file = "roster.csv"
released_file = "released_today.txt"
''',
        encoding="utf-8",
    )

    assert main(["--config", str(config), "--validate-config"]) == 0
    output = capsys.readouterr().out
    assert "Configuration: valid" in output
    assert "Roster: 1 members" in output
    assert "Released today: 1 members" in output
