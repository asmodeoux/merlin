"""Leaderboard + ``/me`` wire schemas.

User-facing strings (display name, asset symbol, plain percent moves)
only. No internal scoring fields, no version markers — see bot-voice
rules in the bot-flow plan.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from merlin_core.contracts.horizons import HORIZON_MAX_SECONDS, HORIZON_MIN_SECONDS


class BoardEntry(BaseModel):
    """One leaderboard row.

    No "regret" / "simulated_size" / "belief_score" surface — the bot
    voice rules forbid them. Reputation is rendered as plain calibration
    + accuracy percentages.
    """

    rank: int
    display_name: str
    calls_resolved: int
    calls_correct: int
    accuracy_pct: float
    last_call_at: datetime | None = None


class BoardResponse(BaseModel):
    """``GET /board`` response."""

    entries: list[BoardEntry] = Field(default_factory=list)
    window: str = "all_time"


class OpenCallEntry(BaseModel):
    """One open call on ``/me`` — what the user is waiting on.

    ``entry_price_usd`` is the snapshot captured when the claim was
    submitted (``MarketDataPort.get_price_at(submitted_at)``).
    ``None`` when CoinGecko was unavailable at submit time (we
    never block a bet on market data) or for legacy rows that
    predate the column. The bot renders the entry-price line iff
    this is set — never invents a placeholder.
    """

    claim_id: UUID
    asset_symbol: str
    direction: str
    horizon_seconds: int = Field(
        ...,
        ge=HORIZON_MIN_SECONDS,
        le=HORIZON_MAX_SECONDS,
        description="Horizon in seconds; integer; 1h..90d range.",
    )
    resolves_at: datetime
    entry_price_usd: float | None = None


class ResolvedCallEntry(BaseModel):
    """One resolved call on ``/me``."""

    claim_id: UUID
    asset_symbol: str
    direction: str
    horizon_seconds: int = Field(
        ...,
        ge=HORIZON_MIN_SECONDS,
        le=HORIZON_MAX_SECONDS,
        description="Horizon in seconds; integer; 1h..90d range.",
    )
    resolved_at: datetime
    observed_return_pct: float


class MeResponse(BaseModel):
    """``GET /me`` payload — open calls, resolved calls, headline stats.

    Headline stats are plain English / plain percent. No "regret",
    no "simulated PnL", no version markers.

    ``accuracy_pct`` and ``rank`` are ``Optional`` because a user with
    zero resolved calls has no honest value to report — defaulting to
    ``0.0`` / ``0`` would tell users "you're at zero" when the truthful
    answer is "we haven't checked anything yet". The bot renders these
    as "tracking your first resolutions" rather than a misleading
    percent.
    """

    display_name: str | None = None
    calls_open: list[OpenCallEntry] = Field(default_factory=list)
    calls_resolved_recent: list[ResolvedCallEntry] = Field(default_factory=list)
    accuracy_pct: float | None = None
    calls_resolved_total: int = 0
    rank: int | None = None
