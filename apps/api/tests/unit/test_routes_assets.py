"""Unit tests for ``GET /assets/search`` + ``POST /assets/resolve``.

Two layered contracts:

1. **Functional pass-through** — the routes shape the CoinGecko result
   into the wire DTOs the bot's asset-picker consumes, and propagate
   upstream failures as the right HTTP status (502 on transport error,
   404 on unknown id).
2. **Response cache** (``merlin_api.cache``) — both routes dedupe
   identical upstream calls behind a tiny Redis layer so a spammy user
   doesn't burn the CoinGecko free-tier quota.

The CoinGecko adapter port is faked with call-counters; ``fakeredis``
stands in for the production async Redis client. No live network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from merlin_api.app import create_app
from merlin_api.cache import ASSETS_RESOLVE_TTL_SECONDS, ASSETS_SEARCH_TTL_SECONDS
from merlin_api.config import Settings
from merlin_api.deps import Ports, get_ports, get_settings
from merlin_core.application.ports import (
    AssetMetadata,
    AssetNotFoundError,
    MarketDataError,
    PricePoint,
)
from merlin_core.domain import AssetCandidate

TEST_TOKEN = "test-internal-token"


# ---------------------------------------------------------------------------
# Fakes — minimal MarketDataPort + Ports + no-op friends
# ---------------------------------------------------------------------------


@dataclass
class _FakeMarketData:
    """Counts every upstream call so tests can assert deduplication."""

    search_results: dict[str, list[AssetCandidate]] = field(default_factory=dict)
    metadata_by_id: dict[str, AssetMetadata] = field(default_factory=dict)
    search_calls: list[str] = field(default_factory=list)
    metadata_calls: list[str] = field(default_factory=list)
    search_raise: Exception | None = None
    metadata_raise: Exception | None = None

    async def search_assets(self, query: str) -> list[AssetCandidate]:
        self.search_calls.append(query)
        if self.search_raise is not None:
            raise self.search_raise
        return list(self.search_results.get(query, []))

    async def get_asset_metadata(self, coingecko_id: str) -> AssetMetadata:
        self.metadata_calls.append(coingecko_id)
        if self.metadata_raise is not None:
            raise self.metadata_raise
        if coingecko_id not in self.metadata_by_id:
            raise AssetNotFoundError(coingecko_id)
        return self.metadata_by_id[coingecko_id]

    async def get_price_at(self, coingecko_id: str, when: datetime) -> PricePoint:
        # Routes under test don't call this; included for the Protocol.
        raise NotImplementedError


class _NoopDrafter:
    async def draft(self, _text: str) -> Any:
        raise NotImplementedError


class _NoopQueue:
    def enqueue_at(self, *_a: Any, **_kw: Any) -> None:
        pass


class _NoopNotifier:
    async def notify(self, *_a: Any, **_kw: Any) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def redis_client() -> AsyncIterator[FakeRedis]:
    fake = FakeRedis()
    try:
        yield fake
    finally:
        await fake.aclose()


def _build_client(
    *,
    market: _FakeMarketData,
    settings: Settings,
    redis_client: FakeRedis | None,
) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ports] = lambda: Ports(
        drafter=_NoopDrafter(),
        market_data=market,
        queue=_NoopQueue(),
        notifier=_NoopNotifier(),
        redis=redis_client,
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# GET /assets/search — functional contract
# ---------------------------------------------------------------------------


async def test_search_returns_candidates_from_upstream(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    market = _FakeMarketData(
        search_results={
            "eth": [
                AssetCandidate(
                    coingecko_id="ethereum",
                    symbol="ETH",
                    name="Ethereum",
                    market_cap_rank=2,
                ),
            ],
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        response = await client.get("/assets/search", params={"q": "eth"}, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["coingecko_id"] == "ethereum"


async def test_search_502s_on_transport_failure(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    market = _FakeMarketData(search_raise=MarketDataError("coingecko exploded"))
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        response = await client.get("/assets/search", params={"q": "eth"}, headers=auth_headers)
    assert response.status_code == 502


# ---------------------------------------------------------------------------
# GET /assets/search — cache behaviour
# ---------------------------------------------------------------------------


async def test_search_deduplicates_identical_queries(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """Pin the core cost-protection contract — two identical ``q``s →
    exactly one upstream call. This is the only thing the cache exists
    to do."""
    market = _FakeMarketData(
        search_results={
            "eth": [
                AssetCandidate(coingecko_id="ethereum", symbol="ETH", name="Ethereum"),
            ],
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        r1 = await client.get("/assets/search", params={"q": "eth"}, headers=auth_headers)
        r2 = await client.get("/assets/search", params={"q": "eth"}, headers=auth_headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert market.search_calls == ["eth"], (
        f"expected exactly one upstream call, got {market.search_calls!r}. "
        "The cache layer is not deduplicating identical queries."
    )


async def test_search_cache_key_is_normalised(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """``q="ETH"`` and ``q=" eth "`` share a cache slot — both
    normalise to ``"eth"`` so the user retyping the same ticker with
    different whitespace/case doesn't bypass the cache.

    Upstream still gets the *original* query string ``"ETH"`` on the
    first call (CoinGecko is case-tolerant; we don't pre-mangle the
    user query before forwarding it).
    """
    market = _FakeMarketData(
        search_results={
            "ETH": [AssetCandidate(coingecko_id="ethereum", symbol="ETH", name="Ethereum")],
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        await client.get("/assets/search", params={"q": "ETH"}, headers=auth_headers)
        await client.get("/assets/search", params={"q": " eth "}, headers=auth_headers)
        await client.get("/assets/search", params={"q": "Eth"}, headers=auth_headers)

    assert market.search_calls == ["ETH"], (
        f"case + whitespace variants must share a cache slot — got {market.search_calls!r}"
    )


async def test_search_cache_sets_ttl(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """The cache slot must carry a TTL (``ASSETS_SEARCH_TTL_SECONDS``)
    so it expires; a missing TTL would mean stale results forever."""
    market = _FakeMarketData(
        search_results={
            "btc": [AssetCandidate(coingecko_id="bitcoin", symbol="BTC", name="Bitcoin")],
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        await client.get("/assets/search", params={"q": "btc"}, headers=auth_headers)

    ttl = await redis_client.ttl("merlin_api:cache:assets:search:btc")
    assert 0 < ttl <= ASSETS_SEARCH_TTL_SECONDS


async def test_search_falls_through_when_redis_unavailable(
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """No Redis client → upstream still serves the request (fail-open).

    Cache absence must never break user-facing behaviour.
    """
    market = _FakeMarketData(
        search_results={
            "sol": [AssetCandidate(coingecko_id="solana", symbol="SOL", name="Solana")],
        },
    )
    async with _build_client(market=market, settings=settings, redis_client=None) as client:
        r1 = await client.get("/assets/search", params={"q": "sol"}, headers=auth_headers)
        r2 = await client.get("/assets/search", params={"q": "sol"}, headers=auth_headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Without Redis, both calls hit upstream — that's the documented
    # trade-off; the route is never blocked by the cache layer.
    assert market.search_calls == ["sol", "sol"]


async def test_search_transport_error_is_not_cached(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """A 502 must NOT be cached — when CoinGecko recovers, the very
    next user gets a fresh successful response. Caching transport
    errors would convert a 30-second outage into a 5-minute one."""
    market = _FakeMarketData(search_raise=MarketDataError("coingecko 502"))
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        r1 = await client.get("/assets/search", params={"q": "doge"}, headers=auth_headers)
        # Recover the upstream.
        market.search_raise = None
        market.search_results = {
            "doge": [AssetCandidate(coingecko_id="dogecoin", symbol="DOGE", name="Dogecoin")],
        }
        r2 = await client.get("/assets/search", params={"q": "doge"}, headers=auth_headers)

    assert r1.status_code == 502
    assert r2.status_code == 200, "post-recovery call must NOT be served from a cached failure"
    assert market.search_calls == ["doge", "doge"]


# ---------------------------------------------------------------------------
# POST /assets/resolve — functional contract
# ---------------------------------------------------------------------------


async def test_resolve_returns_metadata(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    market = _FakeMarketData(
        metadata_by_id={
            "ethereum": AssetMetadata(
                coingecko_id="ethereum",
                symbol="ETH",
                name="Ethereum",
            ),
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        response = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["coingecko_id"] == "ethereum"
    assert body["symbol"] == "ETH"


async def test_resolve_404_on_unknown_id(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    market = _FakeMarketData()  # no metadata seeded → AssetNotFoundError
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        response = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "definitely-not-real"},
            headers=auth_headers,
        )
    assert response.status_code == 404


async def test_resolve_502_on_transport_error(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    market = _FakeMarketData(metadata_raise=MarketDataError("coingecko 502"))
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        response = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
    assert response.status_code == 502


# ---------------------------------------------------------------------------
# POST /assets/resolve — cache behaviour
# ---------------------------------------------------------------------------


async def test_resolve_deduplicates_identical_ids(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """Two resolves on the same id → one upstream call."""
    market = _FakeMarketData(
        metadata_by_id={
            "ethereum": AssetMetadata(coingecko_id="ethereum", symbol="ETH", name="Ethereum"),
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        r1 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
        r2 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert market.metadata_calls == ["ethereum"]


async def test_resolve_cache_sets_long_ttl(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    market = _FakeMarketData(
        metadata_by_id={
            "bitcoin": AssetMetadata(coingecko_id="bitcoin", symbol="BTC", name="Bitcoin"),
        },
    )
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        await client.post(
            "/assets/resolve",
            json={"coingecko_id": "bitcoin"},
            headers=auth_headers,
        )

    ttl = await redis_client.ttl("merlin_api:cache:assets:resolve:bitcoin")
    assert 0 < ttl <= ASSETS_RESOLVE_TTL_SECONDS


async def test_resolve_negative_cache_short_circuits_retry(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """A 404 (``AssetNotFoundError``) is negative-cached briefly so
    a script retrying the same junk id doesn't keep hitting CoinGecko.
    Pin the contract: the second call returns 404 without an upstream
    visit."""
    market = _FakeMarketData()  # nothing seeded → 404 on any id
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        r1 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "asdfghjkl"},
            headers=auth_headers,
        )
        r2 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "asdfghjkl"},
            headers=auth_headers,
        )
    assert r1.status_code == 404
    assert r2.status_code == 404
    assert market.metadata_calls == ["asdfghjkl"], (
        f"negative caching must short-circuit the second call — got {market.metadata_calls!r}"
    )


async def test_resolve_transport_error_is_not_cached(
    settings: Settings,
    auth_headers: dict[str, str],
    redis_client: FakeRedis,
) -> None:
    """Same recovery contract as the search path: a 502 from
    CoinGecko must NOT be cached so the next call after recovery
    succeeds fresh."""
    market = _FakeMarketData(metadata_raise=MarketDataError("transient"))
    async with _build_client(
        market=market,
        settings=settings,
        redis_client=redis_client,
    ) as client:
        r1 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
        market.metadata_raise = None
        market.metadata_by_id = {
            "ethereum": AssetMetadata(coingecko_id="ethereum", symbol="ETH", name="Ethereum"),
        }
        r2 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
    assert r1.status_code == 502
    assert r2.status_code == 200


async def test_resolve_falls_through_when_redis_unavailable(
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    market = _FakeMarketData(
        metadata_by_id={
            "ethereum": AssetMetadata(coingecko_id="ethereum", symbol="ETH", name="Ethereum"),
        },
    )
    async with _build_client(market=market, settings=settings, redis_client=None) as client:
        r1 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
        r2 = await client.post(
            "/assets/resolve",
            json={"coingecko_id": "ethereum"},
            headers=auth_headers,
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert market.metadata_calls == ["ethereum", "ethereum"]
