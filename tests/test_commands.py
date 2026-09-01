from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace

from sheltercheck.commands import CommandHandler, HELP_TEXT
from sheltercheck.database import StateDatabase
from sheltercheck.models import MessageEvent
from sheltercheck.released_list import ReleasedListService
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1, ACI_2, ACI_3, AUTHOR
from test_tracker import Clock, FakePublisher, TRIGGER_TS, trigger


class FakeCommandPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, group_id: str, message: str) -> None:
        self.messages.append((group_id, message))


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


COMMAND_TIMESTAMPS = itertools.count(TRIGGER_TS)


def command(
    text: str,
    *,
    sender: str = AUTHOR,
    group: str = "command-group",
    timestamp: int | None = None,
) -> MessageEvent:
    return MessageEvent(
        group,
        sender,
        next(COMMAND_TIMESTAMPS) if timestamp is None else timestamp,
        text,
    )


def build_handler(app_config, roster):
    database = StateDatabase(":memory:")
    service = ReleasedListService(app_config.released_file)
    publisher = FakeCommandPublisher()

    async def healthy() -> bool:
        return True

    handler = CommandHandler(
        app_config,
        roster,
        service,
        publisher,
        database,
        signal_health_check=healthy,
    )
    return database, service, publisher, handler


def reply(publisher: FakeCommandPublisher) -> str:
    return publisher.messages[-1][1]


def test_authorized_setrt_replaces_list_case_insensitively(app_config, roster) -> None:
    database, service, publisher, handler = build_handler(app_config, roster)
    try:
        handled = run(handler.handle_event(command("/SETRT\n  Петренко П.П  \n\nІваненко І.І")))
        assert handled is True
        assert run(service.get()) == ("Петренко П.П", "Іваненко І.І")
        assert publisher.messages[-1][0] == "command-group"
        assert reply(publisher) == "✅ Список звільнених оновлено.\n\nВсього: 2"
    finally:
        database.close()


def test_setrt_unknown_name_does_not_change_file(app_config, roster) -> None:
    before = app_config.released_file.read_bytes()
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/setrt\nНевідомий Н.Н")))
        assert app_config.released_file.read_bytes() == before
        assert "Не знайдено в roster.csv:\n\nНевідомий Н.Н" in reply(publisher)
        assert reply(publisher).endswith("Поточний список залишився без змін.")
    finally:
        database.close()


def test_setrt_reports_unknown_and_duplicate_without_writing(app_config, roster) -> None:
    before = app_config.released_file.read_bytes()
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        run(
            handler.handle_event(
                command("/setrt\nНевідомий Н.Н\nІваненко І.І\nІваненко І.І")
            )
        )
        response = reply(publisher)
        assert app_config.released_file.read_bytes() == before
        assert "Не знайдено в roster.csv:\n\nНевідомий Н.Н" in response
        assert "Дублікати:\n\nІваненко І.І" in response
    finally:
        database.close()


def test_setrt_without_names_is_not_an_implicit_clear(app_config, roster) -> None:
    before = app_config.released_file.read_bytes()
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/setrt\n \n")))
        assert app_config.released_file.read_bytes() == before
        assert "Не вказано жодного імені." in reply(publisher)
    finally:
        database.close()


def test_getrt_formats_current_and_empty_lists(app_config, roster) -> None:
    database, service, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/GETRT")))
        assert reply(publisher) == (
            "Звільнені сьогодні — 3:\n\n"
            "1. Іваненко І.І\n2. Петренко П.П\n3. Коваль А.А"
        )

        run(service.clear())
        run(handler.handle_event(command("/getrt")))
        assert reply(publisher) == "Звільнені сьогодні — 0.\n\nСписок порожній."
    finally:
        database.close()


def test_addrt_adds_new_and_does_not_duplicate_existing(app_config, roster) -> None:
    run(ReleasedListService(app_config.released_file).replace(("Іваненко І.І",)))
    database, service, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/addrt\nІваненко І.І\nПетренко П.П")))
        assert run(service.get()) == ("Іваненко І.І", "Петренко П.П")
        assert "Додано: 1" in reply(publisher)
        assert "Вже були у списку: 1" in reply(publisher)

        run(handler.handle_event(command("/addrt\nІваненко І.І\nПетренко П.П")))
        assert "ℹ️ Змін немає." in reply(publisher)
        assert "Усі вказані люди вже є у списку." in reply(publisher)
    finally:
        database.close()


def test_addrt_unknown_or_duplicate_does_not_partially_write(app_config, roster) -> None:
    before = app_config.released_file.read_bytes()
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        run(
            handler.handle_event(
                command("/addrt\nКоваль А.А\nНевідомий Н.Н\nКоваль А.А")
            )
        )
        assert app_config.released_file.read_bytes() == before
        assert "Невідомий Н.Н" in reply(publisher)
        assert "Дублікати:\n\nКоваль А.А" in reply(publisher)
    finally:
        database.close()


def test_delrt_distinguishes_removed_and_absent_names(app_config, roster) -> None:
    database, service, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/delrt\nПетренко П.П\nВже Не В Roster")))
        assert run(service.get()) == ("Іваненко І.І", "Коваль А.А")
        assert "Видалено: 1" in reply(publisher)
        assert "Не було у списку: 1" in reply(publisher)

        run(handler.handle_event(command("/delrt\nВже Не В Roster")))
        assert "ℹ️ Змін немає." in reply(publisher)
        assert "Жодної з указаних людей не було у списку." in reply(publisher)
    finally:
        database.close()


def test_clearrt_requires_exact_confirmation(app_config, roster) -> None:
    database, service, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/clearrt")))
        assert len(run(service.get())) == 3
        assert reply(publisher) == "⚠️ Для очищення списку надішліть:\n\n/clearrt confirm"

        run(handler.handle_event(command("/CLEARRT CoNfIrM")))
        assert run(service.get()) == ()
        assert reply(publisher) == "✅ Список звільнених очищено.\n\nВсього: 0"
    finally:
        database.close()


def test_unauthorized_sender_and_wrong_group_are_silently_ignored(
    app_config, roster
) -> None:
    before = app_config.released_file.read_bytes()
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        assert run(handler.handle_event(command("/clearrt confirm", sender=ACI_1))) is True
        assert run(handler.handle_event(command("/clearrt confirm", group="wrong"))) is True
        assert publisher.messages == []
        assert app_config.released_file.read_bytes() == before
    finally:
        database.close()


def test_empty_command_allowlist_disables_commands(app_config, roster) -> None:
    disabled = replace(app_config, command_author_uuids=frozenset())
    database, _, publisher, handler = build_handler(disabled, roster)
    try:
        assert run(handler.handle_event(command("/clearrt confirm"))) is True
        assert publisher.messages == []
        assert app_config.released_file.read_text(encoding="utf-8")
    finally:
        database.close()


def test_normal_message_is_not_a_command(app_config, roster) -> None:
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        assert run(handler.handle_event(command("звичайне повідомлення"))) is False
        assert publisher.messages == []
    finally:
        database.close()


def test_command_message_cannot_create_alert_session(app_config, roster) -> None:
    same_group = replace(
        app_config,
        monitor_group_id="command-group",
        trigger_texts=frozenset({"/getrt"}),
    )
    database, service, _, handler = build_handler(same_group, roster)
    tracker = AlertTracker(
        same_group,
        roster,
        database,
        FakePublisher(),
        released_list=service,
        clock_ms=Clock(TRIGGER_TS),
    )
    try:
        event = command("/getrt")

        async def dispatch() -> None:
            if not await handler.handle_event(event):
                await tracker.handle_event(event)

        run(dispatch())
        assert database.list_alarms() == ()
    finally:
        database.close()


def test_status_returns_available_live_values(app_config, roster) -> None:
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        database.create_alarm(
            group_id="monitor-group",
            trigger_author_aci=AUTHOR,
            trigger_timestamp_ms=TRIGGER_TS,
            deadline_timestamp_ms=TRIGGER_TS + 10_000,
            released_members=roster.members,
            created_at_ms=TRIGGER_TS,
        )
        run(handler.handle_event(command("/status")))
        response = reply(publisher)
        assert "ShelterCheck: 🟢 працює" in response
        assert "Signal: 🟢 connected" in response
        assert "Roster: 3" in response
        assert "Released today: 3" in response
        assert "Active checks: 1" in response
        assert "Last check:" in response
    finally:
        database.close()


def test_help_contains_every_supported_command(app_config, roster) -> None:
    database, _, publisher, handler = build_handler(app_config, roster)
    try:
        run(handler.handle_event(command("/help")))
        assert reply(publisher) == HELP_TEXT
        for token in ("/setrt", "/getrt", "/addrt", "/delrt", "/clearrt confirm", "/status", "/help"):
            assert token in reply(publisher)
    finally:
        database.close()


def test_released_change_does_not_mutate_active_snapshot(app_config, roster) -> None:
    database, service, _, handler = build_handler(app_config, roster)
    tracker = AlertTracker(
        app_config,
        roster,
        database,
        FakePublisher(),
        released_list=service,
        clock_ms=Clock(TRIGGER_TS),
    )
    try:
        run(tracker.handle_event(trigger()))
        run(handler.handle_event(command("/setrt\nІваненко І.І")))
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS + 1)))
        first, second = database.list_alarms()
        assert [member.signal_aci for member in first.released_members] == [ACI_1, ACI_2, ACI_3]
        assert [member.signal_aci for member in second.released_members] == [ACI_1]
    finally:
        database.close()
