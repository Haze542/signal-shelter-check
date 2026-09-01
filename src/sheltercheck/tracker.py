from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import Config, normalize_text
from .database import StateDatabase
from .models import (
    AlarmSession,
    ForceCheckResult,
    MessageEvent,
    NormalizedEvent,
    ObservedMessage,
    ReactionEvent,
)
from .outgoing import OutgoingAttemptResult, attempt_outgoing_once
from .released_list import ReleasedListError, ReleasedListService
from .reporter import (
    ReportPublisher,
    completion_report_for_alarm,
    current_report_for_alarm,
    intermediate_report_for_alarm,
    report_for_alarm,
)
from .roster import Roster, RosterError


LOGGER = logging.getLogger(__name__)


class AlertTracker:
    """Serialized, persistent shelter-check state machine."""

    def __init__(
        self,
        config: Config,
        roster: Roster,
        database: StateDatabase,
        publisher: ReportPublisher,
        *,
        released_list: ReleasedListService | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.roster = roster
        self.database = database
        self.publisher = publisher
        self.released_list = released_list or ReleasedListService(config.released_file)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = asyncio.Lock()

    async def handle_event(self, event: NormalizedEvent) -> None:
        async with self._lock:
            if isinstance(event, MessageEvent):
                await self._handle_message(event)
            elif isinstance(event, ReactionEvent):
                await self._handle_reaction(event)

    async def process_due(self, now_ms: int | None = None) -> None:
        async with self._lock:
            await self._process_due_locked(self._clock_ms() if now_ms is None else now_ms)

    async def force_check_latest(self) -> ForceCheckResult:
        async with self._lock:
            now_ms = self._clock_ms()
            await self._expire_due(now_ms)
            alarm = self.database.latest_standard_alarm()
            if alarm is None:
                return ForceCheckResult("no_standard")
            return await self._force_alarm(alarm, now_ms, created=False)

    async def force_check_message(self, text: str) -> ForceCheckResult:
        async with self._lock:
            now_ms = self._clock_ms()
            await self._expire_due(now_ms)
            self.database.cleanup_observed_history(now_ms)
            observed = self.database.find_latest_observed_message(
                group_id=self.config.monitor_group_id,
                normalized_text=normalize_text(text),
                allowed_authors=tuple(self.config.trigger_author_uuids),
            )
            if observed is None:
                return ForceCheckResult("message_not_found")

            alarm = self.database.get_alarm_by_identity(
                observed.group_id,
                observed.sender_aci,
                observed.sent_timestamp_ms,
            )
            created = False
            if alarm is None:
                alarm = await self._create_alarm(
                    group_id=observed.group_id,
                    trigger_author_aci=observed.sender_aci,
                    trigger_timestamp_ms=observed.sent_timestamp_ms,
                    tracking_started_at_ms=now_ms,
                    source="custom",
                )
                if alarm is None:
                    alarm = self.database.get_alarm_by_identity(
                        observed.group_id,
                        observed.sender_aci,
                        observed.sent_timestamp_ms,
                    )
                else:
                    created = True
                    await self._replay_observed_reactions(alarm, observed, now_ms)
                    alarm = self.database.get_alarm(alarm.id)
            if alarm is None:
                return ForceCheckResult("message_not_found")
            return await self._force_alarm(alarm, now_ms, created=created)

    def _trigger_author_allowed(self, aci: str) -> bool:
        return (
            not self.config.trigger_author_uuids
            or aci in self.config.trigger_author_uuids
        )

    async def _handle_message(self, event: MessageEvent) -> None:
        if event.group_id != self.config.monitor_group_id:
            return
        if not self._trigger_author_allowed(event.sender_aci):
            return

        normalized = normalize_text(event.text)
        self.database.observe_message(
            group_id=event.group_id,
            sender_aci=event.sender_aci,
            sent_timestamp_ms=event.sent_timestamp_ms,
            original_text=event.text,
            normalized_text=normalized,
        )
        now_ms = self._clock_ms()
        self.database.cleanup_observed_history(now_ms)
        if normalized not in self.config.trigger_texts:
            return

        existing = self.database.get_alarm_by_identity(
            event.group_id, event.sender_aci, event.sent_timestamp_ms
        )
        if existing is not None:
            return
        alarm = await self._create_alarm(
            group_id=event.group_id,
            trigger_author_aci=event.sender_aci,
            trigger_timestamp_ms=event.sent_timestamp_ms,
            tracking_started_at_ms=event.sent_timestamp_ms,
            source="standard",
        )
        if alarm is None:
            return
        if alarm.status == "expired":
            LOGGER.info(
                "Created already-expired alert %s without outgoing work",
                event.sent_timestamp_ms,
            )
            return
        LOGGER.info(
            "Created standard alert %s with %d released members",
            event.sent_timestamp_ms,
            len(alarm.released_members),
        )
        if not alarm.missing_members:
            await self._complete_alarm(alarm.id, now_ms)

    async def _create_alarm(
        self,
        *,
        group_id: str,
        trigger_author_aci: str,
        trigger_timestamp_ms: int,
        tracking_started_at_ms: int,
        source: str,
    ) -> AlarmSession | None:
        now_ms = self._clock_ms()
        intermediate = (
            tracking_started_at_ms + self.config.intermediate_check_milliseconds
        )
        final = tracking_started_at_ms + self.config.wait_milliseconds
        expires = tracking_started_at_ms + self.config.active_check_ttl_milliseconds
        try:
            released = await self.released_list.get_members(self.roster)
        except (ReleasedListError, RosterError) as exc:
            alarm = self.database.create_alarm(
                group_id=group_id,
                trigger_author_aci=trigger_author_aci,
                trigger_timestamp_ms=trigger_timestamp_ms,
                tracking_started_at_ms=tracking_started_at_ms,
                intermediate_deadline_timestamp_ms=intermediate,
                deadline_timestamp_ms=final,
                expires_timestamp_ms=expires,
                released_members=(),
                source=source,
                status="error",
                error_detail=str(exc),
                created_at_ms=now_ms,
            )
            LOGGER.error(
                "Alert %s cannot start: released list is invalid: %s",
                trigger_timestamp_ms,
                exc,
            )
            return alarm

        status = "expired" if now_ms >= expires else "pending"
        return self.database.create_alarm(
            group_id=group_id,
            trigger_author_aci=trigger_author_aci,
            trigger_timestamp_ms=trigger_timestamp_ms,
            tracking_started_at_ms=tracking_started_at_ms,
            intermediate_deadline_timestamp_ms=intermediate,
            deadline_timestamp_ms=final,
            expires_timestamp_ms=expires,
            released_members=released,
            source=source,
            status=status,
            created_at_ms=now_ms,
        )

    async def _handle_reaction(self, event: ReactionEvent) -> None:
        if event.group_id != self.config.monitor_group_id:
            return
        if event.emoji not in self.config.accepted_reactions:
            return
        if (
            event.target_author_aci is not None
            and not self._trigger_author_allowed(event.target_author_aci)
        ):
            return

        observed = self.database.resolve_observed_reaction_target(
            group_id=event.group_id,
            target_timestamp_ms=event.target_timestamp_ms,
            target_author_aci=event.target_author_aci,
        )
        if observed is not None and self._trigger_author_allowed(observed.sender_aci):
            self.database.observe_reaction(target=observed, event=event)

        targets = self.database.find_active_reaction_targets(
            group_id=event.group_id,
            trigger_timestamp_ms=event.target_timestamp_ms,
            target_author_aci=event.target_author_aci,
        )
        if not targets:
            return
        if event.target_author_aci is None and len(targets) != 1:
            LOGGER.warning(
                "Ignoring reaction to timestamp %s because target author is absent and the alert is ambiguous",
                event.target_timestamp_ms,
            )
            return

        now_ms = self._clock_ms()
        await self._expire_due(now_ms)
        for target in targets:
            refreshed = self.database.get_alarm(target.id)
            if not refreshed.is_active:
                continue

            # Preserve event-time deadline semantics if both a ticker and a reaction
            # were delayed: a reaction sent after a deadline cannot enter that report.
            if (
                refreshed.status == "pending"
                and now_ms >= refreshed.deadline_timestamp_ms
                and event.sent_timestamp_ms > refreshed.deadline_timestamp_ms
            ):
                await self._process_final(refreshed.id, now_ms)
            elif (
                refreshed.status == "pending"
                and now_ms >= refreshed.intermediate_deadline_timestamp_ms
                and now_ms < refreshed.deadline_timestamp_ms
                and event.sent_timestamp_ms > refreshed.intermediate_deadline_timestamp_ms
            ):
                await self._process_intermediate(refreshed.id, now_ms)

            changed = self.database.apply_reaction(
                alarm_id=target.id,
                reactor_aci=event.reactor_aci,
                responded=not event.removed,
                event_timestamp_ms=event.sent_timestamp_ms,
                emoji=event.emoji,
                removed=event.removed,
                now_ms=now_ms,
                debounce_ms=self.config.edit_debounce_milliseconds,
            )
            refreshed = self.database.get_alarm(target.id)
            if changed:
                LOGGER.info(
                    "Alert %s response state changed for roster ACI",
                    refreshed.trigger_timestamp_ms,
                )
            if not refreshed.is_active:
                continue
            if changed and not event.removed and not refreshed.missing_members:
                await self._complete_alarm(refreshed.id, now_ms)
                continue
            if refreshed.status == "pending" and now_ms >= refreshed.deadline_timestamp_ms:
                await self._process_final(refreshed.id, now_ms)
            elif (
                refreshed.status == "pending"
                and now_ms >= refreshed.intermediate_deadline_timestamp_ms
            ):
                await self._process_intermediate(refreshed.id, now_ms)

    async def _replay_observed_reactions(
        self,
        alarm: AlarmSession,
        observed: ObservedMessage,
        now_ms: int,
    ) -> None:
        for event in self.database.list_observed_reactions(observed):
            self.database.apply_reaction(
                alarm_id=alarm.id,
                reactor_aci=event.reactor_aci,
                responded=not event.removed,
                event_timestamp_ms=event.sent_timestamp_ms,
                emoji=event.emoji,
                removed=event.removed,
                now_ms=now_ms,
                debounce_ms=self.config.edit_debounce_milliseconds,
            )

    async def _process_due_locked(self, now_ms: int) -> None:
        # Strict priority: a TTL-expired alarm performs no Signal operation.
        await self._expire_due(now_ms)

        # Recover a completion transition whose reaction state committed before a
        # crash. Existing durable operation claims make this restart-safe.
        for alarm in self.database.list_alarms():
            if alarm.is_active and not alarm.missing_members:
                await self._complete_alarm(alarm.id, now_ms)

        for alarm in self.database.list_due_intermediate(now_ms):
            await self._process_intermediate(alarm.id, now_ms)

        for alarm in self.database.list_due_final(now_ms):
            await self._process_final(alarm.id, now_ms)

        for alarm in self.database.list_due_edits(now_ms):
            await self._process_edit(alarm.id, now_ms)

        removed = self.database.cleanup_observed_history(now_ms)
        if removed:
            LOGGER.info("Removed %d expired observed Signal candidate message(s)", removed)

    async def _expire_due(self, now_ms: int) -> None:
        for alarm in self.database.expire_due_alarms(now_ms):
            LOGGER.info(
                "Silently expired alert %s at its active-check TTL",
                alarm.trigger_timestamp_ms,
            )

    async def _process_intermediate(self, alarm_id: int, now_ms: int) -> None:
        alarm = self.database.get_alarm(alarm_id)
        key = self.database.alarm_operation_key(alarm.id, "intermediate")
        if alarm.status != "pending":
            self.database.skip_outgoing(key, now_ms=now_ms, reason="alarm is not pending")
            return
        if now_ms >= alarm.expires_timestamp_ms:
            await self._expire_due(now_ms)
            return
        if now_ms >= alarm.deadline_timestamp_ms:
            self.database.skip_outgoing(
                key,
                now_ms=now_ms,
                reason="final deadline already passed",
            )
            return
        if not alarm.missing_members:
            await self._complete_alarm(alarm.id, now_ms)
            return

        text = intermediate_report_for_alarm(alarm)
        self.database.mark_outgoing_due(key, message=text, now_ms=now_ms)
        result = await attempt_outgoing_once(
            self.database,
            key,
            now_ms=now_ms,
            write=lambda: self.publisher.send(text),
        )
        self._log_attempt("intermediate report", alarm, result)

    async def _process_final(self, alarm_id: int, now_ms: int) -> None:
        alarm = self.database.get_alarm(alarm_id)
        if alarm.status != "pending":
            return
        if now_ms >= alarm.expires_timestamp_ms:
            await self._expire_due(now_ms)
            return
        intermediate_key = self.database.alarm_operation_key(alarm.id, "intermediate")
        self.database.skip_outgoing(
            intermediate_key,
            now_ms=now_ms,
            reason="final evaluation superseded intermediate report",
        )
        if not alarm.missing_members:
            await self._complete_alarm(alarm.id, now_ms)
            return

        alarm = self.database.snapshot_final_missing(alarm.id)
        text = report_for_alarm(alarm)
        key = self.database.alarm_operation_key(alarm.id, "final")
        self.database.mark_outgoing_due(key, message=text, now_ms=now_ms)
        result = await attempt_outgoing_once(
            self.database,
            key,
            now_ms=now_ms,
            write=lambda: self.publisher.send(text),
        )
        self.database.mark_reported(
            alarm.id,
            operation_state=result.state,
            report_timestamp_ms=result.result_timestamp_ms,
            report_text=text,
        )
        self._log_attempt("final report", alarm, result)

    async def _process_edit(self, alarm_id: int, now_ms: int) -> None:
        alarm = self.database.get_alarm(alarm_id)
        if alarm.status != "reported" or alarm.report_message_timestamp_ms is None:
            self.database.clear_edit_due(alarm.id)
            return
        if not alarm.missing_members:
            await self._complete_alarm(alarm.id, now_ms)
            return
        text = report_for_alarm(alarm)
        if text == alarm.last_report_text:
            self.database.clear_edit_due(alarm.id)
            return
        key = self.database.alarm_operation_key(
            alarm.id, f"report_edit:{alarm.reaction_revision}"
        )
        self.database.prepare_outgoing(
            operation_key=key,
            alarm_id=alarm.id,
            kind="report_edit",
            state="due_not_attempted",
            message=text,
            target_timestamp_ms=alarm.report_message_timestamp_ms,
            now_ms=now_ms,
        )

        async def edit() -> None:
            await self.publisher.edit(text, alarm.report_message_timestamp_ms)  # type: ignore[arg-type]

        result = await attempt_outgoing_once(
            self.database,
            key,
            now_ms=now_ms,
            write=edit,
        )
        if result.state == "attempted_success":
            self.database.record_successful_edit(alarm.id, text)
        else:
            self.database.clear_edit_due(alarm.id)
        self._log_attempt("report edit", alarm, result)

    async def _complete_alarm(self, alarm_id: int, now_ms: int) -> None:
        alarm = self.database.get_alarm(alarm_id)
        if not alarm.is_active or alarm.missing_members:
            return
        text = completion_report_for_alarm(alarm, now_ms)
        if alarm.status == "reported" and alarm.report_message_timestamp_ms is not None:
            kind = "completion_edit"
            key = self.database.alarm_operation_key(alarm.id, kind)
            self.database.prepare_outgoing(
                operation_key=key,
                alarm_id=alarm.id,
                kind=kind,
                state="due_not_attempted",
                message=text,
                target_timestamp_ms=alarm.report_message_timestamp_ms,
                now_ms=now_ms,
            )

            async def edit() -> None:
                await self.publisher.edit(text, alarm.report_message_timestamp_ms)  # type: ignore[arg-type]

            result = await attempt_outgoing_once(
                self.database,
                key,
                now_ms=now_ms,
                write=edit,
            )
        else:
            kind = "completion"
            key = self.database.alarm_operation_key(alarm.id, kind)
            final_operation = self.database.get_outgoing(
                self.database.alarm_operation_key(alarm.id, "final")
            )
            if (
                alarm.status == "reported"
                and final_operation is not None
                and final_operation.state == "attempted_uncertain"
            ):
                # The final message may already exist, but a transport break left no
                # timestamp that can be edited. A second independent send would risk
                # a duplicate, so completion is local-only in this narrow case.
                self.database.prepare_outgoing(
                    operation_key=key,
                    alarm_id=alarm.id,
                    kind=kind,
                    state="skipped",
                    message=text,
                    now_ms=now_ms,
                )
                result = OutgoingAttemptResult("skipped", attempted_now=False)
                self.database.mark_completed(alarm.id, now_ms=now_ms)
                LOGGER.warning(
                    "Completed alert %s locally because final delivery was uncertain; "
                    "no completion send was attempted",
                    alarm.trigger_timestamp_ms,
                )
                return
            self.database.prepare_outgoing(
                operation_key=key,
                alarm_id=alarm.id,
                kind=kind,
                state="due_not_attempted",
                message=text,
                now_ms=now_ms,
            )
            result = await attempt_outgoing_once(
                self.database,
                key,
                now_ms=now_ms,
                write=lambda: self.publisher.send(text),
            )
        self.database.mark_completed(alarm.id, now_ms=now_ms)
        self._log_attempt(kind, alarm, result)
        LOGGER.info("Completed alert %s", alarm.trigger_timestamp_ms)

    async def _force_alarm(
        self, alarm: AlarmSession, now_ms: int, *, created: bool
    ) -> ForceCheckResult:
        alarm = self.database.get_alarm(alarm.id)
        if not alarm.is_active:
            return ForceCheckResult("terminal", alarm, created=created)
        if not alarm.missing_members:
            await self._complete_alarm(alarm.id, now_ms)
            return ForceCheckResult(
                "completed", self.database.get_alarm(alarm.id), created=created
            )
        if alarm.status == "pending" and now_ms >= alarm.deadline_timestamp_ms:
            await self._process_final(alarm.id, now_ms)
            refreshed = self.database.get_alarm(alarm.id)
            final = self.database.get_outgoing(
                self.database.alarm_operation_key(alarm.id, "final")
            )
            return ForceCheckResult(
                "evaluated",
                refreshed,
                created=created,
                outgoing_state=final.state if final is not None else None,
            )

        text = current_report_for_alarm(alarm)
        key = self.database.alarm_operation_key(alarm.id, "manual_report")
        self.database.prepare_outgoing(
            operation_key=key,
            alarm_id=alarm.id,
            kind="manual_report",
            state="due_not_attempted",
            message=text,
            now_ms=now_ms,
        )
        result = await attempt_outgoing_once(
            self.database,
            key,
            now_ms=now_ms,
            write=lambda: self.publisher.send(text),
        )
        self._log_attempt("manual report", alarm, result)
        return ForceCheckResult(
            "evaluated",
            self.database.get_alarm(alarm.id),
            created=created,
            outgoing_state=result.state,
        )

    @staticmethod
    def _log_attempt(
        operation: str, alarm: AlarmSession, result: OutgoingAttemptResult
    ) -> None:
        if not result.attempted_now and result.state != "skipped":
            return
        if result.state == "attempted_success":
            LOGGER.info("Sent %s for alert %s", operation, alarm.trigger_timestamp_ms)
        elif result.state == "attempted_failed":
            LOGGER.error(
                "%s for alert %s failed explicitly and will not be retried: %s",
                operation,
                alarm.trigger_timestamp_ms,
                result.error,
            )
        elif result.state == "attempted_uncertain":
            LOGGER.critical(
                "%s for alert %s has uncertain delivery and will not be retried: %s",
                operation,
                alarm.trigger_timestamp_ms,
                result.error,
            )
        elif result.state == "skipped":
            LOGGER.error(
                "%s for alert %s was blocked before RPC: %s",
                operation,
                alarm.trigger_timestamp_ms,
                result.error,
            )
