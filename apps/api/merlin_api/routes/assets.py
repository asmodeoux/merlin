"""Asset routes — search + resolve (Q-027 lazy resolution)."""

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
    """Return CoinGecko search candidates for ``q``."""
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
    """Confirm + materialise a CoinGecko-identified asset."""
    try:
        metadata = await ports.market_data.get_asset_metadata(body.coingecko_id)
    except AssetNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown coingecko_id: {body.coingecko_id}",
        ) from e
    except MarketDataError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"market data lookup failed: {e}",
        ) from e

    # Sprint 1 wires the INSERT-or-fetch against the real ``assets``
    # table; the deterministic UUID5 keeps tests stable in the meantime.
    return AssetResolution(
        asset_id=_asset_id_from_coingecko(body.coingecko_id),
        coingecko_id=metadata.coingecko_id,
        symbol=metadata.symbol,
        name=metadata.name,
    )
