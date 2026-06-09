"""Leaderboard + ``/me`` wire schemas.

User-facing strings (display name, asset symbol, plain percent moves)
only. No internal scoring fields, no version markers — see bot-voice
rules in the bot-flow plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from merlin_core.contracts.horizons import HORIZON_MAX_SECONDS, HORIZON_MIN_SECONDS

# Four-mode horizon filter for ``/board``. ``"all"`` mirrors the
# pre-filter snapshot read; ``"day"`` / ``"swing"`` / ``"position"``
# slice by ``HORIZON_DAY_TRADER_MAX_SECONDS`` (≤24h) and
# ``HORIZON_SWING_TRADER_MAX_SECONDS`` (≤21d), and the bot
# surfaces them as "Day Traders" / "Swing Traders" / "Position
# Traders" in the inline filter row.
HorizonFilter = Literal["all", "day", "swing", "position"]


class BoardEntry(BaseModel):
    """One leaderboard row.

    No "regret" / "simulated_size" / "belief_score" surface — the bot
    voice rules forbid them. The three reputation numbers we DO
    surface are layered views of the same history:

    * ``accuracy_pct`` — plain hit rate (the boolean view).
    * ``weighted_pnl_pct`` — conviction-weighted signed PnL %
      (the magnitude view). The bot renders this as the primary
      headline; hit rate is the secondary number per the locked
      ``primary_metric = pnl_primary`` choice.

    The Bayesian-shrunk sort score that drives the row order is
    NOT carried on the wire — it's an internal ranking key, not a
    user-facing concept. ``rank`` is the rendered 1-indexed position.
    """

    rank: int
    display_name: str
    calls_resolved: int
    calls_correct: int
    accuracy_pct: float
    weighted_pnl_pct: float
    last_call_at: datetime | None = None


class BoardResponse(BaseModel):
    """``GET /board`` response.

    ``horizon`` echoes the requested filter so the bot can render the
    active filter in the message header (``"Day Traders (≤24h)"`` /
    ``"Swing Traders (1d-3wk)"`` / ``"Position Traders (>3wk)"``) and
    decide which three of the four inline buttons to surface (the
    active one is hidden — it's already implicit in the header text).

    ``open_calls_total`` / ``open_callers_total`` are platform-wide
    activity counts (NOT horizon-filtered) — every claim whose
    ``resolves_at`` is still in the future, and the number of
    distinct sources behind those open calls. The bot renders them
    on the footer line ("22 open calls by 4 users are in progress.")
    so the leaderboard always carries a heartbeat even when the
    visible top-N is short. Both default to ``0`` so a stale client
    or empty cold-start payload renders sensibly.
    """

    entries: list[BoardEntry] = Field(default_factory=list)
    window: str = "all_time"
    horizon: HorizonFilter = "all"
    open_calls_total: int = Field(
        default=0,
        ge=0,
        description=(
            "Count of claims whose horizon is still in the future "
            "(``resolves_at > now()``), across all sources. Not "
            "horizon-filtered — the bot surfaces it as a platform "
            "activity stat regardless of the active /board slice."
        ),
    )
    open_callers_total: int = Field(
        default=0,
        ge=0,
        description=(
            "Count of distinct sources behind ``open_calls_total``. "
            "Always ``<= open_calls_total`` (one source can have "
            "many open calls). ``0`` iff ``open_calls_total == 0``."
        ),
    )


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

    ``accuracy_pct``, ``weighted_pnl_pct``, and ``rank`` are all
    ``Optional`` because a user with zero resolved calls has no honest
    value to report — defaulting to ``0.0`` / ``0`` would tell users
    "you're at zero" when the truthful answer is "we haven't checked
    anything yet". The bot renders these as "tracking your first
    resolutions" rather than a misleading percent.

    ``weighted_pnl_pct`` is conviction-weighted signed PnL: positive
    means the user's calls paid off on aggregate, negative means they
    lost. The bot renders it as the primary headline (PnL first, hit
    rate as a secondary number) matching the /board layout.
    """

    display_name: str | None = None
    calls_open: list[OpenCallEntry] = Field(default_factory=list)
    calls_resolved_recent: list[ResolvedCallEntry] = Field(default_factory=list)
    accuracy_pct: float | None = None
    weighted_pnl_pct: float | None = None
    calls_resolved_total: int = 0
    rank: int | None = None
