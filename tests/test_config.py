from __future__ import annotations

from pathlib import Path

import pytest

from sheltercheck.config import ConfigError, load_config, normalize_text, validate_loopback_url


def test_trigger_normalization() -> None:
    assert normalize_text("  ВСІ\t в\nукритті?  ") == normalize_text("Всі в укритті?")
    assert normalize_text("Всі в укритті!") != normalize_text("Всі в укритті?")


def test_example_config_is_structurally_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config.example.toml", require_groups=False)
    assert config.signal_cli_url == "http://127.0.0.1:8080"
    assert config.wait_seconds == 600
    assert config.command_group_id
    assert config.command_author_uuids == frozenset()
    assert config.state_db == Path("/var/lib/sheltercheck/state.sqlite3")


def test_empty_command_allowlist_safely_disables_feature(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "config.example.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    config = load_config(path)
    assert config.command_control_enabled is False


def test_existing_config_without_command_keys_remains_compatible(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "config.example.toml").read_text(encoding="utf-8")
    command_block = """# Administrative commands work only in this group and only for this explicit
# ACI UUID allowlist. An empty allowlist disables command control.
command_group_id = "REPLACE_WITH_COMMAND_GROUP_ID"
command_author_uuids = []

"""
    path = tmp_path / "legacy-config.toml"
    path.write_text(text.replace(command_block, ""), encoding="utf-8")

    config = load_config(path)
    assert config.command_group_id == ""
    assert config.command_author_uuids == frozenset()
    assert config.command_control_enabled is False


def test_command_authors_require_group_and_valid_aci(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    original = (root / "config.example.toml").read_text(encoding="utf-8")
    with_author = original.replace(
        "command_author_uuids = []",
        'command_author_uuids = ["00000000-0000-4000-8000-000000000010"]',
    )

    missing_group = with_author.replace(
        'command_group_id = "REPLACE_WITH_COMMAND_GROUP_ID"',
        'command_group_id = ""',
    )
    missing_group_path = tmp_path / "missing-group.toml"
    missing_group_path.write_text(missing_group, encoding="utf-8")
    with pytest.raises(ConfigError, match="command_group_id"):
        load_config(missing_group_path)

    invalid_author = original.replace(
        "command_author_uuids = []", 'command_author_uuids = ["not-an-aci"]'
    )
    invalid_author_path = tmp_path / "invalid-author.toml"
    invalid_author_path.write_text(invalid_author, encoding="utf-8")
    with pytest.raises(ConfigError, match="command_author_uuids entry"):
        load_config(invalid_author_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:8080",
        "http://example.com:8080",
        "http://127.0.0.1:8080/unexpected",
        "http://user:secret@127.0.0.1:8080",
    ],
)
def test_non_loopback_or_unsafe_daemon_urls_are_rejected(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_loopback_url(url)
