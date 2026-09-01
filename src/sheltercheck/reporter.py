from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import Protocol

from .models import AlarmSession, Member
from .signal_client import SignalClient


def format_duration(total_seconds: int) -> str:
    """Compact Ukrainian duration used by completion, status, and uptime."""

    total_seconds = max(0, int(total_seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        parts = [f"{days} д"]
        if hours:
            parts.append(f"{hours} год")
        if minutes:
            parts.append(f"{minutes} хв")
        return " ".join(parts)
    if hours:
        parts = [f"{hours} год"]
        if minutes:
            parts.append(f"{minutes} хв")
        return " ".join(parts)
    if minutes:
        parts = [f"{minutes} хв"]
        if seconds:
            parts.append(f"{seconds} с")
        return " ".join(parts)
    return f"{seconds} с"


def _check_time(trigger_timestamp_ms: int, timezone: tzinfo | None) -> str:
    return datetime.fromtimestamp(
        trigger_timestamp_ms / 1000, tz=timezone
    ).astimezone(timezone).strftime("%H:%M")


def format_report(
    trigger_timestamp_ms: int,
    released_members: Sequence[Member],
    initial_missing: Sequence[Member],
    responded_acis: frozenset[str] | set[str],
    *,
    timezone: tzinfo | None = None,
) -> str:
    check_time = _check_time(trigger_timestamp_ms, timezone)
    missing = [
        member for member in released_members if member.signal_aci not in responded_acis
    ]
    total = len(released_members)
    detail_lines = [
        f"{'🟢' if member.signal_aci in responded_acis else '🔴'} | "
        f"{member.display_name} | {member.phone}"
        for member in initial_missing
    ]
    if not missing:
        lines = [f"Перевірка {check_time}", "", *detail_lines]
        if detail_lines:
            lines.append("")
        lines.append(f"Усі {total}/{total} поставили +.")
        return "\n".join(lines)

    lines = [
        f"Не поставили + на перевірку {check_time} — {len(missing)}/{total}",
        "",
        *detail_lines,
    ]
    return "\n".join(lines)


class ReportPublisher(Protocol):
    async def send(self, message: str) -> int: ...

    async def edit(self, message: str, edit_timestamp_ms: int) -> None: ...


class SignalReportPublisher:
    def __init__(self, client: SignalClient, report_group_id: str) -> None:
        self._client = client
        self._group_id = report_group_id

    async def send(self, message: str) -> int:
        return await self._client.send_group_message(self._group_id, message)

    async def edit(self, message: str, edit_timestamp_ms: int) -> None:
        await self._client.edit_group_message(
            self._group_id, message, edit_timestamp_ms
        )


class DryRunReportPublisher:
    def __init__(self) -> None:
        self._last_timestamp = 0

    async def send(self, message: str) -> int:
        print("\n[DRY RUN] Would send report:\n" + message + "\n", flush=True)
        now = time.time_ns() // 1_000_000
        self._last_timestamp = max(now, self._last_timestamp + 1)
        return self._last_timestamp

    async def edit(self, message: str, edit_timestamp_ms: int) -> None:
        print(
            f"\n[DRY RUN] Would edit report {edit_timestamp_ms}:\n{message}\n",
            flush=True,
        )


def report_for_alarm(alarm: AlarmSession, *, timezone: tzinfo | None = None) -> str:
    return format_report(
        alarm.trigger_timestamp_ms,
        alarm.released_members,
        alarm.initial_missing,
        alarm.responded_acis,
        timezone=timezone,
    )


def current_report_for_alarm(
    alarm: AlarmSession, *, timezone: tzinfo | None = None
) -> str:
    return format_report(
        alarm.trigger_timestamp_ms,
        alarm.released_members,
        alarm.missing_members,
        alarm.responded_acis,
        timezone=timezone,
    )


def intermediate_report_for_alarm(
    alarm: AlarmSession, *, timezone: tzinfo | None = None
) -> str:
    missing = alarm.missing_members
    responded = len(alarm.released_members) - len(missing)
    lines = [
        f"Проміжна перевірка {_check_time(alarm.trigger_timestamp_ms, timezone)}",
        "",
        f"Відмітилися: {responded}/{len(alarm.released_members)}",
        f"Очікуються: {len(missing)}",
    ]
    if missing:
        lines.extend(
            [
                "",
                *(
                    f"{position}. {member.display_name} | {member.phone}"
                    for position, member in enumerate(missing, 1)
                ),
            ]
        )
    return "\n".join(lines)


def completion_report_for_alarm(alarm: AlarmSession, completed_at_ms: int) -> str:
    total = len(alarm.released_members)
    elapsed_seconds = max(
        0, (completed_at_ms - alarm.tracking_started_at_ms) // 1000
    )
    return (
        f"✅ Усі {total}/{total} відмітилися.\n"
        f"Перевірку завершено за {format_duration(elapsed_seconds)}."
    )
