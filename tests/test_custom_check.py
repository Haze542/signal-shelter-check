from __future__ import annotations

import asyncio
from pathlib import Path

from sheltercheck.database import OBSERVED_RETENTION_MS, StateDatabase
from sheltercheck.models import MessageEvent
from sheltercheck.signal_client import SignalRPCError
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1, ACI_2, ACI_3, AUTHOR
from test_lifecycle_v2 import FailingPublisher, build_with_publisher
from test_tracker import Clock, TRIGGER_TS, build, reaction, trigger


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def custom_message(
    text: str,
    *,
    timestamp: int = TRIGGER_TS,
    group: str = "monitor-group",
    author: str = AUTHOR,
) -> MessageEvent:
    return MessageEvent(group, author, timestamp, text)


def test_custom_check_selects_newest_exact_normalized_message_and_uses_manual_timing(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Всі на зв'язку??", timestamp=TRIGGER_TS)))
        newest = TRIGGER_TS + 10_000
        run(
            tracker.handle_event(
                custom_message("  ВСІ\tна  зв'язку?? ", timestamp=newest)
            )
        )
        clock.value = TRIGGER_TS + 20_000
        result = run(tracker.force_check_message("всі на зв'язку??"))

        assert result.created is True
        alarm = database.list_alarms()[0]
        assert alarm.source == "custom"
        assert alarm.trigger_timestamp_ms == newest
        assert alarm.tracking_started_at_ms == TRIGGER_TS + 20_000
        assert alarm.intermediate_deadline_timestamp_ms == TRIGGER_TS + 25_000
        assert alarm.deadline_timestamp_ms == TRIGGER_TS + 30_000
        assert alarm.expires_timestamp_ms == TRIGGER_TS + 80_000
        assert len(publisher.sends) == 1  # immediate manual authoritative evaluation
        assert "3/3" in publisher.sends[0]
    finally:
        database.close()


def test_custom_match_uses_nfkc_casefold_and_whitespace_but_not_fuzzy(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Перевірка   Ａ")))
        clock.value += 1_000
        matched = run(tracker.force_check_message("перевірка a"))
        assert matched.outcome == "evaluated"

        run(
            tracker.handle_event(
                custom_message("Всі на зв'язку?? зараз", timestamp=TRIGGER_TS + 2_000)
            )
        )
        assert run(tracker.force_check_message("Всі на зв'язку??")).outcome == (
            "message_not_found"
        )
        assert run(tracker.force_check_message("Всі на зв'язку?")).outcome == (
            "message_not_found"
        )
    finally:
        database.close()


def test_reactions_received_before_custom_check_are_replayed(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Нестандартна перевірка")))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1_000)))
        run(tracker.handle_event(reaction(ACI_2, sent=TRIGGER_TS + 2_000)))
        clock.value = TRIGGER_TS + 3_000
        run(tracker.force_check_message("нестандартна перевірка"))

        alarm = database.list_alarms()[0]
        assert alarm.responded_acis == frozenset({ACI_1, ACI_2})
        assert [member.signal_aci for member in alarm.missing_members] == [ACI_3]
    finally:
        database.close()


def test_preexisting_custom_reaction_remove_uses_latest_event(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Нестандартна перевірка")))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1_000)))
        run(
            tracker.handle_event(
                reaction(
                    ACI_1,
                    removed=True,
                    sent=TRIGGER_TS + 2_000,
                )
            )
        )
        run(tracker.handle_event(reaction(ACI_2, sent=TRIGGER_TS + 3_000)))
        clock.value = TRIGGER_TS + 4_000
        run(tracker.force_check_message("Нестандартна перевірка"))
        assert database.list_alarms()[0].responded_acis == frozenset({ACI_2})
    finally:
        database.close()


def test_duplicate_custom_check_reuses_session_snapshot_and_operation(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Нестандартна перевірка")))
        clock.value += 1_000
        first = run(tracker.force_check_message("Нестандартна перевірка"))
        alarm = database.list_alarms()[0]
        tracking_start = alarm.tracking_started_at_ms
        snapshot = alarm.released_members
        app_config.released_file.write_text("Іваненко І.І\n", encoding="utf-8")
        clock.value += 2_000
        second = run(tracker.force_check_message("Нестандартна перевірка"))

        assert first.created is True
        assert second.created is False
        assert len(database.list_alarms()) == 1
        assert database.list_alarms()[0].tracking_started_at_ms == tracking_start
        assert database.list_alarms()[0].released_members == snapshot
        assert len(publisher.sends) == 1
    finally:
        database.close()


def test_custom_snapshot_is_taken_when_manual_tracking_starts(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Нестандартна перевірка")))
        app_config.released_file.write_text("Іваненко І.І\n", encoding="utf-8")
        clock.value += 10_000
        run(tracker.force_check_message("Нестандартна перевірка"))
        assert [
            member.signal_aci for member in database.list_alarms()[0].released_members
        ] == [ACI_1]
    finally:
        database.close()


def test_custom_message_security_filters_group_and_author(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(
            tracker.handle_event(
                custom_message("Секретна перевірка", group="wrong-group")
            )
        )
        run(
            tracker.handle_event(
                custom_message(
                    "Секретна перевірка",
                    timestamp=TRIGGER_TS + 1,
                    author=ACI_1,
                )
            )
        )
        result = run(tracker.force_check_message("Секретна перевірка"))
        assert result.outcome == "message_not_found"
        assert database.list_alarms() == ()
        assert publisher.sends == []
    finally:
        database.close()


def test_observed_history_is_bounded_to_24_hours(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(custom_message("Стара перевірка")))
        assert database.observed_message_count() == 1
        clock.value = TRIGGER_TS + OBSERVED_RETENTION_MS + 1
        run(tracker.process_due())
        assert database.observed_message_count() == 0
        assert run(tracker.force_check_message("Стара перевірка")).outcome == (
            "message_not_found"
        )
    finally:
        database.close()


def test_custom_send_failure_is_not_retried_after_duplicate_command_or_restart(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "custom-no-retry.sqlite3"
    clock = Clock(TRIGGER_TS)
    publisher = FailingPublisher(send_error=SignalRPCError(-1, "rejected"))
    database, tracker = build_with_publisher(app_config, roster, clock, publisher, path)
    run(tracker.handle_event(custom_message("Нестандартна перевірка")))
    clock.value += 1_000
    run(tracker.force_check_message("Нестандартна перевірка"))
    run(tracker.force_check_message("Нестандартна перевірка"))
    assert publisher.send_attempts == 1
    database.close()

    publisher2 = FailingPublisher(send_error=SignalRPCError(-1, "rejected"))
    database2, tracker2 = build_with_publisher(
        app_config, roster, clock, publisher2, path
    )
    try:
        run(tracker2.force_check_message("Нестандартна перевірка"))
        assert publisher2.send_attempts == 0
    finally:
        database2.close()


def test_check_without_arguments_reuses_latest_standard_and_never_resets_timers(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        assert run(tracker.force_check_latest()).outcome == "no_standard"
        run(tracker.handle_event(trigger()))
        alarm = database.list_alarms()[0]
        clock.value += 1_000
        run(tracker.force_check_latest())
        run(tracker.force_check_latest())
        refreshed = database.list_alarms()[0]
        assert len(database.list_alarms()) == 1
        assert refreshed.trigger_timestamp_ms == alarm.trigger_timestamp_ms
        assert refreshed.tracking_started_at_ms == alarm.tracking_started_at_ms
        assert refreshed.deadline_timestamp_ms == alarm.deadline_timestamp_ms
        assert len(publisher.sends) == 1
    finally:
        database.close()


def test_terminal_standard_check_does_not_create_another_report(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        for offset, aci in enumerate((ACI_1, ACI_2, ACI_3), 1):
            clock.value += 1_000
            run(tracker.handle_event(reaction(aci, sent=clock.value)))
        sends = len(publisher.sends)
        result = run(tracker.force_check_latest())
        assert result.outcome == "terminal"
        assert len(publisher.sends) == sends
    finally:
        database.close()
