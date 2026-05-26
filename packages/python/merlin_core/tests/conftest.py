"""Shared fixtures for merlin_core tests."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, settings

# Conservative default profile — keep CI fast but exhaustive enough to
# catch domain bugs. Sprint 1 adds custom profiles per-property test.
settings.register_profile(
    "default",
    max_examples=50,
    deadline=200,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("default")


@pytest.fixture
def anyio_backend() -> str:
    """Run pytest-asyncio tests on asyncio only (we don't ship a trio backend)."""
    return "asyncio"
