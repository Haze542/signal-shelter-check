from __future__ import annotations

import asyncio

import pytest

from sheltercheck.commands import CommandHandler, HELP_TEXT
from sheltercheck.database import StateDatabase
from sheltercheck.models import MessageEvent
from sheltercheck.released_list import ReleasedListService
from sheltercheck.signal_client import SignalRPCError
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1, ACI_2, AUTHOR
from test_commands import FakeCommandPublisher, command, reply
from test_tracker import Clock, FakePublisher, TRIGGER_TS, reaction, trigger


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


class MonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FailingCommandPublisher:
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, group_id: str, message: str) -> None:
        self.attempts += 1
        raise SignalRPCError(-1, "reply rejected")


def build_integrated(app_config, roster):
    database = StateDatabase(":memory:")
    service = ReleasedListService(app_config.released_file)
    report_publisher = FakePublisher()
    wall = Clock(TRIGGER_TS)
    tracker = AlertTracker(
        app_config,
        roster,
        database,
        report_publisher,
        released_list=service,
        clock_ms=wall,
    )
    command_publisher = FakeCommandPublisher()
    monotonic = MonotonicClock()

    async def healthy() -> bool:
        return True

    handler = CommandHandler(
        app_config,
        roster,
        service,
        command_publisher,
        database,
        signal_health_check=healthy,
        tracker=tracker,
        clock_ms=wall,
        monotonic_clock=monotonic,
    )
    return (
        database,
        service,
        report_publisher,
        command_publisher,
        tracker,
        handler,
        wall,
        monotonic,
    )


def test_check_without_alarm_returns_clear_reply(app_config, roster) -> None:
    database, _, reports, commands, _, handler, _, _ = build_integrated(
        app_config, roster
    )
    try:
        run(handler.handle_event(command("/check")))
        assert reply(commands) == "Контрольних повідомлень ще не зафіксовано."
        assert reports.sends == []
    finally:
        database.close()


def test_check_command_delegates_standard_and_custom_modes(app_config, roster) -> None:
    database, _, reports, commands, tracker, handler, wall, _ = build_integrated(
        app_config, roster
    )
    try:
        run(tracker.handle_event(trigger()))
        wall.value += 1_000
        run(handler.handle_event(command("/check")))
        assert "Перевірку виконано" in reply(commands)
        assert len(reports.sends) == 1

        custom_timestamp = TRIGGER_TS + 2_000
        run(
            tracker.handle_event(
                MessageEvent(
                    "monitor-group",
                    AUTHOR,
                    custom_timestamp,
                    "Всі на зв'язку??",
                )
            )
        )
        wall.value += 1_000
        run(handler.handle_event(command("/check   Всі   на зв'язку??")))
        assert "Перевірку розпочато" in reply(commands)
        custom_alarm = database.get_alarm_by_identity(
            "monitor-group", AUTHOR, custom_timestamp
        )
        assert custom_alarm is not None
        assert custom_alarm.source == "custom"
        assert len(reports.sends) == 2
    finally:
        database.close()


def test_check_not_found_reply_does_not_claim_server_history(app_config, roster) -> None:
    database, _, reports, commands, _, handler, _, _ = build_integrated(
        app_config, roster
    )
    try:
        run(handler.handle_event(command("/check Неіснуюче повідомлення")))
        assert "Повідомлення для перевірки не знайдено" in reply(commands)
        assert "лише повідомлення, які він уже отримав" in reply(commands)
        assert database.list_alarms() == ()
        assert reports.sends == []
    finally:
        database.close()


def test_disallowed_message_author_does_not_gain_check_command_permission(
    app_config, roster
) -> None:
    database, _, reports, commands, tracker, handler, wall, _ = build_integrated(
        app_config, roster
    )
    try:
        message_timestamp = TRIGGER_TS + 5_000
        run(
            tracker.handle_event(
                MessageEvent(
                    "monitor-group",
                    ACI_2,
                    message_timestamp,
                    "Всі в укритті?",
                )
            )
        )
        wall.value = message_timestamp + 1

        assert run(
            handler.handle_event(
                MessageEvent(
                    "command-group",
                    ACI_2,
                    message_timestamp + 1,
                    "/check Всі в укритті?",
                )
            )
        ) is True
        assert commands.messages == []
        assert database.list_alarms() == ()

        run(handler.handle_event(command("/check Всі в укритті?")))
        assert "Перевірку розпочато" in reply(commands)
        assert len(reports.sends) == 1
        assert database.list_alarms()[0].trigger_author_aci == ACI_2
    finally:
        database.close()


def test_status_without_active_check_and_monotonic_uptime(app_config, roster) -> None:
    database, _, _, commands, _, handler, _, monotonic = build_integrated(
        app_config, roster
    )
    try:
        monotonic.value = 22_800
        run(handler.handle_event(command("/status")))
        response = reply(commands)
        assert "ShelterCheck: 🟢 працює" in response
        assert "Signal: 🟢 connected" in response
        assert "Roster: 3" in response
        assert "Released today: 3" in response
        assert "Active checks: 0" in response
        assert "Last check: немає" in response
        assert "Up time: 6 год 20 хв" in response
        assert response.endswith("Перевірка: неактивна")
        assert "Контрольне повідомлення:" not in response
    finally:
        database.close()


def test_status_reports_signal_disconnected_when_semantic_health_fails(
    app_config, roster
) -> None:
    database = StateDatabase(":memory:")
    service = ReleasedListService(app_config.released_file)
    publisher = FakeCommandPublisher()

    async def not_ready() -> bool:
        return False

    handler = CommandHandler(
        app_config,
        roster,
        service,
        publisher,
        database,
        signal_health_check=not_ready,
    )
    try:
        run(handler.handle_event(command("/status")))
        assert "Signal: 🔴 disconnected" in reply(publisher)
    finally:
        database.close()


def test_status_active_detail_uses_snapshot_but_top_uses_current_list(
    app_config, roster
) -> None:
    database, service, _, commands, tracker, handler, wall, monotonic = build_integrated(
        app_config, roster
    )
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1_000)))
        run(service.replace(("Іваненко І.І",)))
        wall.value = TRIGGER_TS + 6_012
        monotonic.value = 35
        run(handler.handle_event(command("/status")))
        response = reply(commands)
        assert "Released today: 1" in response
        assert "Active checks: 1" in response
        assert "Up time: 35 с" in response
        assert "Перевірка: активна" in response
        assert "Контрольне повідомлення:" in response
        assert "Минуло: 6 с" in response
        assert "Відмітилися: 1/3" in response
        assert "Очікуються: 2" in response
    finally:
        database.close()


def test_status_multiple_active_shows_latest_detail_only(app_config, roster) -> None:
    database, _, _, commands, tracker, handler, wall, _ = build_integrated(
        app_config, roster
    )
    try:
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS)))
        second = TRIGGER_TS + 2_000
        wall.value = second
        run(tracker.handle_event(trigger(timestamp=second)))
        run(
            tracker.handle_event(
                reaction(ACI_1, target=second, sent=second + 1)
            )
        )
        wall.value = second + 2_000
        run(handler.handle_event(command("/status")))
        response = reply(commands)
        assert "Active checks: 2" in response
        assert "Відмітилися: 1/3" in response
        assert response.count("Перевірка: активна") == 1
    finally:
        database.close()


@pytest.mark.parametrize(
    "seconds, expected",
    [(35, "35 с"), (420, "7 хв"), (22_800, "6 год 20 хв"), (97_920, "1 д 3 год 12 хв")],
)
def test_uptime_formats_without_sleep(app_config, roster, seconds, expected) -> None:
    database, _, _, commands, _, handler, _, monotonic = build_integrated(
        app_config, roster
    )
    try:
        monotonic.value = seconds
        run(handler.handle_event(command("/status")))
        assert f"Up time: {expected}" in reply(commands)
    finally:
        database.close()


def test_new_handler_resets_process_uptime(app_config, roster) -> None:
    database, service, _, _, tracker, _, wall, monotonic = build_integrated(
        app_config, roster
    )
    try:
        monotonic.value = 10_000
        publisher = FakeCommandPublisher()

        async def healthy() -> bool:
            return True

        restarted = CommandHandler(
            app_config,
            roster,
            service,
            publisher,
            database,
            signal_health_check=healthy,
            tracker=tracker,
            clock_ms=wall,
            monotonic_clock=monotonic,
        )
        monotonic.value += 35
        run(restarted.handle_event(command("/status")))
        assert "Up time: 35 с" in reply(publisher)
    finally:
        database.close()


def test_command_reply_failure_is_attempted_once(
    app_config, roster, tmp_path
) -> None:
    path = tmp_path / "command-reply.sqlite3"
    database = StateDatabase(path)
    service = ReleasedListService(app_config.released_file)
    publisher = FailingCommandPublisher()

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
    event = command("/help", timestamp=9_999_999)
    try:
        run(handler.handle_event(event))
        run(handler.handle_event(event))
        assert publisher.attempts == 1
        operation = database.get_outgoing(
            f"command_reply:command-group:{AUTHOR}:9999999"
        )
        assert operation is not None
        assert operation.state == "attempted_failed"
    finally:
        database.close()

    database2 = StateDatabase(path)
    publisher2 = FailingCommandPublisher()
    handler2 = CommandHandler(
        app_config,
        roster,
        service,
        publisher2,
        database2,
        signal_health_check=healthy,
    )
    try:
        run(handler2.handle_event(event))
        assert publisher2.attempts == 0
    finally:
        database2.close()


def test_help_lists_check_and_never_alertst() -> None:
    assert "/check" in HELP_TEXT
    assert "/check <текст>" in HELP_TEXT
    assert "/alertst" not in HELP_TEXT
