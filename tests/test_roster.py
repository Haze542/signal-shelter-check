from __future__ import annotations

from pathlib import Path

import pytest

from sheltercheck.roster import RosterError, load_released_members, load_roster


HEADER = "signal_aci,display_name,phone\n"
ACI_1 = "00000000-0000-4000-8000-000000000001"
ACI_2 = "00000000-0000-4000-8000-000000000002"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_roster_and_released_list(tmp_path: Path) -> None:
    roster = load_roster(
        write(
            tmp_path / "roster.csv",
            HEADER
            + f"{ACI_1},Іваненко І.І,+380501111111\n"
            + f"{ACI_2},Петренко П.П,+380502222222\n",
        )
    )
    released = load_released_members(
        write(tmp_path / "released.txt", "  Петренко П.П \n\nІваненко І.І\n"), roster
    )

    assert len(roster) == 2
    assert [member.signal_aci for member in released] == [ACI_2, ACI_1]


@pytest.mark.parametrize(
    "rows, expected",
    [
        (
            f"{ACI_1},Іваненко І.І,+380501111111\n"
            f"{ACI_1},Петренко П.П,+380502222222\n",
            "duplicate signal_aci",
        ),
        (
            f"{ACI_1},Іваненко І.І,+380501111111\n"
            f"{ACI_2},Іваненко І.І,+380502222222\n",
            "duplicate display_name",
        ),
        (f"{ACI_1},,+380501111111\n", "display_name is required"),
        (f"{ACI_1},Іваненко І.І,0501111111\n", "E.164"),
    ],
)
def test_invalid_roster(tmp_path: Path, rows: str, expected: str) -> None:
    with pytest.raises(RosterError, match=expected):
        load_roster(write(tmp_path / "roster.csv", HEADER + rows))


def test_unknown_and_duplicate_released_names_are_rejected(tmp_path: Path) -> None:
    roster = load_roster(
        write(
            tmp_path / "roster.csv",
            HEADER + f"{ACI_1},Іваненко І.І,+380501111111\n",
        )
    )
    released = write(
        tmp_path / "released.txt", "Іваненко І.І\nНевідомий Н.Н\nІваненко І.І\n"
    )

    with pytest.raises(RosterError) as caught:
        load_released_members(released, roster)
    assert "unknown display_name" in str(caught.value)
    assert "duplicate display_name" in str(caught.value)

