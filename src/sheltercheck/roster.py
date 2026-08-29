from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .models import Member


class RosterError(ValueError):
    """Roster or released-list validation failed."""


_PHONE_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_FIELDS = ("signal_aci", "display_name", "phone")


@dataclass(frozen=True, slots=True)
class Roster:
    members: tuple[Member, ...]
    by_aci: dict[str, Member]
    by_name: dict[str, Member]

    def __len__(self) -> int:
        return len(self.members)


def _parse_aci(value: str, row_number: int) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise RosterError(f"roster row {row_number}: signal_aci is not a valid UUID") from exc


def load_roster(path: str | Path) -> Roster:
    roster_path = Path(path)
    try:
        handle = roster_path.open("r", encoding="utf-8", newline="")
    except (FileNotFoundError, OSError) as exc:
        raise RosterError(f"cannot open roster file {roster_path}: {exc}") from exc

    members: list[Member] = []
    by_aci: dict[str, Member] = {}
    by_name: dict[str, Member] = {}
    try:
        with handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise RosterError("roster is empty or has no CSV header")
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise RosterError("roster CSV header contains duplicate fields")
            missing_fields = [field for field in _FIELDS if field not in reader.fieldnames]
            if missing_fields:
                raise RosterError(f"roster is missing fields: {', '.join(missing_fields)}")

            for row_number, row in enumerate(reader, start=2):
                values: dict[str, str] = {}
                for field in _FIELDS:
                    raw = row.get(field)
                    value = raw.strip() if isinstance(raw, str) else ""
                    if not value:
                        raise RosterError(f"roster row {row_number}: {field} is required")
                    values[field] = value

                aci = _parse_aci(values["signal_aci"], row_number)
                name = values["display_name"]
                phone = values["phone"]
                if not _PHONE_RE.fullmatch(phone):
                    raise RosterError(
                        f"roster row {row_number}: phone must be an E.164 number"
                    )
                if aci in by_aci:
                    raise RosterError(f"roster row {row_number}: duplicate signal_aci")
                if name in by_name:
                    raise RosterError(
                        f"roster row {row_number}: duplicate display_name {name!r}"
                    )

                member = Member(aci, name, phone)
                members.append(member)
                by_aci[aci] = member
                by_name[name] = member
    except (csv.Error, UnicodeError) as exc:
        raise RosterError(f"cannot parse roster file {roster_path}: {exc}") from exc

    return Roster(tuple(members), by_aci, by_name)


def load_released_members(path: str | Path, roster: Roster) -> tuple[Member, ...]:
    released_path = Path(path)
    try:
        lines = released_path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        raise RosterError(f"cannot read released list {released_path}: {exc}") from exc

    return released_members_from_names(lines, roster)


def released_members_from_names(
    names: list[str] | tuple[str, ...], roster: Roster
) -> tuple[Member, ...]:
    """Resolve exact display names, retaining released-list validation semantics."""

    members: list[Member] = []
    seen: set[str] = set()
    unknown: list[str] = []
    duplicates: list[str] = []
    for raw_line in names:
        name = raw_line.strip()
        if not name:
            continue
        member = roster.by_name.get(name)
        if member is None:
            unknown.append(name)
            continue
        if name in seen:
            duplicates.append(name)
            continue
        seen.add(name)
        members.append(member)

    problems: list[str] = []
    if unknown:
        problems.append("unknown display_name(s): " + ", ".join(repr(name) for name in unknown))
    if duplicates:
        problems.append(
            "duplicate display_name(s) in released list: "
            + ", ".join(repr(name) for name in duplicates)
        )
    if problems:
        raise RosterError("; ".join(problems))
    return tuple(members)
