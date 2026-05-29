"""Unit tests for ``merlin_bot.config.Settings``.

The interesting bit is the ``telegram_admin_chat_ids`` field validator,
which accepts a comma-separated env string (the .env.example convention)
in addition to pydantic-settings' default JSON parsing.

All tests pass ``_env_file=None`` so they don't accidentally pick up the
developer's ``apps/bot/.env`` (which would override the explicit kwargs
and make the suite flaky on dev machines).
"""

from __future__ import annotations

import pytest
from merlin_bot.config import Settings


def _settings(**env: str) -> Settings:
    """Construct a Settings ignoring the local .env file."""
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_admin_chat_ids_empty_string_yields_empty_list() -> None:
    s = _settings(telegram_admin_chat_ids="")
    assert s.telegram_admin_chat_ids == []


def test_admin_chat_ids_single_value() -> None:
    s = _settings(telegram_admin_chat_ids="987654321")
    assert s.telegram_admin_chat_ids == [987654321]


def test_admin_chat_ids_comma_separated() -> None:
    s = _settings(telegram_admin_chat_ids="111,222,333")
    assert s.telegram_admin_chat_ids == [111, 222, 333]


def test_admin_chat_ids_tolerates_whitespace() -> None:
    s = _settings(telegram_admin_chat_ids=" 111 , 222 ")
    assert s.telegram_admin_chat_ids == [111, 222]


def test_admin_chat_ids_rejects_non_numeric() -> None:
    with pytest.raises(ValueError):
        _settings(telegram_admin_chat_ids="abc")


def test_settings_defaults_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADMIN_CHAT_IDS",
        "TELEGRAM_MODE",
        "MERLIN_API_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    s = _settings()
    assert s.telegram_bot_token == ""
    assert s.telegram_admin_chat_ids == []
    assert s.telegram_mode == "polling"
