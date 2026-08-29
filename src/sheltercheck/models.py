from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Member:
    signal_aci: str
    display_name: str
    phone: str


@dataclass(frozen=True, slots=True)
class MessageEvent:
    group_id: str
    sender_aci: str
    sent_timestamp_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    group_id: str
    reactor_aci: str
    target_author_aci: str | None
    target_timestamp_ms: int
    emoji: str
    removed: bool
    sent_timestamp_ms: int


NormalizedEvent = MessageEvent | ReactionEvent


@dataclass(frozen=True, slots=True)
class AlarmSession:
    id: int
    group_id: str
    trigger_author_aci: str
    trigger_timestamp_ms: int
    deadline_timestamp_ms: int
    status: str
    initial_report_state: int
    report_message_timestamp_ms: int | None
    last_report_text: str | None
    edit_due_timestamp_ms: int | None
    released_members: tuple[Member, ...]
    initial_missing: tuple[Member, ...]
    responded_acis: frozenset[str]

    @property
    def missing_members(self) -> tuple[Member, ...]:
        return tuple(
            member
            for member in self.released_members
            if member.signal_aci not in self.responded_acis
        )
