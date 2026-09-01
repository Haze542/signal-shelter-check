from __future__ import annotations

import asyncio
import sqlite3
from datetime import timezone
from pathlib import Path

from sheltercheck.database import StateDatabase
from sheltercheck.models import MessageEvent, ReactionEvent
from sheltercheck.reporter import format_report
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1, ACI_2, ACI_3, AUTHOR


TRIGGER_TS = 1_000_000


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class FakePublisher:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.next_timestamp = 9_000_000

    async def send(self, message: str) -> int:
        self.sends.append(message)
        self.next_timestamp += 1
        return self.next_timestamp

    async def edit(self, message: str, edit_timestamp_ms: int) -> None:
        self.edits.append((edit_timestamp_ms, message))


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def trigger(
    *,
    timestamp: int = TRIGGER_TS,
    group: str = "monitor-group",
    author: str = AUTHOR,
    text: str = "  ВСІ   в\tукритті?  ",
) -> MessageEvent:
    return MessageEvent(group, author, timestamp, text)


def reaction(
    reactor: str = ACI_1,
    *,
    target: int = TRIGGER_TS,
    author: str | None = AUTHOR,
    emoji: str = "➕",
    removed: bool = False,
    sent: int = TRIGGER_TS + 1_000,
    group: str = "monitor-group",
) -> ReactionEvent:
    return ReactionEvent(group, reactor, author, target, emoji, removed, sent)


def build(app_config, roster, clock: Clock, path: str | Path = ":memory:"):
    database = StateDatabase(path)
    publisher = FakePublisher()
    tracker = AlertTracker(app_config, roster, database, publisher, clock_ms=clock)
    return database, publisher, tracker


def test_valid_trigger_duplicate_and_filters(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger(group="other-group")))
        run(tracker.handle_event(trigger(author=ACI_1)))
        assert database.list_alarms() == ()

        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(trigger()))
        alarms = database.list_alarms()
        assert len(alarms) == 1
        assert len(alarms[0].released_members) == 3
        assert publisher.sends == []
    finally:
        database.close()


def test_identical_text_with_different_timestamps_creates_independent_alerts(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS)))
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS + 2_000)))
        assert [alarm.trigger_timestamp_ms for alarm in database.list_alarms()] == [
            TRIGGER_TS,
            TRIGGER_TS + 2_000,
        ]
    finally:
        database.close()


def test_reaction_routing_membership_emoji_removal_and_idempotency(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS)))
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS + 2_000)))

        run(tracker.handle_event(reaction(target=TRIGGER_TS)))
        run(tracker.handle_event(reaction(target=TRIGGER_TS)))  # duplicate delivery
        first, second = database.list_alarms()
        assert first.responded_acis == frozenset({ACI_1})
        assert second.responded_acis == frozenset()

        run(tracker.handle_event(reaction(reactor="00000000-0000-4000-8000-000000000099")))
        run(tracker.handle_event(reaction(reactor=ACI_2, emoji="👍")))
        run(tracker.handle_event(reaction(reactor=ACI_1, removed=True, sent=TRIGGER_TS + 2_000)))
        assert database.get_alarm(first.id).responded_acis == frozenset()
    finally:
        database.close()


def test_wrong_target_author_is_ignored(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(author=ACI_3)))
        assert database.list_alarms()[0].responded_acis == frozenset()
    finally:
        database.close()


def test_final_report_late_reactions_complete_immediately(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1)))
        clock.value = TRIGGER_TS + 10_000
        run(tracker.process_due())

        assert len(publisher.sends) == 1
        assert "2/3" in publisher.sends[0]
        assert "🔴 | Петренко П.П | +380502222222" in publisher.sends[0]
        assert "🔴 | Коваль А.А | +380503333333" in publisher.sends[0]
        assert "Іваненко І.І" not in publisher.sends[0]
        alarm = database.list_alarms()[0]
        assert [member.signal_aci for member in alarm.initial_missing] == [ACI_2, ACI_3]
        report_timestamp = alarm.report_message_timestamp_ms

        clock.value += 100
        run(tracker.handle_event(reaction(ACI_2, sent=clock.value)))
        clock.value += 100
        run(tracker.handle_event(reaction(ACI_3, sent=clock.value)))

        assert len(publisher.edits) == 1
        assert publisher.edits[0][0] == report_timestamp
        edited = publisher.edits[0][1]
        assert edited == (
            "✅ Усі 3/3 відмітилися.\n"
            "Перевірку завершено за 10 с."
        )
        assert database.list_alarms()[0].status == "completed"
    finally:
        database.close()


def test_all_confirmed_before_deadline_completes_immediately_once(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1)))
        run(tracker.handle_event(reaction(ACI_2, sent=TRIGGER_TS + 2)))
        run(tracker.handle_event(reaction(ACI_3, sent=TRIGGER_TS + 3)))
        assert len(publisher.sends) == 1
        clock.value = TRIGGER_TS + 10_000
        run(tracker.process_due())
        run(tracker.process_due())

        assert len(publisher.sends) == 1
        assert "✅ Усі 3/3 відмітилися." in publisher.sends[0]
        assert "Іваненко І.І" not in publisher.sends[0]
        assert database.list_alarms()[0].initial_missing == ()
        assert database.list_alarms()[0].status == "completed"
    finally:
        database.close()


def test_late_reaction_keeps_row_and_removes_numbering(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        clock.value = TRIGGER_TS + 10_000
        run(tracker.process_due())
        clock.value += 1
        run(tracker.handle_event(reaction(ACI_1, sent=clock.value)))
        run(tracker.process_due(clock.value + 1_000))
        edited = publisher.edits[-1][1]
        assert "🟢 | Іваненко І.І | +380501111111" in edited
        assert "🔴 | Петренко П.П | +380502222222" in edited
        assert "🔴 | Коваль А.А | +380503333333" in edited
        assert "\n1 |" not in edited
        assert "\n2 |" not in edited
        assert [
            member.signal_aci for member in database.list_alarms()[0].initial_missing
        ] == [ACI_1, ACI_2, ACI_3]
    finally:
        database.close()


def test_two_missing_update_independently_and_completed_report_is_terminal(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1)))
        clock.value = TRIGGER_TS + 10_000
        run(tracker.process_due())

        clock.value += 1
        run(tracker.handle_event(reaction(ACI_2, sent=clock.value)))
        run(tracker.process_due(clock.value + 1_000))
        one_late = publisher.edits[-1][1]
        assert "Не поставили +" in one_late
        assert "1/3" in one_late
        assert "🟢 | Петренко П.П | +380502222222" in one_late
        assert "🔴 | Коваль А.А | +380503333333" in one_late

        clock.value += 1_001
        run(tracker.handle_event(reaction(ACI_3, sent=clock.value)))
        run(tracker.process_due(clock.value + 1_000))
        all_late = publisher.edits[-1][1]
        assert "✅ Усі 3/3 відмітилися." in all_late
        assert database.list_alarms()[0].status == "completed"

        edit_count = len(publisher.edits)
        clock.value += 1_001
        run(
            tracker.handle_event(
                reaction(ACI_2, removed=True, sent=clock.value)
            )
        )
        run(tracker.process_due(clock.value + 1_000))
        assert len(publisher.edits) == edit_count
        assert database.list_alarms()[0].status == "completed"
        assert database.list_alarms()[0].responded_acis == frozenset(
            {ACI_1, ACI_2, ACI_3}
        )
    finally:
        database.close()


def test_removed_late_reaction_changes_green_back_to_red(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        clock.value = TRIGGER_TS + 10_000
        run(tracker.process_due())
        assert "🔴 | Іваненко І.І" in publisher.sends[0]

        clock.value += 1
        run(tracker.handle_event(reaction(ACI_1, sent=clock.value)))
        run(tracker.process_due(clock.value + 1_000))
        assert "🟢 | Іваненко І.І" in publisher.edits[-1][1]

        clock.value += 1_001
        run(
            tracker.handle_event(
                reaction(ACI_1, removed=True, sent=clock.value)
            )
        )
        run(tracker.process_due(clock.value + 1_000))
        assert "3/3" in publisher.edits[-1][1]
        assert "🔴 | Іваненко І.І | +380501111111" in publisher.edits[-1][1]
        assert [
            member.signal_aci for member in database.list_alarms()[0].initial_missing
        ] == [ACI_1, ACI_2, ACI_3]
    finally:
        database.close()


def test_released_snapshot_does_not_change_after_trigger(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        app_config.released_file.write_text("Іваненко І.І\n", encoding="utf-8")
        clock.value = TRIGGER_TS + 10_000
        run(tracker.process_due())
        assert "3/3" in publisher.sends[0]
        assert "Коваль А.А" in publisher.sends[0]
    finally:
        database.close()


def test_restart_does_not_duplicate_initial_report(app_config, roster, tmp_path: Path) -> None:
    db_path = tmp_path / "persistent.sqlite3"
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock, db_path)
    run(tracker.handle_event(trigger()))
    clock.value = TRIGGER_TS + 10_000
    run(tracker.process_due())
    assert len(publisher.sends) == 1
    database.close()

    database2, publisher2, tracker2 = build(app_config, roster, clock, db_path)
    try:
        run(tracker2.process_due())
        assert publisher2.sends == []
        assert database2.list_alarms()[0].initial_report_state == 1
    finally:
        database2.close()


def test_restart_preserves_initial_missing_statuses_and_report_timestamp(
    app_config, roster, tmp_path: Path
) -> None:
    db_path = tmp_path / "persistent-state.sqlite3"
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock, db_path)
    run(tracker.handle_event(trigger()))
    run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1)))
    clock.value = TRIGGER_TS + 10_000
    run(tracker.process_due())
    report_timestamp = database.list_alarms()[0].report_message_timestamp_ms
    clock.value += 1
    run(tracker.handle_event(reaction(ACI_2, sent=clock.value)))
    run(tracker.process_due(clock.value + 1_000))
    database.close()

    database2, publisher2, tracker2 = build(app_config, roster, clock, db_path)
    try:
        alarm = database2.list_alarms()[0]
        assert [member.signal_aci for member in alarm.initial_missing] == [ACI_2, ACI_3]
        assert alarm.responded_acis == frozenset({ACI_1, ACI_2})
        assert alarm.report_message_timestamp_ms == report_timestamp

        clock.value += 1_001
        run(
            tracker2.handle_event(
                reaction(ACI_2, removed=True, sent=clock.value)
            )
        )
        run(tracker2.process_due(clock.value + 1_000))
        assert publisher2.sends == []
        assert publisher2.edits[-1][0] == report_timestamp
        assert "🔴 | Петренко П.П | +380502222222" in publisher2.edits[-1][1]
        assert "🔴 | Коваль А.А | +380503333333" in publisher2.edits[-1][1]
    finally:
        database2.close()


def test_existing_database_schema_is_migrated_with_initial_missing(
    members, tmp_path: Path
) -> None:
    db_path = tmp_path / "old-schema.sqlite3"
    connection = sqlite3.connect(db_path)
    with connection:
        connection.executescript(
            """
            CREATE TABLE alarms (
                id INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL,
                trigger_author_aci TEXT NOT NULL,
                trigger_timestamp_ms INTEGER NOT NULL,
                deadline_timestamp_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                initial_report_state INTEGER NOT NULL DEFAULT 0,
                report_message_timestamp_ms INTEGER,
                last_report_text TEXT,
                edit_due_timestamp_ms INTEGER,
                error_detail TEXT,
                created_at_ms INTEGER NOT NULL,
                UNIQUE (group_id, trigger_author_aci, trigger_timestamp_ms)
            );
            CREATE TABLE released_members (
                alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                signal_aci TEXT NOT NULL,
                display_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                PRIMARY KEY (alarm_id, signal_aci),
                UNIQUE (alarm_id, position)
            );
            CREATE TABLE reaction_state (
                alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                reactor_aci TEXT NOT NULL,
                responded INTEGER NOT NULL,
                last_event_timestamp_ms INTEGER NOT NULL,
                last_emoji TEXT NOT NULL,
                last_removed INTEGER NOT NULL,
                PRIMARY KEY (alarm_id, reactor_aci)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO alarms (
                id, group_id, trigger_author_aci, trigger_timestamp_ms,
                deadline_timestamp_ms, status, initial_report_state,
                report_message_timestamp_ms, last_report_text, created_at_ms
            ) VALUES (1, ?, ?, ?, ?, 'reported', 1, 9000001, 'old report', ?)
            """,
            ("monitor-group", AUTHOR, TRIGGER_TS, TRIGGER_TS + 10_000, TRIGGER_TS),
        )
        connection.executemany(
            """
            INSERT INTO released_members (
                alarm_id, position, signal_aci, display_name, phone
            ) VALUES (1, ?, ?, ?, ?)
            """,
            [
                (position, member.signal_aci, member.display_name, member.phone)
                for position, member in enumerate(members)
            ],
        )
        connection.executemany(
            """
            INSERT INTO reaction_state (
                alarm_id, reactor_aci, responded, last_event_timestamp_ms,
                last_emoji, last_removed
            ) VALUES (1, ?, 1, ?, '➕', 0)
            """,
            [
                (ACI_1, TRIGGER_TS + 1),
                (ACI_2, TRIGGER_TS + 10_001),
            ],
        )
    connection.close()

    database = StateDatabase(db_path)
    try:
        alarm = database.get_alarm(1)
        assert [member.signal_aci for member in alarm.initial_missing] == [ACI_2, ACI_3]
        assert alarm.responded_acis == frozenset({ACI_1, ACI_2})
        assert alarm.report_message_timestamp_ms == 9_000_001
        assert alarm.tracking_started_at_ms == TRIGGER_TS
        assert alarm.intermediate_deadline_timestamp_ms == TRIGGER_TS + 10_000
        assert alarm.expires_timestamp_ms == TRIGGER_TS + 21_600_000
        assert alarm.source == "standard"
        operation = database.get_outgoing("alarm:1:final")
        assert operation is not None
        assert operation.state == "attempted_success"
    finally:
        database.close()


def test_independent_alerts_have_independent_initial_missing(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    second_timestamp = TRIGGER_TS + 2_000
    try:
        run(tracker.handle_event(trigger(timestamp=TRIGGER_TS)))
        run(tracker.handle_event(trigger(timestamp=second_timestamp)))
        run(tracker.handle_event(reaction(ACI_1, target=TRIGGER_TS, sent=TRIGGER_TS + 1)))
        run(
            tracker.handle_event(
                reaction(ACI_2, target=second_timestamp, sent=second_timestamp + 1)
            )
        )
        clock.value = second_timestamp + 10_000
        run(tracker.process_due())

        first, second = database.list_alarms()
        assert [member.signal_aci for member in first.initial_missing] == [ACI_2, ACI_3]
        assert [member.signal_aci for member in second.initial_missing] == [ACI_1, ACI_3]
        assert len(publisher.sends) == 2
    finally:
        database.close()


def test_invalid_released_file_records_error_without_report(app_config, roster) -> None:
    app_config.released_file.write_text("Невідомий Н.Н\n", encoding="utf-8")
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        alarm = database.list_alarms()[0]
        assert alarm.status == "error"
        clock.value = TRIGGER_TS + 20_000
        run(tracker.process_due())
        assert publisher.sends == []
    finally:
        database.close()


def test_late_trigger_before_ttl_runs_only_final_evaluation(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS + 10_001)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        alarm = database.list_alarms()[0]
        assert alarm.status == "pending"
        assert len(alarm.released_members) == 3
        run(tracker.process_due())
        assert len(publisher.sends) == 1
        assert database.list_alarms()[0].status == "reported"
    finally:
        database.close()


def test_report_formatter_all_confirmed_and_missing(members) -> None:
    initial_missing = members[1:]
    missing = format_report(
        TRIGGER_TS, members, initial_missing, {ACI_1}, timezone=timezone.utc
    )
    confirmed = format_report(
        TRIGGER_TS,
        members,
        initial_missing,
        {ACI_1, ACI_2, ACI_3},
        timezone=timezone.utc,
    )
    assert missing == (
        "Не поставили + на перевірку 00:16 — 2/3\n\n"
        "🔴 | Петренко П.П | +380502222222\n"
        "🔴 | Коваль А.А | +380503333333"
    )
    assert confirmed == (
        "Перевірка 00:16\n\n"
        "🟢 | Петренко П.П | +380502222222\n"
        "🟢 | Коваль А.А | +380503333333\n\n"
        "Усі 3/3 поставили +."
    )
