from __future__ import annotations

from pathlib import Path

import pytest

from sheltercheck.config import Config, normalize_text
from sheltercheck.models import Member
from sheltercheck.roster import Roster


ACI_1 = "00000000-0000-4000-8000-000000000001"
ACI_2 = "00000000-0000-4000-8000-000000000002"
ACI_3 = "00000000-0000-4000-8000-000000000003"
AUTHOR = "00000000-0000-4000-8000-000000000010"


@pytest.fixture
def members() -> tuple[Member, ...]:
    return (
        Member(ACI_1, "Іваненко І.І", "+380501111111"),
        Member(ACI_2, "Петренко П.П", "+380502222222"),
        Member(ACI_3, "Коваль А.А", "+380503333333"),
    )


@pytest.fixture
def roster(members: tuple[Member, ...]) -> Roster:
    return Roster(
        members,
        {member.signal_aci: member for member in members},
        {member.display_name: member for member in members},
    )


@pytest.fixture
def app_config(tmp_path: Path, members: tuple[Member, ...]) -> Config:
    released = tmp_path / "released_today.txt"
    released.write_text(
        "\n".join(member.display_name for member in members) + "\n", encoding="utf-8"
    )
    return Config(
        signal_cli_url="http://127.0.0.1:8080",
        monitor_group_id="monitor-group",
        report_group_id="report-group",
        trigger_texts=frozenset({normalize_text("Всі в укритті?")}),
        trigger_author_uuids=frozenset({AUTHOR}),
        accepted_reactions=frozenset({"➕"}),
        wait_seconds=10,
        state_db=tmp_path / "state.sqlite3",
        roster_file=tmp_path / "roster.csv",
        released_file=released,
        command_group_id="command-group",
        command_author_uuids=frozenset({AUTHOR}),
        edit_debounce_seconds=1.0,
    )
