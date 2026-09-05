from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sheltercheck.database import StateDatabase
from sheltercheck.models import MessageEvent, ReactionEvent
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1, ACI_2, AUTHOR
from test_tracker import Clock, FakePublisher


SERVICE_START_MS = 1_700_000_000_000
OLD_CHECK_MS = SERVICE_START_MS - 4 * 60 * 60 * 1000


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def message(
    timestamp_ms: int,
    text: str = "Всі в укритті?",
    *,
    author: str = AUTHOR,
) -> MessageEvent:
    return MessageEvent("monitor-group", author, timestamp_ms, text)


def reaction(target_timestamp_ms: int, sent_timestamp_ms: int) -> ReactionEvent:
    return ReactionEvent(
        "monitor-group",
        ACI_1,
        AUTHOR,
        target_timestamp_ms,
        "➕",
        False,
        sent_timestamp_ms,
    )


def build(
    app_config,
    roster,
    clock: Clock,
    *,
    service_started_at_ms: int = SERVICE_START_MS,
    path: str | Path = ":memory:",
):
    database = StateDatabase(path)
    publisher = FakePublisher()
    tracker = AlertTracker(
        app_config,
        roster,
        database,
        publisher,
        clock_ms=clock,
        service_started_at_ms=service_started_at_ms,
    )
    return database, publisher, tracker


def test_old_backlog_check_is_observed_but_not_created_or_reported(
    app_config, roster, caplog
) -> None:
    clock = Clock(SERVICE_START_MS)
    database, publisher, tracker = build(app_config, roster, clock)
    caplog.set_level(logging.INFO, logger="sheltercheck.tracker")
    try:
        run(tracker.handle_event(message(OLD_CHECK_MS)))
        run(tracker.process_due(SERVICE_START_MS + 60_000))

        assert database.list_alarms() == ()
        assert database.observed_message_count() == 1
        assert publisher.sends == []
        assert publisher.edits == []
        assert publisher.reactions == []
        assert "Ignoring pre-start check for automatic processing" in caplog.text
        assert f"message_timestamp_ms={OLD_CHECK_MS}" in caplog.text
        assert f"service_started_at_ms={SERVICE_START_MS}" in caplog.text
    finally:
        database.close()


def test_check_at_or_after_start_is_processed_normally(app_config, roster) -> None:
    clock = Clock(SERVICE_START_MS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(message(SERVICE_START_MS)))
        later = SERVICE_START_MS + 30 * 60 * 1000
        clock.value = later
        run(tracker.handle_event(message(later)))

        alarms = database.list_alarms()
        assert [alarm.trigger_timestamp_ms for alarm in alarms] == [
            SERVICE_START_MS,
            later,
        ]
        assert all(alarm.source == "standard" for alarm in alarms)
        assert len(publisher.reactions) == 2
    finally:
        database.close()


def test_manual_check_can_start_from_old_automatic_trigger(app_config, roster) -> None:
    clock = Clock(SERVICE_START_MS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(message(OLD_CHECK_MS)))
        run(
            tracker.handle_event(
                reaction(OLD_CHECK_MS, OLD_CHECK_MS + 60_000)
            )
        )
        assert database.list_alarms() == ()

        clock.value = SERVICE_START_MS + 10 * 60 * 1000
        result = run(tracker.force_check_latest())

        assert result.outcome == "evaluated"
        assert result.created is True
        assert result.alarm is not None
        assert result.alarm.source == "custom"
        assert result.alarm.trigger_timestamp_ms == OLD_CHECK_MS
        assert result.alarm.tracking_started_at_ms == clock.value
        assert result.alarm.responded_acis == frozenset({ACI_1})
        assert len(publisher.sends) == 1
    finally:
        database.close()


def test_manual_check_text_can_use_old_message_from_any_author(
    app_config, roster
) -> None:
    clock = Clock(SERVICE_START_MS)
    database, publisher, tracker = build(app_config, roster, clock)
    old_custom = SERVICE_START_MS - 2 * 60 * 60 * 1000
    try:
        run(
            tracker.handle_event(
                message(old_custom, "Стара нестандартна перевірка", author=ACI_2)
            )
        )
        clock.value += 1_000
        result = run(tracker.force_check_message("  СТАРА   нестандартна перевірка "))

        assert result.outcome == "evaluated"
        assert result.created is True
        assert result.alarm is not None
        assert result.alarm.trigger_author_aci == ACI_2
        assert result.alarm.trigger_timestamp_ms == old_custom
        assert result.alarm.tracking_started_at_ms == clock.value
        assert len(publisher.sends) == 1
    finally:
        database.close()


def test_restart_does_not_resume_or_mutate_old_persistent_alarm(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "restart-boundary.sqlite3"
    check_timestamp = SERVICE_START_MS - 20_000
    first_clock = Clock(check_timestamp)
    database, _, tracker = build(
        app_config,
        roster,
        first_clock,
        service_started_at_ms=check_timestamp - 1_000,
        path=path,
    )
    run(tracker.handle_event(message(check_timestamp)))
    database.close()

    restarted_clock = Clock(SERVICE_START_MS)
    database2, publisher2, tracker2 = build(
        app_config,
        roster,
        restarted_clock,
        path=path,
    )
    try:
        run(tracker2.handle_event(message(check_timestamp)))
        run(
            tracker2.handle_event(
                reaction(check_timestamp, SERVICE_START_MS + 1_000)
            )
        )
        run(tracker2.process_due(SERVICE_START_MS + 60_000))

        alarm = database2.list_alarms()[0]
        assert alarm.status == "pending"
        assert alarm.responded_acis == frozenset()
        assert database2.count_active_alarms(
            started_at_or_after_ms=SERVICE_START_MS
        ) == 0
        assert publisher2.sends == []
        assert publisher2.edits == []
        assert publisher2.reactions == []
    finally:
        database2.close()


def test_manual_check_can_evaluate_pre_restart_persistent_alarm(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "manual-after-restart.sqlite3"
    check_timestamp = SERVICE_START_MS - 20_000
    first_clock = Clock(check_timestamp)
    database, _, tracker = build(
        app_config,
        roster,
        first_clock,
        service_started_at_ms=check_timestamp,
        path=path,
    )
    run(tracker.handle_event(message(check_timestamp)))
    database.close()

    restarted_clock = Clock(SERVICE_START_MS)
    database2, publisher2, tracker2 = build(
        app_config,
        roster,
        restarted_clock,
        path=path,
    )
    try:
        run(tracker2.process_due())
        assert publisher2.sends == []

        result = run(tracker2.force_check_latest())
        assert result.outcome == "evaluated"
        assert result.created is False
        assert result.alarm is not None
        assert result.alarm.status == "reported"
        assert len(publisher2.sends) == 1
    finally:
        database2.close()


def test_completion_recovery_ignores_pre_restart_persistent_alarm(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "completed-reactions-before-restart.sqlite3"
    database = StateDatabase(path)
    database.create_alarm(
        group_id="monitor-group",
        trigger_author_aci=AUTHOR,
        trigger_timestamp_ms=SERVICE_START_MS - 20_000,
        tracking_started_at_ms=SERVICE_START_MS - 20_000,
        intermediate_deadline_timestamp_ms=SERVICE_START_MS - 15_000,
        deadline_timestamp_ms=SERVICE_START_MS - 10_000,
        expires_timestamp_ms=SERVICE_START_MS + 40_000,
        released_members=(),
        created_at_ms=SERVICE_START_MS - 20_000,
    )
    database.close()

    clock = Clock(SERVICE_START_MS)
    database2, publisher, tracker = build(
        app_config,
        roster,
        clock,
        path=path,
    )
    try:
        run(tracker.process_due())
        assert database2.list_alarms()[0].status == "pending"
        assert publisher.sends == []
    finally:
        database2.close()


def test_signal_reconnect_does_not_move_process_boundary(app_config, roster) -> None:
    clock = Clock(SERVICE_START_MS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        clock.value = SERVICE_START_MS + 60 * 60 * 1000
        run(tracker.handle_event(message(SERVICE_START_MS - 1)))
        run(tracker.handle_event(message(SERVICE_START_MS + 1)))

        assert tracker.service_started_at_ms == SERVICE_START_MS
        assert [alarm.trigger_timestamp_ms for alarm in database.list_alarms()] == [
            SERVICE_START_MS + 1
        ]
    finally:
        database.close()
