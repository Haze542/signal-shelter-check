from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .config import Config
from .database import StateDatabase
from .models import MessageEvent, NormalizedEvent
from .released_list import ReleasedListError, ReleasedListService
from .roster import Roster
from .signal_client import SignalClient


LOGGER = logging.getLogger(__name__)

HELP_TEXT = """Доступні команди:

/setrt — повністю встановити список звільнених
/getrt — показати список
/addrt — додати людей
/delrt — видалити людей
/clearrt confirm — очистити список
/status — стан системи
/help — довідка"""

_SUPPORTED_COMMANDS = {
    "/setrt",
    "/getrt",
    "/addrt",
    "/delrt",
    "/clearrt",
    "/status",
    "/help",
}


class CommandReplyPublisher(Protocol):
    async def send(self, group_id: str, message: str) -> None: ...


class SignalCommandReplyPublisher:
    def __init__(self, client: SignalClient) -> None:
        self._client = client

    async def send(self, group_id: str, message: str) -> None:
        await self._client.send_group_message(group_id, message)


class DryRunCommandReplyPublisher:
    async def send(self, group_id: str, message: str) -> None:
        print(
            f"\n[DRY RUN] Would send command reply to {group_id}:\n{message}\n",
            flush=True,
        )


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    token: str
    arguments: tuple[str, ...]
    names: tuple[str, ...]


class CommandHandler:
    """Authorize, parse, and execute the deliberately small Signal command set."""

    def __init__(
        self,
        config: Config,
        roster: Roster,
        released_list: ReleasedListService,
        publisher: CommandReplyPublisher,
        database: StateDatabase,
        *,
        signal_health_check: Callable[[], Awaitable[bool]],
    ) -> None:
        self.config = config
        self.roster = roster
        self.released_list = released_list
        self.publisher = publisher
        self.database = database
        self._signal_health_check = signal_health_check

    async def handle_event(self, event: NormalizedEvent) -> bool:
        """Return true for slash messages so they never reach alert trigger logic."""

        if not isinstance(event, MessageEvent) or not event.text.startswith("/"):
            return False

        if (
            not self.config.command_control_enabled
            or event.group_id != self.config.command_group_id
            or event.sender_aci not in self.config.command_author_uuids
        ):
            return True

        command = _parse_command(event.text)
        if command.token not in _SUPPORTED_COMMANDS:
            return True

        LOGGER.info(
            "Authorized Signal command %s from ACI %s",
            command.token,
            event.sender_aci,
        )
        try:
            response = await self._execute(command)
        except ReleasedListError as exc:
            LOGGER.error("Signal command %s failed: %s", command.token, exc)
            response = "❌ Не вдалося оновити список.\n\nПоточний список залишився без змін."

        try:
            await self.publisher.send(self.config.command_group_id, response)
        except Exception as exc:
            LOGGER.error(
                "Could not send reply for Signal command %s: %s",
                command.token,
                exc,
            )
        return True

    async def _execute(self, command: ParsedCommand) -> str:
        if command.token == "/setrt":
            return await self._set(command)
        if command.token == "/getrt":
            if command.arguments or command.names:
                return _invalid_format("/getrt")
            return _format_current(await self.released_list.get())
        if command.token == "/addrt":
            return await self._add(command)
        if command.token == "/delrt":
            return await self._delete(command)
        if command.token == "/clearrt":
            return await self._clear(command)
        if command.token == "/status":
            if command.arguments or command.names:
                return _invalid_format("/status")
            return await self._status()
        if command.arguments or command.names:
            return _invalid_format("/help")
        return HELP_TEXT

    async def _set(self, command: ParsedCommand) -> str:
        if command.arguments:
            return _invalid_format("/setrt")
        problems = _validate_names(command.names, self.roster, require_names=True)
        if problems:
            return _validation_failure(problems)
        total = await self.released_list.replace(command.names)
        LOGGER.info("Signal command /setrt succeeded with %d entries", total)
        return f"✅ Список звільнених оновлено.\n\nВсього: {total}"

    async def _add(self, command: ParsedCommand) -> str:
        if command.arguments:
            return _invalid_format("/addrt")
        problems = _validate_names(command.names, self.roster, require_names=True)
        if problems:
            return _validation_failure(problems)
        result = await self.released_list.add(command.names)
        if result.added == 0:
            LOGGER.info("Signal command /addrt made no changes")
            return (
                "ℹ️ Змін немає.\n\n"
                "Усі вказані люди вже є у списку.\n\n"
                f"Всього звільнених: {result.total}"
            )
        LOGGER.info("Signal command /addrt added %d entries", result.added)
        return (
            "✅ Список оновлено.\n\n"
            f"Додано: {result.added}\n"
            f"Вже були у списку: {result.already_present}\n"
            f"Всього звільнених: {result.total}"
        )

    async def _delete(self, command: ParsedCommand) -> str:
        if command.arguments:
            return _invalid_format("/delrt")
        problems = _validate_names(
            command.names, self.roster, require_names=True, check_roster=False
        )
        if problems:
            return _validation_failure(problems)
        result = await self.released_list.remove(command.names)
        if result.removed == 0:
            LOGGER.info("Signal command /delrt made no changes")
            return (
                "ℹ️ Змін немає.\n\n"
                "Жодної з указаних людей не було у списку.\n\n"
                f"Всього звільнених: {result.total}"
            )
        LOGGER.info("Signal command /delrt removed %d entries", result.removed)
        return (
            "✅ Список оновлено.\n\n"
            f"Видалено: {result.removed}\n"
            f"Не було у списку: {result.absent}\n"
            f"Всього звільнених: {result.total}"
        )

    async def _clear(self, command: ParsedCommand) -> str:
        confirmed = (
            len(command.arguments) == 1
            and command.arguments[0].casefold() == "confirm"
            and not command.names
        )
        if not confirmed:
            return "⚠️ Для очищення списку надішліть:\n\n/clearrt confirm"
        await self.released_list.clear()
        LOGGER.info("Signal command /clearrt succeeded")
        return "✅ Список звільнених очищено.\n\nВсього: 0"

    async def _status(self) -> str:
        try:
            signal_connected = await self._signal_health_check()
        except Exception as exc:
            signal_connected = False
            LOGGER.warning("Signal health check for /status failed: %s", exc)

        released_count = len(await self.released_list.get())
        lines = [
            "ShelterCheck: 🟢 працює",
            "",
            f"Signal: {'🟢 connected' if signal_connected else '🔴 disconnected'}",
            f"Roster: {len(self.roster)}",
            f"Released today: {released_count}",
            f"Active checks: {self.database.count_active_alarms()}",
        ]
        last_check = self.database.last_check_timestamp_ms()
        if last_check is not None:
            check_time = datetime.fromtimestamp(last_check / 1000).astimezone()
            lines.append(f"Last check: {check_time:%H:%M}")
        return "\n".join(lines)


def _parse_command(text: str) -> ParsedCommand:
    first_line, *payload_lines = text.splitlines()
    parts = first_line.strip().split()
    token = parts[0].casefold() if parts else ""
    names = tuple(line.strip() for line in payload_lines if line.strip())
    return ParsedCommand(token, tuple(parts[1:]), names)


def _validate_names(
    names: tuple[str, ...],
    roster: Roster,
    *,
    require_names: bool,
    check_roster: bool = True,
) -> list[tuple[str, tuple[str, ...]]]:
    if require_names and not names:
        return [("Не вказано жодного імені.", ())]

    seen: set[str] = set()
    duplicate_seen: set[str] = set()
    duplicates: list[str] = []
    unknown_seen: set[str] = set()
    unknown: list[str] = []
    for name in names:
        if name in seen and name not in duplicate_seen:
            duplicate_seen.add(name)
            duplicates.append(name)
        seen.add(name)
        if check_roster and name not in roster.by_name and name not in unknown_seen:
            unknown_seen.add(name)
            unknown.append(name)

    problems: list[tuple[str, tuple[str, ...]]] = []
    if unknown:
        problems.append(("Не знайдено в roster.csv:", tuple(unknown)))
    if duplicates:
        problems.append(("Дублікати:", tuple(duplicates)))
    return problems


def _validation_failure(problems: list[tuple[str, tuple[str, ...]]]) -> str:
    sections = ["❌ Список не змінено."]
    for title, names in problems:
        section = title
        if names:
            section += "\n\n" + "\n".join(names)
        sections.append(section)
    sections.append("Поточний список залишився без змін.")
    return "\n\n".join(sections)


def _invalid_format(command: str) -> str:
    return f"❌ Невірний формат команди.\n\nПеревірте /help і повторіть {command}."


def _format_current(names: tuple[str, ...]) -> str:
    if not names:
        return "Звільнені сьогодні — 0.\n\nСписок порожній."
    numbered = "\n".join(f"{position}. {name}" for position, name in enumerate(names, 1))
    return f"Звільнені сьогодні — {len(names)}:\n\n{numbered}"
