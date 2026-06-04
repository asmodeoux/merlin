"""Asset routes — search + resolve (Q-027 lazy resolution).

Both endpoints proxy CoinGecko. To avoid burning the free-tier quota
on duplicate user queries, both are wrapped in a tiny Redis-backed
response cache (see ``merlin_api.cache``). Cache keys are derived from
the query / coingecko_id only; nothing user-specific leaks across the
boundary because the responses themselves are cross-user identical.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, status
from merlin_core.application.ports import AssetNotFoundError, MarketDataError
from merlin_core.contracts import (
    AssetCandidateDTO,
    AssetResolution,
    AssetSearchResponse,
    ResolveAssetRequest,
)

from merlin_api.cache import (
    ASSETS_RESOLVE_TTL_SECONDS,
    ASSETS_SEARCH_TTL_SECONDS,
    NEGATIVE_CACHE_MISS,
    cached_or_compute,
    make_key,
)
from merlin_api.deps import Ports, get_ports

router = APIRouter(tags=["assets"])

# Deterministic UUID5 namespace for assets. MVP §15 invariant 9:
# coingecko_id is the only identity; the local asset_id is a stable
# hash of it. Same coingecko_id → same UUID across environments,
# which makes test fixtures and Phase 2 migrations cleaner.
_ASSET_NAMESPACE = UUID("4f6c8a8a-1c45-49d1-a3b2-9c1f3b1e5d2a")


def _asset_id_from_coingecko(coingecko_id: str) -> UUID:
    """Derive the local asset UUID from a coingecko_id (UUID5, deterministic)."""
    return uuid5(_ASSET_NAMESPACE, coingecko_id)


@router.get(
    "/assets/search",
    response_model=AssetSearchResponse,
    summary="Search candidate assets for a freeform query.",
    description=(
        "Proxies CoinGecko ``/search`` and returns the top-5 candidates "
        "ordered by market-cap rank. Drives the bot's asset-picker on "
        "the ``/alpha`` flow (Q-027 lazy asset resolution)."
    ),
)
async def search_assets(
    q: str = Query(..., min_length=1, max_length=64),
    ports: Ports = Depends(get_ports),
) -> AssetSearchResponse:
    """Return CoinGecko search candidates for ``q``.

    Cached by normalised query (lowercase, stripped) for
    ``ASSETS_SEARCH_TTL_SECONDS`` so repeated user typing of the same
    ticker dedupes into a single upstream call. CoinGecko transport
    failures still bubble out as 502 — we never serve a stale cache as
    cover for an outage.
    """

    async def _loader() -> AssetSearchResponse:
        try:
            candidates = await ports.market_data.search_assets(q)
        except MarketDataError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"market data search failed: {e}",
            ) from e
        return AssetSearchResponse(
            candidates=[
                AssetCandidateDTO(
                    coingecko_id=c.coingecko_id,
                    symbol=c.symbol,
                    name=c.name,
                    market_cap_rank=c.market_cap_rank,
                )
                for c in candidates
            ]
        )

    return await cached_or_compute(
        ports.redis,
        key=make_key("assets:search", q.lower().strip()),
        ttl=ASSETS_SEARCH_TTL_SECONDS,
        model=AssetSearchResponse,
        loader=_loader,
    )


@router.post(
    "/assets/resolve",
    response_model=AssetResolution,
    status_code=status.HTTP_200_OK,
    summary="INSERT-or-fetch an asset row by coingecko_id.",
    description=(
        "The bot calls this after the user picks one of the search "
        "candidates. The API confirms the id against CoinGecko (defence "
        "vs. forged ids) and materialises the local ``assets`` row. "
        "Same coingecko_id → same asset_id (deterministic UUID5)."
    ),
)
async def resolve_asset(
    body: ResolveAssetRequest,
    ports: Ports = Depends(get_ports),
) -> AssetResolution:
    """Confirm + materialise a CoinGecko-identified asset.

    Cached by ``coingecko_id`` for ``ASSETS_RESOLVE_TTL_SECONDS``.
    Symbol + name change essentially never, so the long TTL is safe
    and absorbs the burst when 5 users tap Confirm on the same trending
    coin in the same minute. ``AssetNotFoundError`` is negative-cached
    for a short window so a typo'd id doesn't re-hit CoinGecko on every
    retry; ``MarketDataError`` (transport) is NOT cached so a real
    outage recovers immediately when CoinGecko comes back.
    """

    async def _loader() -> AssetResolution:
        try:
            metadata = await ports.market_data.get_asset_metadata(body.coingecko_id)
        except AssetNotFoundError:
            raise
        except MarketDataError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"market data lookup failed: {e}",
            ) from e

        return AssetResolution(
            asset_id=_asset_id_from_coingecko(body.coingecko_id),
            coingecko_id=metadata.coingecko_id,
            symbol=metadata.symbol,
            name=metadata.name,
        )

    try:
        return await cached_or_compute(
            ports.redis,
            key=make_key("assets:resolve", body.coingecko_id),
            ttl=ASSETS_RESOLVE_TTL_SECONDS,
            model=AssetResolution,
            loader=_loader,
            negative_loader_exception=AssetNotFoundError,
        )
    except AssetNotFoundError as e:
        detail = (
            f"unknown coingecko_id: {body.coingecko_id}"
            if str(e) != NEGATIVE_CACHE_MISS
            else f"unknown coingecko_id: {body.coingecko_id} (cached miss)"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        ) from e
