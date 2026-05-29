"""Unit tests for ``FakeMarketData``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from merlin_core.application.ports import (
    AssetMetadata,
    AssetNotFoundError,
    PricePoint,
)
from merlin_core.domain import AssetCandidate
from merlin_infrastructure.market_data.fake import FakeMarketData


async def test_register_and_lookup_search_case_insensitive() -> None:
    md = FakeMarketData()
    md.register_search(
        "eth",
        [AssetCandidate(coingecko_id="ethereum", symbol="ETH", name="Ethereum")],
    )

    a = await md.search_assets("ETH")
    b = await md.search_assets("  Eth  ")

    assert [c.coingecko_id for c in a] == ["ethereum"]
    assert [c.coingecko_id for c in b] == ["ethereum"]
    assert md.search_calls == ["ETH", "  Eth  "]


async def test_search_returns_empty_for_unregistered_query() -> None:
    md = FakeMarketData()
    assert await md.search_assets("never") == []


async def test_metadata_lookup_raises_for_unknown_id() -> None:
    md = FakeMarketData()
    with pytest.raises(AssetNotFoundError):
        await md.get_asset_metadata("not-a-real-coin")


async def test_metadata_round_trip() -> None:
    md = FakeMarketData()
    metadata = AssetMetadata(coingecko_id="bitcoin", symbol="BTC", name="Bitcoin")
    md.register_metadata(metadata)
    assert await md.get_asset_metadata("bitcoin") is metadata


async def test_price_lookup_round_trip() -> None:
    md = FakeMarketData()
    when = datetime(2026, 6, 12, 9, 30, tzinfo=UTC)
    point = PricePoint(
        coingecko_id="bitcoin",
        requested_at=when,
        returned_at=when,
        price_usd=70_000.5,
    )
    md.register_price("bitcoin", when, point)

    assert (await md.get_price_at("bitcoin", when)) is point
    assert md.price_calls == [("bitcoin", when)]


async def test_price_lookup_raises_for_unknown_pair() -> None:
    md = FakeMarketData()
    with pytest.raises(AssetNotFoundError):
        await md.get_price_at("ethereum", datetime.now(tz=UTC))
