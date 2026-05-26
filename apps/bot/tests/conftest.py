"""Shared fixtures for merlin_bot tests."""

from __future__ import annotations

import pytest
from merlin_bot.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Bot Settings with safe defaults for unit tests (no live TG token)."""
    return Settings(
        environment="test",
        telegram_bot_token="",  # empty disables live network
        merlin_api_base_url="http://test-api",
        merlin_api_internal_token="test-token",
        sentry_dsn="",
    )
