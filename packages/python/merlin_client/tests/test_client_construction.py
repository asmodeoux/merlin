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


@pytest.mark.fast
async def test_get_my_tickers_passes_tg_user_id_in_query_string() -> None:
    """``get_my_tickers`` rides ``tg_user_id`` on the URL — same
    convention as ``get_me`` / the rest of the /me family. The HMAC
    bearer authenticates the service; the API needs the per-user id
    on every call."""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(200, json={"entries": []})

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_my_tickers(tg_user_id=42)

    assert captured["path"] == "/me/tickers"
    assert "tg_user_id=42" in captured["query"]


@pytest.mark.fast
async def test_get_my_history_passes_limit_offset_query_string() -> None:
    """``get_my_history`` must thread ``tg_user_id`` + ``limit`` +
    ``offset`` onto the query string. The bot relies on offset
    advancing across pages; a drift in the query shape would break
    the "Show 30 more" button silently."""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(
            200,
            json={
                "entries": [],
                "has_more": False,
                "next_offset": None,
                "as_of": "2026-04-28T12:00:00Z",
            },
        )

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_my_history(tg_user_id=42, limit=30, offset=60)

    assert captured["path"] == "/me/history"
    assert "tg_user_id=42" in captured["query"]
    assert "limit=30" in captured["query"]
    assert "offset=60" in captured["query"]
    # First-page call (no ``as_of`` passed) → the server stamps it.
    assert "as_of" not in captured["query"]


@pytest.mark.fast
async def test_get_my_history_defaults_to_first_page_size_30() -> None:
    """The default ``limit=30`` / ``offset=0`` lets the bot call
    ``client.get_my_history(tg_user_id=...)`` for the first page
    without restating the page size — keeps the bot's command
    handler readable and pins the default at the SDK boundary."""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(
            200,
            json={
                "entries": [],
                "has_more": False,
                "next_offset": None,
                "as_of": "2026-04-28T12:00:00Z",
            },
        )

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_my_history(tg_user_id=42)

    assert "limit=30" in captured["query"]
    assert "offset=0" in captured["query"]


@pytest.mark.fast
async def test_get_my_history_threads_as_of_when_provided() -> None:
    """Subsequent pages must round-trip the ``as_of`` the server
    returned on page 1. Pins that the client sends the value verbatim
    so the frozen-window filter holds across the session."""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(
            200,
            json={
                "entries": [],
                "has_more": False,
                "next_offset": None,
                "as_of": "2026-04-28T12:00:00Z",
            },
        )

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_my_history(
            tg_user_id=42,
            limit=30,
            offset=30,
            as_of="2026-04-28T12:00:00Z",
        )

    # httpx URL-encodes the ``:`` as ``%3A`` in the query string.
    assert "as_of=2026-04-28T12%3A00%3A00Z" in captured["query"]


@pytest.mark.fast
async def test_get_board_defaults_window_and_horizon_on_the_wire() -> None:
    """``get_board()`` with no kwargs sends ``window=all_time&horizon=all``.

    Pins the defaults at the SDK boundary so a future relaxation of
    the route's regex (or a typo in the kwarg names) doesn't silently
    skip the param — the bot reads the response's ``horizon`` echo
    to render the active filter, so a missing send-side value would
    misrender the header.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(
            200,
            json={"entries": [], "window": "all_time", "horizon": "all"},
        )

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_board()

    assert captured["path"] == "/board"
    assert "window=all_time" in captured["query"]
    assert "horizon=all" in captured["query"]


@pytest.mark.fast
@pytest.mark.parametrize("horizon", ["all", "short", "long"])
async def test_get_board_threads_horizon_kwarg_into_query_string(
    horizon: str,
) -> None:
    """The three documented filter values all reach the server verbatim."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(
            200,
            json={"entries": [], "window": "all_time", "horizon": horizon},
        )

    async with MerlinClient(
        base_url="http://localhost:8000",
        token="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.get_board(horizon=horizon)

    assert f"horizon={horizon}" in captured["query"]
    assert result.get("horizon") == horizon
