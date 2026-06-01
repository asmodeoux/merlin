"""Unit tests for ``merlin_api.errors.register_exception_handlers``.

A route that raises one of the typed repository errors must round-trip
as the stable ``{detail, code}`` JSON shape with the documented status
code. We register a tiny throwaway route per error class so the test
exercises the handler the same way real route code would.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import get_settings
from merlin_core.application.ports import (
    InviteAlreadyRedeemedError,
    InviteNotFoundError,
    RepositoryError,
    SourceAlreadyExistsError,
)

TEST_TOKEN = "test-internal-token"


def _raising_router() -> APIRouter:
    """A throwaway router that raises each repository error on its own path.

    Lives in the test file (not in production code) so we don't pollute
    the real API surface with debug-only endpoints. Mounted onto a
    fresh ``create_app()`` instance per test fixture.
    """
    router = APIRouter()

    @router.get("/_test/raise/invite_not_found")
    async def _raise_invite_not_found() -> None:
        raise InviteNotFoundError("no such invite")

    @router.get("/_test/raise/invite_already_redeemed")
    async def _raise_invite_consumed() -> None:
        raise InviteAlreadyRedeemedError("code 'X' consumed by another redeemer")

    @router.get("/_test/raise/source_already_exists")
    async def _raise_source_exists() -> None:
        raise SourceAlreadyExistsError("source 'tg:abc' already exists")

    @router.get("/_test/raise/repository_error")
    async def _raise_repo_error() -> None:
        raise RepositoryError("unspecified storage failure")

    @router.get("/_test/raise/empty_message")
    async def _raise_empty() -> None:
        raise InviteNotFoundError()

    return router


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Test client backed by a fresh app + the raising router attached."""
    app = create_app()
    app.include_router(_raising_router())
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


async def test_invite_not_found_maps_to_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        "/_test/raise/invite_not_found",
        headers=auth_headers,
    )
    assert response.status_code == 404
    body = response.json()
    assert body == {"detail": "no such invite", "code": "invite_not_found"}


async def test_invite_already_redeemed_maps_to_409(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The race-loss path in /invites/redeem must surface as a 409 with
    a stable code, not bubble as a 500."""
    response = await client.get(
        "/_test/raise/invite_already_redeemed",
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "invite_already_redeemed"
    assert "consumed" in body["detail"]


async def test_source_already_exists_maps_to_409(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        "/_test/raise/source_already_exists",
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body == {
        "detail": "source 'tg:abc' already exists",
        "code": "source_already_exists",
    }


async def test_repository_error_catchall_maps_to_500(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The base ``RepositoryError`` is the catchall — any future
    subclass without a dedicated handler still gets mapped to a useful
    JSON shape instead of a bare 500."""
    response = await client.get(
        "/_test/raise/repository_error",
        headers=auth_headers,
    )
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "detail": "unspecified storage failure",
        "code": "repository_error",
    }


async def test_empty_exception_message_falls_back_to_default_detail(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """``raise InviteNotFoundError()`` (no message) must still produce a
    usable detail string rather than the empty body that ``str(exc)``
    would emit."""
    response = await client.get(
        "/_test/raise/empty_message",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": "invite not found",
        "code": "invite_not_found",
    }
