"""MarketDataPort — single-source crypto market data (Q-026 CoinGecko Demo).

Three operations the application layer needs:

- ``search_assets(q)`` — drives the bot's ``/alpha`` asset step (Q-027
  lazy resolution) via CoinGecko ``/api/v3/search``.
- ``get_asset_metadata(coingecko_id)`` — confirms a coingecko_id exists
  and returns canonical ``(symbol, name)`` for the INSERT-or-fetch path
  in ``POST /assets/resolve``.
- ``get_price_at(coingecko_id, when)`` — fetches a single historical
  price point for outcome resolution. Granularity is auto-determined by
  CoinGecko (5-min <= 24h, hourly 2-90d, daily > 90d). The adapter
  records both the **requested** and **actually-returned** timestamps
  for replay (MVP §11.1.H, §12.9).

Implemented by ``merlin_infrastructure.market_data.coingecko.CoinGeckoAdapter``
(real httpx) and ``merlin_infrastructure.market_data.fake.FakeMarketData``
(tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from merlin_core.domain import AssetCandidate


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A single historical price point with replay-grade timestamps.

    ``requested_at`` is what the resolver asked for; ``returned_at`` is
    the data source's effective "as-of" timestamp for ``price_usd``.
    For point-in-time backends this is the actual tick / bucket
    timestamp; for TWAP-over-window backends (the CoinGecko adapter at
    v0) this is the midpoint of the bucket span the average was
    computed over. Either way, the pair is persisted on
    ``outcomes.resolution_price_*_ref`` so audits replay against a
    concrete instant.
    """

    coingecko_id: str
    requested_at: datetime
    returned_at: datetime
    price_usd: float


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    """Canonical metadata for an asset, fetched by ``coingecko_id``."""

    coingecko_id: str
    symbol: str
    name: str


@runtime_checkable
class MarketDataPort(Protocol):
    """Search + resolve + price the universe of supported assets."""

    async def search_assets(self, query: str) -> list[AssetCandidate]:
        """Return candidate assets for a freeform user query.

        Ordered by ``market_cap_rank`` ascending (NULL last). The bot
        presents the top 5 as inline-keyboard buttons.
        """
        ...

    async def get_asset_metadata(self, coingecko_id: str) -> AssetMetadata:
        """Confirm ``coingecko_id`` exists and return canonical name + symbol.

        Raises ``MarketDataError`` if CoinGecko doesn't recognise the id
        (which the API treats as a 400 — the client supplied a fake or
        delisted id).
        """
        ...

    async def get_price_at(self, coingecko_id: str, when: datetime) -> PricePoint:
        """Fetch a price point near ``when``.

        Returns the price from CoinGecko's nearest bucket within ±5
        minutes (the rounding policy locked in Q-026). Raises
        ``MarketDataError`` on persistent provider failure — the
        resolver catches this and writes ``events.status =
        'resolution_failed'`` for the RQ retry path.
        """
        ...


class MarketDataError(Exception):
    """Raised by any concrete ``MarketDataPort`` adapter on failure.

    Subclasses may distinguish 404 (asset mutation), 429 (rate-limit),
    and 5xx (transient). The resolver treats 404 specially — that's
    the Q-023 asset-mutation rejection path. Everything else feeds
    into the RQ retry policy.
    """


class AssetNotFoundError(MarketDataError):
    """CoinGecko returned 404 / empty-series for the requested coingecko_id.

    Treated by the resolver as the Q-023 asset-mutation case:
    ``events.status = 'rejected_asset_mutation'``, no outcome row,
    user gets the "couldn't be settled" notification.
    """
