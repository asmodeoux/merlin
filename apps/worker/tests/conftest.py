"""Shared fixtures for merlin_worker tests."""

from __future__ import annotations

import pytest
from merlin_worker.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Worker Settings with safe defaults for unit tests."""
    return Settings(environment="test", sentry_dsn="")
