from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from .models import MessageEvent, NormalizedEvent, ReactionEvent


LOGGER = logging.getLogger(__name__)

# Compatibility boundary:
# - JsonDataMessage and JsonSyncDataMessage fields below are documented by upstream
#   signal-cli and represented by synthetic fixtures in tests.
# - Do not add alternate reaction/sync nesting based on guesses. Capture it with
#   tools/dump_events.py, redact it, then add a fixture before extending this parser.


def parse_receive_notification(payload: dict[str, Any]) -> tuple[NormalizedEvent, ...]:
    envelope = _extract_envelope(payload)
    if envelope is None:
        return ()

    sender_aci = _uuid_string(envelope.get("sourceUuid"))
    if sender_aci is None:
        return ()

    normalized: list[NormalizedEvent] = []
    data_message = envelope.get("dataMessage")
    if isinstance(data_message, dict):
        normalized.extend(_parse_data_message(envelope, data_message, sender_aci))

    sync_message = envelope.get("syncMessage")
    if isinstance(sync_message, dict):
        sent_message = sync_message.get("sentMessage")
        if isinstance(sent_message, dict):
            normalized.extend(_parse_data_message(envelope, sent_message, sender_aci))
    return tuple(normalized)


def _extract_envelope(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("method") == "receive":
        params = payload.get("params")
        if not isinstance(params, dict):
            return None
        result = params.get("result")
        container = result if isinstance(result, dict) else params
        envelope = container.get("envelope")
        return envelope if isinstance(envelope, dict) else None

    # signal-cli's non-daemon JSON output and deliberately trimmed test fixtures.
    envelope = payload.get("envelope")
    return envelope if isinstance(envelope, dict) else None


def _parse_data_message(
    envelope: dict[str, Any], data: dict[str, Any], sender_aci: str
) -> list[NormalizedEvent]:
    group_info = data.get("groupInfo")
    if not isinstance(group_info, dict):
        return []
    group_id = group_info.get("groupId")
    if not isinstance(group_id, str) or not group_id:
        return []

    sent_timestamp = _positive_int(data.get("timestamp"))
    if sent_timestamp is None:
        sent_timestamp = _positive_int(envelope.get("timestamp"))
    if sent_timestamp is None:
        return []

    events: list[NormalizedEvent] = []
    reaction = data.get("reaction")
    if isinstance(reaction, dict):
        target_timestamp = _positive_int(reaction.get("targetSentTimestamp"))
        emoji = reaction.get("emoji")
        removed = reaction.get("isRemove")
        if (
            target_timestamp is not None
            and isinstance(emoji, str)
            and emoji
            and isinstance(removed, bool)
        ):
            events.append(
                ReactionEvent(
                    group_id=group_id,
                    reactor_aci=sender_aci,
                    target_author_aci=_uuid_string(reaction.get("targetAuthorUuid")),
                    target_timestamp_ms=target_timestamp,
                    emoji=emoji,
                    removed=removed,
                    sent_timestamp_ms=sent_timestamp,
                )
            )

    text = data.get("message")
    if isinstance(text, str):
        events.append(
            MessageEvent(
                group_id=group_id,
                sender_aci=sender_aci,
                sent_timestamp_ms=sent_timestamp,
                text=text,
            )
        )
    return events


def _uuid_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        LOGGER.debug("Ignoring Signal event with an invalid ACI UUID field")
        return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value

