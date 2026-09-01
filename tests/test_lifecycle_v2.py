from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sheltercheck.database import StateDatabase
from sheltercheck.models import ReactionEvent
from sheltercheck.signal_client import SignalClientError, SignalRPCError
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1, ACI_2, ACI_3, AUTHOR
from test_tracker import Clock, FakePublisher, TRIGGER_TS, build, reaction, trigger


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


class FailingPublisher(FakePublisher):
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        edit_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.send_error = send_error
        self.edit_error = edit_error
        self.send_attempts = 0
        self.edit_attempts = 0

    async def send(self, message: str) -> int:
        self.send_attempts += 1
        if self.send_error is not None:
            raise self.send_error
        return await super().send(message)

    async def edit(self, message: str, edit_timestamp_ms: int) -> None:
        self.edit_attempts += 1
        if self.edit_error is not None:
            raise self.edit_error
        await super().edit(message, edit_timestamp_ms)


def build_with_publisher(app_config, roster, clock, publisher, path=":memory:"):
    database = StateDatabase(path)
    tracker = AlertTracker(app_config, roster, database, publisher, clock_ms=clock)
    return database, tracker


@pytest.mark.parametrize(
    "status, active",
    [
        ("pending", True),
        ("reported", True),
        ("completed", False),
        ("expired", False),
        ("error", False),
        ("stale", False),
    ],
)
def test_only_pending_and_reported_are_active(status, active, members) -> None:
    database = StateDatabase(":memory:")
    try:
        database.create_alarm(
            group_id="monitor",
            trigger_author_aci=AUTHOR,
            trigger_timestamp_ms=TRIGGER_TS,
            deadline_timestamp_ms=TRIGGER_TS + 10_000,
            released_members=members,
            status=status,
        )
        assert database.count_active_alarms() == int(active)
    finally:
        database.close()


def test_intermediate_is_once_and_uses_alarm_snapshot(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1_000)))
        app_config.released_file.write_text("Іваненко І.І\n", encoding="utf-8")

        run(tracker.process_due(TRIGGER_TS + 4_999))
        assert publisher.sends == []
        run(tracker.process_due(TRIGGER_TS + 5_000))
        run(tracker.process_due(TRIGGER_TS + 5_001))

        assert len(publisher.sends) == 1
        assert publisher.sends[0].startswith("Проміжна перевірка ")
        assert "Відмітилися: 1/3" in publisher.sends[0]
        assert "Очікуються: 2" in publisher.sends[0]
        assert "1. Петренко П.П | +380502222222" in publisher.sends[0]
        assert "2. Коваль А.А | +380503333333" in publisher.sends[0]
        assert database.list_alarms()[0].status == "pending"
    finally:
        database.close()


def test_intermediate_survives_restart_without_duplicate(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "intermediate.sqlite3"
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock, path)
    run(tracker.handle_event(trigger()))
    run(tracker.process_due(TRIGGER_TS + 5_000))
    assert len(publisher.sends) == 1
    database.close()

    database2, publisher2, tracker2 = build(app_config, roster, clock, path)
    try:
        run(tracker2.process_due(TRIGGER_TS + 6_000))
        assert publisher2.sends == []
    finally:
        database2.close()


def test_final_deadline_skips_late_intermediate(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.process_due(TRIGGER_TS + 15_000))
        assert len(publisher.sends) == 1
        assert not publisher.sends[0].startswith("Проміжна перевірка")
        assert database.list_alarms()[0].status == "reported"
        operation = database.get_outgoing("alarm:1:intermediate")
        assert operation is not None and operation.state == "skipped"
    finally:
        database.close()


def test_early_completion_exactly_once_and_terminal(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        for offset, aci in enumerate((ACI_1, ACI_2, ACI_3), 1):
            clock.value = TRIGGER_TS + offset * 1_000
            run(tracker.handle_event(reaction(aci, sent=clock.value)))

        assert publisher.sends == [
            "✅ Усі 3/3 відмітилися.\nПеревірку завершено за 3 с."
        ]
        alarm = database.list_alarms()[0]
        assert alarm.status == "completed"
        responded = alarm.responded_acis

        run(
            tracker.handle_event(
                reaction(ACI_3, removed=True, sent=clock.value + 1_000)
            )
        )
        run(tracker.process_due(TRIGGER_TS + 70_000))
        assert len(publisher.sends) == 1
        assert database.list_alarms()[0].responded_acis == responded
        assert database.list_alarms()[0].status == "completed"
    finally:
        database.close()


def test_completion_after_intermediate_does_not_wait_for_final(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1_000)))
        run(tracker.process_due(TRIGGER_TS + 5_000))
        assert len(publisher.sends) == 1
        clock.value = TRIGGER_TS + 6_000
        run(tracker.handle_event(reaction(ACI_2, sent=clock.value - 1)))
        run(tracker.handle_event(reaction(ACI_3, sent=clock.value)))
        assert len(publisher.sends) == 2
        assert "завершено за 6 с" in publisher.sends[-1]
        run(tracker.process_due(TRIGGER_TS + 10_000))
        assert len(publisher.sends) == 2
    finally:
        database.close()


def test_final_report_then_last_reaction_completion_edits_once(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 1_000)))
        run(tracker.handle_event(reaction(ACI_2, sent=TRIGGER_TS + 2_000)))
        run(tracker.process_due(TRIGGER_TS + 10_000))
        assert database.list_alarms()[0].status == "reported"

        clock.value = TRIGGER_TS + 12_000
        run(tracker.handle_event(reaction(ACI_3, sent=clock.value)))
        assert len(publisher.edits) == 1
        assert publisher.edits[0][0] == database.list_alarms()[0].report_message_timestamp_ms
        assert "✅ Усі 3/3" in publisher.edits[0][1]
        assert database.list_alarms()[0].status == "completed"
    finally:
        database.close()


@pytest.mark.parametrize("reported_first", [False, True])
def test_ttl_silently_expires_pending_or_reported(
    app_config, roster, reported_first
) -> None:
    clock = Clock(TRIGGER_TS)
    database, publisher, tracker = build(app_config, roster, clock)
    try:
        run(tracker.handle_event(trigger()))
        if reported_first:
            run(tracker.process_due(TRIGGER_TS + 10_000))
        send_count = len(publisher.sends)
        edit_count = len(publisher.edits)
        run(tracker.process_due(TRIGGER_TS + 60_000))
        assert database.list_alarms()[0].status == "expired"
        assert len(publisher.sends) == send_count
        assert len(publisher.edits) == edit_count
        run(tracker.handle_event(reaction(ACI_1, sent=TRIGGER_TS + 60_001)))
        assert database.list_alarms()[0].responded_acis == frozenset()
    finally:
        database.close()


def test_restart_after_ttl_expires_only(app_config, roster, tmp_path: Path) -> None:
    path = tmp_path / "ttl.sqlite3"
    clock = Clock(TRIGGER_TS)
    database, _, tracker = build(app_config, roster, clock, path)
    run(tracker.handle_event(trigger()))
    database.close()

    clock.value = TRIGGER_TS + 60_001
    database2, publisher2, tracker2 = build(app_config, roster, clock, path)
    try:
        run(tracker2.process_due())
        assert database2.list_alarms()[0].status == "expired"
        assert publisher2.sends == []
        assert publisher2.edits == []
    finally:
        database2.close()


@pytest.mark.parametrize(
    "phase",
    ["intermediate", "final", "early_completion", "manual"],
)
def test_explicit_send_failure_is_never_retried(
    app_config, roster, tmp_path: Path, phase: str
) -> None:
    path = tmp_path / f"{phase}.sqlite3"
    clock = Clock(TRIGGER_TS)
    publisher = FailingPublisher(send_error=SignalRPCError(-1, "rejected"))
    database, tracker = build_with_publisher(app_config, roster, clock, publisher, path)
    run(tracker.handle_event(trigger()))
    if phase == "intermediate":
        now = TRIGGER_TS + 5_000
        run(tracker.process_due(now))
    elif phase == "final":
        now = TRIGGER_TS + 10_000
        run(tracker.process_due(now))
    elif phase == "early_completion":
        now = TRIGGER_TS + 3_000
        for offset, aci in enumerate((ACI_1, ACI_2, ACI_3), 1):
            clock.value = TRIGGER_TS + offset * 1_000
            run(tracker.handle_event(reaction(aci, sent=clock.value)))
    else:
        now = TRIGGER_TS + 1_000
        clock.value = now
        run(tracker.force_check_latest())
    assert publisher.send_attempts == 1
    run(tracker.process_due(now))
    run(tracker.process_due(now + 1))
    assert publisher.send_attempts == 1
    database.close()

    publisher2 = FailingPublisher(send_error=SignalRPCError(-1, "rejected"))
    database2, tracker2 = build_with_publisher(
        app_config, roster, clock, publisher2, path
    )
    try:
        run(tracker2.process_due(now + 2))
        if phase == "manual":
            run(tracker2.force_check_latest())
        assert publisher2.send_attempts == 0
    finally:
        database2.close()


def test_uncertain_send_is_persisted_without_retry_or_breaker(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "uncertain.sqlite3"
    clock = Clock(TRIGGER_TS)
    publisher = FailingPublisher(send_error=SignalClientError("timeout"))
    database, tracker = build_with_publisher(app_config, roster, clock, publisher, path)
    run(tracker.handle_event(trigger()))
    run(tracker.process_due(TRIGGER_TS + 5_000))
    operation = database.get_outgoing("alarm:1:intermediate")
    assert operation is not None
    assert operation.state == "attempted_uncertain"
    assert operation.attempt_count == 1
    run(tracker.process_due(TRIGGER_TS + 5_001))
    assert publisher.send_attempts == 1
    database.close()

    publisher2 = FailingPublisher(send_error=SignalClientError("timeout"))
    database2, tracker2 = build_with_publisher(app_config, roster, clock, publisher2, path)
    try:
        run(tracker2.process_due(TRIGGER_TS + 5_002))
        assert publisher2.send_attempts == 0
    finally:
        database2.close()


def test_uncertain_final_never_causes_independent_completion_duplicate(
    app_config, roster
) -> None:
    clock = Clock(TRIGGER_TS)
    publisher = FailingPublisher(send_error=SignalClientError("disconnect"))
    database, tracker = build_with_publisher(app_config, roster, clock, publisher)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.process_due(TRIGGER_TS + 10_000))
        assert publisher.send_attempts == 1
        publisher.send_error = None
        for offset, aci in enumerate((ACI_1, ACI_2, ACI_3), 11):
            clock.value = TRIGGER_TS + offset * 1_000
            run(tracker.handle_event(reaction(aci, sent=clock.value)))
        assert publisher.send_attempts == 1
        assert database.list_alarms()[0].status == "completed"
        completion = database.get_outgoing("alarm:1:completion")
        assert completion is not None and completion.state == "skipped"
    finally:
        database.close()


def test_failed_completion_edit_is_terminal_and_not_retried(app_config, roster) -> None:
    clock = Clock(TRIGGER_TS)
    publisher = FailingPublisher(edit_error=SignalRPCError(-1, "edit rejected"))
    database, tracker = build_with_publisher(app_config, roster, clock, publisher)
    try:
        run(tracker.handle_event(trigger()))
        run(tracker.process_due(TRIGGER_TS + 10_000))
        for offset, aci in enumerate((ACI_1, ACI_2, ACI_3), 11):
            clock.value = TRIGGER_TS + offset * 1_000
            run(tracker.handle_event(reaction(aci, sent=clock.value)))
        assert publisher.edit_attempts == 1
        assert database.list_alarms()[0].status == "completed"
        run(tracker.process_due(clock.value + 10_000))
        assert publisher.edit_attempts == 1
    finally:
        database.close()


def test_failed_partial_report_edit_is_not_retried(
    app_config, roster, tmp_path: Path
) -> None:
    path = tmp_path / "failed-edit.sqlite3"
    clock = Clock(TRIGGER_TS)
    publisher = FailingPublisher(edit_error=SignalRPCError(-1, "edit rejected"))
    database, tracker = build_with_publisher(
        app_config, roster, clock, publisher, path
    )
    run(tracker.handle_event(trigger()))
    run(tracker.process_due(TRIGGER_TS + 10_000))
    clock.value = TRIGGER_TS + 11_000
    run(tracker.handle_event(reaction(ACI_1, sent=clock.value)))
    run(tracker.process_due(clock.value + 1_000))
    run(tracker.process_due(clock.value + 2_000))
    assert publisher.edit_attempts == 1
    assert database.list_alarms()[0].status == "reported"
    database.close()

    publisher2 = FailingPublisher(edit_error=SignalRPCError(-1, "edit rejected"))
    database2, tracker2 = build_with_publisher(
        app_config, roster, clock, publisher2, path
    )
    try:
        run(tracker2.process_due(clock.value + 3_000))
        assert publisher2.edit_attempts == 0
    finally:
        database2.close()
