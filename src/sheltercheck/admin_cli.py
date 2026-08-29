from __future__ import annotations

from pathlib import Path

from .config import Config
from .released_list import ReleasedListError, ReleasedListService
from .roster import RosterError, load_roster, released_members_from_names


async def run_released_command(
    config: Config, action: str, source: Path | None = None
) -> None:
    roster = load_roster(config.roster_file)
    service = ReleasedListService(config.released_file)

    if action in {"show", "validate"}:
        members = await service.get_members(roster)
        if action == "validate":
            print("Released list: valid")
            print(f"Released today: {len(members)} members")
            return

        print(f"Released today: {len(members)}")
        if members:
            print()
            for member in members:
                print(member.display_name)
        return

    if action != "import" or source is None:
        raise ValueError(f"unsupported released command: {action}")

    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleasedListError(f"cannot read import file {source}: {exc}") from exc
    names = tuple(line.strip() for line in lines if line.strip())
    try:
        released_members_from_names(names, roster)
    except RosterError:
        raise
    await service.replace(names)
    print("Released list imported successfully.")
    print(f"Released today: {len(names)} members")
