"""Leaderboard + ``/me`` wire schemas.

User-facing strings (display name, asset symbol, plain percent moves)
only. No internal scoring fields, no version markers — see bot-voice
rules in the bot-flow plan.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


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
    """One open call on ``/me`` — what the user is waiting on."""

    claim_id: UUID
    asset_symbol: str
    direction: str
    horizon: str
    resolves_at: datetime


class ResolvedCallEntry(BaseModel):
    """One resolved call on ``/me``."""

    claim_id: UUID
    asset_symbol: str
    direction: str
    horizon: str
    resolved_at: datetime
    observed_return_pct: float


class MeResponse(BaseModel):
    """``GET /me`` payload — open calls, resolved calls, headline stats.

    Headline stats are plain English / plain percent. No "regret",
    no "simulated PnL", no version markers.
    """

    display_name: str | None = None
    calls_open: list[OpenCallEntry] = Field(default_factory=list)
    calls_resolved_recent: list[ResolvedCallEntry] = Field(default_factory=list)
    accuracy_pct: float = 0.0
    calls_resolved_total: int = 0
    rank: int | None = None
