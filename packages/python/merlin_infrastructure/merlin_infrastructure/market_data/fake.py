"""``FakeMarketData`` — in-memory ``MarketDataPort`` for tests.

Registry-driven: tests call ``register_search``, ``register_price``,
``register_metadata`` to map inputs to canned outputs. Unmapped reads
raise the same errors a real adapter would (``AssetNotFoundError``,
``MarketDataError``) so tests cover the failure branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from merlin_core.application.ports import (
    AssetMetadata,
    AssetNotFoundError,
    PricePoint,
)
from merlin_core.domain import AssetCandidate


@dataclass
class FakeMarketData:
    """Maps queries → fixed responses."""

    _searches: dict[str, list[AssetCandidate]] = field(default_factory=dict)
    _metadata: dict[str, AssetMetadata] = field(default_factory=dict)
    _prices: dict[tuple[str, str], PricePoint] = field(default_factory=dict)
    search_calls: list[str] = field(default_factory=list)
    price_calls: list[tuple[str, datetime]] = field(default_factory=list)
    metadata_calls: list[str] = field(default_factory=list)

    def register_search(self, query: str, candidates: list[AssetCandidate]) -> None:
        """Map a search ``query`` string to a list of candidates."""
        self._searches[query.strip().lower()] = candidates

    def register_metadata(self, metadata: AssetMetadata) -> None:
        """Register a canonical ``(symbol, name)`` lookup for a coingecko_id."""
        self._metadata[metadata.coingecko_id] = metadata

    def register_price(
        self,
        coingecko_id: str,
        when: datetime,
        point: PricePoint,
    ) -> None:
        """Register a price point keyed by ``(coingecko_id, ISO timestamp)``."""
        self._prices[(coingecko_id, when.isoformat())] = point

    async def search_assets(self, query: str) -> list[AssetCandidate]:
        """Return registered candidates for ``query`` (case-insensitive); empty list otherwise."""
        self.search_calls.append(query)
        return list(self._searches.get(query.strip().lower(), []))

    async def get_asset_metadata(self, coingecko_id: str) -> AssetMetadata:
        """Look up registered metadata; raise ``AssetNotFoundError`` if missing."""
        self.metadata_calls.append(coingecko_id)
        if coingecko_id not in self._metadata:
            raise AssetNotFoundError(coingecko_id)
        return self._metadata[coingecko_id]

    async def get_price_at(self, coingecko_id: str, when: datetime) -> PricePoint:
        """Look up registered price; raise ``AssetNotFoundError`` if missing."""
        self.price_calls.append((coingecko_id, when))
        key = (coingecko_id, when.isoformat())
        if key not in self._prices:
            raise AssetNotFoundError(f"no registered price for {coingecko_id} at {when}")
        return self._prices[key]
