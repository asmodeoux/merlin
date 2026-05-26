"""/alpha ConversationHandler — one file per step + a wiring module.

Sprint 1 ships:
- `states.py`      — IntEnum of conversation states
- `conversation.py` — ConversationHandler factory
- `step_asset.py`  — search-driven asset selection (Q-027 lazy resolve):
    1. User types a query (e.g. "render").
    2. Bot calls `MerlinClient.search_assets(query)` → API proxies
       CoinGecko's `/search` endpoint.
    3. Top 5 candidates rendered as inline-keyboard buttons.
    4. On pick, bot calls `MerlinClient.resolve_asset(coingecko_id)` →
       API INSERT-or-fetches the local `assets` row and returns
       `asset_id`. Conversation advances.

  The bot's asset picker is **stateless about which assets exist
  locally** — the user's query goes straight to CoinGecko's universe.
  No top-N hardcoded list, no pre-seed catalog.

- `step_direction.py` — long/short
- `step_horizon.py` — 1h / 4h / 1d
- `step_conviction.py` — low / med / high
- `step_thesis.py` — free-text thesis
- `step_preview.py` — confirm + POST to API
"""
