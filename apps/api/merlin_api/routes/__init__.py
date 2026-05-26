"""Per-resource route modules — Sprint 1 fills with /v1/* business routes.

Every business route is mounted under a single `APIRouter(prefix="/v1")`
in `merlin_api.app.create_app`. Adding a new resource = add a module
here, export its `router`, and include it from `app.py`.

Planned Sprint 1 resources:

- ``claims``  — ``POST /v1/claims``, ``GET /v1/claims/{id}``.
- ``board``   — ``GET /v1/board``, ``GET /v1/me``.
- ``invites`` — ``POST /v1/invites``, ``POST /v1/invites/{code}/redeem``.
- ``assets``  — lazy asset resolution per Q-027:
    - ``GET  /v1/assets/search?q=<text>`` — proxies CoinGecko
      ``/search`` and returns candidate ``{coingecko_id, symbol, name,
      market_cap_rank}`` rows. Drives the bot's asset picker.
    - ``POST /v1/assets/resolve`` — body
      ``{coingecko_id, symbol_hint?, name_hint?}``. INSERT-or-fetch on
      ``assets`` keyed by ``coingecko_id`` (immutable per MVP §15
      invariant 13); returns ``{asset_id, symbol, name, coingecko_id}``.
      Called by the bot at the end of the ``/alpha`` asset step *and*
      implicitly by ``POST /v1/claims`` if the caller supplies a
      ``coingecko_id`` that isn't yet materialised.
"""
