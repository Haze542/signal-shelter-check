from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .config import Config, validate_loopback_url


LOGGER = logging.getLogger(__name__)


class SignalClientError(RuntimeError):
    """The local signal-cli HTTP daemon could not complete an operation."""


class SignalRPCError(SignalClientError):
    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        super().__init__(f"signal-cli JSON-RPC error {code}: {message}")


class SignalClient:
    """Direct async client for signal-cli's upstream HTTP JSON-RPC daemon."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_seconds: float = 5.0,
        request_timeout_seconds: float = 30.0,
        sse_read_timeout_seconds: float = 90.0,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self.base_url = validate_loopback_url(base_url)
        self.connect_timeout_seconds = connect_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.sse_read_timeout_seconds = sse_read_timeout_seconds
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> SignalClient:
        if self._session is not None:
            raise RuntimeError("SignalClient is already open")
        timeout = aiohttp.ClientTimeout(
            total=self.request_timeout_seconds,
            connect=self.connect_timeout_seconds,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("SignalClient must be used as an async context manager")
        return self._session

    async def check_health(self) -> None:
        session = self._require_session()
        try:
            async with session.get(f"{self.base_url}/api/v1/check") as response:
                if response.status != 200:
                    raise SignalClientError(
                        f"signal-cli health check returned HTTP {response.status}"
                    )
                await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise SignalClientError(f"signal-cli health check failed: {exc}") from exc

    async def rpc(self, method: str, params: dict[str, Any]) -> Any:
        session = self._require_session()
        request_id = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id,
        }
        try:
            async with session.post(
                f"{self.base_url}/api/v1/rpc", json=request
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise SignalClientError(
                        f"signal-cli JSON-RPC endpoint returned HTTP {response.status}"
                    )
                try:
                    payload = await response.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError, UnicodeError) as exc:
                    raise SignalClientError("signal-cli returned invalid JSON") from exc
        except SignalClientError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise SignalClientError(f"signal-cli JSON-RPC request failed: {exc}") from exc

        if not isinstance(payload, dict):
            raise SignalClientError("signal-cli returned a non-object JSON-RPC response")
        if payload.get("id") != request_id:
            raise SignalClientError("signal-cli returned a mismatched JSON-RPC response id")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code") if isinstance(error.get("code"), int) else None
            message = error.get("message")
            raise SignalRPCError(code, message if isinstance(message, str) else "unknown error")
        if "result" not in payload:
            raise SignalClientError("signal-cli JSON-RPC response has no result")
        return payload["result"]

    async def send_group_message(self, group_id: str, message: str) -> int:
        result = await self.rpc("send", {"groupId": group_id, "message": message})
        return _result_timestamp(result)

    async def edit_group_message(
        self, group_id: str, message: str, edit_timestamp_ms: int
    ) -> int:
        result = await self.rpc(
            "send",
            {
                "groupId": group_id,
                "message": message,
                "editTimestamp": edit_timestamp_ms,
            },
        )
        return _result_timestamp(result)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded SSE data objects, reconnecting forever with bounded backoff."""

        session = self._require_session()
        backoff = self.reconnect_initial_seconds
        while True:
            received_any = False
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=self.connect_timeout_seconds,
                sock_read=self.sse_read_timeout_seconds,
            )
            try:
                async with session.get(
                    f"{self.base_url}/api/v1/events",
                    headers={"Accept": "text/event-stream"},
                    timeout=timeout,
                ) as response:
                    if response.status != 200:
                        raise SignalClientError(
                            f"signal-cli event stream returned HTTP {response.status}"
                        )
                    LOGGER.info("Signal event stream connected")
                    async for payload in _iter_sse_json(response):
                        received_any = True
                        backoff = self.reconnect_initial_seconds
                        yield payload
                    raise SignalClientError("signal-cli event stream closed")
            except asyncio.CancelledError:
                raise
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                SignalClientError,
                UnicodeError,
            ) as exc:
                LOGGER.warning(
                    "Signal event stream disconnected (%s); reconnecting in %.1fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                if not received_any:
                    backoff = min(backoff * 2, self.reconnect_max_seconds)


def signal_client_from_config(config: Config) -> SignalClient:
    return SignalClient(
        config.signal_cli_url,
        connect_timeout_seconds=config.connect_timeout_seconds,
        request_timeout_seconds=config.request_timeout_seconds,
        sse_read_timeout_seconds=config.sse_read_timeout_seconds,
        reconnect_initial_seconds=config.reconnect_initial_seconds,
        reconnect_max_seconds=config.reconnect_max_seconds,
    )


async def _iter_sse_json(
    response: aiohttp.ClientResponse,
) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for raw_line in response.content:
        line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
        if not line:
            if data_lines:
                text = "\n".join(data_lines)
                data_lines.clear()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    LOGGER.warning("Ignoring malformed JSON in Signal event stream")
                    continue
                if isinstance(payload, dict):
                    yield payload
                else:
                    LOGGER.warning("Ignoring non-object JSON in Signal event stream")
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            if separator and value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring malformed final JSON event in Signal event stream")
        else:
            if isinstance(payload, dict):
                yield payload


def _result_timestamp(result: Any) -> int:
    timestamp: Any
    if isinstance(result, dict):
        timestamp = result.get("timestamp")
    else:
        timestamp = result
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise SignalClientError("signal-cli send result has no valid timestamp")
    return timestamp
