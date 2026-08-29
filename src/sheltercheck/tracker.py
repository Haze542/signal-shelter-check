from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import Config, normalize_text
from .database import StateDatabase
from .models import MessageEvent, NormalizedEvent, ReactionEvent
from .reporter import ReportPublisher, report_for_alarm
from .released_list import ReleasedListError, ReleasedListService
from .roster import Roster, RosterError
from .signal_client import SignalRPCError


LOGGER = logging.getLogger(__name__)
_RPC_RETRY_MS = 5_000


class AlertTracker:
    """Persistent alert state machine, independent of raw Signal JSON and HTTP."""

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
        self._initial_retry_after: dict[int, int] = {}

    async def handle_event(self, event: NormalizedEvent) -> None:
        async with self._lock:
            if isinstance(event, MessageEvent):
                await self._handle_message(event)
            elif isinstance(event, ReactionEvent):
                await self._handle_reaction(event)

    async def process_due(self, now_ms: int | None = None) -> None:
        async with self._lock:
            await self._process_due_locked(self._clock_ms() if now_ms is None else now_ms)

    async def _handle_message(self, event: MessageEvent) -> None:
        if event.group_id != self.config.monitor_group_id:
            return
        if (
            self.config.trigger_author_uuids
            and event.sender_aci not in self.config.trigger_author_uuids
        ):
            return
        if normalize_text(event.text) not in self.config.trigger_texts:
            return

        existing = self.database.get_alarm_by_identity(
            event.group_id, event.sender_aci, event.sent_timestamp_ms
        )
        if existing is not None:
            return

        deadline = event.sent_timestamp_ms + self.config.wait_milliseconds
        now_ms = self._clock_ms()
        try:
            released = await self.released_list.get_members(self.roster)
        except (ReleasedListError, RosterError) as exc:
            self.database.create_alarm(
                group_id=event.group_id,
                trigger_author_aci=event.sender_aci,
                trigger_timestamp_ms=event.sent_timestamp_ms,
                deadline_timestamp_ms=deadline,
                released_members=(),
                status="error",
                error_detail=str(exc),
                created_at_ms=now_ms,
            )
            LOGGER.error(
                "Alert %s cannot produce an authoritative report: released list is invalid: %s",
                event.sent_timestamp_ms,
                exc,
            )
            return

        status = "stale" if now_ms > deadline else "pending"
        alarm = self.database.create_alarm(
            group_id=event.group_id,
            trigger_author_aci=event.sender_aci,
            trigger_timestamp_ms=event.sent_timestamp_ms,
            deadline_timestamp_ms=deadline,
            released_members=released,
            status=status,
            created_at_ms=now_ms,
        )
        if alarm is None:
            return
        if status == "stale":
            LOGGER.error(
                "Skipping stale alert %s: its deadline passed before the trigger was processed",
                event.sent_timestamp_ms,
            )
        else:
            LOGGER.info(
                "Created alert %s with %d released members",
                event.sent_timestamp_ms,
                len(released),
            )

    async def _handle_reaction(self, event: ReactionEvent) -> None:
        if event.emoji not in self.config.accepted_reactions:
            return
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
        for target in targets:
            # Preserve the deadline boundary if event-loop scheduling delayed both the
            # report tick and this reaction. Timely reactions enter the initial report;
            # reactions sent after the deadline first produce, then edit, that report.
            if (
                target.status == "pending"
                and target.deadline_timestamp_ms <= now_ms
                and event.sent_timestamp_ms > target.deadline_timestamp_ms
            ):
                await self._send_initial(target.id, now_ms)

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
            if changed:
                LOGGER.info(
                    "Alert %s response state changed for roster ACI",
                    target.trigger_timestamp_ms,
                )

            refreshed = self.database.get_alarm(target.id)
            if (
                refreshed.status == "pending"
                and refreshed.deadline_timestamp_ms <= now_ms
                and event.sent_timestamp_ms <= refreshed.deadline_timestamp_ms
            ):
                await self._send_initial(refreshed.id, now_ms)

    async def _process_due_locked(self, now_ms: int) -> None:
        for alarm in self.database.list_due_initial_reports(now_ms):
            await self._send_initial(alarm.id, now_ms)

        for alarm in self.database.list_due_edits(now_ms):
            refreshed = self.database.get_alarm(alarm.id)
            text = report_for_alarm(refreshed)
            if text == refreshed.last_report_text:
                self.database.complete_edit(
                    refreshed.id, text, len(refreshed.missing_members)
                )
                continue
            if refreshed.report_message_timestamp_ms is None:
                LOGGER.error(
                    "Alert %s has no report timestamp; edit cannot be sent",
                    refreshed.trigger_timestamp_ms,
                )
                self.database.defer_edit(refreshed.id, now_ms + _RPC_RETRY_MS)
                continue
            try:
                await self.publisher.edit(text, refreshed.report_message_timestamp_ms)
            except Exception as exc:  # edits are idempotent and safe to retry
                LOGGER.error(
                    "Could not edit report for alert %s; retrying later: %s",
                    refreshed.trigger_timestamp_ms,
                    exc,
                )
                self.database.defer_edit(refreshed.id, now_ms + _RPC_RETRY_MS)
                continue
            self.database.complete_edit(
                refreshed.id, text, len(refreshed.missing_members)
            )
            LOGGER.info("Edited report for alert %s", refreshed.trigger_timestamp_ms)

    async def _send_initial(self, alarm_id: int, now_ms: int) -> None:
        if self._initial_retry_after.get(alarm_id, 0) > now_ms:
            return
        alarm = self.database.get_alarm(alarm_id)
        if alarm.status != "pending" or alarm.initial_report_state != 0:
            return
        alarm = self.database.snapshot_initial_missing(alarm.id)
        text = report_for_alarm(alarm)
        if not self.database.begin_initial_report(alarm.id, text):
            return
        try:
            report_timestamp = await self.publisher.send(text)
        except SignalRPCError as exc:
            # A complete JSON-RPC rejection is known not to be a successful send.
            self.database.release_initial_report(alarm.id)
            self._initial_retry_after[alarm.id] = now_ms + _RPC_RETRY_MS
            LOGGER.error(
                "Signal rejected initial report for alert %s; retrying later: %s",
                alarm.trigger_timestamp_ms,
                exc,
            )
            return
        except Exception as exc:
            # HTTP disconnects/timeouts are ambiguous: the daemon might have accepted
            # the message. Keep the durable claim so restart cannot duplicate it.
            LOGGER.critical(
                "Initial report send for alert %s is uncertain; refusing automatic retry to avoid a duplicate: %s",
                alarm.trigger_timestamp_ms,
                exc,
            )
            return

        self.database.complete_initial_report(
            alarm.id,
            report_timestamp_ms=report_timestamp,
            report_text=text,
            missing_count=len(alarm.missing_members),
        )
        self._initial_retry_after.pop(alarm.id, None)
        LOGGER.info("Sent initial report for alert %s", alarm.trigger_timestamp_ms)
