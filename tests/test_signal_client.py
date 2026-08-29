from __future__ import annotations

import asyncio

from sheltercheck.signal_client import SignalClient, _iter_sse_json


class RecordingClient(SignalClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8080")
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def rpc(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        return {"timestamp": 1_700_000_000_000, "results": []}


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


def test_send_and_edit_use_upstream_json_rpc_parameter_names() -> None:
    async def exercise() -> None:
        client = RecordingClient()
        timestamp = await client.send_group_message("case/Sensitive=", "report")
        await client.edit_group_message("case/Sensitive=", "updated", timestamp)
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
        ]

    asyncio.run(exercise())


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
