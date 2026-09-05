from __future__ import annotations

import json
from pathlib import Path

from sheltercheck.event_parser import parse_receive_notification
from sheltercheck.models import MessageEvent, ReactionEvent


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parses_incoming_group_message() -> None:
    events = parse_receive_notification(fixture("incoming_message.json"))
    assert events == (
        MessageEvent(
            group_id="SYNTHETIC_MONITOR_GROUP_ID",
            sender_aci="00000000-0000-4000-8000-000000000010",
            sent_timestamp_ms=1_700_000_000_000,
            text="Всі в укритті?",
        ),
    )


def test_parses_incoming_add_and_removal_reactions() -> None:
    added = parse_receive_notification(fixture("incoming_reaction_add.json"))
    removed = parse_receive_notification(fixture("incoming_reaction_remove.json"))

    assert isinstance(added[0], ReactionEvent)
    assert added[0].reactor_aci == "00000000-0000-4000-8000-000000000001"
    assert added[0].target_timestamp_ms == 1_700_000_000_000
    assert added[0].emoji == "➕"
    assert added[0].removed is False
    assert isinstance(removed[0], ReactionEvent)
    assert removed[0].removed is True


def test_parses_linked_device_synchronized_reaction() -> None:
    events = parse_receive_notification(fixture("sync_reaction_add.json"))
    assert events == (
        ReactionEvent(
            group_id="SYNTHETIC_MONITOR_GROUP_ID",
            reactor_aci="00000000-0000-4000-8000-000000000003",
            target_author_aci="00000000-0000-4000-8000-000000000010",
            target_timestamp_ms=1_700_000_000_000,
            emoji="➕",
            removed=False,
            sent_timestamp_ms=1_700_000_003_000,
        ),
    )


def test_parses_linked_device_synchronized_group_text() -> None:
    events = parse_receive_notification(fixture("sync_message.json"))
    assert events == (
        MessageEvent(
            group_id="SYNTHETIC_MONITOR_GROUP_ID",
            sender_aci="00000000-0000-4000-8000-000000000010",
            sent_timestamp_ms=1_700_000_004_000,
            text="Всі в укритті?",
        ),
    )


def test_message_timestamp_is_taken_from_data_not_later_envelope_delivery() -> None:
    old_message_timestamp = 1_700_000_000_000
    later_envelope_timestamp = 1_700_014_400_000
    events = parse_receive_notification(
        {
            "method": "receive",
            "params": {
                "result": {
                    "envelope": {
                        "sourceUuid": "00000000-0000-4000-8000-000000000010",
                        "timestamp": later_envelope_timestamp,
                        "dataMessage": {
                            "timestamp": old_message_timestamp,
                            "groupInfo": {"groupId": "SYNTHETIC_MONITOR_GROUP_ID"},
                            "message": "Всі в укритті?",
                        },
                    }
                }
            },
        }
    )

    assert isinstance(events[0], MessageEvent)
    assert events[0].sent_timestamp_ms == old_message_timestamp


def test_ignores_unknown_or_non_receive_payloads() -> None:
    assert parse_receive_notification({"jsonrpc": "2.0", "method": "typing"}) == ()
    assert (
        parse_receive_notification(
            {"method": "receive", "params": {"envelope": {"sourceUuid": "bad"}}}
        )
        == ()
    )
