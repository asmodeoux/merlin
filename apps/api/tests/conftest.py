"""Shared fixtures for merlin_api tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import get_settings

TEST_TOKEN = "test-internal-token"


@pytest.fixture
def settings() -> Settings:
    """Settings with the test token wired in."""
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Async test client backed by the in-process FastAPI app.

    Tests can override individual deps via
    `app.dependency_overrides[...]`. The token is injected via the
    Settings override so the `bearerAuth` dep sees our test value.
    """
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Authorization header carrying the test bearer token."""
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
