"""Market-data adapters — CoinGecko (httpx-backed).

Single market-data source per Q-026.
The ``CoinGeckoAdapter`` wraps three endpoints:

- ``GET /api/v3/search?query=<text>`` — fuzzy search returning candidate
  coins. Drives the bot's lazy asset resolution during the ``/alpha``
  flow (Q-027). The API exposes this
  through ``GET /assets/search`` (no URL versioning).
- ``GET /api/v3/coins/{id}/market_chart/range`` — resolution price
  points at arbitrary historical timestamps. The adapter records both
  the **requested** and the **actually-returned** timestamps for every
  range query — required for the ``resolution_price_t0_ref`` /
  ``resolution_price_resolve_ref`` fields on ``outcomes``
  (MVP §11.1.H, §12.9).
- ``GET /api/v3/coins/{id}`` — canonical metadata (symbol, name) for the
  INSERT-or-fetch path in ``POST /assets/resolve``.

The ``FakeMarketData`` sibling registers canned candidate lists and
price points for tests — API + worker integration tests use it to
avoid hitting CoinGecko on every CI run.
"""

from merlin_infrastructure.market_data.coingecko import CoinGeckoAdapter
from merlin_infrastructure.market_data.fake import FakeMarketData

__all__ = ["CoinGeckoAdapter", "FakeMarketData"]
