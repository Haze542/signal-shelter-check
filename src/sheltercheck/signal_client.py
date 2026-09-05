from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .config import Config, validate_loopback_url


LOGGER = logging.getLogger(__name__)


class SignalClientError(RuntimeError):
    """The local signal-cli HTTP daemon could not complete an operation."""


class SignalRPCError(SignalClientError):
    def __init__(self, code: int | None, message: str, *, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"signal-cli JSON-RPC error {code}: {message}")


class SignalRateLimitedError(SignalClientError):
    def __init__(
        self,
        failure_type: str,
        *,
        retry_after_seconds: int | None = None,
        token: str | None = None,
    ) -> None:
        self.failure_type = failure_type
        self.retry_after_seconds = retry_after_seconds
        self.token = token
        details = [failure_type]
        if retry_after_seconds is not None:
            details.append(f"retryAfterSeconds={retry_after_seconds}")
        if token is not None:
            details.append("proof token present")
        super().__init__(
            "signal-cli outgoing rate limit/proof challenge: " + ", ".join(details)
        )


class SignalOutgoingDisabledError(SignalClientError):
    """A write was blocked locally after the process-wide client breaker tripped."""


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
        self._outgoing_disabled = False
        self._rate_limit_failure: SignalRateLimitedError | None = None
        self._outgoing_lock = asyncio.Lock()

    @property
    def outgoing_enabled(self) -> bool:
        return not self._outgoing_disabled

    @property
    def rate_limit_failure(self) -> SignalRateLimitedError | None:
        return self._rate_limit_failure

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
        """Require both the HTTP daemon and one usable loaded account."""

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

        accounts = await self.list_accounts()
        if not accounts:
            raise SignalClientError(
                "signal-cli readiness check failed: no accounts loaded"
            )
        if len(accounts) != 1:
            raise SignalClientError(
                "signal-cli readiness check failed: this deployment requires "
                "exactly one loaded account"
            )

    async def list_accounts(self) -> tuple[str, ...]:
        result = await self.rpc("listAccounts", {})
        if not isinstance(result, list):
            raise SignalClientError("signal-cli listAccounts result is not an array")

        accounts: list[str] = []
        for entry in result:
            if isinstance(entry, str):
                account = entry.strip()
            elif isinstance(entry, dict):
                number = entry.get("number")
                account = number.strip() if isinstance(number, str) else ""
            else:
                account = ""
            if not account:
                raise SignalClientError(
                    "signal-cli listAccounts returned an invalid account entry"
                )
            accounts.append(account)
        return tuple(accounts)

    async def rpc(self, method: str, params: dict[str, Any]) -> Any:
        if method in {"send", "sendReaction"}:
            self._ensure_outgoing_enabled()
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
            rate_limit = _rate_limit_from_value(error)
            if rate_limit is not None:
                self._trip_outgoing(rate_limit)
                raise rate_limit
            code = error.get("code") if isinstance(error.get("code"), int) else None
            message = error.get("message")
            raise SignalRPCError(
                code,
                message if isinstance(message, str) else "unknown error",
                data=error.get("data"),
            )
        if "result" not in payload:
            raise SignalClientError("signal-cli JSON-RPC response has no result")
        return payload["result"]

    async def send_group_message(self, group_id: str, message: str) -> int:
        async with self._outgoing_lock:
            self._ensure_outgoing_enabled()
            result = await self.rpc(
                "send", {"groupId": group_id, "message": message}
            )
            return self._validate_send_result(result)

    async def edit_group_message(
        self, group_id: str, message: str, edit_timestamp_ms: int
    ) -> int:
        async with self._outgoing_lock:
            self._ensure_outgoing_enabled()
            result = await self.rpc(
                "send",
                {
                    "groupId": group_id,
                    "message": message,
                    "editTimestamp": edit_timestamp_ms,
                },
            )
            return self._validate_send_result(result)

    async def send_group_reaction(
        self,
        group_id: str,
        target_author_aci: str,
        target_timestamp_ms: int,
        emoji: str,
    ) -> int:
        async with self._outgoing_lock:
            self._ensure_outgoing_enabled()
            result = await self.rpc(
                "sendReaction",
                {
                    "groupId": group_id,
                    "emoji": emoji,
                    "targetAuthor": target_author_aci,
                    "targetTimestamp": target_timestamp_ms,
                },
            )
            return self._validate_send_result(result)

    def _ensure_outgoing_enabled(self) -> None:
        if self._outgoing_disabled:
            raise SignalOutgoingDisabledError(
                "Signal outgoing writes are disabled until process restart"
            )

    def _validate_send_result(self, result: Any) -> int:
        rate_limit = _rate_limit_from_value(result)
        if rate_limit is not None:
            self._trip_outgoing(rate_limit)
            raise rate_limit

        if isinstance(result, dict) and "results" in result:
            results = result["results"]
            if not isinstance(results, list):
                raise SignalClientError("signal-cli send results field is not an array")
            for entry in results:
                if not isinstance(entry, dict):
                    raise SignalClientError("signal-cli send result entry is not an object")
                result_type = entry.get("type")
                if isinstance(result_type, str) and (
                    result_type.upper().endswith("_FAILURE")
                    or result_type.upper() in {"FAILURE", "ERROR"}
                ):
                    raise SignalRPCError(
                        None,
                        f"signal-cli send result reported {result_type}",
                        data=entry,
                    )
        return _result_timestamp(result)

    def _trip_outgoing(self, failure: SignalRateLimitedError) -> None:
        if self._outgoing_disabled:
            return
        self._outgoing_disabled = True
        self._rate_limit_failure = failure
        LOGGER.critical(
            "Signal outgoing disabled at timestamp=%d after %s; retryAfterSeconds=%s; "
            "breaker remains latched until process restart",
            time.time_ns() // 1_000_000,
            failure.failure_type,
            failure.retry_after_seconds,
        )

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


def _rate_limit_from_value(value: Any) -> SignalRateLimitedError | None:
    """Find explicit rate-limit/proof-required evidence in a send result or error."""

    if isinstance(value, dict):
        raw_type = value.get("type")
        failure_type = raw_type if isinstance(raw_type, str) else ""
        upper_type = failure_type.upper()
        token_value = value.get("token")
        token = token_value if isinstance(token_value, str) and token_value else None
        retry_value = value.get("retryAfterSeconds")
        retry_after = (
            retry_value
            if isinstance(retry_value, int) and not isinstance(retry_value, bool)
            else None
        )
        proof_key_present = any(
            key in value and value[key] is not None
            for key in ("proofRequiredFailure", "proofRequired", "challenge")
        )
        if (
            upper_type == "RATE_LIMIT_FAILURE"
            or "PROOF_REQUIRED" in upper_type
            or token is not None
            or proof_key_present
        ):
            return SignalRateLimitedError(
                failure_type or "PROOF_REQUIRED_CHALLENGE",
                retry_after_seconds=retry_after,
                token=token,
            )
        for nested in value.values():
            found = _rate_limit_from_value(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _rate_limit_from_value(nested)
            if found is not None:
                return found
    return None
