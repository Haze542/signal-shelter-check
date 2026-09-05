#!/usr/bin/env python3
"""Side-effect-free signal-cli HTTP/listAccounts readiness probe."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class ReadinessError(RuntimeError):
    pass


class NoAccountsLoaded(ReadinessError):
    pass


class MultipleAccountsLoaded(ReadinessError):
    pass


def _loopback_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or port is None:
        raise argparse.ArgumentTypeError("URL must be HTTP(S) and include a port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError(
            "URL must not contain credentials, query, or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise argparse.ArgumentTypeError("URL must not contain a path")
    host = parsed.hostname
    if host != "localhost":
        try:
            if host is None or not ipaddress.ip_address(host).is_loopback:
                raise argparse.ArgumentTypeError("URL must use a loopback address")
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "URL hostname must be localhost or a loopback IP address"
            ) from exc
    return value.rstrip("/")


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        type=_loopback_url,
        default="http://127.0.0.1:8080",
        help="loopback signal-cli HTTP daemon URL",
    )
    parser.add_argument("--timeout-seconds", type=_positive_float, default=30.0)
    parser.add_argument("--request-timeout-seconds", type=_positive_float, default=3.0)
    parser.add_argument("--retry-interval-seconds", type=_positive_float, default=1.0)
    parser.add_argument(
        "--wait-for-account",
        action="store_true",
        help="keep polling empty listAccounts until timeout instead of failing fast",
    )
    return parser.parse_args(argv)


def _get_http_check(base_url: str, request_timeout: float) -> None:
    request = urllib.request.Request(f"{base_url}/api/v1/check", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            if response.status != 200:
                raise ReadinessError(
                    f"HTTP health endpoint returned status {response.status}"
                )
            response.read()
    except urllib.error.HTTPError as exc:
        raise ReadinessError(
            f"HTTP health endpoint returned status {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ReadinessError("HTTP API is not reachable") from exc


def _list_accounts(base_url: str, request_timeout: float) -> tuple[str, ...]:
    request_id = "sheltercheck-readiness"
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "listAccounts",
            "params": {},
            "id": request_id,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/v1/rpc",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise ReadinessError(
                    f"listAccounts endpoint returned status {response.status}"
                )
            raw_payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ReadinessError(
            f"listAccounts endpoint returned status {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ReadinessError("listAccounts request failed") from exc

    try:
        payload: Any = json.loads(raw_payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ReadinessError("listAccounts returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("id") != request_id:
        raise ReadinessError("listAccounts returned an invalid JSON-RPC response")
    if isinstance(payload.get("error"), dict):
        code = payload["error"].get("code")
        safe_code = code if isinstance(code, int) else "unknown"
        raise ReadinessError(f"listAccounts returned JSON-RPC error {safe_code}")
    result = payload.get("result")
    if not isinstance(result, list):
        raise ReadinessError("listAccounts result is not an array")

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
            raise ReadinessError("listAccounts returned an invalid account entry")
        accounts.append(account)
    return tuple(accounts)


def wait_until_ready(
    base_url: str,
    *,
    timeout_seconds: float,
    request_timeout_seconds: float,
    retry_interval_seconds: float,
    wait_for_account: bool,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    last_reason = "readiness has not been checked"
    http_announced = False
    waiting_reason: str | None = None

    while True:
        try:
            _get_http_check(base_url, request_timeout_seconds)
            if not http_announced:
                print("signal-cli HTTP API is reachable", flush=True)
                http_announced = True
            accounts = _list_accounts(base_url, request_timeout_seconds)
            if not accounts:
                raise NoAccountsLoaded("no accounts loaded")
            if len(accounts) != 1:
                raise MultipleAccountsLoaded(
                    "this deployment requires exactly one loaded account"
                )
        except MultipleAccountsLoaded as exc:
            print(
                f"signal-cli readiness check failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        except NoAccountsLoaded as exc:
            last_reason = str(exc)
            if not wait_for_account:
                print(
                    "signal-cli readiness check failed: no accounts loaded; "
                    "startup will fail so systemd recovery can retry",
                    file=sys.stderr,
                    flush=True,
                )
                return False
        except ReadinessError as exc:
            last_reason = str(exc)
        else:
            print("signal-cli ready: 1 account loaded", flush=True)
            return True

        if time.monotonic() >= deadline:
            print(
                f"signal-cli readiness check timed out: {last_reason}",
                file=sys.stderr,
                flush=True,
            )
            return False
        if last_reason != waiting_reason:
            print(
                f"signal-cli not ready: {last_reason}; retrying",
                file=sys.stderr,
                flush=True,
            )
            waiting_reason = last_reason
        time.sleep(min(retry_interval_seconds, max(0.0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    ready = wait_until_ready(
        args.url,
        timeout_seconds=args.timeout_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
        retry_interval_seconds=args.retry_interval_seconds,
        wait_for_account=args.wait_for_account,
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
