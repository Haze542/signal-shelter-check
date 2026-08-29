from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import ConfigError, load_config
from .released_list import ReleasedListError, ReleasedListService
from .roster import RosterError, load_roster
from .signal_client import SignalClientError, signal_client_from_config


async def check_signal_daemon(config) -> None:
    async with signal_client_from_config(config) as client:
        await client.check_health()


def check_sqlite(path: Path) -> None:
    if not path.is_file():
        raise sqlite3.OperationalError(f"database file does not exist: {path}")
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=rw", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise sqlite3.DatabaseError(
                f"SQLite quick_check returned {result[0] if result else 'no result'}"
            )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"alarms", "released_members", "reaction_state"}
        missing = sorted(required - tables)
        if missing:
            raise sqlite3.DatabaseError(
                "missing ShelterCheck table(s): " + ", ".join(missing)
            )
    finally:
        connection.close()


async def run_health(config_path: str | Path) -> int:
    print("Sheltercheck health\n")
    try:
        config = load_config(config_path)
    except (ConfigError, OSError) as exc:
        _failed("Application config", exc)
        print("\nSTATUS: FAILED")
        return 1

    print("Application config: OK")
    failed = False

    try:
        await check_signal_daemon(config)
    except (SignalClientError, OSError) as exc:
        _failed("Signal daemon", exc)
        failed = True
    else:
        print("Signal daemon: OK")

    try:
        roster = load_roster(config.roster_file)
    except (RosterError, OSError) as exc:
        _failed("Roster", exc)
        roster = None
        failed = True
    else:
        print(f"Roster: {len(roster)} members")
        if len(roster) == 0:
            print("Reason: roster has no members")
            failed = True

    if roster is not None:
        try:
            released = await ReleasedListService(config.released_file).get_members(roster)
        except (ReleasedListError, RosterError, OSError) as exc:
            _failed("Released today", exc)
            failed = True
        else:
            print(f"Released today: {len(released)} members")

    try:
        check_sqlite(config.state_db)
    except (sqlite3.Error, OSError) as exc:
        _failed("SQLite", exc)
        failed = True
    else:
        print("SQLite: OK")

    print("Monitor group: configured")
    print("Report group: configured")
    print(f"\nSTATUS: {'FAILED' if failed else 'OK'}")
    return 1 if failed else 0


def _failed(component: str, reason: Exception) -> None:
    print(f"{component}: FAILED")
    print(f"Reason: {reason}")
