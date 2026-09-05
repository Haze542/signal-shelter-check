from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sqlite3
import sys
import time
from pathlib import Path

from .admin_cli import run_released_command
from .commands import (
    CommandHandler,
    DryRunCommandReplyPublisher,
    SignalCommandReplyPublisher,
)
from .config import Config, ConfigError, load_config
from .database import DatabaseError, StateDatabase
from .event_parser import parse_receive_notification
from .health import run_health
from .reporter import DryRunReportPublisher, SignalReportPublisher
from .released_list import ReleasedListError, ReleasedListService
from .roster import RosterError, load_released_members, load_roster
from .signal_client import SignalClient, SignalClientError, signal_client_from_config
from .tracker import AlertTracker


LOGGER = logging.getLogger(__name__)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track Signal shelter-check reactions")
    parser.add_argument("--config", default="config.toml", help="path to TOML configuration")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="consume real events but print reports instead of sending them",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="check config, Signal daemon, roster, released list, and SQLite",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="validate configuration, roster, and current released list, then exit",
    )
    commands = parser.add_subparsers(dest="command")
    released = commands.add_parser("released", help="administer released_today.txt")
    released_commands = released.add_subparsers(dest="released_action", required=True)
    released_commands.add_parser("show", help="show the current released list")
    released_commands.add_parser("validate", help="validate the current released list")
    released_import = released_commands.add_parser(
        "import", help="atomically import and validate a new released list"
    )
    released_import.add_argument("source", type=Path, help="UTF-8 file to import")
    args = parser.parse_args(argv)
    selected_modes = sum(
        bool(value)
        for value in (args.dry_run, args.health, args.validate_config, args.command)
    )
    if selected_modes > 1:
        parser.error("choose only one of --dry-run, --health, --validate-config, or a command")
    return args


def _client(config: Config) -> SignalClient:
    return signal_client_from_config(config)


async def _ticker(tracker: AlertTracker) -> None:
    while True:
        try:
            await tracker.process_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Unexpected error while processing report deadlines")
        await asyncio.sleep(0.25)


async def _run(
    config: Config,
    *,
    dry_run: bool,
    process_started_monotonic: float | None = None,
    service_started_at_ms: int | None = None,
) -> None:
    if process_started_monotonic is None:
        process_started_monotonic = time.monotonic()
    if service_started_at_ms is None:
        service_started_at_ms = time.time_ns() // 1_000_000
    roster = load_roster(config.roster_file)
    database = StateDatabase(":memory:" if dry_run else config.state_db)
    released_list = ReleasedListService(config.released_file, in_memory=dry_run)
    try:
        async with _client(config) as client:
            await client.check_health()
            publisher = (
                DryRunReportPublisher()
                if dry_run
                else SignalReportPublisher(client, config.report_group_id)
            )
            tracker = AlertTracker(
                config,
                roster,
                database,
                publisher,
                released_list=released_list,
                service_started_at_ms=service_started_at_ms,
            )

            async def signal_health_check() -> bool:
                await client.check_health()
                return True

            command_publisher = (
                DryRunCommandReplyPublisher()
                if dry_run
                else SignalCommandReplyPublisher(client)
            )
            commands = CommandHandler(
                config,
                roster,
                released_list,
                command_publisher,
                database,
                signal_health_check=signal_health_check,
                tracker=tracker,
                process_started_monotonic=process_started_monotonic,
                service_started_at_ms=service_started_at_ms,
            )

            print(f"Roster: {len(roster)} members")
            print("Signal daemon: ready (1 account loaded)")
            print("Monitor group: configured")
            print("Report group: configured")
            if not config.trigger_author_uuids:
                LOGGER.warning(
                    "trigger_author_uuids is empty; any sender in the monitor group can create an alert"
                )
            if dry_run:
                print("Mode: dry-run (state is in memory; Signal sends are disabled)")
            uncertain = database.list_uncertain_outgoing_operations()
            if uncertain:
                LOGGER.critical(
                    "%d outgoing operation(s) have uncertain delivery and will not be retried automatically; inspect local state and Signal",
                    len(uncertain),
                )
            print("Waiting for trigger...", flush=True)

            await tracker.process_due()
            ticker = asyncio.create_task(_ticker(tracker), name="report-deadline-ticker")
            try:
                async for raw_event in client.events():
                    for event in parse_receive_notification(raw_event):
                        if await commands.handle_event(event):
                            continue
                        await tracker.handle_event(event)
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    service_started_at_ms = time.time_ns() // 1_000_000
    process_started_monotonic = time.monotonic()
    args = _arguments(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.health:
            return asyncio.run(run_health(args.config))
        config = load_config(args.config)
        if args.command == "released":
            asyncio.run(
                run_released_command(
                    config,
                    args.released_action,
                    getattr(args, "source", None),
                )
            )
            return 0
        if not config.command_control_enabled:
            LOGGER.warning(
                "Signal command control disabled: command_author_uuids is empty"
            )
        if args.validate_config:
            roster = load_roster(config.roster_file)
            released = load_released_members(config.released_file, roster)
            print("Configuration: valid")
            print(f"Roster: {len(roster)} members")
            print(f"Released today: {len(released)} members")
            return 0
        asyncio.run(
            _run(
                config,
                dry_run=args.dry_run,
                process_started_monotonic=process_started_monotonic,
                service_started_at_ms=service_started_at_ms,
            )
        )
    except KeyboardInterrupt:
        return 130
    except (
        ConfigError,
        ReleasedListError,
        RosterError,
        SignalClientError,
        DatabaseError,
        sqlite3.Error,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
