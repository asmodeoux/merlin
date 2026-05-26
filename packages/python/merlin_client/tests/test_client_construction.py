"""Tests for MerlinClient construction + transport wiring (no live server)."""

from __future__ import annotations

import httpx
import pytest
from merlin_client import MerlinClient, MerlinHTTPError, MerlinTimeoutError


@pytest.mark.fast
def test_requires_base_url() -> None:
    """Empty base_url is a programming error, not a runtime surprise."""
    with pytest.raises(ValueError):
        MerlinClient(base_url="", token="dev")


@pytest.mark.fast
def test_requires_token() -> None:
    """Empty token is a programming error."""
    with pytest.raises(ValueError):
        MerlinClient(base_url="http://localhost:8000", token="")


@pytest.mark.fast
async def test_health_round_trip_via_mock_transport() -> None:
    """A mocked /health returns the expected payload + Authorization header is sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.headers["User-Agent"].startswith("merlin-client/")
        return httpx.Response(200, json={"status": "ok"})

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.health()

    assert result == {"status": "ok"}


@pytest.mark.fast
async def test_non_2xx_raises_http_error() -> None:
    """Non-2xx responses surface as MerlinHTTPError with detail attached."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "DB unavailable"})

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(MerlinHTTPError) as excinfo:
            await client.ready()

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == {"detail": "DB unavailable"}


@pytest.mark.fast
async def test_timeout_raises_timeout_error() -> None:
    """httpx.TimeoutException → MerlinTimeoutError (callers don't import httpx)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(MerlinTimeoutError):
            await client.health()
