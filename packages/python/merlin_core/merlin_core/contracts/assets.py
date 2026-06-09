"""Asset-related wire schemas.

Backs ``GET /assets/search`` and ``POST /assets/resolve`` (Q-027 lazy
asset resolution). The bot picks a candidate from the search response
and the resolve call materialises it locally.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class AssetCandidateDTO(BaseModel):
    """One row in the response to ``GET /assets/search``.

    Mirrors the domain ``AssetCandidate`` shape but lives in
    ``contracts`` because Pydantic is forbidden in ``domain``.

    ``current_price_usd`` and ``market_cap_usd`` let the bot
    differentiate look-alike candidates (distinct ``coingecko_id``s
    that share ``(symbol, name)``) by surfacing price + market cap in
    the picker body. Both nullable so long-tail rows without market
    data still render.
    """

    coingecko_id: str
    symbol: str
    name: str
    market_cap_rank: int | None = None
    current_price_usd: float | None = None
    market_cap_usd: float | None = None


class AssetSearchResponse(BaseModel):
    """Top-N candidates for a user query, ordered by market-cap rank."""

    candidates: list[AssetCandidateDTO] = Field(default_factory=list)


class ResolveAssetRequest(BaseModel):
    """Body of ``POST /assets/resolve``.

    The bot sends this after the user picks one candidate. The API
    confirms the id against CoinGecko (defence vs. forged ids), then
    INSERT-or-fetches the local ``assets`` row.
    """

    coingecko_id: str = Field(..., min_length=1)


class AssetResolution(BaseModel):
    """Response of ``POST /assets/resolve``."""

    asset_id: UUID
    coingecko_id: str
    symbol: str
    name: str
