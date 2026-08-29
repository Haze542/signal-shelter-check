from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import tomllib


class ConfigError(ValueError):
    """Configuration is missing, malformed, or unsafe."""


_WHITESPACE_RE = re.compile(r"\s+")
_REQUIRED_KEYS = {
    "signal_cli_url",
    "monitor_group_id",
    "report_group_id",
    "trigger_texts",
    "trigger_author_uuids",
    "accepted_reactions",
    "wait_seconds",
    "state_db",
    "roster_file",
    "released_file",
}
_OPTIONAL_KEYS = {
    "command_group_id",
    "command_author_uuids",
    "connect_timeout_seconds",
    "request_timeout_seconds",
    "sse_read_timeout_seconds",
    "reconnect_initial_seconds",
    "reconnect_max_seconds",
    "edit_debounce_seconds",
}


def normalize_text(value: str) -> str:
    """Normalize exact trigger text without fuzzy matching."""

    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE_RE.sub(" ", normalized.strip()).casefold()


def normalize_aci(value: str, *, field: str = "ACI") -> str:
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a valid UUID") from exc


def validate_loopback_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"signal_cli_url is invalid: {exc}") from exc

    if parsed.scheme not in {"http", "https"}:
        raise ConfigError("signal_cli_url must use http or https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("signal_cli_url must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ConfigError("signal_cli_url must not contain a path")
    if port is None or port <= 0:
        raise ConfigError("signal_cli_url must include the signal-cli daemon port")

    host = parsed.hostname
    if host == "localhost":
        pass
    else:
        try:
            if host is None or not ipaddress.ip_address(host).is_loopback:
                raise ConfigError("signal_cli_url must point to a loopback address")
        except ValueError as exc:
            raise ConfigError(
                "signal_cli_url hostname must be localhost or a loopback IP address"
            ) from exc
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class Config:
    signal_cli_url: str
    monitor_group_id: str
    report_group_id: str
    trigger_texts: frozenset[str]
    trigger_author_uuids: frozenset[str]
    accepted_reactions: frozenset[str]
    wait_seconds: int
    state_db: Path
    roster_file: Path
    released_file: Path
    command_group_id: str = ""
    command_author_uuids: frozenset[str] = frozenset()
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    sse_read_timeout_seconds: float = 90.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    edit_debounce_seconds: float = 1.0

    @property
    def wait_milliseconds(self) -> int:
        return self.wait_seconds * 1000

    @property
    def edit_debounce_milliseconds(self) -> int:
        return round(self.edit_debounce_seconds * 1000)

    @property
    def command_control_enabled(self) -> bool:
        return bool(self.command_author_uuids)


def _string(data: dict[str, Any], key: str, *, nonempty: bool = True) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    value = value.strip()
    if nonempty and not value:
        raise ConfigError(f"{key} must not be empty")
    return value


def _string_list(data: dict[str, Any], key: str, *, nonempty: bool = True) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be an array of strings")
    cleaned = [item.strip() for item in value]
    if any(not item for item in cleaned):
        raise ConfigError(f"{key} must not contain empty strings")
    if nonempty and not cleaned:
        raise ConfigError(f"{key} must not be empty")
    return cleaned


def _positive_number(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{key} must be a positive number")
    return float(value)


def _resolve_path(config_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_dir / path


def load_config(path: str | Path = "config.toml", *, require_groups: bool = True) -> Config:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    missing = sorted(_REQUIRED_KEYS - data.keys())
    unknown = sorted(data.keys() - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if missing:
        raise ConfigError(f"missing configuration keys: {', '.join(missing)}")
    if unknown:
        raise ConfigError(f"unknown configuration keys: {', '.join(unknown)}")

    monitor_group_id = _string(data, "monitor_group_id", nonempty=require_groups)
    report_group_id = _string(data, "report_group_id", nonempty=require_groups)
    raw_triggers = _string_list(data, "trigger_texts")
    triggers = frozenset(normalize_text(item) for item in raw_triggers)
    if "" in triggers:
        raise ConfigError("trigger_texts must contain meaningful text")

    raw_authors = _string_list(data, "trigger_author_uuids", nonempty=False)
    authors = frozenset(
        normalize_aci(value, field="trigger_author_uuids entry") for value in raw_authors
    )
    reactions = frozenset(_string_list(data, "accepted_reactions"))

    command_group_id = data.get("command_group_id", "")
    if not isinstance(command_group_id, str):
        raise ConfigError("command_group_id must be a string")
    command_group_id = command_group_id.strip()
    raw_command_authors = data.get("command_author_uuids", [])
    if not isinstance(raw_command_authors, list) or any(
        not isinstance(item, str) for item in raw_command_authors
    ):
        raise ConfigError("command_author_uuids must be an array of strings")
    cleaned_command_authors = [item.strip() for item in raw_command_authors]
    if any(not item for item in cleaned_command_authors):
        raise ConfigError("command_author_uuids must not contain empty strings")
    command_authors = frozenset(
        normalize_aci(value, field="command_author_uuids entry")
        for value in cleaned_command_authors
    )
    if command_authors and not command_group_id:
        raise ConfigError(
            "command_group_id must not be empty when command_author_uuids is configured"
        )

    wait_seconds = data.get("wait_seconds")
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or wait_seconds <= 0:
        raise ConfigError("wait_seconds must be a positive integer")

    config_dir = config_path.resolve().parent
    reconnect_initial = _positive_number(data, "reconnect_initial_seconds", 1.0)
    reconnect_max = _positive_number(data, "reconnect_max_seconds", 30.0)
    if reconnect_max < reconnect_initial:
        raise ConfigError(
            "reconnect_max_seconds must be greater than or equal to reconnect_initial_seconds"
        )

    return Config(
        signal_cli_url=validate_loopback_url(_string(data, "signal_cli_url")),
        monitor_group_id=monitor_group_id,
        report_group_id=report_group_id,
        trigger_texts=triggers,
        trigger_author_uuids=authors,
        accepted_reactions=reactions,
        wait_seconds=wait_seconds,
        state_db=_resolve_path(config_dir, _string(data, "state_db")),
        roster_file=_resolve_path(config_dir, _string(data, "roster_file")),
        released_file=_resolve_path(config_dir, _string(data, "released_file")),
        command_group_id=command_group_id,
        command_author_uuids=command_authors,
        connect_timeout_seconds=_positive_number(data, "connect_timeout_seconds", 5.0),
        request_timeout_seconds=_positive_number(data, "request_timeout_seconds", 30.0),
        sse_read_timeout_seconds=_positive_number(data, "sse_read_timeout_seconds", 90.0),
        reconnect_initial_seconds=reconnect_initial,
        reconnect_max_seconds=reconnect_max,
        edit_debounce_seconds=_positive_number(data, "edit_debounce_seconds", 1.0),
    )
