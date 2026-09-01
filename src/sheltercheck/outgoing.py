from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .database import StateDatabase
from .signal_client import (
    SignalOutgoingDisabledError,
    SignalRateLimitedError,
    SignalRPCError,
)


@dataclass(frozen=True, slots=True)
class OutgoingAttemptResult:
    state: str
    result_timestamp_ms: int | None = None
    error: Exception | None = None
    attempted_now: bool = False


async def attempt_outgoing_once(
    database: StateDatabase,
    operation_key: str,
    *,
    now_ms: int,
    write: Callable[[], Awaitable[int | None]],
) -> OutgoingAttemptResult:
    """Perform at most one durable RPC attempt for a logical operation key."""

    if not database.claim_outgoing(operation_key, now_ms=now_ms):
        existing = database.get_outgoing(operation_key)
        if existing is None:
            raise RuntimeError(f"outgoing operation {operation_key!r} is not prepared")
        return OutgoingAttemptResult(
            existing.state,
            existing.result_timestamp_ms,
            attempted_now=False,
        )

    try:
        result = await write()
    except SignalOutgoingDisabledError as exc:
        database.finish_outgoing(
            operation_key,
            state="skipped",
            now_ms=now_ms,
            error_detail=str(exc),
        )
        return OutgoingAttemptResult("skipped", error=exc, attempted_now=False)
    except (SignalRateLimitedError, SignalRPCError) as exc:
        database.finish_outgoing(
            operation_key,
            state="attempted_failed",
            now_ms=now_ms,
            error_detail=str(exc),
        )
        return OutgoingAttemptResult("attempted_failed", error=exc, attempted_now=True)
    except Exception as exc:
        # A transport break can happen after signal-cli accepted the request. The
        # pre-RPC claim therefore remains uncertain and is never made retryable.
        database.finish_outgoing(
            operation_key,
            state="attempted_uncertain",
            now_ms=now_ms,
            error_detail=str(exc),
        )
        return OutgoingAttemptResult("attempted_uncertain", error=exc, attempted_now=True)

    result_timestamp = result if isinstance(result, int) else None
    database.finish_outgoing(
        operation_key,
        state="attempted_success",
        now_ms=now_ms,
        result_timestamp_ms=result_timestamp,
    )
    return OutgoingAttemptResult(
        "attempted_success",
        result_timestamp,
        attempted_now=True,
    )
