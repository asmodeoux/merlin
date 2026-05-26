"""Market-data adapters — CoinGecko Demo (httpx-backed).

Single market-data source per Q-026.
Sprint 1 fills in ``coingecko.CoinGeckoClient`` wrapping three endpoints:

- ``GET /api/v3/search?query=<text>`` — fuzzy search returning candidate
  coins. Drives the bot's lazy asset resolution during the ``/alpha``
  flow (Q-027). The API exposes this
  through ``GET /v1/assets/search``.
- ``GET /api/v3/coins/{id}/market_chart/range`` — resolution price
  points at arbitrary historical timestamps. The client records both the
  **requested** and the **actually-returned** timestamps for every range
  query — required for the ``resolution_price_t0_ref`` /
  ``resolution_price_resolve_ref`` fields on ``outcomes``
  (MVP §11.1.H, §12.9).
- ``GET /api/v3/simple/price?ids=...&vs_currencies=usd`` — live spot for
  board / leaderboard displays. Cheap enough to call ad hoc; no caching
  layer at v0.
"""
