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
    tracking_started_at_ms: int
    intermediate_deadline_timestamp_ms: int
    deadline_timestamp_ms: int
    expires_timestamp_ms: int
    source: str
    status: str
    initial_report_state: int
    report_message_timestamp_ms: int | None
    last_report_text: str | None
    edit_due_timestamp_ms: int | None
    reaction_revision: int
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

    @property
    def is_active(self) -> bool:
        return self.status in {"pending", "reported"}


@dataclass(frozen=True, slots=True)
class ObservedMessage:
    group_id: str
    sender_aci: str
    sent_timestamp_ms: int
    original_text: str
    normalized_text: str


@dataclass(frozen=True, slots=True)
class OutgoingOperation:
    operation_key: str
    alarm_id: int | None
    kind: str
    state: str
    message: str | None
    target_timestamp_ms: int | None
    result_timestamp_ms: int | None
    attempt_count: int
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class ForceCheckResult:
    outcome: str
    alarm: AlarmSession | None = None
    created: bool = False
    outgoing_state: str | None = None
