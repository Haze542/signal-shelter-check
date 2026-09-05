from __future__ import annotations

import asyncio
import logging

import pytest

from sheltercheck.commands import SignalCommandReplyPublisher
from sheltercheck.database import StateDatabase
from sheltercheck.reporter import SignalReportPublisher
from sheltercheck.signal_client import (
    SignalClient,
    SignalClientError,
    SignalOutgoingDisabledError,
    SignalRPCError,
    SignalRateLimitedError,
    _iter_sse_json,
)
from sheltercheck.tracker import AlertTracker

from conftest import ACI_1
from test_tracker import Clock, TRIGGER_TS, reaction, trigger


class RecordingClient(SignalClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8080")
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def rpc(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        return {"timestamp": 1_700_000_000_000, "results": []}


class ResultClient(SignalClient):
    def __init__(self, result: object) -> None:
        super().__init__("http://127.0.0.1:8080")
        self.result = result
        self.calls = 0

    async def rpc(self, method: str, params: dict[str, object]) -> object:
        self.calls += 1
        return self.result


class NetworkFailureClient(SignalClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8080")
        self.calls = 0

    async def rpc(self, method: str, params: dict[str, object]) -> object:
        self.calls += 1
        raise SignalClientError("connection reset")


class AsyncLines:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.content = AsyncLines(lines)


class HealthResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def read(self) -> bytes:
        return b"ok"


class HealthSession:
    def get(self, url: str) -> HealthResponse:
        return HealthResponse()


class HealthClient(SignalClient):
    def __init__(self, accounts: tuple[str, ...]) -> None:
        super().__init__("http://127.0.0.1:8080")
        self.accounts = accounts
        self._session = HealthSession()  # type: ignore[assignment]

    async def list_accounts(self) -> tuple[str, ...]:
        return self.accounts


def test_send_and_edit_use_upstream_json_rpc_parameter_names() -> None:
    async def exercise() -> None:
        client = RecordingClient()
        timestamp = await client.send_group_message("case/Sensitive=", "report")
        await client.edit_group_message("case/Sensitive=", "updated", timestamp)
        await client.send_group_reaction(
            "case/Sensitive=",
            "00000000-0000-4000-8000-000000000010",
            1_700_000_000_123,
            "➕",
        )
        assert client.calls == [
            ("send", {"groupId": "case/Sensitive=", "message": "report"}),
            (
                "send",
                {
                    "groupId": "case/Sensitive=",
                    "message": "updated",
                    "editTimestamp": 1_700_000_000_000,
                },
            ),
            (
                "sendReaction",
                {
                    "groupId": "case/Sensitive=",
                    "emoji": "➕",
                    "targetAuthor": "00000000-0000-4000-8000-000000000010",
                    "targetTimestamp": 1_700_000_000_123,
                },
            ),
        ]

    asyncio.run(exercise())


def test_list_accounts_accepts_supported_shapes_and_rejects_empty_readiness() -> None:
    objects = ResultClient([{"number": "+380000000001"}])
    strings = ResultClient(["+380000000001"])
    empty = ResultClient([])

    assert asyncio.run(objects.list_accounts()) == ("+380000000001",)
    assert asyncio.run(strings.list_accounts()) == ("+380000000001",)
    assert asyncio.run(empty.list_accounts()) == ()


def test_health_requires_exactly_one_loaded_account() -> None:
    asyncio.run(HealthClient(("+380000000001",)).check_health())
    with pytest.raises(SignalClientError, match="no accounts loaded"):
        asyncio.run(HealthClient(()).check_health())
    with pytest.raises(SignalClientError, match="exactly one"):
        asyncio.run(
            HealthClient(("+380000000001", "+380000000002")).check_health()
        )


def test_sse_multiline_data_and_comments_are_parsed() -> None:
    async def collect() -> list[dict[str, object]]:
        response = FakeResponse(
            [
                b": keepalive\n",
                b"event: receive\n",
                b'data: {"method":\n',
                b'data: "receive"}\n',
                b"\n",
            ]
        )
        return [event async for event in _iter_sse_json(response)]  # type: ignore[arg-type]

    assert asyncio.run(collect()) == [{"method": "receive"}]


def test_normal_send_result_parses_timestamp_and_keeps_breaker_enabled() -> None:
    client = ResultClient(
        {
            "timestamp": 1_700_000_000_000,
            "results": [{"type": "SUCCESS"}],
        }
    )
    assert asyncio.run(client.send_group_message("group", "message")) == (
        1_700_000_000_000
    )
    assert client.outgoing_enabled is True
    assert client.calls == 1


def test_rate_limit_result_trips_latched_breaker_before_future_rpc(caplog) -> None:
    client = ResultClient(
        {
            "timestamp": 1_700_000_000_000,
            "results": [
                {
                    "type": "RATE_LIMIT_FAILURE",
                    "retryAfterSeconds": 60,
                }
            ],
        }
    )
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SignalRateLimitedError) as caught:
            asyncio.run(client.send_group_message("group", "message"))
    assert caught.value.retry_after_seconds == 60
    assert client.outgoing_enabled is False
    assert client.calls == 1
    assert "outgoing disabled" in caplog.text

    with pytest.raises(SignalOutgoingDisabledError):
        asyncio.run(client.send_group_message("group", "another"))
    with pytest.raises(SignalOutgoingDisabledError):
        asyncio.run(client.edit_group_message("group", "edit", 123))
    reply = SignalCommandReplyPublisher(client)
    with pytest.raises(SignalOutgoingDisabledError):
        asyncio.run(reply.send("commands", "status"))
    assert client.calls == 1
    assert client.outgoing_enabled is False  # retryAfterSeconds never auto-resets it


def test_proof_required_token_trips_same_breaker() -> None:
    client = ResultClient(
        {
            "timestamp": 1_700_000_000_000,
            "results": [
                {
                    "type": "PROOF_REQUIRED_FAILURE",
                    "token": "challenge-token",
                    "retryAfterSeconds": 10,
                }
            ],
        }
    )
    with pytest.raises(SignalRateLimitedError) as caught:
        asyncio.run(client.send_group_message("group", "message"))
    assert caught.value.token == "challenge-token"
    assert client.outgoing_enabled is False
    assert client.calls == 1


def test_concurrent_writes_are_serialized_around_rate_limit_breaker() -> None:
    client = ResultClient(
        {
            "timestamp": 1_700_000_000_000,
            "results": [{"type": "RATE_LIMIT_FAILURE"}],
        }
    )

    async def exercise() -> list[object]:
        return await asyncio.gather(
            client.send_group_message("group", "one"),
            client.edit_group_message("group", "two", 123),
            return_exceptions=True,
        )

    results = asyncio.run(exercise())
    assert isinstance(results[0], SignalRateLimitedError)
    assert isinstance(results[1], SignalOutgoingDisabledError)
    assert client.calls == 1


def test_other_recipient_failure_is_explicit_but_does_not_trip_breaker() -> None:
    client = ResultClient(
        {
            "timestamp": 1_700_000_000_000,
            "results": [{"type": "NETWORK_FAILURE"}],
        }
    )
    with pytest.raises(SignalRPCError):
        asyncio.run(client.send_group_message("group", "message"))
    assert client.outgoing_enabled is True
    assert client.calls == 1


def test_network_failure_does_not_trip_breaker_or_retry() -> None:
    client = NetworkFailureClient()
    with pytest.raises(SignalClientError, match="connection reset"):
        asyncio.run(client.send_group_message("group", "message"))
    assert client.calls == 1
    assert client.outgoing_enabled is True


def test_rate_limit_keeps_tracker_alive_for_incoming_and_local_state(
    app_config, roster
) -> None:
    client = ResultClient(
        {
            "timestamp": 1_700_000_000_000,
            "results": [{"type": "RATE_LIMIT_FAILURE", "retryAfterSeconds": 60}],
        }
    )
    database = StateDatabase(":memory:")
    clock = Clock(TRIGGER_TS)
    tracker = AlertTracker(
        app_config,
        roster,
        database,
        SignalReportPublisher(client, "report-group"),
        clock_ms=clock,
    )
    try:
        asyncio.run(tracker.handle_event(trigger()))
        asyncio.run(tracker.process_due(TRIGGER_TS + 5_000))
        assert client.calls == 1
        assert client.outgoing_enabled is False

        clock.value = TRIGGER_TS + 6_000
        asyncio.run(tracker.handle_event(reaction(ACI_1, sent=clock.value)))
        assert database.list_alarms()[0].responded_acis == frozenset({ACI_1})

        second = TRIGGER_TS + 20_000
        clock.value = second
        asyncio.run(tracker.handle_event(trigger(timestamp=second)))
        asyncio.run(tracker.process_due(second + 5_000))
        assert client.calls == 1  # second report blocked before JSON-RPC
        operation = database.get_outgoing("alarm:2:intermediate")
        assert operation is not None and operation.state == "skipped"
    finally:
        database.close()
